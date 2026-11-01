# RB-005 — Bot de operaciones por Telegram (`cercanias-telegram-ops`)

## Qué es
Dos formas de entrada, **excluyentes** (Telegram no permite `getUpdates` con
un webhook activo), con la misma lógica (`telegram_ops_poll.process_update`):

- **Webhook** (preferida, sin coste fijo): `POST /telegram/ops-webhook` en
  `cercanias-api`. Ver «Modo webhook» abajo.
- **Sondeo** (rollback): el job de abajo.

Job de Cloud Run `cercanias-telegram-ops` (`europe-southwest1`, misma imagen que
la ingesta) que lanza el planificador `cercanias-telegram-ops-tick` cada minuto.
Cada ejecución hace **una** llamada `getUpdates`, responde y termina. Sin IA:
comandos con guion y nada destructivo.

| Comando | Respuesta |
|---|---|
| `estado` / `status` | `generated_at`, servicio, nº estaciones y salidas, bloque `health`, edad del snapshot, nº de trenes, `degraded` |
| `horarios` | días de horario por delante y último fichero de la cadena |
| `regenerar` | el comando exacto de RB-003 (**no lo ejecuta**) |
| `ayuda` / `help` | lista de comandos |
| cualquier otro texto | «Recibido, lo paso a Cercanías Ops» y se añade a `ops/inbox.json` |

- Token: secreto `telegram-ops-bot-token` → `TELEGRAM_OPS_BOT_TOKEN`. Nunca se
  registra (los errores se limpian del token antes de loguear).
- Solo responde a `ALLOWED_CHAT_IDS` (por defecto `5830900654`). Otros chats se
  ignoran (log `WARNING` con su chat_id) y solo avanza el offset.
- Offset: `gs://cercanias-madrid-rt-privado/ops/telegram-offset.json`. Se guarda
  **antes** de responder a cada update (como mucho una vez): un fallo a mitad de
  lote nunca repite respuestas, a cambio puede dejar un mensaje sin contestar.
  Cada pasada se corta a los ~35 s y deja el resto para el minuto siguiente.
- Texto del buzón truncado a 1000 caracteres.
- Buzón: `gs://cercanias-madrid-rt-web/ops/inbox.json`, últimos 20 mensajes.

### Privacidad del buzón (decisión consciente)
`ops/inbox.json` está en el bucket **público**: Wall-E lo lee sin credenciales.
Solo lleva `ts`, `update_id` y `text`; **ni chat_id, ni nombre, ni usuario**.
Pero el texto es legible por cualquiera que conozca la URL: no escribas al bot
nada que no publicarías (contraseñas, datos personales, tokens). El consumidor
lleva su propio puntero (`update_id`); el bot no marca procesados.

## Modo webhook (`cercanias-api`)

- Ruta `POST /telegram/ops-webhook`. Exige la cabecera
  `X-Telegram-Bot-Api-Secret-Token` igual a `TELEGRAM_OPS_WEBHOOK_SECRET`
  (secreto `telegram-ops-webhook-secret`; **distinto** del del bot público). Sin
  secreto configurado o con uno distinto: 403 y no se procesa nada.
- Token: `TELEGRAM_OPS_BOT_TOKEN` (mismo secreto `telegram-ops-bot-token`).
- Allowlist: `OPS_ALLOWED_CHAT_IDS` (por defecto `5830900654`; vacío = nadie).
- Buzón y lecturas: el bucket de `SNAPSHOT_BUCKET_GCS` de la API (el web).
- Sin offset: Telegram da el update por entregado al recibir el 200.
  - **200** cuando el update quedó atendido, aunque falle la respuesta al chat
    (un reintento duplicaría respuestas).
  - **503** cuando el mensaje **no** quedó guardado: falta
    `TELEGRAM_OPS_BOT_TOKEN`, no hay almacén o falló la escritura del buzón. No
    se manda el «Recibido»; Telegram lo reentrega (hasta 24 h) y el dedupe por
    `update_id` evita duplicados. Se ve en `last_error_message` de
    `getWebhookInfo`.
  - Un reintento de Telegram (p. ej. timeout) puede, como mucho, repetir una
    respuesta; el buzón no se duplica.
- El buzón se escribe con precondición de generación (`ifGenerationMatch`): si
  dos updates (o el job de sondeo solapado durante el cambio) escriben a la
  vez, el segundo relee y reintenta en vez de pisar al primero.
  `max_connections=1` en `setWebhook` sigue recomendado (menos conflictos),
  pero ya no es lo que garantiza que no se pierdan mensajes.

### Paso a webhook (Juan, en su Mac)

`$TOKEN` = token del **bot de ops**. No pegar tokens en ningún sitio.

1. `cercanias-api` desplegada con `TELEGRAM_OPS_WEBHOOK_SECRET`,
   `TELEGRAM_OPS_BOT_TOKEN` (`--update-secrets`, nunca `--set-secrets`) y
   `SNAPSHOT_BUCKET_GCS=cercanias-madrid-rt-web`; su SA puede leer y escribir
   objetos del bucket web. Humo: `POST /telegram/ops-webhook` sin cabecera → 403.
2. Pausar el planificador y esperar a que no quede ninguna ejecución en curso:
   ```bash
   gcloud scheduler jobs pause cercanias-telegram-ops-tick --location europe-west1 --project cercanias-en-hora
   gcloud run jobs executions list --job cercanias-telegram-ops --region europe-southwest1 \
     --project cercanias-en-hora --limit 3   # ninguna en "Running"
   ```
3. Registrar el webhook y **comprobar** que Telegram responde `ok: true`
   (`jq -e` sale con error si no):
   ```bash
   OPS_SECRET="$(gcloud secrets versions access latest --secret telegram-ops-webhook-secret --project cercanias-en-hora)"
   curl -s "https://api.telegram.org/bot$TOKEN/setWebhook" \
     -d url=https://cercanias-api-549278575802.europe-southwest1.run.app/telegram/ops-webhook \
     -d secret_token="$OPS_SECRET" -d max_connections=1 \
     --data-urlencode 'allowed_updates=["message"]' | jq -e '.ok == true'
   unset OPS_SECRET
   ```
4. Verificar con `getWebhookInfo` (abajo): URL correcta, `max_connections` 1,
   `allowed_updates` `["message"]`, sin `last_error_message`. Desde el chat
   permitido: `estado` contesta; un texto libre devuelve el ACK y aparece en
   `ops/inbox.json`.

Si algo falla en 3 o 4: rollback (abajo).

Checks:
```bash
curl -s "https://api.telegram.org/bot$TOKEN/getWebhookInfo" | jq '.result | {url, pending_update_count, last_error_date, last_error_message, max_connections, allowed_updates}'
gcloud logging read 'resource.labels.service_name="cercanias-api" AND jsonPayload.component="telegram-ops"' \
  --project cercanias-en-hora --limit 20 --format 'value(jsonPayload.severity,jsonPayload.message)'
```
`last_error_message` con `403` = secreto distinto entre Telegram y la API;
con `503` = falta el token o falla el buzón (ver logs).

Rollback al sondeo (sin desplegar nada; **sin** `drop_pending_updates` para no
perder lo pendiente). Reanudar el planificador **solo** si `deleteWebhook`
devolvió `ok: true` y la URL quedó vacía; si no, el job fallaría con
`getUpdates: HTTP 409`:
```bash
curl -s "https://api.telegram.org/bot$TOKEN/deleteWebhook" | jq -e '.ok == true'
curl -s "https://api.telegram.org/bot$TOKEN/getWebhookInfo" | jq -e '.result.url == ""'
gcloud scheduler jobs resume cercanias-telegram-ops-tick --location europe-west1 --project cercanias-en-hora
```

## Checks (modo sondeo)
```bash
gcloud run jobs executions list --job cercanias-telegram-ops --region europe-southwest1 --limit 5
gcloud logging read 'resource.labels.job_name="cercanias-telegram-ops" AND jsonPayload.component="telegram-ops"' \
  --project cercanias-en-hora --limit 20 --format 'value(jsonPayload.severity,jsonPayload.message)'
gcloud storage cat gs://cercanias-madrid-rt-privado/ops/telegram-offset.json
curl -s https://storage.googleapis.com/cercanias-madrid-rt-web/ops/inbox.json | jq '.items[-3:]'
```

## Fallos típicos
- **No responde y el log dice `getUpdates: HTTP 409`**: hay un webhook activo.
  `getUpdates` y webhook son excluyentes → `deleteWebhook` (ver DESPLIEGUE.md).
- **`falta TELEGRAM_OPS_BOT_TOKEN`** (exit 2): el job se desplegó sin `--set-secrets`.
- **Responde en bucle lo mismo**: no se puede escribir el offset → revisar que la
  SA tenga `roles/storage.objectAdmin` en `cercanias-madrid-rt-privado`.
- **`horarios` dice «desconocido»**: la SA no puede listar el bucket web (igual
  que `schedule_days_left` en RB-004).

## Fix
Corregir permisos/secreto y `gcloud run jobs execute cercanias-telegram-ops
--region europe-southwest1 --wait`. Para descartar mensajes atascados, escribir a
mano un offset mayor en `telegram-offset.json` (`{"offset": <update_id+1>}`).

## Verify
Mandar `estado` al bot desde el chat permitido: respuesta en ≤ 1 min.

## Rollback
`gcloud scheduler jobs pause cercanias-telegram-ops-tick --location europe-west1`.
No afecta a la ingesta ni a la web. En modo webhook, además `deleteWebhook`.
