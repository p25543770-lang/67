const test = require("node:test");
const assert = require("node:assert/strict");
let buffers, pixels;
test.before(async () => {
  buffers = await import("../../src/point-buffer.js");
  pixels = await import("../../src/pixel-frames.js");
});

test("reused point buffers include only visible, finite points in both formats", () => {
  const positions = new Float32Array(12).fill(99);
  const colors = new Float32Array(12).fill(99);
  const lut = new Float32Array(256 * 3).fill(0.5);
  const points = [[1, 2, 3, 1], [4, 5, 6, 5], [NaN, 1, 2, 1], [7, 8, 9, 2]];
  let count = buffers.fillPointBuffers(points, 4, "depth", positions, colors, lut);
  assert.equal(count, 2);
  assert.deepEqual(Array.from(positions.slice(0, count * 3)), [1, 2, 3, 7, 8, 9]);
  assert.deepEqual(Array.from(colors.slice(0, count * 3)), Array(6).fill(0.5));
  // Values beyond count may remain allocated but must not be drawn/exported.
  const packed = new Float32Array([9, 8, 7, 1, 6, 5, 4, 6]);
  count = buffers.fillPointBuffers(packed, 4, "mint", positions, colors, lut);
  assert.equal(count, 1);
  assert.deepEqual(Array.from(positions.slice(0, count * 3)), [9, 8, 7]);
  assert.ok(Math.abs(colors[0] - 0.27) < 1e-6);
  assert.ok(Math.abs(colors[1] - 0.86) < 1e-6);
});

test("2D preview rejects malformed and oversized image payloads", () => {
  const good = { width: 2, height: 1, data: Buffer.from([1, 2, 3, 4, 5, 6]).toString("base64") };
  assert.deepEqual(Array.from(pixels.decodePixelFrame(good, 3), c => c.charCodeAt(0)), [1, 2, 3, 4, 5, 6]);
  assert.equal(pixels.decodePixelFrame({ ...good, data: "?" }, 3), null);
  assert.equal(pixels.decodePixelFrame({ ...good, width: 50000 }, 3), null);
  assert.equal(pixels.decodePixelFrame({ ...good, data: "AA==" }, 3), null);
  assert.equal(pixels.decodePixelFrame(null, 3), null);
});
