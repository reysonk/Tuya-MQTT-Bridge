"""Docker healthcheck для моста: реальная проверка MQTT-живости (v1.12.40, беклог #2).

Раньше healthcheck проверял только «процесс запущен» + наличие файла state_cache.json.
Теперь: подключаемся к брокеру тем же paho (он уже в образе) и ждём retained-статус
`{TOPIC_PREFIX}/bridge/status` == `online`. Если статус не пришёл за TIMEOUT — unhealthy.

Никаких новых пакетов не требуется. Код возврата: 0 — жив, 1 — нет.
"""
import os
import sys
import time

try:
    import paho.mqtt.client as mqtt
except Exception as e:  # noqa: BLE001
    print(f"healthcheck: paho недоступен: {e}")
    sys.exit(1)

# Дефолт брокера — тот же, что в main.py (1.13.1): иначе healthcheck стучался бы
# в 127.0.0.1 и на проде всегда был бы unhealthy.
BROKER = os.getenv("MQTT_BROKER") or "192.168.1.10"
PORT = int(os.getenv("MQTT_PORT") or 1883)
USER = os.getenv("MQTT_USERNAME") or None
PASS = os.getenv("MQTT_PASSWORD") or None
PREFIX = os.getenv("TOPIC_PREFIX") or "tuya"
TOPIC = f"{PREFIX}/bridge/status"
TIMEOUT = float(os.getenv("HEALTHCHECK_TIMEOUT") or 6)

seen = {"ok": False}


def on_connect(client, userdata, flags, rc, properties=None):
    client.subscribe(TOPIC, qos=0)


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8", "replace").strip().lower()
    except Exception:  # noqa: BLE001
        payload = ""
    if payload in ("online", "1", "true"):
        seen["ok"] = True


def main():
    # paho-mqtt 2.x: версию callback API указываем явно (иначе DeprecationWarning).
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if USER:
        client.username_pw_set(USER, PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(BROKER, PORT, 5)
    except Exception as e:  # noqa: BLE001
        print(f"healthcheck: нет связи с брокером {BROKER}:{PORT}: {e}")
        return 1
    client.loop_start()
    deadline = time.time() + TIMEOUT
    try:
        while time.time() < deadline:
            if seen["ok"]:
                print(f"healthcheck: OK (retained {TOPIC}=online)")
                return 0
            time.sleep(0.2)
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    print(f"healthcheck: НЕТ retained '{TOPIC}=online' за {TIMEOUT} с")
    return 1


if __name__ == "__main__":
    sys.exit(main())
