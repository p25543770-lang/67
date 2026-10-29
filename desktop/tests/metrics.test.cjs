const test = require("node:test");
const assert = require("node:assert/strict");

let metrics;
test.before(async () => { metrics = await import("../../src/metrics.js"); });

test("speed uses the measured metres per second and shows unknown honestly", () => {
  assert.equal(metrics.formatSpeed(7 / 3.6), "7.0 км/ч");
  assert.equal(metrics.formatSpeed(0), "0.0 км/ч");
  assert.equal(metrics.formatSpeed(null), "— км/ч");
  assert.equal(metrics.formatSpeed(NaN), "— км/ч");
});

test("angular speed is an estimated total camera rotation, not an IMU value", () => {
  assert.equal(metrics.formatAngularSpeed(20.16), "20.2 °/с");
  assert.equal(metrics.formatAngularSpeed(0), "0.0 °/с");
  assert.equal(metrics.formatAngularSpeed(null), "— °/с");
  assert.equal(metrics.formatAngularSpeed(-1), "— °/с");
});

test("FPS displays measured capture and processing values, not the target", () => {
  assert.equal(metrics.formatFps(27.63), "27.6");
  assert.equal(metrics.formatFps(0), "0.0");
  assert.equal(metrics.formatFps(null), "—");
  assert.equal(metrics.formatFps(Infinity), "—");
});

test("corroboration counters and configured OpenMP threads are honest and resettable", () => {
  assert.equal(metrics.formatFrameChecks({ frameChecksPassed: 3, frameChecks: 5 }), "3 / 5");
  assert.equal(metrics.formatFrameChecks({ frameChecksPassed: -1, frameChecks: Infinity }), "— / —");
  assert.equal(metrics.formatFrameChecks(null), "— / —");
  assert.equal(metrics.formatLandmarkChecks({ landmarkChecks: 12, landmarkVetoes: 2 }), "12 / 2");
  assert.equal(metrics.formatLandmarkChecks(null), "— / —");
  assert.equal(metrics.formatCpu({ logicalCpus: 16, openmpThreads: "4" }), "16 / 4");
  assert.equal(metrics.formatCpu({ logicalCpus: 8, openmpThreads: "4,2" }), "8 / 4,2");
  assert.equal(metrics.formatCpu(null), "— / —");
});
