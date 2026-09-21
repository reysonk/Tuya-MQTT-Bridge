#!/usr/bin/env python3
"""
Tuya WiFi -> MQTT Bridge with Home Assistant Discovery
"""

import json
import time
import signal
import logging
import logging.handlers
import threading
import colorsys
import base64
import os
import sys
import re
import shutil
import socket
import struct
import select
import tempfile
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import tinytuya
import paho.mqtt.client as mqtt

# ==================== SETTINGS ====================
# v1.10.18: значения можно переопределить переменными окружения
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
DISCOVERY_PREFIX = os.getenv("DISCOVERY_PREFIX") or "homeassistant"
TOPIC_PREFIX = os.getenv("TOPIC_PREFIX") or "tuya"

# POLL_INTERVAL — как часто воркер делает status() под lock'ом.
POLL_INTERVAL = 15
# v1.9.6: BATTERY_REFRESH_INTERVAL удалена — использовалась в
# run_battery_device, которая удалена в 1.9.2.

# Таймауты сокета.
#   CMD     — команда / status() ждёт ответа устройства.
#             Tuya отвечает за 50-200мс, 300мс с запасом.
#   WORKER  — receive() в воркере. Короткий, чтобы команды не ждали долго.
SOCKET_TIMEOUT_CMD = 0.3
SOCKET_TIMEOUT_WORKER = 0.1

# Пауза между receive() — снижает нагрузку на CPU/сеть.
# receive() сам ждёт SOCKET_TIMEOUT_WORKER (100мс), пауза 400мс = цикл ~500мс.
WORKER_IDLE_SLEEP = 0.4
# Сколько ждать per-device lock перед тем, как пропустить итерацию
LOCK_ACQUIRE_TIMEOUT = 0.05

# v1.10.16: пауза перед реконнектом после restart-флага — чтобы Tuya
# освободила сессию и не вернула разовый 914 на новое TCP.
RECONNECT_SETTLE_SEC = 0.5

DISCOVERY_CLEANUP_WAIT = 3
AVAILABILITY_EXPIRE = 120
OFFLINE_TIMEOUT = 120

# v1.9.0: батарейные устройства (device22, low-power)
# Они спят, просыпаются по событию (дверь) или heartbeat.
# Стратегия: ICMP-пинг → DOWN→UP = событие, в окне UP делаем
# updatedps N раз — забираем данные.
BATTERY_PING_INTERVAL = 0.5            # как часто пингуем (сек)
BATTERY_PING_TIMEOUT = 1.0             # таймаут ICMP

BATTERY_UPDATEDPS_COUNT = 5            # сколько раз за окно UP
BATTERY_UPDATEDPS_INTERVAL = 1         # пауза между вызовами (сек)
BATTERY_UPDATEDPS_TIMEOUT = 3          # socket timeout для updatedps

# v1.9.6: BATTERY_OFFLINE_AFTER_DOWN удалена — после 1.9.4
# offline для батарейных НЕ публикуется, HA полагается на expire_after.

BATTERY_EXPIRE_AFTER = 3600            # HA Discovery expire для батарейных (1 час)
BATTERY_ALERT_AFTER_SEC = 86400         # 24 часа без UP → battery_alert=no_data

# ======================================================================
# ⚠️⚠️⚠️ ОПАСНО: при CLEANUP_DISCOVERY = 1 bridge при старте
# УДАЛЯЕТ все retained Discovery-конфиги для своих устройств из MQTT.
# HA на короткое время удаляет сущности из реестра. Затем bridge
# публикует конфиги заново. Если unique_id НЕ изменился — сущности
# восстановятся с теми же entity_id. Если изменился (например,
# поменяли dps_map или name устройства) — HA создаст НОВЫЕ сущности,
# автоматизации/дашборды на старые сломаются.
#
# Использовать ТОЛЬКО при миграции: смена name устройства, чистка
# устаревших retained Discovery от старых версий bridge.
#
# По умолчанию: 0 (выключено).
# ======================================================================
CLEANUP_DISCOVERY = 0

# v1.10.9: автоочистка orphan retained в homeassistant/# при старте.
# Причина: bridge при delete_device чистит retained только если dev
# найден в DEVICE_INDEX. Если индекс не содержал (гонка, импорт в
# обход) — retained висит навсегда, HA показывает призрачные сущности.
# 0 = выключено, 1 = включено.
# Защита от удаления чужих retained: _filter_orphan_by_suffix() удаляет
# только топики, чей unique_id оканчивается на известный суффикс bridge.
# Каждое удаление логируется. См. sync_discovery_registry().
CLEANUP_ORPHAN_RETAINED = 1

# Известные суффиксы unique_id, которые создаёт bridge.
# Используются _filter_orphan_by_suffix() для распознавания «своих»
# retained. Список должен покрывать все ветки _discovery_topics_for_device.
_ORPHAN_SUFFIXES = (
    # battery / датчики
    "_battery_alert", "_battery_last_seen", "_battery_percentage", "_battery",
    "_moisture", "_door", "_motion", "_fault",
    # типы устройств
    "_light", "_climate",
    # switch / select
    "_switch_1", "_switch_2", "_switch_3", "_switch_4", "_switch",
    "_backlight", "_prepayment",
    "_relay_status", "_switch_type",
    # climate
    "_temp_current", "_temp_set",
    # энергометрия
    "_current", "_power", "_voltage",
    "_energy_total", "_total_forward_energy", "_balance_energy",
    "_leakage_current", "_power_factor", "_supply_frequency",
    "_output_current", "_output_power", "_output_voltage",
    # датчики
    "_humidity", "_temperature",
)

DISCOVERY_VERSION = "1.10.20"
RETAINED_DUP_WINDOW = 10

# Пул команд. 32 — хватает на 40+ устройств.
# Команды на ОДНО устройство сериализуются per-device lock'ом.
CMD_POOL_SIZE = 32

LOG_LEVEL = os.getenv("LOG_LEVEL") or "INFO"

MAX_CONSECUTIVE_904 = 3

# Rate limit: только для стримовых DP.
MIN_CMD_INTERVAL_STREAM = 0.15
MIN_CMD_INTERVAL_SWITCH = 0.0

# Fix 1.8.2: окно сброса счётчиков 914/905.
# Если между событиями прошло > REPEAT_RESET_SECONDS — счётчик сбрасывается.
REPEAT_RESET_SECONDS = 120

STATE_CACHE_FILE = "state/state_cache.json"
BATTERY_LAST_UP_FILE = "state/battery_last_up.json"   # v1.10.0: persist _last_up_ts
DISCOVERY_REGISTRY_FILE = "state/discovery_registry.json"
# v1.10.14: persistent-история имён устройств — защита от удаления
# чужих retained в orphan cleanup. См. _filter_orphan_by_suffix().
KNOWN_DEVICE_NAMES_FILE = "state/known_device_names.json"
STATE_SAVE_INTERVAL = 5
STATE_CACHE_SAVE_ON_START = True

LOG_FILE = "logs/bridge.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 2

DEBOUNCE_BY_TYPE = {
    "bright_value": 150,
    "temp_value":   150,
    "colour_data":  150,
    "temp_set":     300,
    "number":       500,
}

CACHE_SNAPSHOT_ENABLED = True

ALLOW_CONFIG_EDIT = True
VALIDATE_ON_EDIT = True
VALIDATE_TIMEOUT = 3

BACKUP_DIR = "backup"

# v1.8.6: что делать при enabled:false.
#   False (рекомендуется) — мягкое отключение: bridge снимает
#     устройство с опроса, публикует availability=offline, но
#     Discovery-конфиг НЕ удаляет. В HA сущность становится
#     unavailable, но остаётся в registry с тем же unique_id —
#     автоматизации/дашборды не ломаются, при включении обратно
#     восстанавливается та же сущность.
#   True  — жёсткое удаление: Discovery-конфиг и state стираются,
#     HA удаляет сущности из registry. Автоматизации, ссылающиеся
#     на entity_id, могут сломаться, если при повторном включении
#     unique_id изменится (например, изменился dps_map).
#     ОПАСНО включать на продакшене без бэкапа автоматизаций HA.
ENABLED_FALSE_REMOVES_DISCOVERY = False
BACKUP_KEEP = 5

SCAN_WORKERS = 32
SCAN_TIMEOUT = 0.3
SCAN_PORT = 6668

CONFIG_FILE = "config/devices_config.json"

DEBUG_CACHE_RECEIVE = 0
DEBUG_CACHE_STATUS = 0
DEBUG_RAW_DP = 0
DEBUG_MQTT_CMD = 0

DEFAULT_BRIGHT_MIN = 10
DEFAULT_BRIGHT_MAX = 1000
DEFAULT_KELVIN_MIN = 2000
DEFAULT_KELVIN_MAX = 6535
DEFAULT_TEMP_STEP = 1
DEFAULT_MIN_TEMP = 5
DEFAULT_MAX_TEMP = 35

HA_BRIGHT_MIN = 1
HA_BRIGHT_MAX = 100

# v1.10.20: добавлены cover (шторы/рольставни/ворота) и fan (вентиляторы).
ALLOWED_TYPES = ("light", "switch", "climate", "sensor", "binary_sensor",
                 "cover", "fan")
ALLOWED_VERSIONS = ("3.1", "3.2", "3.3", "3.4", "3.5")


# ==================== DPS MAP EDIT (v1.8.5) ====================
# Белые списки для валидации dps_map при edit_config.
# Жёсткая валидация: bridge отклоняет запись, если что-то не так.

COMPONENTS_ALLOWED = {
    "switch", "sensor", "binary_sensor", "select", "number",
    "preset", "light", "climate", "button", "time", "lock", "phase_a",
    # v1.10.20
    "cover", "fan",
}

DEVICE_CLASSES_ALLOWED = {
    # sensor
    "temperature", "humidity", "battery", "energy", "power", "current",
    "voltage", "frequency", "power_factor", "illuminance", "pressure",
    "signal_strength", "timestamp", "duration",
    # binary_sensor
    "problem", "door", "motion", "moisture", "smoke", "gas", "light",
    "opening", "window", "garage_door", "lock", "presence", "running",
    "plug", "sound", "vibration", "update", "connectivity", "tamper",
    "heat", "cold", "moving", "occupancy", "safety",
}

STATE_CLASSES_ALLOWED = {
    "measurement", "total", "total_increasing",
}

# Для type=light — только эти имена DP (bridge знает только их).
# Из publish_light: switch_led, bright_value, temp_value, colour_data.
# colour_data_v2 и work_mode — задел, не используются в publish_light,
# но допустимы в конфиге.
LIGHT_DP_NAMES = {
    "switch_led", "bright_value", "temp_value",
    "colour_data", "colour_data_v2", "work_mode",
}

# Обязательные DP для type=climate (жёстко не требуем, но предупреждаем).
CLIMATE_RECOMMENDED = {"switch", "temp_set", "temp_current", "preset_mode"}

# v1.10.20: допустимые DP для type=cover (шторы / рольставни / ворота).
#   control         — команда: open / stop / close
#   percent_control — целевое положение 0..100 (команда)
#   percent_state   — текущее положение 0..100 (состояние)
COVER_DP_NAMES = {"control", "percent_control", "percent_state"}

# v1.10.20: device_class для cover (необязательное поле DP).
COVER_DEVICE_CLASSES_ALLOWED = {
    "curtain", "blind", "shade", "shutter", "garage", "gate",
    "door", "awning", "damper", "window",
}

# v1.10.20: допустимые DP для type=fan (вентиляторы).
#   switch        — вкл/выкл
#   fan_speed     — скорость: enum (options) → пресеты, число → проценты
#   fan_direction — направление: forward / reverse
FAN_DP_NAMES = {"switch", "fan_speed", "fan_direction"}

# v1.9.11: имена DP, зарезервированные bridge. Если пользователь назовёт
# DP одним из этих name — bridge отклонит dps_map. Защита от коллизий
# с системными сущностями Discovery.
#   - "battery_alert"      — sensor.<dev>_battery_alert      (publish_battery_alert_discovery)
#   - "battery_last_seen"  — sensor.<dev>_battery_last_seen  (publish_battery_alert_discovery)
#   - "output_voltage"     — sensor.<dev>_output_voltage     (publish_phase_a)
#   - "output_current"     — sensor.<dev>_output_current     (publish_phase_a)
#   - "output_power"       — sensor.<dev>_output_power       (publish_phase_a)
# v1.10.11: расширено — ранее был только "battery_alert",
# из-за чего DP с name="battery_last_seen" или "output_voltage"
# создавали конфликт unique_id с системными сущностями.
RESERVED_DP_NAMES = {
    "battery_alert", "battery_last_seen",
    "output_voltage", "output_current", "output_power",
}

# ==================== LOGGING ====================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tuya-bridge")

try:
    _log_dir = os.path.dirname(LOG_FILE)
    if _log_dir:
        os.makedirs(_log_dir, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8",
    )
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(_file_handler)
except Exception as e:
    log.warning(f"[Log] Не удалось открыть файловый лог {LOG_FILE}: {e}")

# ==================== КОНФИГ ====================
_config_dir = os.path.dirname(CONFIG_FILE)
if _config_dir:
    os.makedirs(_config_dir, exist_ok=True)

if not os.path.exists(CONFIG_FILE):
    log.error(f"[Config] Файл {CONFIG_FILE} не найден")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    ALL_DEVICES = json.load(f)

DEVICES = [d for d in ALL_DEVICES if d.get("enabled", True)]
DEVICE_INDEX = {d["name"]: d for d in DEVICES}

DEVICES_LOCK = threading.RLock()

STATE_CACHE = {d["name"]: {} for d in DEVICES}
BATTERY_LAST_UP = {}   # v1.10.0: {name: unix_ts} — persist battery last UP
STATE_LOCK = threading.Lock()

STOP_EVENT = threading.Event()
START_TIME = time.time()

# Restart flags (edit_config)
RESTART_FLAGS = {}
RESTART_FLAGS_LOCK = threading.Lock()

# Fix 1.8.1: после команды — форсируем status() в воркере.
STATUS_REQUESTS = {}
STATUS_REQUESTS_LOCK = threading.Lock()


def request_worker_restart(name):
    with RESTART_FLAGS_LOCK:
        RESTART_FLAGS[name] = True


def consume_restart_flag(name):
    with RESTART_FLAGS_LOCK:
        return RESTART_FLAGS.pop(name, False)


def request_status(name):
    with STATUS_REQUESTS_LOCK:
        STATUS_REQUESTS[name] = True


def consume_status_request(name):
    with STATUS_REQUESTS_LOCK:
        return STATUS_REQUESTS.pop(name, False)


log.info(f"[Config] Загружено устройств: {len(ALL_DEVICES)}, включено: {len(DEVICES)}")
log.info(f"[Config] CONFIG_FILE = {os.path.abspath(CONFIG_FILE)}")


# ==================== DISCOVERY REGISTRY (v1.9.12) ====================
# Реестр всех Discovery-топиков, которые bridge опубликовал.
# Хранится в state/discovery_registry.json.
# При старте — сравнивается с актуальным набором; разница удаляется
# через retained-publish с payload=None.
#
# Решает проблему: после апгрейда bridge (смена unique_id) старые
# retained-конфиги остаются в MQTT, HA создаёт дубликаты сущностей.
# Теперь bridge сам их удаляет.


def _discovery_topics_for_device(dev):
    """Полный список Discovery-топиков, которые публикует publish_discovery(dev).

    Должен совпадать с тем, что реально публикуется в publish_discovery
    и его подфункциях (publish_light, publish_switches, ...). Если меняешь
    формат unique_id — обнови ЗДЕСЬ ЖЕ.
    """
    topics = set()
    dev_name = dev["name"]
    dtype = dev.get("type", "sensor")

    # battery_alert + battery_last_seen
    if dev.get("battery_powered"):
        topics.add(f"{DISCOVERY_PREFIX}/sensor/{dev_name}_battery_alert/config")
        topics.add(f"{DISCOVERY_PREFIX}/sensor/{dev_name}_battery_last_seen/config")

    # phase_a (если dp 6 = phase_a)
    if dev.get("dps_map", {}).get("6", {}).get("component") == "phase_a":
        for suffix in ("voltage", "current", "power"):
            topics.add(f"{DISCOVERY_PREFIX}/sensor/{dev_name}_output_{suffix}/config")

    # light / climate / cover / fan — свои unique_id
    if dtype == "light":
        topics.add(f"{DISCOVERY_PREFIX}/light/{dev_name}_light/config")
    elif dtype == "climate":
        topics.add(f"{DISCOVERY_PREFIX}/climate/{dev_name}_climate/config")
    # v1.10.20
    elif dtype == "cover":
        topics.add(f"{DISCOVERY_PREFIX}/cover/{dev_name}_cover/config")
    elif dtype == "fan":
        topics.add(f"{DISCOVERY_PREFIX}/fan/{dev_name}_fan/config")

    # DP-уровневые сущности
    for dp_str, info in dev.get("dps_map", {}).items():
        comp = info.get("component")
        ent = info.get("name", f"dp_{dp_str}")
        if comp == "switch":
            topics.add(f"{DISCOVERY_PREFIX}/switch/{dev_name}_{ent}/config")
        elif comp == "select":
            topics.add(f"{DISCOVERY_PREFIX}/select/{dev_name}_{ent}/config")
        elif comp == "number":
            topics.add(f"{DISCOVERY_PREFIX}/number/{dev_name}_{ent}/config")
        elif comp in ("sensor", "binary_sensor"):
            topics.add(f"{DISCOVERY_PREFIX}/{comp}/{dev_name}_{ent}/config")
        elif comp == "lock":           # v1.10.20
            topics.add(f"{DISCOVERY_PREFIX}/lock/{dev_name}_{ent}/config")

    return topics


def collect_actual_discovery_topics():
    """Все топики, которые bridge сейчас публикует.

    v1.10.15: идём по ALL_DEVICES, а не DEVICES. Иначе Discovery мягко
    отключённых (enabled:false) не попадал в actual, и sync_discovery_registry
    сносил их retained — вопреки soft-disable.
    """
    actual = set()
    with DEVICES_LOCK:
        devs = list(ALL_DEVICES)
    for dev in devs:
        actual |= _discovery_topics_for_device(dev)
    return actual


def scan_retained_discovery(wait_sec=3.0, filter_by_devices=True):
    """Отдельным MQTT-клиентом подписаться на homeassistant/#, собрать
    retained-топики, оканчивающиеся на /config.

    v1.10.9: параметр filter_by_devices.
      True  (default) — только топики, чей unique_id == <dev_name> или
                        начинается с <dev_name>_ для любого устройства
                        из ALL_DEVICES.
      False           — ВСЕ retained в homeassistant/# (для orphan cleanup).
                        Фильтр по суффиксам применяется отдельно
                        (_filter_orphan_by_suffix).

    Возвращает set полных топиков.
    """
    found = set()
    found_lock = threading.Lock()

    with DEVICES_LOCK:
        all_names = [d["name"] for d in ALL_DEVICES]
    # Длинные имена — вперёд, чтобы «datchik_dvizheniya_tualet_» матчился
    # раньше, чем потенциальный «datchik_».
    all_names.sort(key=len, reverse=True)

    def _on_scan_message(client, userdata, msg):
        if not msg.retain:
            return
        topic = msg.topic
        if not topic.endswith("/config"):
            return
        parts = topic.split("/")
        # homeassistant/<domain>/<unique_id>/config
        if len(parts) < 4:
            return
        if not filter_by_devices:
            with found_lock:
                found.add(topic)
            return
        unique_id = parts[2]
        for dev_name in all_names:
            if unique_id == dev_name or unique_id.startswith(dev_name + "_"):
                with found_lock:
                    found.add(topic)
                return

    try:
        scan_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"tuya_bridge_scan_{int(time.time())}",
        )
        if MQTT_USERNAME:
            scan_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        scan_client.on_message = _on_scan_message
        scan_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        scan_client.subscribe(f"{DISCOVERY_PREFIX}/#", qos=1)
        scan_client.loop_start()
        time.sleep(wait_sec)
        scan_client.loop_stop()
        try:
            scan_client.disconnect()
        except Exception:
            pass
    except Exception as e:
        log.warning(f"[Discovery] scan failed: {e}")

    with found_lock:
        return set(found)


def _load_known_device_names():
    """v1.10.14: история имён устройств (для orphan-фильтра).
    Возвращает set имён, когда-либо виденных bridge."""
    try:
        if os.path.exists(KNOWN_DEVICE_NAMES_FILE):
            with open(KNOWN_DEVICE_NAMES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                names = data.get("names", [])
                if isinstance(names, list):
                    return {str(n) for n in names if n}
    except Exception as e:
        log.warning(f"[Discovery] known names load: {e}")
    return set()


def _save_known_device_names(names):
    """v1.10.14: атомарно сохранить историю имён устройств."""
    try:
        dir_name = os.path.dirname(os.path.abspath(KNOWN_DEVICE_NAMES_FILE)) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".known_names_", suffix=".tmp", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"names": sorted(names)}, f,
                          ensure_ascii=False, indent=2)
            os.replace(tmp_path, KNOWN_DEVICE_NAMES_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        log.warning(f"[Discovery] known names save: {e}")


def _filter_orphan_by_suffix(topics, all_dev_names):
    """v1.10.14: из набора topics оставить только orphan retained.

    Orphan = топик homeassistant/<comp>/<unique_id>/config, у которого:
      1. unique_id НЕ совпадает с <dev> и НЕ начинается с <dev>_ ни для
         одного <dev> из (current ∪ history имён bridge);
      2. unique_id оканчивается на один из _ORPHAN_SUFFIXES, создаваемых
         bridge, ИЛИ на _<name> любого DP из текущих dps_map.

    v1.10.14: добавлена проверка по ИСТОРИИ имён (KNOWN_DEVICE_NAMES_FILE).
    Раньше чужие retained типа homeassistant/switch/my_kitchen_switch/config
    (не наши, но оканчивающиеся на _switch) удалялись как orphan.
    Теперь удаляем только если unique_id начинается с ИЗВЕСТНОГО
    имени (текущего или исторического).

    Возвращает set отфильтрованных топиков.
    """
    # Дополняем hardcoded суффиксы — динамическими из dps_map всех
    # текущих устройств. Ловит DP, которых нет в _ORPHAN_SUFFIXES.
    with DEVICES_LOCK:
        devs = list(DEVICES)
    dynamic_suffixes = set()
    for dev in devs:
        for dp_str, info in dev.get("dps_map", {}).items():
            ent = info.get("name")
            if ent:
                dynamic_suffixes.add(f"_{ent}")

    all_suffixes = tuple(_ORPHAN_SUFFIXES) + tuple(sorted(dynamic_suffixes))

    # v1.10.14: история имён — current ∪ persisted.
    history = _load_known_device_names()
    known_names = set(all_dev_names) | history
    # Длинные имена — вперёд, чтобы «datchik_dvizheniya_tualet_»
    # матчился раньше, чем потенциальный «datchik_».
    known_names_sorted = sorted(known_names, key=len, reverse=True)

    out = set()
    for topic in topics:
        parts = topic.split("/")
        if len(parts) < 4:
            continue
        unique_id = parts[2]
        # 1. Проверка «не привязан к известному имени bridge».
        bound = False
        for dev_name in known_names_sorted:
            if unique_id == dev_name or unique_id.startswith(dev_name + "_"):
                bound = True
                break
        if bound:
            continue
        # 2. v1.10.14: обязательная привязка к известному имени.
        # Раньше здесь был только suffix-матч — он и был причиной
        # удаления чужих retained.
        prefix_matched = False
        for dev_name in known_names_sorted:
            if unique_id.startswith(dev_name):
                prefix_matched = True
                break
        if not prefix_matched:
            continue
        # 3. Проверка суффикса.
        if any(unique_id.endswith(s) for s in all_suffixes):
            out.add(topic)
    return out


def sync_discovery_registry(scan_on_first=False):
    """Синхронизировать реестр Discovery.

    1. Собрать актуальные топики (collect_actual_discovery_topics).
    2. Прочитать старый registry (state/discovery_registry.json).
       Если его нет и scan_on_first=True — просканировать MQTT.
    3. Разница (old - actual) — удалить retained (payload=None).
    4. Сохранить actual в registry.
    """
    actual = collect_actual_discovery_topics()

    old = None
    if os.path.exists(DISCOVERY_REGISTRY_FILE):
        try:
            with open(DISCOVERY_REGISTRY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            old = set(data.get("topics", []))
            log.info(f"[Discovery] Registry загружен: {len(old)} топиков")
        except Exception as e:
            log.warning(f"[Discovery] registry load: {e}")

    if old is None:
        if scan_on_first:
            log.info("[Discovery] Registry не найден — сканирую MQTT retained...")
            old = scan_retained_discovery(wait_sec=3.0)
            log.info(f"[Discovery] Найдено retained наших топиков: {len(old)}")
        else:
            old = set()

    # v1.10.9: orphan cleanup — retained, которых нет ни в actual,
    # ни в old (реестре). Ловит топики удалённых устройств, которые
    # bridge не почистил (см. шапку модуля).
    orphan = set()
    if scan_on_first and CLEANUP_ORPHAN_RETAINED:
        log.info("[Discovery] Scanning ALL retained for orphan cleanup...")
        try:
            all_retained = scan_retained_discovery(
                wait_sec=3.0, filter_by_devices=False)
            with DEVICES_LOCK:
                all_dev_names = [d["name"] for d in ALL_DEVICES]
            # v1.10.14: обновляем persistent-историю имён ПЕРЕД фильтром —
            # иначе только что добавленное устройство не будет защищено.
            _history = _load_known_device_names()
            _history |= set(all_dev_names)
            _save_known_device_names(_history)
            candidate = all_retained - actual - old
            orphan = _filter_orphan_by_suffix(candidate, all_dev_names)
            if orphan:
                log.info(f"[Discovery] Orphan retained: {len(orphan)} — удаляю")
                for topic in sorted(orphan):
                    try:
                        mqtt_client.publish(topic, payload=None, qos=1, retain=True)
                        log.info(f"[Discovery] orphan removed: {topic}")
                    except Exception as e:
                        log.warning(f"[Discovery] orphan remove {topic}: {e}")
            else:
                log.info("[Discovery] Orphan retained: 0")
        except Exception as e:
            log.warning(f"[Discovery] orphan scan failed: {e}")

    to_remove = (old - actual) | orphan
    if to_remove:
        log.info(f"[Discovery] Устаревших retained: {len(to_remove)} — удаляю")
        for topic in sorted(to_remove):
            try:
                mqtt_client.publish(topic, payload=None, qos=1, retain=True)
                log.info(f"[Discovery] removed: {topic}")
            except Exception as e:
                log.warning(f"[Discovery] remove {topic}: {e}")
    else:
        log.info("[Discovery] Устаревших retained нет")

    added = actual - old
    if added:
        log.info(f"[Discovery] Новых топиков: {len(added)}")

    # Сохранить.
    try:
        dir_name = os.path.dirname(DISCOVERY_REGISTRY_FILE) or "."
        os.makedirs(dir_name, exist_ok=True)
        tmp = DISCOVERY_REGISTRY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "topics": sorted(actual),
                "updated_at": int(time.time()),
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DISCOVERY_REGISTRY_FILE)
        log.info(f"[Discovery] Registry сохранён: {len(actual)} топиков")
    except Exception as e:
        log.warning(f"[Discovery] registry save: {e}")


# ==================== CONNECTIONS ====================
# ОДИН persistent сокет на устройство. Tuya не держит два TCP -> 914.
DEVICE_CONN = {}
DEVICE_CONN_LOCK = threading.Lock()


def drop_device_conn(name):
    with DEVICE_CONN_LOCK:
        entry = DEVICE_CONN.pop(name, None)
    if entry:
        # v1.10.11: entry — (sig, Device) или legacy Device.
        try:
            _dev = entry[1] if isinstance(entry, tuple) else entry
            _dev.close()
        except Exception as e:
            log.debug(f"[Conn] close({name}): {e}")


def get_device_conn(dev):
    name = dev["name"]
    # v1.10.11: храним (signature, Device). При смене ip/key/version
    # пересоздаём — иначе после edit_config воркер стучится на старый IP.
    # v1.10.12: guard для legacy Device (если DEVICE_CONN каким-то
    # образом содержит голый Device, а не tuple) — иначе TypeError
    # на entry[0].
    _sig = (dev["id"], dev["ip"], dev["local_key"], str(dev["version"]))
    with DEVICE_CONN_LOCK:
        entry = DEVICE_CONN.get(name)
        if entry is not None and isinstance(entry, tuple) and entry[0] == _sig:
            return entry[1]
        if entry is not None:
            try:
                _stale = entry[1] if isinstance(entry, tuple) else entry
                _stale.close()
            except Exception as e:
                log.debug(f"[Conn] close stale({name}): {e}")
        d = tinytuya.Device(dev["id"], dev["ip"], dev["local_key"])
        d.set_version(float(dev["version"]))
        d.set_socketPersistent(True)
        d.set_socketTimeout(SOCKET_TIMEOUT_CMD)
        DEVICE_CONN[name] = (_sig, d)
        return d


# ==================== ICMP PING (v1.9.0) ====================
# Быстрый ICMP через raw socket — используется в run_battery_listener.
# Требует CAP_NET_RAW в контейнере.


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


def _has_icmp_capability():
    """v1.9.14: True если можем создать raw ICMP сокет.
    Не отправляет пакет — только создаёт/закрывает сокет.
    Работает в контейнерах, где raw ICMP на loopback недоступен."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                          socket.getprotobyname("icmp"))
        s.close()
        return True
    except (PermissionError, OSError):
        return False

# v1.10.11: кеш результата _has_icmp_capability(). Проверяем один раз
# за процесс — иначе run_battery_listener в цикле (0.5с) создаёт
# raw-сокет впустую.
_ICMP_CAPABILITY_CACHE = [None]


def _icmp_capable():
    """v1.10.11: кешированный _has_icmp_capability()."""
    if _ICMP_CAPABILITY_CACHE[0] is None:
        _ICMP_CAPABILITY_CACHE[0] = _has_icmp_capability()
    return _ICMP_CAPABILITY_CACHE[0]


# v1.10.13: кеш стабильного ключа (mode + battery_count) — без ts.
# v1.10.12 сравнивал payload целиком, но ts меняется каждые 30 сек
# → сравнение всегда True → лог спамил как до фикса.
# v1.10.13: логируем по ключу без ts; payload с ts публикуем всегда.
# Lock — защита от race: _publish_ping_mode вызывается из main(),
# health_worker и обработчиков edit_config/import/delete.
_PING_MODE_LAST = [None]
_PING_MODE_LOCK = threading.Lock()


def _publish_ping_mode():
    """v1.10.11: публикует tuya/bridge/ping_mode (retain).

    Используется WebUI: если mode == "none" и в конфиге есть
    батарейные — показывается warning в блоке «Проблемные».

    v1.10.13: лог только при смене mode/battery_count (без ts).
    Payload с ts публикуется всегда.
    """
    try:
        ok = _icmp_capable()
        with DEVICES_LOCK:
            battery_count = sum(
                1 for d in DEVICES if d.get("battery_powered")
            )
        # Стабильный ключ без ts — по нему решаем, логировать ли.
        log_key = f"{'native' if ok else 'none'}|{battery_count}"
        with _PING_MODE_LOCK:
            should_log = (_PING_MODE_LAST[0] != log_key)
            if should_log:
                _PING_MODE_LAST[0] = log_key
        # Публикуем всегда — с ts.
        payload = json.dumps({
            "mode": "native" if ok else "none",
            "ok": bool(ok),
            "battery_count": battery_count,
            "ts": int(time.time()),
        })
        mqtt_client.publish(
            f"{TOPIC_PREFIX}/bridge/ping_mode",
            payload, qos=1, retain=True,
        )
        if should_log:
            log.info(f"[Ping] mode={'native' if ok else 'none'}, "
                     f"battery_devices={battery_count}")
    except Exception as e:
        log.warning(f"[Ping] publish ping_mode: {e}")


def _ping_native(ip, timeout=1.0):
    """
    v1.9.1: Native ICMP ping через raw socket.

    FIX: было struct.pack("bbHHH", ...) — native byte order, а ICMP
    требует big-endian. Плюс лишний htons() на checksum ломал его.
    Теперь struct.pack("!BBHHH", ...) — big-endian.
    """
    try:
        icmp_proto = socket.getprotobyname("icmp")
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, icmp_proto)
    except (PermissionError, OSError):
        return None
    try:
        s.settimeout(timeout)
        icmp_id = os.getpid() & 0xFFFF
        icmp_seq = 1

        # Header с нулевым checksum (big-endian!)
        header = struct.pack("!BBHHH", 8, 0, 0, icmp_id, icmp_seq)
        payload = struct.pack("!d", time.time())
        packet = header + payload

        # Считаем checksum по big-endian байтам
        cs = _icmp_checksum(packet)

        # Пересобираем пакет с checksum
        header = struct.pack("!BBHHH", 8, 0, cs, icmp_id, icmp_seq)
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
                if r_type == 0 and r_id == icmp_id:
                    return int(round((time.time() - t0) * 1000))
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


# ==================== PER-DEVICE SERIALIZATION ====================
DEVICE_CMD_LOCKS = {}
DEVICE_CMD_LOCKS_LOCK = threading.Lock()

DEVICE_LAST_CMD = {}
DEVICE_LAST_CMD_LOCK = threading.Lock()


def get_device_cmd_lock(name):
    with DEVICE_CMD_LOCKS_LOCK:
        lk = DEVICE_CMD_LOCKS.get(name)
        if lk is None:
            lk = threading.Lock()
            DEVICE_CMD_LOCKS[name] = lk
        return lk


def wait_device_rate_limit(name, component=None):
    if component in ("light", "climate", "number"):
        interval = MIN_CMD_INTERVAL_STREAM
    else:
        interval = MIN_CMD_INTERVAL_SWITCH

    if interval <= 0:
        return

    while True:
        now = time.time()
        with DEVICE_LAST_CMD_LOCK:
            last = DEVICE_LAST_CMD.get(name, 0)
            wait = interval - (now - last)
            if wait <= 0:
                DEVICE_LAST_CMD[name] = now
                return
        time.sleep(min(wait, 0.05))


# ==================== УТИЛИТЫ ====================
def parse_payload(payload):
    payload = payload.strip()
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return payload


def dp_int(dp_str):
    try:
        return int(dp_str)
    except (TypeError, ValueError):
        return None


def find_dp_by_name(dev, name, component=None):
    for dp_str, info in dev.get("dps_map", {}).items():
        if info.get("name") != name:
            continue
        if component and info.get("component") != component:
            continue
        return dp_str, info
    return None, None


def get_bright_bounds(info):
    return info.get("min", DEFAULT_BRIGHT_MIN), info.get("max", DEFAULT_BRIGHT_MAX)


def get_kelvin_bounds(info):
    return info.get("kelvin_min", DEFAULT_KELVIN_MIN), info.get("kelvin_max", DEFAULT_KELVIN_MAX)


def get_select_map(info):
    return info.get("map", {})


def tuya_to_ha_brightness(tuya_val, bmin, bmax):
    try:
        v = float(tuya_val)
    except (TypeError, ValueError):
        v = bmin
    if bmax > bmin:
        ha = int(round((v - bmin) * (HA_BRIGHT_MAX - HA_BRIGHT_MIN) / (bmax - bmin))) + HA_BRIGHT_MIN
    else:
        ha = HA_BRIGHT_MAX
    return max(HA_BRIGHT_MIN, min(HA_BRIGHT_MAX, ha))


def ha_to_tuya_brightness(ha_val, bmin, bmax):
    try:
        h = int(ha_val)
    except (TypeError, ValueError):
        h = HA_BRIGHT_MIN
    h = max(HA_BRIGHT_MIN, min(HA_BRIGHT_MAX, h))
    if bmax > bmin:
        tuya = int(round((h - HA_BRIGHT_MIN) * (bmax - bmin) / (HA_BRIGHT_MAX - HA_BRIGHT_MIN))) + bmin
    else:
        tuya = bmin
    return max(bmin, min(bmax, tuya))


# ==================== ВАЛИДАТОРЫ ====================
def _is_valid_ip(ip_str):
    """v1.10.5: ASCII-only + isdigit.
    int("١٢٣") = 123 (арабские цифры Unicode), int("１２３") = 123
    (fullwidth) — без фильтра _is_valid_ip пропускал такие октеты."""
    if not isinstance(ip_str, str):
        return False
    parts = ip_str.strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isascii() or not p.isdigit():
            return False
        n = int(p)
        if n < 0 or n > 255:
            return False
    return True


def _is_valid_version(v):
    return str(v).strip() in ALLOWED_VERSIONS


def _is_valid_key(key):
    if not isinstance(key, str):
        return False
    k = key.strip()
    return 10 <= len(k) <= 50


def _is_valid_type(t):
    return str(t).strip() in ALLOWED_TYPES


def _is_valid_name(n):
    """v1.10.3: ASCII-only — MQTT-топики не поддерживают кириллицу.
    Раньше c.isalnum() пропускал русские буквы, что ломало
    discovery/state топики."""
    if not isinstance(n, str):
        return False
    n = n.strip()
    if not n or len(n) > 100:
        return False
    for c in n:
        if c in "_-":
            continue
        if not ("0" <= c <= "9" or "a" <= c <= "z" or "A" <= c <= "Z"):
            return False
    return True



# ==================== DPS MAP VALIDATION (v1.8.5) ====================

def _validate_dps_map(dev_type, dps_map):
    """
    Жёсткая структурная валидация dps_map.

    Возвращает: (ok: bool, error: str | None, warnings: list[str])

    Проверки (жёсткие — bridge отклоняет):
      - dps_map — dict
      - ключи — строки из цифр, DP ID в диапазоне 1..255
      - info — dict
      - name — присутствует, _is_valid_name
      - component (для type != light) — обязателен, из COMPONENTS_ALLOWED
      - component (для type == light) — опционален, если есть — из COMPONENTS_ALLOWED
      - name (для type == light) — из LIGHT_DP_NAMES
      - уникальность (component, name)
      - number: min < max, step > 0
      - select: options непустой, без дубликатов; map ⊆ options
      - sensor: scale int >= 0, state_class/device_class из белых списков
      - binary_sensor: device_class из белого списка
      - phase_a: name == "phase_a"
      - preset: name == "preset_mode"
      - cover: name из COVER_DP_NAMES; device_class из COVER_DEVICE_CLASSES_ALLOWED
      - fan: name из FAN_DP_NAMES

    Warnings (мягкие — bridge принимает, но сообщает):
      - climate без temp_set / temp_current / preset_mode
      - cover без control/percent_control, fan без switch
    """
    if not isinstance(dps_map, dict):
        return False, "dps_map must be dict", []
    if not dps_map:
        return False, "dps_map is empty", []

    warnings = []
    seen_keys = set()  # (component_or_auto, name)

    for dp_str, info in dps_map.items():
        # --- DP ID ---
        if not isinstance(dp_str, str):
            return False, f"dp id must be string: {dp_str!r}", warnings
        if not dp_str.isdigit():
            return False, f"dp id must be digits: {dp_str!r}", warnings
        dp_int_val = int(dp_str)
        if not (1 <= dp_int_val <= 255):
            return False, f"dp id out of range (1..255): {dp_str}", warnings

        # --- info ---
        if not isinstance(info, dict):
            return False, f"dp {dp_str}: info must be dict", warnings

        # --- name ---
        name = info.get("name")
        if not name or not isinstance(name, str):
            return False, f"dp {dp_str}: name required", warnings
        if not _is_valid_name(name):
            return False, f"dp {dp_str}: invalid name {name!r} " \
                          f"(alnum + '_-', 1..100)", warnings

        # v1.9.11: name не должен быть из RESERVED_DP_NAMES.
        if name in RESERVED_DP_NAMES:
            return False, (
                f"dp {dp_str}: name {name!r} зарезервировано bridge "
                f"(системная сущность Discovery). Используйте другое."
            ), warnings

        # --- component ---
        comp = info.get("component")
        if dev_type == "light":
            if comp is not None:
                if comp not in COMPONENTS_ALLOWED:
                    return False, f"dp {dp_str}: unknown component {comp!r}", warnings
        else:
            if not comp:
                return False, f"dp {dp_str}: component required for type={dev_type}", warnings
            if comp not in COMPONENTS_ALLOWED:
                return False, f"dp {dp_str}: unknown component {comp!r}", warnings

        # v1.10.20: cover/fan — только для своего типа устройства
        # (иначе DP молча игнорировался бы: публикуют их publish_cover/publish_fan).
        if comp == "cover" and dev_type != "cover":
            return False, f"dp {dp_str}: component 'cover' допустим только при type=cover", warnings
        if comp == "fan" and dev_type != "fan":
            return False, f"dp {dp_str}: component 'fan' допустим только при type=fan", warnings

        # --- light: name из белого списка ---
        if dev_type == "light":
            if name not in LIGHT_DP_NAMES:
                return False, f"dp {dp_str}: name {name!r} not allowed for light " \
                              f"(allowed: {sorted(LIGHT_DP_NAMES)})", warnings

        # --- cover / fan: name из белого списка (v1.10.20) ---
        if dev_type == "cover" and name not in COVER_DP_NAMES:
            return False, f"dp {dp_str}: name {name!r} not allowed for cover " \
                          f"(allowed: {sorted(COVER_DP_NAMES)})", warnings
        if dev_type == "fan" and name not in FAN_DP_NAMES:
            return False, f"dp {dp_str}: name {name!r} not allowed for fan " \
                          f"(allowed: {sorted(FAN_DP_NAMES)})", warnings

        # --- уникальность (component, name) ---
        # Для light component может отсутствовать — используем "auto"
        uniq_key = (comp or "auto", name)
        if uniq_key in seen_keys:
            return False, f"dp {dp_str}: duplicate ({comp or 'auto'}, {name})", warnings
        seen_keys.add(uniq_key)

        # --- number: min/max/step/scale ---
        if comp == "number":
            mn = info.get("min")
            mx = info.get("max")
            if mn is None or mx is None:
                return False, f"dp {dp_str}: number requires min and max", warnings
            if not isinstance(mn, (int, float)) or isinstance(mn, bool):
                return False, f"dp {dp_str}: min must be number", warnings
            if not isinstance(mx, (int, float)) or isinstance(mx, bool):
                return False, f"dp {dp_str}: max must be number", warnings
            if mn >= mx:
                return False, f"dp {dp_str}: min ({mn}) must be < max ({mx})", warnings
            step = info.get("step", 1)
            if not isinstance(step, (int, float)) or isinstance(step, bool):
                return False, f"dp {dp_str}: step must be number", warnings
            if step <= 0:
                return False, f"dp {dp_str}: step must be > 0", warnings
            # v1.10.3: scale для number — int >= 0. handle_number_command
            # делает `10 ** scale` — строка даст TypeError.
            sc_n = info.get("scale", 0)
            if isinstance(sc_n, bool) or not isinstance(sc_n, int):
                return False, f"dp {dp_str}: number scale must be int", warnings
            if sc_n < 0:
                return False, f"dp {dp_str}: number scale must be >= 0", warnings

        # --- select: options / map ---
        if comp == "select":
            opts = info.get("options")
            if not isinstance(opts, list) or not opts:
                return False, f"dp {dp_str}: select requires non-empty options list", warnings
            for o in opts:
                if not isinstance(o, str):
                    return False, f"dp {dp_str}: options must be strings", warnings
            if len(opts) != len(set(opts)):
                return False, f"dp {dp_str}: options contain duplicates", warnings
            m = info.get("map")
            if m is not None:
                if not isinstance(m, dict):
                    return False, f"dp {dp_str}: map must be dict", warnings
                for k in m.keys():
                    if k not in opts:
                        return False, f"dp {dp_str}: map key {k!r} not in options", warnings

        # --- sensor: scale / state_class / device_class ---
        if comp == "sensor":
            sc = info.get("scale")
            if sc is not None:
                if isinstance(sc, bool) or not isinstance(sc, int):
                    return False, f"dp {dp_str}: scale must be int >= 0", warnings
                if sc < 0:
                    return False, f"dp {dp_str}: scale must be >= 0", warnings
            st = info.get("state_class")
            if st is not None and st not in STATE_CLASSES_ALLOWED:
                return False, f"dp {dp_str}: unknown state_class {st!r}", warnings
            dc = info.get("device_class")
            if dc is not None and dc not in DEVICE_CLASSES_ALLOWED:
                return False, f"dp {dp_str}: unknown device_class {dc!r}", warnings

        # --- binary_sensor: device_class ---
        if comp == "binary_sensor":
            dc = info.get("device_class")
            if dc is not None and dc not in DEVICE_CLASSES_ALLOWED:
                return False, f"dp {dp_str}: unknown device_class {dc!r}", warnings

        # --- phase_a: name == phase_a ---
        if comp == "phase_a":
            if name != "phase_a":
                return False, f"dp {dp_str}: phase_a requires name='phase_a'", warnings

        # --- preset: name == preset_mode ---
        if comp == "preset":
            if name != "preset_mode":
                return False, f"dp {dp_str}: preset requires name='preset_mode'", warnings

        # --- cover: необязательный device_class (v1.10.20) ---
        if dev_type == "cover":
            dc_cov = info.get("device_class")
            if dc_cov is not None and dc_cov not in COVER_DEVICE_CLASSES_ALLOWED:
                return False, f"dp {dp_str}: unknown cover device_class {dc_cov!r}", warnings

    # --- climate: мягкие warnings ---
    if dev_type == "climate":
        names = {info.get("name") for info in dps_map.values()}
        missing = CLIMATE_RECOMMENDED - names
        for m_name in sorted(missing):
            warnings.append(
                f"climate без DP '{m_name}' — соответствующая функция HA "
                f"будет недоступна"
            )

    # --- cover / fan: мягкие warnings (v1.10.20) ---
    if dev_type == "cover":
        names = {info.get("name") for info in dps_map.values()}
        if not (names & {"control", "percent_control"}):
            warnings.append(
                "cover без DP 'control'/'percent_control' — управление из HA "
                "будет недоступно (только состояние)"
            )
    if dev_type == "fan":
        names = {info.get("name") for info in dps_map.values()}
        if "switch" not in names:
            warnings.append(
                "fan без DP 'switch' — вкл/выкл из HA будет недоступен"
            )

    return True, None, warnings

# ==================== BACKUP ====================
def _backup_config():
    try:
        if not os.path.exists(CONFIG_FILE):
            return None
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.basename(CONFIG_FILE)
        dst = os.path.join(BACKUP_DIR, f"{base}.bak.{ts}")
        shutil.copy2(CONFIG_FILE, dst)
        log.info(f"[Backup] Создан {dst}")
        _cleanup_backups()
        return dst
    except Exception as e:
        log.warning(f"[Backup] Ошибка: {e}")
        return None


def _cleanup_backups():
    try:
        if not os.path.isdir(BACKUP_DIR):
            return
        base = os.path.basename(CONFIG_FILE)
        files = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.startswith(base + ".bak.")],
            reverse=True,
        )
        for old in files[BACKUP_KEEP:]:
            try:
                os.unlink(os.path.join(BACKUP_DIR, old))
                log.debug(f"[Backup] Удалён старый {old}")
            except Exception as e:
                log.debug(f"[Backup] Не удалось удалить {old}: {e}")
    except Exception as e:
        log.warning(f"[Backup] cleanup error: {e}")


# ==================== ВАЛИДАЦИЯ УСТРОЙСТВА ====================
def _validate_device(dev_id, ip, key, version, timeout=VALIDATE_TIMEOUT):
    d = None
    try:
        d = tinytuya.Device(dev_id, ip, key)
        d.set_version(float(version))
        d.set_socketPersistent(False)
        d.set_socketTimeout(timeout)
        r = d.status()
        if r and isinstance(r, dict) and "dps" in r:
            return True, None
        if r and isinstance(r, dict) and "Error" in r:
            err = r.get("Error", "")
            code = r.get("Err", "")
            return False, f"Tuya error {code}: {err}"
        return False, f"Unexpected response: {str(r)[:100]}"
    except Exception as e:
        return False, f"Exception: {e}"
    finally:
        if d:
            try:
                d.close()
            except Exception:
                pass


# ==================== SCAN NETWORK ====================
def _scan_ip(ip, port=SCAN_PORT, timeout=SCAN_TIMEOUT):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        t0 = time.time()
        s.connect((ip, port))
        ms = int((time.time() - t0) * 1000)
        s.close()
        return ms
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return None


# v1.8.3: UDP-проба Tuya на 6666/6667 для подтверждения, что это Tuya.
# Только для IP, у которых TCP 6668 уже открыт. Один пакет, короткий таймаут.
def _probe_tuya_udp(ip, timeout=1.0):
    payload = bytes.fromhex(
        "000055aa000000000000000a00000000000000000000000000000000"
    )
    for udp_port in (6666, 6667):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(timeout)
        try:
            u.sendto(payload, (ip, udp_port))
            data, _ = u.recvfrom(2048)
            u.close()
            if data and len(data) >= 16 and data[:4] == bytes.fromhex("000055aa"):
                result = {"port_6668": True, "udp_port": udp_port}
                try:
                    idx = data.find(b"{")
                    if idx >= 0:
                        parsed = json.loads(
                            data[idx:].decode("utf-8", errors="ignore").rstrip("\x00")
                        )
                        result["gwId"] = parsed.get("gwId", "")
                        result["productKey"] = parsed.get("productKey", "")
                        result["version"] = parsed.get("version", "")
                except Exception:
                    pass
                return result
        except Exception:
            try:
                u.close()
            except Exception:
                pass
    # TCP 6668 открыт, но UDP не ответил — вероятно, не Tuya
    return {"port_6668": True, "tuya_probable": True}


def _scan_subnet(subnet_prefix):
    # v1.8.3: обогащаем hosts полями known/tuya/tuya_unknown — WebUI
    # больше не выдумывает их на клиенте. Tuya определяется UDP-пробой
    # 6666/6667 (только для IP с открытым TCP 6668).
    results = []
    ips = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
    # v1.10.7: known_ips — из ALL_DEVICES (включая disabled),
    # иначе IP отключённого устройства в скане показывается как
    # «неизвестный» и Bridge пытается делать Tuya-пробу — лишний
    # трафик и ложные «Tuya?» бейджи в UI.
    with DEVICES_LOCK:
        known_ips = {d.get("ip") for d in ALL_DEVICES if d.get("ip")}
    log.info(f"[Scan] Сканирую {subnet_prefix}.0/24 ({len(ips)} IP, {SCAN_WORKERS} потоков)")

    def scan_one(ip):
        # v1.10.15: известные IP НЕ трогаем TCP-connect'ом — bridge держит
        # к ним persistent-сокет, второе TCP = 914 (правило №1).
        # Для known берём ICMP — ping правилом разрешён.
        if ip in known_ips:
            ms = _ping_native(ip, timeout=SCAN_TIMEOUT)
            if ms is None:
                return None
            return {"ip": ip, "ms": ms, "known": True}
        ms = _scan_ip(ip)
        if ms is None:
            return None
        entry = {"ip": ip, "ms": ms, "known": False}
        # UDP-проба только для неизвестных IP с открытым 6668
        try:
            tuya = _probe_tuya_udp(ip)
            if tuya:
                entry["tuya"] = tuya
                entry["tuya_unknown"] = True
        except Exception as e:
            log.debug(f"[Scan] tuya probe {ip}: {e}")
        return entry

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="scan") as pool:
        futures = {pool.submit(scan_one, ip): ip for ip in ips}
        for fut in as_completed(futures):
            try:
                entry = fut.result()
                if entry is not None:
                    results.append(entry)
            except Exception:
                pass

    results.sort(key=lambda x: x["ms"])
    log.info(f"[Scan] Найдено {len(results)} устройств с открытым портом {SCAN_PORT}")
    return results


# ==================== MQTT ====================
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tuya_bridge")
if MQTT_USERNAME:
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

mqtt_client.will_set(f"{TOPIC_PREFIX}/bridge/status", "offline", qos=1, retain=True)
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

CLEANUP_MODE = {"active": False, "count": 0}


def collect_our_unique_ids():
    ids = set()
    # v1.8.6: идём по ALL_DEVICES, а не DEVICES — иначе unique_id
    # отключённых (enabled:false) выпадают из OUR_IDS, и команда
    # «Cleanup Discovery» их не находит. При soft-disable это
    # безопасно: мы и не хотим их удалять, но если пользователь
    # захочет — Cleanup должен их видеть.
    with DEVICES_LOCK:
        devs = list(ALL_DEVICES)
    for dev in devs:
        name = dev["name"]
        dtype = dev.get("type", "sensor")
        if dtype == "light":
            ids.add(f"{name}_light")
        if dtype == "climate":
            ids.add(f"{name}_climate")
        # v1.10.20
        if dtype == "cover":
            ids.add(f"{name}_cover")
        if dtype == "fan":
            ids.add(f"{name}_fan")
        for dp_str, info in dev.get("dps_map", {}).items():
            comp = info.get("component")
            ent = info.get("name", f"dp_{dp_str}")
            if comp in ("switch", "select", "number"):
                ids.add(f"{name}_{ent}")
            elif comp in ("sensor", "binary_sensor"):
                ids.add(f"{name}_{ent}")
            elif comp == "lock":           # v1.10.20
                ids.add(f"{name}_{ent}")
        if dev.get("dps_map", {}).get("6", {}).get("component") == "phase_a":
            for s in ("voltage", "current", "power"):
                ids.add(f"{name}_output_{s}")
    return ids


OUR_IDS = collect_our_unique_ids()


def refresh_our_ids():
    """v1.8.4: пересчитать OUR_IDS после import/delete, чтобы
    Cleanup Discovery видел актуальный список unique_id."""
    global OUR_IDS
    OUR_IDS = collect_our_unique_ids()


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info("[MQTT] Подключён")
        client.publish(f"{TOPIC_PREFIX}/bridge/status", "online", qos=1, retain=True)
        client.publish(f"{TOPIC_PREFIX}/bridge/version", DISCOVERY_VERSION, qos=1, retain=True)
        client.subscribe(f"{TOPIC_PREFIX}/light/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/switch/+/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/climate/+/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/select/+/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/number/+/+/set")
        # v1.10.20: cover / fan / lock
        client.subscribe(f"{TOPIC_PREFIX}/cover/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/cover/+/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/fan/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/fan/+/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/lock/+/+/set")
        client.subscribe(f"{TOPIC_PREFIX}/bridge/cleanup")
        client.subscribe(f"{TOPIC_PREFIX}/bridge/edit_config")
        client.subscribe(f"{TOPIC_PREFIX}/bridge/delete_device")
        client.subscribe(f"{TOPIC_PREFIX}/bridge/import_devices")
        client.subscribe(f"{TOPIC_PREFIX}/bridge/scan_network")
        client.subscribe(f"{DISCOVERY_PREFIX}/#", qos=1)
        log.info("[MQTT] Подписка на команды и Discovery")
    else:
        log.error(f"[MQTT] Ошибка подключения: {rc}")


def on_disconnect(client, userdata, flags, rc, properties=None):
    log.warning(f"[MQTT] Отключён (rc={rc}), переподключение...")


LAST_CMD = {}
LAST_CMD_LOCK = threading.Lock()


def is_duplicate_retained(topic, payload, window=RETAINED_DUP_WINDOW):
    key = (topic, payload)
    now = time.time()
    with LAST_CMD_LOCK:
        last = LAST_CMD.get(key)
        LAST_CMD[key] = now
        stale = [k for k, t in LAST_CMD.items() if now - t > window]
        for k in stale:
            LAST_CMD.pop(k, None)
    return last is not None and (now - last) < window


CMD_POOL = ThreadPoolExecutor(max_workers=CMD_POOL_SIZE, thread_name_prefix="cmd")


def on_message(client, userdata, msg):
    topic = msg.topic

    if CLEANUP_MODE["active"] and topic.startswith(DISCOVERY_PREFIX + "/"):
        parts = topic.split("/")
        if len(parts) >= 3 and parts[2] in OUR_IDS:
            client.publish(topic, payload=None, qos=1, retain=True)
            CLEANUP_MODE["count"] += 1
        return

    if topic == f"{TOPIC_PREFIX}/bridge/cleanup":
        log.info("[MQTT] Получена команда cleanup")
        threading.Thread(target=_handle_cleanup_command, daemon=True, name="cleanup-cmd").start()
        return

    if topic == f"{TOPIC_PREFIX}/bridge/edit_config":
        log.info("[MQTT] Получена команда edit_config")
        payload_str = msg.payload.decode("utf-8", errors="replace")
        threading.Thread(target=_handle_edit_config, args=(payload_str,),
                         daemon=True, name="edit-config").start()
        return

    if topic == f"{TOPIC_PREFIX}/bridge/delete_device":
        log.info("[MQTT] Получена команда delete_device")
        payload_str = msg.payload.decode("utf-8", errors="replace")
        threading.Thread(target=_handle_delete_device, args=(payload_str,),
                         daemon=True, name="delete-device").start()
        return

    if topic == f"{TOPIC_PREFIX}/bridge/import_devices":
        log.info("[MQTT] Получена команда import_devices")
        payload_str = msg.payload.decode("utf-8", errors="replace")
        threading.Thread(target=_handle_import_devices, args=(payload_str,),
                         daemon=True, name="import-devices").start()
        return

    if topic == f"{TOPIC_PREFIX}/bridge/scan_network":
        log.info("[MQTT] Получена команда scan_network")
        payload_str = msg.payload.decode("utf-8", errors="replace")
        threading.Thread(target=_handle_scan_network, args=(payload_str,),
                         daemon=True, name="scan-network").start()
        return

    if not topic.endswith("/set"):
        return

    payload = msg.payload.decode("utf-8", errors="replace").strip()

    if msg.retain and is_duplicate_retained(topic, payload):
        log.debug(f"[MQTT] Пропуск retained-дубля: {topic}")
        return

    if DEBUG_MQTT_CMD:
        log.info(f"[MQTT-CMD] <- {topic}: {payload}")

    parts = topic.split("/")
    if len(parts) < 4:
        return

    component = parts[1]
    dev_name = parts[2]
    with DEVICES_LOCK:
        dev = DEVICE_INDEX.get(dev_name)
    if not dev:
        return

    cmd = parse_payload(payload)
    CMD_POOL.submit(process_command, dev, component, parts, cmd)


def process_command(dev, component, parts, cmd):
    name = dev["name"]
    lock = get_device_cmd_lock(name)
    with lock:
        with DEVICES_LOCK:
            if name not in DEVICE_INDEX:
                return
        wait_device_rate_limit(name, component)
        try:
            tuya = get_device_conn(dev)
            if component == "light":
                handle_light_command(tuya, dev, cmd)
            elif component == "switch":
                handle_switch_command(tuya, dev, parts, cmd)
            elif component == "climate":
                handle_climate_command(tuya, dev, parts, cmd)
            elif component == "select":
                handle_select_command(tuya, dev, parts, cmd)
            elif component == "number":
                handle_number_command(tuya, dev, parts, cmd)
            # v1.10.20
            elif component == "cover":
                handle_cover_command(tuya, dev, parts, cmd)
            elif component == "fan":
                handle_fan_command(tuya, dev, parts, cmd)
            elif component == "lock":
                handle_lock_command(tuya, dev, parts, cmd)
        except Exception as e:
            log.warning(f"[Tuya] Ошибка команды {name}: {e}")
            drop_device_conn(name)
            return

    request_status(name)


# ==================== OPTIMISTIC UPDATE ====================
def _cache_update(dev_name, updates):
    if not updates:
        return False
    with STATE_LOCK:
        cache = STATE_CACHE.setdefault(dev_name, {})
        changed = False
        for dp, v in updates.items():
            if cache.get(str(dp)) != v:
                cache[str(dp)] = v
                changed = True
    if changed:
        mark_state_dirty()
    return changed


def optimistic_update_many(dev, updates: dict):
    if not updates:
        return
    _cache_update(dev["name"], updates)


def optimistic_update(dev, dp, value):
    optimistic_update_many(dev, {str(dp): value})


# ==================== ДЕБАУНС ====================
DEBOUNCE = {}
DEBOUNCE_LOCK = threading.Lock()


def debounced_set(tuya, dev, dp, value, dp_type=None, window_ms=None, component="light"):
    if window_ms is None:
        window_ms = DEBOUNCE_BY_TYPE.get(dp_type, 0) if dp_type else 0

    if window_ms <= 0:
        try:
            tuya.set_value(dp, value)
            optimistic_update(dev, dp, value)
        except Exception as e:
            log.warning(f"[Set] {dev['name']} dp={dp}: {e}")
            drop_device_conn(dev["name"])
            return
        request_status(dev["name"])
        return

    key = (dev["name"], dp)
    window = window_ms / 1000.0
    dev_name = dev["name"]

    with DEBOUNCE_LOCK:
        entry = DEBOUNCE.get(key)
        if entry and entry["timer"]:
            entry["timer"].cancel()

        def fire():
            # v1.10.3: pop ПЕРЕД выполнением — только если мы всё ещё
            # "текущий" таймер. Иначе новый debounced_set перезаписал бы
            # DEBOUNCE[key], а старый fire снёс бы запись нового.
            with DEBOUNCE_LOCK:
                current = DEBOUNCE.get(key)
                if current is None or current["timer"] is not _t_ref.get("timer"):
                    return
                DEBOUNCE.pop(key, None)
            lock = get_device_cmd_lock(dev_name)
            with lock:
                with DEVICES_LOCK:
                    if dev_name not in DEVICE_INDEX:
                        return
                wait_device_rate_limit(dev_name, component)
                try:
                    d = get_device_conn(dev)
                    d.set_value(dp, value)
                    optimistic_update(dev, dp, value)
                except Exception as e:
                    log.warning(f"[Debounce] {dev_name} dp={dp}: {e}")
                    drop_device_conn(dev_name)
            request_status(dev_name)

        _t_ref = {}
        t = threading.Timer(window, fire)
        t.daemon = True
        _t_ref["timer"] = t
        DEBOUNCE[key] = {"timer": t, "window_ms": window_ms}
        t.start()


# ==================== HSV <-> RGB ====================
def tuya_hsv_to_rgb(hex_str):
    try:
        h = int(hex_str[0:4], 16)
        s = int(hex_str[4:8], 16)
        v = int(hex_str[8:12], 16)
    except (ValueError, IndexError):
        return (255, 255, 255)
    hh = (h % 360) / 360.0
    ss = min(s, 1000) / 1000.0
    vv = min(v, 1000) / 1000.0
    r, g, b = colorsys.hsv_to_rgb(hh, ss, vv)
    return (int(r * 255), int(g * 255), int(b * 255))


def rgb_to_tuya_hsv(r, g, b):
    hh, ss, vv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    h = int(hh * 360)
    s = int(ss * 1000)
    v = int(vv * 1000)
    return f"{h:04x}{s:04x}{v:04x}"


# ==================== PHASE_A PARSER ====================
def parse_phase_a(b64_str):
    try:
        raw = base64.b64decode(b64_str)
        if len(raw) < 8:
            return None, None, None
        voltage = int.from_bytes(raw[0:2], "big") / 10.0
        current = int.from_bytes(raw[2:5], "big") / 1000.0
        power = int.from_bytes(raw[5:8], "big") / 1000.0
        return voltage, current, power
    except Exception as e:
        log.debug(f"[phase_a] parse failed: {e}")
        return None, None, None


# ==================== STATE CACHE PERSISTENCE ====================
_state_dirty = False
_state_dirty_lock = threading.Lock()


def load_state_cache():
    if not STATE_CACHE_SAVE_ON_START:
        return
    if not os.path.exists(STATE_CACHE_FILE):
        log.info(f"[Cache] Файл {STATE_CACHE_FILE} не найден, старт с пустым кэшем")
        return
    try:
        with open(STATE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("cache not a dict")
        with STATE_LOCK:
            for dev_name, dps in data.items():
                if dev_name in STATE_CACHE and isinstance(dps, dict):
                    STATE_CACHE[dev_name].update(dps)
        total = sum(len(v) for v in data.values() if isinstance(v, dict))
        log.info(f"[Cache] Загружено {total} значений по {len(data)} устройствам")
    except Exception as e:
        log.warning(f"[Cache] Не удалось загрузить {STATE_CACHE_FILE}: {e}")


def save_state_cache():
    global _state_dirty
    # v1.10.15: сбрасываем флаг СРАЗУ после проверки. Иначе mark_state_dirty()
    # во время snapshot терялся — сброс в конце затирал его.
    with _state_dirty_lock:
        if not _state_dirty:
            return
        _state_dirty = False

    try:
        with STATE_LOCK:
            data = {k: dict(v) for k, v in STATE_CACHE.items() if v}

        dir_name = os.path.dirname(os.path.abspath(STATE_CACHE_FILE)) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".state_cache_", suffix=".tmp", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, STATE_CACHE_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        with _state_dirty_lock:
            _state_dirty = True
        log.warning(f"[Cache] Не удалось сохранить: {e} (флаг dirty восстановлен)")


# ==================== BATTERY LAST UP PERSIST (v1.10.0) ====================
def load_battery_last_up():
    """Загружает state/battery_last_up.json. Вызывается один раз из main()."""
    global BATTERY_LAST_UP
    if not os.path.exists(BATTERY_LAST_UP_FILE):
        BATTERY_LAST_UP = {}
        log.info(f"[Battery] {BATTERY_LAST_UP_FILE} не найден, старт с пустым")
        return
    try:
        with open(BATTERY_LAST_UP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            BATTERY_LAST_UP = {k: int(v) for k, v in data.items()
                               if isinstance(v, (int, float))
                               and not isinstance(v, bool)}
            log.info(f"[Battery] last_up загружено: {len(BATTERY_LAST_UP)} устройств")
        else:
            BATTERY_LAST_UP = {}
            log.warning(f"[Battery] {BATTERY_LAST_UP_FILE}: не dict, сброс")
    except Exception as e:
        BATTERY_LAST_UP = {}
        log.warning(f"[Battery] load last_up: {e}")


def _save_battery_last_up_unlocked():
    """Атомарная запись BATTERY_LAST_UP в файл."""
    try:
        dir_name = os.path.dirname(os.path.abspath(BATTERY_LAST_UP_FILE)) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".battery_last_up_", suffix=".tmp", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(dict(BATTERY_LAST_UP), f,
                          ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, BATTERY_LAST_UP_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        log.warning(f"[Battery] save last_up: {e}")


_BATTERY_LAST_UP_LOCK = threading.Lock()


def save_battery_last_up(name, ts):
    """Сохраняет _last_up_ts для одного устройства (атомарно)."""
    with _BATTERY_LAST_UP_LOCK:
        BATTERY_LAST_UP[name] = int(ts)
        _save_battery_last_up_unlocked()


def remove_battery_last_up(name):
    """Удаляет запись при delete_device."""
    with _BATTERY_LAST_UP_LOCK:
        if name in BATTERY_LAST_UP:
            BATTERY_LAST_UP.pop(name, None)
            _save_battery_last_up_unlocked()


def mark_state_dirty():
    global _state_dirty
    with _state_dirty_lock:
        _state_dirty = True


def state_cache_worker():
    log.info(f"[Cache] Writer запущен (интервал {STATE_SAVE_INTERVAL}с)")
    while not STOP_EVENT.is_set():
        STOP_EVENT.wait(STATE_SAVE_INTERVAL)
        if STOP_EVENT.is_set():
            break
        save_state_cache()
    save_state_cache()
    log.info("[Cache] Writer остановлен")


# ==================== КОМАНДЫ ====================
def handle_light_command(tuya, dev, cmd):
    if not isinstance(cmd, dict):
        return

    payload = {}
    cache = {}
    dp_types = {}

    dp_state, _ = find_dp_by_name(dev, "switch_led")
    if dp_state is not None and "state" in cmd:
        val = cmd["state"] == "ON"
        payload[int(dp_state)] = val
        cache[dp_state] = val
        dp_types[int(dp_state)] = "switch_led"

    dp_bright, info_bright = find_dp_by_name(dev, "bright_value")
    if dp_bright is not None and "brightness" in cmd and info_bright is not None:
        bmin, bmax = get_bright_bounds(info_bright)
        val = ha_to_tuya_brightness(cmd["brightness"], bmin, bmax)
        payload[int(dp_bright)] = val
        cache[dp_bright] = val
        dp_types[int(dp_bright)] = "bright_value"

    dp_temp, info_temp = find_dp_by_name(dev, "temp_value")
    if dp_temp is not None and ("color_temp_kelvin" in cmd or "color_temp" in cmd) and info_temp is not None:
        kelvin = cmd.get("color_temp_kelvin") or cmd.get("color_temp")
        kmin, kmax = get_kelvin_bounds(info_temp)
        try:
            kelvin = float(kelvin)
        except (TypeError, ValueError):
            kelvin = kmin
        val = max(0, min(1000, int((kelvin - kmin) * 1000 / (kmax - kmin))))
        payload[int(dp_temp)] = val
        cache[dp_temp] = val
        dp_types[int(dp_temp)] = "temp_value"

    dp_color, _ = find_dp_by_name(dev, "colour_data")
    if dp_color is not None and "color" in cmd:
        color = cmd["color"] or {}
        hex_val = rgb_to_tuya_hsv(
            int(color.get("r", 255)),
            int(color.get("g", 255)),
            int(color.get("b", 255)),
        )
        payload[int(dp_color)] = hex_val
        cache[dp_color] = hex_val
        dp_types[int(dp_color)] = "colour_data"

    if not payload:
        return

    if len(payload) == 1:
        dp, val = next(iter(payload.items()))
        debounced_set(tuya, dev, dp, val, dp_type=dp_types.get(dp), component="light")
        return

    try:
        tuya.set_multiple_values(payload)
        optimistic_update_many(dev, cache)
    except Exception as e:
        log.warning(f"[Light] set_multiple_values failed: {e}; fallback")
        success_cache = {}
        for dp, v in payload.items():
            try:
                tuya.set_value(dp, v)
                success_cache[dp] = v
            except Exception as ee:
                log.warning(f"[Light] set_value({dp}) failed: {ee}")
        if success_cache:
            optimistic_update_many(dev, success_cache)


def handle_switch_command(tuya, dev, topic_parts, cmd):
    if len(topic_parts) < 5:
        return
    entity_name = topic_parts[3]

    if isinstance(cmd, str):
        val = cmd.upper() in ("ON", "1", "TRUE")
    elif isinstance(cmd, bool):
        val = cmd
    elif isinstance(cmd, dict) and "state" in cmd:
        val = str(cmd["state"]).upper() in ("ON", "1", "TRUE")
    else:
        return

    for dp_str, info in dev["dps_map"].items():
        if info.get("component") == "switch" and info.get("name") == entity_name:
            dp = dp_int(dp_str)
            if dp is None:
                return
            debounced_set(tuya, dev, dp, val, window_ms=0, component="switch")
            return


def handle_climate_command(tuya, dev, topic_parts, cmd):
    if len(topic_parts) < 5:
        return
    subtopic = topic_parts[3]

    for dp_str, info in dev["dps_map"].items():
        name = info.get("name")
        dp = dp_int(dp_str)
        if dp is None:
            continue

        if subtopic == "mode" and name == "switch":
            val = str(cmd).upper() in ("HEAT", "ON", "1", "TRUE")
            debounced_set(tuya, dev, dp, val, window_ms=0, component="climate")
            return

        elif subtopic == "temp" and name == "temp_set":
            try:
                temp = float(cmd) if not isinstance(cmd, dict) else float(cmd.get("temperature", 0))
            except (TypeError, ValueError):
                return
            val = int(temp * 10)
            debounced_set(tuya, dev, dp, val, dp_type="temp_set", component="climate")
            return

        elif subtopic == "preset" and name == "preset_mode":
            val_ha = str(cmd).strip('"')
            preset_map = dev.get("preset_map", {})
            reverse_map = {v: k for k, v in preset_map.items()}
            val = reverse_map.get(val_ha, val_ha).lower()
            debounced_set(tuya, dev, dp, val, window_ms=0, component="climate")
            return


def handle_select_command(tuya, dev, topic_parts, cmd):
    if len(topic_parts) < 5:
        return
    entity_name = topic_parts[3]
    val = str(cmd).strip('"')

    for dp_str, info in dev["dps_map"].items():
        if info.get("component") != "select" or info.get("name") != entity_name:
            continue
        dp = dp_int(dp_str)
        if dp is None:
            return

        smap = get_select_map(info)
        if smap:
            rev = {v: k for k, v in smap.items()}
            val = rev.get(val, val)

        debounced_set(tuya, dev, dp, val, window_ms=0, component="select")
        return


def handle_number_command(tuya, dev, topic_parts, cmd):
    if len(topic_parts) < 5:
        return
    entity_name = topic_parts[3]

    for dp_str, info in dev["dps_map"].items():
        if info.get("component") != "number" or info.get("name") != entity_name:
            continue
        dp = dp_int(dp_str)
        if dp is None:
            return

        scale = info.get("scale", 0)
        try:
            raw = float(cmd)
        except (TypeError, ValueError):
            return
        val = int(round(raw * (10 ** scale)))

        debounced_set(tuya, dev, dp, val, dp_type="number", component="number")
        return


# ==================== COVER / FAN / LOCK (v1.10.20) ====================
def _to_int_percent(val):
    """Привести значение к целому 0..100 (положение/скорость)."""
    try:
        n = int(round(float(val)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))


def _lock_locked(val, info):
    """bool-DP замка → заперто? Поле `inverted: true` меняет смысл."""
    if isinstance(val, str):
        locked = val.strip().lower() in ("true", "1", "locked", "lock")
    else:
        locked = bool(val)
    return (not locked) if info.get("inverted") else locked


def handle_cover_command(tuya, dev, topic_parts, cmd):
    """v1.10.20: HA cover → Tuya.

    tuya/cover/<dev>/set           — OPEN / CLOSE / STOP
    tuya/cover/<dev>/position/set  — 0..100
    """
    sub = topic_parts[3] if len(topic_parts) > 4 else ""

    if sub == "position":
        pos = _to_int_percent(cmd if not isinstance(cmd, dict) else cmd.get("position"))
        if pos is None:
            return
        dp_str, _info = find_dp_by_name(dev, "percent_control", component="cover")
        if dp_str is None:
            return
        debounced_set(tuya, dev, dp_int(dp_str), pos, window_ms=0, component="cover")
        return

    action = str(cmd).strip().strip('"').lower()
    payload = {"open": "open", "close": "close", "stop": "stop"}.get(action)
    if payload is None:
        return
    dp_str, _info = find_dp_by_name(dev, "control", component="cover")
    if dp_str is None:
        return
    debounced_set(tuya, dev, dp_int(dp_str), payload, window_ms=0, component="cover")


def handle_fan_command(tuya, dev, topic_parts, cmd):
    """v1.10.20: HA fan → Tuya.

    tuya/fan/<dev>/set             — ON / OFF
    tuya/fan/<dev>/preset/set      — enum скорости (fan_speed с options)
    tuya/fan/<dev>/speed/set       — 1..100 (fan_speed без options)
    tuya/fan/<dev>/direction/set   — forward / reverse
    """
    sub = topic_parts[3] if len(topic_parts) > 4 else ""

    if not sub:
        if isinstance(cmd, dict):
            return
        val = str(cmd).upper() in ("ON", "1", "TRUE")
        dp_str, _info = find_dp_by_name(dev, "switch", component="fan")
        if dp_str is None:
            return
        debounced_set(tuya, dev, dp_int(dp_str), val, window_ms=0, component="fan")
        return

    if sub == "preset":
        dp_str, info = find_dp_by_name(dev, "fan_speed", component="fan")
        if dp_str is None or not info.get("options"):
            return
        val = str(cmd).strip().strip('"')
        smap = get_select_map(info)
        if smap:
            val = {v: k for k, v in smap.items()}.get(val, val)
        debounced_set(tuya, dev, dp_int(dp_str), val, window_ms=0, component="fan")
        return

    if sub == "speed":
        dp_str, info = find_dp_by_name(dev, "fan_speed", component="fan")
        if dp_str is None or info.get("options"):
            return
        val = _to_int_percent(cmd)
        if val is None:
            return
        val = max(info.get("min", 1), min(info.get("max", 100), val))
        debounced_set(tuya, dev, dp_int(dp_str), val, window_ms=0, component="fan")
        return

    if sub == "direction":
        val = str(cmd).strip().strip('"').lower()
        if val not in ("forward", "reverse"):
            return
        dp_str, _info = find_dp_by_name(dev, "fan_direction", component="fan")
        if dp_str is None:
            return
        debounced_set(tuya, dev, dp_int(dp_str), val, window_ms=0, component="fan")


def handle_lock_command(tuya, dev, topic_parts, cmd):
    """v1.10.20: HA lock → Tuya.

    tuya/lock/<dev>/<entity>/set — LOCK / UNLOCK
    """
    if len(topic_parts) < 5:
        return
    entity_name = topic_parts[3]
    raw = str(cmd).strip().strip('"').upper()
    if raw not in ("LOCK", "UNLOCK"):
        return
    wanted_locked = raw == "LOCK"

    for dp_str, info in dev["dps_map"].items():
        if info.get("component") != "lock" or info.get("name") != entity_name:
            continue
        dp = dp_int(dp_str)
        if dp is None:
            return
        val = (not wanted_locked) if info.get("inverted") else wanted_locked
        debounced_set(tuya, dev, dp, val, window_ms=0, component="lock")
        return


mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.on_disconnect = on_disconnect


# ==================== ОЧИСТКА DISCOVERY ====================
def cleanup_discovery():
    log.info("[Cleanup] Очистка старых Discovery-сообщений (только наших)...")
    CLEANUP_MODE["active"] = True
    CLEANUP_MODE["count"] = 0
    STOP_EVENT.wait(5)
    CLEANUP_MODE["active"] = False
    log.info(f"[Cleanup] Удалено сообщений: {CLEANUP_MODE['count']}")
    STOP_EVENT.wait(DISCOVERY_CLEANUP_WAIT)


def _handle_cleanup_command():
    log.info("[Cleanup] Запуск по команде из MQTT...")
    CLEANUP_MODE["active"] = True
    CLEANUP_MODE["count"] = 0
    for _ in range(50):
        if STOP_EVENT.is_set():
            break
        time.sleep(0.1)
    CLEANUP_MODE["active"] = False
    removed = CLEANUP_MODE["count"]

    republished = 0
    with DEVICES_LOCK:
        devs_snapshot = list(DEVICES)
    for dev in devs_snapshot:
        try:
            publish_discovery(dev)
            republished += 1
        except Exception as e:
            log.warning(f"[Cleanup] republish {dev['name']}: {e}")
        time.sleep(0.05)

    log.info(f"[Cleanup] Готово: removed={removed}, republished={republished}")



# ==================== DPS MAP DISCOVERY CLEANUP (v1.8.5) ====================

def _remove_discovery_and_state(dev, dp_str, old_info):
    """
    Удаляет Discovery-конфиг и retained state для одного DP.

    Используется при edit_config, когда пользователь удалил DP:
    без этой очистки HA будет показывать сущность как 'unavailable',
    а через некоторое время — как 'restored' (ghost entity).
    """
    dev_name = dev["name"]
    dev_type = dev.get("type", "sensor")
    comp = old_info.get("component")
    name = old_info.get("name", f"dp_{dp_str}")

    # Для light Discovery — одна сущность на всё устройство
    # (publish_light), отдельные DP не публикуют свои топики.
    # Но если DP менял light entity (например, удалили colour_data) —
    # надо перепубликовать light discovery. Это делает
    # publish_discovery(dev) в _handle_edit_config.
    if dev_type == "light" and not comp:
        return

    # v1.10.20: cover/fan — одна сущность на устройство, отдельного discovery
    # на DP нет. Её конфиг перепубликуется через publish_discovery(dev).
    if dev_type in ("cover", "fan"):
        return

    discovery_topic = None
    state_topic = None

    if comp == "switch":
        discovery_topic = f"{DISCOVERY_PREFIX}/switch/{dev_name}_{name}/config"
        state_topic = f"{TOPIC_PREFIX}/switch/{dev_name}/{name}/state"
    elif comp == "select":
        discovery_topic = f"{DISCOVERY_PREFIX}/select/{dev_name}_{name}/config"
        state_topic = f"{TOPIC_PREFIX}/select/{dev_name}/{name}/state"
    elif comp == "number":
        discovery_topic = f"{DISCOVERY_PREFIX}/number/{dev_name}_{name}/config"
        state_topic = f"{TOPIC_PREFIX}/number/{dev_name}/{name}/state"
    elif comp in ("sensor", "binary_sensor"):
        discovery_topic = f"{DISCOVERY_PREFIX}/{comp}/{dev_name}_{name}/config"
        state_topic = f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/dps/{dp_str}/state"
    elif comp == "lock":               # v1.10.20
        discovery_topic = f"{DISCOVERY_PREFIX}/lock/{dev_name}_{name}/config"
        state_topic = f"{TOPIC_PREFIX}/lock/{dev_name}/{name}/state"
    elif comp == "phase_a":
        # 3 отдельных сенсора
        for suffix in ("voltage", "current", "power"):
            mqtt_client.publish(
                f"{DISCOVERY_PREFIX}/sensor/{dev_name}_output_{suffix}/config",
                payload=None, qos=1, retain=True,
            )
            mqtt_client.publish(
                f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/phase_a/{suffix}/state",
                payload=None, qos=1, retain=True,
            )
        return
    elif comp == "preset":
        # preset_mode — часть climate entity, отдельного discovery нет.
        # Climate discovery перепубликуется через publish_discovery(dev).
        return

    if discovery_topic:
        mqtt_client.publish(discovery_topic, payload=None, qos=1, retain=True)
    if state_topic:
        mqtt_client.publish(state_topic, payload=None, qos=1, retain=True)


def _diff_dps_maps(old_map, new_map):
    """
    Возвращает (removed, added, changed) — множества DP-строк.

    removed — DP, которые были в old, но нет в new
    added   — DP, которых не было в old, но есть в new
    changed — DP, которые есть в обоих, но info отличается
    """
    old_keys = set(old_map.keys())
    new_keys = set(new_map.keys())
    removed = old_keys - new_keys
    added = new_keys - old_keys
    changed = {k for k in (old_keys & new_keys) if old_map[k] != new_map[k]}
    return removed, added, changed

# ==================== EDIT CONFIG ====================
def _extract_request_id(payload_str):
    """v1.8.3: достаём request_id из payload, даже если остальной JSON
    сломан. Нужно, чтобы ответ ушёл с правильным request_id в ошибочных
    ветках (disabled edit, invalid json)."""
    if not payload_str:
        return None
    try:
        d = json.loads(payload_str)
        if isinstance(d, dict):
            return d.get("request_id")
    except Exception:
        pass
    # fallback: регулярка на "request_id": "..."
    m = re.search(r'"request_id"\s*:\s*"([^"]+)"', payload_str)
    if m:
        return m.group(1)
    return None


def _wait_worker_exit(name, timeout=3.0):
    """Ждать, пока тред worker-<name> завершится. v1.9.14."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = False
        for t in threading.enumerate():
            if t.name == f"worker-{name}" and t.is_alive():
                alive = True
                break
        if not alive:
            return True
        time.sleep(0.1)
    return False


def _worker_alive(name):
    """True, если тред worker-<name> ещё жив. v1.10.15."""
    for t in threading.enumerate():
        if t.name == f"worker-{name}" and t.is_alive():
            return True
    return False


def _ensure_worker_running(dev, reason=""):
    """Запустить воркер, только если для устройства нет живого треда.

    v1.10.15: единая точка запуска — исключает два воркера на одно
    устройство (правило №1: один persistent-сокет). True, если запущен.
    """
    name = dev["name"]
    with DEVICES_LOCK:
        if name not in DEVICE_INDEX:
            return False
    if _worker_alive(name):
        return False
    target = run_battery_listener if dev.get("battery_powered") else run_polling_device
    t = threading.Thread(target=target, args=(dev,), daemon=True, name=f"worker-{name}")
    t.start()
    if reason:
        log.info(f"[Worker] {name}: запущен ({reason})")
    return True


def _handle_edit_config(payload_str):
    # v1.8.3: сначала извлекаем request_id, потом проверяем флаги —
    # чтобы ответ ушёл с правильным request_id даже в ошибочных ветках.
    req_id = _extract_request_id(payload_str)

    if not ALLOW_CONFIG_EDIT:
        _publish_edit_result(None, False, "config edit disabled",
                             request_id=req_id)
        return

    try:
        data = json.loads(payload_str)
    except (json.JSONDecodeError, ValueError):
        _publish_edit_result(None, False, "invalid json",
                             request_id=req_id)
        return
    if not isinstance(data, dict):
        _publish_edit_result(None, False, "payload must be json object",
                             request_id=req_id)
        return

    request_id = data.get("request_id") or req_id
    dev_name = data.get("device")
    changes = data.get("changes", {})
    validate = data.get("validate", VALIDATE_ON_EDIT)

    if not dev_name or not isinstance(dev_name, str):
        _publish_edit_result(None, False, "device required (string)",
                             request_id=request_id)
        return
    if not isinstance(changes, dict) or not changes:
        _publish_edit_result(dev_name, False, "device and changes required",
                             request_id=request_id)
        return

    # v1.8.5: dps_map добавлен в разрешённые поля.
    ALLOWED_FIELDS = {"ip", "local_key", "version", "dps_map", "enabled",
                      "battery_powered", "expire_after"}
    filtered = {k: v for k, v in changes.items() if k in ALLOWED_FIELDS}

    if not filtered:
        _publish_edit_result(dev_name, False, "no allowed fields",
                             request_id=request_id)
        return

    # v1.8.6: ищем в DEVICE_INDEX, а если нет — в ALL_DEVICES.
    # Иначе отключённое (enabled:false) устройство нельзя включить
    # обратно: в DEVICE_INDEX его нет, и мы бы вернули "device not found".
    # v1.10.13: если устройства нет в DEVICE_INDEX — перечитываем
    # ALL_DEVICES из файла. Иначе ручное редактирование
    # devices_config.json (добавление устройства без рестарта)
    # не подхватится — ALL_DEVICES остаётся снимком на старте.
    dev = None
    with DEVICES_LOCK:
        dev = DEVICE_INDEX.get(dev_name)
    if dev is None:
        _reload_all_devices()
        with DEVICES_LOCK:
            for _d in ALL_DEVICES:
                if _d.get("name") == dev_name:
                    dev = _d
                    break
    if not dev:
        _publish_edit_result(dev_name, False, "device not found",
                             request_id=request_id)
        return

    # --- Валидация ip ---
    if "ip" in filtered:
        if not _is_valid_ip(filtered["ip"]):
            _publish_edit_result(dev_name, False, f"invalid ip: {filtered['ip']}",
                                 request_id=request_id)
            return
        filtered["ip"] = str(filtered["ip"]).strip()
        # v1.9.10: IP не должен быть занят другим устройством.
        with DEVICES_LOCK:
            for _d in ALL_DEVICES:
                if _d.get("name") == dev_name:
                    continue
                if _d.get("ip") == filtered["ip"]:
                    _publish_edit_result(
                        dev_name, False,
                        f"IP {filtered['ip']} уже занят устройством '{_d['name']}'",
                        request_id=request_id,
                    )
                    return

    # --- Валидация local_key ---
    if "local_key" in filtered:
        if not _is_valid_key(filtered["local_key"]):
            _publish_edit_result(dev_name, False, "invalid local_key",
                                 request_id=request_id)
            return
        filtered["local_key"] = str(filtered["local_key"]).strip()

    # --- Валидация version ---
    if "version" in filtered:
        if not _is_valid_version(filtered["version"]):
            _publish_edit_result(dev_name, False, f"invalid version: {filtered['version']}",
                                 request_id=request_id)
            return
        filtered["version"] = str(filtered["version"]).strip()

    # --- Валидация enabled (v1.8.6) ---
    if "enabled" in filtered:
        v_en = filtered["enabled"]
        if not isinstance(v_en, bool):
            _publish_edit_result(dev_name, False,
                                 f"invalid enabled (must be bool): {v_en!r}",
                                 request_id=request_id)
            return

    # --- Валидация battery_powered (v1.9.14) ---
    if "battery_powered" in filtered:
        v_bp = filtered["battery_powered"]
        if not isinstance(v_bp, bool):
            _publish_edit_result(dev_name, False,
                                 f"invalid battery_powered (must be bool): {v_bp!r}",
                                 request_id=request_id)
            return

    # --- Валидация expire_after (v1.10.3) ---
    if "expire_after" in filtered:
        v_exp = filtered["expire_after"]
        if isinstance(v_exp, bool) or not isinstance(v_exp, int):
            _publish_edit_result(dev_name, False,
                                 f"invalid expire_after (must be int): {v_exp!r}",
                                 request_id=request_id)
            return
        if v_exp <= 0:
            _publish_edit_result(dev_name, False,
                                 f"invalid expire_after (must be > 0): {v_exp}",
                                 request_id=request_id)
            return

    # --- Валидация dps_map ---
    warnings = []
    if "dps_map" in filtered:
        dev_type = dev.get("type", "sensor")
        ok_dps, err_dps, warn_dps = _validate_dps_map(dev_type, filtered["dps_map"])
        if not ok_dps:
            _publish_edit_result(dev_name, False, f"invalid dps_map: {err_dps}",
                                 request_id=request_id)
            return
        warnings = warn_dps
        # Нормализация ключей в строки (на случай, если пришли числа)
        filtered["dps_map"] = {str(k): v for k, v in filtered["dps_map"].items()}

    # --- TCP-валидация (только если есть ip/key/version и validate=True) ---
    # v1.8.5: dps_map НЕ триггерит TCP-валидацию. Только ip/key/version.
    # v1.10.15: если устройство уже опрашивается, воркер держит persistent-
    # сокет. Второе TCP (новый tinytuya.Device.status()) даёт 914. Поэтому
    # сначала останавливаем воркер, затем валидируем. Воркер поднимем заново:
    # при успехе — в конце (см. _ensure_worker_running), при провале — здесь.
    tcp_change = any(k in filtered for k in ("ip", "local_key", "version"))
    _worker_stopped_for_validate = False
    if validate and tcp_change:
        new_ip = filtered.get("ip", dev["ip"])
        new_key = filtered.get("local_key", dev["local_key"])
        new_ver = filtered.get("version", dev["version"])

        with DEVICES_LOCK:
            _in_pool_validate = dev_name in DEVICE_INDEX
        if _in_pool_validate:
            drop_device_conn(dev_name)
            request_worker_restart(dev_name)
            _worker_stopped_for_validate = _wait_worker_exit(dev_name, timeout=3.0)
            if not _worker_stopped_for_validate:
                log.warning(f"[EditConfig] {dev_name}: воркер не завершился за 3с — "
                            f"TCP-валидация пропущена (защита от 914)")
            else:
                log.info(f"[EditConfig] {dev_name}: воркер остановлен для валидации")

        if not _in_pool_validate or _worker_stopped_for_validate:
            log.info(f"[EditConfig] {dev_name}: валидация {new_ip}:{new_ver}...")
            ok, err = _validate_device(dev["id"], new_ip, new_key, new_ver, VALIDATE_TIMEOUT)

            if not ok:
                log.warning(f"[EditConfig] {dev_name}: валидация не прошла: {err}")
                # Воркер уже остановлен — поднимаем обратно со старым конфигом.
                if _worker_stopped_for_validate:
                    _ensure_worker_running(dev, "после провала валидации")
                _publish_edit_result(dev_name, False, f"validation failed: {err}",
                                     request_id=request_id)
                return

            log.info(f"[EditConfig] {dev_name}: валидация OK")

    # --- Чтение config ---
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_devices = json.load(f)
    except Exception as e:
        _publish_edit_result(dev_name, False, f"read config failed: {e}",
                             request_id=request_id)
        return

    # --- Захват старого dps_map для diff (до обновления) ---
    old_dps_map = dict(dev.get("dps_map", {})) if "dps_map" in filtered else {}

    # v1.10.10: снимок старого battery_powered ДО update.
    _old_battery = dev.get("battery_powered", False)

    # --- Обновление в файле ---
    found = False
    for d in all_devices:
        if d.get("name") == dev_name:
            d.update(filtered)
            found = True
            break

    if not found:
        _publish_edit_result(dev_name, False, "device not found in file",
                             request_id=request_id)
        return

    _backup_config()

    if not _write_config_atomic(all_devices):
        _publish_edit_result(dev_name, False, "write config failed",
                             request_id=request_id)
        return

    log.info(f"[EditConfig] {dev_name}: конфиг обновлён ({list(filtered.keys())})")

    # --- Обновление in-memory ---
    with DEVICES_LOCK:
        dev.update(filtered)
        # v1.8.6: синхронизируем ALL_DEVICES тем же полем enabled,
        # чтобы in-memory состояние не расходилось с файлом.
        # (ALL_DEVICES — снимок файла на старте; если поменяем только
        # файл, при следующем edit_config другого поля мы бы видели
        # устаревший enabled.)
        if "enabled" in filtered:
            for _d in ALL_DEVICES:
                if _d.get("name") == dev_name:
                    _d["enabled"] = filtered["enabled"]
                    break

    # --- Обработка enabled (v1.8.6) ---
    # v1.10.3: снимок DEVICE_INDEX ДО обработки — для корректного решения
    # о restart (см. ниже). Если enabled false->true — воркер уже стартанул.
    with DEVICES_LOCK:
        _DEVICE_INDEX_BEFORE_ENABLE = set(DEVICE_INDEX.keys())
    if "enabled" in filtered:
        _new_enabled = filtered["enabled"]   # v1.10.3: валидировано как bool
        with DEVICES_LOCK:
            _in_pool = dev_name in DEVICE_INDEX

        if not _new_enabled and _in_pool:
            # ===== true → false =====
            log.info(f"[EditConfig] {dev_name}: disable (soft={not ENABLED_FALSE_REMOVES_DISCOVERY})")
            with DEVICES_LOCK:
                _d = DEVICE_INDEX.pop(dev_name, None)
                if _d is not None:
                    try:
                        DEVICES.remove(_d)
                    except ValueError:
                        pass
            drop_device_conn(dev_name)
            # Воркер увидит, что device нет в DEVICE_INDEX, и выйдет сам.
            # На всякий случай шлём restart — если он сейчас в receive().
            request_worker_restart(dev_name)

            if ENABLED_FALSE_REMOVES_DISCOVERY:
                # Жёсткое удаление: HA потеряет сущности.
                try:
                    _remove_discovery_for_device(dev)
                except Exception as e:
                    log.warning(f"[EditConfig] {dev_name}: discovery cleanup: {e}")
                with STATE_LOCK:
                    STATE_CACHE.pop(dev_name, None)
                refresh_our_ids()
            else:
                # Мягкое отключение: сущности остаются в HA (unavailable),
                # unique_id/entity_id/автоматизации не ломаются.
                try:
                    publish_availability(dev, False)
                except Exception as e:
                    log.warning(f"[EditConfig] {dev_name}: availability offline: {e}")
                # OUR_IDS содержит этого устройства (идёт по ALL_DEVICES),
                # поэтому refresh_our_ids() не нужен и не опасен.

        elif _new_enabled and not _in_pool:
            # ===== false → true =====
            log.info(f"[EditConfig] {dev_name}: enable — публикуем Discovery, запускаем воркер")
            with DEVICES_LOCK:
                DEVICES.append(dev)
                DEVICE_INDEX[dev_name] = dev
            with STATE_LOCK:
                STATE_CACHE.setdefault(dev_name, {})

            try:
                publish_discovery(dev)
            except Exception as e:
                log.warning(f"[EditConfig] {dev_name}: publish_discovery: {e}")
            try:
                publish_availability(dev, True)
            except Exception as e:
                log.warning(f"[EditConfig] {dev_name}: availability online: {e}")

            # Публикуем кэш — если до отключения были значения, HA их подхватит.
            with STATE_LOCK:
                _cached = dict(STATE_CACHE.get(dev_name, {}))
            if _cached:
                try:
                    publish_state(dev, _cached)
                except Exception as e:
                    log.warning(f"[EditConfig] {dev_name}: publish_state: {e}")

            # Запускаем воркер (батарейный или polling).
            # v1.9.2: для батарейных — run_battery_listener (ping-триггер),
            # не run_battery_device (старый polling, не работает для device22).
            # v1.10.15: сначала гарантированно дожидаемся выхода старого
            # воркера — иначе два воркера держат два persistent-сокета (914).
            try:
                if _worker_alive(dev_name):
                    log.info(f"[EditConfig] {dev_name}: остаток старого воркера, "
                             f"останавливаю")
                    request_worker_restart(dev_name)
                    _wait_worker_exit(dev_name, timeout=3.0)
                _ensure_worker_running(dev, "enable false→true")
            except Exception as e:
                log.warning(f"[EditConfig] {dev_name}: worker start: {e}")

            refresh_our_ids()

        else:
            # enabled не менялось (или повторная установка того же значения).
            # Просто пишем в лог, ничего не делаем — воркер уже работает
            # или уже не работает корректно.
            log.debug(f"[EditConfig] {dev_name}: enabled={_new_enabled}, in_pool={_in_pool} — no change")

    # --- Переключение воркера при смене battery_powered (v1.9.14) ---
    if "battery_powered" in filtered:
        _new_battery = filtered["battery_powered"]   # v1.10.3: валидировано как bool
        # v1.10.10: если реально изменилось — перепубликовать Discovery
        # (battery_alert/last_seen появятся/исчезнут в HA), плюс
        # publish_discovery если включено.
        if _new_battery != _old_battery:
            try:
                publish_discovery(dev)
                log.info(f"[EditConfig] {dev_name}: battery_powered "
                         f"{_old_battery}->{_new_battery} — Discovery перепубликован")
            except Exception as e:
                log.warning(f"[EditConfig] {dev_name}: publish_discovery "
                            f"after battery change: {e}")
            # v1.10.11: состав батарейных изменился — пересчитать ping_mode.
            _publish_ping_mode()
        # dev уже содержит новое значение (dev.update(filtered) выше)
        with DEVICES_LOCK:
            _in_pool = dev_name in DEVICE_INDEX
        if _in_pool:
            log.info(f"[EditConfig] {dev_name}: battery_powered → {_new_battery}, "
                     f"переключаю воркер")
            drop_device_conn(dev_name)
            request_worker_restart(dev_name)  # старый воркер увидит и выйдет
            # Ждём выхода старого воркера
            if _wait_worker_exit(dev_name, timeout=3.0):
                _target = run_battery_listener if _new_battery else run_polling_device
                _t = threading.Thread(target=_target, args=(dev,),
                                      daemon=True, name=f"worker-{dev_name}")
                _t.start()
                log.info(f"[EditConfig] {dev_name}: воркер перезапущен "
                         f"({'battery' if _new_battery else 'polling'})")
            else:
                log.warning(f"[EditConfig] {dev_name}: старый воркер не завершился "
                            f"за 3с, новый НЕ запущен")
        # Убираем старый request_worker_restart ниже, чтобы не дёргать
        # уже перезапущенный воркер.
        _skip_final_restart = True
    else:
        _skip_final_restart = False

    # --- Discovery diff (v1.8.5) ---
    if "dps_map" in filtered:
        new_dps_map = filtered["dps_map"]
        removed, added, changed = _diff_dps_maps(old_dps_map, new_dps_map)

        if removed or added or changed:
            log.info(f"[EditConfig] {dev_name}: dps_map "
                     f"removed={len(removed)} added={len(added)} changed={len(changed)}")

            # Удалённые DP — чистим Discovery + state + STATE_CACHE
            for dp_str in removed:
                old_info = old_dps_map.get(dp_str, {})
                try:
                    _remove_discovery_and_state(dev, dp_str, old_info)
                except Exception as e:
                    log.warning(f"[EditConfig] {dev_name}: cleanup dp={dp_str}: {e}")
                with STATE_LOCK:
                    cache = STATE_CACHE.get(dev_name, {})
                    cache.pop(dp_str, None)

            # v1.10.1: изменённые DP — если name/component изменился,
            # чистим СТАРЫЙ Discovery перед публикацией нового.
            # Иначе старый retained остаётся в MQTT → HA создаёт дубликат
            # сущности. sync_discovery_registry почистит только при
            # следующем рестарте bridge, а при двойном переименовании
            # без рестарта — не почистит вообще.
            for dp_str in changed:
                old_info = old_dps_map.get(dp_str, {})
                new_info = new_dps_map.get(dp_str, {})
                old_name = old_info.get("name", f"dp_{dp_str}")
                new_name = new_info.get("name", f"dp_{dp_str}")
                old_comp = old_info.get("component")
                new_comp = new_info.get("component")

                if old_name != new_name or old_comp != new_comp:
                    log.info(
                        f"[EditConfig] {dev_name}: DP {dp_str} "
                        f"'{old_name}'({old_comp}) → "
                        f"'{new_name}'({new_comp}) — чистка старого Discovery"
                    )
                    try:
                        _remove_discovery_and_state(dev, dp_str, old_info)
                    except Exception as e:
                        log.warning(
                            f"[EditConfig] {dev_name}: cleanup changed "
                            f"dp={dp_str}: {e}"
                        )

            # Перепубликация Discovery — idempotent, покрывает added + changed
            try:
                publish_discovery(dev)
            except Exception as e:
                log.warning(f"[EditConfig] {dev_name}: publish_discovery: {e}")

            # v1.10.14: для light — принудительно перепубликовать
            # combined state. Иначе retained tuya/light/<name>/state
            # держит stale JSON (с полем удалённого DP) до след. опроса.
            if dev.get("type") == "light":
                with STATE_LOCK:
                    _light_cached = dict(STATE_CACHE.get(dev_name, {}))
                if _light_cached:
                    try:
                        publish_state(dev, _light_cached)
                    except Exception as e:
                        log.warning(f"[EditConfig] {dev_name}: light state "
                                    f"republish: {e}")
                else:
                    mqtt_client.publish(
                        f"{TOPIC_PREFIX}/light/{dev_name}/state",
                        payload=None, qos=1, retain=True,
                    )

            # Свежие значения для добавленных DP
            if added:
                with STATE_LOCK:
                    cached = dict(STATE_CACHE.get(dev_name, {}))
                added_state = {dp: cached[dp] for dp in added if dp in cached}
                if added_state:
                    try:
                        publish_state(dev, added_state)
                    except Exception as e:
                        log.warning(f"[EditConfig] {dev_name}: publish_state: {e}")

            # Cleanup Discovery должен знать про новые unique_id
            refresh_our_ids()

    # --- Перезапуск воркера ---
    # v1.9.2b: restart_flag нужен только если воркер УЖЕ работал
    # (ip/key/version/dps_map изменились на живом устройстве).
    # При enable=false→true воркер только что запущен — restart
    # заставит его переподключиться, это лишний шум в логах.
    drop_device_conn(dev_name)
    # v1.10.3: restart подавляем ТОЛЬКО при реальном enabled false->true.
    # Иначе (enabled уже был True) рестарт нужен — ip/key/dps_map могли
    # измениться.
    _enabled_turned_on = (
        "enabled" in filtered
        and filtered["enabled"] is True
        and dev_name not in _DEVICE_INDEX_BEFORE_ENABLE
    )
    if not _enabled_turned_on and not _skip_final_restart:
        request_worker_restart(dev_name)

    # v1.10.11: enabled мог измениться (false→true) — состав DEVICES
    # изменился, ping_mode пересчитываем.
    if "enabled" in filtered:
        _publish_ping_mode()

    # v1.10.15: если воркер останавливали ради TCP-валидации — поднять заново
    # (после остановки restart-флаг выше уже некому обработать).
    if _worker_stopped_for_validate:
        _ensure_worker_running(dev, "после edit_config (validate)")

    _publish_edit_result(dev_name, True, None, changes=filtered,
                         request_id=request_id, warnings=warnings)


def _publish_edit_result(dev_name, ok, error, changes=None, request_id=None,
                         warnings=None):
    payload = {
        "request_id": request_id,
        "device": dev_name,
        "ok": bool(ok),
        "error": error,
        "changes": changes or {},
        "warnings": warnings or [],
        "ts": int(time.time()),
    }
    try:
        mqtt_client.publish(
            f"{TOPIC_PREFIX}/bridge/edit_config_result",
            json.dumps(payload, ensure_ascii=False),
            qos=1, retain=False,
        )
    except Exception as e:
        log.warning(f"[EditConfig] publish result failed: {e}")


# ==================== DELETE DEVICE ====================
def _handle_delete_device(payload_str):
    # v1.8.3: request_id доступен всегда.
    req_id = _extract_request_id(payload_str)

    if not ALLOW_CONFIG_EDIT:
        _publish_delete_result(None, False, "config edit disabled",
                               request_id=req_id)
        return

    try:
        data = json.loads(payload_str)
    except (json.JSONDecodeError, ValueError):
        _publish_delete_result(None, False, "invalid json",
                               request_id=req_id)
        return

    if not isinstance(data, dict):
        _publish_delete_result(None, False, "payload must be json object",
                               request_id=req_id)
        return
    request_id = data.get("request_id") or req_id
    dev_name = data.get("device")

    # v1.10.10: dev_name должен быть str — иначе DEVICE_INDEX.pop() / .get()
    # бросает TypeError (unhashable list/dict). Строгая проверка.
    if not dev_name or not isinstance(dev_name, str):
        _publish_delete_result(None, False, "device required (string)",
                               request_id=request_id)
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_devices = json.load(f)
    except Exception as e:
        # v1.8.4: req_id, а не None — WebUI сматчит ответ и не словит timeout.
        _publish_delete_result(request_id=req_id, ok=False, error=f"read config failed: {e}")
        return

    # v1.10.9: берём полную запись из all_devices ДО фильтрации.
    # Раньше cleanup зависел от DEVICE_INDEX.pop() — если индекс не
    # содержал устройство (гонка, импорт в обход), retained в MQTT
    # оставался навсегда. См. шапку модуля.
    dev_to_remove = None
    for d in all_devices:
        if d.get("name") == dev_name:
            dev_to_remove = d
            break
    if dev_to_remove is None:
        _publish_delete_result(request_id, False, "device not found")
        return

    before = len(all_devices)
    all_devices = [d for d in all_devices if d.get("name") != dev_name]

    _backup_config()

    if not _write_config_atomic(all_devices):
        _publish_delete_result(request_id, False, "write config failed")
        return

    # In-memory cleanup: DEVICE_INDEX / DEVICES (независимо от cleanup).
    with DEVICES_LOCK:
        DEVICE_INDEX.pop(dev_name, None)
        # v1.10.15: удаляем по имени, а не по равенству dict. publish_discovery
        # мутирует in-memory dict (friendly_name/name), поэтому dev_to_remove
        # (из файла) уже не равен ему — remove() падал, призрак оставался.
        DEVICES[:] = [d for d in DEVICES if d.get("name") != dev_name]

    # v1.10.10: сначала останавливаем воркер, только потом cleanup.
    # Иначе гонка: воркер в publish_state/publish_cache_snapshot
    # возвращает retained уже после _remove_discovery_for_device.
    drop_device_conn(dev_name)
    request_worker_restart(dev_name)
    _wait_worker_exit(dev_name, timeout=3.0)

    # cleanup всегда — по dev_to_remove из конфига.
    try:
        _remove_discovery_for_device(dev_to_remove)
    except Exception as e:
        log.warning(f"[Delete] discovery cleanup {dev_name}: {e}")

    with STATE_LOCK:
        STATE_CACHE.pop(dev_name, None)
    remove_battery_last_up(dev_name)   # v1.10.0

    log.info(f"[Delete] {dev_name}: удалено из конфига")

    # v1.10.11: состав батарейных изменился — пересчитать ping_mode.
    _publish_ping_mode()

    # v1.10.8: перечитываем ALL_DEVICES — удалённое устройство
    # не должно висеть в snapshot'е (иначе _handle_edit_config
    # может найти «удалённое» и попытаться его включить).
    _reload_all_devices()

    refresh_our_ids()  # v1.8.4 delete
    _publish_delete_result(request_id, True, None, device=dev_name)


def _publish_delete_result(request_id, ok, error, device=None):
    payload = {
        "request_id": request_id,
        "ok": bool(ok),
        "error": error,
        "device": device,
        "ts": int(time.time()),
    }
    try:
        mqtt_client.publish(
            f"{TOPIC_PREFIX}/bridge/delete_device_result",
            json.dumps(payload, ensure_ascii=False),
            qos=1, retain=False,
        )
    except Exception as e:
        log.warning(f"[Delete] publish result failed: {e}")


def _remove_discovery_for_device(dev):
    """v1.10.7: полная чистка retained Discovery + state для устройства.

    Раньше чистились только Discovery-configs и несколько общих state-топиков,
    а per-DP retained state (switch/select/number/sensor/binary_sensor,
    phase_a) оставался в MQTT навсегда → HA мог показывать stale values
    или «restored» сущности.
    """
    dev_name = dev["name"]
    dtype = dev.get("type", "sensor")
    dps_map = dev.get("dps_map", {})

    discovery_topics = []
    state_topics = []

    # v1.10.10: battery_alert / battery_last_seen — Discovery-configs
    # тоже нужно чистить при удалении батарейного. Раньше чистились
    # только state-топики; retained homeassistant/sensor/<dev>_battery_*
    # висели до orphan cleanup при следующем рестарте.
    if dev.get("battery_powered"):
        discovery_topics.append(f"{DISCOVERY_PREFIX}/sensor/{dev_name}_battery_alert/config")
        discovery_topics.append(f"{DISCOVERY_PREFIX}/sensor/{dev_name}_battery_last_seen/config")

    if dtype == "light":
        discovery_topics.append(f"{DISCOVERY_PREFIX}/light/{dev_name}_light/config")
        state_topics.append(f"{TOPIC_PREFIX}/light/{dev_name}/state")
    elif dtype == "climate":
        discovery_topics.append(f"{DISCOVERY_PREFIX}/climate/{dev_name}_climate/config")
        state_topics.extend([
            f"{TOPIC_PREFIX}/climate/{dev_name}/mode/state",
            f"{TOPIC_PREFIX}/climate/{dev_name}/temp/state",
            f"{TOPIC_PREFIX}/climate/{dev_name}/current/state",
            f"{TOPIC_PREFIX}/climate/{dev_name}/preset/state",
        ])
    # v1.10.20
    elif dtype == "cover":
        discovery_topics.append(f"{DISCOVERY_PREFIX}/cover/{dev_name}_cover/config")
        state_topics.extend([
            f"{TOPIC_PREFIX}/cover/{dev_name}/state",
            f"{TOPIC_PREFIX}/cover/{dev_name}/position/state",
        ])
    elif dtype == "fan":
        discovery_topics.append(f"{DISCOVERY_PREFIX}/fan/{dev_name}_fan/config")
        state_topics.extend([
            f"{TOPIC_PREFIX}/fan/{dev_name}/state",
            f"{TOPIC_PREFIX}/fan/{dev_name}/preset/state",
            f"{TOPIC_PREFIX}/fan/{dev_name}/speed/state",
            f"{TOPIC_PREFIX}/fan/{dev_name}/direction/state",
        ])

    for dp_str, info in dps_map.items():
        comp = info.get("component")
        ent = info.get("name", f"dp_{dp_str}")
        if comp == "switch":
            discovery_topics.append(f"{DISCOVERY_PREFIX}/switch/{dev_name}_{ent}/config")
            state_topics.append(f"{TOPIC_PREFIX}/switch/{dev_name}/{ent}/state")
        elif comp == "select":
            discovery_topics.append(f"{DISCOVERY_PREFIX}/select/{dev_name}_{ent}/config")
            state_topics.append(f"{TOPIC_PREFIX}/select/{dev_name}/{ent}/state")
        elif comp == "number":
            discovery_topics.append(f"{DISCOVERY_PREFIX}/number/{dev_name}_{ent}/config")
            state_topics.append(f"{TOPIC_PREFIX}/number/{dev_name}/{ent}/state")
        elif comp in ("sensor", "binary_sensor"):
            discovery_topics.append(f"{DISCOVERY_PREFIX}/{comp}/{dev_name}_{ent}/config")
            state_topics.append(f"{TOPIC_PREFIX}/{dtype}/{dev_name}/dps/{dp_str}/state")
        elif comp == "lock":           # v1.10.20
            discovery_topics.append(f"{DISCOVERY_PREFIX}/lock/{dev_name}_{ent}/config")
            state_topics.append(f"{TOPIC_PREFIX}/lock/{dev_name}/{ent}/state")

    if dps_map.get("6", {}).get("component") == "phase_a":
        for suffix in ("voltage", "current", "power"):
            discovery_topics.append(f"{DISCOVERY_PREFIX}/sensor/{dev_name}_output_{suffix}/config")
            state_topics.append(f"{TOPIC_PREFIX}/{dtype}/{dev_name}/phase_a/{suffix}/state")

    # Общие топики устройства.
    state_topics.append(f"{TOPIC_PREFIX}/{dev_name}/status")
    state_topics.append(f"{TOPIC_PREFIX}/{dev_name}/last_seen")
    state_topics.append(f"{TOPIC_PREFIX}/{dev_name}/cache_snapshot")
    # v1.9.14: battery_alert / battery_last_up — иначе retained остаётся
    # навсегда и подхватывается при повторном создании устройства.
    state_topics.append(f"{TOPIC_PREFIX}/{dev_name}/battery_alert")
    state_topics.append(f"{TOPIC_PREFIX}/{dev_name}/battery_last_up")

    for t in discovery_topics:
        mqtt_client.publish(t, payload=None, qos=1, retain=True)
    for t in state_topics:
        mqtt_client.publish(t, payload=None, qos=1, retain=True)


# ==================== IMPORT DEVICES ====================
def _handle_import_devices(payload_str):
    # v1.8.3: request_id доступен всегда.
    req_id = _extract_request_id(payload_str)

    if not ALLOW_CONFIG_EDIT:
        _publish_import_result(None, False, "config edit disabled",
                               request_id=req_id)
        return

    try:
        data = json.loads(payload_str)
    except (json.JSONDecodeError, ValueError):
        _publish_import_result(None, False, "invalid json",
                               request_id=req_id)
        return
    if not isinstance(data, dict):
        _publish_import_result(None, False, "payload must be json object",
                               request_id=req_id)
        return

    request_id = data.get("request_id") or req_id
    new_devices = data.get("devices", [])
    overwrite = bool(data.get("overwrite", False))

    if not isinstance(new_devices, list) or not new_devices:
        _publish_import_result(request_id, False, "devices list required")
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_devices = json.load(f)
    except Exception as e:
        # v1.8.4: req_id, а не None.
        _publish_import_result(request_id=req_id, ok=False, error=f"read config failed: {e}")
        return

    existing_names = {d.get("name") for d in all_devices}
    existing_ids = {d.get("id") for d in all_devices}
    # v1.9.10: IP-конфликты — два устройства не могут опрашивать один IP.
    existing_ips = {d.get("ip") for d in all_devices if d.get("ip")}

    added = 0
    updated = 0
    skipped = 0
    errors = []
    warnings = []   # v1.10.12: dps_map warning (не блокирует import)

    for dev in new_devices:
        if not isinstance(dev, dict):
            skipped += 1
            continue

        # v1.10.10: строгие isinstance-проверки ДО .strip().
        # Раньше int/None/список в name/ip/key → AttributeError
        # в тихом import-треде.
        _name_raw = dev.get("name", "")
        _id_raw = dev.get("id", "")
        _ip_raw = dev.get("ip", "")
        _key_raw = dev.get("local_key", "")
        _type_raw = dev.get("type", "")
        if not isinstance(_name_raw, str) or not isinstance(_id_raw, str) \
                or not isinstance(_ip_raw, str) or not isinstance(_key_raw, str) \
                or not isinstance(_type_raw, str):
            errors.append(f"{dev.get('name', '?')}: name/id/ip/local_key/type must be string")
            skipped += 1
            continue
        name = _name_raw.strip()
        dev_id = _id_raw.strip()
        dev_ip = _ip_raw.strip()
        dev_key = _key_raw.strip()
        dev_ver = str(dev.get("version", "")).strip()
        dev_type = _type_raw.strip()

        if not _is_valid_name(name):
            errors.append(f"{name or '?'}: invalid name")
            skipped += 1
            continue
        if not dev_id or len(dev_id) < 10:
            errors.append(f"{name}: invalid id")
            skipped += 1
            continue
        if not _is_valid_ip(dev_ip):
            errors.append(f"{name}: invalid ip {dev_ip}")
            skipped += 1
            continue
        # v1.9.10: IP-конфликт с другим устройством (кроме себя при overwrite)
        # v1.10.3: all_devices — локальный снимок файла, DEVICES_LOCK не нужен.
        if dev_ip in existing_ips:
            _conflict = None
            for _d in all_devices:
                if _d.get("ip") == dev_ip and _d.get("name") != name:
                    _conflict = _d.get("name")
                    break
            if _conflict:
                errors.append(f"{name}: IP {dev_ip} уже занят устройством '{_conflict}'")
                skipped += 1
                continue
        if not _is_valid_key(dev_key):
            errors.append(f"{name}: invalid local_key")
            skipped += 1
            continue
        if not _is_valid_version(dev_ver):
            errors.append(f"{name}: invalid version {dev_ver}")
            skipped += 1
            continue
        if not _is_valid_type(dev_type):
            errors.append(f"{name}: invalid type {dev_type}")
            skipped += 1
            continue

        # v1.10.3: строгая валидация bool (bool("false") == True).
        _bp = dev.get("battery_powered", False)
        if not isinstance(_bp, bool):
            errors.append(f"{name}: battery_powered must be bool")
            skipped += 1
            continue
        _en = dev.get("enabled", True)
        if not isinstance(_en, bool):
            errors.append(f"{name}: enabled must be bool")
            skipped += 1
            continue

        # v1.10.11: валидация dps_map при import. Ранее пропускалась —
        # невалидный dps_map из Cloud/WebUI попадал прямо в config,
        # и ошибка всплывала только при publish_discovery.
        # v1.10.12: warning вместо reject — ручные импорты в обход
        # WebUI могут прислать dps_map без component/device_class.
        # Если validate_dps_map: false в payload — пропускаем проверку.
        # v1.10.13: даже если dps_map невалиден — фильтруем info,
        # оставляем только dict. Иначе bridge упадёт при следующем
        # publish_discovery в main() (AttributeError: 'str' object
        # has no attribute 'get').
        _import_dps_map = dev.get("dps_map", {})
        _validate_dps = data.get("validate_dps_map", True)
        if _import_dps_map:
            if _validate_dps:
                _ok_dps, _err_dps, _warn_dps = _validate_dps_map(dev_type, _import_dps_map)
                if not _ok_dps:
                    warnings.append(f"{name}: dps_map warning: {_err_dps}")
                    log.warning(f"[Import] {name}: dps_map невалиден: {_err_dps}")
            # Нормализация ключей + фильтр info.
            _import_dps_map = {
                str(k): v for k, v in _import_dps_map.items()
                if isinstance(v, dict)
            }
        entry = {
            "id": dev_id,
            "name": name,
            "friendly_name": (dev.get("friendly_name") or name),
            "ip": dev_ip,
            "local_key": dev_key,
            "version": dev_ver,
            "type": dev_type,
            "model": dev.get("model", ""),
            "battery_powered": _bp,
            "enabled": _en,
            "dps_map": _import_dps_map,
        }

        # v1.10.7: сохраняем Cloud-метаданные устройства, если WebUI их
        # прислал. Используются в _rebuild_tinytuya_json_worker для
        # lookup_tuya_local(product_id) и категорийной классификации.
        for k in ("presets", "preset_map", "min_temp", "max_temp",
                  "temp_step", "expire_after",
                  "product_id", "category", "product_name"):
            if k in dev:
                entry[k] = dev[k]

        if name in existing_names:
            if overwrite:
                for i, d in enumerate(all_devices):
                    if d.get("name") == name:
                        # v1.10.3: обновляем existing_ids/existing_ips —
                        # иначе повторный id/ip в том же payload даст
                        # ложный конфликт.
                        _old_id = d.get("id")
                        _old_ip = d.get("ip")
                        all_devices[i] = entry
                        updated += 1
                        if _old_id and _old_id != dev_id:
                            existing_ids.discard(_old_id)
                        existing_ids.add(dev_id)
                        if _old_ip and _old_ip != dev_ip:
                            existing_ips.discard(_old_ip)
                        existing_ips.add(dev_ip)
                        break
            else:
                errors.append(f"{name}: already exists (skip)")
                skipped += 1
                continue
        elif dev_id in existing_ids:
            errors.append(f"{name}: id conflicts with existing device")
            skipped += 1
            continue
        else:
            all_devices.append(entry)
            existing_names.add(name)
            existing_ids.add(dev_id)
            existing_ips.add(dev_ip)
            added += 1

    if added == 0 and updated == 0:
        # v1.10.13: проброс warnings — раньше при полном отказе
        # (added=updated=0) warnings терялись.
        _publish_import_result(request_id, False, "nothing to import",
                               errors=errors, added=0, updated=0, skipped=skipped,
                               warnings=warnings)
        return

    _backup_config()

    if not _write_config_atomic(all_devices):
        _publish_import_result(request_id, False, "write config failed")
        return

    log.info(f"[Import] added={added}, updated={updated}, skipped={skipped}")

    # v1.10.8: перечитываем ALL_DEVICES — новое устройство должно быть
    # видно в _handle_edit_config (enabled false→true). Раньше новое
    # устройство попадало только в DEVICES/DEVICE_INDEX, а ALL_DEVICES
    # (snapshot на старте) его не содержал.
    _reload_all_devices()

    refresh_our_ids()  # v1.8.4 import

    # v1.10.11: могли добавиться батарейные — пересчитать ping_mode.
    _publish_ping_mode()

    for dev in new_devices:
        # v1.10.15: new_devices — из внешнего JSON, элемент может быть не-dict
        # (первый цикл такое отбрасывает, но список не мутирует). Без проверки
        # AttributeError убивает поток, и import_devices_result не публикуется.
        if not isinstance(dev, dict):
            continue
        _name = dev.get("name", "")
        if not isinstance(_name, str):
            continue
        name = _name.strip()
        if not name:
            continue
        matched = None
        for d in all_devices:
            if d.get("name") == name:
                matched = d
                break
        if not matched:
            continue
        with DEVICES_LOCK:
            _existing = DEVICE_INDEX.get(name)
        if _existing is not None:
            # --- Устройство уже активно: overwrite ---
            # v1.10.15: overwrite мог сменить enabled/battery_powered —
            # обрабатываем переходы воркеров. Раньше при enabled:false
            # устройство продолжало опрашиваться, а при смене
            # battery_powered — оставалось с неверным типом воркера.
            _old_battery = bool(_existing.get("battery_powered", False))
            _new_battery = bool(matched.get("battery_powered", False))
            _new_enabled = bool(matched.get("enabled", True))
            with DEVICES_LOCK:
                _existing.update(matched)
            if not _new_enabled:
                with DEVICES_LOCK:
                    DEVICE_INDEX.pop(name, None)
                    DEVICES[:] = [d for d in DEVICES if d.get("name") != name]
                drop_device_conn(name)
                request_worker_restart(name)
                _wait_worker_exit(name, timeout=3.0)
                try:
                    publish_availability(_existing, False)
                except Exception as e:
                    log.warning(f"[Import] {name}: availability offline: {e}")
                continue
            drop_device_conn(name)
            if _new_battery != _old_battery:
                request_worker_restart(name)
                if _wait_worker_exit(name, timeout=3.0):
                    _ensure_worker_running(matched, "import: смена типа воркера")
                else:
                    log.warning(f"[Import] {name}: воркер не завершился за 3с")
            else:
                request_worker_restart(name)
            # v1.10.10: при overwrite перепубликовать Discovery —
            # иначе HA не увидит изменения dps_map/friendly_name.
            try:
                publish_discovery(matched)
            except Exception as e:
                log.warning(f"[Import] {name}: publish_discovery on update: {e}")
        elif matched.get("enabled", True):
            # --- Новое устройство ---
            with DEVICES_LOCK:
                DEVICES.append(matched)
                DEVICE_INDEX[name] = matched
            with STATE_LOCK:
                STATE_CACHE.setdefault(name, {})
            try:
                publish_discovery(matched)
            except Exception as e:
                log.warning(f"[Import] {name}: publish_discovery: {e}")
            _ensure_worker_running(matched, "import: новое устройство")

    _publish_import_result(request_id, True, None, errors=errors,
                           added=added, updated=updated, skipped=skipped,
                           warnings=warnings)


def _publish_import_result(request_id, ok, error, errors=None, added=0, updated=0, skipped=0,
                            warnings=None):
    payload = {
        "request_id": request_id,
        "ok": bool(ok),
        "error": error,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "errors": errors or [],
        "warnings": warnings or [],
        "ts": int(time.time()),
    }
    try:
        mqtt_client.publish(
            f"{TOPIC_PREFIX}/bridge/import_devices_result",
            json.dumps(payload, ensure_ascii=False),
            qos=1, retain=False,
        )
    except Exception as e:
        log.warning(f"[Import] publish result failed: {e}")


# ==================== SCAN NETWORK ====================
def _handle_scan_network(payload_str):
    # v1.8.4: request_id доступен всегда — даже при invalid json.
    req_id = _extract_request_id(payload_str)
    try:
        data = json.loads(payload_str)
    except (json.JSONDecodeError, ValueError):
        _publish_scan_result(req_id, False, "invalid json")
        return
    if not isinstance(data, dict):
        _publish_scan_result(req_id, False, "payload must be json object")
        return

    request_id = data.get("request_id") or req_id
    # v1.10.10: subnet должен быть str — иначе .strip() бросает AttributeError.
    subnet = data.get("subnet", "")
    if not isinstance(subnet, str):
        _publish_scan_result(request_id, False, "subnet must be string")
        return
    subnet = subnet.strip()

    if not subnet:
        with DEVICES_LOCK:
            if DEVICES:
                first_ip = DEVICES[0].get("ip", "")
                parts = first_ip.split(".")
                if len(parts) == 4:
                    subnet = ".".join(parts[:3])
    # v1.10.5: строгая валидация — три октета 0..255 без ведущих нулей.
    # Regex гарантирует 0..255 и отсутствие "01"/"001".
    if not subnet or not re.match(
            r"^(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})$",
            subnet):
        _publish_scan_result(request_id, False, f"invalid subnet: {subnet}")
        return

    try:
        hosts = _scan_subnet(subnet)
        _publish_scan_result(request_id, True, None, hosts=hosts, subnet=subnet)
    except Exception as e:
        log.warning(f"[Scan] error: {e}")
        _publish_scan_result(request_id, False, str(e))


def _publish_scan_result(request_id, ok, error, hosts=None, subnet=None):
    payload = {
        "request_id": request_id,
        "ok": bool(ok),
        "error": error,
        "hosts": hosts or [],
        "subnet": subnet,
        "ts": int(time.time()),
    }
    try:
        mqtt_client.publish(
            f"{TOPIC_PREFIX}/bridge/scan_network_result",
            json.dumps(payload, ensure_ascii=False),
            qos=1, retain=False,
        )
    except Exception as e:
        log.warning(f"[Scan] publish result failed: {e}")


# ==================== RELOAD ALL_DEVICES (v1.10.8) ====================
def _reload_all_devices():
    """Перечитать ALL_DEVICES из CONFIG_FILE.

    v1.10.8: нужен после import/delete — иначе ALL_DEVICES (снимок
    на старте) не содержит новые устройства. _handle_edit_config
    ищет устройство в DEVICE_INDEX, затем в ALL_DEVICES — при import
    новое устройство не попадало ни туда, ни туда → "device not found"
    при попытке включить отключённое.
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            fresh = json.load(f)
        with DEVICES_LOCK:
            ALL_DEVICES.clear()
            ALL_DEVICES.extend(fresh)
        log.info(f"[Config] ALL_DEVICES перечитан: {len(fresh)} устройств")
    except Exception as e:
        log.warning(f"[Config] ALL_DEVICES reload: {e}")


# ==================== АТОМАРНАЯ ЗАПИСЬ КОНФИГА ====================
def _write_config_atomic(all_devices):
    try:
        dir_name = os.path.dirname(os.path.abspath(CONFIG_FILE)) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".devices_config_", suffix=".tmp", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(all_devices, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, CONFIG_FILE)
            return True
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        log.error(f"[Config] Запись не удалась: {e}")
        return False


# ==================== DISCOVERY ====================
def base_availability(dev_name):
    return {
        "availability_topic": f"{TOPIC_PREFIX}/{dev_name}/status",
        "payload_available": "online",
        "payload_not_available": "offline",
    }


def publish_discovery(device):
    # v1.10.14: нормализуем friendly_name — иначе KeyError при ручной
    # правке devices_config.json без friendly_name (все подфункции
    # publish_* читают device["friendly_name"] напрямую).
    if not device.get("friendly_name"):
        device["friendly_name"] = (device.get("name")
                                   or device.get("id")
                                   or "?")
    if not device.get("name"):
        device["name"] = device.get("id") or "unknown"
    dev_type = device.get("type", "sensor")
    dev_name = device["name"]
    friendly = device["friendly_name"]
    model = device.get("model", "Tuya Device")

    device_info = {
        "identifiers": [device["id"]],
        "name": friendly,
        "manufacturer": "Tuya",
        "model": model,
    }

    if device.get("dps_map", {}).get("6", {}).get("component") == "phase_a":
        publish_phase_a(device, device_info)

    # v1.9.5b: battery_alert / battery_last_seen для батарейных
    if device.get("battery_powered"):
        try:
            publish_battery_alert_discovery(device, device_info)
        except Exception as e:
            log.warning(f"[Discovery] battery_alert {dev_name}: {e}")

    # v1.10.20: lock — обычный DP-компонент, публикуется для любого типа устройства.
    publish_locks(device, device_info)

    if dev_type == "light":
        publish_light(device, device_info)
    elif dev_type == "switch":
        publish_switches(device, device_info)
        publish_selects(device, device_info)
        publish_numbers(device, device_info)
        publish_sensors(device, device_info)
    elif dev_type == "climate":
        publish_climate(device, device_info)
    elif dev_type == "cover":
        publish_cover(device, device_info)
    elif dev_type == "fan":
        publish_fan(device, device_info)
    else:
        publish_sensors(device, device_info)


def publish_phase_a(device, device_info):
    dev_name = device["name"]
    friendly = device["friendly_name"]
    dev_type = device.get("type", "switch")
    avail = base_availability(dev_name)

    for suffix, name, unit, dclass in [
        ("voltage", "Output Voltage", "V", "voltage"),
        ("current", "Output Current", "A", "current"),
        ("power", "Output Power", "kW", "power"),
    ]:
        unique_id = f"{dev_name}_output_{suffix}"
        topic = f"{DISCOVERY_PREFIX}/sensor/{unique_id}/config"
        config = {
            "name": f"{friendly} {name}",
            "unique_id": unique_id,
            "state_topic": f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/phase_a/{suffix}/state",
            "unit_of_measurement": unit,
            "device_class": dclass,
            "state_class": "measurement",
            "expire_after": AVAILABILITY_EXPIRE,
            "device": device_info,
            **avail,
        }
        mqtt_client.publish(topic, json.dumps(config), retain=True)
        log.info(f"[Discovery] phase_a: {friendly} / {suffix} ({unit})")


def publish_light(device, device_info):
    dev_name = device["name"]
    avail = base_availability(dev_name)

    dp_bright, _ = find_dp_by_name(device, "bright_value")
    dp_temp, info_temp = find_dp_by_name(device, "temp_value")
    dp_color, _ = find_dp_by_name(device, "colour_data")

    has_bright = dp_bright is not None
    has_temp = dp_temp is not None
    has_color = dp_color is not None

    modes = []
    if has_temp:
        modes.append("color_temp")
    if has_color:
        modes.append("rgb")
    if not modes:
        modes.append("brightness")

    unique_id = f"{dev_name}_light"
    config = {
        "name": device["friendly_name"],
        "unique_id": unique_id,
        "command_topic": f"{TOPIC_PREFIX}/light/{dev_name}/set",
        "state_topic": f"{TOPIC_PREFIX}/light/{dev_name}/state",
        "schema": "json",
        "brightness": has_bright,
        "brightness_scale": HA_BRIGHT_MAX,
        "supported_color_modes": modes,
        "optimistic": False,
        "expire_after": AVAILABILITY_EXPIRE,
        "device": device_info,
        **avail,
    }
    if has_temp and info_temp is not None:
        kmin, kmax = get_kelvin_bounds(info_temp)
        config["color_temp_kelvin"] = True
        config["min_kelvin"] = kmin
        config["max_kelvin"] = kmax
    topic = f"{DISCOVERY_PREFIX}/light/{unique_id}/config"
    mqtt_client.publish(topic, json.dumps(config), retain=True)
    log.info(f"[Discovery] light: {device['friendly_name']} ({modes}, scale={HA_BRIGHT_MAX})")


def publish_switches(device, device_info):
    dev_name = device["name"]
    avail = base_availability(dev_name)

    for dp_str, info in device["dps_map"].items():
        if info.get("component") != "switch":
            continue
        entity_name = info["name"]
        unique_id = f"{dev_name}_{entity_name}"
        config = {
            "name": f"{device['friendly_name']} {entity_name}",
            "unique_id": unique_id,
            "command_topic": f"{TOPIC_PREFIX}/switch/{dev_name}/{entity_name}/set",
            "state_topic": f"{TOPIC_PREFIX}/switch/{dev_name}/{entity_name}/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_on": "ON",
            "state_off": "OFF",
            "optimistic": False,
            "expire_after": AVAILABILITY_EXPIRE,
            "device": device_info,
            **avail,
        }
        topic = f"{DISCOVERY_PREFIX}/switch/{unique_id}/config"
        mqtt_client.publish(topic, json.dumps(config), retain=True)
        log.info(f"[Discovery] switch: {device['friendly_name']} / {entity_name}")


def publish_selects(device, device_info):
    dev_name = device["name"]
    avail = base_availability(dev_name)

    for dp_str, info in device["dps_map"].items():
        if info.get("component") != "select":
            continue
        entity_name = info["name"]
        smap = get_select_map(info)

        options = info.get("options", [])
        if smap:
            map_keys = list(smap.keys())
            for o in options:
                if o not in map_keys:
                    map_keys.append(o)
            options = map_keys

        unique_id = f"{dev_name}_{entity_name}"
        config = {
            "name": f"{device['friendly_name']} {entity_name}",
            "unique_id": unique_id,
            "command_topic": f"{TOPIC_PREFIX}/select/{dev_name}/{entity_name}/set",
            "state_topic": f"{TOPIC_PREFIX}/select/{dev_name}/{entity_name}/state",
            "options": options,
            "optimistic": False,
            "expire_after": AVAILABILITY_EXPIRE,
            "device": device_info,
            **avail,
        }
        topic = f"{DISCOVERY_PREFIX}/select/{unique_id}/config"
        mqtt_client.publish(topic, json.dumps(config), retain=True)
        log.info(f"[Discovery] select: {device['friendly_name']} / {entity_name} ({options})")


def publish_numbers(device, device_info):
    dev_name = device["name"]
    avail = base_availability(dev_name)

    for dp_str, info in device["dps_map"].items():
        if info.get("component") != "number":
            continue
        entity_name = info["name"]
        unique_id = f"{dev_name}_{entity_name}"
        config = {
            "name": f"{device['friendly_name']} {entity_name}",
            "unique_id": unique_id,
            "command_topic": f"{TOPIC_PREFIX}/number/{dev_name}/{entity_name}/set",
            "state_topic": f"{TOPIC_PREFIX}/number/{dev_name}/{entity_name}/state",
            "min": info.get("min", 0),
            "max": info.get("max", 100),
            "step": info.get("step", 1),
            "mode": "box",
            "optimistic": False,
            "expire_after": AVAILABILITY_EXPIRE,
            "device": device_info,
            **avail,
        }
        if info.get("unit"):
            config["unit_of_measurement"] = info["unit"]
        topic = f"{DISCOVERY_PREFIX}/number/{unique_id}/config"
        mqtt_client.publish(topic, json.dumps(config), retain=True)
        log.info(f"[Discovery] number: {device['friendly_name']} / {entity_name}")


def publish_cover(device, device_info):
    """v1.10.20: cover — шторы / рольставни / ворота (HA platform 'cover').

    DP берутся из dps_map по именам (COVER_DP_NAMES):
      control         — open / stop / close (команда)
      percent_control — целевое положение 0..100 (команда)
      percent_state   — текущее положение 0..100 (состояние)

    Нужен хотя бы один из control / percent_control, иначе управление
    из HA будет недоступно (только состояние).
    """
    dev_name = device["name"]
    avail = base_availability(dev_name)
    dps_map = device["dps_map"]

    has_control = any(i.get("name") == "control" for i in dps_map.values())
    has_pos = any(i.get("name") in ("percent_control", "percent_state")
                  for i in dps_map.values())

    # device_class — из DP (необязательно), по умолчанию curtain
    device_class = "curtain"
    for i in dps_map.values():
        if i.get("name") == "control" and i.get("device_class"):
            device_class = i["device_class"]
            break

    config = {
        "name": device["friendly_name"],
        "unique_id": f"{dev_name}_cover",
        "device_class": device_class,
        "optimistic": False,
        "expire_after": AVAILABILITY_EXPIRE,
        "device": device_info,
        **avail,
    }
    if has_control:
        config["command_topic"] = f"{TOPIC_PREFIX}/cover/{dev_name}/set"
        config["payload_open"] = "OPEN"
        config["payload_close"] = "CLOSE"
        config["payload_stop"] = "STOP"
    if has_pos:
        config["position_topic"] = f"{TOPIC_PREFIX}/cover/{dev_name}/position/state"
        config["set_position_topic"] = f"{TOPIC_PREFIX}/cover/{dev_name}/position/set"
        config["position_open"] = 100
        config["position_closed"] = 0

    if not (has_control or has_pos):
        log.warning(f"[Discovery] cover {dev_name}: нет DP control/percent_* — "
                    f"сущность без управления")

    topic = f"{DISCOVERY_PREFIX}/cover/{dev_name}_cover/config"
    mqtt_client.publish(topic, json.dumps(config), retain=True)
    log.info(f"[Discovery] cover: {device['friendly_name']} ({device_class})")


def publish_fan(device, device_info):
    """v1.10.20: fan — вентиляторы (HA platform 'fan').

    DP берутся из dps_map по именам (FAN_DP_NAMES):
      switch        — вкл/выкл (command/state ON|OFF)
      fan_speed     — enum (options) → preset_modes, число → percentage
      fan_direction — forward / reverse
    """
    dev_name = device["name"]
    avail = base_availability(dev_name)
    dps_map = device["dps_map"]

    has_switch = any(i.get("name") == "switch" for i in dps_map.values())
    speed = None
    for i in dps_map.values():
        if i.get("name") == "fan_speed":
            speed = i
            break
    has_dir = any(i.get("name") == "fan_direction" for i in dps_map.values())

    config = {
        "name": device["friendly_name"],
        "unique_id": f"{dev_name}_fan",
        "optimistic": False,
        "expire_after": AVAILABILITY_EXPIRE,
        "device": device_info,
        **avail,
    }
    if has_switch:
        config["command_topic"] = f"{TOPIC_PREFIX}/fan/{dev_name}/set"
        config["state_topic"] = f"{TOPIC_PREFIX}/fan/{dev_name}/state"
        config["payload_on"] = "ON"
        config["payload_off"] = "OFF"
        config["state_on"] = "ON"
        config["state_off"] = "OFF"

    if speed is not None:
        if speed.get("options"):
            config["preset_modes"] = list(speed["options"])
            config["preset_mode_command_topic"] = f"{TOPIC_PREFIX}/fan/{dev_name}/preset/set"
            config["preset_mode_state_topic"] = f"{TOPIC_PREFIX}/fan/{dev_name}/preset/state"
        else:
            config["percentage_command_topic"] = f"{TOPIC_PREFIX}/fan/{dev_name}/speed/set"
            config["percentage_state_topic"] = f"{TOPIC_PREFIX}/fan/{dev_name}/speed/state"
            config["speed_range_min"] = speed.get("min", 1)
            config["speed_range_max"] = speed.get("max", 100)

    if has_dir:
        config["direction_command_topic"] = f"{TOPIC_PREFIX}/fan/{dev_name}/direction/set"
        config["direction_state_topic"] = f"{TOPIC_PREFIX}/fan/{dev_name}/direction/state"

    if not (has_switch or speed or has_dir):
        log.warning(f"[Discovery] fan {dev_name}: нет DP switch/fan_speed/fan_direction")

    topic = f"{DISCOVERY_PREFIX}/fan/{dev_name}_fan/config"
    mqtt_client.publish(topic, json.dumps(config), retain=True)
    log.info(f"[Discovery] fan: {device['friendly_name']}")


def publish_locks(device, device_info):
    """v1.10.20: lock — умные замки (HA platform 'lock').

    DP с component='lock' (обычно name='lock_state', bool: true = заперто).
    Поле `inverted: true` в DP инвертирует смысл значения.
    """
    dev_name = device["name"]
    avail = base_availability(dev_name)

    for dp_str, info in device["dps_map"].items():
        if info.get("component") != "lock":
            continue
        entity_name = info.get("name", "lock")
        unique_id = f"{dev_name}_{entity_name}"
        config = {
            "name": f"{device['friendly_name']} {entity_name}",
            "unique_id": unique_id,
            "command_topic": f"{TOPIC_PREFIX}/lock/{dev_name}/{entity_name}/set",
            "state_topic": f"{TOPIC_PREFIX}/lock/{dev_name}/{entity_name}/state",
            "payload_lock": "LOCK",
            "payload_unlock": "UNLOCK",
            "state_locked": "LOCKED",
            "state_unlocked": "UNLOCKED",
            "optimistic": False,
            "expire_after": AVAILABILITY_EXPIRE,
            "device": device_info,
            **avail,
        }
        topic = f"{DISCOVERY_PREFIX}/lock/{unique_id}/config"
        mqtt_client.publish(topic, json.dumps(config), retain=True)
        log.info(f"[Discovery] lock: {device['friendly_name']} / {entity_name}")


def publish_climate(device, device_info):
    dev_name = device["name"]
    unique_id = f"{dev_name}_climate"
    avail = base_availability(dev_name)

    # v1.10.10: presets/preset_map могут быть не-list/dict (ручной
    # edit devices_config.json). Строгие проверки — иначе for p in 42.
    presets = device.get("presets") or []
    if not isinstance(presets, list):
        presets = []
    preset_map = device.get("preset_map") or {}
    if not isinstance(preset_map, dict):
        preset_map = {}

    # v1.9.10: если presets не заданы явно — выводим из DP с
    # component="preset" (options). Это делает climate-импорт из Cloud
    # автоматически рабочим: options DP становятся preset_modes в HA.
    if not presets:
        for _dp, _info in device.get("dps_map", {}).items():
            if _info.get("component") == "preset":
                _opts = _info.get("options", [])
                if _opts:
                    presets = list(_opts)
                    log.info(
                        f"[Discovery] climate {dev_name}: presets из DP "
                        f"{_dp} (options, {len(_opts)} шт.)"
                    )
                    break

    preset_modes_ha = [preset_map.get(p, p) for p in presets]

    min_temp = device.get("min_temp", DEFAULT_MIN_TEMP)
    max_temp = device.get("max_temp", DEFAULT_MAX_TEMP)
    temp_step = device.get("temp_step", DEFAULT_TEMP_STEP)

    config = {
        "name": device["friendly_name"],
        "unique_id": unique_id,
        "modes": ["off", "heat"],
        "mode_command_topic": f"{TOPIC_PREFIX}/climate/{dev_name}/mode/set",
        "mode_state_topic": f"{TOPIC_PREFIX}/climate/{dev_name}/mode/state",
        "temperature_command_topic": f"{TOPIC_PREFIX}/climate/{dev_name}/temp/set",
        "temperature_state_topic": f"{TOPIC_PREFIX}/climate/{dev_name}/temp/state",
        "current_temperature_topic": f"{TOPIC_PREFIX}/climate/{dev_name}/current/state",
        "min_temp": min_temp,
        "max_temp": max_temp,
        "temp_step": temp_step,
        "temperature_unit": "C",
        "optimistic": False,
        "expire_after": AVAILABILITY_EXPIRE,
        "device": device_info,
        **avail,
    }
    if presets:
        config["preset_modes"] = preset_modes_ha
        config["preset_mode_command_topic"] = f"{TOPIC_PREFIX}/climate/{dev_name}/preset/set"
        config["preset_mode_state_topic"] = f"{TOPIC_PREFIX}/climate/{dev_name}/preset/state"
    topic = f"{DISCOVERY_PREFIX}/climate/{unique_id}/config"
    mqtt_client.publish(topic, json.dumps(config), retain=True)
    log.info(f"[Discovery] climate: {device['friendly_name']} (presets: {preset_modes_ha})")


def publish_sensors(device, device_info):
    dev_name = device["name"]
    dev_type = device.get("type", "sensor")
    avail = base_availability(dev_name)

    for dp_str, info in device["dps_map"].items():
        component = info.get("component", "sensor")
        # v1.10.20: cover/fan/lock публикуются своими функциями
        # (publish_cover/publish_fan/publish_locks) — здесь их быть не должно.
        if component in ("switch", "light", "preset", "select", "phase_a",
                         "number", "cover", "fan", "lock"):
            continue
        entity_name = info.get("name", f"dp_{dp_str}")
        unique_id = f"{dev_name}_{entity_name}"
        state_topic = f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/dps/{dp_str}/state"

        # v1.9.4: per-device expire_after для батарейных.
        # Значения из конфига, дефолт — BATTERY_EXPIRE_AFTER.
        if device.get("battery_powered"):
            _expire = device.get("expire_after", BATTERY_EXPIRE_AFTER)
        else:
            _expire = AVAILABILITY_EXPIRE
        config = {
            "name": f"{device['friendly_name']} {entity_name}",
            "unique_id": unique_id,
            "state_topic": state_topic,
            "expire_after": _expire,
            "device": device_info,
            **avail,
        }
        if info.get("device_class"):
            config["device_class"] = info["device_class"]
        if info.get("unit"):
            config["unit_of_measurement"] = info["unit"]
        if component == "sensor":
            config["state_class"] = info.get("state_class", "measurement")
        elif component == "binary_sensor":
            config["payload_on"] = "ON"
            config["payload_off"] = "OFF"
            config["state_on"] = "ON"
            config["state_off"] = "OFF"
            # v1.9.13: для motion (и любого binary_sensor) НЕ трогаем
            # expire_after. Работает из device.expire_after: 3600/90000
            # для батарейных, 120 для обычных.
            #
            # Раньше для motion стояло expire_after=60 → HA переводил
            # сенсор в `unavailable` через минуту после последнего
            # события. Автоматизации «OFF → выключить свет» ложно
            # срабатывали через 60 сек после движения.
            #
            # Теперь: motion держит последнее значение (ON/OFF) пока
            # bridge живёт. `OFF` приходит только от устройства
            # (pir=none → "none" → OFF). `unavailable` — только если
            # устройство реально молчит > expire_after (для
            # батарейных = 25ч от battery_alert).

        topic = f"{DISCOVERY_PREFIX}/{component}/{unique_id}/config"
        mqtt_client.publish(topic, json.dumps(config), retain=True)
        log.info(f"[Discovery] {component}: {device['friendly_name']} / {entity_name}")


# ==================== BATTERY DISCOVERY (v1.9.5) ====================
def publish_battery_alert_discovery(device, device_info):
    """
    v1.9.5: Discovery для battery_alert и battery_last_seen.

    HA создаёт две сущности:
      sensor.<name>_battery_alert      — ok / no_data
      sensor.<name>_battery_last_seen  — timestamp последнего UP

    object_id задаёт стабильный entity_id (из dev_name, не friendly_name).
    """
    dev_name = device["name"]
    friendly = device["friendly_name"]
    avail = base_availability(dev_name)
    # v1.9.7: expire_after для battery_alert должен быть БОЛЬШЕ
    # BATTERY_ALERT_AFTER_SEC, иначе HA уходит в unavailable
    # через device.expire_after (3600 для двери), а bridge
    # перепубликовывает alert только при смене состояния.
    # Берём BATTERY_ALERT_AFTER_SEC + 3600 (запас 1 час).
    _alert_expire = BATTERY_ALERT_AFTER_SEC + 3600

    # v1.9.11: alert_id = <dev>_battery_alert. Раньше было <dev>_battery —
    # конфликтовало с DP с name="battery" (оба писали в один
    # homeassistant/sensor/<dev>_battery/config). Теперь battery_alert
    # всегда отдельная сущность, никогда не пересекается с DP.
    alert_id = f"{dev_name}_battery_alert"
    alert_config = {
        "name": f"{friendly} battery",
        "unique_id": alert_id,
        "object_id": alert_id,
        "state_topic": f"{TOPIC_PREFIX}/{dev_name}/battery_alert",
        "icon": "mdi:battery-alert-variant-outline",
        "expire_after": _alert_expire,
        "device": device_info,
        **avail,
    }
    mqtt_client.publish(
        f"{DISCOVERY_PREFIX}/sensor/{alert_id}/config",
        json.dumps(alert_config, ensure_ascii=False),
        qos=1, retain=True,
    )
    log.info(f"[Discovery] battery: {friendly}")
    
    # 2. battery_last_seen — timestamp
    lastup_id = f"{dev_name}_battery_last_seen"
    lastup_config = {
        "name": f"{friendly} last seen",
        "unique_id": lastup_id,
        "object_id": lastup_id,
        "state_topic": f"{TOPIC_PREFIX}/{dev_name}/battery_last_up",
        "device_class": "timestamp",
        "icon": "mdi:clock-check-outline",
        "device": device_info,
        **avail,
    }
    mqtt_client.publish(
        f"{DISCOVERY_PREFIX}/sensor/{lastup_id}/config",
        json.dumps(lastup_config, ensure_ascii=False),
        qos=1, retain=True,
    )
    log.info(f"[Discovery] battery_last_seen: {friendly}")


# ==================== STATE PUBLISHING ====================
def publish_state(device, dps):
    dev_type = device.get("type", "sensor")
    dev_name = device["name"]
    dps_map = device["dps_map"]

    if dps:
        _cache_update(dev_name, {str(k): v for k, v in dps.items()})

    with STATE_LOCK:
        cached = dict(STATE_CACHE.get(dev_name, {}))

    # v1.10.20: lock — DP-компонент, публикуется независимо от типа устройства.
    for dp_str, info in dps_map.items():
        if info.get("component") == "lock" and dp_str in cached:
            _publish_lock_state(dev_name, info, cached[dp_str])

    if dev_type == "light":
        state = {"state": "OFF"}
        color_temp_val = None
        color_val = None
        brightness_val = None
        has_color = find_dp_by_name(device, "colour_data")[0] is not None
        has_temp = find_dp_by_name(device, "temp_value")[0] is not None
        has_bright = find_dp_by_name(device, "bright_value")[0] is not None

        for dp_str, info in dps_map.items():
            if dp_str not in cached:
                continue
            val = cached[dp_str]
            name = info.get("name")
            if name == "switch_led":
                state["state"] = "ON" if val else "OFF"
            elif name == "bright_value":
                bmin, bmax = get_bright_bounds(info)
                brightness_val = tuya_to_ha_brightness(val, bmin, bmax)
            elif name == "temp_value":
                kmin, kmax = get_kelvin_bounds(info)
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    v = 0
                color_temp_val = int(kmin + (v / 1000) * (kmax - kmin))
            elif name == "colour_data":
                r, g, b = tuya_hsv_to_rgb(str(val))
                color_val = {"r": r, "g": g, "b": b}

        if state["state"] == "OFF":
            mqtt_client.publish(
                f"{TOPIC_PREFIX}/light/{dev_name}/state",
                json.dumps({"state": "OFF"}),
                retain=True,
            )
            return

        if has_bright and brightness_val is None:
            brightness_val = HA_BRIGHT_MAX

        if has_bright and brightness_val is not None:
            state["brightness"] = brightness_val

        if has_color:
            state["color_mode"] = "rgb"
            if color_val is not None:
                state["color"] = color_val
        elif has_temp:
            state["color_mode"] = "color_temp"
            if color_temp_val is not None:
                state["color_temp_kelvin"] = color_temp_val
        else:
            state["color_mode"] = "brightness"

        mqtt_client.publish(
            f"{TOPIC_PREFIX}/light/{dev_name}/state",
            json.dumps(state),
            retain=True,
        )
        return

    if dev_type == "switch":
        phase_a_dp = None
        for dp_str, info in dps_map.items():
            comp = info.get("component")
            if comp == "phase_a":
                phase_a_dp = dp_str
                continue
            if dp_str not in cached:
                continue
            val = cached[dp_str]
            name = info.get("name")
            if comp == "switch":
                mqtt_client.publish(
                    f"{TOPIC_PREFIX}/switch/{dev_name}/{name}/state",
                    "ON" if val else "OFF", retain=True,
                )
            elif comp == "select":
                sval = str(val)
                smap = get_select_map(info)
                if smap:
                    rev = {v: k for k, v in smap.items()}
                    sval = rev.get(sval, sval)
                mqtt_client.publish(
                    f"{TOPIC_PREFIX}/select/{dev_name}/{name}/state",
                    sval, retain=True,
                )
            elif comp == "number":
                scale = info.get("scale", 0)
                try:
                    nval = float(val) / (10 ** scale) if scale else val
                except (TypeError, ValueError):
                    nval = val
                mqtt_client.publish(
                    f"{TOPIC_PREFIX}/number/{dev_name}/{name}/state",
                    str(nval), retain=True,
                )
            elif comp in ("sensor", "binary_sensor"):
                _publish_sensor_value(dev_type, dev_name, dp_str, info, val)

        if phase_a_dp and phase_a_dp in cached:
            v, c, p = parse_phase_a(str(cached[phase_a_dp]))
            if v is not None:
                mqtt_client.publish(
                    f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/phase_a/voltage/state",
                    str(v), retain=True,
                )
            if c is not None:
                mqtt_client.publish(
                    f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/phase_a/current/state",
                    str(c), retain=True,
                )
            if p is not None:
                mqtt_client.publish(
                    f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/phase_a/power/state",
                    str(p), retain=True,
                )
        return

    if dev_type == "climate":
        mode = "off"
        temp_set = None
        temp_cur = None
        preset = None
        for dp_str, info in dps_map.items():
            if dp_str not in cached:
                continue
            name = info.get("name")
            val = cached[dp_str]
            if name == "switch":
                mode = "heat" if val else "off"
            elif name == "temp_set":
                try:
                    temp_set = float(val) / 10.0
                except (TypeError, ValueError):
                    pass
            elif name == "temp_current":
                try:
                    temp_cur = float(val) / 10.0
                except (TypeError, ValueError):
                    pass
            elif name == "preset_mode":
                preset = str(val)

        mqtt_client.publish(f"{TOPIC_PREFIX}/climate/{dev_name}/mode/state", mode, retain=True)
        if temp_set is not None:
            mqtt_client.publish(f"{TOPIC_PREFIX}/climate/{dev_name}/temp/state", str(temp_set), retain=True)
        if temp_cur is not None:
            mqtt_client.publish(f"{TOPIC_PREFIX}/climate/{dev_name}/current/state", str(temp_cur), retain=True)
        if preset is not None:
            preset_map = device.get("preset_map", {})
            preset_ha = preset_map.get(preset, preset)
            mqtt_client.publish(f"{TOPIC_PREFIX}/climate/{dev_name}/preset/state", preset_ha, retain=True)
        return

    if dev_type == "cover":
        control = None
        position = None
        for dp_str, info in dps_map.items():
            if dp_str not in cached:
                continue
            name = info.get("name")
            val = cached[dp_str]
            if name == "control":
                control = str(val).strip().lower()
            elif name == "percent_state":
                position = _to_int_percent(val)
            elif name == "percent_control" and position is None:
                position = _to_int_percent(val)

        if control in ("open", "opening"):
            mqtt_client.publish(f"{TOPIC_PREFIX}/cover/{dev_name}/state", "OPEN", retain=True)
        elif control in ("close", "closing"):
            mqtt_client.publish(f"{TOPIC_PREFIX}/cover/{dev_name}/state", "CLOSE", retain=True)
        if position is not None:
            mqtt_client.publish(f"{TOPIC_PREFIX}/cover/{dev_name}/position/state",
                                str(position), retain=True)
        return

    if dev_type == "fan":
        state = None
        pct = None
        preset = None
        direction = None
        for dp_str, info in dps_map.items():
            if dp_str not in cached:
                continue
            name = info.get("name")
            val = cached[dp_str]
            if name == "switch":
                state = "ON" if val else "OFF"
            elif name == "fan_speed":
                if info.get("options"):
                    sval = str(val)
                    smap = get_select_map(info)
                    if smap:
                        sval = {v: k for k, v in smap.items()}.get(sval, sval)
                    preset = sval
                else:
                    pct = _to_int_percent(val)
            elif name == "fan_direction":
                direction = str(val).strip().lower()

        if state is not None:
            mqtt_client.publish(f"{TOPIC_PREFIX}/fan/{dev_name}/state", state, retain=True)
        if preset is not None:
            mqtt_client.publish(f"{TOPIC_PREFIX}/fan/{dev_name}/preset/state", preset, retain=True)
        elif pct is not None:
            mqtt_client.publish(f"{TOPIC_PREFIX}/fan/{dev_name}/speed/state", str(pct), retain=True)
        if direction is not None:
            mqtt_client.publish(f"{TOPIC_PREFIX}/fan/{dev_name}/direction/state",
                                direction, retain=True)
        return

    for dp_str, info in dps_map.items():
        if dp_str in cached:
            _publish_sensor_value(dev_type, dev_name, dp_str, info, cached[dp_str])


def _publish_lock_state(dev_name, info, raw_val):
    """v1.10.20: состояние замка → HA (LOCKED / UNLOCKED)."""
    entity_name = info.get("name", "lock")
    payload = "LOCKED" if _lock_locked(raw_val, info) else "UNLOCKED"
    mqtt_client.publish(f"{TOPIC_PREFIX}/lock/{dev_name}/{entity_name}/state",
                        payload, retain=True)


def _publish_sensor_value(dev_type, dev_name, dp_str, info, raw_val):
    component = info.get("component", "sensor")
    scale = info.get("scale", 0)
    name = info.get("name", "")

    if component == "binary_sensor":
        if isinstance(raw_val, bool):
            payload = "ON" if raw_val else "OFF"
        elif isinstance(raw_val, (int, float)):
            if name in ("fault", "problem"):
                try:
                    payload = "ON" if int(raw_val) != 0 else "OFF"
                except (TypeError, ValueError):
                    payload = "OFF"
            else:
                payload = "ON" if raw_val else "OFF"
        else:
            payload = "ON" if str(raw_val).lower() in ("pir", "alarm", "true", "1", "open", "detected") else "OFF"
    else:
        try:
            value = float(raw_val) / (10 ** scale) if scale else raw_val
        except (TypeError, ValueError):
            value = raw_val
        payload = str(value)

    topic = f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/dps/{dp_str}/state"
    mqtt_client.publish(topic, payload, retain=True)


# ==================== AVAILABILITY / SNAPSHOT ====================
def publish_availability(dev, online: bool):
    topic = f"{TOPIC_PREFIX}/{dev['name']}/status"
    mqtt_client.publish(topic, "online" if online else "offline", qos=1, retain=True)


def publish_last_seen(dev):
    topic = f"{TOPIC_PREFIX}/{dev['name']}/last_seen"
    mqtt_client.publish(topic, str(int(time.time())), qos=0, retain=True)


def publish_cache_snapshot(dev):
    if not CACHE_SNAPSHOT_ENABLED:
        return
    name = dev["name"]
    with STATE_LOCK:
        cached = dict(STATE_CACHE.get(name, {}))

    # v1.10.17: производные от phase_a (X_voltage/X_current/X_power) в snapshot
    # не кладём — bridge публикует их отдельными сущностями, в кэше они лишние.
    if not cached:
        return
    topic = f"{TOPIC_PREFIX}/{name}/cache_snapshot"
    try:
        mqtt_client.publish(topic, json.dumps(cached, ensure_ascii=False),
                            qos=1, retain=True)
    except Exception as e:
        log.debug(f"[Snapshot] {name}: {e}")


# ==================== SMART LOGGING 914/905 ====================
# Fix 1.8.2: первый раз INFO, повтор — WARNING, дальше — exponential backoff.
#
# Логика "подряд идущие":
#   - счётчик сбрасывается, если между событиями > REPEAT_RESET_SECONDS (2 мин);
#   - счётчик сбрасывается при успешном ответе устройства.
#
# Интервалы логирования:
#   1        -> INFO (разовый)
#   2-5      -> WARNING (каждый, с номером)
#   6-10     -> WARNING (раз в 2 события)
#   11-50    -> WARNING (раз в 10 событий)
#   51-200   -> WARNING (раз в 50 событий)
#   > 200    -> WARNING (раз в 100 событий)

_LAST_914_STATE = {}   # name -> [last_ts, count, last_logged]
_LAST_914_LOCK = threading.Lock()

_LAST_905_STATE = {}   # name -> [last_ts, count, last_logged]
_LAST_905_LOCK = threading.Lock()


def _log_repeat(name, code_label, code_hint, where="", state_dict=None, lock=None):
    """
    Универсальный логгер повторяющихся событий (914/905).
    """
    now = time.time()
    should_log = False
    log_level = "INFO"
    count = 0

    with lock:
        entry = state_dict.get(name)
        if entry is None:
            entry = [0, 0, 0]  # [last_ts, count, last_logged]
            state_dict[name] = entry

        last_ts, count, last_logged = entry

        # Сброс, если между событиями > REPEAT_RESET_SECONDS
        if now - last_ts > REPEAT_RESET_SECONDS:
            count = 0

        count += 1
        entry[0] = now
        entry[1] = count

        # Решаем, логировать ли
        if count == 1:
            should_log = True
            log_level = "INFO"
        elif count == 2:
            should_log = True
            log_level = "WARNING"
        elif count <= 5:
            should_log = True
            log_level = "WARNING"
        elif count <= 10:
            if count - last_logged >= 2:
                should_log = True
                log_level = "WARNING"
        elif count <= 50:
            if count - last_logged >= 10:
                should_log = True
                log_level = "WARNING"
        elif count <= 200:
            if count - last_logged >= 50:
                should_log = True
                log_level = "WARNING"
        else:
            if count - last_logged >= 100:
                should_log = True
                log_level = "WARNING"

        if should_log:
            entry[2] = count

    if not should_log:
        return

    if count == 1:
        log.info(f"[Worker] {name}: {code_label} {where} (разовый, не критично)")
    else:
        log.warning(f"[Worker] {name}: {code_label} {where} (подряд #{count}) — {code_hint}")


def _log_914_once(name, where=""):
    _log_repeat(
        name,
        code_label="Tuya error 914",
        code_hint="проверь local_key/version в конфиге",
        where=where,
        state_dict=_LAST_914_STATE,
        lock=_LAST_914_LOCK,
    )


def _log_905_once(name, where=""):
    _log_repeat(
        name,
        code_label="Network Error 905",
        code_hint="устройство недоступно",
        where=where,
        state_dict=_LAST_905_STATE,
        lock=_LAST_905_LOCK,
    )


def _reset_repeat_counters(name):
    """Fix 1.8.2: успешный ответ устройства — сбрасываем счётчики 914/905."""
    with _LAST_914_LOCK:
        _LAST_914_STATE.pop(name, None)
    with _LAST_905_LOCK:
        _LAST_905_STATE.pop(name, None)


# ==================== WORKERS ====================
def _read_socket(d, timeout):
    # v1.10.11: ищем timeout по возможным именам атрибута. Ранее был
    # только "socketTimeout"; если tinytuya хранит иначе — old_to
    # оставался None, timeout не восстанавливался.
    old_to = None
    for _attr in ("socketTimeout", "_socketTimeout"):
        if hasattr(d, _attr):
            old_to = getattr(d, _attr)
            break
    try:
        d.set_socketTimeout(timeout)
        return d.receive()
    finally:
        if old_to is not None:
            try:
                d.set_socketTimeout(old_to)
            except Exception:
                pass


def _status_socket(d, timeout=SOCKET_TIMEOUT_CMD):
    # v1.10.11: см. _read_socket.
    old_to = None
    for _attr in ("socketTimeout", "_socketTimeout"):
        if hasattr(d, _attr):
            old_to = getattr(d, _attr)
            break
    try:
        d.set_socketTimeout(timeout)
        return d.status()
    finally:
        if old_to is not None:
            try:
                d.set_socketTimeout(old_to)
            except Exception:
                pass


def run_polling_device(dev):
    """
    v1.8.2: один persistent сокет, receive() под коротким lock (100мс).
    После команды — форсируем status() (consume_status_request).
    Умное логирование 914/905 (INFO первый раз, WARNING повтор).
    """
    name = dev["name"]
    log.info(f"[Worker] polling: {name}")
    last_status_ok = 0
    last_real_data = time.time()
    has_phase_a = dev.get("dps_map", {}).get("6", {}).get("component") == "phase_a"
    last_online = None
    consecutive_904 = 0
    first_iteration = True

    while not STOP_EVENT.is_set():
        if consume_restart_flag(name):
            # v1.9.14: если battery_powered сменилось на true — выходим.
            # Внешний код (_handle_edit_config) запустит run_battery_listener.
            with DEVICES_LOCK:
                _dev = DEVICE_INDEX.get(name)
            if _dev is not None and bool(_dev.get("battery_powered", False)):
                log.info(f"[Worker] {name}: battery_powered=true — выходим "
                         f"(нужен battery listener)")
                break
            log.info(f"[Worker] {name}: restart flag — перезапуск соединения")
            drop_device_conn(name)
            # v1.10.16: пауза, чтобы Tuya освободила сессию и не отдала
            # разовый 914 на новое TCP-соединение.
            if STOP_EVENT.wait(RECONNECT_SETTLE_SEC):
                break
            last_status_ok = 0
            first_iteration = True
            # v1.9.14 (B10): пересчитать has_phase_a — мог измениться
            # через edit_config (добавили/убрали DP 6 = phase_a).
            has_phase_a = _dev.get("dps_map", {}).get("6", {}).get("component") == "phase_a" if _dev else has_phase_a
            _reset_repeat_counters(name)

        if consume_status_request(name):
            last_status_ok = 0

        with DEVICES_LOCK:
            if name not in DEVICE_INDEX:
                log.info(f"[Worker] {name}: удалено из конфига, остановка")
                break

        lock = get_device_cmd_lock(name)

        # ==== READ: receive() под коротким lock ====
        data = None
        got_lock = lock.acquire(timeout=LOCK_ACQUIRE_TIMEOUT)
        if got_lock:
            try:
                try:
                    d = get_device_conn(dev)
                    data = _read_socket(d, SOCKET_TIMEOUT_WORKER)
                except Exception as e:
                    log.debug(f"[Worker] {name} receive: {e}")
                    drop_device_conn(name)
            finally:
                lock.release()
        else:
            if STOP_EVENT.wait(0.02):
                break
            continue

        if DEBUG_CACHE_RECEIVE and data:
            log.info(f"[Worker] {name}: receive()={data}")

        # ==== Обработка данных ====
        if data and isinstance(data, dict):
            if "Error" in data:
                err = data.get("Error", "")
                err_code = str(data.get("Err", ""))
                consecutive_904 += 1

                if err_code == "904":
                    log.debug(f"[Worker] {name}: 904 reconnect "
                              f"[{consecutive_904}/{MAX_CONSECUTIVE_904}]")
                    drop_device_conn(name)
                    if consecutive_904 >= MAX_CONSECUTIVE_904:
                        log.warning(f"[Worker] {name}: 904 повторяется {consecutive_904} раз, offline")
                        publish_availability(dev, False)
                        last_online = False
                        consecutive_904 = 0
                elif err_code == "914":
                    _log_914_once(name, "receive")
                    consecutive_904 = 0
                elif err_code == "905":
                    _log_905_once(name, "receive")
                    drop_device_conn(name)
                    consecutive_904 = 0
                else:
                    log.warning(f"[Worker] {name}: Tuya error {err_code}: {err}")
                    drop_device_conn(name)
                    consecutive_904 = 0
                    # v1.10.3: не busy-loop при частых ошибках
                    if STOP_EVENT.wait(WORKER_IDLE_SLEEP):
                        break
            else:
                consecutive_904 = 0
                dps = data.get("dps", data)
                if dps and isinstance(dps, dict):
                    # Fix 1.8.2: успешный ответ — сбрасываем счётчики 914/905
                    _reset_repeat_counters(name)
                    if DEBUG_RAW_DP:
                        log.info(f"[Worker] {name}: receive dps={dps}")
                    try:
                        publish_state(dev, dps)
                    except Exception as e:
                        log.warning(f"[Worker] {name}: publish_state: {e}")
                    last_real_data = time.time()
                    publish_last_seen(dev)
                    publish_cache_snapshot(dev)
                    if last_online is not True:
                        publish_availability(dev, True)
                        last_online = True

        # ==== STATUS: раз в POLL_INTERVAL или форсированно ====
        need_status = first_iteration or (time.time() - last_status_ok > POLL_INTERVAL)
        if need_status:
            got_lock = lock.acquire(timeout=LOCK_ACQUIRE_TIMEOUT)
            if got_lock:
                poll_data = None
                try:
                    try:
                        d = get_device_conn(dev)
                        poll_data = _status_socket(d)
                    except Exception as e:
                        log.debug(f"[Worker] {name} poll: {e}")
                        poll_data = None
                        drop_device_conn(name)
                finally:
                    lock.release()

                if DEBUG_CACHE_STATUS and poll_data:
                    log.info(f"[Worker] {name}: status()={poll_data}")

                if poll_data and isinstance(poll_data, dict) and "Error" in poll_data:
                    err = poll_data.get("Error", "")
                    err_code = str(poll_data.get("Err", ""))
                    consecutive_904 += 1

                    if err_code == "904":
                        log.debug(f"[Worker] {name}: poll 904 reconnect "
                                  f"[{consecutive_904}/{MAX_CONSECUTIVE_904}]")
                        drop_device_conn(name)
                        if consecutive_904 >= MAX_CONSECUTIVE_904:
                            log.warning(f"[Worker] {name}: poll 904 повторяется, offline")
                            publish_availability(dev, False)
                            last_online = False
                            consecutive_904 = 0
                    elif err_code == "914":
                        _log_914_once(name, "status")
                        consecutive_904 = 0
                    elif err_code == "905":
                        _log_905_once(name, "poll")
                        drop_device_conn(name)
                        consecutive_904 = 0
                    else:
                        log.warning(f"[Worker] {name}: poll error {err_code}: {err}")
                        drop_device_conn(name)
                        consecutive_904 = 0
                        # v1.10.3: не busy-loop при частых ошибках
                        if STOP_EVENT.wait(WORKER_IDLE_SLEEP):
                            break
                elif poll_data and isinstance(poll_data, dict):
                    consecutive_904 = 0
                    dps = poll_data.get("dps", poll_data)
                    if dps and isinstance(dps, dict):
                        # Fix 1.8.2: успешный ответ — сбрасываем счётчики 914/905
                        _reset_repeat_counters(name)
                        if DEBUG_RAW_DP:
                            log.info(f"[Worker] {name}: status dps={dps}")
                        try:
                            publish_state(dev, dps)
                        except Exception as e:
                            log.warning(f"[Worker] {name}: publish_state (status): {e}")
                        last_real_data = time.time()
                        publish_last_seen(dev)
                        publish_cache_snapshot(dev)
                        if last_online is not True:
                            publish_availability(dev, True)
                            last_online = True
                elif poll_data is None:
                    log.debug(f"[Worker] {name}: no response to status()")

                if has_phase_a and poll_data and "6" not in (poll_data.get("dps", {}) or {}):
                    got_lock2 = lock.acquire(timeout=LOCK_ACQUIRE_TIMEOUT)
                    if got_lock2:
                        try:
                            try:
                                d = get_device_conn(dev)
                                extra = _status_socket(d)
                                if extra and isinstance(extra, dict) and "Error" not in extra:
                                    dps = extra.get("dps", extra)
                                    if dps and isinstance(dps, dict) and "6" in dps:
                                        publish_state(dev, dps)
                            except Exception as e:
                                log.debug(f"[Worker] {name} phase_a extra: {e}")
                        finally:
                            lock.release()

                last_status_ok = time.time()
                first_iteration = False

        # ==== OFFLINE_TIMEOUT ====
        if time.time() - last_real_data > OFFLINE_TIMEOUT:
            if last_online is not False:
                log.warning(f"[Worker] {name}: нет данных "
                            f"{int(time.time() - last_real_data)}с, offline")
                publish_availability(dev, False)
                last_online = False
            last_real_data = time.time()
            drop_device_conn(name)

        # receive() уже ждал 100мс, пауза снижает нагрузку на CPU/сеть
        if data is None:
            if STOP_EVENT.wait(WORKER_IDLE_SLEEP):
                break

    log.info(f"[Worker] {name}: остановлен")


# ==================== BATTERY ALERT (v1.9.4) ====================
def publish_battery_alert(dev, no_data: bool):
    """Публикует tuya/<name>/battery_alert = no_data | ok (retain)."""
    topic = f"{TOPIC_PREFIX}/{dev['name']}/battery_alert"
    payload = "no_data" if no_data else "ok"
    try:
        mqtt_client.publish(topic, payload, qos=1, retain=True)
    except Exception as e:
        log.debug(f"[Battery] {dev['name']}: alert publish: {e}")


def publish_battery_last_up(dev, ts: int = None):
    """
    Публикует tuya/<name>/battery_last_up (retain).

    v1.9.8: ISO8601 вместо unix. HA для сенсора с device_class=timestamp
    принимает ТОЛЬКО ISO8601 (2026-09-20T03:48:35+00:00). Unix seconds
    ("1789850915") HA не парсит → sensor.<name>_battery_last_seen = unknown.

    WebUI 1.28.7 читает ISO8601 и конвертирует обратно в unix
    для внутреннего хранения (чтобы fmtAgo/fmtDateTime работали).
    """
    if ts is None:
        ts = int(time.time())
    try:
        iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    except (ValueError, OSError, OverflowError):
        # Некорректный ts — публикуем текущее время
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    topic = f"{TOPIC_PREFIX}/{dev['name']}/battery_last_up"
    try:
        mqtt_client.publish(topic, iso, qos=1, retain=True)
    except Exception as e:
        log.debug(f"[Battery] {dev['name']}: last_up publish: {e}")


# ==================== BATTERY LISTENER (v1.9.0) ====================
def run_battery_listener(dev):
    """
    v1.9.0: воркер для батарейных Tuya-устройств (device22).

    Логика:
      1. ICMP-пинг → ждём переход DOWN → UP (устройство проснулось).
      2. В окне UP: updatedps([dp1, dp2, ...]) N раз с интервалом.
      3. Публикуем изменения dps.
      4. DOWN → считаем циклы, публикуем availability=offline
         через BATTERY_OFFLINE_AFTER_DOWN циклов.

    Батарейные устройства НЕ опрашиваются постоянно — они спят.
    Просыпаются только на событие (дверь) или heartbeat.
    """
    name = dev["name"]
    # v1.10.11: кешированный _icmp_capable — не дёргаем socket() в цикле.
    _icmp_ok = _icmp_capable()
    if not _icmp_ok:
        log.error(
            f"[Battery] {name}: нет CAP_NET_RAW — Battery listener НЕ БУДЕТ "
            f"работать. Проверь cap_add: [NET_RAW] в docker-compose."
        )
    log.info(f"[Battery] {name}: старт (ping {BATTERY_PING_INTERVAL}s)")

    # DP для updatedps — все из dps_map
    dp_list = [int(dp) for dp in dev.get("dps_map", {}).keys() if str(dp).isdigit()]
    if not dp_list:
        log.warning(f"[Battery] {name}: нет DP в dps_map, использую [1]")
        dp_list = [1]
    log.info(f"[Battery] {name}: DP list = {dp_list}")

    last_state = None
    last_dps = None
    was_online = None
    # v1.9.6: _last_up_ts = time.time() при старте (а не 0).
    # Иначе если устройство ни разу не проснулось, battery_alert
    # НИКОГДА не станет no_data — условие `if _last_up_ts > 0` не сработает.
    # Теперь: через 24ч от старта, если не было UP → no_data.
    _start_ts = time.time()
    # v1.10.0: восстанавливаем _last_up_ts из persist (если есть).
    # Иначе age считается от старта bridge, а не от реального UP —
    # при рестартах battery_alert=no_data либо запаздывает, либо
    # никогда не публикуется.
    _last_up_ts = BATTERY_LAST_UP.get(name) or _start_ts
    _alert_sent = False
    # v1.9.6: отслеживаем текущее состояние alert — публикуем только
    # при смене (ok ↔ no_data), иначе спам в MQTT при каждом UP.
    _alert_state = None   # None | "ok" | "no_data"

    while not STOP_EVENT.is_set():
        if consume_restart_flag(name):
            # v1.9.14: если battery_powered сменилось на false — выходим.
            # Внешний код (_handle_edit_config) запустит run_polling_device.
            with DEVICES_LOCK:
                _dev = DEVICE_INDEX.get(name)
            if _dev is not None and not bool(_dev.get("battery_powered", False)):
                log.info(f"[Battery] {name}: battery_powered=false — выходим "
                         f"(нужен polling worker)")
                break
            log.info(f"[Battery] {name}: restart flag")
            drop_device_conn(name)
            # v1.10.16: пауза перед новым TCP (защита от разового 914).
            if STOP_EVENT.wait(RECONNECT_SETTLE_SEC):
                break
            last_state = None

        with DEVICES_LOCK:
            if name not in DEVICE_INDEX:
                log.info(f"[Battery] {name}: удалено из конфига, стоп")
                break

        # === Пинг ===
        ms = _ping_native(dev["ip"], timeout=BATTERY_PING_TIMEOUT)
        up = (ms is not None)
        state = "UP" if up else "DOWN"

        if state != last_state:
            extra = f" ({ms} мс)" if up else ""
            log.info(f"[Battery] {name}: {state}{extra}")

        # === Переход DOWN → UP ===
        if up and last_state != "UP":
            log.info(f"[Battery] {name}: окно пробуждения, делаю updatedps")

            # v1.9.4: publish_availability(True) при ЛЮБОМ UP
            # (не ждём updatedps — может быть промах, но устройство
            # уже живо). Плюс battery_last_up + сброс alert.
            if was_online is not True:
                publish_availability(dev, True)
                was_online = True
            _last_up_ts = time.time()
            publish_battery_last_up(dev, int(_last_up_ts))
            save_battery_last_up(name, int(_last_up_ts))   # v1.10.0
            # v1.9.14: публикуем battery_alert=ok при КАЖДОМ UP.
            # Раньше — только при смене _alert_state (None/no_data → ok).
            # Проблема: HA не получал live-сообщение в state_topic →
            # expire_after (90000с) не сбрасывался → через 25ч HA уходил
            # в unavailable, даже если устройство живо.
            # Теперь retained battery_alert=ok перезаписывается при каждом UP,
            # HA видит новое сообщение → таймер сбрасывается.
            if _alert_sent or _alert_state == "no_data":
                log.info(f"[Battery] {name}: данные пришли, battery_alert=ok")
            publish_battery_alert(dev, False)
            _alert_state = "ok"
            _alert_sent = False
            # v1.9.14: publish_last_seen при UP (безусловно).
            # last_seen обновляется независимо от того, изменились ли dps.
            publish_last_seen(dev)

            try:
                for i in range(BATTERY_UPDATEDPS_COUNT):
                    if STOP_EVENT.is_set():
                        break

                    # v1.10.11: устройство могло быть удалено в окне UP
                    # (delete_device → request_worker_restart). Без проверки
                    # воркер публикует state после cleanup retained → фантом.
                    # v1.10.12: drop_device_conn ПЕРЕД return — иначе
                    # сокет остаётся висеть до явного закрытия извне.
                    with DEVICES_LOCK:
                        _dev_gone = name not in DEVICE_INDEX
                    if _dev_gone:
                        log.info(f"[Battery] {name}: удалено в окне UP, стоп")
                        drop_device_conn(name)
                        return

                    # Прервать, если устройство заснуло
                    if _ping_native(dev["ip"], timeout=BATTERY_PING_TIMEOUT) is None:
                        log.debug(f"[Battery] {name}: DOWN в середине окна, стоп")
                        break

                    # v1.10.15: updatedps идёт по тому же persistent-сокету,
                    # что и команды из MQTT. Берём per-device lock — иначе
                    # конкурентный send/recv (правило №1: один владелец сокета).
                    _lock = get_device_cmd_lock(name)
                    if not _lock.acquire(timeout=LOCK_ACQUIRE_TIMEOUT):
                        if STOP_EVENT.wait(BATTERY_UPDATEDPS_INTERVAL):
                            break
                        continue
                    try:
                        d = get_device_conn(dev)
                        d.set_socketTimeout(BATTERY_UPDATEDPS_TIMEOUT)
                        result = d.updatedps(dp_list)

                        if result and isinstance(result, dict) and "dps" in result:
                            dps = result["dps"]
                            if dps != last_dps:
                                log.info(
                                    f"[Battery] {name}: updatedps → "
                                    f"{json.dumps(dps, ensure_ascii=False)}"
                                )
                                try:
                                    publish_state(dev, dps)
                                except Exception as e:
                                    log.warning(f"[Battery] {name}: publish_state: {e}")
                                publish_last_seen(dev)
                                # v1.9.7: WebUI не видит cache батарейных
                                # без этого вызова (publish_cache_snapshot
                                # в polling-воркере есть, в battery — не было).
                                publish_cache_snapshot(dev)
                                last_dps = dps

                                if was_online is not True:
                                    publish_availability(dev, True)
                                    was_online = True
                        else:
                            log.debug(f"[Battery] {name}: updatedps #{i+1} no dps")
                    except Exception as e:
                        log.debug(f"[Battery] {name}: updatedps #{i+1} err: {e}")
                    finally:
                        _lock.release()

                    if STOP_EVENT.wait(BATTERY_UPDATEDPS_INTERVAL):
                        break

                # Закрываем соединение — устройство засыпает
                drop_device_conn(name)

            except Exception as e:
                log.warning(f"[Battery] {name}: window error: {e}")
                drop_device_conn(name)

        # === DOWN ===
        # v1.9.4: НЕ публикуем offline для батарейных — они спят
        # по определению. HA полагается на expire_after (per-device).
        # Вместо этого — battery_alert через BATTERY_ALERT_AFTER_SEC.
        if not up:
            # v1.10.11: если ping недоступен (нет CAP_NET_RAW) — не
            # публикуем no_data. Это ложная тревога: причина не в
            # устройстве, а в bridge. WebUI покажет warning.
            if not _icmp_ok:
                if _alert_state != "ping_unavailable":
                    log.warning(
                        f"[Battery] {name}: CAP_NET_RAW отсутствует — "
                        f"battery_alert не публикуется (см. UI warning)"
                    )
                    _alert_state = "ping_unavailable"
            else:
                # Проверка «нет данных > 24 часа» (раз в ~30 сек).
                # v1.9.6: _last_up_ts инициализирован при старте, поэтому
                # age корректно считается с момента запуска воркера,
                # даже если устройство НИ РАЗУ не проснулось.
                age = time.time() - _last_up_ts
                if age > BATTERY_ALERT_AFTER_SEC and _alert_state != "no_data":
                    log.warning(
                        f"[Battery] {name}: нет данных {int(age/3600)}ч "
                        f"— battery_alert=no_data"
                    )
                    publish_battery_alert(dev, True)
                    _alert_state = "no_data"
                    _alert_sent = True

        last_state = state

        if STOP_EVENT.wait(BATTERY_PING_INTERVAL):
            break

    drop_device_conn(name)
    log.info(f"[Battery] {name}: остановлен")


# ==================== HEALTH ====================
def health_worker():
    """
    v1.9.3: публикует uptime + cpu_pct + rss_mb.
    CPU считается как delta utime+stime за интервал.
    RSS — из /proc/self/status.
    """
    _prev = {"utime": 0, "stime": 0, "ts": 0.0}
    _hz = None
    try:
        _hz = os.sysconf("SC_CLK_TCK")
    except Exception:
        _hz = 100  # fallback

    def _read_bridge_metrics():
        """Возвращает (cpu_pct, rss_mb). Любое значение может быть None."""
        cpu_pct = None
        rss_mb = None
        try:
            with open("/proc/self/stat", "r") as f:
                parts = f.read().split()
            utime = int(parts[13])
            stime = int(parts[14])
            now = time.time()
            if _prev["ts"] > 0:
                dt = now - _prev["ts"]
                dticks = (utime + stime) - (_prev["utime"] + _prev["stime"])
                if dt > 0 and dticks >= 0:
                    cpu_pct = round((dticks / _hz) / dt * 100, 1)
            _prev["utime"] = utime
            _prev["stime"] = stime
            _prev["ts"] = now
        except Exception as e:
            log.debug(f"[Health] cpu read: {e}")
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        rss_mb = round(kb / 1024, 1)
                        break
        except Exception as e:
            log.debug(f"[Health] rss read: {e}")
        return cpu_pct, rss_mb

    while not STOP_EVENT.is_set():
        mqtt_client.publish(
            f"{TOPIC_PREFIX}/bridge/uptime",
            str(int(time.time() - START_TIME)),
            qos=0, retain=True,
        )
        # v1.10.11: раз в 30с — на случай потери retain или если
        # WebUI подключился позже старта bridge.
        _publish_ping_mode()
        cpu, rss = _read_bridge_metrics()
        if cpu is not None:
            mqtt_client.publish(
                f"{TOPIC_PREFIX}/bridge/cpu_pct",
                str(cpu), qos=0, retain=True,
            )
        if rss is not None:
            mqtt_client.publish(
                f"{TOPIC_PREFIX}/bridge/rss_mb",
                str(rss), qos=0, retain=True,
            )
        STOP_EVENT.wait(30)


# ==================== MAIN ====================
def main():
    log.info("=" * 50)
    log.info(f"Tuya WiFi -> MQTT Bridge v{DISCOVERY_VERSION}")
    log.info(f"DEBUG: receive={DEBUG_CACHE_RECEIVE} status={DEBUG_CACHE_STATUS} "
             f"raw_dp={DEBUG_RAW_DP} mqtt_cmd={DEBUG_MQTT_CMD}")
    log.info(f"POLL_INTERVAL={POLL_INTERVAL}s  OFFLINE_TIMEOUT={OFFLINE_TIMEOUT}s  "
             f"EXPIRE_AFTER={AVAILABILITY_EXPIRE}s")
    log.info(f"CMD_POOL_SIZE={CMD_POOL_SIZE}")
    log.info(f"RATE_LIMIT: stream={MIN_CMD_INTERVAL_STREAM}s switch={MIN_CMD_INTERVAL_SWITCH}s")
    log.info(f"SOCKET_TIMEOUT: cmd={SOCKET_TIMEOUT_CMD}s worker={SOCKET_TIMEOUT_WORKER}s")
    log.info(f"WORKER_IDLE_SLEEP={WORKER_IDLE_SLEEP}s  "
             f"LOCK_ACQUIRE_TIMEOUT={LOCK_ACQUIRE_TIMEOUT}s")
    log.info(f"REPEAT_RESET_SECONDS={REPEAT_RESET_SECONDS}s")
    log.info(f"Brightness scale: 1..{HA_BRIGHT_MAX}")
    log.info(f"Debounce: {DEBOUNCE_BY_TYPE}")
    log.info(f"Config edit: {ALLOW_CONFIG_EDIT}  Validate: {VALIDATE_ON_EDIT}")
    log.info(f"CONFIG_FILE: {os.path.abspath(CONFIG_FILE)}")
    log.info("=" * 50)

    load_state_cache()
    load_battery_last_up()   # v1.10.0

    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()

    time.sleep(1)

    if CLEANUP_DISCOVERY:
        cleanup_discovery()

    with DEVICES_LOCK:
        devs_start = list(DEVICES)

    for dev in devs_start:
        # v1.10.13: try/except — одна сломанная конфигурация
        # (невалидный dps_map, preset_map и т.п.) не должна
        # блокировать запуск всего bridge.
        try:
            publish_discovery(dev)
        except Exception as e:
            log.error(f"[Discovery] {dev.get('name', '?')}: {e}")
        STOP_EVENT.wait(0.1)

    for dev in devs_start:
        dev_name = dev["name"]
        with STATE_LOCK:
            cached = dict(STATE_CACHE.get(dev_name, {}))
        if cached:
            publish_state(dev, cached)

    # v1.9.12: синхронизация реестра Discovery.
    # Первый запуск — сканирует MQTT на наши retained-топики;
    # удаляет те, что больше не публикуются (старые unique_id);
    # сохраняет актуальный набор в state/discovery_registry.json.
    try:
        sync_discovery_registry(scan_on_first=True)
    except Exception as e:
        log.warning(f"[Discovery] sync error: {e}")

    log.info("[Bridge] Discovery опубликован. Запуск воркеров...")

    # v1.10.11: публикуем режим пинга (для UI-warning про CAP_NET_RAW).
    _publish_ping_mode()

    started = 0
    for dev in devs_start:
        if dev.get("battery_powered"):
            # v1.9.0: батарейные — отдельный listener с ping-триггером
            target = run_battery_listener
            log.info(f"[Bridge] Battery listener: {dev['friendly_name']}")
        else:
            target = run_polling_device
        t = threading.Thread(target=target, args=(dev,), daemon=True, name=f"worker-{dev['name']}")
        t.start()
        started += 1
        STOP_EVENT.wait(0.3)

    threading.Thread(target=health_worker, daemon=True, name="health").start()
    threading.Thread(target=state_cache_worker, daemon=True, name="cache-writer").start()

    log.info(f"[Bridge] Запущено воркеров: {started} (из {len(devs_start)})")

    try:
        while not STOP_EVENT.is_set():
            STOP_EVENT.wait(1)
    except KeyboardInterrupt:
        pass

    log.info("[Bridge] Остановка...")
    STOP_EVENT.set()
    time.sleep(1)

    with DEVICE_CONN_LOCK:
        for name, entry in list(DEVICE_CONN.items()):
            try:
                _dev = entry[1] if isinstance(entry, tuple) else entry
                _dev.close()
            except Exception:
                pass
        DEVICE_CONN.clear()

    mqtt_client.publish(f"{TOPIC_PREFIX}/bridge/status", "offline", qos=1, retain=True)
    time.sleep(0.3)
    CMD_POOL.shutdown(wait=False)
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    log.info("[Bridge] Завершено")


def _signal_handler(signum, frame):
    log.info(f"[Signal] Получен сигнал {signum}")
    STOP_EVENT.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    try:
        main()
    except Exception as e:
        log.exception(f"[Main] Fatal: {e}")
        sys.exit(1)
