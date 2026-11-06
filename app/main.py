"""API pública de distribución OTA del firmware ESP-Brookesia (JC4880P443C_I_W).

El firmware de la placa consulta esta API para saber si hay una versión nueva:

    GET /manifest.json?channel=prod   ->  {version, size, sha256, url, ...}
    GET /firmware.bin?channel=prod    ->  el binario de la última versión

Los binarios **no** están en GitHub: los sube el pipeline al endpoint

    POST /upload/{channel}             (protegido con un token)

y la API guarda el último de cada canal (dev y prod) en su disco. GitHub solo
se usa para compilar y para desplegar esta API; si GitHub se cae, la placa
sigue pudiendo descargar lo que ya está publicado.

La placa solo necesita conocer la URL pública: no lleva ninguna credencial.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from store import CHANNELS, MAX_UPLOAD_BYTES, Firmware, InvalidFirmware, Store

logging.basicConfig(
    level=os.getenv("OTA_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("ota.api")

PUBLIC_URL = os.getenv("OTA_PUBLIC_URL", "https://ota.m-a.dev").rstrip("/")
CACHE_DIR = Path(os.getenv("OTA_CACHE_DIR", "/var/cache/brookesia-ota"))
#: Token que protege el endpoint de subida. Viene del .env del servidor (0600).
UPLOAD_TOKEN = os.getenv("OTA_UPLOAD_TOKEN", "")

app = FastAPI(
    title="Brookesia OTA API",
    description="Distribuye el firmware ESP-Brookesia de la JC4880P443C_I_W",
    version="2.0.0",
)

store = Store(cache_dir=CACHE_DIR, public_url=PUBLIC_URL)


# -- utilidades ----------------------------------------------------------


def _channel_or_400(channel: str | None) -> str:
    """Canal pedido por el cliente (por defecto, el de producción)."""
    name = (channel or "prod").lower()
    if name not in CHANNELS:
        raise HTTPException(
            status_code=400,
            detail=f"canal desconocido: '{channel}' (validos: {', '.join(CHANNELS)})",
        )
    return name


def _check_upload_token(
    x_ota_token: str | None, authorization: str | None
) -> None:
    """Valida el token de subida (comparación en tiempo constante)."""
    if not UPLOAD_TOKEN:
        LOG.error("OTA_UPLOAD_TOKEN no está configurado: la subida está deshabilitada")
        raise HTTPException(status_code=503, detail="la subida esta deshabilitada")

    presented = x_ota_token or ""
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if not presented or not secrets.compare_digest(presented, UPLOAD_TOKEN):
        LOG.warning("subida rechazada: token incorrecto")
        raise HTTPException(status_code=401, detail="token incorrecto")


def _firmware_or_503(channel: str) -> Firmware:
    firmware = store.get(channel)
    if firmware is None:
        raise HTTPException(
            status_code=503, detail=f"no hay ningun firmware en el canal '{channel}'"
        )
    return firmware


# -- endpoints ------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    """Sonda de salud para Docker y Traefik."""
    status = store.status()
    return {
        "status": "ok",
        "channels": {
            channel: (info["version"] if info else None) for channel, info in status.items()
        },
    }


@app.get("/manifest.json")
def manifest(channel: str | None = None) -> dict:
    """Manifiesto de la última versión del canal. Es lo que consulta el firmware."""
    name = _channel_or_400(channel)
    firmware = _firmware_or_503(name)
    return firmware.as_manifest(PUBLIC_URL)


@app.get("/version")
def version(channel: str | None = None) -> PlainTextResponse:
    """Solo la versión, para comprobaciones rápidas a mano."""
    name = _channel_or_400(channel)
    firmware = _firmware_or_503(name)
    return PlainTextResponse(firmware.version)


@app.get("/firmware.bin")
def firmware(request: Request, channel: str | None = None):
    """Binario de la última versión del canal.

    El ETag es el sha256 del firmware, así que la placa puede preguntar con
    ``If-None-Match`` y recibir un 304 sin descargar nada.
    """
    name = _channel_or_400(channel)
    fw = _firmware_or_503(name)
    path = store.binary_path(fw)

    etag = f'"{fw.sha256}"' if fw.sha256 else None
    if etag and request.headers.get("if-none-match") == etag:
        return PlainTextResponse(status_code=304)

    headers = {
        "Cache-Control": "public, max-age=300",
        "X-Firmware-Version": fw.version,
        "X-Firmware-Sha256": fw.sha256,
        "X-Firmware-Channel": fw.channel,
    }
    if etag:
        headers["ETag"] = etag
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"brookesia-{fw.channel}-{fw.version}.bin",
        headers=headers,
    )


@app.post("/upload/{channel}")
async def upload(
    channel: str,
    request: Request,
    x_ota_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    x_ota_notes: str | None = Header(default=None),
) -> dict:
    """Sube el binario de un canal. Es lo que usa el pipeline de GitHub Actions.

    El cuerpo es el binario en crudo. La API saca la versión del propio
    descriptor de la aplicación que va dentro, así que no se puede subir un
    binario diciendo que es otra versión.
    """
    _check_upload_token(x_ota_token, authorization)
    name = _channel_or_400(channel)

    # Se comprueba el Content-Length antes de leer el cuerpo, para no cargarlo
    # en memoria si es enorme.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"el binario es demasiado grande (maximo {MAX_UPLOAD_BYTES} bytes)",
                )
        except ValueError:
            pass

    data = await request.body()
    try:
        fw = store.put(name, data, notes=x_ota_notes or "")
    except InvalidFirmware as exc:
        LOG.warning("subida rechazada en el canal %s: %s", name, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return fw.as_manifest(PUBLIC_URL)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Página humana con el estado de los dos canales."""
    rows = ""
    for channel, info in store.status().items():
        if info:
            rows += f"""
        <tr><td><b>{channel}</b></td><td>{info['version']}</td><td>{info['size']} bytes</td>
            <td><code>{info['sha256']}</code></td><td>{info['uploaded_at']}</td></tr>"""
        else:
            rows += f"<tr><td><b>{channel}</b></td><td colspan='4'>sin firmware</td></tr>"

    return HTMLResponse(
        f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Brookesia OTA</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td {{ padding: .4rem .6rem; border-bottom: 1px solid #ddd; }}
 code {{ word-break: break-all; }}
</style></head>
<body>
<h1>Distribución OTA</h1>
<p>Firmware ESP-Brookesia para la JC4880P443C_I_W (panel ST7701S 480&times;800).</p>
<table>
 <tr><th>Canal</th><th>Versión</th><th>Tamaño</th><th>SHA-256</th><th>Subido</th></tr>
 {rows}
</table>
<p>
 <a href="/manifest.json">manifest.json</a> &middot;
 <a href="/manifest.json?channel=dev">manifest (dev)</a> &middot;
 <a href="/firmware.bin">firmware.bin</a> &middot;
 <a href="/version">version</a> &middot;
 <a href="/healthz">healthz</a>
</p>
</body></html>"""
    )
