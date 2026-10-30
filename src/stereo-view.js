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
      ["Пропуски интервалов", String(camera.intervalGaps ?? 0)],
      ["Пропуски номеров кадров", String(camera.sequenceGaps ?? 0)],
      ["Возраст последнего кадра", formatMs(camera.lastFrameAgeMs)],
      ["Пропущено всего", String(dropped)],
    ],
  };
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

export function stereoWarningLines() {
  return [
    "Kinect RGB и USB-камера не имеют аппаратной синхронизации: метки времени программные, это время получения кадра, а не экспозиции.",
    "Стереоглубина не объединяется с глубиной Kinect и не заменяет её.",
    "Приложение не управляет роботом и не обнаруживает препятствия; глубину нельзя использовать для остановки или навигации.",
    "Качество зависит от освещения, текстуры сцены и жёсткости крепления; однотонные поверхности глубины не дают.",
  ];
}
