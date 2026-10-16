# RB-005 — Bot de operaciones por Telegram (`cercanias-telegram-ops`)

## Qué es
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
- Offset: `gs://cercanias-madrid-rt-privado/ops/telegram-offset.json`.
- Buzón: `gs://cercanias-madrid-rt-web/ops/inbox.json`, últimos 20 mensajes.

### Privacidad del buzón (decisión consciente)
`ops/inbox.json` está en el bucket **público**: Wall-E lo lee sin credenciales.
Solo lleva `ts`, `update_id` y `text`; **ni chat_id, ni nombre, ni usuario**.
Pero el texto es legible por cualquiera que conozca la URL: no escribas al bot
nada que no publicarías (contraseñas, datos personales, tokens). El consumidor
lleva su propio puntero (`update_id`); el bot no marca procesados.

## Checks
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
No afecta a la ingesta ni a la web.
