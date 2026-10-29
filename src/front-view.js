// One depth frame in the camera coordinate system, NOT a map for driving.
// Zeros are unknown pixels, not free space. Kinect intrinsics are approximate.
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

// Count unique frame updates, not polls or the camera's advertised FPS. A
// duplicate can never extend the deadline previously assigned to that frame.
export class PreviewClock {
  constructor(windowMs = 3000) { this.windowMs = windowMs; this.reset(); }
  reset() { this.captureId = null; this.deadline = null; this.samples = []; }
  // Preserve the last ID after expiry or an interrupted response. Even if a
  // later poll gives a misleading age, that same frame cannot reappear.
  expire() { this.deadline = 0; this.samples = []; }
  observe(payload, started, now) {
    let deadline = frontViewDeadline(payload, started, now);
    const id = payload?.captureId;
    if (deadline === null || !Number.isFinite(id) || id <= 0 ||
        (this.captureId !== null && id < this.captureId)) return null;
    const freshFrame = this.captureId !== id;
    if (freshFrame) {
      this.captureId = id;
      this.samples.push(now);
    } else {
      if (this.deadline === null || this.deadline <= now) return null;
      deadline = Math.min(deadline, this.deadline);
    }
    this.deadline = deadline;
    this.samples = this.samples.filter((sample) => sample >= now - this.windowMs).slice(-120);
    const fps = this.samples.length >= 2 && now > this.samples[0]
      ? (this.samples.length - 1) * 1000 / (now - this.samples[0]) : null;
    if (deadline <= now) return null;
    return { deadline, freshFrame, fps };
  }
}

// Packed x,y,z,depth avoids allocating thousands of short JS arrays every
// preview frame. It is one frame only; no interpolation or old-depth blending.
export function frontDepthToPackedPoints(payload, stride = 1) {
  const image = payload?.depth, intr = payload?.intrinsics, sample = payload?.sampleStride;
  if (!payload?.available || payload.oneFrameOnly !== true || payload.notForNavigation !== true ||
      !image || !intr || !Number.isSafeInteger(image.width) || !Number.isSafeInteger(image.height) ||
      image.width <= 0 || image.height <= 0 || image.width > 640 || image.height > 480 ||
      !Number.isSafeInteger(sample) || sample < 1 || sample > 16 ||
      !Number.isSafeInteger(intr.width) || !Number.isSafeInteger(intr.height) ||
      image.width !== Math.ceil(intr.width / sample) || image.height !== Math.ceil(intr.height / sample) ||
      ![intr.fx, intr.fy, intr.cx, intr.cy].every(Number.isFinite) || intr.fx <= 0 || intr.fy <= 0 ||
      !Number.isSafeInteger(stride) || stride < 1 || stride > 16 || typeof image.data !== "string") return new Float32Array(0);
  const expected = image.width * image.height * 2;
  if (image.data.length > Math.ceil(expected / 3) * 4 + 4) return new Float32Array(0);
  let bytes;
  try { bytes = atob(image.data); } catch { return new Float32Array(0); }
  if (bytes.length !== expected) return new Float32Array(0);
  const output = new Float32Array(Math.ceil(image.width / stride) * Math.ceil(image.height / stride) * 4);
  let count = 0;
  for (let row = 0; row < image.height; row += stride) {
    for (let col = 0; col < image.width; col += stride) {
      const index = 2 * (row * image.width + col);
      const millimetres = bytes.charCodeAt(index) | (bytes.charCodeAt(index + 1) << 8);
      if (millimetres < 450 || millimetres > 4000) continue;
      const z = millimetres / 1000;
      output[count++] = (col * sample - intr.cx) * z / intr.fx;
      output[count++] = 1.25 - (row * sample - intr.cy) * z / intr.fy;
      output[count++] = -z;
      output[count++] = z;
    }
  }
  return output.subarray(0, count);
}

// Compatibility helper for consumers that need ordinary point arrays.
export function frontDepthToPoints(payload, stride = 2) {
  const packed = frontDepthToPackedPoints(payload, stride);
  const points = [];
  for (let i = 0; i < packed.length; i += 4) points.push(Array.from(packed.subarray(i, i + 4)));
  return points;
}
