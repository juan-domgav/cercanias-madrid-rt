# RB-015 — `health.delay_coverage` crítico con el snapshot sano (los viajes sin retraso son suprimidos)

## Síntoma
`snapshot.json → health.delay_coverage.severity: "critical"` con un detalle tipo
`solo 52/65 (80.0%) tripUpdate con retraso publicado; minimo exigido 90%`, pero:

- `degraded` no aparece (o es `false`), `generated_at` tiene segundos y hay trenes;
- la web y el bot no muestran ningún aviso (sólo miran `degraded`);
- **todos** los `tripUpdate` de Madrid sin retraso son viajes suprimidos
  (`trip.scheduleRelationship = "CANCELED"`).

Visto el 29/10/2026 desde ~12:57 (Madrid): 13 de 65 viajes sin retraso, los 13
`CANCELED` en C1, C2, C3, C4a/b, C5, C7, C8a y C10.

## Causa
`evaluate_feed_health` (`packages/domain/src/cercanias_domain/health.py`)
calculaba la cobertura como `with_delay / matched_nucleo` de
`parse_trip_updates`, y `matched_nucleo` incluye los viajes `CANCELED`. Un tren
suprimido nunca publica retraso, así que cada jornada con muchas supresiones
bajaba la cobertura del 90 % sin que faltara ningún dato. Crítico falso.
No tiene que ver con `ARCHIVAR_CANCELACIONES`/`ARCHIVAR_AVISOS` (RB-014): esas
banderas sólo archivan; el cálculo de salud era el mismo antes.

## Checks
```bash
WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
# 1. ¿Crítico sólo en delay_coverage, con snapshot fresco y sin degraded?
curl -s "$WEB/snapshot.json?t=$(date +%s)" | jq '{generated_at,degraded,trenes:(.vehicles|length),
  malos:[.health|to_entries[]|select(.value.severity!="ok")|{(.key):.value.detail}]}'

# 2. En el feed de Renfe: suprimidos vs viajes sin retraso (Madrid = tripId "10…")
curl -s https://gtfsrt.renfe.com/trip_updates.json | jq '
  [.entity[].tripUpdate | select(.trip.tripId|startswith("10"))] as $m
  | def sin_delay: (.delay==null) and ([.stopTimeUpdate[]? | (.arrival.delay // .departure.delay)] | map(select(.!=null)) | length == 0);
  { total: ($m|length),
    cancelados: ([$m[] | select(.trip.scheduleRelationship=="CANCELED")] | length),
    sin_retraso: ([$m[] | select(sin_delay)] | length),
    sin_retraso_no_cancelados: ([$m[] | select(.trip.scheduleRelationship!="CANCELED" and sin_delay)] | length),
    acortados_skipped: ([$m[] | select(any(.stopTimeUpdate[]?; .scheduleRelationship=="SKIPPED"))] | length) }'

# 3. (Opcional, sólo lectura) las supresiones archivadas en la última pasada (RB-014)
gcloud storage ls "gs://cercanias-madrid-rt-privado/archivo/$(TZ=Europe/Madrid date +%F)/" | tail -1 \
  | xargs gcloud storage cat | jq '{canc:(.canc|length)}'
```
Es este runbook si `sin_retraso_no_cancelados` es 0 (o muy pequeño) y
`total - cancelados` cumpliría el 90 %. Si hay muchos viajes **no** suprimidos
sin retraso, la pérdida es real: Renfe ha dejado de publicar retrasos o el
parser no los reconoce (formato nuevo); no es este runbook.

Nota: el feed se lee en un momento distinto al del snapshot; los números del
check 2 no cuadran exactos con el detalle del snapshot, sólo en proporción.

## Fix
PR «fix(health): delay_coverage excluye viajes suprimidos (RB-015)»:

- `FeedHealthInput` gana `trip_updates_cancelled` y
  `trip_updates_cancelled_with_delay` (por defecto 0: compatibles).
- `delay_coverage` = `(with_delay − suprimidos con delay) / (total − suprimidos)`.
  Sólo se excluyen supresiones de viaje completo (`CANCELED`); un viaje con
  paradas `SKIPPED` (acortado) sigue circulando y **sí** cuenta.
- Todos suprimidos → denominador 0 → `ok` «sin datos que evaluar» (mismo
  criterio que el resto de tasas; la falta de trenes la vigila
  `nucleo_filter_yield`). Nunca divide por cero.
- `snapshot.json → health.delay_coverage` gana `n_cancelados` (clave aditiva;
  ningún consumidor lee `health` del snapshot salvo para mostrarlo) y el
  detalle dice `(N suprimidos excluidos)`.
- `run_cycle` (`apps/worker/src/cercanias_worker/mvp.py`) pasa
  `len(cancelled_trips)` y `TripUpdatesResult.cancelled_with_delay`.

Desplegar (con OK de Juan) **sólo `cercanias-ingesta`**: es lo único que importa
`cercanias_domain.health` (vía `cercanias_worker.mvp`). `cercanias-horarios` y
el bot de ops comparten imagen pero no evalúan salud; API y web no cambian.
Procedimiento de `docs/ESTADO.md` / RB-011, desde un `git archive` limpio de
`main` tras el merge:
```bash
git archive <sha> | tar -x -C /tmp/src && cd /tmp/src
gcloud run jobs describe cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora --format=yaml > /tmp/antes.yaml
gcloud run jobs deploy cercanias-ingesta --source . --region europe-southwest1 --project cercanias-en-hora
gcloud run jobs describe cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora --format=yaml > /tmp/despues.yaml
diff /tmp/antes.yaml /tmp/despues.yaml   # sólo debe cambiar la imagen
```
**Nunca** `--set-env-vars`: borraría `ARCHIVAR_CANCELACIONES=1`,
`ARCHIVAR_AVISOS=1` y el resto. `deploy --source` sin flags de env las conserva.

## Verify
- Las 2-3 ejecuciones siguientes terminan bien:
  `gcloud run jobs executions list --job cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora --limit 5`.
- Check 1: `health.delay_coverage` en `ok` con detalle
  `N/M (…%) tripUpdate con retraso publicado (K suprimidos excluidos)` y
  `n_cancelados` ≈ `cancelados` del check 2. Sin supresiones: `n_cancelados: 0`
  y el detalle de siempre.
- El resto de `health.*` sin cambios; `boards.json` fresco; el archivo sigue
  llevando `canc`/`skip` (RB-014).

## Rollback
Volver a la imagen anterior (la de PR #15, `374c116`); conserva las banderas:
```bash
gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
  --image europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:b747dd76934340ef44373aa29c8f79d361115aee8ce0105de7156adabdebc9e8
```
Vuelve el crítico falso en días con supresiones, pero nada más: el snapshot se
sigue publicando y `n_cancelados` desaparece (clave aditiva, nadie depende de ella).
