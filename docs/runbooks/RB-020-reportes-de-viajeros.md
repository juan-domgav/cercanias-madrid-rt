# RB-020 — Reportes de viajeros al bot de ops (privado y menciones en la comunidad)

## Para qué
Juan anunció en la comunidad pública `t.me/cercanias_madrid_rt` que los
viajeros pueden contar fallos de la app al bot de ops (`@CercaniasRTOPS_bot`).
Hasta este cambio el bot **descartaba en silencio** cualquier mensaje que no
viniera del chat de Juan (`OPS_ALLOWED_CHAT_IDS`, por defecto `5830900654`): sin
respuesta, sin buzón, solo un `WARNING chat no permitido` en los logs.

## Cómo funciona (webhook de `cercanias-api`)
| Origen | Qué pasa |
|---|---|
| Privado, chat de Juan | Igual que siempre: comandos de ops (`estado`, `horarios`, `regenerar`, `ayuda`) y el resto al buzón público `ops/inbox.json` (RB-005) |
| Privado, cualquier otra persona | Reporte en el bucket **privado** + respuesta «Gracias, recibido. Lo revisamos.» (citando su mensaje). `/start` recibe una bienvenida y no se guarda. **Ningún comando de ops**: `estado` es un reporte más |
| Grupo / supergrupo | Solo si **menciona** al bot (`@CercaniasRTOPS_bot`, entidad `mention`/`text_mention`) o **responde** a un mensaje suyo: reporte + respuesta en el hilo (y en el mismo tema si el grupo usa temas). Todo lo demás se ignora en silencio. **Nunca** comandos desde un grupo, tampoco de Juan |
| Canal, bots, mensajes de servicio | Se ignoran |

- Fotos, documentos, audio… igual que en RB-005/RB-019: `text` = pie o
  marcador (`[foto]`), más `type` y `file_id`.
- **Dónde**: `gs://cercanias-madrid-rt-privado/ops/reports/<día UTC>/<update_id>.json`,
  un objeto por mensaje, escrito «solo si no existe»: sin tope (no se pierde
  nada), y una reentrega de Telegram no duplica ni vuelve a contestar.
  Campos: `ts`, `update_id`, `message_id`, `text`, `source` (`private`|`group`),
  `chat_id`, `chat_type`, `chat_title`, `from_id`, `from_username`, `type`, `file_id`.
- **Por qué privado**: llevan usuario y chat de terceros. El buzón público
  `ops/inbox.json` sigue siendo solo de Juan (tope 20; no se sube porque el
  vigía lee sus primeros 3000 bytes y con más items perdería los nuevos).
- **Límite por usuario**: 5 reportes cada 10 min (`ops/report-rate.json`, bucket
  privado). Al pasarse se avisa una vez («Has enviado varios mensajes
  seguidos…») y después se calla hasta que se vacía la ventana.
- Si falla la escritura del reporte: 503 y Telegram lo reentrega (no se contesta
  antes de guardar).
- Logs: `reporte de viajero guardado` (sin usuario ni texto), `reporte de
  viajero por encima del límite`, `grupo no permitido para reportes`.

Variables de `cercanias-api` (todas opcionales):
- `OPS_RIDER_REPORTS` — `0` apaga los reportes sin desplegar (vuelve a ignorar
  chats ajenos). Por defecto encendido.
- `PRIVATE_BUCKET` — ya existe. Sin él **no** hay reportes (nunca al bucket público).
- `OPS_REPORT_CHAT_IDS` — ids de grupo aceptados (coma). Vacío = cualquier grupo.
- `OPS_BOT_USERNAME` — por defecto `CercaniasRTOPS_bot`. El id del bot (para
  reconocer respuestas) sale del propio token; el token no se registra.

## Añadir el bot a la comunidad (lo hace Juan, admin del grupo)
1. En @BotFather → `/mybots` → CercaníasRT-Ops → *Bot Settings*:
   - *Group Privacy* = **Enabled** (`/setprivacy` → Enable). Es el valor por
     defecto: con él el bot solo recibe menciones, respuestas a sus mensajes y
     comandos; la charla del grupo no le llega.
   - *Allow Groups?* = **Enabled** (`/setjoingroups`), si no no se puede añadir.
2. En la comunidad: *Añadir miembros* → `@CercaniasRTOPS_bot`.
   **No** hacerlo administrador: un bot administrador recibe TODOS los mensajes
   aunque tenga el modo privacidad activado.
3. Si `t.me/cercanias_madrid_rt` es un **canal** (solo publica Juan), el bot
   necesita el grupo de comentarios vinculado o un grupo aparte: en un canal no
   hay menciones de viajeros. En el canal basta con enlazar `t.me/CercaniasRTOPS_bot`.
4. Opcional, tras el primer reporte del grupo: leer su `chat_id` del reporte y
   fijar `OPS_REPORT_CHAT_IDS` para que menciones desde otros grupos se ignoren
   (cambio de configuración del servicio, con OK de Juan):
   ```bash
   gcloud run services update cercanias-api --region europe-southwest1 \
     --project cercanias-en-hora --update-env-vars OPS_REPORT_CHAT_IDS=<id>
   ```

## Leer los reportes
```bash
DIA=$(date -u +%F)
gcloud storage ls gs://cercanias-madrid-rt-privado/ops/reports/$DIA/
gcloud storage cat "gs://cercanias-madrid-rt-privado/ops/reports/$DIA/*.json"
```
El vigía (`check.py`) solo lee el buzón **público**: para avisar de reportes de
viajeros hay que añadirle este listado (con el `gcloud` del box).

## Verificar (tras desplegar `cercanias-api`)
1. Desde una cuenta que NO sea la de Juan, por privado: «prueba RB-020» →
   responde «Gracias, recibido…» y aparece `ops/reports/<día>/<update_id>.json`
   con `source: private`. `estado` desde esa cuenta → «Gracias, recibido…»,
   nunca el estado.
2. En la comunidad: «@CercaniasRTOPS_bot prueba» → respuesta citando el
   mensaje y reporte con `source: group`. Un mensaje sin mención → nada.
3. Juan por privado: `estado` sigue contestando el estado; un texto libre sigue
   yendo a `ops/inbox.json`.
4. `getWebhookInfo` (nunca `getUpdates`): `pending_update_count` 0 y sin
   `last_error_message`.

## Rollback
- Sin desplegar: `gcloud run services update cercanias-api --region europe-southwest1
  --project cercanias-en-hora --update-env-vars OPS_RIDER_REPORTS=0` (crea una
  revisión con la misma imagen).
- Imagen anterior: `gcloud run services update-traffic cercanias-api
  --region europe-southwest1 --project cercanias-en-hora --to-revisions cercanias-api-00011-mh4=100`.
- Los reportes ya guardados se quedan en el bucket privado (no borrar sin Juan).
