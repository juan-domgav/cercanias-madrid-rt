# telegram-bridge — síntoma → fix

Puente Cloud Run `telegram-bridge` (`europe-southwest1`, proyecto
`cercanias-en-hora`): DMs del bot → webhook Grok Bot; `/reply` opcional hacia
Telegram. Código en `services/telegram-bridge/`.

## Sintoma → causa → fix

| Sintoma | Causa probable | Fix |
|---|---|---|
| Telegram no entrega updates (`getWebhookInfo` con `last_error_message`) | URL mal, certificado, o servicio caído | Comprobar `status.url` del servicio; `curl $URL/healthz`; volver a `setWebhook` |
| Updates llegan pero Grok no arranca | `GROK_WEBHOOK_URL` / `GROK_WEBHOOK_KEY` mal o rutina pausada | Revisar logs del servicio (`forward` / timeout); renovar key en la rutina; confirmar Active |
| `401` en `/telegram/webhook` | `secret_token` ≠ `TELEGRAM_WEBHOOK_SECRET` | Alinear Secret Manager y `setWebhook`; redeploy si el secreto no está montado |
| Mensajes ignorados (200 `ignored`) | `chat.id` fuera de `ALLOWED_CHAT_IDS` | Añadir el id numérico (DM o grupo) a la env var y redesplegar |
| Grupos/canales sin efecto | Id no allowlisted (por diseño) | Incluir el `chat.id` negativo del grupo/canal, o usar solo DM |
| `/reply` → `401` | Falta header de auth | `Authorization: Bearer $TELEGRAM_WEBHOOK_SECRET` o `X-Reply-Auth-Token` |
| `/reply` → `403` | `chat_id` no allowlisted | Mismo allowlist que el webhook |
| `/reply` → 5xx / sendMessage falla | Token inválido o bot bloqueado | `getMe` con el token; revisar Secret Manager `telegram-bridge-bot-token` |
| Respuesta lenta a Telegram / reintentos | Reenvío a Grok bloqueando (no debería) | Confirmar que el POST a Grok va en background; bajar `GROK_FORWARD_TIMEOUT_SECONDS` |

## Checks rápidos

```bash
PROJECT=cercanias-en-hora
REGION=europe-southwest1
SERVICE=telegram-bridge

gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)'

curl -sS "$(gcloud run services describe "$SERVICE" --project="$PROJECT" \
  --region="$REGION" --format='value(status.url)')/healthz"

gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="telegram-bridge"' \
  --project="$PROJECT" --limit=30 --format='value(textPayload,jsonPayload.message)'

curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

## Rollback

```bash
# Pausar tráfico entrante de Telegram sin borrar el servicio
curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"

# O volver a una revisión anterior
gcloud run services update-traffic telegram-bridge \
  --project=cercanias-en-hora --region=europe-southwest1 \
  --to-revisions=<REVISION>=100
```
