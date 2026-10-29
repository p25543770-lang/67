// Unchanged live TSDF extractions need not be serialized or uploaded to the
// GPU again. A version is bound to a pass, extraction, point cap and color
// mode by the server. The front-camera view does not need the TSDF points.
export function mapRequestQuery(mode, version, versionMode, hasPoints) {
  if (mode === "front") return "&points=0";
  return hasPoints && versionMode === mode && typeof version === "string" && version
    ? `&pointsVersion=${encodeURIComponent(version)}` : "";
}

export function shouldApplyScanPoints(requestedMode, currentMode, points, phase) {
  return requestedMode === currentMode && requestedMode !== "front" && Array.isArray(points) &&
    (points.length > 0 || phase === "ready");
}
