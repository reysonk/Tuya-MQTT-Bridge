#!/usr/bin/env python3
"""
Tuya WiFi -> MQTT Bridge with Home Assistant Discovery
Версия: 1.8.4

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
import shutil
import socket
import tempfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

import tinytuya
import paho.mqtt.client as mqtt

# Загружаем переменные окружения из .env
load_dotenv()

# ==================== SETTINGS ====================
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
DISCOVERY_PREFIX = os.getenv("DISCOVERY_PREFIX", "homeassistant")
TOPIC_PREFIX = os.getenv("TOPIC_PREFIX", "tuya")

# POLL_INTERVAL — как часто воркер делает status() под lock'ом.
POLL_INTERVAL = 15
BATTERY_REFRESH_INTERVAL = 300

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

DISCOVERY_CLEANUP_WAIT = 3
AVAILABILITY_EXPIRE = 120
OFFLINE_TIMEOUT = 120

USE_BATTERY_WORKER = False
CLEANUP_DISCOVERY = 0
DISCOVERY_VERSION = "1.8.4"
RETAINED_DUP_WINDOW = 10

# Пул команд. 32 — хватает на 40+ устройств.
# Команды на ОДНО устройство сериализуются per-device lock'ом.
CMD_POOL_SIZE = 32

LOG_LEVEL = "INFO"

MAX_CONSECUTIVE_904 = 3

# Rate limit: только для стримовых DP.
MIN_CMD_INTERVAL_STREAM = 0.15
MIN_CMD_INTERVAL_SWITCH = 0.0

BACKOFF_BASE = 3
BACKOFF_MAX = 30

# Fix 1.8.2: окно сброса счётчиков 914/905.
# Если между событиями прошло > REPEAT_RESET_SECONDS — счётчик сбрасывается.
REPEAT_RESET_SECONDS = 120

STATE_CACHE_FILE = "state/state_cache.json"
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

ALLOWED_TYPES = ("light", "switch", "climate", "sensor", "binary_sensor")
ALLOWED_VERSIONS = ("3.1", "3.2", "3.3", "3.4", "3.5")

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
if not USE_BATTERY_WORKER:
    battery_count = sum(1 for d in DEVICES if d.get("battery_powered"))
    if battery_count:
        log.info(f"[Config] Батарейных воркеров отключено: {battery_count}")


# ==================== CONNECTIONS ====================
# ОДИН persistent сокет на устройство. Tuya не держит два TCP -> 914.
DEVICE_CONN = {}
DEVICE_CONN_LOCK = threading.Lock()


def drop_device_conn(name):
    with DEVICE_CONN_LOCK:
        old = DEVICE_CONN.pop(name, None)
    if old:
        try:
            old.close()
        except Exception as e:
            log.debug(f"[Conn] close({name}): {e}")


def get_device_conn(dev):
    name = dev["name"]
    with DEVICE_CONN_LOCK:
        d = DEVICE_CONN.get(name)
        if d is None:
            d = tinytuya.Device(dev["id"], dev["ip"], dev["local_key"])
            d.set_version(float(dev["version"]))
            d.set_socketPersistent(True)
            d.set_socketTimeout(SOCKET_TIMEOUT_CMD)
            DEVICE_CONN[name] = d
        return d


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
    if not isinstance(ip_str, str):
        return False
    parts = ip_str.strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            return False
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
    if not isinstance(n, str):
        return False
    n = n.strip()
    if not n or len(n) > 100:
        return False
    return all(c.isalnum() or c in "_-" for c in n)


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
    with DEVICES_LOCK:
        known_ips = {d.get("ip") for d in DEVICES if d.get("ip")}
    log.info(f"[Scan] Сканирую {subnet_prefix}.0/24 ({len(ips)} IP, {SCAN_WORKERS} потоков)")

    def scan_one(ip):
        ms = _scan_ip(ip)
        if ms is None:
            return None
        entry = {"ip": ip, "ms": ms}
        if ip in known_ips:
            entry["known"] = True
        else:
            entry["known"] = False
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
    with DEVICES_LOCK:
        devs = list(DEVICES)
    for dev in devs:
        name = dev["name"]
        dtype = dev.get("type", "sensor")
        if dtype == "light":
            ids.add(f"{name}_light")
        if dtype == "climate":
            ids.add(f"{name}_climate")
        for dp_str, info in dev.get("dps_map", {}).items():
            comp = info.get("component")
            ent = info.get("name", f"dp_{dp_str}")
            if comp in ("switch", "select", "number"):
                ids.add(f"{name}_{ent}")
            elif comp in ("sensor", "binary_sensor"):
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

    payload = msg.payload.decode("utf-8").strip()

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
            with DEBOUNCE_LOCK:
                DEBOUNCE.pop(key, None)

        t = threading.Timer(window, fire)
        t.daemon = True
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
    with _state_dirty_lock:
        if not _state_dirty:
            return

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

        with _state_dirty_lock:
            _state_dirty = False
    except Exception as e:
        log.warning(f"[Cache] Не удалось сохранить: {e} (флаг dirty сохранён)")


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

    dp_state, info_state = find_dp_by_name(dev, "switch_led")
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

    dp_color, info_color = find_dp_by_name(dev, "colour_data")
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
    import re as _re
    m = _re.search(r'"request_id"\s*:\s*"([^"]+)"', payload_str)
    if m:
        return m.group(1)
    return None


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

    request_id = data.get("request_id") or req_id
    dev_name = data.get("device")
    changes = data.get("changes", {})
    validate = data.get("validate", VALIDATE_ON_EDIT)

    if not dev_name or not isinstance(changes, dict) or not changes:
        _publish_edit_result(dev_name, False, "device and changes required",
                             request_id=request_id)
        return

    ALLOWED_FIELDS = {"ip", "local_key", "version"}
    filtered = {k: v for k, v in changes.items() if k in ALLOWED_FIELDS}

    if not filtered:
        _publish_edit_result(dev_name, False, "no allowed fields",
                             request_id=request_id)
        return

    if "ip" in filtered:
        if not _is_valid_ip(filtered["ip"]):
            _publish_edit_result(dev_name, False, f"invalid ip: {filtered['ip']}",
                                 request_id=request_id)
            return
        filtered["ip"] = str(filtered["ip"]).strip()

    if "local_key" in filtered:
        if not _is_valid_key(filtered["local_key"]):
            _publish_edit_result(dev_name, False, "invalid local_key",
                                 request_id=request_id)
            return
        filtered["local_key"] = str(filtered["local_key"]).strip()

    if "version" in filtered:
        if not _is_valid_version(filtered["version"]):
            _publish_edit_result(dev_name, False, f"invalid version: {filtered['version']}",
                                 request_id=request_id)
            return
        filtered["version"] = str(filtered["version"]).strip()

    with DEVICES_LOCK:
        dev = DEVICE_INDEX.get(dev_name)
    if not dev:
        _publish_edit_result(dev_name, False, "device not found",
                             request_id=request_id)
        return

    if validate:
        new_ip = filtered.get("ip", dev["ip"])
        new_key = filtered.get("local_key", dev["local_key"])
        new_ver = filtered.get("version", dev["version"])

        log.info(f"[EditConfig] {dev_name}: валидация {new_ip}:{new_ver}...")
        ok, err = _validate_device(dev["id"], new_ip, new_key, new_ver, VALIDATE_TIMEOUT)

        if not ok:
            log.warning(f"[EditConfig] {dev_name}: валидация не прошла: {err}")
            _publish_edit_result(dev_name, False, f"validation failed: {err}",
                                 request_id=request_id)
            return

        log.info(f"[EditConfig] {dev_name}: валидация OK")

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_devices = json.load(f)
    except Exception as e:
        _publish_edit_result(dev_name, False, f"read config failed: {e}",
                             request_id=request_id)
        return

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

    with DEVICES_LOCK:
        dev.update(filtered)
    drop_device_conn(dev_name)
    request_worker_restart(dev_name)

    _publish_edit_result(dev_name, True, None, changes=filtered,
                         request_id=request_id)


def _publish_edit_result(dev_name, ok, error, changes=None, request_id=None):
    payload = {
        "request_id": request_id,
        "device": dev_name,
        "ok": bool(ok),
        "error": error,
        "changes": changes or {},
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

    request_id = data.get("request_id") or req_id
    dev_name = data.get("device")

    if not dev_name:
        _publish_delete_result(None, False, "device required", request_id=request_id)
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            all_devices = json.load(f)
    except Exception as e:
        # v1.8.4: req_id, а не None — WebUI сматчит ответ и не словит timeout.
        _publish_delete_result(request_id=req_id, ok=False, error=f"read config failed: {e}")
        return

    before = len(all_devices)
    all_devices = [d for d in all_devices if d.get("name") != dev_name]
    if len(all_devices) == before:
        _publish_delete_result(request_id, False, "device not found")
        return

    _backup_config()

    if not _write_config_atomic(all_devices):
        _publish_delete_result(request_id, False, "write config failed")
        return

    with DEVICES_LOCK:
        dev = DEVICE_INDEX.pop(dev_name, None)
        if dev:
            try:
                DEVICES.remove(dev)
            except ValueError:
                pass

    if dev:
        try:
            _remove_discovery_for_device(dev)
        except Exception as e:
            log.warning(f"[Delete] discovery cleanup {dev_name}: {e}")

    drop_device_conn(dev_name)
    with STATE_LOCK:
        STATE_CACHE.pop(dev_name, None)

    log.info(f"[Delete] {dev_name}: удалено из конфига")

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
    dev_name = dev["name"]
    dtype = dev.get("type", "sensor")

    topics = []
    if dtype == "light":
        topics.append(f"{DISCOVERY_PREFIX}/light/{dev_name}_light/config")
    elif dtype == "climate":
        topics.append(f"{DISCOVERY_PREFIX}/climate/{dev_name}_climate/config")

    for dp_str, info in dev.get("dps_map", {}).items():
        comp = info.get("component")
        ent = info.get("name", f"dp_{dp_str}")
        if comp == "switch":
            topics.append(f"{DISCOVERY_PREFIX}/switch/{dev_name}_{ent}/config")
        elif comp == "select":
            topics.append(f"{DISCOVERY_PREFIX}/select/{dev_name}_{ent}/config")
        elif comp == "number":
            topics.append(f"{DISCOVERY_PREFIX}/number/{dev_name}_{ent}/config")
        elif comp in ("sensor", "binary_sensor"):
            topics.append(f"{DISCOVERY_PREFIX}/{comp}/{dev_name}_{ent}/config")
    if dev.get("dps_map", {}).get("6", {}).get("component") == "phase_a":
        for suffix in ("voltage", "current", "power"):
            topics.append(f"{DISCOVERY_PREFIX}/sensor/{dev_name}_output_{suffix}/config")

    for t in topics:
        mqtt_client.publish(t, payload=None, qos=1, retain=True)

    mqtt_client.publish(f"{TOPIC_PREFIX}/{dev_name}/status", payload=None, qos=1, retain=True)
    mqtt_client.publish(f"{TOPIC_PREFIX}/{dev_name}/last_seen", payload=None, qos=0, retain=True)
    mqtt_client.publish(f"{TOPIC_PREFIX}/{dev_name}/cache_snapshot", payload=None, qos=0, retain=True)
    mqtt_client.publish(f"{TOPIC_PREFIX}/light/{dev_name}/state", payload=None, qos=0, retain=True)
    mqtt_client.publish(f"{TOPIC_PREFIX}/switch/{dev_name}/state", payload=None, qos=0, retain=True)
    mqtt_client.publish(f"{TOPIC_PREFIX}/climate/{dev_name}/mode/state", payload=None, qos=0, retain=True)
    mqtt_client.publish(f"{TOPIC_PREFIX}/climate/{dev_name}/temp/state", payload=None, qos=0, retain=True)
    mqtt_client.publish(f"{TOPIC_PREFIX}/climate/{dev_name}/current/state", payload=None, qos=0, retain=True)
    mqtt_client.publish(f"{TOPIC_PREFIX}/climate/{dev_name}/preset/state", payload=None, qos=0, retain=True)


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

    added = 0
    updated = 0
    skipped = 0
    errors = []

    for dev in new_devices:
        if not isinstance(dev, dict):
            skipped += 1
            continue

        name = dev.get("name", "").strip()
        dev_id = dev.get("id", "").strip()
        dev_ip = dev.get("ip", "").strip()
        dev_key = dev.get("local_key", "").strip()
        dev_ver = str(dev.get("version", "")).strip()
        dev_type = dev.get("type", "").strip()

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

        entry = {
            "id": dev_id,
            "name": name,
            "friendly_name": dev.get("friendly_name", name),
            "ip": dev_ip,
            "local_key": dev_key,
            "version": dev_ver,
            "type": dev_type,
            "model": dev.get("model", ""),
            "battery_powered": bool(dev.get("battery_powered", False)),
            "enabled": bool(dev.get("enabled", True)),
            "dps_map": dev.get("dps_map", {}),
        }

        for k in ("presets", "preset_map", "min_temp", "max_temp", "temp_step"):
            if k in dev:
                entry[k] = dev[k]

        if name in existing_names:
            if overwrite:
                for i, d in enumerate(all_devices):
                    if d.get("name") == name:
                        all_devices[i] = entry
                        updated += 1
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
            added += 1

    if added == 0 and updated == 0:
        _publish_import_result(request_id, False, "nothing to import", errors=errors,
                               added=0, updated=0, skipped=skipped)
        return

    _backup_config()

    if not _write_config_atomic(all_devices):
        _publish_import_result(request_id, False, "write config failed")
        return

    log.info(f"[Import] added={added}, updated={updated}, skipped={skipped}")

    refresh_our_ids()  # v1.8.4 import

    for dev in new_devices:
        name = dev.get("name", "").strip()
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
            if name in DEVICE_INDEX:
                DEVICE_INDEX[name].update(matched)
                drop_device_conn(name)
                request_worker_restart(name)
            elif matched.get("enabled", True):
                DEVICES.append(matched)
                DEVICE_INDEX[name] = matched
                with STATE_LOCK:
                    STATE_CACHE.setdefault(name, {})
                try:
                    publish_discovery(matched)
                    target = run_battery_device if matched.get("battery_powered") else run_polling_device
                    t = threading.Thread(target=target, args=(matched,),
                                         daemon=True, name=f"worker-{name}")
                    t.start()
                    log.info(f"[Import] {name}: воркер запущен")
                except Exception as e:
                    log.warning(f"[Import] {name}: worker error: {e}")

    _publish_import_result(request_id, True, None, errors=errors,
                           added=added, updated=updated, skipped=skipped)


def _publish_import_result(request_id, ok, error, errors=None, added=0, updated=0, skipped=0):
    payload = {
        "request_id": request_id,
        "ok": bool(ok),
        "error": error,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "errors": errors or [],
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

    request_id = data.get("request_id") or req_id
    subnet = data.get("subnet", "").strip()

    if not subnet:
        with DEVICES_LOCK:
            if DEVICES:
                first_ip = DEVICES[0].get("ip", "")
                parts = first_ip.split(".")
                if len(parts) == 4:
                    subnet = ".".join(parts[:3])
    if not subnet or subnet.count(".") != 2:
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

    if dev_type == "light":
        publish_light(device, device_info)
    elif dev_type == "switch":
        publish_switches(device, device_info)
        publish_selects(device, device_info)
        publish_numbers(device, device_info)
        publish_sensors(device, device_info)
    elif dev_type == "climate":
        publish_climate(device, device_info)
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

    dp_bright, info_bright = find_dp_by_name(device, "bright_value")
    dp_temp, info_temp = find_dp_by_name(device, "temp_value")
    dp_color, info_color = find_dp_by_name(device, "colour_data")

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


def publish_climate(device, device_info):
    dev_name = device["name"]
    unique_id = f"{dev_name}_climate"
    avail = base_availability(dev_name)

    presets = device.get("presets", [])
    preset_map = device.get("preset_map", {})
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
        if component in ("switch", "light", "preset", "select", "phase_a", "number"):
            continue
        entity_name = info.get("name", f"dp_{dp_str}")
        unique_id = f"{dev_name}_{entity_name}"
        state_topic = f"{TOPIC_PREFIX}/{dev_type}/{dev_name}/dps/{dp_str}/state"

        config = {
            "name": f"{device['friendly_name']} {entity_name}",
            "unique_id": unique_id,
            "state_topic": state_topic,
            "expire_after": AVAILABILITY_EXPIRE,
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
            if info.get("device_class") == "motion":
                config["expire_after"] = min(60, AVAILABILITY_EXPIRE)

        topic = f"{DISCOVERY_PREFIX}/{component}/{unique_id}/config"
        mqtt_client.publish(topic, json.dumps(config), retain=True)
        log.info(f"[Discovery] {component}: {device['friendly_name']} / {entity_name}")


# ==================== STATE PUBLISHING ====================
def publish_state(device, dps):
    dev_type = device.get("type", "sensor")
    dev_name = device["name"]
    dps_map = device["dps_map"]

    if dps:
        _cache_update(dev_name, {str(k): v for k, v in dps.items()})

    with STATE_LOCK:
        cached = dict(STATE_CACHE.get(dev_name, {}))

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

    for dp_str, info in dps_map.items():
        if dp_str in cached:
            _publish_sensor_value(dev_type, dev_name, dp_str, info, cached[dp_str])


def _publish_sensor_value(dev_type, dev_name, dp_str, info, raw_val):
    component = info.get("component", "sensor")
    scale = info.get("scale", 0)
    name = info.get("name", "")

    if component == "binary_sensor":
        if isinstance(raw_val, bool):
            payload = "ON" if raw_val else "OFF"
        elif isinstance(raw_val, (int, float)):
            if name in ("fault", "problema"):
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

    if "6" in cached:
        try:
            v, c, p = parse_phase_a(str(cached["6"]))
            if v is not None:
                cached["6_voltage"] = round(v, 1)
            if c is not None:
                cached["6_current"] = round(c, 3)
            if p is not None:
                cached["6_power"] = round(p, 3)
        except Exception as e:
            log.debug(f"[Snapshot] {name}: phase_a parse failed: {e}")

    if not cached:
        return
    topic = f"{TOPIC_PREFIX}/{name}/cache_snapshot"
    try:
        mqtt_client.publish(topic, json.dumps(cached, ensure_ascii=False),
                            qos=0, retain=True)
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
def _backoff_seconds(failures):
    if failures <= 1:
        return BACKOFF_BASE
    exp = min(failures - 1, 4)
    return min(BACKOFF_MAX, BACKOFF_BASE * (2 ** exp))


def _read_socket(d, timeout):
    old_to = getattr(d, "socketTimeout", None)
    try:
        if old_to is not None:
            d.set_socketTimeout(timeout)
        return d.receive()
    finally:
        if old_to is not None:
            try:
                d.set_socketTimeout(old_to)
            except Exception:
                pass


def _status_socket(d, timeout=SOCKET_TIMEOUT_CMD):
    old_to = getattr(d, "socketTimeout", None)
    try:
        if old_to is not None:
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
    consecutive_failures = 0
    first_iteration = True

    while not STOP_EVENT.is_set():
        if consume_restart_flag(name):
            log.info(f"[Worker] {name}: restart flag — перезапуск соединения")
            drop_device_conn(name)
            consecutive_failures = 0
            last_status_ok = 0
            first_iteration = True
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
            else:
                consecutive_904 = 0
                consecutive_failures = 0
                dps = data.get("dps", data)
                if dps and isinstance(dps, dict):
                    # Fix 1.8.2: успешный ответ — сбрасываем счётчики 914/905
                    _reset_repeat_counters(name)
                    if DEBUG_RAW_DP:
                        log.info(f"[Worker] {name}: receive dps={dps}")
                    publish_state(dev, dps)
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
                elif poll_data and isinstance(poll_data, dict):
                    consecutive_904 = 0
                    consecutive_failures = 0
                    dps = poll_data.get("dps", poll_data)
                    if dps and isinstance(dps, dict):
                        # Fix 1.8.2: успешный ответ — сбрасываем счётчики 914/905
                        _reset_repeat_counters(name)
                        if DEBUG_RAW_DP:
                            log.info(f"[Worker] {name}: status dps={dps}")
                        publish_state(dev, dps)
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


def run_battery_device(dev):
    name = dev["name"]
    log.info(f"[Worker] battery: {name}")
    last_status = 0
    last_online = None
    consecutive_failures = 0
    last_real_data = time.time()

    while not STOP_EVENT.is_set():
        if consume_restart_flag(name):
            log.info(f"[Worker] {name}: restart flag")
            drop_device_conn(name)
            consecutive_failures = 0
            _reset_repeat_counters(name)

        if consume_status_request(name):
            last_status = 0

        with DEVICES_LOCK:
            if name not in DEVICE_INDEX:
                log.info(f"[Worker] {name}: удалено из конфига, остановка")
                break

        lock = get_device_cmd_lock(name)

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

        if data and isinstance(data, dict):
            if "Error" in data:
                err_code = str(data.get("Err", ""))
                if err_code == "914":
                    _log_914_once(name, "receive")
                elif err_code == "905":
                    _log_905_once(name, "receive")
                elif err_code not in ("901", "902"):
                    log.debug(f"[Worker] {name}: Tuya error {err_code}: {data.get('Error','')}")
            else:
                consecutive_failures = 0
                dps = data.get("dps", data)
                if dps and isinstance(dps, dict):
                    # Fix 1.8.2: успешный ответ — сбрасываем счётчики 914/905
                    _reset_repeat_counters(name)
                    if DEBUG_RAW_DP:
                        log.info(f"[Worker] {name}: receive dps={dps}")
                    publish_state(dev, dps)
                    publish_last_seen(dev)
                    publish_cache_snapshot(dev)
                    last_real_data = time.time()
                    last_status = time.time()
                    if last_online is not True:
                        publish_availability(dev, True)
                        last_online = True

        now = time.time()
        if now - last_status > BATTERY_REFRESH_INTERVAL:
            got_lock2 = lock.acquire(timeout=LOCK_ACQUIRE_TIMEOUT)
            if got_lock2:
                try:
                    try:
                        d = get_device_conn(dev)
                        d.status(nowait=True)
                        last_status = now
                    except Exception:
                        pass
                finally:
                    lock.release()

        if time.time() - last_real_data > OFFLINE_TIMEOUT:
            if last_online is not False:
                publish_availability(dev, False)
                last_online = False

        if data is None:
            if STOP_EVENT.wait(WORKER_IDLE_SLEEP):
                break

    log.info(f"[Worker] {name}: остановлен")


# ==================== HEALTH ====================
def health_worker():
    while not STOP_EVENT.is_set():
        mqtt_client.publish(
            f"{TOPIC_PREFIX}/bridge/uptime",
            str(int(time.time() - START_TIME)),
            qos=0, retain=True,
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
    log.info(f"BACKOFF_BASE={BACKOFF_BASE}s  BACKOFF_MAX={BACKOFF_MAX}s")
    log.info(f"REPEAT_RESET_SECONDS={REPEAT_RESET_SECONDS}s")
    log.info(f"Brightness scale: 1..{HA_BRIGHT_MAX}")
    log.info(f"Debounce: {DEBOUNCE_BY_TYPE}")
    log.info(f"Config edit: {ALLOW_CONFIG_EDIT}  Validate: {VALIDATE_ON_EDIT}")
    log.info(f"CONFIG_FILE: {os.path.abspath(CONFIG_FILE)}")
    log.info("=" * 50)

    load_state_cache()

    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()

    time.sleep(1)

    if CLEANUP_DISCOVERY:
        cleanup_discovery()

    with DEVICES_LOCK:
        devs_start = list(DEVICES)

    for dev in devs_start:
        publish_discovery(dev)
        STOP_EVENT.wait(0.1)

    for dev in devs_start:
        dev_name = dev["name"]
        with STATE_LOCK:
            cached = dict(STATE_CACHE.get(dev_name, {}))
        if cached:
            publish_state(dev, cached)

    log.info("[Bridge] Discovery опубликован. Запуск воркеров...")

    started = 0
    skipped = 0
    for dev in devs_start:
        if dev.get("battery_powered") and not USE_BATTERY_WORKER:
            log.info(f"[Bridge] Пропускаю батарейное устройство: {dev['friendly_name']}")
            skipped += 1
            continue
        target = run_battery_device if dev.get("battery_powered") else run_polling_device
        t = threading.Thread(target=target, args=(dev,), daemon=True, name=f"worker-{dev['name']}")
        t.start()
        started += 1
        STOP_EVENT.wait(0.3)

    threading.Thread(target=health_worker, daemon=True, name="health").start()
    threading.Thread(target=state_cache_worker, daemon=True, name="cache-writer").start()

    log.info(f"[Bridge] Запущено воркеров: {started}, пропущено батарейных: {skipped}")

    try:
        while not STOP_EVENT.is_set():
            STOP_EVENT.wait(1)
    except KeyboardInterrupt:
        pass

    log.info("[Bridge] Остановка...")
    STOP_EVENT.set()
    time.sleep(1)

    with DEVICE_CONN_LOCK:
        for name, d in list(DEVICE_CONN.items()):
            try:
                d.close()
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