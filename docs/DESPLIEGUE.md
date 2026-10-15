# Despliegue

## GCP — en producción

| Recurso | Valor |
|---|---|
| Proyecto | `cercanias-en-hora` |
| Web + snapshot | `gs://cercanias-madrid-rt-web` (público) |
| URL | https://storage.googleapis.com/cercanias-madrid-rt-web/index.html |
| Ingesta | Cloud Run job `cercanias-ingesta` (`europe-southwest1`) |
| Planificador | Cloud Scheduler `cercanias-tick`, `* * * * *` (`europe-west1`) |
| Presupuesto | 10 EUR con avisos al 50 %, 90 % y 100 % |

### Por qué el planificador está en otra región

Cloud Scheduler **no existe en `europe-southwest1`** (Madrid). El job vive en
Madrid, cerca del origen de datos, y el planificador en `europe-west1` lo invoca
por HTTP. No es un despiste: es la única combinación posible hoy.

### Flujo

```
Cloud Scheduler (cada minuto)
   -> Cloud Run job: 3 pasadas separadas 20 s (C-7)
        -> sondea vehicle_positions y trip_updates
        -> evalúa invariantes de negocio (WP-1)
        -> publica snapshot.json en el bucket
        -> notifica por Telegram si procede, con enfriamiento persistido
   -> index.html (estático) lee snapshot.json cada 30 s
```

La posición del usuario **no interviene**: el navegador calcula las estaciones
cercanas con el catálogo local. La ruta de lectura es un fichero estático, así
que el coste no crece con el número de usuarios (C-7).

### Cachés

| Objeto | `max-age` | Motivo |
|---|---|---|
| `snapshot.json` | 30 s | Se regenera cada minuto; 30 s permite cachear sin servir datos de dos generaciones |
| `estaciones.json` | 1 h | Cambia unas pocas veces al año |
| `index.html` | 5 min | Permite corregir la app sin esperar un día |

## Telegram en la nube

El token **no está en el repositorio ni en la configuración del job**. Para
activarlo, desde una shell que tenga la variable:

```bash
printf '%s' "$TELEGRAM_BOT_TOKEN" | gcloud secrets create telegram-bot-token \
  --project cercanias-en-hora --data-file=- --replication-policy=automatic

gcloud run jobs update cercanias-ingesta \
  --region europe-southwest1 --project cercanias-en-hora \
  --set-secrets "TELEGRAM_BOT_TOKEN=telegram-bot-token:latest" \
  --update-env-vars "TELEGRAM_CHAT_ID=@cercanias_madrid_rt"
```

Sin el secreto, el job funciona igual y Telegram queda en `dry-run`: publica el
snapshot y registra lo que habría enviado. Los lectores importan más que los
suscriptores, así que un fallo de notificación nunca invalida el snapshot ya
publicado.

### Puente ops Telegram ↔ Grok Bot

Servicio Cloud Run aparte (`services/telegram-bridge/`, región
`europe-southwest1`) que recibe DMs del bot, filtra por `ALLOWED_CHAT_IDS` y
reenvía al webhook de una rutina Grok Bot. El endpoint `POST /reply` permite
devolver texto a Telegram más adelante. Detalle de despliegue y `setWebhook`:
`services/telegram-bridge/README.md`. Runbook: `docs/runbooks/telegram-bridge.md`.

```bash
# desplegar (placeholders; secretos en Secret Manager, nunca en el repo)
gcloud run deploy telegram-bridge \
  --project cercanias-en-hora --region europe-southwest1 \
  --source=services/telegram-bridge --allow-unauthenticated \
  --set-env-vars="ALLOWED_CHAT_IDS=<CHAT_ID>,GROK_WEBHOOK_URL=<GROK_WEBHOOK_URL>" \
  --set-secrets="TELEGRAM_BOT_TOKEN=telegram-bridge-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-bridge-webhook-secret:latest,GROK_WEBHOOK_KEY=telegram-bridge-grok-key:latest"
```

No sustituye las notificaciones del job de ingesta (`telegram-bot-token` /
`TELEGRAM_CHAT_ID`); es un canal de operación persona↔Grok.

## Operación

```bash
# forzar una ejecución
gcloud run jobs execute cercanias-ingesta --region europe-southwest1

# ver ejecuciones
gcloud run jobs executions list --job cercanias-ingesta --region europe-southwest1

# logs de la última
gcloud run jobs executions logs read <EXECUTION_ID> --region europe-southwest1

# parar el gasto sin borrar nada
gcloud scheduler jobs pause cercanias-tick --location europe-west1
```

### Desplegar cambios

```bash
# app web / catálogo
gcloud storage cp apps/web/index.html gs://cercanias-madrid-rt-web/index.html \
  --cache-control="public, max-age=300"

# ingesta
gcloud run jobs deploy cercanias-ingesta --source . --region europe-southwest1
```

## Coste esperado

Todo escala a cero salvo el almacenamiento, que son unos pocos cientos de KB.
Con ~1.440 invocaciones al día de menos de 90 s y 512 MiB, la ingesta cae dentro
o muy cerca del nivel gratuito de Cloud Run. El presupuesto de 10 EUR con avisos
está para enterarse si esa suposición falla, no porque se espere gastarlo.

**No hay balanceador HTTPS ni Cloud CDN.** §16.2 avisa de que introducen un coste
fijo mensual; servir directo desde el bucket lo evita. El precio es que no hay
dominio propio ni CDN de borde, cosa que no hace falta para probar.

## AWS — pendiente

No desplegado. La imagen y los adaptadores ya son portables: `S3SnapshotPublisher`
existe y se selecciona con `SNAPSHOT_BUCKET_S3`, así que el mismo contenedor vale.
Falta el Terraform (Lambda OCI, EventBridge Scheduler, S3, CloudFront, Secrets
Manager, presupuesto) y la comparativa de rendimiento.

**Aviso pendiente:** la cuenta de AWS sigue usando credenciales de root. El plan
acordado es que el primer `terraform apply` cree un usuario IAM `cercanias-deploy`
con alcance acotado para que los siguientes no necesiten root.

## Limitaciones conocidas de esta primera versión

- **El rumbo no sobrevive a la invocación.** Con 3 pasadas hay 2 oportunidades de
  calcularlo, pero al arrancar en frío cada minuto la cadena se reinicia. Es el
  riesgo R-3; lo correcto es un estado externo (WP-3).
- **El sentido sólo es seguro** para los trenes cuya próxima parada es la estación
  consultada. Para el resto la interfaz dice «sentido no confirmado» en vez de
  adivinar.
- **No hay hora estimada de llegada.** Requiere `stop_times.txt` y propagación del
  retraso (R-5).
- **El catálogo tiene 51 estaciones**, las observadas en una captura, no la red
  completa (R-7).
