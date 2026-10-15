# Ops runbooks — Cercanías Madrid RT

Runbooks cortos para incidentes operativos. Proyecto GCP: `cercanias-en-hora`.
Bucket web: `gs://cercanias-madrid-rt-web`.

| Runbook | Propósito |
|---|---|
| [empty-boards-missing-horarios.md](./empty-boards-missing-horarios.md) | Tablones vacíos porque falta `horarios/<servicio>.json` del día en curso |
| [stale-snapshot.md](./stale-snapshot.md) | `snapshot.json` viejo o marcado `degraded` (ingesta / Scheduler) |
| [nightly-horarios-failed.md](./nightly-horarios-failed.md) | Fallo del job nocturno `cercanias-horarios` o cobertura &lt; 25 días |

Referencias: `docs/DESPLIEGUE.md`, `Makefile` (`regenerate-schedules`), `apps/worker/src/cercanias_worker/cloud_tick.py`.
