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
