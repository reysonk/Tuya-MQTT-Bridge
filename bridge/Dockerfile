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

CMD ["python", "-u", "main.py"]
