# API OTA — firmware ESP-Brookesia (JC4880P443C_I_W)

API pública que distribuye el firmware de la placa **Guition JC4880P443C_I_W**
(ESP32-P4 + panel ST7701S 480×800) con **ESP-Brookesia Phone**.

La placa consulta esta API para saber si hay una versión nueva; si la hay, la
descarga, la verifica y reinicia en el slot nuevo.

```
https://ota.m-a.dev/manifest.json?channel=prod   ->  {version, size, sha256, url, ...}
https://ota.m-a.dev/firmware.bin?channel=prod    ->  el binario de ese canal
```

## Los binarios no están en GitHub

La API **guarda el firmware en su propio disco**, en dos canales:

| Canal | Para qué |
| --- | --- |
| `prod` | El firmware de producción (sin consola de depuración ni overlay). |
| `dev` | El firmware de desarrollo (con consola y overlay FPS). |

De cada canal se guarda **solo el último**. El firmware se sube por un endpoint
protegido con un token:

```bash
curl -X POST \
  -H "X-OTA-Token: $OTA_UPLOAD_TOKEN" \
  -H "X-OTA-Notes: lo que sea" \
  --data-binary @firmware.bin \
  https://ota.m-a.dev/upload/prod
```

GitHub se usa **solo para compilar y para desplegar esta API**: el pipeline de
release del repositorio privado del firmware compila y sube el binario aquí.
Si GitHub se cae, la placa sigue pudiendo descargar lo que ya está publicado.

### La versión la pone el binario, no quien lo sube

La API saca la versión del **descriptor de la aplicación** que va dentro del
binario (`esp_app_desc_t`, offset `0x20`), así que no se puede subir un
binario diciendo que es otra versión. Si el descriptor no es válido, la subida
se rechaza con un 400. El sha256 también lo calcula la API.

## Rutas

| Ruta | Método | Qué hace |
| --- | --- | --- |
| `/manifest.json` | GET | Manifiesto de la última versión del canal (`?channel=dev\|prod`, por defecto `prod`). |
| `/firmware.bin` | GET | El binario. ETag = sha256, así que con `If-None-Match` responde 304 sin descargar. |
| `/version` | GET | Solo el número de versión, en texto plano. |
| `/healthz` | GET | Sonda de salud (la usa el healthcheck de Docker). |
| `/` | GET | Página HTML con el estado de los dos canales. |
| `/upload/{canal}` | POST | Sube el binario de un canal. **Protegido con token.** |

El token se manda en la cabecera `X-OTA-Token` (o `Authorization: Bearer ...`).
Si no cuadra, responde 401. La comparación es en tiempo constante.

## Despliegue

El pipeline de GitHub Actions (`.github/workflows/deploy.yml`) copia el código al
servidor **Contabo** por SSH, escribe el `.env` con el token y levanta el servicio
con Docker Compose:

```
/srv/apps_matias/ma-brookesia-ota/
├── app/            (copiado por rsync desde el repositorio)
├── cache/          (bind mount: los binarios publicados)
├── compose.yml     (copiado por rsync)
└── .env            (0600, escrito por el pipeline)
```

En `cache/` quedan los ficheros de cada canal:

```
prod.bin   prod.json    (metadatos: versión, sha256, tamaño, fecha, notas)
dev.bin    dev.json
```

La publicación la hace **Traefik** (el punto de entrada del servidor) con
certificado de Let's Encrypt:

```yaml
labels:
  - traefik.http.routers.ma-brookesia-ota.rule=Host(`ota.m-a.dev`)
  - traefik.http.routers.ma-brookesia-ota.entrypoints=websecure
  - traefik.http.routers.ma-brookesia-ota.tls.certresolver=letsencrypt
```

El DNS es un registro **A** `ota.m-a.dev` → `185.202.236.152` en la zona
`m-a.dev` de Cloudflare, en modo **DNS-only** (sin proxy) para que el challenge
HTTP-01 de Let's Encrypt llegue al origen.

### Secrets del repositorio

| Secret | Para qué |
| --- | --- |
| `DEPLOY_SSH_KEY` | Clave privada de SSH del usuario de despliegue. |
| `DEPLOY_HOST` | Dirección del servidor (185.202.236.152). |
| `DEPLOY_USER` | Usuario SSH (root). |
| `OTA_UPLOAD_TOKEN` | Token que protege `POST /upload/{canal}`. |

> `OTA_UPLOAD_TOKEN` tiene que ser **el mismo valor** en este repositorio (que lo
> escribe en el `.env` del servidor) y en el repositorio privado del firmware (que
> lo usa para subir los binarios). No hace falta ningún PAT de GitHub: la API ya no
> lee nada de GitHub.

### Configuración (`.env`)

| Variable | Por defecto | Para qué |
| --- | --- | --- |
| `OTA_UPLOAD_TOKEN` | — | Token del endpoint de subida. Sin él, la subida está deshabilitada. |
| `OTA_PUBLIC_URL` | `https://ota.m-a.dev` | URL con la que se publica el binario en el manifiesto. |
| `OTA_CACHE_DIR` | `/var/cache/brookesia-ota` | Dónde se guardan los binarios. |
| `OTA_MAX_UPLOAD_BYTES` | `33554432` | Tamaño máximo de una subida. |
| `OTA_LOG_LEVEL` | `INFO` | Nivel de log. |

## Publicar una versión nueva

1. Compilar el firmware (lo hace el pipeline de release del repo privado).
2. Subir el binario al canal:

   ```bash
   curl -X POST -H "X-OTA-Token: $OTA_UPLOAD_TOKEN" \
        --data-binary @firmware.bin \
        https://ota.m-a.dev/upload/prod
   ```

3. La placa consulta el manifiesto de su canal, ve que la versión no coincide con
   la suya y se actualiza sola.

## Desarrollo local

```bash
cd app
pip install -r requirements.txt
export OTA_UPLOAD_TOKEN=un-token-cualquiera
export OTA_PUBLIC_URL=http://localhost:8000
uvicorn main:app --reload --port 8000
```

Las pruebas no necesitan servidor: levantan la app en memoria.

```bash
python test_api.py
```

## Licencia

MIT.
