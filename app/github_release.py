"""Cliente de la API de Releases de GitHub con caché en disco.

El firmware se publica como *release* del repositorio privado
``MatiasFAC/esp-brookesia-jc4880``. Esta API es la única que puede leerlo, y
lo hace con un token de GitHub que vive en el servidor (nunca en el firmware).

Detalle importante: en un repositorio **privado** los assets no se pueden
descargar desde ``browser_download_url`` (devuelve 404). Hay que pedir el
asset por la API (``/releases/assets/<id>``) con ``Accept:
application/octet-stream``, que responde 302 hacia la CDN con una URL firmada.
Esa segunda petición va **sin** el token, para no filtrarlo fuera de GitHub.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

LOG = logging.getLogger("ota.github")

GITHUB_API = "https://api.github.com"

#: Nombre del asset con el binario del firmware dentro del release.
ASSET_NAME = os.getenv("OTA_ASSET_NAME", "firmware.bin")
#: Nombre del asset con la suma sha256 en formato "<hex>  <fichero>".
ASSET_SHA_NAME = os.getenv("OTA_ASSET_SHA_NAME", "firmware.sha256")


@dataclass(frozen=True)
class Release:
    """Una versión del firmware disponible para instalar."""

    version: str
    tag: str
    size: int
    sha256: str
    url: str
    released_at: str
    notes: str

    def as_manifest(self, base_url: str) -> dict:
        """Manifiesto que consume el firmware."""
        return {
            "version": self.version,
            "tag": self.tag,
            "size": self.size,
            "sha256": self.sha256,
            "url": f"{base_url.rstrip('/')}/firmware.bin",
            "released_at": self.released_at,
            "notes": self.notes,
        }


class ReleaseCache:
    """Descarga y cachea el último release y su binario.

    La API de GitHub tiene un límite de peticiones por hora, así que ni el
    manifiesto ni el binario se piden en cada consulta: el manifiesto se cachea
    en memoria unos segundos y el binario en disco.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        cache_dir: Path,
        manifest_ttl: float = 30.0,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.token = token
        self.cache_dir = cache_dir
        self.manifest_ttl = manifest_ttl
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._release: Release | None = None
        self._release_time: float = 0.0
        self._asset: dict | None = None

    # -- API de GitHub ----------------------------------------------------

    def _headers(self, octet_stream: bool = False) -> dict:
        headers = {"Accept": "application/vnd.github+json"}
        if octet_stream:
            headers["Accept"] = "application/octet-stream"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _asset_request(self, asset: dict) -> httpx.Response:
        """Pide un asset por la API de GitHub y sigue la redirección a la CDN.

        La primera petición lleva el token; la de la CDN va sin él.
        """
        api_url = asset.get("url")
        if not api_url:
            raise RuntimeError(f"el asset {asset.get('name')} no tiene url de API")

        with httpx.Client(timeout=120.0, follow_redirects=False) as client:
            response = client.get(
                api_url, headers=self._headers(octet_stream=True)
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise RuntimeError(
                        f"redirección sin location para el asset {asset.get('name')}"
                    )
                response = client.get(
                    location,
                    headers={"Accept": "application/octet-stream"},
                    follow_redirects=True,
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"GitHub devolvió {response.status_code} al pedir "
                    f"{asset.get('name')}"
                )
            return response

    def _fetch_latest_release(self) -> Release:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/releases/latest"
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(url, headers=self._headers())
            if response.status_code != 200:
                raise RuntimeError(
                    f"GitHub Releases respondió {response.status_code}: {response.text[:200]}"
                )
            data = response.json()

        assets = {asset["name"]: asset for asset in data.get("assets", [])}
        if ASSET_NAME not in assets:
            raise RuntimeError(
                f"el release {data.get('tag_name')} no tiene el asset '{ASSET_NAME}'"
            )
        asset = assets[ASSET_NAME]
        self._asset = asset
        sha256 = self._read_sha256(assets)

        tag = data.get("tag_name", "")
        version = tag[1:] if tag.startswith("v") else tag
        return Release(
            version=version,
            tag=tag,
            size=int(asset.get("size", 0)),
            sha256=sha256,
            url=asset.get("browser_download_url", ""),
            released_at=data.get("published_at", ""),
            notes=(data.get("body") or "")[:2000],
        )

    def _read_sha256(self, assets: dict) -> str:
        """Saca el sha256 del asset de texto; si no existe, queda vacío."""
        if ASSET_SHA_NAME not in assets:
            LOG.warning("no hay asset '%s': se calculará al descargar", ASSET_SHA_NAME)
            return ""
        try:
            response = self._asset_request(assets[ASSET_SHA_NAME])
            digest = response.text.strip().split()[0].lower()
        except Exception as exc:  # noqa: BLE001 - no es fatal: se calcula al descargar
            LOG.warning("no se pudo leer '%s': %s", ASSET_SHA_NAME, exc)
            return ""
        if len(digest) != 64:
            LOG.warning("'%s' no contiene un sha256 válido", ASSET_SHA_NAME)
            return ""
        return digest

    # -- caché ------------------------------------------------------------

    def latest(self) -> Release:
        """Devuelve el último release, usando la caché si sigue fresco."""
        now = time.monotonic()
        if self._release is not None and (now - self._release_time) < self.manifest_ttl:
            return self._release

        release = self._fetch_latest_release()
        self._release = release
        self._release_time = now
        LOG.info("release actual: %s (%d bytes)", release.tag, release.size)
        return release

    def binary_path(self, release: Release) -> Path:
        """Ruta del binario cacheado, descargándolo si hace falta."""
        target = self.cache_dir / f"{release.tag}-{ASSET_NAME}"
        if target.exists() and target.stat().st_size == release.size:
            return target

        LOG.info("descargando el firmware de %s (%d bytes)", release.tag, release.size)
        if self._asset is None:
            raise RuntimeError("no hay asset del firmware en caché")
        fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir)
        os.close(fd)
        tmp_path = Path(tmp_name)
        digest = hashlib.sha256()
        try:
            response = self._asset_request(self._asset)
            with tmp_path.open("wb") as handle:
                for chunk in response.iter_bytes(65536):
                    digest.update(chunk)
                    handle.write(chunk)

            if release.sha256 and digest.hexdigest() != release.sha256:
                raise RuntimeError(
                    "sha256 del firmware no coincide: "
                    f"esperado {release.sha256}, obtenido {digest.hexdigest()}"
                )
            if not release.sha256:
                LOG.info("sha256 calculado: %s", digest.hexdigest())

            tmp_path.replace(target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        return target

    def cleanup(self) -> None:
        """Borra del caché los binarios de versiones antiguas."""
        keep = self._release.tag if self._release else None
        for path in self.cache_dir.glob(f"*-{ASSET_NAME}"):
            if keep and path.name == f"{keep}-{ASSET_NAME}":
                continue
            LOG.info("borrando del caché %s", path.name)
            path.unlink(missing_ok=True)

    def disk_usage(self) -> int:
        return sum(p.stat().st_size for p in self.cache_dir.glob("*"))
