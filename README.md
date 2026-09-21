# Tuya WiFi → MQTT Bridge → Home Assistant

Локальный мост **Tuya WiFi → MQTT** для Home Assistant.
Работает **без Tuya Cloud**: устройства опрашиваются напрямую по локальной сети
через [tinytuya](https://github.com/jasonacox/tinytuya).

Состоит из двух независимых контейнеров:

| Контейнер | Назначение |
|---|---|
| `tuya-bridge` | Опрос устройств, публикация состояния в MQTT, Home Assistant Discovery |
| `tuya-webui` | Веб-интерфейс: дашборд, аналитика, импорт устройств, инструменты, справка |

Падение WebUI не влияет на Bridge, а рестарт Bridge не роняет WebUI.

---

## Особенности

### 🔋 Батарейные датчики работают

Самое неприятное в локальном мосте — батарейные устройства (датчики дверей,
движения, протечки, температуры). Они спят и не отвечают на периодический опрос,
поэтому обычно «не видно» ни состояния, ни батареи. Здесь они поддерживаются
полноценно:

- Пробуждение ловится **ICMP-пингом** каждые 0.5 с: переход «не отвечает →
  отвечает» = датчик проснулся (событие или heartbeat).
- В окне бодрствования bridge сразу забирает состояние (`updatedps`, до 5 запросов
  с интервалом 1 с) и публикует изменения в MQTT.
- Пока датчик спит, его сущности в Home Assistant **не помечаются недоступными**:
  для них задан `expire_after` (по умолчанию 1 час, переопределяется полем
  `expire_after` в конфиге устройства).
- Если датчик не просыпался больше 24 часов — публикуется
  `<TOPIC_PREFIX>/<устройство>/battery_alert = no_data`
  (в HA: `sensor.<устройство>_battery_alert`). Рядом —
  `sensor.<устройство>_battery_last_seen` со временем последнего пробуждения.
- Время последнего пробуждения хранится в `data/state/battery_last_up.json`,
  поэтому `battery_alert` считается от реального пробуждения и переживает
  перезапуск bridge.
- `battery_alert` публикуется только при смене состояния — без спама в MQTT.

Включается одним полем в конфиге устройства:

```json
{
  "name": "door_sensor",
  "ip": "192.168.1.51",
  "local_key": "LOCAL_KEY",
  "version": "3.3",
  "battery_powered": true
}
```

Нужен `cap_add: NET_RAW` (в `docker-compose.yml` он уже есть). Без него bridge
предупредит в логе: `нет CAP_NET_RAW — Battery listener НЕ БУДЕТ работать`.

---

## Требования

- Docker + Docker Compose v2
- Работающий MQTT-брокер (например, Mosquitto)
- Home Assistant — по желанию (для авто-обнаружения устройств)
- Для каждого устройства: `ip`, `local_key`, `id`, `version` (обычно `3.3`)

---

## Быстрый старт

1. Создайте файл переменных окружения и отредактируйте его:

   ```bash
   cp .env.example .env
   ```

   Обязательно укажите `MQTT_BROKER` (адрес брокера) и `TZ` (ваш часовой пояс).

2. Положите конфиг устройств в `./data/config/devices_config.json`
   (образец с описанием всех полей — `./data/config/devices_config.json.example`).

3. Соберите и запустите:

   ```bash
   docker compose up -d --build
   ```

4. Откройте WebUI: `http://<адрес-хоста>:<WEBUI_PORT>` (по умолчанию `5386`).

---

## Переменные окружения (`.env`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TZ` | `Europe/Moscow` | Часовой пояс. Влияет на quiet hours и время в логах/аналитике |
| `MQTT_BROKER` | `192.168.1.10` | Адрес MQTT-брокера |
| `MQTT_PORT` | `1883` | Порт MQTT-брокера |
| `MQTT_USERNAME` | пусто | Логин (пусто — без авторизации) |
| `MQTT_PASSWORD` | пусто | Пароль |
| `TOPIC_PREFIX` | `tuya` | Префикс топиков состояния |
| `DISCOVERY_PREFIX` | `homeassistant` | Префикс топиков Home Assistant Discovery |
| `LOG_LEVEL` | `INFO` | Уровень логов Bridge: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `WEBUI_PORT` | `5386` | Порт WebUI **на хосте** (внутри контейнера всегда `5386`) |
| `WEBUI_HOST` | `0.0.0.0` | Адрес прослушивания внутри контейнера |

Параметры можно не задавать — тогда используются значения по умолчанию из
`docker-compose.yml`.

---

## Конфиг устройств

`./data/config/devices_config.json` — массив объектов. Минимальный набор полей:

| Поле | Описание |
|---|---|
| `name` | Уникальное имя устройства (используется в топиках и в Home Assistant) |
| `ip` | IP-адрес устройства в локальной сети |
| `local_key` | Локальный ключ устройства |
| `id` | Tuya device ID |
| `version` | Версия протокола (`3.3`, `3.4`, ...) |
| `enabled` | `false` — временно исключить устройство |
| `battery_powered` | `true` — батарейное устройство (см. «Особенности») |
| `expire_after` | Для батарейных: сколько секунд HA держит состояние без обновления (по умолчанию 3600) |
| `dps_map` | Сопоставление DP-кодов Tuya с сущностями Home Assistant |

Полный пример со всеми полями (`friendly_name`, `type`, `model`,
`battery_powered`, `dps_map` и т.д.) — в `devices_config.json.example`.

**Где взять `local_key`** (на выбор):
- `tinytuya wizard` — мастер из проекта tinytuya;
- Tuya IoT Cloud (нужен Access ID/Secret);
- импорт устройств прямо в WebUI (раздел «Импорт устройств»).

Бэкапы конфига автоматически складываются в `./data/backup/`.

---

## Данные (`./data`)

Все каталоги (`config/`, `state/`, `logs/`, `backup/`, `webui_state/`) в поставке уже
есть. Что нужно положить самому, а что сервисы создают и ведут сами:

| Путь | Кто создаёт | Назначение |
|---|---|---|
| `config/devices_config.json` | **вы — обязательно** | Список устройств |
| `config/devices_config.json.example` | пакет | Образец всех полей (кодом не читается) |
| `state/state_cache.json` | bridge | Кэш последних значений DP |
| `state/battery_last_up.json` | bridge | Время последнего пробуждения батарейных |
| `state/discovery_registry.json` | bridge | Реестр опубликованных Discovery-топиков |
| `state/known_device_names.json` | bridge | Известные имена устройств |
| `logs/bridge.log` | bridge | Лог bridge |
| `logs/webui.log` | webui | Лог webui |
| `backup/` | bridge | Автобэкапы `devices_config.json` |
| `webui_state/analytics.db` | webui | SQLite: история статусов и задержек |
| `webui_state/quiet_hours.json` | webui | Тихое время |
| `webui_state/tuya_cloud_cache.json` | webui | Кэш Tuya Cloud |
| `webui_state/tinytuya_devices.json` | webui | Импортированные устройства |
| `webui_state/config_audit.log` | webui | Аудит правок конфига |
| `webui_state/tuya-local-db.json`, `webui_state/tuya-local-db/` | webui | Локальная база tuya-local |

Строки со статусом «bridge»/«webui» создаются автоматически и восстанавливаются
после перезапуска. **Обязателен только `config/devices_config.json`** — без него
bridge завершится с `Файл config/devices_config.json не найден` и уйдёт в рестарт
(на `state/state_cache.json` дополнительно опирается healthcheck).

Все данные лежат рядом с `docker-compose.yml` — ничего не теряется при
пересборке образов.

---

## Обновление

```bash
# заменить ./bridge и ./webui на новую версию, затем:
docker compose up -d --build
```

Данные в `./data` при этом не затрагиваются.

---

## Диагностика

```bash
docker compose ps                     # статус контейнеров и healthcheck
docker compose logs -f tuya-bridge    # логи bridge
docker compose logs -f tuya-webui     # логи webui
```

- WebUI: `http://<хост>:<WEBUI_PORT>` — статус bridge виден в шапке.
- Проверка здоровья WebUI: `curl http://<хост>:<WEBUI_PORT>/healthz`.

Если Bridge пишет `Файл config/devices_config.json не найден` — конфиг не
положен в `./data/config/`.

---

## Структура проекта

```
.
├── docker-compose.yml     ← описание сервисов
├── .env.example           ← шаблон переменных окружения (cp .env.example .env)
├── README.md
├── bridge/
│   ├── main.py            ← Bridge (Tuya ↔ MQTT)
│   ├── requirements.txt
│   └── Dockerfile
├── webui/
│   ├── webui.py           ← WebUI (backend)
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── templates/
│   │   └── index.html     ← HTML
│   └── static/
│       ├── app.css        ← стили
│       └── app.js         ← скрипты
└── data/                  ← ваши данные (монтируется в контейнеры)
    ├── config/
    │   ├── devices_config.json          ← ваш конфиг устройств (обязателен)
    │   └── devices_config.json.example  ← образец всех полей
    ├── state/                           ← состояние bridge (создаётся автоматически)
    ├── logs/
    │   ├── bridge.log
    │   └── webui.log
    ├── backup/                          ← автобэкапы конфига
    └── webui_state/                     ← БД и настройки WebUI
```

Что именно появляется в `data/` и что из этого нужно положить самому — в таблице
раздела «Данные» выше.

---

## Благодарности

- [tinytuya](https://github.com/jasonacox/tinytuya) — локальный протокол Tuya;
- [paho-mqtt](https://github.com/eclipse/paho.mqtt.python) — MQTT-клиент;
- [tuya-local](https://github.com/make-all/tuya-local) — база описаний устройств.

Лицензия: MIT.
