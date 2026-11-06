FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py github_release.py ./

# El caché de binarios vive en un volumen montado desde el host.
RUN mkdir -p /var/cache/brookesia-ota \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin ota \
    && chown -R ota:ota /var/cache/brookesia-ota /app
USER ota

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz').read()"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
