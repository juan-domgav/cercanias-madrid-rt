# Empty boards — missing horarios

## Symptom

La web o el bot muestran **0 estaciones / 0 salidas**. `boards.json` sale vacío
(o con `stations_with_departures: 0`) aunque haya trenes en el feed. En logs de
`cercanias-ingesta` aparece `AVISO GRAVE: no existe horarios/<servicio>.json`
(o, tras automatizar horarios, `severity=ERROR` con `alert=schedule_missing`).

Causa habitual: el día de servicio en circulación no tiene fichero en el bucket
(`horarios/<servicio>.json`, p. ej. `horarios/1085X.json`). El índice de
calendario rota a diario (ADR-012); el GTFS de Renfe sólo cubre ~30 días.

## Checks

```bash
# Qué servicio espera la ingesta hoy (campo service en boards)
gcloud storage cat gs://cercanias-madrid-rt-web/boards.json | python3 -c \
  "import sys,json; b=json.load(sys.stdin); print(b.get('service'), b.get('stations_with_departures'), b.get('health'))"

# ¿Existe el fichero de ese servicio?
SERVICIO=<service_id>   # p. ej. 1085X
gcloud storage ls "gs://cercanias-madrid-rt-web/horarios/${SERVICIO}.json"

# Cobertura actual de horarios en el bucket
gcloud storage ls gs://cercanias-madrid-rt-web/horarios/ | wc -l

# Últimas ejecuciones del regenerador nocturno
gcloud run jobs executions list --job cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora
```

## Fix

Regenerar y publicar horarios (misma lógica que el job nocturno):

```bash
# Preferido en prod: ejecutar el Cloud Run job
gcloud run jobs execute cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora --wait

# Alternativa local (Makefile / entrypoint regenerate-schedules)
make regenerate-schedules
```

Luego forzar un tick de ingesta para reconstruir `boards.json`:

```bash
gcloud run jobs execute cercanias-ingesta \
  --region europe-southwest1 --project cercanias-en-hora
```

Si el job `cercanias-horarios` o el Scheduler `cercanias-horarios-nightly`
(`17 5 * * *` Europe/Madrid, `europe-west1`) no están desplegados aún, seguir
`docs/DESPLIEGUE.md` (sección de regeneración nocturna / automatización) y
reintentar.

## Verify

```bash
gcloud storage cat gs://cercanias-madrid-rt-web/boards.json | python3 -c \
  "import sys,json; b=json.load(sys.stdin); print('service', b.get('service')); print('with_deps', b.get('stations_with_departures')); print('stations', len(b.get('stations') or {}))"
```

Esperado: `stations_with_departures` > 0 y el objeto `horarios/<servicio>.json`
presente. Recargar la web (cache snapshot/boards ~30 s).

## Rollback

No hay rollback de código: si la regeneración publicó un lote malo, volver a
ejecutar `cercanias-horarios` / `make regenerate-schedules` cuando el GTFS de
Renfe esté sano, o restaurar objetos `horarios/*.json` desde una copia previa
del bucket si existe. Pausar el Scheduler de horarios solo si está spameando
fallos:

```bash
gcloud scheduler jobs pause cercanias-horarios-nightly --location europe-west1 \
  --project cercanias-en-hora
```
