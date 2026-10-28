"""Local Kinect v1 bridge and experimental point-cloud scanner."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

lock = threading.Lock()
state_lock = threading.Lock()
scan = {'running': False, 'cloud': None, 'last': None, 'pose': None, 'frames': 0, 'error': '', 'worker': None}


def read_cloud():
    import freenect
    import numpy as np
    with lock:
        result = freenect.sync_get_depth()
    if result is None or result[0] is None:
        raise RuntimeError('Kinect depth frame unavailable')
    depth = result[0][::4, ::4].astype(float)
    v, u = np.indices(depth.shape)
    u, v = u * 4, v * 4
    z = 0.1236 * np.tan(depth / 2842.5 + 1.1863)
    mask = (depth < 2047) & np.isfinite(z) & (z >= .5) & (z <= 4)
    x = (u - 319.5) * z / 594.2
    y = -(v - 239.5) * z / 591.0
    return np.column_stack((x[mask], y[mask], -z[mask]))


def scan_worker():
    try:
        import open3d as o3d
        import numpy as np
        while True:
            with state_lock:
                if not scan['running']:
                    break
            xyz = read_cloud()
            if len(xyz) < 100:
                raise RuntimeError('Недостаточно точек глубины. Проверьте датчик.')
            cloud = o3d.geometry.PointCloud()
            cloud.points = o3d.utility.Vector3dVector(xyz)
            cloud = cloud.voxel_down_sample(.035)
            with state_lock:
                last, pose = scan['last'], scan['pose']
            if last is None:
                pose = np.eye(4)
                aligned = cloud
            else:
                # Pairwise ICP maps the new camera frame to the preceding frame.
                result = o3d.pipelines.registration.registration_icp(
                    cloud, last, .15, np.eye(4),
                    o3d.pipelines.registration.TransformationEstimationPointToPoint(),
                    o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=35))
                if result.fitness < .3:
                    with state_lock:
                        scan['error'] = 'Не удалось совместить кадр. Двигайте датчик медленнее.'
                    time.sleep(.15)
                    continue
                pose = pose @ result.transformation
                aligned = o3d.geometry.PointCloud(cloud)
                aligned.transform(pose)
            with state_lock:
                if not scan['running']:
                    break
                scan['cloud'] = aligned if scan['cloud'] is None else (scan['cloud'] + aligned).voxel_down_sample(.025)
                scan['last'], scan['pose'] = cloud, pose
                scan['frames'] += 1
                scan['error'] = ''
            time.sleep(.1)
    except Exception as exc:
        with state_lock:
            scan['error'] = str(exc)
            scan['running'] = False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == '/api/frame':
                xyz = read_cloud()
                self.respond(200, {'points': [[round(float(x), 3), round(float(y + 1.25), 3), round(float(z), 3), round(float(-z), 3)] for x, y, z in xyz]})
            elif self.path == '/api/scan':
                import numpy as np
                with state_lock:
                    cloud = scan['cloud']
                    xyz = np.asarray(cloud.points).copy() if cloud is not None else np.empty((0, 3))
                    running, frames, error = scan['running'], scan['frames'], scan['error']
                # Cap preview payload; export always retains the full cloud.
                xyz = xyz[::max(1, len(xyz) // 50000)]
                self.respond(200, {'running': running, 'frames': frames, 'error': error, 'points': [[round(float(x), 3), round(float(y + 1.25), 3), round(float(z), 3), 2.0] for x, y, z in xyz]})
            elif self.path == '/api/export':
                with state_lock:
                    cloud = scan['cloud']
                    if cloud is None:
                        raise RuntimeError('Сначала выполните сканирование')
                    import numpy as np
                    xyz = np.asarray(cloud.points).copy()
                body = ('ply\nformat ascii 1.0\nelement vertex %d\nproperty float x\nproperty float y\nproperty float z\nend_header\n' % len(xyz) + ''.join('%f %f %f\n' % tuple(p) for p in xyz)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition', 'attachment; filename="scan.ply"')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        except Exception as exc:
            self.respond(503, {'error': str(exc)})

    def do_POST(self):
        try:
            if self.path == '/api/scan/start':
                import open3d
                import freenect
                with state_lock:
                    if scan['running'] or (scan['worker'] and scan['worker'].is_alive()):
                        raise RuntimeError('Предыдущее сканирование ещё останавливается')
                    scan.update(running=True, cloud=None, last=None, pose=None, frames=0, error='')
                    worker = threading.Thread(target=scan_worker, daemon=True)
                    scan['worker'] = worker
                    worker.start()
                self.respond(200, {'ok': True})
            elif self.path == '/api/scan/stop':
                with state_lock:
                    scan['running'] = False
                self.respond(200, {'ok': True})
            else:
                self.send_error(404)
        except Exception as exc:
            self.respond(503, {'error': str(exc)})

    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == '__main__':
    print('Kinect bridge listening on http://127.0.0.1:8765', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
