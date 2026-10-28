"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const ui = import(pathToFileURL(path.join(__dirname, "../../src/readiness.js")));
const health = { ok: true, modules: { numpy: true, open3d: true } };
const setup = { phase: "ready", bridge: { ready: true }, packages: { open3d: { available: true } } };
const physical = { source: "kinect_v1_sdk", connected: true, setup, health,
  phase: "idle", running: false, hasVisibleData: true, hasResult: false };

test("desktop принимает только положительный health своего экземпляра", () => {
  const { isBridgeHealthValid } = require("../bridge-health.cjs");
  assert.equal(isBridgeHealthValid(200, { ok: true, instance: "our-token" }, "our-token"), true);
  assert.equal(isBridgeHealthValid(200, { ok: false, instance: "our-token" }, "our-token"), false);
  assert.equal(isBridgeHealthValid(200, { ok: true, instance: "old-token" }, "our-token"), false);
  assert.equal(isBridgeHealthValid(503, { ok: true, instance: "our-token" }, "our-token"), false);
  assert.equal(isBridgeHealthValid(200, null, "our-token"), false);
});

test("готовая установка не означает готовый мост без успешного health", async () => {
  const { setupHeadline, environmentStatus } = await ui;
  const starting = { ...setup, bridge: { ready: false } };
  assert.notEqual(setupHeadline(starting, null), "Среда Depth Studio готова");
  assert.notEqual(setupHeadline(setup, null), "Среда Depth Studio готова");
  assert.equal(setupHeadline(setup, health), "Среда Depth Studio готова");
  const missingOpen3d = { ...health, modules: { ...health.modules, open3d: false } };
  assert.match(setupHeadline(setup, missingOpen3d), /не готова.*Open3D/);
  assert.match(setupHeadline({ ...setup, packages: { open3d: { available: false } } }, health), /не готова.*Open3D/);
  assert.equal(environmentStatus(starting, health).ready, false);
  assert.equal(environmentStatus(setup, { ...health, ok: false }).ready, false);
  assert.equal(environmentStatus(setup, health).ready, true);
});

test("реальные действия недоступны при неподключённом датчике, мосте или Open3D", async () => {
  const { scanActions } = await ui;
  for (const change of [
    { connected: false },
    { health: null },
    { setup: { ...setup, bridge: { ready: false } } },
    { health: { ...health, modules: { ...health.modules, open3d: false } } },
    { setup: { ...setup, phase: "failed" } },
  ]) {
    const state = scanActions({ ...physical, hasResult: true, phase: "ready", ...change });
    assert.equal(state.start, false, JSON.stringify(change));
    assert.equal(state.clear, false, JSON.stringify(change));
    assert.equal(state.export, false, JSON.stringify(change));
    assert.ok(state.reason, JSON.stringify(change));
  }
  assert.match(scanActions({ ...physical, connected: false }).reason, /Подключите/);
  assert.equal(scanActions(physical).clear, false); // live preview is not a saved scan
});

test("экспорт доступен только после асинхронной обработки, остановка не блокируется", async () => {
  const { scanActions, isScanInProgress } = await ui;
  assert.equal(isScanInProgress("starting", true), true);
  assert.equal(isScanInProgress("capturing", true), true);
  assert.equal(isScanInProgress("finalizing", false), true);
  assert.equal(isScanInProgress("idle", false), false);
  assert.equal(scanActions({ ...physical, hasResult: true, phase: "finalizing" }).export, false);
  assert.equal(scanActions({ ...physical, hasResult: true, phase: "merging" }).start, false);
  assert.equal(scanActions({ ...physical, hasResult: true, phase: "ready" }).export, true);
  const running = scanActions({ ...physical, running: true, phase: "capturing", connected: false, health: null });
  assert.equal(running.start, true); // this is the Stop action
  assert.equal(running.cancel, true);
  assert.equal(running.clear, false);
  assert.equal(running.export, false);
});

test("деморежим остаётся доступным без Python и датчика", async () => {
  const { scanActions } = await ui;
  const demo = scanActions({ source: "demo", connected: false, setup: null, health: null, hasVisibleData: true });
  assert.equal(demo.start, true);
  assert.equal(demo.clear, true);
  assert.equal(demo.export, true);
  assert.equal(demo.reason, "");
});

test("конвейер попадает в состав Windows-сборки, без новых зависимостей", () => {
  const pkg = require("../../package.json");
  assert.ok(pkg.build.extraResources.some((entry) => entry.from === "scan_pipeline" && entry.to === "scan_pipeline"));
  assert.deepEqual(Object.keys(pkg.dependencies).sort(), ["lucide", "three"]);
});

test("зависший мост или тело ответа не блокируют интерфейс навсегда", async () => {
  const { apiRequest, requestTimeout } = await import(pathToFileURL(path.join(__dirname, "../../src/api.js")));
  assert.equal(requestTimeout("/api/health"), 3000);
  assert.equal(requestTimeout("/api/devices/connect"), 15000);
  assert.equal(requestTimeout("/api/frame?source=kinect_v1_sdk"), 7000);
  assert.ok(requestTimeout("/api/scan/start") > requestTimeout("/api/health"));
  const oldFetch = global.fetch;
  const waitForAbort = (signal) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  });
  try {
    global.fetch = (_path, options) => waitForAbort(options.signal);
    await assert.rejects(apiRequest("/api/devices/connect", { timeoutMs: 10 }),
      (error) => error.code === "timeout" && /журнал моста/.test(error.message));
    global.fetch = async (_path, options) => ({ ok: true, headers: { get: () => "application/json" },
      json: () => waitForAbort(options.signal) });
    await assert.rejects(apiRequest("/api/health", { timeoutMs: 10 }),
      (error) => error.code === "timeout");
    global.fetch = async () => ({ ok: false, status: 503, headers: { get: () => "application/json" },
      json: async () => ({ error: "device busy", code: "open_failed", hint: "Освободите датчик." }) });
    await assert.rejects(apiRequest("/api/devices/connect"),
      (error) => error.code === "open_failed" && /device busy.*Освободите датчик/.test(error.message));
  } finally {
    global.fetch = oldFetch;
  }
});
