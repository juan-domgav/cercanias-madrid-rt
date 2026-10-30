# Runbooks operativos

Índice sintoma → fichero. Cada runbook es ejecutable: comprobaciones, arreglo,
verificación y rollback. Los jobs viven en el proyecto `cercanias-en-hora`.

| Sintoma | Runbook |
|---|---|
| `boards.json` con 0 estaciones/salidas y `snapshot.json` sano | [RB-001](RB-001-empty-boards-missing-horarios.md) |
| Snapshot viejo (>5 min) o `degraded: true` | [RB-002](RB-002-stale-snapshot.md) |
| Job nocturno `cercanias-horarios` fallido o <25 días | [RB-003](RB-003-nightly-horarios-failed.md) |
| `health.schedule_days_left` bajo en falso al cruzar 1099→1001 | [RB-004](RB-004-wraparound-days-left.md) |
| El bot de ops de Telegram no responde o responde en bucle | [RB-005](RB-005-telegram-ops-bot.md) |
| Tren `SPECIAL_10_*` en `snapshot.json` pero ausente de `boards.json` | [RB-006](RB-006-special-trains-missing.md) |
| Un sentido «sin datos en vivo» con el tren que llega a cabecera localizado | [RB-007](RB-007-return-trip-no-live-data.md) |
| Bot o web dicen «sin confirmar» / «viene con +N» con el tren en vivo y en hora | [RB-008](RB-008-bot-boards-inconsistent.md) |
| Salidas de cabecera «en hora» mientras los que llegan van tarde | [RB-009](RB-009-origin-delay-placeholder.md) |
| Reconstruir, rellenar o verificar el histórico de retrasos (`historico/<día>.parquet`) | [RB-010](RB-010-historico.md) |
| `degraded: true` / «datos conservados» con `generated_at` de segundos (última pasada de Renfe vacía) | [RB-011](RB-011-degraded-with-fresh-data.md) |
| Falta «previsto +N min» en C1/C5, `health.prediction.status: error` o alerta `prediction_*`; excluir un día anómalo (huelga) del previsto | [RB-013](RB-013-previsto-prediction.md) |
| Encender/verificar el archivo de supresiones, paradas omitidas y avisos; WARNING `alerts_feed_stale` | [RB-014](RB-014-archivo-incidencias.md) |
| `health.delay_coverage` crítico con snapshot fresco y sin `degraded`; los viajes sin retraso son `CANCELED` | [RB-015](RB-015-delay-coverage-cancelados.md) |

Detalle de despliegue: [`docs/DESPLIEGUE.md`](../DESPLIEGUE.md).
