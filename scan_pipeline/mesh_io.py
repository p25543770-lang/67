"""Экспорт меша: PLY (binary LE), OBJ (вершинный цвет) и GLB (glTF 2.0 binary).

Все три формата содержат грани, нормали и цвет вершин, а метрики качества — в
комментариях (PLY, OBJ) или в ``asset.extras`` (GLB). Координаты экспортируются в
соглашении приложения: +Y вверх, камера смотрит вдоль -Z, то есть (x, -y, -z).
Писатели написаны на NumPy: сторонних библиотек для экспорта не требуется.
"""
from __future__ import annotations

import json
import struct

import numpy as np

EXPORT_FLIP = np.array([1.0, -1.0, -1.0])


def to_export_frame(vertices, normals):
    v = np.asarray(vertices, dtype=np.float64) * EXPORT_FLIP
    n = np.asarray(normals, dtype=np.float64) * EXPORT_FLIP
    return v, n


def _colors_u8(colors) -> np.ndarray:
    return np.clip(np.round(np.asarray(colors, dtype=np.float64)), 0, 255).astype(np.uint8)


def _metric_lines(metrics: dict) -> list[str]:
    flat = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
    return [line for line in flat.replace("\r", " ").split("\n") if line]


def write_ply(vertices, normals, colors, faces, metrics: dict | None = None) -> bytes:
    v, n = to_export_frame(vertices, normals)
    c = _colors_u8(colors)
    f = np.asarray(faces, dtype=np.int32)
    header = [
        "ply",
        "format binary_little_endian 1.0",
        "comment Depth Studio scan mesh; coordinates +Y up, camera looks along -Z",
        "comment verified_on_hardware false",
    ]
    if metrics is not None:
        # Метрики в комментариях: один JSON на строку (ограничение формата PLY).
        header.append("comment metrics " + json.dumps(_compact(metrics), ensure_ascii=False))
    header += [
        f"element vertex {len(v)}",
        "property float x", "property float y", "property float z",
        "property float nx", "property float ny", "property float nz",
        "property uchar red", "property uchar green", "property uchar blue",
        f"element face {len(f)}",
        "property list uchar int vertex_indices",
        "end_header",
    ]
    vertex_dtype = np.dtype([("xyz", "<f4", 3), ("n", "<f4", 3), ("rgb", "u1", 3)])
    vertex_block = np.empty(len(v), dtype=vertex_dtype)
    vertex_block["xyz"] = v
    vertex_block["n"] = n
    vertex_block["rgb"] = c
    face_dtype = np.dtype([("count", "u1"), ("idx", "<i4", 3)])
    face_block = np.empty(len(f), dtype=face_dtype)
    face_block["count"] = 3
    face_block["idx"] = f
    body = vertex_block.tobytes() + face_block.tobytes()
    return ("\n".join(header) + "\n").encode("ascii") + body


def _compact(metrics: dict) -> dict:
    return json.loads(json.dumps(metrics, ensure_ascii=False, default=str))


def write_obj(vertices, normals, colors, faces, metrics: dict | None = None) -> bytes:
    v, n = to_export_frame(vertices, normals)
    c = _colors_u8(colors).astype(np.float64) / 255.0
    f = np.asarray(faces, dtype=np.int64) + 1
    lines = [
        "# Depth Studio scan mesh (Wavefront OBJ)",
        "# verified_on_hardware false; координаты: +Y вверх, камера смотрит вдоль -Z",
        "# вершинный цвет записан расширением «v x y z r g b» (поддерживают MeshLab, Blender)",
    ]
    if metrics is not None:
        lines += ["# metrics " + line for line in _metric_lines(_compact(metrics))]
    lines.append("o DepthStudioScan")
    lines += ["v %.6f %.6f %.6f %.4f %.4f %.4f" % (*p, *col) for p, col in zip(v, c)]
    lines += ["vn %.5f %.5f %.5f" % tuple(row) for row in n]
    lines += ["f %d//%d %d//%d %d//%d" % (a, a, b, b, d, d) for a, b, d in f]
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_glb(vertices, normals, colors, faces, metrics: dict | None = None) -> bytes:
    """glTF 2.0 binary: POSITION (float), NORMAL (float), COLOR_0 (u8 normalized), indices (u32)."""
    v, n = to_export_frame(vertices, normals)
    v = np.asarray(v, dtype="<f4")
    n = np.asarray(n, dtype="<f4")
    c = _colors_u8(colors)
    idx = np.asarray(faces, dtype="<u4").reshape(-1)
    buffers = []  # (bytes, byteLength, offset)
    views, accessors = [], []
    blob = bytearray()

    def add(data: bytes, target: int | None) -> int:
        while len(blob) % 4:
            blob.append(0)
        offset = len(blob)
        blob.extend(data)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        views.append(view)
        return len(views) - 1

    def accessor(view_index, component, count, kind, minmax=None, normalized=False):
        acc = {"bufferView": view_index, "componentType": component, "count": count, "type": kind}
        if normalized:
            acc["normalized"] = True
        if minmax is not None:
            acc["min"], acc["max"] = minmax
        accessors.append(acc)
        return len(accessors) - 1

    pos_view = add(v.tobytes(), 34962)
    pos = accessor(pos_view, 5126, len(v), "VEC3",
                   (v.min(axis=0).tolist(), v.max(axis=0).tolist()) if len(v) else ([0, 0, 0], [0, 0, 0]))
    nrm_view = add(n.tobytes(), 34962)
    nrm = accessor(nrm_view, 5126, len(n), "VEC3")
    col_view = add(c.tobytes(), 34962)
    col = accessor(col_view, 5121, len(c), "VEC3", normalized=True)
    idx_view = add(idx.tobytes(), 34963)
    ind = accessor(idx_view, 5125, len(idx), "SCALAR")

    extras = {"verified_on_hardware": False,
              "coordinates": "+Y up, camera looks along -Z",
              "vertex_colors": "COLOR_0 (вершинная текстура)"}
    if metrics is not None:
        extras["metrics"] = _compact(metrics)
    gltf = {
        "asset": {"version": "2.0", "generator": "Depth Studio scan_pipeline", "extras": extras},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "DepthStudioScan"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": pos, "NORMAL": nrm, "COLOR_0": col},
                                    "indices": ind, "mode": 4}]}],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
    json_bytes = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
    bin_bytes = bytes(blob) + b"\0" * ((4 - len(blob) % 4) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    out = struct.pack("<III", 0x46546C67, 2, total)
    out += struct.pack("<I4s", len(json_bytes), b"JSON") + json_bytes
    out += struct.pack("<I4s", len(bin_bytes), b"BIN\0") + bin_bytes
    return out


def read_glb(data: bytes) -> dict:
    """Разбор GLB для проверки (тесты и самопроверка): заголовок, JSON, буфер."""
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67 or version != 2 or length != len(data):
        raise ValueError("некорректный заголовок GLB")
    offset = 12
    chunks = {}
    while offset < len(data):
        chunk_length, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        chunks[chunk_type] = data[offset:offset + chunk_length]
        offset += chunk_length
    gltf = json.loads(chunks[b"JSON"].decode("utf-8"))
    return {"gltf": gltf, "bin": chunks.get(b"BIN\0", b"")}


def read_ply_header(data: bytes) -> dict:
    end = data.index(b"end_header\n") + len("end_header\n")
    header = data[:end].decode("ascii").splitlines()
    result = {"elements": {}, "comments": [l[len("comment "):] for l in header if l.startswith("comment ")]}
    current = None
    for line in header:
        parts = line.split()
        if parts[:1] == ["element"]:
            current = parts[1]
            result["elements"][current] = int(parts[2])
    result["format"] = next(l for l in header if l.startswith("format"))
    return result
