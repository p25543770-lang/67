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

test("FPS displays measured capture and processing values, not the target", () => {
  assert.equal(metrics.formatFps(27.63), "27.6");
  assert.equal(metrics.formatFps(0), "0.0");
  assert.equal(metrics.formatFps(null), "—");
  assert.equal(metrics.formatFps(Infinity), "—");
});
