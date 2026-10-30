import * as THREE from "three";
import { environmentStatus, isScanInProgress, scanActions, setupHeadline } from "./readiness.js";
import { apiRequest } from "./api.js";
import { frontDepthToPackedPoints, PreviewClock, isPoseFresh } from "./front-view.js";
import { fillPointBuffers } from "./point-buffer.js";
import { updateMapMirror } from "./map-mirror.js";
import { tiltAvailability } from "./motor-mode.js";
import {
  calibrationStateLabel,
  calibrationSteps,
  cameraMetricLines,
  cameraScanLines,
  cameraStateLabel,
  decodeDepthFrame,
  depthMetricLines,
  depthPlaceholder,
  depthToRgba,
  formatDepthMm,
  formatMs,
  kinectSelectState,
  normalizeViewMode,
  pairMetricLines,
  previewPlan,
  stereoPollQuery,
  stereoStartState,
  stereoWarningLines,
  timingLines,
} from "./stereo-view.js";
import { mapRequestQuery, shouldApplyScanPoints } from "./map-request.js";
import { decodePixelFrame } from "./pixel-frames.js";
import { formatAngularSpeed, formatCpu, formatFps, formatFrameChecks, formatLandmarkChecks, formatSpeed } from "./metrics.js";
import { version as appVersion } from "../package.json";
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
      <span class="version-tag">BETA · ${appVersion}</span>
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
          <div class="view-modes" role="group" aria-label="Режим просмотра 3D">
            <button id="mapViewButton" type="button" aria-pressed="true" title="Общая карта отражена по горизонтали только на экране; экспорт не меняется">Общая карта · зеркально</button>
            <button id="frontViewButton" type="button" aria-pressed="false">Перед камерой</button>
            <button id="colorViewButton" type="button" aria-pressed="false">RGB-карта + наклон Kinect</button>
            <button id="stereoViewButton" type="button" aria-pressed="false" title="Стереопара Kinect RGB + USB-камера: стереоизображение и, после калибровки, глубина">Стерео · Kinect + USB</button>
            <span id="frontViewStatus" aria-live="polite" hidden>Ожидание свежего кадра</span>
          </div>
          <section class="tilt-panel" id="tiltPanel" aria-label="Цветная карта и наклон Kinect" hidden>
            <div class="tilt-header"><div><strong>Цветная карта + наклон Kinect</strong><small>Только мотор камеры, не управление движением робота</small></div><span class="tilt-badge">ЭКСПЕРИМЕНТ</span></div>
            <p id="colorMapStatus" class="tilt-note" aria-live="polite">RGB и глубина не откалиброваны и могут быть не синхронны; цвет на карте приблизительный.</p>
            <div class="tilt-controls">
              <label for="tiltAngle">Угол камеры <strong id="tiltDesired">0°</strong><small> −27° вниз / +27° вверх</small></label>
              <input id="tiltAngle" type="range" min="-27" max="27" step="1" value="0" disabled aria-describedby="tiltStatus">
              <button class="button-secondary" id="tiltApply" type="button" disabled>Применить наклон</button>
              <button class="button-secondary" id="tiltRefresh" type="button" disabled>Прочитать угол</button>
            </div>
            <p id="tiltStatus" class="tilt-note" aria-live="polite">Подключите Kinect через Microsoft Kinect SDK 1.8.</p>
            <p class="tilt-note">Мотор не предназначен для частого движения. Наклон заблокирован при сканировании; после команды дождитесь остановки камеры. Угол SDK не подтверждает остановку.</p>
          </section>
          <section class="stereo-panel" id="stereoPanel" aria-label="Стереорежим Kinect и USB-камера" hidden>
            <div class="tilt-header">
              <div><strong>Стереопара: Kinect RGB + USB-камера</strong><small>Жёсткая планка, общий вид сцены, программные метки времени</small></div>
              <span class="tilt-badge">ЭКСПЕРИМЕНТ · НЕ ДЛЯ НАВИГАЦИИ</span>
            </div>
            <p class="tilt-note" id="stereoSummary" aria-live="polite">Стереорежим не запущен.</p>
            <div class="stereo-controls">
              <label for="stereoKinectSelect">Kinect RGB<select id="stereoKinectSelect"></select></label>
              <label for="stereoUsbSelect">USB-камера<select id="stereoUsbSelect"></select></label>
              <button class="button-secondary" id="stereoRefreshCameras" type="button">Найти USB-камеры</button>
              <button class="button-secondary" id="stereoCancelScan" type="button" hidden>Отменить поиск</button>
              <button class="button-primary" id="stereoStart" type="button">Запустить стерео</button>
              <button class="button-secondary" id="stereoStop" type="button" disabled>Остановить</button>
            </div>
            <p class="stereo-note" id="stereoStartReason" aria-live="polite"></p>
            <div class="stereo-controls stereo-scan-row">
              <label>Индекс вручную<input id="stereoManualIndex" type="number" min="0" max="31" step="1" value="0"></label>
              <button class="button-secondary" id="stereoProbeIndex" type="button">Проверить индекс</button>
              <span class="stereo-note" id="stereoScanProgress" aria-live="polite"></span>
            </div>
            <div class="stereo-grid">
              <div class="stereo-cell">
                <div class="stereo-canvas-wrap">
                  <canvas id="stereoCanvasA" width="320" height="240" aria-label="Изображение камеры A"></canvas>
                  <span class="stereo-canvas-label" id="stereoLabelA">Камера A</span>
                </div>
              </div>
              <div class="stereo-cell">
                <div class="stereo-canvas-wrap">
                  <canvas id="stereoCanvasB" width="320" height="240" aria-label="Изображение камеры B"></canvas>
                  <span class="stereo-canvas-label" id="stereoLabelB">Камера B</span>
                </div>
              </div>
              <div class="stereo-cell">
                <div class="stereo-canvas-wrap">
                  <canvas id="stereoCanvasDepth" width="320" height="240" aria-label="Стереоглубина"></canvas>
                  <span class="stereo-canvas-label" id="stereoLabelDepth">Глубина</span>
                  <span class="stereo-canvas-note" id="stereoDepthNote" aria-live="polite">нет калибровки</span>
                </div>
              </div>
            </div>
            <div class="stereo-diagnostics">
              <div class="stereo-diag-column"><h4>Камера A</h4><dl id="stereoDiagA"></dl></div>
              <div class="stereo-diag-column"><h4>Камера B</h4><dl id="stereoDiagB"></dl></div>
              <div class="stereo-diag-column"><h4>Пары и глубина</h4><dl id="stereoDiagPairs"></dl></div>
            </div>
            <div class="stereo-calibration">
              <div class="stereo-calibration-head">
                <h4>Калибровка стереопары</h4>
                <span class="stereo-state" id="stereoCalibrationState">Калибровки нет</span>
              </div>
              <ol class="stereo-steps" id="stereoSteps"></ol>
              <div class="stereo-board">
                <label>Колонки<input id="stereoBoardCols" type="number" min="3" max="20" step="1" value="9"></label>
                <label>Строки<input id="stereoBoardRows" type="number" min="3" max="20" step="1" value="6"></label>
                <label>Квадрат, мм<input id="stereoBoardSquare" type="number" min="5" max="200" step="1" value="25"></label>
                <label>Мишень<select id="stereoBoardKind"><option value="screen">Экран монитора</option><option value="printed">Печать</option></select></label>
              </div>
              <div class="stereo-buttons">
                <button class="button-secondary" id="stereoCapture" type="button" disabled>Снять пару</button>
                <button class="button-secondary" id="stereoResetViews" type="button" disabled>Сбросить пары</button>
                <button class="button-primary" id="stereoCalibrate" type="button" disabled>Откалибровать</button>
                <button class="button-secondary" id="stereoClearCalibration" type="button">Удалить калибровку</button>
              </div>
              <p class="stereo-note" id="stereoManualHint" hidden></p>
              <ul class="stereo-reasons" id="stereoReasons"></ul>
              <p class="stereo-note" id="stereoQuality"></p>
            </div>
            <ul class="stereo-warnings" id="stereoWarnings"></ul>
          </section>
          <div class="scene-wrap" id="sceneWrap">
            <div class="scene-canvas" id="sceneCanvas"></div>
            <div class="scene-topline"><span class="scene-cross">✳</span> ПРОСТРАНСТВЕННЫЙ ПРОСМОТР <span>/</span> PERSPECTIVE</div>
            <div class="scene-tools">
              <button id="resetView" title="Сбросить вид" aria-label="Сбросить вид"><i data-lucide="rotate-ccw"></i></button>
              <button id="fitView" title="Показать сцену целиком" aria-label="Показать сцену целиком"><i data-lucide="move-3d"></i></button>
              <button id="fullscreen" title="На весь экран" aria-label="На весь экран"><i data-lucide="maximize-2"></i></button>
            </div>
            <div class="empty-scene" id="emptyScene"><span class="empty-icon">✳</span><strong id="emptyTitle">Облако точек пока пустое</strong><small id="emptyText">Подключите датчик или запустите демонстрационное сканирование.</small></div>
            <div class="scene-axis"><span class="axis-y">Y</span><span class="axis-x">X</span><span class="axis-z">Z</span></div>
            <div class="scene-hint"><i data-lucide="mouse-pointer-2"></i> Перетащите — вращение <span>·</span> Колесо — масштаб</div>
          </div>
          <div class="viewer-footer">
            <div class="stat-block"><span>ТОЧЕК</span><strong id="pointCount">—</strong></div>
            <div class="stat-block"><span>КАДРОВ</span><strong id="frameCount">0</strong></div>
            <div class="stat-block stat-depth"><span>ФИЛЬТР ВИДА</span><strong id="depthMetric">≤ 4.0 м</strong></div>
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
          <p class="front-view-warning" id="frontViewWarning" hidden>Это один кадр, не накопленная карта. Время — после чтения, задержка Kinect неизвестна. Невидимые участки неизвестны. Не используйте его для управления или остановки робота.</p>
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
            <div title="Оценка полного вращения камеры по ICP, не гироскоп и не только поворот робота"><span>ВРАЩЕНИЕ · ОЦЕНКА</span><strong id="angularMetric">— °/с</strong></div>
            <div><span>FPS · ЗАХВАТ / ОБРАБОТКА</span><strong id="fpsMetric">— / —</strong></div>
            <div><span>ПРОПУСКИ · ОЧЕРЕДЬ</span><strong id="droppedMetric">—</strong></div>
            <div><span>ПРОПУСКИ · ДО ЧТЕНИЯ SDK</span><strong id="sensorDroppedMetric">—</strong></div>
            <div><span>СОСТОЯНИЕ</span><strong id="stateMetric">Демо</strong></div>
            <div><span>КЛЮЧЕВЫХ КАДРОВ · ВСЕГО</span><strong id="keyframesMetric">—</strong></div>
            <div><span>ПРОФИЛЬ ПРОХОДА</span><strong id="activeProfileMetric">—</strong></div>
            <div title="Установленный программный предел, а не подтверждённая дальность датчика"><span>ПРЕДЕЛ ПРОХОДА</span><strong id="activeDepthMetric">—</strong></div>
            <div><span>FPS · ТРЕКИНГ / TSDF</span><strong id="trackedFpsMetric">— / —</strong></div>
            <div><span>ПРИНЯТО ДЛЯ ТРЕКИНГА</span><strong id="acceptedMetric">—</strong></div>
            <div><span>СВЕРКА · ПРОШЛИ / ПРОВЕРОК</span><strong id="frameChecksMetric">— / —</strong></div>
            <div title="Распределённые RGB-признаки только отбраковывают сильное расхождение; это не распознавание объектов и не гарантия позы"><span>RGB-ОПОРЫ · ПРОВЕРКИ / ОТКЛОНЕНО</span><strong id="landmarkChecksMetric">— / —</strong></div>
            <div title="Логические процессоры / запрошенное число потоков OpenMP"><span>CPU · ЛОГИЧ. / OMP</span><strong id="cpuMetric">— / —</strong></div>
          </div>
          <div class="scan-progress"><div class="scan-progress-fill" id="scanProgressFill"></div></div>
          <label class="field-label scan-profile-label" for="scanProfile">Профиль нового прохода</label>
          <div class="select-wrap"><select id="scanProfile" aria-describedby="scanProfileHint"><option value="max_fps">Максимум FPS</option><option value="robot">Робот · осторожная сверка</option><option value="standard">Стандарт · плотнее</option></select><i data-lucide="chevron-down"></i></div>
          <p class="movement-hint" id="scanProfileHint">«Робот»: после 50 мс без принятой позы нужна RGB-D-сверка, после 120 мс — возврат к последнему виду. В других профилях дополнительная сверка после 50 мс включается при быстрой оценке ICP; заниженная оценка может пропустить ошибку. Сверка может остановить трекинг и не гарантирует верную позу. Повороты ≈90°/с не подтверждены: оценка ICP выше ≈57°/с отбраковывается. Продолжение сохраняет настройки прохода. Kinect физически не проверен.</p>
          <label class="landmark-option"><input id="scanVisualLandmarks" type="checkbox" aria-describedby="landmarkHint" disabled><span>Эксперимент: сверка по RGB-опорам</span></label>
          <p class="movement-hint" id="landmarkHint">Только для нового прохода «Робот». Может замедлять обработку и ложно отклонять правильные позы: RGB и глубина не совмещены. Не подтверждает правильность позы.</p>
          <div class="scan-depth-setting">
            <label class="setting-title" for="scanDepthMax">Предел глубины нового прохода <span id="scanDepthMaxValue">4.0 м</span></label>
            <input id="scanDepthMax" type="range" min="1" max="10" step="0.5" value="4" aria-describedby="scanDepthHint">
            <div class="range-labels"><span>1 м</span><span>10 м</span></div>
            <p class="movement-hint depth-limit-hint" id="scanDepthHint">Выше 4 м — экспериментальный программный предел, не дальность Kinect. SDK 1.8 ограничен 4 м и неизвестные значения отбрасываются; libfreenect может отдавать шумные неподтверждённые оценки. Проверки опираются на ближние данные и могут отклонить дальние кадры. При продолжении берётся сохранённое значение.</p>
          </div>
        </section>

        <section class="card settings-card" id="settingsPanel">
          <div class="section-heading compact-heading"><div><span class="section-kicker">03 / ПРОСМОТР</span><h2>Настройки сцены</h2></div><span class="heading-icon"><i data-lucide="sliders-horizontal"></i></span></div>
          <div class="setting-row"><div class="setting-title">Размер точек <span id="sizeValue">2.0</span></div><input id="pointSize" type="range" min="1" max="5" step="0.1" value="2"><div class="range-labels"><span>Меньше</span><span>Больше</span></div></div>
          <div class="setting-row"><label class="setting-title" for="depthRange">Фильтр отображения <span id="depthValue">4.0 м</span></label><input id="depthRange" type="range" min="1" max="10" step="0.1" value="4"><div class="range-labels"><span>1 м</span><span>10 м</span></div><p class="movement-hint">Только вид: в карте это расстояние от начала прохода; в переднем кадре — глубина по оси камеры. Не меняет предел записи.</p></div>
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
const pointColorLut = new Float32Array(256 * 3);
const lutColor = new THREE.Color();
for (let index = 0; index < 256; index++) {
  const distance = index / 255;
  lutColor.setHSL(0.52 - distance * 0.36, 0.82, 0.46 + (1 - distance) * 0.09);
  depthColorLut[index * 3] = Math.round(lutColor.r * 255);
  depthColorLut[index * 3 + 1] = Math.round(lutColor.g * 255);
  depthColorLut[index * 3 + 2] = Math.round(lutColor.b * 255);
  lutColor.setHSL(0.47 + distance * 0.18, 0.72, 0.48 + (1 - distance) * 0.16);
  pointColorLut[index * 3] = lutColor.r;
  pointColorLut[index * 3 + 1] = lutColor.g;
  pointColorLut[index * 3 + 2] = lutColor.b;
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
// The buffers are reused and may contain old values beyond drawRange. Avoid
// computing a bounding sphere from those unused slots on every preview update.
pointObject.frustumCulled = false;
scene.add(pointObject);
let pointCapacity = 0;
let pointPositions = null;
let pointColors = null;

let originalPoints = [];
// A TSDF model changes only when it is extracted, not on every status poll.
let mapPointsVersion = null;
let mapPointsMode = null;
let scanRefreshForMode = false;
let frontPoints = new Float32Array(0);
let viewMode = "map";
let frontTimer = null;
let frontExpiryTimer = null;
let frontBusy = false;
let frontDeadlineMs = 0;
let frontGeneration = 0;
const previewClock = new PreviewClock();
const LIVE_PREVIEW_POLL_MS = 50; // maximum request rate; no overlapping requests
const SENSOR_PREVIEW_POLL_MS = 100; // direct capture outside a scan
let liveFeedCleared = true;
let demoPoints = makeDemoRoom();
let activeSource = "demo";
let devices = [];
let deviceConnected = false;
let deviceActionBusy = false;
let scanActionBusy = false;
let tiltBusy = false;
let tiltGeneration = 0;
let tiltQueryBusy = false;
let tiltStatus = null;
let tiltMessage = "";
let tiltCooldownUntil = 0;
let tiltScanWaitUntil = 0;
let colorMapInfo = null;
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
let scanGeneration = 0;
let isScanning = false;
let scanStatusUpdatedMs = 0;
let poseExpiryTimer = null;
let poseDeadlineMs = 0;
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
      if (rand(x, z) > 0.22) points.push([x, 0.02, z, Math.hypot(x, z) + 1, 167, 145, 114]);
    }
  }
  for (let x = -3.05; x <= 3.05; x += 0.085) {
    for (let y = 0.03; y <= 2.75; y += 0.085) {
      if (rand(x, y, 1) > 0.29) points.push([x, y, -3.2, 3.2, 202, 219, 207]);
    }
  }
  for (let z = -3.2; z <= 3.2; z += 0.085) {
    for (let y = 0.03; y <= 2.75; y += 0.085) {
      if (rand(z, y, 2) > 0.31) points.push([-3.05, y, z, 3.2, 151, 186, 174]);
    }
  }
  const surfaceBox = (cx, cy, cz, w, h, d, salt = 0) => {
    for (let x = -w / 2; x <= w / 2; x += 0.075) {
      for (let z = -d / 2; z <= d / 2; z += 0.075) {
        points.push([cx + x, cy + h / 2, cz + z, 2.3, 188, 142, 102]);
      }
      for (let y = -h / 2; y <= h / 2; y += 0.075) {
        if (rand(x + salt, y, 3) > 0.11)
          points.push([cx + x, cy + y, cz + d / 2, 2.3, 170, 119, 84]);
      }
    }
    for (let z = -d / 2; z <= d / 2; z += 0.075) {
      for (let y = -h / 2; y <= h / 2; y += 0.075)
        points.push([cx + w / 2, cy + y, cz + z, 2.3, 151, 107, 76]);
    }
  };
  surfaceBox(0.35, 0.72, -0.72, 2.25, 1.42, 0.8, 1);
  surfaceBox(1.95, 0.36, 1.12, 0.95, 0.72, 0.95, 2);
  surfaceBox(-1.52, 0.48, 1.25, 1.05, 0.95, 1.15, 4);
  surfaceBox(-1.52, 1.18, 0.62, 1.05, 0.48, 0.34, 6);
  return points;
}

function renderPoints() {
  updateMapMirror(sceneHost, viewMode);
  const points = viewMode === "front" ? frontPoints : originalPoints;
  const needed = points instanceof Float32Array ? Math.floor(points.length / 4) : points.length;
  if (needed > pointCapacity || pointPositions === null) {
    // Disposing before replacing attributes releases their GPU buffers. Grow
    // geometrically so normal frame-size fluctuations do not reallocate them.
    if (pointCapacity) pointGeometry.dispose();
    pointCapacity = Math.max(256, 2 ** Math.ceil(Math.log2(Math.max(1, needed))));
    pointPositions = new Float32Array(pointCapacity * 3);
    pointColors = new Float32Array(pointCapacity * 3);
    pointGeometry.setAttribute("position", new THREE.BufferAttribute(pointPositions, 3).setUsage(THREE.DynamicDrawUsage));
    pointGeometry.setAttribute("color", new THREE.BufferAttribute(pointColors, 3).setUsage(THREE.DynamicDrawUsage));
  }
  const count = fillPointBuffers(points, Number($("depthRange").value), viewMode === "color" ? "rgb" : $("palette").value,
    pointPositions, pointColors, pointColorLut);
  pointGeometry.setDrawRange(0, count);
  pointGeometry.getAttribute("position").needsUpdate = true;
  pointGeometry.getAttribute("color").needsUpdate = true;
  $("pointCount").textContent = count.toLocaleString("ru-RU");
  $("emptyScene").classList.toggle("visible", count === 0);
  updateExportButtons();
}

function clearFrontView(message = "Нет свежего кадра") {
  const hadPoints = frontPoints.length > 0;
  frontPoints = new Float32Array(0);
  $("frontViewStatus").textContent = message;
  if (viewMode === "front") {
    $("emptyTitle").textContent = "Нет свежего кадра глубины";
    $("emptyText").textContent = "Подключите датчик и начните физический проход; старые кадры не показываются.";
    if (hadPoints) renderPoints();
  }
}

function clearLivePreview(message) {
  if (frontExpiryTimer) clearTimeout(frontExpiryTimer);
  frontExpiryTimer = null;
  frontDeadlineMs = 0;
  previewClock.expire();
  clearFrontView(message);
  if (activeSource !== "demo" && !liveFeedCleared) {
    drawNoSignal();
    $("feedState").textContent = "НЕТ СВЕЖЕГО КАДРА";
    $("rgbDescription").textContent = "Нет свежего RGB-кадра";
    $("depthDescription").textContent = "Нет свежего кадра глубины";
    liveFeedCleared = true;
  }
}

function invalidateFrontView(message, resetClock = false) {
  frontGeneration += 1;
  clearLivePreview(message);
  if (resetClock) previewClock.reset();
}

function stopLivePreview(message) {
  if (frontTimer) clearInterval(frontTimer);
  frontTimer = null;
  invalidateFrontView(message);
}

function ensureLivePreview() {
  if (frontTimer || activeSource === "demo" || !isScanning || scanPhase !== "capturing") return;
  frontTimer = setInterval(pollFrontView, LIVE_PREVIEW_POLL_MS);
  void pollFrontView();
}

// The capture mailbox is independent of ICP. Poll it in both 3D modes so RGB
// and depth don't freeze while the map worker is busy. One request at a time;
// on an overloaded machine the actual preview rate simply drops.
async function pollFrontView() {
  if (frontDeadlineMs && performance.now() >= frontDeadlineMs) clearLivePreview("Кадр устарел");
  if (activeSource === "demo" || !isScanning) {
    clearFrontView("Ожидается физический проход");
    return;
  }
  if (document.hidden || scanActionBusy || scanPhase !== "capturing") {
    clearLivePreview("Ожидается физический проход");
    return;
  }
  if (frontBusy) return;
  frontBusy = true;
  const generation = frontGeneration;
  const started = performance.now();
  try {
    const data = await apiRequest("/api/scan/front?color=1", { timeoutMs: 1000 });
    if (generation !== frontGeneration) return;
    if (document.hidden || activeSource === "demo" || !isScanning || scanActionBusy || scanPhase !== "capturing") {
      clearLivePreview("Ожидается физический проход");
      return;
    }
    const received = performance.now();
    const update = previewClock.observe(data, started, received);
    if (!update) { clearLivePreview("Нет свежего кадра"); return; }
    const { deadline, freshFrame, fps } = update;
    if (freshFrame) {
      const hasRgb = paintRgb(data.color);
      const hasDepth = paintDepth(data.depth, data.maxDepthMm);
      if (!hasDepth) { clearLivePreview("Нет свежей глубины"); return; }
      if (!hasRgb) drawNoSignal(rgbCanvas);
      $("rgbDescription").textContent = hasRgb ? "RGB; с глубиной не совмещён" : "RGB-поток недоступен";
      $("depthDescription").textContent = "Последний кадр глубины после чтения";
      $("rgbFrameLabel").textContent = hasRgb ? "LIVE · RGB" : "NO RGB";
      $("depthFrameLabel").textContent = "LIVE · DEPTH";
      liveFeedCleared = false;
    }
    if (viewMode === "front" && (freshFrame || !frontPoints.length)) {
      const points = frontDepthToPackedPoints(data);
      if (!points.length || performance.now() >= deadline) { clearLivePreview("Нет свежей глубины"); return; }
      frontPoints = points;
      $("sceneModeLabel").textContent = "ПЕРЕД КАМЕРОЙ · ОДИН КАДР";
      renderPoints();
    }
    frontDeadlineMs = deadline;
    if (frontExpiryTimer) clearTimeout(frontExpiryTimer);
    // Clear at the deadline, not the next poll. Event-loop stalls can delay
    // both painting and expiry: this remains an informational display only.
    frontExpiryTimer = setTimeout(() => {
      if (generation === frontGeneration && frontDeadlineMs === deadline) clearLivePreview("Кадр устарел");
    }, Math.max(0, deadline - performance.now()));
    const age = Math.round(data.captureAgeMs + (received - started));
    const trackingAge = data.tracking?.lastAcceptedAgeMs;
    const trackingFresh = data.tracking?.fresh === true && Number.isFinite(trackingAge) && trackingAge >= 0 &&
      trackingAge + received - started <= 250;
    const tracking = trackingFresh && data.tracking?.sameFrame ? "поза этого кадра оценена"
      : trackingFresh ? "поза другого кадра; без совмещения" : "глобальная поза не подтверждена";
    const previewRate = fps === null ? "—" : fps.toFixed(1);
    $("frontViewStatus").textContent = `После чтения ≈${age} мс · превью ${previewRate} к/с · ${tracking}`;
    $("feedState").textContent = `ПРЕВЬЮ · ${previewRate} К/С`;
    if (performance.now() >= deadline) clearLivePreview("Кадр устарел");
  } catch {
    if (generation === frontGeneration) clearLivePreview("Нет ответа датчика");
  } finally {
    frontBusy = false;
  }
}

function setViewMode(mode) {
  viewMode = normalizeViewMode(mode);
  frontPoints = new Float32Array(0);
  $("mapViewButton").setAttribute("aria-pressed", String(viewMode === "map"));
  $("frontViewButton").setAttribute("aria-pressed", String(viewMode === "front"));
  $("colorViewButton").setAttribute("aria-pressed", String(viewMode === "color"));
  $("stereoViewButton").setAttribute("aria-pressed", String(viewMode === "stereo"));
  $("tiltPanel").hidden = viewMode !== "color";
  $("stereoPanel").hidden = viewMode !== "stereo";
  $("frontViewStatus").hidden = viewMode !== "front";
  $("frontViewWarning").hidden = viewMode !== "front";
  if (viewMode === "stereo") {
    $("sceneModeLabel").textContent = "СТЕРЕО · KINECT RGB + USB";
    startStereoPolling();
  } else {
    stopStereoPolling();
  }
  if (viewMode === "front") {
    $("sceneModeLabel").textContent = "ПЕРЕД КАМЕРОЙ · ОДИН КАДР";
    clearFrontView("Ожидание свежего кадра");
    renderPoints();
    ensureLivePreview();
    void pollFrontView();
  } else if (viewMode !== "stereo") {
    $("emptyTitle").textContent = "Облако точек пока пустое";
    $("emptyText").textContent = "Подключите датчик или запустите демонстрационное сканирование.";
    $("sceneModeLabel").textContent = activeSource === "demo" ? "ДЕМО-СЦЕНА"
      : isScanning ? "СКАНИРОВАНИЕ · TSDF" : hasScan ? "РЕЗУЛЬТАТ ПРОХОДА" : "ИСТОЧНИК НЕ ПОДКЛЮЧЁН";
    renderPoints();
    if (viewMode === "color") {
      updateColorStatus();
      updateMotorControls();
      if (activeSource !== "demo" && deviceConnected) void refreshTiltStatus();
    }
    if (activeSource !== "demo") {
      if (isScanning || hasScan) {
        if (scanBusy) scanRefreshForMode = true;
        else void refreshScan();
      } else if (deviceConnected) void pollFrame();
    }
  }
}

$("mapViewButton").onclick = () => setViewMode("map");
$("frontViewButton").onclick = () => setViewMode("front");
$("colorViewButton").onclick = () => setViewMode("color");
$("stereoViewButton").onclick = () => setViewMode("stereo");
document.addEventListener("visibilitychange", () => {
  if (poseDeadlineMs && (document.hidden || performance.now() >= poseDeadlineMs)) expirePoseStatus();
  if (document.hidden) invalidateFrontView("Окно скрыто");
  else if (isScanning && activeSource !== "demo") { ensureLivePreview(); void pollFrontView(); }
  // Скрытое окно не опрашивает стереомост: иначе base64 копится впустую.
  if (document.hidden) stopStereoPolling();
  else if (viewMode === "stereo") startStereoPolling();
});

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
  if (frontDeadlineMs && performance.now() >= frontDeadlineMs) clearLivePreview("Кадр устарел");
  if (poseDeadlineMs && performance.now() >= poseDeadlineMs) expirePoseStatus();
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
    $("connectButton").disabled = deviceActionBusy || tiltBusy || isScanning || bridgeHealth?.ok !== true;
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

function clearPoseDeadline() {
  if (poseExpiryTimer) clearTimeout(poseExpiryTimer);
  poseExpiryTimer = null;
  poseDeadlineMs = 0;
}

function expirePoseStatus() {
  clearPoseDeadline();
  setTracking("stale", null, "Оценка позы устарела или её время неизвестно.",
    "Нет свежей оценки позы", "Не используйте карту для управления или остановки робота.");
}

function armPoseDeadline(deadline, generation) {
  clearPoseDeadline();
  poseDeadlineMs = deadline;
  if (window.performance.now() >= deadline) { expirePoseStatus(); return; }
  poseExpiryTimer = setTimeout(() => {
    if (generation === scanGeneration && poseDeadlineMs === deadline && isScanning) expirePoseStatus();
  }, Math.max(0, deadline - window.performance.now()));
}

function setTracking(status, speed, warning, title = "", text = "") {
  if (status !== "tracking") clearPoseDeadline();
  const labels = {
    demo: "Демо-режим",
    idle: "Ожидание",
    tracking: "Отслеживание",
    moving: "Двигайтесь медленнее",
    too_fast: "Слишком быстро",
    lost: "Трекинг потерян",
    stale: "Поза устарела",
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
  if (status !== "tracking") $("angularMetric").textContent = formatAngularSpeed(null);
  if (status === "demo") {
    $("fpsMetric").textContent = "— / —";
    $("angularMetric").textContent = formatAngularSpeed(null);
    $("droppedMetric").textContent = "—";
    $("sensorDroppedMetric").textContent = "—";
    $("keyframesMetric").textContent = "—";
    $("activeProfileMetric").textContent = "—";
    $("activeDepthMetric").textContent = "—";
    $("trackedFpsMetric").textContent = "— / —";
    $("acceptedMetric").textContent = "—";
    $("frameChecksMetric").textContent = formatFrameChecks(null);
    $("landmarkChecksMetric").textContent = formatLandmarkChecks(null);
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

function drawNoSignal(only = null) {
  for (const canvas of only ? [only] : [rgbCanvas, depthCanvas]) {
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
  if (!only || only === rgbCanvas) $("rgbFrameLabel").textContent = "NO SIGNAL";
  if (!only || only === depthCanvas) $("depthFrameLabel").textContent = "NO SIGNAL";
}

let rgbImageCache = null;
let depthImageCache = null;
function feedImage(canvas, frame, cached) {
  if (!cached || cached.width !== frame.width || cached.height !== frame.height) {
    cached = new ImageData(new Uint8ClampedArray(frame.width * frame.height * 4), frame.width, frame.height);
  }
  if (canvas.width !== frame.width) canvas.width = frame.width;
  if (canvas.height !== frame.height) canvas.height = frame.height;
  return cached;
}

function paintRgb(frame) {
  const bytes = decodePixelFrame(frame, 3);
  if (bytes === null) return false;
  rgbImageCache = feedImage(rgbCanvas, frame, rgbImageCache);
  const rgba = rgbImageCache.data;
  for (let i = 0, j = 0; i < bytes.length; i += 3, j += 4) {
    rgba[j] = bytes.charCodeAt(i);
    rgba[j + 1] = bytes.charCodeAt(i + 1);
    rgba[j + 2] = bytes.charCodeAt(i + 2);
    rgba[j + 3] = 255;
  }
  rgbCanvas.getContext("2d").putImageData(rgbImageCache, 0, 0);
  return true;
}

function paintDepth(frame, maxDepthMm = 4000) {
  // The scale is visual only. Missing or out-of-selection pixels stay dark,
  // not free space; measurements remain unchanged in the bridge.
  const limit = Number.isFinite(maxDepthMm) && maxDepthMm >= 500 && maxDepthMm <= 10000 ? maxDepthMm : 4000;
  const bytes = decodePixelFrame(frame, 2);
  if (bytes === null) return false;
  depthImageCache = feedImage(depthCanvas, frame, depthImageCache);
  const rgba = depthImageCache.data;
  for (let pixel = 0; pixel < frame.width * frame.height; pixel++) {
    const millimetres = bytes.charCodeAt(pixel * 2) | (bytes.charCodeAt(pixel * 2 + 1) << 8);
    const i = pixel * 4;
    if (millimetres < 450 || millimetres > limit) {
      rgba[i] = 10;
      rgba[i + 1] = 20;
      rgba[i + 2] = 18;
      rgba[i + 3] = 255;
      continue;
    }
    const distance = Math.min(1, Math.max(0, (millimetres - 450) / (limit - 450)));
    const paletteIndex = Math.round(distance * 255);
    rgba[i] = depthColorLut[paletteIndex * 3];
    rgba[i + 1] = depthColorLut[paletteIndex * 3 + 1];
    rgba[i + 2] = depthColorLut[paletteIndex * 3 + 2];
    rgba[i + 3] = 255;
  }
  depthCanvas.getContext("2d").putImageData(depthImageCache, 0, 0);
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
    originalPoints = data.points.map(([x, y, z, depth, ...rgb]) => [x, y + 1.25, z, depth, ...rgb]);
    // These are raw preview points, not the TSDF version we may have shown
    // earlier. Never tell the server that the overwritten map is still held.
    mapPointsVersion = null;
    mapPointsMode = null;
    colorMapInfo = data.pointColor || null;
    renderPoints();
    updateColorStatus();
  }
  const hasRgb = paintRgb(data.color);
  const hasDepth = paintDepth(data.depth, Number($("depthRange").value) * 1000);
  liveFeedCleared = false;
  if (!hasRgb) drawNoSignal(rgbCanvas);
  if (!hasDepth) drawNoSignal(depthCanvas);
  $("rgbDescription").textContent = hasRgb ? "Кадр с Kinect 360" : "RGB-поток недоступен";
  $("depthDescription").textContent = hasDepth ? "Глубина в реальном времени" : "Карта глубины недоступна";
  $("rgbFrameLabel").textContent = hasRgb ? "LIVE · RGB" : "NO RGB";
  $("depthFrameLabel").textContent = hasDepth ? "LIVE · DEPTH" : "NO DEPTH";
  $("feedState").textContent = "ПОТОК АКТИВЕН";
  if (!hasScan && !isScanning && !["finalizing", "merging"].includes(scanPhase)) {
    $("sceneModeLabel").textContent = "ПОТОК ДАТЧИКА";
    setTracking("idle", null, data.warning || "", "Датчик подключён",
      data.warning || "Поток глубины поступает; трекинг ещё не запущен.");
    $("frameCount").textContent = "LIVE";
  }
}

async function pollFrame() {
  if (streamBusy || activeSource === "demo" || !deviceConnected || isScanning) return;
  const generation = streamGeneration;
  const source = activeSource;
  streamBusy = true;
  try {
    const data = await apiRequest(`/api/frame?source=${encodeURIComponent(source)}${viewMode === "color" ? "&color=1" : ""}`);
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
      invalidateTilt();
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
    invalidateTilt();
    deviceConnected = true;
    const profile = devices.find((device) => device.id === source);
    if (profile) { profile.connected = true; profile.state = "connected"; profile.error = ""; profile.hint = ""; }
    updateSourcePanel();
    displaySensorFrame(result.frame);
    if (!streamTimer) streamTimer = setInterval(pollFrame, SENSOR_PREVIEW_POLL_MS);
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
    if (viewMode === "color" && deviceConnected) void refreshTiltStatus();
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

function sourceIsSdk() { return activeSource === "kinect_v1_sdk"; }
function invalidateTilt() { tiltGeneration += 1; tiltStatus = null; tiltMessage = ""; }

function updateColorStatus() {
  if (viewMode !== "color") return;
  let message;
  if (activeSource === "demo") {
    message = "Демо-цвета синтетической сцены; Kinect не подключён.";
  } else if (!originalPoints.length) {
    message = "Карта пока пуста; подключите датчик или начните проход.";
  } else if (!colorMapInfo?.available) {
    message = "RGB для этих точек недоступен; они показаны серыми, а не искусственными цветами.";
  } else if (colorMapInfo.status === "partial") {
    message = "Не все точки/кадры имеют RGB; часть показана серой или может быть темнее.";
  } else if (colorMapInfo.status === "unknown") {
    message = "Для сохранённого прохода наличие RGB в каждом кадре неизвестно; цвет может быть неверным.";
  } else {
    message = "Показаны цвета из RGB-кадров; это приблизительное совмещение.";
  }
  $("colorMapStatus").textContent = `${message} RGB и глубина Kinect не откалиброваны и не гарантированно синхронны.`;
}

function updateMotorControls() {
  const environment = environmentStatus(setupState, bridgeHealth);
  const inPass = isScanInProgress(scanPhase, isScanning);
  const cooldown = Math.max(0, Math.ceil((tiltCooldownUntil - performance.now()) / 1000));
  const settle = Math.max(0, Math.ceil((tiltScanWaitUntil - performance.now()) / 1000));
  const available = tiltAvailability({ visible: viewMode === "color", environmentReady: environment.ready,
    sdkSource: sourceIsSdk(), connected: deviceConnected, scanInProgress: inPass,
    deviceBusy: deviceActionBusy, scanBusy: scanActionBusy, motorBusy: tiltBusy,
    queryBusy: tiltQueryBusy, angleKnown: Number.isInteger(tiltStatus?.angle), cooldownMs: cooldown });
  $("tiltAngle").disabled = !available.select;
  $("tiltApply").disabled = !available.apply;
  $("tiltRefresh").disabled = !available.read;
  let message;
  if (!environment.ready) message = environment.reason;
  else if (activeSource === "demo") message = "В демо мотор не двигается. Подключите Kinect через SDK 1.8.";
  else if (!sourceIsSdk()) message = "Управление наклоном доступно только через Microsoft Kinect SDK 1.8.";
  else if (!deviceConnected) message = "Подключите Kinect через Microsoft Kinect SDK 1.8.";
  else if (inPass) message = "Наклон заблокирован во время прохода. Остановите сканирование и подключите датчик снова.";
  else if (tiltBusy) message = "Отправка команды. Камера может двигаться; не запускайте проход.";
  else if (tiltQueryBusy) message = "Читаем угол Kinect…";
  else if (tiltMessage) message = tiltMessage;
  else if (!tiltStatus) message = "Нажмите «Прочитать угол». Наклон не проверен на физическом Kinect.";
  else {
    const angle = Number.isInteger(tiltStatus.angle) ? `${tiltStatus.angle}°` : "неизвестен";
    message = `Угол, сообщённый SDK: ${angle}. Это не подтверждение остановки мотора.`;
    if (cooldown) message += ` Следующая команда через ${cooldown} с.`;
    if (settle) message += ` Сканирование доступно не раньше чем через ${settle} с.`;
  }
  $("tiltStatus").textContent = message;
}

function rememberTiltStatus(result, updateSlider = false) {
  if (!result || !Number.isFinite(result.cooldownMs) || !Number.isFinite(result.scanWaitMs) ||
      result.cooldownMs < 0 || result.scanWaitMs < 0 ||
      !Number.isInteger(result.minAngle) || !Number.isInteger(result.maxAngle) ||
      result.minAngle < -27 || result.maxAngle > 27 || result.minAngle > result.maxAngle ||
      (result.angle !== null && (!Number.isInteger(result.angle) || result.angle < result.minAngle || result.angle > result.maxAngle))) {
    throw new Error("Мост вернул некорректное состояние наклона.");
  }
  tiltStatus = result;
  const now = performance.now();
  tiltCooldownUntil = now + result.cooldownMs;
  tiltScanWaitUntil = Math.max(tiltScanWaitUntil, now + result.scanWaitMs);
  $("tiltAngle").min = result.minAngle;
  $("tiltAngle").max = result.maxAngle;
  if (updateSlider && Number.isInteger(result.angle)) $("tiltAngle").value = result.angle;
  $("tiltDesired").textContent = `${$("tiltAngle").value}°`;
}

async function refreshTiltStatus() {
  if (viewMode !== "color" || !sourceIsSdk() || !deviceConnected || tiltBusy || tiltQueryBusy ||
      deviceActionBusy || scanActionBusy || isScanInProgress(scanPhase, isScanning) ||
      !environmentStatus(setupState, bridgeHealth).ready) return;
  tiltQueryBusy = true;
  const source = activeSource;
  const generation = tiltGeneration;
  updateMotorControls();
  try {
    const data = await apiRequest(`/api/devices/tilt?source=${encodeURIComponent(source)}`, { timeoutMs: 6000 });
    if (generation !== tiltGeneration || source !== activeSource || !deviceConnected || isScanInProgress(scanPhase, isScanning)) return;
    rememberTiltStatus(data, !tiltStatus);
    tiltMessage = "";
  } catch (error) {
    if (generation === tiltGeneration && source === activeSource) { tiltStatus = null; tiltMessage = error.message; }
  } finally {
    tiltQueryBusy = false;
    updateExportButtons();
  }
}

async function applyTilt() {
  if ($("tiltApply").disabled) return;
  const angle = Number($("tiltAngle").value);
  if (!Number.isInteger(angle) || angle < -27 || angle > 27) return;
  tiltBusy = true;
  tiltMessage = "";
  const source = activeSource;
  const generation = tiltGeneration;
  updateSourcePanel();
  updateExportButtons();
  try {
    const data = await apiRequest("/api/devices/tilt", { method: "POST", timeoutMs: 8000,
      headers: { "X-Depth-Studio-Tilt": "1" }, body: { source, angle } });
    if (generation !== tiltGeneration || source !== activeSource || !deviceConnected) return;
    rememberTiltStatus(data);
    tiltMessage = data.commandSent ? "Команда отправлена. Убедитесь, что камера остановилась перед проходом." : "Этот угол уже сообщён SDK; команда мотору не отправлялась.";
    if (data.commandSent) setTimeout(() => { tiltMessage = ""; void refreshTiltStatus(); }, 8500);
  } catch (error) {
    // A timed-out or failed SDK call may have moved the motor anyway. Never
    // display it as stopped or immediately start a scan on that assumption.
    tiltScanWaitUntil = Math.max(tiltScanWaitUntil, performance.now() + 8000);
    tiltStatus = null;
    tiltMessage = `${error.message} Камера могла начать движение; проверьте её и прочитайте угол.`;
  } finally {
    tiltBusy = false;
    updateSourcePanel();
    updateExportButtons();
  }
}

$("tiltAngle").oninput = () => { $("tiltDesired").textContent = `${$("tiltAngle").value}°`; };
$("tiltApply").onclick = applyTilt;
$("tiltRefresh").onclick = () => { tiltMessage = ""; void refreshTiltStatus(); };
setInterval(() => {
  if (viewMode === "color" || tiltScanWaitUntil > 0) {
    if (performance.now() >= tiltScanWaitUntil) tiltScanWaitUntil = 0;
    updateExportButtons();
  }
}, 1000);

function currentScanActions() {
  return scanActions({ source: activeSource, connected: deviceConnected, setup: setupState,
    health: bridgeHealth, phase: scanPhase, running: isScanning, hasResult: hasScan,
    hasVisibleData: originalPoints.length > 0, busy: scanActionBusy || tiltBusy ||
      (sourceIsSdk() && !isScanning && performance.now() < tiltScanWaitUntil) });
}

function updateExportButtons() {
  const actions = currentScanActions();
  if (sourceIsSdk() && !isScanning && performance.now() < tiltScanWaitUntil) {
    actions.reason = "Выдержите защитную паузу после команды мотора и проверьте остановку камеры.";
  } else if (tiltBusy && !isScanning) {
    actions.reason = "Дождитесь завершения запроса наклона Kinect.";
  }
  $("scanButton").disabled = !actions.start;
  $("clearButton").disabled = !actions.clear;
  $("exportPly").disabled = !actions.export;
  $("exportObj").disabled = !actions.export;
  $("exportGlb").disabled = activeSource === "demo" || !actions.export;
  $("cancelScanButton").hidden = activeSource === "demo" || !isScanning;
  $("cancelScanButton").disabled = !actions.cancel;
  $("scanProfile").disabled = isScanning || scanActionBusy || ["starting", "stopping", "finalizing", "merging"].includes(scanPhase);
  $("scanDepthMax").disabled = $("scanProfile").disabled;
  $("scanVisualLandmarks").disabled = $("scanProfile").disabled || $("scanProfile").value !== "robot";
  const reason = $("scanActionReason");
  reason.textContent = actions.reason;
  reason.hidden = !actions.reason;
  $("scanButton").title = !actions.start ? actions.reason : "";
  $("clearButton").title = !actions.clear && actions.reason ? actions.reason : "Очистить просмотр";
  updateMotorControls();
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

// A blocked bridge/ICP request must not leave an old green pose or angular
// speed on screen. This indicator is advisory and never controls the robot.
setInterval(() => {
  if (!isScanning || activeSource === "demo" ||
      window.performance.now() - scanStatusUpdatedMs <= 1000) return;
  $("angularMetric").textContent = formatAngularSpeed(null);
  setTracking("idle", null, "Нет свежего состояния трекинга.", "Состояние сканирования неизвестно",
    "Нет своевременного ответа; захват может продолжаться. Программа не останавливает робота.");
}, 250);

function markScanDeviceReleased(error = "") {
  invalidateTilt();
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
  invalidateTilt();
  scanGeneration += 1;
  invalidateFrontView("Запуск прохода", true);
  setTracking("idle", null, "", "Подготовка сканирования", "Ожидается первая свежая оценка позы.");
  stopStream();
  updateExportButtons();
  try {
    const profile = $("scanProfile").value;
    await apiRequest("/api/scan/start", {
      method: "POST", body: { source: activeSource, profile,
        depthMaxM: Number($("scanDepthMax").value),
        visualLandmarks: profile === "robot" && $("scanVisualLandmarks").checked,
        ...(resumePassId ? { resumePassId } : {}) },
    });
    scanGeneration += 1;
    hasScan = false;
    isScanning = true;
    scanStatusUpdatedMs = window.performance.now();
    scanPhase = "capturing";
    originalPoints = [];
    $("frameCount").textContent = "0";
    $("scanProgressFill").style.width = "0%";
    renderPoints();
    setScanButton();
    setTracking("idle", null, "", "Подготовка сканирования",
      "Ожидается первая свежая оценка позы; приложение не управляет роботом.");
    $("sceneModeLabel").textContent = "СКАНИРОВАНИЕ · TSDF";
    startScanPolling();
    // Start the independent mailbox preview immediately, even if the first
    // processing/status response is slow. Both helpers handle their own errors.
    ensureLivePreview();
    void refreshScan();
    void loadPasses();
  } catch (error) {
    scanGeneration += 1;
    showToast(error.message, "warning");
    if (error.code === "timeout") {
      // The bridge may still be starting. Keep Stop/Cancel available until its
      // actual state is known rather than assuming that the capture failed.
      isScanning = true;
      scanStatusUpdatedMs = window.performance.now();
      scanPhase = "starting";
      startScanPolling();
      await refreshScan();
    } else {
      isScanning = false;
      // The server closes a failed sensor; a new frame request reports details.
      if (deviceConnected && !streamTimer) {
        streamTimer = setInterval(pollFrame, SENSOR_PREVIEW_POLL_MS);
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
  scanGeneration += 1;
  stopLivePreview("Остановка прохода");
  setTracking("idle", null, "", "Остановка прохода", "Ожидается подтверждение моста; робот не управляется программой.");
  updateExportButtons();
  try {
    const result = await apiRequest("/api/scan/stop", { method: "POST", body: {} });
    scanGeneration += 1;
    isScanning = false;
    stopLivePreview("Проход остановлен");
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
    if (isScanning) ensureLivePreview();
    setScanButton();
    renderPasses();
  }
}

async function cancelScan() {
  if (!isScanning || activeSource === "demo" || scanActionBusy) return;
  scanActionBusy = true;
  scanGeneration += 1;
  stopLivePreview("Отмена прохода");
  setTracking("idle", null, "", "Отмена прохода", "Ожидается подтверждение моста; робот не управляется программой.");
  updateExportButtons();
  try {
    const result = await apiRequest("/api/scan/cancel", { method: "POST", body: {} });
    scanGeneration += 1;
    if (scanTimer) clearInterval(scanTimer);
    scanTimer = null;
    isScanning = false;
    stopLivePreview("Проход отменён");
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
    if (isScanning) ensureLivePreview();
    setScanButton();
    renderPasses();
  }
}

async function refreshScan() {
  if (scanBusy) return;
  scanBusy = true;
  const requestStartedMs = window.performance.now();
  const generation = scanGeneration;
  const requestedMode = viewMode;
  const pointQuery = mapRequestQuery(requestedMode, mapPointsVersion, mapPointsMode, originalPoints.length > 0);
  try {
    const data = await apiRequest(`/api/scan?preview=0${requestedMode === "color" ? "&color=1" : ""}${pointQuery}`);
    if (generation !== scanGeneration) return;
    // Include the whole request time when judging whether status could be old.
    scanStatusUpdatedMs = requestStartedMs;
    const previousPhase = scanPhase;
    const wasRunning = isScanning;
    isScanning = Boolean(data.running);
    scanPhase = data.phase || (isScanning ? "capturing" : "idle");
    if (wasRunning && !isScanning) {
      stopLivePreview("Проход остановлен");
      markScanDeviceReleased(data.error || "");
    } else if (isScanning && scanPhase === "capturing" && !scanActionBusy) {
      ensureLivePreview();
    }
    hasScan = scanPhase === "ready" && Boolean(data.lastResultId);
    if (shouldApplyScanPoints(requestedMode, viewMode, data.points, scanPhase)) {
      originalPoints = data.points.map(([x, y, z, depth, ...rgb]) => [x, y + 1.25, z, depth, ...rgb]);
      mapPointsVersion = typeof data.pointsVersion === "string" ? data.pointsVersion : null;
      mapPointsMode = requestedMode;
      colorMapInfo = data.pointColor || null;
      renderPoints();
      updateColorStatus();
    }
    // The status response deliberately omits its old, processing-bound image;
    // the independent latest-frame request above drives both feed canvases.
    const performance = data.performance;
    $("angularMetric").textContent = formatAngularSpeed(data.angularDegS);
    $("fpsMetric").textContent = `${formatFps(performance?.captureFps)} / ${formatFps(performance?.processingFps)}`;
    $("droppedMetric").textContent = Number.isFinite(performance?.droppedFrames) ? String(performance.droppedFrames) : "—";
    $("sensorDroppedMetric").textContent = Number.isFinite(performance?.sensorDroppedFrames) ? String(performance.sensorDroppedFrames) : "—";
    $("keyframesMetric").textContent = Number.isFinite(data.keyframes) ? String(data.keyframes) : "—";
    $("activeProfileMetric").textContent = data.pipeline?.profile === "max_fps" ? "Максимум FPS"
      : data.pipeline?.profile === "robot" ? "Робот · осторожно"
      : data.pipeline?.profile === "standard" ? "Стандарт" : "—";
    $("activeDepthMetric").textContent = Number.isFinite(data.pipeline?.configuredDepthMaxM)
      ? `${data.pipeline.configuredDepthMaxM.toFixed(1)} м` : "—";
    $("trackedFpsMetric").textContent = `${formatFps(performance?.trackingFps)} / ${formatFps(performance?.integrationFps)}`;
    $("acceptedMetric").textContent = Number.isFinite(data.quality?.acceptedRatio)
      ? `${Math.round(data.quality.acceptedRatio * 100)}%` : "—";
    $("frameChecksMetric").textContent = formatFrameChecks(data.quality);
    $("landmarkChecksMetric").textContent = data.pipeline?.visualLandmarkVeto
      ? formatLandmarkChecks(data.quality) : "— / —";
    $("cpuMetric").textContent = formatCpu(data.pipeline?.cpu);
    $("frameCount").textContent = String(data.frames || 0);
    $("scanProgressFill").style.width = `${Math.min(100, (Number(data.frames || 0) / 120) * 100)}%`;
    const advertisedTracking = data.tracking || (isScanning ? "idle" : "stopped");
    const poseFresh = !document.hidden && isPoseFresh(data, requestStartedMs, window.performance.now());
    const tracking = advertisedTracking === "tracking" && !poseFresh ? "stale" : advertisedTracking;
    const statusTitles = {
      stale: "Нет свежей оценки позы", lost: "Отслеживание потеряно", too_fast: "Слишком быстрое движение",
      few_valid_pixels: "Мало валидных пикселей", sensor_covered: "Датчик, возможно, закрыт",
      memory_budget_reached: "Достигнут лимит памяти",
    };
    const failedPasses = (data.resultMetrics?.mergedPasses || []).filter((pass) => !pass.ok);
    const mergeWarning = failedPasses.length
      ? `Не удалось совместить проходы: ${failedPasses.map((pass) => `${pass.passId}: ${pass.reason || "нет совпадения"}`).join("; ")}. Они не вошли в результат.` : "";
    const warning = [data.error, data.warning, data.hint, mergeWarning,
      tracking === "stale" ? "Оценка позы устарела или её время неизвестно." : ""]
      .filter(Boolean).filter((v, i, arr) => arr.indexOf(v) === i).join(" ");
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
    if (tracking === "tracking" && isScanning) {
      // As with the front view, include the entire request time. Since polling
      // is slower than the expiry, stale is preferable to a frozen green pose.
      armPoseDeadline(requestStartedMs + 250 - data.poseAgeMs, generation);
    }
    if (hasScan) $("sceneModeLabel").textContent = data.lastResultId === "merged" ? "ОБЪЕДИНЁННЫЕ ПРОХОДЫ" : "РЕЗУЛЬТАТ ПРОХОДА";
    const processing = isScanInProgress(scanPhase, isScanning);
    if (!processing && scanTimer) { clearInterval(scanTimer); scanTimer = null; }
    if (!processing && previousPhase !== scanPhase) {
      await loadPasses();
      if (data.error) showToast(data.error, "warning");
    }
    setScanButton();
  } catch (error) {
    if (generation !== scanGeneration) return;
    $("angularMetric").textContent = formatAngularSpeed(null);
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
    if (scanRefreshForMode) {
      scanRefreshForMode = false;
      if (activeSource !== "demo" && (isScanning || hasScan)) void refreshScan();
    }
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
  scanGeneration += 1;
  invalidateFrontView("Просмотр очищен");
  try {
    await apiRequest("/api/scan/clear", { method: "POST", body: {} });
    scanGeneration += 1;
    scanPhase = "idle";
    hasScan = false;
    originalPoints = [];
    $("frameCount").textContent = "0";
    $("scanProgressFill").style.width = "0%";
    setScanButton();
    renderPoints();
    setTracking("idle", null, "", "Просмотр очищен", "Сохранённые проходы остаются на диске.");
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
  const visibleLength = Math.min(positions.length, pointGeometry.drawRange.count * 3);
  for (let i = 0; i < visibleLength; i += 3)
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

$("scanProfile").onchange = updateExportButtons;
$("sourceSelect").onchange = async (event) => {
  if (isScanning || scanActionBusy || deviceActionBusy || tiltBusy || ["finalizing", "merging"].includes(scanPhase)) {
    showToast("Дождитесь окончания прохода или запроса наклона перед сменой источника.", "warning");
    event.target.value = activeSource;
    return;
  }
  const nextSource = event.target.value;
  scanGeneration += 1;
  invalidateFrontView("Источник изменён", true);
  const previousSource = activeSource;
  const wasConnected = deviceConnected;
  stopStream();
  if (wasConnected && previousSource !== "demo") {
    try { await apiRequest("/api/devices/disconnect", { method: "POST", body: { source: previousSource } }); }
    catch (error) { showToast(error.message, "warning"); }
  }
  activeSource = nextSource;
  invalidateTilt();
  colorMapInfo = null;
  deviceConnected = false;
  hasScan = false;
  originalPoints = activeSource === "demo" ? demoPoints.slice() : [];
  mapPointsVersion = null;
  mapPointsMode = null;
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

$("scanDepthMax").oninput = (event) => {
  const value = Number(event.target.value);
  $("scanDepthMaxValue").textContent = `${value.toFixed(1)} м`;
  $("scanDepthHint").classList.toggle("experimental", value > 4);
  // Make the newly requested range visible; the user can still adjust this
  // view-only filter separately without changing the pass setting.
  $("depthRange").value = String(value);
  $("depthRange").dispatchEvent(new Event("input"));
};

$("pointSize").oninput = (event) => {
  const value = Number(event.target.value);
  pointMaterial.size = value * 0.0135;
  $("sizeValue").textContent = value.toFixed(1);
};
$("depthRange").oninput = (event) => {
  const value = Number(event.target.value);
  $("depthValue").textContent = `${value.toFixed(1)} м`;
  $("depthMetric").textContent = `≤ ${value.toFixed(1)} м`;
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
      invalidateTilt();
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

/* ------------------------------------------------------------------------ */
/* Стереорежим: Kinect RGB + USB-камера                                      */
/* ------------------------------------------------------------------------ */
// Опрос статуса: не чаще 4 раз/с, без наложенных запросов. Превью и глубина
// пересылаются только при изменении версии, поэтому 300 мс — это дешёвый
// запрос на несколько килобайт, а не 200 КБ base64 на каждые 500 мс.
const STEREO_POLL_MS = 300;
const STEREO_TIMINGS_EVERY = 10;
let stereoStatus = null;
let stereoPollTimer = null;
let stereoBusy = false;
let stereoGeneration = 0;
let stereoInFlight = null;
let stereoPollCount = 0;
let stereoCameras = null;
let stereoScanTimer = null;
let stereoVersions = { previewVersions: {}, depthVersion: null };
let stereoManual = { viewId: null, pointsA: [], pointsB: [], enabled: false };
let stereoPreviewImages = { a: null, b: null };
let stereoDepthCache = { version: null, rgba: null };
let stereoLastStatusText = "";

function stereoBoardPayload() {
  return {
    cols: Number($("stereoBoardCols").value),
    rows: Number($("stereoBoardRows").value),
    squareMm: Number($("stereoBoardSquare").value),
    targetKind: $("stereoBoardKind").value,
  };
}

function renderStereoSelects() {
  const kinect = $("stereoKinectSelect");
  const usb = $("stereoUsbSelect");
  const kinectState = kinectSelectState(stereoStatus, stereoCameras?.kinect);
  kinect.innerHTML = "";
  const kinectOption = document.createElement("option");
  kinectOption.value = "kinect_rgb";
  kinectOption.textContent = kinectState.label;
  kinect.append(kinectOption);
  kinect.disabled = kinectState.disabled;
  const previousIndex = Number(usb.value);
  usb.innerHTML = "";
  const cameras = stereoCameras?.cameras || [];
  if (!cameras.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = stereoCameras
      ? (stereoCameras.pending ? "Идёт поиск USB-камер…" : "USB-камеры не найдены")
      : "Нажмите «Найти USB-камеры»";
    usb.append(option);
  } else {
    cameras.forEach((camera) => {
      const option = document.createElement("option");
      option.value = String(camera.index);
      option.textContent = `${camera.label || `USB-камера #${camera.index}`} · ${camera.width || "?"}×${camera.height || "?"}`;
      usb.append(option);
    });
    if (Number.isInteger(previousIndex) && cameras.some((camera) => camera.index === previousIndex)) {
      usb.value = String(previousIndex);
    }
  }
  const sessionCameras = stereoStatus?.session?.cameras;
  if (sessionCameras && stereoStatus?.session?.active) {
    const usbSpec = [sessionCameras.a, sessionCameras.b].find((spec) => spec && spec.kind === "usb");
    if (usbSpec && Number.isInteger(usbSpec.index)) usb.value = String(usbSpec.index);
  }
  usb.disabled = Boolean(stereoStatus?.session?.active) || Boolean(stereoScanTimer);
  const startState = stereoStartState(stereoStatus, {
    busy: stereoBusy,
    kinect: kinectState,
    cameras: stereoCameras,
  });
  $("stereoStart").disabled = startState.disabled;
  $("stereoStart").title = startState.reason || "";
  $("stereoStartReason").textContent = startState.reason || "";
  $("stereoStop").disabled = stereoBusy || !stereoStatus?.session?.active;
  $("stereoStop").title = stereoStatus?.session?.active ? "" : "Стереорежим не запущен.";
  const scanning = Boolean(stereoCameras?.pending || stereoCameras?.scan?.running);
  $("stereoCancelScan").hidden = !scanning;
  $("stereoRefreshCameras").disabled = stereoBusy || Boolean(stereoStatus?.session?.active);
  $("stereoRefreshCameras").title = stereoStatus?.session?.active
    ? "Сначала остановите стереорежим: он уже занимает камеры."
    : "Перебрать индексы 0–5 в фоне (бюджет 3 с), не блокируя интерфейс.";
}

function stereoCameraLabel(kind) {
  const cameras = stereoStatus?.cameras;
  if (!cameras) return kind === "a" ? "Камера A" : "Камера B";
  const spec = cameras[kind];
  if (!spec) return kind === "a" ? "Камера A" : "Камера B";
  return `${kind.toUpperCase()} · ${spec.label || spec.kind}`;
}

function drawStereoImage(canvas, image) {
  if (!canvas || !image) return;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
}

const stereoDepthBuffer = document.createElement("canvas");

function drawStereoDepth(status) {
  const canvas = $("stereoCanvasDepth");
  const context = canvas.getContext("2d");
  const payload = status?.depth;
  const note = $("stereoDepthNote");
  const placeholder = depthPlaceholder(status);
  note.hidden = !placeholder;
  note.textContent = placeholder || "";
  if (!payload?.depth) {
    // Пустой холст без объяснения выглядит как сбой: чистим и подписываем причину.
    context.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  const version = Number(status?.depthVersion ?? 0);
  if (stereoDepthCache.version !== version || !stereoDepthCache.rgba) {
    const decoded = decodeDepthFrame(payload.depth);
    if (!decoded) return;
    const rgba = depthToRgba(decoded, {
      minMm: Math.round(Number(stereoStatus?.session?.depthConfig?.minDepthM || 0.5) * 1000),
      maxMm: Math.round(Number(stereoStatus?.session?.depthConfig?.maxDepthM || 4) * 1000),
    });
    if (!rgba) return;
    stereoDepthCache = { version, rgba };
  }
  const rgba = stereoDepthCache.rgba;
  stereoDepthBuffer.width = rgba.width;
  stereoDepthBuffer.height = rgba.height;
  const buffer = stereoDepthBuffer.getContext("2d");
  buffer.putImageData(new ImageData(rgba.data, rgba.width, rgba.height), 0, 0);
  context.imageSmoothingEnabled = false;
  context.drawImage(stereoDepthBuffer, 0, 0, canvas.width, canvas.height);
}

function setStereoPreview(role, preview) {
  if (!preview?.data || stereoManual.enabled) return;
  // Image переиспользуется: новый объект на каждый ответ давал лишние загрузки
  // и мигание холста.
  let image = stereoPreviewImages[role];
  if (!image) {
    image = new Image();
    stereoPreviewImages[role] = image;
    image.onload = () => drawStereoImage($(role === "a" ? "stereoCanvasA" : "stereoCanvasB"), image);
  }
  image.src = `data:image/jpeg;base64,${preview.data}`;
}

function fillDefinitionList(element, lines) {
  element.innerHTML = "";
  lines.forEach(([label, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    element.append(dt, dd);
  });
}

function renderStereoDiagnostics(status) {
  const metrics = status?.metrics;
  const cameraA = cameraMetricLines(metrics, "a");
  const cameraB = cameraMetricLines(metrics, "b");
  const idle = [["Состояние", "нет данных"]];
  fillDefinitionList($("stereoDiagA"), cameraA.available ? cameraA.lines : idle);
  fillDefinitionList($("stereoDiagB"), cameraB.available ? cameraB.lines : idle);
  const lines = [...pairMetricLines(metrics), ...depthMetricLines(status?.depth)];
  if (status?.depthError) lines.unshift(["Ошибка глубины", status.depthError]);
  lines.push(...timingLines(status?.timings, ["kinect_capture", "usb_read", "status", "compute_stereo_depth"]));
  fillDefinitionList($("stereoDiagPairs"), lines.length ? lines : [["Состояние", "глубина не вычисляется"]]);
  $("stereoLabelA").textContent = stereoCameraLabel("a");
  $("stereoLabelB").textContent = stereoCameraLabel("b");
  $("stereoLabelDepth").textContent = status?.depth ? "Стереоглубина (после калибровки)" : "Глубина недоступна";
}

function renderStereoScanProgress() {
  const { lines, busy } = cameraScanLines(stereoCameras);
  $("stereoScanProgress").textContent = lines.slice(0, 2).join(" · ");
  const found = stereoCameras?.cameras?.length || 0;
  if (!busy && stereoCameras && !stereoCameras.__announced && !stereoCameras.needsScan) {
    stereoCameras.__announced = true;
    showToast(
      found ? `Найдено USB-камер: ${found}.` : "USB-камеры не найдены. Проверьте индекс вручную и другие приложения.",
      found ? "success" : "warning",
    );
  }
}

async function stereoCommand(path, body = {}) {
  if (stereoBusy) return null;
  stereoBusy = true;
  renderStereoSelects();
  try {
    const response = await apiRequest(path, {
      method: "POST",
      body,
      timeoutMs: path === "/api/stereo/start" ? 30000 : 60000,
    });
    return response;
  } finally {
    stereoBusy = false;
    void refreshStereoSessions(stereoGeneration);
  }
}

$("stereoRefreshCameras").onclick = async () => {
  if (stereoBusy) return;
  if (stereoStatus?.session?.active) {
    showToast("Сначала остановите стереорежим: он уже занимает камеры.", "warning");
    return;
  }
  const generation = stereoGeneration;
  try {
    // Поиск идёт в фоне: мост отвечает сразу и отдаёт job-id с прогрессом.
    const response = await apiRequest("/api/stereo/cameras/scan", {
      method: "POST",
      body: { maxIndex: 6 },
      timeoutMs: 5000,
    });
    stereoCameras = { ...(stereoCameras || {}), pending: true, scan: response.scan, needsScan: false };
    stereoCameras.__announced = false;
    renderStereoSelects();
    renderStereoScanProgress();
    scheduleStereoScanPoll(generation);
  } catch (error) {
    showToast(error.message, "warning");
  }
};

$("stereoCancelScan").onclick = async () => {
  try {
    await apiRequest("/api/stereo/cameras/scan/cancel", { method: "POST", body: {}, timeoutMs: 5000 });
    showToast("Поиск USB-камер отменяется.", "info");
  } catch (error) {
    showToast(error.message, "warning");
  }
};

$("stereoProbeIndex").onclick = async () => {
  const index = Number($("stereoManualIndex").value);
  if (!Number.isInteger(index) || index < 0 || index > 31) {
    showToast("Индекс USB-камеры должен быть целым 0–31.", "warning");
    return;
  }
  try {
    const response = await apiRequest("/api/stereo/cameras/probe", {
      method: "POST",
      body: { index },
      timeoutMs: 8000,
    });
    if (response.ok) {
      const camera = {
        index,
        label: response.label || `USB-камера #${index}`,
        width: response.width,
        height: response.height,
        backend: response.backend,
      };
      const existing = stereoCameras?.cameras || [];
      stereoCameras = {
        ...(stereoCameras || {}),
        available: true,
        cameras: [...existing.filter((item) => item.index !== index), camera]
          .sort((left, right) => left.index - right.index),
        needsScan: false,
        __announced: true,
      };
      renderStereoSelects();
      showToast(`Индекс ${index}: камера открылась (${camera.width}×${camera.height}).`, "success");
    } else {
      showToast(`Индекс ${index}: ${response.message || "камера не открылась"}. ${response.hint || ""}`.trim(), "warning");
    }
  } catch (error) {
    showToast(error.message, "warning");
  }
};

$("stereoStart").onclick = async () => {
  const index = Number($("stereoUsbSelect").value);
  if (!Number.isInteger(index)) {
    showToast("Выберите USB-камеру: список пуст, нажмите «Найти USB-камеры».", "warning");
    return;
  }
  try {
    const status = await stereoCommand("/api/stereo/start", {
      cameras: [
        { kind: "kinect_rgb", source: "kinect_v1_sdk" },
        { kind: "usb", index },
      ],
      minDepthM: 0.5,
      maxDepthM: 4.0,
    });
    const errors = status?.session?.errors || {};
    if (Object.keys(errors).length) {
      // Частичный успех: сессия живёт, но ошибка камеры не прячется.
      showToast(Object.values(errors).map((error) => error.message).join(" "), "warning");
    } else {
      showToast("Стереорежим запущен.", "success");
    }
  } catch (error) {
    showToast(error.message, "warning");
  }
};

$("stereoStop").onclick = async () => {
  try {
    await stereoCommand("/api/stereo/stop");
    showToast("Стереорежим остановлен.", "info");
  } catch (error) {
    showToast(error.message, "warning");
  }
};

$("stereoCapture").onclick = async () => {
  stereoManual = { viewId: null, pointsA: [], pointsB: [], enabled: false };
  try {
    const response = await stereoCommand("/api/stereo/views", { board: stereoBoardPayload() });
    if (!response) return;
    if (response.accepted) {
      showToast(`Пара принята (${response.views}). Разница меток времени ${formatMs(response.skewMs)}.`, "success");
    } else {
      stereoManual.viewId = response.viewId;
      stereoManual.enabled = true;
      const imageA = await loadPreviewImage(response.previews?.a);
      const imageB = await loadPreviewImage(response.previews?.b);
      $("stereoCanvasA").__stereoBase = imageA;
      $("stereoCanvasB").__stereoBase = imageB;
      drawStereoImage($("stereoCanvasA"), imageA);
      drawStereoImage($("stereoCanvasB"), imageB);
      const hint = $("stereoManualHint");
      hint.hidden = false;
      hint.textContent =
        "Автопоиск доски не сработал. Отметьте по четыре внутренних угла на кадрах A и B в порядке TL → TR → BR → BL. " +
        "Отметки не заменяют проверку качества: неверная разметка будет отклонена.";
      showToast("Доска не найдена автоматически: отметьте углы вручную.", "warning");
    }
  } catch (error) {
    showToast(error.message, "warning");
  }
};

async function loadPreviewImage(preview) {
  if (!preview?.data) return null;
  return new Promise((resolve) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = `data:image/jpeg;base64,${preview.data}`;
  });
}

function stereoCanvasPoint(event, canvas) {
  const rect = canvas.getBoundingClientRect();
  const x = (event.clientX - rect.left) / Math.max(1, rect.width);
  const y = (event.clientY - rect.top) / Math.max(1, rect.height);
  return [Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))];
}

function drawStereoMarks(canvas, points) {
  const context = canvas.getContext("2d");
  const image = canvas.__stereoBase;
  if (image) drawStereoImage(canvas, image);
  context.fillStyle = "#ffd166";
  context.strokeStyle = "#1b1b1b";
  points.forEach(([x, y], index) => {
    const px = x * canvas.width;
    const py = y * canvas.height;
    context.beginPath();
    context.arc(px, py, 5, 0, Math.PI * 2);
    context.fill();
    context.stroke();
    context.fillStyle = "#1b1b1b";
    context.font = "12px sans-serif";
    context.fillText(String(index + 1), px + 7, py - 7);
    context.fillStyle = "#ffd166";
  });
}

function wireStereoCanvas(role, canvasId) {
  const canvas = $(canvasId);
  canvas.addEventListener("click", (event) => {
    if (!stereoManual.enabled || stereoBusy) return;
    const points = role === "a" ? stereoManual.pointsA : stereoManual.pointsB;
    if (points.length >= 4) return;
    points.push(stereoCanvasPoint(event, canvas));
    drawStereoMarks(canvas, points);
    if (stereoManual.pointsA.length === 4 && stereoManual.pointsB.length === 4) void applyStereoManualPoints();
  });
}

async function applyStereoManualPoints() {
  if (!stereoManual.viewId) return;
  try {
    const response = await stereoCommand("/api/stereo/views/manual", {
      viewId: stereoManual.viewId,
      pointsA: stereoManual.pointsA,
      pointsB: stereoManual.pointsB,
    });
    if (!response) return;
    showToast(`Ручная разметка принята (${response.views} пар).`, "success");
    stereoManual = { viewId: null, pointsA: [], pointsB: [], enabled: false };
    $("stereoCanvasA").__stereoBase = null;
    $("stereoCanvasB").__stereoBase = null;
    $("stereoManualHint").hidden = true;
  } catch (error) {
    showToast(error.message, "warning");
    stereoManual.pointsA = [];
    stereoManual.pointsB = [];
  }
}

wireStereoCanvas("a", "stereoCanvasA");
wireStereoCanvas("b", "stereoCanvasB");

$("stereoResetViews").onclick = async () => {
  try {
    await stereoCommand("/api/stereo/views/reset");
    showToast("Собранные пары удалены.", "info");
  } catch (error) {
    showToast(error.message, "warning");
  }
};

$("stereoCalibrate").onclick = async () => {
  try {
    const response = await stereoCommand("/api/stereo/calibrate");
    if (!response) return;
    if (response.ok) {
      showToast("Калибровка проверена и сохранена.", "success");
    } else {
      showToast(response.message || "Калибровка не прошла проверку качества.", "warning");
    }
  } catch (error) {
    showToast(error.message, "warning");
  }
};

$("stereoClearCalibration").onclick = async () => {
  try {
    await stereoCommand("/api/stereo/calibration/clear");
    showToast("Калибровка удалена; стереоглубина выключена.", "info");
  } catch (error) {
    showToast(error.message, "warning");
  }
};

/* --- Опрос и отрисовка стереопанели ---------------------------------------- */
// Источник истины — ответ /api/stereo: активна ли сессия, какие камеры в ней,
// какие ошибки. Интерфейс ничего не додумывает: если мост молчит, панель
// показывает «нет ответа моста», но не рисует кадры из кэша как свежие.

function renderStereoSummary(status) {
  const session = status?.session;
  const element = $("stereoSummary");
  if (!session?.active) {
    element.textContent = stereoCameras?.pending
      ? "Идёт поиск USB-камер…"
      : "Стереорежим не запущен.";
    return;
  }
  const errors = Object.values(session.errors || {}).map((error) => error.message);
  const uptime = formatMs(session.uptimeMs);
  const states = session.streamStates || {};
  const stateText = ["a", "b"]
    .map((role) => `${role.toUpperCase()}: ${cameraStateLabel(states[role])}`)
    .join(", ");
  element.textContent = errors.length
    ? `Сессия активна ${uptime}. ${stateText}. Ошибка: ${errors.join(" ")}`
    : `Сессия активна ${uptime}. ${stateText}.`;
}

function renderStereoCalibration(status) {
  const state = calibrationStateLabel(status?.calibration?.state);
  const badge = $("stereoCalibrationState");
  badge.textContent = state.title;
  badge.dataset.tone = state.tone;
  const steps = calibrationSteps(status);
  const list = $("stereoSteps");
  list.innerHTML = "";
  steps.forEach((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    list.append(item);
  });
  const reasons = $("stereoReasons");
  reasons.innerHTML = "";
  (status?.depthEnabled ? [] : status?.calibration?.reasons || []).forEach((reason) => {
    const item = document.createElement("li");
    item.textContent = reason;
    reasons.append(item);
  });
  const quality = $("stereoQuality");
  const detail = status?.quality || status?.calibration?.quality;
  if (!detail) {
    quality.textContent = status?.depthEnabled
      ? "Глубина включена; метки времени программные."
      : "Качество калибровки не подтверждено.";
  } else {
    const rms = typeof detail.rmsPx === "number" ? `RMS ${detail.rmsPx.toFixed(2)} px` : "RMS неизвестен";
    quality.textContent = `${rms}; пар: ${detail.pairs ?? "—"}. ${
      status?.depthEnabled ? "Глубина включена." : "Глубина не включена: проверка качества не пройдена."
    }`;
  }
}

function renderStereo(status) {
  stereoStatus = status || null;
  if (!stereoStatus?.session?.active) {
    // Номера кадров начинаются заново в каждой сессии: старые версии нельзя
    // передавать в новую, иначе первый кадр придёт «без изменений» и без base64.
    stereoVersions = { previewVersions: {}, depthVersion: null };
  }
  const plan = previewPlan(stereoVersions, stereoStatus);
  stereoVersions = plan.next;
  if (plan.redraw.a) setStereoPreview("a", stereoStatus?.previews?.a);
  if (plan.redraw.b) setStereoPreview("b", stereoStatus?.previews?.b);
  if (plan.redraw.depth || !stereoStatus?.depth) drawStereoDepth(stereoStatus);
  renderStereoSelects();
  renderStereoDiagnostics(stereoStatus);
  renderStereoScanProgress();
  renderStereoCalibration(stereoStatus);
  renderStereoSummary(stereoStatus);
  const active = Boolean(stereoStatus?.session?.active);
  const views = stereoStatus?.views || {};
  $("stereoCapture").disabled = !active || stereoBusy;
  $("stereoResetViews").disabled = !active || stereoBusy || !views.count;
  $("stereoCalibrate").disabled = !active || stereoBusy || (views.count || 0) < (views.required || 5);
}

function stereoStatusUrl(withTimings) {
  return stereoPollQuery({
    previewVersions: stereoVersions.previewVersions,
    depthVersion: stereoVersions.depthVersion,
    timings: withTimings,
  });
}

async function refreshStereoSessions(generation = stereoGeneration) {
  try {
    const status = await apiRequest(stereoStatusUrl(false));
    if (generation !== stereoGeneration) return null;
    renderStereo(status);
    return status;
  } catch (error) {
    if (generation === stereoGeneration) {
      // Мост не ответил: состояние сессии неизвестно, поэтому панель говорит об
      // этом прямо и продолжает опрос — после восстановления всё придёт из /api/stereo.
      $("stereoSummary").textContent = `Нет ответа моста: ${error.message}. Повторяю опрос…`;
    }
    return null;
  }
}

async function pollStereo(generation) {
  if (stereoInFlight || document.hidden) return;
  const controller = new AbortController();
  stereoInFlight = controller;
  stereoPollCount += 1;
  try {
    const withTimings = stereoPollCount % STEREO_TIMINGS_EVERY === 1;
    const status = await apiRequest(stereoStatusUrl(withTimings), {
      timeoutMs: 4000,
      signal: controller.signal,
    });
    if (generation !== stereoGeneration) return;
    renderStereo(status);
  } catch (error) {
    if (generation === stereoGeneration && !controller.signal.aborted) {
      stereoLastStatusText = `Нет ответа моста: ${error.message}`;
      $("stereoSummary").textContent = `${stereoLastStatusText}. Повторяю опрос…`;
    }
  } finally {
    if (stereoInFlight === controller) stereoInFlight = null;
  }
}

function startStereoPolling() {
  stopStereoPolling();
  const generation = stereoGeneration;
  void refreshStereoSessions(generation);
  void loadStereoCameras({ silent: true });
  stereoPollTimer = setInterval(() => void pollStereo(generation), STEREO_POLL_MS);
}

function stopStereoPolling() {
  if (stereoPollTimer) clearInterval(stereoPollTimer);
  stereoPollTimer = null;
  if (stereoScanTimer) clearTimeout(stereoScanTimer);
  stereoScanTimer = null;
  if (stereoInFlight) {
    stereoInFlight.abort();
    stereoInFlight = null;
  }
  stereoGeneration += 1;
}

async function loadStereoCameras({ silent = false } = {}) {
  try {
    const catalog = await apiRequest("/api/stereo/cameras");
    stereoCameras = { ...catalog, __announced: Boolean(silent) || catalog.pending };
    renderStereoSelects();
    renderStereoScanProgress();
    return catalog;
  } catch (error) {
    if (error.code === "session_active") {
      // Активная сессия занимает камеры: каталог в этот момент недоступен, и это
      // не ошибка для пользователя — список остаётся прежним.
      return null;
    }
    if (!silent) showToast(error.message, "warning");
    return null;
  }
}

function scheduleStereoScanPoll(generation = stereoGeneration) {
  if (stereoScanTimer) clearTimeout(stereoScanTimer);
  stereoScanTimer = setTimeout(async () => {
    stereoScanTimer = null;
    if (generation !== stereoGeneration) return;
    const catalog = await loadStereoCameras({ silent: true });
    if (!catalog) return;
    if (catalog.pending) {
      // Поиск идёт в фоне: каждый опрос — мгновенный ответ из состояния job-а.
      stereoCameras.__announced = false;
      renderStereoScanProgress();
      scheduleStereoScanPoll(generation);
      return;
    }
    stereoCameras.__announced = false;
    renderStereoScanProgress();
    renderStereoSelects();
  }, 300);
}

function initStereoUi() {
  const list = $("stereoWarnings");
  stereoWarningLines().forEach((line) => {
    const item = document.createElement("li");
    item.textContent = line;
    list.append(item);
  });
  renderStereoSelects();
  // Каталог читается из кэша процесса: это мгновенный ответ, устройства не открываются.
  void loadStereoCameras({ silent: true });
}

function initEnvironmentUi() {
  devices = fallbackDevices();
  renderSourceOptions();
  updateSourcePanel();
  scheduleSetupPoll(0);
}

initStereoUi();

// Demo is immediately usable without Python, a camera, or a depth sensor.
originalPoints = demoPoints.slice();
renderPoints();
renderDemoFeeds();
updateSourcePanel();
initEnvironmentUi();
