import * as THREE from "three";
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
      <span class="version-tag">BETA · 1.1</span>
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
              <button class="button-secondary clear-button" id="clearButton" title="Очистить собранные данные"><i data-lucide="rotate-ccw"></i><span>Очистить</span></button>
              <div class="export-buttons" aria-label="Экспорт облака точек">
                <button id="exportPly" title="Экспортировать облако в PLY"><i data-lucide="download"></i> PLY</button>
                <button id="exportObj" title="Экспортировать вершины в OBJ">OBJ</button>
              </div>
            </div>
          </div>
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
          <div class="tracking-metrics"><div><span>СКОРОСТЬ</span><strong id="speedMetric">— м/с</strong></div><div><span>СОСТОЯНИЕ</span><strong id="stateMetric">Демо</strong></div></div>
          <div class="scan-progress"><div class="scan-progress-fill" id="scanProgressFill"></div></div>
          <p class="movement-hint">Двигайте датчик плавно, примерно до <b>0,45 м/с</b>. Оставляйте в кадре часть уже увиденной поверхности.</p>
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
    <footer class="page-footer"><span>DEPTH STUDIO <b>·</b> ДЕМО И ЭКСПЕРИМЕНТАЛЬНОЕ СКАНИРОВАНИЕ</span><span>ОБЛАКО ТОЧЕК — НЕ ПОЛИГОНАЛЬНАЯ МОДЕЛЬ</span></footer>
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

async function apiRequest(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      body:
        options.body && typeof options.body !== "string"
          ? JSON.stringify(options.body)
          : options.body,
    });
  } catch (error) {
    throw new Error(
      "Python-мост Depth Studio недоступен. Деморежим работает без него; чтобы подключить датчик, завершите настройку среды Depth Studio.",
    );
  }
  let payload;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
  } else {
    payload = {};
  }
  if (!response.ok) {
    const base =
      payload.error ||
      (response.status >= 500
        ? "Python-мост Depth Studio недоступен. Деморежим работает без него; чтобы подключить датчик, завершите настройку среды Depth Studio."
        : `Ошибка локального моста (${response.status}).`);
    // Адаптер возвращает и подсказку — она уточняет, что именно проверить.
    const hint = typeof payload.hint === "string" && payload.hint ? ` ${payload.hint}` : "";
    throw new Error(`${base}${hint}`);
  }
  return payload;
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

async function loadDevices() {
  try {
    const [health, response] = await Promise.all([
      apiRequest("/api/health"),
      apiRequest("/api/devices"),
    ]);
    devices = response.devices || fallbackDevices();
    const env = health.environment || null;
    const envText = env
      ? `Python ${escapeHtml(env.version || health.python || "")} · ${env.isolated ? "среда Depth Studio" : "системный интерпретатор"}`
      : `Python ${escapeHtml(health.python || "")}`;
    $("serverNote").innerHTML = `<span class="server-note-dot online"></span><span>Локальный мост запущен · ${envText}</span>`;
    renderEnvironmentSummary({ ...(setupState || {}), environment: env, bridge: { ready: true } });
  } catch {
    devices = fallbackDevices();
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
    const state = deviceConnected
      ? "connected"
      : profile.connected
        ? "detected"
        : profile.state || "not_checked";
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
      profile.error ||
      profile.limitations ||
      "Выбран экспериментальный источник глубины.";
    $("verificationLabel").innerHTML = profile.verifiedOnHardware
      ? "<span>✓</span><span>Этот модуль прошёл аппаратную проверку.</span>"
      : "<span>!</span><span>Модуль не проверен на реальном датчике; совместимость не гарантируется.</span>";
    $("connectButton").innerHTML = deviceConnected
      ? '<i data-lucide="check"></i><span>Отключить датчик</span>'
      : profile.connected
        ? '<i data-lucide="radio"></i><span>Подключить поток</span>'
        : '<i data-lucide="radio"></i><span>Подключить выбранный источник</span>';
    $("connectButton").disabled = false;
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
    setTracking(
      deviceConnected ? "tracking" : "idle",
      0,
      "",
      deviceConnected
        ? "Поток подключён"
        : profile.connected
          ? "Датчик обнаружен"
          : "Источник не подключён",
      deviceConnected
        ? "Поток глубины поступает в приложение."
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
  updateExportButtons();
}

function setTracking(status, speed, warning, title = "", text = "") {
  const labels = {
    demo: "Демо-режим",
    idle: "Ожидание",
    tracking: "Отслеживание",
    moving: "Двигайтесь медленнее",
    lost: "Трекинг потерян",
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
    status === "demo" || status === "idle" || status === "stopped"
      ? "— м/с"
      : `${Number(speed || 0).toFixed(2)} м/с`;
  $("warningBox").hidden = !warning;
  if (warning) $("warningText").textContent = warning;
  $("liveIndicator").classList.toggle(
    "is-live",
    status === "tracking" || status === "moving",
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
    showToast(
      "Остановите сканирование перед повторным обнаружением датчиков.",
      "warning",
    );
    return;
  }
  const button = $("detectButton");
  button.disabled = true;
  button.classList.add("is-busy");
  try {
    const result = await apiRequest("/api/devices/probe", {
      method: "POST",
      body: {},
    });
    devices = result.devices || devices;
    const connected = result.connected || [];
    // Найденный датчик становится активным источником; при двух адаптерах Kinect 360
    // сервер отдаёт первым рекомендуемый (Microsoft Kinect SDK).
    if (connected.length && !isScanning && activeSource === "demo") {
      activeSource = connected[0];
      $("sourceSelect").value = connected[0];
    }
    renderSourceOptions();
    updateSourcePanel();
    if (connected.length) {
      showToast(
        `Обнаружен датчик: ${devices.find((device) => device.id === connected[0])?.name || connected[0]}.`,
        "success",
      );
    } else {
      const preferred =
        devices.find((device) => device.id === result.preferredSource) ||
        devices.find((device) => device.id === "kinect_v1_sdk") ||
        devices.find((device) => device.id === "kinect_v1");
      showToast(
        preferred?.error ||
          "Совместимый датчик не обнаружен. Проверьте питание, USB и драйверы.",
        "warning",
      );
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
  streamBusy = false;
}

async function pollFrame() {
  if (streamBusy || activeSource === "demo" || hasScan) return;
  streamBusy = true;
  try {
    const data = await apiRequest(
      `/api/frame?source=${encodeURIComponent(activeSource)}`,
    );
    deviceConnected = true;
    const profile = devices.find((device) => device.id === activeSource);
    if (profile) {
      profile.connected = true;
      profile.state = "connected";
      profile.error = "";
    }
    if (Array.isArray(data.points)) {
      originalPoints = data.points.map(([x, y, z, depth]) => [
        x,
        y + 1.25,
        z,
        depth,
      ]);
      renderPoints();
    }
    const hasRgb = paintRgb(data.color);
    const hasDepth = paintDepth(data.depth);
    $("rgbDescription").textContent = hasRgb
      ? "Кадр с Kinect 360"
      : "RGB-поток недоступен";
    $("depthDescription").textContent = hasDepth
      ? "Глубина в реальном времени"
      : "Карта глубины недоступна";
    $("rgbFrameLabel").textContent = hasRgb ? "LIVE · RGB" : "NO RGB";
    $("depthFrameLabel").textContent = hasDepth ? "LIVE · DEPTH" : "NO DEPTH";
    $("feedState").textContent = "ПОТОК АКТИВЕН";
    $("sceneModeLabel").textContent = "ПОТОК ДАТЧИКА";
    setTracking(
      "tracking",
      0,
      "",
      "Датчик подключён",
      "Поток глубины поступает в приложение.",
    );
    $("frameCount").textContent = "LIVE";
  } catch (error) {
    stopStream();
    deviceConnected = false;
    const profile = devices.find((device) => device.id === activeSource);
    if (profile) {
      profile.connected = false;
      profile.state = "no_device";
      profile.error = error.message;
    }
    updateSourcePanel();
    showToast(`Поток датчика остановлен. ${error.message}`, "warning");
  } finally {
    streamBusy = false;
  }
}

async function connectSelectedSource() {
  if (isScanning) {
    showToast(
      "Сначала остановите сканирование, затем отключайте датчик.",
      "warning",
    );
    return;
  }
  if (activeSource === "demo") {
    stopStream();
    deviceConnected = false;
    hasScan = false;
    originalPoints = demoPoints.slice();
    renderPoints();
    updateSourcePanel();
    showToast(
      "Демо-сцена готова. Подключение датчика не требуется.",
      "success",
    );
    return;
  }
  if (deviceConnected) {
    stopStream();
    deviceConnected = false;
    hasScan = false;
    originalPoints = [];
    renderPoints();
    const profile = devices.find((device) => device.id === activeSource);
    if (profile) {
      profile.connected = false;
      profile.state = "not_checked";
    }
    updateSourcePanel();
    drawNoSignal();
    return;
  }
  const button = $("connectButton");
  button.disabled = true;
  button.classList.add("is-busy");
  try {
    const result = await apiRequest("/api/devices/probe", {
      method: "POST",
      body: {},
    });
    devices = result.devices || devices;
    renderSourceOptions();
    const profile = devices.find((device) => device.id === activeSource);
    if (!profile?.connected) {
      updateSourcePanel();
      throw new Error(
        profile?.error ||
          "Выбранный датчик не обнаружен. Проверьте драйвер и подключение.",
      );
    }
    deviceConnected = true;
    updateSourcePanel();
    await pollFrame();
    if (!deviceConnected) return;
    if (!streamTimer) streamTimer = setInterval(pollFrame, 300);
    showToast("Датчик подключён. Перемещайте его медленно.", "success");
  } catch (error) {
    showToast(error.message, "warning");
  } finally {
    button.disabled = false;
    button.classList.remove("is-busy");
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
}

function updateExportButtons() {
  const enabled = activeSource === "demo" ? originalPoints.length > 0 : hasScan;
  $("exportPly").disabled = !enabled;
  $("exportObj").disabled = !enabled;
  $("clearButton").disabled =
    originalPoints.length === 0 && !hasScan && !isScanning;
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

async function toggleScan() {
  if (isScanning) {
    if (activeSource === "demo") {
      stopDemoScan();
      return;
    }
    try {
      await apiRequest("/api/scan/stop", { method: "POST", body: {} });
      isScanning = false;
      if (scanTimer) clearInterval(scanTimer);
      scanTimer = null;
      await refreshScan();
      setScanButton();
      showToast(
        "Сканирование остановлено. Облако сохранено для экспорта.",
        "success",
      );
    } catch (error) {
      showToast(error.message, "warning");
    }
    return;
  }
  if (activeSource === "demo") {
    startDemoScan();
    return;
  }
  if (!deviceConnected) {
    showToast("Сначала выберите и подключите обнаруженный датчик.", "warning");
    return;
  }
  try {
    const result = await apiRequest("/api/scan/start", {
      method: "POST",
      body: { source: activeSource },
    });
    hasScan = false;
    isScanning = true;
    $("frameCount").textContent = "0";
    $("scanProgressFill").style.width = "0%";
    setScanButton();
    setTracking(
      "tracking",
      0,
      "",
      "Подготовка сканирования",
      "Медленно перемещайте датчик, сохраняя перекрытие кадров.",
    );
    $("sceneModeLabel").textContent = "СКАНИРОВАНИЕ";
    if (streamTimer) {
      clearInterval(streamTimer);
      streamTimer = null;
    }
    scanTimer = setInterval(refreshScan, 450);
    await refreshScan();
  } catch (error) {
    isScanning = false;
    setScanButton();
    showToast(error.message, "warning");
  }
}

async function refreshScan() {
  if (scanBusy) return;
  scanBusy = true;
  try {
    const data = await apiRequest("/api/scan");
    isScanning = Boolean(data.running);
    if (Array.isArray(data.points) && data.points.length > 0) {
      originalPoints = data.points.map(([x, y, z, depth]) => [
        x,
        y + 1.25,
        z,
        depth,
      ]);
      hasScan = true;
      renderPoints();
    }
    if (data.preview) {
      const hasRgb = paintRgb(data.preview.color);
      const hasDepth = paintDepth(data.preview.depth);
      $("rgbDescription").textContent = hasRgb
        ? "Кадр с Kinect 360"
        : "RGB-поток недоступен";
      $("depthDescription").textContent = hasDepth
        ? "Карта глубины в реальном времени"
        : "Карта глубины недоступна";
      $("rgbFrameLabel").textContent = hasRgb ? "LIVE · RGB" : "NO RGB";
      $("depthFrameLabel").textContent = hasDepth ? "LIVE · DEPTH" : "NO DEPTH";
      $("feedState").textContent = "ПОТОК АКТИВЕН · СКАНИРОВАНИЕ";
    }
    $("frameCount").textContent = String(data.frames || 0);
    $("scanProgressFill").style.width =
      `${Math.min(100, (Number(data.frames || 0) / 120) * 100)}%`;
    const tracking = data.tracking || (data.running ? "tracking" : "stopped");
    setTracking(
      tracking,
      data.speedMps || 0,
      data.warning || data.error || "",
      tracking === "lost"
        ? "Отслеживание потеряно"
        : tracking === "moving"
          ? "Слишком быстрое движение"
          : data.running
            ? "Идёт сканирование"
            : "Сканирование остановлено",
      data.running
        ? "Сохраняйте перекрытие между кадрами и избегайте резких поворотов."
        : "Сканирование остановлено; облако точек сохранено.",
    );
    if (!data.running && scanTimer) {
      clearInterval(scanTimer);
      scanTimer = null;
      setScanButton();
      if (data.error) showToast(data.error, "warning");
    }
  } catch (error) {
    if (scanTimer) clearInterval(scanTimer);
    scanTimer = null;
    isScanning = false;
    setScanButton();
    showToast(
      `Ошибка получения состояния сканирования. ${error.message}`,
      "warning",
    );
  } finally {
    scanBusy = false;
    updateExportButtons();
  }
}

async function clearScan() {
  if (activeSource === "demo") {
    stopDemoScan(true);
    $("scanProgressFill").style.width = "0%";
    $("sceneModeLabel").textContent = "ДЕМО-СЦЕНА · ОЧИЩЕНО";
    setTracking(
      "demo",
      0,
      "",
      "Данные очищены",
      "Запустите демо-сканирование, чтобы собрать сцену заново.",
    );
    return;
  }
  try {
    await apiRequest("/api/scan/clear", { method: "POST", body: {} });
    if (scanTimer) clearInterval(scanTimer);
    scanTimer = null;
    isScanning = false;
    hasScan = false;
    originalPoints = [];
    $("frameCount").textContent = "0";
    $("scanProgressFill").style.width = "0%";
    setScanButton();
    renderPoints();
    setTracking(
      deviceConnected ? "tracking" : "idle",
      0,
      "",
      "Облако очищено",
      "Поток датчика остаётся подключённым; сканирование остановлено.",
    );
    if (deviceConnected && !streamTimer) {
      pollFrame();
      streamTimer = setInterval(pollFrame, 300);
    }
    showToast("Собранное облако точек очищено.", "success");
  } catch (error) {
    showToast(error.message, "warning");
  }
}

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
  if (!hasScan) {
    showToast("Сначала выполните сканирование.", "warning");
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
      `Облако точек экспортировано в ${format.toUpperCase()}.`,
      "success",
    );
  } catch (error) {
    showToast(error.message, "warning");
  }
}

$("sourceSelect").onchange = (event) => {
  if (isScanning) {
    showToast(
      "Сначала остановите сканирование, затем смените источник.",
      "warning",
    );
    event.target.value = activeSource;
    return;
  }
  stopStream();
  activeSource = event.target.value;
  deviceConnected = false;
  hasScan = false;
  originalPoints = activeSource === "demo" ? demoPoints.slice() : [];
  $("frameCount").textContent = "0";
  $("scanProgressFill").style.width = "0%";
  $("sceneModeLabel").textContent =
    activeSource === "demo" ? "ДЕМО-СЦЕНА" : "ИСТОЧНИК НЕ ПОДКЛЮЧЁН";
  renderPoints();
  if (activeSource === "demo") renderDemoFeeds();
  else drawNoSignal();
  updateSourcePanel();
};
$("detectButton").onclick = probeDevices;
$("connectButton").onclick = connectSelectedSource;
$("scanButton").onclick = toggleScan;
$("clearButton").onclick = clearScan;
$("exportPly").onclick = () => exportScan("ply");
$("exportObj").onclick = () => exportScan("obj");

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
let setupPollAttempts = 0;

async function fetchSetupState() {
  try {
    const response = await fetch("/app/setup", { cache: "no-store" });
    const type = response.headers.get("content-type") || "";
    if (!response.ok || !type.includes("application/json")) return null;
    return await response.json();
  } catch {
    return null;
  }
}

function setupHeadline(state) {
  if (state.phase === "ready") return "Среда Depth Studio готова";
  if (state.phase === "failed") return "Настройка среды не завершена — доступен деморежим";
  if (state.phase === "running") return "Настройка среды выполняется…";
  return "Среда Depth Studio ещё не настроена";
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
  const headline = state.phase ? setupHeadline(state) : "Python-мост Depth Studio запущен";
  summary.textContent = `${state.phase === "ready" ? "Среда готова" : headline} · ${envDir}`;
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
    banner.hidden = true;
    renderEnvironmentSummary(null);
    return;
  }
  setupState = state;
  const ready = state.phase === "ready";
  const bridgeReady = Boolean(state.bridge?.ready);
  const open3dMissing = state.packages?.open3d?.available === false;
  renderEnvironmentSummary(state);
  if (ready && bridgeReady && !open3dMissing) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.dataset.phase = ready && !open3dMissing ? "bridge" : ready ? "warning" : state.phase || "idle";
  $("setupTitle").textContent =
    ready && open3dMissing ? "Open3D недоступен: сканирование с датчиком выключено" : setupHeadline(state);
  const parts = [];
  if (state.phase === "failed" && state.error?.message) parts.push(state.error.message);
  if (state.phase === "failed" && state.error?.hint) parts.push(state.error.hint);
  if (ready && !bridgeReady) {
    parts.push(state.bridge?.error || "Python-мост запускается; деморежим уже доступен.");
  }
  if (ready && open3dMissing) {
    parts.push(
      "Совмещение кадров и сканирование с датчиком требуют Open3D. Обычно не хватает Microsoft Visual C++ 2015–2022 Redistributable (x64) — установите его вручную, затем нажмите «Повторить». Деморежим и экспорт работают.",
    );
  }
  if (!ready) {
    parts.push("Деморежим работает сразу: 3D-сцена, синтетический поток и экспорт PLY/OBJ доступны без датчика.");
  }
  $("setupText").textContent = parts.join(" ");
  renderSetupRemedies(state);
  renderSetupDetails(state);
  const running = state.phase === "running";
  $("setupProgressWrap").hidden = !running;
  $("setupProgressFill").style.width = `${Math.max(0, Math.min(100, state.progress || 0))}%`;
  $("setupRetryBtn").hidden = ready;
  $("setupRetryBtn").disabled = running;
  $("setupRepairBtn").disabled = running;
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

function scheduleSetupPoll(delay = 1500) {
  clearTimeout(setupPollTimer);
  if (setupPollAttempts > 240) return;
  setupPollTimer = setTimeout(async () => {
    setupPollAttempts += 1;
    const state = await fetchSetupState();
    renderSetupBanner(state);
    const settled = state && state.phase === "ready" && state.bridge?.ready;
    if (!settled) {
      scheduleSetupPoll(setupState?.phase === "running" ? 1200 : 3000);
    } else {
      loadDevices();
    }
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
    setupPollAttempts = 0;
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

async function initEnvironmentUi() {
  const state = await fetchSetupState();
  renderSetupBanner(state);
  if (state && !(state.phase === "ready" && state.bridge?.ready)) scheduleSetupPoll(1200);
}

// Demo is immediately usable without Python, a camera, or a depth sensor.
originalPoints = demoPoints.slice();
renderPoints();
renderDemoFeeds();
updateSourcePanel();
loadDevices();
initEnvironmentUi();
