// Чистые помощники стереорежима. Здесь нет DOM и сетевых вызовов: их удобно
// проверять в node:test, а main.js отвечает только за разметку и опрос моста.

export const VIEW_MODES = ["map", "front", "color", "stereo"];

export function normalizeViewMode(mode) {
  return VIEW_MODES.includes(mode) ? mode : "map";
}

const CALIBRATION_STATES = {
  valid: { title: "Калибровка действительна", tone: "ok" },
  missing: { title: "Калибровки нет", tone: "warn" },
  invalid: { title: "Калибровка недействительна", tone: "bad" },
  camera_mismatch: { title: "Другие камеры или разрешение", tone: "bad" },
  tilt_unknown: { title: "Положение мотора Kinect неизвестно", tone: "warn" },
  camera_unavailable: { title: "Камера недоступна", tone: "bad" },
};

export function calibrationStateLabel(state) {
  return CALIBRATION_STATES[state] || { title: "Состояние калибровки неизвестно", tone: "warn" };
}

/** base64 (uint16 little-endian, мм) → типизированный массив. */
export function decodeDepthFrame(payload) {
  if (!payload || typeof payload.data !== "string" || !payload.data) return null;
  const width = Number(payload.width);
  const height = Number(payload.height);
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) return null;
  let binary;
  try {
    binary = atob(payload.data);
  } catch {
    return null;
  }
  if (binary.length !== width * height * 2) return null;
  const data = new Uint16Array(width * height);
  for (let index = 0; index < data.length; index += 1) {
    data[index] = binary.charCodeAt(index * 2) | (binary.charCodeAt(index * 2 + 1) << 8);
  }
  return { width, height, data };
}

/**
 * Глубина → RGBA. Ноль означает «неизвестно» и остаётся прозрачным, а не
 * подкрашивается: сглаживание и заливка дыр запрещены требованиями режима.
 */
export function depthToRgba(depth, { minMm = 500, maxMm = 4000 } = {}) {
  if (!depth || !depth.data) return null;
  const low = Math.min(minMm, maxMm);
  const high = Math.max(minMm, maxMm);
  const span = Math.max(1, high - low);
  const rgba = new Uint8ClampedArray(depth.width * depth.height * 4);
  for (let index = 0; index < depth.data.length; index += 1) {
    const value = depth.data[index];
    const offset = index * 4;
    if (!value) continue; // неизвестная глубина не окрашивается вообще
    const clamped = Math.min(high, Math.max(low, value));
    const ratio = (clamped - low) / span;
    rgba[offset] = Math.round(255 * Math.min(1, Math.max(0, 1.55 - ratio * 1.55)));
    rgba[offset + 1] = Math.round(255 * Math.min(1, Math.max(0, 0.35 + ratio * 0.95)));
    rgba[offset + 2] = Math.round(255 * Math.min(1, Math.max(0, ratio * 1.6 - 0.15)));
    rgba[offset + 3] = 255;
  }
  return { width: depth.width, height: depth.height, data: rgba };
}

export function formatMs(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(2)} с`;
  return `${value.toFixed(value < 100 ? 1 : 0)} мс`;
}

export function formatPercent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(value < 0.1 ? 1 : 0)} %`;
}

export function formatDepthMm(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} м` : `${Math.round(value)} мм`;
}

/**
 * Источник правды для панели — ответ /api/stereo, а не отдельные флаги
 * приложения. Если сессия активна, Kinect уже подключён (он в ней работает),
 * даже если верхний статус датчика успел сброситься после таймаута моста.
 */
export function kinectSelectState(status, kinectInfo = null) {
  const sessionActive = Boolean(status?.session?.active);
  const inSession = Boolean(sessionActive && status?.session?.kinectRole);
  const camera = sessionActive ? status?.cameras?.kinectRole ?? null : null;
  const info = kinectInfo || status?.kinect || null;
  const available = inSession || Boolean(info?.available);
  if (inSession) {
    return { available: true, disabled: true, label: "Kinect 360 RGB (подключён в стереосессии)" };
  }
  if (available) {
    return { available: true, disabled: false, label: "Kinect 360 RGB (подключён)" };
  }
  const reason = info?.reason ? `: ${info.reason}` : "";
  return { available: false, disabled: true, label: `Kinect 360 RGB (не подключён)${reason}` };
}

/**
 * Почему кнопка «Запустить стерео» недоступна. Помимо disabled интерфейс обязан
 * показать текст причины — иначе пользователь не понимает, что делать.
 */
export function stereoStartState(status, { busy = false, kinect = null, cameras = null } = {}) {
  const active = Boolean(status?.session?.active);
  if (active) return { disabled: true, reason: "Стереорежим уже запущен. Нажмите «Остановить»." };
  if (busy) return { disabled: true, reason: "Выполняется команда моста…" };
  const kinectState = kinect || kinectSelectState(status, null);
  if (!kinectState.available) {
    return { disabled: true, reason: kinectState.label || "Kinect не подключён: подключите источник Kinect 360." };
  }
  const found = cameras?.cameras?.length || 0;
  if (!found) {
    const scanning = Boolean(cameras?.pending || cameras?.scan?.running);
    return {
      disabled: true,
      reason: scanning
        ? "Идёт поиск USB-камер…"
        : "USB-камера не выбрана: нажмите «Найти USB-камеры» или проверьте индекс вручную.",
    };
  }
  return { disabled: false, reason: "" };
}

/**
 * Причина, по которой карта глубины пуста. Возвращает строку или null, если
 * глубина есть. Пустой чёрный прямоугольник без подписи запрещён.
 */
export function depthPlaceholder(status) {
  const session = status?.session;
  if (!session?.active) return "сессия не запущена";
  const depth = status?.depth;
  if (depth && depth.depth) return null;
  const calibration = status?.calibration || {};
  if (status?.depthError) return `ошибка глубины: ${status.depthError}`;
  if (calibration.state === "camera_unavailable") return "камера недоступна";
  if (!calibration.present) return "нет калибровки";
  if (!calibration.valid) {
    const reason = (calibration.reasons || [])[0];
    return reason ? `калибровка недействительна: ${reason}` : "калибровка недействительна";
  }
  const pairs = status?.metrics?.pairs || {};
  if (!pairs.attempts) return "нет пар: кадры обеих камер ещё не совпали по времени";
  if (!pairs.accepted) {
    return `нет принятых пар: ${pairs.rejectedSkew || 0} по разнице времени, ` +
      `${pairs.rejectedStale || 0} устаревших, ${pairs.rejectedMissing || 0} без пары`;
  }
  if (depth && typeof depth.ageMs === "number") {
    return `глубина устарела (возраст ${formatMs(depth.ageMs)})`;
  }
  return "глубина ещё не рассчитана";
}

/** Строки прогресса поиска USB-камер для панели. */
export function cameraScanLines(catalog) {
  if (!catalog) return { lines: [], busy: false };
  const scan = catalog.scan || null;
  const busy = Boolean(scan?.running || catalog.pending);
  const lines = [];
  if (busy) {
    lines.push(`Поиск: проверено ${scan?.checked ?? 0} из ${scan?.total ?? 0}` +
      ((scan?.foundIndices || []).length ? `, найдено: ${scan.foundIndices.join(", ")}` : ""));
  } else if (scan) {
    lines.push(
      `Поиск завершён: проверено ${scan.checked} из ${scan.total}, найдено ${(scan.foundIndices || []).length}` +
        `, ${formatMs(scan.elapsedMs)}${scan.cancelled ? ", отменён" : ""}`,
    );
  } else if (catalog.needsScan) {
    lines.push("Поиск USB-камер ещё не выполнялся: нажмите «Найти USB-камеры».");
  }
  if (catalog.cached && catalog.scannedAt) {
    lines.push(`Результат из кэша (${catalog.scannedAt}).`);
  }
  (catalog.errors || []).slice(0, 4).forEach((error) => {
    const code = error.code || "";
    const label = code === "timeout"
      ? "не отвечает"
      : code === "budget_exhausted"
        ? "не проверен (бюджет)"
        : code === "open_failed"
          ? "не открылась"
          : code || "ошибка";
    lines.push(`Индекс ${error.index}: ${label} — ${error.message || ""}`.trim());
  });
  return { lines, busy };
}

/** Строка запроса статуса: неизменённые блоки не пересылаются. */
export function stereoPollQuery({ previewVersions, depthVersion, timings = false } = {}) {
  const parts = ["depth=1", "preview=1", `timings=${timings ? 1 : 0}`];
  const a = previewVersions?.a;
  const b = previewVersions?.b;
  if (Number.isInteger(a)) parts.push(`previewA=${a}`);
  if (Number.isInteger(b)) parts.push(`previewB=${b}`);
  if (Number.isInteger(depthVersion)) parts.push(`depthVersion=${depthVersion}`);
  return `/api/stereo?${parts.join("&")}`;
}

/**
 * Что перерисовать по версиям и какие версии запомнить для следующего опроса.
 * `previous` — версии, которые уже есть на экране. Сервер присылает base64
 * только для изменившихся блоков, поэтому перерисовка идёт строго по данным.
 */
export function previewPlan(previous, status) {
  const next = {
    previewVersions: { ...(previous?.previewVersions || {}) },
    depthVersion: previous?.depthVersion ?? null,
  };
  const redraw = { a: false, b: false, depth: false };
  for (const role of ["a", "b"]) {
    const preview = status?.previews?.[role];
    if (!preview) continue;
    const version = Number(preview.version ?? preview.frameId ?? 0) || 0;
    if (preview.data) {
      redraw[role] = true;
      next.previewVersions[role] = version;
    } else if (preview.unchanged && version) {
      next.previewVersions[role] = version;
    }
  }
  const depth = status?.depth;
  const version = Number(status?.depthVersion ?? 0) || 0;
  if (depth?.depth) {
    redraw.depth = true;
    next.depthVersion = version;
  } else if (depth?.unchanged && version) {
    next.depthVersion = version;
  }
  return { redraw, next };
}

/** Краткие строки диагностики для одной камеры пары. */
export function cameraMetricLines(metrics, role) {
  const camera = metrics?.[role];
  if (!camera) return { role, available: false, lines: [] };
  const dropped = (camera.intervalGaps || 0) + (camera.sequenceGaps || 0);
  return {
    role,
    available: Boolean(camera.frames),
    lines: [
      ["Кадров", String(camera.frames ?? 0)],
      ["FPS (по медиане интервалов)", typeof camera.fps === "number" ? camera.fps.toFixed(1) : "—"],
      ["Интервал, медиана / P95", `${formatMs(camera.intervalMedianMs)} / ${formatMs(camera.intervalP95Ms)}`],
      ["Выброшено из очереди SDK", String(camera.queueDropped ?? 0)],
      ["Пропуски интервалов", String(camera.intervalGaps ?? 0)],
      ["Пропуски номеров кадров", String(camera.sequenceGaps ?? 0)],
      ["Возраст последнего кадра", formatMs(camera.lastFrameAgeMs)],
      ["Состояние", cameraStateLabel(camera.state)],
      ["Переоткрытий", String(camera.reopens ?? 0)],
      ["Пропущено всего", String(dropped)],
    ],
  };
}

export function cameraStateLabel(state) {
  if (state === "streaming") return "поток идёт";
  if (state === "camera_unavailable") return "камера недоступна";
  if (state === "starting") return "ожидание первого кадра";
  return "нет данных";
}

export function pairMetricLines(metrics) {
  const pairs = metrics?.pairs;
  if (!pairs) return [];
  const accepted = pairs.accepted || 0;
  const attempts = pairs.attempts || 0;
  return [
    ["Попыток пары", String(attempts)],
    ["Принято пар", `${accepted}${attempts ? ` из ${attempts}` : ""}`],
    ["Отброшено: разница времени", String(pairs.rejectedSkew ?? 0)],
    ["Отброшено: устаревшие кадры", String(pairs.rejectedStale ?? 0)],
    ["Отброшено: нет кадра", String(pairs.rejectedMissing ?? 0)],
    ["Отброшено: скачок часов", String(pairs.rejectedClock ?? 0)],
    ["Разница времени, медиана", formatMs(pairs.medianSkewMs)],
    ["Разница времени, P95", formatMs(pairs.p95SkewMs)],
    ["Разница времени, максимум", formatMs(pairs.maxSkewMs)],
  ];
}

export function depthMetricLines(depth) {
  if (!depth) return [];
  const lines = [
    ["Валидных пикселей", `${depth.validPixels ?? 0} (${formatPercent(depth.validRatio)})`],
    ["Отброшено по текстуре", String(depth.textureRejected ?? 0)],
    ["Отброшено по диапазону", String(depth.rangeRejected ?? 0)],
    ["Совпадений до отбраковки", String(depth.matchedBeforeReject ?? 0)],
    ["Диапазон", `${formatDepthMm((depth.minDepthM ?? 0) * 1000)} … ${formatDepthMm((depth.maxDepthM ?? 0) * 1000)}`],
    ["Базис", depth.baselineM ? `${(depth.baselineM * 1000).toFixed(0)} мм` : "—"],
    ["Шаг глубины на 1 м", formatDepthMm(depth.depthResolutionAt1mMm)],
    ["Шаг глубины на 3 м", formatDepthMm(depth.depthResolutionAt3mMm)],
    ["Возраст расчёта", formatMs(depth.ageMs)],
    ["Разница времени пары", formatMs(depth.pairSkewMs)],
  ];
  if (depth.disparitiesCapped) {
    lines.push(["Внимание", "диапазон диспаратности ограничен: ближняя граница глубже запрошенной"]);
  }
  return lines;
}

/** Пошаговая инструкция калибровки; зависит от состояния сессии. */
export function calibrationSteps(status) {
  const views = status?.views || {};
  const count = views.count || 0;
  const required = views.required || 5;
  const recommended = views.recommended || 8;
  const steps = [
    "Закрепите обе камеры на жёсткой планке: между ними должно быть 30–300 мм, объективы смотрят примерно в одну сторону.",
    "Не двигайте планку и не наклоняйте Kinect во время съёмки пар: любое движение делает калибровку недействительной.",
    "Откройте доску на экране монитора (или распечатайте), измерьте сторону квадрата линейкой и укажите её в миллиметрах.",
    "Снимайте пары, поворачивая доску на 10–40° и перемещая её по кадру; нужно не менее " +
      `${required} пар, лучше ${recommended}.`,
    "Следите, чтобы доска целиком попадала в кадр обеих камер и была резкой, без бликов и ШИМ-полос.",
    "Нажмите «Откалибровать». Если проверка качества не пройдена, причина будет показана — исправьте и повторите.",
  ];
  if (count) steps.push(`Собрано пар: ${count} из ${required} обязательных.`);
  if (status?.calibration?.state === "valid") {
    steps.push("Калибровка действительна. Глубина показывается только для пар, принятых по разнице времени.");
  }
  return steps;
}

/** Строки таймингов моста: видно, что именно медленно, без сглаживания. */
export function timingLines(timings, names = []) {
  if (!timings) return [];
  const keys = names.length ? names : Object.keys(timings);
  return keys
    .filter((key) => timings[key])
    .map((key) => [key, `p50 ${formatMs(timings[key].p50Ms)} · p95 ${formatMs(timings[key].p95Ms)} · max ${formatMs(timings[key].maxMs)} (n=${timings[key].count ?? 0})`]);
}

export function stereoWarningLines() {
  return [
    "Kinect RGB и USB-камера не имеют аппаратной синхронизации: метки времени программные, это время получения кадра, а не экспозиции.",
    "Стереоглубина не объединяется с глубиной Kinect и не заменяет её.",
    "Приложение не управляет роботом и не обнаруживает препятствия; глубину нельзя использовать для остановки или навигации.",
    "Качество зависит от освещения, текстуры сцены и жёсткости крепления; однотонные поверхности глубины не дают.",
  ];
}
