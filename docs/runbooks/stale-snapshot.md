# Stale snapshot

## Symptom

`snapshot.json` tiene `generated_at` antiguo (orden de **~4–5 min** o más) y/o
lleva `degraded: true` / `stale_since` / `reason` del estilo «sin datos nuevos
del nucleo…». La UI avisa de datos viejos. Umbral de aviso en código:
`STALE_WARNING_SECONDS` (por defecto **240** s) en
`apps/worker/src/cercanias_worker/cloud_tick.py` (ADR-011).

## Checks

```bash
# Edad del snapshot publicado
gcloud storage cat gs://cercanias-madrid-rt-web/snapshot.json | python3 -c \
  "import sys,json,datetime as d; s=json.load(sys.stdin); g=s.get('generated_at'); print('generated_at', g); print('degraded', s.get('degraded')); print('stale_since', s.get('stale_since')); print('reason', s.get('reason')); print('vehicles', len(s.get('vehicles') or [])); t=d.datetime.fromisoformat(g); print('age_s', (d.datetime.now(tz=d.UTC)-t).total_seconds())"

# Scheduler cada minuto (* * * * *, europe-west1)
gcloud scheduler jobs describe cercanias-tick \
  --location europe-west1 --project cercanias-en-hora

# Ejecuciones recientes de ingesta
gcloud run jobs executions list --job cercanias-ingesta \
  --region europe-southwest1 --project cercanias-en-hora --limit 10

# Logs de la última ejecución (sustituir EXECUTION_ID)
gcloud run jobs executions logs read <EXECUTION_ID> \
  --region europe-southwest1 --project cercanias-en-hora
```

Comprobar también si el Scheduler está en pausa y si el feed Renfe responde
(desapariciones intermitentes del núcleo: ADR-011).

## Fix

```bash
# Forzar un tick ahora
gcloud run jobs execute cercanias-ingesta \
  --region europe-southwest1 --project cercanias-en-hora

# Si el planificador estaba pausado, reanudarlo
gcloud scheduler jobs resume cercanias-tick \
  --location europe-west1 --project cercanias-en-hora
```

Si las ejecuciones fallan de forma sostenida: revisar imagen/deploy del job
(`docs/DESPLIEGUE.md`), cuotas, y logs. El token de Telegram se resuelve en
runtime para no tumbar la publicación del snapshot.

## Verify

```bash
gcloud storage cat gs://cercanias-madrid-rt-web/snapshot.json | python3 -c \
  "import sys,json,datetime as d; s=json.load(sys.stdin); g=d.datetime.fromisoformat(s['generated_at']); print('age_s', (d.datetime.now(tz=d.UTC)-g).total_seconds()); print('degraded', s.get('degraded')); print('n', len(s.get('vehicles') or []))"
```

Esperado: `age_s` del orden de **&lt; 120 s** en operación normal, sin
`degraded` (salvo desaparición real del núcleo en el feed). La web relee
`snapshot.json` cada ~30 s (`max-age` 30 s).

## Rollback

Para parar gasto / cascada de fallos sin borrar recursos:

```bash
gcloud scheduler jobs pause cercanias-tick \
  --location europe-west1 --project cercanias-en-hora
```

El último `snapshot.json` bueno permanece en el bucket (ADR-011: se conserva el
anterior ante pasadas vacías). Reanudar el Scheduler cuando la causa esté
corregida.
