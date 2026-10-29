// Mirror only the on-screen map image, independent of camera orbit. Keep the
// world geometry, preview feeds, front view, tracking and exports untouched.
export function updateMapMirror(host, viewMode) {
  host.classList.toggle("map-mirrored", viewMode === "map");
}
