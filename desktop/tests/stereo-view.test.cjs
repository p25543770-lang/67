"use strict";

// Чистые помощники стереорежима: декодирование глубины, окраска и подписи.
// DOM не нужен, поэтому тест идёт в обычном node:test, как остальные UI-тесты.
const test = require("node:test");
const assert = require("node:assert/strict");

async function loadStereoView() {
  return import("../../src/stereo-view.js");
}

function encodeDepth(values, width, height) {
  const bytes = new Uint8Array(width * height * 2);
  values.forEach((value, index) => {
    bytes[index * 2] = value & 0xff;
    bytes[index * 2 + 1] = (value >> 8) & 0xff;
  });
  return { width, height, data: Buffer.from(bytes).toString("base64") };
}

test("нормализация режима не пропускает неизвестные значения", async () => {
  const { normalizeViewMode, VIEW_MODES } = await loadStereoView();
  assert.deepEqual(VIEW_MODES, ["map", "front", "color", "stereo"]);
  assert.equal(normalizeViewMode("stereo"), "stereo");
  assert.equal(normalizeViewMode("map"), "map");
  assert.equal(normalizeViewMode("unknown"), "map");
  assert.equal(normalizeViewMode(undefined), "map");
});

test("глубина декодируется как uint16 little-endian", async () => {
  const { decodeDepthFrame } = await loadStereoView();
  const payload = encodeDepth([0, 500, 1000, 65535], 2, 2);
  const depth = decodeDepthFrame(payload);
  assert.ok(depth);
  assert.equal(depth.width, 2);
  assert.equal(depth.height, 2);
  assert.deepEqual(Array.from(depth.data), [0, 500, 1000, 65535]);
});

test("битые данные глубины не превращаются в мнимые измерения", async () => {
  const { decodeDepthFrame } = await loadStereoView();
  assert.equal(decodeDepthFrame(null), null);
  assert.equal(decodeDepthFrame({ width: 2, height: 2, data: "" }), null);
  assert.equal(decodeDepthFrame({ width: 0, height: 2, data: "AAAA" }), null);
  // Длина буфера не совпадает с width*height*2.
  assert.equal(decodeDepthFrame({ width: 4, height: 4, data: "AAAA" }), null);
  assert.equal(decodeDepthFrame({ width: 1, height: 1, data: "!!!not base64" }), null);
});

test("неизвестная глубина остаётся прозрачной, а не заливается цветом", async () => {
  const { depthToRgba } = await loadStereoView();
  const depth = { width: 2, height: 1, data: new Uint16Array([0, 2000]) };
  const rgba = depthToRgba(depth, { minMm: 500, maxMm: 4000 });
  assert.ok(rgba);
  assert.equal(rgba.data.length, 8);
  assert.equal(rgba.data[3], 0, "ноль миллиметров — нет измерения");
  assert.equal(rgba.data[7], 255, "валидный пиксель непрозрачен");
  assert.equal(depthToRgba(null), null);
});

test("подписи метрик не выдают отсутствие данных за ноль", async () => {
  const { formatMs, formatPercent, formatDepthMm } = await loadStereoView();
  assert.equal(formatMs(null), "—");
  assert.equal(formatMs(Number.NaN), "—");
  assert.equal(formatMs(12.34), "12.3 мс");
  assert.equal(formatMs(1500), "1.50 с");
  assert.equal(formatPercent(null), "—");
  assert.equal(formatPercent(0.132), "13 %");
  assert.equal(formatDepthMm(null), "—");
  assert.equal(formatDepthMm(883), "883 мм");
  assert.equal(formatDepthMm(16640), "16.64 м");
});

test("диагностика камер показывает пропуски и возраст кадра", async () => {
  const { cameraMetricLines } = await loadStereoView();
  const lines = cameraMetricLines(
    {
      a: {
        frames: 120,
        fps: 19.53,
        intervalMedianMs: 48.3,
        intervalP95Ms: 65.9,
        intervalGaps: 14,
        sequenceGaps: 3,
        lastFrameAgeMs: 52.4,
      },
    },
    "a",
  );
  assert.equal(lines.available, true);
  const map = Object.fromEntries(lines.lines);
  assert.equal(map["Кадров"], "120");
  assert.equal(map["FPS (по медиане интервалов)"], "19.5");
  assert.equal(map["Пропуски интервалов"], "14");
  assert.equal(map["Пропуски номеров кадров"], "3");
  assert.equal(map["Пропущено всего"], "17");
  assert.equal(cameraMetricLines(null, "a").available, false);
});

test("пары и глубина описывают отбраковку без округления в пользу режима", async () => {
  const { pairMetricLines, depthMetricLines } = await loadStereoView();
  const pairs = Object.fromEntries(
    pairMetricLines({
      pairs: {
        attempts: 12,
        accepted: 7,
        rejectedSkew: 3,
        rejectedStale: 1,
        rejectedMissing: 1,
        rejectedClock: 0,
        medianSkewMs: 19.7,
        p95SkewMs: 21.4,
        maxSkewMs: 24.0,
      },
    }),
  );
  assert.equal(pairs["Принято пар"], "7 из 12");
  assert.equal(pairs["Отброшено: разница времени"], "3");
  assert.equal(pairs["Разница времени, медиана"], "19.7 мс");

  const depth = Object.fromEntries(
    depthMetricLines({
      validPixels: 10138,
      validRatio: 0.132,
      textureRejected: 33100,
      rangeRejected: 651,
      matchedBeforeReject: 43889,
      minDepthM: 0.5,
      maxDepthM: 4,
      baselineM: 0.1001,
      depthResolutionAt1mMm: 16636,
      depthResolutionAt3mMm: 149730,
      ageMs: 123.4,
      pairSkewMs: 19.7,
      disparitiesCapped: true,
    }),
  );
  assert.equal(depth["Валидных пикселей"], "10138 (13 %)");
  assert.equal(depth["Отброшено по текстуре"], "33100");
  assert.equal(depth["Базис"], "100 мм");
  assert.match(depth["Внимание"], /диспаратности/);
  assert.deepEqual(depthMetricLines(null), []);
});

test("состояние калибровки переводится в понятный статус", async () => {
  const { calibrationStateLabel } = await loadStereoView();
  assert.equal(calibrationStateLabel("valid").tone, "ok");
  assert.equal(calibrationStateLabel("invalid").tone, "bad");
  assert.equal(calibrationStateLabel("tilt_unknown").tone, "warn");
  assert.equal(calibrationStateLabel("нет-такого").tone, "warn");
});

test("инструкция требует планку, экран и минимум пар", async () => {
  const { calibrationSteps, stereoWarningLines } = await loadStereoView();
  const steps = calibrationSteps({ views: { count: 3, required: 5, recommended: 8 } });
  assert.ok(steps.length >= 6);
  assert.ok(steps.some((step) => step.includes("планк")));
  assert.ok(steps.some((step) => step.includes("Собрано пар: 3 из 5")));
  const warnings = stereoWarningLines().join(" ");
  assert.match(warnings, /аппаратной синхронизации/);
  assert.match(warnings, /не управляет роботом/);
  assert.match(warnings, /не объединяется с глубиной Kinect/);
});

// --- Состояние панели: источник истины — /api/stereo ------------------------
// Помощники ниже решают, что показать и что можно нажать. Ошибка в них даёт
// «мёртвую» кнопку без причины или кадр, нарисованный из ничего.

function activeSession(overrides = {}) {
  return {
    session: {
      active: true,
      kinectRole: "a",
      cameras: { a: { kind: "kinect_rgb" }, b: { kind: "usb", index: 0 } },
      errors: {},
      streamStates: { a: "streaming", b: "streaming" },
      ...overrides,
    },
    cameras: { a: { kind: "kinect_rgb" }, b: { kind: "usb", index: 0 } },
    calibration: { state: "missing", present: false, valid: false, reasons: [] },
    depthEnabled: false,
    depth: null,
    metrics: { pairs: { attempts: 0, accepted: 0 } },
  };
}

test("Kinect в активной сессии считается подключённым, а не «не подключён»", async () => {
  const { kinectSelectState } = await loadStereoView();
  const inSession = kinectSelectState(activeSession(), { available: false, reason: "нет устройства" });
  assert.equal(inSession.available, true);
  assert.equal(inSession.disabled, true);
  assert.match(inSession.label, /стереосессии/);

  const idle = kinectSelectState({ session: { active: false } }, { available: false, reason: "SDK не найден" });
  assert.equal(idle.available, false);
  assert.equal(idle.disabled, true);
  assert.match(idle.label, /не подключён/);
  assert.match(idle.label, /SDK не найден/);

  const ready = kinectSelectState({ session: { active: false } }, { available: true });
  assert.deepEqual([ready.available, ready.disabled], [true, false]);
});

test("кнопка «Запустить» всегда объясняет, почему недоступна", async () => {
  const { stereoStartState, kinectSelectState } = await loadStereoView();
  const running = stereoStartState(activeSession(), {});
  assert.equal(running.disabled, true);
  assert.match(running.reason, /уже запущен/);

  const busy = stereoStartState({ session: { active: false } }, { busy: true });
  assert.equal(busy.disabled, true);
  assert.match(busy.reason, /моста/);

  const noKinect = stereoStartState({ session: { active: false } },
    { kinect: kinectSelectState({ session: { active: false } }, { available: false }) });
  assert.equal(noKinect.disabled, true);
  assert.match(noKinect.reason, /Kinect/);

  const scanning = stereoStartState({ session: { active: false } },
    { kinect: { available: true }, cameras: { pending: true, cameras: [] } });
  assert.equal(scanning.disabled, true);
  assert.match(scanning.reason, /Идёт поиск/);

  const noCamera = stereoStartState({ session: { active: false } },
    { kinect: { available: true }, cameras: { cameras: [], needsScan: true } });
  assert.equal(noCamera.disabled, true);
  assert.match(noCamera.reason, /Найти USB-камеры/);

  const ready = stereoStartState({ session: { active: false } },
    { kinect: { available: true }, cameras: { cameras: [{ index: 1 }] } });
  assert.deepEqual(ready, { disabled: false, reason: "" });
});

test("пустая карта глубины подписана причиной, а не остаётся чёрным квадратом", async () => {
  const { depthPlaceholder } = await loadStereoView();
  assert.equal(depthPlaceholder({ session: { active: false } }), "сессия не запущена");
  assert.equal(depthPlaceholder(activeSession()), "нет калибровки");

  const invalid = activeSession();
  invalid.calibration = { state: "invalid", present: true, valid: false, reasons: ["мало пар"] };
  assert.match(depthPlaceholder(invalid), /мало пар/);

  const noPairs = activeSession();
  noPairs.calibration = { state: "valid", present: true, valid: true, reasons: [] };
  assert.match(depthPlaceholder(noPairs), /нет пар/);

  const rejected = activeSession();
  rejected.calibration = { state: "valid", present: true, valid: true, reasons: [] };
  rejected.metrics.pairs = { attempts: 4, accepted: 0, rejectedSkew: 3, rejectedStale: 1, rejectedMissing: 0 };
  assert.match(depthPlaceholder(rejected), /3 по разнице времени/);

  const stale = activeSession();
  stale.calibration = { state: "valid", present: true, valid: true, reasons: [] };
  stale.metrics.pairs = { attempts: 2, accepted: 2 };
  stale.depth = { ageMs: 900 };
  assert.match(depthPlaceholder(stale), /устарела/);

  const cameraGone = activeSession();
  cameraGone.calibration = { state: "camera_unavailable", present: true, valid: false, reasons: [] };
  assert.equal(depthPlaceholder(cameraGone), "камера недоступна");

  const withDepth = activeSession();
  withDepth.depth = { depth: { width: 2, height: 2 }, ageMs: 10 };
  assert.equal(depthPlaceholder(withDepth), null);
});

test("строки поиска камер показывают прогресс и причины отказов", async () => {
  const { cameraScanLines } = await loadStereoView();
  assert.deepEqual(cameraScanLines(null), { lines: [], busy: false });

  const running = cameraScanLines({ pending: true, scan: { running: true, checked: 2, total: 6, foundIndices: [1] } });
  assert.equal(running.busy, true);
  assert.match(running.lines[0], /проверено 2 из 6/);
  assert.match(running.lines[0], /найдено: 1/);

  const done = cameraScanLines({
    cached: true, scannedAt: "2026-10-30T10:00:00+00:00",
    scan: { running: false, checked: 6, total: 6, foundIndices: [1], elapsedMs: 1531, cancelled: false },
    errors: [{ index: 4, code: "timeout", message: "Индекс 4 не ответил" }],
  });
  assert.equal(done.busy, false);
  assert.match(done.lines[0], /Поиск завершён/);
  assert.match(done.lines[0], /1\.53 с/);
  assert.ok(done.lines.some((line) => /из кэша/.test(line)));
  assert.ok(done.lines.some((line) => /не отвечает/.test(line)));

  const never = cameraScanLines({ needsScan: true, pending: false });
  assert.equal(never.busy, false);
  assert.match(never.lines[0], /ещё не выполнялся/);
});

test("запрос статуса несёт только известные версии", async () => {
  const { stereoPollQuery } = await loadStereoView();
  assert.equal(stereoPollQuery(), "/api/stereo?depth=1&preview=1&timings=0");
  assert.equal(
    stereoPollQuery({ previewVersions: { a: 4, b: 5 }, depthVersion: 2, timings: true }),
    "/api/stereo?depth=1&preview=1&timings=1&previewA=4&previewB=5&depthVersion=2",
  );
  assert.equal(stereoPollQuery({ previewVersions: { a: null, b: 3 } }),
    "/api/stereo?depth=1&preview=1&timings=0&previewB=3");
});

test("перерисовка идёт только по пришедшим данным", async () => {
  const { previewPlan } = await loadStereoView();
  const first = previewPlan({ previewVersions: {}, depthVersion: null }, {
    previews: { a: { version: 3, data: "AAAA" }, b: null },
    depth: null,
    depthVersion: 0,
  });
  assert.deepEqual(first.redraw, { a: true, b: false, depth: false });
  assert.equal(first.next.previewVersions.a, 3);

  const unchanged = previewPlan(first.next, {
    previews: { a: { version: 3, unchanged: true, data: null }, b: { version: 4, data: "BBBB" } },
    depth: null,
  });
  assert.deepEqual(unchanged.redraw, { a: false, b: true, depth: false });
  assert.deepEqual(unchanged.next.previewVersions, { a: 3, b: 4 });

  const withDepth = previewPlan(unchanged.next, {
    previews: { a: { version: 3, unchanged: true }, b: { version: 4, unchanged: true } },
    depth: { depth: { width: 1, height: 1, data: "AA" } },
    depthVersion: 7,
  });
  assert.deepEqual(withDepth.redraw, { a: false, b: false, depth: true });
  assert.equal(withDepth.next.depthVersion, 7);

  // Пустой статус (сессия остановлена) ничего не стирает и не рисует.
  const stopped = previewPlan(withDepth.next, {});
  assert.deepEqual(stopped.redraw, { a: false, b: false, depth: false });
  assert.equal(stopped.next.depthVersion, 7);
});

test("состояние камеры и тайминги читаются человеком", async () => {
  const { cameraStateLabel, timingLines } = await loadStereoView();
  assert.equal(cameraStateLabel("streaming"), "поток идёт");
  assert.equal(cameraStateLabel("camera_unavailable"), "камера недоступна");
  assert.equal(cameraStateLabel("starting"), "ожидание первого кадра");
  assert.equal(cameraStateLabel(undefined), "нет данных");

  assert.deepEqual(timingLines(null), []);
  const lines = timingLines({
    kinect_capture: { p50Ms: 12.5, p95Ms: 40, maxMs: 400, count: 120 },
    status: { p50Ms: 4.27, p95Ms: 5.35, maxMs: 9, count: 30 },
  }, ["kinect_capture", "status", "usb_read"]);
  assert.equal(lines.length, 2);
  assert.equal(lines[0][0], "kinect_capture");
  assert.match(lines[0][1], /p50 12\.5 мс/);
  assert.match(lines[0][1], /n=120/);
  assert.equal(timingLines({ speed: { p50Ms: 1, p95Ms: 2, maxMs: 3 } }).length, 1);
});

test("диагностика камеры показывает пропуски очереди и переоткрытия", async () => {
  const { cameraMetricLines } = await loadStereoView();
  const lines = cameraMetricLines({
    a: { frames: 86, fps: 30.5, intervalMedianMs: 32.4, intervalP95Ms: 33.2, queueDropped: 12,
         intervalGaps: 1, sequenceGaps: 2, lastFrameAgeMs: 40, state: "streaming", reopens: 2 },
  }, "a").lines;
  const map = Object.fromEntries(lines);
  assert.equal(map["Кадров"], "86");
  assert.equal(map["FPS (по медиане интервалов)"], "30.5");
  assert.equal(map["Выброшено из очереди SDK"], "12");
  assert.equal(map["Состояние"], "поток идёт");
  assert.equal(map["Переоткрытий"], "2");
  assert.equal(map["Пропущено всего"], "3");
});
