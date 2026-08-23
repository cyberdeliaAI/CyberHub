FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CYBERHUB_DOCKER=1 \
    CYBERHUB_DATA_DIR=/app/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir "onnxruntime>=1.17"

COPY . .

# Keep first launch local and fast. A failed font download is non-fatal because
# CyberHub can use the system fonts installed above.
RUN python resources/fonts/download_fonts.py || true

RUN groupadd --gid 10001 cyberhub \
    && useradd --uid 10001 --gid cyberhub --create-home --shell /usr/sbin/nologin cyberhub \
    && mkdir -p /app/data /app/resources/auto_tagger/wd-eva02-large-tagger-v3 \
    && chown -R cyberhub:cyberhub /app

USER cyberhub

EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8899/', timeout=3).read(1)"

ENTRYPOINT ["python", "-u", "/app/hub.py"]
CMD ["--listen"]
