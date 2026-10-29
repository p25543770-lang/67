// Populate reusable GPU buffers for either nested [x,y,z,depth] points or a
// packed Float32Array. No temporal blending: one raw front frame stays one frame.
export function fillPointBuffers(points, maxDepth, palette, positions, colors, depthLut) {
  const packed = points instanceof Float32Array;
  const length = packed ? Math.floor(points.length / 4) : points.length;
  let count = 0;
  for (let i = 0; i < length; i++) {
    const point = packed ? null : points[i];
    if (!packed && !Array.isArray(point)) continue;
    const x = packed ? points[i * 4] : point[0];
    const y = packed ? points[i * 4 + 1] : point[1];
    const z = packed ? points[i * 4 + 2] : point[2];
    const depth = packed ? points[i * 4 + 3] : (point[3] ?? 2);
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z) ||
        !Number.isFinite(depth) || depth > maxDepth) continue;
    const offset = count * 3;
    positions[offset] = x; positions[offset + 1] = y; positions[offset + 2] = z;
    if (palette === "mono") {
      colors[offset] = 0.74; colors[offset + 1] = 0.83; colors[offset + 2] = 0.78;
    } else if (palette === "mint") {
      colors[offset] = 0.27; colors[offset + 1] = 0.86; colors[offset + 2] = 0.63;
    } else {
      const idx = Math.round(Math.min(Math.max(depth / 5, 0), 1) * 255) * 3;
      colors[offset] = depthLut[idx]; colors[offset + 1] = depthLut[idx + 1]; colors[offset + 2] = depthLut[idx + 2];
    }
    count++;
  }
  return count;
}
