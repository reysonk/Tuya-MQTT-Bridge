#!/usr/bin/env python3
"""
Tuya Bridge WebUI — отдельный контейнер.
Версия: 1.20.1
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
import shutil
import socket as _socket
import queue as _queue
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import paho.mqtt.client as mqtt

try:
    import tinytuya
    HAS_TINYTUYA = True
except ImportError:
    HAS_TINYTUYA = False

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ==================== SETTINGS ====================
load_dotenv()

MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
TOPIC_PREFIX = os.getenv("TOPIC_PREFIX", "tuya")
WEBUI_PORT = int(os.getenv("WEBUI_PORT", 5386))
WEBUI_HOST = os.getenv("WEBUI_HOST", "0.0.0.0")
WEBUI_VERSION = os.getenv("WEBUI_VERSION", "1.20.1")

CONFIG_FILE = "devices_config.json"
LOG_FILE = "logs/bridge.log"
DB_FILE = "webui_state/analytics.db"
TINYTUYA_DEVICES_FILE = "webui_state/tinytuya_devices.json"
TUYA_CLOUD_CACHE_FILE = "webui_state/tuya_cloud_cache.json"
TUYA_LOCAL_DB_DIR = "webui_state/tuya-local-db"
TUYA_LOCAL_YAML_DIR = "webui_state/tuya-local-db/custom_components/tuya_local/devices"
TUYA_LOCAL_DB_OLD_DIR = "/app/tuya-local-db"
TUYA_LOCAL_TARBALL_URL = "https://github.com/make-all/tuya-local/archive/refs/heads/main.tar.gz"

LOG_POLL_INTERVAL = 1.0
LOG_HISTORY_LINES = 1000

ANALYTICS_ENABLED = True
STATUS_HISTORY_ENABLED = True

RETENTION_DAYS = 3
SNAPSHOT_INTERVAL = 60
FLUSH_INTERVAL = 30
HOURLY_INTERVAL = 60

LATENCY_INTERVAL = 900
LATENCY_INITIAL_DELAY = 5
LATENCY_PING_TIMEOUT = 1
LATENCY_PING_COMMAND = "ping"

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

SSE_MAX_SUBSCRIBERS = 50
SSE_IDLE_TIMEOUT = 60
SSE_BACKLOG = 100

SAFE_PORTS = [
    (22, "SSH"), (23, "Telnet"), (53, "DNS"), (80, "HTTP"), (443, "HTTPS"),
    (554, "RTSP"), (631, "IPP"), (1883, "MQTT"), (3389, "RDP"), (5000, "UPnP/AV"),
    (5001, "Synology"), (8008, "Chromecast"), (8009, "Chromecast"),
    (8080, "HTTP-alt"), (8443, "HTTPS-alt"), (9100, "Printer"),
    (32400, "Plex"), (62078, "iPhone"),
]

MAC_VENDOR_MAP = {
    "7C:DF:A1": "Tuya", "A4:C1:38": "Tuya", "68:57:2D": "Tuya",
    "10:52:1C": "Tuya", "18:69:D8": "Tuya", "44:BB:3B": "Tuya",
    "D4:CA:6D": "MikroTik", "B8:27:EB": "Raspberry Pi", "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi", "84:0D:8E": "Espressif", "EC:FA:BC": "Espressif",
    "C4:4F:33": "Espressif", "5C:CF:7F": "Espressif", "DC:4F:22": "Espressif",
    "24:0A:C4": "Espressif", "30:AE:A4": "Espressif", "3C:71:BF": "Espressif",
    "A0:20:A6": "Espressif", "B4:E6:2D": "Espressif", "00:17:88": "Philips Hue",
    "F4:F5:D8": "Google", "54:60:09": "Google", "1C:F2:9A": "Google",
    "00:1A:22": "Apple", "F0:18:98": "Apple", "AC:BC:32": "Apple",
}

JUNK_DP_CODES = {
    "refresh", "clear_energy", "clr_all_energy",
    "leakagecurr_test", "charge_energy",
    "alarm_set_1", "alarm_set_2",
    "breaker_id", "sn",
    "scene_data", "music_data", "control_data",
    "voltage_coe", "electric_coe", "power_coe", "electricity_coe",
    "test_bit",
    "work_days", "holiday_days_set",
    "factory_reset",
}

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tuya-webui")

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
         "bridge_started_at": 0}
STATE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()

DEVICE_META = {}
DEVICE_META_LOCK = threading.Lock()
DEVICE_HISTORY_CACHE = {}

PENDING_REQUESTS = {}
PENDING_LOCK = threading.Lock()

REBUILD_STATE = {
    "running": False, "current": 0, "total": 0, "device": "",
    "errors": [], "started_at": 0, "finished_at": 0, "ok": None,
}
REBUILD_LOCK = threading.Lock()

LATENCY_REFRESH_STATE = {
    "running": False, "current": 0, "total": 0, "device": "",
    "started_at": 0, "finished_at": 0, "ok": None,
}
LATENCY_REFRESH_STATE_LOCK = threading.Lock()


def load_device_meta():
    global DEVICE_META
    new_meta = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            devices = json.load(f)
        for d in devices:
            if d.get("enabled", True):
                meta = {
                    "friendly_name": d.get("friendly_name", d["name"]),
                    "type": d.get("type", "unknown"),
                    "model": d.get("model", ""),
                    "ip": d.get("ip", ""),
                    "version": d.get("version", ""),
                    "battery_powered": d.get("battery_powered", False),
                    "enabled": d.get("enabled", True),
                    "tuya_id": d.get("id", ""),
                    "local_key": d.get("local_key", ""),
                    "dps_map": copy.deepcopy(d.get("dps_map", {})),
                }
                if d.get("type") == "climate":
                    meta["presets"] = d.get("presets", [])
                    meta["preset_map"] = d.get("preset_map", {})
                    meta["min_temp"] = d.get("min_temp")
                    meta["max_temp"] = d.get("max_temp")
                    meta["temp_step"] = d.get("temp_step")
                if meta["dps_map"].get("6", {}).get("component") == "phase_a":
                    meta["dps_map"]["6_voltage"] = {"name": "voltage", "component": "sensor",
                        "device_class": "voltage", "unit": "V", "state_class": "measurement"}
                    meta["dps_map"]["6_current"] = {"name": "current", "component": "sensor",
                        "device_class": "current", "unit": "A", "state_class": "measurement"}
                    meta["dps_map"]["6_power"] = {"name": "power", "component": "sensor",
                        "device_class": "power", "unit": "kW", "state_class": "measurement"}
                new_meta[d["name"]] = meta
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
        return {m.get("ip") for m in DEVICE_META.values() if m.get("ip")}


def _humanize_bridge_error(err):
    if not err:
        return "Неизвестная ошибка"
    e = str(err)
    low = e.lower()
    if "914" in e or "check device key or version" in low:
        return ("⚠️ Устройство занято (уже подключено к bridge на порту 6668). "
                "Это не про key или version — просто порт занят bridge'ом. "
                "Изменения сохранены в конфиг и применятся при следующем reconnect.")
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
_state_buffer = []
_latency_buffer = []
_last_snapshot = {}
_last_snapshot_lock = threading.Lock()


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
            _db_conn.execute("""CREATE TABLE IF NOT EXISTS state_history (
                ts INTEGER NOT NULL, dev TEXT NOT NULL, dp TEXT NOT NULL, value TEXT NOT NULL)""")
            _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_state_ts ON state_history(ts)")
            _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_state_dev_dp ON state_history(dev, dp)")
            _db_conn.execute("""CREATE TABLE IF NOT EXISTS hourly_online_count (
                hour_ts INTEGER PRIMARY KEY, online INTEGER NOT NULL, total INTEGER NOT NULL)""")
    log.info(f"[DB] SQLite: {DB_FILE}")


def db_insert_status(ts, dev, status):
    if not STATUS_HISTORY_ENABLED: return
    with _db_lock: _status_buffer.append((ts, dev, status))


def db_insert_snapshot(dev, cache):
    if not ANALYTICS_ENABLED: return
    now = int(time.time())
    with _db_lock:
        for dp, val in cache.items():
            dp = str(dp)
            try: sval = json.dumps(val, ensure_ascii=False)
            except Exception: sval = str(val)
            key = (dev, dp)
            with _last_snapshot_lock: last = _last_snapshot.get(key)
            if last is not None:
                last_ts, last_sval = last
                if last_sval == sval and now - last_ts < SNAPSHOT_INTERVAL:
                    continue
            _state_buffer.append((now, dev, dp, sval))
            with _last_snapshot_lock: _last_snapshot[key] = (now, sval)


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
                if ANALYTICS_ENABLED and _state_buffer:
                    _db_conn.executemany("INSERT INTO state_history VALUES (?,?,?,?)", _state_buffer)
                    _state_buffer.clear()
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
                if ANALYTICS_ENABLED:
                    _db_conn.execute("DELETE FROM state_history WHERE ts < ?", (cutoff,))
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
            if ANALYTICS_ENABLED:
                cur = _db_conn.execute("DELETE FROM state_history WHERE ts < ?", (cutoff,))
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
    with STATE_LOCK:
        total = len(STATE["devices"])
        online = sum(1 for d in STATE["devices"].values() if d.get("status") == "online")
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


def db_query_timeline_total(period_hours=24):
    if not ANALYTICS_ENABLED: return 0
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            cur = _db_conn.execute("SELECT COUNT(*) FROM status_events WHERE ts>=?", (cutoff,))
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


def db_query_flaps_hourly(period_hours=24):
    """v1.18.8: количество переходов online↔offline по часам за период."""
    if not ANALYTICS_ENABLED: return []
    cutoff = int(time.time()) - period_hours * 3600
    with _db_lock:
        try:
            cur = _db_conn.execute(
                "SELECT (ts/3600)*3600 AS hour_ts, COUNT(*) "
                "FROM status_events WHERE ts>=? "
                "GROUP BY hour_ts ORDER BY hour_ts",
                (cutoff,)
            )
            return [{"ts": r[0], "flaps": r[1]} for r in cur.fetchall()]
        except: return []


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
        header = struct.pack("bbHHh", 8, 0, 0, pid, seq)
        payload = struct.pack("d", time.time())
        chksum = _icmp_checksum(header + payload)
        header = struct.pack("bbHHh", 8, 0, _socket.htons(chksum), pid, seq)
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


# ==================== LATENCY WORKER ====================
def _do_latency_round():
    with STATE_LOCK:
        devices = list(STATE["devices"].keys())
    meta_snap = snapshot_device_meta()
    if not devices:
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
        ms = measure_latency(ip)
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
            ms = measure_latency(ip)
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
        try:
            _do_latency_round()
        except Exception as e:
            log.warning(f"[Latency] error: {e}")
        if STOP_EVENT.wait(LATENCY_INTERVAL): break
    log.info("[Latency] Воркер остановлен")


_LATENCY_REFRESH_LOCK = threading.Lock()
_LATENCY_REFRESH_RUNNING = [False]


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


def detect_version(ip, dev_id, local_key, timeout=PROBE_TIMEOUT_PER_VERSION):
    if not HAS_TINYTUYA:
        return None, {}
    for v in PROBE_VERSIONS:
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
                log.info(f"[Probe] {dev_id}@{ip}: version={v} (dps={len(r['dps'])})")
                return v, r.get("dps", {})
        except Exception as e:
            log.debug(f"[Probe] {dev_id}@{ip} v={v}: {e}")
            continue
    log.warning(f"[Probe] {dev_id}@{ip}: ни одна версия не ответила")
    return None, {}


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


def match_dps_to_codes(local_dps, cloud_status_meta, cloud_current_values):
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

    def dp_sort_key(k):
        try: return (0, int(k))
        except Exception: return (1, str(k))

    dp_ids = sorted(local_dps.keys(), key=dp_sort_key)
    pending = []
    for dp_id in dp_ids:
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
            result[dp_id] = candidates[0]
            used_codes.add(candidates[0]["code"])
        elif len(candidates) > 1:
            pending.append((dp_id, candidates))

    for dp_id, candidates in pending:
        candidates = [c for c in candidates if c.get("code") not in used_codes]
        if not candidates:
            continue
        best = _heuristic_pick(candidates)
        if best:
            result[dp_id] = best
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
def _get_arp_table():
    arp = {}
    try:
        out = subprocess.check_output(["ip", "neigh", "show"], timeout=2).decode(errors="replace")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and "lladdr" in parts:
                ip = parts[0]; idx = parts.index("lladdr")
                if idx + 1 < len(parts):
                    mac = parts[idx + 1]
                    if mac != "FAILED":
                        arp[ip] = mac.upper()
    except Exception:
        pass
    if not arp:
        try:
            out = subprocess.check_output(["arp", "-an"], timeout=2).decode(errors="replace")
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[0].startswith("("):
                    ip = parts[0].strip("()"); mac = parts[3]
                    if mac != "<incomplete>":
                        arp[ip] = mac.upper()
        except Exception:
            pass
    return arp


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


def _lookup_mac_vendor(mac):
    if not mac or len(mac) < 8: return None
    return MAC_VENDOR_MAP.get(mac[:8].upper())


def _ip_sort_key(ip):
    try:
        return tuple(int(p) for p in ip.split("."))
    except Exception:
        return (999, 999, 999, 999)


def _scan_extended(subnet_prefix):
    import concurrent.futures
    arp = _get_arp_table()
    known_ips = get_known_ips()
    ips = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
    log.info(f"[Scan] {subnet_prefix}.0/24 (known: {len(known_ips)}, ping mode: {_PING_MODE[0]})")

    def scan_one(ip):
        is_known = ip in known_ips
        ms = measure_latency(ip)
        if ms is None:
            return None
        entry = {"ip": ip, "ms": ms, "known": is_known}
        if ip in arp:
            entry["mac"] = arp[ip]
            entry["vendor"] = _lookup_mac_vendor(arp[ip])
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        for r in pool.map(scan_one, ips):
            if r:
                results.append(r)
    results.sort(key=lambda x: _ip_sort_key(x["ip"]))
    log.info(f"[Scan] Найдено {len(results)} устройств")
    return results


# ==================== LOG TAIL ====================
_log_buffer = []
_log_buffer_lock = threading.Lock()
_log_seq = 0
_log_seq_lock = threading.Lock()
_sse_subscribers = []
_sse_subscribers_lock = threading.Lock()
LOG_BUFFER_MAX = 5000


def _read_initial_log():
    global _log_seq
    if not os.path.exists(LOG_FILE): return
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-LOG_HISTORY_LINES:]:
            _append_log_line(line.rstrip("\n"))
    except Exception as e:
        log.warning(f"[Log] init: {e}")


def _append_log_line(line):
    global _log_seq
    if not line: return
    with _log_seq_lock:
        _log_seq += 1; seq = _log_seq
    level = "INFO"
    for lvl in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        if f"[{lvl}]" in line:
            level = lvl; break
    line_ts = parse_log_line_timestamp(line)
    item = {"seq": seq, "ts": line_ts if line_ts else int(time.time()),
            "level": level, "msg": line}
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


def _log_tailer():
    f = None; current_inode = None
    try:
        while not STOP_EVENT.is_set():
            try:
                if f is None:
                    if not os.path.exists(LOG_FILE):
                        if STOP_EVENT.wait(LOG_POLL_INTERVAL): break
                        continue
                    f = open(LOG_FILE, "r", encoding="utf-8", errors="replace")
                    try: current_inode = os.fstat(f.fileno()).st_ino
                    except OSError: current_inode = None
                    f.seek(0, 2)
                try:
                    st = os.stat(LOG_FILE)
                    if current_inode is not None and st.st_ino != current_inode:
                        f.close(); f = None; current_inode = None; continue
                    if st.st_size < f.tell():
                        f.close(); f = None; current_inode = None; continue
                except OSError:
                    f.close(); f = None; current_inode = None; continue
                line = f.readline()
                if line: _append_log_line(line.rstrip("\n"))
                else:
                    if STOP_EVENT.wait(LOG_POLL_INTERVAL): break
            except Exception as e:
                log.warning(f"[Log] tailer: {e}")
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
            f"{TOPIC_PREFIX}/bridge/status", f"{TOPIC_PREFIX}/bridge/uptime",
            f"{TOPIC_PREFIX}/bridge/version", f"{TOPIC_PREFIX}/+/status",
            f"{TOPIC_PREFIX}/+/last_seen", f"{TOPIC_PREFIX}/+/cache_snapshot",
            f"{TOPIC_PREFIX}/bridge/edit_config_result",
            f"{TOPIC_PREFIX}/bridge/import_devices_result",
            f"{TOPIC_PREFIX}/bridge/scan_network_result",
            f"{TOPIC_PREFIX}/bridge/delete_device_result",
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
        elif key in ("edit_config_result", "import_devices_result",
                     "scan_network_result", "delete_device_result"):
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
                    # v1.18.9: в первые BRIDGE_STARTUP_GRACE_SEC после старта bridge —
                    # не пишем в status_events (чтобы старт bridge не выглядел мерцанием)
                    started = STATE.get("bridge_started_at", 0)
                    if started == 0 or (time.time() - started) >= BRIDGE_STARTUP_GRACE_SEC:
                        db_insert_status(int(time.time()), dev, payload)
            elif key == "last_seen":
                try: STATE["devices"][dev]["last_seen"] = int(payload)
                except: pass
            elif key == "cache_snapshot":
                try:
                    cd = json.loads(payload)
                    if isinstance(cd, dict):
                        STATE["devices"][dev]["cache"] = cd
                        db_insert_snapshot(dev, cd)
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
                "mac": d.get("mac", ""), "online": d.get("online", False),
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

        save_tinytuya_devices_json(normalized)

        return {"ok": True, "devices": normalized}
    except Exception as e:
        import traceback
        log.error(f"[Cloud] fetch error: {e}\n{traceback.format_exc()}")
        return {"ok": False, "error": str(e)}


def save_tinytuya_devices_json(devices):
    try:
        out = []
        for d in devices:
            if not d.get("mapping"):
                continue
            out.append({
                "id": d.get("id"),
                "name": d.get("name"),
                "key": d.get("local_key"),
                "product_id": d.get("product_id"),
                "product_name": d.get("product_name"),
                "category": d.get("category"),
                "ip": d.get("ip", ""),
                "version": d.get("version_guess", "3.3"),
                "mapping": d.get("mapping"),
            })
        os.makedirs(os.path.dirname(TINYTUYA_DEVICES_FILE) or ".", exist_ok=True)
        with open(TINYTUYA_DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        log.info(f"[Base] tinytuya_devices.json: {len(out)} устройств сохранено")
    except Exception as e:
        log.warning(f"[Base] save tinytuya_devices.json: {e}")


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
            return True
        except Exception as e:
            log.warning(f"[CloudCache] save: {e}")
            return False


def load_cloud_cache():
    with CLOUD_CACHE_LOCK:
        try:
            if not os.path.exists(TUYA_CLOUD_CACHE_FILE):
                return None
            with open(TUYA_CLOUD_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            devices = data.get("devices")
            if not isinstance(devices, list) or not devices:
                return None
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


def mapping_to_dps_map(mapping, category=""):
    dps_map = {}
    if not isinstance(mapping, dict): return dps_map
    for dp_key, m in mapping.items():
        if not isinstance(m, dict): continue
        code = m.get("code", "")
        dtype = m.get("type", "")
        values = m.get("values", {}) if isinstance(m.get("values"), dict) else {}
        if not code: continue
        entry = {}
        if dtype == "Boolean":
            if code in ("switch_led", "switch", "switch_1"):
                entry["component"] = "switch"; entry["name"] = code
            elif code.startswith("switch_") and code != "switch_led":
                entry["component"] = "switch"; entry["name"] = code
            elif code == "switch_backlight":
                entry["component"] = "switch"; entry["name"] = "backlight"
            elif code == "switch_prepayment":
                entry["component"] = "switch"; entry["name"] = "prepayment"
            elif code in ("doorcontact_state",):
                entry["component"] = "binary_sensor"; entry["name"] = "door"; entry["device_class"] = "door"
            elif code in ("pir",):
                entry["component"] = "binary_sensor"; entry["name"] = "motion"; entry["device_class"] = "motion"
            elif code in ("watersensor_state",):
                entry["component"] = "binary_sensor"; entry["name"] = "moisture"; entry["device_class"] = "moisture"
            elif code in ("fault", "problema"):
                entry["component"] = "binary_sensor"; entry["name"] = "fault"; entry["device_class"] = "problem"
            else:
                entry["component"] = "switch"; entry["name"] = code
        elif dtype == "Integer":
            entry["component"] = "sensor"; entry["name"] = code
            if "min" in values: entry["min"] = values["min"]
            if "max" in values: entry["max"] = values["max"]
            if "scale" in values and values["scale"]: entry["scale"] = values["scale"]
            if "unit" in values and values["unit"]: entry["unit"] = values["unit"]
            if code in ("va_temperature", "temp_current", "temp_set", "temp_current_f",
                        "temp_set_f", "upper_temp", "upper_temp_f"):
                entry["device_class"] = "temperature"
                if "f" in code.lower():
                    entry.setdefault("unit", "°F")
                else:
                    entry.setdefault("unit", "°C")
                entry["state_class"] = "measurement"
            elif code in ("va_humidity", "humidity"):
                entry["device_class"] = "humidity"; entry.setdefault("unit", "%"); entry["state_class"] = "measurement"
            elif "battery" in code:
                entry["device_class"] = "battery"; entry.setdefault("unit", "%"); entry["state_class"] = "measurement"
            elif "energy" in code or code in ("add_ele", "balance_energy", "charge_energy",
                                               "total_forward_energy", "cur_consumption"):
                entry["device_class"] = "energy"
                entry.setdefault("unit", "kWh")
                if any(x in code for x in ("total", "forward", "add")):
                    entry["state_class"] = "total_increasing"
                else:
                    entry["state_class"] = "total"
            elif code in ("cur_power", "power", "output_power", "power_consumption"):
                entry["device_class"] = "power"
                entry.setdefault("unit", "kW" if "output" in code else "W")
                entry["state_class"] = "measurement"
            elif "current" in code or code in ("leakage_current", "output_current", "cur_current"):
                entry["device_class"] = "current"
                if "leakage" in code:
                    entry.setdefault("unit", "mA")
                else:
                    entry.setdefault("unit", "A" if "output" in code else "mA")
                entry["state_class"] = "measurement"
            elif "voltage" in code or code in ("cur_voltage", "output_voltage"):
                entry["device_class"] = "voltage"; entry.setdefault("unit", "V"); entry["state_class"] = "measurement"
            elif code == "supply_frequency" or "frequency" in code:
                entry["device_class"] = "frequency"; entry.setdefault("unit", "Hz"); entry["state_class"] = "measurement"
            elif code == "power_factor":
                entry["device_class"] = "power_factor"; entry["state_class"] = "measurement"
            elif code in ("temp_value", "bright_value", "colour_temp"):
                entry["name"] = code
            else:
                entry.setdefault("state_class", "measurement")
        elif dtype == "Enum":
            rng = values.get("range", [])
            if code == "relay_status":
                entry["component"] = "select"; entry["name"] = "relay_status"; entry["options"] = list(rng)
            elif code in ("mode", "preset_mode"):
                entry["component"] = "preset"; entry["name"] = "preset_mode"; entry["options"] = list(rng)
            elif code == "work_mode":
                entry["component"] = "select"; entry["name"] = "work_mode"; entry["options"] = list(rng)
            elif code in ("watersensor_state",):
                entry["component"] = "binary_sensor"; entry["name"] = "moisture"; entry["device_class"] = "moisture"
            elif code in ("battery_state",):
                entry["component"] = "sensor"; entry["name"] = "battery_state"
            else:
                entry["component"] = "sensor"; entry["name"] = code
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


def guess_type_from_category(category, product_name="", mapping=None):
    c = (category or "").lower().strip(); pn = (product_name or "").lower()
    if c in CATEGORY_TO_TYPE: return CATEGORY_TO_TYPE[c]
    if any(x in pn for x in ("light", "lamp", "strip", "bulb")): return "light"
    if any(x in pn for x in ("thermostat", "heating", "floor")): return "climate"
    if any(x in pn for x in ("switch", "breaker", "plug", "socket")): return "switch"
    if any(x in pn for x in ("sensor", "temp", "humid")): return "sensor"
    if any(x in pn for x in ("door", "motion", "leak")): return "binary_sensor"
    return "switch"


# ==================== TUYA-LOCAL DB ====================
def download_tuya_local_db(max_retries=3):
    if os.path.isdir(TUYA_LOCAL_DB_OLD_DIR) and not os.path.isdir(TUYA_LOCAL_DB_DIR):
        try:
            os.makedirs(os.path.dirname(TUYA_LOCAL_DB_DIR) or ".", exist_ok=True)
            shutil.move(TUYA_LOCAL_DB_OLD_DIR, TUYA_LOCAL_DB_DIR)
            log.info(f"[tuya-local] Миграция: {TUYA_LOCAL_DB_OLD_DIR} → {TUYA_LOCAL_DB_DIR}")
        except Exception as e:
            log.warning(f"[tuya-local] Миграция не удалась: {e}")

    if os.path.isdir(TUYA_LOCAL_YAML_DIR):
        try:
            shutil.rmtree(TUYA_LOCAL_DB_DIR)
        except Exception as e:
            log.warning(f"[tuya-local] rmtree: {e}")
    os.makedirs(TUYA_LOCAL_DB_DIR, exist_ok=True)

    tarball_path = os.path.join(TUYA_LOCAL_DB_DIR, "_tuya-local.tar.gz")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"[tuya-local] Скачивание (попытка {attempt}/{max_retries})...")
            r = subprocess.run(["curl", "-fsSL", "-o", tarball_path, TUYA_LOCAL_TARBALL_URL],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                r = subprocess.run(["wget", "-q", "-O", tarball_path, TUYA_LOCAL_TARBALL_URL],
                                   capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                last_err = f"curl/wget: {r.stderr[:200]}"
                log.warning(f"[tuya-local] {last_err}")
                time.sleep(2)
                continue
            r = subprocess.run(["tar", "-xzf", tarball_path, "-C", TUYA_LOCAL_DB_DIR, "--strip-components=1"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                last_err = f"tar: {r.stderr[:200]}"
                log.warning(f"[tuya-local] {last_err}")
                continue
            if os.path.isdir(TUYA_LOCAL_YAML_DIR):
                files = [f for f in os.listdir(TUYA_LOCAL_YAML_DIR) if f.endswith(".yaml")]
                log.info(f"[tuya-local] OK: {len(files)} YAML-файлов")
                try: os.unlink(tarball_path)
                except: pass
                return True, None
            last_err = "yaml dir not found after extract"
        except Exception as e:
            last_err = str(e)
            log.warning(f"[tuya-local] attempt {attempt}: {e}")
            time.sleep(2)
    return False, last_err


def lookup_tuya_local(product_id):
    if not HAS_YAML or not os.path.isdir(TUYA_LOCAL_YAML_DIR):
        return None
    try:
        for fname in os.listdir(TUYA_LOCAL_YAML_DIR):
            if not fname.endswith(".yaml"):
                continue
            path = os.path.join(TUYA_LOCAL_YAML_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            if product_id not in content:
                continue
            try:
                data = yaml.safe_load(content)
            except Exception:
                continue
            products = data.get("products", []) if isinstance(data, dict) else []
            for p in products:
                if isinstance(p, dict) and p.get("id") == product_id:
                    return parse_tuya_local_yaml(data)
    except Exception as e:
        log.warning(f"[tuya-local] lookup: {e}")
    return None


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
def _rebuild_tinytuya_json_worker(device_names):
    with REBUILD_LOCK:
        REBUILD_STATE["running"] = True
        REBUILD_STATE["current"] = 0
        REBUILD_STATE["total"] = len(device_names)
        REBUILD_STATE["device"] = ""
        REBUILD_STATE["errors"] = []
        REBUILD_STATE["started_at"] = int(time.time())
        REBUILD_STATE["finished_at"] = 0
        REBUILD_STATE["ok"] = None

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
        dev_id = cfg.get("id", "")
        ip = cfg.get("ip", "")
        local_key = cfg.get("local_key", "")
        product_id = cfg.get("product_id", "") or cfg.get("model", "")
        if not dev_id or not ip or not local_key:
            errors.append(f"{name}: нет id/ip/local_key")
            continue

        try:
            version, local_dps = detect_version(ip, dev_id, local_key)
        except Exception as e:
            errors.append(f"{name}: probe: {e}")
            continue
        if not version:
            errors.append(f"{name}: probe не ответил")
            existing = existing_by_id.get(dev_id)
            if existing and existing.get("mapping"):
                results.append({
                    "id": dev_id, "name": cfg.get("friendly_name", name),
                    "key": local_key, "product_id": product_id,
                    "product_name": cfg.get("model", ""),
                    "category": cfg.get("category", ""),
                    "ip": ip, "version": cfg.get("version", "3.3"),
                    "mapping": existing["mapping"],
                    "_source": "existing",
                })
            continue

        matched = {}
        cloud_status_meta = cfg.get("_cloud_status_meta", []) or []
        cloud_current_values = cfg.get("_cloud_status_values", {}) or {}
        if cloud_status_meta and cloud_current_values:
            try:
                matched = match_dps_to_codes(local_dps, cloud_status_meta, cloud_current_values)
            except Exception as e:
                errors.append(f"{name}: match: {e}")

        existing = existing_by_id.get(dev_id)
        existing_mapping = (existing or {}).get("mapping", {}) if existing else {}
        if existing_mapping:
            for dp, m in existing_mapping.items():
                if dp not in matched:
                    matched[dp] = m

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
            continue

        results.append({
            "id": dev_id,
            "name": cfg.get("friendly_name", name),
            "key": local_key,
            "product_id": product_id,
            "product_name": cfg.get("model", ""),
            "category": cfg.get("category", ""),
            "ip": ip,
            "version": version or cfg.get("version", "3.3"),
            "mapping": mapping,
            "_source": "rebuild",
        })

        time.sleep(0.05)

    try:
        os.makedirs(os.path.dirname(TINYTUYA_DEVICES_FILE) or ".", exist_ok=True)
        with open(TINYTUYA_DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        log.info(f"[Rebuild] tinytuya_devices.json: {len(results)} устройств")
    except Exception as e:
        errors.append(f"save: {e}")
        log.error(f"[Rebuild] save: {e}")

    with REBUILD_LOCK:
        REBUILD_STATE["running"] = False
        REBUILD_STATE["finished_at"] = int(time.time())
        REBUILD_STATE["ok"] = len(errors) == 0 or len(results) > 0
        REBUILD_STATE["errors"] = errors
    log.info(f"[Rebuild] Завершено: {len(results)} ok, {len(errors)} ошибок")


# ==================== HTML ====================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Tuya Bridge</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8C%89%3C/text%3E%3C/svg%3E">
<script>
  // v1.20.1: применяем тему ДО рендера, чтобы не мигало белым
  // при переключении вкладок.
  (function() {
    try {
      var t = localStorage.getItem("tuya_webui_theme") || "dark";
      document.documentElement.setAttribute("data-theme", t);
    } catch (e) {
      document.documentElement.setAttribute("data-theme", "dark");
    }
  })();
</script>
<style>
  :root {
    --bg:#f4f6f8; --fg:#222; --muted:#666; --card:#fff; --border:#e1e4e8;
    --green:#2ea043; --red:#d73a49; --yellow:#b08800; --accent:#0366d6;
    --code-bg:#f6f8fa;
  }
  html[data-theme="dark"] {
    --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --card:#161b22; --border:#30363d;
    --green:#3fb950; --red:#f85149; --yellow:#d29922; --accent:#58a6ff;
    --code-bg:#0d1117;
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:16px; font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; background:var(--bg); color:var(--fg); font-size:14px; }
  code, pre { font-family:"JetBrains Mono",ui-monospace,monospace; }
  header { display:flex; align-items:center; gap:16px; margin-bottom:16px; flex-wrap:wrap; }
  header h1 { margin:0; font-size:20px; }
  nav { display:flex; gap:4px; }
  nav a { color:var(--fg); text-decoration:none; padding:6px 14px; border-radius:6px; font-size:13px; border:1px solid var(--border); }
  nav a.active { background:var(--accent); color:white; border-color:var(--accent); }
  .badge { padding:2px 8px; border-radius:12px; font-size:12px; background:var(--border); color:var(--muted); }
  .badge.online { background:var(--green); color:white; }
  .badge.offline { background:var(--red); color:white; }
  .badge.battery { background:rgba(176,136,0,0.2); color:var(--yellow); }
  .badge.junk { background:rgba(215,58,73,0.15); color:var(--red); font-size:10px; }
  .muted { color:var(--muted); }
  button { padding:6px 14px; border:1px solid var(--border); border-radius:6px; background:var(--card); color:var(--fg); cursor:pointer; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button:disabled { opacity:0.5; cursor:not-allowed; }
  button.active { background:var(--accent); color:white; border-color:var(--accent); }
  button.primary { background:var(--accent); color:white; border-color:var(--accent); }
  button.danger { background:var(--red); color:white; border-color:var(--red); }
  button.logs-pause.active { background:var(--yellow); color:white; border-color:var(--yellow); animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.65;} }
  input, select { padding:6px 10px; border:1px solid var(--border); border-radius:6px; background:var(--bg); color:var(--fg); font-size:13px; width:100%; }
  label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
  .form-group { margin-bottom:12px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:0; overflow:hidden; margin-bottom:16px; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:8px 12px; text-align:left; border-bottom:1px solid var(--border); }
  th { font-weight:600; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:0.5px; }
  th[data-sort] { cursor:pointer; user-select:none; }
  th[data-sort]:hover { color:var(--accent); }
  .sort-ind { color:var(--accent); font-weight:700; }
  tr:last-child td { border-bottom:none; }
  .device-row:hover { background:var(--border); cursor:pointer; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; background:var(--muted); }
  .dot.online { background:var(--green); }
  .dot.offline { background:var(--red); }
  .type-badge { display:inline-block; padding:1px 6px; font-size:11px; border-radius:4px; background:var(--border); color:var(--muted); }
  .type-badge.primary { background:rgba(3,102,214,0.15); color:var(--accent); }
  .device-ip { font-size:11px; color:var(--muted); margin-top:2px; }
  .latency { display:inline-block; padding:1px 6px; font-size:11px; border-radius:3px; }
  .lat-good { background:rgba(46,160,67,0.15); color:var(--green); }
  .lat-mid { background:rgba(176,136,0,0.15); color:var(--yellow); }
  .lat-bad { background:rgba(215,58,73,0.15); color:var(--red); }
  .lat-timeout { background:rgba(215,58,73,0.25); color:var(--red); }
  .problems { background:var(--card); border:1px solid var(--yellow); border-radius:8px; margin-bottom:16px; overflow:hidden; }
  .problems-header { padding:10px 16px; background:rgba(176,136,0,0.12); font-size:13px; color:var(--yellow); font-weight:600; border-bottom:1px solid var(--border); }
  .problem-item { padding:8px 16px; border-bottom:1px solid var(--border); font-size:13px; display:flex; justify-content:space-between; gap:16px; cursor:pointer; }
  .logs-toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; padding:8px 16px; border-bottom:1px solid var(--border); background:var(--card); }
  .logs-toolbar button { padding:4px 10px; font-size:12px; }
  .logs-toolbar input { width:auto; padding:4px 10px; font-size:12px; }
  .time-btn-group { display:flex; gap:0; }
  .time-btn-group button { border-radius:0; border-right-width:0; padding:4px 10px; font-size:11px; }
  .time-btn-group button:first-child { border-radius:6px 0 0 6px; }
  .time-btn-group button:last-child { border-radius:0 6px 6px 0; border-right-width:1px; }
  .logs-toolbar .time-btn-group { margin-left:auto; }
  .logs-paused-banner { background:var(--yellow); color:white; padding:6px 12px; font-size:12px; text-align:center; font-weight:600; }
  .logs { background:#0a0e14; color:#d1d5da; font-family:"JetBrains Mono",ui-monospace,monospace; font-size:12px; line-height:1.5; max-height:420px; min-height:420px; overflow-y:auto; padding:12px; position:relative; }
  .logs .line { white-space:pre-wrap; word-break:break-all; }
  .logs .lvl-DEBUG { color:#6e7681; } .logs .lvl-INFO { color:#d1d5da; }
  .logs .lvl-WARNING { color:#d29922; } .logs .lvl-ERROR, .logs .lvl-CRITICAL { color:#f85149; }
  .logs mark { background:#ffd33d; color:#000; padding:0 2px; }
  .logs .line.current-match { background:rgba(88,166,255,0.15); }
  .scroll-down-btn { position:sticky; bottom:12px; left:50%; transform:translateX(-50%); background:var(--accent); color:#fff; border:none; padding:6px 14px; border-radius:16px; cursor:pointer; font-size:12px; box-shadow:0 2px 8px rgba(0,0,0,0.3); display:none; z-index:10; margin:0 auto; width:fit-content; }
  .scroll-down-btn.visible { display:block; }
  .section-title { margin:0; padding:12px 16px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }
  .section-title .hint { font-size:11px; text-transform:none; letter-spacing:0; color:var(--muted); font-weight:400; }
  .toolbar { display:flex; gap:8px; align-items:center; margin-left:auto; flex-wrap:wrap; }
  .spin { display:inline-block; width:12px; height:12px; border:2px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .table-toolbar { display:flex; gap:8px; align-items:center; padding:10px 16px; border-bottom:1px solid var(--border); background:var(--bg); flex-wrap:wrap; }
  .table-toolbar input[type="text"] { max-width:280px; padding:5px 10px; font-size:12px; }
  .table-toolbar .toolbar-info { font-size:11px; color:var(--muted); margin-left:auto; }
  .table-toolbar .toolbar-clear { padding:3px 8px; font-size:11px; }
  .skeleton { display:inline-block; height:12px; border-radius:3px;
    background: linear-gradient(90deg, var(--border) 25%, var(--bg) 50%, var(--border) 75%);
    background-size: 200% 100%; animation: shimmer 1.5s infinite; vertical-align:middle; }
  .skeleton.w40 { width:40%; } .skeleton.w60 { width:60%; } .skeleton.w80 { width:80%; }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
  .version-badge { display:inline-block; padding:1px 6px; border-radius:4px; font-size:11px;
    font-weight:600; font-family:ui-monospace,monospace; vertical-align:middle; }
  .version-badge.v31 { background:rgba(148,163,184,0.18); color:#94a3b8; }
  .version-badge.v32 { background:rgba(56,189,248,0.15); color:#38bdf8; }
  .version-badge.v33 { background:rgba(46,160,67,0.18); color:var(--green); }
  .version-badge.v34 { background:rgba(3,102,214,0.18); color:var(--accent); }
  .version-badge.v35 { background:rgba(163,113,247,0.18); color:#a371f7; }
  .badge.bridge-version, .badge.webui-version {
    background: rgba(56,120,200,0.18); color: #6ea8fe; border:1px solid rgba(56,120,200,0.25); }
  html[data-theme="light"] .badge.bridge-version,
  html[data-theme="light"] .badge.webui-version {
    background: rgba(3,102,214,0.12); color: #0550ae; border:1px solid rgba(3,102,214,0.2); }
  .type-badge.light { background:rgba(210,153,34,0.18); color:#d29922; }
  .type-badge.switch { background:rgba(46,160,67,0.18); color:var(--green); }
  .type-badge.climate { background:rgba(219,109,40,0.18); color:#db6d28; }
  .type-badge.sensor { background:rgba(56,139,253,0.18); color:#388bfd; }
  .type-badge.binary_sensor { background:rgba(163,113,247,0.18); color:#a371f7; }
  .state-on { color:var(--green); font-weight:600; }
  .state-off { color:var(--muted); }
  .modal-overlay { display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.55); z-index:1000; align-items:center; justify-content:center; padding:20px; }
  .modal-overlay.open { display:flex; }
  .modal-overlay.top { z-index:1100; }
  .modal { background:var(--card); border:1px solid var(--border); border-radius:8px; max-width:750px; width:100%; max-height:85vh; display:flex; flex-direction:column; overflow:hidden; }
  .modal.wide { max-width:1100px; }
  .modal.small { max-width:440px; }
  .modal-header { display:flex; align-items:center; justify-content:space-between; padding:12px 16px; border-bottom:1px solid var(--border); }
  .modal-header h2 { margin:0; font-size:16px; }
  .modal-close { background:none; border:none; color:var(--fg); font-size:24px; cursor:pointer; padding:0 8px; line-height:1; }
  .modal-body { padding:16px; overflow-y:auto; }
  /* v1.18.11: запрещаем браузеру "прыгать" к фокусному чекбоксу
     внутри overflow:auto контейнера (Firefox/Chrome focus-scroll). */
  .modal-body input[type="checkbox"],
  .modal-body input[type="radio"] { scroll-margin: 9999px; }
  .modal-body h3 { margin:16px 0 8px 0; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; }
  .modal-body h3:first-child { margin-top:0; }
  .detail-table { width:100%; border-collapse:collapse; font-size:13px; }
  .detail-table td { padding:5px 8px; border-bottom:1px solid var(--border); vertical-align:top; }
  .detail-table td:first-child { color:var(--muted); width:35%; white-space:nowrap; }
  .history-scroll { max-height:300px; overflow-y:auto; }
  .history-item { padding:4px 8px; font-family:ui-monospace,monospace; font-size:12px; border-bottom:1px solid var(--border); }
  .history-item.online { color:var(--green); }
  .history-item.offline { color:var(--red); }
  code { font-family:ui-monospace,monospace; font-size:12px; background:var(--bg); padding:1px 5px; border-radius:3px; }
  .copy-row { display:inline-flex; align-items:center; gap:4px; max-width:100%; }
  .copy-row code { max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; user-select:all; -webkit-user-select:all; }
  .copy-row code.sel-ok { background:var(--accent); color:#fff; }
  .copy-hint { cursor:pointer; opacity:0.5; font-size:11px; user-select:none; display:inline-block; padding:0 2px; border-radius:3px; }
  .copy-hint:hover { opacity:1; background:var(--border); }
  .copy-hint.copied { color:var(--green); opacity:1; }
  .secret-masked { letter-spacing:2px; color:var(--muted); }
  .sparkline { background:var(--bg); border-radius:4px; padding:8px; margin-bottom:8px; }
  .sparkline svg { display:block; width:100%; height:40px; }
  .sparkline-labels { display:flex; justify-content:space-between; font-size:10px; color:var(--muted); margin-top:2px; }
  .analytics-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; align-items:start; }
  @media (max-width:1000px) { .analytics-grid { grid-template-columns:1fr; } }
  .analytics-card { margin-bottom:0; display:flex; flex-direction:column; max-height:520px; overflow:hidden; }
  .analytics-card > .analytics-card-header { flex-shrink:0; }
  .analytics-card-header { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:12px 16px; border-bottom:1px solid var(--border); flex-wrap:wrap; }
  .analytics-card-header h2 { margin:0; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; }
  .analytics-card-header .analytics-toolbar { display:flex; gap:8px; align-items:center; margin-left:auto; flex-wrap:wrap; }
  .analytics-table-scroll { flex:1 1 auto; min-height:0; max-height:420px; overflow-y:auto; }
  .timeline-scroll { flex:1 1 auto; min-height:0; max-height:420px; overflow-y:auto; }
  .timeline-item { padding:6px 16px; border-bottom:1px solid var(--border); font-size:12px; display:flex; gap:12px; }
  .timeline-item .timeline-ts { color:var(--muted); flex:0 0 155px; white-space:nowrap; }
  .scan-host { padding:10px 12px; border:1px solid var(--border); border-radius:6px; margin-bottom:6px; }
  .scan-host:hover { border-color:var(--accent); }
  .scan-host.bridge-new { border-left:3px solid var(--accent); }
  .scan-meta { font-size:11px; color:var(--muted); margin-top:2px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .scan-badge { display:inline-block; padding:1px 6px; border-radius:4px; font-size:10px; font-weight:600; }
  .scan-badge.tuya-unknown { background:rgba(215,58,73,0.2); color:var(--red); }
  .scan-badge.router { background:rgba(3,102,214,0.2); color:var(--accent); }
  .scan-badge.known { background:rgba(46,160,67,0.2); color:var(--green); }
  .scan-badge.iot { background:rgba(176,136,0,0.2); color:var(--yellow); }
  .scan-badge.unknown { background:var(--border); color:var(--muted); }
  .scan-badge.bridge { background:rgba(163,113,247,0.2); color:#a371f7; }
  .scan-badge.bridge-confirmed { background:rgba(46,160,67,0.25); color:var(--green); font-weight:700; }
  .scan-port { font-family:ui-monospace,monospace; font-size:10px; background:var(--bg); padding:0 4px; border-radius:2px; }
  .scan-toolbar { display:flex; gap:8px; align-items:center; padding:10px 16px; border-bottom:1px solid var(--border); background:var(--bg); flex-wrap:wrap; }
  .scan-toolbar-info { font-size:12px; color:var(--muted); }
  details summary { cursor:pointer; padding:6px 0; user-select:none; }
  details summary::-webkit-details-marker { color:var(--muted); }
  .theme-btn { background:none; border:1px solid var(--border); padding:4px 10px; font-size:16px; line-height:1; cursor:pointer; border-radius:6px; }
  .theme-btn:hover { border-color:var(--accent); }
  .tools-toolbar { display:flex; gap:8px; padding:12px 16px; border-bottom:1px solid var(--border); background:var(--bg); align-items:center; flex-wrap:wrap; }
  .tools-split { display:grid; grid-template-columns:280px 1fr; min-height:500px; }
  .tools-list { border-right:1px solid var(--border); max-height:70vh; overflow-y:auto; }
  .tools-list-item { padding:8px 12px; border-bottom:1px solid var(--border); cursor:pointer; font-size:13px; }
  .tools-list-item:hover { background:var(--border); }
  .tools-list-item.active { background:var(--accent); color:white; }
  .tools-list-item .tools-list-ip { font-size:11px; color:var(--muted); margin-top:2px; }
  .tools-list-item.active .tools-list-ip { color:rgba(255,255,255,0.7); }
  .tools-detail { padding:16px; overflow-y:auto; max-height:70vh; }
  .tools-error { padding:24px; text-align:center; color:var(--red); }
  pre.json-view { background:var(--code-bg); border:1px solid var(--border); border-radius:6px; padding:12px; font-size:12px; overflow-x:auto; max-height:600px; margin:0; }
  pre.json-view code { background:none; padding:0; }
  html[data-theme="dark"] .hljs-attr { color:#79c0ff; }
  html[data-theme="dark"] .hljs-string { color:#a5d6ff; }
  html[data-theme="dark"] .hljs-number { color:#79c0ff; }
  html[data-theme="dark"] .hljs-literal { color:#ff7b72; }
  html[data-theme="dark"] .hljs-punctuation { color:#8b949e; }
  html[data-theme="light"] .hljs-attr { color:#0550ae; }
  html[data-theme="light"] .hljs-string { color:#0a3069; }
  html[data-theme="light"] .hljs-number { color:#0550ae; }
  html[data-theme="light"] .hljs-literal { color:#cf222e; }
  html[data-theme="light"] .hljs-punctuation { color:#57606a; }
  .base-info { padding:12px 16px; display:flex; gap:20px; align-items:center; flex-wrap:wrap; font-size:13px; }
  .base-info-item { display:flex; align-items:center; gap:6px; }
  .junk-row { background:rgba(215,58,73,0.05); }
  .junk-row td { opacity:0.7; }
  .preview-dp-table { font-size:12px; }
  .preview-dp-table th { padding:4px 8px; }
  .preview-dp-table td { padding:3px 8px; }
  .preview-device { border:1px solid var(--border); border-radius:6px; margin-bottom:12px; overflow:hidden; }
  .preview-device-header { padding:8px 12px; background:var(--bg); display:flex; justify-content:space-between; align-items:center; font-weight:600; }
  .preview-device-body { padding:8px 12px; }
  .mobile-nav { display:none; padding:8px 12px; background:var(--bg); border-bottom:1px solid var(--border); align-items:center; justify-content:space-between; }
  @media (max-width:1000px) { .mobile-nav { display:flex; } }
  .cloud-warn { background:rgba(176,136,0,0.12); border:1px solid var(--yellow); color:var(--yellow); padding:8px 12px; border-radius:6px; margin-bottom:12px; font-size:12px; }
  .rebuild-bar { width:100%; height:6px; background:var(--border); border-radius:3px; overflow:hidden; margin-top:6px; }
  .rebuild-bar-fill { height:100%; background:var(--accent); transition: width 0.3s; }
  .cloud-cache-banner {
    display:flex; gap:8px; align-items:center; flex-wrap:wrap;
    padding:8px 16px; font-size:12px;
    background:rgba(176,136,0,0.12); color:var(--yellow);
    border-bottom:1px solid var(--border);
  }
  .cloud-cache-banner.red {
    background:rgba(215,58,73,0.12); color:var(--red);
  }
  .cloud-cache-banner button {
    padding:3px 10px; font-size:11px;
  }
  select { appearance: auto; }
  .edit-row {
    display:flex; gap:10px; align-items:flex-start;
    padding:10px 0; border-bottom:1px solid var(--border);
  }
  .edit-row:last-of-type { border-bottom:none; }
  .edit-check { padding-top:22px; cursor:pointer; }
  .edit-check input[type="checkbox"] {
    width:18px; height:18px; cursor:pointer; accent-color:var(--accent);
  }
  .edit-field { flex:1; }
  .edit-field label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
  .edit-field input:disabled,
  .edit-field select:disabled,
  .edit-field button:disabled {
    opacity:0.45; cursor:not-allowed;
  }
  .radio-row { display:flex; gap:8px; align-items:center; margin:8px 0; cursor:pointer; }
  .radio-row input[type="radio"] { width:16px; height:16px; accent-color:var(--accent); cursor:pointer; }
  .radio-row label { cursor:pointer; margin:0; font-size:13px; color:var(--fg); }
  .ui-confirm-text { margin:0 0 16px 0; font-size:14px; line-height:1.5; }
  .ui-alert-icon { font-size:32px; text-align:center; margin-bottom:8px; }
  .modal-footer { display:flex; gap:8px; justify-content:flex-end; padding:12px 16px; border-top:1px solid var(--border); background:var(--bg); }
  .latency-spark-labels { display:flex; justify-content:space-between; font-size:10px; color:var(--muted); margin-top:2px; }
  .chart-block { position:relative; }
  .chart-subtitle { font-size:11px; color:var(--muted); margin-bottom:4px; text-transform:uppercase; letter-spacing:0.5px; }
  .chart-legend { display:flex; gap:16px; align-items:center; margin-top:6px; font-size:11px; color:var(--muted); flex-wrap:wrap; }
  .legend-item { display:inline-flex; align-items:center; gap:6px; }
  .legend-line { display:inline-block; width:20px; height:0; border-top:2px solid; }
  .legend-line.online { border-color:var(--green); }
  .legend-line.total { border-top-style:dashed; border-color:var(--muted); }
  .legend-bar { display:inline-block; width:12px; height:10px; border-radius:2px; }
  .legend-bar.flaps { background:var(--yellow); }
</style>
</head>
<body>

<header>
  <h1>Tuya Bridge</h1>
  <nav>
    <a href="/" id="nav-dashboard">Дашборд</a>
    <a href="/analytics" id="nav-analytics">Аналитика</a>
    <a href="/import" id="nav-import">Импорт устройств</a>
    <a href="/tools" id="nav-tools">Инструменты</a>
  </nav>
  <span id="bridge-version-badge" class="badge bridge-version" title="Версия Bridge">Bridge v?</span>
  <span id="webui-version-badge" class="badge webui-version" title="Версия WebUI">WebUI v?</span>
  <span id="bridge-status" class="badge">…</span>
  <span id="bridge-uptime" class="muted"></span>
  <span id="devices-summary" class="muted">устройства: —</span>
  <div class="toolbar">
    <button id="theme-btn" class="theme-btn" onclick="toggleTheme()" title="Переключить тему">🌙</button>
    <button id="latency-btn" onclick="doLatencyRefresh()">📡 Обновить задержку</button>
    <span id="latency-result" class="muted" style="font-size:11px;"></span>
    <button id="db-cleanup-open" onclick="openDbCleanup()">🗑 Очистить БД</button>
    <button id="cleanup-btn" onclick="doCleanup()">🧹 Очистить Discovery</button>
    <span id="cleanup-result" class="muted" style="font-size:11px;"></span>
  </div>
</header>

<div id="view-dashboard">
  <div id="problems-block" class="problems" style="display:none;">
    <div class="problems-header">⚠️ Проблемные устройства <span id="problems-count"></span></div>
    <div id="problems-list"></div>
  </div>

  <div class="card">
    <h2 class="section-title">Устройства <span class="hint">клик по строке — подробности, клик по заголовку — сортировка</span></h2>
    <div class="table-toolbar">
      <input id="device-search" type="text" placeholder="Поиск: имя, IP, тип…" oninput="onDeviceSearch(this.value)">
      <button class="toolbar-clear" onclick="clearDeviceSearch()">×</button>
      <span class="toolbar-info" id="device-search-info"></span>
    </div>
    <table>
      <thead><tr>
        <th data-sort="name" onclick="sortDevices('name')">Имя <span class="sort-ind"></span></th>
        <th data-sort="type" onclick="sortDevices('type')">Тип <span class="sort-ind"></span></th>
        <th data-sort="status" onclick="sortDevices('status')">Статус <span class="sort-ind"></span></th>
        <th data-sort="latency" onclick="sortDevices('latency')">Задержка <span class="sort-ind"></span></th>
        <th data-sort="last_seen" onclick="sortDevices('last_seen')">Последняя активность <span class="sort-ind"></span></th>
      </tr></thead>
      <tbody id="devices-body"><tr><td colspan="5" class="muted">Загрузка…</td></tr></tbody>
    </table>
  </div></div>

<div id="view-analytics" style="display:none;">
  <div class="analytics-grid">
    <div class="card analytics-card">
      <div class="analytics-card-header">
        <h2>Задержка (ICMP ping)</h2>
        <div class="analytics-toolbar">
          <div class="time-btn-group" id="latency-period-group">
            <button data-lat="1800" onclick="setLatencyPeriod(1800)">30мин</button>
            <button data-lat="3600" class="active" onclick="setLatencyPeriod(3600)">1час</button>
            <button data-lat="21600" onclick="setLatencyPeriod(21600)">6час</button>
            <button data-lat="86400" onclick="setLatencyPeriod(86400)">Сутки</button>
            <button data-lat="0" onclick="setLatencyPeriod(0)">Всё</button>
          </div>
        </div>
      </div>
      <div class="analytics-table-scroll">
        <table>
          <thead><tr>
            <th data-lsort="name" onclick="sortLatency('name')">Устройство <span class="sort-ind"></span></th>
            <th data-lsort="ip" onclick="sortLatency('ip')">IP <span class="sort-ind"></span></th>
            <th data-lsort="avg" onclick="sortLatency('avg')">Средний ping <span class="sort-ind"></span></th>
            <th data-lsort="ts" onclick="sortLatency('ts')">Последняя проверка <span class="sort-ind"></span></th>
          </tr></thead>
          <tbody id="latency-body"><tr><td colspan="4" class="muted">Загрузка…</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card analytics-card">
      <div class="analytics-card-header">
        <h2>Хронология событий</h2>
        <div class="analytics-toolbar">
          <span id="timeline-count" class="muted" style="font-size:11px;"></span>
          <button onclick="openTimelineCleanup()" style="padding:3px 10px; font-size:11px;">🗑 Очистить</button>
        </div>
      </div>
      <div id="timeline-list" class="timeline-scroll"></div>
    </div>
  </div>
  <div class="analytics-grid">
    <div class="card analytics-card">
      <div class="analytics-card-header"><h2>Мерцающие устройства (24ч)</h2></div>
      <div class="analytics-table-scroll">
        <table>
          <thead><tr>
            <th data-fsort="dev" onclick="sortFlappers('dev')">Устройство <span class="sort-ind"></span></th>
            <th data-fsort="flaps" onclick="sortFlappers('flaps')">Переходов <span class="sort-ind"></span></th>
          </tr></thead>
          <tbody id="flappers-body"></tbody>
        </table>
      </div>
    </div>
    <div class="card analytics-card" style="max-height:none;">
      <div class="analytics-card-header">
        <h2>Активность и мерцания (24ч)</h2>
        <span id="activity-summary" class="muted" style="font-size:11px;"></span>
      </div>
      <div style="padding:0 16px 12px 16px;">
        <div class="chart-block">
          <div class="chart-subtitle">Online по часам</div>
          <svg id="chart-activity" viewBox="0 0 800 180" preserveAspectRatio="none" style="width:100%; height:180px; display:block;"></svg>
          <div class="chart-legend">
            <span class="legend-item"><span class="legend-line online"></span>online</span>
            <span class="legend-item"><span class="legend-line total"></span>total (всего устройств)</span>
          </div>
        </div>
        <div class="chart-block" style="margin-top:16px;">
          <div class="chart-subtitle">Мерцания (переходы online↔offline по часам)</div>
          <svg id="chart-flaps" viewBox="0 0 800 140" preserveAspectRatio="none" style="width:100%; height:140px; display:block;"></svg>
          <div class="chart-legend">
            <span class="legend-item"><span class="legend-bar flaps"></span>переходов за час</span>
          </div>
        </div>
      </div>
    </div>
  </div>



  <div class="card">
    <h2 class="section-title">Логи (live, все уровни)</h2>
    <div class="logs-toolbar">
      <span class="muted" style="font-size:12px;">Фильтр:</span>
      <button data-level="DEBUG" onclick="toggleLevel('DEBUG')">DEBUG</button>
      <button data-level="INFO" class="active" onclick="toggleLevel('INFO')">INFO+</button>
      <button data-level="WARNING" onclick="toggleLevel('WARNING')">WARN+</button>
      <button data-level="ERROR" onclick="toggleLevel('ERROR')">ERROR</button>
      <button onclick="pauseLogs()" id="pause-btn" class="logs-pause">⏸ Пауза</button>
      <input id="log-search" type="text" placeholder="Поиск..." oninput="doSearch()" style="min-width:160px; max-width:200px;">
      <button onclick="findNext()" id="find-next-btn" disabled>▼</button>
      <button onclick="findPrev()" id="find-prev-btn" disabled>▲</button>
      <span id="find-info" class="muted" style="font-size:11px;"></span>
      <div class="time-btn-group">
        <button data-range="1800" onclick="setLogRange(1800)">30 мин</button>
        <button data-range="3600" class="active" onclick="setLogRange(3600)">1 час</button>
        <button data-range="86400" onclick="setLogRange(86400)">Сутки</button>
        <button data-range="0" onclick="setLogRange(0)">Всё</button>
      </div>
      <button onclick="downloadLogs()">⬇ Скачать</button>
    </div>
    <div class="logs" id="logs"></div>
    <button class="scroll-down-btn" id="scroll-down-btn" onclick="scrollLogsToBottom()">↓ Вниз</button>
  </div>
</div>
<div id="view-import" style="display:none;">
  <div class="card">
    <h2 class="section-title">Доступы к Tuya Cloud</h2>
    <div id="cloud-cache-banner" class="cloud-cache-banner" style="display:none;"></div>
    <div style="padding:16px;">
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
        <div class="form-group"><label>Access ID</label><input id="cloud-access-id" type="text"></div>
        <div class="form-group"><label>Access Secret</label><input id="cloud-access-secret" type="password"></div>
        <div class="form-group"><label>Region</label>
          <select id="cloud-region">
            <option value="eu">EU</option><option value="us">US</option>
            <option value="us-e">US-E</option><option value="eu-w">EU-W</option>
            <option value="cn">CN</option><option value="in">IN</option>
            <option value="sg">SG</option>
          </select>
        </div>
        <div class="form-group"><label>&nbsp;</label>
          <div style="display:flex; gap:8px;">
            <button class="primary" onclick="fetchCloudDevices()" id="fetch-btn" style="flex:1;">📥 Запросить устройства</button>
            <button onclick="clearCloudCache()" title="Очистить кэш">🗑</button>
          </div>
        </div>
      </div>
      <div id="cloud-result" class="muted" style="margin-top:12px;"></div>
      <div id="cloud-cache-info" class="muted" style="margin-top:4px; font-size:11px;"></div>
    </div>
  </div>

  <div class="card">
    <h2 class="section-title">Локальные базы DP</h2>
    <div class="base-info">
      <div class="base-info-item">
        <b>tinytuya devices.json:</b>
        <span id="base-tinytuya-info" class="muted">—</span>
      </div>
      <div class="base-info-item">
        <b>tuya-local база:</b>
        <span id="base-tuya-local-info" class="muted">—</span>
      </div>
      <button onclick="loadBaseInfo()">🔄 Обновить</button>
      <button onclick="updateTuyaLocalDb()" id="tuya-local-update-btn">⬇ Обновить tuya-local</button>
      <button onclick="rebuildTinytuyaJson()" id="rebuild-btn">🔄 Пересобрать tinytuya.json</button>
      <span id="base-update-result" class="muted" style="font-size:11px;"></span>
    </div>
    <div id="rebuild-progress" style="display:none; padding:0 16px 12px 16px;">
      <div class="muted" style="font-size:12px;" id="rebuild-progress-text"></div>
      <div class="rebuild-bar"><div class="rebuild-bar-fill" id="rebuild-bar-fill" style="width:0%"></div></div>
    </div>
  </div>

  <div class="card">
    <h2 class="section-title">Устройства из Cloud <span id="cloud-count" class="muted" style="float:right; text-transform:none;"></span></h2>
    <div class="table-toolbar">
      <input id="cloud-search" type="text" placeholder="Поиск: имя, ID, продукт…" oninput="onCloudSearch(this.value)">
      <button class="toolbar-clear" onclick="clearCloudSearch()">×</button>
      <span class="toolbar-info" id="cloud-search-info"></span>
    </div>
    <div id="cloud-devices-container">
      <div class="muted" style="padding:16px;">Введите данные и нажмите «Запросить устройства»</div>
    </div>
    <div style="display:flex; gap:8px; align-items:center; padding:12px 16px; border-top:1px solid var(--border); background:var(--bg);" id="import-actions">
      <button onclick="selectAllCloud(true)">Выбрать все</button>
      <button onclick="selectAllCloud(false)">Снять всё</button>
      <span id="selected-count" class="muted">Выбрано: 0</span>
      <button class="primary" onclick="importSelected()" style="margin-left:auto;">📦 Импортировать выбранные</button>
    </div>
  </div>

  <div id="import-result-block" class="card" style="display:none;">
    <h2 class="section-title">Результат импорта</h2>
    <div style="padding:16px;" id="import-result-content"></div>
  </div>

  <div class="card">
    <h2 class="section-title">Скан сети (безопасный)</h2>
    <div style="padding:16px;">
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
        <div class="form-group"><label>Подсеть (префикс)</label>
          <input id="scan-subnet" type="text" placeholder="192.168.0" value="192.168.0">
        </div>
        <div class="form-group"><label>&nbsp;</label>
          <button class="primary" onclick="doScanExtended()" id="scan-btn" style="width:100%;">🔍 Безопасный скан</button>
        </div>
      </div>
      <div class="muted" style="font-size:11px; margin-top:4px;">
        ICMP + ARP + MAC vendor + безопасные порты. Tuya-проба (6668+UDP) только для неизвестных. Сортировка по IP.
      </div>
      <div id="scan-result" style="margin-top:12px; display:none;"></div>
    </div>
  </div>
</div>

<div id="view-tools" style="display:none;">
  <div class="card">
    <h2 class="section-title">Конфигурация (devices_config.json) <span class="hint">только просмотр</span></h2>
    <div class="tools-toolbar">
      <button id="tools-view-raw" class="active" onclick="setToolsView('raw')">📄 Raw JSON</button>
      <button id="tools-view-bydev" onclick="setToolsView('bydev')">📋 По устройствам</button>
      <button onclick="loadConfig()" style="margin-left:auto;">🔄 Обновить</button>
      <button onclick="copyConfigRaw()">📋 Копировать JSON</button>
      <span id="tools-info" class="muted" style="font-size:11px;"></span>
    </div>
    <div id="tools-container">
      <div class="muted" style="padding:16px;">Загрузка…</div>
    </div>
  </div>
</div>

<div id="modal-overlay" class="modal-overlay" onclick="closeModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-header"><h2 id="modal-title">Устройство</h2>
      <button class="modal-close" onclick="closeModal()">×</button>
    </div>
    <div id="modal-body" class="modal-body"></div>
  </div>
</div>

<div id="cloud-modal-overlay" class="modal-overlay" onclick="closeCloudModal(event)">
  <div class="modal wide" onclick="event.stopPropagation()">
    <div class="modal-header"><h2 id="cloud-modal-title">Cloud Device</h2>
      <button class="modal-close" onclick="closeCloudModal()">×</button>
    </div>
    <div id="cloud-modal-body" class="modal-body"></div>
  </div>
</div>

<div id="preview-overlay" class="modal-overlay" onclick="closePreview(event)">
  <div class="modal wide" onclick="event.stopPropagation()">
    <div class="modal-header"><h2 id="preview-title">Превью импорта</h2>
      <button class="modal-close" onclick="closePreview()">×</button>
    </div>
    <div id="preview-mobile-nav" class="mobile-nav">
      <button onclick="previewPrev()">◀ Назад</button>
      <span id="preview-counter">1/1</span>
      <button onclick="previewNext()">Далее ▶</button>
    </div>
    <div id="preview-body" class="modal-body"></div>
    <div style="display:flex; gap:8px; padding:12px 16px; border-top:1px solid var(--border); background:var(--bg);">
      <button onclick="closePreview()">Отмена</button>
      <button onclick="probeAllInPreview()" id="preview-probe-all-btn">🔍 Probe все</button>
      <button class="primary" onclick="confirmImport()" style="margin-left:auto;" id="preview-import-btn">📦 Импортировать всё</button>
    </div>
  </div>
</div>

<div id="edit-device-overlay" class="modal-overlay" onclick="closeEditDevice(event)">
  <div class="modal" style="max-width:520px;" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h2 id="edit-device-title">Редактирование</h2>
      <button class="modal-close" onclick="closeEditDevice()">×</button>
    </div>
    <div class="modal-body">
      <p class="muted" style="font-size:12px; margin-top:0;">
        Отметь галочкой поля, которые хочешь изменить. Остальные останутся как есть.
      </p>

      <div class="edit-row">
        <label class="edit-check">
          <input type="checkbox" id="edit-device-ip-check" onchange="updateEditSubmitState()">
        </label>
        <div class="edit-field">
          <label>IP-адрес</label>
          <input id="edit-device-ip" type="text" placeholder="192.168.0.100" disabled oninput="updateEditSubmitState()">
        </div>
      </div>

      <div class="edit-row">
        <label class="edit-check">
          <input type="checkbox" id="edit-device-version-check" onchange="updateEditSubmitState()">
        </label>
        <div class="edit-field">
          <label>Версия протокола</label>
          <select id="edit-device-version" disabled onchange="updateEditSubmitState()">
            <option value="3.1">3.1</option>
            <option value="3.2">3.2</option>
            <option value="3.3">3.3</option>
            <option value="3.4">3.4</option>
            <option value="3.5">3.5</option>
          </select>
          <div class="muted" style="font-size:11px; margin-top:4px;">
            Сейчас у bridge: v<span id="edit-device-current-version">3.3</span>
          </div>
        </div>
      </div>

      <div class="edit-row">
        <label class="edit-check">
          <input type="checkbox" id="edit-device-key-check" onchange="updateEditSubmitState()">
        </label>
        <div class="edit-field">
          <label>Local key</label>
          <div style="display:flex; gap:8px; align-items:center;">
            <input id="edit-device-key" type="password" placeholder="введи новый key" disabled oninput="updateEditSubmitState()">
            <button type="button" onclick="toggleEditKeyVisibility()" id="edit-device-key-toggle" style="white-space:nowrap;" disabled>👁 Показать</button>
          </div>
          <div class="muted" style="font-size:11px; margin-top:4px;">
            Текущий key скрыт. Отметь галочку, чтобы ввести новый.
          </div>
        </div>
      </div>

      <div id="edit-device-error" style="display:none; margin-top:12px; font-size:12px; padding:10px; border-radius:6px; background:rgba(215,58,73,0.08); color:var(--red); border:1px solid rgba(215,58,73,0.25);"></div>

      <details id="edit-device-raw-error" style="display:none; margin-top:8px;">
        <summary class="muted" style="font-size:11px; cursor:pointer;">▶ Технические детали</summary>
        <pre id="edit-device-raw-error-text" style="background:var(--bg); padding:8px; border-radius:4px; font-size:11px; overflow-x:auto; margin-top:6px; white-space:pre-wrap; word-break:break-all;"></pre>
      </details>

      <div style="display:flex; gap:8px; justify-content:flex-end; margin-top:16px;">
        <button onclick="closeEditDevice()">Отмена</button>
        <button class="primary" onclick="submitEditDevice()" id="edit-device-submit" disabled>Сохранить</button>
      </div>
    </div>
  </div>
</div>

<div id="db-cleanup-overlay" class="modal-overlay" onclick="closeDbCleanup(event)">
  <div class="modal small" onclick="event.stopPropagation()">
    <div class="modal-header"><h2>Очистить БД</h2>
      <button class="modal-close" onclick="closeDbCleanup()">×</button>
    </div>
    <div class="modal-body">
      <p class="muted">Удалить все записи старше указанного периода.</p>
      <div class="form-group"><label>Оставить дней</label>
        <input id="db-keep-days" type="number" value="3" min="0" max="365">
      </div>
      <div class="form-group"><label>Оставить часов (дополнительно)</label>
        <input id="db-keep-hours" type="number" value="0" min="0" max="23">
      </div>
      <div id="db-cleanup-result" class="muted" style="margin-bottom:12px;"></div>
    </div>
    <div class="modal-footer">
      <button onclick="closeDbCleanup()">Отмена</button>
      <button class="danger" onclick="doDbCleanup()" id="db-cleanup-btn">🗑 Удалить</button>
    </div>
  </div>
</div>

<div id="timeline-cleanup-overlay" class="modal-overlay" onclick="closeTimelineCleanup(event)">
  <div class="modal" style="max-width:520px;" onclick="event.stopPropagation()">
    <div class="modal-header"><h2>Очистить хронологию событий</h2>
      <button class="modal-close" onclick="closeTimelineCleanup()">×</button>
    </div>
    <div class="modal-body">
      <p class="muted" style="margin-top:0;">Таблица <code>status_events</code>. История задержек и снапшоты состояний не тронутся.</p>

      <div class="radio-row">
        <input type="radio" name="timeline-scope" id="tl-scope-all" value="all" checked onchange="updateTimelineCleanupForm()">
        <label for="tl-scope-all">Удалить всё</label>
      </div>

      <div class="radio-row">
        <input type="radio" name="timeline-scope" id="tl-scope-age" value="age" onchange="updateTimelineCleanupForm()">
        <label for="tl-scope-age">Удалить старше:</label>
      </div>
      <div id="tl-age-fields" style="display:none; margin-left:24px;">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
          <div><label>Дней</label><input id="tl-keep-days" type="number" value="3" min="0" max="365"></div>
          <div><label>Часов</label><input id="tl-keep-hours" type="number" value="0" min="0" max="23"></div>
        </div>
      </div>

      <div class="radio-row">
        <input type="radio" name="timeline-scope" id="tl-scope-before" value="before" onchange="updateTimelineCleanupForm()">
        <label for="tl-scope-before">Удалить до даты:</label>
      </div>
      <div id="tl-before-fields" style="display:none; margin-left:24px;">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
          <div><label>Дата</label><input id="tl-before-date" type="date"></div>
          <div><label>Время</label><input id="tl-before-time" type="time" value="00:00"></div>
        </div>
      </div>

      <div id="timeline-cleanup-result" class="muted" style="margin-top:12px;"></div>
    </div>
    <div class="modal-footer">
      <button onclick="closeTimelineCleanup()">Отмена</button>
      <button class="danger" onclick="doTimelineCleanup()" id="timeline-cleanup-btn">🗑 Удалить</button>
    </div>
  </div>
</div>

<div id="ui-confirm-overlay" class="modal-overlay top" onclick="closeUiConfirm(event)">
  <div class="modal small" onclick="event.stopPropagation()">
    <div class="modal-header"><h2 id="ui-confirm-title">Подтверждение</h2>
      <button class="modal-close" onclick="closeUiConfirm(null, false)">×</button>
    </div>
    <div class="modal-body">
      <p id="ui-confirm-text" class="ui-confirm-text"></p>
    </div>
    <div class="modal-footer">
      <button id="ui-confirm-cancel" onclick="closeUiConfirm(null, false)">Отмена</button>
      <button id="ui-confirm-ok" class="primary" onclick="closeUiConfirm(null, true)">OK</button>
    </div>
  </div>
</div>

<div id="ui-prompt-overlay" class="modal-overlay top" onclick="closeUiPrompt(event)">
  <div class="modal small" onclick="event.stopPropagation()">
    <div class="modal-header"><h2 id="ui-prompt-title">Ввод</h2>
      <button class="modal-close" onclick="closeUiPrompt(null, null)">×</button>
    </div>
    <div class="modal-body">
      <p id="ui-prompt-text" class="ui-confirm-text"></p>
      <div class="form-group">
        <input id="ui-prompt-input" type="text">
      </div>
      <div id="ui-prompt-error" class="muted" style="font-size:12px; color:var(--red); margin-top:4px; display:none;"></div>
    </div>
    <div class="modal-footer">
      <button id="ui-prompt-cancel" onclick="closeUiPrompt(null, null)">Отмена</button>
      <button id="ui-prompt-ok" class="primary" onclick="submitUiPrompt()">OK</button>
    </div>
  </div>
</div>

<div id="ui-alert-overlay" class="modal-overlay top" onclick="closeUiAlert(event)">
  <div class="modal small" onclick="event.stopPropagation()">
    <div class="modal-header"><h2 id="ui-alert-title">Сообщение</h2>
      <button class="modal-close" onclick="closeUiAlert(null)">×</button>
    </div>
    <div class="modal-body">
      <div id="ui-alert-icon" class="ui-alert-icon"></div>
      <p id="ui-alert-text" class="ui-confirm-text" style="text-align:center;"></p>
    </div>
    <div class="modal-footer">
      <button class="primary" onclick="closeUiAlert(null)">OK</button>
    </div>
  </div>
</div>

<script>
const ANALYTICS_ENABLED = __ANALYTICS_ENABLED__;
const STATUS_HISTORY_ENABLED = __STATUS_HISTORY_ENABLED__;
const WEBUI_VERSION = "__WEBUI_VERSION__";

const logsEl = document.getElementById("logs");
let LOG_RANGE_SECONDS = 3600;
let logPaused = false, lastSeq = 0;
let userScrolledUp = false;
let LAST_DEVICES = [];
let CURRENT_MODAL_IDX = -1;
let LOG_LEVEL_FILTER = "INFO";
let SEARCH_TERM = "", SEARCH_MATCHES = [], SEARCH_CURRENT = -1;
let VIEW = "dashboard";
let REVEALED_KEYS = {};
let CLOUD_DETAILS_OPEN = {};
let CLOUD_DEVICES = [], CLOUD_SELECTED = {};
let DEVICE_HISTORY_CACHE = {}, DEVICE_LATENCY_CACHE = {}, DEVICE_AVG_LATENCY_CACHE = {};
let SORT_KEY = "name", SORT_DIR = 1;

let LATENCY_PERIOD = 3600;      // v1.18.6: по умолчанию 1 час
let LATENCY_DATA = [];
let LATENCY_SORT_KEY = "avg", LATENCY_SORT_DIR = 1;
let FLAPPER_DATA = [];
let FLAPPER_SORT_KEY = "flaps", FLAPPER_SORT_DIR = -1;

let SCAN_RESULTS = [];
let SCAN_SUBNET = "";
let BRIDGE_SCAN_RUNNING = false;

let LOG_BUFFER = [];
const LOG_BUFFER_MAX = 5000;

let TOOLS_CONFIG = null;
let TOOLS_VIEW = "raw";
let TOOLS_SELECTED_IDX = -1;

let PREVIEW_DEVICES = [];
let PREVIEW_CURRENT = 0;

let REBUILD_POLL_TIMER = null;
let CLOUD_CACHE_FETCHED_AT = 0;
let EDIT_DEVICE_NAME = null;

const LOG_TS_RE = /^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})/;
const LEVEL_ORDER = { DEBUG:0, INFO:1, WARNING:2, ERROR:3, CRITICAL:4 };

const CLOUD_CREDS_KEY = "tuya_cloud_creds";
const THEME_KEY = "tuya_webui_theme";
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
  closeUiPrompt(null, val);
}

function closeUiPrompt(evt, result) {
  if (evt && evt.target && evt.target.id !== "ui-prompt-overlay") return;
  document.getElementById("ui-prompt-overlay").classList.remove("open");
  const r = _uiPromptResolver;
  _uiPromptResolver = null;
  _uiPromptValidate = null;
  if (r) r(result === undefined ? null : result);
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

function isMobile() { return window.innerWidth < 1000; }

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
function initTheme() {
  let t = "dark";
  try { t = localStorage.getItem(THEME_KEY) || "dark"; } catch (e) {}
  applyTheme(t);
}

initTheme();

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
  else VIEW = "dashboard";
  document.getElementById("view-dashboard").style.display = VIEW === "dashboard" ? "block" : "none";
  document.getElementById("view-analytics").style.display = VIEW === "analytics" ? "block" : "none";
  document.getElementById("view-import").style.display = VIEW === "import" ? "block" : "none";
  document.getElementById("view-tools").style.display = VIEW === "tools" ? "block" : "none";
  document.getElementById("nav-dashboard").className = VIEW === "dashboard" ? "active" : "";
  if (ANALYTICS_ENABLED) document.getElementById("nav-analytics").className = VIEW === "analytics" ? "active" : "";
  document.getElementById("nav-import").className = VIEW === "import" ? "active" : "";
  document.getElementById("nav-tools").className = VIEW === "tools" ? "active" : "";
  if (VIEW === "tools") loadConfig();
  if (VIEW === "import") loadBaseInfo();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}
function copyCode(text, extraStyle) {
  const st = extraStyle ? ` style="${extraStyle}"` : "";
  return `<span class="copy-row"><code data-copy="${escapeAttr(text)}"${st}>${escapeHtml(text)}</code><span class="copy-hint" title="Клик — выделить">📋</span></span>`;
}
function copyCodePlain(text, extraStyle) {
  const st = extraStyle ? ` style="${extraStyle}"` : "";
  return `<code data-copy="${escapeAttr(text)}"${st}>${escapeHtml(text)}</code>`;
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

// ==================== ПОИСК (v1.20) ====================
let DEVICE_SEARCH = "";
let CLOUD_SEARCH = "";
let _deviceSearchTimer = null;
let _cloudSearchTimer = null;

function onDeviceSearch(v) {
  DEVICE_SEARCH = (v || "").trim();
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

let _firstStatusLoad = true;
async function fetchStatus() {
  if (_firstStatusLoad && VIEW === "dashboard") {
    renderSkeleton(document.getElementById("devices-body"), 5, 5);
  }
  try {
    const r = await fetch("/api/status");
    const data = await r.json();
    const bv = data.version || "?";
    document.getElementById("bridge-version-badge").textContent = "Bridge v" + bv;
    document.getElementById("webui-version-badge").textContent = "WebUI v" + WEBUI_VERSION;
    const bs = document.getElementById("bridge-status");
    bs.textContent = data.bridge_status || "unknown";
    bs.className = "badge " + (data.bridge_status === "online" ? "online" : "offline");
    document.getElementById("bridge-uptime").textContent = data.uptime > 0 ? "· " + fmtUptime(data.uptime) : "";
    const devs = data.devices || [];
    for (const d of devs) if (DEVICE_HISTORY_CACHE[d.name]) d.history = DEVICE_HISTORY_CACHE[d.name];
    LAST_DEVICES = devs;
    const online = devs.filter(d => d.status === "online").length;
    document.getElementById("devices-summary").textContent =
      `устройства: ${devs.length} (${online} online, ${devs.length - online} offline)`;
    if (VIEW === "dashboard") {
      renderProblems(computeProblems(devs));
      renderDeviceTable(devs);
    }
    _firstStatusLoad = false;
    if (CURRENT_MODAL_IDX >= 0 && CURRENT_MODAL_IDX < LAST_DEVICES.length) {
      renderModal(LAST_DEVICES[CURRENT_MODAL_IDX]);
    }
  } catch (e) { console.error(e); }
}

function computeProblems(devs) {
  const now = Math.floor(Date.now()/1000);
  const out = [];
  for (const d of devs) {
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

function renderProblems(problems) {
  const block = document.getElementById("problems-block");
  const list = document.getElementById("problems-list");
  const count = document.getElementById("problems-count");
  if (problems.length === 0) { block.style.display = "none"; return; }
  block.style.display = "block";
  count.textContent = `(${problems.length})`;
  list.innerHTML = problems.map(p => {
    const idx = LAST_DEVICES.indexOf(p.dev);
    return `<div class="problem-item" onclick="showDevice(${idx})">
      <span>${escapeHtml(p.dev.friendly_name || p.dev.name)}</span>
      <span class="muted" style="font-size:12px;">${p.reasons.map(escapeHtml).join(" · ")}</span>
    </div>`;
  }).join("");
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
  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${DEVICE_SEARCH ? "Ничего не найдено" : "Нет устройств"}</td></tr>`;
    return;
  }
  const sorted = [...filtered].sort((a, b) => {
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
    const lat = `<span class="latency ${latencyClass(d.latency_ms)}">${latencyText(d.latency_ms)}</span>`;
    return `<tr class="device-row" onclick="showDevice(${realIdx})">
      <td><div style="font-weight:500;">${escapeHtml(d.friendly_name || d.name)}</div>${ip}</td>
      <td>${typeBadge(d.type)}</td>
      <td><span class="dot ${on ? 'online' : 'offline'}"></span>
          <span style="color:${on ? 'var(--green)' : 'var(--red)'}">${on ? 'online' : 'offline'}</span></td>
      <td>${lat}</td>
      <td class="muted" style="font-size:12px;">${d.last_seen ? fmtAgo(d.last_seen) : '—'}</td>
    </tr>`;
  }).join("");
  updateSortIndicators();
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
  for (const seg of segments) {
    const x1 = ((seg.start - startTs) / total) * width;
    const x2 = ((seg.end - startTs) / total) * width;
    const w = Math.max(1, x2 - x1);
    const color = seg.status === "online" ? "var(--green)" : "var(--red)";
    rects += `<rect x="${x1.toFixed(1)}" y="0" width="${w.toFixed(1)}" height="${height}" fill="${color}" opacity="0.7"/>`;
  }
  const svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${rects}</svg>`;
  const labels = `<div class="sparkline-labels"><span>${fmtDateTime(startTs)}</span><span>${fmtDateTime(now)}</span></div>`;
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
  const tsStart = points[0].ts; const tsEnd = points[points.length-1].ts;
  const tsSpan = (tsEnd - tsStart) || 1;
  const msValues = valid.map(p => p.ms);
  const msMin = Math.min(...msValues); const msMax = Math.max(...msValues);
  const msSpan = (msMax - msMin) || 1;
  const PAD = {top:12, bottom:12, left:4, right:4};
  const W = width - PAD.left - PAD.right; const H = height - PAD.top - PAD.bottom;
  let pathD = ""; let pen = false;
  for (const p of points) {
    const x = PAD.left + ((p.ts - tsStart) / tsSpan) * W;
    if (p.ms === null || p.ms === undefined) { pen = false; continue; }
    const y = PAD.top + H - ((p.ms - msMin) / msSpan) * H;
    pathD += (pen ? "L" : "M") + ` ${x.toFixed(1)} ${y.toFixed(1)} `; pen = true;
  }
  let timeouts = "";
  for (const p of points) {
    if (p.ms === null || p.ms === undefined) {
      const x = PAD.left + ((p.ts - tsStart) / tsSpan) * W;
      timeouts += `<circle cx="${x.toFixed(1)}" cy="${height-2}" r="1.5" fill="var(--red)" opacity="0.7"/>`;
    }
  }
  const avg = msValues.reduce((a, b) => a+b, 0) / msValues.length;
  const strokeColor = avg < 20 ? "var(--green)" : (avg < 100 ? "var(--yellow)" : "var(--red)");
  return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" style="color:var(--muted);">
    <path d="${pathD}" fill="none" stroke="${strokeColor}" stroke-width="1.5"/>${timeouts}
    <text x="${PAD.left+2}" y="10" fill="currentColor" font-size="9">${msMax} мс</text>
    <text x="${PAD.left+2}" y="${height-4}" fill="currentColor" font-size="9">${msMin} мс</text></svg>`;
}

async function revealSecret(name) {
  try {
    const r = await fetch(`/api/device/${encodeURIComponent(name)}/secret`);
    const data = await r.json();
    if (data.ok) {
      REVEALED_KEYS[name] = data.local_key;
      if (CURRENT_MODAL_IDX >= 0) renderModal(LAST_DEVICES[CURRENT_MODAL_IDX]);
    } else uiAlert("Ошибка", "Не удалось: " + (data.error || "unknown"), "error");
  } catch (e) { console.error(e); }
}
function hideSecret(name) {
  delete REVEALED_KEYS[name];
  if (CURRENT_MODAL_IDX >= 0) renderModal(LAST_DEVICES[CURRENT_MODAL_IDX]);
}

function renderModal(d) {
  if (!d) return;
  if (DEVICE_HISTORY_CACHE[d.name] && (!d.history || d.history.length === 0)) d.history = DEVICE_HISTORY_CACHE[d.name];
  const on = d.status === "online";
  let html = "";
  html += `<h3>Информация</h3><table class="detail-table">`;
  html += `<tr><td>Статус</td><td><span class="dot ${on?'online':'offline'}"></span>${escapeHtml(d.status || "unknown")}</td></tr>`;
  html += `<tr><td>Последняя активность</td><td>${d.last_seen ? fmtAgo(d.last_seen) : '—'}</td></tr>`;
  html += `<tr><td>Задержка</td><td><span class="latency ${latencyClass(d.latency_ms)}">${latencyText(d.latency_ms)}</span>${d.latency_ts ? ' <span class="muted">('+fmtAgo(d.latency_ts)+')</span>' : ''}</td></tr>`;
  html += `<tr><td>Имя (id)</td><td>${copyCode(d.name)}</td></tr>`;
  html += `<tr><td>Тип</td><td>${typeBadge(d.type)}${d.battery_powered ? ' <span class="badge battery">🔋 battery</span>' : ''}</td></tr>`;
  if (d.model) html += `<tr><td>Модель</td><td>${escapeHtml(d.model)}</td></tr>`;
  if (d.ip) html += `<tr><td>IP</td><td>${copyCode(d.ip)}</td></tr>`;
  if (d.version) html += `<tr><td>Версия протокола</td><td>${versionBadge(d.version)}</td></tr>`;
  if (d.tuya_id) html += `<tr><td>Tuya ID</td><td>${copyCode(d.tuya_id)}</td></tr>`;
  if (d.local_key_present) {
    const revealedKey = REVEALED_KEYS[d.name];
    if (revealedKey) {
      html += `<tr><td>Local key</td><td>
        ${copyCode(revealedKey)}
        <button onclick="hideSecret('${escapeHtml(d.name)}')" style="margin-left:4px; padding:2px 8px; font-size:11px;">Скрыть</button></td></tr>`;
    } else {
      html += `<tr><td>Local key</td><td><span class="secret-masked">••••••••••</span>
        <button onclick="revealSecret('${escapeHtml(d.name)}')" style="margin-left:6px; padding:2px 8px; font-size:11px;">👁 Показать</button></td></tr>`;
    }
  }
  html += `</table>`;

  html += `<div style="display:flex; gap:8px; margin-top:12px; padding-top:12px; border-top:1px solid var(--border); flex-wrap:wrap;">
    <button onclick="editDevice('${escapeHtml(d.name)}')" style="padding:4px 12px; font-size:12px;">✏️ Изменить IP / Key / Version</button>
    <button class="danger" onclick="deleteDevice('${escapeHtml(d.name)}')" style="padding:4px 12px; font-size:12px;">🗑 Удалить</button>
  </div>`;

  if (d.type === "climate" && (d.presets?.length > 0 || d.min_temp || d.max_temp)) {
    html += `<h3>Климат</h3><table class="detail-table">`;
    if (d.min_temp !== null && d.min_temp !== undefined && d.max_temp !== null && d.max_temp !== undefined)
      html += `<tr><td>Диапазон</td><td>${d.min_temp}°C — ${d.max_temp}°C (шаг ${d.temp_step || "?"})</td></tr>`;
    if (d.presets?.length > 0) {
      const pmap = d.preset_map || {};
      html += `<tr><td>Пресеты</td><td>${escapeHtml(d.presets.map(p => pmap[p] || p).join(", "))}</td></tr>`;
    }
    html += `</table>`;
  }

  const histAll = d.history || [];
  if (STATUS_HISTORY_ENABLED && histAll.length > 0) {
    html += `<h3>Хронология статуса</h3><div class="sparkline">${sparklineSvgWithLabels(histAll, 700, 40)}</div>`;
  }
  if (STATUS_HISTORY_ENABLED) {
    const latPoints = DEVICE_LATENCY_CACHE[d.name] || [];
    const avgData = DEVICE_AVG_LATENCY_CACHE[d.name];
    if (latPoints.length > 0) {
      const valid = latPoints.filter(p => p.ms !== null && p.ms !== undefined);
      const timeouts = latPoints.length - valid.length;
      let stats = "";
      if (avgData && avgData.avg !== null && avgData.avg !== undefined) {
        stats += `сред. 24ч <strong>${avgData.avg} мс</strong> (${avgData.count} замеров)`;
      }
      if (d.latency_ms !== null && d.latency_ms !== undefined) { if (stats) stats += " · "; stats += `сейчас <strong>${d.latency_ms} мс</strong>`; }
      if (timeouts > 0) { if (stats) stats += " · "; stats += `<span style="color:var(--red)">timeout: ${timeouts}</span>`; }
      const cnt = latPoints.length;
      html += `<h3>Задержка (24ч, ${cnt}) <span class="muted" style="float:right; text-transform:none; font-weight:normal;">${stats}</span></h3>`;
      html += `<div class="sparkline" style="height:60px;">${latencySparklineSvg(latPoints, 700, 60)}</div>`;
    }
  }

  const cache = d.cache || {}; const dps_map = d.dps_map || {};
  const keys = Object.keys(cache);
  if (keys.length > 0) {
    html += `<h3>Кэш состояния (${keys.length})</h3><table class="detail-table">`;
    const sorted = keys.sort((a, b) => {
      const ai = parseInt(a), bi = parseInt(b);
      if (!isNaN(ai) && !isNaN(bi)) return ai - bi;
      return a.localeCompare(b);
    });
    for (const dp of sorted) {
      const info = dps_map[dp] || {}; const name = info.name || "?";
      const val = JSON.stringify(cache[dp]);
      html += `<tr><td>${escapeHtml(dp)} <span class="muted">(${escapeHtml(name)})</span></td>
        <td>${copyCode(val)}</td></tr>`;
    }
    html += `</table>`;
  }

  if (STATUS_HISTORY_ENABLED) {
    const hist = histAll.slice().reverse();
    if (hist.length > 0) {
      html += `<h3>История статуса (последние ${hist.length})</h3><div class="history-scroll">`;
      for (const h of hist) html += `<div class="history-item ${h.status}">${fmtDateTime(h.ts)} — ${h.status}</div>`;
      html += `</div>`;
    }
  }
  document.getElementById("modal-body").innerHTML = html;
}

async function showDevice(idx) {
  const d = LAST_DEVICES[idx];
  if (!d) return;
  CURRENT_MODAL_IDX = idx;
  document.getElementById("modal-title").textContent = (d.friendly_name || d.name) + " [" + (d.type || "?") + "]";
  if (DEVICE_HISTORY_CACHE[d.name]) d.history = DEVICE_HISTORY_CACHE[d.name];
  renderModal(d);
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
    if (rerender) renderModal(d);
  }
}

function closeModal(evt) {
  if (evt && evt.target && evt.target.id !== "modal-overlay") return;
  CURRENT_MODAL_IDX = -1;
  document.getElementById("modal-overlay").classList.remove("open");
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeModal(); closeCloudModal(); closeDbCleanup(); closePreview(); closeEditDevice(); closeTimelineCleanup();
    closeUiAlert(); closeUiConfirm(null, false); closeUiPrompt(null, null);
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

  document.getElementById("edit-device-title").textContent = `Редактирование: ${d.friendly_name || name}`;

  document.getElementById("edit-device-ip-check").checked = false;
  document.getElementById("edit-device-version-check").checked = false;
  document.getElementById("edit-device-key-check").checked = false;

  document.getElementById("edit-device-ip").value = d.ip || "";
  document.getElementById("edit-device-version").value = d.version || "3.3";
  document.getElementById("edit-device-key").value = "";
  document.getElementById("edit-device-key").type = "password";
  document.getElementById("edit-device-key-toggle").textContent = "👁 Показать";

  document.getElementById("edit-device-current-version").textContent = d.version || "?";

  document.getElementById("edit-device-ip").disabled = true;
  document.getElementById("edit-device-version").disabled = true;
  document.getElementById("edit-device-key").disabled = true;
  document.getElementById("edit-device-key-toggle").disabled = true;

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
  btn.onclick = () => submitEditDevice();
  btn.classList.add("primary");
  btn.textContent = "Сохранить";

  updateEditSubmitState();
  document.getElementById("edit-device-overlay").classList.add("open");
}

function closeEditDevice(evt) {
  if (evt && evt.target && evt.target.id !== "edit-device-overlay") return;
  EDIT_DEVICE_NAME = null;
  document.getElementById("edit-device-overlay").classList.remove("open");
}

function toggleEditKeyVisibility() {
  const inp = document.getElementById("edit-device-key");
  const btn = document.getElementById("edit-device-key-toggle");
  if (inp.disabled) return;
  if (inp.type === "password") {
    inp.type = "text";
    btn.textContent = "🙈 Скрыть";
  } else {
    inp.type = "password";
    btn.textContent = "👁 Показать";
  }
}

function updateEditSubmitState() {
  if (!EDIT_DEVICE_NAME) return;
  const d = LAST_DEVICES.find(x => x.name === EDIT_DEVICE_NAME);
  if (!d) return;

  const ipChk = document.getElementById("edit-device-ip-check").checked;
  const verChk = document.getElementById("edit-device-version-check").checked;
  const keyChk = document.getElementById("edit-device-key-check").checked;

  const ipEl = document.getElementById("edit-device-ip");
  const verEl = document.getElementById("edit-device-version");
  const keyEl = document.getElementById("edit-device-key");
  const keyTgl = document.getElementById("edit-device-key-toggle");

  ipEl.disabled = !ipChk;
  verEl.disabled = !verChk;
  keyEl.disabled = !keyChk;
  keyTgl.disabled = !keyChk;

  let hasChanges = false;

  if (ipChk) {
    const ip = ipEl.value.trim();
    if (ip && /^\d+\.\d+\.\d+\.\d+$/.test(ip) && ip !== (d.ip || "")) hasChanges = true;
  }
  if (verChk) {
    const v = verEl.value;
    if (v && v !== (d.version || "")) hasChanges = true;
  }
  if (keyChk) {
    const k = keyEl.value;
    if (k && k.length > 0) hasChanges = true;
  }

  const btn = document.getElementById("edit-device-submit");
  if (btn.textContent !== "Закрыть") {
    btn.disabled = !hasChanges;
  }
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

  const ipChk = document.getElementById("edit-device-ip-check").checked;
  const verChk = document.getElementById("edit-device-version-check").checked;
  const keyChk = document.getElementById("edit-device-key-check").checked;

  const changes = {};
  if (ipChk) {
    const ip = document.getElementById("edit-device-ip").value.trim();
    if (!ip || !/^\d+\.\d+\.\d+\.\d+$/.test(ip)) {
      errEl.style.display = "block";
      errEl.textContent = "Некорректный IP-адрес";
      return;
    }
    if (ip !== (d.ip || "")) changes.ip = ip;
  }
  if (verChk) {
    const v = document.getElementById("edit-device-version").value;
    if (v !== (d.version || "")) changes.version = v;
  }
  if (keyChk) {
    const k = document.getElementById("edit-device-key").value;
    if (k) changes.local_key = k;
  }

  if (Object.keys(changes).length === 0) {
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
      rawWrap.style.display = "none";
      rawText.textContent = "";
      btn.disabled = false;
      btn.textContent = "Закрыть";
      btn.classList.remove("primary");
      btn.onclick = () => { closeEditDevice(); closeModal(); fetchStatus(); };
      fetchStatus();
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
      btn.textContent = "Сохранить";
      updateEditSubmitState();
    }
  } catch (e) {
    errEl.style.display = "block";
    errEl.style.background = "rgba(215,58,73,0.08)";
    errEl.style.color = "var(--red)";
    errEl.style.borderColor = "rgba(215,58,73,0.25)";
    errEl.textContent = "Ошибка сети: " + e.message;
    btn.disabled = false;
    btn.textContent = "Сохранить";
    updateEditSubmitState();
  }
}

async function deleteDevice(name) {
  const d = LAST_DEVICES.find(x => x.name === name) || {};
  const ok = await uiConfirm("Удалить устройство?", `Удалить "${d.friendly_name || name}"?`, {danger:true, okText:"Удалить"});
  if (!ok) return;
  try {
    const r = await fetch(`/api/device/${encodeURIComponent(name)}/delete`, { method: "POST" });
    const data = await r.json();
    if (data.ok) { closeModal(); delete DEVICE_HISTORY_CACHE[name]; delete REVEALED_KEYS[name]; fetchStatus(); }
    else uiAlert("Ошибка", "Не удалось удалить: " + (data.error || "unknown"), "error");
  } catch (e) { uiAlert("Ошибка", "Ошибка сети: " + e.message, "error"); }
}

function logLevelPass(level) { return (LEVEL_ORDER[level] || 0) >= (LEVEL_ORDER[LOG_LEVEL_FILTER] || 0); }
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
  for (const item of LOG_BUFFER) {
    if (!logLevelPass(item.level)) continue;
    if (!logTimePass(item)) continue;
    if (!logMatchesSearch(item.msg)) continue;
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
  logsEl.appendChild(frag);
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
}
function appendLog(item) {
  if (item.seq <= lastSeq) return;
  lastSeq = item.seq;
  LOG_BUFFER.push(item);
  if (LOG_BUFFER.length > LOG_BUFFER_MAX) LOG_BUFFER.shift();
  if (logLevelPass(item.level) && logTimePass(item) && logMatchesSearch(item.msg)) {
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
  LOG_LEVEL_FILTER = level;
  document.querySelectorAll(".logs-toolbar button[data-level]").forEach(b => b.classList.toggle("active", b.dataset.level === level));
  renderLogsFromBuffer();
}
function setLogRange(seconds) {
  LOG_RANGE_SECONDS = parseInt(seconds, 10) || 0;
  document.querySelectorAll(".logs-toolbar .time-btn-group button").forEach(b => {
    const r = parseInt(b.dataset.range, 10) || 0;
    b.classList.toggle("active", r === LOG_RANGE_SECONDS);
  });
  renderLogsFromBuffer();
}
function pauseLogs() {
  logPaused = !logPaused;
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
  renderLogsFromBuffer();
}
function findNext() {
  if (SEARCH_MATCHES.length === 0) return;
  SEARCH_CURRENT = (SEARCH_CURRENT + 1) % SEARCH_MATCHES.length;
  SEARCH_MATCHES.forEach(el => el.classList.remove("current-match"));
  const el = SEARCH_MATCHES[SEARCH_CURRENT]; el.classList.add("current-match");
  el.scrollIntoView({block: "center", behavior: "smooth"});
}
function findPrev() {
  if (SEARCH_MATCHES.length === 0) return;
  SEARCH_CURRENT = (SEARCH_CURRENT - 1 + SEARCH_MATCHES.length) % SEARCH_MATCHES.length;
  SEARCH_MATCHES.forEach(el => el.classList.remove("current-match"));
  const el = SEARCH_MATCHES[SEARCH_CURRENT]; el.classList.add("current-match");
  el.scrollIntoView({block: "center", behavior: "smooth"});
}
function downloadLogs() {
  const lines = [];
  logsEl.querySelectorAll(".line").forEach(el => lines.push(el.dataset.msg || ""));
  const blob = new Blob([lines.join("\n")], {type: "text/plain"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url;
  a.download = `bridge-log-${new Date().toISOString().slice(0,10)}.txt`;
  a.click();
  // v1.19: revokeObjectURL сразу после click() иногда не даёт
  // браузеру начать скачивание. Даём 1 секунду форы.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function scrollLogsToBottom() {
  userScrolledUp = false;
  logsEl.scrollTop = logsEl.scrollHeight;
  document.getElementById("scroll-down-btn").classList.remove("visible");
}
logsEl.addEventListener("scroll", () => {
  const atBottom = logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight < 50;
  userScrolledUp = !atBottom;
  const btn = document.getElementById("scroll-down-btn");
  if (userScrolledUp) btn.classList.add("visible");
  else btn.classList.remove("visible");
});

let SSE_RECONNECT_DELAY = 3000;
function connectSSE() {
  const es = new EventSource(`/api/logs/stream?since=${lastSeq}&v=${Date.now()}`);
  es.onmessage = (e) => { try { const item = JSON.parse(e.data); if (item?.seq) appendLog(item); } catch {} };
  es.onerror = () => { es.close(); SSE_RECONNECT_DELAY = Math.min(SSE_RECONNECT_DELAY * 2, 30000); setTimeout(connectSSE, SSE_RECONNECT_DELAY); };
  es.onopen = () => { SSE_RECONNECT_DELAY = 3000; };
}
async function loadLogHistory() {
  try {
    const r = await fetch("/api/logs/history?tail=1000&v=" + Date.now());
    const data = await r.json();
    for (const item of (data.logs || [])) {
      if (item.seq <= lastSeq) continue;
      lastSeq = item.seq;
      LOG_BUFFER.push(item);
      if (LOG_BUFFER.length > LOG_BUFFER_MAX) LOG_BUFFER.shift();
    }
    renderLogsFromBuffer();
  } catch {}
}

async function doCleanup() {
  const btn = document.getElementById("cleanup-btn");
  const res = document.getElementById("cleanup-result");
  const ok = await uiConfirm("Очистить Discovery?", "Отправить команду bridge на очистку Discovery-топиков?", {okText:"Очистить"});
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
function openDbCleanup() { document.getElementById("db-cleanup-overlay").classList.add("open"); }
function closeDbCleanup(evt) {
  if (evt && evt.target && evt.target.id !== "db-cleanup-overlay") return;
  document.getElementById("db-cleanup-overlay").classList.remove("open");
}
async function doDbCleanup() {
  const btn = document.getElementById("db-cleanup-btn");
  const res = document.getElementById("db-cleanup-result");
  const days = parseInt(document.getElementById("db-keep-days").value) || 0;
  const hours = parseInt(document.getElementById("db-keep-hours").value) || 0;
  if (days === 0 && hours === 0) { res.textContent = "❌ Укажи дни или часы"; return; }
  const ok = await uiConfirm("Очистить БД", `Удалить записи старше ${days}д ${hours}ч?`, {danger:true, okText:"Удалить"});
  if (!ok) return;
  btn.disabled = true; res.innerHTML = '<span class="spin"></span> удаление…';
  try {
    const r = await fetch("/api/db/cleanup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_days: days, keep_hours: hours })
    });
    const data = await r.json();
    if (data.ok) { res.textContent = `✅ Удалено: ${data.deleted}`; setTimeout(closeDbCleanup, 1500); }
    else res.textContent = "❌ " + (data.error || "ошибка");
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
async function loadBaseInfo() {
  try {
    const r = await fetch("/api/base/info");
    const data = await r.json();
    const t = data.tinytuya;
    const u = data.tuya_local;
    const tEl = document.getElementById("base-tinytuya-info");
    const uEl = document.getElementById("base-tuya-local-info");
    if (tEl) tEl.textContent = t.exists ? `${t.count} устройств · ${fmtAgo(t.modified)}` : "не создана";
    if (uEl) uEl.textContent = u.exists ? `${u.count} шаблонов · обновлена ${fmtAgo(u.modified)}` : "не найдена";
  } catch (e) { console.error("loadBaseInfo", e); }
}
async function updateTuyaLocalDb() {
  const btn = document.getElementById("tuya-local-update-btn");
  const res = document.getElementById("base-update-result");
  const ok = await uiConfirm("Обновить tuya-local?", "Скачать/обновить базу tuya-local? (~50 МБ, займёт до 2 мин)", {okText:"Скачать"});
  if (!ok) return;
  btn.disabled = true; res.innerHTML = '<span class="spin"></span> скачивание…';
  try {
    const r = await fetch("/api/base/tuya-local/update", { method: "POST" });
    const data = await r.json();
    if (data.ok) {
      res.textContent = "✅ " + (data.message || "обновлено");
      loadBaseInfo();
    } else {
      res.textContent = "❌ " + (data.error || "ошибка") + " (эвристика остаётся)";
    }
  } catch (e) { res.textContent = "❌ " + e.message; }
  btn.disabled = false;
  setTimeout(() => res.textContent = "", 20000);
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
  progressWrap.style.display = "block";
  progressText.innerHTML = '<span class="spin"></span> запуск…';
  progressFill.style.width = "0%";

  try {
    const r = await fetch("/api/base/tinytuya/rebuild", { method: "POST", headers: {"Content-Type":"application/json"}, body: "{}" });
    const data = await r.json();
    if (!data.ok) {
      progressText.textContent = "❌ " + (data.error || "ошибка");
      btn.disabled = false;
      return;
    }
    progressText.textContent = "запущено, ждём прогресс…";
    pollRebuildProgress();
  } catch (e) {
    progressText.textContent = "❌ " + e.message;
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
  const filtered = CLOUD_SEARCH ? CLOUD_DEVICES.filter(cloudSearchPass) : CLOUD_DEVICES;
  const searchInfo = document.getElementById("cloud-search-info");
  if (searchInfo) {
    searchInfo.textContent = CLOUD_SEARCH
      ? `найдено ${filtered.length} из ${CLOUD_DEVICES.length}`
      : "";
  }
  count.textContent = CLOUD_SEARCH
    ? `${filtered.length} из ${CLOUD_DEVICES.length}`
    : `${CLOUD_DEVICES.length} устройств`;
  // v1.18.12: сохраняем scrollTop внутреннего скроллера — иначе при
  // перерисовке (Выбрать все / Снять всё) список "улетает" вверх.
  const oldScroller = c.querySelector('div[style*="overflow-y"]');
  const savedScrollTop = oldScroller ? oldScroller.scrollTop : 0;
  let html = `<div style="max-height:500px; overflow-y:auto;"><table>
    <thead><tr><th style="width:40px;"></th><th>Имя</th><th>Тип</th><th>Продукт</th><th>Local key</th><th>DP</th><th>Online</th></tr></thead><tbody>`;
  for (const d of filtered) {
    const i = CLOUD_DEVICES.indexOf(d);
    const sel = CLOUD_SELECTED[i] ? "checked" : "";
    const cls = CLOUD_SELECTED[i] ? "background:rgba(3,102,214,0.08);" : "";
    const key = d.local_key ? copyCode(d.local_key, "font-size:11px;") : '<span class="muted">—</span>';
    const dpCount = d.mapping ? Object.keys(d.mapping).length : 0;
    const online = d.online
      ? '<span class="dot online"></span><span class="state-on" style="margin-left:4px;">on</span>'
      : '<span class="dot offline"></span><span class="state-off" style="margin-left:4px;">off</span>';
    html += `<tr style="${cls}" data-cloud-idx="${i}">
      <td><input type="checkbox" ${sel} onchange="toggleCloudSelect(${i}, this.checked)"></td>
      <td style="cursor:pointer;" onclick="showCloudDevice(${i})">
        <div style="font-weight:500;">${escapeHtml(d.name || '?')}</div>
        <div class="muted" style="font-size:11px; font-family:ui-monospace,monospace;">${escapeHtml(d.id || '')}</div>
      </td>
      <td>${typeBadge(d.type_guess || 'switch')}</td>
      <td>${escapeHtml(d.product_name || '?')}</td>
      <td>${key}</td>
      <td class="muted" style="font-size:11px;">${dpCount}</td>
      <td>${online}</td>
    </tr>`;
  }
  html += "</tbody></table></div>";
  c.innerHTML = html;
  // v1.18.12: восстанавливаем scrollTop после перерисовки.
  const newScroller = c.querySelector('div[style*="overflow-y"]');
  if (newScroller && savedScrollTop) newScroller.scrollTop = savedScrollTop;
  actions.style.display = "flex";
  updateSelectedCount();
}
function toggleCloudSelect(idx, checked) {
  if (checked) CLOUD_SELECTED[idx] = true; else delete CLOUD_SELECTED[idx];
  // v1.18.12: не перерисовываем весь список — только фон строки
  // и счётчик. renderCloudDevices() сбрасывал scrollTop внутреннего div'а.
  const row = document.querySelector(`#cloud-devices-container tr[data-cloud-idx="${idx}"]`);
  if (row) row.style.background = checked ? "rgba(3,102,214,0.08)" : "";
  updateSelectedCount();
}
function selectAllCloud(v) {
  CLOUD_SELECTED = {};
  if (v) for (let i = 0; i < CLOUD_DEVICES.length; i++) CLOUD_SELECTED[i] = true;
  renderCloudDevices();
}
function updateSelectedCount() { document.getElementById("selected-count").textContent = `Выбрано: ${Object.keys(CLOUD_SELECTED).length}`; }

function showCloudDevice(idx) {
  const d = CLOUD_DEVICES[idx];
  if (!d) return;
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
  html += row("Имя", d.name);
  html += row("ID", d.id, true);
  html += `<tr><td>Category (raw)</td><td><span class="type-badge">${escapeHtml(d.category || '?')}</span> <span class="muted" style="font-size:11px;">(код Tuya)</span></td></tr>`;
  html += `<tr><td>Тип устройства</td><td><span class="type-badge primary">${escapeHtml(d.type_guess || 'switch')}</span></td></tr>`;
  html += row("Продукт", d.product_name);
  html += row("Product ID", d.product_id, true);
  html += row("Модель", d.model);
  html += row("UUID", d.uuid, true);
  html += row("MAC", d.mac, true);
  html += row("Local key", d.local_key, true);
  if (d.gateway_id) html += row("Gateway ID", d.gateway_id, true);
  if (d._key_from_parent) html += `<tr><td>Key source</td><td class="muted">взят от parent <code>${escapeHtml(d._key_from_parent)}</code></td></tr>`;
  html += `<tr><td>Online</td><td>${d.online ? '✅' : '❌'}</td></tr>`;
  html += `</table>`;

  const cloudStatus = d.cloud_status || {};
  if (Object.keys(cloudStatus).length > 0) {
    html += `<h3>Status из облака (${Object.keys(cloudStatus).length} значений)</h3>`;
    html += `<table class="detail-table"><thead><tr><th>Код</th><th>Значение</th></tr></thead><tbody>`;
    for (const k of Object.keys(cloudStatus).sort()) {
      const v = cloudStatus[k];
      html += `<tr><td>${copyCodePlain(k)}</td><td>${copyCode(displayValue(v))}</td></tr>`;
    }
    html += `</tbody></table>`;
  } else {
    html += `<h3>Status из облака</h3><div class="muted">Облако не вернуло status для этого устройства.</div>`;
  }

  const generated = d.dps_map_generated || {};
  const codeToName = {};
  if (d._raw_properties) {
    for (const section of ["functions", "status"]) {
      const arr = d._raw_properties[section];
      if (Array.isArray(arr)) {
        for (const item of arr) {
          if (item.code) codeToName[item.code] = item.name || "";
        }
      }
    }
  }
  const codeToValue = {};
  if (d._raw_cloud?.status) {
    for (const item of d._raw_cloud.status) {
      if (item.code) codeToValue[item.code] = item.value;
    }
  }

  if (Object.keys(mapping).length > 0) {
    html += `<h3>Сопоставление DP (${Object.keys(mapping).length} DP)</h3>`;
    html += `<table class="detail-table"><thead><tr>
      <th>DP</th><th>Code</th><th>Имя</th><th>Тип</th><th>Значения</th><th>Текущее</th><th>Component</th>
    </tr></thead><tbody>`;
    const sorted = Object.entries(mapping).sort(([a],[b]) => parseInt(a) - parseInt(b));
    for (const [dp, m] of sorted) {
      const code = m.code || "";
      const name = codeToName[code] || m.name || "";
      const dtype = m.type || "?";
      const vals = m.values && Object.keys(m.values).length ? JSON.stringify(m.values) : '—';
      const cur = codeToValue[code];
      const curStr = cur !== undefined ? displayValue(cur) : "—";
      const gen = generated[dp] || {};
      const comp = gen.component || "—";
      const isJunk = JUNK_DP_CODES.has(code);
      const rowCls = isJunk ? "junk-row" : "";
      html += `<tr class="${rowCls}">
        <td><strong>${escapeHtml(dp)}</strong>${isJunk ? ' <span class="badge junk">мусор</span>' : ''}</td>
        <td>${copyCodePlain(code)}</td>
        <td class="muted" style="font-size:11px;">${escapeHtml(name)}</td>
        <td><span class="type-badge">${escapeHtml(dtype)}</span></td>
        <td><code style="font-size:10px;">${escapeHtml(vals)}</code></td>
        <td>${copyCodePlain(curStr)}</td>
        <td><span class="type-badge">${escapeHtml(comp)}</span></td>
      </tr>`;
    }
    html += `</tbody></table>`;
  } else {
    html += `<h3>Сопоставление DP</h3><div class="muted">Облако не вернуло mapping для этого устройства.</div>`;
  }

  html += `<h3>Рекомендации</h3><table class="detail-table">`;
  html += `<tr><td>Тип устройства</td><td><span class="type-badge primary">${escapeHtml(d.type_guess || 'switch')}</span></td></tr>`;
  html += `<tr><td>Версия протокола (guess)</td><td>${copyCodePlain(d.version_guess || '3.3')}</td></tr>`;
  html += `</table>`;

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
    <pre style="background:var(--bg); padding:12px; border-radius:4px; font-size:11px; overflow-x:auto; max-height:400px; margin-top:8px;"><code class="json-view">${rawJson}</code></pre>
  </details>`;

  document.getElementById("cloud-modal-title").textContent = d.name || d.id;
  document.getElementById("cloud-modal-body").innerHTML = html;
  const codeEl = document.querySelector("#cloud-modal-body code.json-view");
  if (codeEl) highlightJsonInto(codeEl);
  document.getElementById("cloud-modal-overlay").classList.add("open");
}
function closeCloudModal(evt) {
  if (evt && evt.target && evt.target.id !== "cloud-modal-overlay") return;
  document.getElementById("cloud-modal-overlay").classList.remove("open");
}

// ===== Import + Preview =====
// v1.18.10: единая точка получения mapping устройства из Cloud-кэша.
// Раньше код смотрел только на d._raw_cloud.mapping, но в кэше
// mapping лежит на верхнем уровне (d.mapping), а _raw_cloud.mapping
// отсутствует. Теперь — цепочка фолбэков.
function getDeviceMapping(d) {
  if (!d) return {};
  if (d.mapping && Object.keys(d.mapping).length > 0) return d.mapping;
  if (d._raw_cloud && d._raw_cloud.mapping && Object.keys(d._raw_cloud.mapping).length > 0) {
    return d._raw_cloud.mapping;
  }
  if (d.dps_map_generated && Object.keys(d.dps_map_generated).length > 0) {
    const m = {};
    for (const [dp, info] of Object.entries(d.dps_map_generated)) {
      m[dp] = {
        code: info.name || ("dp_" + dp),
        type: info.component || "",
        values: {},
        name: info.name || "",
      };
    }
    return m;
  }
  return {};
}

async function importSelected() {
  const sel = Object.keys(CLOUD_SELECTED).map(i => CLOUD_DEVICES[parseInt(i)]);
  if (sel.length === 0) { uiAlert("Импорт", "Ничего не выбрано", "warning"); return; }

  const prepared = [];
  for (const d of sel) {
    const defaultName = (d.name || d.id).toLowerCase().replace(/[^a-z0-9а-яё]+/gi, "_").replace(/^_+|_+$/g, "").slice(0, 40) || ("device_" + d.id.slice(-6));
    const ip = await uiPrompt(
      "IP-адрес",
      `IP для "${d.name || d.id}":`,
      {
        placeholder: "192.168.0.100",
        okText: "Далее",
        validate: (v) => {
          if (!v || !/^\d+\.\d+\.\d+\.\d+$/.test(v.trim())) return "Некорректный IP-адрес";
          return null;
        }
      }
    );
    if (ip === null) return;

    const friendly = await uiPrompt(
      "Отображаемое имя",
      `Отображаемое имя для "${d.name || d.id}":`,
      { value: d.name || d.id, okText: "Готово" }
    );
    if (friendly === null) return;

    let dps_map = d.dps_map_generated || {};

    const known = LAST_DEVICES.find(x => x.ip === ip.trim());
    let version = d.version_guess || "3.3";
    let probeInfo = null;
    if (!known) {
      probeInfo = { skipped: true, reason: "auto probe disabled, будет по кнопке в превью" };
    }

    prepared.push({
      id: d.id, name: defaultName, friendly_name: friendly.trim(),
      ip: ip.trim(), local_key: d.local_key || "",
      version: version, type: d.type_guess || "switch",
      model: d.product_name || "", battery_powered: false, enabled: true,
      dps_map: dps_map,
      _cloud_ref: d,
      _probe: probeInfo,
    });
  }

  PREVIEW_DEVICES = prepared.map(p => ({ device: p, enabled_dps: {} }));

  for (const item of PREVIEW_DEVICES) {
    const mapping = getDeviceMapping(item.device._cloud_ref);
    if (Object.keys(mapping).length === 0) {
      item.enabled_dps = {};
      item.needs_probe = true;
    } else {
      for (const [dp, m] of Object.entries(mapping)) {
        const code = m.code || "";
        const isJunk = JUNK_DP_CODES.has(code);
        item.enabled_dps[dp] = !isJunk;
      }
    }
  }

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
  const title = document.getElementById("preview-title");
  const counter = document.getElementById("preview-counter");
  const nav = document.getElementById("preview-mobile-nav");

  if (isMobile()) {
    nav.style.display = "flex";
    counter.textContent = `${PREVIEW_CURRENT + 1}/${PREVIEW_DEVICES.length}`;
    renderPreviewMobile(body);
  } else {
    nav.style.display = "none";
    renderPreviewDesktop(body);
  }
  title.textContent = `Превью импорта (${PREVIEW_DEVICES.length} устройств)`;
  body.scrollTop = scrollTop;

  // v1.18.11.fix: не даём чекбоксу получить фокус — иначе Firefox
  // делает нативный focus-scroll внутри overflow:auto, и список
  // "улетает" вверх. Клик обрабатываем сами: переключаем checked
  // и кидаем change. Клавиатура (Space) сохраняется — там e.detail===0.
  if (body && !body.dataset.noFocusBound) {
    body.dataset.noFocusBound = "1";
    body.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || e.detail === 0) return;
      const cb = e.target.closest('input[type="checkbox"], input[type="radio"]');
      if (!cb) return;
      e.preventDefault();
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change", { bubbles: true }));
    }, true);
  }
}

function renderPreviewDeviceBlock(item, idx) {
  const d = item.device;
  const mapping = getDeviceMapping(d._cloud_ref);
  const codeToName = {};
  if (d._cloud_ref?._raw_properties) {
    for (const section of ["functions", "status"]) {
      const arr = d._cloud_ref._raw_properties[section];
      if (Array.isArray(arr)) {
        for (const i2 of arr) if (i2.code) codeToName[i2.code] = i2.name || "";
      }
    }
  }
  const total = Object.keys(mapping).length;
  const enabled = Object.values(item.enabled_dps).filter(x => x).length;

  let html = `<div class="preview-device" data-idx="${idx}">
    <div class="preview-device-header">
      <span>${escapeHtml(d.friendly_name)} <span class="muted" style="font-weight:400; font-size:11px;">(${escapeHtml(d.name)})</span></span>
      <span class="muted" style="font-size:11px;">${escapeHtml(d.ip)} · ${versionBadge(d.version)} · <span class="preview-dp-count">${enabled}/${total} DP</span> <span class="muted preview-probe-status" data-idx="${idx}" style="font-size:11px;">${item.probe_status_html || ""}</span></span>
    </div>
    <div class="preview-device-body">`;

  if (Object.keys(mapping).length === 0) {
    html += `<div class="cloud-warn">⚠️ Cloud не вернул mapping. Нажми «🔍 Probe» — DP будут сопоставлены по значениям из Cloud status + типам.</div>`;
  } else {
    html += `<table class="detail-table preview-dp-table"><thead><tr>
      <th style="width:40px;"></th><th>DP</th><th>Code</th><th>Имя</th><th>Component</th>
    </tr></thead><tbody>`;
    const sorted = Object.entries(mapping).sort(([a],[b]) => parseInt(a) - parseInt(b));
    for (const [dp, m] of sorted) {
      const code = m.code || "";
      const name = codeToName[code] || m.name || "";
      const isJunk = JUNK_DP_CODES.has(code);
      const checked = item.enabled_dps[dp] ? "checked" : "";
      const rowCls = isJunk ? "junk-row" : "";
      html += `<tr class="${rowCls}">
        <td><input type="checkbox" ${checked} onchange="togglePreviewDp(${idx}, '${escapeAttr(dp)}', this.checked)"></td>
        <td><strong>${escapeHtml(dp)}</strong></td>
        <td>${copyCodePlain(code)}${isJunk ? ' <span class="badge junk">мусор</span>' : ''}</td>
        <td class="muted" style="font-size:11px;">${escapeHtml(name)}</td>
        <td><span class="type-badge">${escapeHtml(d.type || '?')}</span></td>
      </tr>`;
    }
    html += `</tbody></table>`;
  }
  html += `</div></div>`;
  return html;
}

function renderPreviewDesktop(body) {
  let html = "";
  html += `<p class="muted">Проверьте DP перед импортом. Мусорные (🗑) выключены по умолчанию. <b>countdown_*</b> включены.</p>`;
  for (let i = 0; i < PREVIEW_DEVICES.length; i++) {
    html += renderPreviewDeviceBlock(PREVIEW_DEVICES[i], i);
  }
  body.innerHTML = html;
}

function renderPreviewMobile(body) {
  if (PREVIEW_CURRENT < 0 || PREVIEW_CURRENT >= PREVIEW_DEVICES.length) return;
  let html = "";
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

  // Обновляем только счётчик «N/M DP» в шапке карточки —
  // без перерисовки всего списка.
  const card = document.querySelector(`.preview-device[data-idx="${deviceIdx}"]`);
  if (card) {
    const counterEl = card.querySelector(".preview-dp-count");
    if (counterEl) {
      const mapping = getDeviceMapping(item.device._cloud_ref);
      const total = Object.keys(mapping).length;
      const enabled = Object.values(item.enabled_dps).filter(x => x).length;
      counterEl.textContent = `${enabled}/${total} DP`;
    }
  }

  // v1.18.11: возвращаем скролл.
  if (body) body.scrollTop = st;
}

function previewPrev() { if (PREVIEW_CURRENT > 0) { PREVIEW_CURRENT--; renderImportPreview(); } }
function previewNext() { if (PREVIEW_CURRENT < PREVIEW_DEVICES.length - 1) { PREVIEW_CURRENT++; renderImportPreview(); } }

function closePreview(evt) {
  if (evt && evt.target && evt.target.id !== "preview-overlay") return;
  document.getElementById("preview-overlay").classList.remove("open");
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

async function probeAllInPreview() {
  const btn = document.getElementById("preview-probe-all-btn");
  const title = document.getElementById("preview-title");
  const oldTitle = title ? title.textContent : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> probe…'; }
  const total = PREVIEW_DEVICES.length;
  let okCount = 0, failCount = 0;
  // v1.19: try/finally — иначе при синхронном исключении внутри
  // probeDeviceItem кнопка осталась бы навсегда «probe…».
  try {
    for (let i = 0; i < total; i++) {
      if (title) title.textContent = `Probe ${i+1}/${total}…`;
      try {
        await probeDeviceItem(PREVIEW_DEVICES[i], i);
      } catch (e) {
        console.error("probe item failed", e);
      }
      if (!PREVIEW_DEVICES[i].needs_probe) okCount++; else failCount++;
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔍 Probe все"; }
    if (!isMobile()) renderImportPreview();
    if (title) {
      title.textContent = `Probe завершён: ✅ ${okCount} / ❌ ${failCount}`;
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
  const btn = document.getElementById("preview-import-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> импорт…';
  try {
    const prepared = PREVIEW_DEVICES.map(item => {
      const d = item.device;
      const filtered_dps = {};
      for (const [dp, enabled] of Object.entries(item.enabled_dps)) {
        if (enabled && d.dps_map && d.dps_map[dp]) {
          filtered_dps[dp] = d.dps_map[dp];
        }
      }
      return {
        id: d.id, name: d.name, friendly_name: d.friendly_name,
        ip: d.ip, local_key: d.local_key, version: d.version,
        type: d.type, model: d.model,
        battery_powered: false, enabled: true,
        dps_map: filtered_dps,
      };
    });

    const r = await fetch("/api/import_devices", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ devices: prepared, overwrite: false })
    });
    const data = await r.json();
    closePreview();
    const res = document.getElementById("import-result-block");
    const content = document.getElementById("import-result-content");
    res.style.display = "block";
    let html = "";
    if (data.ok) html += `<div style="color:var(--green);">✅ Добавлено: ${data.added}, обновлено: ${data.updated}, пропущено: ${data.skipped}</div>`;
    else html += `<div style="color:var(--red);">❌ ${escapeHtml(data.error || "ошибка")}</div>`;
    if (data.errors?.length) {
      html += '<div class="muted" style="font-size:12px;">Ошибки:<ul>';
      for (const e of data.errors) html += `<li>${escapeHtml(e)}</li>`;
      html += "</ul></div>";
    }
    content.innerHTML = html;
  } catch (e) {
    uiAlert("Ошибка", "Ошибка импорта: " + e.message, "error");
  }
  btn.disabled = false;
  btn.textContent = "📦 Импортировать всё";
}

// ===== Scan =====
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
  // v1.18.15: если bridge 1.8.3 подтвердил Tuya UDP-пробой —
  // показываем жёстко, до vendor-эвристики.
  if (h.tuya && h.tuya.udp_port) {
    return {cls: "tuya-unknown", name: "Tuya (UDP подтверждён)"};
  }
  const v = (h.vendor || "").toLowerCase();
  if (v.includes("tuya")) return {cls: "tuya-unknown", name: "Tuya (не в конфиге)"};
  if (v.includes("mikrotik")) return {cls: "router", name: "MikroTik"};
  if (v.includes("espressif")) return {cls: "iot", name: "ESP (IoT)"};
  if (v.includes("raspberry")) return {cls: "iot", name: "Raspberry Pi"};
  if (v.includes("apple")) return {cls: "iot", name: "Apple"};
  if (v.includes("philips")) return {cls: "iot", name: "Philips Hue"};
  if (h.open_ports) {
    if (h.open_ports.includes(62078)) return {cls: "iot", name: "iPhone"};
    if (h.open_ports.includes(8008) || h.open_ports.includes(8009)) return {cls: "iot", name: "Chromecast"};
    if (h.open_ports.includes(9100) || h.open_ports.includes(631)) return {cls: "iot", name: "Принтер"};
  }
  return {cls: "unknown", name: "Неизвестно"};
}

function renderScanResults(subnet) {
  const res = document.getElementById("scan-result");
  res.style.display = "block";
  if (SCAN_RESULTS.length === 0) {
    res.innerHTML = `<div style="padding:12px;" class="muted">Ничего не найдено. Нажми «Безопасный скан».</div>`;
    return;
  }
  let html = "";
  html += `<div class="scan-toolbar">
    <button class="primary" id="bridge-scan-btn" onclick="doBridgeScan()">📡 Скан через Bridge</button>
    <span class="scan-toolbar-info" id="bridge-scan-status"></span>
    <span class="scan-toolbar-info" style="margin-left:auto;">Найдено: ${SCAN_RESULTS.length} (подсеть ${escapeHtml(subnet)})</span>
  </div>`;
  for (const h of SCAN_RESULTS) {
    const cls = classifyHost(h);
    const vendor = h.vendor ? ` · ${escapeHtml(h.vendor)}` : "";
    const hostname = h.hostname ? `<span>${escapeHtml(h.hostname)}</span>` : "";
    const mac = h.mac ? copyCode(h.mac) : "";
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
      <div class="scan-meta">${hostname}${vendor}${mac ? " · " + mac : ""}</div>
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
  renderTools();
}
function renderTools() {
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
function copyConfigRaw() {
  if (TOOLS_CONFIG === null) return;
  const jsonStr = JSON.stringify(TOOLS_CONFIG, null, 2);
  tryExecCopy(jsonStr);
  showCopiedToast("Конфиг (" + jsonStr.length + " симв.)");
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
  if (devs.length === 0) { tb.innerHTML = '<tr><td colspan="4" class="muted">Нет данных</td></tr>'; return; }
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
    const avgHtml = (avg === null || avg === undefined)
      ? `<span class="latency lat-timeout">нет данных</span>`
      : `<span class="latency ${latencyClass(avg)}">${avg} ms</span>`;
    return `<tr>
      <td>${escapeHtml(d.friendly_name || d.name)}</td>
      <td><code>${escapeHtml(d.ip || "—")}</code></td>
      <td>${avgHtml}
          <span class="muted" style="font-size:11px;">за ${escapeHtml(periodLabel)} (${cnt} замеров${timeouts > 0 ? `, ${timeouts} timeout` : ""})</span></td>
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
  });
  tb.innerHTML = s.map(x => `<tr><td>${escapeHtml(x.dev)}</td><td><strong>${x.flaps}</strong></td></tr>`).join("");
}

function renderTimeline(t, total) {
  const list = document.getElementById("timeline-list");
  const counter = document.getElementById("timeline-count");
  if (counter) {
    if (total && total > (t.length || 0)) counter.textContent = `показаны последние ${t.length} из ${total}`;
    else if (t.length) counter.textContent = `всего: ${t.length}`;
    else counter.textContent = "";
  }
  if (!list) return;
  if (t.length === 0) { list.innerHTML = '<div class="muted" style="padding:16px;">Нет событий</div>'; return; }
  list.innerHTML = t.map(x => `<div class="timeline-item">
    <span class="timeline-ts">${fmtDateTime(x.ts)}</span>
    <span>${escapeHtml(x.dev)}</span>
    <span style="color:${x.status === "online" ? "var(--green)" : "var(--red)"};">${escapeHtml(x.status)}</span>
  </div>`).join("");
}

function renderActivity(points) {
  const svg = document.getElementById("chart-activity");
  if (!svg) return;
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
  const tsStart = now - 24 * 3600;
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

  if (summaryEl) {
    const last = points[points.length - 1];
    const totalDev = last.total || 0;
    const onlineDev = last.online || 0;
    summaryEl.textContent = `${onlineDev}/${totalDev} online сейчас`;
  }
}

function renderFlapsChart(points) {
  const svg = document.getElementById("chart-flaps");
  if (!svg) return;
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
  const tsStart = now - 24 * 3600;
  const tsSpan = tsEnd - tsStart;

  const byHour = {};
  for (const p of points) {
    byHour[p.ts] = (byHour[p.ts] || 0) + (p.flaps || 0);
  }
  const maxFlaps = Math.max(...Object.values(byHour), 1);

  const barWidth = iw / 24;
  let bars = "";
  let totalFlaps = 0;
  for (let i = 0; i < 24; i++) {
    const hourTs = Math.floor((tsStart + i * 3600 + 1800) / 3600) * 3600;
    let v = 0;
    for (const k of Object.keys(byHour)) {
      const kInt = parseInt(k, 10);
      if (Math.abs(kInt - hourTs) < 1800) { v = byHour[k]; break; }
    }
    totalFlaps += v;
    if (v === 0) continue;
    const x = PAD.left + (i / 24) * iw;
    const h = (v / maxFlaps) * ih;
    const y = PAD.top + ih - h;
    bars += `<rect x="${(x + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${(barWidth - 2).toFixed(1)}" height="${h.toFixed(1)}" fill="var(--yellow)" opacity="0.75" rx="1"/>`;
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

  const summaryEl = document.getElementById("activity-summary");
  if (summaryEl) {
    const avg = (totalFlaps / 24).toFixed(1);
    const prev = summaryEl.textContent;
    summaryEl.textContent = prev ? `${prev} · переходов: ${totalFlaps} (${avg}/ч)` : `переходов: ${totalFlaps} (${avg}/ч)`;
  }
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
loadLogHistory().then(() => { setLogRange(LOG_RANGE_SECONDS); connectSSE(); });
setInterval(fetchStatus, 5000);
if (VIEW === "analytics" && ANALYTICS_ENABLED) {
  setLatencyPeriod(LATENCY_PERIOD);
  loadAnalytics();
  setInterval(loadAnalytics, 30000);
}
</script>
</body>
</html>
"""


def render_html():
    analytics_js = "true" if ANALYTICS_ENABLED else "false"
    status_js = "true" if STATUS_HISTORY_ENABLED else "false"
    return (HTML_PAGE
            .replace("__ANALYTICS_ENABLED__", analytics_js)
            .replace("__STATUS_HISTORY_ENABLED__", status_js)
            .replace("__WEBUI_VERSION__", WEBUI_VERSION))


# ==================== HTTP ====================
class WebUIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self):
        try:
            cl = int(self.headers.get("Content-Length", 0))
            if cl == 0: return None
            return json.loads(self.rfile.read(cl).decode("utf-8"))
        except Exception: return None

    @staticmethod
    def _parse_dev_path(path, suffix):
        prefix = "/api/device/"
        if not path.startswith(prefix) or not path.endswith(suffix): return None
        return path[len(prefix):-len(suffix)].rstrip("/")

    def do_GET(self):
        parsed = urlparse(self.path); path = parsed.path; qs = parse_qs(parsed.query)

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

        if path == "/healthz":
            with STATE_LOCK: st = STATE["bridge_status"]
            payload = {"status": "ok" if st == "online" else "unhealthy", "bridge": st,
                       "analytics": ANALYTICS_ENABLED, "status_history": STATUS_HISTORY_ENABLED}
            self._send_json(200 if st == "online" else 503, payload)
            return

        if path == "/api/status":
            meta_snap = snapshot_device_meta()
            with STATE_LOCK:
                status = {
                    "version": STATE["version"], "bridge_status": STATE["bridge_status"],
                    "uptime": STATE["uptime"],
                    "analytics_enabled": ANALYTICS_ENABLED, "status_history_enabled": STATUS_HISTORY_ENABLED,
                    "webui_version": WEBUI_VERSION,
                    "devices": [],
                }
                for name, info in STATE["devices"].items():
                    m = meta_snap.get(name, {})
                    status["devices"].append({
                        "name": name, "friendly_name": m.get("friendly_name", name),
                        "type": m.get("type", "unknown"), "model": m.get("model", ""),
                        "ip": m.get("ip", ""), "version": m.get("version", ""),
                        "battery_powered": m.get("battery_powered", False),
                        "tuya_id": m.get("tuya_id", ""),
                        "local_key_present": bool(m.get("local_key")),
                        "dps_map": m.get("dps_map", {}),
                        "presets": m.get("presets", []), "preset_map": m.get("preset_map", {}),
                        "min_temp": m.get("min_temp"), "max_temp": m.get("max_temp"), "temp_step": m.get("temp_step"),
                        "status": info.get("status", "unknown"),
                        "last_seen": info.get("last_seen"),
                        "latency_ms": info.get("latency_ms"), "latency_ts": info.get("latency_ts"),
                        "cache": info.get("cache", {}), "history": [],
                    })
            status["devices"].sort(key=lambda d: d["friendly_name"].lower())
            self._send_json(200, status)
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

        if path == "/api/base/rebuild/progress":
            with REBUILD_LOCK:
                self._send_json(200, dict(REBUILD_STATE))
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
            hours = int(qs.get("hours", ["24"])[0]); limit = int(qs.get("limit", ["100"])[0])
            self._send_json(200, {"history": db_query_dev_history(dev, hours, limit)})
            return

        dev = self._parse_dev_path(path, "/latency")
        if dev is not None:
            if not STATUS_HISTORY_ENABLED:
                self._send_json(200, {"latency": []})
                return
            hours = int(qs.get("hours", ["24"])[0]); limit = int(qs.get("limit", ["2000"])[0])
            self._send_json(200, {"latency": db_query_dev_latency(dev, hours, limit)})
            return

        dev = self._parse_dev_path(path, "/avg_latency")
        if dev is not None:
            hours = int(qs.get("hours", ["24"])[0])
            data = db_query_avg_latency(dev, hours)
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
            with STATE_LOCK:
                latency = []
                for name, info in STATE["devices"].items():
                    m = meta_snap.get(name, {})
                    avg_data = db_query_avg_latency(name, latency_seconds)
                    latency.append({
                        "name": name,
                        "friendly_name": m.get("friendly_name", name),
                        "ip": m.get("ip", ""),
                        "latency_ms": info.get("latency_ms"),
                        "latency_ts": info.get("latency_ts"),
                        "avg_ms_24h": (avg_data or {}).get("avg"),
                        "latency_count": (avg_data or {}).get("count", 0),
                        "latency_timeouts": (avg_data or {}).get("timeouts", 0),
                    })
            timeline = db_query_timeline(24, 500)
            timeline_total = db_query_timeline_total(24)
            self._send_json(200, {
                "activity": db_query_hourly(24),
                "flaps_hourly": db_query_flaps_hourly(24),
                "timeline": timeline,
                "timeline_total": timeline_total,
                "flappers": db_query_flappers(24, 3),
                "latency": latency,
                "latency_seconds": latency_seconds,
            })
            return

        if path == "/api/logs/history":
            tail = int(qs.get("tail", ["1000"])[0])
            with _log_buffer_lock: items = list(_log_buffer)[-tail:]
            self._send_json(200, {"logs": items})
            return

        if path == "/api/logs/stream":
            self._handle_sse(qs)
            return

        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/cleanup":
            try:
                _mqtt.publish(f"{TOPIC_PREFIX}/bridge/cleanup", "1", qos=1, retain=False)
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/latency/refresh":
            started, err = trigger_latency_refresh()
            self._send_json(200, {"ok": started, "error": err})
            return

        if path == "/api/base/tuya-local/update":
            ok, err = download_tuya_local_db(max_retries=3)
            if ok:
                self._send_json(200, {"ok": True, "message": "tuya-local база обновлена"})
            else:
                self._send_json(500, {"ok": False, "error": err or "не удалось скачать"})
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
            threading.Thread(target=_rebuild_tinytuya_json_worker, args=(names,),
                             daemon=True, name="tinytuya-rebuild").start()
            self._send_json(200, {"ok": True, "message": "пересборка запущена", "total": len(names)})
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
                version, dps = detect_version(ip, dev_id, local_key)
                if version:
                    self._send_json(200, {"ok": True, "version": version, "dps_count": len(dps)})
                else:
                    self._send_json(200, {"ok": False, "error": "ни одна версия не ответила"})
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
            if not dev_id or not ip or not local_key:
                self._send_json(400, {"ok": False, "error": "id, ip, local_key required"})
                return

            known_ips = get_known_ips()
            if ip in known_ips:
                self._send_json(200, {"ok": False, "error": f"IP {ip} уже есть в конфиге — probe пропущен"})
                return

            try:
                version, dps = detect_version(ip, dev_id, local_key)
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"probe: {e}"})
                return
            if not version:
                self._send_json(200, {"ok": False, "error": "ни одна версия не ответила"})
                return

            dp_to_code = {}
            try:
                if isinstance(cloud_status_meta, list) and cloud_status_meta and isinstance(cloud_current_values, dict):
                    dp_to_code = match_dps_to_codes(dps, cloud_status_meta, cloud_current_values)
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
                } for dp, m in dp_to_code.items()},
                "dps_map": dps_map,
            })
            return

        if path == "/api/db/cleanup":
            if not body:
                self._send_json(400, {"ok": False, "error": "body required"})
                return
            days = int(body.get("keep_days", 0))
            hours = int(body.get("keep_hours", 0))
            keep_s = days * 86400 + hours * 3600
            scope = (body.get("scope") or "all").strip()
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

        dev = self._parse_dev_path(path, "/config")
        if dev is not None:
            if not body or "changes" not in body:
                self._send_json(400, {"ok": False, "error": "changes required"})
                return
            result = _send_request(f"{TOPIC_PREFIX}/bridge/edit_config",
                                   {"device": dev, "changes": body["changes"], "validate": False},
                                   timeout=EDIT_TIMEOUT_WAIT)
            if result.get("ok"):
                with DEVICE_META_LOCK:
                    if dev in DEVICE_META:
                        DEVICE_META[dev].update(body["changes"])
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
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            result = _send_request(f"{TOPIC_PREFIX}/bridge/delete_device", {"device": dev}, timeout=DELETE_TIMEOUT_WAIT)
            if result.get("ok"):
                with DEVICE_META_LOCK:
                    DEVICE_META.pop(dev, None)
                with STATE_LOCK:
                    STATE["devices"].pop(dev, None)
                with _last_snapshot_lock:
                    for k in [k for k in _last_snapshot if k[0] == dev]:
                        _last_snapshot.pop(k, None)
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
            if not subnet or subnet.count(".") != 2:
                self._send_json(400, {"ok": False, "error": f"invalid subnet: {subnet}"})
                return
            try:
                hosts = _scan_extended(subnet)
                self._send_json(200, {"ok": True, "hosts": hosts, "subnet": subnet})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/scan/bridge":
            subnet = (body or {}).get("subnet", "").strip()
            if not subnet:
                self._send_json(400, {"ok": False, "error": "subnet required"})
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
            result = tuya_cloud_fetch(aid, asec, region, fetch_mappings=True)
            if not result["ok"]:
                self._send_json(400, {"ok": False, "error": result.get("error")})
                return
            enriched = []
            for d in result["devices"]:
                mapping = d.get("mapping", {}) or {}
                dps_map = mapping_to_dps_map(mapping, d.get("category", ""))
                product_id = d.get("product_id", "")
                if product_id:
                    tl_map = lookup_tuya_local(product_id)
                    if tl_map:
                        dps_map = merge_dps_maps(dps_map, tl_map)
                        log.info(f"[Cloud] {d.get('name','?')}: tuya-local обогатил {len(tl_map)} DP")
                d["dps_map_generated"] = dps_map
                d["type_guess"] = guess_type_from_category(d.get("category", ""), d.get("product_name", ""), mapping)
                d["version_guess"] = "3.3"
                enriched.append(d)
            save_cloud_cache(enriched, access_id=aid, region=region)
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
                self._send_json(200, {"ok": True, "added": result.get("added", 0),
                                      "updated": result.get("updated", 0),
                                      "skipped": result.get("skipped", 0),
                                      "errors": result.get("errors", [])})
            else:
                self._send_json(400, {"ok": False, "error": result.get("error", "unknown"),
                                      "errors": result.get("errors", [])})
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
    _read_initial_log()
    threading.Thread(target=_log_tailer, daemon=True, name="log-tailer").start()
    if STATUS_HISTORY_ENABLED or ANALYTICS_ENABLED:
        threading.Thread(target=db_worker, daemon=True, name="db-worker").start()
    threading.Thread(target=latency_worker, daemon=True, name="latency").start()
    try:
        _mqtt.connect(MQTT_BROKER, MQTT_PORT, 60)
        _mqtt.loop_start()
        log.info(f"[MQTT] {MQTT_BROKER}:{MQTT_PORT}")
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

