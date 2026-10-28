"use strict";

const api = window.depthStudioSetup;
const STATE_LABELS = {
  pending: "ожидание",
  running: "выполняется",
  done: "готово",
  cached: "уже готово",
  error: "ошибка",
  warning: "внимание",
  skipped: "пропущено",
};
const DEFAULT_STEPS = [
  { id: "prepare", title: "Подготовка каталога приложения", detail: "%LOCALAPPDATA%\\DepthStudio" },
  { id: "python", title: "Python 3.11 x64", detail: "Встроенный интерпретатор Depth Studio" },
  { id: "packages", title: "NumPy и Open3D", detail: "Закреплённые версии из python-lock.json" },
  { id: "verify", title: "Проверка среды", detail: "Импорт пакетов и изоляция" },
  { id: "sensors", title: "Проверка датчиков (только чтение)", detail: "Драйверы не устанавливаются" },
];

const elements = {
  steps: document.getElementById("steps"),
  progressFill: document.getElementById("progressFill"),
  progressPercent: document.getElementById("progressPercent"),
  phaseLabel: document.getElementById("phaseLabel"),
  envPath: document.getElementById("envPath"),
  elapsed: document.getElementById("elapsed"),
  notice: document.getElementById("notice"),
  noticeTitle: document.getElementById("noticeTitle"),
  noticeMessage: document.getElementById("noticeMessage"),
  noticeHint: document.getElementById("noticeHint"),
  noticeDetails: document.getElementById("noticeDetails"),
  noticeDetailsText: document.getElementById("noticeDetailsText"),
  noticeCode: document.getElementById("noticeCode"),
  warnings: document.getElementById("warnings"),
  warningList: document.getElementById("warningList"),
  logPanel: document.getElementById("logPanel"),
  logBody: document.getElementById("logBody"),
  logPath: document.getElementById("logPath"),
  primary: document.getElementById("primaryAction"),
  retry: document.getElementById("retryAction"),
  repair: document.getElementById("repairAction"),
  log: document.getElementById("logAction"),
  demo: document.getElementById("demoAction"),
  toggleLog: document.getElementById("toggleLog"),
  appVersion: document.getElementById("appVersion"),
  lead: document.getElementById("leadText"),
};

let state = {
  phase: "running",
  progress: 0,
  steps: DEFAULT_STEPS,
  error: null,
  warnings: [],
  envDir: "%LOCALAPPDATA%\\DepthStudio",
  logFile: "",
  startedAt: null,
  finishedAt: null,
  appVersion: "",
};
let logLines = [];
let logVisible = false;
let elapsedTimer = null;

function renderSteps() {
  const steps = state.steps?.length ? state.steps : DEFAULT_STEPS;
  elements.steps.innerHTML = steps
    .map((step) => {
      const status = step.status || "pending";
      const detail = step.detailText || step.detail || "";
      return `
        <li class="step" data-status="${escapeAttr(status)}">
          <span class="step-dot" aria-hidden="true"></span>
          <div>
            <div class="step-title">${escapeHtml(step.title || step.id)}</div>
            <div class="step-detail">${escapeHtml(detail)}</div>
          </div>
          <span class="step-state">${STATE_LABELS[status] || escapeHtml(status)}</span>
        </li>`;
    })
    .join("");
}

function renderProgress() {
  const percent = Math.max(0, Math.min(100, Math.round(state.progress || 0)));
  elements.progressFill.style.width = `${percent}%`;
  elements.progressPercent.textContent = `${percent}%`;
  const running = state.steps?.find((step) => step.status === "running");
  if (state.phase === "ready") elements.phaseLabel.textContent = "Среда готова";
  else if (state.phase === "failed") elements.phaseLabel.textContent = "Настройка не завершена";
  else if (running) elements.phaseLabel.textContent = running.title;
  else elements.phaseLabel.textContent = "Проверка установки…";
}

function renderError() {
  const error = state.error;
  if (!error) {
    elements.notice.hidden = true;
    return;
  }
  elements.notice.hidden = false;
  elements.noticeTitle.textContent = error.message || "Не удалось настроить среду";
  elements.noticeMessage.textContent =
    "Depth Studio запустится в деморежиме: просмотр сцены, синтетический поток и экспорт доступны без Python.";
  elements.noticeHint.hidden = !error.hint;
  elements.noticeHint.textContent = error.hint || "";
  elements.noticeDetails.hidden = !error.details;
  elements.noticeDetailsText.textContent = error.details || "";
  elements.noticeCode.hidden = !error.code;
  elements.noticeCode.textContent = error.code ? `Код: ${error.code}` : "";
}

function renderWarnings() {
  const warnings = state.warnings || [];
  elements.warnings.hidden = warnings.length === 0;
  elements.warningList.innerHTML = warnings
    .map(
      (warning) => `<li><strong>${escapeHtml(warning.message)}</strong>${
        warning.hint ? `<span>${escapeHtml(warning.hint)}</span>` : ""
      }</li>`,
    )
    .join("");
}

function renderActions() {
  const running = state.phase === "running";
  const failed = state.phase === "failed";
  const ready = state.phase === "ready";
  elements.primary.hidden = false;
  elements.primary.disabled = running || failed;
  elements.primary.textContent = ready ? "Открыть Depth Studio" : "Настройка…";
  elements.retry.hidden = !failed;
  elements.retry.disabled = running;
  elements.repair.hidden = !(ready || failed);
  elements.repair.disabled = running;
  elements.demo.hidden = !(failed || running);
  elements.demo.disabled = false;
  elements.demo.textContent = running ? "Перейти в деморежим сейчас" : "Продолжить в деморежиме";
  elements.log.disabled = false;
}

function render() {
  renderSteps();
  renderProgress();
  renderError();
  renderWarnings();
  renderActions();
  elements.envPath.textContent = state.envDir || "%LOCALAPPDATA%\\DepthStudio";
  elements.logPath.textContent = state.logFile || "";
  if (state.appVersion) elements.appVersion.textContent = `BETA · ${state.appVersion}`;
}

function appendLog(line) {
  if (!line) return;
  logLines.push(line);
  if (logLines.length > 500) logLines = logLines.slice(-500);
  if (logVisible) {
    const body = elements.logBody;
    const atEnd = body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
    body.textContent = logLines.join("\n");
    if (atEnd) body.scrollTop = body.scrollHeight;
  }
}

function startElapsedTimer() {
  if (elapsedTimer) return;
  elapsedTimer = setInterval(() => {
    if (!state.startedAt) return;
    const end = state.finishedAt ? new Date(state.finishedAt).getTime() : Date.now();
    const seconds = Math.max(0, Math.round((end - new Date(state.startedAt).getTime()) / 1000));
    elements.elapsed.textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(
      seconds % 60,
    ).padStart(2, "0")}`;
  }, 500);
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character],
  );
}
function escapeAttr(value) {
  return escapeHtml(value).replace(/[\s"]/g, "_");
}

function applyEvent(event) {
  if (!event) return;
  switch (event.type) {
    case "log":
      appendLog(event.line);
      break;
    case "reset": {
      state = {
        ...state,
        ...(event.state || {}),
        phase: "running",
        progress: 0,
        error: null,
        warnings: [],
        sensorNotes: [],
        finishedAt: null,
        startedAt: new Date().toISOString(),
      };
      render();
      break;
    }
    case "step": {
      const steps = state.steps?.length ? [...state.steps] : DEFAULT_STEPS.map((step) => ({ ...step }));
      const index = steps.findIndex((item) => item.id === event.step.id);
      if (index >= 0) steps[index] = { ...steps[index], ...event.step };
      else steps.push(event.step);
      state = { ...state, steps, progress: event.progress ?? state.progress, phase: "running" };
      render();
      break;
    }
    case "done": {
      const result = event.result || {};
      state = {
        ...state,
        phase: "ready",
        progress: 100,
        steps: result.steps?.length ? result.steps : state.steps,
        warnings: result.warnings || [],
        error: null,
        envDir: result.envDir || state.envDir,
        logFile: result.logFile || state.logFile,
        finishedAt: new Date().toISOString(),
      };
      render();
      break;
    }
    case "error": {
      state = { ...state, phase: "failed", error: event.error, finishedAt: new Date().toISOString() };
      if (event.result?.steps?.length) state.steps = event.result.steps;
      if (event.result?.warnings?.length) state.warnings = event.result.warnings;
      render();
      break;
    }
    default:
      break;
  }
}

async function loadInitialState() {
  try {
    const initial = await api.getState();
    if (initial) {
      state = { ...state, ...initial };
      if (initial.log) initial.log.forEach(appendLog);
      render();
    }
  } catch (error) {
    appendLog(`Не удалось получить состояние: ${error.message}`);
  }
  startElapsedTimer();
}

elements.primary.addEventListener("click", async () => {
  if (state.phase === "ready") await api.openApp();
});
elements.retry.addEventListener("click", async () => {
  logLines.push("— повтор установки —");
  await api.run("retry");
});
elements.repair.addEventListener("click", async () => {
  const confirmed = window.confirm(
    "Восстановить среду? Каталог %LOCALAPPDATA%\\DepthStudio\\runtime\\python будет удалён и создан заново. " +
      "Настройки приложения и облака точек не затрагиваются.",
  );
  if (!confirmed) return;
  logLines.push("— восстановление среды —");
  await api.run("repair");
});
elements.log.addEventListener("click", async () => {
  await api.revealLog();
});
elements.demo.addEventListener("click", async () => {
  await api.continueDemo();
});
elements.toggleLog.addEventListener("click", async () => {
  logVisible = !logVisible;
  elements.logPanel.hidden = !logVisible;
  elements.toggleLog.setAttribute("aria-expanded", String(logVisible));
  elements.toggleLog.textContent = logVisible ? "Скрыть журнал" : "Журнал установки";
  if (logVisible) {
    if (!logLines.length) {
      const payload = await api.readLog().catch(() => null);
      if (payload?.setup?.length) logLines = payload.setup;
    }
    elements.logBody.textContent = logLines.join("\n");
    elements.logBody.scrollTop = elements.logBody.scrollHeight;
  }
});

if (api?.onEvent) api.onEvent(applyEvent);
loadInitialState();
