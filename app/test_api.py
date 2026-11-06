#!/usr/bin/env python3
"""Prueba local de la API OTA: subida por canal, manifiesto, descarga y auth."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

CACHE = Path(tempfile.mkdtemp(prefix="ota-test-"))
TOKEN = "token-de-prueba-123456"

os.environ["OTA_CACHE_DIR"] = str(CACHE)
os.environ["OTA_UPLOAD_TOKEN"] = TOKEN
os.environ["OTA_PUBLIC_URL"] = "https://ota.m-a.dev"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

client = TestClient(app)
FIRMWARE = Path("/tmp/fw120.bin").read_bytes()
FIRMWARE_DEV = Path(
    "/home/mts/Documents/mini-cel/esp-brookesia/examples/esp_idf/"
    "esp_brookesia_phone_p4_function_ev_board/build/"
    "esp_brookesia_phone_p4_function_ev_board.bin"
)

fallos = 0


def check(nombre: str, condicion: bool, extra: str = "") -> None:
    global fallos
    if condicion:
        print(f"  OK   {nombre}")
    else:
        fallos += 1
        print(f"  FALLO {nombre} {extra}")


print("1) healthz y manifiesto sin nada subido")
r = client.get("/healthz")
check("healthz 200", r.status_code == 200, r.text)
check("canales vacios", r.json()["channels"] == {"dev": None, "prod": None}, r.text)
r = client.get("/manifest.json")
check("manifest 503 sin firmware", r.status_code == 503, r.text)

print("2) subida sin token")
r = client.post("/upload/prod", content=FIRMWARE)
check("sin token -> 401", r.status_code == 401, r.text)
r = client.post("/upload/prod", content=FIRMWARE, headers={"X-OTA-Token": "malo"})
check("token malo -> 401", r.status_code == 401, r.text)

print("3) subida con token")
r = client.post(
    "/upload/prod",
    content=FIRMWARE,
    headers={"X-OTA-Token": TOKEN, "X-OTA-Notes": "prueba"},
)
check("subida 200", r.status_code == 200, r.text)
data = r.json()
check("canal prod", data["channel"] == "prod", str(data))
check("version sacada del descriptor = 1.2.0", data["version"] == "1.2.0", str(data))
check("sha256 correcto", data["sha256"] == "cb7418b588a3fa728d76bfd1e4e97f00d52c992dd9b654b5a4dd052b9bba80c0", str(data))
check("size", data["size"] == len(FIRMWARE), str(data))
check("url con canal", data["url"].endswith("/firmware.bin?channel=prod"), str(data))

print("4) canal desconocido y binario invalido")
r = client.post("/upload/otro", content=FIRMWARE, headers={"X-OTA-Token": TOKEN})
check("canal desconocido -> 400", r.status_code == 400, r.text)
r = client.post("/upload/prod", content=b"esto no es un binario",
                headers={"X-OTA-Token": TOKEN})
check("binario invalido -> 400", r.status_code == 400, r.text)

print("5) manifiesto y descarga")
r = client.get("/manifest.json")
check("manifest 200", r.status_code == 200, r.text)
check("manifest version", r.json()["version"] == "1.2.0", r.text)
r = client.get("/manifest.json?channel=dev")
check("canal dev vacio -> 503", r.status_code == 503, r.text)
r = client.get("/firmware.bin")
check("descarga 200", r.status_code == 200)
check("binario identico", r.content == FIRMWARE)
etag = r.headers.get("etag")
check("etag = sha256", etag == '"cb7418b588a3fa728d76bfd1e4e97f00d52c992dd9b654b5a4dd052b9bba80c0"', str(etag))
r2 = client.get("/firmware.bin", headers={"If-None-Match": etag})
check("if-none-match -> 304", r2.status_code == 304, str(r2.status_code))
r = client.get("/version")
check("/version", r.text == "1.2.0", r.text)

print("6) canal dev con otro binario")
if FIRMWARE_DEV.exists():
    r = client.post("/upload/dev", content=FIRMWARE_DEV.read_bytes(),
                    headers={"X-OTA-Token": TOKEN})
    check("subida dev 200", r.status_code == 200, r.text)
    r = client.get("/manifest.json?channel=dev")
    check("manifest dev distinto de prod", r.json()["version"] != "1.2.0" or r.json()["sha256"] != data["sha256"], r.text)
else:
    print("  (sin binario dev local: se omite)")

print("7) bearer token y pagina")
r = client.post("/upload/prod", content=FIRMWARE, headers={"Authorization": f"Bearer {TOKEN}"})
check("bearer -> 200", r.status_code == 200, r.text)
r = client.get("/")
check("html 200", r.status_code == 200 and "prod" in r.text)

print()
print("FALLOS:", fallos)
sys.exit(1 if fallos else 0)
