# telegram-bridge

Puente mínimo **Telegram DM → Cloud Run → webhook de rutina Grok Bot**, con
`POST /reply` opcional para devolver texto a Telegram.

Proyecto GCP: `cercanias-en-hora` · Región: `europe-southwest1`.

**No hay secretos en este directorio.** Todo va por variables de entorno /
Secret Manager.

## Endpoints

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/healthz` | — | Liveness |
| `POST` | `/telegram/webhook` | `X-Telegram-Bot-Api-Secret-Token` | Update de Telegram; reenvía a Grok |
| `POST` | `/reply` | mismo secret, `X-Reply-Auth-Token` o `Authorization: Bearer …` | `sendMessage` a un chat allowlisted |

Cuerpo de reenvío a Grok (`Authorization: Bearer $GROK_WEBHOOK_KEY`):

```json
{
  "source": "telegram",
  "chat_id": 123,
  "text": "…",
  "from": {"id": 1, "username": "…", "first_name": "…"},
  "update_id": 42
}
```

El webhook responde **200 enseguida** a Telegram; el POST a Grok va en
background con timeout corto (`GROK_FORWARD_TIMEOUT_SECONDS`, por defecto 2 s).

## Variables de entorno

| Variable | Obligatoria | Uso |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | sí (para `/reply`) | Token del bot |
| `TELEGRAM_WEBHOOK_SECRET` | sí | Secret de `setWebhook` y auth de `/reply` |
| `ALLOWED_CHAT_IDS` | sí | IDs numéricos separados por coma (DM/grupo/canal) |
| `GROK_WEBHOOK_URL` | sí | URL POST de la rutina Grok Bot |
| `GROK_WEBHOOK_KEY` | sí | Key de la rutina (`Authorization: Bearer …`) |
| `REPLY_AUTH_TOKEN` | no | Alternativa a `TELEGRAM_WEBHOOK_SECRET` para `/reply` |
| `GROK_FORWARD_TIMEOUT_SECONDS` | no | Timeout del reenvío (default `2.0`) |

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
printf '%s' "$GROK_WEBHOOK_KEY" | gcloud secrets create telegram-bridge-grok-key \
  --project="$PROJECT" --data-file=- --replication-policy=automatic

# Si ya existen: gcloud secrets versions add … --data-file=-

gcloud run deploy "$SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --source=services/telegram-bridge \
  --allow-unauthenticated \
  --set-env-vars="ALLOWED_CHAT_IDS=<CHAT_ID>,GROK_WEBHOOK_URL=<GROK_WEBHOOK_URL>" \
  --set-secrets="TELEGRAM_BOT_TOKEN=telegram-bridge-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-bridge-webhook-secret:latest,GROK_WEBHOOK_KEY=telegram-bridge-grok-key:latest"
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
  -d '{"chat_id": <CHAT_ID>, "text": "ping desde el puente"}'
```

## Runbook

Ver `docs/runbooks/telegram-bridge.md`.
