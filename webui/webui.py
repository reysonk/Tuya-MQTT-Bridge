#!/usr/bin/env python3
"""
Tuya Bridge WebUI — отдельный контейнер.
"""

import json
import os
import re
import struct
import select
import sqlite3
import subprocess
import time
import threading
import logging
import uuid
import copy
from collections import deque  # audit_read + v1.28.19: _read_initial_log
import shutil
import socket as _socket
import queue as _queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import paho.mqtt.client as mqtt

try:
    import tinytuya
    HAS_TINYTUYA = True
except ImportError:
    # v1.33.13: имя должно существовать и без пакета — иначе обращение к
    # tinytuya.* дало бы NameError (замечание инспекций IDE).
    tinytuya = None
    HAS_TINYTUYA = False

try:
    import yaml
    HAS_YAML = True
except ImportError:
    yaml = None
    HAS_YAML = False


# ==================== SETTINGS ====================
# v1.28.99: значения можно переопределить переменными окружения
# (docker-compose `environment:`); иначе берутся значения по умолчанию.
def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MQTT_BROKER = os.getenv("MQTT_BROKER") or "192.168.1.10"
MQTT_PORT = _env_int("MQTT_PORT", 1883)
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
TOPIC_PREFIX = os.getenv("TOPIC_PREFIX") or "tuya"
WEBUI_PORT = _env_int("WEBUI_PORT", 5386)
WEBUI_HOST = os.getenv("WEBUI_HOST") or "0.0.0.0"
WEBUI_VERSION = "1.33.29"
# v1.32.33: публичный номер релиза (совпадает с тегом релиза на GitHub).
# Подвал показывает «Release X», а WebUI сверяет по нему наличие новой версии.
RELEASE_TAG = os.getenv("RELEASE_TAG") or "1.2"

# v1.32.34: проверка «есть ли релиз новее» на GitHub (публичный репозиторий, без токена;
# GITHUB_TOKEN поддержан на случай, если репозиторий останется приватным).
GITHUB_RELEASES_LATEST = ("https://api.github.com/repos/"
                          "reysonk/Tuya-MQTT-Bridge/releases/latest")
_RELEASE_CHECK = {"ts": 0, "latest": None, "newer": False, "url": "", "error": ""}


def _ver_tuple(tag):
    """'v1.2.3' → (1, 2, 3); нечисловые части считаются нулями."""
    nums = []
    # v1.32.39: отбрасываем pre-release/build-суффикс и выравниваем длину —
    # иначе «1.2.0» > «1.2» и «1.2-rc1» давали ложное «новее».
    base = str(tag or "").lstrip("vV").split("-")[0].split("+")[0]
    for part in base.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums or [0])


def check_new_release(force=False):
    """Спрашивает GitHub не чаще раза в 6 часов: есть ли тег новее RELEASE_TAG."""
    import urllib.request as _u
    now = time.time()
    if not force and (now - _RELEASE_CHECK["ts"]) < 6 * 3600:
        return dict(_RELEASE_CHECK)
    try:
        req = _u.Request(GITHUB_RELEASES_LATEST, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "tuya-bridge-webui",
        })
        token = os.getenv("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with _u.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = data.get("tag_name") or ""
        _RELEASE_CHECK.update({
            "ts": now, "latest": tag,
            "newer": _ver_tuple(tag) > _ver_tuple(RELEASE_TAG),
            "url": data.get("html_url") or "", "error": "",
        })
    except Exception as e:
        # нет интернета / приватный репозиторий / лимит GitHub — молчим,
        # v1.32.39: и СБРАСЫВАЕМ прошлый результат — иначе значок «!» висел бы
        # до 6 часов со старой ссылкой на уже проверенный релиз.
        _RELEASE_CHECK.update({"ts": now, "error": str(e)[:200],
                               "latest": None, "newer": False, "url": ""})
    return dict(_RELEASE_CHECK)

CONFIG_FILE = "config/devices_config.json"
LOG_FILE = "logs/bridge.log"
LOG_FILE_WEBUI = "logs/webui.log"
LOG_FILE_MAX_BYTES_WEBUI = 5 * 1024 * 1024
LOG_FILE_BACKUPS_WEBUI = 2
DB_FILE = "webui_state/analytics.db"
TINYTUYA_DEVICES_FILE = "webui_state/tinytuya_devices.json"
TUYA_CLOUD_CACHE_FILE = "webui_state/tuya_cloud_cache.json"
CONFIG_AUDIT_FILE = "webui_state/config_audit.log"
QUIET_HOURS_FILE = "webui_state/quiet_hours.json"
QUIET_GRACE_SEC = 120
AUDIT_MAX_BYTES = 5 * 1024 * 1024  # 5 МБ (v1.22.0)
AUDIT_BACKUPS = 3
TUYA_LOCAL_DB_DIR = "webui_state/tuya-local-db"
TUYA_LOCAL_YAML_DIR = "webui_state/tuya-local-db/custom_components/tuya_local/devices"
TUYA_LOCAL_DB_OLD_DIR = "/app/tuya-local-db"
TUYA_LOCAL_TARBALL_URL = "https://github.com/make-all/tuya-local/archive/refs/heads/main.tar.gz"
# v1.33.9: сколько ждать одну попытку скачивания (сеть до GitHub бывает медленной).
TUYA_LOCAL_DL_TIMEOUT = 180
# v1.28.63: проиндексированная tuya-local БД {product_id: dps_map}.
TUYA_LOCAL_INDEX_FILE = "webui_state/tuya-local-db.json"

LOG_POLL_INTERVAL = 1.0
LOG_HISTORY_LINES = 1000

ANALYTICS_ENABLED = True
STATUS_HISTORY_ENABLED = True

# v1.27.7b: политика для устройств с enabled: false.
# enabled=false означает «не опрашивать, не пинговать, не показывать
# в аналитике». WebUI всё равно грузит их в DEVICE_META, чтобы
# показать в серой секции «⛔ Отключённые» и в Cloud-модалке.
DISABLED_HIDE_FROM_ANALYTICS = True   # не показывать в /api/analytics (latency)
DISABLED_BLOCK_IP_REUSE       = True   # IP отключённых = занят (probe/import)

RETENTION_DAYS = 3
FLUSH_INTERVAL = 30
HOURLY_INTERVAL = 60

LATENCY_INTERVAL = 900
LATENCY_INITIAL_DELAY = 5
LATENCY_PING_TIMEOUT = 1
LATENCY_PING_COMMAND = "ping"
# v1.33.6: один замер часто врёт (сеть/спящий стек) — берём среднее из N проб.
LATENCY_PING_SAMPLES = 5

LATENCY_RETRY_COUNT = 3
LATENCY_RETRY_DELAY = 10
LATENCY_RETRY_WORKERS = 10

# v1.18.9: игнорировать события online/offline в первые N сек после старта bridge
BRIDGE_STARTUP_GRACE_SEC = 60

SCAN_TIMEOUT_WAIT = 30
IMPORT_TIMEOUT_WAIT = 30
EDIT_TIMEOUT_WAIT = 20
DELETE_TIMEOUT_WAIT = 20
PROBE_TIMEOUT_PER_VERSION = 1.5
PROBE_VERSIONS = ("3.3", "3.4", "3.5", "3.1")

# v1.31.19: предел тела запроса — защита от «заявленного» гигабайта и мусорных POST.
MAX_BODY_BYTES = 1024 * 1024

SSE_MAX_SUBSCRIBERS = 50
SSE_IDLE_TIMEOUT = 180   # v1.22.1: 60 → 180 (реже reconnect)
SSE_BACKLOG = 100

SAFE_PORTS = [
    (22, "SSH"), (23, "Telnet"), (53, "DNS"), (80, "HTTP"), (443, "HTTPS"),
    (554, "RTSP"), (631, "IPP"), (1883, "MQTT"), (3389, "RDP"), (5000, "UPnP/AV"),
    (5001, "Synology"), (8008, "Chromecast"), (8009, "Chromecast"),
    (8080, "HTTP-alt"), (8443, "HTTPS-alt"), (9100, "Printer"),
    (32400, "Plex"), (62078, "iPhone"),
]

# ============================================================
# v1.25.0 (task #C): словари русских имён DP.
# ============================================================

# v1.28.19: Python-словари DP_CODE_NAMES_RU / DP_CN_NAMES_RU удалены —
# их роль выполняет JS-дубликат DP_CODE_NAMES_RU_FRONT /
# DP_CN_NAMES_RU_FRONT (в static/app.js). В Python-коде не использовались.

# v1.25.0 (fix #cloud_dps): индексы Cloud-mapping для обогащения
# dps_map в /api/status. Загружаются при старте и после /api/cloud/fetch.
TUYA_CLOUD_MAPPING_BY_ID = {}
TUYA_CLOUD_MAPPING_BY_NAME = {}
# v1.28.34: code из Cloud functions (записываемые DP) — вариант C.
TUYA_CLOUD_WRITABLE_BY_ID = {}
TUYA_CLOUD_WRITABLE_BY_NAME = {}
# v1.28.62: product_id из Cloud-кэша (для tuya-local lookup, если в конфиге нет).
TUYA_CLOUD_PID_BY_ID = {}
TUYA_CLOUD_PID_BY_NAME = {}
TUYA_CLOUD_MAPPING_LOCK = threading.Lock()


def _load_cloud_mappings():
    """Загрузить mapping из webui_state/tuya_cloud_cache.json в память.
    Ключи: tuya_id и name (friendly) — оба индекса для надёжности."""
    global TUYA_CLOUD_MAPPING_BY_ID, TUYA_CLOUD_MAPPING_BY_NAME
    global TUYA_CLOUD_WRITABLE_BY_ID, TUYA_CLOUD_WRITABLE_BY_NAME
    global TUYA_CLOUD_PID_BY_ID, TUYA_CLOUD_PID_BY_NAME
    by_id = {}
    by_name = {}
    wr_by_id = {}
    wr_by_name = {}
    pid_by_id = {}
    pid_by_name = {}
    try:
        if not os.path.exists(TUYA_CLOUD_CACHE_FILE):
            log.info("[CloudDPS] Кэш Cloud не найден — обогащение пропущено")
            with TUYA_CLOUD_MAPPING_LOCK:
                TUYA_CLOUD_MAPPING_BY_ID = {}
                TUYA_CLOUD_MAPPING_BY_NAME = {}
                TUYA_CLOUD_WRITABLE_BY_ID = {}
                TUYA_CLOUD_WRITABLE_BY_NAME = {}
                TUYA_CLOUD_PID_BY_ID = {}
                TUYA_CLOUD_PID_BY_NAME = {}
            return
        with open(TUYA_CLOUD_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        devices = data.get("devices") or []
        for d in devices:
            if not isinstance(d, dict):
                continue
            mapping = d.get("mapping") or {}
            if not isinstance(mapping, dict) or not mapping:
                continue
            did = d.get("id") or ""
            dname = d.get("name") or ""
            _pid = d.get("product_id") or ""
            # v1.28.34: code из Cloud functions (вариант C).
            wr = _writable_codes_from_props(d.get("_raw_properties"))
            if did:
                by_id[did] = mapping
                if wr is not None:
                    wr_by_id[did] = wr
                if _pid:
                    pid_by_id[did] = _pid
            if dname:
                by_name[dname] = mapping
                if wr is not None:
                    wr_by_name[dname] = wr
                if _pid:
                    pid_by_name[dname] = _pid
        with TUYA_CLOUD_MAPPING_LOCK:
            TUYA_CLOUD_MAPPING_BY_ID = by_id
            TUYA_CLOUD_MAPPING_BY_NAME = by_name
            TUYA_CLOUD_WRITABLE_BY_ID = wr_by_id
            TUYA_CLOUD_WRITABLE_BY_NAME = wr_by_name
            TUYA_CLOUD_PID_BY_ID = pid_by_id
            TUYA_CLOUD_PID_BY_NAME = pid_by_name
        log.info(f"[CloudDPS] Загружено mapping: by_id={len(by_id)}, by_name={len(by_name)}, "
                 f"writable: {len(wr_by_id)}, product_id: {len(pid_by_id)}")
    except Exception as e:
        # v1.28.27: сбрасываем индексы — иначе после порчи файла
        # в памяти остаются старые данные.
        log.warning(f"[CloudDPS] load failed: {e}")
        with TUYA_CLOUD_MAPPING_LOCK:
            TUYA_CLOUD_MAPPING_BY_ID = {}
            TUYA_CLOUD_MAPPING_BY_NAME = {}
            TUYA_CLOUD_WRITABLE_BY_ID = {}
            TUYA_CLOUD_WRITABLE_BY_NAME = {}
            TUYA_CLOUD_PID_BY_ID = {}
            TUYA_CLOUD_PID_BY_NAME = {}


def _get_cloud_product_id(tuya_id, friendly_name):
    """v1.28.62: product_id из Cloud-кэша (fallback, если в конфиге нет)."""
    with TUYA_CLOUD_MAPPING_LOCK:
        if tuya_id and tuya_id in TUYA_CLOUD_PID_BY_ID:
            return TUYA_CLOUD_PID_BY_ID[tuya_id]
        if friendly_name and friendly_name in TUYA_CLOUD_PID_BY_NAME:
            return TUYA_CLOUD_PID_BY_NAME[friendly_name]
    return ""


def _get_cloud_mapping(tuya_id, friendly_name):
    """Достать Cloud-mapping для устройства по tuya_id или friendly_name."""
    with TUYA_CLOUD_MAPPING_LOCK:
        if tuya_id and tuya_id in TUYA_CLOUD_MAPPING_BY_ID:
            return TUYA_CLOUD_MAPPING_BY_ID[tuya_id]
        if friendly_name and friendly_name in TUYA_CLOUD_MAPPING_BY_NAME:
            return TUYA_CLOUD_MAPPING_BY_NAME[friendly_name]
    return {}


def _get_cloud_writable(tuya_id, friendly_name):
    """v1.28.34: code Cloud functions (записываемые DP) или None."""
    with TUYA_CLOUD_MAPPING_LOCK:
        if tuya_id and tuya_id in TUYA_CLOUD_WRITABLE_BY_ID:
            return TUYA_CLOUD_WRITABLE_BY_ID[tuya_id]
        if friendly_name and friendly_name in TUYA_CLOUD_WRITABLE_BY_NAME:
            return TUYA_CLOUD_WRITABLE_BY_NAME[friendly_name]
    return None


# v1.28.33.fixup3: датчики — всегда component=sensor, даже если
# Cloud вернул Integer + min/max. Number-input — только для
# сервисных параметров (уставки, чувствительность и т.п.).
_SENSOR_CODES = frozenset([
    # температура / влажность (текущие значения)
    "va_temperature", "va_humidity", "temp_current", "humidity",
    "temp_current_f", "upper_temp", "upper_temp_f",
    # батарея
    "battery_percentage", "battery_state", "battery_value", "va_battery",
    # энергометрия
    "cur_voltage", "cur_current", "cur_power",
    "output_power", "output_voltage", "output_current",
    "leakage_current", "supply_frequency", "power_factor",
    "add_ele", "total_forward_energy", "forward_energy_total",
    "reverse_energy_total", "electric_total",
    "balance_energy", "charge_energy",
    # прочее
    "signal_strength", "illuminance_value", "illuminance",
])


def _is_sensor_code(code):
    """v1.28.33.fixup3: True, если DP — датчик (не number-input)."""
    if not code:
        return False
    c = code.lower()
    if c in _SENSOR_CODES:
        return True
    # Эвристика по суффиксам (на случай нестандартных code).
    # НО: temp_set, maxhum_set, minitemp_set — НЕ датчики.
    if c.endswith("_set") or c.endswith("_sensitivity"):
        return False
    if c.startswith("va_") or c.startswith("cur_"):
        return True
    return False


# v1.28.34: единое правило component для Cloud-DP (вариант C:
# Cloud functions = записываемые DP, status = только чтение).
# Используется импортом (mapping_to_dps_map), карточкой устройства
# (_enrich_dps_map_from_cache) и JS-фолбэком (_dpsCompFromCloudType) —
# Component больше не расходится между экранами.
_BOOL_BINARY_CODES = {
    "doorcontact_state": "door",
    "pir": "motion",
    "watersensor_state": "moisture",
    "fault": "problem",
    "problema": "problem",
}
_ENUM_BINARY_CODES = {"watersensor_state": "moisture"}
_ENUM_SENSOR_CODES = {"battery_state"}
_CLIMATE_PRESET_CODES = {"mode", "preset_mode"}


def _writable_codes_from_props(raw_properties):
    """Множество code из Cloud `functions` (записываемые DP).

    None, если данных о functions нет — тогда вызывающий использует
    фолбэк (min/max). См. cloud_dp_component.
    """
    if not isinstance(raw_properties, dict):
        return None
    funcs = raw_properties.get("functions")
    if not isinstance(funcs, list):
        return None
    out = set()
    for f in funcs:
        if isinstance(f, dict) and f.get("code"):
            out.add(f["code"])
    return out


def cloud_dp_component(dtype, code, writable=None, dev_type=""):
    """Component для Cloud-DP по единому правилу (вариант C).

    Boolean → binary_sensor (спецкоды) / switch.
    Enum    → preset (climate mode/preset_mode) / select
              (battery_state → sensor, watersensor_state → binary_sensor).
    Integer → number, если DP записываемый (Cloud functions) и не датчик,
              иначе sensor.
    String/Json/Raw/Bitmap → sensor.

    writable — True/False (есть ли DP в Cloud functions) или None, если
    данных нет (тогда вызывающий сам решает фолбэк).
    """
    if dtype == "Boolean":
        return "binary_sensor" if code in _BOOL_BINARY_CODES else "switch"
    if dtype == "Enum":
        if dev_type == "climate" and code in _CLIMATE_PRESET_CODES:
            return "preset"
        if code in _ENUM_SENSOR_CODES:
            return "sensor"
        if code in _ENUM_BINARY_CODES:
            return "binary_sensor"
        return "select"
    if dtype == "Integer":
        # light/climate: числовые DP — часть составной сущности
        # (светимость, уставка), не отдельный number-input.
        if writable and not _is_sensor_code(code) and dev_type not in ("light", "climate"):
            return "number"
        return "sensor"
    return "sensor"


# v1.28.53: эвристика device_class/unit/state_class по code — синхронно с JS
# _dpsHeuristicMeta. device_class — только из белого списка bridge.
def heuristic_device_meta(code):
    out = {}
    if not code:
        return out
    c = str(code).lower()
    if "temp" in c:
        out["device_class"] = "temperature"
        out["unit"] = "°F" if (c.endswith("_f") or "temp_f" in c) else "°C"
        out["state_class"] = "measurement"
    elif "humidity" in c or c == "va_humidity":
        out["device_class"] = "humidity"; out["unit"] = "%"; out["state_class"] = "measurement"
    elif (c.startswith("battery") or c == "va_battery"
          or ("battery" in c and not re.search(
              r"no_battery|battery_off|battery_mode|battery_state", c))):
        out["device_class"] = "battery"; out["unit"] = "%"; out["state_class"] = "measurement"
    elif c == "power_factor":
        out["device_class"] = "power_factor"; out["state_class"] = "measurement"
    elif ("energy" in c or c in ("add_ele", "cur_consumption", "balance_energy",
                                 "charge_energy", "total_forward_energy")):
        out["device_class"] = "energy"; out["unit"] = "kWh"
        out["state_class"] = ("total_increasing"
                              if any(x in c for x in ("total", "forward", "add")) else "total")
    elif "voltage" in c:
        out["device_class"] = "voltage"; out["unit"] = "V"; out["state_class"] = "measurement"
    elif "current" in c:
        out["device_class"] = "current"
        out["unit"] = "mA" if "leakage" in c else ("A" if "output" in c else "mA")
        out["state_class"] = "measurement"
    elif "power" in c:
        out["device_class"] = "power"; out["unit"] = "kW" if "output" in c else "W"
        out["state_class"] = "measurement"
    elif "frequency" in c:
        out["device_class"] = "frequency"; out["unit"] = "Hz"; out["state_class"] = "measurement"
    elif "pressure" in c:
        out["device_class"] = "pressure"; out["state_class"] = "measurement"
    elif "illuminance" in c or "lux" in c:
        out["device_class"] = "illuminance"; out["unit"] = "lx"; out["state_class"] = "measurement"
    elif "signal" in c:
        out["device_class"] = "signal_strength"; out["unit"] = "dBm"; out["state_class"] = "measurement"
    elif "co2" in c:
        out["unit"] = "ppm"; out["state_class"] = "measurement"
    elif "pm2" in c or "pm10" in c:
        out["unit"] = "µg/m³"; out["state_class"] = "measurement"
    elif "soil" in c:
        out["device_class"] = "moisture"; out["unit"] = "%"; out["state_class"] = "measurement"
    return out


# v1.28.33.fixup5b: эвристика подсматривает в похожие устройства.
# Если новое устройство имеет product_id, и в конфиге уже есть
# устройство с тем же product_id — берём его dps_map как шаблон.
# Это надёжнее, чем угадывать по code.
def _find_similar_by_product_id(product_id, exclude_name=""):
    """Ищет dps_map устройства с тем же product_id.
    Возвращает (dps_map, source_name) или (None, None)."""
    if not product_id:
        return None, None
    # 1. Из конфига (DEVICE_META).
    try:
        with DEVICE_META_LOCK:
            _snap = dict(DEVICE_META)
    except Exception:
        _snap = {}
    for name, meta in _snap.items():
        if name == exclude_name:
            continue
        if meta.get("product_id") == product_id:
            dm = meta.get("dps_map") or {}
            if dm:
                return dm, name
    # 2. Из Cloud-cache (v1.28.34: через load_cloud_cache — под lock.
    # Прямое чтение файла гонялось с save_cloud_cache: на Windows
    # os.replace падал, пока файл открыт на чтение).
    try:
        data = load_cloud_cache()
        for d in ((data or {}).get("devices") or []):
            if not isinstance(d, dict) or d.get("product_id") != product_id:
                continue
            m = d.get("mapping") or {}
            if isinstance(m, dict) and m:
                return mapping_to_dps_map(m, d.get("category", "")), d.get("name", "")
    except Exception as e:
        log.debug(f"[Similar] cloud-cache lookup: {e}")
    return None, None


def _cloud_entry(dp_str, m, writable, dev_type):
    """Собрать запись dps_map из Cloud-mapping (DP dict m). None если нет code."""
    if not isinstance(m, dict):
        return None
    code = (m.get("code") or "").strip()
    if not code:
        return None
    dtype = m.get("type", "")
    entry = {
        "component": "sensor",
        "name": code,
        "_name_source": "cloud",
        "_from_cache": True,
        "_dps_source": "cloud",
        "_dps_source_reason": f"code={code}, type={dtype}",
    }
    if dtype:
        entry["_cloud_type"] = dtype
    vals = m.get("values", {})
    if isinstance(vals, dict):
        for k in ("unit", "scale", "min", "max", "step"):
            if k in vals:
                entry[k] = vals[k]
        _rng = vals.get("range")
        if isinstance(_rng, list) and _rng:
            entry["options"] = list(_rng)
    _writable_fallback = isinstance(vals, dict) and "min" in vals and "max" in vals
    entry["component"] = cloud_dp_component(
        dtype, code,
        writable=(writable if writable is not None else _writable_fallback),
        dev_type=dev_type,
    )
    if entry["component"] == "binary_sensor" and code in _BOOL_BINARY_CODES:
        entry.setdefault("device_class", _BOOL_BINARY_CODES[code])
    # device_class/unit/state_class из эвристики, если Cloud не дал.
    _heu = heuristic_device_meta(code)
    _guessed = []
    for _k, _v in _heu.items():
        if _k not in entry:
            entry[_k] = _v
            _guessed.append(_k)
    if _guessed:
        entry["_meta_guess"] = _guessed
    return entry


def _dp_candidates_for(candidates, dp):
    """Кандидаты записи для конкретного DP: {source: entry} (или None).

    v1.28.61: отдаём даже одного кандидата — JS добавит «эвристику» и
    получится выбор минимум из двух источников.
    """
    res = {}
    for src, m in candidates.items():
        if dp in m and isinstance(m[dp], dict):
            res[src] = m[dp]
    return res or None


# v1.30.0: локальная база мэппингов (tinytuya_devices.json) — кэш по mtime.
_LOCALDB_CACHE = {"mtime": None, "devices": {}}
_LOCALDB_LOCK = threading.Lock()


def _get_localdb_mapping(tuya_id):
    """Mapping устройства из локальной базы (tinytuya_devices.json).

    База собирается вручную (Инструменты → Пересборка): облачный mapping
    через tinytuya + локальный probe. Используется как источник, когда
    Cloud и tuya-local ничего не дали.
    """
    if not tuya_id:
        return {}
    try:
        with _LOCALDB_LOCK:
            mtime = (os.path.getmtime(TINYTUYA_DEVICES_FILE)
                     if os.path.exists(TINYTUYA_DEVICES_FILE) else None)
            if _LOCALDB_CACHE["mtime"] != mtime:
                _LOCALDB_CACHE["devices"] = load_tinytuya_devices_json()
                _LOCALDB_CACHE["mtime"] = mtime
            data = _LOCALDB_CACHE["devices"]
    except Exception as e:
        log.debug(f"[LocalDB] {tuya_id}: {e}")
        return {}
    dev = data.get(tuya_id)
    if not isinstance(dev, dict):
        return {}
    m = dev.get("mapping")
    if not isinstance(m, dict):
        return {}
    return {str(k): (dict(v) if isinstance(v, dict) else v) for k, v in m.items()}


def _enrich_dps_map_from_cache(dps_map, cache, tuya_id, friendly_name, product_id="",
                               exclude_name="", dev_type="", writable=None):
    """Дополнить dps_map DP из Cloud/tuya-local/similar + кандидаты источников.

    Приоритет: cloud → tuya_local → similar. Для DP, доступного более чем из
    одного источника, кладём `_dps_candidates` (UI даст выбрать источник).
    Возвращает НОВЫЙ dict (не мутирует входной).
    """
    if cache is None or not isinstance(cache, dict):
        cache = {}
    cloud_map = _get_cloud_mapping(tuya_id, friendly_name)
    # v1.28.62: если в конфиге нет product_id — берём из Cloud-кэша
    # (иначе tuya-local lookup не сработает).
    if not product_id:
        product_id = _get_cloud_product_id(tuya_id, friendly_name)

    # --- кандидаты по источникам ---
    candidates = {}
    if cloud_map:
        cnd = {}
        for _dp, _m in cloud_map.items():
            e = _cloud_entry(str(_dp), _m, writable, dev_type)
            if e:
                cnd[str(_dp)] = e
        if cnd:
            candidates["cloud"] = cnd
    tl_map = lookup_tuya_local(product_id) if product_id else None
    if tl_map:
        candidates["tuya_local"] = {
            str(k): (dict(v) if isinstance(v, dict) else v) for k, v in tl_map.items()
        }
    sim_map, sim_name = None, None
    if product_id and "cloud" not in candidates:
        sim_map, sim_name = _find_similar_by_product_id(product_id, exclude_name)
        if sim_map:
            candidates["similar"] = {
                str(k): (dict(v) if isinstance(v, dict) else v) for k, v in sim_map.items()
            }
    # v1.30.0: локальная база мэппингов (tinytuya_devices.json). Записи базы по
    # форме совпадают с облачными (code/type/values/name), поэтому component и
    # мету выводим тем же конвертером, что и для Cloud, а источник помечаем.
    ldb_map = _get_localdb_mapping(tuya_id)
    if ldb_map:
        _ldb_cnd = {}
        for _dp, _m in ldb_map.items():
            e = _cloud_entry(str(_dp), _m, writable, dev_type)
            if not e:
                continue
            e["_dps_source"] = "local_db"
            e["_name_source"] = "local_db"
            e["_dps_source_reason"] = "взято из локальной базы (tinytuya_devices.json)"
            _ldb_cnd[str(_dp)] = e
        if _ldb_cnd:
            candidates["local_db"] = _ldb_cnd

    # --- сборка результата по приоритету ---
    out = dict(dps_map or {})
    added = 0
    chosen = ("cloud" if "cloud" in candidates else
              "tuya_local" if "tuya_local" in candidates else
              "local_db" if "local_db" in candidates else
              "similar" if "similar" in candidates else None)
    if chosen:
        _reason = {
            "cloud": None,
            "tuya_local": f"Cloud молчал, product_id={product_id} найден в tuya-local",
            "local_db": "Cloud и tuya-local молчали — взято из локальной базы (tinytuya_devices.json)",
            "similar": f"product_id={product_id} совпал с '{sim_name}'",
        }[chosen]
        for dp_str, info in candidates[chosen].items():
            if dp_str in out:
                continue
            e = dict(info)
            e["_from_cache"] = True
            e["_dps_source"] = chosen
            if _reason:
                e["_dps_source_reason"] = _reason
            cand = _dp_candidates_for(candidates, dp_str)
            if cand:
                e["_dps_candidates"] = cand
            out[dp_str] = e
            added += 1
    if added:
        log.debug(f"[CloudDPS] {friendly_name}: +{added} DP из {chosen}")

    # --- аннотация существующих DP (тип/значения/кандидаты из Cloud) ---
    # v1.31.16: кандидаты нужны и когда облако молчит — иначе в «Кэше состояния»
    # нечего подставлять для DP без имени (локальная база / tuya-local / similar).
    if cloud_map or candidates:
        for _dp in list(out.keys()):
            _info = out.get(_dp)
            if not isinstance(_info, dict):
                continue
            _new = dict(_info)
            _changed = False
            if not _new.get("_cloud_type"):
                _m = cloud_map.get(_dp)
                if isinstance(_m, dict):
                    _dt = _m.get("type", "")
                    if _dt:
                        _new["_cloud_type"] = _dt
                        _changed = True
                    _vals = _m.get("values", {})
                    if isinstance(_vals, dict) and _vals and not _new.get("values"):
                        _new["values"] = dict(_vals)
                        _changed = True
            if not _new.get("_dps_candidates"):
                cand = _dp_candidates_for(candidates, _dp)
                if cand:
                    _new["_dps_candidates"] = cand
                    _changed = True
            if _changed:
                out[_dp] = _new
    return out


# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tuya-webui")

# v1.21.3: собственный файловый лог WebUI (logs/webui.log).
# Ротация как у Bridge: 5 МБ × 2 бэкапа.
try:
    import logging.handlers as _lh
    _webui_log_dir = os.path.dirname(LOG_FILE_WEBUI)
    if _webui_log_dir:
        os.makedirs(_webui_log_dir, exist_ok=True)
    _webui_file_handler = _lh.RotatingFileHandler(
        LOG_FILE_WEBUI,
        maxBytes=LOG_FILE_MAX_BYTES_WEBUI,
        backupCount=LOG_FILE_BACKUPS_WEBUI,
        encoding="utf-8",
    )
    _webui_file_handler.setLevel(logging.DEBUG)
    _webui_file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(_webui_file_handler)
except Exception as _e:
    log.warning(f"[Log] Не удалось открыть {LOG_FILE_WEBUI}: {_e}")


LOG_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")


def parse_log_line_timestamp(line):
    m = LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        import calendar
        return calendar.timegm((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                int(m.group(4)), int(m.group(5)), int(m.group(6))))
    except Exception:
        return None


# ==================== STATE ====================
STATE = {"bridge_status": "unknown", "uptime": 0, "version": "?", "devices": {},
         "bridge_started_at": 0,
         # v1.28.3: CPU/RAM bridge (публикует bridge 1.9.3+)
         "bridge_cpu_pct": None, "bridge_rss_mb": None,
         # v1.28.27: режим пинга bridge (для warning про CAP_NET_RAW).
         "bridge_ping_mode": None,
         # v1.33.8: отклик на команду + окно защиты от «эха» (bridge 1.12.19+).
         "bridge_cmd_ack": None}
STATE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()

DEVICE_META = {}
DEVICE_META_LOCK = threading.Lock()

PENDING_REQUESTS = {}
PENDING_LOCK = threading.Lock()

REBUILD_STATE = {
    "running": False, "current": 0, "total": 0, "device": "",
    "errors": [], "started_at": 0, "finished_at": 0, "ok": None,
    # v1.31.0: отчёт качества сопоставления (verified/mismatch/ambiguous + fetched_at)
    "report": None,
}
REBUILD_LOCK = threading.Lock()
# v1.32.2: расширенный скан — только один за раз (каждый вызов плодит пул потоков).
SCAN_EXTENDED_LOCK = threading.Lock()
SCAN_EXTENDED_STATE = {"running": False}

LATENCY_REFRESH_STATE = {
    "running": False, "current": 0, "total": 0, "device": "",
    "started_at": 0, "finished_at": 0, "ok": None,
}
LATENCY_REFRESH_STATE_LOCK = threading.Lock()

# v1.28.65: прогресс обновления tuya-local (2 фазы: download → import).
TUYA_LOCAL_STATE = {
    "running": False, "phase": "", "current": 0, "total": 0,
    "message": "", "started_at": 0, "finished_at": 0, "ok": None, "error": "",
    "mb_done": 0,   # v1.32.28: сколько МБ уже скачано (фаза 1/2)
    "mb_total": 0,  # v1.33.3: ожидаемый размер (Content-Length), 0 — неизвестен
    # v1.33.9: номер попытки скачивания (чтобы «МБ» не выглядели зависшими)
    # и отчёт по завершении (карточки в UI).
    "attempt": 0, "attempts": 0,
    "dl_stats": None,
    "report": None,
}
TUYA_LOCAL_STATE_LOCK = threading.Lock()


QUIET_LOCK = threading.Lock()
# v1.31.19: отдельный лок для read-modify-write правок тихих часов —
# quiet_save() сам берёт QUIET_LOCK, поэтому вложенный захват недопустим.
QUIET_EDIT_LOCK = threading.Lock()
QUIET_CONFIG = {}  # {name: {"windows": [{"from": "HH:MM", "to": "HH:MM"}, ...]}}


def quiet_windows_of(name):
    """v1.33.18: окна тишины устройства — читаем под локом.

    Раньше /api/status брал QUIET_CONFIG без блокировки, а quiet_save()
    пересобирает словарь под QUIET_LOCK — чтение могло поймать半 состояние.
    """
    with QUIET_LOCK:
        return list((QUIET_CONFIG.get(name, {}) or {}).get("windows", []))




def _check_tz_for_quiet():
    """P2 1.22.0: quiet hours привязаны к локальному времени контейнера."""
    try:
        import datetime as _dt
        now_dt = _dt.datetime.now().astimezone()
        tzname = now_dt.tzname() or "?"
        offset = now_dt.utcoffset()
        off_min = int(offset.total_seconds() // 60) if offset else 0
        off_h = off_min // 60
        off_m = abs(off_min % 60)
        off_str = f"UTC{off_h:+d}" + (f":{off_m:02d}" if off_m else "")
        log.info(f"[Quiet] TZ контейнера: {tzname} ({off_str})")
        # v1.25.12: ложное срабатывание для Europe/London зимой
        # (tzname="GMT", off=0). Считаем UTC только если tzname
        # явно UTC/GMT и НЕ задан TZ через env.
        env_tz = os.environ.get("TZ", "")
        if off_h == 0 and off_m == 0 and not env_tz and tzname.upper() in ("UTC", "GMT"):
            log.warning("[Quiet] TZ=UTC — часовой пояс контейнера не задан (TZ в .env)")
    except Exception as e:
        log.warning(f"[Quiet] TZ check: {e}")

def _quiet_parse_hm(s):
    """'HH:MM' → минуты от полуночи, иначе None."""
    if not isinstance(s, str):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def _quiet_window_contains(win, now_minutes):
    f_ = _quiet_parse_hm(win.get("from"))
    t_ = _quiet_parse_hm(win.get("to"))
    if f_ is None or t_ is None or f_ == t_:
        return False
    if f_ < t_:
        return f_ <= now_minutes < t_
    return now_minutes >= f_ or now_minutes < t_


def _quiet_now_minutes():
    import datetime as _dt
    now = _dt.datetime.now()
    return now.hour * 60 + now.minute


def is_quiet_now(name):
    """True, если устройство сейчас в окне тишины."""
    with QUIET_LOCK:
        cfg = QUIET_CONFIG.get(name)
    if not cfg:
        return False
    wins = cfg.get("windows") or []
    if not wins:
        return False
    nm = _quiet_now_minutes()
    return any(_quiet_window_contains(w, nm) for w in wins)


def _should_ping(name):
    """
    v1.28.2: нужно ли измерять latency для устройства.

    False для:
      - enabled: false (disabled)
      - battery_powered: true (батарейные спят)
      - в окне тишины (quiet hours)

    Используется в _do_latency_round и /api/status.
    """
    m = get_device_meta(name)
    if m:
        if m.get("enabled", True) is False:
            return False
        if m.get("battery_powered"):
            return False
    if is_quiet_now(name):
        return False
    return True


def _ping_hidden_reason(name):
    """v1.28.2: причина, почему latency не измеряется. Или None."""
    m = get_device_meta(name) or {}
    if m.get("enabled", True) is False:
        return "disabled"
    if m.get("battery_powered"):
        return "battery"
    if is_quiet_now(name):
        return "quiet"
    return None


def quiet_until_ts(name):
    """Если сейчас quiet — unix ts окончания текущего окна.
    Если только что вышли (в пределах QUIET_GRACE_SEC) — ts окончания + grace.
    Иначе 0.
    """
    with QUIET_LOCK:
        cfg = QUIET_CONFIG.get(name)
    if not cfg:
        return 0
    wins = cfg.get("windows") or []
    if not wins:
        return 0
    nm = _quiet_now_minutes()
    now = time.time()
    # v1.25.12: сначала ищем окно, в котором мы СЕЙЧАС (is_quiet),
    # возвращаем его end. Это правильнее, чем брать первое попавшееся:
    # при пересечении окон (напр. 23:00-08:00 и 08:00-09:00)
    # _quiet_window_contains матчит оба, а мы должны вернуть то,
    # которое реально определяет текущий quiet.
    for w in wins:
        if _quiet_window_contains(w, nm):
            t_ = _quiet_parse_hm(w.get("to"))
            if t_ is None:
                continue
            delta_min = (t_ - nm) % (24 * 60)
            return int(now + delta_min * 60)
    # Не в окне — ищем окно, из которого только что вышли (grace).
    grace_min = max(1, QUIET_GRACE_SEC // 60)
    best_end = 0
    best_diff = None
    for w in wins:
        t_ = _quiet_parse_hm(w.get("to"))
        if t_ is None:
            continue
        diff = (nm - t_) % (24 * 60)
        if 0 <= diff <= grace_min:
            # выбираем окно с минимальной diff — оно самое «свежее».
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_end = t_
    if best_end:
        return int(now - best_diff * 60 + grace_min * 60)
    return 0


def is_quiet_or_grace_now(name):
    """v1.22.6: True, если сейчас quiet ИЛИ только что вышли
    из окна (< QUIET_GRACE_SEC назад). Нужно для db_insert_status,
    чтобы не писать «фантомные» переходы сразу после окна."""
    if is_quiet_now(name):
        return True
    ts_end = quiet_until_ts(name)
    if ts_end == 0:
        return False
    grace_min = max(1, QUIET_GRACE_SEC // 60)
    end_window = ts_end - grace_min * 60
    now = time.time()
    return 0 <= (now - end_window) < QUIET_GRACE_SEC


def quiet_load():
    """Загрузить webui_state/quiet_hours.json. Создать пустой, если нет."""
    global QUIET_CONFIG
    try:
        os.makedirs(os.path.dirname(QUIET_HOURS_FILE) or ".", exist_ok=True)
        if not os.path.exists(QUIET_HOURS_FILE):
            with open(QUIET_HOURS_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
            log.info(f"[Quiet] Создан пустой {QUIET_HOURS_FILE}")
            with QUIET_LOCK:
                QUIET_CONFIG = {}
            return
        with open(QUIET_HOURS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("[Quiet] Файл не dict — игнорируем")
            data = {}
        clean = {}
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            wins = cfg.get("windows")
            if not isinstance(wins, list):
                continue
            parsed = []
            for w in wins:
                if not isinstance(w, dict):
                    continue
                f_, t_ = w.get("from"), w.get("to")
                if _quiet_parse_hm(f_) is None or _quiet_parse_hm(t_) is None:
                    continue
                if f_ == t_:
                    continue
                parsed.append({"from": f_, "to": t_})
            if parsed:
                clean[name] = {"windows": parsed}
        with QUIET_LOCK:
            QUIET_CONFIG = clean
        log.info(f"[Quiet] Загружено {len(clean)} устройств из {QUIET_HOURS_FILE}")
    except Exception as e:
        log.warning(f"[Quiet] load: {e}")
        with QUIET_LOCK:
            QUIET_CONFIG = {}


def quiet_save(new_config):
    global QUIET_CONFIG
    try:
        os.makedirs(os.path.dirname(QUIET_HOURS_FILE) or ".", exist_ok=True)
        tmp = QUIET_HOURS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new_config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, QUIET_HOURS_FILE)
        with QUIET_LOCK:
            QUIET_CONFIG = dict(new_config)
        log.info(f"[Quiet] Сохранено {len(new_config)} устройств")
        quiet_publish_to_bridge()
        return True, None
    except Exception as e:
        return False, str(e)


def quiet_publish_to_bridge():
    """v1.32.6: отдать тихие часы мосту (retain).

    Мост по ним не шумит WARNING'ами про 905/offline: устройство в окне тишины
    выключено намеренно. Retain — чтобы конфиг дошёл и после перезапуска моста.
    """
    try:
        with QUIET_LOCK:
            cfg = {k: dict(v) for k, v in QUIET_CONFIG.items() if isinstance(v, dict)}
        _mqtt.publish(f"{TOPIC_PREFIX}/bridge/quiet_config",
                      json.dumps(cfg, ensure_ascii=False), qos=1, retain=True)
    except Exception as e:
        log.warning(f"[Quiet] публикация в bridge: {e}")


def load_device_meta():
    global DEVICE_META
    new_meta = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            devices = json.load(f)
        for d in devices:
            # v1.32.1: запись без name раньше роняла загрузку всей DEVICE_META
            # (KeyError ловился снаружи) — остальные устройства не обновлялись.
            _name = d.get("name") if isinstance(d, dict) else None
            if not _name:
                log.warning("[Meta] запись без name в конфиге — пропущена")
                continue
            # v1.27.7b: грузим ВСЕ устройства, включая enabled=false.
            # Фильтрация «кого опрашивать» — забота bridge (1.8.6+).
            # WebUI должен знать про отключённые, чтобы показать их
            # в серой секции «⛔ Отключённые» и отдать enabled в /api/status.
            meta = {
                "friendly_name": d.get("friendly_name", _name),
                "type": d.get("type", "unknown"),
                "model": d.get("model", ""),
                "ip": d.get("ip", ""),
                "version": d.get("version", ""),
                "battery_powered": d.get("battery_powered", False),
                "enabled": d.get("enabled", True),
                "tuya_id": d.get("id", ""),
                "local_key": d.get("local_key", ""),
                "product_id": d.get("product_id", ""),  # v1.28.33.fixup3
                # v1.30.1: expire_after — нужен ✏️ (показывать текущее значение
                # и «По умолчанию» только когда поле реально есть в конфиге).
                "expire_after": d.get("expire_after"),
                "dps_map": copy.deepcopy(d.get("dps_map", {})),
            }
            if d.get("type") == "climate":
                meta["presets"] = d.get("presets", [])
                meta["preset_map"] = d.get("preset_map", {})
                meta["min_temp"] = d.get("min_temp")
                meta["max_temp"] = d.get("max_temp")
                meta["temp_step"] = d.get("temp_step")
            # v1.28.67: 6_voltage/6_current/6_power НЕ добавляем в dps_map —
            # bridge сам публикует их как phase_a/voltage|current|power и
            # менять их нельзя (это производные от DP 6 = phase_a).
            new_meta[_name] = meta
        with DEVICE_META_LOCK:
            DEVICE_META = new_meta
        log.info(f"[Config] Загружено {len(new_meta)} устройств из {CONFIG_FILE}")
    except Exception as e:
        log.warning(f"[Config] Не удалось загрузить {CONFIG_FILE}: {e}")


def get_device_meta(name):
    with DEVICE_META_LOCK:
        return DEVICE_META.get(name)


def snapshot_device_meta():
    with DEVICE_META_LOCK:
        return dict(DEVICE_META)


def get_known_ips():
    with DEVICE_META_LOCK:
        # v1.27.7b: политика DISABLED_BLOCK_IP_REUSE.
        if DISABLED_BLOCK_IP_REUSE:
            return {m.get("ip") for m in DEVICE_META.values()
                    if m.get("ip") and m.get("enabled", True) is not False}
        return {m.get("ip") for m in DEVICE_META.values() if m.get("ip")}


def _humanize_bridge_error(err):
    if not err:
        return "Неизвестная ошибка"
    e = str(err)
    low = e.lower()
    if "914" in e or "check device key or version" in low:
        # v1.28.27: 914 может означать и «занято», и «неверный key/version».
        # Раньше сообщение категорично утверждало «занято bridge».
        return ("⚠️ Ошибка 914: устройство занято (уже подключено к bridge) "
                "ИЛИ неверный local_key/version. Проверь, что bridge не "
                "опрашивает это устройство, и сверь key/version в Tuya IoT "
                "Platform. Изменения сохранены в конфиг и применятся при "
                "следующем reconnect.")
    if "timeout" in low:
        return "⏱ Bridge не ответил за отведённое время. Проверь, что tuya-bridge запущен."
    if "not found" in low:
        return "Устройство не найдено в конфиге bridge."
    if "permission" in low or "read-only" in low:
        return "🔒 Bridge не может записать devices_config.json (проверь права на volume)."
    return e


# ==================== SQLITE ====================
_db_lock = threading.Lock()
_db_conn = None
_status_buffer = []
# v1.28.11: _state_buffer / _last_snapshot / _last_snapshot_lock удалены
# (были для db_insert_snapshot, который не вызывался).
_latency_buffer = []


def db_init():
    global _db_conn
    if not ANALYTICS_ENABLED and not STATUS_HISTORY_ENABLED:
        log.info("[DB] Всё отключено — SQLite не создаётся")
        return
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    _db_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA synchronous=NORMAL")
    _db_conn.execute("PRAGMA temp_store=MEMORY")
    with _db_conn:
        if STATUS_HISTORY_ENABLED:
            _db_conn.execute("""CREATE TABLE IF NOT EXISTS status_events (
                ts INTEGER NOT NULL, dev TEXT NOT NULL, status TEXT NOT NULL)""")
            _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_status_ts ON status_events(ts)")
            _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_status_dev ON status_events(dev)")
            _db_conn.execute("""CREATE TABLE IF NOT EXISTS latency_history (
                ts INTEGER NOT NULL, dev TEXT NOT NULL, ms INTEGER)""")
            _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_latency_ts ON latency_history(ts)")
            _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_latency_dev_ts ON latency_history(dev, ts)")
        if ANALYTICS_ENABLED:
            # v1.28.11: CREATE TABLE state_history удалён. Таблица не
            # создаётся, но если уже есть в БД (со старых версий) —
            # не удаляем. Пользователь может почистить через SQL.
            _db_conn.execute("""CREATE TABLE IF NOT EXISTS hourly_online_count (
                hour_ts INTEGER PRIMARY KEY, online INTEGER NOT NULL, total INTEGER NOT NULL)""")
    log.info(f"[DB] SQLite: {DB_FILE}")


def db_insert_status(ts, dev, status):
    if not STATUS_HISTORY_ENABLED: return
    with _db_lock: _status_buffer.append((ts, dev, status))


# v1.28.11: db_insert_snapshot удалён — функция не вызывалась
# с v1.24.2. Таблица state_history не наполняется.
# Таблица в БД остаётся (не DROP) — на случай старых данных.


def db_insert_latency(dev, ms):
    if not STATUS_HISTORY_ENABLED: return
    now = int(time.time())
    with _db_lock: _latency_buffer.append((now, dev, ms))


def db_flush():
    if not STATUS_HISTORY_ENABLED and not ANALYTICS_ENABLED: return
    with _db_lock:
        if _db_conn is None: return
        try:
            with _db_conn:
                if STATUS_HISTORY_ENABLED and _status_buffer:
                    _db_conn.executemany("INSERT INTO status_events VALUES (?,?,?)", _status_buffer)
                    _status_buffer.clear()
                if STATUS_HISTORY_ENABLED and _latency_buffer:
                    _db_conn.executemany("INSERT INTO latency_history VALUES (?,?,?)", _latency_buffer)
                    _latency_buffer.clear()
        except Exception as e: log.warning(f"[DB] flush: {e}")


def db_cleanup_old():
    if not STATUS_HISTORY_ENABLED and not ANALYTICS_ENABLED: return
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with _db_lock:
        if _db_conn is None: return
        try:
            with _db_conn:
                if STATUS_HISTORY_ENABLED:
                    _db_conn.execute("DELETE FROM status_events WHERE ts < ?", (cutoff,))
                    _db_conn.execute("DELETE FROM latency_history WHERE ts < ?", (cutoff,))
        except Exception as e: log.warning(f"[DB] cleanup: {e}")


def db_cleanup_by_period(keep_seconds):
    if not STATUS_HISTORY_ENABLED and not ANALYTICS_ENABLED:
        return 0
    cutoff = int(time.time()) - keep_seconds
    n = 0
    with _db_lock:
        if _db_conn is None: return 0
        with _db_conn:
            if STATUS_HISTORY_ENABLED:
                cur = _db_conn.execute("DELETE FROM status_events WHERE ts < ?", (cutoff,))
                n += cur.rowcount
                cur = _db_conn.execute("DELETE FROM latency_history WHERE ts < ?", (cutoff,))
                n += cur.rowcount
    return n


def db_cleanup_timeline_only():
    if not STATUS_HISTORY_ENABLED: return 0
    with _db_lock:
        if _db_conn is None: return 0
        with _db_conn:
            cur = _db_conn.execute("DELETE FROM status_events")
            return cur.rowcount


def db_cleanup_timeline_before(before_ts):
    if not STATUS_HISTORY_ENABLED: return 0
    with _db_lock:
        if _db_conn is None: return 0
        with _db_conn:
            cur = _db_conn.execute("DELETE FROM status_events WHERE ts < ?", (int(before_ts),))
            return cur.rowcount


def db_vacuum():
    try:
        with _db_lock:
            if _db_conn is not None:
                _db_conn.execute("VACUUM")
        log.info("[DB] VACUUM выполнен")
    except Exception as e:
        log.warning(f"[DB] VACUUM: {e}")


def db_record_hourly():
    if not ANALYTICS_ENABLED: return
    # v1.28.9: исключаем батарейные и отключённые из total/online.
    # Иначе график «Online по часам» искажён: батарейные никогда
    # не online (спят), disabled не опрашиваются.
    meta_snap = snapshot_device_meta()
    _statuses = []
    with STATE_LOCK:
        for _n, _info in STATE["devices"].items():
            _m = meta_snap.get(_n, {})
            if _m.get("enabled", True) is False:
                continue
            if _m.get("battery_powered"):
                continue
            _statuses.append(_info.get("status"))
    total = len(_statuses)
    online = sum(1 for s in _statuses if s == "online")
    now = int(time.time()); hour_ts = now - (now % 3600)
    with _db_lock:
        if _db_conn is None: return
        try:
            with _db_conn:
                _db_conn.execute("""INSERT INTO hourly_online_count VALUES (?,?,?)
                    ON CONFLICT(hour_ts) DO UPDATE SET online=excluded.online, total=excluded.total""",
                    (hour_ts, online, total))
        except Exception as e: log.warning(f"[DB] hourly: {e}")


def db_worker():
    if not STATUS_HISTORY_ENABLED and not ANALYTICS_ENABLED: return
    last_flush = time.time(); last_cleanup = time.time(); last_hourly = time.time()
    while not STOP_EVENT.is_set():
        if STOP_EVENT.wait(5): break
        now = time.time()
        if now - last_flush >= FLUSH_INTERVAL:
            db_flush(); last_flush = now
        if now - last_cleanup >= 3600:
            db_cleanup_old(); last_cleanup = now
        if ANALYTICS_ENABLED and now - last_hourly >= HOURLY_INTERVAL:
            db_record_hourly(); last_hourly = now
    db_flush()


def db_query_hourly(period_hours=24):
    if not ANALYTICS_ENABLED: return []
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            cur = _db_conn.execute("SELECT hour_ts, online, total FROM hourly_online_count WHERE hour_ts>=? ORDER BY hour_ts", (cutoff,))
            return [{"ts": r[0], "online": r[1], "total": r[2]} for r in cur.fetchall()]
        except: return []


def db_query_timeline(period_hours=24, limit=500):
    if not ANALYTICS_ENABLED: return []
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            cur = _db_conn.execute("SELECT ts,dev,status FROM status_events WHERE ts>=? ORDER BY ts DESC LIMIT ?", (cutoff, limit))
            return [{"ts": r[0], "dev": r[1], "status": r[2]} for r in cur.fetchall()]
        except: return []


def db_query_timeline_total(period_hours=24, exclude_devs=None):
    """v1.28.19: exclude_devs — не считать устройства в quiet/disabled.
    Иначе UI показывал «показаны 50 из 2000», а в timeline после
    фильтра могло быть всего 300."""
    if not ANALYTICS_ENABLED: return 0
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            if exclude_devs:
                ex_list = tuple(exclude_devs)
                placeholders = ",".join(["?"] * len(ex_list))
                cur = _db_conn.execute(
                    f"SELECT COUNT(*) FROM status_events "
                    f"WHERE ts>=? AND dev NOT IN ({placeholders})",
                    (cutoff, *ex_list)
                )
            else:
                cur = _db_conn.execute(
                    "SELECT COUNT(*) FROM status_events WHERE ts>=?", (cutoff,))
            row = cur.fetchone()
            return row[0] if row else 0
        except: return 0


def db_query_flappers(period_hours=24, min_flaps=3):
    if not ANALYTICS_ENABLED: return []
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            cur = _db_conn.execute("SELECT dev, COUNT(*) FROM status_events WHERE ts>=? GROUP BY dev HAVING COUNT(*)>=? ORDER BY COUNT(*) DESC", (cutoff, min_flaps))
            return [{"dev": r[0], "flaps": r[1]} for r in cur.fetchall()]
        except: return []


def _db_query_flaps_hourly_excluding(period_hours, exclude_devs):
    """v1.22.3: переходы по часам, исключая указанные устройства (quiet-hours).
    v1.24.6: возвращаем и список устройств по каждому часу —
    devices: [{name, count}, ...]."""
    if not ANALYTICS_ENABLED:
        return []
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            if exclude_devs:
                # v1.25.13: явный tuple — детерминированный порядок
                # параметров (set давал произвольный).
                # v1.28.20: ["?"] * len — как в db_query_timeline_total.
                ex_list = tuple(exclude_devs)
                placeholders = ",".join(["?"] * len(ex_list))
                cur = _db_conn.execute(
                    f"SELECT (ts/3600)*3600 AS hour_ts, dev, COUNT(*) "
                    f"FROM status_events WHERE ts>=? AND dev NOT IN ({placeholders}) "
                    f"GROUP BY hour_ts, dev ORDER BY hour_ts",
                    (cutoff, *ex_list)
                )
            else:
                cur = _db_conn.execute(
                    "SELECT (ts/3600)*3600 AS hour_ts, dev, COUNT(*) "
                    "FROM status_events WHERE ts>=? "
                    "GROUP BY hour_ts, dev ORDER BY hour_ts",
                    (cutoff,)
                )
            by_hour = {}
            for hour_ts, dev, cnt in cur.fetchall():
                by_hour.setdefault(hour_ts, []).append({"name": dev, "count": cnt})
            out = []
            for hour_ts in sorted(by_hour.keys()):
                devs = sorted(by_hour[hour_ts],
                              key=lambda x: (-x["count"], x["name"]))
                out.append({
                    "ts": hour_ts,
                    "flaps": sum(d["count"] for d in devs),
                    "devices": devs,
                })
            return out
        except Exception:
            return []


def db_query_dev_history(dev, period_hours=24, limit=100):
    if not STATUS_HISTORY_ENABLED: return []
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            cur = _db_conn.execute("SELECT ts,status FROM status_events WHERE dev=? AND ts>=? ORDER BY ts DESC LIMIT ?", (dev, cutoff, limit))
            rows = [{"ts": r[0], "status": r[1]} for r in cur.fetchall()]
            return list(reversed(rows))
        except: return []


def db_query_dev_latency(dev, period_hours=24, limit=2000):
    if not STATUS_HISTORY_ENABLED: return []
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            cur = _db_conn.execute("SELECT ts,ms FROM latency_history WHERE dev=? AND ts>=? ORDER BY ts LIMIT ?", (dev, cutoff, limit))
            return [{"ts": r[0], "ms": r[1]} for r in cur.fetchall()]
        except: return []


def db_query_avg_latency(dev, period_seconds=86400):
    """v1.18.7: period_seconds — секунды. 0 или None → всё время."""
    if not STATUS_HISTORY_ENABLED: return None
    with _db_lock:
        try:
            if not period_seconds or period_seconds <= 0:
                cur = _db_conn.execute(
                    "SELECT AVG(ms), COUNT(*), SUM(CASE WHEN ms IS NULL THEN 1 ELSE 0 END) "
                    "FROM latency_history WHERE dev=?",
                    (dev,)
                )
            else:
                cutoff = int(time.time()) - int(period_seconds)
                cur = _db_conn.execute(
                    "SELECT AVG(ms), COUNT(*), SUM(CASE WHEN ms IS NULL THEN 1 ELSE 0 END) "
                    "FROM latency_history WHERE dev=? AND ts>=?",
                    (dev, cutoff)
                )
            row = cur.fetchone()
            if not row: return None
            avg, cnt, timeouts = row
            return {
                "avg": int(round(avg)) if avg is not None else None,
                "count": cnt or 0,
                "timeouts": timeouts or 0,
            }
        except: return None


# v1.33.24: перцентили задержки — медиана и 95-й для таблицы аналитики.
def _lat_pct(vals, p):
    """Перцентиль по ОТСОРТИРОВАННОМУ списку (линейная интерполяция).

    Без math: k = (n-1) * p/100, значение интерполируется между k и k+1.
    """
    n = len(vals)
    if n == 0: return None
    if n == 1: return float(vals[0])
    k = (n - 1) * p / 100.0
    lo = int(k)
    hi = lo + 1 if lo + 1 < n else lo
    if hi == lo: return float(vals[lo])
    frac = k - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def db_query_latency_stats(dev, period_seconds=86400):
    """v1.33.24: avg + медиана + p95 + max по latency_history за период.

    В SQLite нет MEDIAN/PERCENTILE, поэтому берём ms одного устройства и считаем
    в питоне. Это замена db_query_avg_latency в /api/analytics — число запросов
    не выросло (по-прежнему один на устройство), а полей стало больше.
    period_seconds: 0/None → всё время.
    """
    if not STATUS_HISTORY_ENABLED: return None
    with _db_lock:
        try:
            if not period_seconds or period_seconds <= 0:
                cur = _db_conn.execute(
                    "SELECT ms FROM latency_history WHERE dev=?", (dev,))
            else:
                cutoff = int(time.time()) - int(period_seconds)
                cur = _db_conn.execute(
                    "SELECT ms FROM latency_history WHERE dev=? AND ts>=?",
                    (dev, cutoff))
            rows = cur.fetchall()
        except Exception:
            return None
    vals = sorted(r[0] for r in rows if r[0] is not None)
    total = len(rows)
    if not vals:
        return {"avg": None, "median": None, "p95": None, "max": None,
                "count": total, "timeouts": total}
    return {
        "avg": int(round(sum(vals) / len(vals))),
        "median": int(round(_lat_pct(vals, 50))),
        "p95": int(round(_lat_pct(vals, 95))),
        "max": int(vals[-1]),
        "count": total,
        "timeouts": total - len(vals),
    }


# ==================== ICMP PING ====================
_PING_MODE = [None]


def _icmp_checksum(data):
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        w = (data[i] << 8) + data[i + 1]
        s += w
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def _ping_native(ip, timeout=LATENCY_PING_TIMEOUT):
    try:
        icmp_proto = _socket.getprotobyname("icmp")
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_RAW, icmp_proto)
    except (PermissionError, OSError):
        return None
    try:
        s.settimeout(timeout)
        pid = os.getpid() & 0xFFFF
        seq = 1
        # v1.28.9: big-endian (как в bridge 1.9.1). Раньше был
        # native byte order + лишний htons на checksum — на x86
        # checksum неверный, ответы не парсились.
        header = struct.pack("!BBHHH", 8, 0, 0, pid, seq)
        payload = struct.pack("!d", time.time())
        packet = header + payload
        chksum = _icmp_checksum(packet)
        header = struct.pack("!BBHHH", 8, 0, chksum, pid, seq)
        packet = header + payload
        t0 = time.time()
        s.sendto(packet, (ip, 0))
        deadline = t0 + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            r, _, _ = select.select([s], [], [], remaining)
            if not r:
                return None
            recv, addr = s.recvfrom(1024)
            if addr[0] != ip:
                continue
            if len(recv) >= 28:
                r_type = recv[20]
                r_id = struct.unpack("!H", recv[24:26])[0]
                if r_type == 0 and r_id == pid:
                    return int(round((time.time() - t0) * 1000))
    except Exception:
        return None
    finally:
        try: s.close()
        except: pass


def _ping_subprocess(ip, timeout=LATENCY_PING_TIMEOUT):
    try:
        result = subprocess.run([LATENCY_PING_COMMAND, "-c", "1", "-W", str(timeout), "-n", ip],
                                capture_output=True, text=True, timeout=timeout + 2)
        if result.returncode != 0: return None
        for line in result.stdout.splitlines():
            if "time=" in line:
                return int(round(float(line.split("time=")[1].split()[0])))
        return None
    except Exception:
        return None


def _detect_ping_mode():
    r = _ping_native("127.0.0.1", timeout=1)
    if r is not None:
        _PING_MODE[0] = "native"
        log.info("[Ping] Native ICMP доступен (CAP_NET_RAW)")
        return
    try:
        result = subprocess.run([LATENCY_PING_COMMAND, "-c", "1", "-W", "1", "-n", "127.0.0.1"],
                                capture_output=True, timeout=3)
        if result.returncode == 0:
            _PING_MODE[0] = "subprocess"
            log.info("[Ping] Native ICMP недоступен, используем subprocess ping")
            return
    except FileNotFoundError:
        pass
    _PING_MODE[0] = "none"
    log.error("[Ping] Ни native ICMP, ни subprocess ping не работают")


def measure_latency(ip):
    if _PING_MODE[0] is None:
        _detect_ping_mode()
    if _PING_MODE[0] == "native":
        return _ping_native(ip)
    if _PING_MODE[0] == "subprocess":
        return _ping_subprocess(ip)
    return None


def measure_latency_avg(ip, samples=None):
    """v1.33.6: среднее из `samples` ICMP-проб (по умолчанию 5).

    Один замер часто врёт (например 213 мс вместо типичных 5-15 мс), из-за
    чего устройство «выглядит за 4000 км». Считаем среднее по успешным
    пробам, как `ping` в Windows. Все пробы провалились → None.
    """
    n = int(samples or LATENCY_PING_SAMPLES)
    vals = []
    for _ in range(max(1, n)):
        ms = measure_latency(ip)
        if ms is not None:
            vals.append(ms)
    if not vals:
        return None
    return int(round(sum(vals) / len(vals)))


# ==================== LATENCY WORKER ====================
def _do_latency_round():
    with STATE_LOCK:
        devices = list(STATE["devices"].keys())
    # v1.28.2: единый хелпер — skip disabled/battery/quiet.
    # Заменяет старые проверки is_quiet_now + DISABLED_SKIP_LATENCY_PING
    # + battery.
    devices = [d for d in devices if _should_ping(d)]
    meta_snap = snapshot_device_meta()
    if not devices:
        # v1.25.13: не оставляем UI в состоянии «running» —
        # иначе прогресс-бар висит 0/0 до конца тика.
        with LATENCY_REFRESH_STATE_LOCK:
            LATENCY_REFRESH_STATE["running"] = False
            LATENCY_REFRESH_STATE["current"] = 0
            LATENCY_REFRESH_STATE["total"] = 0
            LATENCY_REFRESH_STATE["device"] = ""
            LATENCY_REFRESH_STATE["finished_at"] = int(time.time())
            LATENCY_REFRESH_STATE["ok"] = True
        return

    with LATENCY_REFRESH_STATE_LOCK:
        if LATENCY_REFRESH_STATE["running"]:
            LATENCY_REFRESH_STATE["total"] = len(devices)
            LATENCY_REFRESH_STATE["current"] = 0

    # Шаг 1: быстрый проход
    first_pass = {}
    timeouts = []
    done = 0
    for dev in devices:
        if STOP_EVENT.is_set(): break
        meta = meta_snap.get(dev, {}); ip = meta.get("ip")
        if not ip:
            first_pass[dev] = None
            done += 1
            continue
        ms = measure_latency_avg(ip)
        first_pass[dev] = ms
        if ms is None:
            timeouts.append((dev, ip))
        done += 1
        with LATENCY_REFRESH_STATE_LOCK:
            if LATENCY_REFRESH_STATE["running"]:
                LATENCY_REFRESH_STATE["current"] = done
                LATENCY_REFRESH_STATE["device"] = dev
        if STOP_EVENT.wait(0.02): break

    log.info(f"[Latency] Быстрый проход: {len(devices)} устройств, "
             f"{len(timeouts)} timeout → retry")

    # Шаг 2: параллельный retry
    retry_results = {}

    def retry_one(dev, ip):
        for attempt in range(LATENCY_RETRY_COUNT - 1):
            if STOP_EVENT.wait(LATENCY_RETRY_DELAY):
                return dev, None
            ms = measure_latency_avg(ip)
            if ms is not None:
                log.info(f"[Ping] {dev} ({ip}): успех с retry #{attempt+1}")
                return dev, ms
        return dev, None

    if timeouts:
        with ThreadPoolExecutor(max_workers=LATENCY_RETRY_WORKERS,
                                 thread_name_prefix="ping-retry") as pool:
            futures = {pool.submit(retry_one, dev, ip): dev for dev, ip in timeouts}
            for fut in as_completed(futures):
                try:
                    dev, ms = fut.result()
                    retry_results[dev] = ms
                except Exception as e:
                    log.warning(f"[Latency] retry error: {e}")

    # Шаг 3: финальные результаты
    measured = 0; failed = 0
    now = int(time.time())
    with STATE_LOCK:
        for dev in devices:
            ms = first_pass.get(dev)
            if ms is None and dev in retry_results:
                ms = retry_results[dev]
            if dev in STATE["devices"]:
                STATE["devices"][dev]["latency_ms"] = ms
                STATE["devices"][dev]["latency_ts"] = now
            db_insert_latency(dev, ms)
            measured += 1
            if ms is None: failed += 1

    log.info(f"[Latency] Замер завершён: {measured} устройств, {failed} timeout "
             f"(после {LATENCY_RETRY_COUNT} попыток)")

    with LATENCY_REFRESH_STATE_LOCK:
        LATENCY_REFRESH_STATE["running"] = False
        LATENCY_REFRESH_STATE["finished_at"] = int(time.time())
        LATENCY_REFRESH_STATE["ok"] = True
        LATENCY_REFRESH_STATE["current"] = len(devices)



_LATENCY_REFRESH_LOCK = threading.Lock()
_LATENCY_REFRESH_RUNNING = [False]

def latency_worker():
    if _PING_MODE[0] is None:
        _detect_ping_mode()
    if _PING_MODE[0] == "none":
        log.error("[Latency] Нет доступного метода ping — воркер отключён")
        return
    log.info(f"[Latency] Воркер запущен (mode={_PING_MODE[0]}, "
             f"первый замер через {LATENCY_INITIAL_DELAY}с, далее раз в {LATENCY_INTERVAL}с, "
             f"retry: {LATENCY_RETRY_COUNT}×{LATENCY_RETRY_DELAY}с)")
    if STOP_EVENT.wait(LATENCY_INITIAL_DELAY): return
    while not STOP_EVENT.is_set():
        # v1.22.6: worker сам ставит _LATENCY_REFRESH_RUNNING
        # на время раунда — иначе ручной refresh во время планового
        # запускал ВТОРОЙ раунд параллельно (гонка в STATE и
        # двойные записи в latency_history).
        with _LATENCY_REFRESH_LOCK:
            if _LATENCY_REFRESH_RUNNING[0]:
                if STOP_EVENT.wait(LATENCY_INTERVAL): break
                continue
            _LATENCY_REFRESH_RUNNING[0] = True
        # v1.25.0 (fix #1): синхронизируем публичное состояние прогресса —
        # иначе _do_latency_round() не обновляет total/current/device
        # (там условие `if LATENCY_REFRESH_STATE["running"]:`).
        with LATENCY_REFRESH_STATE_LOCK:
            if not LATENCY_REFRESH_STATE["running"]:
                LATENCY_REFRESH_STATE["running"] = True
                LATENCY_REFRESH_STATE["current"] = 0
                LATENCY_REFRESH_STATE["total"] = 0
                LATENCY_REFRESH_STATE["device"] = ""
                LATENCY_REFRESH_STATE["started_at"] = int(time.time())
                LATENCY_REFRESH_STATE["finished_at"] = 0
                LATENCY_REFRESH_STATE["ok"] = None
        _latency_ok = True
        try:
            _do_latency_round()
        except Exception as e:
            log.warning(f"[Latency] error: {e}")
            _latency_ok = False
        finally:
            with _LATENCY_REFRESH_LOCK:
                _LATENCY_REFRESH_RUNNING[0] = False
            with LATENCY_REFRESH_STATE_LOCK:
                if LATENCY_REFRESH_STATE["running"]:
                    LATENCY_REFRESH_STATE["running"] = False
                    LATENCY_REFRESH_STATE["finished_at"] = int(time.time())
                    LATENCY_REFRESH_STATE["ok"] = _latency_ok
        if STOP_EVENT.wait(LATENCY_INTERVAL): break
    log.info("[Latency] Воркер остановлен")




def trigger_latency_refresh():
    with _LATENCY_REFRESH_LOCK:
        if _LATENCY_REFRESH_RUNNING[0]:
            return False, "already running"
        _LATENCY_REFRESH_RUNNING[0] = True

    with LATENCY_REFRESH_STATE_LOCK:
        LATENCY_REFRESH_STATE["running"] = True
        LATENCY_REFRESH_STATE["current"] = 0
        LATENCY_REFRESH_STATE["total"] = 0
        LATENCY_REFRESH_STATE["device"] = ""
        LATENCY_REFRESH_STATE["started_at"] = int(time.time())
        LATENCY_REFRESH_STATE["finished_at"] = 0
        LATENCY_REFRESH_STATE["ok"] = None

    def run():
        try:
            _do_latency_round()
        except Exception as e:
            log.warning(f"[Latency] refresh error: {e}")
            with LATENCY_REFRESH_STATE_LOCK:
                LATENCY_REFRESH_STATE["running"] = False
                LATENCY_REFRESH_STATE["ok"] = False
        finally:
            with _LATENCY_REFRESH_LOCK:
                _LATENCY_REFRESH_RUNNING[0] = False

    threading.Thread(target=run, daemon=True, name="latency-refresh").start()
    return True, None


# ==================== TUYA PROBE ====================
TUYA_PROBE_TIMEOUT = 1.0


def _probe_tuya_6668(ip, timeout=TUYA_PROBE_TIMEOUT):
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, 6668))
        s.close()
    except Exception:
        try: s.close()
        except: pass
        return None
    result = {"port_6668": True}
    payload = bytes.fromhex("000055aa000000000000000a00000000000000000000000000000000")
    for udp_port in (6666, 6667):
        u = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        u.settimeout(timeout)
        try:
            u.sendto(payload, (ip, udp_port))
            data, _ = u.recvfrom(2048)
            u.close()
            if data and len(data) >= 16 and data[:4] == bytes.fromhex("000055aa"):
                try:
                    idx = data.find(b"{")
                    if idx >= 0:
                        parsed = json.loads(data[idx:].decode("utf-8", errors="ignore").rstrip("\x00"))
                        result["udp_port"] = udp_port
                        result["gwId"] = parsed.get("gwId", "")
                        result["productKey"] = parsed.get("productKey", "")
                        result["version"] = parsed.get("version", "")
                        return result
                except Exception:
                    pass
                result["udp_port"] = udp_port
                return result
        except Exception:
            try: u.close()
            except: pass
    result["tuya_probable"] = True
    return result


def detect_version(ip, dev_id, local_key, timeout=PROBE_TIMEOUT_PER_VERSION,
                   stop_first=False, name=""):
    """Определить версию протокола, перебрав версии из PROBE_VERSIONS.

    v1.29.2: возвращаем СПИСОК версий, на которых устройство ответило
    (бывает, что работают несколько — тогда выбор за пользователем),
    и dps первой ответившей.

    v1.31.0: stop_first=True — остановиться на первой ответившей версии
    (в пересборке это экономит до 3 TCP-подключений на устройство; правило №1).

    Возвращает: (versions: list[str], dps: dict)
    """
    if not HAS_TINYTUYA:
        return [], {}
    # v1.31.7: в логах пишем и имя устройства — по одному ID в логах не разобраться
    _tag = f"{name} ({dev_id}@{ip})" if name else f"{dev_id}@{ip}"
    found = []
    dps = {}
    for v in PROBE_VERSIONS:
        r = None   # v1.33.13: страховка от «r не присвоена» (замечание IDE)
        try:
            d = tinytuya.Device(dev_id, ip, local_key)
            d.set_version(float(v))
            d.set_socketPersistent(False)
            d.set_socketTimeout(timeout)
            try:
                r = d.status()
            finally:
                try: d.close()
                except: pass
            if r and isinstance(r, dict) and "dps" in r:
                found.append(v)
                if not dps:
                    dps = r.get("dps", {})
                log.info(f"[Probe] {_tag}: version={v} (dps={len(r['dps'])})")
                if stop_first:
                    break
        except Exception as e:
            log.debug(f"[Probe] {_tag} v={v}: {e}")
            continue
    if found:
        log.info(f"[Probe] {_tag}: отвечают версии {found}")
    else:
        log.warning(f"[Probe] {_tag}: ни одна версия не ответила")
    return found, dps


# ==================== DP MATCHING ====================
def _values_match(local, cloud, dtype):
    if dtype == "Boolean":
        try: return bool(local) == bool(cloud)
        except Exception: return False
    if dtype == "Integer":
        try: return int(local) == int(cloud)
        except Exception: return False
    if dtype == "Enum":
        return str(local).strip() == str(cloud).strip()
    if dtype == "Json":
        try:
            return json.dumps(local, sort_keys=True) == json.dumps(cloud, sort_keys=True)
        except Exception:
            return str(local) == str(cloud)
    return str(local) == str(cloud)


_HEURISTIC_PRIORITY = {
    "switch_led": 100, "switch": 95, "switch_1": 95,
    "va_temperature": 90, "va_humidity": 90,
    "temp_current": 90, "humidity": 90,
    "battery_percentage": 85, "battery_state": 80,
    "bright_value": 85, "temp_value": 85, "colour_data": 80,
    "work_mode": 80, "temp_unit_convert": 75,
    "do_not_disturb": 70, "countdown": 60,
}


def _heuristic_pick(candidates):
    best, best_score = None, -1
    for c in candidates:
        s = _HEURISTIC_PRIORITY.get(c.get("code", ""), 10)
        if s > best_score:
            best_score, best = s, c
    return best


def match_dps_to_codes(local_dps, cloud_status_meta, cloud_current_values,
                       cloud_dp_mapping=None):
    """Сопоставить локальные DP с облачными code.

    v1.30.0: приоритет — облачный mapping (в нём ЕСТЬ номера DP, и он
    авторитетнее любых догадок). Сопоставление по значениям остаётся
    только как проверка/фолбэк для DP, которых в облачном mapping нет.

    Неоднозначные DP (несколько равных кандидатов) больше не «угадываются»
    молча: запись помечается `_ambiguous` + `_candidates`, а несовпадение
    значения в облаке — `_mismatch` (видно в RAW-блоке результата опроса).
    """
    result = {}
    used_codes = set()
    if not isinstance(local_dps, dict) or not cloud_status_meta:
        return result
    meta_by_code = {}
    for meta in cloud_status_meta:
        if not isinstance(meta, dict):
            continue
        code = meta.get("code")
        if code and code not in meta_by_code:
            meta_by_code[code] = meta

    # --- 1) привязка по облачному mapping (номера DP — источник истины) ---
    if isinstance(cloud_dp_mapping, dict):
        for dp_id, m in cloud_dp_mapping.items():
            dp_s = str(dp_id)
            code = (m or {}).get("code") if isinstance(m, dict) else None
            if not code:
                continue
            meta = meta_by_code.get(code) or m
            if dp_s in local_dps and code in cloud_current_values:
                # проверка значениями: не совпало — не блокируем, но помечаем
                if not _values_match(local_dps[dp_s],
                                     cloud_current_values.get(code),
                                     meta.get("type", "")):
                    e = dict(meta)
                    e["_mismatch"] = True
                    result[dp_s] = e
                    used_codes.add(code)
                    continue
            result[dp_s] = dict(meta)
            used_codes.add(code)

    def dp_sort_key(k):
        try: return (0, int(k))
        except Exception: return (1, str(k))

    # --- 2) остальные DP — по совпадению значений ---
    dp_ids = sorted(local_dps.keys(), key=dp_sort_key)
    pending = []
    for dp_id in dp_ids:
        if str(dp_id) in result:
            continue
        local_val = local_dps[dp_id]
        candidates = []
        for code, meta in meta_by_code.items():
            if code in used_codes:
                continue
            cloud_val = cloud_current_values.get(code)
            if cloud_val is None:
                continue
            if _values_match(local_val, cloud_val, meta.get("type", "")):
                candidates.append(meta)
        if len(candidates) == 1:
            result[str(dp_id)] = candidates[0]
            used_codes.add(candidates[0]["code"])
        elif len(candidates) > 1:
            pending.append((dp_id, candidates))

    # --- 3) неоднозначные: выбираем, но помечаем (не «угадываем молча») ---
    for dp_id, candidates in pending:
        candidates = [c for c in candidates if c.get("code") not in used_codes]
        if not candidates:
            continue
        best = _heuristic_pick(candidates)
        if best:
            e = dict(best)
            e["_ambiguous"] = True
            e["_candidates"] = [c.get("code") for c in candidates]
            result[str(dp_id)] = e
            used_codes.add(best["code"])
    return result


def build_dps_map_from_matched(matched):
    return mapping_to_dps_map(
        {dp: {"code": m.get("code", ""), "type": m.get("type", ""),
              "values": m.get("values", {}), "name": m.get("name", "")}
         for dp, m in matched.items()},
        ""
    )


# ==================== SCAN ====================
def _get_hostname(ip):
    try:
        return _socket.gethostbyaddr(ip)[0]
    except Exception:
        return None


def _probe_safe_ports(ip, timeout=0.2):
    open_ports = []
    for port, _name in SAFE_PORTS:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            open_ports.append(port)
            s.close()
        except Exception:
            try: s.close()
            except Exception: pass
    return open_ports


def _ip_sort_key(ip):
    try:
        return tuple(int(p) for p in ip.split("."))
    except Exception:
        return (999, 999, 999, 999)


def _scan_extended(subnet_prefix):
    known_ips = get_known_ips()
    ips = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
    log.info(f"[Scan] {subnet_prefix}.0/24 (known: {len(known_ips)}, ping mode: {_PING_MODE[0]})")

    def scan_one(ip):
        is_known = ip in known_ips
        ms = measure_latency(ip)
        if ms is None:
            return None
        entry = {"ip": ip, "ms": ms, "known": is_known}
        hn = _get_hostname(ip)
        if hn:
            entry["hostname"] = hn
        try:
            ports = _probe_safe_ports(ip)
            if ports:
                entry["open_ports"] = ports
        except Exception:
            pass
        if not is_known:
            try:
                tuya_info = _probe_tuya_6668(ip)
                if tuya_info:
                    entry["tuya"] = tuya_info
                    entry["tuya_unknown"] = True
            except Exception as e:
                log.debug(f"[Scan] tuya probe {ip}: {e}")
        return entry

    results = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        for r in pool.map(scan_one, ips):
            if r:
                results.append(r)
    results.sort(key=lambda x: _ip_sort_key(x["ip"]))
    log.info(f"[Scan] Найдено {len(results)} устройств")
    return results


# ==================== LOG TAIL ====================
_log_buffer = []
_log_buffer_lock = threading.Lock()
# v1.28.9: _log_seq при старте = timestamp в мс. Гарантирует,
# что seq монотонно растёт МЕЖДУ рестартами WebUI — иначе после
# рестарта _log_seq снова с 0/малого, фронт с сохранённым
# lastSeq из sessionStorage отбрасывает новые записи
# (`if item.seq <= _getLastSeq() return`).
_log_seq = int(time.time() * 1000)
_log_seq_lock = threading.Lock()
_sse_subscribers = []
_sse_subscribers_lock = threading.Lock()
LOG_BUFFER_MAX = 5000


def _read_initial_log():
    global _log_seq
    for path, src in ((LOG_FILE, "bridge"), (LOG_FILE_WEBUI, "webui")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                # v1.28.19: deque(maxlen) — не читаем весь файл (5 МБ)
                tail = deque(f, maxlen=LOG_HISTORY_LINES)
            for line in tail:
                _append_log_line(line.rstrip("\n"), source=src)
        except Exception as e:
            log.warning(f"[Log] init {path}: {e}")


def _append_log_line(line, source="bridge"):
    global _log_seq
    if not line: return
    with _log_seq_lock:
        _log_seq += 1; seq = _log_seq
    level = "INFO"
    # v1.22.1: уровень ищем только в шапке (первые 40 символов),
    # чтобы "[INFO]" в тексте сообщения не путал парсер.
    _head = line[:40]
    for lvl in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        if f"[{lvl}]" in _head:
            level = lvl; break
    line_ts = parse_log_line_timestamp(line)
    item = {"seq": seq, "ts": line_ts if line_ts else int(time.time()),
            "level": level, "msg": line, "source": source}
    with _log_buffer_lock:
        _log_buffer.append(item)
        if len(_log_buffer) > LOG_BUFFER_MAX:
            del _log_buffer[:len(_log_buffer) - LOG_BUFFER_MAX]
    dead = []
    with _sse_subscribers_lock:
        subs = list(_sse_subscribers)
    for q, _la in subs:
        try: q.put_nowait(item)
        except Exception: dead.append(q)
    if dead:
        with _sse_subscribers_lock:
            _sse_subscribers[:] = [(q, la) for (q, la) in _sse_subscribers if q not in dead]


def _log_tailer(file_path=None, source="bridge"):
    # v1.21.3: читает один файл (bridge.log или webui.log),
    # помечает все строки source="bridge"|"webui". Запускается
    # двумя threads в main().
    if file_path is None:
        file_path = LOG_FILE
    f = None; current_inode = None
    try:
        while not STOP_EVENT.is_set():
            try:
                if f is None:
                    if not os.path.exists(file_path):
                        if STOP_EVENT.wait(LOG_POLL_INTERVAL): break
                        continue
                    f = open(file_path, "r", encoding="utf-8", errors="replace")
                    try: current_inode = os.fstat(f.fileno()).st_ino
                    except OSError: current_inode = None
                    f.seek(0, 2)
                    # v1.22.6: tailer start — видно, с какого inode/offset начали.
                    try:
                        log.info(f"[Log] tailer start {file_path} (src={source}, "
                                 f"inode={current_inode}, offset={f.tell()})")
                    except Exception:
                        pass
                try:
                    st = os.stat(file_path)
                    if current_inode is not None and st.st_ino != current_inode:
                        f.close(); f = None; current_inode = None; continue
                    if st.st_size < f.tell():
                        f.close(); f = None; current_inode = None; continue
                except OSError:
                    f.close(); f = None; current_inode = None; continue
                line = f.readline()
                if line: _append_log_line(line.rstrip("\n"), source=source)
                else:
                    if STOP_EVENT.wait(LOG_POLL_INTERVAL): break
            except Exception as e:
                log.warning(f"[Log] tailer {file_path}: {e}")
                if f:
                    try: f.close()
                    except: pass
                    f = None; current_inode = None
                if STOP_EVENT.wait(2): break
    except Exception: pass


def _subscribe_sse():
    with _sse_subscribers_lock:
        if len(_sse_subscribers) >= SSE_MAX_SUBSCRIBERS:
            _sse_subscribers.sort(key=lambda x: x[1])
            old_q, _ = _sse_subscribers.pop(0)
            try: old_q.put_nowait(None)
            except: pass
        q = _queue.Queue(maxsize=5000)
        _sse_subscribers.append((q, time.time()))
    return q


def _unsubscribe_sse(q):
    with _sse_subscribers_lock:
        _sse_subscribers[:] = [(qq, la) for (qq, la) in _sse_subscribers if qq is not q]


def _sse_touch(q):
    with _sse_subscribers_lock:
        for i, (qq, _) in enumerate(_sse_subscribers):
            if qq is q: _sse_subscribers[i] = (qq, time.time()); return


# ==================== MQTT ====================
_mqtt = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id=f"tuya_webui_{uuid.uuid4().hex[:8]}",
)
if MQTT_USERNAME: _mqtt.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
_mqtt.reconnect_delay_set(min_delay=1, max_delay=30)


def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        for t in [
            # v1.30.1: всё, что bridge публикует под bridge/ — status/uptime/
            # version/cpu_pct/rss_mb/ping_mode и любые *_result. Дикая карта
            # вместо перечисления: раньше новая команда (config_report,
            # expire_*, config_normalize) не была подписана, ответ приходил
            # «в пустоту», и UI показывал timeout при живом bridge.
            f"{TOPIC_PREFIX}/bridge/#",
            f"{TOPIC_PREFIX}/+/status",
            # v1.28.6: battery_alert / battery_last_up (bridge 1.9.4+)
            f"{TOPIC_PREFIX}/+/battery_alert", f"{TOPIC_PREFIX}/+/battery_last_up",
            f"{TOPIC_PREFIX}/+/last_seen", f"{TOPIC_PREFIX}/+/cache_snapshot",
        ]:
            client.subscribe(t)
        log.info("[MQTT] Подключён, подписки OK")
    else:
        log.error(f"[MQTT] Ошибка: {rc}")


def _on_disconnect(client, userdata, flags, rc, properties=None):
    log.warning(f"[MQTT] Отключён (rc={rc})")


def _send_request(topic, payload, timeout=30):
    rid = str(uuid.uuid4())[:8]
    q = _queue.Queue(maxsize=1)
    with PENDING_LOCK: PENDING_REQUESTS[rid] = q
    payload = dict(payload); payload["request_id"] = rid
    try:
        _mqtt.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=False)
    except Exception as e:
        with PENDING_LOCK: PENDING_REQUESTS.pop(rid, None)
        return {"ok": False, "error": f"publish failed: {e}"}
    try: return q.get(timeout=timeout)
    except _queue.Empty: return {"ok": False, "error": "timeout"}
    finally:
        with PENDING_LOCK: PENDING_REQUESTS.pop(rid, None)


def _resolve_pending(data):
    rid = data.get("request_id")
    if not rid: return
    with PENDING_LOCK: q = PENDING_REQUESTS.get(rid)
    if q:
        try: q.put_nowait(data)
        except: pass


def _on_message(client, userdata, msg):
    try: payload = msg.payload.decode("utf-8")
    except: return
    parts = msg.topic.split("/")
    if len(parts) == 3 and parts[1] == "bridge":
        key = parts[2]
        if key == "status":
            with STATE_LOCK:
                old_status = STATE["bridge_status"]
                STATE["bridge_status"] = payload
                # v1.18.9: bridge только что стартовал (был offline/unknown, стал online)
                if payload == "online" and old_status in ("unknown", "offline"):
                    STATE["bridge_started_at"] = int(time.time())
                    log.info(f"[Bridge] Старт — grace period {BRIDGE_STARTUP_GRACE_SEC}с для status_events")
        elif key == "uptime":
            try:
                with STATE_LOCK:
                    STATE["uptime"] = int(payload)
                    # v1.18.9: если bridge давно работает, а started_at не выставлен — выставим
                    if STATE["bridge_started_at"] == 0 and STATE["uptime"] > 0:
                        STATE["bridge_started_at"] = int(time.time()) - STATE["uptime"]
            except: pass
        elif key == "version":
            with STATE_LOCK: STATE["version"] = payload
        elif key == "cpu_pct":
            # v1.28.3: CPU/RAM bridge от bridge 1.9.3+
            try:
                with STATE_LOCK:
                    STATE["bridge_cpu_pct"] = float(payload)
            except (ValueError, TypeError):
                pass
        elif key == "rss_mb":
            try:
                with STATE_LOCK:
                    STATE["bridge_rss_mb"] = float(payload)
            except (ValueError, TypeError):
                pass
        elif key == "ping_mode":
            # v1.28.27: режим пинга bridge + число батарейных.
            try:
                with STATE_LOCK:
                    STATE["bridge_ping_mode"] = json.loads(payload)
            except Exception:
                pass
        elif key == "cmd_ack":
            # v1.33.8: статистика отклика «команда → отчёт» и текущее окно
            # защиты от «эха» (bridge 1.12.19+).
            try:
                with STATE_LOCK:
                    STATE["bridge_cmd_ack"] = json.loads(payload)
            except Exception:
                pass
        elif key.endswith("_result"):
            # v1.30.1: раньше здесь был жёсткий список топиков, и ответ новой
            # команды не разбирался (UI ловил timeout). Любой ответ bridge несёт
            # request_id; неизвестный id _resolve_pending просто игнорирует.
            try: _resolve_pending(json.loads(payload))
            except: pass
        return
    if len(parts) == 3:
        dev = parts[1]; key = parts[2]
        if dev == "bridge": return
        with STATE_LOCK:
            if dev not in STATE["devices"]:
                STATE["devices"][dev] = {"status": "unknown", "last_seen": None,
                                          "cache": {}, "latency_ms": None, "latency_ts": None}
            if key == "status":
                old = STATE["devices"][dev].get("status")
                STATE["devices"][dev]["status"] = payload
                if old != payload:
                    # v1.18.9: grace после старта bridge
                    started = STATE.get("bridge_started_at", 0)
                    grace_ok = (started == 0 or (time.time() - started) >= BRIDGE_STARTUP_GRACE_SEC)
                    # v1.21.0: quiet hours
                    # v1.22.6: учитываем grace-period (первые 2 мин
                    # после окна тоже не пишем — иначе «фантомные»
                    # переходы в мерцаниях сразу после окна тишины).
                    quiet_ok = not is_quiet_or_grace_now(dev)
                    # v1.28.4: не пишем status_events для батарейных.
                    # Bridge публикует offline по таймеру (30 сек DOWN),
                    # потом online при следующем UP — это не реальные
                    # события, а артефакт. Без этого фикса в «Мерцающих»
                    # и «Хронологии» копятся фантомные переходы.
                    _battery_skip = False
                    _m = get_device_meta(dev)
                    if _m and _m.get("battery_powered"):
                        _battery_skip = True
                    if grace_ok and quiet_ok and not _battery_skip:
                        db_insert_status(int(time.time()), dev, payload)
            elif key == "last_seen":
                try: STATE["devices"][dev]["last_seen"] = int(payload)
                except: pass
            # v1.28.6: battery_alert (ok / no_data) и battery_last_up (unix ts)
            elif key == "battery_alert":
                STATE["devices"][dev]["battery_alert"] = payload
            elif key == "battery_last_up":
                # v1.28.7: bridge 1.9.8+ публикует ISO8601
                # ("2026-09-20T03:48:35+00:00"), а не unix seconds.
                # HA для device_class=timestamp требует ISO8601.
                # WebUI конвертирует обратно в unix — внутренний
                # формат остался unix (fmtAgo/fmtDateTime работают).
                try:
                    # ISO8601 → unix
                    _iso = payload.strip()
                    if _iso and ("T" in _iso or "-" in _iso[:5]):
                        # v1.28.19: fromisoformat в Python <=3.10 не парсит
                        # 'Z' и не всегда понимает миллисекунды — fallback
                        # через strptime для типичных форматов.
                        import datetime as _dt
                        _s = _iso.replace("Z", "+00:00")
                        _dt_obj = None
                        try:
                            _dt_obj = _dt.datetime.fromisoformat(_s)
                        except ValueError:
                            for _fmt in ("%Y-%m-%dT%H:%M:%S.%f%z",
                                         "%Y-%m-%dT%H:%M:%S%z",
                                         "%Y-%m-%dT%H:%M:%S.%f",
                                         "%Y-%m-%dT%H:%M:%S"):
                                try:
                                    _dt_obj = _dt.datetime.strptime(_s, _fmt)
                                    break
                                except ValueError:
                                    continue
                        if _dt_obj is None:
                            raise ValueError(f"unrecognized ISO8601: {_iso!r}")
                        if _dt_obj.tzinfo is None:
                            _dt_obj = _dt_obj.replace(
                                tzinfo=_dt.timezone.utc
                            )
                        STATE["devices"][dev]["battery_last_up"] = int(
                            _dt_obj.timestamp()
                        )
                    else:
                        # Legacy: unix seconds (на случай, если bridge
                        # откатили на 1.9.7 или сам ещё не обновлён).
                        STATE["devices"][dev]["battery_last_up"] = int(payload)
                except Exception as _e:
                    log.debug(
                        f"[MQTT] battery_last_up parse failed "
                        f"for {dev}: {payload!r} ({_e})"
                    )
            elif key == "cache_snapshot":
                try:
                    cd = json.loads(payload)
                    if isinstance(cd, dict):
                        STATE["devices"][dev]["cache"] = cd
                        # v1.24.2: db_insert_snapshot отключён — state_history
                        # была мёртвой фичей (данные копились, но UI их
                        # не показывал). Функция оставлена в коде на будущее.
                        # db_insert_snapshot(dev, cd)
                except: pass


_mqtt.on_connect = _on_connect
_mqtt.on_disconnect = _on_disconnect
_mqtt.on_message = _on_message


# ==================== TUYA CLOUD ====================
def _extract_cloud_list(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in ("result", "devices", "list"):
            v = raw.get(k)
            if isinstance(v, list):
                return v
    return None


def _safe_json_loads(s):
    if isinstance(s, dict): return s
    if isinstance(s, list): return s
    if not isinstance(s, str): return {}
    s = s.strip()
    if not s: return {}
    try: return json.loads(s)
    except Exception: return {}


def extract_status_values(raw_cloud):
    result = {}
    if not isinstance(raw_cloud, dict):
        return result
    status = raw_cloud.get("status")
    if isinstance(status, list):
        for item in status:
            if isinstance(item, dict):
                code = item.get("code")
                if code:
                    result[code] = item.get("value")
    return result


def extract_status_meta(raw_props):
    out = []
    if not isinstance(raw_props, dict):
        return out
    seen = set()
    for section in ("functions", "status"):
        arr = raw_props.get(section)
        if not isinstance(arr, list):
            continue
        for item in arr:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            raw_values = item.get("values", "{}")
            values = _safe_json_loads(raw_values)
            out.append({
                "code": code,
                "type": item.get("type", ""),
                "values": values if isinstance(values, dict) else {},
                "name": item.get("name", ""),
            })
    return out


def _fetch_props_for_device(cloud, dev_id, retries=1):
    for attempt in range(retries):
        try:
            props = cloud.getproperties(dev_id)
            if isinstance(props, dict):
                raw = props
                if "result" in props and isinstance(props["result"], dict):
                    raw = props["result"]
                return raw
        except Exception as e:
            log.warning(f"[Cloud] getproperties({dev_id}) attempt {attempt+1}: {e}")
    return {}


def _fetch_mappings_for_devices(cloud, device_ids, retries=1):
    if not device_ids:
        return {}
    result = {}
    for attempt in range(retries):
        try:
            raw = cloud.getdevices(False, include_map=True)
            devices = _extract_cloud_list(raw)
            if devices is None:
                log.warning("[Cloud] mapping-запрос вернул неожиданный формат")
                break
            wanted = set(device_ids)
            for d in devices:
                if not isinstance(d, dict):
                    continue
                did = d.get("id")
                if did in wanted and isinstance(d.get("mapping"), dict):
                    result[did] = d["mapping"]
            if result:
                log.info(f"[Cloud] mapping получен для {len(result)}/{len(wanted)} устройств")
                return result
        except TypeError:
            log.warning("[Cloud] include_map не поддерживается, mapping будет из verbose")
            break
        except Exception as e:
            log.warning(f"[Cloud] mapping-запрос attempt {attempt+1}: {e}")
    return result


CLOUD_FETCH_TIMEOUT = 60   # v1.22.6: глобальный таймаут Cloud-запросов


def _cloud_fetch_with_timeout(access_id, access_secret, region,
                              fetch_mappings=True, timeout=CLOUD_FETCH_TIMEOUT):
    """v1.22.6: обёртка tuya_cloud_fetch в отдельный поток с join(timeout).
    Защищает воркер ThreadingHTTPServer от вечного зависания, если
    Tuya Cloud недоступен или tinytuya.requests висит без таймаута."""
    result_box = {"result": None}

    def _runner():
        try:
            result_box["result"] = tuya_cloud_fetch(access_id, access_secret, region,
                                                     fetch_mappings=fetch_mappings)
        except Exception as e:
            result_box["result"] = {"ok": False, "error": f"cloud fetch exception: {e}"}

    t = threading.Thread(target=_runner, daemon=True, name="cloud-fetch")
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        log.warning(f"[Cloud] fetch timeout {timeout}s — возвращаем ошибку")
        return {"ok": False, "error": f"timeout {timeout}s (Tuya Cloud недоступен)"}
    return result_box["result"] or {"ok": False, "error": "empty result"}


def tuya_cloud_fetch(access_id, access_secret, region, fetch_mappings=True):
    if not HAS_TINYTUYA:
        return {"ok": False, "error": "tinytuya not installed"}
    try:
        cloud = tinytuya.Cloud(apiRegion=region, apiKey=access_id, apiSecret=access_secret)

        try:
            raw_verbose = cloud.getdevices(True)
        except Exception as e:
            log.warning(f"[Cloud] getdevices(True): {e}")
            raw_verbose = None

        devices_verbose = _extract_cloud_list(raw_verbose) if raw_verbose is not None else None
        if devices_verbose is None:
            try:
                raw_verbose = cloud.getdevices(True, include_map=True)
                devices_verbose = _extract_cloud_list(raw_verbose)
            except Exception as e:
                return {"ok": False, "error": f"getdevices: {e}"}

        if devices_verbose is None:
            return {"ok": False, "error": f"unexpected cloud response: {type(raw_verbose).__name__}"}

        normalized = []
        device_ids = []
        for d in devices_verbose:
            if not isinstance(d, dict):
                continue
            did = d.get("id", "")
            if did:
                device_ids.append(did)
            cloud_status = extract_status_values(d)
            normalized.append({
                "id": did, "name": d.get("name", ""),
                "local_key": d.get("key", "") or d.get("local_key", ""),
                "category": d.get("category", ""),
                "product_name": d.get("product_name", ""),
                "product_id": d.get("product_id", ""),
                "model": d.get("model", ""), "uuid": d.get("uuid", ""),
                "online": d.get("online", False),
                "mapping": d.get("mapping", {}) if isinstance(d.get("mapping"), dict) else {},
                "gateway_id": d.get("gateway_id", "") or "",
                "sub": bool(d.get("sub", False)),
                "cloud_status": cloud_status,
                "_raw_keys": sorted(d.keys()),
                "_raw_cloud": d,
            })

        need_mapping = [d["id"] for d in normalized if d["id"] and not d["mapping"]]
        if fetch_mappings and need_mapping:
            log.info(f"[Cloud] Запрашиваем mapping отдельно для {len(need_mapping)} устройств")
            mappings = _fetch_mappings_for_devices(cloud, need_mapping, retries=1)
            by_id = {d["id"]: d for d in normalized}
            got = 0
            for did, m in mappings.items():
                if did in by_id and m:
                    by_id[did]["mapping"] = m
                    got += 1
            log.info(f"[Cloud] mapping отдельным запросом: получено для {got}/{len(need_mapping)}")

        by_id = {d["id"]: d for d in normalized if d["id"]}
        for d in normalized:
            if d["local_key"]: continue
            gw = d.get("gateway_id")
            if gw and gw in by_id:
                parent = by_id[gw]
                if parent.get("local_key"):
                    d["local_key"] = parent["local_key"]
                    d["_key_from_parent"] = parent["id"]

        if fetch_mappings:
            for d in normalized:
                if not d["id"]: continue
                try:
                    raw_props = _fetch_props_for_device(cloud, d["id"], retries=1)
                    if raw_props:
                        d["_raw_properties"] = raw_props
                        status_meta = extract_status_meta(raw_props)
                        d["_cloud_status_meta"] = status_meta
                        code_to_name = {m["code"]: m.get("name", "") for m in status_meta}
                        if isinstance(d["mapping"], dict):
                            for dp, m in d["mapping"].items():
                                if isinstance(m, dict) and m.get("code"):
                                    code = m["code"]
                                    if "name" not in m and code in code_to_name:
                                        m["name"] = code_to_name[code]
                except Exception as e:
                    log.warning(f"[Cloud] props for {d['id']}: {e}")

        # v1.25.0 (fix3): Cloud fetch больше НЕ перезаписывает
        # tinytuya_devices.json. Только tuya_cloud_cache.json
        # (через save_cloud_cache в /api/cloud/fetch handler).
        # tinytuya_devices.json обновляется только через
        # /api/base/tinytuya/rebuild (кнопка «🔄 Пересобрать tinytuya.json»).
        # save_tinytuya_devices_json(normalized)  ← было, убрано

        return {"ok": True, "devices": normalized}
    except Exception as e:
        import traceback
        log.error(f"[Cloud] fetch error: {e}\n{traceback.format_exc()}")
        return {"ok": False, "error": str(e)}


def load_tinytuya_devices_json():
    try:
        with open(TINYTUYA_DEVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return {}
        out = {}
        for d in data:
            did = d.get("id")
            if did:
                out[did] = d
        return out
    except Exception:
        return {}


# ==================== CLOUD CACHE ====================
CLOUD_CACHE_LOCK = threading.Lock()
# v1.32.1: разобранный кэш по mtime файла — иначе /api/status перечитывал
# tuya_cloud_cache.json на каждое устройство (N чтений файла за один опрос).
_CLOUD_CACHE_MEM = {"mtime": None, "data": None}


def save_cloud_cache(devices, fetched_at=None, access_id="", region="eu"):
    with CLOUD_CACHE_LOCK:
        try:
            os.makedirs(os.path.dirname(TUYA_CLOUD_CACHE_FILE) or ".", exist_ok=True)
            payload = {
                "fetched_at": int(fetched_at or time.time()),
                "access_id": access_id,
                "region": region,
                "devices": devices,
            }
            tmp = TUYA_CLOUD_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, TUYA_CLOUD_CACHE_FILE)
            try:
                os.chmod(TUYA_CLOUD_CACHE_FILE, 0o600)
            except Exception:
                pass
            log.info(f"[CloudCache] Сохранено {len(devices)} устройств")
            _CLOUD_CACHE_MEM["mtime"] = None   # v1.32.1: сбросить разобранный кэш
            _CLOUD_CACHE_MEM["data"] = None
            return True
        except Exception as e:
            log.warning(f"[CloudCache] save: {e}")
            return False


def load_cloud_cache():
    """v1.32.1: кэш разбирается один раз на версию файла (по mtime).

    Раньше функция вызывалась из enrich на каждое устройство и каждый раз
    читала и парсила весь tuya_cloud_cache.json — на 40 устройств это 40 чтений
    файла за один `/api/status`.
    """
    with CLOUD_CACHE_LOCK:
        try:
            if not os.path.exists(TUYA_CLOUD_CACHE_FILE):
                _CLOUD_CACHE_MEM["mtime"] = None
                _CLOUD_CACHE_MEM["data"] = None
                return None
            mtime = os.path.getmtime(TUYA_CLOUD_CACHE_FILE)
            if _CLOUD_CACHE_MEM["mtime"] == mtime:
                return _CLOUD_CACHE_MEM["data"]
            with open(TUYA_CLOUD_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = None
            else:
                devices = data.get("devices")
                if not isinstance(devices, list) or not devices:
                    data = None
            _CLOUD_CACHE_MEM["mtime"] = mtime
            _CLOUD_CACHE_MEM["data"] = data
            return data
        except Exception as e:
            log.warning(f"[CloudCache] load: {e}")
            return None


def clear_cloud_cache():
    with CLOUD_CACHE_LOCK:
        try:
            if os.path.exists(TUYA_CLOUD_CACHE_FILE):
                os.unlink(TUYA_CLOUD_CACHE_FILE)
            return True
        except Exception as e:
            log.warning(f"[CloudCache] clear: {e}")
            return False


CATEGORY_TO_TYPE = {
    "xdd": "light", "dd": "light", "fwd": "light", "dc": "light", "tgq": "light",
    "kg": "switch", "tdq": "switch", "cz": "switch", "pc": "switch", "dlq": "switch", "kj": "switch",
    "wk": "climate", "rs": "climate",
    "wsdcg": "sensor",
    "mcs": "binary_sensor", "pir": "binary_sensor", "sj": "binary_sensor",
    "ywbj": "binary_sensor", "rqbj": "binary_sensor",
    "ldcg": "sensor", "pm2.5": "sensor", "co2bj": "sensor", "hps": "sensor",
    "ldcg2": "sensor", "sb": "sensor",
}


def mapping_to_dps_map(mapping, category="", writable_codes=None, dev_type=None):
    dps_map = {}
    if not isinstance(mapping, dict): return dps_map
    # v1.28.34: единое правило component (вариант C) — dev_type и
    # writable_codes (Cloud functions) приходят от вызывающего.
    if dev_type is None:
        # v1.29.2: guess_type_from_category может вернуть "" (тип не определён) —
        # для генерации dps_map берём безопасный "switch".
        dev_type = guess_type_from_category(category) or "switch"
    for dp_key, m in mapping.items():
        if not isinstance(m, dict): continue
        code = m.get("code", "")
        dtype = m.get("type", "")
        values = m.get("values", {}) if isinstance(m.get("values"), dict) else {}
        if not code: continue
        entry = {}
        if dtype == "Boolean":
            if code in ("switch_led", "switch", "switch_1"):
                entry["name"] = code
            elif code.startswith("switch_") and code != "switch_led":
                entry["name"] = code
            elif code == "switch_backlight":
                entry["name"] = "backlight"
            elif code == "switch_prepayment":
                entry["name"] = "prepayment"
            elif code in ("doorcontact_state",):
                entry["name"] = "door"; entry["device_class"] = "door"
            elif code in ("pir",):
                entry["name"] = "motion"; entry["device_class"] = "motion"
            elif code in ("watersensor_state",):
                entry["name"] = "moisture"; entry["device_class"] = "moisture"
            elif code in ("fault", "problema"):
                entry["name"] = "fault"; entry["device_class"] = "problem"
            else:
                entry["name"] = code
            # v1.28.34: единое правило (вариант C).
            entry["component"] = cloud_dp_component("Boolean", code)
        elif dtype == "Integer":
            entry["name"] = code
            if "min" in values: entry["min"] = values["min"]
            if "max" in values: entry["max"] = values["max"]
            if "scale" in values and values["scale"]: entry["scale"] = values["scale"]
            if "unit" in values and values["unit"]: entry["unit"] = values["unit"]
            # v1.28.53: device_class/unit/state_class — единая эвристика
            # (синхронно с JS _dpsHeuristicMeta). Cloud unit выше — приоритет.
            for _k, _v in heuristic_device_meta(code).items():
                entry.setdefault(_k, _v)
            if code in ("temp_value", "bright_value", "colour_temp"):
                entry["name"] = code
            entry.setdefault("state_class", "measurement")
            # v1.28.34: единое правило (вариант C).
            _wr_i = writable_codes if writable_codes is not None else ("min" in values and "max" in values)
            entry["component"] = cloud_dp_component("Integer", code, writable=_wr_i, dev_type=dev_type)
        elif dtype == "Enum":
            rng = values.get("range", [])
            if code == "relay_status":
                entry["name"] = "relay_status"; entry["options"] = list(rng)
            elif code in ("mode", "preset_mode"):
                entry["name"] = "preset_mode"; entry["options"] = list(rng)
            elif code == "work_mode":
                entry["name"] = "work_mode"; entry["options"] = list(rng)
            elif code in ("watersensor_state",):
                entry["name"] = "moisture"; entry["device_class"] = "moisture"
            elif code in ("battery_state",):
                entry["name"] = "battery_state"
            else:
                entry["name"] = code
            # v1.28.34: единое правило (вариант C).
            entry["component"] = cloud_dp_component("Enum", code, dev_type=dev_type)
        elif dtype == "Json":
            entry["component"] = "sensor"; entry["name"] = code
        elif dtype in ("String", "Raw"):
            if code == "phase_a":
                entry["component"] = "sensor"; entry["name"] = "phase_a"
            else:
                entry["component"] = "sensor"; entry["name"] = code
        elif dtype == "Bitmap":
            if code == "fault":
                entry["component"] = "binary_sensor"; entry["name"] = "fault"; entry["device_class"] = "problem"
            else: continue
        else:
            entry["component"] = "sensor"; entry["name"] = code
        dps_map[str(dp_key)] = entry
    return dps_map


def guess_type_from_category(category, product_name=""):
    """Тип устройства по категории Tuya (и имени продукта — как fallback).

    v1.29.2: если категория неизвестна и имя ничего не подсказало —
    возвращаем "" (тип не определён), а не молчаливый "switch".
    Вызывающий код сам решает, что записать в конфиг.
    """
    c = (category or "").lower().strip(); pn = (product_name or "").lower()
    if c in CATEGORY_TO_TYPE: return CATEGORY_TO_TYPE[c]
    if any(x in pn for x in ("light", "lamp", "strip", "bulb")): return "light"
    if any(x in pn for x in ("thermostat", "heating", "floor")): return "climate"
    if any(x in pn for x in ("switch", "breaker", "plug", "socket")): return "switch"
    if any(x in pn for x in ("sensor", "temp", "humid")): return "sensor"
    if any(x in pn for x in ("door", "motion", "leak")): return "binary_sensor"
    return ""


# ==================== TUYA-LOCAL DB ====================
def _remote_content_length(url, timeout=10):
    """v1.33.3: размер файла по URL (Content-Length) для честного процента.

    0 — если узнать не удалось (тогда полоса остаётся indeterminate).
    """
    try:
        r = subprocess.run(["curl", "-fsSIL", "--max-time", str(timeout), url],
                           capture_output=True, text=True, timeout=timeout + 5)
        for line in reversed((r.stdout or "").splitlines()):
            if line.lower().startswith("content-length:"):
                try:
                    n = int(line.split(":", 1)[1].strip())
                    return n if n > 0 else 0
                except ValueError:
                    return 0
    except Exception as e:
        log.debug(f"[tuya-local] content-length: {e}")
    return 0


def _download_with_progress(cmd, dest, timeout_s):
    """v1.33.3: скачать файл, следя за его размером (МБ → TUYA_LOCAL_STATE).

    Работает и для curl, и для wget-фолбэка. Раньше wget запускался «немо»
    (subprocess.run), из-за чего счётчик «скачано МБ» залипал на последнем
    значении от curl до конца фазы.

    Возвращает (returncode, stderr).
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    deadline = time.time() + timeout_s
    while proc.poll() is None:
        if time.time() > deadline:
            proc.kill()
            break
        try:
            _mb = round(os.path.getsize(dest) / (1024 * 1024), 1)
            with TUYA_LOCAL_STATE_LOCK:
                TUYA_LOCAL_STATE["mb_done"] = _mb
        except OSError:
            pass
        time.sleep(0.4)
    err = ""
    try:
        _, err = proc.communicate(timeout=5)   # reap + закрыть пайпы
    except Exception:
        pass
    rc = proc.returncode
    return (rc if rc is not None else -1), (err or "")


def download_tuya_local_db(max_retries=3):
    if os.path.isdir(TUYA_LOCAL_DB_OLD_DIR) and not os.path.isdir(TUYA_LOCAL_DB_DIR):
        try:
            os.makedirs(os.path.dirname(TUYA_LOCAL_DB_DIR) or ".", exist_ok=True)
            shutil.move(TUYA_LOCAL_DB_OLD_DIR, TUYA_LOCAL_DB_DIR)
            log.info(f"[tuya-local] Миграция: {TUYA_LOCAL_DB_OLD_DIR} → {TUYA_LOCAL_DB_DIR}")
        except Exception as e:
            log.warning(f"[tuya-local] Миграция не удалась: {e}")

    # v1.28.34: проверяем/удаляем ОДИН и тот же каталог (DB_DIR).
    # Раньше проверялся YAML_DIR, а удалялся DB_DIR — при частичной
    # распаковке старые файлы смешивались с новыми.
    if os.path.isdir(TUYA_LOCAL_DB_DIR):
        try:
            shutil.rmtree(TUYA_LOCAL_DB_DIR)
        except Exception as e:
            log.warning(f"[tuya-local] rmtree: {e}")
    os.makedirs(TUYA_LOCAL_DB_DIR, exist_ok=True)

    tarball_path = os.path.join(TUYA_LOCAL_DB_DIR, "_tuya-local.tar.gz")
    last_err = None
    # v1.33.3: ожидаемый размер (для процента скачивания) + сброс счётчика.
    _mb_total = _remote_content_length(TUYA_LOCAL_TARBALL_URL)
    with TUYA_LOCAL_STATE_LOCK:
        TUYA_LOCAL_STATE["mb_total"] = _mb_total
        TUYA_LOCAL_STATE["mb_done"] = 0
        TUYA_LOCAL_STATE["attempts"] = max_retries
    _dl_t0 = time.time()
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"[tuya-local] Скачивание (попытка {attempt}/{max_retries})...")
            # v1.33.9: показываем номер попытки. Раньше при падении загрузки
            # монитор размера останавливался, и «скачано N МБ» замирало на всё
            # время повторов — выглядело как зависание.
            with TUYA_LOCAL_STATE_LOCK:
                TUYA_LOCAL_STATE["mb_done"] = 0
                TUYA_LOCAL_STATE["attempt"] = attempt
                TUYA_LOCAL_STATE["message"] = (
                    f"Фаза 1/2: скачивание tuya-local "
                    f"(попытка {attempt}/{max_retries})…")
            # v1.32.28 + v1.33.3: прогресс в МБ ведём для ЛЮБОГО скачивателя.
            # v1.33.9: curl сам повторяет транзиентные сбои и быстро отваливается,
            # если GitHub недоступен (--connect-timeout), — не висим 2 минуты.
            _curl = ["curl", "-fsSL", "--connect-timeout", "15",
                     "--retry", "2", "--retry-delay", "2",
                     "-o", tarball_path, TUYA_LOCAL_TARBALL_URL]
            _rc, _err = _download_with_progress(_curl, tarball_path,
                                                TUYA_LOCAL_DL_TIMEOUT)
            if _rc != 0:
                # fallback: wget, если curl не смог (тоже с прогрессом)
                _rc, _err = _download_with_progress(
                    ["wget", "-q", "-O", tarball_path, TUYA_LOCAL_TARBALL_URL],
                    tarball_path, TUYA_LOCAL_DL_TIMEOUT)
            if _rc != 0:
                last_err = f"curl/wget rc={_rc}: {_err[:200]}"
                log.warning(f"[tuya-local] {last_err}")
                time.sleep(2)
                continue
            _mb = 0.0
            try:
                _mb = round(os.path.getsize(tarball_path) / (1024 * 1024), 1)
            except OSError:
                pass
            r = subprocess.run(["tar", "-xzf", tarball_path, "-C", TUYA_LOCAL_DB_DIR, "--strip-components=1"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                last_err = f"tar: {r.stderr[:200]}"
                log.warning(f"[tuya-local] {last_err}")
                continue
            if os.path.isdir(TUYA_LOCAL_YAML_DIR):
                files = [f for f in os.listdir(TUYA_LOCAL_YAML_DIR) if f.endswith(".yaml")]
                log.info(f"[tuya-local] OK: {len(files)} YAML-файлов (попытка {attempt})")
                _prune_tuya_local_db()   # v1.28.64: оставить только devices/*.yaml
                # v1.33.9: индекс строит фаза 2 — с прогрессом. Раньше он
                # собирался ЗДЕСЬ ЖЕ, и работа шла дважды: фаза 2 «молчала», а
                # импорт занимал вдвое больше времени.
                try: os.unlink(tarball_path)
                except: pass
                with TUYA_LOCAL_STATE_LOCK:
                    TUYA_LOCAL_STATE["dl_stats"] = {
                        "attempts_used": attempt,
                        "download_mb": _mb,
                        "download_sec": round(time.time() - _dl_t0, 1),
                        "yaml_files": len(files),
                    }
                return True, None
            last_err = "yaml dir not found after extract"
        except Exception as e:
            last_err = str(e)
            log.warning(f"[tuya-local] attempt {attempt}: {e}")
            time.sleep(2)
    return False, last_err


# v1.28.63: единая проиндексированная БД tuya-local: {product_id: dps_map}.
# Строится один раз (при скачивании базы или лениво в фоне) и сохраняется в
# webui_state/tuya-local-db.json — без сканирования 1962 YAML на каждый запрос.
_TL_DB = None
_TL_DB_LOCK = threading.Lock()
_TL_DB_BUILDING = False


def _prune_tuya_local_db():
    """v1.28.64: храним только custom_components/tuya_local/devices/*.yaml."""
    def _rm(path):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception:
            pass

    def _keep_only(dir_path, keep_name):
        try:
            for name in os.listdir(dir_path):
                if name != keep_name:
                    _rm(os.path.join(dir_path, name))
        except Exception:
            pass

    base = TUYA_LOCAL_DB_DIR
    if not os.path.isdir(base):
        return
    _keep_only(base, "custom_components")
    cc = os.path.join(base, "custom_components")
    if os.path.isdir(cc):
        _keep_only(cc, "tuya_local")
    tl = os.path.join(cc, "tuya_local")
    if os.path.isdir(tl):
        _keep_only(tl, "devices")
    dev = os.path.join(tl, "devices")
    if os.path.isdir(dev):
        for name in os.listdir(dev):
            if not name.endswith(".yaml"):
                _rm(os.path.join(dev, name))
    log.info("[tuya-local] prune: оставлены только devices/*.yaml")


def _build_tuya_local_db(progress=None):
    """Просканировать YAML → {product_id: dps_map}, сохранить JSON.

    progress(i, total) — v1.28.65: колбэк прогресса импорта.
    """
    # v1.33.13: без pyyaml строить нечего — раньше каждый файл молча падал
    # во внутренний except, и получалась пустая БД (замечание инспекций IDE).
    if not HAS_YAML:
        log.warning("[tuya-local] pyyaml не установлен — индекс не строится")
        return None
    global _TL_DB, _TL_DB_BUILDING
    with _TL_DB_LOCK:
        if _TL_DB_BUILDING:
            return None
        _TL_DB_BUILDING = True
    db = {}
    try:
        if os.path.isdir(TUYA_LOCAL_YAML_DIR):
            files = [f for f in os.listdir(TUYA_LOCAL_YAML_DIR) if f.endswith(".yaml")]
            total = len(files)
            for i, fname in enumerate(files, 1):
                path = os.path.join(TUYA_LOCAL_YAML_DIR, fname)
                data = None
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f.read())
                except Exception:
                    data = None
                if isinstance(data, dict):
                    entries = parse_tuya_local_yaml(data)
                    if entries:
                        for p in (data.get("products") or []):
                            if isinstance(p, dict) and p.get("id"):
                                db[str(p["id"])] = entries
                if progress:
                    try:
                        progress(i, total)
                    except Exception:
                        pass
        try:
            dir_name = os.path.dirname(TUYA_LOCAL_INDEX_FILE) or "."
            os.makedirs(dir_name, exist_ok=True)
            tmp = TUYA_LOCAL_INDEX_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, TUYA_LOCAL_INDEX_FILE)
        except Exception as e:
            log.warning(f"[tuya-local] save db: {e}")
        log.info(f"[tuya-local] БД проиндексирована: {len(db)} product_id")
    except Exception as e:
        log.warning(f"[tuya-local] build db: {e}")
    finally:
        with _TL_DB_LOCK:
            if db:
                _TL_DB = db
            _TL_DB_BUILDING = False
    return db


def _load_tuya_local_db():
    """Загрузить webui_state/tuya-local-db.json (один раз в память)."""
    global _TL_DB
    with _TL_DB_LOCK:
        if _TL_DB is not None:
            return _TL_DB
        building = _TL_DB_BUILDING
    if building:
        return None
    db = None
    try:
        if os.path.exists(TUYA_LOCAL_INDEX_FILE):
            with open(TUYA_LOCAL_INDEX_FILE, "r", encoding="utf-8") as f:
                db = json.load(f)
            log.info(f"[tuya-local] БД загружена: {len(db)} product_id")
    except Exception as e:
        log.warning(f"[tuya-local] load db: {e}")
        db = None
    if db is not None:
        with _TL_DB_LOCK:
            _TL_DB = db
    return db


def lookup_tuya_local(product_id):
    if not HAS_YAML:
        return None
    db = _load_tuya_local_db()
    if db is None:
        # БД ещё нет — соберём в фоне (не блокируем HTTP-запрос).
        if not _TL_DB_BUILDING:
            threading.Thread(target=_build_tuya_local_db, daemon=True,
                             name="tuya-local-index").start()
        return None
    return db.get(str(product_id))


def _tuya_local_update_worker():
    """v1.28.65: скачать tuya-local и построить индекс — с прогрессом."""
    def _set(**kw):
        with TUYA_LOCAL_STATE_LOCK:
            TUYA_LOCAL_STATE.update(kw)

    _set(running=True, phase="download", current=0, total=0,
         message="Фаза 1/2: скачивание tuya-local…",
         mb_done=0, mb_total=0, attempt=0, attempts=0,
         dl_stats=None, report=None,
         started_at=int(time.time()), finished_at=0, ok=None, error="")
    _t_start = int(time.time())
    ok, err = download_tuya_local_db(max_retries=3)
    if not ok:
        _set(running=False, phase="", current=0, total=0,
             message="Ошибка скачивания", finished_at=int(time.time()),
             ok=False, error=err or "download failed")
        return
    with TUYA_LOCAL_STATE_LOCK:
        _dl = dict(TUYA_LOCAL_STATE.get("dl_stats") or {})
    # v1.33.9: фаза 2 называет число файлов — видно, что идёт импорт.
    _set(phase="import", current=0, total=0,
         message=f"Фаза 2/2: импорт (индекс), YAML: {_dl.get('yaml_files', '?')}…",
         ok=None, error="")
    try:
        _build_tuya_local_db(
            progress=lambda i, t: _set(current=i, total=t))
    except Exception as e:
        _set(running=False, phase="", message="Ошибка импорта",
             finished_at=int(time.time()), ok=False, error=str(e))
        return
    with _TL_DB_LOCK:
        n = len(_TL_DB) if isinstance(_TL_DB, dict) else 0
    # v1.33.9: отчёт для карточек в UI (как у пересборки tinytuya).
    _report = {
        "yaml_files": _dl.get("yaml_files", 0),
        "product_ids": n,
        "download_mb": _dl.get("download_mb", 0),
        "download_sec": _dl.get("download_sec", 0),
        "attempts_used": _dl.get("attempts_used", 0),
        "total_sec": int(time.time()) - _t_start,
        "fetched_at": int(time.time()),
    }
    _set(running=False, phase="done", current=1, total=1,
         message=f"Готово: {n} product_id", finished_at=int(time.time()),
         ok=True, report=_report)


def parse_tuya_local_yaml(yaml_data):
    result = {}
    if not isinstance(yaml_data, dict):
        return None
    entities = yaml_data.get("entities", [])
    if not isinstance(entities, list):
        return None
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        component = ent.get("entity", "sensor")
        dps = ent.get("dps", [])
        if not isinstance(dps, list):
            continue
        for dp in dps:
            if not isinstance(dp, dict):
                continue
            dp_id = dp.get("id")
            if dp_id is None:
                continue
            dp_id_str = str(dp_id)
            entry = {
                "component": component,
                "name": dp.get("name", f"dp_{dp_id}"),
            }
            for k in ("device_class", "unit", "state_class", "scale",
                      "min", "max", "step", "options", "range"):
                if k in dp:
                    entry[k] = dp[k]
            result[dp_id_str] = entry
    return result if result else None


def merge_dps_maps(base, override):
    if not override:
        return base
    merged = dict(base)
    for dp, info in override.items():
        if dp in merged:
            m = dict(merged[dp])
            m.update(info)
            merged[dp] = m
        else:
            merged[dp] = info
    return merged


# ==================== REBUILD tinytuya_devices.json ====================
def _rebuild_tinytuya_json_worker(device_names, stop_first=True, skip_battery=True):
    with REBUILD_LOCK:
        REBUILD_STATE["running"] = True
        REBUILD_STATE["current"] = 0
        REBUILD_STATE["total"] = len(device_names)
        REBUILD_STATE["device"] = ""
        REBUILD_STATE["errors"] = []
        REBUILD_STATE["started_at"] = int(time.time())
        REBUILD_STATE["finished_at"] = 0
        REBUILD_STATE["ok"] = None
        REBUILD_STATE["report"] = None

    # v1.28.22: читаем Cloud-кэш ОДИН раз, а не в цикле по устройствам.
    # Раньше load_cloud_cache() вызывался внутри for-цикла — 40 устройств
    # = 40 чтений файла ~500 KB.
    _cloud_cache_raw = (load_cloud_cache() or {}).get("devices") or []

    existing_by_id = load_tinytuya_devices_json()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config_devices = json.load(f)
    except Exception as e:
        log.error(f"[Rebuild] Не удалось прочитать {CONFIG_FILE}: {e}")
        with REBUILD_LOCK:
            REBUILD_STATE["running"] = False
            REBUILD_STATE["finished_at"] = int(time.time())
            REBUILD_STATE["ok"] = False
            REBUILD_STATE["errors"].append(f"config: {e}")
        return

    config_by_name = {d.get("name"): d for d in config_devices if d.get("name")}
    results = []
    errors = []
    report = []
    skipped_battery = []

    for idx, name in enumerate(device_names, 1):
        if STOP_EVENT.is_set():
            break
        with REBUILD_LOCK:
            REBUILD_STATE["current"] = idx
            REBUILD_STATE["device"] = name

        cfg = config_by_name.get(name)
        if not cfg:
            errors.append(f"{name}: нет в {CONFIG_FILE}")
            continue
        # v1.31.0: батарейные спят и на probe не отвечают — не тратим TCP
        # (правило №1). Но прежнюю базу для них сохраняем: она — единственный
        # источник маппинга для спящих устройств.
        if skip_battery and cfg.get("battery_powered"):
            skipped_battery.append(name)
            _b_ex = existing_by_id.get(cfg.get("id", ""))
            _b_map = ((_b_ex or {}).get("mapping") or {}) if _b_ex else {}
            if _b_map:
                results.append({
                    "id": cfg.get("id", ""), "name": cfg.get("friendly_name", name),
                    "key": cfg.get("local_key", ""),
                    "product_id": cfg.get("product_id", "") or cfg.get("model", ""),
                    "product_name": cfg.get("model", ""),
                    "category": cfg.get("category", ""),
                    "ip": cfg.get("ip", ""),
                    "version": cfg.get("version", "3.3"),
                    "mapping": _b_map,
                    "_source": "existing",
                })
            report.append({"name": name, "status": "battery_skipped",
                           "versions": [], "dps": 0, "matched": len(_b_map),
                           "verified": 0, "mismatch": 0, "ambiguous": 0,
                           "kept_existing": len(_b_map),
                           "source": "existing" if _b_map else ""})
            continue
        dev_id = cfg.get("id", "")
        ip = cfg.get("ip", "")
        local_key = cfg.get("local_key", "")
        product_id = cfg.get("product_id", "") or cfg.get("model", "")
        if not dev_id or not ip or not local_key:
            errors.append(f"{name}: нет id/ip/local_key")
            continue

        try:
            versions, local_dps = detect_version(ip, dev_id, local_key,
                                                 stop_first=stop_first,
                                                 name=cfg.get("friendly_name", name))
        except Exception as e:
            errors.append(f"{name}: probe: {e}")
            continue
        existing = existing_by_id.get(dev_id)
        existing_mapping = (existing or {}).get("mapping", {}) if existing else {}
        item = {"name": name, "status": "ok", "versions": versions,
                "dps": len(local_dps or {}), "matched": 0, "verified": 0,
                "mismatch": 0, "ambiguous": 0, "kept_existing": 0, "source": ""}
        if not versions:
            errors.append(f"{name}: probe не ответил")
            item["status"] = "probe_failed"
            if existing_mapping:
                # живое устройство не ответило (спит) — оставляем прежнюю базу
                results.append({
                    "id": dev_id, "name": cfg.get("friendly_name", name),
                    "key": local_key, "product_id": product_id,
                    "product_name": cfg.get("model", ""),
                    "category": cfg.get("category", ""),
                    "ip": ip, "version": cfg.get("version", "3.3"),
                    "mapping": existing_mapping,
                    "_source": "existing",
                })
                item["matched"] = len(existing_mapping)
                item["kept_existing"] = len(existing_mapping)
                item["source"] = "existing"
            report.append(item)
            continue

        matched = {}
        # v1.25.0 (release): cloud_status_meta берём из Cloud-индексов
        # (TUYA_CLOUD_MAPPING_BY_ID), а не из cfg — в devices_config.json
        # этих полей нет, поэтому match_dps_to_codes() всегда работал
        # по пустым спискам и не сопоставлял DP.
        cloud_status_meta = []
        cloud_current_values = {}
        _cm = _get_cloud_mapping(dev_id, cfg.get("friendly_name", name))
        if _cm:
            for _dp, _m in _cm.items():
                if not isinstance(_m, dict):
                    continue
                _code = _m.get("code", "")
                if not _code:
                    continue
                cloud_status_meta.append({
                    "code": _code,
                    "type": _m.get("type", ""),
                    "values": _m.get("values", {}),
                    "name": _m.get("name", ""),
                })
            # v1.28.22: _cloud_cache_raw прочитан до цикла.
            for _d in _cloud_cache_raw:
                if _d.get("id") == dev_id:
                    _st = _d.get("_raw_cloud", {}).get("status", [])
                    if isinstance(_st, list):
                        for _item in _st:
                            if isinstance(_item, dict) and _item.get("code"):
                                cloud_current_values[_item["code"]] = _item.get("value")
                    break
        if cloud_status_meta and cloud_current_values:
            try:
                matched = match_dps_to_codes(local_dps, cloud_status_meta,
                                             cloud_current_values,
                                             cloud_dp_mapping=_cm)
            except Exception as e:
                errors.append(f"{name}: match: {e}")

        # v1.31.0: метрики качества — до того, как пометки матчера срезаются
        # в чистую схему базы (code/type/values/name).
        for _m in matched.values():
            if not isinstance(_m, dict):
                continue
            if _m.get("_ambiguous"):
                item["ambiguous"] += 1
            elif _m.get("_mismatch"):
                item["mismatch"] += 1
            else:
                item["verified"] += 1

        mapping = {}
        for dp, m in matched.items():
            if not isinstance(m, dict):
                continue
            mapping[dp] = {
                "code": m.get("code", ""),
                "type": m.get("type", ""),
                "values": m.get("values", {}),
                "name": m.get("name", ""),
            }

        # v1.31.0: устройство на probe отдаёт все свои DP, поэтому прежняя
        # база берётся ТОЛЬКО для DP, которых сейчас не было (иначе старая
        # ошибочная привязка жила бы вечно).
        for dp, m in existing_mapping.items():
            if dp not in mapping and str(dp) not in (local_dps or {}):
                mapping[dp] = m
                item["kept_existing"] += 1

        if not mapping and product_id:
            tl_map = lookup_tuya_local(product_id)
            if tl_map:
                for dp, info in tl_map.items():
                    mapping[dp] = {
                        "code": info.get("name", f"dp_{dp}"),
                        "type": "Integer" if info.get("unit") else "Boolean",
                        "values": {},
                        "name": info.get("name", ""),
                    }

        if not mapping:
            errors.append(f"{name}: mapping пуст (нет Cloud-кэша и tuya-local)")
            item["status"] = "empty"
            report.append(item)
            continue

        item["matched"] = len(mapping)
        if not item["source"]:
            item["source"] = "cloud" if matched else "existing"
        results.append({
            "id": dev_id,
            "name": cfg.get("friendly_name", name),
            "key": local_key,
            "product_id": product_id,
            "product_name": cfg.get("model", ""),
            "category": cfg.get("category", ""),
            "ip": ip,
            "version": (versions[0] if versions else cfg.get("version", "3.3")),
            "mapping": mapping,
            "_source": "rebuild",
        })
        report.append(item)

        time.sleep(0.05)

    # --- запись базы: атомарно (tmp + os.replace), v1.31.0 ---
    saved = True
    if not results and existing_by_id:
        # v1.31.1: fail-safe — не затираем непустую базу пустой. Так бывает,
        # если сеть отвалилась и ни одно устройство не ответило: терять
        # единственный источник маппинга нельзя, лучше оставить прежнюю базу
        # и сказать об этом в отчёте.
        saved = False
        errors.append("база не перезаписана: ни одно устройство не дало данных")
        log.warning("[Rebuild] ни одного устройства — прежняя база оставлена как есть")
    if saved:
        try:
            os.makedirs(os.path.dirname(TINYTUYA_DEVICES_FILE) or ".", exist_ok=True)
            _tmp = TINYTUYA_DEVICES_FILE + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            os.replace(_tmp, TINYTUYA_DEVICES_FILE)
            log.info(f"[Rebuild] {TINYTUYA_DEVICES_FILE}: {len(results)} устройств")
        except Exception as e:
            saved = False
            errors.append(f"save: {e}")
            log.error(f"[Rebuild] save: {e}")
            try:
                os.unlink(TINYTUYA_DEVICES_FILE + ".tmp")
            except Exception:
                pass

    # v1.32.29: что именно изменилось в базе относительно прежней (плитки отчёта).
    def _rb_key(entry):
        # v1.32.35: в базе карта DP лежит в поле "mapping" (не "dps_map") —
        # из-за этого «обновлено» всегда было 0, а всё попадало в «без изменений».
        try:
            e = entry or {}
            return json.dumps(e.get("mapping") or e.get("dps_map") or {},
                              sort_keys=True, ensure_ascii=False)
        except Exception:
            return ""
    _added = _updated = _unchanged = 0
    for _e in results:
        _id = (_e or {}).get("id", "")
        if not _id:
            continue
        _old = existing_by_id.get(_id)
        if not _old:
            _added += 1
        elif _rb_key(_old) != _rb_key(_e):
            _updated += 1
        else:
            _unchanged += 1
    _new_ids = {(e or {}).get("id", "") for e in results}
    _removed = sum(1 for _id in existing_by_id if _id and _id not in _new_ids)

    summary = {
        "devices": len(results),
        "added": _added,
        "updated": _updated,
        "unchanged": _unchanged,
        "removed": _removed,
        "probed_ok": sum(1 for r in report if r["status"] == "ok"),
        "probe_failed": sum(1 for r in report if r["status"] == "probe_failed"),
        "empty": sum(1 for r in report if r["status"] == "empty"),
        "battery_skipped": len(skipped_battery),
        "verified": sum(r["verified"] for r in report),
        "mismatch": sum(r["mismatch"] for r in report),
        "ambiguous": sum(r["ambiguous"] for r in report),
        "kept_existing": sum(r["kept_existing"] for r in report),
        "errors": len(errors),
        "stop_first": bool(stop_first),
        "skip_battery": bool(skip_battery),
        "fetched_at": int(time.time()),
    }
    with REBUILD_LOCK:
        REBUILD_STATE["running"] = False
        REBUILD_STATE["finished_at"] = int(time.time())
        REBUILD_STATE["ok"] = saved and (len(errors) == 0 or len(results) > 0)
        REBUILD_STATE["errors"] = errors
        REBUILD_STATE["report"] = {"summary": summary, "devices": report}
    log.info(f"[Rebuild] Готово: {len(results)} в базе, ошибок {len(errors)}; "
             f"verified={summary['verified']} mismatch={summary['mismatch']} "
             f"ambiguous={summary['ambiguous']} kept={summary['kept_existing']} "
             f"battery_skipped={summary['battery_skipped']}")


# ==================== HTML ====================

# ==================== FRONTEND ASSETS (v1.29.0) ====================
# v1.29.0: HTML/CSS/JS вынесены из webui.py в файлы:
#   templates/index.html  — HTML-скелет + инлайн-конфиг;
#   static/app.css        — стили;
#   static/app.js         — скрипты (статический, без плейсхолдеров).
# Бэкенд только подставляет значения и раздаёт файлы.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
FRONTEND_STATIC_DIR = os.path.join(BASE_DIR, "static")
INDEX_TEMPLATE_FILE = os.path.join(FRONTEND_TEMPLATES_DIR, "index.html")

# Разрешённые статические файлы → Content-Type.
STATIC_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}
_ASSETS = {}
_ASSETS_LOCK = threading.Lock()


def _read_asset(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_index_template():
    """templates/index.html (читается один раз, дальше из памяти)."""
    with _ASSETS_LOCK:
        if "index" not in _ASSETS:
            try:
                _ASSETS["index"] = _read_asset(INDEX_TEMPLATE_FILE)
            except Exception as e:
                log.error(f"[WebUI] не читается {INDEX_TEMPLATE_FILE}: {e}")
                _ASSETS["index"] = (
                    '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
                    '<title>MQTT Tuya Bridge</title></head><body>'
                    '<h1>WebUI</h1><p>Не найден templates/index.html</p></body></html>'
                )
        return _ASSETS["index"]


def load_static_asset(name):
    """static/<name> (читается один раз, дальше из памяти)."""
    with _ASSETS_LOCK:
        if name not in _ASSETS:
            try:
                _ASSETS[name] = _read_asset(os.path.join(FRONTEND_STATIC_DIR, name))
            except Exception as e:
                log.error(f"[WebUI] не читается static/{name}: {e}")
                _ASSETS[name] = ""
        return _ASSETS[name]


# ==================== HEALTH / AUDIT (v1.21.2) ====================
_config_audit_lock = threading.Lock()


def audit_init():
    """Создать пустой config_audit.log, если нет."""
    try:
        os.makedirs(os.path.dirname(CONFIG_AUDIT_FILE) or ".", exist_ok=True)
        if not os.path.exists(CONFIG_AUDIT_FILE):
            open(CONFIG_AUDIT_FILE, "w", encoding="utf-8").close()
            log.info(f"[Audit] Создан пустой {CONFIG_AUDIT_FILE}")
    except Exception as e:
        log.warning(f"[Audit] init: {e}")


def audit_log(op, device=None, changes=None, ok=True, error=None, extra=None):
    """Записать событие в config_audit.log (JSONL)."""
    try:
        entry = {"ts": int(time.time()), "op": op, "ok": bool(ok)}
        if device:
            entry["device"] = device
        if changes:
            entry["changes"] = changes
        if error:
            entry["error"] = str(error)
        if extra and isinstance(extra, dict):
            entry.update(extra)
        line = json.dumps(entry, ensure_ascii=False)
        with _config_audit_lock:
            rotation_failed = False
            try:
                if (os.path.exists(CONFIG_AUDIT_FILE)
                        and os.path.getsize(CONFIG_AUDIT_FILE) > AUDIT_MAX_BYTES):
                    # v1.26.0: классическая схема ротации.
                    # 1. удаляем самый старый бэкап (audit.log.N);
                    # 2. сдвигаем audit.log.N-1 → audit.log.N,
                    #    ..., audit.log.1 → audit.log.2;
                    # 3. audit.log → audit.log.1.
                    # ВАЖНО: внутри цикла src всегда = f"{CONFIG_AUDIT_FILE}.{i}",
                    # НЕ подменяем его на CONFIG_AUDIT_FILE для i == 1 —
                    # иначе финальный os.replace(base, base.1) упадёт
                    # на уже перемещённый файл (регресс 1.25.14).
                    oldest = f"{CONFIG_AUDIT_FILE}.{AUDIT_BACKUPS}"
                    if os.path.exists(oldest):
                        os.unlink(oldest)
                    for i in range(AUDIT_BACKUPS - 1, 0, -1):
                        src = f"{CONFIG_AUDIT_FILE}.{i}"
                        dst = f"{CONFIG_AUDIT_FILE}.{i + 1}"
                        if os.path.exists(src):
                            os.replace(src, dst)
                    os.replace(CONFIG_AUDIT_FILE, f"{CONFIG_AUDIT_FILE}.1")
            except Exception as e:
                # v1.22.6: если ротация упала — НЕ пишем в исходный файл,
                # иначе он растёт без ограничений. Логируем и выходим.
                rotation_failed = True
                log.error(f"[Audit] rotate FAILED — запись пропущена: {e}")
            if not rotation_failed:
                with open(CONFIG_AUDIT_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
    except Exception as e:
        log.warning(f"[Audit] write: {e}")


def audit_cleanup(keep_seconds=0, purge_all=False):
    """v1.28.74: обрезать config_audit.log — оставить записи новее cutoff.

    purge_all=True — удалить все записи. Возвращает число удалённых.
    """
    try:
        if not os.path.exists(CONFIG_AUDIT_FILE):
            return 0
        cutoff = 0 if purge_all else (int(time.time()) - int(keep_seconds))
        with open(CONFIG_AUDIT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        kept, removed = [], 0
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            try:
                it = json.loads(s)
            except Exception:
                removed += 1
                continue
            if purge_all or int(it.get("ts", 0)) >= cutoff:
                kept.append(ln if ln.endswith("\n") else ln + "\n")
            else:
                removed += 1
        tmp = CONFIG_AUDIT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, CONFIG_AUDIT_FILE)
        log.info(f"[Audit] cleanup: удалено {removed}, осталось {len(kept)}")
        return removed
    except Exception as e:
        log.warning(f"[Audit] cleanup: {e}")
        return 0


def audit_read(limit=100):
    """Прочитать последние N записей (свежие сверху)."""
    try:
        if not os.path.exists(CONFIG_AUDIT_FILE):
            return []
        # P2 1.22.0: deque(maxlen) — не грузим весь файл
        with open(CONFIG_AUDIT_FILE, "r", encoding="utf-8") as f:
            lines = deque(f, maxlen=limit)
        items = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue
        items.reverse()
        return items
    except Exception as e:
        log.warning(f"[Audit] read: {e}")
        return []


_health_last_flush = [0, 0, 0]
_health_lock = threading.Lock()


def _proc_self_stats():
    """CPU% и RSS текущего процесса (WebUI)."""
    result = {"cpu_pct": None, "rss_mb": None, "threads": None}
    try:
        with open("/proc/self/stat", "r") as f:
            parts = f.read().split()
        if len(parts) > 15:
            utime = int(parts[13]); stime = int(parts[14])
            threads = int(parts[19]) if len(parts) > 19 else None
            result["threads"] = threads
            now = time.time()
            total_ticks = utime + stime
            # P2 1.22.0: критическая секция под _health_lock
            with _health_lock:
                prev = tuple(_health_last_flush)
                if prev[1] > 0:
                    dt = now - prev[1]
                    dticks = total_ticks - prev[0]
                    if dt > 0:
                        hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
                        result["cpu_pct"] = round((dticks / hz) / dt * 100, 1)
                _health_last_flush[0] = total_ticks
                _health_last_flush[1] = now
    except Exception:
        pass
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    result["rss_mb"] = round(kb / 1024, 1)
                    break
    except Exception:
        pass
    return result


def _db_stats():
    """Размер БД и количество строк в статусах/задержках."""
    out = {"size_mb": None, "status_events": None,
           "latency_history": None}
    try:
        if os.path.exists(DB_FILE):
            out["size_mb"] = round(os.path.getsize(DB_FILE) / (1024 * 1024), 2)
    except Exception:
        pass
    with _db_lock:
        if _db_conn is None:
            return out
        try:
            if STATUS_HISTORY_ENABLED:
                out["status_events"] = _db_conn.execute(
                    "SELECT COUNT(*) FROM status_events").fetchone()[0]
                out["latency_history"] = _db_conn.execute(
                    "SELECT COUNT(*) FROM latency_history").fetchone()[0]
        except Exception:
            pass
    return out


def collect_health():
    """Собрать health-инфо для /api/health/full."""
    proc = _proc_self_stats()
    db = _db_stats()
    with STATE_LOCK:
        bridge_status = STATE["bridge_status"]
        bridge_uptime = STATE["uptime"]
        bridge_version = STATE["version"]
        # v1.28.3: CPU/RAM bridge
        bridge_cpu_pct = STATE.get("bridge_cpu_pct")
        bridge_rss_mb = STATE.get("bridge_rss_mb")
        total = len(STATE["devices"])
        online = sum(1 for d in STATE["devices"].values() if d.get("status") == "online")
        names = list(STATE["devices"].keys())
    with QUIET_LOCK:
        quiet_total = len([n for n in QUIET_CONFIG if QUIET_CONFIG[n].get("windows")])
    quiet_now = 0
    for n in names:
        if is_quiet_now(n):
            quiet_now += 1
    return {
        "webui": {
            "version": WEBUI_VERSION,
            "cpu_pct": proc["cpu_pct"],
            "rss_mb": proc["rss_mb"],
            "threads": proc["threads"],
        },
        "bridge": {
            "version": bridge_version,
            "status": bridge_status,
            "uptime": bridge_uptime,
            # v1.28.3: CPU/RAM bridge (может быть None)
            "cpu_pct": bridge_cpu_pct,
            "rss_mb": bridge_rss_mb,
        },
        "devices": {"total": total, "online": online},
        "quiet": {"total": quiet_total, "now": quiet_now},
        "db": db,
    }


def render_html():
    analytics_js = "true" if ANALYTICS_ENABLED else "false"
    status_js = "true" if STATUS_HISTORY_ENABLED else "false"
    return (load_index_template()
            .replace("__ANALYTICS_ENABLED__", analytics_js)
            .replace("__STATUS_HISTORY_ENABLED__", status_js)
            .replace("__WEBUI_VERSION__", WEBUI_VERSION)
            # v1.32.33: публичный номер релиза — для подвала и сверки с GitHub.
            .replace("__RELEASE_TAG__", RELEASE_TAG)
            # v1.27.6: подсеть MQTT — fallback для пресета IP-префикса.
            .replace("__MQTT_BROKER__", MQTT_BROKER or ""))


# ==================== HTTP ====================
def _qs_int(qs, key, default):
    """v1.28.34: безопасный int из query-параметра (битый ?hours=abc)."""
    try:
        return int(qs.get(key, [str(default)])[0])
    except (TypeError, ValueError):
        return default


class _BodyError(Exception):
    """v1.31.19: некорректное тело запроса (тип или размер) → JSON-ошибка, а не молчание."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class WebUIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # v1.28.63: клиент отключился — не шумим трейсбеком

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # v1.28.63: клиент отключился

    def _send_static(self, name):
        """v1.29.0: static/app.css|app.js. В URL есть ?v=<версия> →
        файл можно кэшировать надолго (immutable)."""
        ctype = STATIC_FILES.get(name)
        if not ctype:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        body = load_static_asset(name).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # v1.24.5: PWA — SVG-иконка 🌉 + manifest.json.
    # Отдаются из Python-строк, никаких внешних файлов.
    _PWA_SVG_ICON = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'>"
        "<rect width='512' height='512' rx='96' fill='#0d1117'/>"
        "<text x='256' y='256' font-size='330' text-anchor='middle'"
        " dominant-baseline='central'>\U0001F309</text>"
        "</svg>"
    )

    def _send_favicon_svg(self):
        body = self._PWA_SVG_ICON.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _send_manifest(self):
        import base64 as _b64
        svg_b64 = _b64.b64encode(self._PWA_SVG_ICON.encode("utf-8")).decode("ascii")
        icon_data = "data:image/svg+xml;base64," + svg_b64
        manifest = {
            "name": "MQTT Tuya Bridge",
            "short_name": "Tuya Bridge",
            "description": "MQTT Tuya Bridge — локальное управление устройствами",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#0d1117",
            "theme_color": "#0d1117",
            "icons": [
                {"src": icon_data, "sizes": "192x192",
                 "type": "image/svg+xml", "purpose": "any"},
                {"src": icon_data, "sizes": "512x512",
                 "type": "image/svg+xml", "purpose": "any"},
                {"src": icon_data, "sizes": "512x512",
                 "type": "image/svg+xml", "purpose": "maskable"},
            ],
        }
        body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        """v1.31.19: тело — только JSON, с ограничением размера и типа.

        Content-Length заявлен клиентом: без лимита поток висит на `1e9`,
        а без проверки Content-Type cross-site POST с `text/plain` проходит
        без CORS-preflight. Оба случая закрыты здесь.
        """
        try:
            cl = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            raise _BodyError(400, "invalid Content-Length")
        if cl <= 0:
            return None
        if cl > MAX_BODY_BYTES:
            # v1.31.19: тело не читаем (может быть огромным) — отвечаем и закрываем.
            self.close_connection = True
            raise _BodyError(413, f"body too large (max {MAX_BODY_BYTES} bytes)")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # v1.31.19: иначе cross-site POST с text/plain проходит без preflight.
            # Тело (оно в пределах лимита) вычитываем, чтобы при закрытии не ушёл RST
            # и клиент гарантированно получил 415.
            try:
                self.rfile.read(cl)
            except Exception:
                pass
            raise _BodyError(415, "Content-Type must be application/json")
        try:
            raw = self.rfile.read(cl)
        except Exception:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _BodyError(400, "invalid JSON body")

    @staticmethod
    def _parse_dev_path(path, suffix):
        prefix = "/api/device/"
        if not path.startswith(prefix) or not path.endswith(suffix): return None
        return path[len(prefix):-len(suffix)].rstrip("/")

    def do_GET(self):
        parsed = urlparse(self.path); path = unquote(parsed.path); qs = parse_qs(parsed.query)

        if path == "/manifest.json":
            self._send_manifest()
            return
        if path == "/favicon.svg":
            self._send_favicon_svg()
            return

        # v1.29.0: статика вынесена в файлы (static/app.css, static/app.js).
        if path == "/static/app.css":
            self._send_static("app.css")
            return
        if path == "/static/app.js":
            self._send_static("app.js")
            return

        if path == "/":
            self._send_html(200, render_html())
            return
        if path == "/analytics":
            if not ANALYTICS_ENABLED:
                self._redirect("/")
                return
            self._send_html(200, render_html())
            return
        if path == "/import":
            self._send_html(200, render_html())
            return
        if path == "/tools":
            self._send_html(200, render_html())
            return
        if path == "/help":
            self._send_html(200, render_html())
            return

        if path == "/api/update/check":
            # v1.32.34: есть ли на GitHub релиз новее текущего (кэш 6 часов).
            self._send_json(200, {"ok": True, "current": RELEASE_TAG,
                                  **check_new_release()})
            return

        if path == "/healthz":
            with STATE_LOCK: st = STATE["bridge_status"]
            payload = {"status": "ok" if st == "online" else "unhealthy", "bridge": st,
                       "analytics": ANALYTICS_ENABLED, "status_history": STATUS_HISTORY_ENABLED}
            self._send_json(200 if st == "online" else 503, payload)
            return

        if path == "/api/health/full":
            try:
                self._send_json(200, collect_health())
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/status":
            meta_snap = snapshot_device_meta()
            with STATE_LOCK:
                status = {
                    "version": STATE["version"], "bridge_status": STATE["bridge_status"],
                    "uptime": STATE["uptime"],
                    "analytics_enabled": ANALYTICS_ENABLED, "status_history_enabled": STATUS_HISTORY_ENABLED,
                    "webui_version": WEBUI_VERSION,
                    # v1.28.27: режим пинга bridge + время старта (для grace).
                    "ping_mode": STATE.get("bridge_ping_mode"),
                    "bridge_started_at": STATE.get("bridge_started_at", 0),
                    # v1.33.8: отклик на команду / окно защиты от «эха».
                    "cmd_ack": STATE.get("bridge_cmd_ack"),
                    "devices": [],
                }
                # v1.27.7c: union STATE и DEVICE_META — иначе
                # отключённые (enabled:false) не видны: bridge 1.8.4
                # их не шлёт в MQTT → нет в STATE → нет в ответе.
                # v1.32.5: копируем и вложенный cache — MQTT-поток обновляет его
                # на месте, а раньше на время enrich нас защищал STATE_LOCK
                # (иначе возможен «dictionary changed size during iteration»).
                _devs_snap = {}
                for _n, _v in STATE["devices"].items():
                    _vv = dict(_v)
                    _c = _v.get("cache")
                    if isinstance(_c, dict):
                        _vv["cache"] = dict(_c)
                    _devs_snap[_n] = _vv
                _all_names = set(_devs_snap) | set(meta_snap.keys())
            # v1.32.2: тяжёлые enrich/quiet считаем ВНЕ STATE_LOCK. Раньше лок
            # держался на весь цикл (файлы, вложенные локи) — MQTT-поток ждал
            # и статусы устройств «залипали».
            for name in sorted(_all_names):
                info = _devs_snap.get(name, {})
                m = meta_snap.get(name, {})
                # v1.25.0 (fix #cloud_dps): дополняем dps_map из Cloud-mapping
                # теми DP, что bridge прислал в cache, но их нет в config.
                _dps_map_full = _enrich_dps_map_from_cache(
                    m.get("dps_map", {}),
                    info.get("cache", {}),
                    m.get("tuya_id", ""),
                    m.get("friendly_name", name),
                    m.get("product_id", ""),   # v1.28.33.fixup5b
                    name,                          # exclude_name
                    m.get("type", ""),             # v1.28.34: dev_type
                    _get_cloud_writable(m.get("tuya_id", ""),
                                        m.get("friendly_name", name)),  # v1.28.34
                )
                # v1.28.19: вычисляем причину один раз (был 3x вызов
                # get_device_meta + is_quiet_now на каждое устройство).
                _lat_reason = _ping_hidden_reason(name)
                status["devices"].append({
                    "name": name, "friendly_name": m.get("friendly_name", name),
                    "type": m.get("type", "unknown"), "model": m.get("model", ""),
                    "ip": m.get("ip", ""), "version": m.get("version", ""),
                    "battery_powered": m.get("battery_powered", False),
                    # v1.27.7: enabled — для UI-отображения (серые
                    # строки, свёрнутая секция «Отключённые»).
                    "enabled": bool(m.get("enabled", True)),
                    # v1.31.1: expire_after — ✏️ показывает текущее значение
                    # и «Сбросить по умолчанию» только когда поле задано.
                    "expire_after": m.get("expire_after"),
                    "tuya_id": m.get("tuya_id", ""),
                    "local_key_present": bool(m.get("local_key")),
                    "dps_map": _dps_map_full,
                    "presets": m.get("presets", []), "preset_map": m.get("preset_map", {}),
                    "min_temp": m.get("min_temp"), "max_temp": m.get("max_temp"), "temp_step": m.get("temp_step"),
                    "status": info.get("status", "unknown"),
                    "last_seen": info.get("last_seen"),
                    # v1.28.6: battery_alert / battery_last_up
                    # (для батарейных; для остальных None).
                    "battery_alert": info.get("battery_alert"),
                    "battery_last_up": info.get("battery_last_up"),
                    # v1.28.2: latency_hidden — не измеряется (battery/
                    # disabled/quiet). latency_ms = None чтобы не залипало
                    # старое значение timeout.
                    "latency_ms": (None if _lat_reason
                                   else info.get("latency_ms")),
                    "latency_ts": info.get("latency_ts"),
                    "latency_hidden": _lat_reason is not None,
                    "latency_reason": _lat_reason,
                    # v1.32.2: поле "history" убрано — его никто не читал
                    # (историю подтягивает сам UI из своего кэша).
                    "cache": info.get("cache", {}),
                    "quiet": is_quiet_now(name),
                    "quiet_until": quiet_until_ts(name),
                    "quiet_windows": quiet_windows_of(name),
                })
            # v1.22.1: безопасный sort по friendly_name (str + fallback)
            status["devices"].sort(
                key=lambda d: str(d.get("friendly_name") or d.get("name") or "").lower())
            self._send_json(200, status)
            return

        if path == "/api/config/audit":
            try:
                limit = int(qs.get("limit", ["100"])[0])
                if limit < 1: limit = 100
                if limit > 1000: limit = 1000
            except Exception:
                limit = 100
            self._send_json(200, {"ok": True, "items": audit_read(limit)})
            return

        if path == "/api/config/raw":
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                self._send_json(200, {"ok": True, "config": config})
            except FileNotFoundError:
                self._send_json(404, {"ok": False, "error": "файл не найден"})
            except json.JSONDecodeError as e:
                self._send_json(500, {"ok": False, "error": f"JSON parse error: {e}"})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/base/info":
            result = {
                "tinytuya": {"exists": False, "age": None, "modified": None, "count": 0},
                "tuya_local": {"exists": False, "age": None, "modified": None, "count": 0},
            }
            if os.path.exists(TINYTUYA_DEVICES_FILE):
                try:
                    st = os.stat(TINYTUYA_DEVICES_FILE)
                    with open(TINYTUYA_DEVICES_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    result["tinytuya"] = {
                        "exists": True,
                        "age": int(time.time() - st.st_mtime),
                        "modified": int(st.st_mtime),
                        "count": len(data) if isinstance(data, list) else 0,
                    }
                except Exception as e:
                    log.warning(f"[Base] tinytuya info: {e}")
            if os.path.isdir(TUYA_LOCAL_YAML_DIR):
                try:
                    files = [f for f in os.listdir(TUYA_LOCAL_YAML_DIR) if f.endswith(".yaml")]
                    if files:
                        latest = max(os.stat(os.path.join(TUYA_LOCAL_YAML_DIR, f)).st_mtime for f in files)
                        result["tuya_local"] = {
                            "exists": True,
                            "age": int(time.time() - latest),
                            "modified": int(latest),
                            "count": len(files),
                        }
                except Exception as e:
                    log.warning(f"[Base] tuya-local info: {e}")
            self._send_json(200, result)
            return

        if path == "/api/config/backups":
            # v1.12.0: список бэкапов формирует bridge — папка backup/
            # смонтирована только ему (WebUI видит config:ro).
            res = _send_request(f"{TOPIC_PREFIX}/bridge/config_backups", {},
                                timeout=EDIT_TIMEOUT_WAIT)
            if not isinstance(res, dict):
                self._send_json(200, {"ok": False, "error": "bad response",
                                      "backups": []})
                return
            self._send_json(200, res)
            return

        if path == "/api/config/report":
            # v1.11.0: отчёт по конфигу строит bridge (единый белый список).
            res = _send_request(f"{TOPIC_PREFIX}/bridge/config_report", {},
                                timeout=EDIT_TIMEOUT_WAIT)
            if not isinstance(res, dict):
                self._send_json(200, {"ok": False, "error": "bad response"})
                return
            self._send_json(200, res)
            return

        if path == "/api/base/rebuild/progress":
            with REBUILD_LOCK:
                self._send_json(200, dict(REBUILD_STATE))
            return

        if path == "/api/base/tuya-local/progress":
            with TUYA_LOCAL_STATE_LOCK:
                self._send_json(200, dict(TUYA_LOCAL_STATE))
            return

        if path == "/api/latency/refresh/progress":
            with LATENCY_REFRESH_STATE_LOCK:
                self._send_json(200, dict(LATENCY_REFRESH_STATE))
            return

        if path == "/api/cloud/cache":
            data = load_cloud_cache()
            if data is None:
                self._send_json(200, {"ok": True, "exists": False})
            else:
                self._send_json(200, {
                    "ok": True, "exists": True,
                    "fetched_at": data.get("fetched_at", 0),
                    "access_id": data.get("access_id", ""),
                    "region": data.get("region", "eu"),
                    "devices": data.get("devices", []),
                })
            return

        dev = self._parse_dev_path(path, "/secret")
        if dev is not None:
            m = get_device_meta(dev)
            if not m:
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            key = m.get("local_key", "")
            if not key:
                self._send_json(404, {"ok": False, "error": "no key"})
                return
            self._send_json(200, {"ok": True, "local_key": key})
            return

        dev = self._parse_dev_path(path, "/history")
        if dev is not None:
            if not STATUS_HISTORY_ENABLED:
                self._send_json(200, {"history": []})
                return
            hours = _qs_int(qs, "hours", 24); limit = _qs_int(qs, "limit", 100)
            self._send_json(200, {"history": db_query_dev_history(dev, hours, limit)})
            return

        dev = self._parse_dev_path(path, "/latency")
        if dev is not None:
            if not STATUS_HISTORY_ENABLED:
                self._send_json(200, {"latency": []})
                return
            hours = _qs_int(qs, "hours", 24); limit = _qs_int(qs, "limit", 2000)
            self._send_json(200, {"latency": db_query_dev_latency(dev, hours, limit)})
            return

        dev = self._parse_dev_path(path, "/avg_latency")
        if dev is not None:
            # v1.28.34: фронт шлёт latency_seconds (сек). Раньше читали
            # `hours` и передавали его как СЕКУНДЫ — выходило 24с вместо 24ч.
            _default_sec = _qs_int(qs, "hours", 24) * 3600
            latency_seconds = _qs_int(qs, "latency_seconds", _default_sec)
            data = db_query_avg_latency(dev, latency_seconds)
            if data is None:
                self._send_json(200, {"avg": None, "count": 0, "timeouts": 0})
            else:
                self._send_json(200, data)
            return

        if path == "/api/analytics":
            if not ANALYTICS_ENABLED:
                self._send_json(404, {"ok": False, "error": "disabled"})
                return
            # v1.18.8: latency_seconds — период для среднего ping (0 = всё время)
            try:
                latency_seconds = int(qs.get("latency_seconds", ["3600"])[0])
            except Exception:
                latency_seconds = 3600
            if latency_seconds < 0:
                latency_seconds = 0
            meta_snap = snapshot_device_meta()
            # P1 1.22.0: снимок под локом, SQL — вне лока.
            with STATE_LOCK:
                info_snap = {
                    n: {"latency_ms": i.get("latency_ms"),
                        "latency_ts": i.get("latency_ts")}
                    for n, i in STATE["devices"].items()
                }
            latency = []
            for name, info in info_snap.items():
                m = meta_snap.get(name, {})
                # v1.27.7b: не показываем отключённые в аналитике.
                if DISABLED_HIDE_FROM_ANALYTICS and m.get("enabled", True) is False:
                    continue
                # v1.28.5: батарейные не пингуются (_should_ping = False),
                # поэтому avg всегда None — показывать их в latency-таблице
                # бессмысленно. Исключаем.
                if m.get("battery_powered"):
                    continue
                # v1.28.27: N+1 запрос — терпимо для SQLite, но при
                # 100+ устройствах стоит перейти на один GROUP BY.
                # v1.33.24: один запрос отдаёт avg + медиану + p95 (см. ниже).
                st = db_query_latency_stats(name, latency_seconds) or {}
                latency.append({
                    "name": name,
                    "friendly_name": m.get("friendly_name", name),
                    "ip": m.get("ip", ""),
                    "latency_ms": info.get("latency_ms"),
                    "latency_ts": info.get("latency_ts"),
                    "avg_ms_24h": st.get("avg"),
                    "median_ms_24h": st.get("median"),
                    "p95_ms_24h": st.get("p95"),
                    "max_ms_24h": st.get("max"),
                    "latency_count": st.get("count", 0),
                    "latency_timeouts": st.get("timeouts", 0),
                })
            # v1.21.0: quiet hours — исключаем устройства в окне/grace
            def _is_quiet_or_grace(n):
                if is_quiet_now(n):
                    return True
                return quiet_until_ts(n) > int(time.time())
            quiet_names = set()
            with STATE_LOCK:
                all_names = list(STATE["devices"].keys())
            for n in all_names:
                if _is_quiet_or_grace(n):
                    quiet_names.add(n)
            # v1.28.11: фильтр disabled (они не пишут status_events).
            # v1.28.15: перенесено ВЫШЕ timeline — иначе UnboundLocalError.
            _disabled_names = {n for n, m in meta_snap.items()
                               if m.get("enabled", True) is False}
            timeline_raw = db_query_timeline(24, 500)
            # v1.28.11: фильтр disabled.
            timeline = [x for x in timeline_raw
                        if x.get("dev") not in quiet_names
                        and x.get("dev") not in _disabled_names]
            # v1.28.19: total тоже фильтруется — иначе UI показывал
            # «показаны 50 из 2000», а в списке после фильтра было
            # всего 300. Считаем по тем же исключениям.
            _exclude_all = quiet_names | _disabled_names
            timeline_total = db_query_timeline_total(24, _exclude_all)
            # v1.22.5: min_flaps 3 → 1, чтобы список «Мерцающие
            # устройства» совпадал с графиком «Мерцания по часам».
            flappers_raw = db_query_flappers(24, 1)
            # v1.28.11: фильтр disabled (они не пишут status_events).
            flappers = [x for x in flappers_raw
                        if x.get("dev") not in quiet_names
                        and x.get("dev") not in _disabled_names]
            # v1.22.3: график мерцаний фильтруется так же, как список flappers.
            # v1.28.19: + disabled — иначе на графике столбики есть, а
            # в списке пусто.
            flaps_hourly = _db_query_flaps_hourly_excluding(24, _exclude_all)
            self._send_json(200, {
                "activity": db_query_hourly(24),
                "flaps_hourly": flaps_hourly,
                "timeline": timeline,
                "timeline_total": timeline_total,
                "flappers": flappers,
                "latency": latency,
                "latency_seconds": latency_seconds,
            })
            return

        if path == "/api/logs/history":
            tail = _qs_int(qs, "tail", 1000)
            src = qs.get("source", ["all"])[0]
            with _log_buffer_lock:
                if src in ("bridge", "webui"):
                    items_all = [x for x in _log_buffer if x.get("source") == src]
                else:
                    items_all = list(_log_buffer)
            items = items_all[-tail:]
            self._send_json(200, {"logs": items})
            return

        if path == "/api/logs/stream":
            self._handle_sse(qs)
            return

        self.send_error(404)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            body = self._read_body()
        except _BodyError as e:
            # v1.31.19: отдаём JSON-ошибку, а не рвём соединение.
            self._send_json(e.status, {"ok": False, "error": e.message})
            return
        if body is not None and not isinstance(body, dict):
            # v1.31.19: все POST-эндпоинты принимают JSON-объект.
            self._send_json(400, {"ok": False, "error": "body must be a JSON object"})
            return

        if path == "/api/cleanup/orphans":
            # v1.12.2: удаляем только «зависшие» retained-топики (живые не трогаем)
            res = _send_request(f"{TOPIC_PREFIX}/bridge/cleanup_orphans", {},
                                timeout=EDIT_TIMEOUT_WAIT)
            if not isinstance(res, dict):
                self._send_json(200, {"ok": False, "error": "bad response"})
                return
            self._send_json(200, res)
            return

        if path == "/api/cleanup":
            # v1.12.1: ждём подтверждение от bridge (removed/republished) —
            # очистка занимает ~5-8 секунд, поэтому не «отправлено», а результат.
            res = _send_request(f"{TOPIC_PREFIX}/bridge/cleanup", {},
                                timeout=EDIT_TIMEOUT_WAIT)
            if not isinstance(res, dict):
                self._send_json(200, {"ok": False, "error": "bad response"})
                return
            self._send_json(200, res)
            return

        if path == "/api/latency/refresh":
            started, err = trigger_latency_refresh()
            self._send_json(200, {"ok": started, "error": err})
            return

        if path == "/api/base/tuya-local/update":
            with TUYA_LOCAL_STATE_LOCK:
                if TUYA_LOCAL_STATE["running"]:
                    self._send_json(200, {"ok": False, "error": "уже запущено"})
                    return
                # v1.32.2: флаг под тем же локом, что и проверка — гонка запуска.
                TUYA_LOCAL_STATE["running"] = True
            threading.Thread(target=_tuya_local_update_worker, daemon=True,
                             name="tuya-local-update").start()
            self._send_json(200, {"ok": True, "message": "обновление запущено"})
            return

        if path == "/api/base/tinytuya/rebuild":
            with REBUILD_LOCK:
                if REBUILD_STATE["running"]:
                    self._send_json(200, {"ok": False, "error": "уже запущено"})
                    return
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                names = [d.get("name") for d in cfg if d.get("name") and d.get("enabled", True)]
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"config: {e}"})
                return
            if not names:
                self._send_json(400, {"ok": False, "error": "нет устройств в конфиге"})
                return
            # v1.31.0: параметры пересборки приходят из UI.
            #  stop_first   — не перебирать все версии (меньше TCP);
            #  skip_battery — не опрашивать спящие батарейные (правило №1).
            _stop_first = bool(body.get("stop_first", True)) if body else True
            _skip_batt = bool(body.get("skip_battery", True)) if body else True
            # v1.32.2: флаг «уже запущено» ставим ДО старта потока (и после всех
            # ранних выходов) — иначе два быстрых POST успевали оба пройти проверку.
            with REBUILD_LOCK:
                REBUILD_STATE["running"] = True
            threading.Thread(target=_rebuild_tinytuya_json_worker,
                             args=(names, _stop_first, _skip_batt),
                             daemon=True, name="tinytuya-rebuild").start()
            self._send_json(200, {"ok": True, "message": "пересборка запущена",
                                  "total": len(names),
                                  "stop_first": _stop_first,
                                  "skip_battery": _skip_batt})
            return

        if path == "/api/cloud/cache":
            if body and body.get("clear"):
                ok = clear_cloud_cache()
                self._send_json(200, {"ok": ok})
                return
            if not body or not isinstance(body.get("devices"), list):
                self._send_json(400, {"ok": False, "error": "devices required"})
                return
            ok = save_cloud_cache(
                body["devices"],
                fetched_at=body.get("fetched_at"),
                access_id=body.get("access_id", ""),
                region=body.get("region", "eu"),
            )
            self._send_json(200, {"ok": ok})
            return

        if path == "/api/cloud/probe":
            if not body:
                self._send_json(400, {"ok": False, "error": "body required"})
                return
            dev_id = body.get("id", "").strip()
            ip = body.get("ip", "").strip()
            local_key = body.get("local_key", "").strip()
            if not dev_id or not ip or not local_key:
                self._send_json(400, {"ok": False, "error": "id, ip, local_key required"})
                return
            known_ips = get_known_ips()
            if ip in known_ips:
                self._send_json(200, {"ok": False, "error": f"IP {ip} уже есть в конфиге — probe пропущен"})
                return
            try:
                versions, dps = detect_version(
                ip, dev_id, local_key,
                name=(body.get("name") or body.get("friendly_name") or ""))
                if versions:
                    self._send_json(200, {"ok": True, "version": versions[0],
                                          "versions": versions,
                                          "dps_count": len(dps)})
                else:
                    self._send_json(200, {"ok": False, "error": "ни одна версия не ответила (устройство спит?) — выберите версию вручную"})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/cloud/probe_and_match":
            if not body:
                self._send_json(400, {"ok": False, "error": "body required"})
                return
            dev_id = (body.get("id") or "").strip()
            ip = (body.get("ip") or "").strip()
            local_key = (body.get("local_key") or "").strip()
            cloud_status_meta = body.get("cloud_status_meta") or []
            cloud_current_values = body.get("cloud_current_values") or {}
            cloud_mapping = body.get("cloud_mapping") or {}
            if not dev_id or not ip or not local_key:
                self._send_json(400, {"ok": False, "error": "id, ip, local_key required"})
                return
            # v1.31.19: карта облака тоже ограничена — иначе произвольный JSON гоняется
            # по алгоритму сопоставления (комментарий ниже обещал проверку размеров).
            if not isinstance(cloud_mapping, dict) or len(cloud_mapping) > 1000:
                self._send_json(400, {"ok": False, "error": "cloud_mapping must be a dict (max 1000 entries)"})
                return

            known_ips = get_known_ips()
            if ip in known_ips:
                self._send_json(200, {"ok": False, "error": f"IP {ip} уже есть в конфиге — probe пропущен"})
                return

            try:
                versions, dps = detect_version(ip, dev_id, local_key)
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"probe: {e}"})
                return
            if not versions:
                self._send_json(200, {"ok": False, "error": "ни одна версия не ответила (устройство спит?) — выберите версию вручную"})
                return
            version = versions[0]

            dp_to_code = {}
            try:
                # v1.28.27: жёсткая проверка размеров — клиент мог
                # прислать произвольный JSON и загрузить CPU.
                if (isinstance(cloud_status_meta, list)
                        and isinstance(cloud_current_values, dict)
                        and 0 < len(cloud_status_meta) <= 500
                        and 0 < len(cloud_current_values) <= 500):
                    dp_to_code = match_dps_to_codes(
                        dps, cloud_status_meta, cloud_current_values,
                        cloud_dp_mapping=cloud_mapping)
                else:
                    log.warning("[ProbeMatch] cloud_status_meta/current_values "
                                "некорректны или слишком велики — пропуск")
            except Exception as e:
                log.warning(f"[ProbeMatch] match error: {e}")

            dps_map = build_dps_map_from_matched(dp_to_code) if dp_to_code else {}
            self._send_json(200, {
                "ok": True,
                "version": version,
                "dps_count": len(dps),
                "matched_count": len(dp_to_code),
                "dp_to_code": {dp: {
                    "code": m.get("code", ""), "type": m.get("type", ""),
                    "values": m.get("values", {}), "name": m.get("name", ""),
                    # v1.30.0: пометки матчера — видны в RAW-блоке опроса.
                    **({"ambiguous": True, "candidates": m.get("_candidates")}
                       if m.get("_ambiguous") else {}),
                    **({"value_mismatch": True} if m.get("_mismatch") else {}),
                } for dp, m in dp_to_code.items()},
                "dps_map": dps_map,
                "versions": versions,
                "dps": dps,
            })
            return

        # v1.11.0: инструменты конфига (отчёт / expire_after / нормализация).
        # Всё меняет bridge — WebUI только просит и пишет запись в аудит.
        if path == "/api/config/expire_clear":
            res = _send_request(f"{TOPIC_PREFIX}/bridge/expire_clear", {},
                                timeout=EDIT_TIMEOUT_WAIT)
            if not isinstance(res, dict):
                self._send_json(200, {"ok": False, "error": "bad response"})
                return
            _names = res.get("cleared") or []
            if res.get("ok"):
                audit_log("expire_clear", changes={"cleared": len(_names)},
                          extra={"names": _names})
                # конфиг изменил bridge — перечитываем мету, иначе ✏️ покажет старое
                try:
                    load_device_meta()
                except Exception as e:
                    log.warning(f"[CfgCmd] reload meta: {e}")
            self._send_json(200, res)
            return

        if path == "/api/config/expire_fill":
            _value = body.get("value") if body else None
            res = _send_request(f"{TOPIC_PREFIX}/bridge/expire_fill",
                                {"value": _value}, timeout=EDIT_TIMEOUT_WAIT)
            if not isinstance(res, dict):
                self._send_json(200, {"ok": False, "error": "bad response"})
                return
            _names = res.get("changed") or []
            if res.get("ok"):
                audit_log("expire_fill",
                          changes={"value": res.get("value"),
                                   "changed": len(_names)},
                          extra={"names": _names})
                try:
                    load_device_meta()
                except Exception as e:
                    log.warning(f"[CfgCmd] reload meta: {e}")
            self._send_json(200, res)
            return

        if path == "/api/config/normalize":
            _dry = bool(body.get("dry_run")) if body else False
            # v1.33.5: галки из модалки — что именно исправлять.
            _req = {"dry_run": _dry}
            if body:
                if "remove_extra" in body:
                    _req["remove_extra"] = bool(body.get("remove_extra"))
                if "fix_types" in body:
                    _req["fix_types"] = bool(body.get("fix_types"))
            res = _send_request(f"{TOPIC_PREFIX}/bridge/config_normalize",
                                _req, timeout=EDIT_TIMEOUT_WAIT)
            if not isinstance(res, dict):
                self._send_json(200, {"ok": False, "error": "bad response"})
                return
            if res.get("ok") and not _dry:
                audit_log("normalize",
                          changes={"removed": res.get("removed", 0),
                                   "fixes": res.get("fixes", {})})
                try:
                    load_device_meta()
                except Exception as e:
                    log.warning(f"[CfgCmd] reload meta: {e}")
            self._send_json(200, res)
            return

        if path == "/api/config/restore":
            _backup = body.get("backup") if body else None
            res = _send_request(f"{TOPIC_PREFIX}/bridge/restore_config",
                                {"backup": _backup}, timeout=EDIT_TIMEOUT_WAIT)
            if not isinstance(res, dict):
                self._send_json(200, {"ok": False, "error": "bad response"})
                return
            if res.get("ok"):
                audit_log("restore",
                          changes={"backup": res.get("backup"),
                                   "devices": res.get("devices")},
                          extra={"removed": res.get("removed")})
                # bridge перезапустил воркеры и перепубликовал Discovery —
                # перечитываем мету, чтобы UI показывал новый конфиг
                try:
                    load_device_meta()
                    load_cloud_cache()
                except Exception as e:
                    log.warning(f"[Restore] reload: {e}")
            self._send_json(200, res)
            return

        if path == "/api/config/audit/cleanup":
            if not body:
                self._send_json(400, {"ok": False, "error": "body required"})
                return
            try:
                if body.get("purge_all"):
                    n = audit_cleanup(purge_all=True)
                else:
                    keep_s = (int(body.get("keep_days", 0)) * 86400
                              + int(body.get("keep_hours", 0)) * 3600)
                    if keep_s <= 0:
                        self._send_json(400, {"ok": False, "error": "keep_days or keep_hours required"})
                        return
                    n = audit_cleanup(keep_seconds=keep_s)
                self._send_json(200, {"ok": True, "deleted": n})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/db/cleanup":
            if not body:
                self._send_json(400, {"ok": False, "error": "body required"})
                return
            try:
                days = int(body.get("keep_days", 0))
                hours = int(body.get("keep_hours", 0))
            except (TypeError, ValueError):
                # v1.31.19: битые keep_days/keep_hours → 400, а не обрыв соединения.
                self._send_json(400, {"ok": False, "error": "keep_days/keep_hours must be integers"})
                return
            keep_s = days * 86400 + hours * 3600
            scope = (body.get("scope") or "all").strip()
            purge_all = bool(body.get("purge_all"))
            if scope == "all" and purge_all:
                try:
                    n = db_cleanup_by_period(0)   # cutoff = now → удалить всё
                    # v1.32.1: чистим и буферы в памяти — иначе накопленные события
                    # вернутся в базу следующим db_flush() и «удалить всё» не удалит всё.
                    with _db_lock:
                        _status_buffer.clear()
                        _latency_buffer.clear()
                    threading.Thread(target=db_vacuum, daemon=True).start()
                    self._send_json(200, {"ok": True, "deleted": n, "scope": "purge_all"})
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                return
            if scope == "timeline":
                try:
                    n = db_cleanup_timeline_only()
                    threading.Thread(target=db_vacuum, daemon=True).start()
                    self._send_json(200, {"ok": True, "deleted": n, "scope": scope})
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                return
            if scope == "timeline_age":
                if keep_s <= 0:
                    self._send_json(400, {"ok": False, "error": "keep_days or keep_hours required"})
                    return
                try:
                    cutoff = int(time.time()) - keep_s
                    n = db_cleanup_timeline_before(cutoff)
                    threading.Thread(target=db_vacuum, daemon=True).start()
                    self._send_json(200, {"ok": True, "deleted": n, "scope": scope})
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                return
            if scope == "timeline_before":
                before_ts = int(body.get("before_ts", 0))
                if before_ts <= 0:
                    self._send_json(400, {"ok": False, "error": "before_ts required"})
                    return
                try:
                    n = db_cleanup_timeline_before(before_ts)
                    threading.Thread(target=db_vacuum, daemon=True).start()
                    self._send_json(200, {"ok": True, "deleted": n, "scope": scope})
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                return
            if keep_s <= 0:
                self._send_json(400, {"ok": False, "error": "keep_days or keep_hours required"})
                return
            try:
                n = db_cleanup_by_period(keep_s)
                threading.Thread(target=db_vacuum, daemon=True).start()
                self._send_json(200, {"ok": True, "deleted": n, "keep_seconds": keep_s, "scope": "all"})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        dev = self._parse_dev_path(path, "/quiet")
        if dev is not None:
            if body is None or "windows" not in body:
                self._send_json(400, {"ok": False, "error": "windows required"})
                return
            windows_in = body.get("windows") or []
            if not isinstance(windows_in, list):
                self._send_json(400, {"ok": False, "error": "windows must be list"})
                return
            parsed = []
            for w in windows_in:
                if not isinstance(w, dict):
                    continue
                f_, t_ = w.get("from"), w.get("to")
                if _quiet_parse_hm(f_) is None or _quiet_parse_hm(t_) is None:
                    self._send_json(400, {"ok": False, "error": f"invalid time: {f_}-{t_}"})
                    return
                if f_ == t_:
                    self._send_json(400, {"ok": False, "error": f"from == to: {f_}"})
                    return
                parsed.append({"from": f_, "to": t_})
            with QUIET_EDIT_LOCK:
                with QUIET_LOCK:
                    new_cfg = dict(QUIET_CONFIG)
                if parsed:
                    new_cfg[dev] = {"windows": parsed}
                else:
                    new_cfg.pop(dev, None)
                ok, err = quiet_save(new_cfg)
            if ok:
                self._send_json(200, {"ok": True, "windows": parsed})
            else:
                self._send_json(500, {"ok": False, "error": err})
            return

        dev = self._parse_dev_path(path, "/config")
        if dev is not None:
            if not body or "changes" not in body:
                self._send_json(400, {"ok": False, "error": "changes required"})
                return
            # v1.28.0: _meta_before берём ДО отправки в bridge и ДО
            # update/load_device_meta — иначе old_v показывает новое
            # значение («enabled: false → false», пустые изменения).
            _meta_before = {}
            with DEVICE_META_LOCK:
                _meta_before = dict(DEVICE_META.get(dev, {}))
            changes_for_audit = {}
            for k, v in (body.get("changes") or {}).items():
                if k == "local_key":
                    changes_for_audit[k] = {"old": "***", "new": "***"}
                elif k == "dps_map":
                    _old_map = _meta_before.get("dps_map", {}) or {}
                    _new_map = v if isinstance(v, dict) else {}
                    _added = sorted(set(_new_map) - set(_old_map), key=lambda x: int(x) if str(x).isdigit() else 0)
                    _removed = sorted(set(_old_map) - set(_new_map), key=lambda x: int(x) if str(x).isdigit() else 0)
                    _changed = sorted(
                        (dp for dp in (set(_old_map) & set(_new_map)) if _old_map[dp] != _new_map[dp]),
                        key=lambda x: int(x) if str(x).isdigit() else 0,
                    )
                    changes_for_audit[k] = {
                        "old_count": len(_old_map),
                        "new_count": len(_new_map),
                        "added": _added,
                        "removed": _removed,
                        "changed": _changed,
                    }
                else:
                    changes_for_audit[k] = {
                        "old": _meta_before.get(k),
                        "new": v,
                    }

            result = _send_request(f"{TOPIC_PREFIX}/bridge/edit_config",
                                   {"device": dev, "changes": body["changes"], "validate": False},
                                   timeout=EDIT_TIMEOUT_WAIT)
            if result.get("ok"):
                # v1.27.0: если менялся dps_map — полная перезагрузка
                # (нужно для _enrich_dps_names и phase_a-разбивки).
                if "dps_map" in (body.get("changes") or {}):
                    try:
                        load_device_meta()
                        _load_cloud_mappings()
                    except Exception as e:
                        log.warning(f"[EditConfig] reload meta: {e}")
                else:
                    with DEVICE_META_LOCK:
                        if dev in DEVICE_META:
                            DEVICE_META[dev].update(body["changes"])
                try:
                    audit_log("edit", device=dev, changes=changes_for_audit, ok=True)
                except Exception:
                    pass
                self._send_json(200, {"ok": True})
            else:
                raw_err = result.get("error", "unknown")
                human_err = _humanize_bridge_error(raw_err)
                self._send_json(400, {"ok": False, "error": human_err, "error_raw": raw_err})
            return

        dev = self._parse_dev_path(path, "/delete")
        if dev is not None:
            meta = get_device_meta(dev)
            if not meta:
                # v1.28.23: даже если meta нет — чистим STATE["devices"].
                # Иначе ghost-запись от повторного удаления (или удаления
                # уже удалённого) висит в /api/status как type=unknown.
                with STATE_LOCK:
                    STATE["devices"].pop(dev, None)
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            result = _send_request(f"{TOPIC_PREFIX}/bridge/delete_device", {"device": dev}, timeout=DELETE_TIMEOUT_WAIT)
            if result.get("ok"):
                with DEVICE_META_LOCK:
                    DEVICE_META.pop(dev, None)
                with STATE_LOCK:
                    STATE["devices"].pop(dev, None)
                # v1.25.0 (release): обновляем Cloud-индексы после удаления.
                _load_cloud_mappings()
                try:
                    audit_log("delete", device=dev, ok=True)
                except Exception:
                    pass
                self._send_json(200, {"ok": True})
            else:
                self._send_json(400, {"ok": False, "error": result.get("error", "unknown")})
            return

        if path == "/api/scan/extended":
            subnet = (body or {}).get("subnet", "").strip()
            if not subnet:
                meta_snap = snapshot_device_meta()
                for m in meta_snap.values():
                    ip = m.get("ip", "")
                    parts = ip.split(".")
                    if len(parts) == 4:
                        subnet = ".".join(parts[:3])
                        break
            # v1.28.20: строгая валидация — три октета 0..255 без
            # ведущих нулей (синхронно с bridge 1.10.5).
            if not subnet or not re.match(
                    r"^(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})$",
                    subnet):
                self._send_json(400, {"ok": False, "error": f"invalid subnet: {subnet}"})
                return
            # v1.32.2: расширенный скан идёт 20–40 с и плодит пул потоков — не даём
            # запускать его параллельно (раньше каждый POST создавал свой пул).
            with SCAN_EXTENDED_LOCK:
                if SCAN_EXTENDED_STATE["running"]:
                    self._send_json(200, {"ok": False, "error": "скан уже выполняется"})
                    return
                SCAN_EXTENDED_STATE["running"] = True
            try:
                hosts = _scan_extended(subnet)
                self._send_json(200, {"ok": True, "hosts": hosts, "subnet": subnet})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            finally:
                with SCAN_EXTENDED_LOCK:
                    SCAN_EXTENDED_STATE["running"] = False
            return

        if path == "/api/scan/bridge":
            subnet = (body or {}).get("subnet", "").strip()
            # v1.28.20: строгая валидация — три октета 0..255 без
            # ведущих нулей (синхронно с bridge 1.10.5).
            if not subnet or not re.match(
                    r"^(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})$",
                    subnet):
                self._send_json(400, {"ok": False, "error": f"invalid subnet: {subnet}"})
                return
            result = _send_request(f"{TOPIC_PREFIX}/bridge/scan_network",
                                   {"subnet": subnet},
                                   timeout=SCAN_TIMEOUT_WAIT + 5)
            if not result.get("ok"):
                self._send_json(400, {"ok": False, "error": result.get("error", "unknown")})
                return
            hosts = result.get("hosts", [])
            self._send_json(200, {"ok": True, "hosts": hosts, "subnet": result.get("subnet", subnet)})
            return

        if path == "/api/cloud/fetch":
            if not body:
                self._send_json(400, {"ok": False, "error": "empty body"})
                return
            aid = body.get("access_id", "").strip()
            asec = body.get("access_secret", "").strip()
            region = body.get("region", "eu").strip()
            if not aid or not asec:
                self._send_json(400, {"ok": False, "error": "creds required"})
                return
            # v1.22.6: оборачиваем в поток с таймаутом 60 сек —
            # иначе при недоступном Tuya Cloud воркер висит вечно.
            result = _cloud_fetch_with_timeout(aid, asec, region, fetch_mappings=True)
            if not result["ok"]:
                self._send_json(400, {"ok": False, "error": result.get("error")})
                return
            enriched = []
            for d in result["devices"]:
                mapping = d.get("mapping", {}) or {}
                dps_map = mapping_to_dps_map(
                    mapping,
                    d.get("category", ""),
                    _writable_codes_from_props(d.get("_raw_properties")),
                )
                product_id = d.get("product_id", "")
                if product_id:
                    tl_map = lookup_tuya_local(product_id)
                    if tl_map:
                        dps_map = merge_dps_maps(dps_map, tl_map)
                        log.info(f"[Cloud] {d.get('name','?')}: tuya-local обогатил {len(tl_map)} DP")
                d["dps_map_generated"] = dps_map
                d["type_guess"] = guess_type_from_category(d.get("category", ""), d.get("product_name", ""))
                # v1.29.2: version_guess убран — облако версию протокола не отдаёт,
                # а подставлять константу «3.3» как факт было враньём.
                # Версия определяется пробой (🔍) или задаётся вручную.
                enriched.append(d)
            save_cloud_cache(enriched, access_id=aid, region=region)
            # v1.25.0 (fix #cloud_dps): Cloud-кэш обновился —
            # перечитываем mapping-индексы для обогащения dps_map.
            _load_cloud_mappings()
            self._send_json(200, {"ok": True, "devices": enriched})
            return

        if path == "/api/import_devices":
            if not body or "devices" not in body:
                self._send_json(400, {"ok": False, "error": "devices required"})
                return
            devices = body["devices"]
            if not isinstance(devices, list) or not devices:
                self._send_json(400, {"ok": False, "error": "list empty"})
                return
            result = _send_request(f"{TOPIC_PREFIX}/bridge/import_devices",
                                   {"devices": devices, "overwrite": bool(body.get("overwrite", False))},
                                   timeout=IMPORT_TIMEOUT_WAIT + 5)
            if result.get("ok"):
                load_device_meta()
                # v1.25.0 (release): обновляем Cloud-индексы —
                # иначе обогащение dps_map работает на старых данных
                # до рестарта или следующего /api/cloud/fetch.
                _load_cloud_mappings()
                try:
                    names_imported = [d.get("name") for d in devices if isinstance(d, dict) and d.get("name")]
                    audit_log("import", ok=True, extra={
                        "added": result.get("added", 0),
                        "updated": result.get("updated", 0),
                        "skipped": result.get("skipped", 0),
                        "devices": names_imported[:50],
                    })
                except Exception:
                    pass
                self._send_json(200, {"ok": True, "added": result.get("added", 0),
                                      "updated": result.get("updated", 0),
                                      "skipped": result.get("skipped", 0),
                                      "errors": result.get("errors", [])})
            else:
                self._send_json(400, {"ok": False, "error": result.get("error", "unknown"),
                                      "errors": result.get("errors", [])})
            return


        # ===== v1.27.0: DPS MAP APPLY =====
        dev = self._parse_dev_path(path, "/dps_map/apply")
        if dev is not None:
            if not body or "dps_map" not in body:
                self._send_json(400, {"ok": False, "error": "dps_map required"})
                return
            new_map = body.get("dps_map")
            if not isinstance(new_map, dict):
                self._send_json(400, {"ok": False, "error": "dps_map must be dict"})
                return
            # v1.28.0: старую карту берём ДО обновления meta.
            with DEVICE_META_LOCK:
                _old_map = dict(DEVICE_META.get(dev, {}).get("dps_map", {}) or {})
            _added = sorted(set(new_map) - set(_old_map), key=lambda x: int(x) if str(x).isdigit() else 0)
            _removed = sorted(set(_old_map) - set(new_map), key=lambda x: int(x) if str(x).isdigit() else 0)
            _changed = sorted(
                (dp for dp in (set(_old_map) & set(new_map)) if _old_map[dp] != new_map[dp]),
                key=lambda x: int(x) if str(x).isdigit() else 0,
            )
            # Шлём в bridge через edit_config
            result = _send_request(
                f"{TOPIC_PREFIX}/bridge/edit_config",
                {"device": dev, "changes": {"dps_map": new_map}, "validate": False},
                timeout=EDIT_TIMEOUT_WAIT,
            )
            if result.get("ok"):
                try:
                    load_device_meta()
                    _load_cloud_mappings()
                except Exception as e:
                    log.warning(f"[DpsApply] reload meta: {e}")
                try:
                    audit_log("edit", device=dev, ok=True, changes={
                        "dps_map": {
                            "old_count": len(_old_map),
                            "new_count": len(new_map),
                            "added": _added,
                            "removed": _removed,
                            "changed": _changed,
                        }
                    })
                except Exception:
                    pass
                self._send_json(200, {
                    "ok": True,
                    "warnings": result.get("warnings", []),
                    "dps_map": new_map,
                })
            else:
                self._send_json(400, {
                    "ok": False,
                    "error": result.get("error", "unknown"),
                })
            return

        self.send_error(404)

    def _handle_sse(self, qs):
        try: since = int(qs.get("since", ["0"])[0])
        except: since = 0
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n"); self.wfile.flush()
        except Exception: return
        self.close_connection = True
        q = _subscribe_sse()
        try:
            if since == 0:
                with _log_buffer_lock: backlog = list(_log_buffer)[-SSE_BACKLOG:]
                for item in backlog:
                    if item["seq"] > since: self._sse_write(item)
            last_ping = time.time(); last_act = time.time()
            while not STOP_EVENT.is_set():
                if time.time() - last_ping > 10:
                    try:
                        self.wfile.write(b": ping\n\n"); self.wfile.flush()
                        last_ping = time.time()
                    except (BrokenPipeError, ConnectionResetError, OSError): break
                try: item = q.get(timeout=15)
                except _queue.Empty:
                    if time.time() - last_act > SSE_IDLE_TIMEOUT: break
                    continue
                if item is None: break
                try:
                    self._sse_write(item)
                    last_ping = time.time(); last_act = time.time()
                    _sse_touch(q)
                except (BrokenPipeError, ConnectionResetError, OSError): break
        except (BrokenPipeError, ConnectionResetError, OSError): pass
        finally:
            _unsubscribe_sse(q)
            try: self.wfile.flush()
            except: pass

    def _sse_write(self, item):
        self.wfile.write(f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()


class DaemonThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # v1.28.63: молча игнорируем обрыв соединения клиентом
        # (BrokenPipe/ConnectionReset) — иначе трейсбек спамит лог.
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


# ==================== MAIN ====================
def main():
    log.info("=" * 50)
    log.info(f"Tuya Bridge WebUI v{WEBUI_VERSION}")
    log.info(f"ANALYTICS_ENABLED = {ANALYTICS_ENABLED}")
    log.info(f"STATUS_HISTORY_ENABLED = {STATUS_HISTORY_ENABLED}")
    log.info(f"HAS_YAML = {HAS_YAML} (tuya-local парсинг)")
    log.info("=" * 50)
    if not HAS_TINYTUYA: log.warning("[Cloud] tinytuya не установлен")
    if not HAS_YAML: log.warning("[tuya-local] pyyaml не установлен — YAML-обогащение отключено")
    db_init()
    load_device_meta()
    _load_cloud_mappings()
    quiet_load()
    _check_tz_for_quiet()
    audit_init()
    _read_initial_log()
    threading.Thread(target=_log_tailer, args=(LOG_FILE, "bridge"),
                     daemon=True, name="log-tailer-bridge").start()
    threading.Thread(target=_log_tailer, args=(LOG_FILE_WEBUI, "webui"),
                     daemon=True, name="log-tailer-webui").start()
    if STATUS_HISTORY_ENABLED or ANALYTICS_ENABLED:
        threading.Thread(target=db_worker, daemon=True, name="db-worker").start()
    threading.Thread(target=latency_worker, daemon=True, name="latency").start()
    try:
        _mqtt.connect(MQTT_BROKER, MQTT_PORT, 60)
        _mqtt.loop_start()
        log.info(f"[MQTT] {MQTT_BROKER}:{MQTT_PORT}")
        # v1.32.6: сразу отдаём мосту тихие часы (retain) — чтобы он не шумел
        # про ожидаемую недоступность устройств в окне тишины.
        quiet_publish_to_bridge()
    except Exception as e:
        log.warning(f"[MQTT] connect: {e}")
    try:
        httpd = DaemonThreadingHTTPServer((WEBUI_HOST, WEBUI_PORT), WebUIHandler)
    except OSError as e:
        log.error(f"[WebUI] {WEBUI_HOST}:{WEBUI_PORT}: {e}"); raise
    log.info(f"[WebUI] http://{WEBUI_HOST}:{WEBUI_PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Остановка...")
    finally:
        STOP_EVENT.set()
        httpd.server_close()
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановка...")

