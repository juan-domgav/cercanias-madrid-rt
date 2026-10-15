# RB-002 — Snapshot viejo o degradado

## Sintoma
`snapshot.json` con `generated_at` >5 min, o `degraded: true` /
`stale_since` presente. La web muestra «datos no actualizados».

## Checks
```bash
curl -s "$WEB/snapshot.json" | jq '{generated_at,degraded,reason,stale_since,n:(.vehicles|length)}'
gcloud run jobs executions list --job=cercanias-ingesta --region=europe-southwest1 --limit=5
gcloud scheduler jobs describe cercanias-tick --location=europe-west1
curl -sI -m 20 https://gtfsrt.renfe.com/vehicle_positions.json | head -2
curl -sI -m 20 https://gtfsrt.renfe.com/trip_updates.json | head -2
gcloud logging read 'resource.labels.job_name="cercanias-ingesta" severity>=ERROR' \
  --limit=10 --project=cercanias-en-hora
```

## Fix
```bash
# planificador pausado → reanudar
gcloud scheduler jobs resume cercanias-tick --location=europe-west1
# forzar un tick
gcloud run jobs execute cercanias-ingesta --region=europe-southwest1
# si Renfe cae: esperar; el tick conserva el último bueno (ADR-011) hasta STALE_WARNING_SECONDS
```

## Verify
Nuevo `generated_at` reciente, sin `degraded`, `vehicles.length` > 0 en hora de servicio.

## Rollback
`gcloud scheduler jobs pause cercanias-tick --location=europe-west1` detiene gasto
sin borrar datos; el último snapshot sigue sirviéndose.
