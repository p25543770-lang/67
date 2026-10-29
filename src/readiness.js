// Pure UI state rules shared by the renderer and the desktop tests.
// The synthetic demo deliberately does not depend on Python or a sensor.
export function environmentStatus(setup, health) {
  if (setup && setup.phase !== "ready") {
    return { ready: false, reason: "Дождитесь завершения настройки среды Depth Studio." };
  }
  if (setup && !setup.bridge?.ready) {
    return { ready: false, reason: setup.bridge?.error || "Дождитесь ответа Python-моста." };
  }
  if (health?.ok !== true) {
    return { ready: false, reason: "Python-мост ещё не ответил или недоступен." };
  }
  if (setup?.packages?.open3d?.available === false || health.modules?.open3d !== true) {
    return { ready: false, reason: "Open3D недоступен: проверьте среду Depth Studio." };
  }
  if (setup?.packages?.numpy?.available === false || health.modules?.numpy !== true) {
    return { ready: false, reason: "NumPy недоступен: восстановите среду Depth Studio." };
  }
  return { ready: true, reason: "" };
}

export function setupHeadline(setup, health) {
  if (setup.phase === "ready") {
    if (!setup.bridge?.ready || health?.ok !== true) {
      return setup.bridge?.error ? "Python-мост недоступен" : "Ожидание Python-моста Depth Studio";
    }
    const environment = environmentStatus(setup, health);
    return environment.ready ? "Среда Depth Studio готова" : `Среда не готова: ${environment.reason}`;
  }
  if (setup.phase === "failed") return "Настройка среды не завершена — доступен деморежим";
  if (setup.phase === "running") return "Настройка среды выполняется…";
  return "Среда Depth Studio ещё не настроена";
}

export function isScanInProgress(phase, running) {
  return Boolean(running) || ["starting", "stopping", "finalizing", "merging"].includes(phase);
}

export function scanActions({ source, connected, setup, health, phase = "idle", running = false,
                              hasResult = false, hasVisibleData = false, busy = false }) {
  if (source === "demo") {
    return {
      start: !busy,
      clear: !busy && (hasVisibleData || running),
      export: !busy && hasVisibleData,
      cancel: false,
      reason: "",
    };
  }
  const environment = environmentStatus(setup, health);
  const processing = phase === "finalizing" || phase === "merging";
  let reason = environment.reason;
  if (environment.ready && !connected) reason = "Подключите выбранный источник глубины.";
  else if (environment.ready && connected && processing) {
    reason = phase === "merging" ? "Дождитесь объединения проходов." : "Дождитесь обработки прохода.";
  } else if (environment.ready && connected && !running && !hasResult) {
    reason = "Для очистки или экспорта завершите сканирование либо откройте сохранённый проход.";
  }
  const available = environment.ready && connected && !processing && !busy;
  return {
    // Never lock the user out of stopping/cancelling an active capture if the
    // bridge or sensor becomes unavailable while it is running.
    start: running ? !busy : available,
    cancel: running && !busy,
    clear: available && !running && hasResult,
    export: available && !running && phase === "ready" && hasResult,
    reason: running ? "" : reason,
  };
}
