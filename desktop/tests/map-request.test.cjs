const test = require("node:test");
const assert = require("node:assert/strict");
let mapRequestQuery, shouldApplyScanPoints;
test.before(async () => ({ mapRequestQuery, shouldApplyScanPoints } = await import("../../src/map-request.js")));

test("request only changed map geometry and never include it in front view", () => {
  const token = "pass:4:14000:rgb";
  assert.equal(mapRequestQuery("front", token, "color", true), "&points=0");
  assert.equal(mapRequestQuery("color", token, "color", true), "&pointsVersion=pass%3A4%3A14000%3Argb");
  assert.equal(mapRequestQuery("map", token, "color", true), "", "new color mode requires points");
  assert.equal(mapRequestQuery("color", token, "color", false), "", "cleared display requires points");
  assert.equal(mapRequestQuery("color", null, "color", true), "");
});

test("ignore unchanged, empty-live, front and out-of-order mode responses", () => {
  assert.equal(shouldApplyScanPoints("map", "map", null, "capturing"), false);
  assert.equal(shouldApplyScanPoints("map", "map", [], "capturing"), false);
  assert.equal(shouldApplyScanPoints("map", "map", [], "ready"), true);
  assert.equal(shouldApplyScanPoints("front", "front", [[1, 2, 3, 4]], "capturing"), false);
  assert.equal(shouldApplyScanPoints("map", "color", [[1, 2, 3, 4]], "capturing"), false);
  assert.equal(shouldApplyScanPoints("color", "color", [[1, 2, 3, 4, 5, 6, 7]], "capturing"), true);
});
