FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        curl \
        iputils-ping \
        net-tools \
        iproute2 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY webui.py .
COPY templates ./templates
COPY static ./static
RUN mkdir -p /app/webui_state
EXPOSE 5386
CMD ["python", "-u", "webui.py"]
