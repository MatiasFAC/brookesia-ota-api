"""Almacén de firmware por canal (dev / prod).

La API **no** depende de GitHub: los binarios viven en el disco del servidor,
en un directorio por canal, y solo se guarda el último de cada uno.

El upload se hace con un endpoint protegido por token, que usa el pipeline de
GitHub Actions (o a mano) para dejar el binario nuevo.

Formato en disco (``OTA_CACHE_DIR``)::

    dev.bin     último firmware del canal dev
    dev.json    metadatos (versión, sha256, tamaño, fecha, notas)
    prod.bin
    prod.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("ota.store")

#: Canales que la API sirve.
CHANNELS = ("dev", "prod")

#: Tamaño máximo admitido en una subida (16 MB de sobra para el firmware).
MAX_UPLOAD_BYTES = int(os.getenv("OTA_MAX_UPLOAD_BYTES", str(32 * 1024 * 1024)))

#: Offset del descriptor de la aplicación dentro de la imagen ESP32.
#: 24 bytes de cabecera de imagen + 8 bytes de cabecera del primer segmento.
APP_DESC_OFFSET = 0x20
#: "magic word" del descriptor (esp_app_desc_t.magic_word).
APP_DESC_MAGIC = 0xABCD5432
#: Offset de esp_app_desc_t.version dentro del descriptor.
APP_DESC_VERSION_OFFSET = 16
#: Longitud del campo de versión.
APP_DESC_VERSION_LEN = 32

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class InvalidFirmware(ValueError):
    """El binario subido no parece una imagen de aplicación de ESP32."""


@dataclass(frozen=True)
class Firmware:
    """Un firmware disponible para instalar."""

    channel: str
    version: str
    size: int
    sha256: str
    uploaded_at: str
    notes: str

    def as_manifest(self, base_url: str) -> dict:
        """Manifiesto que consume el firmware."""
        base = base_url.rstrip("/")
        return {
            "channel": self.channel,
            "version": self.version,
            "size": self.size,
            "sha256": self.sha256,
            "url": f"{base}/firmware.bin?channel={self.channel}",
            "uploaded_at": self.uploaded_at,
            "notes": self.notes,
        }


def extract_app_version(data: bytes) -> str:
    """Saca la versión del descriptor de la aplicación que va dentro del binario.

    La versión la pone el propio build (``PROJECT_VER``), así que es la
    fuente de verdad: nadie puede subir un binario diciendo que es otra
    versión. Si el binario no lleva un descriptor válido, se rechaza.
    """
    if len(data) < APP_DESC_OFFSET + APP_DESC_VERSION_OFFSET + APP_DESC_VERSION_LEN:
        raise InvalidFirmware("el binario es demasiado corto")

    magic = struct.unpack_from("<I", data, APP_DESC_OFFSET)[0]
    if magic != APP_DESC_MAGIC:
        raise InvalidFirmware(
            "el binario no parece una imagen de aplicacion de ESP32 "
            f"(magic 0x{magic:08x}, esperado 0x{APP_DESC_MAGIC:08x})"
        )

    raw = data[
        APP_DESC_OFFSET + APP_DESC_VERSION_OFFSET :
        APP_DESC_OFFSET + APP_DESC_VERSION_OFFSET + APP_DESC_VERSION_LEN
    ]
    version = raw.split(b"\x00")[0].decode("utf-8", errors="replace").strip()
    if not VERSION_RE.match(version):
        raise InvalidFirmware(f"la version del binario no es valida: '{version}'")
    return version


class Store:
    """Guarda el último firmware de cada canal."""

    def __init__(self, cache_dir: Path, public_url: str) -> None:
        self.cache_dir = Path(cache_dir)
        self.public_url = public_url.rstrip("/")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- rutas ------------------------------------------------------------

    def _binary_path(self, channel: str) -> Path:
        return self.cache_dir / f"{channel}.bin"

    def _meta_path(self, channel: str) -> Path:
        return self.cache_dir / f"{channel}.json"

    # -- lectura ----------------------------------------------------------

    def get(self, channel: str) -> Firmware | None:
        """Devuelve el firmware del canal, o None si ese canal está vacío."""
        if channel not in CHANNELS:
            raise ValueError(f"canal desconocido: {channel}")

        meta_path = self._meta_path(channel)
        binary_path = self._binary_path(channel)
        if not meta_path.exists() or not binary_path.exists():
            return None

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.error("no se pudo leer los metadatos de %s: %s", channel, exc)
            return None

        if binary_path.stat().st_size != meta.get("size"):
            LOG.error("el binario de %s no cuadra con sus metadatos", channel)
            return None

        return Firmware(
            channel=channel,
            version=meta.get("version", "0.0.0"),
            size=int(meta.get("size", 0)),
            sha256=meta.get("sha256", ""),
            uploaded_at=meta.get("uploaded_at", ""),
            notes=meta.get("notes", ""),
        )

    def binary_path(self, firmware: Firmware) -> Path:
        """Ruta del binario en disco."""
        return self._binary_path(firmware.channel)

    # -- escritura --------------------------------------------------------

    def put(self, channel: str, data: bytes, notes: str = "") -> Firmware:
        """Guarda el binario como último firmware del canal.

        Se escribe primero en un fichero temporal y se renombra al final, de
        forma que nunca queda un binario a medio escribir.
        """
        if channel not in CHANNELS:
            raise ValueError(f"canal desconocido: {channel}")
        if not data:
            raise InvalidFirmware("el binario esta vacio")
        if len(data) > MAX_UPLOAD_BYTES:
            raise InvalidFirmware(
                f"el binario es demasiado grande ({len(data)} bytes, maximo {MAX_UPLOAD_BYTES})"
            )

        version = extract_app_version(data)
        digest = hashlib.sha256(data).hexdigest()
        previous = self.get(channel)
        if previous and VERSION_RE.match(previous.version) and previous.version > version:
            # No es un error (a veces se quiere hacer un rollback), pero se avisa.
            LOG.warning(
                "el canal %s pasa de %s a %s (retroceso de version)",
                channel, previous.version, version,
            )

        fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(data)
            tmp_path.replace(self._binary_path(channel))
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        firmware = Firmware(
            channel=channel,
            version=version,
            size=len(data),
            sha256=digest,
            uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            notes=notes[:500],
        )
        self._meta_path(channel).write_text(
            json.dumps(
                {
                    "channel": firmware.channel,
                    "version": firmware.version,
                    "size": firmware.size,
                    "sha256": firmware.sha256,
                    "uploaded_at": firmware.uploaded_at,
                    "notes": firmware.notes,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        LOG.info(
            "canal %s: %s (%d bytes, sha256 %s)",
            channel, firmware.version, firmware.size, firmware.sha256,
        )
        return firmware

    # -- mantenimiento ----------------------------------------------------

    def disk_usage(self) -> int:
        return sum(p.stat().st_size for p in self.cache_dir.glob("*") if p.is_file())

    def status(self) -> dict:
        """Estado de los dos canales, para la pagina y la sonda de salud."""
        return {
            channel: (
                {
                    "version": fw.version,
                    "size": fw.size,
                    "sha256": fw.sha256,
                    "uploaded_at": fw.uploaded_at,
                    "notes": fw.notes,
                }
                if (fw := self.get(channel))
                else None
            )
            for channel in CHANNELS
        }
