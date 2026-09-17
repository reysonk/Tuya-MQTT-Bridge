FROM python:3.12-slim

# Отключаем буферизацию Python — логи идут сразу
ENV PYTHONUNBUFFERED=1

# Устанавливаем часовой пояс
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости и устанавливаем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код и конфиг
COPY main.py .

# Создаём непривилегированного пользователя
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

CMD ["python", "-u", "main.py"]
