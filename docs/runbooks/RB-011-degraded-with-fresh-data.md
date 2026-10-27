# RB-011 — «Datos conservados» / `degraded: true` con datos frescos (última pasada de Renfe vacía)

## Síntoma
`snapshot.json` sale con `degraded: true` y
`reason: "pasada sin datos de Madrid; se conserva el ultimo bueno"` en muchos
ticks, aunque `generated_at` tiene ~20-60 s y hay 50-90 trenes. La web muestra el
aviso de datos conservados sobre datos que sirven. Visto el 25/10/2026 desde
~05:50 (Madrid).

## Causa
Cada invocación de `cercanias-ingesta` hace 3 pasadas (20 s). Si la pasada 3 (o
la 2) viene vacía —el núcleo desaparece del feed, ADR-011—, `SnapshotStore.publish`
conserva la pasada anterior **de la misma invocación** y la marca `degraded` con
`stale_since`. `_snapshot_a_publicar` (`apps/worker/src/cercanias_worker/cloud_tick.py`)
la publicaba tal cual: el umbral `STALE_WARNING_SECONDS` (240 s) sólo se aplicaba
entre invocaciones. El 25/10, 04:51–05:03 UTC, la pasada 3/3 vino con
`trenes=0 salud=critical` en 7 de 12 invocaciones con el feed respondiendo 200.

## Checks
```bash
WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
curl -s "$WEB/snapshot.json?t=$(date +%s)" | jq '{generated_at,degraded,reason,stale_since,n:(.vehicles|length)}'
# ¿la última pasada de la invocación viene vacía?
gcloud logging read 'resource.labels.job_name="cercanias-ingesta" textPayload:"pasada"' \
  --project cercanias-en-hora --limit 12 --format='value(timestamp,textPayload)'
# ¿qué imagen corre? (PR #11 = sha256:ee6d9dad…)
gcloud run jobs describe cercanias-ingesta --region europe-southwest1 \
  --format='value(spec.template.spec.template.spec.containers[0].image)'
```
Es este runbook si: `degraded: true` + `stale_since` + `generated_at` de menos
de 4 min + `vehicles` no vacío + en logs `pasada 3/3: trenes=0` justo antes.
Si `generated_at` es viejo (>5 min) o no hay ejecuciones, es
[RB-002](RB-002-stale-snapshot.md).

## Fix
PR #11 (`0a6dc68`, desplegado el 25/10/2026): si el snapshot de la invocación
lleva `stale_since` y sus datos tienen menos de `STALE_WARNING_SECONDS`, se
quitan `degraded` y `reason` (se mantiene `stale_since`, informativo; la web sólo
mira `degraded`). Datos más viejos siguen marcados. Si vuelve a pasar con esta
imagen, comprobar que nadie ha bajado `STALE_WARNING_SECONDS` en el job.

Desplegar (con OK de Juan), desde un `git archive` limpio de `main`:
```bash
git archive <sha> | tar -x -C /tmp/src && cd /tmp/src
gcloud run jobs deploy cercanias-ingesta --source . --region europe-southwest1 --project cercanias-en-hora
```
Sólo cambia la imagen; env, secretos, SA y Scheduler quedan igual (comparar
`gcloud run jobs describe … --format=yaml` antes y después).

## Verify
- Las 2-3 ejecuciones siguientes terminan bien con la imagen nueva:
  `gcloud run jobs executions list --job cercanias-ingesta --region europe-southwest1 --limit 5`.
- Con una pasada final vacía, el log dice
  `pasada final vacia: se conserva la anterior (20s), aun vigente` y el snapshot
  publicado lleva `stale_since` pero **no** `degraded`. Visto en producción el
  25/10 a las 08:32 (Madrid).
- `snapshot.json` con <2 min, trenes > 0, `health.*.severity` ok; `boards.json` fresco.

## Rollback
```bash
gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
  --image europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:646a4d8b871226afb99de7f7d15a72a9f9196ae6397d776161227bfb84083294
```
Vuelve el aviso falso, pero nada más: el snapshot sigue publicándose.
