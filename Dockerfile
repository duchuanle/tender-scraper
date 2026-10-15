# Generic container for the tender scraper: runs on any Docker host
# (VPS, Azure Container Apps / App Service, AWS, GCP, ...).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    DOWNLOAD_DIR=/data/downloads \
    FORWARDED_ALLOW_IPS=*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py firecrawl_client.py attachment_links.py attachment_downloader.py \
     tender_artifacts.py onedrive_client.py settings.py ./

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/downloads \
    && chown -R app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=4)"

# --proxy-headers takes the scheme (X-Forwarded-Proto) and client IP from a reverse proxy;
# the host in statusUrl/next comes from the Host header the proxy forwards.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --timeout-keep-alive 75"]
