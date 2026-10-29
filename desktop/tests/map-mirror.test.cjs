const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

test("only the map canvas is horizontally mirrored; point data stay unchanged", async () => {
  const { updateMapMirror } = await import("../../src/map-mirror.js");
  const classes = new Set();
  const host = { classList: { toggle(name, value) { value ? classes.add(name) : classes.delete(name); } } };
  const points = [[2, 3, -4, 1]];
  updateMapMirror(host, "map");
  assert.equal(classes.has("map-mirrored"), true);
  assert.deepEqual(points, [[2, 3, -4, 1]]);
  updateMapMirror(host, "front");
  assert.equal(classes.has("map-mirrored"), false);
  updateMapMirror(host, "map");
  assert.equal(classes.has("map-mirrored"), true);
  const css = fs.readFileSync(path.join(__dirname, "../../src/style.css"), "utf8");
  assert.match(css, /\.scene-canvas\.map-mirrored\s+canvas\s*\{\s*transform:\s*scaleX\(-1\)/);
});
