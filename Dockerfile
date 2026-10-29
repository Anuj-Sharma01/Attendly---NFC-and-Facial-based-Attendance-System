FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake pkg-config libopenblas-dev liblapack-dev \
    libglib2.0-0 libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-web.txt ./requirements-web.txt
RUN python -m pip install --upgrade pip wheel && \
    python -m pip install --no-cache-dir -r requirements-web.txt

COPY Project ./Project
COPY app.py hardware_bridge.py README-WEB.md ./
COPY templates ./templates
COPY static ./static
RUN mkdir -p /app/data/Photos

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
