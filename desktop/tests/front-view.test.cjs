const test = require("node:test");
const assert = require("node:assert/strict");
let front;
test.before(async () => { front = await import("../../src/front-view.js"); });

function frame() {
  // 4x4 source sampled every 2 px -> 2x2 uint16 little-endian depths.
  const raw = Buffer.alloc(8);
  [1000, 2000, 0, 4000].forEach((value, i) => raw.writeUInt16LE(value, i * 2));
  return {
    available: true, oneFrameOnly: true, notForNavigation: true,
    captureAgeMs: 20, maxAgeMs: 250, sampleStride: 2,
    intrinsics: { width: 4, height: 4, fx: 2, fy: 2, cx: 1, cy: 1 },
    depth: { width: 2, height: 2, data: raw.toString("base64") },
  };
}

test("camera projection uses source pixel spacing and omits unknown depth", () => {
  const points = front.frontDepthToPoints(frame(), 1);
  assert.deepEqual(points, [
    [-0.5, 1.75, -1, 1],
    [1, 2.25, -2, 2],
    [2, -0.75, -4, 4],
  ]);
});

test("front preview permits an explicit limit through 10 m but rejects excess", () => {
  const data = frame();
  const raw = Buffer.alloc(8);
  [450, 5000, 10000, 10001].forEach((value, i) => raw.writeUInt16LE(value, i * 2));
  data.depth.data = raw.toString("base64");
  assert.equal(front.frontDepthToPackedPoints(data).length, 4, "old server defaults to 4 m");
  assert.equal(front.frontDepthToPackedPoints({ ...data, maxDepthMm: 10000 }).length, 12);
  assert.equal(front.frontDepthToPackedPoints({ ...data, maxDepthMm: 5000 }).length, 8);
  for (const invalid of [10001, 450, -1, NaN, "10000"]) {
    assert.equal(front.frontDepthToPackedPoints({ ...data, maxDepthMm: invalid }).length, 0);
  }
});

test("bad, incomplete or unsafe frame payloads never render", () => {
  assert.deepEqual(front.frontDepthToPoints({ ...frame(), available: false }), []);
  assert.deepEqual(front.frontDepthToPoints({ ...frame(), notForNavigation: false }), []);
  const bad = frame(); bad.depth.data = Buffer.alloc(3).toString("base64");
  assert.deepEqual(front.frontDepthToPoints(bad), []);
  const wrong = frame(); wrong.depth.width = 100;
  assert.deepEqual(front.frontDepthToPoints(wrong), []);
  const badCamera = frame(); badCamera.intrinsics.fx = 0;
  assert.deepEqual(front.frontDepthToPoints(badCamera), []);
});

test("packed front preview uses every sampled pixel without blending frames", () => {
  const data = frame();
  const packed = front.frontDepthToPackedPoints(data);
  assert.ok(packed instanceof Float32Array);
  assert.equal(packed.length, 12); // three valid points, four floats each
  assert.deepEqual(Array.from(packed), [-0.5, 1.75, -1, 1, 1, 2.25, -2, 2, 2, -0.75, -4, 4]);
  assert.equal(front.frontDepthToPackedPoints({ ...data, available: false }).length, 0);
});

test("preview rate counts unique frames and a duplicate cannot extend its deadline", () => {
  const clock = new front.PreviewClock();
  let data = { ...frame(), captureId: 10, captureAgeMs: 20 };
  let result = clock.observe(data, 1000, 1010);
  assert.equal(result.freshFrame, true);
  assert.equal(result.fps, null);
  assert.equal(result.deadline, 1230);
  result = clock.observe({ ...data, captureAgeMs: 0 }, 1040, 1050);
  assert.equal(result.freshFrame, false);
  assert.equal(result.deadline, 1230); // cannot renew the same captured frame
  data = { ...data, captureId: 11 };
  result = clock.observe(data, 1050, 1060);
  assert.equal(result.freshFrame, true);
  assert.equal(result.fps, 20);
  assert.equal(clock.observe({ ...data, captureId: 9 }, 1060, 1070), null);
  assert.equal(clock.observe({ ...data, captureId: null }, 1060, 1070), null);
  assert.equal(clock.observe({ ...data, captureAgeMs: 250 }, 1060, 1070), null);
  clock.expire();
  assert.equal(clock.observe({ ...data, captureAgeMs: 0 }, 1080, 1090), null,
    "an expired/invalidated frame cannot reappear even if the server advertises a lower age");
  assert.equal(clock.observe({ ...data, captureId: 12 }, 1090, 1100).freshFrame, true);
  clock.reset();
  assert.equal(clock.observe({ ...data, captureId: 9 }, 1080, 1090).fps, null);
});

test("current pose status expires conservatively, including request time", () => {
  assert.equal(front.isPoseFresh({ poseFresh: true, poseAgeMs: 100 }, 1000, 1140), true);
  assert.equal(front.isPoseFresh({ poseFresh: true, poseAgeMs: 100 }, 1000, 1151), false);
  assert.equal(front.isPoseFresh({ poseFresh: true, poseAgeMs: null }, 1000, 1001), false);
  assert.equal(front.isPoseFresh({ poseFresh: false, poseAgeMs: 10 }, 1000, 1001), false);
  assert.equal(front.isPoseFresh({ poseFresh: true, poseAgeMs: 0 }, 1001, 1000), false);
});

test("client expiry accounts for server age and the whole request time", () => {
  const data = frame();
  assert.equal(front.frontViewDeadline(data, 1000, 1040), 1230);
  assert.equal(front.frontViewDeadline(data, 1000, 1230), null);
  assert.equal(front.frontViewDeadline({ ...data, captureAgeMs: null }, 1000, 1001), null);
  assert.equal(front.frontViewDeadline({ ...data, available: false }, 1000, 1001), null);
  // A permissive server can't make the viewer retain a frame for >250ms.
  assert.equal(front.frontViewDeadline({ ...data, maxAgeMs: 9000 }, 1000, 1040), 1230);
});
