import * as THREE from "three";
import { environmentStatus, isScanInProgress, scanActions, setupHeadline } from "./readiness.js";
import { apiRequest } from "./api.js";
import { formatCpu, formatFps, formatFrameChecks, formatSpeed } from "./metrics.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import Box from "lucide/dist/esm/icons/box.js";
import Radio from "lucide/dist/esm/icons/radio.js";
import Settings2 from "lucide/dist/esm/icons/settings-2.js";
import CircleHelpIcon from "lucide/dist/esm/icons/circle-help.js";
import ChevronDown from "lucide/dist/esm/icons/chevron-down.js";
import Maximize2 from "lucide/dist/esm/icons/maximize-2.js";
import Activity from "lucide/dist/esm/icons/activity.js";
import RotateCcw from "lucide/dist/esm/icons/rotate-ccw.js";
import Move3d from "lucide/dist/esm/icons/move-3d.js";
import MousePointer2 from "lucide/dist/esm/icons/mouse-pointer-2.js";
import Download from "lucide/dist/esm/icons/download.js";
import Play from "lucide/dist/esm/icons/play.js";
import Pause from "lucide/dist/esm/icons/pause.js";
import Check from "lucide/dist/esm/icons/check.js";
import SlidersHorizontal from "lucide/dist/esm/icons/sliders-horizontal.js";
import ScanLine from "lucide/dist/esm/icons/scan-line.js";
import ArrowUpRight from "lucide/dist/esm/icons/arrow-up-right.js";
import Menu from "lucide/dist/esm/icons/menu.js";
import replaceIcon from "lucide/dist/esm/replaceElement.js";
import "./style.css";

const HelpCircle = CircleHelpIcon;
const CircleHelp = CircleHelpIcon;
function createIcons({
  icons = {},
  nameAttr = "data-lucide",
  attrs = {},
} = {}) {
  document.querySelectorAll(`[${nameAttr}]`).forEach((element) => {
    replaceIcon(element, { nameAttr, icons, attrs });
  });
}

document.querySelector("#app").innerHTML = `
  <header class="topbar">
    <a class="brand" href="#" aria-label="Depth Studio — главная">
      <span class="brand-mark"><i data-lucide="box"></i></span>
      <span class="brand-word">depth<span>studio</span><small>ROOM SCANNER</small></span>
    </a>
    <div class="topbar-right">
      <div class="connection-pill" id="headerConnection"><span class="status-dot"></span><span id="headerStatus">Демо-режим</span></div>
      <button class="top-icon" id="helpBtn" aria-label="Помощь и требования" title="Помощь и требования"><i data-lucide="circle-help"></i></button>
      <span class="version-tag">BETA · 1.2.1</span>
    </div>
  </header>

  <main class="app-shell">
    <section class="setup-banner" id="setupBanner" data-phase="idle" hidden>
      <div class="setup-banner-main">
        <span class="setup-banner-dot"></span>
        <div class="setup-banner-copy">
          <strong id="setupTitle">Среда Depth Studio не настроена</strong>
          <p id="setupText"></p>
          <div class="setup-banner-remedies" id="setupRemedies" hidden>
            <span class="setup-banner-remedies-title">Что проверить</span>
            <ul id="setupRemedyList"></ul>
          </div>
          <details class="setup-banner-details" id="setupDetails" hidden>
            <summary id="setupDetailsSummary">Технические подробности</summary>
            <pre id="setupDetailsText"></pre>
          </details>
          <div class="setup-banner-progress" id="setupProgressWrap" hidden>
            <div class="setup-banner-fill" id="setupProgressFill"></div>
          </div>
        </div>
      </div>
      <div class="setup-banner-actions">
        <button class="setup-action" id="setupRetryBtn">Повторить</button>
        <button class="setup-action" id="setupRepairBtn">Восстановить среду</button>
        <button class="setup-action ghost" id="setupCopyLogBtn">Скопировать журнал</button>
        <button class="setup-action ghost" id="setupLogBtn">Журнал установки</button>
      </div>
    </section>
    <div class="page-heading">
      <div>
        <div class="eyebrow"><span class="eyebrow-mark"></span> DEPTH STUDIO <span class="eyebrow-separator">/</span> ПРОСТРАНСТВО</div>
        <h1>Сканирование помещения<span class="title-period">.</span></h1>
        <p>Медленно перемещайте датчик и собирайте облако точек с контролем отслеживания.</p>
      </div>
      <button class="help-button" id="requirementsBtn"><i data-lucide="circle-help"></i><span>Датчики и требования</span><i data-lucide="arrow-up-right"></i></button>
    </div>

    <div class="workspace-grid">
      <div class="main-column">
        <section class="viewer-card card">
          <div class="viewer-toolbar">
            <div class="viewer-title-block">
              <span class="live-indicator" id="liveIndicator"></span>
              <div><strong>3D-ОБЛАКО ТОЧЕК</strong><small id="sceneModeLabel">ДЕМО-СЦЕНА</small></div>
            </div>
            <div class="tracking-chip" id="trackingChip" data-status="demo"><span class="tracking-pulse"></span><span id="trackingLabel">Демо-режим</span></div>
          </div>
          <div class="scene-wrap" id="sceneWrap">
            <div class="scene-canvas" id="sceneCanvas"></div>
            <div class="scene-topline"><span class="scene-cross">✳</span> ПРОСТРАНСТВЕННЫЙ ПРОСМОТР <span>/</span> PERSPECTIVE</div>
            <div class="scene-tools">
              <button id="resetView" title="Сбросить вид" aria-label="Сбросить вид"><i data-lucide="rotate-ccw"></i></button>
              <button id="fitView" title="Показать сцену целиком" aria-label="Показать сцену целиком"><i data-lucide="move-3d"></i></button>
              <button id="fullscreen" title="На весь экран" aria-label="На весь экран"><i data-lucide="maximize-2"></i></button>
            </div>
            <div class="empty-scene" id="emptyScene"><span class="empty-icon">✳</span><strong>Облако точек пока пустое</strong><small>Подключите датчик или запустите демонстрационное сканирование.</small></div>
            <div class="scene-axis"><span class="axis-y">Y</span><span class="axis-x">X</span><span class="axis-z">Z</span></div>
            <div class="scene-hint"><i data-lucide="mouse-pointer-2"></i> Перетащите — вращение <span>·</span> Колесо — масштаб</div>
          </div>
          <div class="viewer-footer">
            <div class="stat-block"><span>ТОЧЕК</span><strong id="pointCount">—</strong></div>
            <div class="stat-block"><span>КАДРОВ</span><strong id="frameCount">0</strong></div>
            <div class="stat-block stat-depth"><span>ДИАПАЗОН</span><strong id="depthMetric">0.5 – 4.0 м</strong></div>
            <div class="footer-separator"></div>
            <div class="scan-actions">
              <button class="button-primary" id="scanButton"><i data-lucide="scan-line"></i><span>Начать сканирование</span></button>
              <button class="button-secondary" id="cancelScanButton" title="Отменить текущий проход" hidden>Отменить</button>
              <button class="button-secondary clear-button" id="clearButton" title="Очистить просмотр"><i data-lucide="rotate-ccw"></i><span>Очистить</span></button>
              <div class="export-buttons" aria-label="Экспорт результата">
                <button id="exportPly" title="Экспорт в PLY"><i data-lucide="download"></i> PLY</button>
                <button id="exportObj" title="Экспорт в OBJ">OBJ</button>
                <button id="exportGlb" title="Экспорт сетки в GLB">GLB</button>
              </div>
            </div>
          </div>
          <p class="scan-action-reason" id="scanActionReason" aria-live="polite" hidden></p>
        </section>

        <section class="feed-card card">
          <div class="feed-header">
            <div><span class="feed-heading-mark"></span><strong>ПОТОК ДАТЧИКА</strong><small>ИЗОБРАЖЕНИЕ И ГЛУБИНА</small></div>
            <span class="feed-state" id="feedState">ДЕМО · БЕЗ ДАТЧИКА</span>
          </div>
          <div class="feed-grid">
            <div class="feed-panel">
              <div class="feed-canvas-wrap"><canvas id="rgbCanvas" width="640" height="360" aria-label="Цветное изображение датчика"></canvas><div class="feed-overlay"><span>RGB</span><span id="rgbFrameLabel">DEMO FRAME</span></div></div>
              <div class="feed-caption"><strong>Цветное изображение</strong><small id="rgbDescription">Демонстрационный кадр</small></div>
            </div>
            <div class="feed-panel">
              <div class="feed-canvas-wrap depth-canvas-wrap"><canvas id="depthCanvas" width="320" height="180" aria-label="Карта глубины датчика"></canvas><div class="feed-overlay"><span>DEPTH</span><span id="depthFrameLabel">DEMO MAP</span></div><div class="depth-legend"><span>БЛИЖЕ</span><i></i><span>ДАЛЬШЕ</span></div></div>
              <div class="feed-caption"><strong>Карта глубины</strong><small id="depthDescription">Синтетическая демо-карта</small></div>
            </div>
          </div>
        </section>

        <section class="passes-card card" id="passesCard" hidden>
          <div class="passes-heading"><div><span class="section-kicker">ПРОХОДЫ / СОХРАНЕНИЕ</span><h2>Сохранённые проходы</h2></div><button class="button-secondary" id="refreshPassesButton">Обновить</button></div>
          <p class="passes-intro">Проходы сохраняются на диске. Выберите не менее двух завершённых проходов для объединения.</p>
          <div class="passes-list" id="passesList" aria-live="polite"></div>
          <div class="passes-actions"><button class="button-secondary" id="mergePassesButton">Объединить выбранные</button><span id="passesNote">Калибровка приблизительная; на физическом датчике не проверено.</span></div>
        </section>
      </div>

      <aside class="side-column">
        <section class="card source-card" id="sourcePanel">
          <div class="section-heading"><div><span class="section-kicker">01 / ИСТОЧНИК</span><h2>Датчик глубины</h2></div><span class="heading-icon"><i data-lucide="radio"></i></span></div>
          <label class="field-label" for="sourceSelect">Выберите источник</label>
          <div class="select-wrap"><select id="sourceSelect" aria-label="Источник данных"><option value="demo">Демо-режим · без датчика</option></select><i data-lucide="chevron-down"></i></div>
          <div class="source-status-box">
            <div class="source-status-line"><span class="source-state-dot" id="sourceDot"></span><strong id="sourceState">Демо-сцена активна</strong><span class="source-state-meta" id="sourceStateMeta">DEMO</span></div>
            <p id="sourceDescription">Синтетическая сцена помогает проверить интерфейс и экспорт. Физический датчик не используется.</p>
            <div class="verification-label" id="verificationLabel"><span>i</span><span>Демо не требует датчика. Аппаратная проверка не выполнялась.</span></div>
          </div>
          <div class="requirements-block" id="sourceRequirements" hidden></div>
          <div class="source-buttons">
            <button class="button-secondary full-button" id="detectButton"><i data-lucide="radio"></i> Обнаружить датчики</button>
            <button class="button-primary full-button connect-button" id="connectButton"><i data-lucide="check"></i><span>Демо-режим активен</span></button>
          </div>
          <div class="server-note" id="serverNote"><span class="server-note-dot"></span><span>Локальный мост не проверен · демо доступно</span></div>
        </section>

        <section class="card tracking-card">
          <div class="section-heading compact-heading"><div><span class="section-kicker">02 / СЪЁМКА</span><h2>Состояние трекинга</h2></div><span class="heading-icon"><i data-lucide="activity"></i></span></div>
          <div class="tracking-summary" id="trackingSummary" data-status="demo"><span class="summary-icon">✳</span><div><strong id="trackingSummaryTitle">Демонстрация</strong><small id="trackingSummaryText">Перейдите на источник глубины, чтобы сканировать комнату.</small></div></div>
          <div class="warning-box" id="warningBox" hidden><span class="warning-mark">!</span><span id="warningText"></span></div>
          <div class="tracking-metrics">
            <div><span>СКОРОСТЬ · ОЦЕНКА</span><strong id="speedMetric">— км/ч</strong></div>
            <div><span>FPS · ЗАХВАТ / ОБРАБОТКА</span><strong id="fpsMetric">— / —</strong></div>
            <div><span>ПРОПУСКИ · ОЧЕРЕДЬ</span><strong id="droppedMetric">—</strong></div>
            <div><span>ПРОПУСКИ · ДО ЧТЕНИЯ SDK</span><strong id="sensorDroppedMetric">—</strong></div>
            <div><span>СОСТОЯНИЕ</span><strong id="stateMetric">Демо</strong></div>
            <div><span>КЛЮЧЕВЫХ КАДРОВ · ВСЕГО</span><strong id="keyframesMetric">—</strong></div>
            <div><span>ПРОФИЛЬ ПРОХОДА</span><strong id="activeProfileMetric">—</strong></div>
            <div><span>FPS · ТРЕКИНГ / TSDF</span><strong id="trackedFpsMetric">— / —</strong></div>
            <div><span>ПРИНЯТО ДЛЯ ТРЕКИНГА</span><strong id="acceptedMetric">—</strong></div>
            <div><span>СВЕРКА · ПРОШЛИ / ПРОВЕРОК</span><strong id="frameChecksMetric">— / —</strong></div>
            <div title="Логические процессоры / запрошенное число потоков OpenMP"><span>CPU · ЛОГИЧ. / OMP</span><strong id="cpuMetric">— / —</strong></div>
          </div>
          <div class="scan-progress"><div class="scan-progress-fill" id="scanProgressFill"></div></div>
          <label class="field-label scan-profile-label" for="scanProfile">Профиль нового прохода</label>
          <div class="select-wrap"><select id="scanProfile" aria-describedby="scanProfileHint"><option value="max_fps">Максимум FPS</option><option value="standard">Стандарт · плотнее</option></select><i data-lucide="chevron-down"></i></div>
          <p class="movement-hint" id="scanProfileHint">«Максимум FPS» объединяет меньше ключевых кадров: может быть больше шума и хуже детали или трекинг. Неоднозначные позы дополнительно сверяются с последним надёжным кадром, когда подходит RGB. На i5‑13400 автоматически запрашиваются до 4 потоков OpenMP; продолжение сохраняет профиль прохода. 320×240, Kinect v1 — до 30 FPS; обработка и цель 7 км/ч не гарантируются.</p>
        </section>

        <section class="card settings-card" id="settingsPanel">
          <div class="section-heading compact-heading"><div><span class="section-kicker">03 / ПРОСМОТР</span><h2>Настройки сцены</h2></div><span class="heading-icon"><i data-lucide="sliders-horizontal"></i></span></div>
          <div class="setting-row"><div class="setting-title">Размер точек <span id="sizeValue">2.0</span></div><input id="pointSize" type="range" min="1" max="5" step="0.1" value="2"><div class="range-labels"><span>Меньше</span><span>Больше</span></div></div>
          <div class="setting-row"><div class="setting-title">Макс. глубина <span id="depthValue">4.0 м</span></div><input id="depthRange" type="range" min="1" max="6" step="0.1" value="4"><div class="range-labels"><span>1 м</span><span>6 м</span></div></div>
          <div class="setting-row palette-setting"><label class="setting-title" for="palette">Цветовая схема</label><div class="select-wrap"><select id="palette"><option value="depth">По глубине</option><option value="mint">Мятная</option><option value="mono">Монохром</option></select><i data-lucide="chevron-down"></i></div></div>
          <label class="grid-toggle"><span><strong>Сетка пространства</strong><small>Ориентир в 3D-сцене</small></span><input id="gridToggle" type="checkbox" checked><span class="toggle-track"></span></label>
        </section>

        <section class="honesty-note"><span class="honesty-icon">i</span><p><strong>Важно</strong> Камера без датчика глубины не подходит автоматически. Поддержка устройства появится только после реализации и проверки его отдельного модуля.</p></section>
      </aside>
    </div>
    <footer class="page-footer"><span>DEPTH STUDIO <b>·</b> ДЕМО И ЭКСПЕРИМЕНТАЛЬНОЕ СКАНИРОВАНИЕ</span><span>ДЕМО — ОБЛАКО ТОЧЕК · ДАТЧИК — ЭКСПЕРИМЕНТАЛЬНАЯ СЕТКА</span></footer>
  </main>

  <div class="modal-backdrop" id="helpModal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
    <div class="help-modal">
      <button class="modal-close" id="closeModal" aria-label="Закрыть">×</button>
      <span class="section-kicker">СПРАВКА / ИСТОЧНИКИ ГЛУБИНЫ</span>
      <h2 id="modalTitle">Подключение датчиков</h2>
      <p class="modal-intro">В этой сборке физическое сканирование реализовано только для Kinect 360: рекомендуемый путь — Microsoft Kinect SDK 1.8, альтернативный — libfreenect с другим USB-драйвером. Оба помечены как не проверенные на реальном устройстве. Остальные строки — требования для будущих модулей, а не заявление о поддержке.</p>
      <div class="device-table-wrap"><table class="device-table"><thead><tr><th>Устройство</th><th>Статус</th><th>Windows и драйверы</th><th>Ограничения</th></tr></thead><tbody>
        <tr><td><strong>Kinect 360</strong><small>Kinect v1 / Xbox 360</small></td><td><span class="status-tag experimental">Экспериментальный модуль</span><small>На физическом датчике не проверен</small></td><td><b>Рекомендуется:</b> штатный драйвер Microsoft Kinect + Kinect for Windows SDK 1.8 (pythonnet уже входит в Depth Studio). USB-драйвер не меняется. <b>Альтернатива:</b> libfreenect + модуль <code>freenect</code> с драйвером libusbK/WinUSB — он заменяет драйвер Microsoft и может сломать другие Kinect-приложения.</td><td>Калибровка глубины приближённая; цвет и глубина не совмещены; ICP может дрейфовать. Нужен штатный блок питания/USB-адаптер.</td></tr>
        <tr><td><strong>Kinect One</strong><small>Kinect v2 / Xbox One</small></td><td><span class="status-tag unavailable">Не реализован</span></td><td>Исторически Kinect for Windows SDK 2.0 на Windows 8.1/10 x64, USB 3.0-контроллер и адаптер питания. Windows 11 требует отдельной проверки.</td><td>Приложение не обнаруживает этот датчик. Нужны отдельный адаптер, проверка SDK и аппаратные тесты.</td></tr>
        <tr><td><strong>Azure Kinect DK</strong></td><td><span class="status-tag unavailable">Не реализован</span></td><td>Для будущей интеграции: Windows 10 x64, Azure Kinect Sensor SDK/прошивка и USB 3.0; доступность устаревшего SDK следует проверить.</td><td>Приложение не обнаруживает устройство. Модель снята с рынка; SDK и конкретная прошивка требуют отдельной проверки.</td></tr>
        <tr><td><strong>Intel RealSense</strong></td><td><span class="status-tag unavailable">Не реализован</span></td><td>Для будущей интеграции: Windows 10/11 x64, совместимая с моделью версия RealSense SDK 2.0 и USB 3.x.</td><td>Приложение не обнаруживает устройство. Дальность и точность зависят от модели, прошивки и условий сцены.</td></tr>
        <tr><td><strong>Другие датчики</strong></td><td><span class="status-tag unavailable">Только после проверки</span></td><td>Нужны совместимые драйверы/SDK и отдельный модуль для конкретного устройства.</td><td>Обычная RGB-веб-камера не является датчиком глубины и не определяется как совместимая.</td></tr>
      </tbody></table></div>
      <div class="env-block" id="envBlock">
        <h3>Среда Python</h3>
        <p id="envSummary">Среда Depth Studio расположена в %LOCALAPPDATA%\DepthStudio. Пакеты не устанавливаются глобально.</p>
        <p class="env-detail" id="envDetail"></p>
        <div class="env-actions"><button class="setup-action" id="envRetryBtn">Проверить и повторить настройку</button><button class="setup-action ghost" id="envLogBtn">Журнал установки</button></div>
      </div>
      <div class="modal-columns"><div><h3>Запуск демо</h3><p>Демо работает без датчика и Python-моста. Кнопки сканирования и экспорта в демо-режиме используют синтетическую комнату.</p></div><div><h3>Реальное устройство</h3><p>Для Kinect 360 установите Kinect for Windows SDK 1.8 с сайта Microsoft: драйвер Microsoft Kinect при этом остаётся как есть, а pythonnet уже входит в Depth Studio. Вариант с libfreenect и Zadig описан в README как альтернатива с предупреждением о конфликте драйверов. Оба пути не проверены на физическом датчике в этом репозитории.</p></div></div>
      <button class="button-primary modal-done" id="modalDone"><i data-lucide="check"></i> Понятно</button>
    </div>
  </div>
  <div class="toast" id="toast" role="status" aria-live="polite"></div>
`;

createIcons({
  icons: {
    Box,
    Radio,
    Settings2,
    HelpCircle,
    ChevronDown,
    Maximize2,
    Activity,
    RotateCcw,
    Move3d,
    MousePointer2,
    Download,
    Play,
    Pause,
    Check,
    CircleHelp,
    SlidersHorizontal,
    ScanLine,
    ArrowUpRight,
    Menu,
  },
});

const $ = (id) => document.getElementById(id);
const viewport = $("sceneWrap");
const sceneHost = $("sceneCanvas");
const rgbCanvas = $("rgbCanvas");
const depthCanvas = $("depthCanvas");
const depthColorLut = new Uint8ClampedArray(256 * 3);
for (let index = 0; index < 256; index++) {
  const distance = index / 255;
  const color = new THREE.Color().setHSL(
    0.52 - distance * 0.36,
    0.82,
    0.46 + (1 - distance) * 0.09,
  );
  depthColorLut[index * 3] = Math.round(color.r * 255);
  depthColorLut[index * 3 + 1] = Math.round(color.g * 255);
  depthColorLut[index * 3 + 2] = Math.round(color.b * 255);
}

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1714);
scene.fog = new THREE.Fog(0x0b1714, 8, 27);
const camera = new THREE.PerspectiveCamera(43, 1, 0.02, 100);
camera.position.set(6.4, 4.4, 7.4);
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
sceneHost.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 1.25, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.065;
controls.minDistance = 1.1;
controls.maxDistance = 20;
const grid = new THREE.GridHelper(14, 28, 0x527568, 0x263c34);
grid.position.y = -0.035;
scene.add(grid);
const pointGeometry = new THREE.BufferGeometry();
const pointMaterial = new THREE.PointsMaterial({
  size: 0.027,
  vertexColors: true,
  sizeAttenuation: true,
  transparent: true,
  opacity: 0.96,
});
const pointObject = new THREE.Points(pointGeometry, pointMaterial);
scene.add(pointObject);

let originalPoints = [];
let demoPoints = makeDemoRoom();
let activeSource = "demo";
let devices = [];
let deviceConnected = false;
let deviceActionBusy = false;
let scanActionBusy = false;
let bridgeHealth = null;
let devicesAvailable = false;
let scanPhase = "idle";
let savedPasses = [];
let selectedPassIds = new Set();
let streamGeneration = 0;
let streamTimer = null;
let streamBusy = false;
let scanTimer = null;
let demoScanTimer = null;
let scanBusy = false;
let isScanning = false;
let hasScan = false;
let demoScanFrames = 0;
let toastTimeout = null;

function makeDemoRoom() {
  const points = [];
  const rand = (a, b, salt = 0) => {
    const v = Math.sin(a * 127.1 + b * 311.7 + salt * 74.7) * 43758.5453;
    return v - Math.floor(v);
  };
  for (let x = -3.05; x <= 3.05; x += 0.085) {
    for (let z = -3.2; z <= 3.2; z += 0.085) {
      if (rand(x, z) > 0.22) points.push([x, 0.02, z, Math.hypot(x, z) + 1]);
    }
  }
  for (let x = -3.05; x <= 3.05; x += 0.085) {
    for (let y = 0.03; y <= 2.75; y += 0.085) {
      if (rand(x, y, 1) > 0.29) points.push([x, y, -3.2, 3.2]);
    }
  }
  for (let z = -3.2; z <= 3.2; z += 0.085) {
    for (let y = 0.03; y <= 2.75; y += 0.085) {
      if (rand(z, y, 2) > 0.31) points.push([-3.05, y, z, 3.2]);
    }
  }
  const surfaceBox = (cx, cy, cz, w, h, d, salt = 0) => {
    for (let x = -w / 2; x <= w / 2; x += 0.075) {
      for (let z = -d / 2; z <= d / 2; z += 0.075) {
        points.push([cx + x, cy + h / 2, cz + z, 2.3]);
      }
      for (let y = -h / 2; y <= h / 2; y += 0.075) {
        if (rand(x + salt, y, 3) > 0.11)
          points.push([cx + x, cy + y, cz + d / 2, 2.3]);
      }
    }
    for (let z = -d / 2; z <= d / 2; z += 0.075) {
      for (let y = -h / 2; y <= h / 2; y += 0.075)
        points.push([cx + w / 2, cy + y, cz + z, 2.3]);
    }
  };
  surfaceBox(0.35, 0.72, -0.72, 2.25, 1.42, 0.8, 1);
  surfaceBox(1.95, 0.36, 1.12, 0.95, 0.72, 0.95, 2);
  surfaceBox(-1.52, 0.48, 1.25, 1.05, 0.95, 1.15, 4);
  surfaceBox(-1.52, 1.18, 0.62, 1.05, 0.48, 0.34, 6);
  return points;
}

function renderPoints() {
  const maxDepth = Number($("depthRange").value);
  const palette = $("palette").value;
  const positions = [];
  const colors = [];
  for (const point of originalPoints) {
    const [x, y, z, depth = 2] = point;
    if (Number.isFinite(depth) && depth > maxDepth) continue;
    positions.push(x, y, z);
    const color = new THREE.Color();
    if (palette === "mono") {
      color.setRGB(0.74, 0.83, 0.78);
    } else if (palette === "mint") {
      color.setRGB(0.27, 0.86, 0.63);
    } else {
      const t = Math.min(Math.max((Number(depth) || 1) / 5, 0), 1);
      color.setHSL(0.47 + t * 0.18, 0.72, 0.48 + (1 - t) * 0.16);
    }
    colors.push(color.r, color.g, color.b);
  }
  pointGeometry.setAttribute(
    "position",
    new THREE.Float32BufferAttribute(positions, 3),
  );
  pointGeometry.setAttribute(
    "color",
    new THREE.Float32BufferAttribute(colors, 3),
  );
  pointGeometry.computeBoundingSphere();
  const count = positions.length / 3;
  $("pointCount").textContent = count.toLocaleString("ru-RU");
  $("emptyScene").classList.toggle("visible", count === 0);
  updateExportButtons();
}

function resizeScene() {
  const rect = sceneHost.getBoundingClientRect();
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resizeScene).observe(sceneHost);
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();
resizeScene();

function resetView() {
  camera.position.set(6.4, 4.4, 7.4);
  controls.target.set(0, 1.25, 0);
  controls.update();
}
$("resetView").onclick = resetView;
$("fitView").onclick = resetView;
$("fullscreen").onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else viewport.requestFullscreen?.();
};

function showToast(message, kind = "info") {
  const toast = $("toast");
  toast.textContent = message;
  toast.dataset.kind = kind;
  toast.classList.add("show");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => toast.classList.remove("show"), 4800);
}

function fallbackDevices() {
  return [
    {
      id: "kinect_v1_sdk",
      name: "Kinect 360 · Microsoft Kinect SDK",
      implemented: true,
      verifiedOnHardware: false,
      dependenciesAvailable: false,
      state: "driver_missing",
      connected: null,
      error:
        "Python-мост не запущен: статус компонентов неизвестен. Деморежим работает без него.",
      windows: "Windows 10/11 x64, драйвер Microsoft Kinect остаётся как есть.",
      driver: "Kinect for Windows SDK 1.8 и pythonnet.",
      limitations: "Экспериментальный, не проверен на физическом датчике.",
      requirements: [
        {
          id: "kinect_sdk_runtime",
          kind: "driver",
          title: "Драйвер устройства · Kinect for Windows SDK 1.8",
          detail:
            "Статус неизвестен: нужен запущенный Python-мост. SDK ставится вручную с сайта Microsoft. " +
            "https://www.microsoft.com/en-us/download/details.aspx?id=40278",
          satisfied: null,
        },
        {
          id: "pythonnet",
          kind: "python",
          title: "Компонент Python · pythonnet",
          detail: "Статус неизвестен: нужен запущенный Python-мост Depth Studio.",
          satisfied: null,
        },
      ],
    },
    {
      id: "kinect_v1",
      name: "Kinect 360 · libfreenect (альтернатива)",
      implemented: true,
      verifiedOnHardware: false,
      dependenciesAvailable: false,
      state: "driver_missing",
      connected: null,
      error:
        "Python-мост не запущен: статус компонентов неизвестен. Деморежим работает без него.",
      windows: "Windows 10/11 x64 — не проверено, нужен драйвер libusbK/WinUSB.",
      driver: "libfreenect и Python-модуль freenect.",
      limitations: "Экспериментальный, не проверен на физическом устройстве; конфликтует с драйвером Microsoft Kinect.",
      requirements: [
        {
          id: "libusbk_driver",
          kind: "driver",
          title: "Драйвер устройства · libusbK/WinUSB для Kinect",
          detail:
            "libfreenect требует привязки Kinect к libusbK/WinUSB (Zadig). Это заменяет драйвер Microsoft Kinect " +
            "и может сломать другие Kinect-приложения; Depth Studio не меняет USB-драйверы автоматически.",
          satisfied: null,
        },
        {
          id: "freenect",
          kind: "python",
          title: "Компонент Python · freenect (libfreenect)",
          detail: "Модуль freenect не входит в установщик: привязки нужно собирать вручную.",
          satisfied: null,
        },
      ],
    },
    {
      id: "kinect_v2",
      name: "Kinect One (Kinect v2)",
      implemented: false,
      state: "not_implemented",
      notImplementedReason: "Модуль не реализован.",
    },
    {
      id: "azure_kinect",
      name: "Azure Kinect DK",
      implemented: false,
      state: "not_implemented",
      notImplementedReason: "Модуль не реализован.",
    },
    {
      id: "intel_realsense",
      name: "Intel RealSense",
      implemented: false,
      state: "not_implemented",
      notImplementedReason: "Модуль не реализован.",
    },
  ];
}

function renderSourceOptions() {
  const select = $("sourceSelect");
  const chosen = select.value || activeSource;
  const options = ['<option value="demo">Демо-режим · без датчика</option>'];
  for (const device of devices) {
    const stateLabel = device.implemented
      ? device.verifiedOnHardware
        ? "проверен"
        : "экспериментальный · не проверен"
      : "не реализован";
    options.push(
      `<option value="${escapeHtml(device.id)}" ${device.implemented ? "" : "disabled"}>${escapeHtml(device.name)} · ${stateLabel}</option>`,
    );
  }
  select.innerHTML = options.join("");
  const exists = Array.from(select.options).some(
    (option) => option.value === chosen && !option.disabled,
  );
  select.value = exists ? chosen : "demo";
  activeSource = select.value;
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character],
  );
}

async function loadDevices(knownHealth = null) {
  try {
    const health = knownHealth || await apiRequest("/api/health");
    if (health?.ok !== true) throw new Error("Python-мост не подтвердил готовность.");
    bridgeHealth = health;
    const response = await apiRequest("/api/devices");
    devices = response.devices || fallbackDevices();
    devicesAvailable = true;
    const env = health.environment || null;
    const envText = env
      ? `Python ${escapeHtml(env.version || health.python || "")} · ${env.isolated ? "среда Depth Studio" : "системный интерпретатор"}`
      : `Python ${escapeHtml(health.python || "")}`;
    $("serverNote").innerHTML = `<span class="server-note-dot online"></span><span>Локальный мост запущен · ${envText}</span>`;
    renderEnvironmentSummary({ ...(setupState || {}), environment: env });
  } catch {
    if (!knownHealth) bridgeHealth = null;
    devices = fallbackDevices();
    devicesAvailable = false;
    $("serverNote").innerHTML =
      '<span class="server-note-dot"></span><span>Локальный мост не запущен · демо доступно</span>';
  }
  renderSourceOptions();
  updateSourcePanel();
}

function selectedProfile() {
  return devices.find((device) => device.id === activeSource) || null;
}

function linkifyDetail(text) {
  // Ссылки только https: приложение не открывает произвольные схемы.
  return escapeHtml(text).replace(
    /(https:\/\/[^\s<]+[^\s<.,;:)])/g,
    (url) => `<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`,
  );
}

const REQUIREMENT_GROUPS = [
  {
    kind: "driver",
    title: "Драйвер устройства",
    note: "Драйверы, SDK и системные компоненты ставите вы; Depth Studio не устанавливает их автоматически.",
  },
  {
    kind: "python",
    title: "Компоненты Python",
    note: "Входят в изолированную среду Depth Studio и ставятся установщиком.",
  },
];

function requirementItem(item) {
  const status = item.satisfied === true ? "ok" : item.satisfied === false ? "missing" : "unknown";
  const mark = status === "ok" ? "✓" : status === "missing" ? "!" : "·";
  return `<li data-status="${status}"><span class="requirement-mark">${mark}</span><div><strong>${escapeHtml(
    item.title,
  )}</strong><small>${linkifyDetail(item.detail)}</small></div></li>`;
}

function renderSourceRequirements(profile) {
  const block = $("sourceRequirements");
  if (!block) return;
  const requirements = Array.isArray(profile?.requirements) ? profile.requirements : [];
  if (!requirements.length) {
    block.hidden = true;
    block.innerHTML = "";
    return;
  }
  block.hidden = false;
  block.innerHTML = REQUIREMENT_GROUPS.map((group) => {
    const items = requirements.filter((item) => item.kind === group.kind);
    if (!items.length) return "";
    return `<div class="requirements-group" data-kind="${group.kind}">
        <span class="requirements-group-title">${group.title}</span>
        <ul>${items.map(requirementItem).join("")}</ul>
        <span class="requirements-note">${group.note}</span>
      </div>`;
  }).join("");
}

function updateSourcePanel() {
  const profile = selectedProfile();
  const dot = $("sourceDot");
  if (activeSource === "demo") {
    deviceConnected = false;
    dot.dataset.state = "demo";
    $("sourceState").textContent = "Демо-сцена активна";
    $("sourceStateMeta").textContent = "DEMO";
    $("sourceDescription").textContent =
      "Синтетическая сцена помогает проверить интерфейс и экспорт. Физический датчик не используется.";
    renderSourceRequirements(null);
    $("verificationLabel").innerHTML =
      "<span>i</span><span>Демо не требует датчика. Аппаратная проверка не выполнялась.</span>";
    $("connectButton").innerHTML =
      '<i data-lucide="check"></i><span>Демо-режим активен</span>';
    $("connectButton").disabled = false;
    $("headerStatus").textContent = "Демо-режим";
    $("headerConnection").dataset.state = "demo";
    $("sceneModeLabel").textContent = "ДЕМО-СЦЕНА";
    $("feedState").textContent = "ДЕМО · БЕЗ ДАТЧИКА";
    $("rgbDescription").textContent = "Синтетическое изображение";
    $("depthDescription").textContent = "Синтетическая карта глубины";
    setTracking(
      "demo",
      0,
      "",
      "Демонстрация",
      "Проверьте управление и экспорт без датчика.",
    );
    renderDemoFeeds();
  } else if (profile) {
    renderSourceRequirements(profile);
    const stateLabels = {
      connected: "Поток активен",
      detected: "Датчик обнаружен",
      not_checked: "Не проверен",
      driver_missing: "Требования не выполнены",
      no_device: "Датчик не найден",
      not_implemented: "Не реализован",
    };
    const state = deviceConnected ? "connected" :
      profile.state === "connected" ? "detected" : profile.state || "not_checked";
    dot.dataset.state = deviceConnected
      ? "connected"
      : state === "driver_missing"
        ? "warning"
        : "offline";
    $("sourceState").textContent = stateLabels[state] || "Ожидает подключения";
    $("sourceStateMeta").textContent = profile.verifiedOnHardware
      ? "ПРОВЕРЕН"
      : "НЕ ПРОВЕРЕН";
    $("sourceDescription").textContent =
      [profile.error, profile.hint, profile.warning].filter(Boolean).join(" ") ||
      profile.limitations || "Выбран экспериментальный источник глубины.";
    $("verificationLabel").innerHTML = profile.verifiedOnHardware
      ? "<span>✓</span><span>Этот модуль прошёл аппаратную проверку.</span>"
      : "<span>!</span><span>Модуль не проверен на реальном датчике; совместимость не гарантируется.</span>";
    $("connectButton").innerHTML = deviceConnected
      ? '<i data-lucide="check"></i><span>Отключить датчик</span>'
      : profile.connected
        ? '<i data-lucide="radio"></i><span>Подключить поток</span>'
        : '<i data-lucide="radio"></i><span>Подключить выбранный источник</span>';
    $("connectButton").disabled = deviceActionBusy || isScanning || bridgeHealth?.ok !== true;
    $("headerStatus").textContent = deviceConnected
      ? "Поток датчика активен"
      : profile.connected
        ? "Датчик обнаружен"
        : "Ожидание датчика";
    $("headerConnection").dataset.state = deviceConnected
      ? "connected"
      : "waiting";
    $("sceneModeLabel").textContent = deviceConnected
      ? "ПОТОК ДАТЧИКА"
      : profile.connected
        ? "ДАТЧИК ОБНАРУЖЕН"
        : "ИСТОЧНИК НЕ ПОДКЛЮЧЁН";
    $("feedState").textContent = deviceConnected
      ? "ПОТОК АКТИВЕН"
      : profile.connected
        ? "ГОТОВ К ПОДКЛЮЧЕНИЮ"
        : "НЕТ ПОТОКА";
    $("rgbDescription").textContent = deviceConnected
      ? "Изображение с датчика"
      : profile.connected
        ? "Датчик готов к открытию потока"
        : "Ожидание цветового кадра";
    $("depthDescription").textContent = deviceConnected
      ? "Карта глубины датчика"
      : profile.connected
        ? "Датчик готов к открытию потока"
        : "Ожидание данных глубины";
    if (!deviceConnected) drawNoSignal();
    if (!isScanning && !["finalizing", "merging"].includes(scanPhase)) setTracking(
      deviceConnected ? "tracking" : "idle",
      0,
      deviceConnected ? (profile.warning || "") : "",
      deviceConnected
        ? "Поток подключён"
        : profile.connected
          ? "Датчик обнаружен"
          : "Источник не подключён",
      deviceConnected
        ? (profile.warning || "Поток глубины поступает в приложение.")
        : profile.connected
          ? "Подключите источник, чтобы начать поток изображения и глубины."
          : "Обнаружьте устройство и проверьте драйверы.",
    );
  }
  createIcons({
    icons: {
      Box,
      Radio,
      Settings2,
      HelpCircle,
      ChevronDown,
      Maximize2,
      Activity,
      RotateCcw,
      Move3d,
      MousePointer2,
      Download,
      Play,
      Pause,
      Check,
      CircleHelp,
      SlidersHorizontal,
      ScanLine,
      ArrowUpRight,
      Menu,
    },
  });
  $("passesCard").hidden = activeSource === "demo";
  updateExportButtons();
  renderPasses();
}

function setTracking(status, speed, warning, title = "", text = "") {
  const labels = {
    demo: "Демо-режим",
    idle: "Ожидание",
    tracking: "Отслеживание",
    moving: "Двигайтесь медленнее",
    too_fast: "Слишком быстро",
    lost: "Трекинг потерян",
    few_valid_pixels: "Мало данных глубины",
    sensor_covered: "Датчик закрыт",
    memory_budget_reached: "Лимит памяти",
    stopped: "Остановлено",
  };
  const display = labels[status] || status;
  $("trackingChip").dataset.status = status;
  $("trackingLabel").textContent = display;
  $("trackingSummary").dataset.status = status;
  $("trackingSummaryTitle").textContent = title || display;
  $("trackingSummaryText").textContent =
    text ||
    (warning ? "Смотрите подсказку ниже и скорректируйте движение." : "");
  $("stateMetric").textContent = status === "demo" ? "Демо" : display;
  $("speedMetric").textContent =
    status === "demo" || status === "idle" || status === "stopped" ? formatSpeed(null) : formatSpeed(speed);
  if (status === "demo") {
    $("fpsMetric").textContent = "— / —";
    $("droppedMetric").textContent = "—";
    $("sensorDroppedMetric").textContent = "—";
    $("keyframesMetric").textContent = "—";
    $("activeProfileMetric").textContent = "—";
    $("trackedFpsMetric").textContent = "— / —";
    $("acceptedMetric").textContent = "—";
    $("frameChecksMetric").textContent = formatFrameChecks(null);
    $("cpuMetric").textContent = formatCpu(null);
  }
  $("warningBox").hidden = !warning;
  if (warning) $("warningText").textContent = warning;
  $("liveIndicator").classList.toggle(
    "is-live",
    ["tracking", "moving", "too_fast"].includes(status),
  );
}

function drawDemoRgb() {
  const canvas = rgbCanvas;
  canvas.width = 640;
  canvas.height = 360;
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  const gradient = ctx.createLinearGradient(0, 0, 0, h);
  gradient.addColorStop(0, "#253a35");
  gradient.addColorStop(0.55, "#37453c");
  gradient.addColorStop(0.56, "#867a64");
  gradient.addColorStop(1, "#4e5046");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = "#6b7464";
  ctx.beginPath();
  ctx.moveTo(0, h * 0.05);
  ctx.lineTo(w * 0.12, h * 0.2);
  ctx.lineTo(w * 0.12, h * 0.63);
  ctx.lineTo(0, h * 0.8);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#263932";
  ctx.beginPath();
  ctx.moveTo(w, h * 0.04);
  ctx.lineTo(w * 0.84, h * 0.2);
  ctx.lineTo(w * 0.84, h * 0.63);
  ctx.lineTo(w, h * 0.82);
  ctx.closePath();
  ctx.fill();
  ctx.strokeStyle = "rgba(188,211,183,.21)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 8; i++) {
    const t = i / 8;
    ctx.beginPath();
    ctx.moveTo(w * 0.5, h * 0.53);
    ctx.lineTo(w * t, h);
    ctx.stroke();
  }
  for (let i = 1; i <= 4; i++) {
    const y = h * (0.56 + i * i * 0.026);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  // A simple, geometric furnishing silhouette; this is illustrative, not captured data.
  ctx.fillStyle = "#8e876d";
  ctx.fillRect(w * 0.27, h * 0.53, w * 0.35, h * 0.06);
  ctx.fillStyle = "#706b59";
  ctx.fillRect(w * 0.31, h * 0.59, w * 0.29, h * 0.22);
  ctx.fillStyle = "#8e876d";
  ctx.fillRect(w * 0.28, h * 0.56, w * 0.06, h * 0.18);
  ctx.fillRect(w * 0.58, h * 0.56, w * 0.06, h * 0.18);
  ctx.fillStyle = "#3b4c3f";
  ctx.fillRect(w * 0.67, h * 0.38, w * 0.13, h * 0.18);
  ctx.fillStyle = "#667661";
  ctx.fillRect(w * 0.7, h * 0.28, w * 0.07, h * 0.1);
  ctx.fillStyle = "rgba(10,22,19,.2)";
  ctx.fillRect(0, 0, w, h);
}

function drawDemoDepth() {
  const canvas = depthCanvas;
  canvas.width = 320;
  canvas.height = 180;
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  const image = ctx.createImageData(w, h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const nx = x / w;
      const ny = y / h;
      let depth = 0.15 + ny * 0.63 + Math.abs(nx - 0.5) * 0.23;
      if (nx > 0.29 && nx < 0.62 && ny > 0.52 && ny < 0.79) depth = 0.31;
      if (nx > 0.68 && nx < 0.81 && ny > 0.36 && ny < 0.58) depth = 0.49;
      const i = (y * w + x) * 4;
      const paletteIndex = Math.max(0, Math.min(255, Math.round(depth * 255)));
      image.data[i] = depthColorLut[paletteIndex * 3];
      image.data[i + 1] = depthColorLut[paletteIndex * 3 + 1];
      image.data[i + 2] = depthColorLut[paletteIndex * 3 + 2];
      image.data[i + 3] = 255;
    }
  }
  ctx.putImageData(image, 0, 0);
}

function renderDemoFeeds() {
  drawDemoRgb();
  drawDemoDepth();
  $("rgbFrameLabel").textContent = "DEMO FRAME";
  $("depthFrameLabel").textContent = "DEMO MAP";
}

function drawNoSignal() {
  for (const canvas of [rgbCanvas, depthCanvas]) {
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#17241f";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#31453d";
    ctx.setLineDash([8, 8]);
    ctx.strokeRect(14, 14, canvas.width - 28, canvas.height - 28);
    ctx.setLineDash([]);
    ctx.fillStyle = "#789287";
    ctx.font = "600 14px system-ui";
    ctx.textAlign = "center";
    ctx.fillText("Нет сигнала датчика", canvas.width / 2, canvas.height / 2);
  }
  $("rgbFrameLabel").textContent = "NO SIGNAL";
  $("depthFrameLabel").textContent = "NO SIGNAL";
}

function decodeBase64(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++)
    bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function paintRgb(frame) {
  if (!frame?.data || !frame.width || !frame.height) return false;
  const bytes = decodeBase64(frame.data);
  const rgba = new Uint8ClampedArray(frame.width * frame.height * 4);
  for (let i = 0, j = 0; i < bytes.length; i += 3, j += 4) {
    rgba[j] = bytes[i];
    rgba[j + 1] = bytes[i + 1];
    rgba[j + 2] = bytes[i + 2];
    rgba[j + 3] = 255;
  }
  rgbCanvas.width = frame.width;
  rgbCanvas.height = frame.height;
  rgbCanvas
    .getContext("2d")
    .putImageData(new ImageData(rgba, frame.width, frame.height), 0, 0);
  return true;
}

function paintDepth(frame) {
  if (!frame?.data || !frame.width || !frame.height) return false;
  const bytes = decodeBase64(frame.data);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const rgba = new Uint8ClampedArray(frame.width * frame.height * 4);
  for (let pixel = 0; pixel < frame.width * frame.height; pixel++) {
    const millimetres = view.getUint16(pixel * 2, true);
    const i = pixel * 4;
    if (!millimetres) {
      rgba[i] = 10;
      rgba[i + 1] = 20;
      rgba[i + 2] = 18;
      rgba[i + 3] = 255;
      continue;
    }
    const distance = Math.min(1, Math.max(0, (millimetres - 450) / 3550));
    const paletteIndex = Math.round(distance * 255);
    rgba[i] = depthColorLut[paletteIndex * 3];
    rgba[i + 1] = depthColorLut[paletteIndex * 3 + 1];
    rgba[i + 2] = depthColorLut[paletteIndex * 3 + 2];
    rgba[i + 3] = 255;
  }
  depthCanvas.width = frame.width;
  depthCanvas.height = frame.height;
  depthCanvas
    .getContext("2d")
    .putImageData(new ImageData(rgba, frame.width, frame.height), 0, 0);
  return true;
}

async function probeDevices() {
  if (isScanning) {
    showToast("Остановите сканирование перед повторным обнаружением датчиков.", "warning");
    return;
  }
  const button = $("detectButton");
  button.disabled = true;
  button.classList.add("is-busy");
  try {
    const result = await apiRequest("/api/devices/probe", { method: "POST", body: {} });
    devices = result.devices || devices;
    const detected = result.detected || [];
    if (deviceConnected && !(result.connected || []).includes(activeSource)) {
      stopStream();
      deviceConnected = false;
    }
    // Обнаружение не оставляет датчик открытым: подключение — отдельный шаг.
    if (detected.length && activeSource === "demo") {
      activeSource = detected[0];
      $("sourceSelect").value = activeSource;
      deviceConnected = false;
      hasScan = false;
      originalPoints = [];
      $("frameCount").textContent = "0";
      renderPoints();
    }
    renderSourceOptions();
    updateSourcePanel();
    if (detected.length) {
      showToast(`Обнаружен датчик: ${devices.find((device) => device.id === detected[0])?.name || detected[0]}. Подключите выбранный источник.`, "success");
    } else {
      const preferred = devices.find((device) => device.id === activeSource) ||
        devices.find((device) => device.id === "kinect_v1_sdk") ||
        devices.find((device) => device.id === "kinect_v1");
      showToast([preferred?.error, preferred?.hint].filter(Boolean).join(" ") ||
        "Совместимый датчик не обнаружен. Проверьте питание, USB-адаптер и драйверы.", "warning");
    }
  } catch (error) {
    showToast(error.message, "warning");
  } finally {
    button.disabled = false;
    button.classList.remove("is-busy");
  }
}

function stopStream() {
  if (streamTimer) clearInterval(streamTimer);
  streamTimer = null;
  // Ignore in-flight frame requests from an old connection/source.
  streamGeneration += 1;
  streamBusy = false;
}

function displaySensorFrame(data) {
  const profile = selectedProfile();
  if (profile) {
    profile.warning = data.warning || "";
    $("sourceDescription").textContent = profile.warning || profile.limitations || "Выбран экспериментальный источник глубины.";
  }
  if (!hasScan && !isScanning && !["finalizing", "merging"].includes(scanPhase) && Array.isArray(data.points)) {
    originalPoints = data.points.map(([x, y, z, depth]) => [x, y + 1.25, z, depth]);
    renderPoints();
  }
  const hasRgb = paintRgb(data.color);
  const hasDepth = paintDepth(data.depth);
  $("rgbDescription").textContent = hasRgb ? "Кадр с Kinect 360" : "RGB-поток недоступен";
  $("depthDescription").textContent = hasDepth ? "Глубина в реальном времени" : "Карта глубины недоступна";
  $("rgbFrameLabel").textContent = hasRgb ? "LIVE · RGB" : "NO RGB";
  $("depthFrameLabel").textContent = hasDepth ? "LIVE · DEPTH" : "NO DEPTH";
  $("feedState").textContent = "ПОТОК АКТИВЕН";
  if (!hasScan && !isScanning && !["finalizing", "merging"].includes(scanPhase)) {
    $("sceneModeLabel").textContent = "ПОТОК ДАТЧИКА";
    setTracking("tracking", 0, data.warning || "", "Датчик подключён",
      data.warning || "Поток глубины поступает в приложение.");
    $("frameCount").textContent = "LIVE";
  }
}

async function pollFrame() {
  if (streamBusy || activeSource === "demo" || !deviceConnected || isScanning) return;
  const generation = streamGeneration;
  const source = activeSource;
  streamBusy = true;
  try {
    const data = await apiRequest(`/api/frame?source=${encodeURIComponent(source)}`);
    if (generation !== streamGeneration || source !== activeSource) return;
    displaySensorFrame(data);
  } catch (error) {
    if (generation !== streamGeneration || source !== activeSource) return;
    stopStream();
    deviceConnected = false;
    const profile = devices.find((device) => device.id === source);
    if (profile) {
      profile.connected = false;
      profile.state = "no_device";
      profile.error = error.message;
      profile.hint = "";
      profile.errorCode = error.code || "";
    }
    updateSourcePanel();
    showToast(`Поток датчика остановлен. ${error.message}`, "warning");
  } finally {
    if (generation === streamGeneration) streamBusy = false;
  }
}

async function connectSelectedSource({ reconnect = false } = {}) {
  if (isScanning) {
    showToast("Сначала остановите сканирование, затем отключайте датчик.", "warning");
    return false;
  }
  if (activeSource === "demo") {
    showToast("Демо-сцена готова. Подключение датчика не требуется.", "success");
    return true;
  }
  if (deviceActionBusy) return false;
  const source = activeSource;
  deviceActionBusy = true;
  updateSourcePanel();
  try {
    if (deviceConnected && !reconnect) {
      stopStream();
      await apiRequest("/api/devices/disconnect", { method: "POST", body: { source } });
      deviceConnected = false;
      const profile = devices.find((device) => device.id === source);
      if (profile) { profile.connected = false; profile.state = "detected"; profile.error = ""; profile.hint = ""; }
      if (!hasScan) { originalPoints = []; renderPoints(); }
      drawNoSignal();
      return true;
    }
    if (deviceConnected) return true;
    const result = await apiRequest("/api/devices/connect", { method: "POST", body: { source } });
    if (source !== activeSource) {
      await apiRequest("/api/devices/disconnect", { method: "POST", body: { source } });
      return false;
    }
    deviceConnected = true;
    const profile = devices.find((device) => device.id === source);
    if (profile) { profile.connected = true; profile.state = "connected"; profile.error = ""; profile.hint = ""; }
    updateSourcePanel();
    displaySensorFrame(result.frame);
    if (!streamTimer) streamTimer = setInterval(pollFrame, 300);
    if (!reconnect) showToast("Датчик подключён. Перемещайте его медленно.", "success");
    return true;
  } catch (error) {
    deviceConnected = false;
    stopStream();
    const profile = devices.find((device) => device.id === source);
    if (profile) {
      profile.connected = false;
      profile.state = "no_device";
      profile.error = error.message;
      profile.hint = "";
      profile.errorCode = error.code || "";
    }
    showToast(`Не удалось подключить источник. ${error.message}`, "warning");
    return false;
  } finally {
    deviceActionBusy = false;
    updateSourcePanel();
  }
}

function setScanButton() {
  $("scanButton").innerHTML = isScanning
    ? '<i data-lucide="pause"></i><span>Остановить сканирование</span>'
    : '<i data-lucide="scan-line"></i><span>Начать сканирование</span>';
  createIcons({
    icons: {
      Box,
      Radio,
      Settings2,
      HelpCircle,
      ChevronDown,
      Maximize2,
      Activity,
      RotateCcw,
      Move3d,
      MousePointer2,
      Download,
      Play,
      Pause,
      Check,
      CircleHelp,
      SlidersHorizontal,
      ScanLine,
      ArrowUpRight,
      Menu,
    },
  });
  updateExportButtons();
}

function currentScanActions() {
  return scanActions({ source: activeSource, connected: deviceConnected, setup: setupState,
    health: bridgeHealth, phase: scanPhase, running: isScanning, hasResult: hasScan,
    hasVisibleData: originalPoints.length > 0, busy: scanActionBusy });
}

function updateExportButtons() {
  const actions = currentScanActions();
  $("scanButton").disabled = !actions.start;
  $("clearButton").disabled = !actions.clear;
  $("exportPly").disabled = !actions.export;
  $("exportObj").disabled = !actions.export;
  $("exportGlb").disabled = activeSource === "demo" || !actions.export;
  $("cancelScanButton").hidden = activeSource === "demo" || !isScanning;
  $("cancelScanButton").disabled = !actions.cancel;
  $("scanProfile").disabled = isScanning || scanActionBusy || ["starting", "stopping", "finalizing", "merging"].includes(scanPhase);
  const reason = $("scanActionReason");
  reason.textContent = actions.reason;
  reason.hidden = !actions.reason;
  $("scanButton").title = !actions.start ? actions.reason : "";
  $("clearButton").title = !actions.clear && actions.reason ? actions.reason : "Очистить просмотр";
  for (const id of ["exportPly", "exportObj", "exportGlb"]) {
    const button = $(id);
    const format = id.slice("export".length).toUpperCase();
    button.title = button.disabled && actions.reason ? actions.reason : `Экспорт в ${format}`;
  }
}

function startDemoScan() {
  isScanning = true;
  hasScan = false;
  demoScanFrames = 0;
  originalPoints = [];
  $("frameCount").textContent = "0";
  $("scanProgressFill").style.width = "0%";
  setScanButton();
  renderPoints();
  setTracking(
    "tracking",
    0.12,
    "",
    "Идёт демонстрация",
    "Синтетические кадры накапливаются — датчик не используется.",
  );
  $("sceneModeLabel").textContent = "ДЕМО-СКАНИРОВАНИЕ";
  demoScanTimer = setInterval(() => {
    if (!isScanning) return;
    demoScanFrames += 1;
    const ratio = Math.min(1, demoScanFrames / 12);
    const end = Math.floor(demoPoints.length * ratio);
    originalPoints = demoPoints.slice(0, end);
    hasScan = end > 0;
    $("frameCount").textContent = String(demoScanFrames);
    $("scanProgressFill").style.width = `${Math.round(ratio * 100)}%`;
    renderPoints();
    setTracking(
      "tracking",
      0.12 + (demoScanFrames % 3) * 0.025,
      "",
      "Демо-сканирование",
      "Поток и перемещение датчика не моделируются физически.",
    );
    if (ratio >= 1) {
      stopDemoScan(false);
      showToast("Демо-скан завершён. Данные можно экспортировать.", "success");
    }
  }, 520);
}

function stopDemoScan(reset = false) {
  if (demoScanTimer) clearInterval(demoScanTimer);
  demoScanTimer = null;
  isScanning = false;
  if (reset) {
    originalPoints = [];
    hasScan = false;
    demoScanFrames = 0;
    $("frameCount").textContent = "0";
    $("scanProgressFill").style.width = "0%";
  }
  setScanButton();
  renderPoints();
  setTracking(
    "stopped",
    0,
    "",
    "Сканирование остановлено",
    "Демо-данные остаются доступны для экспорта.",
  );
}

function startScanPolling() {
  if (!scanTimer) scanTimer = setInterval(refreshScan, 450);
}

function markScanDeviceReleased(error = "") {
  stopStream();
  deviceConnected = false;
  const profile = selectedProfile();
  if (profile) {
    profile.connected = false;
    profile.state = error ? "no_device" : "detected";
    profile.error = error;
    profile.hint = "";
  }
  updateSourcePanel();
}

async function startPhysicalScan(resumePassId = null) {
  if (!currentScanActions().start || isScanning) return;
  scanActionBusy = true;
  stopStream();
  updateExportButtons();
  try {
    await apiRequest("/api/scan/start", {
      method: "POST", body: { source: activeSource, profile: $("scanProfile").value,
        ...(resumePassId ? { resumePassId } : {}) },
    });
    hasScan = false;
    isScanning = true;
    scanPhase = "capturing";
    originalPoints = [];
    $("frameCount").textContent = "0";
    $("scanProgressFill").style.width = "0%";
    renderPoints();
    setScanButton();
    setTracking("tracking", 0, "", "Подготовка сканирования",
      "Медленно перемещайте датчик, сохраняя перекрытие кадров.");
    $("sceneModeLabel").textContent = "СКАНИРОВАНИЕ · TSDF";
    startScanPolling();
    await refreshScan();
    await loadPasses();
  } catch (error) {
    showToast(error.message, "warning");
    if (error.code === "timeout") {
      // The bridge may still be starting. Keep Stop/Cancel available until its
      // actual state is known rather than assuming that the capture failed.
      isScanning = true;
      scanPhase = "starting";
      startScanPolling();
      await refreshScan();
    } else {
      isScanning = false;
      // The server closes a failed sensor; a new frame request reports details.
      if (deviceConnected && !streamTimer) {
        streamTimer = setInterval(pollFrame, 300);
        void pollFrame();
      }
    }
  } finally {
    scanActionBusy = false;
    setScanButton();
    renderPasses();
  }
}

async function toggleScan() {
  if (activeSource === "demo") {
    if (isScanning) stopDemoScan();
    else startDemoScan();
    return;
  }
  if (!isScanning) {
    await startPhysicalScan();
    return;
  }
  scanActionBusy = true;
  updateExportButtons();
  try {
    const result = await apiRequest("/api/scan/stop", { method: "POST", body: {} });
    isScanning = false;
    scanPhase = result.phase || "idle";
    // ScanRunner releases the handle when capture stops. Reopen the stream
    // explicitly; export stays disabled until that succeeds.
    markScanDeviceReleased();
    startScanPolling();
    await refreshScan();
    if (scanPhase !== "error") await connectSelectedSource({ reconnect: true });
    showToast("Захват остановлен. Идёт обработка прохода.", "info");
  } catch (error) {
    showToast(error.message, "warning");
    await refreshScan();
  } finally {
    scanActionBusy = false;
    setScanButton();
    renderPasses();
  }
}

async function cancelScan() {
  if (!isScanning || activeSource === "demo" || scanActionBusy) return;
  scanActionBusy = true;
  updateExportButtons();
  try {
    const result = await apiRequest("/api/scan/cancel", { method: "POST", body: {} });
    if (scanTimer) clearInterval(scanTimer);
    scanTimer = null;
    isScanning = false;
    scanPhase = "idle";
    hasScan = false;
    originalPoints = [];
    $("frameCount").textContent = "0";
    $("scanProgressFill").style.width = "0%";
    markScanDeviceReleased();
    renderPoints();
    await loadPasses();
    await connectSelectedSource({ reconnect: true });
    showToast(result.keptPass
      ? "Захват отменён; продолженный проход сохранён как прерванный."
      : "Проход отменён; его временные файлы удалены.", "info");
  } catch (error) {
    showToast(error.message, "warning");
    await refreshScan();
  } finally {
    scanActionBusy = false;
    setScanButton();
    renderPasses();
  }
}

async function refreshScan() {
  if (scanBusy) return;
  scanBusy = true;
  try {
    const data = await apiRequest("/api/scan");
    const previousPhase = scanPhase;
    const wasRunning = isScanning;
    isScanning = Boolean(data.running);
    scanPhase = data.phase || (isScanning ? "capturing" : "idle");
    if (wasRunning && !isScanning) markScanDeviceReleased(data.error || "");
    hasScan = scanPhase === "ready" && Boolean(data.lastResultId);
    if (Array.isArray(data.points) && (data.points.length > 0 || scanPhase === "ready")) {
      originalPoints = data.points.map(([x, y, z, depth]) => [x, y + 1.25, z, depth]);
      renderPoints();
    }
    if (data.preview) {
      const hasRgb = paintRgb(data.preview.color);
      const hasDepth = paintDepth(data.preview.depth);
      $("rgbDescription").textContent = hasRgb ? "Кадр с Kinect 360" : "RGB-поток недоступен";
      $("depthDescription").textContent = hasDepth ? "Карта глубины в реальном времени" : "Карта глубины недоступна";
      $("rgbFrameLabel").textContent = hasRgb ? "LIVE · RGB" : "NO RGB";
      $("depthFrameLabel").textContent = hasDepth ? "LIVE · DEPTH" : "NO DEPTH";
      $("feedState").textContent = "ПОТОК АКТИВЕН · СКАНИРОВАНИЕ";
    }
    const performance = data.performance;
    $("fpsMetric").textContent = `${formatFps(performance?.captureFps)} / ${formatFps(performance?.processingFps)}`;
    $("droppedMetric").textContent = Number.isFinite(performance?.droppedFrames) ? String(performance.droppedFrames) : "—";
    $("sensorDroppedMetric").textContent = Number.isFinite(performance?.sensorDroppedFrames) ? String(performance.sensorDroppedFrames) : "—";
    $("keyframesMetric").textContent = Number.isFinite(data.keyframes) ? String(data.keyframes) : "—";
    $("activeProfileMetric").textContent = data.pipeline?.profile === "max_fps" ? "Максимум FPS"
      : data.pipeline?.profile === "standard" ? "Стандарт" : "—";
    $("trackedFpsMetric").textContent = `${formatFps(performance?.trackingFps)} / ${formatFps(performance?.integrationFps)}`;
    $("acceptedMetric").textContent = Number.isFinite(data.quality?.acceptedRatio)
      ? `${Math.round(data.quality.acceptedRatio * 100)}%` : "—";
    $("frameChecksMetric").textContent = formatFrameChecks(data.quality);
    $("cpuMetric").textContent = formatCpu(data.pipeline?.cpu);
    $("frameCount").textContent = String(data.frames || 0);
    $("scanProgressFill").style.width = `${Math.min(100, (Number(data.frames || 0) / 120) * 100)}%`;
    const tracking = data.tracking || (isScanning ? "tracking" : "stopped");
    const statusTitles = {
      lost: "Отслеживание потеряно", too_fast: "Слишком быстрое движение",
      few_valid_pixels: "Мало валидных пикселей", sensor_covered: "Датчик, возможно, закрыт",
      memory_budget_reached: "Достигнут лимит памяти",
    };
    const failedPasses = (data.resultMetrics?.mergedPasses || []).filter((pass) => !pass.ok);
    const mergeWarning = failedPasses.length
      ? `Не удалось совместить проходы: ${failedPasses.map((pass) => `${pass.passId}: ${pass.reason || "нет совпадения"}`).join("; ")}. Они не вошли в результат.` : "";
    const warning = [data.error, data.warning, data.hint, mergeWarning].filter(Boolean).filter((v, i, arr) => arr.indexOf(v) === i).join(" ");
    const title = scanPhase === "starting" ? "Подготовка сканирования" :
      scanPhase === "finalizing" ? "Обработка прохода" :
      scanPhase === "merging" ? "Объединение проходов" :
      scanPhase === "error" ? "Ошибка сканирования" :
      statusTitles[tracking] || (isScanning ? "Идёт сканирование" : hasScan ? "Результат готов" : "Ожидание");
    const text = scanPhase === "starting" ? "Открытие датчика или восстановление сохранённых кадров ещё выполняется." :
      scanPhase === "finalizing" ? "Извлечение сетки и проекция цвета выполняются в фоне." :
      scanPhase === "merging" ? "Выполняется выравнивание сохранённых проходов." :
      isScanning ? "Сохраняйте перекрытие между кадрами и избегайте резких поворотов." :
      hasScan ? "Результат сохранён; экспорт содержит сетку и вершинные цвета." : "Подключите источник и начните новый проход.";
    setTracking(tracking, data.speedMps ?? null, warning, title, text);
    if (hasScan) $("sceneModeLabel").textContent = data.lastResultId === "merged" ? "ОБЪЕДИНЁННЫЕ ПРОХОДЫ" : "РЕЗУЛЬТАТ ПРОХОДА";
    const processing = isScanInProgress(scanPhase, isScanning);
    if (!processing && scanTimer) { clearInterval(scanTimer); scanTimer = null; }
    if (!processing && previousPhase !== scanPhase) {
      await loadPasses();
      if (data.error) showToast(data.error, "warning");
    }
    setScanButton();
  } catch (error) {
    if (isScanInProgress(scanPhase, isScanning)) {
      // A failed status request is not evidence that capture stopped. Keep
      // polling and allow Stop/Cancel until the server confirms the outcome.
      startScanPolling();
      setTracking("idle", 0, error.message, "Состояние сканирования неизвестно",
        "Ожидаем ответ моста; захват может продолжаться. При необходимости остановите или отмените его.");
    } else {
      if (scanTimer) clearInterval(scanTimer);
      scanTimer = null;
      scanPhase = "error";
      setTracking("idle", 0, error.message, "Нет ответа о сканировании", "Повторите запрос после восстановления моста.");
    }
    setScanButton();
    showToast(`Ошибка получения состояния сканирования. ${error.message}`, "warning");
  } finally {
    scanBusy = false;
    updateExportButtons();
    renderPasses();
  }
}

async function clearScan() {
  if (activeSource === "demo") {
    stopDemoScan(true);
    $("scanProgressFill").style.width = "0%";
    $("sceneModeLabel").textContent = "ДЕМО-СЦЕНА · ОЧИЩЕНО";
    setTracking("demo", 0, "", "Данные очищены", "Запустите демо-сканирование, чтобы собрать сцену заново.");
    return;
  }
  if (!currentScanActions().clear) return;
  try {
    await apiRequest("/api/scan/clear", { method: "POST", body: {} });
    scanPhase = "idle";
    hasScan = false;
    originalPoints = [];
    $("frameCount").textContent = "0";
    $("scanProgressFill").style.width = "0%";
    setScanButton();
    renderPoints();
    setTracking("tracking", 0, "", "Просмотр очищен", "Сохранённые проходы остаются на диске.");
    showToast("Просмотр очищен. Сохранённые проходы не удалены.", "success");
  } catch (error) {
    showToast(error.message, "warning");
  }
}

async function loadPasses() {
  if (bridgeHealth?.ok !== true) return;
  try {
    const result = await apiRequest("/api/scan/passes");
    savedPasses = result.passes || [];
    const finished = new Set(savedPasses.filter((pass) => pass.state === "finished").map((pass) => pass.id));
    selectedPassIds = new Set([...selectedPassIds].filter((id) => finished.has(id)));
    renderPasses();
  } catch (error) {
    $("passesList").textContent = `Не удалось получить список проходов. ${error.message}`;
  }
}

function renderPasses() {
  const list = $("passesList");
  if (!list) return;
  const environment = environmentStatus(setupState, bridgeHealth);
  const busy = isScanning || scanActionBusy || ["finalizing", "merging"].includes(scanPhase);
  const labels = { finished: "Завершён", interrupted: "Прерван", running: "Идёт захват", empty: "Пустой" };
  if (!savedPasses.length) {
    list.innerHTML = '<p class="passes-empty">Сохранённых проходов пока нет.</p>';
  } else {
    list.innerHTML = savedPasses.map((pass) => {
      const id = escapeHtml(pass.id);
      const done = pass.state === "finished";
      const resumable = pass.state === "interrupted" && pass.source === activeSource;
      const status = labels[pass.state] || pass.state;
      const selectable = done && environment.ready && !busy;
      return `<div class="pass-row"><label><input type="checkbox" data-pass-check="${id}" ${selectedPassIds.has(pass.id) ? "checked" : ""} ${selectable ? "" : "disabled"} aria-label="Выбрать ${escapeHtml(pass.name || pass.id)} для объединения"><span><strong>${escapeHtml(pass.name || pass.id)}</strong><small>${escapeHtml(status)} · кадров: ${Number(pass.frames || 0)} · ${id}</small></span></label><div class="pass-buttons">${done ? `<button data-pass-action="select" data-pass-id="${id}" ${environment.ready && !busy ? "" : "disabled"}>Показать</button>` : ""}${resumable ? `<button data-pass-action="resume" data-pass-id="${id}" ${currentScanActions().start && !busy ? "" : "disabled"}>Продолжить</button>` : ""}<button data-pass-action="delete" data-pass-id="${id}" ${environment.ready && !busy ? "" : "disabled"}>Удалить</button></div></div>`;
    }).join("");
  }
  $("mergePassesButton").disabled = !environment.ready || !deviceConnected || busy || selectedPassIds.size < 2;
}

$("passesList").onchange = (event) => {
  const id = event.target.dataset.passCheck;
  if (!id) return;
  if (event.target.checked) selectedPassIds.add(id); else selectedPassIds.delete(id);
  renderPasses();
};
$("passesList").onclick = async (event) => {
  const button = event.target.closest("button[data-pass-action]");
  if (!button) return;
  const id = button.dataset.passId;
  const action = button.dataset.passAction;
  if (action === "resume") { await startPhysicalScan(id); return; }
  if (action === "delete" && !window.confirm("Удалить сохранённый проход и его файлы?")) return;
  try {
    await apiRequest(`/api/scan/passes/${action}`, { method: "POST", body: { passId: id } });
    await loadPasses();
    await refreshScan();
    if (action === "delete") showToast("Проход удалён.", "info");
  } catch (error) { showToast(error.message, "warning"); }
};
$("refreshPassesButton").onclick = loadPasses;
$("mergePassesButton").onclick = async () => {
  try {
    await apiRequest("/api/scan/merge", { method: "POST", body: { passIds: [...selectedPassIds] } });
    scanPhase = "merging";
    hasScan = false;
    startScanPolling();
    await refreshScan();
  } catch (error) { showToast(error.message, "warning"); }
};

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1200);
}

function exportDemo(format) {
  const visible = [];
  const positions = pointGeometry.getAttribute("position")?.array || [];
  for (let i = 0; i < positions.length; i += 3)
    visible.push([positions[i], positions[i + 1], positions[i + 2]]);
  if (!visible.length) {
    showToast(
      "Нет данных для экспорта. Сначала выполните сканирование.",
      "warning",
    );
    return;
  }
  let text;
  if (format === "obj") {
    text =
      "# Depth Studio demo point cloud; vertices only, no mesh faces\no DepthStudioDemo\n" +
      visible
        .map(
          (p) => `v ${p[0].toFixed(6)} ${p[1].toFixed(6)} ${p[2].toFixed(6)}`,
        )
        .join("\n") +
      "\n";
  } else {
    text =
      `ply\nformat ascii 1.0\ncomment Depth Studio demo point cloud; no mesh faces\nelement vertex ${visible.length}\nproperty float x\nproperty float y\nproperty float z\nend_header\n` +
      visible
        .map((p) => `${p[0].toFixed(6)} ${p[1].toFixed(6)} ${p[2].toFixed(6)}`)
        .join("\n") +
      "\n";
  }
  downloadBlob(
    new Blob([text], { type: "text/plain;charset=utf-8" }),
    `depth-studio-demo.${format}`,
  );
  showToast(`Демо-облако экспортировано в ${format.toUpperCase()}.`, "success");
}

async function exportScan(format) {
  if (activeSource === "demo") {
    exportDemo(format);
    return;
  }
  if (!currentScanActions().export) {
    showToast("Подключите источник и дождитесь завершения обработки прохода.", "warning");
    return;
  }
  try {
    const response = await fetch(`/api/export?format=${format}`, {
      cache: "no-store",
    });
    if (!response.ok) {
      let details = {};
      try {
        details = await response.json();
      } catch {
        /* no JSON body */
      }
      throw new Error(details.error || `Ошибка экспорта (${response.status}).`);
    }
    const blob = await response.blob();
    downloadBlob(blob, `depth-studio-scan.${format}`);
    showToast(
      `Результат экспортирован в ${format.toUpperCase()}.`,
      "success",
    );
  } catch (error) {
    showToast(error.message, "warning");
  }
}

$("sourceSelect").onchange = async (event) => {
  if (isScanning || scanActionBusy || deviceActionBusy || ["finalizing", "merging"].includes(scanPhase)) {
    showToast("Дождитесь окончания текущего прохода перед сменой источника.", "warning");
    event.target.value = activeSource;
    return;
  }
  const nextSource = event.target.value;
  const previousSource = activeSource;
  const wasConnected = deviceConnected;
  stopStream();
  if (wasConnected && previousSource !== "demo") {
    try { await apiRequest("/api/devices/disconnect", { method: "POST", body: { source: previousSource } }); }
    catch (error) { showToast(error.message, "warning"); }
  }
  activeSource = nextSource;
  deviceConnected = false;
  hasScan = false;
  originalPoints = activeSource === "demo" ? demoPoints.slice() : [];
  $("frameCount").textContent = "0";
  $("scanProgressFill").style.width = "0%";
  $("sceneModeLabel").textContent = activeSource === "demo" ? "ДЕМО-СЦЕНА" : "ИСТОЧНИК НЕ ПОДКЛЮЧЁН";
  renderPoints();
  if (activeSource === "demo") renderDemoFeeds(); else drawNoSignal();
  updateSourcePanel();
  setScanButton();
  if (activeSource !== "demo" && bridgeHealth?.ok) {
    await loadPasses();
    await refreshScan();
  }
};
$("detectButton").onclick = probeDevices;
$("connectButton").onclick = () => connectSelectedSource();
$("scanButton").onclick = toggleScan;
$("cancelScanButton").onclick = cancelScan;
$("clearButton").onclick = clearScan;
$("exportPly").onclick = () => exportScan("ply");
$("exportObj").onclick = () => exportScan("obj");
$("exportGlb").onclick = () => exportScan("glb");

$("pointSize").oninput = (event) => {
  const value = Number(event.target.value);
  pointMaterial.size = value * 0.0135;
  $("sizeValue").textContent = value.toFixed(1);
};
$("depthRange").oninput = (event) => {
  const value = Number(event.target.value);
  $("depthValue").textContent = `${value.toFixed(1)} м`;
  $("depthMetric").textContent = `0.5 – ${value.toFixed(1)} м`;
  renderPoints();
};
$("palette").onchange = renderPoints;
$("gridToggle").onchange = (event) => {
  grid.visible = event.target.checked;
};

function openHelp() {
  $("helpModal").classList.add("open");
}
function closeHelp() {
  $("helpModal").classList.remove("open");
}
$("helpBtn").onclick = openHelp;
$("requirementsBtn").onclick = openHelp;
$("closeModal").onclick = closeHelp;
$("modalDone").onclick = closeHelp;
$("helpModal").onclick = (event) => {
  if (event.target === $("helpModal")) closeHelp();
};
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeHelp();
});

/* --- Среда Depth Studio: экран настройки, прогресс и журнал --------------- */
let setupState = null;
let setupPollTimer = null;

async function fetchSetupState() {
  try {
    const state = await apiRequest("/app/setup", { timeoutMs: 4000 });
    return typeof state?.phase === "string" ? state : null;
  } catch {
    return null;
  }
}

function renderEnvironmentSummary(state) {
  const summary = $("envSummary");
  const detail = $("envDetail");
  if (!summary || !detail) return;
  const environment = state?.environment;
  const python = state?.python;
  const packages = state?.packages || {};
  const numpyVersion = packages.numpy?.version || environment?.packages?.numpy || null;
  const open3dVersion = packages.open3d?.version || environment?.packages?.open3d || null;
  const envDir = state?.envDir || "%LOCALAPPDATA%\\DepthStudio";
  if (!state || (!environment && state.phase === undefined)) {
    summary.textContent =
      "Среда Python настраивается установщиком Windows в %LOCALAPPDATA%\\DepthStudio. Веб-версия работает в деморежиме.";
    detail.textContent = "";
    return;
  }
  const isolated = environment?.isolated ?? Boolean(python?.executable);
  const headline = state.phase ? setupHeadline(state, bridgeHealth) :
    bridgeHealth?.ok ? "Python-мост Depth Studio запущен" : "Ожидание Python-моста Depth Studio";
  summary.textContent = `${headline} · ${envDir}`;
  const pieces = [
    python?.version ? `Python ${python.version}` : null,
    numpyVersion ? `NumPy ${numpyVersion}` : "NumPy не проверен",
    open3dVersion ? `Open3D ${open3dVersion}` : "Open3D недоступен",
    isolated ? "изолированная среда" : "интерпретатор не изолирован",
  ].filter(Boolean);
  detail.textContent = pieces.join(" · ");
}

function renderSetupBanner(state) {
  const banner = $("setupBanner");
  if (!banner) return;
  if (!state) {
    banner.hidden = true; // web/demo mode has no desktop setup service
    renderEnvironmentSummary(bridgeHealth ? { environment: bridgeHealth.environment } : null);
    updateExportButtons();
    return;
  }
  setupState = state;
  const setupReady = state.phase === "ready";
  const bridgeReady = Boolean(state.bridge?.ready) && bridgeHealth?.ok === true;
  const open3dMissing = state.packages?.open3d?.available === false ||
    (bridgeHealth?.ok === true && bridgeHealth.modules?.open3d !== true);
  const numpyMissing = state.packages?.numpy?.available === false ||
    (bridgeHealth?.ok === true && bridgeHealth.modules?.numpy !== true);
  renderEnvironmentSummary({ ...state, environment: bridgeHealth?.environment });
  if (setupReady && bridgeReady && !open3dMissing && !numpyMissing) {
    banner.hidden = true;
    updateExportButtons();
    return;
  }
  banner.hidden = false;
  banner.dataset.phase = setupReady && !bridgeReady ? "bridge" : setupReady ? "warning" : state.phase || "idle";
  $("setupTitle").textContent = setupReady && bridgeReady && (open3dMissing || numpyMissing)
    ? `${open3dMissing ? "Open3D" : "NumPy"} недоступен: сканирование с датчиком выключено`
    : setupHeadline(state, bridgeHealth);
  const parts = [];
  if (state.phase === "failed" && state.error?.message) parts.push(state.error.message);
  if (state.phase === "failed" && state.error?.hint) parts.push(state.error.hint);
  if (setupReady && !bridgeReady) {
    parts.push(state.bridge?.error || "Python-мост запускается или ещё не ответил; деморежим доступен.");
  }
  if (setupReady && bridgeReady && open3dMissing) {
    parts.push("Сканирование с датчиком требует Open3D. Проверьте Microsoft Visual C++ 2015–2022 Redistributable (x64), затем повторите настройку. Деморежим доступен.");
  }
  if (setupReady && bridgeReady && numpyMissing) parts.push("Восстановите среду Depth Studio, чтобы установить NumPy.");
  if (!setupReady) parts.push("Деморежим работает сразу: синтетическая сцена и экспорт PLY/OBJ доступны без датчика.");
  $("setupText").textContent = parts.join(" ");
  renderSetupRemedies(state);
  renderSetupDetails(state);
  const running = state.phase === "running";
  $("setupProgressWrap").hidden = !running;
  $("setupProgressFill").style.width = `${Math.max(0, Math.min(100, state.progress || 0))}%`;
  $("setupRetryBtn").hidden = setupReady && bridgeReady && !open3dMissing && !numpyMissing;
  $("setupRetryBtn").disabled = running;
  $("setupRepairBtn").disabled = running;
  updateExportButtons();
}

function renderSetupRemedies(state) {
  const container = $("setupRemedies");
  const list = $("setupRemedyList");
  if (!container || !list) return;
  const remedies = Array.isArray(state?.error?.remedies) ? state.error.remedies : [];
  container.hidden = remedies.length === 0;
  list.innerHTML = remedies
    .map(
      (remedy) => `<li data-relevant="${remedy.relevant ? "true" : "false"}"><strong>${escapeHtml(
        remedy.title,
      )}</strong><span>${escapeHtml(remedy.text)}</span></li>`,
    )
    .join("");
}

function renderSetupDetails(state) {
  const details = $("setupDetails");
  const summary = $("setupDetailsSummary");
  const text = $("setupDetailsText");
  if (!details || !summary || !text) return;
  const error = state?.error;
  const body = error?.details || "";
  details.hidden = !body;
  text.textContent = body;
  if (body) {
    const lines = body.split(/\r?\n/).length;
    summary.textContent = `Технические подробности — вывод pip, ${lines} стр. (${
      error?.cause ? `причина: ${error.cause}` : "см. журнал"
    })`;
  }
}

async function copySetupLog() {
  try {
    const response = await fetch("/app/setup/copy-log", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) {
      throw new Error(payload.reason || "Журнал доступен только в установленном приложении.");
    }
    showToast(`Журнал и подробности ошибки скопированы (${payload.characters} символов).`, "success");
  } catch (error) {
    showToast(error.message, "warning");
  }
}

function scheduleSetupPoll(delay = 3000) {
  clearTimeout(setupPollTimer);
  setupPollTimer = setTimeout(async () => {
    const wasOnline = bridgeHealth?.ok === true;
    const [state, health] = await Promise.all([
      fetchSetupState(),
      apiRequest("/api/health").catch(() => null),
    ]);
    if (state) setupState = state;
    bridgeHealth = health?.ok === true ? health : null;
    if (bridgeHealth && (!wasOnline || !devicesAvailable)) {
      await loadDevices(bridgeHealth);
      if (activeSource !== "demo") await loadPasses();
    } else if (!bridgeHealth && wasOnline) {
      devicesAvailable = false;
      stopStream();
      deviceConnected = false;
      $("serverNote").innerHTML = '<span class="server-note-dot"></span><span>Локальный мост не отвечает · демо доступно</span>';
      updateSourcePanel();
    }
    renderSetupBanner(setupState);
    updateExportButtons();
    renderPasses();
    // Continue checking even after startup: the bridge can exit or recover later.
    scheduleSetupPoll(setupState?.phase === "running" ? 1200 : 3000);
  }, delay);
}

async function runSetupAction(mode) {
  try {
    const response = await fetch("/app/setup/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Не удалось запустить настройку среды.");
    if (payload.started === false) {
      showToast(payload.reason || "Настройка уже выполняется.", "info");
    } else {
      showToast(
        mode === "repair"
          ? "Восстановление среды запущено. Прогресс — в этом окне и в журнале установки."
          : "Повторная настройка среды запущена.",
        "success",
      );
    }
    renderSetupBanner(payload.state || setupState);
    scheduleSetupPoll(700);
  } catch (error) {
    showToast(error.message, "warning");
  }
}

async function revealSetupLog() {
  try {
    const response = await fetch("/app/setup/reveal", { method: "POST" });
    if (!response.ok) throw new Error("Журнал доступен только в установленном приложении.");
    showToast("Открываю папку журнала установки.", "info");
  } catch (error) {
    showToast(error.message, "warning");
  }
}

$("setupRetryBtn").onclick = () => runSetupAction("retry");
$("setupRepairBtn").onclick = () => runSetupAction("repair");
$("setupLogBtn").onclick = revealSetupLog;
$("setupCopyLogBtn").onclick = copySetupLog;
$("envRetryBtn").onclick = () => runSetupAction("retry");
$("envLogBtn").onclick = revealSetupLog;

function initEnvironmentUi() {
  devices = fallbackDevices();
  renderSourceOptions();
  updateSourcePanel();
  scheduleSetupPoll(0);
}

// Demo is immediately usable without Python, a camera, or a depth sensor.
originalPoints = demoPoints.slice();
renderPoints();
renderDemoFeeds();
updateSourcePanel();
initEnvironmentUi();
