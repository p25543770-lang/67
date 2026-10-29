// A single depth frame in the camera coordinate system, NOT a map for driving.
// Zeros are unknown pixels, not free space. The Kinect intrinsics are approximate.
export function isPoseFresh(payload, requestStartedMs, nowMs) {
  return payload?.poseFresh === true && Number.isFinite(payload.poseAgeMs) && payload.poseAgeMs >= 0 &&
    Number.isFinite(requestStartedMs) && Number.isFinite(nowMs) && nowMs >= requestStartedMs &&
    payload.poseAgeMs + nowMs - requestStartedMs <= 250;
}

export function frontViewDeadline(payload, requestStartedMs, nowMs) {
  if (!payload?.available || payload.oneFrameOnly !== true || payload.notForNavigation !== true ||
      !Number.isFinite(payload.captureAgeMs) || payload.captureAgeMs < 0 ||
      !Number.isFinite(payload.maxAgeMs) || payload.maxAgeMs <= 0 ||
      !Number.isFinite(requestStartedMs) || !Number.isFinite(nowMs) || nowMs < requestStartedMs) return null;
  // Subtract the entire request duration (conservative: the server age was
  // measured before the response reached this process). Never trust a server
  // that asks the viewer to show a frame for longer than 250 ms.
  const remaining = Math.min(payload.maxAgeMs, 250) - payload.captureAgeMs - (nowMs - requestStartedMs);
  return remaining > 0 ? nowMs + remaining : null;
}

export function frontDepthToPoints(payload, stride = 2) {
  const image = payload?.depth, intr = payload?.intrinsics, sample = payload?.sampleStride;
  if (!payload?.available || payload.oneFrameOnly !== true || payload.notForNavigation !== true ||
      !image || !intr || !Number.isSafeInteger(image.width) || !Number.isSafeInteger(image.height) ||
      image.width <= 0 || image.height <= 0 || image.width > 640 || image.height > 480 ||
      !Number.isSafeInteger(sample) || sample < 1 || sample > 16 ||
      !Number.isSafeInteger(intr.width) || !Number.isSafeInteger(intr.height) ||
      image.width !== Math.ceil(intr.width / sample) || image.height !== Math.ceil(intr.height / sample) ||
      ![intr.fx, intr.fy, intr.cx, intr.cy].every(Number.isFinite) || intr.fx <= 0 || intr.fy <= 0 ||
      !Number.isSafeInteger(stride) || stride < 1 || stride > 16 || typeof image.data !== "string") return [];
  const expected = image.width * image.height * 2;
  if (image.data.length > Math.ceil(expected / 3) * 4 + 4) return [];
  let bytes;
  try { bytes = atob(image.data); } catch { return []; }
  if (bytes.length !== expected) return [];
  const points = [];
  for (let row = 0; row < image.height; row += stride) {
    for (let col = 0; col < image.width; col += stride) {
      const index = 2 * (row * image.width + col);
      const millimetres = bytes.charCodeAt(index) | (bytes.charCodeAt(index + 1) << 8);
      if (millimetres < 450 || millimetres > 4000) continue;
      const z = millimetres / 1000;
      const x = (col * sample - intr.cx) * z / intr.fx;
      const y = (row * sample - intr.cy) * z / intr.fy;
      // Display convention: camera looks toward -Z, +Y is up. The 1.25 m
      // offset is only a visual origin, not a calibrated robot/camera height.
      points.push([x, 1.25 - y, -z, z]);
    }
  }
  return points;
}
