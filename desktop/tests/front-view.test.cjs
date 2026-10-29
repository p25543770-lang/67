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
