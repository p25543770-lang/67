const test = require("node:test");
const assert = require("node:assert/strict");
let tiltAvailability;
test.before(async () => { ({ tiltAvailability } = await import("../../src/motor-mode.js")); });

function ready() {
  return { visible: true, environmentReady: true, sdkSource: true, connected: true,
    scanInProgress: false, deviceBusy: false, scanBusy: false, motorBusy: false,
    queryBusy: false, angleKnown: true, cooldownMs: 0 };
}

test("motor controls require connected SDK, healthy environment and no scan", () => {
  assert.deepEqual(tiltAvailability(ready()), { read: true, select: true, apply: true });
  for (const field of ["visible", "environmentReady", "sdkSource", "connected"]) {
    assert.deepEqual(tiltAvailability({ ...ready(), [field]: false }), { read: false, select: false, apply: false }, field);
  }
  for (const field of ["scanInProgress", "deviceBusy", "scanBusy", "motorBusy"]) {
    assert.deepEqual(tiltAvailability({ ...ready(), [field]: true }), { read: false, select: false, apply: false }, field);
  }
});

test("unknown angle or cooldown forbids applying, but not reading", () => {
  assert.deepEqual(tiltAvailability({ ...ready(), angleKnown: false }), { read: true, select: false, apply: false });
  assert.deepEqual(tiltAvailability({ ...ready(), cooldownMs: 1 }), { read: true, select: true, apply: false });
  assert.deepEqual(tiltAvailability({ ...ready(), queryBusy: true }), { read: false, select: true, apply: false });
  assert.equal(tiltAvailability({ ...ready(), cooldownMs: NaN }).apply, false);
});
