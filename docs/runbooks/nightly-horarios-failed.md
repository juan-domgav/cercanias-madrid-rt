# Nightly horarios failed

## Symptom

El Cloud Run job `cercanias-horarios` falla, no arranca, o termina con
**cobertura insuficiente**: `regenerate-schedules` debe publicar el lote de
días de servicio desde el GTFS; con umbral `MIN_SERVICE_DAYS` (**25** por
defecto) una corrida con menos de 25 días se marca como fallida
(`severity=ERROR`, código de salida 2 en esa variante). El Scheduler
`cercanias-horarios-nightly` (`17 5 * * *` Europe/Madrid, región
`europe-west1`) es quien lo dispara tras la ventana de actualización de Renfe
(~04:08).

Si no se corrige, en días sucesivos faltará `horarios/<servicio>.json` y los
tablones quedarán vacíos (ver `empty-boards-missing-horarios.md`).

## Checks

```bash
gcloud run jobs executions list --job cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora --limit 5

gcloud scheduler jobs describe cercanias-horarios-nightly \
  --location europe-west1 --project cercanias-en-hora

# Contar días publicados
gcloud storage ls gs://cercanias-madrid-rt-web/horarios/*.json | wc -l
gcloud storage ls gs://cercanias-madrid-rt-web/horarios/ | sort | head
gcloud storage ls gs://cercanias-madrid-rt-web/horarios/ | sort | tail

# Logs (sustituir EXECUTION_ID)
gcloud run jobs executions logs read <EXECUTION_ID> \
  --region europe-southwest1 --project cercanias-en-hora
```

Buscar: `ERROR FATAL` (GTFS no disponible), `horarios publicados: N días`, o
línea con `severity=ERROR` y mínimo 25 días. Alerta recomendada en Monitoring:
`resource.type="cloud_run_job"` +
`job_name=("cercanias-ingesta" OR "cercanias-horarios")` + `severity>=ERROR`.

## Fix

```bash
# Reintento inmediato
gcloud run jobs execute cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora --wait

# Local / diagnóstico
make regenerate-schedules
```

Si el GTFS de Renfe está incompleto o caído, esperar a que publique el lote
~30 días y reintentar; no bajar `MIN_SERVICE_DAYS` en prod salvo incidente
consciente. Si el job no está desplegado, seguir `docs/DESPLIEGUE.md` (misma
imagen que `cercanias-ingesta`, comando
`uv run --frozen --no-sync regenerate-schedules`,
`SNAPSHOT_BUCKET_GCS=cercanias-madrid-rt-web`).

Tras un éxito, opcional: un tick de `cercanias-ingesta` para refrescar
`boards.json`.

## Verify

```bash
gcloud run jobs executions list --job cercanias-horarios \
  --region europe-southwest1 --project cercanias-en-hora --limit 1

gcloud storage ls gs://cercanias-madrid-rt-web/horarios/*.json | wc -l
# Esperado: >= 25 (típicamente ~30)
```

Esperado: última ejecución **Succeeded** y N ≥ 25 objetos bajo `horarios/`.

## Rollback

No revertir horarios buenos con un lote truncado: volver a ejecutar cuando el
GTFS esté completo, o restaurar `horarios/*.json` desde backup. Pausar el
nightly solo si hay reintentos inútiles:

```bash
gcloud scheduler jobs pause cercanias-horarios-nightly \
  --location europe-west1 --project cercanias-en-hora
```

Reanudar con `gcloud scheduler jobs resume …` cuando el origen GTFS sea fiable.
