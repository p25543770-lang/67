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

test("depth palette spans the selected 10 m view without changing point geometry", () => {
  const positions = new Float32Array(6), colors = new Float32Array(6), lut = new Float32Array(768);
  for (let i = 0; i < 256; i++) lut[i * 3] = i;
  const points = [[1, 2, 3, 5], [4, 5, 6, 10], [7, 8, 9, 10.1]];
  assert.equal(buffers.fillPointBuffers(points, 10, "depth", positions, colors, lut), 2);
  assert.deepEqual(Array.from(positions), [1, 2, 3, 4, 5, 6]);
  assert.deepEqual([colors[0], colors[3]], [128, 255]);
  assert.deepEqual(points[1], [4, 5, 6, 10]);
});

test("2D preview rejects malformed and oversized image payloads", () => {
  const good = { width: 2, height: 1, data: Buffer.from([1, 2, 3, 4, 5, 6]).toString("base64") };
  assert.deepEqual(Array.from(pixels.decodePixelFrame(good, 3), c => c.charCodeAt(0)), [1, 2, 3, 4, 5, 6]);
  assert.equal(pixels.decodePixelFrame({ ...good, data: "?" }, 3), null);
  assert.equal(pixels.decodePixelFrame({ ...good, width: 50000 }, 3), null);
  assert.equal(pixels.decodePixelFrame({ ...good, data: "AA==" }, 3), null);
  assert.equal(pixels.decodePixelFrame(null, 3), null);
});


test("RGB mode renders actual channels with sRGB conversion, missing RGB is neutral", () => {
  const positions = new Float32Array(9), colors = new Float32Array(9), lut = new Float32Array(768);
  const points = [[1, 2, 3, 1, 255, 128, 0], [4, 5, 6, 1], [7, 8, 9, 1, -1, -1, -1]];
  const count = buffers.fillPointBuffers(points, 4, "rgb", positions, colors, lut);
  assert.equal(count, 3);
  assert.equal(colors[0], 1);
  assert.ok(Math.abs(colors[1] - 0.21586) < 0.0001);
  assert.equal(colors[2], 0);
  for (let i = 3; i < 9; i++) assert.ok(Math.abs(colors[i] - 0.22) < 1e-6);
  assert.deepEqual(points[0], [1, 2, 3, 1, 255, 128, 0]);
});
