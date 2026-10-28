// Rates and speed are measured by the bridge, never inferred from the target FPS.
export function formatSpeed(speedMps) {
  return typeof speedMps === "number" && Number.isFinite(speedMps) && speedMps >= 0
    ? `${(speedMps * 3.6).toFixed(1)} км/ч`
    : "— км/ч";
}

export function formatFps(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value.toFixed(1)
    : "—";
}
