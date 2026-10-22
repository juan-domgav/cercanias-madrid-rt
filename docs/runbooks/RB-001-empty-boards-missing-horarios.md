# RB-001 — Tablones vacíos, snapshot sano

## Sintoma
`boards.json` tiene `stations_with_departures: 0` y
`health.schedule_missing: true`, pero `snapshot.json` trae trenes frescos y no
está `degraded`. La app parece «sin trenes» en hora punta.

`stations_with_departures` cuenta sólo salidas DEL HORARIO: con trenes
especiales circulando, `stations` puede no estar vacío (tablones sólo con
especiales, `specials_on_boards > 0`) y el síntoma sigue siendo este. Ver RB-006.

## Causa
Falta `horarios/<servicio>.json` en el bucket (el índice rota a diario,
ADR-012). Ocurrió tras el 29/08/2026: los horarios subidos a mano caducaron y
el tick salía 0 sin alerta.

## Checks
```bash
curl -s "$WEB/boards.json" | jq '{service,stations_with_departures,specials_on_boards,health}'
curl -s "$WEB/snapshot.json" | jq '{generated_at,n:(.vehicles|length),degraded}'
# WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
gcloud storage ls "gs://cercanias-madrid-rt-web/horarios/${SERVICIO}.json"
gcloud logging read 'jsonPayload.alert="schedule_missing"' --limit=5 --project=cercanias-en-hora
```

## Fix
```bash
gcloud run jobs execute cercanias-horarios --region europe-southwest1 --wait
# local: SNAPSHOT_DIR=/tmp/out uv run regenerate-schedules
#        gcloud storage cp /tmp/out/horarios/*.json gs://cercanias-madrid-rt-web/horarios/
```

## Verify
Tras el siguiente tick (`cercanias-tick` cada minuto): `stations_with_departures > 0`
y `health.schedule_missing: false`. Contar días por delante con la cadena completa
del id (letra incluida; ver RB-004), no solo el número.

## Rollback
No aplica: regenerar no borra. Si un fichero nuevo está mal, restaurar el objeto
GCS desde una versión anterior del bucket.
