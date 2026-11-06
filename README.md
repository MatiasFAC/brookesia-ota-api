# API OTA — firmware ESP-Brookesia (JC4880P443C_I_W)

API pública que distribuye el firmware de la placa **Guition JC4880P443C_I_W**
(ESP32-P4 + panel ST7701S 480×800) con **ESP-Brookesia Phone**.

La placa consulta esta API para saber si hay una versión nueva; si la hay, la
descarga, la verifica y reinicia en el slot nuevo.

```
https://ota.m-a.dev/manifest.json   ->  {version, size, sha256, url, released_at, ...}
https://ota.m-a.dev/firmware.bin    ->  el binario de la última versión
```

## De dónde sale el firmware

El binario se publica como *release* del repositorio **privado**
[`MatiasFAC/esp-brookesia-jc4880`](https://github.com/MatiasFAC/esp-brookesia-jc4880).
Esta API es la única que puede leerlo: el token de GitHub vive en el servidor,
nunca en el firmware. La placa solo necesita conocer la URL pública.

Un release debe traer dos assets:

| Asset | Contenido |
| --- | --- |
| `firmware.bin` | el binario del firmware |
| `firmware.sha256` | la suma sha256 en formato `<hex>  <fichero>` |

Si falta el asset de la suma, la API la calcula al descargar y avisa por log.

## Rutas

| Ruta | Método | Qué hace |
| --- | --- | --- |
| `/manifest.json` | GET | Manifiesto de la última versión (lo que consulta el firmware). |
| `/firmware.bin` | GET | El binario. ETag = sha256, así que con `If-None-Match` responde 304 sin descargar. |
| `/version` | GET | Solo el número de versión, en texto plano. |
| `/healthz` | GET | Sonda de salud (la usa el healthcheck de Docker). |
| `/` | GET | Página HTML con el estado de la distribución. |
| `/cleanup` | POST | Borra del caché los binarios de versiones antiguas. |

## Despliegue

El pipeline de GitHub Actions (`.github/workflows/deploy.yml`) copia el código al
servidor **Contabo** por SSH, escribe el `.env` con el token y levanta el servicio
con Docker Compose:

```
/srv/apps_matias/ma-brookesia-ota/
├── app/            (copiado por rsync desde el repositorio)
├── cache/          (bind mount: binarios descargados de GitHub)
├── compose.yml     (copiado por rsync)
└── .env            (0600, escrito por el pipeline)
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
| `OTA_GITHUB_TOKEN` | PAT de GitHub con scope `repo` para leer los releases privados. |

### Configuración (`.env`)

| Variable | Por defecto | Para qué |
| --- | --- | --- |
| `OTA_GITHUB_TOKEN` | — | Token para leer el repositorio privado. |
| `OTA_GITHUB_OWNER` | `MatiasFAC` | Dueño del repositorio del firmware. |
| `OTA_GITHUB_REPO` | `esp-brookesia-jc4880` | Repositorio del firmware. |
| `OTA_PUBLIC_URL` | `https://ota.m-a.dev` | URL con la que se publica el binario en el manifiesto. |
| `OTA_MANIFEST_TTL` | `30` | Segundos que se cachea el manifiesto en memoria. |
| `OTA_LOG_LEVEL` | `INFO` | Nivel de log. |

## Publicar una versión nueva

1. Compilar el firmware en el repositorio privado con la versión en la etiqueta
   (el pipeline de release del repo privado se encarga).
2. El release nuevo se publica en el repositorio privado.
3. La API lo ve en su siguiente consulta (cache de `OTA_MANIFEST_TTL` segundos)
   y sirve el binario nuevo.
4. La placa consulta el manifiesto, ve que la versión no coincide con la suya y
   se actualiza sola.

## Desarrollo local

```bash
cd app
pip install -r requirements.txt
export OTA_GITHUB_TOKEN=ghp_...
export OTA_PUBLIC_URL=http://localhost:8000
uvicorn main:app --reload --port 8000
```

## Licencia

MIT.
