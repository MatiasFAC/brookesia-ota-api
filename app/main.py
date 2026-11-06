"""API pública de distribución OTA del firmware ESP-Brookesia (JC4880P443C_I_W).

El firmware de la placa consulta esta API para saber si hay una versión nueva:

    GET /manifest.json   ->  {version, size, sha256, url, released_at, ...}
    GET /firmware.bin    ->  el binario de la última versión

El binario se publica como *release* del repositorio privado de GitHub y esta
API es la única que puede leerlo: el token vive en el servidor, nunca en el
firmware. La placa solo necesita conocer la URL pública.

Se despliega detrás de Traefik en https://ota.m-a.dev
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from github_release import ReleaseCache

logging.basicConfig(
    level=os.getenv("OTA_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("ota.api")

OWNER = os.getenv("OTA_GITHUB_OWNER", "MatiasFAC")
REPO = os.getenv("OTA_GITHUB_REPO", "esp-brookesia-jc4880")
TOKEN = os.getenv("OTA_GITHUB_TOKEN", "")
PUBLIC_URL = os.getenv("OTA_PUBLIC_URL", "https://ota.m-a.dev").rstrip("/")
CACHE_DIR = Path(os.getenv("OTA_CACHE_DIR", "/var/cache/brookesia-ota"))
MANIFEST_TTL = float(os.getenv("OTA_MANIFEST_TTL", "30"))

app = FastAPI(
    title="Brookesia OTA API",
    description="Distribuye el firmware ESP-Brookesia de la JC4880P443C_I_W",
    version="1.0.0",
)

cache = ReleaseCache(
    owner=OWNER,
    repo=REPO,
    token=TOKEN,
    cache_dir=CACHE_DIR,
    manifest_ttl=MANIFEST_TTL,
)


@app.get("/healthz")
def healthz() -> dict:
    """Sonda de salud para Docker y Traefik."""
    return {"status": "ok", "repo": f"{OWNER}/{REPO}"}


@app.get("/manifest.json")
def manifest() -> dict:
    """Manifiesto de la última versión. Es lo que consulta el firmware."""
    try:
        release = cache.latest()
    except Exception as exc:  # noqa: BLE001 - se devuelve como error de la API
        LOG.error("no se pudo leer el release: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return release.as_manifest(PUBLIC_URL)


@app.get("/version")
def version() -> PlainTextResponse:
    """Solo la versión, para comprobaciones rápidas a mano."""
    try:
        release = cache.latest()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PlainTextResponse(release.version)


@app.get("/firmware.bin")
def firmware(request: Request):
    """Binario de la última versión.

    El ETag es el sha256 del firmware, así que la placa puede preguntar con
    ``If-None-Match`` y recibir un 304 sin descargar nada.
    """
    try:
        release = cache.latest()
        path = cache.binary_path(release)
    except Exception as exc:  # noqa: BLE001
        LOG.error("no se pudo preparar el firmware: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    etag = f'"{release.sha256}"' if release.sha256 else None
    if etag and request.headers.get("if-none-match") == etag:
        return PlainTextResponse(status_code=304)

    headers = {
        "Cache-Control": "public, max-age=300",
        "X-Firmware-Version": release.version,
        "X-Firmware-Sha256": release.sha256,
    }
    if etag:
        headers["ETag"] = etag
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"brookesia-{release.version}.bin",
        headers=headers,
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Página humana con el estado de la distribución."""
    try:
        release = cache.latest()
        rows = f"""
        <tr><td>Versión</td><td>{release.version}</td></tr>
        <tr><td>Tag</td><td>{release.tag}</td></tr>
        <tr><td>Tamaño</td><td>{release.size} bytes</td></tr>
        <tr><td>SHA-256</td><td><code>{release.sha256}</code></td></tr>
        <tr><td>Publicado</td><td>{release.released_at}</td></tr>
        """
    except Exception as exc:  # noqa: BLE001
        rows = f"<tr><td>Error</td><td>{exc}</td></tr>"

    return HTMLResponse(
        f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Brookesia OTA</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 44rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td {{ padding: .4rem .6rem; border-bottom: 1px solid #ddd; }}
 code {{ word-break: break-all; }}
</style></head>
<body>
<h1>Distribución OTA</h1>
<p>Firmware ESP-Brookesia para la JC4880P443C_I_W (panel ST7701S 480&times;800).</p>
<table>{rows}</table>
<p>
 <a href="/manifest.json">manifest.json</a> &middot;
 <a href="/firmware.bin">firmware.bin</a> &middot;
 <a href="/version">version</a> &middot;
 <a href="/healthz">healthz</a>
</p>
</body></html>"""
    )


@app.post("/cleanup")
def cleanup() -> dict:
    """Borra del caché los binarios de versiones antiguas."""
    cache.cleanup()
    return {"cache_dir": str(CACHE_DIR), "bytes": cache.disk_usage()}
