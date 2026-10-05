#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""udp_detect.py — пассивный UDP-детектор Tuya-вещаний (порт 6667).

Зачем (беклог п.10): приборы Tuya сами вещают broadcast в локальную сеть ~каждые 5 с.
Это независимый сигнал «прибор жив» (не зависит от TCP-сессии). Мост использует его,
чтобы не публиковать `offline` и не рвать TCP, если устройство реально в сети.

Инварианты:
  * Только ПРИЁМ (bind + recvfrom), никаких отправок → правило «1 TCP на устройство» не нарушается.
  * Состояние/DP берутся ТОЛЬКО по TCP (правило №1). Здесь — лишь признак живости.
  * `main.py` импортирует этот модуль; обратного импорта НЕТ (никаких циклических зависимостей).

Формат вещания (проверено журналом project/journal/2026-09-28-udp-broadcast-decode.md):
    000055aa | seq(4) | cmd(4) | len(4) | AES-128-ECB(payload)
    ключ = md5(b"yGAdlopoPVldABfn")  (фиксированный, НЕ local_key)
    cmd = 0x13 → JSON-анонс {"gwId","ip","productKey","version","encrypt"}
    cmd = 0x23 → иная структура (не парсим)
    префикс 00006699 → другой протокол (3.5) — игнор
`gwId` совпадает с полем `id` устройства в конфиге моста 1:1.
"""

import hashlib
import json
import logging
import select
import socket
import struct
import threading
import time

log = logging.getLogger("udp_detect")

# --- протокол ---
UDP_PREFIX_55AA = b"\x00\x00\x55\xaa"
UDP_PREFIX_6699 = b"\x00\x00\x66\x99"
UDP_CMD_ANNOUNCE = 0x13
_UDP_BROADCAST_KEY = hashlib.md5(b"yGAdlopoPVldABfn").digest()  # 16 байт, AES-128

# --- разделяемое состояние (под _LOCK) ---
_LOCK = threading.Lock()
_alive = {}          # gwId -> epoch последнего вещания
_gwid_to_name = {}   # gwId -> имя устройства в мосте
_alive_window = 30   # окно «жив», сек
_reconnect_cooldown = 120
_last_force_reconnect_ts = 0.0
_stats = {"packets": 0, "announce": 0, "foreign": 0, "garbage": 0}


def register_devices(gwid_to_name):
    """Зарегистрировать наши устройства: {gwId: name}. Только не-батарейные."""
    with _LOCK:
        _gwid_to_name.clear()
        _gwid_to_name.update(gwid_to_name)


def is_alive(name):
    """True, если устройство `name` вещало за последнее окно живости."""
    now = time.time()
    with _LOCK:
        for gwid, dev_name in _gwid_to_name.items():
            if dev_name == name:
                return (now - _alive.get(gwid, 0.0)) <= _alive_window
    return False


def last_seen(name):
    """epoch последнего вещания устройства или 0.0."""
    with _LOCK:
        for gwid, dev_name in _gwid_to_name.items():
            if dev_name == name:
                return _alive.get(gwid, 0.0)
    return 0.0


def alive_count():
    """Сколько наших устройств вещало за окно (для диагностики)."""
    now = time.time()
    with _LOCK:
        known = set(_gwid_to_name.keys())
        return sum(1 for gwid, ts in _alive.items()
                   if gwid in known and (now - ts) <= _alive_window)


def can_force_reconnect():
    """Cooldown-гейт на форс-реконнекты TCP (общий на все устройства)."""
    global _last_force_reconnect_ts
    with _LOCK:
        now = time.time()
        if now - _last_force_reconnect_ts < _reconnect_cooldown:
            return False
        _last_force_reconnect_ts = now
        return True


def stats():
    """Снимок счётчиков (диагностика/логи)."""
    with _LOCK:
        return dict(_stats)


def decode_announce(msg):
    """Разобрать UDP-пакет. Вернуть {"gwId": str, "ip": str} или None.

    Чистая функция (без состояния) — тестируется оффлайн на работе дампа.
    """
    if len(msg) < 20 or msg[:4] != UDP_PREFIX_55AA:
        return None
    try:
        _seq, cmd, _length = struct.unpack("!III", msg[4:16])
    except struct.error:
        return None
    if cmd != UDP_CMD_ANNOUNCE:
        return None
    # Факт (проверено на дампе work/udp6667b.jsonl): AES-128-ECB-блоки идут от смещения 4,
    # первый блок — служебный (декодируется в «мусор»), а JSON лежит внутри потока —
    # поэтому ищем объект по фигурным скобкам, а не режем по фиксированному смещению.
    body = msg[4:]
    body = body[: (len(body) // 16) * 16]  # AES-ECB работает по 16 байт
    if not body:
        return None
    try:
        from Crypto.Cipher import AES
        dec = AES.new(_UDP_BROADCAST_KEY, AES.MODE_ECB).decrypt(body)
    except Exception:
        return None
    dec = dec.rstrip(b"\x00")
    start = dec.find(b"{")
    end = dec.rfind(b"}")
    if start < 0 or end <= start:
        return None
    try:
        js = json.loads(dec[start:end + 1].decode("utf-8", errors="replace"))
    except Exception:
        return None
    gwid = js.get("gwId") or js.get("id")
    if not gwid:
        return None
    return {"gwId": str(gwid), "ip": str(js.get("ip") or "")}


def _bump(key, n=1):
    with _LOCK:
        _stats[key] = _stats.get(key, 0) + n


def run(stop_event, port=6667, alive_window=30, reconnect_cooldown=120):
    """Цикл слушателя. Блокирующий — запускать в демон-треде.

    stop_event — threading.Event (в мосте: STOP_EVENT).
    """
    global _alive_window, _reconnect_cooldown
    with _LOCK:
        _alive_window = alive_window
        _reconnect_cooldown = reconnect_cooldown

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as e:
        log.error("[UDP] bind 0.0.0.0:%s не удался: %s — детектор выключен", port, e)
        try:
            sock.close()
        except Exception:
            pass
        return

    log.info("[UDP] пассивный слушатель 6667 запущен (окно живости %sс, "
             "cooldown реконнекта %sс)", alive_window, reconnect_cooldown)
    known_ip = {}
    try:
        while not stop_event.is_set():
            try:
                ready, _, _ = select.select([sock], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                msg, (src_ip, _src_port) = sock.recvfrom(4096)
            except OSError:
                continue
            _bump("packets")
            if not msg:
                continue
            if msg[:4] == UDP_PREFIX_6699:
                _bump("garbage")
                continue
            info = decode_announce(msg)
            if not info:
                # 0x23 и прочее — не наш формат; считаем, но не логируем шум
                _bump("garbage")
                continue
            gwid = info["gwId"]
            with _LOCK:
                if gwid not in _gwid_to_name:
                    _stats["foreign"] = _stats.get("foreign", 0) + 1
                    continue
                _alive[gwid] = time.time()
                _stats["announce"] = _stats.get("announce", 0) + 1
                name = _gwid_to_name[gwid]
            prev = known_ip.get(gwid)
            if prev != src_ip:
                known_ip[gwid] = src_ip
                log.info("[UDP] %s (gwId %s) вещает с %s", name, gwid, src_ip)
    finally:
        try:
            sock.close()
        except Exception:
            pass
        log.info("[UDP] слушатель остановлен")
