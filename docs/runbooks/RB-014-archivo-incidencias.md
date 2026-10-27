# RB-014 — Archivo de supresiones, paradas omitidas y avisos (`ARCHIVAR_CANCELACIONES`, `ARCHIVAR_AVISOS`)

Dos banderas del job `cercanias-ingesta`, **apagadas por defecto**. Con ambas a
`0` (o sin definir) el job se comporta exactamente igual que antes: mismo
`archivo/<dia>/*.json`, mismo `boards.json`, ninguna petición a `alerts.json`.
Encenderlas es decisión de Juan.

| Variable | Defecto | Qué hace |
|---|---|---|
| `ARCHIVAR_CANCELACIONES` | `0` | Añade `canc` y `skip` a cada objeto de `archivo/<dia>/<hhmmss>.json` |
| `ARCHIVAR_AVISOS` | `0` | Descarga `https://gtfsrt.renfe.com/alerts.json` una vez por invocación y guarda los avisos de Madrid en `archivo/alerts/<dia>/<hhmmss>.json` **sólo cuando cambian** |
| `ALERTS_ARCHIVE_PREFIX` | `archivo/alerts` | Prefijo de los avisos archivados |
| `ALERTS_STATE_KEY` | `alerts-state.json` | Estado (bucket privado) para saber si los avisos han cambiado |
| `ALERTS_STALE_HOURS` | `6` | Edad de cabecera a partir de la cual el feed se considera congelado |

Sólo `1` enciende una bandera (`true`, `si`… cuentan como apagada).

## Formato

**`canc` / `skip`** (dentro del objeto de pasada, en el bucket privado):
```json
{"ts":"…","n":71,"cols":[…],"trains":[…],
 "canc":["1093J20001C3"],"skip":{"1093J20316C4a":["17004","17005"]}}
```
- Clave presente y vacía (`"canc":[]`) = se consultó trip_updates y no había.
- Clave **ausente** = no se sabe (bandera apagada o trip_updates caído en esa
  pasada). El histórico lo trata como nulo, nunca como «cero supresiones».
- `added` (viajes `ADDED`) no se guarda: el conector no los parsea hoy.

**Avisos** (`archivo/alerts/<dia>/<hhmmss>.json`, NDJSON de una línea):
```json
{"ts":"2026-10-27T09:30:05+01:00","feed_ts":"2026-10-07T09:32:52+02:00","hash":"…","n":15,
 "alerts":[{"id":"AVISO_518335","start":"…","end":"…","lines":["C3"],"stops":[],
            "text":"…","tipo":"obras"}]}
```
- Madrid = `routeId` que empieza por `10T`, `stopId` de 5 cifras con prefijo de
  estación de Madrid (`MADRID_STOP_PREFIXES` en `cercanias_domain/avisos.py`,
  sacado de las 95 paradas del GTFS) o hashtag `#MadCx` en el texto.
- `tipo` por palabras clave, en este orden de precedencia: `personas_en_via`,
  `meteorologia`, `averia`, `obras`, `reajuste`, `otros`
  (`packages/domain/src/cercanias_domain/avisos.py`).
- El hash es SHA-256 del conjunto normalizado (sin cabecera): un feed congelado
  no genera objetos nuevos.

**Estado** `alerts-state.json` (bucket privado, mismo patrón que `alert-state.json`):
`{"hash","key","written_at","stale_warned_at"}`. Si se borra o se corrompe, el
siguiente tick reescribe los avisos una vez; no hay más efecto.

**Salud** (sólo con `ARCHIVAR_AVISOS=1`), en `boards.json → health`:
`alerts_feed_stale` (bool o `null` si no hay cabecera/descarga),
`alerts_feed_age_seconds`, `alerts_fetch_failed`, `alerts_madrid`.
Es informativo: **no** pone `degraded` en el snapshot.

## Coste de almacenamiento (estimado)
- `canc`/`skip`: ~2-4 MB/día (≈4 300 pasadas/día × unos cientos de bytes).
- Avisos: <1 MB/día. Con el feed real del 7-oct cada escritura son ~4-5 KB
  (15 avisos de Madrid) y sólo se escribe cuando cambia.

No hay backfill posible: Renfe no publica histórico de trip_updates ni de
avisos; el archivo empieza el día que se encienda.

## Síntoma
- No aparecen objetos en `archivo/alerts/` o las pasadas no llevan `canc`/`skip`
  con las banderas encendidas.
- WARNING `alerts_feed_stale` en Cloud Logging (como mucho uno por hora) o
  `boards.json → health.alerts_feed_stale: true`. **Esperado** mientras el feed
  siga congelado: la cabecera de `alerts.json` está fija en
  2026-10-07 09:32:52 (Madrid) desde el 7 de octubre.
- WARNING/ERROR `alerts_fetch_failed` o ERROR `alerts_archive_failed`.

## Checks
```bash
# ¿banderas puestas en el job?
gcloud run jobs describe cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
  --format=yaml | grep -A1 -E 'ARCHIVAR_|ALERTS_'
# ¿el feed está vivo? (cabecera en hora local)
curl -s https://gtfsrt.renfe.com/alerts.json | jq '.header.timestamp|tonumber|strflocaltime("%F %T")'
# logs estructurados de avisos
gcloud logging read 'resource.labels.job_name="cercanias-ingesta" jsonPayload.alert=~"^alerts_"' \
  --project cercanias-en-hora --limit 20 --format='value(timestamp,severity,jsonPayload.alert,jsonPayload.error)'
# salud en boards.json
curl -s "https://storage.googleapis.com/cercanias-madrid-rt-web/boards.json?t=$(date +%s)" | jq '.health'
```
Un fallo de `alerts.json` (timeout de 5 s, 1 reintento en 5xx) nunca tumba el
tick ni los tablones: si los tablones faltan, el problema es otro (RB-001/RB-002).

## Encender (con OK de Juan)
> ⚠️ Usar **siempre** `--update-env-vars`. **Nunca** `--set-env-vars`: reemplaza
> TODAS las variables del job y borraría `PRIVATE_BUCKET`, `SNAPSHOT_BUCKET_GCS`, etc.

```bash
gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
  --update-env-vars ARCHIVAR_CANCELACIONES=1,ARCHIVAR_AVISOS=1
```
Se pueden encender por separado. Requiere una imagen que incluya este cambio.

## Verify
```bash
DIA=$(TZ=Europe/Madrid date +%F)
gcloud storage ls "gs://cercanias-madrid-rt-privado/archivo/alerts/$DIA/"
gcloud storage cat "gs://cercanias-madrid-rt-privado/alerts-state.json" | jq '{key,written_at,stale_warned_at}'
# una pasada reciente lleva canc/skip
OBJ=$(gcloud storage ls "gs://cercanias-madrid-rt-privado/archivo/$DIA/" | tail -1)
gcloud storage cat "$OBJ" | jq '{ts,n,canc,skip}'
```
- Con el feed congelado: **un** objeto en `archivo/alerts/<dia>/` tras el primer
  tick y ninguno más mientras no cambie; un WARNING `alerts_feed_stale` por hora.
- Las ejecuciones siguen terminando bien:
  `gcloud run jobs executions list --job cercanias-ingesta --region europe-southwest1 --limit 5`.

## Rollback
```bash
gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
  --remove-env-vars ARCHIVAR_CANCELACIONES,ARCHIVAR_AVISOS
```
Vuelve al comportamiento previo en el siguiente tick. Los objetos ya escritos
no molestan: `scripts/analizar_archivo.py` sólo lee carpetas `YYYY-MM-DD` e
ignora claves que no conoce.
