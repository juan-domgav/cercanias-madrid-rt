# RB-003 — Job nocturno de horarios fallido

## Sintoma
Ejecución de `cercanias-horarios` en estado FAILED, o logs con
`severity=ERROR` y «solo N días de servicio» (N < 25). Scheduler:
`cercanias-horarios-nightly` (05:17 Europe/Madrid, `europe-west1`).

## Checks
```bash
gcloud run jobs executions list --job=cercanias-horarios --region=europe-southwest1 --limit=5
gcloud run jobs executions logs read EXECUTION_ID --region=europe-southwest1
gcloud scheduler jobs describe cercanias-horarios-nightly --location=europe-west1
curl -sI https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip | head -5
# conteo en bucket (aprox.)
gcloud storage ls gs://cercanias-madrid-rt-web/horarios/ | wc -l
```

## Fix
```bash
# reintento inmediato
gcloud run jobs execute cercanias-horarios --region=europe-southwest1 --wait
# si el ZIP de Renfe falla: reintentar más tarde (suele actualizarse ~04:08 Madrid)
# si publica <25 días pero el GTFS es correcto: revisar filtro Madrid / calendar.txt
```

## Verify
La ejecución termina 0. `gcloud storage ls .../horarios/ | wc -l` ≥ 25.
`boards.json` tras un tick: `health.schedule_days_left` ≥ 5.

## Rollback
Los objetos publicados en el intento fallido parcial pueden quedar; no borrar a
ciegas. Restaurar versiones anteriores de GCS si un día concreto quedó corrupto.
