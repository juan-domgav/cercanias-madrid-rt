# telegram-bridge

Puente mínimo **Telegram DM → Cloud Run → email a Grok Bot** (inbox
`cercanias-ops@mail.grokbot.com`), con webhook de rutina Grok **opcional** y
`POST /reply` para devolver texto a Telegram.

Proyecto GCP: `cercanias-en-hora` · Región: `europe-southwest1`.

**No hay secretos en este directorio.** Todo va por variables de entorno /
Secret Manager.

## Endpoints

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/healthz` | — | Liveness |
| `POST` | `/telegram/webhook` | `X-Telegram-Bot-Api-Secret-Token` | Update de Telegram; email ops (+ webhook Grok si está) |
| `POST` | `/reply` | mismo secret, `X-Reply-Auth-Token` o `Authorization: Bearer …` | `sendMessage` a un chat allowlisted |

### Camino principal: email → inbox Grok

Cuando el `chat_id` está en `ALLOWED_CHAT_IDS`, el servicio envía un correo a
`OPS_EMAIL_TO` (por defecto `cercanias-ops@mail.grokbot.com`) vía SMTP
(`SMTP_HOST`, etc.). Asunto:

```
[telegram-ops] {primeros 60 caracteres del texto}
```

Cuerpo (texto plano):

```
source: telegram
chat_id: 5830900654
username: obiguany
update_id: 42
text:
estado C5
```

Juan no necesita la UI de webhook/key de Grok Routines: basta con que la inbox
del bot esté reclamada y el SMTP entregue el mensaje.

### Camino opcional: webhook de rutina Grok

Si `GROK_WEBHOOK_URL` y `GROK_WEBHOOK_KEY` están definidos, además se hace POST
con `Authorization: Bearer $GROK_WEBHOOK_KEY`:

```json
{
  "source": "telegram",
  "chat_id": 123,
  "text": "…",
  "from": {"id": 1, "username": "…", "first_name": "…"},
  "update_id": 42
}
```

El webhook responde **200 enseguida** a Telegram; email y (si aplica) POST a
Grok van en background. Timeout del webhook:
`GROK_FORWARD_TIMEOUT_SECONDS` (default `2.0`).

## Variables de entorno

| Variable | Obligatoria | Uso |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | sí (para `/reply`) | Token del bot (`CercaniasRTOPS_bot`) |
| `TELEGRAM_WEBHOOK_SECRET` | sí | Secret de `setWebhook` y auth de `/reply` |
| `ALLOWED_CHAT_IDS` | sí | IDs numéricos separados por coma. Ejemplo ops: `5830900654` |
| `OPS_EMAIL_TO` | no | Destino del aviso (default `cercanias-ops@mail.grokbot.com`) |
| `SMTP_HOST` | sí (para email) | Host SMTP transaccional o Gmail |
| `SMTP_PORT` | no | Default `587` (STARTTLS). `465` → SSL |
| `SMTP_USER` | según proveedor | Usuario SMTP |
| `SMTP_PASSWORD` | según proveedor | Contraseña / API key / app password (`SMTP_PASS` también se acepta) |
| `SMTP_FROM` | sí (para email) | Remitente (`From:`) verificado en el proveedor |
| `GROK_WEBHOOK_URL` | no | URL POST de rutina Grok (opcional) |
| `GROK_WEBHOOK_KEY` | no | Key de la rutina (opcional; no hace falta si solo usas email) |
| `REPLY_AUTH_TOKEN` | no | Alternativa a `TELEGRAM_WEBHOOK_SECRET` para `/reply` |
| `GROK_FORWARD_TIMEOUT_SECONDS` | no | Timeout del reenvío webhook (default `2.0`) |

### SMTP recomendado en GCP

No uses el SMTP de Gmail desde Cloud Run si puedes evitarlo (cuotas, bloqueos).
Opciones gratuitas / freemium con SMTP clásico (`smtplib`, sin deps nuevas):

- [Resend](https://resend.com) / [Brevo](https://www.brevo.com) / [Mailgun](https://www.mailgun.com) / [SendGrid](https://sendgrid.com): `SMTP_HOST` + API key como `SMTP_PASSWORD`, `SMTP_FROM` = dominio verificado.
- Gmail **app password** solo como fallback casero: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER` = cuenta, `SMTP_PASSWORD` = app password.

## Pruebas locales

```bash
cd services/telegram-bridge
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
```

## Despliegue (Cloud Run)

Sustituye los placeholders. No pegues secretos en el historial del chat si
puedes evitarlo: usa Secret Manager.

```bash
export PROJECT=cercanias-en-hora
export REGION=europe-southwest1
export SERVICE=telegram-bridge

# Secretos (una vez)
printf '%s' "$TELEGRAM_BOT_TOKEN" | gcloud secrets create telegram-bridge-bot-token \
  --project="$PROJECT" --data-file=- --replication-policy=automatic
printf '%s' "$TELEGRAM_WEBHOOK_SECRET" | gcloud secrets create telegram-bridge-webhook-secret \
  --project="$PROJECT" --data-file=- --replication-policy=automatic
printf '%s' "$SMTP_PASSWORD" | gcloud secrets create telegram-bridge-smtp-password \
  --project="$PROJECT" --data-file=- --replication-policy=automatic
# Opcional (solo si usas rutina Grok por webhook):
# printf '%s' "$GROK_WEBHOOK_KEY" | gcloud secrets create telegram-bridge-grok-key \
#   --project="$PROJECT" --data-file=- --replication-policy=automatic

# Si ya existen: gcloud secrets versions add … --data-file=-

gcloud run deploy "$SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --source=services/telegram-bridge \
  --allow-unauthenticated \
  --set-env-vars="ALLOWED_CHAT_IDS=5830900654,OPS_EMAIL_TO=cercanias-ops@mail.grokbot.com,SMTP_HOST=<SMTP_HOST>,SMTP_PORT=587,SMTP_USER=<SMTP_USER>,SMTP_FROM=<SMTP_FROM>" \
  --set-secrets="TELEGRAM_BOT_TOKEN=telegram-bridge-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-bridge-webhook-secret:latest,SMTP_PASSWORD=telegram-bridge-smtp-password:latest"
```

`--allow-unauthenticated` es necesario para que Telegram pueda llamar al
webhook; la protección real es `TELEGRAM_WEBHOOK_SECRET` + allowlist.

Anota la URL del servicio:

```bash
gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)'
# → https://telegram-bridge-….run.app
```

## setWebhook en Telegram

```bash
export BOT_TOKEN="<TELEGRAM_BOT_TOKEN>"
export WEBHOOK_SECRET="<TELEGRAM_WEBHOOK_SECRET>"
export SERVICE_URL="https://telegram-bridge-XXXX.run.app"

curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -H 'Content-Type: application/json' \
  -d "{
    \"url\": \"${SERVICE_URL}/telegram/webhook\",
    \"secret_token\": \"${WEBHOOK_SECRET}\",
    \"allowed_updates\": [\"message\", \"edited_message\"]
  }"

curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

## Probar /reply

```bash
curl -sS -X POST "${SERVICE_URL}/reply" \
  -H "Authorization: Bearer ${WEBHOOK_SECRET}" \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": 5830900654, "text": "ping desde el puente"}'
```

## Runbook

Ver `docs/runbooks/telegram-bridge.md`.
