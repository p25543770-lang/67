// Rates and speed are measured by the bridge, never inferred from the target FPS.
export function formatSpeed(speedMps) {
  return typeof speedMps === "number" && Number.isFinite(speedMps) && speedMps >= 0
    ? `${(speedMps * 3.6).toFixed(1)} км/ч`
    : "— км/ч";
}

export function formatAngularSpeed(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? `${value.toFixed(1)} °/с`
    : "— °/с";
}

export function formatFps(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value.toFixed(1)
    : "—";
}

export function formatFrameChecks(quality) {
  const count = (value) => Number.isSafeInteger(value) && value >= 0 ? String(value) : "—";
  return `${count(quality?.frameChecksPassed)} / ${count(quality?.frameChecks)}`;
}

export function formatCpu(cpu) {
  const logical = Number.isSafeInteger(cpu?.logicalCpus) && cpu.logicalCpus > 0 ? cpu.logicalCpus : "—";
  // OpenMP supports a comma-separated list for nested levels; show the
  // configured value rather than implying these are measured active threads.
  const omp = typeof cpu?.openmpThreads === "string" && cpu.openmpThreads.trim()
    ? cpu.openmpThreads.trim() : "—";
  return `${logical} / ${omp}`;
}
