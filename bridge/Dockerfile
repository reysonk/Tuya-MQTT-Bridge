FROM python:3.12-slim

# tzdata — чтобы TZ из окружения работал (quiet hours, метки в логах).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY udp_detect.py .
COPY healthcheck.py .

# v1.14.8: H4 — HEALTHCHECK: реальная проверка MQTT-живости (healthcheck.py
# уже в образе). timeout (10с) больше дефолтного HEALTHCHECK_TIMEOUT (6с).
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python healthcheck.py

CMD ["python", "-u", "main.py"]
