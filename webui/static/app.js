// v1.28.27: grace после рестарта bridge (синхронно с Python
// BRIDGE_STARTUP_GRACE_SEC = 60). computeProblems использовал эту
// константу, но она не была объявлена в JS → ReferenceError.
const BRIDGE_STARTUP_GRACE_SEC = 60;
// v1.28.27: локальный STATE для bridge ping_mode / bridge_started_at.
const BRIDGE_STATE = { bridge_ping_mode: null, bridge_started_at: 0 };

const logsEl = document.getElementById("logs");
// v1.23.8: состояние лог-панели выживает при переключении вкладок
// (каждая вкладка — отдельная HTML-страница, JS-переменные сбрасываются).
const LOG_UI_STATE_KEY = "tuya_webui_log_ui_state";
function _logUiLoad() {
  try {
    const s = JSON.parse(sessionStorage.getItem(LOG_UI_STATE_KEY) || "{}");
    return s && typeof s === "object" ? s : {};
  } catch (e) { return {}; }
}
function _logUiSave() {
  try {
    sessionStorage.setItem(LOG_UI_STATE_KEY, JSON.stringify({
      range: LOG_RANGE_SECONDS,
      paused: logPaused,
      search: SEARCH_TERM,
      scrolledUp: userScrolledUp,
    }));
  } catch (e) {}
}
const _LOG_UI = _logUiLoad();
// v1.28.92: период логов хранится в localStorage (переживает перезапуск
// браузера) — «автодефолт 1 час» применяется только если памяти нет.
const LOG_RANGE_KEY = "tuya_webui_log_range";
let LOG_RANGE_SECONDS = (function () {
  try {
    const v = localStorage.getItem(LOG_RANGE_KEY);
    if (v !== null && v !== "") {
      const n = parseInt(v, 10);
      if (!isNaN(n) && n >= 0) return n;
    }
  } catch (e) {}
  if (typeof _LOG_UI.range === "number") return _LOG_UI.range;   // миграция из sessionStorage
  return 3600;
})();
let logPaused = !!_LOG_UI.paused;
// v1.25.12: lastSeq — per-source в sessionStorage. Раньше был общий:
// при переключении bridge↔webui SSE шёл с «чужим» seq и не получал
// старые записи нового источника.
const _LOG_SEQ_KEY = "tuya_webui_log_seq";
let LOG_SEQ_BY_SOURCE = (function () {
  try {
    const raw = sessionStorage.getItem(_LOG_SEQ_KEY);
    const o = raw ? JSON.parse(raw) : {};
    return {
      bridge: typeof o.bridge === "number" ? o.bridge : 0,
      webui: typeof o.webui === "number" ? o.webui : 0,
    };
  } catch (e) { return { bridge: 0, webui: 0 }; }
})();
function _saveLogSeq() {
  try { sessionStorage.setItem(_LOG_SEQ_KEY, JSON.stringify(LOG_SEQ_BY_SOURCE)); } catch (e) {}
}
function _getLastSeq() { return LOG_SEQ_BY_SOURCE[LOG_SOURCE] || 0; }
function _setLastSeq(v) { LOG_SEQ_BY_SOURCE[LOG_SOURCE] = v; _saveLogSeq(); }
let userScrolledUp = !!_LOG_UI.scrolledUp;
let LAST_DEVICES = [];
let CURRENT_MODAL_IDX = -1;
// v1.25.13: имя устройства, открытого в модалке. Нужно,
// чтобы closeModal() чистил кэш даже если CURRENT_MODAL_IDX
// уже сброшен (повторный вызов, закрытие по Escape).
let CURRENT_MODAL_NAME = null;
let LOG_LEVEL_FILTER = "INFO";
let LOG_SOURCE = (function(){ try { return localStorage.getItem("tuya_webui_log_source") || "bridge"; } catch(e){ return "bridge"; } })();  // v1.22.0
let LOG_SOURCE_TOKEN = 0;  // P2 1.22.0: токен для race
// v1.28.88: у КАЖДОГО источника логов свой запомненный уровень.
const _LEVELS_ALLOWED = ["DEBUG", "INFO", "WARNING", "ERROR"];
function _loadLevel(key, def) {
  try {
    const v = localStorage.getItem(key);
    if (v && _LEVELS_ALLOWED.includes(v)) return v;
  } catch (e) {}
  return def;
}
let _LOG_LEVELS = {
  bridge: _loadLevel("tuya_webui_bridge_level", "INFO"),
  webui:  _loadLevel("tuya_webui_webui_level", "DEBUG"),
};
function _saveLogLevel(src, lvl) {
  _LOG_LEVELS[src] = lvl;
  try { localStorage.setItem("tuya_webui_" + src + "_level", lvl); } catch (e) {}
}
let SEARCH_TERM = (typeof _LOG_UI.search === "string") ? _LOG_UI.search : "";
let SEARCH_MATCHES = [], SEARCH_CURRENT = -1;
let VIEW = "dashboard";
let REVEALED_KEYS = {};
let CLOUD_DETAILS_OPEN = {};
let CLOUD_DEVICES = [], CLOUD_SELECTED = {};
// v1.27.12: Cloud Local key — стейт показа per-device (по tuya_id).
// Сбрасывается при closeCloudModal.
let CLOUD_REVEALED_KEYS = {};
let DEVICE_HISTORY_CACHE = {}, DEVICE_LATENCY_CACHE = {}, DEVICE_AVG_LATENCY_CACHE = {};
// v1.25.0 (task #A): сортировка выживает при F5 (localStorage).
function _loadSort(prefix, defKey, defDir) {
  try {
    const raw = localStorage.getItem("tuya_webui_sort_" + prefix);
    if (!raw) return { key: defKey, dir: defDir };
    const o = JSON.parse(raw);
    if (o && typeof o.key === "string" && (o.dir === 1 || o.dir === -1)) return o;
  } catch (e) {}
  return { key: defKey, dir: defDir };
}
function _saveSort(prefix, key, dir) {
  try { localStorage.setItem("tuya_webui_sort_" + prefix, JSON.stringify({ key, dir })); } catch (e) {}
}
const _SORT_DEV = _loadSort("devices", "name", 1);
let SORT_KEY = _SORT_DEV.key, SORT_DIR = _SORT_DEV.dir;
// v1.27.9: отдельная сортировка для секции «⛔ Отключённые».
const _SORT_DEV_DIS = _loadSort("disabled", "name", 1);
let SORT_KEY_DISABLED = _SORT_DEV_DIS.key, SORT_DIR_DISABLED = _SORT_DEV_DIS.dir;

let LATENCY_PERIOD = 3600;      // v1.18.6: по умолчанию 1 час
let LATENCY_DATA = [];
const _SORT_LAT = _loadSort("latency", "avg", 1);
let LATENCY_SORT_KEY = _SORT_LAT.key, LATENCY_SORT_DIR = _SORT_LAT.dir;
let FLAPPER_DATA = [];
const _SORT_FLAP = _loadSort("flappers", "flaps", -1);
let FLAPPER_SORT_KEY = _SORT_FLAP.key, FLAPPER_SORT_DIR = _SORT_FLAP.dir;

let SCAN_RESULTS = [];
let SCAN_SUBNET = "";
let SCAN_TS = 0;                     // v1.28.74: когда был замер
let BRIDGE_SCAN_RUNNING = false;
// v1.28.74: результаты храним в sessionStorage (до закрытия вкладки),
// старше 24 ч — затираем при загрузке.
const SCAN_CACHE_KEY = "tuya_scan_results_v1";
function _saveScanCache() {
  try {
    sessionStorage.setItem(SCAN_CACHE_KEY, JSON.stringify({
      ts: SCAN_TS, subnet: SCAN_SUBNET, hosts: SCAN_RESULTS,
    }));
  } catch (e) {}
}
function _loadScanCache() {
  try {
    const raw = sessionStorage.getItem(SCAN_CACHE_KEY);
    if (!raw) return false;
    const obj = JSON.parse(raw);
    if (!obj || !Array.isArray(obj.hosts) || obj.hosts.length === 0) return false;
    if (Math.floor(Date.now() / 1000) - (obj.ts || 0) > 86400) {
      sessionStorage.removeItem(SCAN_CACHE_KEY);
      return false;
    }
    SCAN_RESULTS = obj.hosts;
    SCAN_SUBNET = obj.subnet || "";
    SCAN_TS = obj.ts || 0;
    return true;
  } catch (e) { return false; }
}

let LOG_BUFFER = [];
const LOG_BUFFER_MAX = 5000;

let TOOLS_CONFIG = null;
let TOOLS_VIEW = "raw";
let TOOLS_SELECTED_IDX = -1;

let PREVIEW_DEVICES = [];
// v1.28.34: выбор источника данных в превью импорта.
let PREVIEW_SOURCE = "auto";        // auto|cloud|cache|tuya_local|heuristic
let PREVIEW_ROW_SOURCE = {};        // "idx:dp" -> source (ручное переопределение)
const PREVIEW_SOURCES = [
  { id: "auto",       label: "Авто",                  icon: "🤖" },
  { id: "cloud",      label: "Tuya Cloud",            icon: "☁" },
  { id: "cache",      label: "Текущий",               icon: "⚙️" },
  { id: "tuya_local", label: "tuya-local",            icon: "📚" },
  { id: "heuristic",  label: "эвристика (значение)",  icon: "⚠️" },
];
let PREVIEW_CURRENT = 0;

let REBUILD_POLL_TIMER = null;
let TUYA_LOCAL_POLL_TIMER = null;
let CLOUD_CACHE_FETCHED_AT = 0;
let EDIT_DEVICE_NAME = null;

const LOG_TS_RE = /^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})/;
const LEVEL_ORDER = { DEBUG:0, INFO:1, WARNING:2, ERROR:3, CRITICAL:4 };

const CLOUD_CREDS_KEY = "tuya_cloud_creds";
const THEME_KEY = "tuya_webui_theme";

// v1.28.16: язык preset-режимов для climate при импорте.
//   "ru"    — Tuya→RU (auto→"Автоматический режим"), preset_map
//             реально пишется в devices_config.json.
//   "as-is" — не заполняем preset_map, bridge сам подставит
//             Tuya-значения (auto/comfort/eco) на лету.
// Хранится в localStorage (tuya_webui_preset_lang).
const PRESET_LANG_KEY = "tuya_webui_preset_lang";
let PRESET_LANG = (function() {
  try {
    const v = localStorage.getItem(PRESET_LANG_KEY);
    if (v === "ru" || v === "as-is") return v;
  } catch (e) {}
  return "ru";  // по умолчанию — русский
})();

// v1.28.16: словарь Tuya preset → RU. Незнакомые значения
// остаются as-is (auto → auto), чтобы не терять данные.
const PRESET_TUYA_TO_RU = {
  "auto":       "Автоматический режим",
  "comfort":    "Комфортный режим",
  "eco":        "Режим экономии",
  "manual":     "Ручной режим",
  "program":    "Программный режим",
  "temporary":  "Временный режим",
  "cold":       "Холодный режим",
  "hot":        "Горячий режим",
  "wind":       "Временный режим",
  "comfortable": "Комфорт",
  "energy":     "Энергосбережение",
  "holiday":    "Отпуск",
  "dry":        "Сушка",
  "floor_heat": "Тёплый пол",
  "auxiliary_heat": "Доп. обогрев",
  "smart":      "Умный режим",
  "away":       "Отсутствие",
  "home":       "Дома",
  "sleep":      "Сон",
  "boost":      "Ускорение",
  "antifreeze": "Защита от замерзания",
  "auto_1":     "Автоматический режим 1",
  "comfort_1":  "Комфортный режим 1",
  "comfort_2":  "Комфортный режим 2",
  "eco_1":      "Режим экономии 1",
  "manual_1":   "Ручной режим 1",
};
const CLOUD_CACHE_STALE_AFTER = 600;
const CLOUD_CACHE_WARN_AFTER = 6 * 3600;
const CLOUD_CACHE_OLD_AFTER = 24 * 3600;
const CLOUD_CACHE_BANNER_KEY = "cloud_cache_banner_dismissed_at";

const JUNK_DP_CODES = new Set([
  "refresh", "clear_energy", "clr_all_energy",
  "leakagecurr_test", "charge_energy",
  "alarm_set_1", "alarm_set_2",
  "breaker_id", "sn",
  "scene_data", "music_data", "control_data",
  "voltage_coe", "electric_coe", "power_coe", "electricity_coe",
  "test_bit",
  "work_days", "holiday_days_set",
  "factory_reset",
  // v1.27.5: сервисные String/Enum DP, бесполезные в HA
  "cycle_time", "random_time", "switch_inching", "light_mode",
]);

// ==================== UI CONFIRM / ALERT (v1.18.6) ====================
let _uiConfirmResolver = null;

function uiConfirm(title, message, opts) {
  opts = opts || {};
  return new Promise((resolve) => {
    _uiConfirmResolver = resolve;
    document.getElementById("ui-confirm-title").textContent = title || "Подтверждение";
    document.getElementById("ui-confirm-text").textContent = message || "";
    const okBtn = document.getElementById("ui-confirm-ok");
    okBtn.textContent = opts.okText || "OK";
    okBtn.className = opts.danger ? "danger" : "primary";
    const cancelBtn = document.getElementById("ui-confirm-cancel");
    cancelBtn.textContent = opts.cancelText || "Отмена";
    cancelBtn.style.display = (opts.hideCancel ? "none" : "inline-block");
    document.getElementById("ui-confirm-overlay").classList.add("open");
    setTimeout(() => okBtn.focus(), 50);
  });
}

function closeUiConfirm(evt, result) {
  if (evt && evt.target && evt.target.id !== "ui-confirm-overlay") return;
  document.getElementById("ui-confirm-overlay").classList.remove("open");
  const r = _uiConfirmResolver;
  _uiConfirmResolver = null;
  if (r) r(!!result);
}

let _uiPromptResolver = null;
let _uiPromptValidate = null;

function uiPrompt(title, message, opts) {
  opts = opts || {};
  return new Promise((resolve) => {
    _uiPromptResolver = resolve;
    _uiPromptValidate = opts.validate || null;
    document.getElementById("ui-prompt-title").textContent = title || "Ввод";
    document.getElementById("ui-prompt-text").textContent = message || "";
    const inp = document.getElementById("ui-prompt-input");
    inp.value = opts.value || "";
    inp.placeholder = opts.placeholder || "";
    inp.type = opts.type || "text";
    const errEl = document.getElementById("ui-prompt-error");
    errEl.style.display = "none";
    errEl.textContent = "";
    const okBtn = document.getElementById("ui-prompt-ok");
    okBtn.textContent = opts.okText || "OK";
    okBtn.className = opts.danger ? "danger" : "primary";
    const cancelBtn = document.getElementById("ui-prompt-cancel");
    cancelBtn.textContent = opts.cancelText || "Отмена";
    cancelBtn.style.display = (opts.hideCancel ? "none" : "inline-block");
    // v1.27.7: опциональная кнопка «Пропустить».
    // resolve(undefined) — пропустить (отличимо от null — отмена).
    const skipBtn = document.getElementById("ui-prompt-skip");
    if (skipBtn) {
      if (opts.skipText) {
        skipBtn.textContent = opts.skipText;
        skipBtn.style.display = "inline-block";
      } else {
        skipBtn.style.display = "none";
      }
    }
    document.getElementById("ui-prompt-overlay").classList.add("open");
    setTimeout(() => { inp.focus(); inp.select(); }, 50);
  });
}

function submitUiPrompt() {
  const inp = document.getElementById("ui-prompt-input");
  const val = inp.value;
  if (_uiPromptValidate) {
    const err = _uiPromptValidate(val);
    if (err) {
      const errEl = document.getElementById("ui-prompt-error");
      errEl.style.display = "block";
      errEl.textContent = err;
      return;
    }
  }
  // v1.28.9: пустая строка == отмена (null), не «Пропустить» (undefined).
  if (val === "") { closeUiPrompt(null, null); return; }
  closeUiPrompt(null, val);
}

function closeUiPrompt(evt, result) {
  if (evt && evt.target && evt.target.id !== "ui-prompt-overlay") return;
  document.getElementById("ui-prompt-overlay").classList.remove("open");
  const r = _uiPromptResolver;
  _uiPromptResolver = null;
  _uiPromptValidate = null;
  // v1.28.9: сохраняем undefined как маркер «Пропустить».
  // Раньше undefined → null, и «Пропустить» работало как «Отмена».
  if (r) r(result);
}

let _uiAlertResolver = null;

function uiAlert(title, message, type) {
  return new Promise((resolve) => {
    _uiAlertResolver = resolve;
    document.getElementById("ui-alert-title").textContent = title || "Сообщение";
    document.getElementById("ui-alert-text").textContent = message || "";
    const ic = document.getElementById("ui-alert-icon");
    if (type === "error") ic.textContent = "❌";
    else if (type === "warning") ic.textContent = "⚠️";
    else if (type === "success") ic.textContent = "✅";
    else ic.textContent = "ℹ️";
    document.getElementById("ui-alert-overlay").classList.add("open");
  });
}

function closeUiAlert(evt) {
  if (evt && evt.target && evt.target.id !== "ui-alert-overlay") return;
  document.getElementById("ui-alert-overlay").classList.remove("open");
  const r = _uiAlertResolver;
  _uiAlertResolver = null;
  if (r) r();
}

// ==================== CHART TOOLTIP (v1.24.6) ====================
// Единый tooltip для SVG-графиков.
//   ПК: mousemove → tooltip следует за курсором.
//   Мобиль: быстрый тап → 10 сек; long-press (1 сек) → фиксация
//           + touchmove для смены точки. Скролл (>8px) отменяет.
// Позиция: сначала контент, потом двойной rAF, потом position —
//           чтобы tooltip не «прыгал».
const _chartTooltip = (function() {
  let el = null;
  let visible = false;
  let hideTimer = null;
  let activeSvg = null;
  let activeHandler = null;
  let pendingRaf = null;

  function ensureEl() {
    if (el) return el;
    el = document.createElement("div");
    el.className = "chart-tooltip";
    el.style.display = "none";
    document.body.appendChild(el);
    return el;
  }

  function position(x, y, opts) {
    if (!el) return;
    const pad = 12;
    const rect = el.getBoundingClientRect();
    const w = rect.width || el.offsetWidth || 200;
    const h = rect.height || el.offsetHeight || 60;
    const vw = document.documentElement.clientWidth || window.innerWidth;
    const vh = document.documentElement.clientHeight || window.innerHeight;
    let left = x + pad;
    let top = y + pad;
    if (opts && opts.centerAbove) {
      left = x - w / 2;
      top = y - h - 16;
      if (top < 8) top = y + pad;
    }
    if (left + w > vw - 8) left = vw - w - 8;
    if (left < 8) left = 8;
    if (top + h > vh - 8) top = vh - h - 8;
    if (top < 8) top = 8;
    el.style.left = Math.round(left) + "px";
    el.style.top = Math.round(top) + "px";
  }

  function show(html, x, y, opts) {
    ensureEl();
    el.innerHTML = html;
    el.style.display = "block";
    el.style.visibility = "hidden";
    el.style.left = "-9999px";
    el.style.top = "0px";
    visible = true;
    if (pendingRaf) cancelAnimationFrame(pendingRaf);
    pendingRaf = requestAnimationFrame(() => {
      position(x, y, opts);
      el.style.visibility = "visible";
      pendingRaf = requestAnimationFrame(() => {
        position(x, y, opts);
        pendingRaf = null;
      });
    });
  }

  function hide() {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (pendingRaf) { cancelAnimationFrame(pendingRaf); pendingRaf = null; }
    visible = false;
    if (el) el.style.display = "none";
    if (activeSvg && activeHandler && activeHandler.onLeave) {
      activeHandler.onLeave(activeSvg);
    }
    activeSvg = null;
    activeHandler = null;
  }

  function bindDesktop(svg, handler) {
    if (!svg) return;
    // v1.25.14: AbortController — явно снимаем старых слушателей.
    // Раньше `delete svg.dataset.ttBound` в renderActivity/
    // renderFlapsChart приводил к тому, что bind снова навешивал
    // mousemove/mouseleave на тот же SVG (id chart-activity не
    // меняется), слушатели копились каждые 30 сек автообновления.
    if (svg._ttAbortDesktop) {
      try { svg._ttAbortDesktop.abort(); } catch (e) {}
    }
    svg._ttAbortDesktop = new AbortController();
    // v1.26.0: возвращаем флаг — guard в renderActivity/
    // renderFlapsChart проверяет svg.dataset.ttBound === "1".
    svg.dataset.ttBound = "1";
    const sig = svg._ttAbortDesktop.signal;
    svg.addEventListener("mousemove", (e) => {
      const hit = handler.hitTest(svg, e);
      if (!hit) { hide(); return; }
      activeSvg = svg;
      activeHandler = handler;
      handler.onHover(svg, hit);
      show(handler.render(hit), e.clientX, e.clientY, {});
    }, { signal: sig });
    svg.addEventListener("mouseleave", () => { hide(); }, { signal: sig });
  }

  // Мобиль: только быстрый тап. Long-press убран.
  function bindMobile(svg, handler) {
    if (!svg) return;
    // v1.25.14: AbortController — как в bindDesktop.
    if (svg._ttAbortMobile) {
      try { svg._ttAbortMobile.abort(); } catch (e) {}
    }
    svg._ttAbortMobile = new AbortController();
    // v1.26.0: возвращаем флаг (см. bindDesktop).
    svg.dataset.ttMobileBound = "1";
    const sig = svg._ttAbortMobile.signal;
    let touchStartX = 0, touchStartY = 0;
    let moved = false;

    svg.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) return;
      const t = e.touches[0];
      touchStartX = t.clientX;
      touchStartY = t.clientY;
      moved = false;
    }, { passive: true, signal: sig });

    svg.addEventListener("touchmove", (e) => {
      const t = e.touches[0];
      const dist = Math.hypot(t.clientX - touchStartX, t.clientY - touchStartY);
      if (dist > 8) moved = true;
    }, { passive: true, signal: sig });

    svg.addEventListener("touchend", (e) => {
      if (moved) return;
      const t = (e.changedTouches && e.changedTouches[0]) || null;
      if (!t) return;
      const hit = handler.hitTest(svg, t);
      if (!hit) return;
      activeSvg = svg;
      activeHandler = handler;
      handler.onHover(svg, hit);
      show(handler.render(hit), t.clientX, t.clientY, { centerAbove: true });
      hideTimer = setTimeout(() => { hide(); }, 10000);
    }, { signal: sig });
    svg.addEventListener("touchcancel", () => { hide(); }, { signal: sig });
  }

  function bind(svg, handler) {
    bindDesktop(svg, handler);
    bindMobile(svg, handler);
  }

  return {
    bind,
    hide,
    isOpen: () => visible,
  };
})();

// ==================== DP TOOLTIP (v1.27.4) ====================
// Единый глобальный tooltip для .dp-tip и .tip-generic.
// Не обрезается overflow: hidden. ПК: hover. Мобиль: tap → 10 сек.
// Позиция: снизу от значка (для графиков используется свой _chartTooltip).
const _dpTooltip = (function() {
  let el = null;
  let hideTimer = null;
  let activeEl = null;

  function ensureEl() {
    if (el) return el;
    el = document.createElement("div");
    el.id = "dp-tooltip";
    el.style.display = "none";
    document.body.appendChild(el);
    return el;
  }

  function position(x, y) {
    if (!el) return;
    const pad = 10;
    const rect = el.getBoundingClientRect();
    const w = rect.width || el.offsetWidth || 200;
    const h = rect.height || el.offsetHeight || 60;
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    // Показываем снизу от значка, центрировано.
    let left = x - w / 2;
    let top = y + pad;
    if (left + w > vw - 8) left = vw - w - 8;
    if (left < 8) left = 8;
    if (top + h > vh - 8) top = y - h - 18;   // не влезает вниз — наверх
    if (top < 8) top = 8;
    el.style.left = Math.round(left) + "px";
    el.style.top = Math.round(top) + "px";
  }

  function show(targetEl) {
    if (!targetEl) return;
    const text = targetEl.dataset.tip || "";
    if (!text) return;
    ensureEl();
    el.textContent = text;
    el.style.display = "block";
    el.style.visibility = "hidden";
    el.style.left = "-9999px";
    el.style.top = "0px";
    activeEl = targetEl;
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    const rect = targetEl.getBoundingClientRect();
    requestAnimationFrame(() => {
      position(rect.left + rect.width / 2, rect.bottom);
      el.style.visibility = "visible";
    });
  }

  function hide() {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (el) el.style.display = "none";
    activeEl = null;
  }

  function _sel(el) {
    // v1.27.11: .badge-locked тоже через JS-тултип.
    return el && el.closest ? el.closest(".dp-tip, .tip-generic, .badge-locked") : null;
  }

  // ПК: hover
  document.addEventListener("mouseover", (e) => {
    const t = _sel(e.target);
    if (!t || !t.dataset.tip) return;
    if (t === activeEl) return;
    show(t);
  });
  document.addEventListener("mouseout", (e) => {
    const t = _sel(e.target);
    if (!t) return;
    // mouseout может сработать на child — проверяем relatedTarget
    const related = _sel(e.relatedTarget);
    if (related === t) return;
    hide();
  });

  // Мобиль: tap → 10 сек
  document.addEventListener("touchstart", (e) => {
    const t = _sel(e.target);
    if (!t || !t.dataset.tip) {
      // тап вне значка — скрыть, если открыт
      if (activeEl) hide();
      return;
    }
    e.preventDefault();
    show(t);
    hideTimer = setTimeout(() => hide(), 10000);
  }, { passive: false });

  // Скролл/ресайз — скрыть (координаты устаревают)
  window.addEventListener("scroll", hide, { passive: true });
  window.addEventListener("resize", hide);

  return { show, hide };
})();

// v1.25.0 (fix #6): при уходе с вкладки (blur / visibilitychange)
// закрываем tooltip — иначе _chartTooltip.isOpen() навсегда true,
// и renderActivity/renderFlapsChart не перерисовываются из-за guard.
(function bindTooltipAutoClose() {
  function closeAll() {
    try { _chartTooltip.hide(); } catch (e) {}
  }
  window.addEventListener("blur", closeAll);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) closeAll();
  });
})();

// SVG-координаты из client-координат.
function svgClientToLocal(svg, clientX, clientY) {
  const rect = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  const scaleX = vb.width / rect.width;
  const scaleY = vb.height / rect.height;
  return {
    x: (clientX - rect.left) * scaleX,
    y: (clientY - rect.top) * scaleY,
  };
}

function ttEscape(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ==================== JSON HIGHLIGHT (v1.18.6) ====================
function highlightJsonInto(el) {
  if (!el) return;
  const txt = el.textContent || "";
  const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const escaped = esc(txt);
  // Порядок правил важен: сначала ключи (строка + :), потом строки, потом литералы, числа, пунктуация
  const html = escaped.replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|\b(-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\b|([{}\[\],:])/g,
    (m, str, colon, lit, num, punct) => {
      if (str !== undefined && colon !== undefined)
        return `<span class="hljs-attr">${str}</span><span class="hljs-punctuation">${colon}</span>`;
      if (str !== undefined)
        return `<span class="hljs-string">${str}</span>`;
      if (lit !== undefined)
        return `<span class="hljs-literal">${lit}</span>`;
      if (num !== undefined)
        return `<span class="hljs-number">${num}</span>`;
      if (punct !== undefined)
        return `<span class="hljs-punctuation">${punct}</span>`;
      return m;
    }
  );
  el.innerHTML = html;
}

function isMobile() { return window.innerWidth < 700; }

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = document.getElementById("theme-btn");
  if (btn) btn.textContent = theme === "dark" ? "☀️" : "🌙";
  try { localStorage.setItem(THEME_KEY, theme); } catch (e) {}
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(cur === "dark" ? "light" : "dark");
}

// v1.23.3: burger-меню для мобильной навигации
// v1.28.48: клик по заголовку — на дашборд.
function goHome() { window.location.href = "/"; }

function toggleNav() {
  const nav = document.getElementById("main-nav");
  const btn = document.getElementById("nav-toggle");
  if (!nav) return;
  const isOpen = nav.classList.toggle("nav-open");
  if (btn) btn.textContent = isOpen ? "✕" : "☰";
}
function _closeNavIfOpen() {
  const nav = document.getElementById("main-nav");
  const btn = document.getElementById("nav-toggle");
  if (!nav || !nav.classList.contains("nav-open")) return;
  nav.classList.remove("nav-open");
  if (btn) btn.textContent = "☰";
}
document.addEventListener("click", (e) => {
  const nav = document.getElementById("main-nav");
  if (!nav || !nav.classList.contains("nav-open")) return;
  if (e.target.closest("#main-nav") || e.target.closest("#nav-toggle")) return;
  _closeNavIfOpen();
});
function initTheme() {
  let t = "dark";
  try { t = localStorage.getItem(THEME_KEY) || "dark"; } catch (e) {}
  applyTheme(t);
}

initTheme();

// v1.24.0: держим body.modal-open, пока есть хоть одна открытая
// модалка. MutationObserver следит за class "open" на всех
// .modal-overlay — работает для всех 9 модалок (device, cloud,
// preview, edit, db-cleanup, timeline-cleanup, ui-confirm,
// ui-prompt, ui-alert) без ручных правок в каждом open/close.
(function bindModalOpenFreeze() {
  function syncBodyModalOpen() {
    const anyOpen = document.querySelector(".modal-overlay.open") !== null;
    document.body.classList.toggle("modal-open", anyOpen);
  }
  function bindAll() {
    syncBodyModalOpen();
    updateScanHint();   // v1.28.46: подсказка «сканируются адреса …»
    const obs = new MutationObserver(syncBodyModalOpen);
    document.querySelectorAll(".modal-overlay").forEach(ov => {
      obs.observe(ov, { attributes: true, attributeFilter: ["class"] });
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindAll);
  } else {
    bindAll();
  }
})();

if (!ANALYTICS_ENABLED) {
  const n = document.getElementById("nav-analytics");
  if (n) n.style.display = "none";
}

function parseLogTs(msg) {
  if (!msg) return null;
  const m = msg.match(LOG_TS_RE);
  if (!m) return null;
  try {
    const d = new Date(
      parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10),
      parseInt(m[4], 10), parseInt(m[5], 10), parseInt(m[6], 10)
    );
    return Math.floor(d.getTime() / 1000);
  } catch (e) { return null; }
}

function detectView() {
  const p = window.location.pathname;
  if (p.startsWith("/analytics")) {
    if (!ANALYTICS_ENABLED) { window.location.replace("/"); return; }
    VIEW = "analytics";
  } else if (p.startsWith("/import")) VIEW = "import";
  else if (p.startsWith("/tools")) VIEW = "tools";
  else if (p.startsWith("/help")) VIEW = "help";
  else VIEW = "dashboard";
  document.getElementById("view-dashboard").style.display = VIEW === "dashboard" ? "block" : "none";
  document.getElementById("view-analytics").style.display = VIEW === "analytics" ? "block" : "none";
  document.getElementById("view-import").style.display = VIEW === "import" ? "block" : "none";
  document.getElementById("view-tools").style.display = VIEW === "tools" ? "block" : "none";
  document.getElementById("view-help").style.display = VIEW === "help" ? "block" : "none";
  document.getElementById("nav-dashboard").className = VIEW === "dashboard" ? "active" : "";
  if (ANALYTICS_ENABLED) document.getElementById("nav-analytics").className = VIEW === "analytics" ? "active" : "";
  document.getElementById("nav-import").className = VIEW === "import" ? "active" : "";
  document.getElementById("nav-tools").className = VIEW === "tools" ? "active" : "";
  const _navHelp = document.getElementById("nav-help");
  if (_navHelp) _navHelp.className = VIEW === "help" ? "active" : "";
  if (VIEW === "tools") loadConfig();
  if (VIEW === "import") { loadBaseInfo(); _ensureBaseInfoTimer(); }
  if (VIEW === "help") _renderHelp();
  // v1.28.74: вернуть результаты безопасного скана LAN (если ещё свежие).
  if (_loadScanCache()) renderScanResults(SCAN_SUBNET);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}
// v1.25.0 (task #C): зеркало backend-словарей для Cloud-таблицы
// и «Кэш состояния». Используются только для тултипов — колонка
// «Имя» показывает code (английское).
const DP_CODE_NAMES_RU_FRONT = {
    "switch_led": "Свет",
    "switch_led_1": "Свет 1",
    "switch": "Реле",
    "switch_1": "Реле 1",
    "switch_2": "Реле 2",
    "switch_3": "Реле 3",
    "switch_4": "Реле 4",
    "bright_value": "Яркость",
    "bright_value_1": "Яркость 1",
    "temp_value": "Цветовая температура",
    "temp_value_1": "Цветовая температура",
    "colour_data": "Цвет (RGB)",
    "colour_data_v2": "Цвет (RGB v2)",
    "work_mode": "Режим работы",
    "scene_data": "Сцены",
    "flash_scene_1": "Сцена 1",
    "flash_scene_2": "Сцена 2",
    "music_data": "Музыкальный режим",
    "control_data": "Управление",
    "countdown": "Таймер",
    "countdown_1": "Таймер 1",
    "countdown_2": "Таймер 2",
    "countdown_3": "Таймер 3",
    "countdown_4": "Таймер 4",
    "va_temperature": "Текущая температура",
    "temp_current": "Текущая температура",
    "temp_set": "Уставка температуры",
    "temp_current_f": "Текущая темп. (°F)",
    "va_humidity": "Влажность",
    "humidity": "Влажность",
    "battery_percentage": "Батарея (%)",
    "battery_state": "Состояние батареи",
    "battery_value": "Уровень батареи",
    "cur_voltage": "Напряжение",
    "cur_current": "Ток",
    "cur_power": "Мощность",
    "add_ele": "Накопленная энергия",
    "forward_energy_total": "Энергия (всего)",
    "reverse_energy_total": "Энергия (обратно)",
    "phase_a": "Фаза A",
    "phase_b": "Фаза B",
    "phase_c": "Фаза C",
    "fault": "Ошибка",
    "leakage_current": "Ток утечки",
    "supply_frequency": "Частота сети",
    "power_factor": "Коэффициент мощности",
    "electric_total": "Электроэнергия",
    "total_forward_energy": "Общая прямая энергия",
    "output_voltage": "Напряжение",
    "output_current": "Ток",
    "output_power": "Активная мощность",
    "relay_status": "Состояние при включении",
    "switch_backlight": "Подсветка",
    "switch_prepayment": "Предоплата",
    "switch_inching": "Импульсный режим",
    "switch_type": "Тип выключателя",
    "cycle_time": "Циклический таймер",
    "random_time": "Случайный таймер",
    "inching_time": "Время импульса",
    "test_bit": "Результат теста",
    "overcharge_switch": "Защита от перезарядки",
    "light_mode": "Режим индикатора",
    "child_lock": "Блокировка от детей",
    "doorcontact_state": "Дверь",
    "pir": "Движение",
    "motion_sensitivity": "Чувствительность движения",
    "watersensor_state": "Протечка",
    "smoke_sensor_status": "Дым",
    "gas_sensor_status": "Газ",
    "temp_alarm": "Тревога температуры",
    "hum_alarm": "Тревога влажности",
    "mode": "Режим работы",
    "preset_mode": "Пресет",
    "eco": "Эко",
    "window_check": "Обнаружение окна",
    "frost": "Защита от замерзания",
    "valve_check": "Обнаружение клапана",
    "temp_correction": "Коррекция температуры",
    "upper_temp": "Верхний порог",
    "upper_temp_f": "Верхний порог (°F)",
    "lower_temp": "Нижний порог",
    "maxtemp_set": "Верхний порог температуры",
    "minitemp_set": "Нижний порог температуры",
    "maxhum_set": "Верхний порог влажности",
    "minihum_set": "Нижний порог влажности",
    "temp_unit_convert": "Единицы температуры",
    "temp_set_f": "Уставка (°F)",
    "temp_sensitivity": "Чувствительность температуры",
    "hum_sensitivity": "Чувствительность влажности",
    "temp_periodic_report": "Период отчёта температуры",
    "work_days": "Рабочие дни",
    "holiday_days_set": "Дней в режиме отпуска",
    "factory_reset": "Сброс к заводским",
    "do_not_disturb": "Не беспокоить",
    "alarm_set_1": "Настройка тревоги 1",
    "alarm_set_2": "Настройка тревоги 2",
    "clear_energy": "Сброс энергии",
    "clr_all_energy": "Сброс энергии",
    "breaker_id": "ID устройства",
    "refresh": "Обновить",
    "balance_energy": "Остаток энергии",
    "charge_energy": "Пополнение энергии",
    "leakagecurr_test": "Тест тока утечки",
    "voltage_coe": "Калибровка напряжения",
    "electric_coe": "Калибровка тока",
    "power_coe": "Калибровка мощности",
    "electricity_coe": "Калибровка энергии",
};

const DP_CN_NAMES_RU_FRONT = {
    "开关": "Выключатель",
    "开关1": "Выключатель 1",
    "开关2": "Выключатель 2",
    "开关3": "Выключатель 3",
    "开关4": "Выключатель 4",
    "开关1倒计时": "Таймер выключателя 1",
    "开关2倒计时": "Таймер выключателя 2",
    "开关3倒计时": "Таймер выключателя 3",
    "开关4倒计时": "Таймер выключателя 4",
    "上电状态": "Состояние при включении",
    "上电状态设置": "Настройка состояния при включении",
    "背光开关": "Подсветка",
    "循环定时": "Циклический таймер",
    "随机定时": "Случайный таймер",
    "点动开关": "Импульсный режим",
    "开关类型": "Тип выключателя",
    "产测结果位": "Результат теста",
    "故障告警": "Аварийная сигнализация",
    "过充保护": "Защита от перезарядки",
    "模式": "Режим работы",
    "亮度值": "Яркость",
    "冷暖值": "Цветовая температура",
    "场景": "Сцены",
    "倒计时1": "Таймер",
    "倒计时剩余时间": "Остаток таймера",
    "彩光": "RGB-цвет",
    "音乐灯": "Музыкальный режим",
    "调节": "Управление",
    "勿扰模式": "Не беспокоить",
    "温标切换": "Единицы температуры",
    "温标切换设置": "Настройка единиц температуры",
    "温度上限设置": "Верхний порог температуры",
    "温度下限设置": "Нижний порог температуры",
    "湿度上限设置": "Верхний порог влажности",
    "湿度下限设置": "Нижний порог влажности",
    "温度报警": "Тревога температуры",
    "湿度报警": "Тревога влажности",
    "温度灵敏度": "Чувствительность температуры",
    "湿度灵敏度": "Чувствительность влажности",
    "温度周期上报": "Период отчёта температуры",
    "当前温度": "Текущая температура",
    "湿度数值": "Влажность",
    "电池电量": "Батарея",
    "电池电量百分比": "Батарея (%)",
    "电池电量状态": "Состояние батареи",
    "门磁状态": "Дверь",
    "人体感应状态": "Датчик движения",
    "水浸检测状态": "Датчик протечки",
    "工作模式": "Режим работы",
    "温度设置": "Уставка температуры",
    "目标温度_F": "Уставка (°F)",
    "设置温度上限": "Верхний порог",
    "设置温度上限_F": "Верхний порог (°F)",
    "当前温度_F": "Текущая темп. (°F)",
    "开窗检测": "Обнаружение окна",
    "防霜冻功能": "Защита от замерзания",
    "阀门检测": "Обнаружение клапана",
    "工作日设置": "Рабочие дни",
    "假日模式天数设置": "Дней в режиме отпуска",
    "恢复出厂设置": "Сброс к заводским",
    "童锁": "Блокировка от детей",
    "当前电压": "Текущее напряжение",
    "当前电流": "Текущий ток",
    "当前功率": "Текущая мощность",
    "电压校准系数": "Калибровка напряжения",
    "电流校准系数": "Калибровка тока",
    "功率校准系数": "Калибровка мощности",
    "电量校准系数": "Калибровка энергии",
    "增加电量": "Накопленная энергия",
    "指示灯状态设置": "Режим индикатора",
    "童锁开关": "Блокировка от детей",
    "正向总有功电量": "Общая прямая энергия",
    "剩余可用电量清零": "Сбросить остаток",
    "剩余可用电量显示": "Остаток энергии",
    "电量充值": "Пополнение энергии",
    "剩余电流显示": "Ток утечки",
    "剩余电流测试": "Тест тока утечки",
    "预付费功能开关": "Предоплата",
    "断路器开关": "Главный выключатель",
    "告警设置1": "Настройка тревоги 1",
    "告警设置2": "Настройка тревоги 2",
    "设备号显示": "ID устройства",
    "功率因素": "Коэффициент мощности",
    "供电频率": "Частота сети",
    "有功功率": "Активная мощность",
    "清电量": "Сброс энергии",
    "刷新上报": "Обновить",
    "A相电压，电流及功率": "Фаза A (U/I/P)",
    "Voltage": "Напряжение",
    "Current": "Ток",
};

// v1.25.0 (task #C): единый резолвер отображения DP.
// Возвращает:
//   display  — то, что показываем в колонке «Имя» (code, английское)
//   tooltip  — «все остальные варианты» для title
//   badge    — 📖 / 🈶 / ☁ / ''
// v1.27.3: единый резолвер для UI. Философия проекта:
//   - display — ТОЛЬКО английский code (или '?').
//   - никаких fallback на cn/cfg — они только в подсказке.
//   - tooltip — СТОЛБИКОМ (\n), рендерится через .dp-tip + data-tip.
//   - если в UI китайский заменён на EN — строка «⚠️ имя заменено на EN».
function resolveDpDisplay(code, cloudName, cfgName) {
  const cjk = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]/;
  const cd = (code || '').trim();
  const cn = (cloudName || '').trim();
  const cfg = (cfgName || '').trim();

  // display — только code. Никаких fallback на cn/cfg.
  const display = cd || '?';

  const lines = [];
  let badge = '';

  // 1. RU-перевод
  let ru = '';
  if (cd && DP_CODE_NAMES_RU_FRONT[cd]) {
    ru = DP_CODE_NAMES_RU_FRONT[cd];
    badge = '📖';
  } else if (cn && DP_CN_NAMES_RU_FRONT[cn]) {
    ru = DP_CN_NAMES_RU_FRONT[cn];
    badge = '🈶';
  } else if (cfg && DP_CN_NAMES_RU_FRONT[cfg]) {
    ru = DP_CN_NAMES_RU_FRONT[cfg];
    badge = '🈶';
  }
  if (ru) lines.push('RU: ' + ru);

  // 2. EN (code)
  if (cd) lines.push('EN: ' + cd);

  // v1.27.5: дедуп оригиналов — cn и cfg часто совпадают.
  const origs = [...new Set([cn, cfg].filter(x => x && cjk.test(x) && x !== cd))];
  for (const o of origs) {
    lines.push('Оригинал: ' + o);
    if (!badge) badge = '🈶';
  }

  // 4. Облачное имя (латиница ≠ code)
  if (cn && !cjk.test(cn) && cn !== cd) lines.push('Облачное имя: ' + cn);

  // 5. Config-имя (латиница ≠ code и ≠ cn)
  if (cfg && !cjk.test(cfg) && cfg !== cd && cfg !== cn) lines.push('Config: ' + cfg);

  // v1.27.5: строка «Источник:» убрана — значок сам показывает источник
  // (📖 — code-словарь, 🈶 — cn-словарь, ☁ — cloud/cache).

  // v1.28.11: ru не используется вызывающими — убран из return.
  return {
    display: display,
    tooltip: lines.join('\n'),
    badge: badge,
  };
}

function jsStr(s) {
  // P3 1.22.0 fix1: одинарные кавычки + экранирование \\ и '
  // Возвращает строку ВИДА 'name' (с кавычками), пригодную для
  // вставки в onclick="...(...)" без обёртки.
  try {
    var t = String(s ?? "");
    return "'" + t.replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
  } catch (e) { return "''"; }
}

// v1.27.9: транслитерация кириллицы → латиница (ГОСТ-подобная).
// Нужна для name устройства — HA/URL не любят не-ASCII.
// Целевое: "Датчик протечки Туалет" → "datchik_protechki_tualet".
const _RU_TO_LAT = (function() {
  const map = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e",
    "ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m",
    "н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u",
    "ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh","щ":"shch",
    "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    "А":"A","Б":"B","В":"V","Г":"G","Д":"D","Е":"E","Ё":"E",
    "Ж":"Zh","З":"Z","И":"I","Й":"Y","К":"K","Л":"L","М":"M",
    "Н":"N","О":"O","П":"P","Р":"R","С":"S","Т":"T","У":"U",
    "Ф":"F","Х":"H","Ц":"Ts","Ч":"Ch","Ш":"Sh","Щ":"Shch",
    "Ъ":"","Ы":"Y","Ь":"","Э":"E","Ю":"Yu","Я":"Ya",
  };
  const keys = Object.keys(map);
  const re = new RegExp("[" + keys.join("") + "]", "g");
  return { map, re };
})();

function _translitRu(s) {
  if (!s) return "";
  return String(s).replace(_RU_TO_LAT.re, ch => _RU_TO_LAT.map[ch] || ch);
}

// v1.27.9: name для устройства — транслит + snake_case.
// "Датчик протечки Туалет" → "datchik_protechki_tualet".
// Если транслит пуст — fallback на tuya_id.slice(-6).
function _makeDeviceName(friendly, tuyaId) {
  let t = _translitRu(String(friendly || ""));
  t = t.toLowerCase()
       .replace(/[^a-z0-9]+/g, "_")
       .replace(/^_+|_+$/g, "")
       .slice(0, 40);
  if (!t) {
    const id = String(tuyaId || "");
    t = "device_" + (id ? id.slice(-6) : Math.random().toString(36).slice(2,8));
  }
  return t;
}
// v1.28.41: уникальность имён при импорте.
function _uniquifyName(base, used) {
  if (!used.has(base)) return base;
  let i = 2;
  while (used.has(base + "_" + i)) i++;
  return base + "_" + i;
}
function _uniquifyFriendly(base, used) {
  if (!used.has(base)) return base;
  let i = 2;
  while (used.has(base + " (" + i + ")")) i++;
  return base + " (" + i + ")";
}
function copyCode(text, extraStyle) {
  const st = extraStyle ? ` style="${extraStyle}"` : "";
  return `<span class="copy-row"><code data-copy="${escapeAttr(text)}"${st}>${escapeHtml(text)}</code><span class="copy-hint" title="Клик — выделить">📋</span></span>`;
}
function copyCodePlain(text, extraStyle) {
  const st = extraStyle ? ` style="${extraStyle}"` : "";
  return `<code data-copy="${escapeAttr(text)}"${st}>${escapeHtml(text)}</code>`;
}

// v1.25.9: короткое (≤ SHORT_LIMIT символов) — как есть,
// без переноса. Длинное — 2 строки + …, полное в title.
// Работает для «Значения» / «Текущее» / «Значение».
// v1.25.12: title обрезаем до 500 символов — нативный tooltip
// всё равно не показывает длинные значения, а DOM не раздувается.
// data-copy оставляем полным — для копирования.
const _TRUNC_TITLE_LIMIT = 500;
function _truncCell(text, shortLimit) {
  const LIMIT = (typeof shortLimit === "number") ? shortLimit : 50;
  const raw = (text == null) ? "" : String(text);
  const titleSafe = raw.length > _TRUNC_TITLE_LIMIT ? raw.slice(0, _TRUNC_TITLE_LIMIT) : raw;
  if (raw.length <= LIMIT) {
    // v1.27.12: title= ставим ВСЕГДА — даже если значение короткое,
    // CSS может его обрезать (col узкая). Без title тултип не появится.
    return `<code class="nowrap-short" data-copy="${escapeAttr(raw)}" title="${escapeAttr(titleSafe)}">${escapeHtml(raw)}</code>`;
  }
  // длинное — 2 строки + ellipsis, полное — в data-copy, обрезанное — в title
  return `<code class="trunc-2" data-copy="${escapeAttr(raw)}" title="${escapeAttr(titleSafe)}">${escapeHtml(raw)}</code>`;
}

// v1.25.12: пустое значение — не <code>, а <span class="muted">—</span>.
// Семантически корректнее и не даёт «недорисованных полос» с фоном.
function _cellValue(text, shortLimit) {
  const raw = (text == null) ? "" : String(text);
  if (raw === "" || raw === "—" || raw === "null" || raw === "{}") {
    return '<span class="muted">—</span>';
  }
  return _truncCell(raw, shortLimit);
}

// v1.28.38: «Значения» Cloud — компактно (unit · min..max · шаг · range),
// полный JSON — в data-copy/title.
function _fmtValues(values) {
  if (!values || typeof values !== "object") return "";
  const p = [];
  if (values.unit) p.push(String(values.unit));
  if (values.min !== undefined && values.max !== undefined) {
    p.push(`${values.min}..${values.max}`);
  } else if (values.min !== undefined) {
    p.push(`≥${values.min}`);
  } else if (values.max !== undefined) {
    p.push(`≤${values.max}`);
  }
  if (values.step !== undefined && values.step !== null && Number(values.step) !== 1) {
    p.push(`шаг ${values.step}`);
  }
  if (Array.isArray(values.range) && values.range.length) {
    p.push("range: " + values.range.join(", "));
  }
  return p.join(" · ");
}

function _valuesCellHtml(values) {
  if (!values || typeof values !== "object" || Object.keys(values).length === 0) {
    return '<span class="muted">—</span>';
  }
  // v1.28.51: показываем первые 50 символов, полный JSON — в тултипе
  // и по клику (data-copy).
  const full = JSON.stringify(values);
  const compact = _fmtValues(values) || full;
  const shown = compact.length > 50 ? compact.slice(0, 50) + "…" : compact;
  return `<code class="nowrap-short" data-copy="${escapeAttr(full)}" title="${escapeAttr(full)}">${escapeHtml(shown)}</code>`;
}

// v1.28.51: короткое представление значения (первые N символов).
function _shortVal(v, n) {
  const s = displayValue(v);
  const lim = n || 50;
  return s.length > lim ? s.slice(0, lim) + "…" : s;
}

function _scaleVal(raw, scale) {
  const s = Number(scale);
  if (!isFinite(s) || s === 0) return null;
  const n = Number(raw);
  if (!isFinite(n)) return null;
  return +(n / Math.pow(10, s)).toFixed(6);
}

function latencyClass(ms) {
  if (ms === null || ms === undefined) return "lat-timeout";
  if (ms < 20) return "lat-good";
  if (ms < 100) return "lat-mid";
  return "lat-bad";
}
function latencyText(ms) { return (ms === null || ms === undefined) ? "timeout" : ms + " ms"; }
function fmtUptime(s) {
  s = parseInt(s) || 0;
  const d = Math.floor(s/86400); s %= 86400;
  const h = Math.floor(s/3600);  s %= 3600;
  const m = Math.floor(s/60);    s %= 60;
  if (d) return `${d}д ${h}ч ${m}м`;
  if (h) return `${h}ч ${m}м`;
  if (m) return `${m}м ${s}с`;
  return `${s}с`;
}
function fmtAgo(ts) {
  if (!ts) return "—";
  const d = Math.floor(Date.now()/1000) - ts;
  if (d < 0) return "только что";
  if (d < 60) return `${d}с назад`;
  if (d < 3600) return `${Math.floor(d/60)}м назад`;
  if (d < 86400) return `${Math.floor(d/3600)}ч назад`;
  return `${Math.floor(d/86400)}д назад`;
}

// v1.23.0: короткий формат для мобилы — "10с" вместо "10с назад"
function fmtAgoShort(ts) {
  if (!ts) return "—";
  const d = Math.floor(Date.now()/1000) - ts;
  if (d < 0) return "сейчас";
  if (d < 60) return `${d}с`;
  if (d < 3600) return `${Math.floor(d/60)}м`;
  if (d < 86400) return `${Math.floor(d/3600)}ч`;
  return `${Math.floor(d/86400)}д`;
}
function fmtDateTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('ru-RU') + " " + d.toLocaleTimeString('ru-RU');
}
function fmtTimeShort(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'});
}
function displayValue(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
// v1.22.4: резолвим name → {friendly, name} для показа в списках.
function resolveDeviceDisplayName(rawName) {
  if (!rawName) return { friendly: "?", name: "" };
  const d = (LAST_DEVICES || []).find(x => x.name === rawName);
  if (!d) return { friendly: rawName, name: "" };
  const f = d.friendly_name || d.name || rawName;
  const n = d.name || "";
  return { friendly: f, name: (n && n !== f) ? n : "" };
}

// v1.22.4: HTML для ячейки устройства: friendly + серое name.
function deviceNameCell(rawName, grayClass) {
  const r = resolveDeviceDisplayName(rawName);
  const gray = grayClass || "muted";
  if (!r.name) return escapeHtml(r.friendly);
  return escapeHtml(r.friendly)
    + ' <span class="' + gray + '" style="font-size:11px;">(' + escapeHtml(r.name) + ')</span>';
}

// v1.27.6: строгая валидация IPv4 (0-255, без ведущих нулей).
function _isValidIPv4(s) {
  if (!s || typeof s !== "string") return false;
  const t = s.trim();
  const parts = t.split(".");
  if (parts.length !== 4) return false;
  for (const p of parts) {
    if (!/^\d{1,3}$/.test(p)) return false;
    const n = parseInt(p, 10);
    if (n < 0 || n > 255) return false;
    if (p.length > 1 && p[0] === "0") return false;  // ведущий ноль
  }
  return true;
}

// v1.27.6: определяем префикс сети — по IP устройств в конфиге.
// Fallback: подсеть MQTT-брокера.
function _guessSubnetPrefix() {
  const counts = {};
  for (const d of LAST_DEVICES) {
    if (!d.ip) continue;
    const parts = String(d.ip).split(".");
    if (parts.length !== 4) continue;
    if (parts.some(p => !/^\d{1,3}$/.test(p))) continue;
    const prefix = parts.slice(0, 3).join(".");
    counts[prefix] = (counts[prefix] || 0) + 1;
  }
  let best = null, bestN = 0;
  for (const [p, n] of Object.entries(counts)) {
    if (n > bestN) { bestN = n; best = p; }
  }
  if (best) return best;
  // Fallback — подсеть MQTT-брокера.
  if (MQTT_BROKER && /^\d+\.\d+\.\d+\.\d+$/.test(MQTT_BROKER)) {
    return MQTT_BROKER.split(".").slice(0, 3).join(".");
  }
  return null;
}

// v1.27.6: проверка, что IP занят (кроме excludeIp — для edit).
function _ipInUse(ip, excludeIp) {
  if (!ip) return false;
  const t = ip.trim();
  if (excludeIp && t === excludeIp) return false;
  return LAST_DEVICES.some(d => d.ip === t);
}

// v1.28.16: смена языка preset-режимов. Сохраняет в localStorage,
// перерисовывает превью (карточки climate обновят preview).
function setPresetLang(lang) {
  if (lang !== "ru" && lang !== "as-is") return;
  PRESET_LANG = lang;
  try { localStorage.setItem(PRESET_LANG_KEY, lang); } catch (e) {}
  // Перерисовываем превью — карточки climate покажут новый preset_map.
  if (typeof renderImportPreview === "function"
      && document.getElementById("preview-overlay")?.classList.contains("open")) {
    renderImportPreview();
  }
}

// v1.28.16: извлечь options из DP с component="preset" у climate.
// Возвращает массив строк (пустой, если preset-DP нет или options пусты).
function _climatePresetOptions(d) {
  const dps_map = d && d.dps_map ? d.dps_map : {};
  for (const [dp, info] of Object.entries(dps_map)) {
    if (info && info.component === "preset"
        && Array.isArray(info.options) && info.options.length > 0) {
      return info.options.slice();
    }
  }
  return [];
}

// v1.28.16: HTML preview preset_map для карточки climate.
// Формат: построчно "auto → Автоматический режим".
function _renderPresetPreview(options, lang) {
  // v1.28.40: «плоский» компактный список чипами.
  if (!options || options.length === 0) return "";
  const LIMIT = 6;
  const shown = options.slice(0, LIMIT);
  const extra = options.length - shown.length;
  const items = [];
  for (const p of shown) {
    const display = (lang === "ru") ? (PRESET_TUYA_TO_RU[p] || p) : p;
    items.push(`<span class="preset-chip"><code>${escapeHtml(p)}</code>${escapeHtml(display)}</span>`);
  }
  if (extra > 0) items.push(`<span class="preset-chip muted">+${extra}</span>`);
  const head = (lang === "ru") ? "Preset:" : "Preset (cloud):";
  return `<div class="preset-preview"><span class="preset-preview-title">${head}</span>${items.join("")}</div>`;
}

// v1.28.16: определить, есть ли среди PREVIEW_DEVICES climate
// с preset-DP и непустыми options. Нужно, чтобы решить — показывать
// ли radio в шапке превью.
function _hasClimateWithPresets() {
  if (!Array.isArray(PREVIEW_DEVICES)) return false;
  for (const item of PREVIEW_DEVICES) {
    const d = item.device;
    if (!d || d.type !== "climate") continue;
    if (_climatePresetOptions(d).length > 0) return true;
  }
  return false;
}

// v1.28.16: HTML radio для шапки превью.
function _renderPresetLangRadio() {
  if (!_hasClimateWithPresets()) return "";
  const ruChecked = (PRESET_LANG === "ru") ? "checked" : "";
  const asisChecked = (PRESET_LANG === "as-is") ? "checked" : "";
  return `<div class="preset-lang-bar">`
       + `<span class="preset-lang-label">Preset-режимы:</span>`
       + `<label class="preset-lang-opt">`
       +   `<input type="radio" name="preset-lang" value="ru" ${ruChecked}`
       +   ` onchange="setPresetLang('ru')"> 🇷🇺 Русский</label>`
       + `<label class="preset-lang-opt">`
       +   `<input type="radio" name="preset-lang" value="as-is" ${asisChecked}`
       +   ` onchange="setPresetLang('as-is')"> ⚙️ Технические</label>`
       + `</div>`;
}

// v1.27.6: Cloud-устройство уже есть в конфиге? Сопоставление по tuya_id.
function _isCloudDeviceInConfig(cloudDev) {
  // v1.28.30: требуем непустой id с обеих сторон. Раньше при
  // пустом tuya_id в конфиге любое cloudDev с id=undefined давало
  // ложное «уже в конфиге» (undefined === undefined).
  if (!cloudDev || !cloudDev.id) return false;
  return LAST_DEVICES.some(ld => !!ld.tuya_id && ld.tuya_id === cloudDev.id);
}

// v1.28.39: запись конфига для Cloud-устройства (по tuya_id) — чтобы
// предзаполнять известный IP при overwrite.
function _knownConfigDeviceForCloud(cloudDev) {
  if (!cloudDev || !cloudDev.id) return null;
  return LAST_DEVICES.find(ld => !!ld.tuya_id && ld.tuya_id === cloudDev.id) || null;
}

function latencyPeriodLabel(sec) {
  if (sec === 1800) return "30 мин";
  if (sec === 3600) return "1ч";
  if (sec === 21600) return "6ч";
  if (sec === 86400) return "Сутки";
  if (sec === 0) return "Всё";
  return `${Math.round(sec/60)} мин`;
}

function selectNodeContents(el) {
  if (!el) return false;
  try {
    if (window.getSelection) {
      const sel = window.getSelection();
      sel.removeAllRanges();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.addRange(range);
      return true;
    }
  } catch (e) { console.warn("selection err", e); }
  return false;
}
function tryExecCopy(txt) {
  try {
    const ta = document.createElement("textarea");
    ta.value = txt;
    ta.style.cssText = "position:fixed;top:0;left:0;width:2em;height:2em;opacity:0;border:none;padding:0;";
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try { ta.setSelectionRange(0, txt.length); } catch(e){}
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) { return false; }
}
function handleCopyAttempt(targetEl) {
  const txt = targetEl?.dataset?.copy || targetEl?.textContent || "";
  if (!txt) return;
  const ok = tryExecCopy(txt);
  selectNodeContents(targetEl);
  const hint = targetEl.parentElement?.querySelector(".copy-hint") ||
               targetEl.closest(".copy-row")?.querySelector(".copy-hint");
  if (hint) {
    const old = hint.textContent;
    hint.textContent = ok ? "✓" : "🔵";
    hint.classList.toggle("copied", !!ok);
    setTimeout(() => { hint.textContent = old; hint.classList.remove("copied"); }, 1200);
  }
  targetEl.classList.add("sel-ok");
  setTimeout(() => targetEl.classList.remove("sel-ok"), 600);
  if (ok) showCopiedToast(txt);
}
function showCopiedToast(txt) {
  const t = document.createElement("div");
  t.textContent = "✓ Скопировано: " + (txt.length > 30 ? txt.slice(0,30) + "…" : txt);
  t.style.cssText = "position:fixed;bottom:20px;right:20px;background:var(--green);color:#fff;padding:8px 14px;border-radius:6px;font-size:12px;z-index:3000;box-shadow:0 4px 12px rgba(0,0,0,0.2);max-width:80vw;word-break:break-all;";
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 1500);
}
document.addEventListener("mousedown", (e) => {
  const hint = e.target.closest(".copy-hint");
  const code = e.target.closest("code[data-copy]");
  const target = code || hint;
  if (!target) return;
  let realTarget = code;
  if (!realTarget && hint) {
    realTarget = hint.parentElement?.querySelector("code[data-copy]");
  }
  if (!realTarget) return;
  e.preventDefault();
  e.stopPropagation();
  handleCopyAttempt(realTarget);
}, true);

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".time-btn-group button");
  if (!btn) return;
  if (btn.closest("#latency-period-group")) return;  // обрабатывается через setLatencyPeriod
  e.preventDefault();
  e.stopPropagation();
  const r = parseInt(btn.dataset.range, 10);
  if (!isNaN(r)) setLogRange(r);
});

function sortDevices(key) {
  if (SORT_KEY === key) SORT_DIR = -SORT_DIR;
  else { SORT_KEY = key; SORT_DIR = 1; }
  _saveSort("devices", SORT_KEY, SORT_DIR);
  renderDeviceTable(LAST_DEVICES);
  updateSortIndicators();
}
function updateSortIndicators() {
  document.querySelectorAll("th[data-sort]").forEach(th => {
    const ind = th.querySelector(".sort-ind");
    if (!ind) return;
    if (th.dataset.sort === SORT_KEY) ind.textContent = SORT_DIR > 0 ? "▲" : "▼";
    else ind.textContent = "";
  });
}

// v1.27.9: сортировка disabled-секции независима от основной.
function sortDisabled(key) {
  if (SORT_KEY_DISABLED === key) SORT_DIR_DISABLED = -SORT_DIR_DISABLED;
  else { SORT_KEY_DISABLED = key; SORT_DIR_DISABLED = 1; }
  _saveSort("disabled", SORT_KEY_DISABLED, SORT_DIR_DISABLED);
  // Перерисовываем только disabled-секцию.
  const _dis = LAST_DEVICES.filter(d => d.enabled === false);
  _renderDisabledSection(_dis);
  updateDisabledSortIndicators();
}

function updateDisabledSortIndicators() {
  // v1.27.9: индикаторы — только для заголовков внутри #disabled-block.
  const block = document.getElementById("disabled-block");
  if (!block) return;
  block.querySelectorAll("th[data-sort]").forEach(th => {
    const ind = th.querySelector(".sort-ind");
    if (!ind) return;
    if (th.dataset.sort === SORT_KEY_DISABLED) ind.textContent = SORT_DIR_DISABLED > 0 ? "▲" : "▼";
    else ind.textContent = "";
  });
}

// ==================== ПОИСК (v1.20) ====================
let DEVICE_SEARCH = "";
let CLOUD_SEARCH = "";
// v1.28.42: фильтр/сортировка таблицы Cloud.
let CLOUD_FILTER = "all";                       // all | new | added
let CLOUD_SORT = { key: "name", dir: 1 };
let _deviceSearchTimer = null;
let _cloudSearchTimer = null;

function onDeviceSearch(v) {
  DEVICE_SEARCH = (v || "").trim();
  // v1.22.1: сбрасываем сортировку при поиске — предсказуемо.
  if (DEVICE_SEARCH) { SORT_KEY = "name"; SORT_DIR = 1; }
  if (_deviceSearchTimer) clearTimeout(_deviceSearchTimer);
  _deviceSearchTimer = setTimeout(() => { renderDeviceTable(LAST_DEVICES); }, 150);
}
function clearDeviceSearch() {
  DEVICE_SEARCH = "";
  const el = document.getElementById("device-search");
  if (el) el.value = "";
  renderDeviceTable(LAST_DEVICES);
}
function deviceSearchPass(d) {
  if (!DEVICE_SEARCH) return true;
  const q = DEVICE_SEARCH.toLowerCase();
  return ((d.friendly_name || "").toLowerCase().includes(q)
    || (d.name || "").toLowerCase().includes(q)
    || (d.ip || "").toLowerCase().includes(q)
    || (d.type || "").toLowerCase().includes(q));
}

function onCloudSearch(v) {
  CLOUD_SEARCH = (v || "").trim();
  if (_cloudSearchTimer) clearTimeout(_cloudSearchTimer);
  _cloudSearchTimer = setTimeout(() => { renderCloudDevices(); }, 150);
}
function clearCloudSearch() {
  CLOUD_SEARCH = "";
  const el = document.getElementById("cloud-search");
  if (el) el.value = "";
  renderCloudDevices();
}
function cloudSearchPass(d) {
  if (!CLOUD_SEARCH) return true;
  const q = CLOUD_SEARCH.toLowerCase();
  return ((d.name || "").toLowerCase().includes(q)
    || (d.id || "").toLowerCase().includes(q)
    || (d.product_name || "").toLowerCase().includes(q)
    || (d.type_guess || "").toLowerCase().includes(q));
}

// v1.28.42: фильтр «новые / добавленные / все» и сортировка по столбцам.
function setCloudFilter(f) {
  CLOUD_FILTER = f;
  for (const id of ["cf-all", "cf-new", "cf-added"]) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("active", id === "cf-" + f);
  }
  renderCloudDevices();
}
function _cloudFilterPass(d) {
  const inCfg = _isCloudDeviceInConfig(d);
  if (CLOUD_FILTER === "new") return !inCfg;
  if (CLOUD_FILTER === "added") return inCfg;
  return true;
}
function _cloudSortVal(d, key) {
  switch (key) {
    case "type": return String(d.type_guess || "");
    case "product": return String(d.product_name || "").toLowerCase();
    case "dp": return (d.mapping ? Object.keys(d.mapping).length : 0);
    case "online": return d.online ? 1 : 0;
    case "name":
    default: return String(d.name || "").toLowerCase();
  }
}
function sortCloudDevices(key) {
  if (CLOUD_SORT.key === key) CLOUD_SORT.dir = -CLOUD_SORT.dir;
  else { CLOUD_SORT.key = key; CLOUD_SORT.dir = 1; }
  renderCloudDevices();
}
function _cloudSortInd(key) {
  if (CLOUD_SORT.key !== key) return "";
  return CLOUD_SORT.dir === 1 ? " ▲" : " ▼";
}

// ==================== SKELETON (v1.20) ====================
function renderSkeleton(tbody, cols, rows) {
  if (!tbody) return;
  rows = rows || 5;
  let html = "";
  for (let i = 0; i < rows; i++) {
    html += "<tr>";
    for (let j = 0; j < cols; j++) {
      const w = j === 0 ? "w60" : (j === cols - 1 ? "w40" : "w80");
      html += `<td><span class="skeleton ${w}"></span></td>`;
    }
    html += "</tr>";
  }
  tbody.innerHTML = html;
}

// ==================== ЦВЕТНЫЕ ПЛАШКИ (v1.20) ====================
function versionBadge(v) {
  if (!v) return "";
  const s = String(v).trim();
  const key = "v" + s.replace(".", "");
  const cls = ["v31","v32","v33","v34","v35"].includes(key) ? key : "v33";
  return `<span class="version-badge ${cls}">v${escapeHtml(s)}</span>`;
}
function typeBadge(t) {
  if (!t) return `<span class="type-badge">?</span>`;
  const cls = ["light","switch","climate","sensor","binary_sensor"].includes(t) ? t : "";
  return `<span class="type-badge ${cls}">${escapeHtml(t)}</span>`;
}
// v1.28.57: бейдж источника DP с иконкой и тултипом (cloud/tuya-local/similar/cache).
function _dpsSourceBadge(src, info) {
  // v1.28.69: бейдж показывает ТОЛЬКО источник сопоставления.
  // Наличие живого значения (бывший суффикс «+cache») — только в тултипе.
  const raw = String(src || "");
  const hasVal = raw.endsWith("+cache") || raw === "cache";
  const base = raw.replace("+cache", "");
  const M = {
    cloud:      { icon: "☁",  label: "Tuya Cloud",         what: "из Tuya Cloud (functions/status)" },
    tuya_local: { icon: "📚", label: "tuya-local",         what: "из локальной базы tuya-local" },
    similar:    { icon: "🔗", label: "similar",            what: "с таким же устройством (тот же product_id)" },
    removed:    { icon: "⚙️", label: "из конфига",         what: "DP был в конфиге, сейчас убран (staging)" },
    cache:      { icon: "📦", label: "нет сопоставления",  what: "" },
  };
  const m = M[base] || { icon: "❓", label: base || "—", what: "" };
  const lines = [base === "cache"
    ? "Сопоставление: нет — есть только значение из кэша bridge"
    : "Сопоставление: " + (m.what || m.label)];
  const reason = (info && info._dps_source_reason) || "";
  if (reason && base === "cloud") lines.push(reason);
  if (hasVal) lines.push("Значение: есть в кэше bridge (cache_snapshot)");
  return `<span class="dp-tip dps-source-badge ${base}"`
       + ` data-tip="${escapeAttr(lines.join("\n"))}">${m.icon} ${escapeHtml(m.label)}</span>`;
}

// v1.28.49: индикатор питания (🔋 батарейное / 🔌 проводное) для колонки «Тип».
// v1.28.55: без подложки — просто значок.
function _powerBadge(d) {
  if (!d) return "";
  return (d.battery_powered === true)
    ? ' <span class="power-glyph" title="Батарейное устройство">🔋</span>'
    : ' <span class="power-glyph" title="Проводное устройство">🔌</span>';
}
// v1.25.0 (fix3): цветной бейдж для component DP.
function componentBadge(c) {
  if (!c || c === "—") return `<span class="component-badge">—</span>`;
  const allowed = ["switch","sensor","binary_sensor","select","number",
                   "preset","light","climate","button","time","lock","phase_a"];
  const cls = allowed.includes(c) ? c : "";
  return `<span class="component-badge ${cls}">${escapeHtml(c)}</span>`;
}
// v1.25.0 (fix3): статус кнопок баз DP (loading/ok/err/idle).
function setButtonState(btn, state, text) {
  if (!btn) return;
  btn.classList.remove("btn-success", "btn-error");
  if (state === "loading") {
    btn.innerHTML = '<span class="spin"></span> ' + (text || "Загрузка…");
  } else if (state === "ok") {
    btn.classList.add("btn-success");
    btn.innerHTML = "✅ " + (text || "Готово");
  } else if (state === "err") {
    btn.classList.add("btn-error");
    btn.innerHTML = "❌ " + (text || "Ошибка");
  } else {
    btn.innerHTML = text || "—";
  }
}
function restoreButtonAfter(btn, delayMs, idleHtml) {
  setTimeout(() => setButtonState(btn, "idle", idleHtml), delayMs || 2500);
}

let _firstStatusLoad = true;
async function fetchStatus() {
  if (_firstStatusLoad && VIEW === "dashboard") {
    renderSkeleton(document.getElementById("devices-body"), 5, 5);
  }
  try {
    const r = await fetch("/api/status");
    const data = await r.json();
    // v1.21.2.fix1: старые ID удалены из HTML (health-widget вместо них).
    // Их роль выполняет refreshHealthWidget() через /api/health/full.
    // v1.28.27: bridge ping_mode и started_at (для CAP_NET_RAW warning).
    BRIDGE_STATE.bridge_ping_mode = data.ping_mode || null;
    if (data.bridge_started_at !== undefined) {
      BRIDGE_STATE.bridge_started_at = data.bridge_started_at || 0;
    }
    const devs = data.devices || [];
    for (const d of devs) if (DEVICE_HISTORY_CACHE[d.name]) d.history = DEVICE_HISTORY_CACHE[d.name];
    LAST_DEVICES = devs;
    // v1.22.0: мёртвая переменная online удалена.
    if (VIEW === "dashboard") {
      renderProblems(computeProblems(devs));
      renderDeviceTable(devs);
    }
    // v1.28.42: подсветка Cloud-таблицы зависит от LAST_DEVICES —
    // перерисовываем, иначе после обновления статуса подсветка «терялась».
    if (VIEW === "import" && CLOUD_DEVICES.length) renderCloudDevices();
    // v1.28.78: сканер LAN хранит результаты в sessionStorage, но классификация
    // «свой/чужой» зависит от LAST_DEVICES — перерисовываем, когда они пришли.
    if (SCAN_RESULTS.length) renderScanResults(SCAN_SUBNET);
    // v1.21.2: обновляем ТОЛЬКО volatile-зону модалки (если открыта).
    // Sensitive-зона (quiet-редактор, local key) не трогается.
    // v1.28.25: ищем по имени, а не по индексу — индекс мог сдвинуться
    // при удалении устройства из другого таба (LAST_DEVICES сжался).
    if (CURRENT_MODAL_NAME) {
      const _idx = LAST_DEVICES.findIndex(x => x.name === CURRENT_MODAL_NAME);
      if (_idx < 0) {
        // Устройство исчезло — закрыть модалку, чтобы не показывать чужое.
        closeModal();
        return;
      }
      CURRENT_MODAL_IDX = _idx;
      const md = LAST_DEVICES[_idx];
      if (md && document.getElementById("modal-overlay").classList.contains("open")) {
        // v1.23.0: volatile обновляем ВСЕГДА (точечно по зонам — выделение
        // текста сохраняется). Sensitive (quiet-редактор) — только если
        // нет несохранённых правок.
        renderModalVolatile(md);
        const quietDirty = typeof QUIET_EDIT !== "undefined" && QUIET_EDIT._dirty;
        if (!quietDirty) {
          renderModalSensitive(md);
        }
      }
    }
  } catch (e) { console.error(e); }
  finally { _firstStatusLoad = false; }  // v1.21.4: не залипаем на skeleton
}

// v1.28.27: проверка «bridge без CAP_NET_RAW, но есть батарейные».
// Возвращает объект проблемы или null. НЕ влияет на основной список
// проблемных — рендерится отдельной строкой в renderProblems().
function computePingProblem() {
  const pm = BRIDGE_STATE.bridge_ping_mode;
  if (!pm || pm.mode !== "none") return null;
  // v1.28.28: игнорируем enabled:false батарейные — bridge
  // их не опрашивает независимо от CAP_NET_RAW.
  const batteryDevs = (LAST_DEVICES || []).filter(
    d => d.battery_powered === true && d.enabled !== false
  );
  if (batteryDevs.length === 0) return null;
  return {
    kind: "ping_unavailable",
    battery_count: batteryDevs.length,
    devices: batteryDevs.map(d => d.friendly_name || d.name),
  };
}

function computeProblems(devs) {
  const now = Math.floor(Date.now()/1000);
  // v1.28.23: grace period после рестарта bridge. Сразу после старта
  // last_seen/status в state_cache старые (bridge ещё не опросил
  // устройства) — иначе UI показывает 28 «проблемных» с
  // «последняя активность 5м назад».
  const _bs = (typeof STATE !== "undefined" && BRIDGE_STATE.bridge_started_at) || 0;
  const _bridge_grace = (_bs > 0 && (now - _bs) < BRIDGE_STARTUP_GRACE_SEC);
  const out = [];
  for (const d of devs) {
    // v1.27.7: отключённые устройства не попадают в «Проблемные».
    if (d.enabled === false) continue;
    // v1.28.1: батарейные — не считаем проблемы. Они спят по
    // определению, `offline` для них — нормальное состояние.
    if (d.battery_powered === true) continue;
    // v1.28.23: в grace-периоде не считаем проблемы — bridge
    // только что стартовал, устройства ещё не опрошены.
    if (_bridge_grace) continue;
    // v1.21.0: quiet hours
    if (d.quiet) continue;
    if (d.quiet_until && d.quiet_until > now) continue;
    const reasons = [];
    if (d.status === "offline") {
      const hist = d.history || [];
      let since = null;
      for (let i = hist.length - 1; i >= 0; i--) {
        if (hist[i].status === "offline" && typeof hist[i].ts === "number") since = hist[i].ts;
        else break;
      }
      const forSec = since ? (now - since) : null;
      if (forSec === null || forSec > 5*60) reasons.push(forSec ? `offline ${fmtUptime(forSec)}` : "offline");
    }
    if (d.status === "online" && d.last_seen && (now - d.last_seen) > 120) {
      reasons.push(`последняя активность ${fmtAgo(d.last_seen)}`);
    }
    if (reasons.length > 0) out.push({dev: d, reasons});
  }
  return out;
}

// v1.22.6: пропускаем перерисовку, если набор проблем не изменился.
// v1.23.0: sig включает округлённое время (без секунд). Иначе
// "offline 5м 30с" → "offline 5м 35с" не менял sig, DOM застревал
// на старом значении. Теперь "5м" → "6м" триггерит перерисовку,
// а "5м 30с" → "5м 35с" — нет.
let _lastProblemsSig = "";

function _problemsSig(problems) {
  return problems.map(p => {
    const reasonsKey = p.reasons.map(r => {
      // v1.25.14: два уточнённых regex.
      //   1. "5м 30с" → "5м" (было)
      //   2. "Nс" → "0с" — покрывает и "offline 30с",
      //      и "последняя активность 5с назад".
      //      Раньше второй regex ловил только с префиксом
      //      "последняя активность ", а offline Nс (без м)
      //      продолжал дрожать. Плюс "$10" был хрупким
      //      (мог трактоваться как $10 = группа 10).
      return r
        .replace(/(\d+м)\s+\d+с/g, "$1")
        .replace(/(\d+)с(?!\d)/g, "0с");
    }).join("·");
    return p.dev.name + "|" + reasonsKey;
  }).join(";");
}

function renderProblems(problems) {
  const block = document.getElementById("problems-block");
  const list = document.getElementById("problems-list");
  const count = document.getElementById("problems-count");

  // v1.28.27: агрегирующая проблема CAP_NET_RAW — отдельной строкой.
  const pingProblem = computePingProblem();
  const totalCount = problems.length + (pingProblem ? 1 : 0);

  if (totalCount === 0) {
    if (block.style.display !== "none") block.style.display = "none";
    _lastProblemsSig = "";
    return;
  }

  // сигнатура для skip-перерисовки (учитываем и ping-проблему)
  const sig = _problemsSig(problems)
    + (pingProblem ? "|ping:" + pingProblem.devices.join(",") : "");
  if (sig === _lastProblemsSig) return;
  _lastProblemsSig = sig;

  block.style.display = "block";
  // v1.28.28: разделяем «проблемные устройства» и «bridge».
  // v1.28.29: не показываем «(0 + 1 bridge)» — только «(1 bridge)».
  if (pingProblem) {
    count.textContent = problems.length > 0
      ? `(${problems.length} + 1 bridge)`
      : `(1 bridge)`;
  } else {
    count.textContent = `(${problems.length})`;
  }

  let html = problems.map(p => {
    const idx = LAST_DEVICES.indexOf(p.dev);
    return `<div class="problem-item" onclick="showDevice(${idx})">
      <span>${escapeHtml(p.dev.friendly_name || p.dev.name)}</span>
      <span class="muted" style="font-size:12px;">${p.reasons.map(escapeHtml).join(" · ")}</span>
    </div>`;
  }).join("");

  if (pingProblem) {
    html += `<div class="problem-item problem-ping">
      <div style="font-weight:500;">⚠️ Bridge без CAP_NET_RAW — батарейные устройства не опрашиваются</div>
      <div class="muted" style="font-size:12px; margin-top:2px;">
        Затронуто: ${pingProblem.devices.map(escapeHtml).join(", ")}
      </div>
      <div class="muted" style="font-size:12px; margin-top:4px;">
        → Добавьте <code>cap_add: [NET_RAW]</code> в docker-compose.yml и перезапустите bridge
      </div>
    </div>`;
  }

  list.innerHTML = html;
}

function renderDeviceTable(devs) {
  const tbody = document.getElementById("devices-body");
  const filtered = DEVICE_SEARCH ? devs.filter(deviceSearchPass) : devs;
  const infoEl = document.getElementById("device-search-info");
  if (infoEl) {
    infoEl.textContent = DEVICE_SEARCH
      ? `найдено ${filtered.length} из ${devs.length}`
      : (devs.length ? `${devs.length} устройств` : "");
  }
  // v1.27.7: разделяем на enabled и disabled.
  const _enabled = filtered.filter(d => d.enabled !== false);
  const _disabled = filtered.filter(d => d.enabled === false);
  _renderDisabledSection(_disabled);
  // Дальше основная таблица — только enabled.
  const filteredEnabled = _enabled;
  if (filteredEnabled.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">${DEVICE_SEARCH ? "Ничего не найдено" : "Нет активных устройств"}</td></tr>`;
    return;
  }
  const sorted = [...filteredEnabled].sort((a, b) => {
    let av, bv;
    switch (SORT_KEY) {
      case "name": av = (a.friendly_name || a.name || "").toLowerCase(); bv = (b.friendly_name || b.name || "").toLowerCase(); break;
      case "type": av = (a.type || "").toLowerCase(); bv = (b.type || "").toLowerCase(); break;
      case "status": av = a.status || ""; bv = b.status || ""; break;
      case "latency": av = (a.latency_ms === null || a.latency_ms === undefined) ? 99999 : a.latency_ms;
                     bv = (b.latency_ms === null || b.latency_ms === undefined) ? 99999 : b.latency_ms; break;
      case "last_seen": av = a.last_seen || 0; bv = b.last_seen || 0; break;
    }
    if (typeof av === "string") return av.localeCompare(bv) * SORT_DIR;
    return (av - bv) * SORT_DIR;
  });
  tbody.innerHTML = sorted.map((d) => {
    const realIdx = LAST_DEVICES.indexOf(d);
    const on = d.status === "online";
    const ip = d.ip ? `<div class="device-ip">${escapeHtml(d.ip)}</div>` : "";
    // v1.28.2: для battery/disabled/quiet — иконка вместо timeout
    let lat;
    if (d.latency_hidden) {
      let _icon = '—';
      if (d.latency_reason === 'battery')  _icon = '🔋';
      else if (d.latency_reason === 'disabled') _icon = '⛔';
      else if (d.latency_reason === 'quiet')    _icon = '🔇';
      const _tip = d.latency_reason === 'battery'  ? 'Батарейное — задержка не измеряется'
                 : d.latency_reason === 'disabled' ? 'Отключено — задержка не измеряется'
                 : 'Режим тишины — задержка не измеряется';
      lat = `<span class="muted" title="${_tip}">${_icon}</span>`;
    } else {
      lat = `<span class="latency ${latencyClass(d.latency_ms)}">${latencyText(d.latency_ms)}</span>`;
    }
    // v1.28.49: 🔋 перенесён в колонку «Тип»; в имени — только тишина.
    let quietBadge = '';
    if (d.battery_powered !== true) {
      const quietNow = d.quiet || (d.quiet_until && d.quiet_until > Math.floor(Date.now()/1000));
      if (quietNow) quietBadge = ' <span class="badge quiet" title="Режим тишины">🔇</span>';
    }
    // v1.28.74: время — в одну строку после online/offline, без «назад».
    const agoShort = d.last_seen ? fmtAgoShort(d.last_seen) : '—';
    let statusCell;
    if (isMobile()) {
      statusCell = `<span class="dot ${on ? 'online' : 'offline'}"></span>` +
                   `<span class="muted" style="font-size:12px;">${escapeHtml(agoShort)}</span>`;
    } else {
      statusCell = `<span class="dot ${on ? 'online' : 'offline'}"></span>
          <span style="color:${on ? 'var(--green)' : 'var(--red)'}">${on ? 'online' : 'offline'}</span>
          <span class="muted" style="font-size:11px; margin-left:6px;">${escapeHtml(agoShort)}</span>`;
    }
    return `<tr class="device-row" onclick="showDevice(${realIdx})">
      <td><div style="font-weight:500;">${escapeHtml(d.friendly_name || d.name)}${quietBadge}</div>${ip}</td>
      <td>${typeBadge(d.type)}${_powerBadge(d)}</td>
      <td>${statusCell}</td>
      <td>${lat}</td>
    </tr>`;
  }).join("");
  updateSortIndicators();
}

// v1.27.7: свёрнутая секция «Отключённые» в дашборде.
function _renderDisabledSection(disabled) {
  const block = document.getElementById("disabled-block");
  const body = document.getElementById("disabled-body");
  const countEl = document.getElementById("disabled-count");
  if (!block || !body) return;
  if (disabled.length === 0) {
    block.style.display = "none";
    return;
  }
  block.style.display = "block";
  if (countEl) countEl.textContent = disabled.length;
  // v1.27.9: независимая сортировка — SORT_KEY_DISABLED/SORT_DIR_DISABLED.
  // Ключи только name/type (для disabled status/latency бессмысленны).
  const _sortKey = (SORT_KEY_DISABLED === "name" || SORT_KEY_DISABLED === "type")
                   ? SORT_KEY_DISABLED : "name";
  const _sortDir = SORT_DIR_DISABLED || 1;
  const sorted = [...disabled].sort((a, b) => {
    let av, bv;
    if (_sortKey === "type") {
      av = (a.type || "").toLowerCase();
      bv = (b.type || "").toLowerCase();
    } else {
      av = (a.friendly_name || a.name || "").toLowerCase();
      bv = (b.friendly_name || b.name || "").toLowerCase();
    }
    return av.localeCompare(bv) * _sortDir;
  });
  body.innerHTML = sorted.map(d => {
    const realIdx = LAST_DEVICES.indexOf(d);
    const ip = d.ip ? `<div class="device-ip">${escapeHtml(d.ip)}</div>` : "";
    return `<tr class="device-row device-row-disabled" onclick="showDevice(${realIdx})">
      <td><div style="font-weight:500;">${escapeHtml(d.friendly_name || d.name)} <span class="badge enabled-off" style="font-size:10px;">⛔</span></div>${ip}</td>
      <td>${typeBadge(d.type)}${_powerBadge(d)}</td>
    </tr>`;
  }).join("");
  // v1.27.9: обновить индикаторы сортировки disabled.
  updateDisabledSortIndicators();
}

function sparklineSvgWithLabels(history, width, height) {
  if (!history || history.length === 0) return "";
  const now = Math.floor(Date.now()/1000);
  const startTs = history[0].ts;
  const total = (now - startTs) || 1;
  let segments = []; let cur = null, curStart = startTs;
  for (const h of history) {
    if (cur === null) { cur = h.status; curStart = h.ts; }
    else if (h.status !== cur) { segments.push({start: curStart, end: h.ts, status: cur}); cur = h.status; curStart = h.ts; }
  }
  if (cur !== null) segments.push({start: curStart, end: now, status: cur});
  let rects = "";
  const svgId = "spark-" + Math.random().toString(36).slice(2, 10);
  const hitData = [];
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    const x1 = ((seg.start - startTs) / total) * width;
    const x2 = ((seg.end - startTs) / total) * width;
    const w = Math.max(1, x2 - x1);
    const color = seg.status === "online" ? "var(--green)" : "var(--red)";
    hitData.push({
      idx: i, x1: x1, x2: x2, w: w,
      start: seg.start, end: seg.end, status: seg.status,
    });
    rects += `<rect class="seg-hover hoverable" data-hit="${i}" x="${x1.toFixed(1)}" y="0" width="${w.toFixed(1)}" height="${height}" fill="${color}" opacity="0.7"/>`;
  }
  const svg = `<svg id="${svgId}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" data-hits='${JSON.stringify(hitData).replace(/'/g, "&#39;")}'>${rects}</svg>`;
  const labels = `<div class="sparkline-labels"><span>${fmtDateTime(startTs)}</span><span>${fmtDateTime(now)}</span></div>`;

  setTimeout(() => {
    const el = document.getElementById(svgId);
    if (!el) return;
    const hits = JSON.parse(el.dataset.hits || "[]");
    _chartTooltip.bind(el, {
      hitTest: (svgEl, ev) => {
        const { x } = svgClientToLocal(svgEl, ev.clientX, ev.clientY);
        for (const h of hits) {
          if (x >= h.x1 && x <= h.x2) return { data: h };
        }
        return null;
      },
      onHover: (svgEl, hit) => {
        svgEl.querySelectorAll("rect.seg-hover").forEach(r => {
          if (parseInt(r.dataset.hit) === hit.data.idx) r.classList.add("hover");
          else r.classList.remove("hover");
        });
      },
      onLeave: (svgEl) => {
        svgEl.querySelectorAll("rect.seg-hover").forEach(r => r.classList.remove("hover"));
      },
      render: (hit) => {
        const h = hit.data;
        const d1 = new Date(h.start * 1000);
        const d2 = new Date(h.end * 1000);
        const fmt = (d) => {
          const dd = String(d.getDate()).padStart(2, "0");
          const mm = String(d.getMonth() + 1).padStart(2, "0");
          const hh = String(d.getHours()).padStart(2, "0");
          const mi = String(d.getMinutes()).padStart(2, "0");
          return `${dd}.${mm} ${hh}:${mi}`;
        };
        const dur = h.end - h.start;
        const hh = Math.floor(dur / 3600);
        const mi = Math.floor((dur % 3600) / 60);
        let durStr = "";
        if (hh > 0) durStr = `${hh}ч ${mi}м`;
        else if (mi > 0) durStr = `${mi}м`;
        else durStr = `${dur}с`;
        const badgeCls = h.status === "online" ? "online" : "offline";
        return `<div class="tt-head">${fmt(d1)} — ${fmt(d2)} (${durStr})</div>
          <div class="tt-row"><span class="tt-name">статус</span>
            <span class="tt-badge ${badgeCls}">${h.status}</span></div>`;
      },
    });
  }, 0);

  return svg + labels;
}

function latencySparklineSvg(points, width, height) {
  if (!points || points.length === 0) return "";
  const valid = points.filter(p => p.ms !== null && p.ms !== undefined);
  if (points.length === 1) {
    const p = points[0];
    if (p.ms === null || p.ms === undefined) {
      return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
        <text x="${width/2}" y="${height/2+4}" text-anchor="middle" fill="currentColor" font-size="11" opacity="0.6">timeout</text></svg>`;
    }
    const color = p.ms < 20 ? "var(--green)" : (p.ms < 100 ? "var(--yellow)" : "var(--red)");
    return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" style="color:var(--muted);">
      <circle cx="${width/2}" cy="${height/2}" r="5" fill="${color}"/>
      <text x="${width/2}" y="${height/2-12}" text-anchor="middle" fill="currentColor" font-size="10">1 замер</text>
      <text x="${width/2}" y="${height/2+20}" text-anchor="middle" fill="currentColor" font-size="11">${p.ms} мс</text></svg>`;
  }
  if (valid.length === 0) {
    return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <text x="${width/2}" y="${height/2+4}" text-anchor="middle" fill="currentColor" font-size="11" opacity="0.6">все замеры timeout</text></svg>`;
  }
  // v1.24.11: ось X привязана к реальному 24-часовому окну
  // [now-24ч, now], а не к первой/последней точке. Иначе
  // после quiet-разрывов линия уезжает в сторону, а правый
  // край пустует.
  const now = Math.floor(Date.now() / 1000);
  const tsEnd = now;
  const tsStart = now - 24 * 3600;
  const tsSpan = 24 * 3600;
  const msValues = valid.map(p => p.ms);
  const msMin = Math.min(...msValues); const msMax = Math.max(...msValues);
  const msSpan = (msMax - msMin) || 1;
  const PAD = {top:12, bottom:12, left:4, right:4};
  const W = width - PAD.left - PAD.right; const H = height - PAD.top - PAD.bottom;

  // Собираем координаты всех точек (включая timeout с ms=null).
  const allPts = points.map(p => {
    const x = PAD.left + ((p.ts - tsStart) / tsSpan) * W;
    if (p.ms === null || p.ms === undefined) {
      return { x: x, y: null, ts: p.ts, ms: null };
    }
    const y = PAD.top + H - ((p.ms - msMin) / msSpan) * H;
    return { x: x, y: y, ts: p.ts, ms: p.ms };
  });

  // Основная линия (только валидные точки).
  // v1.24.10: разрываем линию, если между соседними точками прошло
  // больше GAP_THRESHOLD_SEC (30 мин) — иначе жёлтая линия идёт
  // прямо через серый quiet-разрыв и перекрывает пунктир.
  const PATH_GAP_SEC = 1800;
  let pathD = ""; let pen = false; let prevTs = null;
  for (const p of allPts) {
    if (p.ms === null || p.ms === undefined) { pen = false; prevTs = null; continue; }
    if (pen && prevTs !== null && (p.ts - prevTs) > PATH_GAP_SEC) {
      pen = false;  // разрыв по времени — начинаем новую линию
    }
    pathD += (pen ? "L" : "M") + ` ${p.x.toFixed(1)} ${p.y.toFixed(1)} `;
    pen = true;
    prevTs = p.ts;
  }

  // v1.24.8: разрывы — красный пунктир между точкой до и точкой после.
  // Ищем consecutive-группы timeout-точек между двумя валидными.
  let gapD = "";
  const gapHits = [];
  let i = 0;
  while (i < allPts.length) {
    if (allPts[i].ms !== null && allPts[i].ms !== undefined) { i++; continue; }
    // Начало группы timeout'ов.
    let j = i;
    while (j < allPts.length && (allPts[j].ms === null || allPts[j].ms === undefined)) j++;
    // allPts[i..j-1] — timeout'ы. Нужны точки до (i-1) и после (j).
    if (i > 0 && j < allPts.length) {
      const prev = allPts[i-1];
      const next = allPts[j];
      if (prev.y !== null && next.y !== null) {
        const x1 = prev.x;
        const x2 = next.x;
        const yMid = (prev.y + next.y) / 2;
        gapD += `M ${x1.toFixed(1)} ${prev.y.toFixed(1)} L ${x2.toFixed(1)} ${next.y.toFixed(1)} `;
        gapHits.push({
          x1: x1, x2: x2, y: yMid,
          tsFrom: prev.ts, tsTo: next.ts,
          count: (j - i),
        });
      }
    }
    i = j;
  }

  // v1.24.9: серые разрывы — между соседними точками прошло
  // больше GAP_THRESHOLD_SEC (30 мин) — значит замеров вообще
  // не было (quiet-hours, рестарт bridge, ручной пропуск).
  let gapTimeD = "";
  const gapTimeHits = [];
  const GAP_THRESHOLD_SEC = 1800;
  for (let k = 1; k < allPts.length; k++) {
    const a = allPts[k - 1];
    const b = allPts[k];
    if (b.ts - a.ts <= GAP_THRESHOLD_SEC) continue;
    const yA = (a.y === null) ? height - 2 : a.y;
    const yB = (b.y === null) ? height - 2 : b.y;
    // Проверяем, что между ними нет валидной точки (иначе это не разрыв).
    gapTimeD += `M ${a.x.toFixed(1)} ${yA.toFixed(1)} L ${b.x.toFixed(1)} ${yB.toFixed(1)} `;
    gapTimeHits.push({
      x1: a.x, x2: b.x, y: (yA + yB) / 2,
      tsFrom: a.ts, tsTo: b.ts,
    });
  }

  // Красные маркеры timeout-точек внизу.
  let timeouts = "";
  for (const p of allPts) {
    if (p.ms === null || p.ms === undefined) {
      timeouts += `<circle cx="${p.x.toFixed(1)}" cy="${height-2}" r="1.5" fill="var(--red)" opacity="0.7"/>`;
    }
  }
  const avg = msValues.reduce((a, b) => a+b, 0) / msValues.length;
  const strokeColor = avg < 20 ? "var(--green)" : (avg < 100 ? "var(--yellow)" : "var(--red)");

  const svgId = "lat-" + Math.random().toString(36).slice(2, 10);
  // Точки для tooltip — все (валидные с координатой y, timeout — с y=height-2).
  const pts = allPts.map(p => ({
    x: p.x,
    y: (p.ms === null || p.ms === undefined) ? height - 2 : p.y,
    ts: p.ts, ms: p.ms,
  }));
  const ptsJson = JSON.stringify(pts).replace(/'/g, "&#39;");
  const gapJson = JSON.stringify(gapHits).replace(/'/g, "&#39;");
  const gapTimeJson = JSON.stringify(gapTimeHits).replace(/'/g, "&#39;");

  const svgHtml = `<svg id="${svgId}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" style="color:var(--muted);" data-hits='${ptsJson}' data-gaps='${gapJson}' data-gaps-time='${gapTimeJson}'>
    <path d="${pathD}" fill="none" stroke="${strokeColor}" stroke-width="1.5"/>
    ${gapTimeD ? `<path d="${gapTimeD}" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="3,4" opacity="0.7"/>` : ""}
    ${gapD ? `<path d="${gapD}" fill="none" stroke="var(--red)" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.9"/>` : ""}
    ${timeouts}
    <text x="${PAD.left+2}" y="10" fill="currentColor" font-size="9">${msMax} мс</text>
    <text x="${PAD.left+2}" y="${height-4}" fill="currentColor" font-size="9">${msMin} мс</text></svg>`;

  setTimeout(() => {
    const el = document.getElementById(svgId);
    if (!el) return;
    const hits = JSON.parse(el.dataset.hits || "[]");
    const gaps = JSON.parse(el.dataset.gaps || "[]");
    const gapsTime = JSON.parse(el.dataset.gapsTime || "[]");
    _chartTooltip.bind(el, {
      hitTest: (svgEl, ev) => {
        const { x, y } = svgClientToLocal(svgEl, ev.clientX, ev.clientY);
        // 1. Ближайшая точка в радиусе 12 SVG-единиц — приоритет.
        let best = null, bestD = 144;
        for (const h of hits) {
          const dx = h.x - x, dy = h.y - y;
          const d2 = dx*dx + dy*dy;
          if (d2 < bestD) { bestD = d2; best = h; }
        }
        if (best && bestD < 144) return { data: best, isGap: false, isTimeGap: false };
        // 2. Красный разрыв (timeout).
        for (const g of gaps) {
          if (x >= g.x1 && x <= g.x2) {
            return { data: g, isGap: true, isTimeGap: false };
          }
        }
        // 3. Серый разрыв (нет замеров > 30 мин).
        for (const g of gapsTime) {
          if (x >= g.x1 && x <= g.x2) {
            return { data: g, isGap: false, isTimeGap: true };
          }
        }
        return null;
      },
      onHover: (svgEl, hit) => {
        svgEl.querySelectorAll("circle.pt-hover").forEach(c => c.remove());
        const ns = "http://www.w3.org/2000/svg";

        // v1.24.14: маркер для серого разрыва — как синяя, только серая.
        if (hit.isTimeGap) {
          const g = hit.data;
          const c = document.createElementNS(ns, "circle");
          c.setAttribute("class", "pt-hover");
          c.setAttribute("cx", (g.x1 + g.x2) / 2);
          c.setAttribute("cy", (typeof g.y === "number") ? g.y : (height / 2));
          c.setAttribute("r", 5);
          c.setAttribute("fill", "#b0b8c0");
          c.setAttribute("stroke", "var(--fg)");
          c.setAttribute("stroke-width", "1.5");
          svgEl.appendChild(c);
          return;
        }

        // v1.24.14: маркер для красного разрыва — как синяя, только красная.
        if (hit.isGap) {
          const g = hit.data;
          const c = document.createElementNS(ns, "circle");
          c.setAttribute("class", "pt-hover");
          c.setAttribute("cx", (g.x1 + g.x2) / 2);
          c.setAttribute("cy", (typeof g.y === "number") ? g.y : (height / 2));
          c.setAttribute("r", 5);
          c.setAttribute("fill", "var(--red)");
          c.setAttribute("stroke", "var(--fg)");
          c.setAttribute("stroke-width", "1.5");
          svgEl.appendChild(c);
          return;
        }

        // Обычная точка — цвет маркера совпадает с цветом бейджа.
        const h = hit.data;
        let dotColor = "var(--accent)";
        if (h.ms === null || h.ms === undefined) {
          dotColor = "var(--red)";
        } else if (h.ms >= 100) {
          dotColor = "var(--red)";
        } else if (h.ms >= 20) {
          dotColor = "var(--yellow)";
        } else {
          dotColor = "var(--green)";
        }
        const c = document.createElementNS(ns, "circle");
        c.setAttribute("class", "pt-hover hover");
        c.setAttribute("cx", h.x);
        c.setAttribute("cy", h.y);
        c.setAttribute("r", 5);
        c.setAttribute("fill", dotColor);
        c.setAttribute("stroke", "var(--fg)");
        c.setAttribute("stroke-width", "1.5");
        svgEl.appendChild(c);
      },
      onLeave: (svgEl) => {
        svgEl.querySelectorAll("circle.pt-hover").forEach(c => c.remove());
      },
      render: (hit) => {
        const h = hit.data;
        const fmtD = (ts) => {
          const d = new Date(ts * 1000);
          const dd = String(d.getDate()).padStart(2, "0");
          const mm = String(d.getMonth() + 1).padStart(2, "0");
          const hh = String(d.getHours()).padStart(2, "0");
          const mi = String(d.getMinutes()).padStart(2, "0");
          return `${dd}.${mm} ${hh}:${mi}`;
        };
        if (hit.isGap) {
          // Красный разрыв — интервал + количество timeout'ов.
          const from = fmtD(h.tsFrom);
          const to = fmtD(h.tsTo);
          return `<div class="tt-head">${from} — ${to}</div>
            <div class="tt-row"><span class="tt-name">Задержка:</span>
              <span class="tt-badge bad">timeout × ${h.count}</span></div>`;
        }
        if (hit.isTimeGap) {
          // Серый разрыв — нет замеров вообще (quiet/рестарт/пропуск).
          const from = fmtD(h.tsFrom);
          const to = fmtD(h.tsTo);
          const dur = h.tsTo - h.tsFrom;
          const hh = Math.floor(dur / 3600);
          const mi = Math.floor((dur % 3600) / 60);
          let durStr = "";
          if (hh > 0) durStr = `${hh}ч ${mi}м`;
          else durStr = `${mi}м`;
          return `<div class="tt-head">${from} — ${to}</div>
            <div class="tt-row"><span class="tt-name">Нет замеров:</span>
              <span class="tt-badge" style="background:rgba(139,148,158,0.18);color:var(--muted);">${durStr}</span></div>`;
        }
        const head = fmtD(h.ts);
        if (h.ms === null || h.ms === undefined) {
          return `<div class="tt-head">${head}</div>
            <div class="tt-row"><span class="tt-name">Задержка:</span>
              <span class="tt-badge bad">timeout</span></div>`;
        }
        let cls = "good";
        if (h.ms >= 100) cls = "bad";
        else if (h.ms >= 20) cls = "mid";
        return `<div class="tt-head">${head}</div>
          <div class="tt-row"><span class="tt-name">Задержка:</span>
            <span class="tt-badge ${cls}">${h.ms} мс</span></div>`;
      },
    });
  }, 0);

  return svgHtml;
}

// v1.23.0: точечное обновление модалки.
// Каждая зона — отдельный контейнер. Если HTML зоны не изменился —
// DOM не трогаем → выделение текста сохраняется между автообновлениями.
const _modalZoneHashes = {};
// v1.28.17: _modalActionsHash удалён — actions-zone больше нет (v1.28.12).

// v1.23.10: состояние раскрытия секции «Кэш состояния».
// Ключ — имя устройства. Хранит boolean для каждого устройства,
// чтобы между автообновлениями (каждые 5 сек) выбор пользователя
// сохранялся.
const _CACHE_OPEN_STATE = {};
function _onCacheToggle(deviceName, isOpen) {
  if (deviceName) _CACHE_OPEN_STATE["_cacheOpen_" + deviceName] = !!isOpen;
}

// v1.28.82: сохраняем/восстанавливаем скролл ЛЮБЫХ таблиц при перерисовке
// (иначе после renderUpdate таблицы «откидывались» влево/вверх).
function _captureScrolls(root) {
  const nodes = Array.from(root.querySelectorAll("*"));
  const out = [];
  nodes.forEach((el, i) => {
    if (el.scrollLeft || el.scrollTop) out.push({ i, l: el.scrollLeft, t: el.scrollTop });
  });
  return out;
}
function _restoreScrolls(root, saved) {
  if (!saved || !saved.length) return;
  const nodes = Array.from(root.querySelectorAll("*"));
  for (const s of saved) {
    const el = nodes[s.i];
    if (!el) continue;
    if (s.l) el.scrollLeft = s.l;
    if (s.t) el.scrollTop = s.t;
  }
}

// v1.28.84: любое раскрытие <details> — небольшой скролл, чтобы блок был виден.
document.addEventListener("toggle", function (e) {
  const el = e.target;
  if (!el || el.tagName !== "DETAILS" || !el.open) return;
  setTimeout(function () {
    try { el.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch (err) {}
  }, 60);
}, true);

function _updateZone(zoneId, html) {
  const el = document.getElementById(zoneId);
  if (!el) return;
  const prev = _modalZoneHashes[zoneId];
  if (prev === html) return;  // ← не трогаем DOM
  _modalZoneHashes[zoneId] = html;
  const _sc = _captureScrolls(el);
  el.innerHTML = html;
  _restoreScrolls(el, _sc);
}

async function revealSecret(name) {
  try {
    const r = await fetch(`/api/device/${encodeURIComponent(name)}/secret`);
    const data = await r.json();
    if (data.ok) {
      REVEALED_KEYS[name] = data.local_key;
      // v1.22.1: точечная перерисовка key-зоны (quiet не трогаем).
      _rerenderModalKeyZone();
    } else uiAlert("Ошибка", "Не удалось: " + (data.error || "unknown"), "error");
  } catch (e) { console.error(e); }
}
function hideSecret(name) {
  delete REVEALED_KEYS[name];
  _rerenderModalKeyZone();  // v1.22.1
}

// v1.23.0: renderModalVolatile разбит на 6 зон (_renderInfoHtml,
// _renderClimateHtml, _renderSparklineHtml, _renderLatencyHtml,
// _renderCacheHtml, _renderHistoryHtml). Каждая зона обновляется
// через _updateZone — если HTML не изменился, DOM не трогается.
function _renderInfoHtml(d) {
  if (!d) return "";
  if (DEVICE_HISTORY_CACHE[d.name] && (!d.history || d.history.length === 0)) d.history = DEVICE_HISTORY_CACHE[d.name];
  const on = d.status === "online";
  let html = "";
  html += `<h3>Информация</h3><table class="detail-table">`;
  // v1.27.7: для отключённых — явный бейдж.
  if (d.enabled === false) {
    html += `<tr><td>Состояние</td><td><span class="badge enabled-off">⛔ Отключено</span></td></tr>`;
  }
  html += `<tr><td>Статус</td><td><span class="dot ${on?'online':'offline'}"></span>${escapeHtml(d.status || "unknown")}</td></tr>`;
  html += `<tr><td>Последняя активность</td><td>${d.last_seen ? fmtAgo(d.last_seen) : '—'}</td></tr>`;
  // v1.28.2: для battery/disabled/quiet — показываем «не измеряется»
  if (d.latency_hidden) {
    const _r = d.latency_reason;
    let _label = 'не измеряется';
    let _icon = '';
    if (_r === 'battery')  { _icon = '🔋'; _label = 'батарейное — не измеряется'; }
    else if (_r === 'disabled') { _icon = '⛔'; _label = 'отключено — не измеряется'; }
    else if (_r === 'quiet')    { _icon = '🔇'; _label = 'режим тишины — не измеряется'; }
    html += `<tr><td>Задержка</td><td class="muted">${_icon ? _icon + ' ' : ''}${_label}</td></tr>`;
  } else {
    html += `<tr><td>Задержка</td><td><span class="latency ${latencyClass(d.latency_ms)}">${latencyText(d.latency_ms)}</span>${d.latency_ts ? ' <span class="muted">('+fmtAgo(d.latency_ts)+')</span>' : ''}</td></tr>`;
  }
  html += `<tr><td>Имя (id)</td><td>${copyCode(d.name)}</td></tr>`;
  html += `<tr><td>Тип</td><td>${typeBadge(d.type)}${d.battery_powered ? ' <span class="power-glyph" title="Батарейное">🔋 Батарейный</span>' : ' <span class="power-glyph" title="Проводное">🔌 Проводной</span>'}</td></tr>`;
  // v1.28.6: для батарейных — battery_alert + battery_last_up.
  // v1.28.28: +ping_unavailable — если bridge без CAP_NET_RAW,
  // battery_alert не публикуется, но это не «ещё не публиковалось».
  if (d.battery_powered === true) {
    const _ba = d.battery_alert;
    if (_ba === "ok") {
      html += `<tr><td>Battery alert</td><td><span style="color:var(--green);">✅ ok</span></td></tr>`;
    } else if (_ba === "no_data") {
      html += `<tr><td>Battery alert</td><td><span style="color:var(--red);">⚠️ no_data (нет данных &gt; 24ч)</span></td></tr>`;
    } else if (BRIDGE_STATE.bridge_ping_mode && BRIDGE_STATE.bridge_ping_mode.mode === "none") {
      html += `<tr><td>Battery alert</td><td class="muted">⚠️ не публикуется: нет CAP_NET_RAW</td></tr>`;
    } else {
      html += `<tr><td>Battery alert</td><td class="muted">— (ещё не публиковалось)</td></tr>`;
    }
    if (d.battery_last_up) {
      html += `<tr><td>Battery last up</td><td>${fmtAgo(d.battery_last_up)} <span class="muted" style="font-size:11px;">(${fmtDateTime(d.battery_last_up)})</span></td></tr>`;
    } else {
      html += `<tr><td>Battery last up</td><td class="muted">—</td></tr>`;
    }
  }
  if (d.model) html += `<tr><td>Модель</td><td>${escapeHtml(d.model)}</td></tr>`;
  if (d.ip) html += `<tr><td>IP</td><td>${copyCode(d.ip)}</td></tr>`;
  if (d.version) html += `<tr><td>Версия протокола</td><td>${versionBadge(d.version)}</td></tr>`;
  if (d.tuya_id) html += `<tr><td>Tuya ID</td><td>${copyCode(d.tuya_id)}</td></tr>`;
  html += `</table>`;
  return html;
}

function _renderClimateHtml(d) {
  if (!d || d.type !== "climate") return "";
  if (!(d.presets?.length > 0 || d.min_temp || d.max_temp)) return "";
  let html = `<h3>Климат</h3><table class="detail-table">`;
  if (d.min_temp !== null && d.min_temp !== undefined && d.max_temp !== null && d.max_temp !== undefined)
    html += `<tr><td>Диапазон</td><td>${d.min_temp}°C — ${d.max_temp}°C (шаг ${d.temp_step || "?"})</td></tr>`;
  if (d.presets?.length > 0) {
    const pmap = d.preset_map || {};
    html += `<tr><td>Пресеты</td><td>${escapeHtml(d.presets.map(p => pmap[p] || p).join(", "))}</td></tr>`;
  }
  html += `</table>`;
  return html;
}

function _renderSparklineHtml(d) {
  if (!STATUS_HISTORY_ENABLED) return "";
  // v1.28.10d: для батарейных WebUI не пишет status_events
  // (bridge не публикует offline для них). Старые записи от
  // polling-периода рисуют фантомный offline — скрываем график.
  if (d.battery_powered === true) return "";
  // v1.28.11: disabled — тоже скрываем (согласовано с политикой
  // DISABLED_HIDE_FROM_ANALYTICS: disabled не пишет status_events).
  if (d.enabled === false) return "";
  const histAll = d.history || [];
  if (histAll.length === 0) return "";
  return `<h3>Хронология статуса</h3><div class="sparkline">${sparklineSvgWithLabels(histAll, 700, 40)}</div>`;
}

function _renderLatencyHtml(d) {
  if (!STATUS_HISTORY_ENABLED) return "";
  // v1.28.10d: для батарейных latency не измеряется (_should_ping=false),
  // старые записи от polling-периода показывают timeout — скрываем график.
  if (d.battery_powered === true || d.latency_hidden) return "";
  const latPoints = DEVICE_LATENCY_CACHE[d.name] || [];
  const avgData = DEVICE_AVG_LATENCY_CACHE[d.name];
  if (latPoints.length === 0) return "";
  const valid = latPoints.filter(p => p.ms !== null && p.ms !== undefined);
  const timeouts = latPoints.length - valid.length;
  let stats = "";
  if (avgData && avgData.avg !== null && avgData.avg !== undefined) {
    stats += `сред. 24ч <strong>${avgData.avg} мс</strong> (${avgData.count} замеров)`;
  }
  if (d.latency_ms !== null && d.latency_ms !== undefined) { if (stats) stats += " · "; stats += `сейчас <strong>${d.latency_ms} мс</strong>`; }
  if (timeouts > 0) { if (stats) stats += " · "; stats += `<span style="color:var(--red)">timeout: ${timeouts}</span>`; }
  const cnt = latPoints.length;
  let html = `<h3>Задержка (24ч, ${cnt}) <span class="muted" style="float:right; text-transform:none; font-weight:normal;">${stats}</span></h3>`;
  html += `<div class="sparkline" style="height:60px;">${latencySparklineSvg(latPoints, 700, 60)}</div>`;
  return html;
}

// v1.25.12: идемпотентная инициализация _CACHE_OPEN_STATE.
// Сохраняем ручное раскрытие пользователя при росте набора DP:
// - если хэш не менялся → ничего не трогаем
// - если менялся, но ключ уже был → сохраняем прежний open/close
// - если ключа нет → ставим по умолчанию (<= threshold открыт)
// v1.28.50: «Кэш состояния» всегда свёрнут по умолчанию; пользователь
// может раскрыть (состояние помнит _onCacheToggle/_CACHE_OPEN_STATE).
function _renderCacheHtml(d) {
  const cache = d.cache || {}; const dps_map = d.dps_map || {};
  const keys = Object.keys(cache);
  if (keys.length === 0) return "";

  const cacheKey = "_cacheOpen_" + (d.name || "");
  const isOpen = _CACHE_OPEN_STATE[cacheKey] ? "open" : "";

  let rows = "";
  const sorted = keys.slice().sort((a, b) => {
    const ai = parseInt(a), bi = parseInt(b);
    if (!isNaN(ai) && !isNaN(bi)) return ai - bi;
    return a.localeCompare(b);
  });
  for (const dp of sorted) {
    const info = dps_map[dp] || {};
    // v1.25.0 (fix #cache_en): в колонке — АНГЛИЙСКОЕ (code),
    // русский перевод / оригинал / облачное — только в тултипе.
    // Бейдж источника: ☁ cloud / 📖 code-словарь / 🈶 cn-словарь /
    // config — без бейджа / ? — без бейджа.
    const cjk = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]/;
    const rawName = (info.name || "").trim();
    const code = (info.code || "").trim();
    const origCn = (info._name_original || "").trim();
    const src = info._name_source || "";
    const fromCache = !!info._from_cache;

    // v1.25.0 (fix2): display = code || name(если не CJK) || "?".
    // Раньше было display = code || "?" — теряло DP из config
    // (у них есть только name, без code).
    let display = "";
    if (code) {
      display = code;
    } else if (rawName && !cjk.test(rawName)) {
      display = rawName;
    } else {
      display = "?";
    }

    // v1.28.6: блок tt удалён — дублировал resolveDpDisplay.
    // v1.28.6b: убран остаточный if (!tt.length) tt.push(...) —
    // ссылка на несуществующую переменную (ReferenceError при
    // display === "?"). Бейдж истинного источника:
    let badge = "";
    if (display !== "?") {
      if (fromCache || src === "cloud") {
        badge = "☁";
      } else if (src === "dict") {
        badge = "📖";
      } else if (src === "cn") {
        badge = "🈶";
      }
    }
    // src === "config" → без бейджа

    // v1.27.3: единый .dp-tip со столбиком вместо title с « · ».
    // Источник ☁ (cloud/cache) имеет приоритет над словарными.
    const _r = resolveDpDisplay(code, rawName || "", origCn || "");
    let _badge = badge;
    if (fromCache || src === "cloud") _badge = "☁";
    else if (!_badge && _r.badge) _badge = _r.badge;
    // v1.28.11: убран мёртвый _warn (_r.replaced не существует в resolveDpDisplay).
    const _tipText = (fromCache || src === "cloud")
      ? _r.tooltip + (src ? "\nИсточник: " + (fromCache ? "cache" : src) : "")
      : _r.tooltip;
    const _badgeHtml = _badge
      ? ` <span class="dp-tip" data-tip="${escapeAttr(_tipText)}">${_badge}</span>`
      : '';
    const val = JSON.stringify(cache[dp]);
    // v1.25.12: обёртка .cache-dp-name — чтобы ☁ не съезжал на новую строку
    rows += `<tr><td><span class="cache-dp-name">${escapeHtml(dp)} <span class="muted">(${escapeHtml(display)})</span>${_badgeHtml}</span></td>
      <td>${_cellValue(val, 80)}</td></tr>`;
  }

  return `<details class="cache-details" ${isOpen}
      ontoggle="_onCacheToggle('${escapeAttr(d.name || "")}', this.open)">
    <summary><h3 style="display:inline; margin:0;">Кэш состояния (${keys.length})</h3>
      <span class="muted" style="font-size:11px; margin-left:6px;">${_CACHE_OPEN_STATE[cacheKey] ? "" : "клик — раскрыть"}</span>
    </summary>
    <table class="detail-table" style="margin-top:6px;">${rows}</table>
  </details>`;
}

function _renderHistoryHtml(d) {
  if (!STATUS_HISTORY_ENABLED) return "";
  const histAll = d.history || [];
  const hist = histAll.slice().reverse();
  if (hist.length === 0) return "";
  let html = `<h3>История статуса (последние ${hist.length})</h3><div class="history-scroll">`;
  for (const h of hist) html += `<div class="history-item ${h.status}">${fmtDateTime(h.ts)} — ${h.status}</div>`;
  html += `</div>`;
  return html;
}

// v1.28.71: сырые данные устройства (Bridge / Cloud) в модалке дашборда.
// Содержимое грузится лениво при раскрытии — блок статичный, не «дёргает»
// модалку при каждом обновлении статуса.
function _renderRawHtml(d) {
  if (!d) return "";
  const name = escapeAttr(d.name || "");
  const tid = escapeAttr(d.tuya_id || "");
  return `<details class="raw-details" ontoggle="toggleBridgeRaw(this, '${name}')">
    <summary class="muted">▶ Показать сырые данные по устройству (Bridge)<span class="muted" style="font-size:11px; margin-left:6px; text-transform:none; letter-spacing:0;">клик — раскрыть</span></summary>
    <pre class="raw-pre"><code class="json-view" data-raw="bridge"></code></pre>
  </details>
  <details class="raw-details" ontoggle="toggleCloudRaw(this, '${name}', '${tid}')">
    <summary class="muted">▶ Показать сырые данные по устройству (Cloud)<span class="muted" style="font-size:11px; margin-left:6px; text-transform:none; letter-spacing:0;">клик — раскрыть</span></summary>
    <pre class="raw-pre"><code class="json-view" data-raw="cloud"></code></pre>
  </details>`;
}

function toggleBridgeRaw(el, name) {
  if (!el.open) return;
  const codeEl = el.querySelector('code[data-raw="bridge"]');
  if (!codeEl || codeEl.dataset.loaded === "1") return;
  const d = (typeof LAST_DEVICES !== "undefined" ? LAST_DEVICES : []).find(x => x.name === name) || null;
  if (!d) { codeEl.textContent = "нет данных"; return; }
  const dump = {
    name: d.name, friendly_name: d.friendly_name, type: d.type, model: d.model,
    ip: d.ip, tuya_id: d.tuya_id, enabled: d.enabled,
    battery_powered: d.battery_powered, status: d.status, last_seen: d.last_seen,
    dps_map: d.dps_map || {}, cache: d.cache || {},
  };
  codeEl.textContent = JSON.stringify(dump, null, 2);
  codeEl.dataset.loaded = "1";
  highlightJsonInto(codeEl);
}

async function toggleCloudRaw(el, name, tuyaId) {
  if (!el.open) return;
  const codeEl = el.querySelector('code[data-raw="cloud"]');
  if (!codeEl || codeEl.dataset.loaded === "1") return;
  codeEl.textContent = "загрузка…";
  try {
    if (!_CLOUD_RAW_MAP) {
      const r = await fetch("/api/cloud/cache");
      const data = await r.json();
      _CLOUD_RAW_MAP = {};
      for (const x of (data.devices || [])) if (x && x.id) _CLOUD_RAW_MAP[x.id] = x;
    }
    const dev = tuyaId ? _CLOUD_RAW_MAP[tuyaId] : null;
    const dump = dev ? Object.assign({}, dev)
                     : { _note: "нет данных Cloud для этого устройства" };
    delete dump.local_key;
    delete dump._raw_cloud;   // слишком объёмно, есть в Cloud-модалке
    codeEl.textContent = JSON.stringify(dump, null, 2);
    codeEl.dataset.loaded = "1";
    highlightJsonInto(codeEl);
  } catch (e) {
    codeEl.textContent = "ошибка: " + e.message;
  }
}

// v1.23.0: рендер модалки — точечное обновление по зонам.
function renderModalVolatile(d) {
  if (!d) return;

  // Первое открытие — создаём структуру зон
  if (!document.getElementById("modal-volatile")) {
    // v1.28.0: modal-key-zone перенесён сразу после mv-info —
    // Local key визуально примыкает к таблице «Информация».
    // Остальные volatile-зоны — в modal-volatile-rest.
    document.getElementById("modal-body").innerHTML =
      '<div id="modal-volatile">'
      +   '<div id="mv-info"></div>'
      + '</div>'
      + '<div id="modal-key-zone"></div>'
      + '<div id="modal-volatile-rest">'
      +   '<div id="mv-climate"></div>'
      +   '<div id="modal-quiet-zone"></div>'
      +   '<div id="mv-sparkline"></div>'
      +   '<div id="mv-latency"></div>'
      +   '<div id="mv-dps"></div>'
      +   '<div id="mv-cache"></div>'
      +   '<div id="mv-history"></div>'
      +   '<div id="mv-raw"></div>'
      + '</div>';
    // Сбрасываем хэши, чтобы зоны отрисовались
    Object.keys(_modalZoneHashes).forEach(k => delete _modalZoneHashes[k]);
    renderModalSensitive(d);
    // v1.28.32: НЕ инициализируем DPS_EDIT здесь — staging
    // инициализируется в showDevice(). Иначе при пересоздании
    // modal-volatile (fetchStatus после ошибки apply) staging
    // сбрасывался, и удалённые DP пропадали из UI.
    dpsRenderSection();
  }

  _updateZone("mv-info",      _renderInfoHtml(d));
  _updateZone("mv-climate",   _renderClimateHtml(d));
  // v1.24.6: sparkline и latency не трогаем при открытом tooltip —
  // иначе слетают hover и marker точек.
  // v1.25.0 (fix #21): при открытом tooltip спарклайна пропускаем
  // только mv-sparkline. mv-latency не конфликтует с ним — обновляем.
  const _ttOpen = (typeof _chartTooltip !== "undefined") && _chartTooltip.isOpen();
  // v1.25.12: при открытом tooltip пропускаем ОБА спарклайна
  // (mv-sparkline = статус, mv-latency = задержка). Иначе при
  // перерисовке mv-latency старый SVG удаляется и tooltip
  // «отвязывается» от несуществующего элемента.
  if (!_ttOpen) {
    _updateZone("mv-sparkline", _renderSparklineHtml(d));
    _updateZone("mv-latency", _renderLatencyHtml(d));
  }
  _updateZone("mv-cache",     _renderCacheHtml(d));
  _updateZone("mv-history",   _renderHistoryHtml(d));
  _updateZone("mv-raw",       _renderRawHtml(d));   // v1.28.71: сырые данные
}

// v1.22.1: key-часть вынесена — revealSecret/hideSecret
// перерисовывают только её, не трогая quiet-редактор.
function renderModalKeySection(d) {
  if (!d || !d.local_key_present) return "";
  const revealedKey = REVEALED_KEYS[d.name];
  // v1.27.12: убран <h3>Local key</h3> — строка визуально
  // примыкает к таблице «Информация» (border-top снят).
  // Кнопки: [👁 Показать] / [🙈 Скрыть] — как в Cloud-модалке.
  let s = '<table class="detail-table detail-table-key-merged">';
  if (revealedKey) {
    s += '<tr><td>Local key</td><td>'
      + copyCode(revealedKey)
      + ' <button onclick="hideSecret(' + jsStr(d.name) + ')" title="Скрыть" style="margin-left:4px; padding:2px 8px; font-size:12px;">🔓</button></td></tr>';
  } else {
    s += '<tr><td>Local key</td><td><span class="secret-masked">••••••••••</span>'
      + ' <button onclick="revealSecret(' + jsStr(d.name) + ')" title="Показать" style="margin-left:6px; padding:2px 8px; font-size:12px;">🔒</button></td></tr>';
  }
  s += '</table>';
  return s;
}

// v1.22.1: точечная перерисовка только key-зоны.
function _rerenderModalKeyZone() {
  if (CURRENT_MODAL_IDX < 0) return;
  const d = LAST_DEVICES[CURRENT_MODAL_IDX];
  if (!d || !d.local_key_present) return;
  const el = document.getElementById("modal-key-zone");
  if (!el) return;
  el.innerHTML = renderModalKeySection(d);
}

function renderModalSensitive(d) {
  if (!d) return;
  // v1.24.1: три отдельные зоны — порядок quiet → key → кнопки.
  const quietZone   = document.getElementById("modal-quiet-zone");
  const keyZone     = document.getElementById("modal-key-zone");
  // v1.28.17: actionsZone удалён (зона не создаётся).
  if (quietZone) quietZone.innerHTML = renderQuietSection(d);
  // v1.27.12: key-zone НЕ перерисовываем при каждом fetchStatus.
  // Иначе кнопка [👁 Показать] сбрасывается каждые 5 сек.
  // Перерисовка — только при первом открытии (пусто) и в
  // _rerenderModalKeyZone() (revealSecret/hideSecret).
  if (keyZone && !keyZone.innerHTML.trim()) {
    keyZone.innerHTML = renderModalKeySection(d);
  }
  // QUIET_EDIT инициализируется в showDevice() (force) при открытии
  // модалки. Здесь его НЕ трогаем — иначе при показе local key
  // (revealSecret) потеряются несохранённые правки quiet-окон.
}

// v1.28.12: открыть editDevice из header showDevice.
function editDeviceFromModal() {
  if (!CURRENT_MODAL_NAME) return;
  editDevice(CURRENT_MODAL_NAME);
}


async function showDevice(idx) {
  const d = LAST_DEVICES[idx];
  if (!d) return;
  CURRENT_MODAL_IDX = idx;
  CURRENT_MODAL_NAME = d.name;  // v1.25.13
  // v1.27.0: staging DP — снимок при открытии.
  dpsInitEdit(d, true);
  // v1.21.1: явное открытие — сбросить старый quiet-редактор
  // (в т.ч. _dirty от прошлого устройства).
  if (typeof d.quiet_windows !== "undefined") {
    quietInitEdit(d, true);
  }
  document.getElementById("modal-title").textContent = (d.friendly_name || d.name) + " [" + (d.type || "?") + "]";
  if (DEVICE_HISTORY_CACHE[d.name]) d.history = DEVICE_HISTORY_CACHE[d.name];
  document.getElementById("modal-body").innerHTML = "";
  renderModalVolatile(d);
  document.getElementById("modal-overlay").classList.add("open");
  if (STATUS_HISTORY_ENABLED) {
    let rerender = false;
    if (!DEVICE_HISTORY_CACHE[d.name]) {
      try {
        const r = await fetch(`/api/device/${encodeURIComponent(d.name)}/history?hours=24&limit=100`);
        const data = await r.json();
        if (data.history) { DEVICE_HISTORY_CACHE[d.name] = data.history; d.history = data.history; rerender = true; }
      } catch (e) {}
    }
    if (!DEVICE_LATENCY_CACHE[d.name]) {
      try {
        const r = await fetch(`/api/device/${encodeURIComponent(d.name)}/latency?hours=24&limit=2000`);
        const data = await r.json();
        if (data.latency) { DEVICE_LATENCY_CACHE[d.name] = data.latency; rerender = true; }
      } catch (e) {}
    }
    if (!DEVICE_AVG_LATENCY_CACHE[d.name]) {
      try {
        const r = await fetch(`/api/device/${encodeURIComponent(d.name)}/avg_latency?latency_seconds=86400`);
        const data = await r.json();
        if (data.avg !== undefined) { DEVICE_AVG_LATENCY_CACHE[d.name] = data; rerender = true; }
      } catch (e) {}
    }
    // v1.28.34: устройство могло смениться, пока шли запросы —
    // не перерисовываем чужую модалку.
    if (CURRENT_MODAL_NAME !== d.name) return;
    if (rerender) renderModalVolatile(d);
  }
}

// v1.21.0: режим тишины (quiet hours)
let QUIET_EDIT = { windows: [] };

function renderQuietSection(d) {
  const wins = d.quiet_windows || [];
  const now = Math.floor(Date.now()/1000);
  const inQuiet = d.quiet || (d.quiet_until && d.quiet_until > now);

  // v1.24.0: разные эмодзи — 🔇 в тишине, 🔈 не в тишине.
  // Статус «до HH:MM» — inline-суффиксом, только когда quiet-on.
  const emoji = inQuiet ? "🔇" : "🔈";
  const cls = inQuiet ? "quiet-title quiet-on" : "quiet-title quiet-off";
  const statusInline = inQuiet && d.quiet_until
    ? `<span class="quiet-status-inline">· сейчас до ${fmtTimeShort(d.quiet_until)}</span>`
    : "";

  let rows = "";
  for (let i = 0; i < QUIET_EDIT.windows.length; i++) {
    const w = QUIET_EDIT.windows[i] || {};
    rows += `<div class="quiet-row" data-qidx="${i}">
      <input type="time" value="${escapeAttr(w.from || '23:00')}" onchange="quietUpdate(${i}, 'from', this.value)">
      <span class="muted">—</span>
      <input type="time" value="${escapeAttr(w.to || '08:00')}" onchange="quietUpdate(${i}, 'to', this.value)">
      <button class="danger" style="padding:2px 8px; font-size:11px;" onclick="quietRemove(${i})">×</button>
    </div>`;
  }
  if (!rows) rows = '<div class="muted" style="font-size:12px;">Окон нет — устройство всегда активно.</div>';

  return `<h3 class="${cls}">
      <span class="quiet-emoji">${emoji}</span>
      <span>Режим тишины</span>
      ${statusInline}
    </h3>
    <div class="muted quiet-help">
      В тишине устройство не попадает в мерцания, хронологию и «Проблемные».
      <b>latency</b> не измеряется. После окна — grace 2 мин.
    </div>
    <div id="quiet-rows">${rows}</div>
    <div class="quiet-actions">
      <button onclick="quietAdd()" style="padding:4px 12px; font-size:12px;">+ Добавить окно</button>
      <button class="primary" id="quiet-save-btn" onclick="quietSave(${jsStr(d.name)})" style="padding:4px 12px; font-size:12px; ${QUIET_EDIT && QUIET_EDIT._dirty ? "" : "display:none;"}">💾 Сохранить</button>
      <button id="quiet-cancel-btn" onclick="quietCancel()" style="padding:4px 12px; font-size:12px; ${QUIET_EDIT && QUIET_EDIT._dirty ? "" : "display:none;"}">↶ Отмена</button>
      <span id="quiet-dirty" class="muted" style="display:${QUIET_EDIT && QUIET_EDIT._dirty ? "inline" : "none"}; font-size:11px; color:var(--yellow);">● не сохранено</span>
      <span id="quiet-save-status" class="muted" style="font-size:11px;"></span>
    </div>`;
}

function quietInitEdit(d, force) {
  // v1.21.1: не сбрасываем редактор, если для этого же устройства
  // уже есть несохранённые изменения.
  if (!force && QUIET_EDIT && QUIET_EDIT._name === d.name && QUIET_EDIT._dirty) {
    return;
  }
  const orig = JSON.parse(JSON.stringify(d.quiet_windows || []));
  QUIET_EDIT = {
    _name: d.name,
    _dirty: false,
    _original: JSON.parse(JSON.stringify(orig)),
    windows: JSON.parse(JSON.stringify(orig))
  };
}

// v1.25.0 (task #D): _dirty = есть ли реальные отличия от _original.
function quietRecalcDirty() {
  if (!QUIET_EDIT) return;
  const a = JSON.stringify(QUIET_EDIT.windows || []);
  const b = JSON.stringify(QUIET_EDIT._original || []);
  QUIET_EDIT._dirty = (a !== b);
  _quietUpdateUi();
}

// v1.25.0 (task #D): показать/скрыть кнопки и индикатор по _dirty.
function _quietUpdateUi() {
  const dirty = !!(QUIET_EDIT && QUIET_EDIT._dirty);
  const btnSave = document.getElementById("quiet-save-btn");
  const btnCancel = document.getElementById("quiet-cancel-btn");
  const dirtyEl = document.getElementById("quiet-dirty");
  if (btnSave) btnSave.style.display = dirty ? "inline-block" : "none";
  if (btnCancel) btnCancel.style.display = dirty ? "inline-block" : "none";
  if (dirtyEl) dirtyEl.style.display = dirty ? "inline" : "none";
}
function quietMarkDirty() {
  quietRecalcDirty();
}
function quietClearDirty() {
  if (QUIET_EDIT) {
    QUIET_EDIT._dirty = false;
    QUIET_EDIT._original = JSON.parse(JSON.stringify(QUIET_EDIT.windows || []));
  }
  _quietUpdateUi();
}
function quietAddRowToDom(w, i) {
  const wrap = document.getElementById("quiet-rows");
  if (!wrap) return;
  // убираем placeholder если есть
  const ph = wrap.querySelector(".quiet-placeholder");
  if (ph) ph.remove();
  const div = document.createElement("div");
  div.className = "quiet-row";
  div.dataset.qidx = String(i);
  div.innerHTML = `
    <input type="time" value="${escapeAttr(w.from || '23:00')}" onchange="quietUpdate(${i}, 'from', this.value)">
    <span class="muted">—</span>
    <input type="time" value="${escapeAttr(w.to || '08:00')}" onchange="quietUpdate(${i}, 'to', this.value)">
    <button class="danger" style="padding:2px 8px; font-size:11px;" onclick="quietRemove(${i})">×</button>`;
  wrap.appendChild(div);
}

function quietAdd() {
  // v1.21.1: точечная вставка строки — без полной перерисовки.
  if (!QUIET_EDIT.windows) QUIET_EDIT.windows = [];
  QUIET_EDIT.windows.push({ from: "23:00", to: "08:00" });
  quietAddRowToDom(QUIET_EDIT.windows[QUIET_EDIT.windows.length - 1],
                   QUIET_EDIT.windows.length - 1);
  quietMarkDirty();
}
function quietUpdate(i, key, value) {
  if (!QUIET_EDIT.windows || !QUIET_EDIT.windows[i]) return;
  QUIET_EDIT.windows[i][key] = value;
  quietMarkDirty();  // v1.21.1
}
function quietReindexRows() {
  // после удаления переиндексируем onchange/onclick у оставшихся строк
  const wrap = document.getElementById("quiet-rows");
  if (!wrap) return;
  const rows = wrap.querySelectorAll(".quiet-row");
  rows.forEach((row, idx) => {
    row.dataset.qidx = String(idx);
    const inputs = row.querySelectorAll('input[type="time"]');
    const btn = row.querySelector("button");
    if (inputs[0]) inputs[0].setAttribute("onchange", `quietUpdate(${idx}, 'from', this.value)`);
    if (inputs[1]) inputs[1].setAttribute("onchange", `quietUpdate(${idx}, 'to', this.value)`);
    if (btn) btn.setAttribute("onclick", `quietRemove(${idx})`);
  });
}

function quietRemove(i) {
  if (!QUIET_EDIT.windows || !QUIET_EDIT.windows[i]) return;
  // v1.21.1: точечное удаление — без полной перерисовки.
  QUIET_EDIT.windows.splice(i, 1);
  const wrap = document.getElementById("quiet-rows");
  if (wrap) {
    const row = wrap.querySelector(`.quiet-row[data-qidx="${i}"]`);
    if (row) row.remove();
    if (QUIET_EDIT.windows.length === 0) {
      wrap.innerHTML = '<div class="muted quiet-placeholder" style="font-size:12px;">Окон нет — устройство всегда активно.</div>';
    } else {
      quietReindexRows();
    }
  }
  quietMarkDirty();
}
// v1.25.0 (task #D): отмена — восстановить _original, перерисовать секцию.
function quietCancel() {
  if (!QUIET_EDIT || !QUIET_EDIT._original) return;
  QUIET_EDIT.windows = JSON.parse(JSON.stringify(QUIET_EDIT._original));
  QUIET_EDIT._dirty = false;
  if (CURRENT_MODAL_IDX >= 0) {
    const d = LAST_DEVICES[CURRENT_MODAL_IDX];
    if (d) {
      const zone = document.getElementById("modal-quiet-zone");
      if (zone) zone.innerHTML = renderQuietSection(d);
    }
  }
  _quietUpdateUi();
}

async function quietSave(name) {
  const statusEl = document.getElementById("quiet-save-status");
  if (statusEl) statusEl.innerHTML = '<span class="spin"></span> сохранение…';
  try {
    const r = await fetch(`/api/device/${encodeURIComponent(name)}/quiet`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ windows: QUIET_EDIT.windows || [] })
    });
    const data = await r.json();
    if (data.ok) {
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--green);">✅ сохранено</span>';
      const d = LAST_DEVICES.find(x => x.name === name);
      if (d) d.quiet_windows = data.windows || [];
      // v1.21.1: сбрасываем dirty и обновляем ТОЛЬКО данные, без перерисовки модалки
      quietClearDirty();
      // v1.25.0 (task #D): _original уже обновлён в quietClearDirty —
      // кнопки «Сохранить»/«Отмена» прячутся автоматически.
      // тихо подтянем свежий /api/status, но модалку не трогаем
      fetchStatus();
      setTimeout(() => { if (statusEl) statusEl.textContent = ""; }, 2000);
    } else {
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--red);">❌ ' + escapeHtml(data.error || "ошибка") + '</span>';
    }
  } catch (e) {
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--red);">❌ ' + escapeHtml(e.message) + '</span>';
  }
}

function closeModal(evt) {
  if (evt && evt.target && evt.target.id !== "modal-overlay") return;
  // v1.25.13: чистим по имени, а не по индексу — индекс мог
  // стать -1 при повторном вызове или закрытии по Escape.
  if (CURRENT_MODAL_NAME) {
    delete DEVICE_HISTORY_CACHE[CURRENT_MODAL_NAME];
    delete DEVICE_LATENCY_CACHE[CURRENT_MODAL_NAME];
    delete DEVICE_AVG_LATENCY_CACHE[CURRENT_MODAL_NAME];
    // v1.28.25: сбрасываем показ Local key — при повторном открытии
    // того же устройства key снова под маской (безопасность).
    try { delete REVEALED_KEYS[CURRENT_MODAL_NAME]; } catch (e) {}
    try {
      delete _CACHE_OPEN_STATE["_cacheOpen_" + CURRENT_MODAL_NAME];
      delete _CACHE_OPEN_STATE["_cacheHash_" + CURRENT_MODAL_NAME];
    } catch (e) {}
    // v1.27.0: очистка staging DP.
    // v1.27.1: DPS_EDIT сбрасывается целиком (включая _modified/_editMode).
    DPS_EDIT = null;
    try {
      delete _DPS_OPEN_STATE["_dpsOpen_" + CURRENT_MODAL_NAME];
      delete _DPS_OPEN_STATE["_dpsJunkOpen_" + CURRENT_MODAL_NAME];
    } catch (e) {}
    CURRENT_MODAL_NAME = null;
  }
  CURRENT_MODAL_IDX = -1;
  // v1.25.0 (fix #2): явно чистим хэши зон модалки — иначе
  // _updateZone может не перерисовать зону, если HTML случайно
  // совпал с прошлым (при открытии другого устройства).
  try {
    Object.keys(_modalZoneHashes).forEach(k => delete _modalZoneHashes[k]);
  } catch (e) {}
  document.getElementById("modal-overlay").classList.remove("open");
}

async function refreshModal() {
  if (CURRENT_MODAL_IDX < 0) return;
  const d = LAST_DEVICES[CURRENT_MODAL_IDX];
  if (!d) return;
  if (typeof QUIET_EDIT !== "undefined" && QUIET_EDIT._dirty) {
    const ok = await uiConfirm("Обновить?",
      "Есть несохранённые изменения в режиме тишины. Они будут потеряны.",
      {danger: true, okText: "Обновить"});
    if (!ok) return;
  }
  // v1.27.0: staging DP — тоже сбрасываем при обновлении.
  if (typeof DPS_EDIT !== "undefined" && DPS_EDIT && dpsIsDirty()) {
    const ok = await uiConfirm("Обновить?",
      "Есть несохранённые изменения в DP. Они будут потеряны.",
      {danger: true, okText: "Обновить"});
    if (!ok) return;
  }
  DPS_EDIT = null;
  const btn = document.getElementById("modal-refresh-btn");
  if (btn) btn.classList.add("spinning");
  delete DEVICE_HISTORY_CACHE[d.name];
  delete DEVICE_LATENCY_CACHE[d.name];
  delete DEVICE_AVG_LATENCY_CACHE[d.name];
  document.getElementById("modal-body").innerHTML = "";
  // v1.28.33: dpsInitEdit нужен здесь — renderModalVolatile больше
  // его не вызывает (фикс 1.28.32). Без этого секция DP висела
  // в placeholder «загрузка…» после 🔄.
  dpsInitEdit(d, true);
  renderModalVolatile(d);
  try {
    const r = await fetch(`/api/device/${encodeURIComponent(d.name)}/history?hours=24&limit=100`);
    const hist = await r.json();
    if (hist.history) { DEVICE_HISTORY_CACHE[d.name] = hist.history; d.history = hist.history; }
    const r2 = await fetch(`/api/device/${encodeURIComponent(d.name)}/latency?hours=24&limit=2000`);
    const lat = await r2.json();
    if (lat.latency) { DEVICE_LATENCY_CACHE[d.name] = lat.latency; }
    const r3 = await fetch(`/api/device/${encodeURIComponent(d.name)}/avg_latency?latency_seconds=86400`);
    const avg = await r3.json();
    if (avg.avg !== undefined) { DEVICE_AVG_LATENCY_CACHE[d.name] = avg; }
  } catch (e) { console.warn("refreshModal", e); }
  // v1.28.34: модалку могли закрыть/сменить, пока шли запросы.
  if (CURRENT_MODAL_NAME !== d.name) return;
  renderModalVolatile(d);
  if (btn) btn.classList.remove("spinning");
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    // v1.28.9: закрываем ТОЛЬКО верхнюю модалку, по одной за Esc.
    // Порядок = z-index сверху вниз (ui-alert → ui-prompt → ui-confirm
    // → dps-fill → dps-preview → edit-device → ... → device).
    // Это защищает от случая, когда closeDpsFill(null) асинхронно
    // открывает ui-confirm (dirty), и device-модалка закрылась бы
    // одновременно — пользователь запутается.
    const _esc_chain = [
      { id: "ui-alert-overlay",         close: () => closeUiAlert(null) },
      { id: "ui-prompt-overlay",        close: () => closeUiPrompt(null, null) },
      { id: "ui-confirm-overlay",       close: () => closeUiConfirm(null, false) },
      { id: "dps-fill-overlay",         close: () => closeDpsFill(null, "esc") },
      { id: "dps-preview-overlay",      close: () => closeDpsPreview() },
      { id: "edit-device-overlay",      close: () => closeEditDevice() },
      { id: "db-cleanup-overlay",       close: () => closeDbCleanup() },
      { id: "timeline-cleanup-overlay", close: () => closeTimelineCleanup() },
      { id: "cloud-modal-overlay",      close: () => closeCloudModal() },
      { id: "preview-overlay",          close: () => closePreview() },
      { id: "modal-overlay",            close: () => closeModal() },
    ];
    for (const _step of _esc_chain) {
      const _el = document.getElementById(_step.id);
      if (_el && _el.classList.contains("open")) {
        _step.close();
        return;
      }
    }
  }
  if (e.key === "Enter" && document.getElementById("ui-confirm-overlay").classList.contains("open")) {
    closeUiConfirm(null, true);
  }
  if (e.key === "Enter" && document.getElementById("ui-prompt-overlay").classList.contains("open")) {
    submitUiPrompt();
  }
  if (e.key === "Enter" && document.getElementById("edit-device-overlay").classList.contains("open")) {
    const btn = document.getElementById("edit-device-submit");
    if (btn && !btn.disabled) submitEditDevice();
  }
});

// ==================== EDIT DEVICE ====================
function editDevice(name) {
  const d = LAST_DEVICES.find(x => x.name === name);
  if (!d) return;
  EDIT_DEVICE_NAME = name;

  document.getElementById("edit-device-title").textContent =
    `Редактирование: ${d.friendly_name || name}`;

  // v1.28.12: поля активны сразу, без чекбоксов.
  document.getElementById("edit-device-ip").value = d.ip || "";
  document.getElementById("edit-device-version").value = d.version || "3.3";
  document.getElementById("edit-device-key").value = "";
  // v1.28.26: type="text" — пользователь должен видеть, что вводит.
  // Текущий key показывается отдельно (👁 Показать в модалке устройства).
  document.getElementById("edit-device-key").type = "text";

  // v1.28.12: battery — селект (true/false).
  document.getElementById("edit-device-battery").value =
    d.battery_powered ? "true" : "false";

  // v1.28.12: enabled — кнопка в «Опасные действия».
  _editDeviceUpdateEnabledBtn(d.enabled !== false);

  // v1.28.24: страховка на случай, если кнопка удаления осталась
  // в disabled+спиннер от прошлого удаления (другого устройства,
  // без F5). См. фикс B в editDeviceDelete().
  const _delBtnReset = document.getElementById("edit-device-delete-btn");
  if (_delBtnReset) {
    _delBtnReset.disabled = false;
    _delBtnReset.textContent = "🗑 Удалить";
  }

  const errEl = document.getElementById("edit-device-error");
  errEl.style.display = "none";
  errEl.innerHTML = "";
  errEl.style.background = "";
  errEl.style.color = "";
  errEl.style.borderColor = "";
  const rawWrap = document.getElementById("edit-device-raw-error");
  rawWrap.style.display = "none";
  document.getElementById("edit-device-raw-error-text").textContent = "";

  const btn = document.getElementById("edit-device-submit");
  btn.classList.add("primary");
  btn.textContent = "💾 Сохранить";

  updateEditSubmitState();
  document.getElementById("edit-device-overlay").classList.add("open");
}

// v1.28.12: обновить кнопку вкл/откл в Опасных действиях.
function _editDeviceUpdateEnabledBtn(isEnabled) {
  const btn = document.getElementById("edit-device-toggle-btn");
  if (!btn) return;
  if (isEnabled) {
    btn.textContent = "⛔ Отключить";
    btn.className = "danger";
    btn.dataset.action = "disable";
  } else {
    btn.textContent = "✅ Включить";
    btn.className = "primary";
    btn.dataset.action = "enable";
  }
}

// v1.28.12: toggle enabled из editDevice.
async function editDeviceToggleEnabled() {
  if (!EDIT_DEVICE_NAME) return;
  const d = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
  if (!d) return;
  const btn = document.getElementById("edit-device-toggle-btn");
  const action = btn?.dataset.action || "disable";
  const newEnabled = (action === "enable");

  const ok = await uiConfirm(
    newEnabled ? "Включить устройство?" : "Отключить устройство?",
    newEnabled
      ? `Включить "${d.friendly_name || d.name}"?\n\nBridge начнёт опрашивать устройство.`
      : `Отключить "${d.friendly_name || d.name}"?\n\nBridge перестанет опрашивать. HA покажет unavailable через ~2 мин. Автоматизации не сломаются.`,
    { danger: !newEnabled, okText: newEnabled ? "Включить" : "Отключить" }
  );
  if (!ok) return;

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> ' + (newEnabled ? "Включение…" : "Отключение…");
  }
  try {
    const r = await fetch(`/api/device/${encodeURIComponent(EDIT_DEVICE_NAME)}/config`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ changes: { enabled: newEnabled } })
    });
    const data = await r.json();
    if (data.ok) {
      await fetchStatus();
      const _updated = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
      if (_updated) _editDeviceUpdateEnabledBtn(_updated.enabled !== false);
    } else {
      uiAlert("Ошибка", data.error || "неизвестная ошибка", "error");
    }
  } catch (e) {
    uiAlert("Ошибка сети", e.message, "error");
  } finally {
    // v1.28.30: восстанавливаем кнопку ВСЕГДА — и innerHTML тоже,
    // иначе при исключении/ошибке кнопка залипает со спиннером
    // «Включение…»/«Отключение…».
    const _tb = document.getElementById("edit-device-toggle-btn");
    if (_tb) {
      _tb.disabled = false;
      const _d2 = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
      if (_d2) _editDeviceUpdateEnabledBtn(_d2.enabled !== false);
    }
  }
}

// v1.28.12: удаление из editDevice (двойное подтверждение).
async function editDeviceDelete() {
  if (!EDIT_DEVICE_NAME) return;
  const d = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
  if (!d) return;
  const friendly = d.friendly_name || EDIT_DEVICE_NAME;

  // 1-е подтверждение: пугаем.
  const ok1 = await uiConfirm(
    "⚠️ Удалить устройство?",
    `Удалить "${friendly}" из конфига bridge?\n\n` +
    `Bridge очистит Discovery-конфиги. HA удалит сущности.\n\n` +
    `Действие НЕОБРАТИМО.`,
    { danger: true, okText: "Продолжить" }
  );
  if (!ok1) return;

  // 2-е подтверждение: точный вопрос.
  const ok2 = await uiConfirm(
    "⚠️ Подтвердите удаление",
    `Точно удалить "${friendly}"?\n\n` +
    `Введите OK в уме и нажмите «Удалить навсегда».`,
    { danger: true, okText: "Удалить навсегда", cancelText: "Отмена" }
  );
  if (!ok2) return;

  // v1.28.23: фиксируем имя ДО closeEditDevice — иначе потеряем.
  const _delName = EDIT_DEVICE_NAME;

  // v1.28.23: дизейбл кнопки — предотвращает двойной клик.
  const _delBtn = document.getElementById("edit-device-delete-btn");
  if (_delBtn) {
    _delBtn.disabled = true;
    _delBtn.innerHTML = '<span class="spin"></span> удаление…';
  }

  try {
    const r = await fetch(`/api/device/${encodeURIComponent(_delName)}/delete`, {
      method: "POST"
    });
    const data = await r.json();
    if (data.ok) {
      closeEditDevice();
      closeModal();
      delete DEVICE_HISTORY_CACHE[_delName];
      delete REVEALED_KEYS[_delName];
      // v1.28.23: fetchStatus всегда — обновит LAST_DEVICES.
      fetchStatus();
      return;
    }
    // v1.28.23: "device not found" — уже удалено (двойной клик).
    // Не показываем uiAlert, просто закрываем и обновляем.
    if (data.error && String(data.error).toLowerCase().includes("not found")) {
      closeEditDevice();
      closeModal();
      fetchStatus();
      return;
    }
    uiAlert("Ошибка", "Не удалось удалить: " + (data.error || "unknown"), "error");
    // v1.28.23: обновить статус даже при ошибке — UI не должен
    // держать ghost-устройство.
    fetchStatus();
  } catch (e) {
    uiAlert("Ошибка сети", e.message, "error");
    fetchStatus();
  } finally {
    // v1.28.24: восстанавливаем кнопку ВСЕГДА, независимо от того,
    // открыта ли модалка. Иначе при успешном удалении closeEditDevice()
    // снимает .open ДО finally, условие становится ложным, и кнопка
    // остаётся disabled=true со спиннером. При следующем editDevice()
    // (без F5) клик по ней не срабатывает — editDeviceDelete() не
    // вызывается, спиннер крутится вечно. Обновление страницы лечило
    // именно это (сбрасывало DOM-состояние кнопки).
    const _b = document.getElementById("edit-device-delete-btn");
    if (_b) {
      _b.disabled = false;
      _b.textContent = "🗑 Удалить";
    }
  }
}

function closeEditDevice(evt) {
  if (evt && evt.target && evt.target.id !== "edit-device-overlay") return;
  EDIT_DEVICE_NAME = null;
  document.getElementById("edit-device-overlay").classList.remove("open");
}

// v1.28.13: _bindEditKeyClickClear удалён — поле всегда пустое
// (только для ввода нового key). Текущий key — в showDevice.

// v1.28.12: подсчёт изменений в editDevice.
// v1.28.14: любое непустое изменение = изменение. Валидность — при submit
// (IP — блокируется, key — отдаётся bridge, он сам скажет invalid).
function _editDeviceCountChanges() {
  const d = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
  if (!d) return { count: 0, changes: {} };
  const changes = {};

  // v1.28.14: любое непустое значение, отличающееся от исходного.
  const ip = document.getElementById("edit-device-ip").value.trim();
  if (ip && ip !== (d.ip || "")) {
    changes.ip = ip;
  }

  const ver = document.getElementById("edit-device-version").value;
  if (ver && ver !== (d.version || "")) {
    changes.version = ver;
  }

  // v1.28.14: key — любое непустое. Длину проверяет bridge (_is_valid_key: 10–50).
  const key = document.getElementById("edit-device-key").value;
  if (key && key.length > 0) {
    changes.local_key = key;
  }

  const bat = document.getElementById("edit-device-battery").value;
  const _cur_bat = d.battery_powered ? "true" : "false";
  if (bat !== _cur_bat) {
    changes.battery_powered = (bat === "true");
  }

  return { count: Object.keys(changes).length, changes: changes };
}

// v1.28.13: подсветка изменённых полей — жёлтая рамка + ● у лейбла.
function _editDeviceMarkModified() {
  if (!EDIT_DEVICE_NAME) return;
  const d = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
  if (!d) return;
  const setMod = (id, isMod) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("field-modified", !!isMod);
  };
  const setLabelMod = (id, isMod) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("edit-label-modified", !!isMod);
  };

  // v1.28.14: IP — подсветка при любом изменении, красная подсказка при невалидном.
  const ip = document.getElementById("edit-device-ip").value.trim();
  const ipChanged = ip && ip !== (d.ip || "");
  setMod("edit-device-ip", ipChanged);
  setLabelMod("edit-device-ip-label", ipChanged);
  const ipErr = document.getElementById("edit-device-ip-error");
  if (ipErr) {
    if (ipChanged && !_isValidIPv4(ip)) {
      ipErr.textContent = "⚠️ Некорректный IP-адрес (0-255, без ведущих нулей)";
      ipErr.style.display = "block";
    } else if (ipChanged && _ipInUse(ip, d.ip)) {
      ipErr.textContent = "⚠️ Этот IP уже используется другим устройством";
      ipErr.style.display = "block";
    } else {
      ipErr.style.display = "none";
    }
  }

  // Version
  const ver = document.getElementById("edit-device-version").value;
  const verMod = ver && ver !== (d.version || "");
  setMod("edit-device-version", verMod);
  setLabelMod("edit-device-version-label", verMod);

  // v1.28.14: key — подсветка при любом непустом. Длину решает bridge.
  const key = document.getElementById("edit-device-key").value;
  const keyMod = key && key.length > 0;
  setMod("edit-device-key", keyMod);
  setLabelMod("edit-device-key-label", keyMod);

  // Battery
  const bat = document.getElementById("edit-device-battery").value;
  const cur_bat = d.battery_powered ? "true" : "false";
  const batMod = bat !== cur_bat;
  setMod("edit-device-battery", batMod);
  setLabelMod("edit-device-battery-label", batMod);
}

function updateEditSubmitState() {
  if (!EDIT_DEVICE_NAME) return;
  const d = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
  if (!d) return;

  const { count, changes } = _editDeviceCountChanges();

  const btn = document.getElementById("edit-device-submit");
  btn.disabled = (count === 0);

  const ind = document.getElementById("edit-device-changed");
  if (ind) {
    ind.textContent = count > 0
      ? `Изменено: ${count} ${count === 1 ? "поле" : (count < 5 ? "поля" : "полей")}`
      : "Изменено: 0 полей";
    ind.classList.toggle("dirty", count > 0);
  }

  // v1.28.13: подсветка изменённых полей.
  _editDeviceMarkModified();
}

async function submitEditDevice() {
  if (!EDIT_DEVICE_NAME) return;
  const d = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
  if (!d) return;

  const errEl = document.getElementById("edit-device-error");
  const rawWrap = document.getElementById("edit-device-raw-error");
  const rawText = document.getElementById("edit-device-raw-error-text");
  errEl.style.display = "none";
  errEl.innerHTML = "";
  errEl.style.background = "";
  errEl.style.color = "";
  errEl.style.borderColor = "";
  rawWrap.style.display = "none";
  rawText.textContent = "";

  const { count, changes } = _editDeviceCountChanges();

  // v1.28.14: блокируем только IP (универсальная валидация IPv4).
  // Длину key решает bridge (_is_valid_key: 10–50). Если Tuya выдаст
  // нестандартный key — меняем только bridge, WebUI отдаёт как есть.
  if (changes.ip !== undefined && !_isValidIPv4(changes.ip)) {
    errEl.style.display = "block";
    errEl.textContent = "Некорректный IP-адрес (0-255, без ведущих нулей)";
    return;
  }
  if (changes.ip !== undefined && _ipInUse(changes.ip, d.ip)) {
    errEl.style.display = "block";
    errEl.textContent = "Этот IP уже используется другим устройством";
    return;
  }

  if (count === 0) {
    closeEditDevice();
    return;
  }

  const btn = document.getElementById("edit-device-submit");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> сохранение…';

  try {
    const r = await fetch(`/api/device/${encodeURIComponent(EDIT_DEVICE_NAME)}/config`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ changes })
    });
    const data = await r.json();
    if (data.ok) {
      errEl.style.display = "block";
      errEl.style.background = "rgba(46,160,67,0.08)";
      errEl.style.color = "var(--green)";
      errEl.style.borderColor = "rgba(46,160,67,0.25)";
      errEl.innerHTML = "✅ Изменения сохранены. Bridge применит их при следующем reconnect.";
      btn.textContent = "✅ Сохранено";
      btn.classList.remove("primary");
      fetchStatus();
      document.getElementById("edit-device-key").value = "";
      setTimeout(updateEditSubmitState, 100);
      btn.disabled = false;
    } else {
      errEl.style.display = "block";
      errEl.style.background = "rgba(215,58,73,0.08)";
      errEl.style.color = "var(--red)";
      errEl.style.borderColor = "rgba(215,58,73,0.25)";
      errEl.innerHTML = escapeHtml(data.error || "Ошибка сохранения");
      if (data.error_raw && data.error_raw !== data.error) {
        rawWrap.style.display = "block";
        rawText.textContent = data.error_raw;
      }
      btn.disabled = false;
      btn.textContent = "💾 Сохранить";
      updateEditSubmitState();
    }
  } catch (e) {
    errEl.style.display = "block";
    errEl.style.background = "rgba(215,58,73,0.08)";
    errEl.style.color = "var(--red)";
    errEl.style.borderColor = "rgba(215,58,73,0.25)";
    errEl.textContent = "Ошибка сети: " + e.message;
    btn.disabled = false;
    btn.textContent = "💾 Сохранить";
    updateEditSubmitState();
  }
}

function logLevelPass(level, source) {
  // v1.28.86: уровни работают одинаково для Bridge и WebUI.
  return (LEVEL_ORDER[level || "INFO"] || 1) >= (LEVEL_ORDER[LOG_LEVEL_FILTER] || 0);
}
function logSourcePass(item) {
  // v1.21.3: фильтр по source
  if (!item || !item.source) return LOG_SOURCE === "bridge";  // legacy — считаем bridge
  return item.source === LOG_SOURCE;
}
function logTimePass(item) {
  if (LOG_RANGE_SECONDS === 0) return true;
  let t = parseLogTs(item.msg);
  if (t === null || t === undefined) t = item.ts;
  t = parseInt(t, 10);
  if (isNaN(t) || t <= 0) return true;
  const now = Math.floor(Date.now() / 1000);
  return (now - t) <= LOG_RANGE_SECONDS;
}
function logMatchesSearch(msg) {
  if (!SEARCH_TERM) return true;
  return (msg || "").toLowerCase().includes(SEARCH_TERM.toLowerCase());
}
function renderLogsFromBuffer() {
  const frag = document.createDocumentFragment();
  let anyShown = false;
  for (const item of LOG_BUFFER) {
    if (!logSourcePass(item)) continue;
    if (!logLevelPass(item.level, item.source)) continue;
    if (!logTimePass(item)) continue;
    if (!logMatchesSearch(item.msg)) continue;
    anyShown = true;
    const div = document.createElement("div");
    div.className = "line lvl-" + item.level;
    div.dataset.level = item.level;
    div.dataset.msg = item.msg;
    div.dataset.seq = item.seq;
    div.dataset.ts = item.ts;
    if (SEARCH_TERM) {
      const msg = item.msg;
      const idx = msg.toLowerCase().indexOf(SEARCH_TERM.toLowerCase());
      if (idx >= 0) {
        div.innerHTML = `${escapeHtml(msg.slice(0, idx))}<mark>${escapeHtml(msg.slice(idx, idx + SEARCH_TERM.length))}</mark>${escapeHtml(msg.slice(idx + SEARCH_TERM.length))}`;
      } else div.textContent = msg;
    } else div.textContent = item.msg;
    frag.appendChild(div);
  }
  logsEl.innerHTML = "";
  // v1.21.3: placeholder для пустого WebUI-лога
  if (!anyShown && LOG_SOURCE === "webui") {
    const ph = document.createElement("div");
    ph.className = "muted";
    ph.style.cssText = "padding:16px; text-align:center; color:var(--muted);";
    ph.textContent = "Пока нет записей WebUI";
    logsEl.appendChild(ph);
  } else {
    logsEl.appendChild(frag);
  }
  SEARCH_MATCHES = [];
  if (SEARCH_TERM) {
    logsEl.querySelectorAll(".line").forEach(line => {
      if ((line.dataset.msg || "").toLowerCase().includes(SEARCH_TERM.toLowerCase())) SEARCH_MATCHES.push(line);
    });
  }
  document.getElementById("find-info").textContent = SEARCH_TERM ? `${SEARCH_MATCHES.length}` : "";
  document.getElementById("find-next-btn").disabled = SEARCH_MATCHES.length === 0;
  document.getElementById("find-prev-btn").disabled = SEARCH_MATCHES.length === 0;
  if (!logPaused && !userScrolledUp) logsEl.scrollTop = logsEl.scrollHeight;
  _updateLogLevelButtons();
}

// v1.28.85: скрываем кнопки уровней, для которых в логе нет сообщений.
function _updateLogLevelButtons() {
  const wrap = document.getElementById("log-level-filter");
  if (!wrap) return;
  const present = new Set();
  for (const item of LOG_BUFFER) {
    if (!logSourcePass(item)) continue;
    present.add(String(item.level || "INFO").toUpperCase());
  }
  let any = false;
  wrap.querySelectorAll("button[data-level]").forEach(btn => {
    const lv = btn.getAttribute("data-level");
    // v1.28.87: выбранный уровень показываем всегда (иначе при переключении
    // источника «терялся», т.к. в другом логе нет сообщений этого уровня).
    const show = present.has(lv) || lv === LOG_LEVEL_FILTER;
    btn.style.display = show ? "" : "none";
    if (show) any = true;
  });
  const label = wrap.querySelector("span.muted");
  if (label) label.style.display = any ? "" : "none";
}
function appendLog(item) {
  // v1.25.12: seq per-source. Сравниваем с seq текущего источника.
  if (item.seq <= _getLastSeq()) return;
  if (item.source === LOG_SOURCE) _setLastSeq(item.seq);
  LOG_BUFFER.push(item);
  if (LOG_BUFFER.length > LOG_BUFFER_MAX) LOG_BUFFER.shift();
  if (logSourcePass(item) && logLevelPass(item.level, item.source) && logTimePass(item) && logMatchesSearch(item.msg)) {
    const div = document.createElement("div");
    div.className = "line lvl-" + item.level;
    div.dataset.level = item.level;
    div.dataset.msg = item.msg;
    div.dataset.seq = item.seq;
    div.dataset.ts = item.ts;
    if (SEARCH_TERM) {
      const msg = item.msg;
      const idx = msg.toLowerCase().indexOf(SEARCH_TERM.toLowerCase());
      if (idx >= 0) {
        div.innerHTML = `${escapeHtml(msg.slice(0, idx))}<mark>${escapeHtml(msg.slice(idx, idx + SEARCH_TERM.length))}</mark>${escapeHtml(msg.slice(idx + SEARCH_TERM.length))}`;
      } else div.textContent = msg;
    } else div.textContent = item.msg;
    logsEl.appendChild(div);
    while (logsEl.children.length > 2000) logsEl.removeChild(logsEl.firstChild);
    if (!logPaused && !userScrolledUp) logsEl.scrollTop = logsEl.scrollHeight;
  }
}
function toggleLevel(level) {
  // v1.28.88: уровень запоминается ОТДЕЛЬНО для каждого источника.
  LOG_LEVEL_FILTER = level;
  _saveLogLevel(LOG_SOURCE, level);
  document.querySelectorAll(".logs-toolbar button[data-level]").forEach(b => b.classList.toggle("active", b.dataset.level === level));
  renderLogsFromBuffer();
}
// v1.21.3: переключение источника логов [Bridge] / [WebUI]
function switchLogSource(src) {
  if (LOG_SOURCE === src) return;
  // v1.22.1: закрываем EventSource сразу, чтобы старый поток
  // не наливал записи, пока идёт loadLogHistory для нового source.
  if (LOG_SSE_ES) {
    try { LOG_SSE_ES.close(); } catch(e){}
    LOG_SSE_ES = null;
  }
  // P3 1.22.0: сохраняем в localStorage
  try { localStorage.setItem("tuya_webui_log_source", src); } catch(e){}
  // P2 1.22.0: токен — защита от race при быстром переключении
  const myToken = ++LOG_SOURCE_TOKEN;

  // v1.28.88: у каждого источника — свой запомненный уровень.
  if (LOG_SOURCE === "bridge" || LOG_SOURCE === "webui") {
    _saveLogLevel(LOG_SOURCE, LOG_LEVEL_FILTER);
  }
  LOG_SOURCE = src;
  LOG_LEVEL_FILTER = _LOG_LEVELS[src] || "INFO";

  document.querySelectorAll(".log-source-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.src === src);
  });

  const lvlWrap = document.getElementById("log-level-filter");
  // v1.28.86: уровни одинаковы для обоих источников — просто показываем панель.
  if (lvlWrap) lvlWrap.classList.remove("hidden");
  document.querySelectorAll(".logs-toolbar button[data-level]").forEach(b => {
    b.classList.toggle("active", b.dataset.level === LOG_LEVEL_FILTER);
  });

  if (logPaused) {
    logPaused = false;
    // v1.23.8: сохраняем сброс паузы при смене источника.
    _logUiSave();
    const pauseBtn = document.getElementById("pause-btn");
    if (pauseBtn) {
      pauseBtn.textContent = "⏸ Пауза";
      pauseBtn.classList.remove("active");
    }
    const oldBanner = document.getElementById("logs-paused-banner");
    if (oldBanner) oldBanner.remove();
  }

  // P1 1.22.0: НЕ очищаем LOG_BUFFER и НЕ сбрасываем lastSeq.
  // loadLogHistory() дополнит буфер по дедупликации seq,
  // затем переподключим SSE с текущим глобальным lastSeq.
  loadLogHistory().then((maxSeq) => {
    if (myToken !== LOG_SOURCE_TOKEN) return;
    // v1.25.12: seq per-source.
    _setLastSeq(maxSeq > 0 ? maxSeq : 1);
    connectSSE();
  });
}

function setLogRange(seconds) {
  LOG_RANGE_SECONDS = parseInt(seconds, 10) || 0;
  // v1.23.8: сохраняем диапазон, чтобы он выжил при смене вкладки.
  _logUiSave();
  // v1.28.92: и в localStorage — чтобы не сбрасывался на «1 час».
  try { localStorage.setItem(LOG_RANGE_KEY, String(LOG_RANGE_SECONDS)); } catch (e) {}
  // v1.21.3.fix3: селектим ТОЛЬКО кнопки с data-range, иначе
  // захватываем .log-source-btn (Bridge/WebUI) и подсвечиваем оба.
  document.querySelectorAll(".logs-toolbar button[data-range]").forEach(b => {
    const r = parseInt(b.dataset.range, 10) || 0;
    b.classList.toggle("active", r === LOG_RANGE_SECONDS);
  });
  renderLogsFromBuffer();
}
function pauseLogs() {
  logPaused = !logPaused;
  // v1.23.8: сохраняем состояние, чтобы пауза выжила при смене вкладки.
  _logUiSave();
  const btn = document.getElementById("pause-btn");
  btn.textContent = logPaused ? "▶ Продолжить" : "⏸ Пауза";
  btn.classList.toggle("active", logPaused);
  const oldBanner = document.getElementById("logs-paused-banner");
  if (logPaused) {
    if (!oldBanner) {
      const b = document.createElement("div");
      b.id = "logs-paused-banner";
      b.className = "logs-paused-banner";
      b.textContent = "⏸ Логи на паузе — новые записи не отображаются";
      logsEl.parentNode.insertBefore(b, logsEl);
    }
  } else {
    if (oldBanner) oldBanner.remove();
    renderLogsFromBuffer();
  }
}
function doSearch() {
  SEARCH_TERM = document.getElementById("log-search").value.trim();
  SEARCH_CURRENT = -1;
  // v1.23.8: сохраняем поиск.
  _logUiSave();
  renderLogsFromBuffer();
}
// v1.23.6: кнопка «×» справа от input поиска логов.
function clearLogSearch() {
  const el = document.getElementById("log-search");
  if (el) el.value = "";
  SEARCH_TERM = "";
  SEARCH_CURRENT = -1;
  // v1.23.8: сохраняем состояние.
  _logUiSave();
  renderLogsFromBuffer();
}
function findNext() {
  if (SEARCH_MATCHES.length === 0) return;
  SEARCH_CURRENT = (SEARCH_CURRENT + 1) % SEARCH_MATCHES.length;
  SEARCH_MATCHES.forEach(el => el.classList.remove("current-match"));
  // v1.28.25: защита от race с renderLogsFromBuffer — элемент мог быть удалён из DOM.
  const el = SEARCH_MATCHES[SEARCH_CURRENT];
  if (!el || !el.isConnected) { SEARCH_CURRENT = -1; return; }
  el.classList.add("current-match");
  el.scrollIntoView({block: "center", behavior: "smooth"});
}
function findPrev() {
  if (SEARCH_MATCHES.length === 0) return;
  SEARCH_CURRENT = (SEARCH_CURRENT - 1 + SEARCH_MATCHES.length) % SEARCH_MATCHES.length;
  SEARCH_MATCHES.forEach(el => el.classList.remove("current-match"));
  const el = SEARCH_MATCHES[SEARCH_CURRENT];
  if (!el || !el.isConnected) { SEARCH_CURRENT = -1; return; }
  el.classList.add("current-match");
  el.scrollIntoView({block: "center", behavior: "smooth"});
}
function downloadLogs() {
  const lines = [];
  logsEl.querySelectorAll(".line").forEach(el => lines.push(el.dataset.msg || ""));
  const blob = new Blob([lines.join("\n")], {type: "text/plain"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url;
  a.download = `${LOG_SOURCE || "bridge"}-log-${new Date().toISOString().slice(0,10)}.txt`;
  a.click();
  // v1.19: revokeObjectURL сразу после click() иногда не даёт
  // браузеру начать скачивание. Даём 1 секунду форы.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function scrollLogsToBottom() {
  userScrolledUp = false;
  _logUiSave();
  logsEl.scrollTop = logsEl.scrollHeight;
  document.getElementById("scroll-down-btn").classList.remove("visible");
}
logsEl.addEventListener("scroll", () => {
  const atBottom = logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight < 50;
  const prev = userScrolledUp;
  userScrolledUp = !atBottom;
  // v1.23.8: сохраняем только при смене состояния.
  if (prev !== userScrolledUp) _logUiSave();
  const btn = document.getElementById("scroll-down-btn");
  if (userScrolledUp) btn.classList.add("visible");
  else btn.classList.remove("visible");
});

let SSE_RECONNECT_DELAY = 3000;
let LOG_SSE_ES = null;  // v1.21.4: текущий EventSource логов
function connectSSE() {
  // v1.21.4: закрываем предыдущий EventSource, чтобы не плодить
  // соединения при переключении источника логов.
  if (LOG_SSE_ES) {
    try { LOG_SSE_ES.close(); } catch (e) {}
    LOG_SSE_ES = null;
  }
  const es = new EventSource(`/api/logs/stream?since=${_getLastSeq()}&v=${Date.now()}`);
  LOG_SSE_ES = es;
  es.onmessage = (e) => { try { const item = JSON.parse(e.data); if (item?.seq) appendLog(item); } catch {} };
  es.onerror = () => {
    es.close();
    if (LOG_SSE_ES === es) LOG_SSE_ES = null;
    SSE_RECONNECT_DELAY = Math.min(SSE_RECONNECT_DELAY * 2, 30000);
    setTimeout(connectSSE, SSE_RECONNECT_DELAY);
  };
  es.onopen = () => { SSE_RECONNECT_DELAY = 3000; };
}
async function loadLogHistory() {
  try {
    const src = LOG_SOURCE || "bridge";
    const r = await fetch(`/api/logs/history?tail=1000&source=${src}&v=${Date.now()}`);
    const data = await r.json();
    // P1 1.22.0: дедупликация по seq, буфер не сбрасываем.
    const existing = new Set(LOG_BUFFER.map(x => x.seq));
    let maxSeq = 0;
    for (const item of (data.logs || [])) {
      if (item.seq > maxSeq) maxSeq = item.seq;
      if (existing.has(item.seq)) continue;
      LOG_BUFFER.push(item);
    }
    LOG_BUFFER.sort((a, b) => a.seq - b.seq);
    if (LOG_BUFFER.length > LOG_BUFFER_MAX) {
      LOG_BUFFER = LOG_BUFFER.slice(-LOG_BUFFER_MAX);
    }
    renderLogsFromBuffer();
    return maxSeq;
  } catch (e) { return 0; }
}

async function doCleanup() {
  const btn = document.getElementById("cleanup-btn");
  const res = document.getElementById("cleanup-result");
  const ok = await uiConfirm(
    "Очистить Discovery в Home Assistant?",
    "Bridge удалит свои retained MQTT-топики Home Assistant Discovery "
    + "(config-сообщения) для всех устройств, затем подождёт и опубликует их заново.\n\n"
    + "Что произойдёт: так как это retained-топики, Home Assistant на короткое "
    + "время удалит сущности (retained-конфиг очищен) и создаст их снова после "
    + "повторной публикации. «Фантомные» сущности удалённых DP при этом пропадут.\n\n"
    + "Дополнительно bridge сам чистит «зависшие» (orphan) retained-топики "
    + "Discovery при каждом старте — если сущность удалили из конфига, она уйдёт "
    + "автоматически, без этой кнопки.\n\n"
    + "Конфиг устройств и данные не трогаются.",
    {okText: "Очистить"});
  if (!ok) return;
  btn.disabled = true; res.innerHTML = '<span class="spin"></span>';
  try { await fetch("/api/cleanup", { method: "POST" }); res.textContent = "✅ отправлено"; }
  catch (e) { res.textContent = "❌ " + e.message; }
  btn.disabled = false;
  setTimeout(() => res.textContent = "", 5000);
}

// ==================== LATENCY REFRESH ====================
async function doLatencyRefresh() {
  const btn = document.getElementById("latency-btn");
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Замер…';

  try {
    const r = await fetch("/api/latency/refresh", { method: "POST" });
    const data = await r.json();
    if (!data.ok) {
      btn.textContent = "❌ " + (data.error || "занято");
      setTimeout(() => { btn.textContent = oldText; btn.disabled = false; }, 3000);
      return;
    }
    const poll = async () => {
      try {
        const s = await (await fetch("/api/latency/refresh/progress")).json();
        if (s.running) {
          const pct = s.total > 0 ? Math.round((s.current / s.total) * 100) : 0;
          btn.innerHTML = `<span class="spin"></span> ${s.current}/${s.total} (${pct}%)`;
          setTimeout(poll, 800);
        } else {
          btn.textContent = "✅ Готово";
          setTimeout(fetchStatus, 500);
          if (VIEW === "analytics" && ANALYTICS_ENABLED) loadAnalytics();
          setTimeout(() => { btn.textContent = oldText; btn.disabled = false; }, 5000);
        }
      } catch (e) {
        setTimeout(poll, 1500);
      }
    };
    poll();
  } catch (e) {
    btn.textContent = "❌ " + e.message;
    setTimeout(() => { btn.textContent = oldText; btn.disabled = false; }, 3000);
  }
}

// ==================== DB CLEANUP ====================
function openDbCleanup() {
  const res = document.getElementById("db-cleanup-result");
  if (res) res.textContent = "";
  updateDbCleanupForm();
  document.getElementById("db-cleanup-overlay").classList.add("open");
}
function closeDbCleanup(evt) {
  if (evt && evt.target && evt.target.id !== "db-cleanup-overlay") return;
  document.getElementById("db-cleanup-overlay").classList.remove("open");
}
// v1.28.75: понятный выбор — готовые периоды / «своё» / удалить всё.
function updateDbCleanupForm() {
  const scope = document.querySelector('input[name="db-cleanup-scope"]:checked')?.value || "3";
  const f = document.getElementById("dbc-age-fields");
  if (f) f.style.display = scope === "custom" ? "block" : "none";
}
async function doDbCleanup() {
  const btn = document.getElementById("db-cleanup-btn");
  const res = document.getElementById("db-cleanup-result");
  const scope = document.querySelector('input[name="db-cleanup-scope"]:checked')?.value || "3";
  let body, msg;
  if (scope === "all") {
    body = { purge_all: true };
    msg = "Удалить ВСЮ историю задержек и снапшоты состояний?";
  } else if (scope === "custom") {
    const days = parseInt(document.getElementById("db-keep-days").value) || 0;
    const hours = parseInt(document.getElementById("db-keep-hours").value) || 0;
    if (days === 0 && hours === 0) { res.textContent = "❌ Укажи дни или часы"; return; }
    body = { keep_days: days, keep_hours: hours };
    msg = `Удалить данные старше ${days}д ${hours}ч?`;
  } else {
    const d = parseInt(scope, 10) || 0;
    body = { keep_days: d, keep_hours: 0 };
    msg = `Удалить данные старше ${d} дн.?`;
  }
  const ok = await uiConfirm("Очистить БД", msg + " Действие необратимо.", {danger:true, okText:"Удалить"});
  if (!ok) return;
  btn.disabled = true; res.innerHTML = '<span class="spin"></span> удаление…';
  try {
    const r = await fetch("/api/db/cleanup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await r.json();
    if (data.ok) { res.textContent = `✅ Удалено: ${data.deleted}`; setTimeout(closeDbCleanup, 1500); }
    else res.textContent = "❌ " + (data.error || "ошибка");
  } catch (e) { res.textContent = "❌ " + e.message; }
  btn.disabled = false;
}

// ==================== AUDIT (config history) CLEANUP ====================
function openAuditCleanup() {
  const res = document.getElementById("audit-cleanup-result");
  if (res) res.textContent = "";
  updateAuditCleanupForm();
  document.getElementById("audit-cleanup-overlay").classList.add("open");
}
function closeAuditCleanup(evt) {
  if (evt && evt.target && evt.target.id !== "audit-cleanup-overlay") return;
  document.getElementById("audit-cleanup-overlay").classList.remove("open");
}
// v1.28.75: выбор периода — пресеты / «своё» / всё.
function updateAuditCleanupForm() {
  const scope = document.querySelector('input[name="audit-cleanup-scope"]:checked')?.value || "30";
  const f = document.getElementById("audc-age-fields");
  if (f) f.style.display = scope === "custom" ? "block" : "none";
}
async function doAuditCleanup() {
  const btn = document.getElementById("audit-cleanup-btn");
  const res = document.getElementById("audit-cleanup-result");
  const scope = document.querySelector('input[name="audit-cleanup-scope"]:checked')?.value || "30";
  let body, msg;
  if (scope === "all") {
    body = { purge_all: true };
    msg = "Удалить ВСЮ историю конфига?";
  } else if (scope === "custom") {
    const days = parseInt(document.getElementById("audc-keep-days").value) || 0;
    const hours = parseInt(document.getElementById("audc-keep-hours").value) || 0;
    if (days === 0 && hours === 0) { res.textContent = "❌ Укажи дни или часы"; return; }
    body = { keep_days: days, keep_hours: hours };
    msg = `Удалить записи старше ${days}д ${hours}ч?`;
  } else {
    const d = parseInt(scope, 10) || 0;
    body = { keep_days: d, keep_hours: 0 };
    msg = `Удалить записи старше ${d} дн.?`;
  }
  const ok = await uiConfirm("Очистить историю конфига", msg + " Действие необратимо.", {danger:true, okText:"Удалить"});
  if (!ok) return;
  btn.disabled = true; res.innerHTML = '<span class="spin"></span> удаление…';
  try {
    const r = await fetch("/api/config/audit/cleanup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await r.json();
    if (data.ok) {
      res.textContent = `✅ Удалено записей: ${data.deleted}`;
      setTimeout(() => { closeAuditCleanup(); loadAudit(); }, 1200);
    } else res.textContent = "❌ " + (data.error || "ошибка");
  } catch (e) { res.textContent = "❌ " + e.message; }
  btn.disabled = false;
}

// ==================== TIMELINE CLEANUP ====================
function openTimelineCleanup() {
  document.getElementById("tl-scope-all").checked = true;
  document.getElementById("tl-keep-days").value = 3;
  document.getElementById("tl-keep-hours").value = 0;
  const today = new Date();
  const yyyy = today.getFullYear();
  const mm = String(today.getMonth() + 1).padStart(2, "0");
  const dd = String(today.getDate()).padStart(2, "0");
  document.getElementById("tl-before-date").value = `${yyyy}-${mm}-${dd}`;
  document.getElementById("tl-before-time").value = "00:00";
  document.getElementById("timeline-cleanup-result").textContent = "";
  updateTimelineCleanupForm();
  document.getElementById("timeline-cleanup-overlay").classList.add("open");
}
function closeTimelineCleanup(evt) {
  if (evt && evt.target && evt.target.id !== "timeline-cleanup-overlay") return;
  document.getElementById("timeline-cleanup-overlay").classList.remove("open");
}
function updateTimelineCleanupForm() {
  const scope = document.querySelector('input[name="timeline-scope"]:checked')?.value || "all";
  document.getElementById("tl-age-fields").style.display = scope === "age" ? "block" : "none";
  document.getElementById("tl-before-fields").style.display = scope === "before" ? "block" : "none";
}
async function doTimelineCleanup() {
  const btn = document.getElementById("timeline-cleanup-btn");
  const res = document.getElementById("timeline-cleanup-result");
  const scope = document.querySelector('input[name="timeline-scope"]:checked')?.value || "all";

  let body = { scope: "timeline" };
  let confirmMsg = "Удалить ВСЮ хронологию событий?";

  if (scope === "age") {
    const days = parseInt(document.getElementById("tl-keep-days").value) || 0;
    const hours = parseInt(document.getElementById("tl-keep-hours").value) || 0;
    if (days === 0 && hours === 0) { res.textContent = "❌ Укажи дни или часы"; return; }
    body = { scope: "timeline_age", keep_days: days, keep_hours: hours };
    confirmMsg = `Удалить события старше ${days}д ${hours}ч?`;
  } else if (scope === "before") {
    const dateStr = document.getElementById("tl-before-date").value;
    const timeStr = document.getElementById("tl-before-time").value || "00:00";
    if (!dateStr) { res.textContent = "❌ Укажи дату"; return; }
    const dt = new Date(`${dateStr}T${timeStr}:00`);
    if (isNaN(dt.getTime())) { res.textContent = "❌ Некорректная дата"; return; }
    body = { scope: "timeline_before", before_ts: Math.floor(dt.getTime() / 1000) };
    confirmMsg = `Удалить события до ${dateStr} ${timeStr}?`;
  }

  const ok = await uiConfirm("Очистить хронологию", confirmMsg + " Действие необратимо.", {danger:true, okText:"Удалить"});
  if (!ok) return;
  btn.disabled = true;
  res.innerHTML = '<span class="spin"></span> удаление…';
  try {
    const r = await fetch("/api/db/cleanup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await r.json();
    if (data.ok) {
      res.textContent = `✅ Удалено записей: ${data.deleted}`;
      if (ANALYTICS_ENABLED) loadAnalytics();
      setTimeout(closeTimelineCleanup, 1500);
    } else {
      res.textContent = "❌ " + (data.error || "ошибка");
    }
  } catch (e) {
    res.textContent = "❌ " + e.message;
  }
  btn.disabled = false;
}

// ===== Base Info =====
// v1.28.74: счётчики «Локальные базы» обновляются сами раз в 15 с.
let BASE_INFO_TIMER = null;
function _ensureBaseInfoTimer() {
  if (BASE_INFO_TIMER) return;
  BASE_INFO_TIMER = setInterval(() => { if (VIEW === "import") loadBaseInfo(); }, 15000);
}

async function loadBaseInfo(btn) {
  if (btn) setButtonState(btn, "loading", "Обновление…");
  try {
    const r = await fetch("/api/base/info");
    const data = await r.json();
    const t = data.tinytuya;
    const u = data.tuya_local;
    const tEl = document.getElementById("base-tinytuya-info");
    const uEl = document.getElementById("base-tuya-local-info");
    if (tEl) tEl.textContent = t.exists ? `${t.count} устройств · ${fmtAgo(t.modified)}` : "не создана";
    if (uEl) uEl.textContent = u.exists ? `${u.count} шаблонов · обновлена ${fmtAgo(u.modified)}` : "не найдена";
    if (btn) {
      setButtonState(btn, "ok", "Обновлено");
      restoreButtonAfter(btn, 2000, "🔄 Обновить");
    }
  } catch (e) {
    console.error("loadBaseInfo", e);
    if (btn) {
      setButtonState(btn, "err", "Ошибка");
      restoreButtonAfter(btn, 3000, "🔄 Обновить");
    }
  }
}
async function updateTuyaLocalDb() {
  const btn = document.getElementById("tuya-local-update-btn");
  const res = document.getElementById("base-update-result");
  const progressWrap = document.getElementById("rebuild-progress");
  const progressText = document.getElementById("rebuild-progress-text");
  const progressFill = document.getElementById("rebuild-bar-fill");
  const ok = await uiConfirm("Обновить tuya-local?", "Скачать/обновить базу tuya-local? (~50 МБ, займёт до 2 мин)\n\nДва этапа: 1) скачивание  2) импорт (индекс).", {okText:"Скачать"});
  if (!ok) return;
  btn.disabled = true;
  setButtonState(btn, "loading", "Запуск…");
  res.innerHTML = "";
  progressWrap.style.display = "block";
  progressText.innerHTML = '<span class="spin"></span> запуск…';
  progressFill.style.width = "0%";
  try {
    const r = await fetch("/api/base/tuya-local/update", { method: "POST" });
    const data = await r.json();
    if (!data.ok) {
      progressText.textContent = "❌ " + (data.error || "ошибка");
      setButtonState(btn, "err", "Ошибка");
      restoreButtonAfter(btn, 3000, "⬇ Обновить tuya-local");
      btn.disabled = false;
      return;
    }
    pollTuyaLocalProgress();
  } catch (e) {
    progressText.textContent = "❌ " + e.message;
    setButtonState(btn, "err", "Ошибка");
    restoreButtonAfter(btn, 3000, "⬇ Обновить tuya-local");
    btn.disabled = false;
  }
}

// v1.28.65: прогресс обновления tuya-local (фаза download → import).
function pollTuyaLocalProgress() {
  if (TUYA_LOCAL_POLL_TIMER) clearTimeout(TUYA_LOCAL_POLL_TIMER);
  TUYA_LOCAL_POLL_TIMER = setTimeout(async () => {
    try {
      const r = await fetch("/api/base/tuya-local/progress");
      const s = await r.json();
      const btn = document.getElementById("tuya-local-update-btn");
      const progressWrap = document.getElementById("rebuild-progress");
      const progressText = document.getElementById("rebuild-progress-text");
      const progressFill = document.getElementById("rebuild-bar-fill");
      const pct = s.total > 0 ? Math.round((s.current / s.total) * 100) : 0;
      progressFill.style.width = pct + "%";
      if (s.running) {
        progressFill.classList.toggle("indeterminate", s.total <= 0);
        const det = s.total > 0 ? ` ${s.current}/${s.total} (${pct}%)` : "";
        progressText.innerHTML = `<span class="spin"></span> ${escapeHtml(s.message || "…")}${det}`;
        pollTuyaLocalProgress();
      } else if (s.ok === true) {
        progressFill.classList.remove("indeterminate");
        progressText.textContent = "✅ " + (s.message || "готово");
        setButtonState(btn, "ok", "Готово");
        restoreButtonAfter(btn, 3000, "⬇ Обновить tuya-local");
        btn.disabled = false;
        loadBaseInfo();
        setTimeout(() => { progressWrap.style.display = "none"; }, 6000);
      } else if (s.ok === false) {
        progressFill.classList.remove("indeterminate");
        progressText.textContent = "❌ " + (s.error || s.message || "ошибка");
        setButtonState(btn, "err", "Ошибка");
        restoreButtonAfter(btn, 3000, "⬇ Обновить tuya-local");
        btn.disabled = false;
        setTimeout(() => { progressWrap.style.display = "none"; }, 8000);
      } else {
        pollTuyaLocalProgress();   // состояние ещё не инициализировано
      }
    } catch (e) {
      pollTuyaLocalProgress();
    }
  }, 800);
}

// ===== Rebuild tinytuya.json =====
async function rebuildTinytuyaJson() {
  const btn = document.getElementById("rebuild-btn");
  const progressWrap = document.getElementById("rebuild-progress");
  const progressText = document.getElementById("rebuild-progress-text");
  const progressFill = document.getElementById("rebuild-bar-fill");

  const ok = await uiConfirm("Пересобрать tinytuya.json?",
    "Пересобрать webui_state/tinytuya_devices.json? Это может занять несколько минут (probe каждого устройства).",
    {okText:"Запустить"});
  if (!ok) return;

  btn.disabled = true;
  setButtonState(btn, "loading", "Сборка…");
  progressWrap.style.display = "block";
  progressText.innerHTML = '<span class="spin"></span> запуск…';
  progressFill.classList.remove("indeterminate");
  progressFill.style.width = "0%";

  try {
    const r = await fetch("/api/base/tinytuya/rebuild", { method: "POST", headers: {"Content-Type":"application/json"}, body: "{}" });
    const data = await r.json();
    if (!data.ok) {
      progressText.textContent = "❌ " + (data.error || "ошибка");
      setButtonState(btn, "err", "Ошибка");
      restoreButtonAfter(btn, 3000, "🔄 Пересобрать tinytuya.json");
      btn.disabled = false;
      return;
    }
    progressText.textContent = "запущено, ждём прогресс…";
    pollRebuildProgress();
  } catch (e) {
    progressText.textContent = "❌ " + e.message;
    setButtonState(btn, "err", "Ошибка");
    restoreButtonAfter(btn, 3000, "🔄 Пересобрать tinytuya.json");
    btn.disabled = false;
  }
}

function pollRebuildProgress() {
  if (REBUILD_POLL_TIMER) clearTimeout(REBUILD_POLL_TIMER);
  REBUILD_POLL_TIMER = setTimeout(async () => {
    try {
      const r = await fetch("/api/base/rebuild/progress");
      const s = await r.json();
      const btn = document.getElementById("rebuild-btn");
      const progressWrap = document.getElementById("rebuild-progress");
      const progressText = document.getElementById("rebuild-progress-text");
      const progressFill = document.getElementById("rebuild-bar-fill");
      if (s.running) {
        const pct = s.total > 0 ? Math.round((s.current / s.total) * 100) : 0;
        progressFill.style.width = pct + "%";
        progressText.innerHTML = `<span class="spin"></span> ${s.current}/${s.total} — ${escapeHtml(s.device || "")}`;
        pollRebuildProgress();
      } else {
        const pct = s.total > 0 ? Math.round((s.current / s.total) * 100) : 0;
        progressFill.style.width = pct + "%";
        let msg = s.ok ? `✅ Готово: ${s.current}/${s.total}` : `⚠️ Завершено с ошибками (${s.errors?.length || 0})`;
        if (s.errors?.length) msg += " · " + s.errors.slice(0,3).map(escapeHtml).join("; ");
        progressText.textContent = msg;
        if (s.ok) {
          setButtonState(btn, "ok", "Готово");
        } else {
          setButtonState(btn, "err", "Ошибка");
        }
        restoreButtonAfter(btn, 3000, "🔄 Пересобрать tinytuya.json");
        btn.disabled = false;
        loadBaseInfo();
        setTimeout(() => { progressWrap.style.display = "none"; }, 6000);
      }
    } catch (e) {
      pollRebuildProgress();
    }
  }, 800);
}

// ===== Cloud cache =====
function loadCloudCreds() {
  try {
    const s = JSON.parse(localStorage.getItem(CLOUD_CREDS_KEY) || "{}");
    if (s.access_id) document.getElementById("cloud-access-id").value = s.access_id;
    if (s.access_secret) document.getElementById("cloud-access-secret").value = s.access_secret;
    if (s.region) document.getElementById("cloud-region").value = s.region;
  } catch {}
}
function saveCloudCreds() {
  try {
    localStorage.setItem(CLOUD_CREDS_KEY, JSON.stringify({
      access_id: document.getElementById("cloud-access-id").value.trim(),
      access_secret: document.getElementById("cloud-access-secret").value.trim(),
      region: document.getElementById("cloud-region").value,
    }));
  } catch {}
}

function cacheAgeSeconds() {
  if (!CLOUD_CACHE_FETCHED_AT) return null;
  return Math.floor(Date.now()/1000) - CLOUD_CACHE_FETCHED_AT;
}

function renderCloudCacheInfo() {
  const el = document.getElementById("cloud-cache-info");
  if (!el) return;
  if (!CLOUD_CACHE_FETCHED_AT || CLOUD_DEVICES.length === 0) {
    el.textContent = "";
    el.style.color = "";
    return;
  }
  const age = cacheAgeSeconds();
  let color = "var(--muted)";
  let suffix = "";
  if (age >= CLOUD_CACHE_OLD_AFTER) { color = "var(--red)";    suffix = " 🔴 устарел"; }
  else if (age >= CLOUD_CACHE_WARN_AFTER) { color = "var(--yellow)"; suffix = " 🔴"; }
  else if (age >= CLOUD_CACHE_STALE_AFTER) { color = "var(--yellow)"; suffix = " ⚠️"; }
  el.style.color = color;
  el.textContent = `Кэш: ${fmtAgo(CLOUD_CACHE_FETCHED_AT)} (${CLOUD_DEVICES.length} устройств)${suffix}`;
}

function maybeShowCacheBanner() {
  const age = cacheAgeSeconds();
  if (age === null || age < CLOUD_CACHE_WARN_AFTER) { hideCacheBanner(); return; }
  const dismissedAt = parseInt(sessionStorage.getItem(CLOUD_CACHE_BANNER_KEY) || "0", 10);
  if (dismissedAt && (Date.now()/1000 - dismissedAt) < 3600) { hideCacheBanner(); return; }
  showCacheBanner();
}

function showCacheBanner() {
  const wrap = document.getElementById("cloud-cache-banner");
  if (!wrap) return;
  const age = cacheAgeSeconds();
  const isRed = age >= CLOUD_CACHE_OLD_AFTER;
  wrap.className = "cloud-cache-banner" + (isRed ? " red" : "");
  wrap.innerHTML = `
    <span>⚠️ Кэш Cloud устарел (${fmtAgo(CLOUD_CACHE_FETCHED_AT)}). Данные могли измениться.</span>
    <div style="display:flex; gap:8px; margin-left:auto;">
      <button onclick="fetchCloudDevices()">🔄 Запросить из облака</button>
      <button onclick="dismissCacheBanner()">× Скрыть</button>
    </div>
  `;
  wrap.style.display = "flex";
}

function hideCacheBanner() {
  const wrap = document.getElementById("cloud-cache-banner");
  if (wrap) wrap.style.display = "none";
}

function dismissCacheBanner() {
  sessionStorage.setItem(CLOUD_CACHE_BANNER_KEY, String(Math.floor(Date.now()/1000)));
  hideCacheBanner();
}

async function loadCloudCacheServer() {
  try {
    const r = await fetch("/api/cloud/cache");
    const data = await r.json();
    if (!data.ok || !data.exists) return false;
    if (!Array.isArray(data.devices) || data.devices.length === 0) return false;
    CLOUD_DEVICES = data.devices;
    CLOUD_SELECTED = {};
    CLOUD_CACHE_FETCHED_AT = data.fetched_at || 0;
    if (data.access_id && !document.getElementById("cloud-access-id").value) {
      document.getElementById("cloud-access-id").value = data.access_id;
    }
    if (data.region) {
      document.getElementById("cloud-region").value = data.region;
    }
    renderCloudCacheInfo();
    maybeShowCacheBanner();
    return true;
  } catch (e) {
    console.warn("cache load err", e);
    return false;
  }
}

async function clearCloudCache() {
  const ok = await uiConfirm("Очистить кэш Cloud?", "Очистить кэш Cloud-устройств на сервере?", {danger:true, okText:"Очистить"});
  if (!ok) return;
  try { await fetch("/api/cloud/cache", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ clear: true }) }); } catch {}
  CLOUD_DEVICES = []; CLOUD_SELECTED = {};
  CLOUD_CACHE_FETCHED_AT = 0;
  renderCloudDevices();
  renderCloudCacheInfo();
  hideCacheBanner();
}

// ===== Cloud =====
async function fetchCloudDevices() {
  const btn = document.getElementById("fetch-btn");
  const res = document.getElementById("cloud-result");
  const aid = document.getElementById("cloud-access-id").value.trim();
  const asec = document.getElementById("cloud-access-secret").value.trim();
  const region = document.getElementById("cloud-region").value;
  if (!aid || !asec) { res.textContent = "❌ Заполни Access ID и Secret"; return; }
  saveCloudCreds();
  btn.disabled = true;
  res.innerHTML = '<span class="spin"></span> запрос… (до 30 сек)';
  try {
    const r = await fetch("/api/cloud/fetch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_id: aid, access_secret: asec, region })
    });
    const data = await r.json();
    if (!data.ok) { res.textContent = "❌ " + (data.error || "ошибка"); btn.disabled = false; return; }
    CLOUD_DEVICES = data.devices || []; CLOUD_SELECTED = {};
    CLOUD_CACHE_FETCHED_AT = Math.floor(Date.now()/1000);
    const withKey = CLOUD_DEVICES.filter(d => d.local_key).length;
    const withMap = CLOUD_DEVICES.filter(d => d.mapping && Object.keys(d.mapping).length > 0).length;
    const withStatus = CLOUD_DEVICES.filter(d => d.cloud_status && Object.keys(d.cloud_status).length > 0).length;
    res.textContent = `✅ ${CLOUD_DEVICES.length} устройств · key: ${withKey} · mapping: ${withMap} · status: ${withStatus}`;
    renderCloudDevices();
    renderCloudCacheInfo();
    hideCacheBanner();
    loadBaseInfo();
  } catch (e) { res.textContent = "❌ " + e.message; }
  btn.disabled = false;
}

function renderCloudDevices() {
  const c = document.getElementById("cloud-devices-container");
  const actions = document.getElementById("import-actions");
  const count = document.getElementById("cloud-count");
  if (CLOUD_DEVICES.length === 0) { c.innerHTML = '<div class="muted" style="padding:16px;">Пусто</div>'; actions.style.display = "none"; return; }
  // v1.28.42: поиск + фильтр (новые/добавленные/все) + сортировка.
  let filtered = CLOUD_DEVICES.filter(d => cloudSearchPass(d) && _cloudFilterPass(d));
  const _sv = (d) => _cloudSortVal(d, CLOUD_SORT.key);
  filtered = filtered.slice().sort((a, b) => {
    const va = _sv(a), vb = _sv(b);
    return va < vb ? -CLOUD_SORT.dir : (va > vb ? CLOUD_SORT.dir : 0);
  });
  const searchInfo = document.getElementById("cloud-search-info");
  if (searchInfo) {
    searchInfo.textContent = (CLOUD_SEARCH || CLOUD_FILTER !== "all")
      ? `найдено ${filtered.length} из ${CLOUD_DEVICES.length}`
      : "";
  }
  count.textContent = `${CLOUD_DEVICES.length} устройств`;
  // v1.18.12: сохраняем scrollTop внутреннего скроллера — иначе при
  // перерисовке (Выбрать все / Снять всё) список "улетает" вверх.
  const oldScroller = c.querySelector('.cloud-scroll');
  const savedScrollTop = oldScroller ? oldScroller.scrollTop : 0;
  const _th = (key, label) =>
    `<th style="cursor:pointer;" onclick="sortCloudDevices('${key}')">${label}${_cloudSortInd(key)}</th>`;
  let html = `<div class="cloud-scroll" style="max-height:500px; overflow-y:auto;"><table>
    <thead><tr><th style="width:40px;"></th>${_th("name","Имя")}${_th("type","Тип")}${_th("product","Продукт")}<th>Local key</th>${_th("dp","DP")}${_th("online","Online")}</tr></thead><tbody>`;
  for (const d of filtered) {
    const i = CLOUD_DEVICES.indexOf(d);
    const inConfig = _isCloudDeviceInConfig(d);
    // v1.27.7: устройство в конфиге, но отключено?
    const _ld = LAST_DEVICES.find(ld => ld.tuya_id === d.id);
    const isDisabled = !!(inConfig && _ld && _ld.enabled === false);
    const sel = CLOUD_SELECTED[i] ? "checked" : "";
    const cls = isDisabled
      ? "background:rgba(139,148,158,0.10);"
      : (inConfig
          ? "background:rgba(46,160,67,0.08);"
          : (CLOUD_SELECTED[i] ? "background:rgba(3,102,214,0.08);" : ""));
    const rowCls = (isDisabled ? "cloud-row-disabled"
                  : (inConfig ? "cloud-row-in-config" : ""))
                  + (CLOUD_SELECTED[i] ? " cloud-row-selected" : "");
    // v1.27.7: компактный бейдж — только ✅, текст в title.
    let alreadyBadge = '';
    if (isDisabled) {
      alreadyBadge = ' <span class="badge enabled-off" style="font-size:10px;" title="Уже в конфиге (отключено)">✓</span>';
    } else if (inConfig) {
      alreadyBadge = ' <span class="badge enabled-ok" style="font-size:10px;" title="Уже в конфиге">✅</span>';
    }
    // v1.28.26: Local key в таблице Cloud — под маской. Показ через
    // кнопку 👁, состояние — per-device в CLOUD_REVEALED_KEYS (сброс
    // при closeCloudModal). Раньше ключ светился в открытом виде.
    let key;
    if (!d.local_key) {
      key = '<span class="muted">—</span>';
    } else if (CLOUD_REVEALED_KEYS[d.id]) {
      key = copyCode(d.local_key, "font-size:11px;")
          + ' <button onclick="cloudHideSecret(\'' + escapeAttr(d.id) + '\')"'
          + ' style="margin-left:4px; padding:1px 6px; font-size:11px;"'
          + ' title="Скрыть">🔓</button>';
    } else {
      key = '<span class="secret-masked" style="font-size:11px;">••••••••</span>'
          + ' <button onclick="cloudRevealSecret(\'' + escapeAttr(d.id) + '\')"'
          + ' style="margin-left:4px; padding:1px 6px; font-size:11px;"'
          + ' title="Показать">🔒</button>';
    }
    const dpCount = d.mapping ? Object.keys(d.mapping).length : 0;
    const online = d.online
      ? '<span class="dot online"></span><span class="state-on" style="margin-left:4px;">on</span>'
      : '<span class="dot offline"></span><span class="state-off" style="margin-left:4px;">off</span>';
    html += `<tr class="${rowCls}" style="${cls}; cursor:pointer;" data-cloud-idx="${i}" onclick="showCloudDevice(${i})">
      <td onclick="event.stopPropagation()"><input type="checkbox" ${sel} onchange="toggleCloudSelect(${i}, this.checked)"></td>
      <td>
        <div style="font-weight:500;">${escapeHtml(d.name || '?')}${alreadyBadge}</div>
        <div class="muted" style="font-size:11px; font-family:ui-monospace,monospace;">${escapeHtml(d.id || '')}</div>
      </td>
      <td>${typeBadge(d.type_guess || 'switch')}</td>
      <td>${escapeHtml(d.product_name || '?')}</td>
      <td onclick="event.stopPropagation()" style="white-space:nowrap;">${key}</td>
      <td class="muted" style="font-size:11px;">${dpCount}</td>
      <td>${online}</td>
    </tr>`;
  }
  html += "</tbody></table></div>";
  c.innerHTML = html;
  // v1.18.12: восстанавливаем scrollTop после перерисовки.
  const newScroller = c.querySelector('.cloud-scroll');
  if (newScroller && savedScrollTop) newScroller.scrollTop = savedScrollTop;
  actions.style.display = "flex";
  updateSelectedCount();
}
function toggleCloudSelect(idx, checked) {
  if (checked) CLOUD_SELECTED[idx] = true; else delete CLOUD_SELECTED[idx];
  // v1.28.42: выбранное — классом (фон строки несёт подсветку
  // «в конфиге/отключено», не перетираем).
  const row = document.querySelector(`#cloud-devices-container tr[data-cloud-idx="${idx}"]`);
  if (row) row.classList.toggle("cloud-row-selected", !!checked);
  updateSelectedCount();
}
function selectAllCloud(v) {
  // v1.25.13 + v1.28.42: «Выбрать все»/«Снять всё» — только видимые (поиск+фильтр).
  const visible = CLOUD_DEVICES.filter(d => cloudSearchPass(d) && _cloudFilterPass(d));
  CLOUD_SELECTED = {};
  if (v) {
    for (const d of visible) {
      const i = CLOUD_DEVICES.indexOf(d);
      if (i >= 0) CLOUD_SELECTED[i] = true;
    }
  }
  renderCloudDevices();
}
function updateSelectedCount() { document.getElementById("selected-count").textContent = `Выбрано: ${Object.keys(CLOUD_SELECTED).length}`; }

let CURRENT_CLOUD_IDX = -1;
function showCloudDevice(idx) {
  const d = CLOUD_DEVICES[idx];
  if (!d) return;
  CURRENT_CLOUD_IDX = idx;   // v1.27.12: для _rerenderCloudModal
  const devId = d.id || ('idx_' + idx);
  let html = "";

  const mapping = d._raw_cloud?.mapping || d.mapping || {};
  if (Object.keys(mapping).length === 0) {
    html += `<div class="cloud-warn">⚠️ Cloud не вернул mapping для этого устройства. DP-сопоставление будет сделано через Probe при импорте (по значению + типам).</div>`;
  }

  html += `<h3>Cloud info</h3><table class="detail-table">`;
  const row = (k, v, copyable) => {
    if (!v) return `<tr><td>${k}</td><td class="muted">—</td></tr>`;
    if (copyable) return `<tr><td>${k}</td><td>${copyCode(v)}</td></tr>`;
    return `<tr><td>${k}</td><td>${escapeHtml(v)}</td></tr>`;
  };
  // v1.28.51: Online — только точка (как в карточке), без слова.
  html += `<tr><td>Online</td><td><span class="dot ${d.online ? 'online' : 'offline'}"></span></td></tr>`;
  html += row("Имя", d.name);
  html += row("ID", d.id, true);
  html += `<tr><td>Category (raw)</td><td><span class="type-badge">${escapeHtml(d.category || '?')}</span> <span class="muted" style="font-size:11px;">(код Tuya)</span></td></tr>`;
  html += `<tr><td>Тип устройства</td><td>${typeBadge(d.type_guess || 'switch')}</td></tr>`;
  html += `<tr><td>Версия протокола (guess)</td><td>${copyCodePlain(d.version_guess || '3.3')}</td></tr>`;
  html += row("Продукт", d.product_name);
  html += row("Product ID", d.product_id, true);
  html += row("Модель", d.model);
  html += row("UUID", d.uuid, true);
  // v1.27.12: Local key в Cloud-модалке — под 👁 (как на дашборде).
  if (d.local_key) {
    const _revealed = CLOUD_REVEALED_KEYS[d.id];
    if (_revealed) {
      html += `<tr><td>Local key</td><td>${copyCode(d.local_key)} <button onclick="cloudHideSecret('${escapeAttr(d.id)}')" title="Скрыть" style="margin-left:4px; padding:2px 8px; font-size:12px;">🔓</button></td></tr>`;
    } else {
      html += `<tr><td>Local key</td><td><span class="secret-masked">••••••••••</span> <button onclick="cloudRevealSecret('${escapeAttr(d.id)}')" title="Показать" style="margin-left:6px; padding:2px 8px; font-size:12px;">🔒</button></td></tr>`;
    }
  } else {
    html += `<tr><td>Local key</td><td class="muted">—</td></tr>`;
  }
  if (d.gateway_id) html += row("Gateway ID", d.gateway_id, true);
  if (d._key_from_parent) html += `<tr><td>Key source</td><td class="muted">взят от parent <code>${escapeHtml(d._key_from_parent)}</code></td></tr>`;
  html += `</table>`;

  // v1.28.37: «Status из облака» больше не дублирует «Сопоставление DP».
  // Совпадающие code показываются в DP-таблице (колонка «Текущее»);
  // отдельно выводим только code из status, которых нет в mapping.
  const cloudStatus = d.cloud_status || {};
  const _statusCodes = Object.keys(cloudStatus);

  // v1.28.38: единый предпросмотр DP вместо отдельной read-only таблицы.
  const _cloudInCfg = _isCloudDeviceInConfig(d);
  const _mappedCodes = new Set(
    Object.values(mapping).map(x => x && x.code).filter(Boolean));
  const _unmapped = _statusCodes.filter(c => !_mappedCodes.has(c)).sort();

  html += `<h3 style="margin-top:16px;">${_cloudInCfg ? "Просмотр" : "Импорт"}</h3>`;
  if (_cloudInCfg) {
    html += `<div class="muted" style="margin-bottom:8px;">⚠️ Устройство уже есть в конфиге — доступен только просмотр.</div>`;
    html += `<button class="primary" onclick="previewSingleCloudDevice(${idx})">📦 Посмотреть это устройство</button>`;
  } else {
    html += `<button class="primary" onclick="previewSingleCloudDevice(${idx})">📦 Импортировать это устройство</button>`;
  }
  html += `<div class="muted" style="font-size:11px; margin-top:6px;">Откроется предпросмотр DP (источник / компоненты / значения).</div>`;

  if (_unmapped.length > 0) {
    html += `<h3>Status без сопоставления (${_unmapped.length})</h3>`;
    html += `<div class="wide-table-wrap"><table class="detail-table wide-table wide-table-2col"><colgroup>
      <col class="col-code"><col class="col-current">
    </colgroup><thead><tr><th>Код</th><th>Значение</th></tr></thead><tbody>`;
    for (const k of _unmapped) {
      html += `<tr><td class="cell col-code"><div class="cell-inner">${copyCodePlain(k)}</div></td><td class="cell col-current"><div class="cell-inner cell-trunc">${_cellValue(displayValue(cloudStatus[k]), 80)}</div></td></tr>`;
    }
    html += `</tbody></table></div>`;
  } else if (_statusCodes.length > 0 && Object.keys(mapping).length === 0) {
    // mapping нет — показываем сырой status (иначе смотреть нечего).
    html += `<h3>Status из облака (${_statusCodes.length} значений)</h3>`;
    html += `<div class="wide-table-wrap"><table class="detail-table wide-table wide-table-2col"><colgroup>
      <col class="col-code"><col class="col-current">
    </colgroup><thead><tr><th>Код</th><th>Значение</th></tr></thead><tbody>`;
    for (const k of _statusCodes.slice().sort()) {
      html += `<tr><td class="cell col-code"><div class="cell-inner">${copyCodePlain(k)}</div></td><td class="cell col-current"><div class="cell-inner cell-trunc">${_cellValue(displayValue(cloudStatus[k]), 80)}</div></td></tr>`;
    }
    html += `</tbody></table></div>`;
  }

  const isOpen = !!CLOUD_DETAILS_OPEN[devId];
  const rawDump = {};
  for (const k of Object.keys(d)) {
    if (k === "mapping" || k === "cloud_status" || k === "dps_map_generated") continue;
    if (k.startsWith("_raw")) continue;
    rawDump[k] = d[k];
  }
  if (d._raw_cloud) rawDump._raw_cloud = d._raw_cloud;
  if (d._raw_properties) rawDump._raw_properties = d._raw_properties;
  const rawJson = escapeHtml(JSON.stringify(rawDump, null, 2));
  html += `<details style="margin-top:16px;" ${isOpen ? 'open' : ''} ontoggle="CLOUD_DETAILS_OPEN['${escapeAttr(devId)}'] = this.open;">
    <summary class="muted" style="font-size:12px; text-transform:uppercase; letter-spacing:0.5px;">▶ Показать сырые данные по устройству (Cloud)</summary>
    <pre style="background:var(--bg); padding:12px; border-radius:4px; font-size:11px; white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; overflow-x:hidden; overflow-y:auto; width:0; min-width:100%; box-sizing:border-box; max-height:400px; margin-top:8px;"><code class="json-view">${rawJson}</code></pre>
  </details>`;

  // v1.27.6: если устройство уже в конфиге — бейдж в шапке.
  const _inCfg = _isCloudDeviceInConfig(d);
  const _titleEl = document.getElementById("cloud-modal-title");
  if (_inCfg) {
    _titleEl.innerHTML = escapeHtml(d.name || d.id)
      + ' <span class="badge enabled-ok" style="font-size:11px; vertical-align:middle;">✅ уже в конфиге</span>';
  } else {
    _titleEl.textContent = d.name || d.id;
  }
  document.getElementById("cloud-modal-body").innerHTML = html;
  const codeEl = document.querySelector("#cloud-modal-body code.json-view");
  if (codeEl) highlightJsonInto(codeEl);
  document.getElementById("cloud-modal-overlay").classList.add("open");
}
// v1.27.12: Cloud Local key — показать/скрыть.
function cloudRevealSecret(devId) {
  if (!devId) return;
  CLOUD_REVEALED_KEYS[devId] = true;
  _rerenderCloudModal();
}
function cloudHideSecret(devId) {
  if (!devId) return;
  delete CLOUD_REVEALED_KEYS[devId];
  _rerenderCloudModal();
}
function _rerenderCloudModal() {
  // v1.28.29: перерисовываем только то, что реально открыто.
  // Раньше renderCloudDevices() вызывался ВСЕГДА — при клике на 👁
  // в таблице (модалка закрыта) полностью перерисовывалась вся
  // таблица Cloud, что при 50+ устройствах даёт заметный фриз.
  const modalOpen = CURRENT_CLOUD_IDX >= 0
      && CURRENT_CLOUD_IDX < CLOUD_DEVICES.length
      && document.getElementById("cloud-modal-overlay")
          ?.classList.contains("open");
  if (modalOpen) {
    const body = document.getElementById("cloud-modal-body");
    const st = body ? body.scrollTop : 0;
    showCloudDevice(CURRENT_CLOUD_IDX);
    if (body) body.scrollTop = st;
  }
  // Таблицу перерисовываем, но: если модалка открыта — она уже
  // перерисована, вторую перерисовку можно пропустить.
  // Если модалка закрыта — перерисовываем только таблицу.
  if (!modalOpen) {
    renderCloudDevices();
  }
}

function closeCloudModal(evt) {
  if (evt && evt.target && evt.target.id !== "cloud-modal-overlay") return;
  document.getElementById("cloud-modal-overlay").classList.remove("open");
  // v1.27.12: сброс показа Local key при закрытии.
  CLOUD_REVEALED_KEYS = {};
  CURRENT_CLOUD_IDX = -1;
}

// ===== Import + Preview =====
// v1.18.10: единая точка получения mapping устройства из Cloud-кэша.
// Раньше код смотрел только на d._raw_cloud.mapping, но в кэше
// mapping лежит на верхнем уровне (d.mapping), а _raw_cloud.mapping
// отсутствует. Теперь — цепочка фолбэков.
function getDeviceMapping(d) {
  // v1.28.33.fixup3: union d.dps_map_generated ∪ d.mapping.
  // dps_map_generated — база (Cloud + tuya-local + эвристики).
  // d.mapping — дополняет недостающие поля (не перезаписывает).
  if (!d) return {};
  const out = {};
  // 1. dps_map_generated — база. v1.28.34: не создаём пустые type/values —
  //    иначе реальные значения из mapping не подмешивались.
  if (d.dps_map_generated && Object.keys(d.dps_map_generated).length > 0) {
    for (const [dp, info] of Object.entries(d.dps_map_generated)) {
      const e = { code: info.name || info.code || ("dp_" + dp) };
      if (info._cloud_type) e.type = info._cloud_type;
      if (info.name) e.name = info.name;
      out[dp] = e;
    }
  }
  // 2. d.mapping — только недостающие поля (в т.ч. values/type).
  const _raw_mapping =
    (d.mapping && Object.keys(d.mapping).length > 0) ? d.mapping :
    (d._raw_cloud && d._raw_cloud.mapping && Object.keys(d._raw_cloud.mapping).length > 0)
      ? d._raw_cloud.mapping
      : null;
  if (_raw_mapping) {
    for (const [dp, m] of Object.entries(_raw_mapping)) {
      if (!m || typeof m !== "object") continue;
      if (!out[dp]) {
        out[dp] = Object.assign({}, m);
      } else {
        for (const k of Object.keys(m)) {
          if (!(k in out[dp])) out[dp][k] = m[k];
        }
      }
    }
  }
  return out;
}

// v1.28.10: эвристика «батарейное устройство» — единая точка.
// Возвращает true, если:
//   - category в BATTERY_CATS (wsdcg/mcs/pir/sj/ywbj/rqbj), ИЛИ
//   - есть DP с code, начинающимся на "battery" (battery_percentage,
//     battery_state, battery_value, battery_...), ИЛИ
//   - есть DP с code, содержащим "battery" где угодно (va_battery, ...).
// Cloud не возвращает battery_powered — только эвристика.
// Пользователь может переопределить в превью импорта (тумблер).
const _BATTERY_CATS = new Set(["wsdcg","mcs","pir","sj","ywbj","rqbj"]);
function _guessIsBattery(d) {
  if (!d) return false;
  const cat = String(d.category || "").toLowerCase();
  if (_BATTERY_CATS.has(cat)) return true;
  // Проверяем DP: cloud mapping или dps_map_generated.
  const sources = [
    d.mapping || {},
    d.dps_map_generated || {},
    (d._raw_cloud && d._raw_cloud.mapping) || {},
  ];
  for (const src of sources) {
    if (!src || typeof src !== "object") continue;
    for (const v of Object.values(src)) {
      if (!v || typeof v !== "object") continue;
      const code = String(v.code || v.name || "").toLowerCase();
      if (!code) continue;
      // v1.28.28: расширено — startsWith + va_battery + includes
      // с исключением "no_" / "_off" (no_battery_mode, battery_off).
      if (code.startsWith("battery")) return true;
      if (code === "va_battery") return true;
      // v1.28.29: точечные исключения. Раньше "no_" и "_off"
      // блокировали и валидные "battery_low", "battery_off_...".
      // Теперь блокируем только "no_battery" и "battery_off"
      // (как отдельные токены).
      if (code.includes("battery")
          && code !== "no_battery"
          && !code.endsWith("_off")) return true;
    }
  }
  return false;
}

// v1.28.38: импорт одного Cloud-устройства из его карточки — открывает
// тот же предпросмотр, что и массовый импорт.
function previewSingleCloudDevice(idx) {
  if (idx < 0 || idx >= CLOUD_DEVICES.length) return;
  CLOUD_SELECTED = {};
  CLOUD_SELECTED[idx] = true;
  updateSelectedCount();
  closeCloudModal();
  importSelected();
}

async function importSelected() {
  const sel = Object.keys(CLOUD_SELECTED).map(i => CLOUD_DEVICES[parseInt(i)]);
  if (sel.length === 0) { uiAlert("Импорт", "Ничего не выбрано", "warning"); return; }

  // v1.28.40: если все выбранные уже в конфиге — это просмотр, не импорт.
  const _allInConfig = sel.every(d => _isCloudDeviceInConfig(d));
  if (_allInConfig) {
    const _ok = await uiConfirm(
      "Уже добавлены",
      `Все выбранные устройства (${sel.length}) уже есть в конфиге.\n\n⚠️ Доступен только просмотр ⚠️`,
      { okText: "Просмотр", cancelText: "Отмена" }
    );
    if (!_ok) return;
  }

  // v1.27.6: пресет IP — по локальной подсети из конфига (fallback MQTT).
  const prefix = _guessSubnetPrefix();
  const placeholder = prefix ? `${prefix}.100` : "192.168.1.100";
  const preValue = prefix ? `${prefix}.` : "";
  const netHint = prefix ? `Сеть: ${prefix}.x` : "";

  const prepared = [];
  const _assigned_ips = new Set();   // v1.28.11: для проверки дублей IP
  // v1.28.41: уникальность name (латиница) и friendly_name.
  const _usedNames = new Set(LAST_DEVICES.map(x => x.name).filter(Boolean));
  const _usedFriendly = new Set(LAST_DEVICES.map(x => x.friendly_name).filter(Boolean));
  for (const d of sel) {
    // v1.27.9: транслит имени — в name только [a-z0-9_].
    // friendly_name остаётся как есть (кириллица, для UI/HA).
    const _inCfg = _isCloudDeviceInConfig(d);
    const _knownCfg = _inCfg ? _knownConfigDeviceForCloud(d) : null;
    const _knownIp = (_knownCfg && _knownCfg.ip) ? _knownCfg.ip : "";

    let ip, friendly;
    if (_inCfg && _knownIp) {
      // v1.28.40: устройство известно — формы не показываем,
      // подставляем IP и имя из конфига автоматически.
      ip = _knownIp;
      friendly = (_knownCfg && _knownCfg.friendly_name) ? _knownCfg.friendly_name : (d.name || d.id);
    } else {
      const _msg = `IP для "${d.name || d.id}":` + (netHint ? `\n\n${netHint}` : "");
      const _opts = {
        placeholder: placeholder,
        value: _knownIp || preValue,
        okText: "Далее",
        validate: (v) => {
          const t = (v || "").trim();
          if (!t) return "Укажите IP-адрес";
          if (!_isValidIPv4(t)) return "Некорректный IP-адрес (0-255, без ведущих нулей)";
          if (_ipInUse(t, _knownIp)) return "Этот IP уже используется другим устройством";
          if (_assigned_ips.has(t)) return "Этот IP уже назначен другому устройству в этом импорте";
          return null;
        }
      };
      const _ip = await uiPrompt("IP-адрес", _msg, _opts);
      if (_ip === null) return;
      if (_ip === undefined) continue;
      ip = _ip.trim();
      _assigned_ips.add(ip);

      // v1.28.41: имя больше не спрашиваем — берём из Cloud (d.name) или id.
      friendly = d.name || d.id;
    }

    // v1.28.41: friendly_name и name (латиница) — уникальные.
    friendly = _uniquifyFriendly(friendly || d.name || d.id, _usedFriendly);
    _usedFriendly.add(friendly);
    const defaultName = _uniquifyName(_makeDeviceName(friendly, d.id), _usedNames);
    _usedNames.add(defaultName);

    let dps_map = d.dps_map_generated || {};

    const known = (_knownCfg && _knownCfg.ip === ip) ? _knownCfg
                : LAST_DEVICES.find(x => x.ip === ip);
    let version = (known && known.version) ? known.version : (d.version_guess || "3.3");
    let probeInfo = null;
    if (!known) {
      probeInfo = { skipped: true, reason: "auto probe disabled, будет по кнопке в превью" };
    }

    // v1.28.10: единая эвристика _guessIsBattery().
    const _isBattery = _guessIsBattery(d);

    const _base = {
      id: d.id, name: defaultName, friendly_name: friendly.trim(),
      ip: ip.trim(), local_key: d.local_key || "",
      version: version, type: d.type_guess || "switch",
      model: d.product_name || "",
      battery_powered: _isBattery, enabled: true,
      dps_map: dps_map,
      _cloud_ref: d,
      _probe: probeInfo,
    };
    // v1.28.9: пробрасываем climate-поля, если Cloud их дал.
    for (const _k of ["presets","preset_map","min_temp","max_temp","temp_step"]) {
      if (d[_k] !== undefined) _base[_k] = d[_k];
    }
    // v1.28.16: авто preset_map для climate.
    // Cloud не отдаёт presets/preset_map — заполняем из DP
    // с component="preset" и его options. Язык — по PRESET_LANG:
    //   "ru"    → preset_map реально пишется в конфиг (Tuya→RU).
    //   "as-is" → НЕ заполняем, bridge сам выведет presets из options.
    // Существующие climate не трогаем: их preset_map уже в конфиге,
    // edit_config не перезаписывает без явного изменения.
    if (_base.type === "climate" && (!_base.presets || _base.presets.length === 0)) {   // v1.28.17: пустой массив тоже
      const _opts = _climatePresetOptions(_base);
      if (_opts.length > 0) {
        if (PRESET_LANG === "ru") {
          _base.presets = _opts.slice();
          const _pm = {};
          for (const p of _opts) {
            _pm[p] = PRESET_TUYA_TO_RU[p] || p;
          }
          _base.preset_map = _pm;
        }
        // "as-is" — ничего не заполняем, bridge сам выведет.
      }
    }
    prepared.push(_base);
  }

  // v1.27.7: если пользователь всё пропустил — не открываем превью.
  if (prepared.length === 0) {
    uiAlert("Ничего не импортировано", "Все устройства пропущены.", "info");
    return;
  }
  // v1.28.10c: battery_powered — отдельное поле item (переопределяемо).
  PREVIEW_DEVICES = prepared.map(p => ({
    device: p,
    enabled_dps: {},
    battery_powered: !!p.battery_powered,
  }));

  for (const item of PREVIEW_DEVICES) {
    const _cr = item.device._cloud_ref;
    const _isKnown = _isCloudDeviceInConfig(_cr);
    const _kc = _isKnown ? _knownConfigDeviceForCloud(_cr) : null;
    const _cfgMap = (_kc && _kc.dps_map) ? _kc.dps_map : null;
    // v1.28.40: для уже-в-конфиге — режим просмотра (правка по ✏️).
    item._isKnown = _isKnown;
    item._edit = !_isKnown;
    const _autoMap = getDeviceMapping(_cr);
    const _dps = _previewAllDps(item);
    item.enabled_dps = {};
    if (_dps.length === 0) {
      item.needs_probe = true;
    } else {
      const _idx = PREVIEW_DEVICES.indexOf(item);
      for (const dp of _dps) {
        if (_cfgMap) {
          // v1.28.40: по умолчанию показываем то, что уже настроено в конфиге.
          item.enabled_dps[dp] = Object.prototype.hasOwnProperty.call(_cfgMap, dp);
        } else {
          const _r = _previewResolveRow(item, _idx, dp);
          const code = (_r.m && _r.m.code) || "";
          const inAuto = Object.prototype.hasOwnProperty.call(_autoMap, dp);
          item.enabled_dps[dp] = inAuto && !JUNK_DP_CODES.has(code);
        }
      }
    }
  }

  // v1.28.72: для уже-добавленных по умолчанию «Текущий» (конфиг как есть),
  // для новых — «Авто».
  PREVIEW_ROW_SOURCE = {};
  PREVIEW_SOURCE = PREVIEW_DEVICES.some(it => !it._isKnown) ? "auto" : "cache";

  PREVIEW_CURRENT = 0;
  renderImportPreview();
  document.getElementById("preview-overlay").classList.add("open");
}

function renderImportPreview() {
  if (PREVIEW_DEVICES.length === 0) return;
  const body = document.getElementById("preview-body");
  // v1.18.10: сохраняем и восстанавливаем скролл, чтобы перерисовка
  // (например, при Probe все) не сбрасывала позицию наверх.
  const scrollTop = body.scrollTop;
  const scrollLeft = body.scrollLeft;   // v1.28.79: не «слайдить» таблицу
  // v1.28.82: сохраняем скролл ВСЕХ таблиц — перерисовка не должна
  // «откидывать» их (кнопки/селекторы вызывают renderImportPreview).
  const _scrolls = _captureScrolls(body);
  const title = document.getElementById("preview-title");
  const counter = document.getElementById("preview-counter");
  const nav = document.getElementById("preview-mobile-nav");

  if (isMobile()) {
    // v1.28.50: при одном устройстве навигация «Назад/Далее» не нужна.
    nav.style.display = (PREVIEW_DEVICES.length > 1) ? "flex" : "none";
    counter.textContent = `${PREVIEW_CURRENT + 1}/${PREVIEW_DEVICES.length}`;
    renderPreviewMobile(body);
  } else {
    nav.style.display = "none";
    renderPreviewDesktop(body);
  }
  title.textContent = `Превью импорта (${PREVIEW_DEVICES.length} устройств)`;
  // v1.28.86: одиночный Probe избыточен — он есть в шапке каждого устройства.
  const _probeAllBtn = document.getElementById("preview-probe-all-btn");
  if (_probeAllBtn) {
    const _multi = PREVIEW_DEVICES.length > 1;
    _probeAllBtn.style.display = _multi ? "" : "none";
    _probeAllBtn.textContent = "🔍 Опросить все";
  }
  body.scrollTop = scrollTop;
  body.scrollLeft = scrollLeft;
  _restoreScrolls(body, _scrolls);
  _updateImportButton();

  // v1.28.33: убран mousedown-перехват чекбоксов. Проблема
  // focus-scroll (1.18.11) больше не актуальна в современных
  // браузерах, а перехват ломал нативный toggle.
}

// v1.28.40: кнопка импорта зависит от состава превью:
//   только новые → «Импортировать всё»;
//   есть и новые, и уже-в-конфиге → «Импортировать новые»;
//   только уже-в-конфиге → форма только для просмотра.
function _updateImportButton() {
  const btn = document.getElementById("preview-import-btn");
  if (!btn) return;
  let newCount = 0, knownCount = 0, enabledNew = 0;
  for (const it of PREVIEW_DEVICES) {
    if (_isCloudDeviceInConfig(it.device._cloud_ref)) { knownCount++; continue; }
    newCount++;
    if (Object.values(it.enabled_dps || {}).some(Boolean)) enabledNew++;
  }
  if (newCount === 0) {
    btn.disabled = true;
    btn.textContent = "⚠️ Форма только для просмотра";
    return;
  }
  if (enabledNew === 0) {
    btn.disabled = true;
    btn.textContent = "⚠️ Не выбрано ни одного DP";
    return;
  }
  btn.disabled = false;
  btn.textContent = (knownCount > 0) ? "📦 Импортировать новые" : "📦 Импортировать всё";
}

// v1.28.40: ✏️ — режим правки для уже добавленных (по умолчанию — просмотр).
function previewToggleEdit(idx) {
  const item = PREVIEW_DEVICES[idx];
  if (!item) return;
  item._edit = !(item._edit === true);
  renderImportPreview();
}

// ===== v1.28.34: источники данных превью импорта =====
// Глобальный источник (Авто/Cloud/cache/tuya-local/эвристика) + ручное
// переопределение на строку. Правило: как только есть ручная строка —
// глобальный селект показывает «Ручной» (сброс ручных → «Авто»).
function _previewRowKey(idx, dp) { return idx + ":" + dp; }
function _previewHasManualRows() { return Object.keys(PREVIEW_ROW_SOURCE).length > 0; }

function _previewEffectiveSource(idx, dp) {
  // v1.28.70: верхний селект «Источник данных» влияет на новые устройства
  // всегда; на уже добавленные — только когда новых нет вовсе (это
  // предпросмотр: такие устройства в конфиг не импортируются).
  const item = PREVIEW_DEVICES[idx];
  const k = _previewRowKey(idx, dp);
  const rowSrc = PREVIEW_ROW_SOURCE[k];
  const _hasNew = PREVIEW_DEVICES.some(it => !it._isKnown);
  if (item && item._isKnown) {
    if (item._edit === true && rowSrc) return rowSrc;
    // v1.28.72: у уже добавленных по умолчанию — «Текущий» (конфиг как есть).
    // Если новых нет — шапка управляет ими для предпросмотра.
    if (_hasNew) return "cache";
    return rowSrc || PREVIEW_SOURCE;
  }
  return rowSrc || PREVIEW_SOURCE;
}

function setPreviewSource(src) {
  if (src === "manual") return;   // «Ручной» — состояние, не выбор
  PREVIEW_SOURCE = src;
  // v1.28.83: выбор в шапке сбрасывает ВСЕ ручные переопределения строк —
  // иначе бейдж «Ручной» «залипал» (перерисовка не снимала ручной режим).
  PREVIEW_ROW_SOURCE = {};
  renderImportPreview();
}

function _previewCloudMapping(d) {
  if (!d) return null;
  if (d.mapping && Object.keys(d.mapping).length) return d.mapping;
  if (d._raw_cloud && d._raw_cloud.mapping && Object.keys(d._raw_cloud.mapping).length) {
    return d._raw_cloud.mapping;
  }
  return null;
}

function _previewKnownDevice(name) {
  return (typeof LAST_DEVICES !== "undefined" ? LAST_DEVICES : [])
    .find(x => x.name === name) || null;
}

function _previewCurrentValues(d) {
  // v1.28.40: значения из Cloud status (code->value). _raw_properties
  // содержит только метаданные (type/values), без текущих значений —
  // раньше колонка «Текущее» была пустой.
  const out = {};
  const cs = d && d.cloud_status;
  if (cs && typeof cs === "object") {
    for (const [k, v] of Object.entries(cs)) out[k] = v;
  }
  const st = d && d._raw_cloud && d._raw_cloud.status;
  if (Array.isArray(st)) {
    for (const it of st) {
      if (it && it.code) out[it.code] = it.value;
    }
  }
  return out;
}

// Карта источника: dp -> {code, type, values, name, component?}.
function _previewSourceMapping(item, src) {
  const d = (item.device && item.device._cloud_ref) ? item.device._cloud_ref : {};
  if (src === "cloud") {
    const out = {};
    for (const [dp, m] of Object.entries(_previewCloudMapping(d) || {})) {
      if (m && typeof m === "object") out[dp] = Object.assign({}, m);
    }
    return out;
  }
  if (src === "tuya_local") {
    const out = {};
    for (const [dp, info] of Object.entries(d.dps_map_generated || {})) {
      if (info && info._dps_source === "tuya_local") {
        out[dp] = Object.assign({}, info, { code: info.name || info.code || ("dp_" + dp) });
      }
    }
    return out;
  }
  if (src === "cache") {
    // v1.28.34: только для устройств, уже бывших в конфиге (нет кэша — пусто).
    // v1.28.71: ищем по tuya_id (имена Cloud и конфига часто не совпадают).
    const known = _knownConfigDeviceForCloud(d) || _previewKnownDevice(d.name);
    const out = {};
    if (known && known.dps_map) {
      for (const [dp, info] of Object.entries(known.dps_map)) {
        if (info && typeof info === "object") {
          out[dp] = Object.assign({}, info, { code: info.name || info.code || ("dp_" + dp) });
        }
      }
    }
    return out;
  }
  if (src === "heuristic") {
    // Component по текущему значению (bool→switch, number→number, строка→sensor).
    const base = getDeviceMapping(d);
    const vals = _previewCurrentValues(d);
    const out = {};
    for (const [dp, m] of Object.entries(base)) {
      const code = m.code || "";
      const comp = Object.prototype.hasOwnProperty.call(vals, code)
        ? _dpsFillSuggestFromValue(vals[code]) : "sensor";
      out[dp] = Object.assign({}, m, { component: comp });
    }
    return out;
  }
  return getDeviceMapping(d);   // auto
}

function _previewSourceAvailable(item, src) {
  if (src === "cache") return Object.keys(_previewSourceMapping(item, "cache")).length > 0;
  return true;
}

function _previewAllDps(item) {
  const set = new Set();
  for (const s of ["auto", "cloud", "cache", "tuya_local", "heuristic"]) {
    for (const dp of Object.keys(_previewSourceMapping(item, s))) set.add(dp);
  }
  return Array.from(set).sort((a, b) => (parseInt(a) || 0) - (parseInt(b) || 0));
}

function _previewResolveRow(item, idx, dp) {
  const src = _previewEffectiveSource(idx, dp);
  const m = _previewSourceMapping(item, src)[dp];
  if (m) return { m, src };
  // v1.28.73: у выбранного источника нет записи для DP — показываем
  // реальный источник, из которого реально возьмём (cloud/tuya-local/…).
  const fb = getDeviceMapping(item.device._cloud_ref)[dp];
  if (!fb) return { m: {}, src };
  return { m: fb, src: _previewResolvedOrigin(item, dp) };
}

// v1.28.68: bridge-forced DP в превью — {key, info} или null.
// Определяем по имени записи и/или облачному code этого DP.
function _previewBridgeForced(item, dp, entry) {
  const d = (item.device && item.device._cloud_ref) ? item.device._cloud_ref : {};
  const cm = _previewCloudMapping(d) || {};
  const code = (cm[dp] && cm[dp].code) || (entry && (entry.code || "")) || "";
  const k = _bridgeForcedKey(entry && entry.name, code) || _bridgeForcedKey(code, code);
  return k ? { key: k, info: _BRIDGE_FORCED_NAMES[k] } : null;
}

function _previewResolvedOrigin(item, dp) {
  // v1.28.35: фактический источник для «Авто» — наводим порядок:
  // 1) _dps_source из dps_map_generated, 2) иначе это Cloud mapping.
  const d = (item.device && item.device._cloud_ref) ? item.device._cloud_ref : {};
  const gen = (d.dps_map_generated || {})[dp];
  if (gen && gen._dps_source) return gen._dps_source;
  if (gen) return "cloud";
  const cm = _previewCloudMapping(d);
  if (cm && cm[dp]) return "cloud";
  return "unknown";
}

function _previewSourceLabel(id) {
  if (id === "unknown") return "не определён";
  const s = PREVIEW_SOURCES.find(x => x.id === id);
  return s ? s.label : id;
}

// v1.28.43: Δ-дифф (добавлено/изменено/удалено) относительно текущего конфига.
// Отдельная функция — чтобы пересчитывать при клике по галочкам, не
// перерисовывая всю карточку.
function _computePreviewDiff(item, idx) {
  const d = item.device;
  if (!_isCloudDeviceInConfig(d._cloud_ref)) return null;
  const _knownDev = _knownConfigDeviceForCloud(d._cloud_ref) || _previewKnownDevice(d.name);
  if (!_knownDev || !_knownDev.dps_map) return null;
  const curMap = _knownDev.dps_map || {};
  const _enabledDps = Object.keys(item.enabled_dps).filter(x => item.enabled_dps[x]);
  const nextSet = new Set(_enabledDps);
  let _add = 0, _rem = 0, _chg = 0;
  for (const dp of _enabledDps) {
    const old = curMap[dp];
    if (!old) { _add++; continue; }
    const nw = (_previewResolveRow(item, idx, dp).m) || {};
    const nwV = nw.values || {};
    const oldCode = old.name || old.code || "";
    const newCode = nw.code || nw.name || "";
    let _diff = (oldCode !== newCode);
    if (!_diff) {
      const _pairs = [
        ["component", old.component, nw.component],
        ["unit", old.unit, nw.unit !== undefined ? nw.unit : nwV.unit],
        ["scale", old.scale, nw.scale !== undefined ? nw.scale : nwV.scale],
        ["min", old.min, nw.min !== undefined ? nw.min : nwV.min],
        ["max", old.max, nw.max !== undefined ? nw.max : nwV.max],
        ["device_class", old.device_class, nw.device_class],
      ];
      for (const [, a0, b0] of _pairs) {
        const a = (a0 === undefined || a0 === null) ? "" : String(a0);
        const b = (b0 === undefined || b0 === null) ? "" : String(b0);
        if (a !== b) { _diff = true; break; }
      }
    }
    if (!_diff) {
      const aOpt = Array.isArray(old.options) ? old.options.join(",") : "";
      const bRng = Array.isArray(nwV.range) ? nwV.range
                 : (Array.isArray(nw.options) ? nw.options : []);
      if (aOpt !== bRng.join(",")) _diff = true;
    }
    if (_diff) _chg++;
  }
  for (const dp of Object.keys(curMap)) if (!nextSet.has(dp)) _rem++;
  return { add: _add, chg: _chg, rem: _rem };
}

function _diffHtmlOf(diff) {
  return (diff === null) ? "" :
    `<span class="preview-diff" title="Изменения относительно текущего конфига">Δ <b style="color:var(--green);">+${diff.add}</b> / <b style="color:var(--yellow);">~${diff.chg}</b> / <b style="color:var(--red);">−${diff.rem}</b></span>`;
}

function _renderPreviewSourceBar() {
  const manual = _previewHasManualRows();
  const _hasNew = PREVIEW_DEVICES.some(it => !it._isKnown);
  const _hasKnown = PREVIEW_DEVICES.some(it => it._isKnown);
  const _enable = _hasNew || _hasKnown;
  // v1.28.86: массовые кнопки — только если есть что редактировать
  // (новые устройства или включённая песочница 🧪 у известных).
  const _editable = PREVIEW_DEVICES.some(it => !it._isKnown || it._edit === true);
  let btns = "";
  if (manual) {
    btns += `<button type="button" class="active" title="Есть ручные строки">✋ Ручной</button>`;
  }
  for (const s of PREVIEW_SOURCES) {
    const active = !manual && PREVIEW_SOURCE === s.id;
    // v1.28.72: «Текущий» (конфиг как есть) — только для уже добавленных.
    const _dis = (s.id === "cache") ? !_hasKnown : !_enable;
    btns += `<button type="button" class="${active ? "active" : ""}" ${_dis ? "disabled" : ""} onclick="setPreviewSource('${s.id}')" title="${s.label}">${s.icon} ${s.label}</button>`;
  }
  // v1.28.70: если новых устройств нет — шапка управляет уже добавленными,
  // но только для предпросмотра (в конфиг такие не импортируются).
  const _hint = _hasNew
    ? ""
    : (_hasKnown
        ? '<span class="muted" style="font-size:11px;">предпросмотр: устройства уже добавлены, изменения не сохраняются</span>'
        : "");
  return `<div class="preview-source-bar">
    <label class="preview-source-label">Источник данных:</label>
    <span class="preview-src-btns">${btns}</span>
    ${_hint}
    ${_editable ? `<span class="preview-mass-btns">
      <button type="button" onclick="previewSetAll(true)" title="Включить все DP">✅ Все</button>
      <button type="button" onclick="previewSetAll(false)" title="Выключить все DP">⬜ Ничего</button>
      <button type="button" onclick="previewSetNonJunk()" title="Выключить мусорные DP">🚫 Без мусора</button>
    </span>` : ""}
  </div>`;
}

// v1.28.40: массовые чекбоксы превью. Действуют только на редактируемые
// карточки (у уже-в-конфиге по умолчанию режим просмотра — включи ✏️).
function previewSetAll(v) {
  for (const item of PREVIEW_DEVICES) {
    if (item._edit === false) continue;
    for (const dp of Object.keys(item.enabled_dps)) item.enabled_dps[dp] = !!v;
  }
  renderImportPreview();
}

function previewSetNonJunk() {
  for (const item of PREVIEW_DEVICES) {
    if (item._edit === false) continue;
    const idx = PREVIEW_DEVICES.indexOf(item);
    for (const dp of Object.keys(item.enabled_dps)) {
      const m = (_previewResolveRow(item, idx, dp).m) || {};
      item.enabled_dps[dp] = !JUNK_DP_CODES.has(m.code || "");
    }
  }
  renderImportPreview();
}

function _previewSourceIcon(id) {
  const s = PREVIEW_SOURCES.find(x => x.id === id);
  return s ? s.icon : "❓";
}
function previewPickSrc(idx, dp, src) {
  PREVIEW_SRC_OPEN = null;
  const k = _previewRowKey(idx, dp);
  if (!src) delete PREVIEW_ROW_SOURCE[k];
  else PREVIEW_ROW_SOURCE[k] = src;
  renderImportPreview();
}

let PREVIEW_SRC_OPEN = null;   // v1.28.79: открытый список источника ("idx:dp")
let _PREVIEW_SRC_MENU_EL = null;

// v1.28.81: «источник взят из шапки» — точка на самом элементе списка.
function _previewSrcItemsHtml(item, idx, dp) {
  const cur = PREVIEW_ROW_SOURCE[_previewRowKey(idx, dp)] || "";
  let items = "";
  for (const s of PREVIEW_SOURCES) {
    if (s.id === "auto") continue;
    const avail = _previewSourceAvailable(item, s.id);
    const isHdr = (s.id === PREVIEW_SOURCE);
    const isActive = cur ? (cur === s.id) : isHdr;
    items += `<button type="button" class="prev-src-item${isActive ? " active" : ""}"`
          + ` ${avail ? "" : "disabled"} onclick="previewPickSrc(${idx}, '${escapeAttr(dp)}', '${s.id}')">`
          + `${s.icon} ${escapeHtml(s.label)}`
          + (isHdr ? `<span class="prev-src-dot" title="источник из шапки">•</span>` : "")
          + `${avail ? "" : " (нет)"}</button>`;
  }
  return items;
}
function _previewCloseSrcMenuEl() {
  if (_PREVIEW_SRC_MENU_EL && _PREVIEW_SRC_MENU_EL.parentNode) {
    _PREVIEW_SRC_MENU_EL.parentNode.removeChild(_PREVIEW_SRC_MENU_EL);
  }
  _PREVIEW_SRC_MENU_EL = null;
}
function previewToggleSrc(idx, dp, evt) {
  if (evt) evt.stopPropagation();
  const k = _previewRowKey(idx, dp);
  if (PREVIEW_SRC_OPEN === k) { previewCloseSrc(); return; }
  _previewCloseSrcMenuEl();
  PREVIEW_SRC_OPEN = k;
  const item = PREVIEW_DEVICES[idx];
  const menu = document.createElement("div");
  menu.className = "prev-src-menu";
  menu.setAttribute("data-key", k);
  menu.innerHTML = _previewSrcItemsHtml(item, idx, dp);
  document.body.appendChild(menu);
  _PREVIEW_SRC_MENU_EL = menu;
  _positionPreviewSrcMenu(k);
  setTimeout(() => {
    document.addEventListener("click", previewCloseSrc, { once: true });
    window.addEventListener("scroll", previewCloseSrc, { once: true, capture: true });
  }, 0);
}
function previewCloseSrc() {
  _previewCloseSrcMenuEl();
  if (PREVIEW_SRC_OPEN) { PREVIEW_SRC_OPEN = null; renderImportPreview(); }
}
function _positionPreviewSrcMenu(k) {
  const menu = _PREVIEW_SRC_MENU_EL;
  const btn = document.querySelector(`.prev-src-btn[data-key="${k}"]`);
  if (!menu || !btn) return;
  const r = btn.getBoundingClientRect();
  const w = Math.min(260, window.innerWidth - 16);
  menu.style.minWidth = w + "px";
  const h = menu.offsetHeight || 180;
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + "px";
  menu.style.top = top + "px";
}

function _renderPreviewRowSource(item, idx, dp) {
  // v1.28.41: источник данных — только для новых.
  // v1.28.42: в песочнице — и для известных.
  // v1.28.70: когда новых нет — шапка управляет известными (предпросмотр).
  if (item._isKnown && item._edit !== true) {
    const _eff = _previewResolveRow(item, idx, dp).src || "auto";
    if (_eff && _eff !== "auto") {
      return `<span class="muted" title="предпросмотр — изменения не сохраняются">`
           + `${_previewSourceIcon(_eff)} ${escapeHtml(_previewSourceLabel(_eff))}</span>`;
    }
    return '<span class="muted">—</span>';
  }
  const k = _previewRowKey(idx, dp);
  const _eff = _previewResolveRow(item, idx, dp).src || "auto";
  return `<button type="button" class="prev-src-btn" data-key="${k}"`
    + ` onclick="previewToggleSrc(${idx}, '${escapeAttr(dp)}', event)">`
    + `${_previewSourceIcon(_eff)} ${escapeHtml(_previewSourceLabel(_eff))}</button>`;
}

function renderPreviewDeviceBlock(item, idx) {
  const d = item.device;
  const codeToName = {};
  if (d._cloud_ref?._raw_properties) {
    for (const section of ["functions", "status"]) {
      const arr = d._cloud_ref._raw_properties[section];
      if (Array.isArray(arr)) {
        for (const i2 of arr) if (i2.code) codeToName[i2.code] = i2.name || "";
      }
    }
  }
  const total = _previewAllDps(item).length;
  const enabled = Object.values(item.enabled_dps).filter(x => x).length;

  // v1.27.6: если Cloud-устройство уже в конфиге — крестик и бейдж.
  const _inCfg = _isCloudDeviceInConfig(d._cloud_ref);
  const _alreadyBadge = _inCfg
    ? ' <span class="preview-already-badge" title="Уже добавлено в конфиг">✅ уже в конфиге</span>'
    : '';
  const _removeBtn = _inCfg
    ? `<button class="preview-remove-btn" onclick="previewRemoveDevice(${idx})" title="Убрать из импорта">✕</button>`
    : '';
  // v1.27.7: класс has-remove-btn — чтобы header зарезервировал место.
  const _cardCls = "preview-device" + (_inCfg ? " has-remove-btn" : "");
  // v1.27.7d: плашка ошибки импорта для конкретного устройства.
  const _errMsg = (item && item.import_error) ? item.import_error : "";
  const _errHtml = _errMsg
    ? `<div class="preview-device-error">⚠️ ${escapeHtml(_errMsg)}</div>`
    : "";
  // v1.28.10c: визуальный тумблер «Батарейное». Лейбл меняет текст
  // и иконку по состоянию через CSS :checked. Badge из header убран.
  const _batteryChecked = item.battery_powered ? "checked" : "";
  const _batteryToggle = `<label class="battery-switch" title="Устройство работает от батареи — bridge опрашивает его через ICMP ping, HA полагается на expire_after">
      <input type="checkbox" class="battery-switch-input preview-battery-input" ${_batteryChecked}
             onchange="togglePreviewBattery(${idx}, this.checked)">
      <span class="battery-switch-track">
        <span class="battery-switch-thumb"></span>
      </span>
      <span class="battery-switch-label">
        <span class="battery-label-on">🔋 Батарейное устройство</span>
        <span class="battery-label-off">🔌 Проводное устройство</span>
      </span>
    </label>`;

  // v1.28.16: preview preset_map для climate.
  const _presetOptions = (d.type === "climate")
    ? _climatePresetOptions(d)
    : [];
  const _presetPreviewHtml = _renderPresetPreview(_presetOptions, PRESET_LANG);

  // v1.28.43: устройство уже в конфиге — DP-дифф (пересчитываем в
  // _computePreviewDiff, чтобы обновлять при клике по галочкам).
  const _diffHtml = _diffHtmlOf(_computePreviewDiff(item, idx));
  // v1.28.42: 🧪 — «песочница»: по умолчанию «как настроено», клик — поиграть
  // (ничего не сохраняется). Активное состояние — красная рамка + подпись.
  const _sandboxOn = item._edit === true;
  const _editBtn = _inCfg
    ? `<button type="button" class="preview-edit-btn${_sandboxOn ? " active" : ""}" onclick="previewToggleEdit(${idx})" title="${_sandboxOn ? "Выключить песочницу (вернуть «как настроено»)" : "Песочница: посмотреть все DP/значения (не сохраняется)"}">🧪</button>`
      + `<span class="preview-sandbox-label">${_sandboxOn ? "Песочница (не сохраняется)" : "Как настроено"}</span>`
    : "";

  const _probeBtn = `<button type="button" class="preview-edit-btn" onclick="probeDeviceItem(PREVIEW_DEVICES[${idx}], ${idx})" title="Опрос устройства локально (probe): сопоставить DP, если Cloud не дал mapping">🔍 Опросить устройство</button>`;

  let html = `<div class="${_cardCls}" data-idx="${idx}">
    ${_removeBtn}
    <div class="preview-device-header">
      <span>${escapeHtml(d.friendly_name)} <span class="muted" style="font-weight:400; font-size:11px;">(${escapeHtml(d.name)})</span>${_alreadyBadge}</span>
      <span class="muted" style="font-size:11px;">${escapeHtml(d.ip)} · ${versionBadge(d.version)} · <span class="preview-dp-count">${enabled}/${total} DP</span> <span class="muted preview-probe-status" data-idx="${idx}" style="font-size:11px;">${item.probe_status_html || ""}</span></span>
    </div>
    <div class="preview-device-header-tools">${_batteryToggle}${_presetPreviewHtml}${_diffHtml}${_editBtn}${_probeBtn}</div>
    <div class="preview-device-body">${_errHtml}`;

  const _allDps = _previewAllDps(item);
  if (_allDps.length === 0) {
    html += `<div class="cloud-warn">⚠️ Cloud не вернул mapping. Нажми «🔍 Probe» — DP будут сопоставлены по значениям из Cloud status + типам.</div>`;
  } else {
    const _curVals = _previewCurrentValues(d._cloud_ref);
    const _known = _previewKnownDevice(d.name);
    // v1.28.41: «Текущее» показываем всегда; источник (☁/📦) — у значения.
    const _showCurrent = true;
    html += `<div class="wide-table-wrap"><table class="detail-table wide-table preview-dp-table"><colgroup>
      <col class="col-check"><col class="col-dp"><col class="col-code">
      <col class="col-translate"><col class="col-component"><col class="col-type">
      <col class="col-values">${_showCurrent ? '<col class="col-current">' : ''}<col class="col-src"><col class="col-rowsrc">
    </colgroup><thead><tr>
      <th></th><th>DP</th><th>Code</th><th class="col-translate">Перевод</th><th>Component</th>
      <th>Тип</th><th>Значения</th>${_showCurrent ? '<th>Текущее</th>' : ''}<th>Источник</th><th class="col-rowsrc">Выбор</th>
    </tr></thead><tbody>`;
    for (const dp of _allDps) {
      // v1.28.34: строка разрешается по эффективному источнику
      // (ручной per-row → глобальный → авто).
      const _res = _previewResolveRow(item, idx, dp);
      const m = _res.m || {};
      const code = m.code || "";
      const _cn = codeToName[code] || "";
      const _cfg = m.name || "";
      const _r = resolveDpDisplay(code, _cn, _cfg);
      const name = _r.display;   // = code || '?'
      const isJunk = JUNK_DP_CODES.has(code);
      // v1.28.40: BUG был в том, что boolean печатался как "true"/"false"
      // (невалидный атрибут) — галки не проставлялись. Теперь строка.
      const _enabled = Object.prototype.hasOwnProperty.call(item.enabled_dps, dp)
        ? !!item.enabled_dps[dp] : !isJunk;
      const _cbAttrs = (_enabled ? "checked" : "") + (item._edit === false ? " disabled" : "");
      const rowCls = isJunk ? "junk-row" : "";
      // v1.25.0 (fix #11): «Component» — component DP, а не тип устройства.
      const _component = m.component
        || (d.dps_map && d.dps_map[dp] && d.dps_map[dp].component) || "—";
      const _tip = _dpTranslateCell(code, _cn, _cfg);
      // v1.27.5: badge junk справа от DP (ПК — текст, мобиль — 🗑).
      const _junkBadge = isJunk
        ? ' <span class="badge junk"><span class="junk-text">мусор</span><span class="junk-icon">🗑</span></span>'
        : '';
      // v1.28.42: bridge-forced — по имени ИЛИ облачному code.
      const _bfInfoPrev = _bridgeForcedInfo(name, code);
      const _bfBadge = _bfInfoPrev
        ? `<span class="badge-locked" data-tip="${escapeAttr("Bridge-forced: облако '" + _bfInfoPrev.cloud + "' → '" + name + "' для " + _bfInfoPrev.why)}">🔒</span>`
        : '';
      const _type = m.type || m._cloud_type || "";
      // v1.28.41: «Текущее» — raw, с пометкой источника (☁ cloud / 📦 cache)
      // и скалированным значением в тултипе.
      let _curVal, _curFrom = "";
      if (Object.prototype.hasOwnProperty.call(_curVals, code)) {
        _curVal = _curVals[code]; _curFrom = "cloud";
      } else if (_known && _known.cache && _known.cache[dp] !== undefined) {
        _curVal = _known.cache[dp]; _curFrom = "cache";
      }
      const _scale = (m.values && m.values.scale !== undefined)
        ? m.values.scale : m.scale;
      const _curScaled = (_scale !== undefined && _curVal !== undefined) ? _scaleVal(_curVal, _scale) : null;
      const _curTitle = (_curScaled !== null && String(_curScaled) !== String(_curVal))
        ? `Масштаб (scale ${_scale}): ${_curScaled}` : "";
      const _curOriginIcon = _curFrom === "cloud" ? "☁" : (_curFrom === "cache" ? "📦" : "");
      const _srcIcon = { cloud: "☁", tuya_local: "📚", cache: "⚙️", heuristic: "⚠️", auto: "🤖", unknown: "❓" };
      // v1.28.70: показываем РЕАЛЬНЫЙ источник, из которого будет взято
      // сопоставление (у «Авто» — вычисленный), а не слово «Авто».
      const _origin = (_res.src === "auto") ? _previewResolvedOrigin(item, dp) : _res.src;
      const _showSrc = _origin || "unknown";
      const _srcTip = (_res.src === "auto")
        ? `Авто → ${_previewSourceLabel(_showSrc)}`
        : `Сопоставление: ${_previewSourceLabel(_res.src)}`;
      const _srcBadge = `<span class="dp-tip preview-src-badge src-${_showSrc}" data-tip="${escapeAttr(_srcTip)}">${_srcIcon[_showSrc] || "❓"} ${escapeHtml(_previewSourceLabel(_showSrc))}</span>`;
      const _curFromTip = _curFrom === "cloud" ? "значение из Cloud status" : (_curFrom === "cache" ? "значение из кэша bridge" : "");
      const _curTipAll = [_curFromTip, _curTitle].filter(Boolean).join(" · ");
      // v1.28.75: тултип — через общий #dp-tooltip, зона = значок.
      const _curCell = _showCurrent
        ? `<td class="col-current muted" style="font-size:11px;">`
          + (_curVal !== undefined
              ? (_curOriginIcon
                  ? `<span class="dp-tip cur-origin"${_curTipAll ? ` data-tip="${escapeAttr(_curTipAll)}"` : ""}>${_curOriginIcon}</span> `
                  : "")
                + escapeHtml(_shortVal(_curVal))
              : "—")
          + `</td>`
        : '';
      html += `<tr class="${rowCls}">
        <td><input type="checkbox" ${_cbAttrs} onchange="togglePreviewDp(${idx}, '${escapeAttr(dp)}', this.checked)"></td>
        <td><strong>${escapeHtml(dp)}</strong>${_junkBadge}</td>
        <td><span class="code-line">${copyCodePlain(code)}${_bfBadge}</span></td>
        <td class="muted" style="font-size:11px;">${_tip}</td>
        <td>${componentBadge(_component)}</td>
        <td>${_type ? typeBadge(_type) : '<span class="muted">—</span>'}</td>
        <td>${_valuesCellHtml(m.values)}</td>
        ${_curCell}
        <td>${_srcBadge}</td>
        <td class="col-rowsrc">${_renderPreviewRowSource(item, idx, dp)}</td>
      </tr>`;
    }
    html += `</tbody></table></div>`;
  }
  html += `</div></div>`;
  return html;
}

function renderPreviewDesktop(body) {
  let html = "";
  html += _renderPreviewSourceBar();
  html += _renderPresetLangRadio();
  html += `<p class="muted">Проверьте DP перед импортом. Мусорные (🗑) выключены по умолчанию. <b>countdown_*</b> включены.</p>`;
  for (let i = 0; i < PREVIEW_DEVICES.length; i++) {
    html += renderPreviewDeviceBlock(PREVIEW_DEVICES[i], i);
  }
  body.innerHTML = html;
}

function renderPreviewMobile(body) {
  if (PREVIEW_CURRENT < 0 || PREVIEW_CURRENT >= PREVIEW_DEVICES.length) return;
  let html = "";
  html += _renderPreviewSourceBar();
  html += _renderPresetLangRadio();
  html += `<p class="muted">Устройство ${PREVIEW_CURRENT + 1} из ${PREVIEW_DEVICES.length}</p>`;
  html += renderPreviewDeviceBlock(PREVIEW_DEVICES[PREVIEW_CURRENT], PREVIEW_CURRENT);
  html += `<button onclick="probeCurrentDevice()" style="margin-top:12px;">🔍 Проверить устройство (probe)</button>`;
  html += `<div id="probe-result" class="muted" style="margin-top:8px; font-size:12px;"></div>`;
  body.innerHTML = html;
}

function togglePreviewDp(deviceIdx, dp, checked) {
  if (deviceIdx >= PREVIEW_DEVICES.length) return;

  // v1.18.11: сохраняем скролл в самом начале — до всего остального.
  // Firefox/Chrome могут "прыгнуть" к фокусному чекбоксу внутри
  // overflow:auto контейнера; scroll-margin в CSS это гасит, но
  // подстрахуемся и вернём scrollTop синхронно в конце функции.
  const body = document.getElementById("preview-body");
  const st = body ? body.scrollTop : 0;

  const item = PREVIEW_DEVICES[deviceIdx];
  item.enabled_dps[dp] = checked;

  // Обновляем только счётчик «N/M DP» и Δ в шапке карточки —
  // без перерисовки всего списка.
  const card = document.querySelector(`.preview-device[data-idx="${deviceIdx}"]`);
  if (card) {
    const counterEl = card.querySelector(".preview-dp-count");
    if (counterEl) {
      const total = _previewAllDps(item).length;
      const enabled = Object.values(item.enabled_dps).filter(x => x).length;
      counterEl.textContent = `${enabled}/${total} DP`;
    }
    // v1.28.43: Δ-дифф пересчитываем при клике по галочке.
    const diffEl = card.querySelector(".preview-diff");
    if (diffEl) {
      const _diff = _computePreviewDiff(item, deviceIdx);
      if (_diff) {
        diffEl.innerHTML = `Δ <b style="color:var(--green);">+${_diff.add}</b> / <b style="color:var(--yellow);">~${_diff.chg}</b> / <b style="color:var(--red);">−${_diff.rem}</b>`;
      } else {
        diffEl.textContent = "";
      }
    }
  }
  // v1.28.43: кнопка импорта зависит от числа выбранных DP.
  _updateImportButton();

  // v1.18.11: возвращаем скролл.
  if (body) body.scrollTop = st;
}

// v1.28.10c: переключение «Батарейное» в превью.
// Лейбл тумблера меняет текст/иконку автоматически через CSS :checked —
// DOM трогать не нужно. Единственная работа — обновить item.
function togglePreviewBattery(idx, checked) {
  if (idx < 0 || idx >= PREVIEW_DEVICES.length) return;
  const item = PREVIEW_DEVICES[idx];
  item.battery_powered = !!checked;
}

// v1.27.6: убрать устройство из превью импорта (крестик).
function previewRemoveDevice(idx) {
  if (idx < 0 || idx >= PREVIEW_DEVICES.length) return;
  PREVIEW_DEVICES.splice(idx, 1);
  if (PREVIEW_DEVICES.length === 0) {
    closePreview();
    uiAlert(
      "Превью пусто",
      "Все устройства из превью убраны.\n\n" +
      "Импорт не требуется.",
      "info"
    );
    return;
  }
  PREVIEW_CURRENT = Math.min(PREVIEW_CURRENT, PREVIEW_DEVICES.length - 1);
  renderImportPreview();
}

function previewPrev() { if (PREVIEW_CURRENT > 0) { PREVIEW_CURRENT--; renderImportPreview(); } }
function previewNext() { if (PREVIEW_CURRENT < PREVIEW_DEVICES.length - 1) { PREVIEW_CURRENT++; renderImportPreview(); } }

function closePreview(evt) {
  if (evt && evt.target && evt.target.id !== "preview-overlay") return;
  document.getElementById("preview-overlay").classList.remove("open");
  // v1.27.7d: чистим баннер ошибок при закрытии.
  _clearPreviewErrors();
  // v1.28.42: песочница и ручные источники не сохраняются — сбрасываем.
  PREVIEW_ROW_SOURCE = {};
  PREVIEW_SOURCE = "auto";
  for (const item of PREVIEW_DEVICES) {
    if (item._isKnown) item._edit = false;
  }
}

async function probeCurrentDevice() {
  if (isMobile()) {
    const item = PREVIEW_DEVICES[PREVIEW_CURRENT];
    if (!item) return;
    await probeDeviceItem(item, PREVIEW_CURRENT);
  } else {
    await probeAllInPreview();
  }
}

// v1.23.0: прогресс probe с pending/ok/fail в заголовке + подсветка карточки
let _probeStats = { ok: 0, fail: 0, total: 0 };

function _updateProbeTitle() {
  const title = document.getElementById("preview-title");
  if (!title) return;
  const s = _probeStats;
  // v1.25.12: pending = сколько ещё не завершено
  const pending = Math.max(0, s.total - s.ok - s.fail);
  title.textContent = `Probe: ✅ ${s.ok} / ⏳ ${pending} / ❌ ${s.fail} / всего ${s.total}`;
}

function _setDeviceProbeClass(idx, cls) {
  const card = document.querySelector(`.preview-device[data-idx="${idx}"]`);
  if (!card) return;
  card.classList.remove("probing", "probe-ok", "probe-fail");
  if (cls) card.classList.add(cls);
}

function _setDeviceProbeStatus(idx, html) {
  const el = document.querySelector(`.preview-probe-status[data-idx="${idx}"]`);
  if (el) el.innerHTML = html;
}

async function probeAllInPreview() {
  const btn = document.getElementById("preview-probe-all-btn");
  const title = document.getElementById("preview-title");
  const oldTitle = title ? title.textContent : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> probe…'; }
  const total = PREVIEW_DEVICES.length;
  // v1.26.0: pending вычисляется в _updateProbeTitle() как
  // total - ok - fail, отдельное поле не нужно.
  _probeStats = { ok: 0, fail: 0, total: total };
  _updateProbeTitle();
  // v1.19: try/finally — иначе при синхронном исключении внутри
  // probeDeviceItem кнопка осталась бы навсегда «probe…».
  // v1.22.1: параллельный probe — по 5 одновременно.
  // v1.23.0: pending / ok / fail в заголовке, подсветка карточки.
  try {
    const CONCURRENCY = 5;
    let idx = 0;
    const runOne = async () => {
      while (true) {
        const my = idx++;
        if (my >= total) return;
        // v1.28.34: держим ссылку на элемент — список мог измениться,
        // пока шёл probe (удаление карточки сдвигало индексы).
        const item = PREVIEW_DEVICES[my];
        if (!item) return;
        _setDeviceProbeStatus(my, '<span class="muted">⏳ в очереди</span>');
        _setDeviceProbeClass(my, "probing");
        _updateProbeTitle();
        try {
          await probeDeviceItem(item, my);
        } catch (e) {
          console.error("probe item failed", e);
        }
        if (!item.needs_probe) {
          _probeStats.ok++;
          _setDeviceProbeClass(my, "probe-ok");
        } else {
          _probeStats.fail++;
          _setDeviceProbeClass(my, "probe-fail");
        }
        _updateProbeTitle();
        setTimeout(() => _setDeviceProbeClass(my, ""), 2000);
      }
    };
    const workers = [];
    for (let w = 0; w < Math.min(CONCURRENCY, total); w++) workers.push(runOne());
    await Promise.allSettled(workers);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔍 Probe все"; }
    if (!isMobile()) renderImportPreview();
    if (title) {
      title.textContent = `Probe завершён: ✅ ${_probeStats.ok} / ❌ ${_probeStats.fail}`;
      setTimeout(() => { if (title) title.textContent = oldTitle; }, 5000);
    }
  }
}

async function probeDeviceItem(item, idx) {
  const d = item.device;
  const cloudRef = d._cloud_ref || {};
  const resultEl = document.getElementById("probe-result");
  const statusEl = document.querySelector(`.preview-probe-status[data-idx="${idx}"]`);
  const setStatus = (html) => {
    if (resultEl) resultEl.innerHTML = html;
    if (statusEl) statusEl.innerHTML = html;
  };
  try {
    setStatus('<span class="spin"></span> probe…');

    // v1.18.11: таймаут 15 сек — защита от вечного зависания fetch.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);
    let data;
    try {
      const r = await fetch("/api/cloud/probe_and_match", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: d.id, ip: d.ip, local_key: d.local_key, version_hint: d.version,
          cloud_status_meta: cloudRef._cloud_status_meta || [],
          cloud_current_values: cloudRef.cloud_status || {},
        }),
        signal: controller.signal,
      });
      data = await r.json();
    } finally {
      clearTimeout(timeoutId);
    }

    if (data.ok) {
      d.version = data.version || d.version;
      if (data.dps_map && Object.keys(data.dps_map).length > 0) {
        d.dps_map = data.dps_map;
        const dp_to_code = data.dp_to_code || {};
        const newMapping = {};
        for (const [dp, m] of Object.entries(dp_to_code)) {
          newMapping[dp] = { code: m.code || "", type: m.type || "", values: m.values || {}, name: m.name || "" };
        }
        if (Object.keys(newMapping).length > 0) {
          // v1.18.10: пишем mapping во все места, где его могут искать.
          d.mapping = newMapping;
          if (d._cloud_ref) {
            d._cloud_ref.mapping = newMapping;
            if (!d._cloud_ref._raw_cloud) d._cloud_ref._raw_cloud = {};
            d._cloud_ref._raw_cloud.mapping = newMapping;
          }
        }
        item.enabled_dps = {};
        for (const [dp, m] of Object.entries(newMapping)) {
          const code = m.code || "";
          item.enabled_dps[dp] = !JUNK_DP_CODES.has(code);
        }
        item.needs_probe = false;
      }
      const okHtml = `<span style="color:var(--green);">✅ v${d.version}, DP ${data.matched_count || 0}</span>`;
      item.probe_status_html = okHtml;
      setStatus(okHtml);
      if (isMobile()) renderImportPreview();
    } else {
      const failHtml = `<span style="color:var(--red);">❌ ${escapeHtml(data.error || "ошибка")}</span>`;
      item.probe_status_html = failHtml;
      setStatus(failHtml);
    }
  } catch (e) {
    const msg = (e && e.name === "AbortError") ? "таймаут 15 сек" : (e.message || String(e));
    const errHtml = `<span style="color:var(--red);">❌ ${escapeHtml(msg)}</span>`;
    item.probe_status_html = errHtml;
    setStatus(errHtml);
  }
}

async function confirmImport() {
  // v1.25.13: предупреждаем, если есть устройства с пустым dps_map —
  // они импортируются «молча», но не создадут MQTT-сущностей.
  const _emptyCount = PREVIEW_DEVICES.filter(item => {
    const d = item.device;
    let any = false;
    for (const k of Object.keys(item.enabled_dps || {})) {
      if (item.enabled_dps[k] && d.dps_map && d.dps_map[k]) { any = true; break; }
    }
    return !any;
  }).length;
  if (_emptyCount > 0) {
    const ok = await uiConfirm(
      "Импорт с пустым dps_map",
      `${_emptyCount} устройств(а) с пустым dps_map — у них не будет MQTT-сущностей.\n\nИмпортировать всё равно?`,
      { danger: true, okText: "Импортировать" }
    );
    if (!ok) return;
  }
  const btn = document.getElementById("preview-import-btn");
  // v1.27.7d: сбрасываем старые ошибки в превью.
  _clearPreviewErrors();
  // v1.27.7d: исключаем устройства, уже находящиеся в конфиге.
  // Bridge их отклонит («id conflicts with existing device»),
  // а мы и так знаем их статус — зачем гонять.
  const _skippedAlreadyInConfig = [];
  const _toImport = [];
  for (const item of PREVIEW_DEVICES) {
    const _cr = item.device && item.device._cloud_ref;
    if (_cr && _isCloudDeviceInConfig(_cr)) {
      _skippedAlreadyInConfig.push(item);
    } else {
      _toImport.push(item);
    }
  }
  if (_toImport.length === 0) {
    const _n = _skippedAlreadyInConfig.length;
    _showPreviewErrors([
      `Нечего импортировать: все выбранные (${_n}) уже есть в конфиге.`,
    ], "Ничего не импортируется");
    // Помечаем карточки, чтобы пользователь видел причину.
    for (const item of _skippedAlreadyInConfig) {
      item.import_error = "Уже в конфиге — будет пропущено";
    }
    if (!isMobile()) renderImportPreview();
    else renderImportPreview();
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> импорт…';
  try {
    const prepared = _toImport.map(item => {
      const d = item.device;
      const _itemIdx = PREVIEW_DEVICES.indexOf(item);
      const filtered_dps = {};
      for (const [dp, enabled] of Object.entries(item.enabled_dps)) {
        if (!enabled) continue;
        const _idx = _itemIdx >= 0 ? _itemIdx : 0;
        const _res = _previewResolveRow(item, _idx, dp);
        const _manualSrc = PREVIEW_ROW_SOURCE[_previewRowKey(_idx, dp)] || "";
        // v1.28.34: база — серверный dps_map (bridge-схема). Явно выбранный
        // источник переопределяет entry; heuristic — только component.
        let entry = (d.dps_map && d.dps_map[dp]) ? Object.assign({}, d.dps_map[dp]) : null;
        if (_manualSrc && _manualSrc !== "auto") {
          const _e = _previewSourceMapping(item, _manualSrc)[dp];
          if (_e && typeof _e === "object") {
            const _clean = {};
            for (const [k, v] of Object.entries(_e)) {
              if (!k.startsWith("_") && k !== "code" && k !== "type" && k !== "values") _clean[k] = v;
            }
            if (!_clean.name) _clean.name = _res.m.code || _e.name || _e.code || ("dp_" + dp);
            if (!_clean.component) _clean.component = _res.m.component || "sensor";
            entry = _clean;
          }
        } else if (entry && _res.src === "heuristic" && _res.m.component) {
          entry.component = _res.m.component;
        }
        // v1.28.68: зарезервированный bridge DP — фиксируем name/component.
        if (entry) {
          const _bf = _previewBridgeForced(item, dp, entry);
          if (_bf) entry = { name: _bf.key, component: _bf.info.comp };
          filtered_dps[dp] = entry;
        }
      }
      const _out = {
        id: d.id, name: d.name, friendly_name: d.friendly_name,
        ip: d.ip, local_key: d.local_key, version: d.version,
        type: d.type, model: d.model,
        // v1.28.10: battery_powered берём из item (переопределение
        // пользователя), а не из d (эвристика). Fallback — d.battery_powered.
        battery_powered: (item.battery_powered !== undefined)
                         ? !!item.battery_powered
                         : !!d.battery_powered,
        enabled: true,
        dps_map: filtered_dps,
      };
      // v1.28.9: climate-поля, если есть.
      for (const _k of ["presets","preset_map","min_temp","max_temp","temp_step"]) {
        if (d[_k] !== undefined) _out[_k] = d[_k];
      }
      return _out;
    });

    const r = await fetch("/api/import_devices", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ devices: prepared, overwrite: false })
    });
    const data = await r.json();

    // v1.27.7d: при ошибке НЕ закрываем превью — показываем
    // ошибки прямо в модалке, привязанными к карточкам.
    if (!data.ok) {
      const errs = Array.isArray(data.errors) ? data.errors.slice() : [];
      if (!errs.length && data.error) errs.push(String(data.error));
      // Парсим «<name>: <сообщение>» и вешаем на карточки.
      const byName = {};
      for (const e of errs) {
        const m = String(e).match(/^([^:]+):\s*(.+)$/);
        if (m) {
          const nm = m[1].trim();
          const msg = m[2].trim();
          byName[nm] = msg;
        }
      }
      for (const item of PREVIEW_DEVICES) {
        const nm = item.device && item.device.name;
        if (nm && byName[nm]) item.import_error = byName[nm];
      }
      _showPreviewErrors(errs, "Импорт не удался");
      // Перерисовываем превью (карточки с плашками).
      renderImportPreview();
      btn.disabled = false;
      btn.textContent = "📦 Импортировать всё";
      return;
    }

    // v1.27.9: при успехе превью НЕ закрываем — пользователь
    // закрывает сам. Показываем баннер «Импортировано: N»,
    // кнопка [📦 Импортировать всё] → disabled + «✅ Импортировано».
    const _skipped = _skippedAlreadyInConfig.length;
    const _added = data.added || 0;
    const _updated = data.updated || 0;
    const _bannerTitle = "✅ Импортировано";
    const _bannerLines = [
      `Добавлено: ${_added}, обновлено: ${_updated}` +
        (_skipped ? `, пропущено (уже в конфиге): ${_skipped}` : ""),
      "Закройте окно крестиком или кнопкой «Отмена».",
    ];
    _showPreviewErrors(_bannerLines, _bannerTitle);
    // Перекрашиваем баннер в зелёный (успех).
    const _be = document.getElementById("preview-errors-banner");
    if (_be) {
      _be.style.background = "rgba(46,160,67,0.12)";
      _be.style.borderBottomColor = "rgba(46,160,67,0.4)";
      _be.style.color = "var(--green)";
    }
    // Пропущенные (уже в конфиге) — плашка на карточке.
    for (const item of _skippedAlreadyInConfig) {
      item.import_error = "Уже в конфиге — не отправлено в bridge";
    }
    renderImportPreview();
    // Кнопка импорта → disabled.
    btn.disabled = true;
    btn.textContent = "✅ Импортировано";
    return;
  } catch (e) {
    _showPreviewErrors(["Сеть: " + e.message], "Импорт не удался");
  }
  btn.disabled = false;
  btn.textContent = "📦 Импортировать всё";
}

// v1.27.7d: показать/скрыть баннер ошибок в превью.
function _showPreviewErrors(errors, title) {
  const el = document.getElementById("preview-errors-banner");
  if (!el) return;
  const list = Array.isArray(errors) ? errors : [String(errors)];
  if (!list.length) { el.style.display = "none"; el.innerHTML = ""; return; }
  // v1.28.29: счётчик только для ошибок. Для success-баннера
  // (✅ Импортировано) счётчик вводит в заблуждение — там не
  // «ошибок N», а «строк N».
  const isSuccess = typeof title === "string" && title.startsWith("✅");
  const titleHtml = isSuccess
    ? escapeHtml(title)
    : `${escapeHtml(title || "Ошибки импорта")} (${list.length})`;
  let html = `<div class="peb-title">${titleHtml}</div><ul>`;
  for (const e of list) html += `<li>${escapeHtml(String(e))}</li>`;
  html += "</ul>";
  el.innerHTML = html;
  el.style.display = "block";
}

function _clearPreviewErrors() {
  const el = document.getElementById("preview-errors-banner");
  if (el) { el.style.display = "none"; el.innerHTML = ""; }
  for (const item of (PREVIEW_DEVICES || [])) {
    if (item) delete item.import_error;
  }
}

// ===== Scan =====
function updateScanHint() {
  const el = document.getElementById("scan-subnet");
  const hint = document.getElementById("scan-subnet-hint");
  if (!hint) return;
  const v = ((el && el.value) || "").trim() || "192.168.0";
  hint.textContent = `${v}.1–254`;
}

async function doScanExtended() {
  const btn = document.getElementById("scan-btn");
  const res = document.getElementById("scan-result");
  const subnet = document.getElementById("scan-subnet").value.trim();
  btn.disabled = true;
  res.style.display = "block";
  res.innerHTML = '<div style="padding:12px;"><span class="spin"></span> Сканирование (до 60 сек)…</div>';
  try {
    const r = await fetch("/api/scan/extended", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subnet })
    });
    // v1.19: r.json() бросит, если backend вернул не-JSON (500 + HTML).
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    if (!data.ok) { res.innerHTML = "❌ " + escapeHtml(data.error || "ошибка"); return; }
    SCAN_RESULTS = data.hosts || [];
    SCAN_SUBNET = data.subnet || subnet;
    SCAN_TS = Math.floor(Date.now() / 1000);
    _saveScanCache();
    renderScanResults(SCAN_SUBNET);
  } catch (e) {
    res.innerHTML = "❌ " + escapeHtml(e.message);
  } finally {
    btn.disabled = false;
  }
}

function classifyHost(h) {
  const known = LAST_DEVICES.find(d => d.ip === h.ip);
  if (known) return {cls: "known", name: known.friendly_name || known.name, type: known.type};
  // v1.28.78: имя из конфига могло ещё не приехать (LAST_DEVICES) —
  // но bridge проставил флаг known при скане, показываем хотя бы это.
  if (h.known) return {cls: "known", name: "из конфига"};
  // v1.18.15: если bridge 1.8.3 подтвердил Tuya UDP-пробой —
  // показываем жёстко.
  if (h.tuya && h.tuya.udp_port) {
    return {cls: "tuya-unknown", name: "Tuya (UDP подтверждён)"};
  }
  if (h.open_ports) {
    if (h.open_ports.includes(62078)) return {cls: "iot", name: "iPhone"};
    if (h.open_ports.includes(8008) || h.open_ports.includes(8009)) return {cls: "iot", name: "Chromecast"};
    if (h.open_ports.includes(9100) || h.open_ports.includes(631)) return {cls: "iot", name: "Принтер"};
  }
  return {cls: "unknown", name: "Неизвестно"};
}

function renderScanResults(subnet) {
  const res = document.getElementById("scan-result");
  if (!res) return;
  res.style.display = "block";
  if (SCAN_RESULTS.length === 0) {
    res.innerHTML = `<div style="padding:12px;" class="muted">Ничего не найдено. Нажми «Безопасный скан».</div>`;
    return;
  }
  let html = "";
  html += `<div class="scan-toolbar">
    <button class="primary" id="bridge-scan-btn" onclick="doBridgeScan()" title="Опрос через bridge: неизвестные IP проверяются TCP-коннектом на порт 6668 (находит устройства, которые не отвечают на ICMP), известные — только ICMP (правило «один TCP-сокет»). Плюс UDP-проба Tuya 6666/6667.">📡 Скан через Bridge</button>
    <span class="scan-toolbar-info" style="margin-left:8px; font-size:11px;">находит больше (TCP 6668)</span>
    <span class="scan-toolbar-info" id="bridge-scan-status"></span>
    <span class="scan-toolbar-info" style="margin-left:auto;">Найдено: ${SCAN_RESULTS.length} (сеть ${escapeHtml(subnet)}.x)${SCAN_TS ? " · измерено " + escapeHtml(fmtAgo(SCAN_TS)) : ""}</span>
  </div>`;
  for (const h of SCAN_RESULTS) {
    const cls = classifyHost(h);
    const hostname = h.hostname ? `<span>${escapeHtml(h.hostname)}</span>` : "";
    const ports = h.open_ports?.length ? h.open_ports.map(p => `<span class="scan-port">${p}</span>`).join(" ") : "";
    const latCls = latencyClass(h.ms);
    const latTxt = latencyText(h.ms);
    let badges = `<span class="scan-badge ${cls.cls}" style="margin-left:6px;">${escapeHtml(cls.name)}</span>`;
    // v1.18.15: различаем жёсткое (UDP) и вероятное (TCP 6668) подтверждение Tuya.
    if (h.bridge && h.tuya && h.tuya.udp_port) {
      badges += `<span class="scan-badge bridge-confirmed" style="margin-left:4px;">✅ Bridge: UDP ${h.tuya.udp_port} подтверждён</span>`;
    } else if (h.bridge && h.tuya && h.tuya.tuya_probable) {
      badges += `<span class="scan-badge bridge-confirmed" style="margin-left:4px;">📡 Bridge: 6668 открыт</span>`;
    } else if (h.bridge) {
      badges += `<span class="scan-badge bridge" style="margin-left:4px;">📡 из Bridge</span>`;
    }
    if (h.tuya_unknown && h.tuya && h.tuya.udp_port) {
      badges += `<span class="scan-badge tuya-unknown" style="margin-left:4px;">Tuya (UDP подтверждён)</span>`;
    } else if (h.tuya_unknown && h.tuya && h.tuya.tuya_probable) {
      badges += `<span class="scan-badge tuya-unknown" style="margin-left:4px;">Tuya? (TCP 6668)</span>`;
    }
    const rowCls = h.bridge_new ? "scan-host bridge-new" : "scan-host";
    html += `<div class="${rowCls}">
      <div>
        ${copyCode(h.ip, "font-size:13px;font-weight:600;")}
        <span class="latency ${latCls}" style="margin-left:8px; font-size:11px;">${latTxt}</span>
        ${badges}
      </div>
      <div class="scan-meta">${hostname}</div>
      ${ports ? `<div class="scan-meta">Порты: ${ports}</div>` : ""}
      ${h.tuya && h.tuya.gwId ? `<div class="scan-meta">gwId: <code>${escapeHtml(h.tuya.gwId)}</code>${h.tuya.productKey ? ' · productKey: <code>' + escapeHtml(h.tuya.productKey) + '</code>' : ''}${h.tuya.version ? ' · ver: ' + versionBadge(h.tuya.version) : ''}</div>` : ""}
    </div>`;
  }
  res.innerHTML = html;
}

async function doBridgeScan() {
  const btn = document.getElementById("bridge-scan-btn");
  const status = document.getElementById("bridge-scan-status");
  if (BRIDGE_SCAN_RUNNING) return;
  BRIDGE_SCAN_RUNNING = true;
  btn.disabled = true;
  status.innerHTML = '<span class="spin"></span> Bridge сканирует (до 30 сек)…';
  try {
    const subnet = SCAN_SUBNET || document.getElementById("scan-subnet").value.trim();
    const r = await fetch("/api/scan/bridge", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subnet })
    });
    const data = await r.json();
    if (!data.ok) {
      status.innerHTML = "❌ " + escapeHtml(data.error || "ошибка");
      BRIDGE_SCAN_RUNNING = false;
      btn.disabled = false;
      return;
    }
    const bridgeHosts = data.hosts || [];
    let added = 0, updated = 0;
    const byIp = {};
    for (const h of SCAN_RESULTS) byIp[h.ip] = h;
    for (const bh of bridgeHosts) {
      // v1.18.15: bridge 1.8.3 сам обогащает hosts полями
      // tuya/tuya_unknown/known — используем их, не выдумываем.
      if (byIp[bh.ip]) {
        byIp[bh.ip].bridge = true;
        if (bh.ms !== undefined) byIp[bh.ip].bridge_ms = bh.ms;
        if (bh.tuya) byIp[bh.ip].tuya = bh.tuya;
        if (bh.tuya_unknown) byIp[bh.ip].tuya_unknown = true;
        if (bh.known === true) byIp[bh.ip].known_bridge = true;
        updated++;
      } else {
        SCAN_RESULTS.push({
          ip: bh.ip, ms: bh.ms, bridge: true, bridge_new: true,
          known_bridge: bh.known === true,
          tuya: bh.tuya || null,
          tuya_unknown: bh.tuya_unknown === true,
        });
        added++;
      }
    }
    SCAN_RESULTS.sort((a, b) => {
      const pa = a.ip.split(".").map(Number);
      const pb = b.ip.split(".").map(Number);
      for (let i = 0; i < 4; i++) { if (pa[i] !== pb[i]) return pa[i] - pb[i]; }
      return 0;
    });
    renderScanResults(SCAN_SUBNET);
    // v1.28.80: сохраняем и результат bridge-скана (переживает перезаход).
    SCAN_TS = Math.floor(Date.now() / 1000);
    _saveScanCache();
    const newStatus = document.getElementById("bridge-scan-status");
    if (newStatus) newStatus.innerHTML = `✅ Bridge: +${added} новых, ~${updated} обновлено`;
    setTimeout(() => {
      const s = document.getElementById("bridge-scan-status");
      if (s) s.innerHTML = "";
    }, 8000);
  } catch (e) {
    const s = document.getElementById("bridge-scan-status");
    if (s) s.innerHTML = "❌ " + escapeHtml(e.message);
  }
  BRIDGE_SCAN_RUNNING = false;
  const b = document.getElementById("bridge-scan-btn");
  if (b) b.disabled = false;
}

// ===== Tools =====
async function loadConfig() {
  const container = document.getElementById("tools-container");
  const info = document.getElementById("tools-info");
  container.innerHTML = '<div class="muted" style="padding:16px;"><span class="spin"></span> Загрузка…</div>';
  try {
    const r = await fetch("/api/config/raw?v=" + Date.now());
    const data = await r.json();
    if (!data.ok) {
      container.innerHTML = `<div class="tools-error">❌ Конфиг недоступен: ${escapeHtml(data.error || "unknown")}
        <div style="margin-top:12px;"><button onclick="loadConfig()">Повторить</button></div></div>`;
      if (info) info.textContent = "";
      return;
    }
    TOOLS_CONFIG = data.config || [];
    if (info) info.textContent = `Устройств: ${Array.isArray(TOOLS_CONFIG) ? TOOLS_CONFIG.length : "?"}`;
    renderTools();
  } catch (e) {
    container.innerHTML = `<div class="tools-error">❌ Конфиг недоступен: ${escapeHtml(e.message)}
      <div style="margin-top:12px;"><button onclick="loadConfig()">Повторить</button></div></div>`;
    if (info) info.textContent = "";
  }
}
function setToolsView(view) {
  TOOLS_VIEW = view;
  document.getElementById("tools-view-raw").classList.toggle("active", view === "raw");
  document.getElementById("tools-view-bydev").classList.toggle("active", view === "bydev");
  document.getElementById("tools-view-audit").classList.toggle("active", view === "audit");
  _updateToolsToolbar(view);
  if (view === "audit") { loadAudit(); return; }
  renderTools();
}

// v1.28.74: кнопки действий — только для своего раздела.
function _updateToolsToolbar(view) {
  const v = view || TOOLS_VIEW;
  const raw = document.getElementById("tools-copy-raw-btn");
  const dev = document.getElementById("tools-copy-dev-btn");
  const aud = document.getElementById("tools-audit-cleanup-btn");
  if (raw) raw.style.display = (v === "raw") ? "" : "none";
  if (dev) dev.style.display = (v === "bydev") ? "" : "none";
  if (aud) aud.style.display = (v === "audit") ? "" : "none";
}
function renderTools() {
  // v1.23.7: если активна «История конфига», renderTools не должен
  // переключать вид на «По устройствам» после loadConfig().
  if (TOOLS_VIEW === "audit") { loadAudit(); return; }
  _updateToolsToolbar();
  if (TOOLS_CONFIG === null) return;
  const container = document.getElementById("tools-container");
  if (TOOLS_VIEW === "raw") {
    const jsonStr = JSON.stringify(TOOLS_CONFIG, null, 2);
    container.innerHTML = `<div style="padding:16px;"><pre class="json-view"><code class="language-json" id="tools-raw-code">${escapeHtml(jsonStr)}</code></pre></div>`;
    const el = document.getElementById("tools-raw-code");
    if (el) highlightJsonInto(el);
  } else {
    const devices = Array.isArray(TOOLS_CONFIG) ? TOOLS_CONFIG : [];
    if (devices.length === 0) {
      container.innerHTML = '<div class="muted" style="padding:16px;">Нет устройств в конфиге.</div>';
      return;
    }
    let listHtml = '<div class="tools-split"><div class="tools-list">';
    for (let i = 0; i < devices.length; i++) {
      const d = devices[i];
      const active = (i === TOOLS_SELECTED_IDX) ? " active" : "";
      listHtml += `<div class="tools-list-item${active}" onclick="selectToolDevice(${i})">
        <div>${escapeHtml(d.friendly_name || d.name || '?')}</div>
        <div class="tools-list-ip">${escapeHtml(d.ip || '?')} · ${escapeHtml(d.type || '?')}</div>
      </div>`;
    }
    listHtml += '</div><div class="tools-detail" id="tools-detail">';
    if (TOOLS_SELECTED_IDX >= 0 && TOOLS_SELECTED_IDX < devices.length) {
      const dev = devices[TOOLS_SELECTED_IDX];
      const jsonStr = JSON.stringify(dev, null, 2);
      listHtml += `<pre class="json-view"><code class="language-json" id="tools-detail-code">${escapeHtml(jsonStr)}</code></pre>`;
    } else {
      listHtml += '<div class="muted">Выберите устройство слева.</div>';
    }
    listHtml += '</div></div>';
    container.innerHTML = listHtml;
    const el = document.getElementById("tools-detail-code");
    if (el) highlightJsonInto(el);
  }
}
function selectToolDevice(idx) {
  TOOLS_SELECTED_IDX = idx;
  renderTools();
}
// ==================== HELP ====================
// v1.28.97: справка разделена — параметры и диагностика по своим разделам.
const HELP_TOP = [
  { id: "bridge", label: "🔌 Bridge" },
  { id: "webui",  label: "🌐 WebUI" },
];
let HELP_TOP_ID = "bridge";
const HELP_SECTION = { bridge: "overview", webui: "overview" };
const HELP_SECTIONS = {
  bridge: [
    { id: "overview",    label: "🚀 Обзор" },
    { id: "devices",     label: "🔧 Устройства" },
    { id: "config",      label: "⚙️ Конфигурация" },
    { id: "settings",    label: "🎛 Настройки" },
    { id: "mqtt",        label: "📡 MQTT-топики" },
    { id: "scan",        label: "🔍 Скан сети" },
    { id: "reliability", label: "🛡 Надёжность" },
    { id: "perf",        label: "⚡ Производительность" },
    { id: "twotcp",      label: "⛔ Два TCP" },
    { id: "diag",        label: "🩺 Диагностика" },
    { id: "limits",      label: "📌 Ограничения" },
  ],
  webui: [
    { id: "overview",  label: "🚀 Обзор" },
    { id: "dashboard", label: "📊 Дашборд" },
    { id: "import",    label: "📥 Импорт" },
    { id: "analytics", label: "📈 Аналитика" },
    { id: "quiet",     label: "🔇 Режим тишины" },
    { id: "logs",      label: "📜 Логи" },
    { id: "tools",     label: "🛠 Инструменты" },
    { id: "settings",  label: "🎛 Настройки" },
    { id: "diag",      label: "🩺 Диагностика" },
    { id: "ha",        label: "🏠 Home Assistant" },
    { id: "api",       label: "🔌 API (HTTP)" },
  ],
};
const HELP_TEXT = {
  bridge: {
    overview: `
      <h3>Что это</h3>
      <p>Локальный мост Tuya → Home Assistant через MQTT, <b>без Tuya Cloud</b>:
      общение с устройствами идёт по локальной сети (tinytuya). Bridge сам создаёт сущности
      в Home Assistant через MQTT Discovery.</p>
      <h3>Архитектура</h3>
      <ul>
        <li><b>tuya-bridge</b> (<code>main.py</code>) — воркеры устройств, MQTT-клиент, кэш состояний;</li>
        <li><b>tuya-webui</b> (<code>webui.py</code>) — HTTP <code>:5386</code>, SQLite, SSE-логи, настройка;</li>
        <li>Связь между контейнерами — через MQTT и общие файлы; Home Assistant подхватывает Discovery.</li>
        <li>WebUI не имеет доступа к Docker и не управляет устройствами напрямую.</li>
      </ul>
      <p>Общие тома: <code>devices_config.json</code> (bridge — rw, webui — ro),
      <code>logs/</code>, <code>state/</code> (только bridge), <code>webui_state/</code> (только webui).</p>
      <h3>Требования</h3>
      <ul>
        <li>Python 3.10+, <code>tinytuya</code>, <code>paho-mqtt</code>, <code>pyyaml</code>;</li>
        <li>MQTT-брокер (Mosquitto и т.п.) и Home Assistant с MQTT-интеграцией;</li>
        <li>Docker + Compose (рекомендуется);</li>
        <li>WebUI: <code>iputils-ping</code>, <code>curl</code>, <code>CAP_NET_RAW</code> (native ICMP).</li>
      </ul>
      <p class="help-note">Тома монтируются <b>папками</b>: bind-mount файла ломает
      <code>os.replace</code>. <code>webui_state/</code> — обязательный volume.</p>
      <h3>Структура проекта</h3>
      <ul>
        <li><code>bridge/</code> — <code>main.py</code> и Dockerfile;</li>
        <li><code>webui/</code> — <code>webui.py</code> и Dockerfile;</li>
        <li><code>config/</code> — <code>devices_config.json</code> (общий: bridge — rw, webui — ro);</li>
        <li><code>state/</code> — кэш состояний bridge (<code>state_cache.json</code>);</li>
        <li><code>webui_state/</code> — SQLite, Cloud-кэш, tuya-local, quiet, audit;</li>
        <li><code>logs/</code> — <code>bridge.log</code> и <code>webui.log</code>.</li>
      </ul>
      <h3>Благодарности и лицензия</h3>
      <p>Проект использует <b>tinytuya</b>, <b>paho-mqtt</b> и базу шаблонов
      <b>tuya-local</b>. Лицензия — MIT.</p>`,
    devices: `
      <h3>Поддерживаемые устройства</h3>
      <ul>
        <li><b>light</b> — вкл/выкл, яркость, цветовая температура (kelvin), RGB-цвет;</li>
        <li><b>switch</b> — одноканальные и многоканальные выключатели, розетки, breaker'ы;</li>
        <li><b>climate</b> — термостаты: режим, уставка, пресеты (RU/EN);</li>
        <li><b>sensor</b> — температура, влажность, энергия, ток, напряжение, мощность;</li>
        <li><b>binary_sensor</b> — двери, движение, утечка, fault;</li>
        <li><b>number</b> — числовые настройки (таймеры, лимиты);</li>
        <li><b>phase_a</b> — breaker: распаковка DP 6 в напряжение, ток и мощность;</li>
        <li><b>select</b> — Enum-настройки (например, relay_status).</li>
      </ul>
      <p class="help-note">Fan и cover не поддерживаются (см. «Ограничения»).</p>`,
    config: `
      <h3>Формат devices_config.json</h3>
      <p>Массив объектов-устройств.</p>
      <h3>Поля верхнего уровня</h3>
      <ul>
        <li><code>id</code> — Tuya Device ID;</li>
        <li><code>name</code> — уникальное имя (латиница, snake_case), оно же entity_id;</li>
        <li><code>friendly_name</code> — человеческое имя для Home Assistant;</li>
        <li><code>ip</code> — локальный IP устройства;</li>
        <li><code>local_key</code> — локальный ключ Tuya;</li>
        <li><code>version</code> — версия протокола: <code>3.1</code>, <code>3.3</code>, <code>3.4</code>, <code>3.5</code>;</li>
        <li><code>type</code> — <code>light</code> / <code>switch</code> / <code>climate</code> /
            <code>sensor</code> / <code>binary_sensor</code>;</li>
        <li><code>model</code> — необязательно, отображается в Home Assistant;</li>
        <li><code>battery_powered</code> — необязательно, <code>true</code> для батарейных;</li>
        <li><code>enabled</code> — необязательно, <code>false</code> чтобы пропустить устройство;</li>
        <li><code>dps_map</code> — маппинг DP в сущности Home Assistant.</li>
      </ul>
      <h3>Структура dps_map</h3>
      <p>Ключ — номер DP (строка). Значение — объект:</p>
      <ul>
        <li><code>component</code> — тип сущности Home Assistant (switch, sensor, binary_sensor, select, number, preset, light, phase_a);</li>
        <li><code>name</code> — имя сущности (entity_id);</li>
        <li><code>device_class</code> — класс устройства (temperature, humidity, energy, …);</li>
        <li><code>unit</code> — единица измерения (°C, %, kWh, …);</li>
        <li><code>state_class</code> — measurement / total / total_increasing;</li>
        <li><code>scale</code> — делитель: значение / 10^scale;</li>
        <li><code>options</code> — список допустимых значений (для select);</li>
        <li><code>map</code> — переименование значений (например <code>{"off":"power_off"}</code>);</li>
        <li><code>min</code> / <code>max</code> — границы для number;</li>
        <li><code>kelvin_min</code> / <code>kelvin_max</code> — диапазон цветовой температуры для light.</li>
      </ul>
      <h3>Climate-специфичные поля</h3>
      <ul>
        <li><code>min_temp</code> / <code>max_temp</code> / <code>temp_step</code> — границы и шаг уставки;</li>
        <li><code>presets</code> — список режимов (<code>auto</code>, <code>comfort</code>, <code>eco</code>…);</li>
        <li><code>preset_map</code> — отображаемые названия режимов;</li>
        <li>В <code>dps_map</code>: роли <code>target</code> (уставка) и <code>current</code> (текущая температура), а также <code>preset_mode</code>.</li>
      </ul>
      <p class="help-note">Локальный ключ можно получить через <code>tinytuya wizard</code>,
      в кабинете Tuya Cloud (iot.tuya.com → Project → Devices → Local key) или через WebUI
      (вкладка «Импорт»).</p>
      <p class="help-note">Конфиг общий: bridge читает и пишет, WebUI — только читает
      (изменения отправляются командами MQTT через bridge).</p>`,
    settings: `
      <h3>main.py — параметры bridge</h3>
      <ul>
        <li><code>MQTT_BROKER</code> — адрес MQTT-брокера;</li>
        <li><code>MQTT_PORT</code> — порт брокера (по умолчанию 1883);</li>
        <li><code>MQTT_USERNAME</code> / <code>MQTT_PASSWORD</code> — логин и пароль брокера (если требуются);</li>
        <li><code>TOPIC_PREFIX</code> — префикс всех MQTT-топиков (по умолчанию <code>tuya</code>);</li>
        <li><code>DISCOVERY_PREFIX=homeassistant</code> — префикс топиков Discovery для Home Assistant;</li>
        <li><code>POLL_INTERVAL=15</code> — период опроса проводных устройств, сек;</li>
        <li><code>OFFLINE_TIMEOUT=120</code> — сколько секунд без данных считать устройство offline;</li>
        <li><code>AVAILABILITY_EXPIRE=120</code> — <code>expire_after</code> в Discovery (Home Assistant сам пометит недоступным);</li>
        <li><code>SOCKET_TIMEOUT_CMD=0.3</code> — таймаут сокета при отправке команды, сек;</li>
        <li><code>SOCKET_TIMEOUT_WORKER=0.1</code> — таймаут <code>receive()</code> в воркере, сек;</li>
        <li><code>CMD_POOL_SIZE=32</code> — размер пула потоков для команд;</li>
        <li><code>WORKER_IDLE_SLEEP=0.4</code> — пауза между <code>receive()</code> в воркере, сек;</li>
        <li><code>LOCK_ACQUIRE_TIMEOUT=0.05</code> — сколько секунд ждать блокировку сокета;</li>
        <li><code>MIN_CMD_INTERVAL_STREAM=0.15</code> — минимальный интервал команд для light/climate/number, сек;</li>
        <li><code>MIN_CMD_INTERVAL_SWITCH=0</code> — то же для switch/select (без ограничения);</li>
        <li><code>DEBOUNCE_BY_TYPE</code> — окна дебаунса команд по типу устройства (гасят дребезг);</li>
        <li><code>REPEAT_RESET_SECONDS=120</code> — через сколько секунд сбрасывать счётчики повторов 914/905;</li>
        <li><code>CLEANUP_DISCOVERY=0</code> — <code>1</code> — очистить Discovery при старте;</li>
        <li><code>SCAN_WORKERS=32</code> — число потоков для сканирования подсети;</li>
        <li><code>SCAN_TIMEOUT=0.3</code> — таймаут TCP-connect на порт 6668, сек;</li>
        <li><code>LOG_LEVEL</code> — уровень логов: <code>DEBUG</code> / <code>INFO</code> / <code>WARNING</code> / <code>ERROR</code>.</li>
      </ul>
      <h3>DEBUG-флаги (Bridge)</h3>
      <ul>
        <li><code>DEBUG_CACHE_RECEIVE</code> — логировать приём <code>cache_snapshot</code>;</li>
        <li><code>DEBUG_CACHE_STATUS</code> — логировать status-ответы устройств;</li>
        <li><code>DEBUG_RAW_DP</code> — логировать «сырые» DP (диагностика смены раскладки прошивкой);</li>
        <li><code>DEBUG_MQTT_CMD</code> — логировать входящие MQTT-команды и тайминги.</li>
      </ul>
      <p class="help-note">Поставь <code>1</code> для диагностики и перезапусти контейнер.</p>
      <h3>Переменные окружения (docker compose)</h3>
      <ul>
        <li><code>MQTT_BROKER</code>, <code>MQTT_PORT</code>, <code>MQTT_USERNAME</code>,
            <code>MQTT_PASSWORD</code> — брокер;</li>
        <li><code>TOPIC_PREFIX</code>, <code>DISCOVERY_PREFIX</code>, <code>LOG_LEVEL</code>;</li>
        <li><code>TZ</code> — часовой пояс контейнера (влияет на время в логах).</li>
      </ul>
      <p class="help-note">Если переменная не задана — берётся значение по умолчанию из
      <code>main.py</code>.</p>
      <p class="help-note">Батарейное устройство (<code>battery_powered</code>) bridge обслуживает
      отдельным воркером, который слушает сообщения устройства; проводное — периодическим опросом.</p>`,
    mqtt: `
      <h3>Префикс</h3>
      <p><code>TOPIC_PREFIX</code> по умолчанию <code>tuya</code>:
      <code>&lt;prefix&gt;/&lt;type&gt;/&lt;name&gt;/…</code>.</p>
      <h3>Команды (Home Assistant → bridge)</h3>
      <ul>
        <li><code>tuya/light/&lt;dev&gt;/set</code> — JSON:
            <code>{"state":"ON","brightness":180,"color_temp_kelvin":4000}</code>;</li>
        <li><code>tuya/switch/&lt;dev&gt;/&lt;entity&gt;/set</code> — <code>ON</code> или <code>OFF</code>;</li>
        <li><code>tuya/climate/&lt;dev&gt;/mode/set</code> — <code>off</code> или <code>heat</code>;</li>
        <li><code>tuya/climate/&lt;dev&gt;/temp/set</code> — уставка, число °C;</li>
        <li><code>tuya/climate/&lt;dev&gt;/preset/set</code> — название пресета;</li>
        <li><code>tuya/select/&lt;dev&gt;/&lt;entity&gt;/set</code> — значение из <code>options</code>;</li>
        <li><code>tuya/number/&lt;dev&gt;/&lt;entity&gt;/set</code> — число;</li>
        <li><code>tuya/bridge/cleanup</code> — <code>1</code> = очистить Discovery;</li>
        <li><code>tuya/bridge/edit_config</code> — JSON <code>{device, changes, validate, request_id}</code>;</li>
        <li><code>tuya/bridge/delete_device</code> — JSON <code>{device, request_id}</code>;</li>
        <li><code>tuya/bridge/import_devices</code> — JSON <code>{devices, overwrite, request_id}</code>;</li>
        <li><code>tuya/bridge/scan_network</code> — JSON <code>{subnet, request_id}</code>.</li>
      </ul>
      <h3>Состояние (bridge → Home Assistant)</h3>
      <ul>
        <li><code>tuya/light/&lt;dev&gt;/state</code> — JSON:
            <code>{state, brightness, color_mode, color, color_temp_kelvin}</code>;</li>
        <li><code>tuya/switch/&lt;dev&gt;/&lt;entity&gt;/state</code> — <code>ON</code> или <code>OFF</code>;</li>
        <li><code>tuya/climate/&lt;dev&gt;/mode/state</code> — <code>off</code> или <code>heat</code>;</li>
        <li><code>tuya/climate/&lt;dev&gt;/temp/state</code> — уставка;</li>
        <li><code>tuya/climate/&lt;dev&gt;/current/state</code> — текущая температура;</li>
        <li><code>tuya/climate/&lt;dev&gt;/preset/state</code> — текущий пресет;</li>
        <li><code>tuya/select|number/&lt;dev&gt;/&lt;entity&gt;/state</code> — значение;</li>
        <li><code>tuya/&lt;type&gt;/&lt;dev&gt;/dps/&lt;dp&gt;/state</code> — сенсоры (с учётом <code>scale</code>);</li>
        <li><code>tuya/&lt;type&gt;/&lt;dev&gt;/phase_a/voltage/state</code> — вольты;</li>
        <li><code>tuya/&lt;type&gt;/&lt;dev&gt;/phase_a/current/state</code> — амперы;</li>
        <li><code>tuya/&lt;type&gt;/&lt;dev&gt;/phase_a/power/state</code> — киловатты.</li>
      </ul>
      <h3>Диагностика</h3>
      <ul>
        <li><code>tuya/bridge/status</code> — <code>online</code> / <code>offline</code> (LWT при старте и падении);</li>
        <li><code>tuya/bridge/uptime</code> — аптайм в секундах (раз в 30 сек);</li>
        <li><code>tuya/bridge/version</code> — версия bridge при старте;</li>
        <li><code>tuya/&lt;dev&gt;/status</code> — <code>online</code> / <code>offline</code> при изменении;</li>
        <li><code>tuya/&lt;dev&gt;/last_seen</code> — unix-timestamp последнего успешного ответа;</li>
        <li><code>tuya/&lt;dev&gt;/cache_snapshot</code> — снимок всех DP (JSON) при каждом успешном опросе.</li>
      </ul>
      <h3>Результаты команд (bridge → WebUI)</h3>
      <p>Все <code>*_result</code> содержат <code>request_id</code> — даже в ошибочных ветках.</p>
      <ul>
        <li><code>tuya/bridge/edit_config_result</code> — <code>{request_id, device, ok, error, changes, ts}</code>;</li>
        <li><code>tuya/bridge/delete_device_result</code> — <code>{request_id, ok, error, device, ts}</code>;</li>
        <li><code>tuya/bridge/import_devices_result</code> — <code>{request_id, ok, added, updated, skipped, errors, ts}</code>;</li>
        <li><code>tuya/bridge/scan_network_result</code> — <code>{request_id, ok, hosts, subnet, ts}</code>.</li>
      </ul>
      <h3>Структура hosts[i] в скане</h3>
      <ul>
        <li><code>ip</code> — адрес; <code>ms</code> — отклик;</li>
        <li><code>known</code> — IP есть в конфиге;</li>
        <li><code>tuya</code> — <code>{udp_port, gwId, productKey, version}</code> при подтверждении UDP;</li>
        <li><code>tuya.tuya_probable</code> — TCP 6668 открыт, но UDP молчит;</li>
        <li><code>tuya_unknown</code> — неизвестное Tuya-устройство.</li>
      </ul>`,
    scan: `
      <h3>Скан сети через bridge</h3>
      <ol>
        <li>TCP-connect на порт <code>6668</code> — <b>только для неизвестных IP</b>;</li>
        <li>Для IP с открытым 6668 — <b>UDP-проба</b> 6666/6667: получаем
            <code>gwId</code>, <code>productKey</code> и <code>version</code>;</li>
        <li>Для <b>известных</b> (в конфиге) IP — только <b>ICMP</b>, по правилу «один TCP-сокет».</li>
      </ol>
      <p>UDP-проба не мешает persistent-сокету. Так находятся устройства, которые не отвечают
      на ICMP, но открыты на 6668.</p>
      <p class="help-note">Безопасный скан из самого WebUI (ICMP и порты) описан в разделе
      «🌐 WebUI → 📥 Импорт → Скан сети». Бейджи результата: «Bridge: UDP подтверждён»,
      «Bridge: 6668 открыт», «из Bridge», «Tuya (UDP подтверждён)», «Tuya? (TCP 6668)».</p>`,
    reliability: `
      <h3>Надёжность</h3>
      <ul>
        <li><b>Persistent TCP</b> — одно постоянное соединение на устройство;</li>
        <li><b>Коды 904/905/914/900</b>: 904 — как «шум» (до 3 попыток), 914 — без сброса
            availability; логирование умное (первый раз INFO, повторы WARNING с backoff);</li>
        <li><b>Watchdog</b> — нет данных дольше <code>OFFLINE_TIMEOUT</code> → offline;</li>
        <li><b>Availability</b> per-device: LWT и <code>expire_after</code>;</li>
        <li><b>Автопереподключение MQTT</b> с экспоненциальной задержкой;</li>
        <li><b>JSON-кэш</b>: Home Assistant видит последние значения сразу после рестарта;</li>
        <li><b>Graceful shutdown</b>: закрытие сокетов и финальный publish offline;</li>
        <li><b>Grace period</b>: события online/offline игнорируются первые 60 секунд после старта bridge.</li>
      </ul>`,
    perf: `
      <h3>Производительность (эталон)</h3>
      <p>Конфигурация: ~40 Tuya-устройств, Raspberry Pi 4 / Intel N100.</p>
      <ul>
        <li>Bridge: CPU (покой) ~5%, RAM ~35 МБ;</li>
        <li>WebUI: CPU &lt; 1%, RAM ~35 МБ;</li>
        <li>Задержка <b>Home Assistant → Tuya &lt; 400 мс</b>;</li>
        <li><b>Tuya → Home Assistant</b> (физическое изменение) <b>≤ 15 с</b> — за счёт периода опроса;</li>
        <li>Home Assistant → Home Assistant после команды <b>&lt; 500 мс</b>;</li>
        <li><b>TCP-connect WebUI → Tuya = 0</b> (только ICMP);</li>
        <li>Reconnect'ов &lt; 10 за час на 40 устройств; ошибки 914 — единичные;</li>
        <li>Первый запуск WebUI — 0 сетевых запросов (без CDN).</li>
      </ul>
      <h3>За счёт чего</h3>
      <ul>
        <li><code>CMD_POOL_SIZE=32</code> — пул потоков команд с запасом на 40+ устройств;</li>
        <li><code>SOCKET_TIMEOUT_CMD=0.3</code> — команды не ждут долго;</li>
        <li>Rate-limit только для «стримовых» DP (light/climate/number);</li>
        <li>Post-command status — состояние обновляется сразу после команды.</li>
      </ul>`,
    twotcp: `
      <h3>⛔ Два TCP-соединения к Tuya не работают</h3>
      <p><b>Tuya-прошивки не терпят два TCP-соединения к одному устройству с одного IP.</b></p>
      <p>Следствия — нельзя:</p>
      <ul>
        <li>❌ делать <code>latency_worker</code> через TCP-connect;</li>
        <li>❌ запускать <code>tinytuya scan</code> параллельно с bridge;</li>
        <li>❌ запускать второй bridge на те же устройства;</li>
        <li>❌ делать TCP-probe на устройство из конфига bridge.</li>
      </ul>
      <p>Безопасно:</p>
      <ul>
        <li>✅ <b>ICMP ping</b>;</li>
        <li>✅ <b>UDP-проба 6666/6667</b>;</li>
        <li>✅ один persistent-сокет на устройство.</li>
      </ul>
      <p class="help-note">Поэтому WebUI измеряет задержку только ICMP, а известные IP
      в скане проверяются ICMP, а не TCP.</p>`,
    diag: `
      <h3>Быстрая проверка</h3>
      <ul>
        <li><code>docker compose ps</code> — статус контейнеров;</li>
        <li><code>docker compose logs -f tuya-bridge</code> (и <code>tuya-webui</code>) — логи;</li>
        <li><code>docker exec tuya-webui ping -c 1 &lt;ip&gt;</code> — проверка ICMP из контейнера.</li>
      </ul>
      <h3>Мост не публикует состояние устройства</h3>
      <ul>
        <li>Проверь прямой доступ: <code>tinytuya.Device(id, ip, key).status()</code> в контейнере bridge;</li>
        <li>Логи по устройству: <code>docker logs tuya-bridge 2>&1 | grep &lt;dev_name&gt;</code>;</li>
        <li>Включи <code>LOG_LEVEL=DEBUG</code>, <code>DEBUG_RAW_DP=1</code>,
            <code>DEBUG_MQTT_CMD=1</code> и перезапусти контейнер.</li>
      </ul>
      <h3>Ошибка 914 «Check device key or version»</h3>
      <ul>
        <li>Неверный <code>local_key</code> или <code>version</code>;</li>
        <li>Cold start после перезагрузки устройства (15–30 минут);</li>
        <li>Первый 914 — INFO, повторы — WARNING с backoff; кнопка «Сохранить» в edit-модалке
            ошибку 914 не вызывает.</li>
      </ul>
      <h3>Ошибка 905 «Device Unreachable»</h3>
      <ul>
        <li>Единичная — норма; массовая — сетевое событие;</li>
        <li>Постоянно по одному устройству — оно выключено, настрой режим тишины.</li>
      </ul>
      <h3>Команды Home Assistant → Tuya тормозят</h3>
      <ul>
        <li>Убедись, что замер задержки идёт только по ICMP;</li>
        <li>Включи <code>DEBUG_MQTT_CMD=1</code> и посмотри тайминги:
            <code>[MQTT-CMD]</code> через 10 с — проблема в Home Assistant или брокере;
            <code>status/receive</code> через 10 с — в bridge;
            <code>dps</code> сразу, но Home Assistant не обновился — в Home Assistant.</li>
      </ul>`,
    limits: `
      <h3>Известные особенности (актуально)</h3>
      <ul>
        <li><b>904 «Unexpected Payload»</b> — нормальное поведение persistent-сокета (до 3 попыток);</li>
        <li><b>Offline</b> приходит через 15–120 секунд после потери питания (конденсатор устройства);</li>
        <li><b>color_temp и brightness</b>: Home Assistant не любит <code>brightness != 255</code>
            в режиме <code>color_temp</code> — bridge не публикует brightness в этом режиме;</li>
        <li>Рекомендуется <b>DHCP reservation</b> для всех Tuya-устройств;</li>
        <li>Прошивка может обновиться и сменить раскладку DP (диагностика: <code>DEBUG_RAW_DP=1</code>);</li>
        <li>Файлы <code>state_cache.json</code>, <code>analytics.db</code>,
            <code>quiet_hours.json</code> и <code>config_audit.log</code> монтируются <b>папками</b>.</li>
      </ul>
      <h3>Известные ограничения</h3>
      <ul>
        <li>Нет fan и cover; нет управления Tuya Cloud (только локально); нет Prometheus-метрик;</li>
        <li>История в SQLite хранится ~3 дня;</li>
        <li>WebUI без аутентификации (рассчитан на локальную сеть); PWA устанавливается вручную;</li>
        <li>Режим тишины — без дней недели (только часы) и влияет только на WebUI;</li>
        <li>Cloud-кэш и tuya-local живут в <code>webui_state/</code> — нужен volume.</li>
      </ul>`,
  },
  webui: {
    overview: `
      <h3>Что такое WebUI</h3>
      <p>Отдельный контейнер для настройки, диагностики и контроля моста. Открывается по
      <code>http://&lt;host&gt;:5386</code>. Общается с bridge через MQTT, не управляет
      устройствами напрямую и не имеет доступа к Docker.</p>
      <h3>Вкладки</h3>
      <ul>
        <li><b>📊 Дашборд</b> — устройства, статусы, задержка, карточка устройства;</li>
        <li><b>📈 Аналитика</b> — задержка, хронология, мерцания, здоровье, логи;</li>
        <li><b>📥 Импорт устройств</b> — Cloud, сопоставление DP, скан, локальные базы;</li>
        <li><b>🛠 Инструменты</b> — конфиг, история, обслуживание;</li>
        <li><b>📡 Ping</b> и <b>❓ Справка</b> — в шапке.</li>
      </ul>
      <h3>Особенности</h3>
      <ul>
        <li>Падение WebUI не влияет на мост; рестарт моста не роняет WebUI;</li>
        <li>Cloud-кэш и tuya-local живут на сервере (<code>webui_state/</code>), а не в браузере;</li>
        <li>История в SQLite ~3 дня; SSE-логи; своя подсветка JSON (работает офлайн, без CDN);</li>
        <li>Тёмная и светлая тема (🌙), интерфейс русифицирован.</li>
      </ul>
      <h3>Безопасность</h3>
      <ul>
        <li><code>devices_config.json</code> содержит <code>local_key</code> — не коммить в git;</li>
        <li>WebUI без авторизации — только локальная сеть; Docker socket не монтируется;</li>
        <li>Local key отдаётся только отдельным эндпоинтом; креды Tuya Cloud — в
            <code>localStorage</code> браузера;</li>
        <li>В <code>config_audit.log</code> <code>local_key</code> заменяется на <code>***</code>.</li>
      </ul>`,
    dashboard: `
      <h3>Таблица устройств</h3>
      <ul>
        <li><b>Статус</b> — online/offline и «сколько назад» был ответ;</li>
        <li><b>Задержка</b> — последний ICMP-замер (для батарейных и выключенных не измеряется);</li>
        <li><b>Проблемные устройства</b> — блок сверху (offline, просроченный last_seen);</li>
        <li><b>Бейдж 🔇</b> — устройство в окне тишины; <b>🔋 / 🔌</b> — тип питания;</li>
        <li>Поиск по имени, IP или типу, со счётчиком «найдено N из M».</li>
      </ul>
      <h3>Карточка устройства</h3>
      <ul>
        <li><b>Информация</b> — статус, тип, IP, версия протокола, Tuya ID, Model;</li>
        <li><b>Local key</b> — скрыт, показывается кнопкой 🔒 / 🔓;</li>
        <li><b>🔈 Режим тишины</b> — окна (см. раздел «Режим тишины»);</li>
        <li><b>Задержка</b> — график за 24 часа и среднее;</li>
        <li><b>Управление DP</b> — правка сопоставлений (component, имя, тип, значения),
            «Режим редактирования» и сохранение с валидацией;</li>
        <li><b>Кэш состояния</b>, <b>История статуса</b>, <b>Сырые данные</b> (Bridge и Cloud) —
            для диагностики.</li>
      </ul>
      <p>Кнопка ✏️ в карточке открывает настройки: имя, IP, local key, <code>enabled</code>,
      <code>battery_powered</code>, тип. Кнопка ➖ в списке DP удаляет DP из конфига.</p>`,
    import: `
      <h3>Зачем это нужно</h3>
      <p>Сопоставить «сырые» DP устройства с сущностями Home Assistant.</p>
      <h3>Источники сопоставления (по приоритету)</h3>
      <ul>
        <li><b>☁ Cloud</b> — mapping из Tuya Cloud;</li>
        <li><b>📚 tuya-local</b> — локальная база шаблонов;</li>
        <li><b>🔗 similar</b> — совпал <code>product_id</code> с уже настроенным устройством;</li>
        <li><b>⚙️ Текущий</b> — как уже сохранено в конфиге;</li>
        <li><b>⚠️ эвристика</b> — догадка по имени или значению (проверяйте).</li>
      </ul>
      <h3>Превью</h3>
      <ul>
        <li>Колонки: DP, Code, Перевод, Component, Тип, Значения, Текущее, Источник, Выбор;</li>
        <li><b>Выбор</b> — источник для конкретного DP; источник в шапке управляет новыми устройствами;</li>
        <li><b>🧪 Песочница</b> — посмотреть все DP и значения известного устройства
            (изменения не сохраняются);</li>
        <li><b>🔍 Опросить устройство</b> — локальный probe, если Cloud не дал mapping
            (таймаут 15 секунд);</li>
        <li>Мусорные DP выключены по умолчанию; <b>Δ +/−/~</b> — изменения относительно
            текущего конфига;</li>
        <li>Переключатели: приоритет батарейного устройства и язык пресетов.</li>
      </ul>
      <p class="help-note">Импортируются только новые устройства; уже добавленные — только просмотр.</p>
      <h3>Cloud-кэш</h3>
      <p>Креды хранятся в браузере (<code>localStorage</code>), сам кэш — на сервере
      (<code>webui_state/tuya_cloud_cache.json</code>, chmod 600). Есть баннер о старом кэше
      (старше 6 часов) и кнопка очистки.</p>
      <h3>Локальные базы DP</h3>
      <ul>
        <li><b>tinytuya_devices.json</b> — список устройств с DP; <b>tuya-local</b> — 1700+ YAML; оба в <code>webui_state/</code>;</li>
        <li><b>⬇ Обновить tuya-local</b> — скачать базу из GitHub (2 фазы: скачивание и импорт/индекс);</li>
        <li><b>🔄 Пересобрать tinytuya.json</b> — фоновый probe и слияние mapping;</li>
        <li>Счётчики обновляются сами раз в 15 секунд.</li>
      </ul>
      <h3>Скан сети</h3>
      <p>Безопасный скан (ICMP и порты) и «📡 Скан через Bridge» (TCP 6668 для неизвестных
      IP — находит больше устройств).</p>`,
    analytics: `
      <h3>Что показывает</h3>
      <ul>
        <li><b>Задержка (ICMP ping)</b> — период <code>[30мин][1час][6час][Сутки][Всё]</code>,
            средний ping, сортировка и цветовой индикатор (timeout выделяется);</li>
        <li><b>Хронология событий</b> — до 500 записей online/offline с очисткой по режимам;</li>
        <li><b>Мерцающие устройства (24ч)</b> — сортировка по клику;</li>
        <li><b>Активность и мерцания (24ч)</b> — два графика в одной карточке;</li>
        <li><b>Здоровье</b> — CPU, RAM и диск WebUI и bridge.</li>
      </ul>
      <p class="help-note">Устройства в тишине не пингуются и исключаются из мерцаний,
      хронологии и «Проблемных».</p>
      <h3>SQLite-хранилище</h3>
      <p>Файл <code>webui_state/analytics.db</code>. Таблицы:</p>
      <ul>
        <li><code>status_events</code> — переходы online/offline: хронология и мерцания;</li>
        <li><code>latency_history</code> — замеры ICMP-задержки;</li>
        <li><code>hourly_online_count</code> — агрегат «online по часам».</li>
      </ul>
      <p>Срок хранения — <code>RETENTION_DAYS</code> (~3 дня); буфер сбрасывается раз в
      <code>FLUSH_INTERVAL</code> (30 секунд). Набор таблиц зависит от флагов: при
      <code>ANALYTICS_ENABLED</code> — все; при <code>ANALYTICS=False</code> и
      <code>STATUS_HISTORY=True</code> — только <code>status_events</code> и
      <code>latency_history</code>; при обоих <code>False</code> база не создаётся.</p>
      <p>Очистка — в 🛠 Инструменты → «Обслуживание БД».</p>`,
    quiet: `
      <h3>Зачем это нужно</h3>
      <p>Окна тишины для отдельных устройств: если ты физически выключаешь устройство,
      WebUI не считает это «мерцанием» и не показывает его в «Проблемных».</p>
      <h3>Что отключается в окне тишины</h3>
      <ul>
        <li>❌ не пишется в <code>status_events</code> — мерцания и хронология;</li>
        <li>❌ исключается из «Мерцающих», «Хронологии» и «Проблемных»;</li>
        <li>❌ не пингуется — <code>latency_history</code> не пишется;</li>
        <li>✅ статус на дашборде и бейдж 🔇 показываются;</li>
        <li>✅ «Online по часам» и снапшоты — без изменений.</li>
      </ul>
      <h3>Как настроить</h3>
      <p>Карточка устройства → «🔈 Режим тишины» → «+ Добавить окно» → задать
      <code>from</code> и <code>to</code> (например <code>23:00</code>–<code>08:00</code>).
      Ночные окна (через полночь) поддерживаются, окон может быть несколько. Сохраняется
      кнопкой «💾 Сохранить».</p>
      <h3>Где хранится</h3>
      <p><code>webui_state/quiet_hours.json</code>: ключ — <code>name</code> устройства,
      окна — <code>{"from":"HH:MM","to":"HH:MM"}</code>. Файл можно править руками — изменения
      подхватятся при следующем старте контейнера.</p>
      <p class="help-note">Grace: после окончания окна устройство ещё 2 минуты
      (<code>QUIET_GRACE_SEC</code>) не считается проблемным. Bridge при этом не меняется —
      режим тишины работает только на уровне WebUI.</p>
      <h3>API</h3>
      <p><code>POST /api/device/&lt;name&gt;/quiet</code> с телом
      <code>{"windows":[{"from":"23:00","to":"08:00"}]}</code>; пустой список удаляет окна.</p>`,
    logs: `
      <h3>Источники</h3>
      <p><b>Bridge</b> и <b>WebUI</b> — отдельные потоки; у каждого источника свой
      запомненный уровень.</p>
      <h3>Уровни</h3>
      <p><code>DEBUG</code> (всё), <code>INFO+</code>, <code>WARN+</code>, <code>ERROR</code>.
      Кнопки уровней, для которых нет сообщений, скрываются (кроме выбранного).</p>
      <h3>Управление</h3>
      <ul>
        <li>Live-поток через SSE, кольцевой буфер 5000 строк;</li>
        <li>Период (всё время, 30 минут, 1 час, сутки) и поиск с подсветкой и переходами ▲▼;</li>
        <li>Пауза и продолжение, автоскролл, скачивание видимых строк;</li>
        <li>Состояние панели и выбранный период переживают перезагрузку; старые логи не теряются
            при переключении источника.</li>
      </ul>`,
    tools: `
      <h3>Конфигурация (devices_config.json)</h3>
      <ul>
        <li><b>📄 Raw JSON</b> — весь конфиг; кнопка <b>📋 Копировать JSON</b>;</li>
        <li><b>📋 По устройствам</b> — по одному устройству; кнопка
            <b>📋 Копировать JSON выбранного</b>;</li>
        <li><b>📜 История конфига</b> — журнал изменений (<code>config_audit.log</code>, JSONL)
            и кнопка <b>🗑 Очистить</b>.</li>
      </ul>
      <h3>Обслуживание</h3>
      <ul>
        <li><b>🗑 Очистить БД</b> — удаляет старые записи истории задержек и снапшотов
            (оставить 3, 7 или 30 дней; своё значение; удалить всё);</li>
        <li><b>🧹 Очистить Discovery</b> — перепубликация сущностей в Home Assistant.</li>
      </ul>`,
    settings: `
      <h3>webui.py — параметры WebUI</h3>
      <ul>
        <li><code>WEBUI_PORT=5386</code> — HTTP-порт WebUI;</li>
        <li><code>WEBUI_HOST=0.0.0.0</code> — адрес прослушивания;</li>
        <li><code>ANALYTICS_ENABLED</code> — включает полную аналитику (задержки, хронология, мерцания);</li>
        <li><code>STATUS_HISTORY_ENABLED</code> — минимальная история (события и задержки) без полной аналитики;</li>
        <li><code>BRIDGE_STARTUP_GRACE_SEC=60</code> — сколько секунд после старта bridge игнорировать online/offline;</li>
        <li><code>QUIET_GRACE_SEC=120</code> — «хвост» режима тишины после окончания окна, сек;</li>
        <li><code>LATENCY_INTERVAL=900</code> — период замера задержки, сек (15 минут);</li>
        <li><code>LATENCY_INITIAL_DELAY=5</code> — задержка перед первым замером после старта, сек;</li>
        <li><code>LATENCY_PING_TIMEOUT=1</code> — таймаут одной попытки ping, сек;</li>
        <li><code>LATENCY_RETRY_COUNT=3</code> — число попыток при timeout;</li>
        <li><code>LATENCY_RETRY_DELAY=10</code> — пауза между попытками, сек;</li>
        <li><code>LATENCY_RETRY_WORKERS=10</code> — сколько повторов выполнять параллельно;</li>
        <li><code>AUDIT_MAX_BYTES</code> — размер <code>config_audit.log</code> до ротации (5 МБ);</li>
        <li><code>AUDIT_BACKUPS=3</code> — сколько бэкапов истории конфига хранить;</li>
        <li><code>RETENTION_DAYS=3</code> — срок хранения истории в SQLite, дней;</li>
        <li><code>FLUSH_INTERVAL=30</code> — период сброса буфера событий в SQLite, сек;</li>
        <li><code>SSE_MAX_SUBSCRIBERS=50</code> — ограничение числа SSE-подписчиков (клиентов логов);</li>
        <li><code>SSE_IDLE_TIMEOUT=180</code> — таймаут неактивного SSE-соединения, сек;</li>
        <li><code>SSE_BACKLOG=100</code> — сколько последних строк лога отдавать при подключении;</li>
        <li><code>EDIT_TIMEOUT_WAIT=20</code> — сколько секунд ждать ответ bridge на изменение конфига;</li>
        <li><code>DELETE_TIMEOUT_WAIT=20</code> — то же для удаления устройства;</li>
        <li><code>IMPORT_TIMEOUT_WAIT=30</code> — то же для импорта устройств;</li>
        <li><code>SCAN_TIMEOUT_WAIT=30</code> — то же для скана сети.</li>
      </ul>
      <h3>Переменные окружения (docker compose)</h3>
      <ul>
        <li><code>MQTT_BROKER</code>, <code>MQTT_PORT</code>, <code>MQTT_USERNAME</code>,
            <code>MQTT_PASSWORD</code>, <code>TOPIC_PREFIX</code>;</li>
        <li><code>WEBUI_PORT</code>, <code>WEBUI_HOST</code>;</li>
        <li><code>TZ</code> — часовой пояс контейнера (влияет на окна тишины и время в логах).</li>
      </ul>
      <p class="help-note">Если переменная не задана — берётся значение по умолчанию из
      <code>webui.py</code>. Требуется перезапуск контейнера WebUI.</p>`,
    diag: `
      <h3>WebUI не измеряет задержку</h3>
      <ul>
        <li>Проверь наличие <code>ping</code> в контейнере:
            <code>docker exec tuya-webui which ping</code>;</li>
        <li>Если «Operation not permitted» — добавь <code>cap_add: NET_RAW</code> в compose;</li>
        <li>Проверка вручную: <code>docker exec tuya-webui ping -c 1 &lt;ip&gt;</code>.</li>
      </ul>
      <h3>WebUI не видит логи</h3>
      <ul>
        <li><code>docker exec tuya-bridge ls -la /app/logs/</code>;</li>
        <li><code>docker exec tuya-webui ls -la /app/logs/</code> — том <code>logs/</code>
            должен быть смонтирован в оба контейнера (в webui — <code>:ro</code>).</li>
      </ul>
      <h3>Инструменты: «Конфиг недоступен»</h3>
      <p>Убедись, что <code>devices_config.json</code> смонтирован в контейнер WebUI
      (только чтение, <code>:ro</code>).</p>
      <h3>Аналитика пуста</h3>
      <ul>
        <li>История накапливается со временем: график активности заполнится через 2+ часа;</li>
        <li>Проверь <code>ANALYTICS_ENABLED=True</code> (иначе будет только минимальная история
            или ничего).</li>
      </ul>
      <h3>Устройства «мерцают» в аналитике</h3>
      <ul>
        <li>20–30 переходов за 24 часа — нормальное поведение;</li>
        <li>Больше 100 в сутки — смотри логи bridge;</li>
        <li>Регулярные «мерцания» из-за выключения устройства — настрой режим тишины.</li>
      </ul>
      <h3>Старый Cloud-кэш</h3>
      <p>Возраст кэша виден на вкладке «Импорт»; очистить можно кнопкой «🗑 Очистить» или
      удалив <code>webui_state/tuya_cloud_cache.json</code>.</p>`,
    ha: `
      <h3>Discovery</h3>
      <p>Bridge публикует <b>retained</b> config-топики
      <code>homeassistant/&lt;component&gt;/&lt;unique_id&gt;/config</code>
      (префикс задаётся константой <code>DISCOVERY_PREFIX</code>), и Home Assistant создаёт
      сущности.</p>
      <ul>
        <li><code>unique_id</code> = <code>&lt;устройство&gt;_&lt;имя&gt;</code>
            (например <code>lyustra_v_spalne_switch_led</code>);</li>
        <li>для составных сущностей — <code>&lt;устройство&gt;_light</code>,
            <code>&lt;устройство&gt;_climate</code>;</li>
        <li>системные сущности bridge: <code>&lt;устройство&gt;_battery_alert</code>,
            <code>&lt;устройство&gt;_battery_last_seen</code>,
            <code>&lt;устройство&gt;_output_voltage|current|power</code>;</li>
        <li><code>state_topic</code> сенсоров — <code>tuya/&lt;type&gt;/&lt;dev&gt;/dps/&lt;dp&gt;/state</code>.</li>
      </ul>
      <h3>Имена, зарезервированные bridge</h3>
      <ul>
        <li><code>preset_mode</code> — обязательно для component <code>preset</code> (из облачного <code>mode</code>);</li>
        <li><code>phase_a</code> — обязательно для component <code>phase_a</code> (DP 6).</li>
      </ul>
      <p>Bridge отклоняет конфиг, если для этих компонентов имя другое. Дополнительно запрещены имена
      системных сущностей: <code>battery_alert</code>, <code>battery_last_seen</code>,
      <code>output_voltage</code>, <code>output_current</code>, <code>output_power</code>.</p>
      <h3>Фиксированные имена WebUI</h3>
      <ul>
        <li><code>backlight</code>, <code>prepayment</code> — для switch;</li>
        <li><code>door</code>, <code>motion</code>, <code>moisture</code> — для binary_sensor
            (чтобы получить нужный <code>device_class</code> в Home Assistant).</li>
      </ul>
      <p>WebUI сам подставляет эти имена по облачному коду и помечает такие DP 🔒 (только чтение) —
      так сущности в Home Assistant получаются предсказуемыми.</p>
      <p>В WebUI такие DP помечены 🔒 и доступны только для чтения: bridge сам управляет их
      именем и компонентом. Если ошиблись — удалите DP и добавьте заново.</p>
      <h3>Чистка</h3>
      <p>Удаление DP из конфига удаляет и discovery, и retained state — «призрачных» сущностей
      не остаётся. Полная перепубликация — кнопка 🧹 в Инструментах. При старте bridge сам
      чистит «зависшие» (orphan) retained-топики.</p>`,
    api: `
      <h3>HTTP API WebUI</h3>
      <p>Все методы — на порту <code>5386</code>. Пример: <code>http://&lt;host&gt;:5386/api/status</code>.</p>
      <h3>Страницы и служебное (GET)</h3>
      <ul>
        <li><code>/</code> — HTML-страница дашборда;</li>
        <li><code>/analytics</code> — HTML-страница аналитики;</li>
        <li><code>/import</code> — HTML-страница импорта устройств;</li>
        <li><code>/tools</code> — HTML-страница инструментов;</li>
        <li><code>/help</code> — HTML-страница справки;</li>
        <li><code>/manifest.json</code> — PWA-манифест;</li>
        <li><code>/favicon.svg</code> — иконка сайта;</li>
        <li><code>/healthz</code> — healthcheck контейнера (200 или 503);</li>
        <li><code>/api/health/full</code> — подробное здоровье (CPU, RAM, диск, аптайм).</li>
      </ul>
      <h3>Данные (GET)</h3>
      <ul>
        <li><code>/api/status</code> — состояние bridge и список устройств (JSON);</li>
        <li><code>/api/config/raw</code> — полный <code>devices_config.json</code>;</li>
        <li><code>/api/config/audit?limit=N</code> — история изменений конфига (последние N);</li>
        <li><code>/api/device/&lt;name&gt;/secret</code> — локальный ключ устройства;</li>
        <li><code>/api/device/&lt;name&gt;/history</code> — история статусов устройства;</li>
        <li><code>/api/device/&lt;name&gt;/latency</code> — история замеров задержки;</li>
        <li><code>/api/device/&lt;name&gt;/avg_latency?latency_seconds=N</code> — средний ping за период;</li>
        <li><code>/api/analytics?latency_seconds=N</code> — данные аналитики (задержки, мерцания);</li>
        <li><code>/api/base/info</code> — сведения о локальных базах DP;</li>
        <li><code>/api/base/rebuild/progress</code> — прогресс пересборки tinytuya.json;</li>
        <li><code>/api/base/tuya-local/progress</code> — прогресс скачивания/импорта tuya-local;</li>
        <li><code>/api/latency/refresh/progress</code> — прогресс ручного замера задержки;</li>
        <li><code>/api/cloud/cache</code> — Cloud-кэш устройств;</li>
        <li><code>/api/logs/history?tail=1000</code> — последние N строк логов;</li>
        <li><code>/api/logs/stream?since=0</code> — поток логов (SSE).</li>
      </ul>
      <h3>Действия (POST)</h3>
      <ul>
        <li><code>/api/cleanup</code> — очистить и переопубликовать Discovery;</li>
        <li><code>/api/latency/refresh</code> — запустить ручной замер задержки;</li>
        <li><code>/api/db/cleanup</code> — очистить SQLite (по периоду или полностью);</li>
        <li><code>/api/config/audit/cleanup</code> — очистить историю конфига;</li>
        <li><code>/api/device/&lt;name&gt;/config</code> — изменить устройство (ip, key, name, enabled…);</li>
        <li><code>/api/device/&lt;name&gt;/delete</code> — удалить устройство из конфига;</li>
        <li><code>/api/device/&lt;name&gt;/dps_map/apply</code> — применить правки DP (<code>{dps_map}</code>);</li>
        <li><code>/api/device/&lt;name&gt;/quiet</code> — задать окна тишины;</li>
        <li><code>/api/scan/extended</code> — скан сети средствами WebUI;</li>
        <li><code>/api/scan/bridge</code> — скан сети через bridge;</li>
        <li><code>/api/cloud/fetch</code> — запросить устройства из Tuya Cloud;</li>
        <li><code>/api/cloud/probe</code> — probe устройства: определить версию и число DP;</li>
        <li><code>/api/cloud/probe_and_match</code> — probe и сопоставление DP;</li>
        <li><code>/api/cloud/cache</code> — сохранить или очистить Cloud-кэш;</li>
        <li><code>/api/base/tuya-local/update</code> — скачать и проиндексировать tuya-local;</li>
        <li><code>/api/base/tinytuya/rebuild</code> — пересобрать tinytuya.json;</li>
        <li><code>/api/import_devices</code> — импортировать устройства в конфиг.</li>
      </ul>
      <p class="help-note">Пример: <code>curl -X POST http://&lt;host&gt;:5386/api/device/&lt;name&gt;/quiet
      -H "Content-Type: application/json" -d '{"windows":[{"from":"23:00","to":"08:00"}]}'</code>.</p>`,
  },
};

function _renderHelp() {
  const tabs = document.getElementById("help-tabs");
  const body = document.getElementById("help-body");
  if (!tabs || !body) return;
  const top = HELP_TOP.map(t =>
    `<button class="help-top${t.id === HELP_TOP_ID ? " active" : ""}" onclick="setHelpTop('${t.id}')">${t.label}</button>`
  ).join("");
  const secs = (HELP_SECTIONS[HELP_TOP_ID] || []).map(s =>
    `<button class="${s.id === HELP_SECTION[HELP_TOP_ID] ? "active" : ""}" onclick="setHelpTab('${s.id}')">${s.label}</button>`
  ).join("");
  tabs.innerHTML = `<div class="help-top-row">${top}</div><div class="help-tabs-row">${secs}</div>`;
  body.innerHTML = (HELP_TEXT[HELP_TOP_ID] || {})[HELP_SECTION[HELP_TOP_ID]] || "";
}
function setHelpTop(id) {
  HELP_TOP_ID = id;
  _renderHelp();
}
function setHelpTab(id) {
  HELP_SECTION[HELP_TOP_ID] = id;
  _renderHelp();
}

function copyConfigRaw() {
  if (TOOLS_CONFIG === null) return;
  const jsonStr = JSON.stringify(TOOLS_CONFIG, null, 2);
  tryExecCopy(jsonStr);
  showCopiedToast("Конфиг (" + jsonStr.length + " симв.)");
}

// v1.28.74: копировать JSON выбранного устройства («По устройствам»).
function copyConfigDevice() {
  if (TOOLS_CONFIG === null) return;
  const devices = Array.isArray(TOOLS_CONFIG) ? TOOLS_CONFIG : [];
  if (TOOLS_SELECTED_IDX < 0 || TOOLS_SELECTED_IDX >= devices.length) {
    showCopiedToast("Устройство не выбрано");
    return;
  }
  const dev = devices[TOOLS_SELECTED_IDX];
  const jsonStr = JSON.stringify(dev, null, 2);
  tryExecCopy(jsonStr);
  showCopiedToast("JSON: " + (dev.friendly_name || dev.name || "?")
                  + " (" + jsonStr.length + " симв.)");
}

// ===== Analytics =====
function setLatencyPeriod(seconds) {
  LATENCY_PERIOD = parseInt(seconds, 10) || 0;
  document.querySelectorAll("#latency-period-group button").forEach(b => {
    const s = parseInt(b.dataset.lat, 10) || 0;
    b.classList.toggle("active", s === LATENCY_PERIOD);
  });
  if (ANALYTICS_ENABLED) loadAnalytics();
}

function sortLatency(key) {
  if (LATENCY_SORT_KEY === key) LATENCY_SORT_DIR = -LATENCY_SORT_DIR;
  else { LATENCY_SORT_KEY = key; LATENCY_SORT_DIR = 1; }
  _saveSort("latency", LATENCY_SORT_KEY, LATENCY_SORT_DIR);
  renderLatencyTable(LATENCY_DATA);
  updateLatencySortIndicators();
}
function updateLatencySortIndicators() {
  document.querySelectorAll("th[data-lsort]").forEach(th => {
    const ind = th.querySelector(".sort-ind");
    if (!ind) return;
    if (th.dataset.lsort === LATENCY_SORT_KEY) ind.textContent = LATENCY_SORT_DIR > 0 ? "▲" : "▼";
    else ind.textContent = "";
  });
}

function sortFlappers(key) {
  if (FLAPPER_SORT_KEY === key) FLAPPER_SORT_DIR = -FLAPPER_SORT_DIR;
  else { FLAPPER_SORT_KEY = key; FLAPPER_SORT_DIR = (key === "flaps") ? -1 : 1; }
  _saveSort("flappers", FLAPPER_SORT_KEY, FLAPPER_SORT_DIR);
  renderFlappers(FLAPPER_DATA);
  updateFlapperSortIndicators();
}
function updateFlapperSortIndicators() {
  document.querySelectorAll("th[data-fsort]").forEach(th => {
    const ind = th.querySelector(".sort-ind");
    if (!ind) return;
    if (th.dataset.fsort === FLAPPER_SORT_KEY) ind.textContent = FLAPPER_SORT_DIR > 0 ? "▲" : "▼";
    else ind.textContent = "";
  });
}

let _firstAnalyticsLoad = true;
async function loadAnalytics() {
  if (!ANALYTICS_ENABLED) return;
  if (_firstAnalyticsLoad) {
    renderSkeleton(document.getElementById("latency-body"), 4, 5);
    renderSkeleton(document.getElementById("flappers-body"), 2, 3);
  }
  try {
    const url = `/api/analytics?latency_seconds=${LATENCY_PERIOD}&_=${Date.now()}`;
    const r = await fetch(url);
    const data = await r.json();
    LATENCY_DATA = data.latency || [];
    FLAPPER_DATA = data.flappers || [];
    renderLatencyTable(LATENCY_DATA);
    // v1.25.13: slice(0,50) теперь внутри renderFlappers
    // (иначе сортировка по клику работала только по «первым 50»).
    renderFlappers(FLAPPER_DATA);
    renderTimeline(data.timeline || [], data.timeline_total || 0);
    const summaryEl = document.getElementById("activity-summary");
    if (summaryEl) summaryEl.textContent = "";
    renderActivity(data.activity || []);
    renderFlapsChart(data.flaps_hourly || []);
    updateLatencySortIndicators();
    updateFlapperSortIndicators();
    _firstAnalyticsLoad = false;
  } catch (e) { console.error("loadAnalytics", e); }
}

function renderLatencyTable(devs) {
  const tb = document.getElementById("latency-body");
  if (!tb) return;
  if (devs.length === 0) { tb.innerHTML = '<tr><td colspan="3" class="muted">Нет данных</td></tr>'; return; }
  const s = [...devs].sort((a, b) => {
    let av, bv;
    switch (LATENCY_SORT_KEY) {
      case "name": av = (a.friendly_name || a.name || "").toLowerCase();
                   bv = (b.friendly_name || b.name || "").toLowerCase();
                   return av.localeCompare(bv) * LATENCY_SORT_DIR;
      case "ip":
        av = (a.ip || "").split(".").map(n => parseInt(n, 10) || 0);
        bv = (b.ip || "").split(".").map(n => parseInt(n, 10) || 0);
        for (let i = 0; i < 4; i++) {
          if (av[i] !== bv[i]) return (av[i] - bv[i]) * LATENCY_SORT_DIR;
        }
        return 0;
      case "avg":
        av = (a.avg_ms_24h === null || a.avg_ms_24h === undefined) ? 999999 : a.avg_ms_24h;
        bv = (b.avg_ms_24h === null || b.avg_ms_24h === undefined) ? 999999 : b.avg_ms_24h;
        return (av - bv) * LATENCY_SORT_DIR;
      case "ts":
        av = a.latency_ts || 0; bv = b.latency_ts || 0;
        return (av - bv) * LATENCY_SORT_DIR;
    }
    return 0;
  });
  const periodLabel = latencyPeriodLabel(LATENCY_PERIOD);
  tb.innerHTML = s.map(d => {
    const avg = d.avg_ms_24h;
    const cnt = d.latency_count || 0;
    const timeouts = d.latency_timeouts || 0;
    // v1.23.6: плашка с мс сверху, счётчики — под ней.
    const avgHtml = (avg === null || avg === undefined)
      ? `<span class="latency lat-timeout">нет данных</span>`
      : `<span class="latency ${latencyClass(avg)}">${avg} ms</span>`;
    // v1.28.87: компактно — период · кол-во пингов (+ ⚠timeout).
    const metaLine = `<div class="muted" style="font-size:11px; margin-top:2px;">${escapeHtml(periodLabel)} · ${cnt}p${timeouts > 0 ? ` · <span style="color:var(--red)" title="${timeouts} timeout">⚠${timeouts}</span>` : ""}</div>`;
    const ipLine = d.ip ? `<div class="muted" style="font-size:11px; font-family:ui-monospace,monospace;">${escapeHtml(d.ip)}</div>` : "";
    return `<tr>
      <td><div>${escapeHtml(d.friendly_name || d.name)}</div>${ipLine}</td>
      <td>${avgHtml}${metaLine}</td>
      <td class="muted">${d.latency_ts ? fmtAgo(d.latency_ts) : "—"}</td>
    </tr>`;
  }).join("");
}

function renderFlappers(f) {
  const tb = document.getElementById("flappers-body");
  if (!tb) return;
  if (f.length === 0) { tb.innerHTML = '<tr><td colspan="2" class="muted">Ничего не мерцало 🎉</td></tr>'; return; }
  const s = [...f].sort((a, b) => {
    if (FLAPPER_SORT_KEY === "dev") return (a.dev || "").localeCompare(b.dev || "") * FLAPPER_SORT_DIR;
    return ((a.flaps || 0) - (b.flaps || 0)) * FLAPPER_SORT_DIR;
  }).slice(0, 50);  // v1.25.13: сортируем весь набор, показываем топ-50
  // v1.22.4: friendly_name + серое name
  tb.innerHTML = s.map(x => `<tr><td>${deviceNameCell(x.dev)}</td><td><strong>${x.flaps}</strong></td></tr>`).join("");
}

function renderTimeline(t, total) {
  const list = document.getElementById("timeline-list");
  const counter = document.getElementById("timeline-count");
  // v1.23.6: защита от тысяч записей — показываем максимум 50.
  const LIMIT = 50;
  const shown = (t || []).slice(0, LIMIT);
  if (counter) {
    if (total && total > shown.length) counter.textContent = `показаны ${shown.length} из ${total}`;
    else if (shown.length) counter.textContent = `всего: ${shown.length}`;
    else counter.textContent = "";
  }
  if (!list) return;
  if (shown.length === 0) { list.innerHTML = '<div class="muted" style="padding:16px;">Нет событий</div>'; return; }
  // v1.22.4: friendly_name + серое name
  list.innerHTML = shown.map(x => `<div class="timeline-item">
    <span class="timeline-ts">${fmtDateTime(x.ts)}</span>
    <span>${deviceNameCell(x.dev)}</span>
    <span style="color:${x.status === "online" ? "var(--green)" : "var(--red)"};">${escapeHtml(x.status)}</span>
  </div>`).join("");
}

let _ACTIVITY_HITS = [];

function renderActivity(points) {
  const svg = document.getElementById("chart-activity");
  if (!svg) return;
  if (svg.dataset.ttBound === "1" && _chartTooltip.isOpen()) return;
  const summaryEl = document.getElementById("activity-summary");
  if (!points || points.length === 0) {
    svg.innerHTML = '<text x="400" y="90" text-anchor="middle" fill="var(--muted)" font-size="13">Нет данных (нужно минимум 2 часа работы)</text>';
    if (summaryEl) summaryEl.textContent = "";
    return;
  }

  const W = 800, H = 180;
  const PAD = {top:16, bottom:26, left:34, right:14};
  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;
  const maxTotal = Math.max(...points.map(p => p.total || 0), 1);

  const now = Math.floor(Date.now() / 1000);
  const tsEnd = now;
  // v1.22.7: округляем tsStart до целого часа, иначе первый/последний
  // столбик/точка обрезается при now не ровно в :00 (M4 follow-up).
  const tsStart = Math.floor((now - 24 * 3600) / 3600) * 3600;
  const tsSpan = tsEnd - tsStart;

  let pathOnline = "";
  let pathTotal = "";
  let areaOnline = "";
  let firstX = null, lastX = null;
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const x = PAD.left + ((p.ts - tsStart) / tsSpan) * iw;
    const yO = PAD.top + ih - (p.online / maxTotal) * ih;
    const yT = PAD.top + ih - (p.total / maxTotal) * ih;
    if (firstX === null) firstX = x;
    lastX = x;
    pathOnline += (i === 0 ? "M" : "L") + ` ${x.toFixed(1)} ${yO.toFixed(1)}`;
    pathTotal += (i === 0 ? "M" : "L") + ` ${x.toFixed(1)} ${yT.toFixed(1)}`;
  }
  if (firstX !== null && lastX !== null) {
    areaOnline = `M ${firstX.toFixed(1)} ${(PAD.top + ih).toFixed(1)} ` +
                 pathOnline.replace(/^M/, "L") +
                 ` L ${lastX.toFixed(1)} ${(PAD.top + ih).toFixed(1)} Z`;
  }

  const yLines = [0, maxTotal / 2, maxTotal];
  let grid = "";
  for (const val of yLines) {
    const y = PAD.top + ih - (val / maxTotal) * ih;
    grid += `<line x1="${PAD.left}" y1="${y.toFixed(1)}" x2="${W - PAD.right}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-dasharray="2,2"/>`;
    grid += `<text x="${PAD.left - 4}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="var(--muted)" font-size="10">${Math.round(val)}</text>`;
  }

  let xLabels = "";
  const stepSec = 2 * 3600;
  let t = Math.ceil(tsStart / 3600) * 3600;
  while (t <= tsEnd) {
    const x = PAD.left + ((t - tsStart) / tsSpan) * iw;
    const d = new Date(t * 1000);
    xLabels += `<text x="${x.toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="var(--muted)" font-size="10">${String(d.getHours()).padStart(2,'0')}:00</text>`;
    t += stepSec;
  }

  svg.innerHTML = `
    ${grid}
    <path d="${areaOnline}" fill="var(--green)" opacity="0.15"/>
    <path d="${pathTotal}" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.8"/>
    <path d="${pathOnline}" fill="none" stroke="var(--green)" stroke-width="2"/>
    ${xLabels}
  `;

  _ACTIVITY_HITS = [];
  const hourW = iw / 24;
  for (const p of points) {
    const x = PAD.left + ((p.ts - tsStart) / tsSpan) * iw;
    _ACTIVITY_HITS.push({
      x: x, w: hourW, ts: p.ts,
      online: p.online || 0, total: p.total || 0,
    });
  }
  // v1.25.14: bind() сам снимает старых слушателей через
  // AbortController (см. bindDesktop/bindMobile). delete ttBound
  // больше не нужен — он приводил к накоплению слушателей.
  _chartTooltip.bind(svg, {
    hitTest: (svgEl, ev) => {
      const { x } = svgClientToLocal(svgEl, ev.clientX, ev.clientY);
      for (const h of _ACTIVITY_HITS) {
        if (x >= h.x && x <= h.x + h.w) return { data: h };
      }
      return null;
    },
    onHover: (svgEl, hit) => {
      svgEl.querySelectorAll("circle.activity-marker").forEach(c => c.remove());
      const h = hit.data;
      const maxTotal = Math.max(..._ACTIVITY_HITS.map(x => x.total || 0), 1);
      const yO = PAD.top + ih - (h.online / maxTotal) * ih;
      const yT = PAD.top + ih - (h.total / maxTotal) * ih;
      const cx = h.x + h.w / 2;
      const ns = "http://www.w3.org/2000/svg";
      const c1 = document.createElementNS(ns, "circle");
      c1.setAttribute("class", "activity-marker");
      c1.setAttribute("cx", cx);
      c1.setAttribute("cy", yO);
      c1.setAttribute("r", 5);
      c1.setAttribute("fill", "var(--green)");
      c1.setAttribute("stroke", "var(--fg)");
      c1.setAttribute("stroke-width", "1.5");
      svgEl.appendChild(c1);
      const c2 = document.createElementNS(ns, "circle");
      c2.setAttribute("class", "activity-marker");
      c2.setAttribute("cx", cx);
      c2.setAttribute("cy", yT);
      c2.setAttribute("r", 4);
      c2.setAttribute("fill", "var(--muted)");
      c2.setAttribute("stroke", "var(--fg)");
      c2.setAttribute("stroke-width", "1.2");
      svgEl.appendChild(c2);
    },
    onLeave: (svgEl) => {
      svgEl.querySelectorAll("circle.activity-marker").forEach(c => c.remove());
    },
    render: (hit) => {
      const h = hit.data;
      const d1 = new Date(h.ts * 1000);
      const hm = String(d1.getHours()).padStart(2, "0") + ":00";
      const dd = d1.toLocaleDateString("ru-RU", { day:"2-digit", month:"2-digit" });
      const offline = Math.max(0, (h.total || 0) - (h.online || 0));
      return `<div class="tt-head">${dd} ${hm}</div>
        <div class="tt-row"><span class="tt-name">⚪ Всего:</span><span class="tt-val">${h.total}</span></div>
        <div class="tt-row"><span class="tt-name">🟢 Online:</span><span class="tt-val">${h.online}</span></div>
        <div class="tt-row"><span class="tt-name">🔴 Offline:</span><span class="tt-val">${offline}</span></div>`;
    },
  });

  if (summaryEl) {
    const last = points[points.length - 1];
    const totalDev = last.total || 0;
    const onlineDev = last.online || 0;
    summaryEl.textContent = `${onlineDev}/${totalDev} online сейчас`;
  }
}

let _FLAPS_HITS = [];

function renderFlapsChart(points) {
  const svg = document.getElementById("chart-flaps");
  if (!svg) return;
  if (svg.dataset.ttBound === "1" && _chartTooltip.isOpen()) return;
  if (!points || points.length === 0) {
    svg.innerHTML = '<text x="400" y="70" text-anchor="middle" fill="var(--muted)" font-size="13">Нет переходов за 24ч</text>';
    return;
  }

  const W = 800, H = 140;
  const PAD = {top:16, bottom:26, left:34, right:14};
  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;

  const now = Math.floor(Date.now() / 1000);
  const tsEnd = now;
  // v1.22.7: округляем tsStart до целого часа, чтобы все hour_ts из БД
  // (целые часы) попадали в [tsStart, tsEnd] (M4 follow-up).
  const tsStart = Math.floor((now - 24 * 3600) / 3600) * 3600;
  const tsSpan = tsEnd - tsStart;

  const byHour = {};
  const byHourDevices = {};
  for (const p of points) {
    byHour[p.ts] = (byHour[p.ts] || 0) + (p.flaps || 0);
    if (Array.isArray(p.devices) && p.devices.length > 0) {
      if (!byHourDevices[p.ts]) byHourDevices[p.ts] = [];
      for (const d of p.devices) {
        byHourDevices[p.ts].push(d);
      }
    }
  }
  for (const k of Object.keys(byHourDevices)) {
    const merged = {};
    for (const d of byHourDevices[k]) {
      merged[d.name] = (merged[d.name] || 0) + (d.count || 0);
    }
    byHourDevices[k] = Object.keys(merged)
      .map(n => ({ name: n, count: merged[n] }))
      .sort((a, b) => (-a.count) || a.name.localeCompare(b.name));
  }
  const maxFlaps = Math.max(...Object.values(byHour), 1);

  // v1.22.6: столбики рисуем по реальным ключам byHour,
  // а не по фиксированным 24 часам. Раньше hourTs округлялся
  // от tsStart=now-24ч, и при now не ровно в :00 столбики
  // сдвигались/терялись.
  const barWidth = iw / 24;
  let bars = "";
  let totalFlaps = 0;
  const sortedHours = Object.keys(byHour).map(k => parseInt(k, 10)).sort((a, b) => a - b);
  _FLAPS_HITS = [];
  for (const hTs of sortedHours) {
    const v = byHour[hTs] || byHour[String(hTs)] || 0;
    totalFlaps += v;
    if (v === 0) continue;
    const x = PAD.left + ((hTs - tsStart) / tsSpan) * iw;
    const h = (v / maxFlaps) * ih;
    const y = PAD.top + ih - h;
    const hitIdx = _FLAPS_HITS.length;
    _FLAPS_HITS.push({ x: x, y: y, w: barWidth, h: h,
                       ts: hTs, flaps: v, devices: byHourDevices[hTs] || [] });
    bars += `<rect class="bar-hover hoverable" data-hit="${hitIdx}" x="${(x + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${(barWidth - 2).toFixed(1)}" height="${h.toFixed(1)}" fill="var(--yellow)" opacity="0.75" rx="1"/>`;
  }

  let grid = "";
  for (const val of [0, maxFlaps]) {
    const y = PAD.top + ih - (val / maxFlaps) * ih;
    grid += `<line x1="${PAD.left}" y1="${y.toFixed(1)}" x2="${W - PAD.right}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-dasharray="2,2"/>`;
    grid += `<text x="${PAD.left - 4}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="var(--muted)" font-size="10">${val}</text>`;
  }

  let xLabels = "";
  let t = Math.ceil(tsStart / 3600) * 3600;
  while (t <= tsEnd) {
    const x = PAD.left + ((t - tsStart) / tsSpan) * iw;
    const d = new Date(t * 1000);
    xLabels += `<text x="${x.toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="var(--muted)" font-size="10">${String(d.getHours()).padStart(2,'0')}:00</text>`;
    t += 2 * 3600;
  }

  svg.innerHTML = `
    ${grid}
    ${bars}
    ${xLabels}
  `;

  // v1.25.14: bind() сам снимает старых слушателей через
  // AbortController — delete ttBound отменён (накапливал слушателей).
  _chartTooltip.bind(svg, {
    hitTest: (svgEl, ev) => {
      const { x, y } = svgClientToLocal(svgEl, ev.clientX, ev.clientY);
      for (let i = 0; i < _FLAPS_HITS.length; i++) {
        const h = _FLAPS_HITS[i];
        if (x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h) {
          return { idx: i, data: h };
        }
      }
      return null;
    },
    onHover: (svgEl, hit) => {
      svgEl.querySelectorAll("rect.bar-hover").forEach(el => {
        if (parseInt(el.dataset.hit) === hit.idx) el.classList.add("hover");
        else el.classList.remove("hover");
      });
    },
    onLeave: (svgEl) => {
      svgEl.querySelectorAll("rect.bar-hover").forEach(el => el.classList.remove("hover"));
    },
    render: (hit) => {
      const h = hit.data;
      const d1 = new Date(h.ts * 1000);
      const hm = String(d1.getHours()).padStart(2, "0") + ":00";
      const dd = d1.toLocaleDateString("ru-RU", { day:"2-digit", month:"2-digit" });
      const head = `${dd} ${hm}`;
      const parts = [`<div class="tt-head">${head}</div>`];
      parts.push(`<div class="tt-row"><span class="tt-name">⚡ Мерцания:</span><span class="tt-val">${h.flaps}</span></div>`);
      const devs = h.devices || [];
      if (devs.length > 0) {
        parts.push(`<div style="height:6px;"></div>`);
        const LIMIT = 5;
        for (let i = 0; i < Math.min(LIMIT, devs.length); i++) {
          const d = devs[i];
          const disp = resolveDeviceDisplayName(d.name);
          parts.push(`<div class="tt-row"><span class="tt-name">${ttEscape(disp.friendly)}</span><span class="tt-val">${d.count}</span></div>`);
        }
        if (devs.length > LIMIT) {
          parts.push(`<div class="tt-more">+${devs.length - LIMIT} ещё</div>`);
        }
      }
      return parts.join("");
    },
  });

  const summaryEl = document.getElementById("activity-summary");
  if (summaryEl) {
    const avg = (totalFlaps / 24).toFixed(1);
    const prev = summaryEl.textContent;
    summaryEl.textContent = prev ? `${prev} · переходов: ${totalFlaps} (${avg}/ч)` : `переходов: ${totalFlaps} (${avg}/ч)`;
  }
}

// v1.21.2: health-widget
let HEALTH_DETAIL_OPEN = false;

function fmtUptimeShort(s) {
  s = parseInt(s) || 0;
  if (s <= 0) return "\u2014";
  const d = Math.floor(s/86400); s %= 86400;
  const h = Math.floor(s/3600);  s %= 3600;
  const m = Math.floor(s/60);
  if (d) return `${d}\u0434 ${h}\u0447`;
  if (h) return `${h}\u0447 ${m}\u043c`;
  return `${m}\u043c`;
}

async function refreshHealthWidget(force) {
  const widget = document.getElementById("health-widget");
  if (!widget) return;
  try {
    const r = await fetch("/api/health/full?_=" + Date.now());
    if (!r.ok) throw new Error("HTTP " + r.status);
    const h = await r.json();

    document.getElementById("hw-webui").textContent = "WebUI v" + (h.webui?.version || "?");
    document.getElementById("hw-bridge").textContent = "Bridge v" + (h.bridge?.version || "?");
    const st = document.getElementById("hw-status");
    const okBridge = h.bridge?.status === "online";
    // v1.28.17: hw-status — только точка. Слово online убрано (title — тултип).
    st.innerHTML = `<span class="dot ${okBridge ? "online" : "offline"}" title="${okBridge ? "online" : "offline"}"></span>`;
    st.className = okBridge ? "hw-dim" : "hw-err";

    const up = fmtUptimeShort(h.bridge?.uptime || 0);
    const total = h.devices?.total || 0;
    const online = h.devices?.online || 0;
    const quietNow = h.quiet?.now || 0;
    const cpu = h.webui?.cpu_pct;
    const ram = h.webui?.rss_mb;
    const cpuStr = (cpu !== null && cpu !== undefined) ? cpu + "%" : "\u2014";
    const ramStr = (ram !== null && ram !== undefined) ? ram + "\u041c\u0411" : "\u2014";
    // v1.23.0: на мобиле (hw-opt) скрываются uptime, quiet, cpu/ram.
    // Остаётся: Bridge vX · ● online · N/M.
    const quietHtml = quietNow > 0 ? `<span class="hw-quiet hw-opt">\ud83d\udd07 ${quietNow}</span>` : "";
    document.getElementById("hw-rest").innerHTML =
      `<span class="hw-sep hw-opt">\u00b7</span> <span class="hw-opt">${up}</span> ` +
      `<span class="hw-sep">\u00b7</span> ${online}/${total} ` +
      (quietHtml ? `<span class="hw-sep hw-opt">\u00b7</span> ${quietHtml} ` : "") +
      `<span class="hw-sep hw-opt">\u00b7</span> <span class="hw-dim hw-opt">\u2699 ${cpuStr} / ${ramStr}</span>`;

    // v1.23.8: рендерим деталку, если открыта ИЛИ принудительно.
    if (HEALTH_DETAIL_OPEN || force) renderHealthDetail(h);
  } catch (e) {
    const st = document.getElementById("hw-status");
    if (st) { st.innerHTML = '<span class="dot offline" title="\u043e\u0448\u0438\u0431\u043a\u0430"></span>'; st.className = "hw-err"; }
    const rest = document.getElementById("hw-rest");
    if (rest) rest.innerHTML = "";
    // v1.23.8: если деталка открыта — покажем ошибку.
    if (HEALTH_DETAIL_OPEN || force) {
      const el = document.getElementById("health-detail");
      if (el) el.innerHTML = '<div class="hd-row"><span class="hd-key">\u2014</span>'
                          + '<span style="color:var(--red)">ошибка: '
                          + escapeHtml(e.message) + '</span></div>';
    }
  }
}

// v1.25.0 (task #B): health-карточка — на русском.
// Прогресс-бар убран. Строка «Снапшоты» показывается только
// если state_history > 0 (иначе она бессмысленна).
function _hdCpuBadge(pct) {
  if (pct === null || pct === undefined) return '<span class="hd-badge muted">—</span>';
  let cls = "ok";
  if (pct >= 80) cls = "err";
  else if (pct >= 50) cls = "warn";
  return `<span class="hd-badge ${cls}">${pct}%</span>`;
}
function _hdRamBadge(mb) {
  if (mb === null || mb === undefined) return '<span class="hd-badge muted">—</span>';
  let cls = "ok";
  if (mb >= 300) cls = "err";
  else if (mb >= 150) cls = "warn";
  return `<span class="hd-badge ${cls}">${mb} МБ</span>`;
}
function renderHealthDetail(h) {
  const el = document.getElementById("health-detail");
  if (!el) return;
  try {
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
      const r = sel.getRangeAt(0);
      if (el.contains(r.commonAncestorContainer)) return;
    }
  } catch (e) { /* ignore */ }
  const w = h.webui || {}; const b = h.bridge || {}; const db = h.db || {};
  const fmt = (v, suf) => (v === null || v === undefined) ? "—" : (v + (suf || ""));
  const okB = b.status === "online";
  const dotB = okB ? "g" : "r";
  const uptime = fmtUptimeShort(b.uptime);
  const dbMb = fmt(db.size_mb, " МБ");
  const online = h.devices?.online || 0;
  const total = h.devices?.total || 0;
  const qTotal = h.quiet?.total || 0;
  const qNow = h.quiet?.now || 0;
  el.innerHTML = `
    <div class="hd-head">
      <span class="hd-head-title">⚙ Состояние системы</span>
    </div>
    <div class="hd-grid">
      <span class="hd-lbl">WebUI:</span>
      <span class="hd-val">v${fmt(w.version)} · потоков ${fmt(w.threads)}</span>

      <span class="hd-lbl">CPU / RAM:</span>
      <span class="hd-val"><span class="hd-badge">CPU</span> ${_hdCpuBadge(w.cpu_pct)} <span class="hd-badge">RAM</span> ${_hdRamBadge(w.rss_mb)}</span>

      <span class="hd-lbl">Bridge:</span>
      <span class="hd-val">v${fmt(b.version)} · <span class="hd-dot ${dotB}"></span>${okB ? "online" : "offline"} · ${uptime}</span>

      <span class="hd-lbl">Bridge CPU/RAM:</span>
      <span class="hd-val"><span class="hd-badge">CPU</span> ${_hdCpuBadge(b.cpu_pct)} <span class="hd-badge">RAM</span> ${_hdRamBadge(b.rss_mb)}</span>

      <span class="hd-lbl">Устройства:</span>
      <span class="hd-val">${online}/${total} онлайн${
          qTotal ? ` <span class="hd-badge warn">🔇 тишина: ${qTotal} (сейчас ${qNow})</span>` : ''
        }</span>
    </div>

    <div class="hd-section">
      <div class="hd-section-title">🗄 База данных SQLite</div>
      <div class="hd-grid">
        <span class="hd-lbl">Размер:</span>
        <span class="hd-val">${dbMb}</span>
        <span class="hd-lbl">Статусы:</span>
        <span class="hd-val">${fmt(db.status_events)} записей</span>
        <span class="hd-lbl">Задержки:</span>
        <span class="hd-val">${fmt(db.latency_history)} замеров</span>
      </div>
    </div>
  `;
}

function toggleHealthDetail() {
  const el = document.getElementById("health-detail");
  const widget = document.getElementById("health-widget");
  if (!el) return;
  HEALTH_DETAIL_OPEN = !HEALTH_DETAIL_OPEN;
  if (HEALTH_DETAIL_OPEN) {
    el.style.display = "block";
    // v1.23.8: показываем индикатор загрузки, потом обновляем.
    el.innerHTML = '<div class="hd-row"><span class="hd-key">—</span>'
                 + '<span class="muted">загрузка…</span></div>';
    if (widget) widget.classList.add("hw-expanded");
    // v1.23.8: фикс — при простое >30 сек деталка была пустой.
    // Всегда дёргаем refreshHealthWidget(true).
    refreshHealthWidget(true);
  } else {
    el.style.display = "none";
    if (widget) widget.classList.remove("hw-expanded");
  }
}

async function loadAudit() {
  const c = document.getElementById("tools-container");
  const info = document.getElementById("tools-info");
  if (!c) return;
  c.innerHTML = '<div class="muted" style="padding:16px;"><span class="spin"></span> \u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430\u2026</div>';
  try {
    const r = await fetch("/api/config/audit?limit=100&_=" + Date.now());
    const data = await r.json();
    const items = data.items || [];
    if (info) info.textContent = `\u0417\u0430\u043f\u0438\u0441\u0435\u0439: ${items.length}`;
    if (items.length === 0) {
      c.innerHTML = '<div class="audit-empty">\u0418\u0441\u0442\u043e\u0440\u0438\u044f \u043f\u0443\u0441\u0442\u0430 \u2014 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0444\u0438\u0433\u0430 \u0447\u0435\u0440\u0435\u0437 WebUI \u0435\u0449\u0451 \u043d\u0435 \u0431\u044b\u043b\u043e.</div>';
      return;
    }
    // v1.28.7: обёртка .audit-wrap — горизонтальный скролл на мобиле.
    let html = '<div style="padding:0 16px 16px 16px;"><div class="audit-wrap"><table class="audit-table"><thead><tr>'
      + '<th style="width:160px;">\u0412\u0440\u0435\u043c\u044f</th>'
      + '<th style="width:90px;">\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f</th>'
      + '<th style="width:180px;">\u0423\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e</th>'
      + '<th>\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f</th>'
      + '<th style="width:70px;">\u0421\u0442\u0430\u0442\u0443\u0441</th>'
      + '</tr></thead><tbody>';
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const ts = new Date((it.ts || 0) * 1000).toLocaleString("ru-RU");
      const op = it.op || "?";
      const opCls = ["edit","delete","import"].includes(op) ? op : "edit";
      const dev = it.device ? escapeHtml(it.device) : "\u2014";
      let changes = "";
      if (op === "edit" && it.changes) {
        const keys = Object.keys(it.changes);
        // v1.28.0: для dps_map — компактный diff (+N −M ~K DP).
        const _fmtChange = (k) => {
          const c2 = it.changes[k] || {};
          if (k === "dps_map") {
            const a = (c2.added || []).length;
            const r = (c2.removed || []).length;
            const ch = (c2.changed || []).length;
            const parts = [];
            if (a) parts.push("+" + a);
            if (r) parts.push("\u2212" + r);
            if (ch) parts.push("~" + ch);
            if (!parts.length) return "dps_map: \u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439 (" + (c2.new_count ?? "?") + " DP)";
            return "dps_map: " + parts.join(" ") + " DP (" + (c2.old_count ?? "?") + "\u2192" + (c2.new_count ?? "?") + ")";
          }
          return `${escapeHtml(k)}: ${escapeHtml(String(c2.old ?? "\u2014"))} \u2192 ${escapeHtml(String(c2.new ?? "\u2014"))}`;
        };
        if (keys.length <= 3) {
          changes = keys.map(_fmtChange).join(" \u00b7 ");
        } else {
          changes = `\u0438\u0437\u043c\u0435\u043d\u0435\u043d\u043e: ${keys.length} \u043f\u043e\u043b\u0435\u0439`;
        }
      } else if (op === "import") {
        changes = `+${it.added || 0} / ~${it.updated || 0} / skip ${it.skipped || 0}`;
      } else if (op === "delete") {
        changes = "\u0443\u0434\u0430\u043b\u0435\u043d\u043e \u0438\u0437 \u043a\u043e\u043d\u0444\u0438\u0433\u0430";
      }
      const okHtml = it.ok
        ? '<span style="color:var(--green);">\u2705</span>'
        : '<span style="color:var(--red);">\u274c</span>';
      const rowCls = it.ok ? "" : "fail";
      const detailJson = escapeHtml(JSON.stringify(it, null, 2));
      html += `<tr class="${rowCls}" onclick="this.nextElementSibling.style.display = this.nextElementSibling.style.display === 'none' ? 'table-row' : 'none';">
        <td>${ts}</td>
        <td><span class="audit-op ${opCls}">${escapeHtml(op)}</span></td>
        <td>${dev}</td>
        <td>${changes || '<span class="muted">\u2014</span>'}</td>
        <td>${okHtml}</td>
      </tr>
      <tr style="display:none;"><td colspan="5"><div class="audit-detail">${detailJson}</div></td></tr>`;
    }
    // v1.28.7: закрыть .audit-wrap перед внешним </div>.
    html += '</tbody></table></div></div>';
    c.innerHTML = html;
  } catch (e) {
    c.innerHTML = `<div class="tools-error">\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0438\u0441\u0442\u043e\u0440\u0438\u044e: ${escapeHtml(e.message)}</div>`;
  }
}

// v1.23.6: mobile-only раскладка тулбара логов.
// На десктопе ничего не делает — только проставляет data-role,
// которые используются в @media (max-width:700px).
let _MOBILE_LOGS_LAYOUT_APPLIED = false;
function applyMobileLogsLayout() {
  if (_MOBILE_LOGS_LAYOUT_APPLIED) return;
  const tb = document.querySelector(".logs-toolbar");
  if (!tb) return;
  // v1.23.7: роли — по id/классу, не по позиции (структура изменилась).
  const set = (sel, role) => {
    const el = tb.querySelector(sel);
    if (el && !el.dataset.role) el.dataset.role = role;
  };
  set("#pause-btn", "pause");
  set("#log-source-switch", "source");
  set("#log-level-filter", "level");
  set("#find-info", "info");
  set(".logs-tb-search", "search");
  set(".logs-download", "download");
  // Роль range — только у диапазона ВНУТРИ row2-right.
  const rangeGroup = tb.querySelector(".logs-toolbar-row2-right .time-btn-group");
  if (rangeGroup && !rangeGroup.dataset.role) rangeGroup.dataset.role = "range";
  // Подпись «Источник:»
  const lbl = Array.from(tb.querySelectorAll("span.muted"))
    .find(s => s.textContent.trim().startsWith("Источник"));
  if (lbl && !lbl.dataset.role) lbl.dataset.role = "source-label";
  _MOBILE_LOGS_LAYOUT_APPLIED = true;
}
applyMobileLogsLayout();
// v1.24.2: убран resize-listener — applyMobileLogsLayout имеет
// флаг _MOBILE_LOGS_LAYOUT_APPLIED, повторные вызовы ничего не делают.


// ==================== v1.27.0: DPS EDIT ====================
// Staging-редактор dps_map.
// Изолирован от renderModalVolatile — fetchStatus каждые 5 сек
// не трогает секцию DP.
//
// DPS_EDIT = {
//   _name: string,                    // имя устройства
//   _devType: string,                 // v1.27.1: type устройства (light/climate/...)
//   _editMode: false,                 // v1.27.1: тумблер «Режим редактирования»
//   _on_device: {dp: info},           // снимок без _from_cache
//   _discovered: [{dp, info, source, value}],
//   _removed: Set<dp>,                // удалённые из on_device (в staging)
//   _added: [{dp, info}],             // добавленные из discovered
//   _modified: {dp: info},            // v1.27.1: правки старых DP (вариант B)
// }

let DPS_EDIT = null;
const _DPS_OPEN_STATE = {};

function _onDpsToggle(deviceName, isOpen) {
  if (!deviceName) return;
  // v1.27.5: junk-секция использует ключ с префиксом junk_
  if (deviceName.startsWith("junk_")) {
    _DPS_OPEN_STATE["_dpsJunkOpen_" + deviceName.slice(5)] = !!isOpen;
    return;
  }
  _DPS_OPEN_STATE["_dpsOpen_" + deviceName] = !!isOpen;
}

function dpsIsDirty() {
  if (!DPS_EDIT) return false;
  return DPS_EDIT._removed.size > 0
      || DPS_EDIT._added.length > 0
      || Object.keys(DPS_EDIT._modified || {}).length > 0;
}

function dpsInitEdit(d, force) {
  if (!force && DPS_EDIT && DPS_EDIT._name === d.name && dpsIsDirty()) {
    return;
  }
  // v1.28.32.fixup2: сохраняем старый _discovered для union.
  // Без этого DP, переставшие приходить в cache_snapshot
  // (battery спит, bridge не опрашивает), теряются при пересборке,
  // и removed DP показывает прочерки вместо info.
  const _old_discovered = (DPS_EDIT && DPS_EDIT._name === d.name)
    ? DPS_EDIT._discovered.slice()
    : [];
  const onDevice = {};
  const discovered = [];
  const dps_map = d.dps_map || {};
  const cache = d.cache || {};

  // v1.28.67: производные от phase_a (6_voltage/6_current/6_power) — не DP,
  // их нельзя править. В старых конфигах могли сохраниться — скрываем.
  const _derivedKeys = new Set();
  for (const [k, v] of Object.entries(dps_map)) {
    if (v && v.component === "phase_a") {
      _derivedKeys.add(k + "_voltage");
      _derivedKeys.add(k + "_current");
      _derivedKeys.add(k + "_power");
    }
  }

  // Разбираем dps_map: без _from_cache → на устройстве,
  // с _from_cache → discovered (из Cloud/tuya-local/similar).
  for (const [dp, info] of Object.entries(dps_map)) {
    if (_derivedKeys.has(String(dp))) continue;
    if (!info || typeof info !== "object") continue;
    if (info._from_cache) {
      // v1.28.57: реальный источник, а не всегда "cloud".
      const _src = info._dps_source || "cloud";
      discovered.push({
        dp: String(dp),
        info: info,
        source: _src,
        value: cache[dp],
        cloud_meta: info,
      });
    } else {
      onDevice[String(dp)] = info;
    }
  }

  // DP в cache, которых нет в dps_map → discovered (из cache).
  for (const [dp, val] of Object.entries(cache)) {
    const dpS = String(dp);
    if (dps_map[dpS] || _derivedKeys.has(dpS)) continue;  // уже учтён / производный
    discovered.push({
      dp: dpS,
      info: null,
      source: "cache",
      value: val,
      cloud_meta: null,
    });
  }

  // v1.28.57: если DP есть и в источнике, и в cache — помечаем "+cache".
  for (const item of discovered) {
    if (item.value !== undefined && !item.source.includes("+cache") && item.source !== "cache") {
      item.source = item.source + "+cache";
    }
  }

  // Сортируем discovered: сначала с cache, потом без
  discovered.sort((a, b) => {
    const order = { "cloud+cache": 0, "cloud": 1, "tuya_local+cache": 2,
                    "tuya_local": 3, "similar+cache": 4, "similar": 5, "cache": 6 };
    const oa = order[a.source] !== undefined ? order[a.source] : 7;
    const ob = order[b.source] !== undefined ? order[b.source] : 7;
    if (oa !== ob) return oa - ob;
    return parseInt(a.dp) - parseInt(b.dp);
  });

  // v1.28.32.fixup2: union — добавляем DP из старого _discovered,
  // которых нет в новом. Новый приоритетнее (свежий info).
  // Дедуп по dp.
  const _new_dp_set = new Set(discovered.map(x => x.dp));
  for (const old_item of _old_discovered) {
    if (_new_dp_set.has(old_item.dp)) continue;
    discovered.push(old_item);
  }

  DPS_EDIT = {
    _name: d.name,
    _devType: d.type || "",
    _editMode: false,
    _on_device: onDevice,
    _discovered: discovered,
    _cache: cache,              // v1.28.67: значения из кэша bridge
    _tuyaId: d.tuya_id || "",   // v1.28.71: для справки Cloud status
    _removed: new Set(),
    _added: [],
    _modified: {},
  };
}

// v1.27.3: DP-подсказка. В UI — только code, а перевод/оригинал/
// источник — в столбик через .dp-tip. Никаких врезаний текста.
function _dpTipBadge(code, cloudName, cfgName) {
  const r = resolveDpDisplay(code || "", cloudName || "", cfgName || "");
  const tip = escapeAttr(r.tooltip);
  const badge = r.badge ? `${r.badge}` : '';
  if (!badge) return '';
  return ` <span class="dp-tip" data-tip="${tip}">${badge}</span>`;
}

// v1.27.4: ячейка колонки «Перевод» — 📖/🈶 или «—».
function _dpTranslateCell(code, cloudName, cfgName) {
  const r = resolveDpDisplay(code || "", cloudName || "", cfgName || "");
  if (!r.badge) return '<span class="muted">—</span>';
  const tip = escapeAttr(r.tooltip);
  return `<span class="dp-tip" data-tip="${tip}">${r.badge}</span>`;
}

function _dpsRenderSection(d) {
  if (!d) return "";

  // Не инициализирован — placeholder
  if (!DPS_EDIT || DPS_EDIT._name !== d.name) {
    return `<div class="dps-section">
      <div class="dps-header">
        <h3>Управление DP</h3>
      </div>
      <div class="dps-empty"><span class="spin"></span> загрузка…</div>
    </div>`;
  }

  const cache = d.cache || {};
  const hasCache = Object.keys(cache).length > 0;
  const hasDiscovered = DPS_EDIT._discovered.length > 0;
  const hasOnDevice = Object.keys(DPS_EDIT._on_device).length > 0;

  // Совсем нет данных — предлагаем запросить Cloud
  if (!hasCache && !hasDiscovered && !hasOnDevice) {
    return `<div class="dps-section">
      <div class="dps-header">
        <h3>Управление DP</h3>
      </div>
      <div class="dps-empty">
        <div>⚠️ Нет данных для сопоставления.</div>
        <div class="dps-info" style="margin-top:8px;">
          Кэш состояния пуст — bridge не присылал DP.<br>
          Cloud-кэш тоже пуст или устарел.
        </div>
        <div class="dps-info">
          Запросите данные на вкладке
          <a href="/import">«Импорт устройств»</a> →
          кнопка «📥 Запросить устройства».
        </div>
        <button onclick="window.location.href='/import'"
                style="margin-top:8px;">→ Импорт устройств</button>
      </div>
    </div>`;
  }

  // Формируем список 1: on_device минус removed, плюс added
  const rows1 = [];
  for (const [dp, info] of Object.entries(DPS_EDIT._on_device)) {
    if (DPS_EDIT._removed.has(dp)) continue;
    rows1.push({ dp, info, added: false });
  }
  for (const a of DPS_EDIT._added) {
    rows1.push({ dp: a.dp, info: a.info, added: true });
  }
  rows1.sort((a, b) => parseInt(a.dp) - parseInt(b.dp));

  // v1.28.32: removed DP возвращаются в список 2 «Обнаружено»
  // с source="removed" (как в 1.28.30). Отдельной секции
  // «Удалено» больше нет — устраняет коллизию дублирования.
  const addedDps = new Set(DPS_EDIT._added.map(a => a.dp));

  const rows2 = [];
  const seen2 = new Set();
  // Removed DP → в список 2 первым (уникальный dp, дедуп через seen2).
  for (const dp of DPS_EDIT._removed) {
    if (seen2.has(dp)) continue;
    seen2.add(dp);
    const disc = DPS_EDIT._discovered.find(x => x.dp === dp);
    const onDev = DPS_EDIT._on_device[dp];
    rows2.push({
      dp,
      info: onDev || (disc && disc.info) || null,
      source: "removed",
      value: disc ? disc.value : undefined,
      cloud_meta: disc ? disc.cloud_meta : null,
    });
  }
  // Discovered DP (Cloud/cache), минус added, минус уже добавленные
  // через _removed, минус on_device (те, что сейчас на устройстве).
  for (const item of DPS_EDIT._discovered) {
    if (seen2.has(item.dp)) continue;
    if (addedDps.has(item.dp)) continue;
    if (DPS_EDIT._on_device[item.dp] && !DPS_EDIT._removed.has(item.dp)) continue;
    seen2.add(item.dp);
    rows2.push(item);
  }
  rows2.sort((a, b) => parseInt(a.dp) - parseInt(b.dp));

  // v1.28.32: rows3 оставлен пустым для совместимости.
  const rows3 = [];

  // v1.27.5: разделяем на обычные и мусорные
  const rows2_normal = [];
  const rows2_junk = [];
  for (const r of rows2) {
    const rinfo = r.info || {};
    const rcode = rinfo.code || rinfo.name || "";
    if (rcode && JUNK_DP_CODES.has(rcode)) {
      rows2_junk.push(r);
    } else {
      rows2_normal.push(r);
    }
  }

  // v1.28.50: секция DP — постоянная (не сворачивается), переименована
  // в «Управление DP» (добавление/удаление/редактирование DP).
  const onCount = Object.keys(DPS_EDIT._on_device).length;
  const removedCount = DPS_EDIT._removed.size;
  const addedCount = DPS_EDIT._added.length;
  const modifiedCount = Object.keys(DPS_EDIT._modified || {}).length;
  const totalOnDevice = onCount - removedCount + addedCount;
  const dirty = dpsIsDirty();
  const editMode = !!DPS_EDIT._editMode;

  const toggleTitle = dirty
    ? "Сначала сохраните или отмените изменения"
    : (editMode ? "Режим редактирования: ВКЛ" : "Режим редактирования: выкл");
  const toggleDisabled = dirty ? "disabled" : "";
  const toggleCls = editMode ? "dps-edit-mode-toggle dps-edit-mode-toggle-on"
                             : "dps-edit-mode-toggle";
  const toggleLabel = editMode ? "✏️ Режим редактирования: ON"
                               : "✏️ Режим редактирования: OFF";

  let html = `<div class="dps-section">
    <h3 style="margin:0 0 4px 0;">Управление DP (${totalOnDevice})</h3>
    <div style="margin-top:8px;">`;

  html += `<div class="dps-hint muted">Обновление — через 🔄 в шапке модалки</div>`;

  // v1.27.1: тумблер режима редактирования.
  html += `<div class="dps-edit-mode-bar">
    <button type="button" class="${toggleCls}" ${toggleDisabled}
      onclick="dpsToggleEditMode()"
      title="${escapeAttr(toggleTitle)}">${toggleLabel}</button>
    ${dirty ? '<span class="dps-edit-mode-locked muted">заблокировано: есть изменения</span>' : ''}
  </div>`;

  if (editMode && !dirty) {
    html += `<div class="dps-edit-mode-warning">⚠️ Режим редактирования: изменения затронут существующие сущности HA</div>`;
  }

  if (dirty) {
    html += `<div class="dps-dirty-banner">● Есть несохранённые изменения`;
    if (addedCount) html += ` · добавлено: ${addedCount}`;
    if (removedCount) html += ` · удалено: ${removedCount}`;
    if (modifiedCount) html += ` · изменено: ${modifiedCount}`;
    html += `</div>`;
  }

  // Список 1
  html += `<div class="dps-list-title">
    <span>На устройстве</span>
    <span class="dps-count">${totalOnDevice}${(removedCount || addedCount) ? ' <span style="color:var(--yellow);">(изменения)</span>' : ''}</span>
  </div>`;

  if (rows1.length === 0) {
    html += `<div class="dps-empty">Нет DP — добавьте из списка ниже</div>`;
  } else {
    html += `<div class="dps-table-wrap"><table class="dps-table"><thead><tr>
      <th class="col-dp">DP</th>
      <th class="col-code">Code</th>
      <th class="col-translate">Перевод</th>
      <th class="col-comp">Component</th>
      <th class="col-type">Тип</th>
      <th class="col-values">Значения</th>
      <th class="col-current">Текущее</th>
      <th class="col-src"></th>
      <th class="col-act"></th>
    </tr></thead><tbody>`;
    for (const row of rows1) {
      const dp = row.dp;
      const info = row.info || {};
      const code = info.code || info.name || "?";
      const comp = info.component || "—";
      const isAdded = row.added;
      const isModified = !isAdded && !!DPS_EDIT._modified[dp];
      let badge = "";
      if (isAdded) badge += ` <span class="dps-source-badge cloud-cache" title="Добавлено в этой сессии">NEW</span>`;
      if (isModified) badge += ` <span class="dps-badge-mod" title="Изменено">MOD</span>`;

      // v1.28.68: зарезервированные bridge DP — только просмотр (🔒).
      const _bfRow = _bridgeForcedInfo(code, code);
      const btnEdit = _bfRow
        ? `<button onclick="dpsViewDp('${escapeAttr(dp)}')" title="Зарезервировано bridge (${escapeAttr(_bfRow.why)}) — только просмотр">🔒</button>`
        : (isAdded
            ? `<button onclick="dpsEditAdded('${escapeAttr(dp)}')" title="Редактировать">✏️</button>`
            : (editMode
                ? `<button onclick="dpsEditOld('${escapeAttr(dp)}')" title="Редактировать">✏️</button>`
                : `<button onclick="dpsViewDp('${escapeAttr(dp)}')" title="Просмотр">👁</button>`));
      const btnRevert = isModified
        ? `<button onclick="dpsRevertModified('${escapeAttr(dp)}')" title="Откатить правку">↩️</button>`
        : "";
      const btnRemove = `<button onclick="dpsMoveOut('${escapeAttr(dp)}')" title="Убрать из конфига">➖</button>`;

      // v1.28.35: единый порядок колонок (как в Cloud-модалке и превью).
      const _translate = _dpTranslateCell(code, "", info.name || "");
      const _curVal = (d.cache && d.cache[dp] !== undefined) ? d.cache[dp] : undefined;
      // v1.28.41: raw в тексте, scale — в тултипе.
      const _sc = (info.scale !== undefined) ? info.scale
                : (info.values && info.values.scale !== undefined ? info.values.scale : undefined);
      const _curScaled = (_sc !== undefined && _curVal !== undefined) ? _scaleVal(_curVal, _sc) : null;
      const _curTitle = (_curScaled !== null && String(_curScaled) !== String(_curVal))
        ? `Масштаб (scale ${_sc}): ${_curScaled}` : "";
      const _curCell = (_curVal !== undefined)
        ? `<span${_curTitle ? ` title="${escapeAttr(_curTitle)}"` : ""}>${escapeHtml(_shortVal(_curVal))}</span>`
        : '<span class="muted">—</span>';
      const _typeCell = info._cloud_type
        ? typeBadge(info._cloud_type) : '<span class="muted">—</span>';
      const _valuesCell = _valuesCellHtml(info.values);
      html += `<tr>
        <td class="col-dp"><strong>${escapeHtml(dp)}</strong></td>
        <td class="col-code"><code>${escapeHtml(code)}</code>${badge}</td>
        <td class="col-translate">${_translate}</td>
        <td class="col-comp">${componentBadge(comp)}</td>
        <td class="col-type">${_typeCell}</td>
        <td class="col-values">${_valuesCell}</td>
        <td class="col-current">${_curCell}</td>
        <td class="col-src"></td>
        <td class="col-act dps-actions">${btnEdit}${btnRevert}${btnRemove}</td>
      </tr>`;
    }
    html += `</tbody></table></div>`;
  }

  // Список 2
  html += `<div class="dps-list-title">
    <span>Обнаружено в Cloud / cache</span>
    <span class="dps-count">${rows2_normal.length}</span>
  </div>`;

  if (rows2_normal.length === 0 && rows2_junk.length === 0) {
    html += `<div class="dps-empty">Нет новых DP</div>`;
  } else {
    if (rows2_normal.length === 0) {
      html += `<div class="dps-empty">Нет новых обычных DP</div>`;
    } else {
    html += `<div class="dps-table-wrap"><table class="dps-table"><thead><tr>
      <th class="col-dp">DP</th>
      <th class="col-code">Code</th>
      <th class="col-translate">Перевод</th>
      <th class="col-comp">Component</th>
      <th class="col-type">Тип</th>
      <th class="col-values">Значения</th>
      <th class="col-current">Текущее</th>
      <th class="col-src">Источник</th>
      <th class="col-act"></th>
    </tr></thead><tbody>`;
    for (const row of rows2_normal) {
      const dp = row.dp;
      const info = row.info || {};
      const code = info.code || info.name || "—";
      const comp = info.component || "—";
      const src = row.source || "";
      const srcCls = src.replace("+", "-");
      // v1.28.70: модалку «Добавить» показываем только ненадёжным
      // источникам (нет сопоставления / эвристика). cloud/tuya-local/similar
      // добавляются сразу.
      const needsFill = !_isTrustedSource(src) && src !== "removed";
      // v1.28.33.fixup5b: 6 групп источника + fallback ❓ unknown.
      // Приоритет: cloud → tuya_local → similar → cache_only
      // → config_only → heuristic → unknown.
      let _dpsSrc = "";
      let _dpsSrcReason = "";
      if (info && info._dps_source) {
        _dpsSrc = info._dps_source;
        _dpsSrcReason = info._dps_source_reason || "";
      } else if (src === "cache") {
        _dpsSrc = "cache_only";
        _dpsSrcReason = "Cloud и tuya-local не дали сопоставления";
      } else if (src === "cloud" || src === "cloud+cache") {
        _dpsSrc = "cloud";
        _dpsSrcReason = "";
      } else if (src === "removed") {
        _dpsSrc = "removed";
        _dpsSrcReason = "DP убран из конфига (staging)";
      } else {
        _dpsSrc = "unknown";
        _dpsSrcReason = "источник не определён";
      }
      const _SRC_ICON = {
        cloud: '☁', tuya_local: '📚', similar: '🔗',
        cache_only: '📦', config_only: '⚙️', removed: '🗑',
        heuristic: '⚠️', unknown: '❓'
      };
      const _SRC_LABEL = {
        cloud: 'Tuya Cloud', tuya_local: 'tuya-local', similar: 'similar',
        cache_only: 'нет сопоставления', config_only: 'из конфига',
        removed: 'удалён', heuristic: 'эвристика', unknown: 'не определён'
      };
      const _srcIcon = _SRC_ICON[_dpsSrc] || '❓';
      const _srcTipLines = [];
      _srcTipLines.push(_dpsSrc === 'cache_only'
        ? 'Сопоставление: нет — есть только значение из кэша bridge'
        : (_dpsSrc === 'removed'
            ? 'Удалён из конфига — можно вернуть (↩️)'
            : 'Сопоставление: ' + (_SRC_LABEL[_dpsSrc] || _dpsSrc)));
      if (_dpsSrcReason) _srcTipLines.push('Причина: ' + _dpsSrcReason);
      if (info && info._cloud_type) _srcTipLines.push('Тип: ' + info._cloud_type);
      if (info && info.code) _srcTipLines.push('Code: ' + info.code);
      if (info && info._name_original) _srcTipLines.push('Оригинал: ' + info._name_original);
      if (info && info.values && Object.keys(info.values).length > 0) {
        try { _srcTipLines.push('Значения: ' + JSON.stringify(info.values)); } catch (e) {}
      }
      if (row.value !== undefined && row.value !== null) {
        _srcTipLines.push('Значение: ' + displayValue(row.value) + ' (кэш bridge)');
      }
      const _srcTip = _srcTipLines.join('\n');
      // v1.28.32: removed DP → ↩️ (вернуть), cloud/cache → ➕ (добавить).
      const addBtn = (src === "removed")
        ? `<button onclick="dpsMoveIn('${escapeAttr(dp)}')" title="Вернуть в конфиг">↩️</button>`
        : needsFill
          ? `<button onclick="dpsOpenFillModal('${escapeAttr(dp)}')" title="Нет надёжного сопоставления — проверьте поля">➕</button>`
          : `<button onclick="dpsMoveIn('${escapeAttr(dp)}')" title="Добавить в конфиг">➕</button>`;
      const _translate2 = _dpTranslateCell(code, "", (info.name || ""));
      // v1.28.35: единый порядок; источник — в бейдже «Источник».
      const _type2 = info._cloud_type
        ? typeBadge(info._cloud_type) : '<span class="muted">—</span>';
      const _values2 = _valuesCellHtml(info.values);
      // v1.28.41: raw в тексте, scale — в тултипе.
      const _sc2 = (info.scale !== undefined) ? info.scale
                : (info.values && info.values.scale !== undefined ? info.values.scale : undefined);
      const _curScaled2 = (_sc2 !== undefined && row.value !== undefined && row.value !== null)
        ? _scaleVal(row.value, _sc2) : null;
      const _curTitle2 = (_curScaled2 !== null && String(_curScaled2) !== String(row.value))
        ? `Масштаб (scale ${_sc2}): ${_curScaled2}` : "";
      const _cur2 = (row.value !== undefined && row.value !== null)
        ? `<span${_curTitle2 ? ` title="${escapeAttr(_curTitle2)}"` : ""}>${escapeHtml(_shortVal(row.value))}</span>`
        : '<span class="muted">—</span>';
      html += `<tr>
        <td class="col-dp"><strong>${escapeHtml(dp)}</strong></td>
        <td class="col-code"><code>${escapeHtml(code)}</code></td>
        <td class="col-translate">${_translate2}</td>
        <td class="col-comp">${componentBadge(comp)}</td>
        <td class="col-type">${_type2}</td>
        <td class="col-values">${_values2}</td>
        <td class="col-current">${_cur2}</td>
        <td class="col-src"><span class="dp-tip dps-source-badge ${srcCls}" data-tip="${escapeAttr(_srcTip)}">${_srcIcon} ${escapeHtml(_SRC_LABEL[_dpsSrc] || _dpsSrc)}</span></td>
        <td class="col-act dps-actions">${addBtn}</td>
      </tr>`;
    }
    html += `</tbody></table></div>`;
    }  // end rows2_normal.length > 0

    // v1.27.5: секция мусорных DP (свёрнута по умолчанию)
    if (rows2_junk.length > 0) {
      const junkOpenKey = "_dpsJunkOpen_" + d.name;
      const isJunkOpen = _DPS_OPEN_STATE[junkOpenKey] === true;
      const junkOpenAttr = isJunkOpen ? "open" : "";
      html += `<details class="dps-junk-section" ${junkOpenAttr}
          ontoggle="_onDpsToggle('${escapeAttr("junk_" + (d.name || ""))}', this.open)">
        <summary>⚠️ Мусорные DP (${rows2_junk.length})<span class="muted" style="font-size:11px; margin-left:6px;">клик — раскрыть</span></summary>
        <div class="dps-table-wrap"><table class="dps-table"><thead><tr>
          <th class="col-dp">DP</th>
          <th class="col-code">Code</th>
          <th class="col-translate"></th>
          <th class="col-comp">Component</th>
          <th class="col-type">Тип</th>
          <th class="col-values">Значения</th>
          <th class="col-current">Текущее</th>
          <th class="col-src">Источник</th>
          <th class="col-act"></th>
        </tr></thead><tbody>`;
      for (const row of rows2_junk) {
        const dp = row.dp;
        const info = row.info || {};
        const code = info.code || info.name || "—";
        const comp = info.component || "—";
        const src = row.source || "";
        const srcCls = src.replace("+", "-");
        const addBtn = `<button class="junk-add" onclick="dpsMoveIn('${escapeAttr(dp)}')" title="Добавить (не рекомендуется)">➕</button>`;
        const _type3 = info._cloud_type ? typeBadge(info._cloud_type) : '<span class="muted">—</span>';
        const _values3 = _valuesCellHtml(info.values);
        const _sc3 = (info.scale !== undefined) ? info.scale
                  : (info.values && info.values.scale !== undefined ? info.values.scale : undefined);
        const _curScaled3 = (_sc3 !== undefined && row.value !== undefined && row.value !== null)
          ? _scaleVal(row.value, _sc3) : null;
        const _curTitle3 = (_curScaled3 !== null && String(_curScaled3) !== String(row.value))
          ? `Масштаб (scale ${_sc3}): ${_curScaled3}` : "";
        const _cur3 = (row.value !== undefined && row.value !== null)
          ? `<span${_curTitle3 ? ` title="${escapeAttr(_curTitle3)}"` : ""}>${escapeHtml(_shortVal(row.value))}</span>`
          : '<span class="muted">—</span>';
        html += `<tr>
          <td class="col-dp"><strong>${escapeHtml(dp)}</strong></td>
          <td class="col-code"><code>${escapeHtml(code)}</code> <span class="badge junk"><span class="junk-text">мусор</span><span class="junk-icon">🗑</span></span></td>
          <td class="col-translate"></td>
          <td class="col-comp">${componentBadge(comp)}</td>
          <td class="col-type">${_type3}</td>
          <td class="col-values">${_values3}</td>
          <td class="col-current">${_cur3}</td>
          <td class="col-src">${_dpsSourceBadge(src, info)}</td>
          <td class="col-act dps-actions">${addBtn}</td>
        </tr>`;
      }
      html += `</tbody></table></div></details>`;
    }
  }

  // v1.28.32: секция «Удалено» удалена — removed DP теперь
  // в списке 2 «Обнаружено в Cloud / cache» (source="removed").

  // Кнопки сохранения/отмены
  if (dirty) {
    html += `<div class="dps-actions-bar">
      <button class="dps-save-btn" onclick="dpsOpenPreview()">💾 Сохранить</button>
      <button onclick="dpsCancel()">↶ Отмена</button>
    </div>`;
  }

  html += `</div></div>`;
  return html;
}

function dpsRenderSection() {
  if (CURRENT_MODAL_IDX < 0) return;
  const d = LAST_DEVICES[CURRENT_MODAL_IDX];
  if (!d) return;
  const el = document.getElementById("mv-dps");
  if (!el) return;
  const _sc = _captureScrolls(el);
  el.innerHTML = _dpsRenderSection(d);
  _restoreScrolls(el, _sc);
}


function dpsEditAdded(dp) {
  // v1.27.1: редактировать NEW.
  if (!DPS_EDIT) return;
  dpsOpenFillModal(dp, "edit");
}

function dpsEditOld(dp) {
  // v1.27.1: редактировать старый (только при _editMode === true).
  if (!DPS_EDIT || !DPS_EDIT._editMode) return;
  dpsOpenFillModal(dp, "edit");
}

function dpsViewDp(dp) {
  // v1.27.1: read-only просмотр старого.
  if (!DPS_EDIT) return;
  dpsOpenFillModal(dp, "view");
}

function dpsMoveOut(dp) {
  if (!DPS_EDIT) return;
  const wasAdded = DPS_EDIT._added.some(a => a.dp === dp);
  if (wasAdded) {
    // v1.27.2: NEW — просто убрать из _added, в _removed НЕ кидать.
    // Иначе DP вернётся в список 2 как «removed», а при повторном
    // добавлении dpsMoveIn() просто удалит его из _removed и
    // вернёт в _discovered — визуально «ничего не происходит».
    DPS_EDIT._added = DPS_EDIT._added.filter(a => a.dp !== dp);
  } else {
    // v1.28.33: при снятии DP сохраняем его info + помечаем
    // source="removed". Раньше _removed был только Set<dp> —
    // после apply _on_device обновлялся, и info взять было
    // негде → Code/Component/Value показывали прочерки.
    const _onDev = DPS_EDIT._on_device[dp];
    const _discIdx = DPS_EDIT._discovered.findIndex(x => x.dp === dp);
    if (_discIdx >= 0) {
      // DP уже в _discovered — запоминаем оригинальный source.
      const _orig = DPS_EDIT._discovered[_discIdx];
      DPS_EDIT._discovered[_discIdx] = Object.assign({}, _orig, {
        source: "removed",
        _prev_source: _orig.source,
      });
    } else if (_onDev) {
      // Только в _on_device — добавляем в _discovered.
      DPS_EDIT._discovered.push({
        dp: dp,
        info: Object.assign({}, _onDev),
        source: "removed",
        value: undefined,
        cloud_meta: null,
      });
    }
    DPS_EDIT._removed.add(dp);
    delete DPS_EDIT._modified[dp];
  }
  dpsRenderSection();
}

// v1.28.70: источник, которому можно доверять (реальное сопоставление DP).
function _isTrustedSource(src) {
  const s = String(src || "").replace("+cache", "");
  return s === "cloud" || s === "tuya_local" || s === "similar";
}

function dpsMoveIn(dp) {
  if (!DPS_EDIT) return;
  // Если DP был в removed — возвращаем.
  if (DPS_EDIT._removed.has(dp)) {
    DPS_EDIT._removed.delete(dp);
    // v1.28.33: восстанавливаем прежний source из _prev_source.
    const _discIdx = DPS_EDIT._discovered.findIndex(x => x.dp === dp);
    if (_discIdx >= 0) {
      const _d = DPS_EDIT._discovered[_discIdx];
      if (_d.source === "removed") {
        DPS_EDIT._discovered[_discIdx] = Object.assign({}, _d, {
          source: _d._prev_source || "cloud",
        });
        delete DPS_EDIT._discovered[_discIdx]._prev_source;
      }
    }
    // v1.28.31: если DP нет в _on_device (был удалён apply'ем),
    // кладём его в _added, иначе dpsBuildFinalMap его не соберёт.
    if (!DPS_EDIT._on_device[dp]) {
      const _disc = DPS_EDIT._discovered.find(x => x.dp === dp);
      const _info = (_disc && _disc.info) || {};
      if (!DPS_EDIT._added.some(a => a.dp === dp)) {
        // v1.28.34: не тащим внутренние `_`-поля (_dps_source,
        // _from_cache, _name_source, _cloud_type) в devices_config.json.
        const _clean = {};
        for (const [k, v] of Object.entries(_info)) {
          if (!k.startsWith("_")) _clean[k] = v;
        }
        DPS_EDIT._added.push({ dp, info: _clean, source: (_disc && _disc.source) || "" });
      }
    }
    dpsRenderSection();
    return;
  }
  const disc = DPS_EDIT._discovered.find(x => x.dp === dp);
  if (!disc) return;

  // v1.27.5: мусорные DP — подтверждение с предупреждением.
  const _info = disc.info || {};
  const _code = _info.code || _info.name || "";
  if (JUNK_DP_CODES.has(_code)) {
    dpsJunkConfirm(dp, _code);
    return;
  }

  // v1.28.70: надёжный источник (cloud/tuya-local/similar) — добавляем сразу,
  // без модалки: поля уже валидны. Модалка «Добавить» — только для
  // ненадёжных (cache/эвристика/unknown), где нужна проверка.
  if (_isTrustedSource(disc.source)) {
    const _clean = {};
    for (const [k, v] of Object.entries(disc.info || {})) {
      if (!k.startsWith("_")) _clean[k] = v;
    }
    if (!DPS_EDIT._added.some(a => a.dp === dp)) {
      DPS_EDIT._added.push({ dp, info: _clean, source: disc.source });
    }
    dpsRenderSection();
    return;
  }
  dpsOpenFillModal(dp, "add");
}

// v1.28.33.fixup3: white-list sensor-codes (синхронно с Python).
const _SENSOR_CODES_FRONT = new Set([
  "va_temperature", "va_humidity", "temp_current", "humidity",
  "temp_current_f", "upper_temp", "upper_temp_f",
  "battery_percentage", "battery_state", "battery_value", "va_battery",
  "cur_voltage", "cur_current", "cur_power",
  "output_power", "output_voltage", "output_current",
  "leakage_current", "supply_frequency", "power_factor",
  "add_ele", "total_forward_energy", "forward_energy_total",
  "reverse_energy_total", "electric_total",
  "balance_energy", "charge_energy",
  "signal_strength", "illuminance_value", "illuminance",
]);
function _isSensorCodeFront(code) {
  if (!code) return false;
  const c = String(code).toLowerCase();
  if (_SENSOR_CODES_FRONT.has(c)) return true;
  if (c.endsWith("_set") || c.endsWith("_sensitivity")) return false;
  if (c.startsWith("va_") || c.startsWith("cur_")) return true;
  return false;
}
// v1.27.1: маппинг Cloud type → component.
// v1.28.33.fixup3: Integer → number ТОЛЬКО для не-датчиков.
// v1.28.34: единое правило component (вариант C) — синхронно с Python
// cloud_dp_component(). Устраняет расхождение Component между экранами.
const _BOOL_BINARY_CODES_FRONT = {
  doorcontact_state: "door", pir: "motion", watersensor_state: "moisture",
  fault: "problem", problema: "problem",
};
const _ENUM_BINARY_CODES_FRONT = { watersensor_state: "moisture" };
const _ENUM_SENSOR_CODES_FRONT = new Set(["battery_state"]);
const _CLIMATE_PRESET_CODES_FRONT = new Set(["mode", "preset_mode"]);

function _dpsCompFromCloudType(t, code, writable, devType) {
  if (t === "Boolean") {
    return (code in _BOOL_BINARY_CODES_FRONT) ? "binary_sensor" : "switch";
  }
  if (t === "Enum") {
    if (devType === "climate" && _CLIMATE_PRESET_CODES_FRONT.has(code)) return "preset";
    if (_ENUM_SENSOR_CODES_FRONT.has(code)) return "sensor";
    if (code in _ENUM_BINARY_CODES_FRONT) return "binary_sensor";
    return "select";
  }
  if (t === "Integer") {
    if (devType === "light" || devType === "climate") return "sensor";
    if (writable && !_isSensorCodeFront(code)) return "number";
    return "sensor";
  }
  return "sensor";
}

// v1.27.10: bridge-forced names. Bridge жёстко назначает name
// для некоторых component (требование HA MQTT). UI показывает
// фактический name, но запрещает редактирование (🔒).
const _BRIDGE_FORCED_NAMES = {
  "preset_mode": { cloud: "mode",              comp: "preset",        why: "HA climate preset_mode" },
  "phase_a":     { cloud: "phase_a",           comp: "phase_a",       why: "HA phase_a (V/I/P split)" },
  "backlight":   { cloud: "switch_backlight",  comp: "switch",        why: "короткое имя для HA switch" },
  "prepayment":  { cloud: "switch_prepayment", comp: "switch",        why: "короткое имя для HA switch" },
  "door":        { cloud: "doorcontact_state", comp: "binary_sensor", why: "HA device_class=door" },
  "motion":      { cloud: "pir",               comp: "binary_sensor", why: "HA device_class=motion" },
  "moisture":    { cloud: "watersensor_state", comp: "binary_sensor", why: "HA device_class=moisture" },
};

// true, если name — bridge-forced для данного component.
function _isBridgeForcedName(name, comp) {
  const info = _BRIDGE_FORCED_NAMES[name];
  if (!info) return false;
  if (comp && info.comp !== comp) return false;
  return true;
}

// v1.28.68: имена, зарезервированные bridge под системные сущности
// (RESERVED_DP_NAMES в main.py) — такие name bridge отклонит.
const _RESERVED_DP_NAMES = new Set([
  "battery_alert", "battery_last_seen",
  "output_voltage", "output_current", "output_power",
]);

// v1.28.68: ключ bridge-forced по имени ИЛИ облачному code (null — не forced).
function _bridgeForcedKey(name, code) {
  if (name && _BRIDGE_FORCED_NAMES[name]) return name;
  if (code) {
    for (const k of Object.keys(_BRIDGE_FORCED_NAMES)) {
      if (_BRIDGE_FORCED_NAMES[k].cloud === code) return k;
    }
  }
  return null;
}

// v1.28.42: bridge-forced по имени ИЛИ по облачному code (единообразно).
// watersensor_state → moisture и т.п. Иначе «🔒» показывался не всегда.
function _bridgeForcedInfo(name, code) {
  const k = _bridgeForcedKey(name, code);
  return k ? _BRIDGE_FORCED_NAMES[k] : null;
}

// Проверка code (live). Возвращает {level: "ok"|"warn"|"err", msg}.
//   err  — блокирующая (regex, длина, дубликат, bridge-forced).
//   warn — облачный code отличается (можно продолжить).
function _validateDpCode(code, component, currentDp) {
  const c = String(code || "").trim();
  if (!c) return { level: "err", msg: "Code обязателен" };
  if (c.length > 100) return { level: "err", msg: "Code слишком длинный (макс 100)" };
  // v1.28.27: regex синхронизирован с bridge _is_valid_name —
  // bridge допускает A-Z, WebUI раньше блокировал.
  // v1.28.30: regex синхронизирован с bridge _is_valid_name —
  // bridge разрешает [A-Za-z0-9_-] в любой позиции, включая первую.
  // Раньше WebUI требовал букву/_ в начале и отклонял валидные
  // для bridge имена (например начинающиеся с цифры).
  if (!/^[A-Za-z0-9_-]+$/.test(c)) {
    return { level: "err", msg: "Code: только A-Z, a-z, 0-9, _, -" };
  }
  // bridge-forced name? — блок, если это НЕ текущий DP
  if (_BRIDGE_FORCED_NAMES[c]) {
    return { level: "err", msg: "Имя '" + c + "' зарезервировано bridge (HA MQTT). Используйте другое." };
  }
  // v1.28.68: имена системных сущностей Discovery (RESERVED_DP_NAMES).
  if (_RESERVED_DP_NAMES.has(c)) {
    return { level: "err", msg: "Имя '" + c + "' зарезервировано bridge (системная сущность). Используйте другое." };
  }
  // v1.27.11: light — Code из LIGHT_DP_NAMES_FRONT.
  if (_devTypeSafe() === "light" && !_BRIDGE_FORCED_NAMES[c]) {
    if (!_LIGHT_DP_NAMES_FRONT.includes(c)) {
      return { level: "err", msg: "Для light «Code» должен быть одним из: " + JSON.stringify(_LIGHT_DP_NAMES_FRONT) };
    }
  }
  // дубликат (component, name) — исключая currentDp
  if (DPS_EDIT) {
    const key = (component || "auto") + "|" + c;
    const check = (dp, info) => {
      if (dp === currentDp) return false;
      const _c = (info && info.component) || "auto";
      const _n = (info && (info.name || info.code)) || "";
      return (_c + "|" + _n) === key;
    };
    for (const [dp, info] of Object.entries(DPS_EDIT._on_device || {})) {
      if (check(dp, info)) return { level: "err", msg: "Дубликат: (" + (component||"auto") + ", " + c + ") уже у DP " + dp };
    }
    for (const a of (DPS_EDIT._added || [])) {
      if (check(a.dp, a.info)) return { level: "err", msg: "Дубликат: (" + (component||"auto") + ", " + c + ") уже у DP " + a.dp };
    }
    for (const [dp, info] of Object.entries(DPS_EDIT._modified || {})) {
      if (check(dp, info)) return { level: "err", msg: "Дубликат: (" + (component||"auto") + ", " + c + ") уже у DP " + dp };
    }
  }
  return { level: "ok", msg: "" };
}

// Warning: code не совпадает с облачным code для этого DP.
function _cloudCodeWarning(code, dp) {
  if (!DPS_EDIT || !dp) return null;
  const disc = (DPS_EDIT._discovered || []).find(x => x.dp === dp);
  if (!disc || !disc.cloud_meta) return null;
  const cm = disc.cloud_meta;
  const cloudCode = (cm.code || "").trim();
  if (!cloudCode) return null;
  if (cloudCode === code) return null;
  return "В облаке: '" + cloudCode + "'. Продолжить на свой риск?";
}

// v1.27.5: подтверждение добавления мусорного DP.
async function dpsJunkConfirm(dp, code) {
  const ok = await uiConfirm(
    "Добавить мусорный DP?",
    `DP ${dp} (${code}) — сервисный, в HA обычно бесполезен.\n\n` +
    `Он не создаёт полезной сущности: сброс, калибровка, ID и т.п.\n\n` +
    `Всё равно добавить?`,
    { danger: true, okText: "Добавить" }
  );
  if (!ok) return;
  // Пользователь подтвердил — добавляем как обычно.
  const disc = DPS_EDIT._discovered.find(x => x.dp === dp);
  if (!disc) return;
  const info = {};
  if (disc.info) {
    const src = disc.info;
    if (src.component) info.component = src.component;
    const _nm = String(src.name || "");
    if (/^[A-Za-z0-9_-]+$/.test(_nm)) info.name = _nm;
    else if (src.code) info.name = String(src.code);
    for (const k of ["device_class", "unit", "scale", "state_class", "min", "max", "step"]) {
      if (k in src) info[k] = src[k];
    }
    if (disc.cloud_meta && disc.cloud_meta.values && Array.isArray(disc.cloud_meta.values.range)) {
      info.options = disc.cloud_meta.values.range;
    }
  }
  if (!info.name && !info.code) {
    dpsOpenFillModal(dp);
    return;
  }
  if (!info.name) info.name = info.code;
  DPS_EDIT._added.push({ dp, info, source: (disc && disc.source) || "" });
  dpsRenderSection();
}

function dpsToggleEditMode() {
  // v1.27.1: тумблер «Режим редактирования». Заблокирован, если есть
  // несохранённые изменения — нельзя одновременно добавлять новое
  // и править старое.
  if (!DPS_EDIT) return;
  if (dpsIsDirty()) {
    uiAlert(
      "Нельзя переключить режим",
      "Сначала сохраните или отмените изменения в DP.",
      "warning"
    );
    return;
  }
  DPS_EDIT._editMode = !DPS_EDIT._editMode;
  dpsRenderSection();
}

function dpsRevertModified(dp) {
  // v1.27.1: откатить правку старого DP.
  if (!DPS_EDIT) return;
  delete DPS_EDIT._modified[dp];
  dpsRenderSection();
}

function dpsCancel() {
  if (!DPS_EDIT) return;
  DPS_EDIT._removed.clear();
  DPS_EDIT._added = [];
  DPS_EDIT._modified = {};
  dpsRenderSection();
}

// v1.28.55: dps_map без внутренних `_`-полей (_meta_guess, _dps_source, ...).
function _cleanDpInfo(info) {
  const out = {};
  for (const [k, v] of Object.entries(info || {})) {
    if (!k.startsWith("_")) out[k] = v;
  }
  return out;
}

function dpsBuildFinalMap() {
  if (!DPS_EDIT) return {};
  const out = {};
  for (const [dp, info] of Object.entries(DPS_EDIT._on_device)) {
    if (DPS_EDIT._removed.has(dp)) continue;
    out[dp] = _cleanDpInfo(DPS_EDIT._modified[dp]
      ? DPS_EDIT._modified[dp]
      : info);
  }
  for (const a of DPS_EDIT._added) {
    out[a.dp] = _cleanDpInfo(a.info);
  }
  return out;
}

function dpsOpenPreview() {
  if (!DPS_EDIT) return;
  // v1.28.33.fixup2: если ничего не изменилось — не открываем
  // превью (защита от случайного клика «Сохранить»).
  if (!dpsIsDirty()) {
    uiAlert("Нечего сохранять", "Изменений в DP нет.", "info");
    return;
  }
  const finalMap = dpsBuildFinalMap();
  const oldCount = Object.keys(DPS_EDIT._on_device).length;
  const newCount = Object.keys(finalMap).length;
  const removed = Array.from(DPS_EDIT._removed);
  const added = DPS_EDIT._added.map(a => a.dp);
  const modified = Object.keys(DPS_EDIT._modified);

  // v1.27.1: пре-валидация.
  const issues = _dpsPreValidate(finalMap);

  let html = `<p class="muted" style="margin-top:0;">Проверьте изменения перед применением.</p>`;
  if (removed.length > 0) {
    html += `<div class="dps-list-title"><span>➖ Удалено из конфига</span></div>`;
    html += `<ul style="margin:4px 0 12px 20px; padding:0;">`;
    for (const dp of removed) {
      const info = DPS_EDIT._on_device[dp] || {};
      html += `<li><code>DP ${escapeHtml(dp)}</code> — ${escapeHtml(info.code || info.name || "?")}</li>`;
    }
    html += `</ul>`;
  }
  if (added.length > 0) {
    html += `<div class="dps-list-title"><span>➕ Добавлено в конфиг</span></div>`;
    html += `<ul style="margin:4px 0 12px 20px; padding:0;">`;
    for (const dp of added) {
      const a = DPS_EDIT._added.find(x => x.dp === dp);
      html += `<li><code>DP ${escapeHtml(dp)}</code> — ${escapeHtml(a.info.name || a.info.code || "?")} ${componentBadge(a.info.component || "")}</li>`;
    }
    html += `</ul>`;
  }
  if (modified.length > 0) {
    html += `<div class="dps-list-title"><span>✏️ Изменено</span></div>`;
    html += `<ul style="margin:4px 0 12px 20px; padding:0;">`;
    for (const dp of modified) {
      const info = DPS_EDIT._modified[dp] || {};
      html += `<li><code>DP ${escapeHtml(dp)}</code> — ${escapeHtml(info.code || info.name || "?")}</li>`;
    }
    html += `</ul>`;
  }

  if (issues.length > 0) {
    html += `<div class="dps-list-title" style="color:var(--red);">`;
    html += `<span>❌ Проблемы (${issues.length})</span></div>`;
    html += `<ul style="margin:4px 0 12px 20px; padding:0; color:var(--red);">`;
    for (const iss of issues) {
      html += `<li><code>DP ${escapeHtml(iss.dp)}</code> — ${escapeHtml(iss.msg)}</li>`;
    }
    html += `</ul>`;
    html += `<p class="muted" style="font-size:12px;">Исправьте их перед сохранением.</p>`;
  }

  html += `<p class="muted" style="margin-top:16px; font-size:12px;">`;
  html += `Итого: <strong>${oldCount} → ${newCount} DP</strong>. `;
  html += `Bridge перезапишет <code>devices_config.json</code>, HA перезагрузит сущности.`;
  html += `</p>`;

  document.getElementById("dps-preview-body").innerHTML = html;
  const applyBtn = document.getElementById("dps-preview-apply");
  if (applyBtn) applyBtn.disabled = issues.length > 0;
  document.getElementById("dps-preview-overlay").classList.add("open");
}

// v1.28.27: белый список components (синхронно с bridge COMPONENTS_ALLOWED).
const _COMPS_ALLOWED = new Set([
  "switch","sensor","binary_sensor","select","number",
  "preset","light","climate","button","time","lock","phase_a",
]);

// v1.27.1: пре-валидация финальной карты (зеркало bridge _validate_dps_map).
function _dpsPreValidate(finalMap) {
  const issues = [];
  const seen = new Set();
  // v1.28.32: bridge не принимает пустой dps_map (_validate_dps_map).
  // Ловим это ДО отправки, чтобы пользователь увидел проблему
  // в превью apply, а не получил ошибку от bridge.
  if (Object.keys(finalMap).length === 0) {
    issues.push({dp: "—", msg: "Нельзя удалить все DP: bridge отклонит пустой dps_map. Оставьте хотя бы один."});
    return issues;
  }
  for (const [dp, info] of Object.entries(finalMap)) {
    if (!info || typeof info !== "object") { issues.push({dp, msg: "info не dict"}); continue; }
    const comp = info.component || "";
    const name = info.name || "";
    if (!name) { issues.push({dp, msg: "name обязателен"}); continue; }
    // v1.28.27: component из белого списка (bridge отклонит иначе).
    if (comp && !_COMPS_ALLOWED.has(comp)) {
      issues.push({dp, msg: "component '" + comp + "' не из белого списка"});
    }
    // v1.28.30: синхронизировано с bridge и с _validateDpCode.
    if (!/^[A-Za-z0-9_-]+$/.test(name)) {
      issues.push({dp, msg: "name: только A-Z, a-z, 0-9, _, -"});
    }
    if (name.length > 100) {
      issues.push({dp, msg: "name: макс 100 символов"});
    }
    // v1.27.10: device_class / state_class из белых списков
    if (info.device_class && !_DC_ALLOWED.includes(info.device_class)) {
      issues.push({dp, msg: "device_class '" + info.device_class + "' не из белого списка"});
    }
    if (info.state_class && !_SC_ALLOWED.includes(info.state_class)) {
      issues.push({dp, msg: "state_class '" + info.state_class + "' не из белого списка"});
    }
    if (info.scale !== undefined) {
      if (typeof info.scale !== "number" || info.scale < 0 || !Number.isInteger(info.scale)) {
        issues.push({dp, msg: "scale: целое >= 0"});
      }
    }
    // v1.27.10: light — name только из LIGHT_DP_NAMES_FRONT
    if (_devTypeSafe() === "light" && !_isBridgeForcedName(name, comp)) {
      if (!_LIGHT_DP_NAMES_FRONT.includes(name)) {
        issues.push({dp, msg: "light: name должен быть из " + JSON.stringify(_LIGHT_DP_NAMES_FRONT)});
      }
    }
    // v1.27.11: options нужен только для select (preset — нет).
    if (comp === "select") {
      if (!Array.isArray(info.options) || info.options.length === 0) {
        issues.push({dp, msg: "select: нужен непустой options"});
      }
    }
    if (comp === "number") {
      if (info.min === undefined || info.max === undefined) {
        issues.push({dp, msg: "number: нужны min и max"});
      } else if (info.min >= info.max) {
        issues.push({dp, msg: "number: min < max"});
      } else if (info.step !== undefined && info.step <= 0) {
        issues.push({dp, msg: "number: step > 0"});
      }
    }
    if (comp === "phase_a" && name !== "phase_a") {
      issues.push({dp, msg: "phase_a: name должен быть 'phase_a'"});
    }
    if (comp === "preset" && name !== "preset_mode") {
      issues.push({dp, msg: "preset: name должен быть 'preset_mode'"});
    }
    const key = (comp || "auto") + "|" + name;
    if (seen.has(key)) {
      issues.push({dp, msg: "дубликат (" + (comp || "auto") + ", " + name + ")"});
    }
    seen.add(key);
  }
  return issues;
}

function closeDpsPreview(evt) {
  if (evt && evt.target && evt.target.id !== "dps-preview-overlay") return;
  document.getElementById("dps-preview-overlay").classList.remove("open");
  // v1.27.3: сброс кнопки при закрытии — страховка от залипания
  // состояния «применение…» при отмене/Esc/overlay-клике.
  const _btn = document.getElementById("dps-preview-apply");
  if (_btn) { _btn.disabled = false; _btn.innerHTML = "Применить"; }
}

async function dpsConfirmApply() {
  if (!DPS_EDIT) return;
  const finalMap = dpsBuildFinalMap();
  const issues = _dpsPreValidate(finalMap);
  if (issues.length > 0) {
    await uiAlert(
      "Есть ошибки в DP",
      issues.map(x => "• DP " + x.dp + ": " + x.msg).join("\n"),
      "error"
    );
    return;
  }
  const btn = document.getElementById("dps-preview-apply");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> применение…';

  const devName = DPS_EDIT._name;

  try {
    const r = await fetch(`/api/device/${encodeURIComponent(devName)}/dps_map/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dps_map: finalMap }),
    });
    const data = await r.json();
    if (data.ok) {
      // v1.27.3: сбрасываем кнопку ДО closeDpsPreview — иначе она
      // остаётся в «применение…» (disabled + спиннер) и пользователь
      // видит это при следующем открытии превью.
      btn.disabled = false;
      btn.innerHTML = "Применить";
      closeDpsPreview();
      if (data.warnings && data.warnings.length > 0) {
        await uiAlert("Сохранено с предупреждениями",
                      data.warnings.map(x => "• " + x).join("\n"),
                      "warning");
      }
      // v1.27.3: перезагружаем статус, но переинициализируем staging
      // ТОЛЬКО если модалка всё ещё открыта на том же устройстве.
      // Иначе: fetchStatus async, пользователь мог уже открыть другое
      // устройство — и мы бы перезаписали его staging.
      await fetchStatus();
      // v1.28.33.fixup2: после успешного apply сбрасываем staging
      // полностью. Пользователь нажал «Сохранить» → изменения
      // зафиксированы в bridge. Ожидание: «Сохранить = применить
      // и забыть». Если нужен откат — переоткрыть модалку или ➕.
      if (CURRENT_MODAL_NAME === devName
          && CURRENT_MODAL_IDX >= 0
          && CURRENT_MODAL_IDX < LAST_DEVICES.length) {
        dpsInitEdit(LAST_DEVICES[CURRENT_MODAL_IDX], true);
        dpsRenderSection();
      }
    } else {
      btn.disabled = false;
      btn.textContent = "Применить";
      uiAlert("Ошибка", data.error || "неизвестная ошибка", "error");
      // v1.28.32: перерисовать секцию DP — staging не сброшен,
      // но UI мог потерять актуальное состояние.
      if (CURRENT_MODAL_NAME === devName) {
        dpsRenderSection();
      }
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Применить";
    uiAlert("Ошибка сети", e.message, "error");
  }
}

// --- Модалка заполнения DP ---
// v1.27.1: всегда открывается для [➕], с предзаполнением
// известных данных из Cloud / cache.
// v1.27.2: _dpsFillDp / _dpsFillMode / _dpsFillDirty объявлены в блоке выше
// (в P8). Здесь — только резолверы.

function _dpsFillSuggestFromValue(val) {
  if (typeof val === "boolean") return "switch";
  if (typeof val === "number") return "number";
  if (typeof val === "string") {
    if (val.length > 0 && val.length < 30 && !val.startsWith("{") && !val.startsWith("[")) {
      return "select";
    }
    return "sensor";
  }
  if (typeof val === "object" && val !== null) return "sensor";
  return "sensor";
}

// v1.28.67: эвристика component по значению + code (когда Cloud не дал type).
// Порядок: значение → известные бинарные/sensor-коды → enum → число.
function _dpsGuessComponent(code, val) {
  const c = String(code || "").toLowerCase();
  // boolean → switch, либо binary_sensor для известных кодов (fault/pir/door/...)
  if (val === true || val === false) {
    return (c in _BOOL_BINARY_CODES_FRONT) ? "binary_sensor" : "switch";
  }
  if (c in _BOOL_BINARY_CODES_FRONT || c in _ENUM_BINARY_CODES_FRONT) return "binary_sensor";
  if (_isSensorCodeFront(c) || _ENUM_SENSOR_CODES_FRONT.has(c)) return "sensor";
  if (typeof val === "string" && val.length > 0 && val.length < 30
      && !val.startsWith("{") && !val.startsWith("[")) {
    // строка-число → сенсор/число, иначе — перечисление
    if (/^-?\d+(\.\d+)?$/.test(val)) {
      return _isNumberCode(c) ? "number" : "sensor";
    }
    if (val === "true" || val === "false") return "switch";
    return "select";
  }
  if (typeof val === "object" && val !== null) return "sensor";
  if (typeof val === "number") return _isNumberCode(c) ? "number" : "sensor";
  return "sensor";
}

// v1.28.67: код похож на записываемое числовое поле (уставка/цель/коррекция).
function _isNumberCode(code) {
  const c = String(code || "").toLowerCase();
  return c.endsWith("_set") || c.includes("setpoint") || c.includes("target")
      || c.includes("_correction") || c.includes("sensitivity")
      || c.startsWith("temp_set") || c === "bright_value" || c === "temp_value";
}


// v1.27.1: список device_class из DEVICE_CLASSES_ALLOWED (bridge 1.8.5).
// Используется для datalist — подсказки, но не жёсткого ограничения.
const _DC_ALLOWED = [
  // sensor
  "temperature","humidity","battery","energy","power","current",
  "voltage","frequency","power_factor","illuminance","pressure",
  "signal_strength","timestamp","duration",
  // binary_sensor
  "problem","door","motion","moisture","smoke","gas","light",
  "opening","window","garage_door","lock","presence","running",
  "plug","sound","vibration","update","connectivity","tamper",
  "heat","cold","moving","occupancy","safety",
];
const _SC_ALLOWED = ["measurement","total","total_increasing"];
const _LIGHT_DP_NAMES_FRONT = [
  "switch_led","bright_value","temp_value",
  "colour_data","colour_data_v2","work_mode",
];

// v1.27.1: эвристика device_class/unit по code (когда Cloud не дал).
function _dpsHeuristicMeta(code, component) {
  // v1.28.52: расширенная эвристика device_class/unit/state_class по code.
  // device_class берём только из белого списка (_DC_ALLOWED), иначе bridge/превью
  // отклонит. Battery — с точечными исключениями (no_battery/battery_off/...).
  const out = {};
  if (!code) return out;
  const c = String(code).toLowerCase();
  const comp = component || "";
  if (comp === "sensor" || comp === "number" || !comp) {
    if (c.includes("temp")) {
      out.device_class = "temperature";
      out.unit = (c.endsWith("_f") || c.includes("temp_f")) ? "°F" : "°C";
      out.state_class = "measurement";
    } else if (c.includes("humidity") || c === "va_humidity") {
      out.device_class = "humidity"; out.unit = "%"; out.state_class = "measurement";
    } else if (c.startsWith("battery") || c === "va_battery"
               || (c.includes("battery") && !/no_battery|battery_off|battery_mode|battery_state/.test(c))) {
      out.device_class = "battery"; out.unit = "%"; out.state_class = "measurement";
    } else if (c === "power_factor") {
      out.device_class = "power_factor"; out.state_class = "measurement";
    } else if (c.includes("energy") || c === "add_ele" || c === "cur_consumption"
               || c === "balance_energy" || c === "charge_energy"
               || c === "total_forward_energy") {
      out.device_class = "energy"; out.unit = "kWh";
      out.state_class = (c.includes("total") || c.includes("forward") || c.includes("add"))
        ? "total_increasing" : "total";
    } else if (c.includes("voltage")) {
      out.device_class = "voltage"; out.unit = "V"; out.state_class = "measurement";
    } else if (c.includes("current")) {
      out.device_class = "current";
      out.unit = c.includes("leakage") ? "mA" : (c.includes("output") ? "A" : "mA");
      out.state_class = "measurement";
    } else if (c.includes("power")) {
      out.device_class = "power"; out.unit = c.includes("output") ? "kW" : "W";
      out.state_class = "measurement";
    } else if (c.includes("frequency")) {
      out.device_class = "frequency"; out.unit = "Hz"; out.state_class = "measurement";
    } else if (c.includes("pressure")) {
      out.device_class = "pressure"; out.state_class = "measurement";
    } else if (c.includes("illuminance") || c.includes("lux")) {
      out.device_class = "illuminance"; out.unit = "lx"; out.state_class = "measurement";
    } else if (c.includes("signal")) {
      out.device_class = "signal_strength"; out.unit = "dBm"; out.state_class = "measurement";
    } else if (c.includes("co2")) {
      out.unit = "ppm"; out.state_class = "measurement";
    } else if (c.includes("pm2") || c.includes("pm10")) {
      out.unit = "µg/m³"; out.state_class = "measurement";
    } else if (c.includes("soil")) {
      out.device_class = "moisture"; out.unit = "%"; out.state_class = "measurement";
    }
  }
  if (comp === "binary_sensor" || !comp) {
    if (c === "doorcontact_state") out.device_class = out.device_class || "door";
    else if (c === "pir" || c.includes("motion")) out.device_class = out.device_class || "motion";
    else if (c === "watersensor_state" || c.includes("water") || c.includes("leak")) {
      out.device_class = out.device_class || "moisture";
    } else if (c === "fault" || c === "problema") out.device_class = out.device_class || "problem";
  }
  return out;
}

// v1.27.1: нужны ли доп. поля для component → авто-раскрытие <details>.
function _dpsComponentNeedsDetails(component) {
  return component === "select" || component === "preset"
      || component === "number" || component === "sensor"
      || component === "binary_sensor" || component === "light";
}

// v1.27.1: у какого-то component жёстко фиксирован name.
function _dpsComponentForcedName(component) {
  if (component === "preset") return "preset_mode";
  if (component === "phase_a") return "phase_a";
  return null;
}

let _dpsFillDp = null;
let _dpsFillMode = "add";   // "add" | "edit" | "view"
let _dpsFillDirty = false;
let _dpsFillSource = "";    // v1.28.66: выбранный источник ("" = не выбран)
let _dpsFillOrigSnapshot = null;  // v1.28.75: снимок DOM-полей формы (объект)
let _dpsFillValidateTimer = null;

// v1.28.66: клик по источнику — применить его поля; повторный клик — снять
// (по умолчанию не выбрано ничего, поля показываются «как есть»).
function dpsFillToggleSource(src) {
  _dpsFillSource = (_dpsFillSource === src) ? "" : src;
  if (_dpsFillDp === null) return;
  dpsOpenFillModal(_dpsFillDp, _dpsFillMode, true);
}

// v1.28.66: короткий ярлык источника для кнопок.
function _srcLabelRu(s) {
  const m = { cloud: "☁ Cloud", tuya_local: "📚 tuya-local", similar: "🔗 similar",
              heuristic: "⚠️ эвристика" };
  return m[s] || s;
}

// v1.28.69: честная причина сопоставления (одна строка, без дублирования).
function _srcReasonText(src, entry) {
  const s = String(src || "").replace("+cache", "");
  const r = (entry && entry._dps_source_reason) || "";
  if (s === "cloud")      return r ? "Tuya Cloud (" + r + ")" : "Tuya Cloud";
  if (s === "tuya_local") return "локальная база tuya-local";
  if (s === "similar")    return "такое же устройство (тот же product_id)";
  if (s === "cache")      return "нет сопоставления — только значение из кэша bridge";
  if (s === "heuristic")  return "компонент угадан по имени/значению";
  return s;
}

function _dpsFillMarkDirty() {
  _dpsFillDirty = true;
  const errEl = document.getElementById("dps-fill-error");
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  // v1.28.25: перезапустить валидацию — иначе submitBtn.disabled
  // остаётся залипшим от старой ошибки в другом поле (unit, scale и т.п.).
  dpsFillUpdateWillWrite();
  if (typeof dpsFillValidateCodeLive === "function") dpsFillValidateCodeLive();
}

// v1.28.75: полный снимок полей формы (включая «Дополнительно») — чтобы
// «Будет обновлено» не врало.
function _dpsFillSnapshot() {
  const body = document.getElementById("dps-fill-body");
  const out = {};
  if (!body) return out;
  body.querySelectorAll("input, select, textarea").forEach(el => {
    if (!el.id || el.id.indexOf("dps-fill-") !== 0) return;
    out[el.id] = (el.type === "checkbox" || el.type === "radio")
      ? (el.checked ? "1" : "0") : String(el.value);
  });
  return out;
}

// v1.28.76: что именно изменилось (id → old/new), для сводки.
function _dpsFillDiff() {
  const cur = _dpsFillSnapshot();
  const orig = _dpsFillOrigSnapshot || {};
  const out = [];
  for (const id of Object.keys(cur)) {
    const a = (orig[id] === undefined) ? "" : String(orig[id]);
    const b = String(cur[id]);
    if (a !== b) out.push({ id, old: a, next: b });
  }
  return out;
}
function _dpsFillFieldLabel(id) {
  const m = {
    "dps-fill-code": "Code",
    "dps-fill-component": "Компонент",
    "dps-fill-device_class": "device_class",
    "dps-fill-state_class": "state_class",
    "dps-fill-unit": "unit",
    "dps-fill-scale": "scale",
    "dps-fill-min": "min",
    "dps-fill-max": "max",
    "dps-fill-step": "step",
    "dps-fill-options": "options",
  };
  return m[id] || id.replace("dps-fill-", "");
}

// v1.28.69: короткая сводка «что уйдёт в конфиг» (component / name / источник).
function dpsFillUpdateWillWrite() {
  const el = document.getElementById("dps-fill-willwrite-body");
  if (!el) return;
  const box = document.getElementById("dps-fill-willwrite");
  const g = id => { const e = document.getElementById(id); return e ? String(e.value).trim() : ""; };
  const comp = g("dps-fill-component");
  const name = _dpsComponentForcedName(comp) || g("dps-fill-code");
  const srcEl = document.getElementById("dps-fill-willsrc");
  const src = (srcEl && srcEl.dataset.src) || "";
  const isEdit = _dpsFillMode === "edit";
  const diff = _dpsFillDiff();

  // v1.28.76: при правке без изменений блок НЕ показываем.
  if (isEdit && diff.length === 0) {
    if (box) box.style.display = "none";
    return;
  }
  if (box) box.style.display = "";

  if (!isEdit) {
    el.innerHTML = `<span class="muted">Будет записано в конфиг:</span> `
      + `<code>${escapeHtml(comp || "—")}</code> / <code>${escapeHtml(name || "—")}</code>`
      + ` · источник: <b>${escapeHtml(src || "как есть")}</b>`;
    return;
  }
  // Правка: перечисляем только реально изменённые поля.
  const lines = diff.map(d => {
    const a = d.old === "" ? '<span class="muted">—</span>' : `<code>${escapeHtml(d.old)}</code>`;
    const b = d.next === "" ? '<span class="muted">—</span>' : `<code>${escapeHtml(d.next)}</code>`;
    return `<div><span class="muted">${escapeHtml(_dpsFillFieldLabel(d.id))}:</span> ${a} → ${b}</div>`;
  });
  el.innerHTML = `<div class="muted">Будет обновлено в конфиге: `
    + `<code>${escapeHtml(comp || "—")}</code> / <code>${escapeHtml(name || "—")}</code>`
    + ` · источник: <b>${escapeHtml(src || "как есть")}</b></div>`
    + lines.join("");
}

let _CLOUD_RAW_MAP = null;   // v1.28.71: tuya_id → cloud-cache device (ленивая загрузка)

// v1.28.71: справочные значения (кэш bridge + Cloud status) по требованию.
async function dpsFillRenderRef(el) {
  if (!el || !el.open) return;
  const body = el.querySelector(".dps-fill-ref-body");
  if (!body || body.dataset.loaded === "1") return;
  const dp = String(_dpsFillDp);
  const rows = [];
  const _bv = (DPS_EDIT && DPS_EDIT._cache && DPS_EDIT._cache[dp] !== undefined)
    ? DPS_EDIT._cache[dp] : undefined;
  rows.push({ label: "Кэш bridge (DP " + dp + ")", value: _bv });
  body.innerHTML = '<span class="spin"></span> …';
  try {
    const tid = DPS_EDIT && DPS_EDIT._tuyaId;
    let dev = null;
    if (tid) {
      if (!_CLOUD_RAW_MAP) {
        const r = await fetch("/api/cloud/cache");
        const data = await r.json();
        _CLOUD_RAW_MAP = {};
        for (const x of (data.devices || [])) if (x && x.id) _CLOUD_RAW_MAP[x.id] = x;
      }
      dev = _CLOUD_RAW_MAP[tid] || null;
    }
    if (!dev) {
      rows.push({ label: "Cloud status", note: "нет данных Cloud (кэш пуст)" });
    } else {
      const ent = (dev.mapping || {})[dp] || null;
      const code = ent && (ent.code || ent.name);
      const cs = dev.cloud_status || {};
      if (code) {
        rows.push({
          label: "Cloud status (" + code + ")",
          value: Object.prototype.hasOwnProperty.call(cs, code) ? cs[code] : undefined,
        });
      } else {
        rows.push({ label: "Cloud status", note: "для этого DP нет сопоставления" });
      }
    }
  } catch (e) {
    rows.push({ label: "Cloud status", note: "ошибка чтения кэша" });
  }
  body.innerHTML = rows.map(r => {
    const v = (r.value === undefined || r.value === null)
      ? '<span class="muted">—</span>'
      : `<code>${escapeHtml(displayValue(r.value))}</code>`;
    const note = r.note ? ` <span class="muted">(${escapeHtml(r.note)})</span>` : "";
    return `<div class="dps-fill-ref-row"><span class="dps-fill-ref-label">`
         + `${escapeHtml(r.label)}</span><span>${v}${note}</span></div>`;
  }).join("");
  body.dataset.loaded = "1";
}

function dpsOpenFillModal(dp, mode, keepDirty) {
  // v1.27.1: mode ∈ {"add", "edit", "view"}.
  if (!DPS_EDIT) return;
  _dpsFillDp = dp;
  _dpsFillMode = mode || "add";
  if (!keepDirty) { _dpsFillDirty = false; _dpsFillSource = ""; }

  // Приоритет источников: _added → _modified → _on_device → _discovered.
  // Мерж с cloudMeta — только для ОТСУТСТВУЮЩИХ полей.
  const addedRec = DPS_EDIT._added.find(x => x.dp === dp);
  const modifiedRec = DPS_EDIT._modified[dp];
  const onDeviceRec = DPS_EDIT._on_device[dp];
  const discRec = DPS_EDIT._discovered.find(x => x.dp === dp);
  // v1.28.67: значение берём из cache (работает и для DP «на устройстве»,
  // которых нет в _discovered). Раньше для них всегда было «—».
  const val = (DPS_EDIT._cache && DPS_EDIT._cache[dp] !== undefined)
    ? DPS_EDIT._cache[dp]
    : (discRec ? discRec.value : undefined);

  let info = {};
  if (addedRec && addedRec.info) info = Object.assign({}, addedRec.info);
  else if (modifiedRec) info = Object.assign({}, modifiedRec);
  else if (onDeviceRec) info = Object.assign({}, onDeviceRec);
  else if (discRec && discRec.info) info = Object.assign({}, discRec.info);

  // v1.28.68: bridge-forced определяем ДО выбора источника и по стабильной
  // «личности» DP (config-name / cloud-code), а не по пересчитанному component.
  // Иначе смена источника «разблокировала» зарезервированные DP.
  const _rawName = (addedRec && addedRec.info && (addedRec.info.name || addedRec.info.code))
    || (modifiedRec && (modifiedRec.name || modifiedRec.code))
    || (onDeviceRec && (onDeviceRec.name || onDeviceRec.code))
    || (discRec && discRec.info && (discRec.info.name || discRec.info.code))
    || (info && (info.name || info.code)) || "";
  const _rawCloudCode = (discRec && discRec.cloud_meta && discRec.cloud_meta.code)
    || (info && info.code) || "";
  const _bfKey = _bridgeForcedKey(_rawName, _rawCloudCode)
    || _bridgeForcedKey(_rawCloudCode, _rawName);   // cloud-code ∈ .cloud (mode→preset_mode)
  const _bridgeForced = !!_bfKey;
  const _bfInfo = _bridgeForced ? _BRIDGE_FORCED_NAMES[_bfKey] : null;
  if (_bridgeForced) _dpsFillSource = "";     // RO: источник не применяем

  // v1.28.58: кандидаты источников — выбор источника в форме.
  // v1.28.59: всегда добавляем «эвристику», чтобы выбор был даже если
  // Cloud/tuya-local дают только один вариант.
  const _candBase = Object.assign({},
    (info && info._dps_candidates)
    || (discRec && discRec.info && discRec.info._dps_candidates) || {});
  // v1.28.61: добавляем кандидата текущего источника, если его нет
  // (cloud/tuya_local/similar), чтобы был выбор даже без кандидатов из Python.
  const _baseSrc = String((info && info._dps_source)
    || (discRec && discRec.source) || "").replace("+cache", "");
  if (_baseSrc && !_candBase[_baseSrc] && info && Object.keys(info).length) {
    _candBase[_baseSrc] = Object.assign({}, info);
  }
  const _hCode = (discRec && discRec.cloud_meta && discRec.cloud_meta.code)
    || info.code || info.name || "";
  if (_hCode && !_candBase.heuristic) {
    // v1.28.67: component угадываем по Cloud-type; если Cloud молчит —
    // по значению и code (раньше всегда получался sensor).
    const _hComp = (discRec && discRec.cloud_meta && discRec.cloud_meta.type)
      ? _dpsCompFromCloudType(discRec.cloud_meta.type, _hCode, undefined, _devTypeSafe())
      : _dpsGuessComponent(_hCode, val);
    const _hMeta = _dpsHeuristicMeta(_hCode, _hComp);
    const _hEntry = Object.assign({ component: _hComp, name: _hCode }, _hMeta);
    _hEntry._meta_guess = Object.keys(_hMeta);
    _candBase.heuristic = _hEntry;
  }
  // v1.28.69: кандидат «cache» из источников убран — он не «берёт из кэша»,
  // а угадывал component по значению (это эвристика, не источник).
  // Значение и так видно строкой «Значение с устройства».
  const _PRIO = ["cloud", "tuya_local", "similar", "heuristic"];
  const _sources = Object.keys(_candBase).sort((a, b) => {
    const ia = _PRIO.indexOf(a), ib = _PRIO.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  // v1.28.71: для добавленного DP (бейдж NEW) предвыбираем источник,
  // из которого он был добавлен — чтобы поля и «Будет записано» были честными.
  if (!keepDirty && addedRec && addedRec.source) {
    const _aSrc = String(addedRec.source).replace("+cache", "");
    if (_sources.includes(_aSrc)) _dpsFillSource = _aSrc;
  }
  // v1.28.66: по умолчанию источник НЕ выбран — поля показываются «как есть».
  if (_dpsFillSource && !_sources.includes(_dpsFillSource)) _dpsFillSource = "";
  if (_dpsFillSource) {
    const _c = _candBase[_dpsFillSource];
    if (_c && typeof _c === "object") info = Object.assign({}, info, _c);
  }

  const cloudMeta = discRec ? discRec.cloud_meta : null;

  // v1.28.66: явно выбранный источник приоритетнее Cloud/эвристики.
  const _sel = _dpsFillSource ? _candBase[_dpsFillSource] : null;

  // v1.27.1 (B): cloudMeta.type приоритетнее info.component.
  let preComponent = "";
  if (_sel && _sel.component) {
    preComponent = _sel.component;
  } else if (cloudMeta && cloudMeta.type) {
    // v1.28.33.fixup3: передаём code — датчики (va_temperature)
    // должны остаться sensor, а не стать number.
    preComponent = _dpsCompFromCloudType(cloudMeta.type, cloudMeta.code, undefined, _devTypeSafe());
  } else if (info.component) {
    preComponent = info.component;
  } else if (val !== undefined) {
    preComponent = _dpsFillSuggestFromValue(val);
  } else {
    preComponent = "sensor";
  }

  // v1.28.33.fixup3: Cloud-code приоритетнее info.name/info.code.
  // Раньше устаревшее info.name ("temperature") перебивало
  // свежий Cloud-code ("va_temperature").
  let preCode = "";
  if (_sel && (_sel.code || _sel.name)) {
    preCode = _sel.code || _sel.name;
  } else if (cloudMeta && cloudMeta.code) {
    preCode = cloudMeta.code;
  } else if (info.code) {
    preCode = info.code;
  } else if (info.name && info.name !== "preset_mode" && info.name !== "phase_a") {
    preCode = info.name;
  }

  // v1.27.1: forced name для preset/phase_a (по component).
  const forcedName = _dpsComponentForcedName(preComponent);
  let preName = forcedName || preCode;
  // v1.28.68: bridge-forced DP — вся карточка только для чтения.
  // Поля фиксируем на «резервных» значениях, что бы ни выбрал источник.
  if (_bridgeForced) {
    preComponent = _bfInfo.comp;
    preName = _bfKey;
    preCode = _bfKey;   // entity_id = зарезервированное имя (phase_a/moisture/…)
  }

  // Остальные поля: info → cloudMeta.values → эвристика.
  const heur = _dpsHeuristicMeta(preCode, preComponent);
  const cvals = (cloudMeta && cloudMeta.values) || {};
  const preDeviceClass = info.device_class || cvals.device_class || heur.device_class || "";
  const preUnit = info.unit || cvals.unit || heur.unit || "";
  const preScale = (info.scale !== undefined) ? String(info.scale)
                 : (cvals.scale !== undefined ? String(cvals.scale) : "0");
  const preMin = (info.min !== undefined) ? String(info.min)
               : (cvals.min !== undefined ? String(cvals.min) : "");
  const preMax = (info.max !== undefined) ? String(info.max)
               : (cvals.max !== undefined ? String(cvals.max) : "");
  const preStep = (info.step !== undefined) ? String(info.step)
                : (cvals.step !== undefined ? String(cvals.step) : "1");
  const preStateClass = info.state_class || heur.state_class || "";

  let preOptions = "";
  if (Array.isArray(info.options) && info.options.length > 0) {
    preOptions = JSON.stringify(info.options);
  } else if (Array.isArray(cvals.range) && cvals.range.length > 0) {
    preOptions = JSON.stringify(cvals.range);
  }
  const hasCloudOptions = Array.isArray(cvals.range) && cvals.range.length > 0;

  // v1.28.69: одна честная подсказка про источник сопоставления.
  // Раньше склеивались «Источник: X» + причина от другого источника +
  // «Component из Cloud-mapping» — и путали.
  const _effSrc = String(_dpsFillSource || (info && info._dps_source)
    || (discRec && discRec.source) || "").replace("+cache", "");
  const _selCand = _dpsFillSource ? _candBase[_dpsFillSource] : null;
  const _srcEntry = _selCand || info;
  // v1.28.67: жёлтая подсветка — только когда действует источник «эвристика».
  const _heuristicActive = (_effSrc === "heuristic");
  const _guess = _heuristicActive && Array.isArray(info._meta_guess) ? info._meta_guess : [];
  const _hDeviceClass = _heuristicActive && (_guess.includes("device_class")
    || (!info.device_class && !cvals.device_class && !!heur.device_class));
  const _hStateClass = _heuristicActive && (_guess.includes("state_class")
    || (!info.state_class && !!heur.state_class));
  const _hUnit = _heuristicActive && (_guess.includes("unit")
    || (!info.unit && !cvals.unit && !!heur.unit));
  const _hYellow = (_hDeviceClass || _hStateClass || _hUnit);

  let hint;
  if (_bridgeForced) {
    hint = (_dpsFillMode === "add"
      ? "🔒 Bridge зарезервировал «" + _bfKey + "» (" + _bfInfo.why
        + ") — имя и компонент фиксированы."
      : "🔒 Bridge зарезервировал «" + _bfKey + "» (" + _bfInfo.why
        + "). Изменять нельзя: ошиблись — удалите DP и добавьте заново.");
  } else if (!_effSrc) {
    hint = "Поля «как есть». Выбери источник — подставятся его компонент и тип.";
  } else if (_effSrc === "heuristic") {
    hint = "Компонент угадан по имени/значению — проверьте поля"
         + (_hYellow ? " (жёлтые — догадка)." : ".");
  } else if (_effSrc === "cache") {
    hint = "Сопоставления нет: показано только значение из кэша bridge.";
  } else {
    hint = "Сопоставление: " + _srcReasonText(_effSrc, _srcEntry) + "."
         + (_hYellow ? " Жёлтые поля — догадка по имени, проверьте." : "");
  }

  const isView = _dpsFillMode === "view";
  const isEdit = _dpsFillMode === "edit";
  const isAdd = _dpsFillMode === "add";

  // Список компонентов с пометками.
  const comps = [
    { v: "switch",        label: "switch" },
    { v: "sensor",        label: "sensor" },
    { v: "binary_sensor", label: "binary_sensor" },
    { v: "select",        label: "select" },
    { v: "number",        label: "number" },
    { v: "preset",        label: "preset" },
    { v: "light",         label: "light" },
    { v: "phase_a",       label: "phase_a" },
    { v: "climate",       label: "climate (не используется в bridge)" },
    { v: "button",        label: "button (не используется в bridge)" },
    { v: "time",          label: "time (не используется в bridge)" },
    { v: "lock",          label: "lock (не используется в bridge)" },
  ];

  const needDetails = _dpsComponentNeedsDetails(preComponent)
    || preDeviceClass || preUnit || preScale !== "0" || preOptions
    || preMin || preMax;

  // v1.28.68: bridge-forced DP — все поля только для чтения; добавлять
  // зарезервированный DP можно, но с фиксированными name/component.
  const disAttr = (isView || _bridgeForced) ? "disabled" : "";

  // Кнопка submit
  let submitLabel = "Добавить";
  if (isEdit) submitLabel = "Сохранить";
  if (isView) submitLabel = "Закрыть";

  // datalist для name (light)
  const nameListId = (_devTypeSafe() === "light") ? "dps-fill-name-list" : "";
  let nameListHtml = "";
  if (_devTypeSafe() === "light") {
    nameListHtml = `<datalist id="dps-fill-name-list">${
      _LIGHT_DP_NAMES_FRONT.map(x => `<option value="${x}">`).join("")
    }</datalist>`;
  }

  let html = `<p class="muted" style="margin-top:0;font-size:12px;">DP ${escapeHtml(dp)}</p>    <div class="dps-fill-hint">${escapeHtml(hint)}</div>
    ${(!_bridgeForced && _sources.length >= 1) ? `<div class="form-group">
      <label>Источник сопоставления</label>
      <div class="src-btns">
        ${_sources.map(s => {
          const _c = _candBase[s] || {};
          return `<button type="button" class="src-btn${s === _dpsFillSource ? " active" : ""}"`
               + ` ${isView ? "disabled" : ""} onclick="dpsFillToggleSource('${s}')"`
               + ` title="${escapeAttr(_srcReasonText(s, _c))}">${_srcLabelRu(s)}</button>`;
        }).join("")}
      </div>
      <div class="dps-fill-hint" style="margin-top:4px;">Клик по источнику подставит его поля; повторный клик — снять выбор.</div>
    </div>` : ""}

    <div class="form-group">
      <label>Code * ${_bridgeForced ? '<span class="badge-locked" data-tip="' + escapeAttr("Bridge-forced: облако '" + _bfInfo.cloud + "' → '" + preName + "' для " + _bfInfo.why + ". Изменить нельзя.") + '">🔒</span>' : ''}</label>
      <input id="dps-fill-code" type="text" value="${escapeAttr(preCode)}"
             placeholder="например temp_correction" ${_bridgeForced ? "disabled" : disAttr}
             oninput="_dpsFillMarkDirty(); dpsFillValidateCodeLive()"
             onblur="dpsFillValidateCode(true)">
      <div id="dps-fill-code-err" class="dps-fill-warn-msg" style="display:none;"></div>
      <div id="dps-fill-code-warn" class="dps-fill-warn-msg" style="display:none;"></div>
      ${_bridgeForced ? '<div class="dps-fill-hint-locked">Bridge управляет этим именем (HA MQTT). В облаке: ' + escapeHtml(_bfInfo.cloud) + '.</div>' : ''}
    </div>

    <div class="form-group">
      <label>Component * ${_bridgeForced ? '<span class="badge-locked" data-tip="' + escapeAttr("Bridge-forced: облако '" + _bfInfo.cloud + "' → '" + preName + "' (" + _bfInfo.why + "). Компонент изменить нельзя.") + '">🔒</span>' : ''}</label>
      <select id="dps-fill-component" ${_bridgeForced ? "disabled" : disAttr} onchange="_dpsFillMarkDirty(); dpsFillUpdateVisibility(); dpsFillValidateCodeLive()">
        ${comps.map(c => `<option value="${c.v}" ${c.v === preComponent ? "selected" : ""}>${escapeHtml(c.label)}</option>`).join("")}
      </select>
    </div>

    ${nameListHtml}

    <details id="dps-fill-details" style="margin-top:8px;" ${needDetails ? "open" : ""}>
      <summary class="muted" style="font-size:12px; cursor:pointer;">▶ Дополнительно<span class="muted" style="font-size:11px; margin-left:6px;">клик — раскрыть</span></summary>
      <div style="margin-top:8px;">
        <div class="form-group">
          <label>device_class</label>
          <input id="dps-fill-device_class" type="text" list="dps-fill-dc-list"
                 value="${escapeAttr(preDeviceClass)}" placeholder="temperature / humidity / ..."
                 class="${_hDeviceClass ? "heuristic-field" : ""}"
                 title="${_hDeviceClass ? "угадано эвристикой по code" : ""}"
                 ${disAttr} oninput="_dpsFillMarkDirty()">
          <datalist id="dps-fill-dc-list">${_DC_ALLOWED.map(x => `<option value="${x}">`).join("")}</datalist>
        </div>
        <div class="form-group">
          <label>state_class</label>
          <input id="dps-fill-state_class" type="text" list="dps-fill-sc-list"
                 value="${escapeAttr(preStateClass)}" placeholder="measurement / total / total_increasing"
                 class="${_hStateClass ? "heuristic-field" : ""}"
                 title="${_hStateClass ? "угадано эвристикой по code" : ""}"
                 ${disAttr} oninput="_dpsFillMarkDirty()">
          <datalist id="dps-fill-sc-list">${_SC_ALLOWED.map(x => `<option value="${x}">`).join("")}</datalist>
        </div>
        <div class="form-group">
          <label>unit</label>
          <input id="dps-fill-unit" type="text" value="${escapeAttr(preUnit)}"
                 placeholder="°C / % / kWh / ..."
                 class="${_hUnit ? "heuristic-field" : ""}"
                 title="${_hUnit ? "угадано эвристикой по code" : ""}"
                 ${disAttr} oninput="_dpsFillMarkDirty()">
        </div>
        <div class="form-group">
          <label>scale</label>
          <input id="dps-fill-scale" type="number" value="${escapeAttr(preScale)}"
                 min="0" max="9" ${disAttr} oninput="_dpsFillMarkDirty()">
        </div>
        <div id="dps-fill-number-block" style="display:none;">
          <div class="form-group">
            <label>min *</label>
            <input id="dps-fill-min" type="number" value="${escapeAttr(preMin)}"
                   ${disAttr} oninput="_dpsFillMarkDirty()">
          </div>
          <div class="form-group">
            <label>max *</label>
            <input id="dps-fill-max" type="number" value="${escapeAttr(preMax)}"
                   ${disAttr} oninput="_dpsFillMarkDirty()">
          </div>
          <div class="form-group">
            <label>step</label>
            <input id="dps-fill-step" type="number" value="${escapeAttr(preStep)}"
                   ${disAttr} oninput="_dpsFillMarkDirty()">
          </div>
        </div>
        <div id="dps-fill-options-block" style="display:none;">
          <div class="form-group">
            <label>options (JSON-массив строк) *
              ${hasCloudOptions ? `<button type="button" id="dps-fill-options-from-cloud"
                style="float:right; padding:2px 8px; font-size:11px;"
                onclick="dpsFillFromCloud()">📋 Из Cloud</button>` : ""}
            </label>
            <textarea id="dps-fill-options" rows="3"
              placeholder='Пример: ["off", "on"]'
              style="width:100%; font-family:ui-monospace,monospace; font-size:12px;"
              ${disAttr} oninput="_dpsFillMarkDirty(); dpsFillValidateOptionsLive()"
              onblur="dpsFillValidateOptions(true)">${escapeHtml(preOptions)}</textarea>
            <div id="dps-fill-options-preview" class="muted"
                 style="font-size:11px; margin-top:4px; display:none;"></div>
            <div id="dps-fill-options-cloud-warn" class="muted"
                 style="font-size:11px; margin-top:4px; color:var(--yellow); display:none;"></div>
          </div>
        </div>
      </div>
    </details>

    ${(isAdd || isEdit) ? `<div class="dps-fill-willwrite" id="dps-fill-willwrite">
      <span id="dps-fill-willwrite-body"></span>
      <span id="dps-fill-willsrc" style="display:none;"
            data-src="${escapeAttr(_dpsFillSource ? _srcLabelRu(_dpsFillSource) : "")}"></span>
    </div>` : ""}

    <details id="dps-fill-ref" class="dps-fill-ref" ontoggle="dpsFillRenderRef(this)">
      <summary class="muted">▶ Значения (кэш bridge / Cloud)<span class="muted" style="font-size:11px; margin-left:6px;">клик — раскрыть</span></summary>
      <div class="dps-fill-ref-body"><span class="muted">раскройте — покажем значения из кэшей</span></div>
    </details>

    <div id="dps-fill-error" class="muted"
         style="display:none; color:var(--red); font-size:12px; margin-top:8px;"></div>
  `;

  const titleEl = document.getElementById("dps-fill-title");
  // v1.28.68: bridge-forced — read-only. Добавлять зарезервированный DP
  // можно (поля фиксированы), редактировать существующий — нельзя.
  const _ro = isView || (_bridgeForced && !isAdd);
  if (_bridgeForced) titleEl.textContent = `DP ${dp} — `
    + (isAdd ? "добавление (зарезервировано bridge)" : "только просмотр");
  else if (isView) titleEl.textContent = `DP ${dp} — просмотр`;
  else if (isEdit) titleEl.textContent = `Редактировать DP ${dp}`;
  else titleEl.textContent = `Добавить DP ${dp}`;

  document.getElementById("dps-fill-body").innerHTML = html;

  const submitBtn = document.getElementById("dps-fill-submit");
  submitBtn.textContent = _ro ? "Закрыть" : submitLabel;
  submitBtn.disabled = _ro;   // в view/RO-режиме submit не нужен
  submitBtn.style.display = _ro ? "none" : "";

  document.getElementById("dps-fill-overlay").classList.add("open");
  setTimeout(() => {
    dpsFillUpdateVisibility();
    // v1.28.75: снимок полей ПОСЛЕ инициализации — база для «изменено?».
    _dpsFillOrigSnapshot = _dpsFillSnapshot();
    dpsFillUpdateWillWrite();   // v1.28.69: сводка «что уйдёт в конфиг»
    if (!_ro) {
      const inp = document.getElementById("dps-fill-code");
      if (inp) inp.focus();
    }
  }, 50);
}

// v1.27.1: безопасный доступ к типу устройства.
function _devTypeSafe() {
  return (DPS_EDIT && DPS_EDIT._devType) ? DPS_EDIT._devType : "";
}

// v1.28.30: единая точка для состояния submit-кнопки.
// Учитывает и Code (regex/дубликаты/light), и Options (только select).
// Раньше каждый валидатор ставил submitBtn.disabled независимо:
// переключение component с select на sensor оставляло кнопку disabled.
function _dpsFillUpdateSubmitState() {
  const submitBtn = document.getElementById("dps-fill-submit");
  if (!submitBtn || _dpsFillMode === "view") return;
  const codeInput = document.getElementById("dps-fill-code");
  const compInput = document.getElementById("dps-fill-component");
  if (!codeInput || !compInput) return;
  const codeValid = codeInput.disabled
    ? true
    : _validateDpCode((codeInput.value || "").trim(),
                      compInput.value || "", _dpsFillDp).level !== "err";
  let optionsValid = true;
  if ((compInput.value || "") === "select") {
    const ta = document.getElementById("dps-fill-options");
    const val = ta ? (ta.value || "").trim() : "";
    if (!val) {
      optionsValid = false;
    } else {
      try {
        const parsed = JSON.parse(val);
        optionsValid = Array.isArray(parsed) && parsed.length > 0
          && parsed.every(x => typeof x === "string");
      } catch (e) { optionsValid = false; }
    }
  }
  submitBtn.disabled = !(codeValid && optionsValid);
}

function dpsFillUpdateVisibility() {
  const comp = (document.getElementById("dps-fill-component") || {}).value || "";
  const numBlock = document.getElementById("dps-fill-number-block");
  const optBlock = document.getElementById("dps-fill-options-block");
  if (numBlock) numBlock.style.display = comp === "number" ? "block" : "none";
  // v1.27.11: options-блок только для select (preset не требует).
  if (optBlock) optBlock.style.display = comp === "select" ? "block" : "none";

  // v1.27.1: авто-раскрытие <details> для компонентов, требующих доп. поля.
  const det = document.getElementById("dps-fill-details");
  if (det && _dpsComponentNeedsDetails(comp)) det.open = true;

  // v1.28.30: пересчитать состояние submit-кнопки при смене component.
  // Это ключевой фикс: при select→sensor кнопка разблокируется,
  // при sensor→select — блокируется (пока options не введены).
  _dpsFillUpdateSubmitState();

  if (comp === "select") {
    const ta = document.getElementById("dps-fill-options");
    if (ta) dpsFillValidateOptionsLive();
  }
  // v1.27.2: плашка про отсутствие range в Cloud.
  if (typeof _dpsFillUpdateCloudWarn === "function") _dpsFillUpdateCloudWarn();
}

async function closeDpsFill(evt, force) {
  // v1.27.1: force=true — закрытие из dpsFillSubmit (не спрашивать).
  // force=undefined и evt — обычное закрытие (Escape/overlay/крестик).
  // v1.28.25: force==="esc" — Escape. Закрываем БЕЗ подтверждения.
  // Иначе цикл: Esc → uiConfirm → Cancel → dps-fill открыт → Esc → uiConfirm ...
  if (evt && evt.target && evt.target.id !== "dps-fill-overlay") return;
  const _no_confirm = (force === "esc");
  if (!_no_confirm && !force && _dpsFillDirty) {
    const ok = await uiConfirm(
      "Закрыть без сохранения?",
      "Введённые данные будут потеряны.",
      { danger: true, okText: "Закрыть" }
    );
    if (!ok) return;
  }
  document.getElementById("dps-fill-overlay").classList.remove("open");
  _dpsFillDp = null;
  _dpsFillMode = "add";
  _dpsFillSource = "";
  _dpsFillDirty = false;
  const prev = document.getElementById("dps-fill-options-preview");
  if (prev) { prev.style.display = "none"; prev.textContent = ""; }
}

function dpsFillSubmit() {
  if (!DPS_EDIT || _dpsFillDp === null) return;
  if (_dpsFillMode === "view") { closeDpsFill(); return; }
  const errEl = document.getElementById("dps-fill-error");
  errEl.style.display = "none";

  const code = (document.getElementById("dps-fill-code").value || "").trim();
  const component = document.getElementById("dps-fill-component").value;
  const deviceClass = (document.getElementById("dps-fill-device_class").value || "").trim();
  const stateClass = (document.getElementById("dps-fill-state_class").value || "").trim();
  const unit = (document.getElementById("dps-fill-unit").value || "").trim();
  const scaleStr = (document.getElementById("dps-fill-scale").value || "0").trim();

  // v1.27.10: серверная проверка code (regex ^[a-z_][a-z0-9_-]*$).
  const _v = _validateDpCode(code, component, _dpsFillDp);
  if (_v.level === "err") {
    errEl.style.display = "block";
    errEl.textContent = _v.msg;
    return;
  }

  // v1.27.1: forced name для preset/phase_a (требование bridge).
  const forcedName = _dpsComponentForcedName(component);
  const finalName = forcedName || code;

  // v1.27.11: для light — Code из LIGHT_DP_NAMES_FRONT
  // (сообщение — «Code», а не «name» — так пользователю понятнее).
  if (_devTypeSafe() === "light" && !forcedName) {
    if (!_LIGHT_DP_NAMES_FRONT.includes(finalName)) {
      errEl.style.display = "block";
      errEl.textContent =
        "Для light «Code» должен быть одним из: " + JSON.stringify(_LIGHT_DP_NAMES_FRONT);
      return;
    }
  }

  const info = { component: component, name: finalName };
  if (deviceClass) info.device_class = deviceClass;
  if (stateClass) info.state_class = stateClass;
  if (unit) info.unit = unit;
  const scale = parseInt(scaleStr, 10);
  if (!isNaN(scale) && scale > 0) info.scale = scale;

  if (component === "number") {
    const minV = parseFloat(document.getElementById("dps-fill-min").value);
    const maxV = parseFloat(document.getElementById("dps-fill-max").value);
    const stepV = parseFloat(document.getElementById("dps-fill-step").value);
    if (isNaN(minV) || isNaN(maxV)) {
      errEl.style.display = "block";
      errEl.textContent = "number: нужно указать min и max";
      return;
    }
    if (minV >= maxV) {
      errEl.style.display = "block";
      errEl.textContent = "number: min < max";
      return;
    }
    info.min = minV;
    info.max = maxV;
    if (!isNaN(stepV) && stepV > 0) info.step = stepV;
  }

  // v1.27.11: options обязателен только для select (preset — нет).
  if (component === "select") {
    const optStr = (document.getElementById("dps-fill-options").value || "").trim();
    if (!optStr) {
      errEl.style.display = "block";
      errEl.textContent = "options: нужен непустой JSON-массив строк";
      return;
    }
    let opts;
    try { opts = JSON.parse(optStr); } catch (e) {
      errEl.style.display = "block";
      errEl.textContent = "options: невалидный JSON";
      return;
    }
    if (!Array.isArray(opts) || opts.length === 0 || !opts.every(x => typeof x === "string")) {
      errEl.style.display = "block";
      errEl.textContent = "options: должен быть непустой массив строк";
      return;
    }
    info.options = opts;
  }

  // v1.27.1: логика по режиму.
  if (_dpsFillMode === "edit") {
    // NEW — обновляем _added
    const addedRec = DPS_EDIT._added.find(x => x.dp === _dpsFillDp);
    if (addedRec) {
      addedRec.info = info;
    } else {
      // edit старого — в _modified (вариант B)
      DPS_EDIT._modified[_dpsFillDp] = info;
    }
  } else {
    // add
    DPS_EDIT._added.push({ dp: _dpsFillDp, info: info, source: _dpsFillSource || "" });
  }

  _dpsFillDirty = false;
  // v1.28.22: force=true → второй аргумент (первый — evt из onclick).
  // Раньше closeDpsFill(true) попадал в evt, работал случайно.
  closeDpsFill(null, true);
  dpsRenderSection();
}

// v1.27.1: логика кнопки «📋 Из Cloud».
async function dpsFillFromCloud() {
  const disc = DPS_EDIT._discovered.find(x => x.dp === _dpsFillDp);
  if (!disc || !disc.cloud_meta || !disc.cloud_meta.values) return;
  const range = disc.cloud_meta.values.range;
  if (!Array.isArray(range) || range.length === 0) return;
  const newVal = JSON.stringify(range);
  const ta = document.getElementById("dps-fill-options");
  if (!ta) return;
  const cur = (ta.value || "").trim();
  if (cur && cur !== newVal) {
    const ok = await uiConfirm(
      "Заменить options?",
      "Текущее значение будет перезаписано данными из Cloud.",
      { danger: true, okText: "Заменить" }
    );
    if (!ok) return;
  }
  ta.value = newVal;
  _dpsFillMarkDirty();
  dpsFillValidateOptionsLive();
}

// v1.27.1: живой парсер options.
function dpsFillValidateOptionsLive() {
  if (_dpsFillValidateTimer) clearTimeout(_dpsFillValidateTimer);
  _dpsFillValidateTimer = setTimeout(() => dpsFillValidateOptions(false), 300);
}

// v1.27.10: live-проверка Code (debounce 300мс).
let _dpsFillCodeTimer = null;
function dpsFillValidateCodeLive() {
  if (_dpsFillCodeTimer) clearTimeout(_dpsFillCodeTimer);
  _dpsFillCodeTimer = setTimeout(() => dpsFillValidateCode(false), 300);
}

function dpsFillValidateCode(isBlur) {
  const inp = document.getElementById("dps-fill-code");
  if (!inp || inp.disabled) return;   // bridge-forced — не проверяем
  const errEl = document.getElementById("dps-fill-code-err");
  const warnEl = document.getElementById("dps-fill-code-warn");
  const code = (inp.value || "").trim();
  const component = (document.getElementById("dps-fill-component") || {}).value || "";
  const v = _validateDpCode(code, component, _dpsFillDp);
  if (v.level === "err") {
    inp.classList.add("dps-fill-field-error");
    inp.classList.remove("dps-fill-input-warn");
    if (errEl) { errEl.style.display = "block"; errEl.textContent = "❌ " + v.msg; }
    if (warnEl) { warnEl.style.display = "none"; warnEl.textContent = ""; }
    // v1.28.30: единая точка — учитывает и options.
    _dpsFillUpdateSubmitState();
    return;
  }
  inp.classList.remove("dps-fill-field-error");
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  // warning — облачный code отличается?
  const w = _cloudCodeWarning(code, _dpsFillDp);
  if (w) {
    inp.classList.add("dps-fill-input-warn");
    if (warnEl) { warnEl.style.display = "block"; warnEl.textContent = "⚠️ " + w; }
  } else {
    inp.classList.remove("dps-fill-input-warn");
    if (warnEl) { warnEl.style.display = "none"; warnEl.textContent = ""; }
  }
  // v1.28.30: единая точка — учитывает и options.
  _dpsFillUpdateSubmitState();
}

// v1.27.2: плашка «Cloud не вернул значения» для select/preset без options.
function _dpsFillUpdateCloudWarn() {
  const warn = document.getElementById("dps-fill-options-cloud-warn");
  if (!warn) return;
  const comp = (document.getElementById("dps-fill-component") || {}).value || "";
  if (comp !== "select" && comp !== "preset") {
    warn.style.display = "none"; warn.textContent = "";
    return;
  }
  // Ищем cloudMeta у текущего DP.
  const disc = DPS_EDIT && DPS_EDIT._discovered
    ? DPS_EDIT._discovered.find(x => x.dp === _dpsFillDp)
    : null;
  const cm = disc && disc.cloud_meta ? disc.cloud_meta : null;
  const cvals = (cm && cm.values) || {};
  let cvalsObj = cvals;
  if (typeof cvalsObj === "string") {
    try { cvalsObj = JSON.parse(cvalsObj); } catch (e) { cvalsObj = {}; }
  }
  const hasRange = Array.isArray(cvalsObj.range) && cvalsObj.range.length > 0;
  const ta = document.getElementById("dps-fill-options");
  const taVal = ta ? (ta.value || "").trim() : "";
  if (!hasRange && !taVal) {
    warn.style.display = "block";
    warn.textContent = "⚠️ Cloud не вернул values.range для этого DP — заполните options вручную.";
  } else if (!hasRange && taVal) {
    // Ручное заполнение — предупреждение не нужно.
    warn.style.display = "none"; warn.textContent = "";
  } else {
    warn.style.display = "none"; warn.textContent = "";
  }
}

function dpsFillValidateOptions(isBlur) {
  const ta = document.getElementById("dps-fill-options");
  if (!ta) return;
  const prev = document.getElementById("dps-fill-options-preview");
  const comp = (document.getElementById("dps-fill-component") || {}).value || "";
  if (comp !== "select") {
    if (prev) { prev.style.display = "none"; prev.textContent = ""; }
    ta.classList.remove("dps-fill-field-error");
    // v1.28.30: не оставляем кнопку disabled после смены component.
    _dpsFillUpdateSubmitState();
    return;
  }
  const val = (ta.value || "").trim();
  if (!val) {
    if (prev) {
      prev.style.display = "block";
      prev.style.color = "var(--muted)";
      prev.textContent = "ℹ️ Обязательное поле: непустой JSON-массив строк";
    }
    if (isBlur) ta.classList.add("dps-fill-field-error");
    else ta.classList.remove("dps-fill-field-error");
    _dpsFillUpdateSubmitState();
    return;
  }
  let parsed;
  try { parsed = JSON.parse(val); } catch (e) {
    if (prev) {
      prev.style.display = "block";
      prev.style.color = "var(--red)";
      prev.textContent = "❌ Невалидный JSON: " + (e.message || "ошибка");
    }
    ta.classList.add("dps-fill-field-error");
    _dpsFillUpdateSubmitState();
    return;
  }
  if (!Array.isArray(parsed) || parsed.length === 0 || !parsed.every(x => typeof x === "string")) {
    if (prev) {
      prev.style.display = "block";
      prev.style.color = "var(--red)";
      prev.textContent = "❌ Должен быть непустой массив строк";
    }
    ta.classList.add("dps-fill-field-error");
    _dpsFillUpdateSubmitState();
    return;
  }
  ta.classList.remove("dps-fill-field-error");
  if (prev) {
    prev.style.display = "block";
    prev.style.color = "var(--green)";
    prev.textContent = "✅ " + parsed.length + " значений: " + parsed.join(", ");
  }
  _dpsFillUpdateSubmitState();
  // v1.27.2: скрыть warn, если пользователь ввёл options вручную.
  if (typeof _dpsFillUpdateCloudWarn === "function") _dpsFillUpdateCloudWarn();
}


// Init
detectView();
loadCloudCreds();
if (VIEW === "import") {
  (async () => {
    if (await loadCloudCacheServer()) {
      renderCloudDevices();
    }
    loadBaseInfo();
  })();
}
fetchStatus();
// P2 1.22.0: применяем LOG_SOURCE из localStorage к кнопкам при старте
// v1.23.0: плашка «Все» для WebUI
(function() {
  document.querySelectorAll(".log-source-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.src === LOG_SOURCE);
  });
  // v1.28.86: единая панель уровней для Bridge и WebUI.
  const lvlWrap = document.getElementById("log-level-filter");
  if (lvlWrap) lvlWrap.classList.remove("hidden");
  LOG_LEVEL_FILTER = _LOG_LEVELS[LOG_SOURCE] || "INFO";
  document.querySelectorAll(".logs-toolbar button[data-level]").forEach(b => {
    b.classList.toggle("active", b.dataset.level === LOG_LEVEL_FILTER);
  });
  // v1.28.92: применяем сохранённый период сразу (без ожидания истории),
  // иначе кнопки «прыгают» на дефолт «1 час».
  setLogRange(LOG_RANGE_SECONDS);
})();
loadLogHistory().then((maxSeq) => {
  // v1.25.12: lastSeq = maxSeq (а не max(lastSeq, maxSeq)) —
  // для текущего источника это правильнее.
  _setLastSeq(maxSeq > 0 ? maxSeq : 1);
  connectSSE();
});
// v1.23.8: восстанавливаем UI лог-панели из sessionStorage.
(function restoreLogUiState() {
  if (SEARCH_TERM) {
    const inp = document.getElementById("log-search");
    if (inp) inp.value = SEARCH_TERM;
    doSearch();
  }
  if (logPaused) {
    const btn = document.getElementById("pause-btn");
    if (btn) {
      btn.textContent = "▶ Продолжить";
      btn.classList.add("active");
    }
    if (!document.getElementById("logs-paused-banner")) {
      const b = document.createElement("div");
      b.id = "logs-paused-banner";
      b.className = "logs-paused-banner";
      b.textContent = "⏸ Логи на паузе — новые записи не отображаются";
      logsEl.parentNode.insertBefore(b, logsEl);
    }
  }
  if (userScrolledUp) {
    const sb = document.getElementById("scroll-down-btn");
    if (sb) sb.classList.add("visible");
  }
})();
setInterval(fetchStatus, 5000);
refreshHealthWidget();
setInterval(refreshHealthWidget, 30000);
if (VIEW === "analytics" && ANALYTICS_ENABLED) {
  setLatencyPeriod(LATENCY_PERIOD);
  loadAnalytics();
  setInterval(loadAnalytics, 30000);
}
