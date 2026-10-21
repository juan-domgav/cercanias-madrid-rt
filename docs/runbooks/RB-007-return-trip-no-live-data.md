# RB-007 — Vuelta en cabecera «sin datos en vivo» con el tren que llega localizado

## Sintoma
En una estación de paso (p. ej. Tres Cantos, C4b) un sentido muestra retraso y el
otro «sin datos en vivo · aún no localizado» en casi todos los trenes. Observado
el 21/10/2026 15:26: hacia Colmenar Viejo 20479/20483/20485 con dato; hacia
Parla sólo 20474, y 20476/20478/20480/20482/20484 sin datos.

## Causa
**Renfe no publica un viaje en el feed GTFS-RT hasta ~30 min antes de su salida
de cabecera** (archivo del 21/10, n=850 viajes: p10 29 min, mediana 30, p90 30).
No es un fallo nuestro: a las 15:28 el feed traía 7 de los 11 trenes (20474,
20476, 20479, 20481, 20483, 20485, 20487) y el snapshot los traía todos; los 4
ausentes (20478, 20480, 20482, 20484) salen de Colmenar a ≥16:12. Desde Tres
Cantos, la vuelta de Colmenar está a 6 min de su origen, así que el hueco
«sin datos» es de ~25 min para un sentido y casi nulo para el otro.

## Mitigación (este cambio)
Si el viaje aún no está en el feed pero su **enlace entrante** sí, se estima:

    estimado = max(0, retraso_entrante - (maniobra_programada - 3 min))

- `MIN_PHYSICAL_TURNAROUND_MINUTES = 3`: minutos que una vuelta no comprime.
  Un adelanto nunca se propaga (`max(0, …)`).
- Se aplica en todas las paradas del viaje (no sólo en cabecera).
- `boards.json`: `delay: null`, `live: false`, `estimated_delay: N`,
  `delay_source: "inbound_estimate"`, `eta` = hora + estimado. Web y bot:
  «≈ +N min (estimado)» / «≈ en hora (estimado)», nunca con estilo de dato vivo.
- En cuanto el viaje aparece en el feed, su dato manda (`delay_source: "live"`).
- No se estima: con snapshot `degraded` o ≥ `STALE_WARNING_SECONDS` (guard de
  PR #7), con el entrante suprimido o sin retraso publicado, sin maniobra
  conocida, con enlace ambiguo (`amb: true`) ni en líneas excluidas.

### Etiqueta «Renfe aún no lo publica» (`realtime_status`)
Cada salida lleva `realtime_status`, por orden de precedencia:
`live` (está en el feed) > `inbound_estimate` (C1 por defecto) >
`not_published` > `no_data`.

- `not_published`: sin dato ni estimado y su salida de **origen** (`seq 1`,
  no la hora en esta estación) a **más** de `PUBLISH_WINDOW_MINUTES` (30 por
  defecto; variable de `cercanias-ingesta`). A 30 min exactos ya es `no_data`.
  Web y bot: «programado · Renfe aún no lo publica», en estilo neutro.
- `no_data` dentro de la ventana es la anomalía de verdad: «sin datos en vivo».
- Con el snapshot `degraded` o viejo no se afirma `not_published` (todo
  queda `no_data`, como antes).
- Salud: `health.departures_not_published` y
  `health.departures_missing_in_window` (salidas `no_data` con snapshot
  fresco; 0 con snapshot viejo). Esta última es la señal útil para el vigía.

### Emparejamiento (`regenerate_schedules._link_turnarounds`)
1. `block_id` del GTFS **cuando agrupa ≥2 viajes**. Renfe lo rellena con el
   número de tren (1207 bloques el 21/10), pero 84 bloques sí encadenan viajes:
   pasantes C2↔C8a/C8b por Chamartín y C8a→C10 en El Escorial (90 enlaces).
2. Si no, inferencia: fin del entrante = origen del saliente, salida 0–40 min
   después de la llegada, la llegada libre más cercana, 1:1.
3. Familia de línea igual (C4 = C4/C4a/C4b), salvo interlineado validado en
   `INTERLINE_TERMINI` (hoy: Cercedilla C8↔C9). En nudos como Chamartín cruzar
   familias asignaba 280 vueltas que el feed no respalda (precisión 37 %).
4. **Ambiguo** (`amb: true` en `horarios/<día>.json`): otra llegada u otra
   salida compatible a ≤2 min (`AMBIGUITY_TIE_MINUTES`). Se publica el enlace,
   pero no se estima desde él.

### Cobertura y validación por línea (día 1092X, archivo del 21/10/2026)
Precisión = la vuelta salió con un retraso a ±3 min del estimado, sobre enlaces
con entrante y saliente observados. Criterio para estimar: ≥90 % con n ≥ 30.

| Línea | Viajes | Enlazados | Ambiguos | Sin enlace | Precisión ±3 min (n) | Estimado ≥3 min se cumple (n) | ¿Se estima? |
|---|---|---|---|---|---|---|---|
| C1 | 144 | 140 (97 %) | 0 | 4 | 91 % (74) | 73 % (15) | sí |
| C2 | 164 | 127 (77 %) | 4 | 37 | 38 % (66) | 42 % (45) | no (excluida) |
| C3 | 118 | 96 (81 %) | 0 | 22 | 61 % (51) | 52 % (25) | no (excluida) |
| C4 | 13 | 6 (46 %) | 0 | 7 | 50 % (2) | 100 % (2) | no (excluida) |
| C4a | 124 | 110 (89 %) | 0 | 14 | 36 % (61) | 30 % (44) | no (excluida) |
| C4b | 122 | 112 (92 %) | 0 | 10 | 47 % (55) | 30 % (33) | no (excluida) |
| C5 | 293 | 255 (87 %) | 0 | 38 | 69 % (145) | 54 % (93) | no (excluida) |
| C7 | 74 | 62 (84 %) | 0 | 12 | 31 % (32) | 35 % (26) | no (excluida) |
| C8a | 49 | 37 (76 %) | 0 | 12 | 90 % (20) | 83 % (12) | no (excluida) |
| C8b | 42 | 39 (93 %) | 0 | 3 | 80 % (20) | 82 % (17) | no (excluida) |
| C9 | 10 | 9 (90 %) | 0 | 1 | — (0) | — (0) | no (excluida) |
| C10 | 144 | 131 (91 %) | 0 | 13 | 42 % (69) | 52 % (42) | no (excluida) |
| **Total** | 1297 | 1124 (87 %) | 4 | 173 | 58 % (595) | 49 % (354) | |

**Conclusión:** sólo C1 pasa. En C4 (el caso de Tres Cantos) Renfe cambia
material en cabecera: con el entrante +15, la vuelta suele salir en hora con
otra unidad. Por eso el valor por defecto de
`INBOUND_ESTIMATE_EXCLUDED_ROUTES` es `C2,C3,C4,C5,C7,C8,C9,C10`, y en C4b
se mostrará «programado · Renfe aún no lo publica» hasta ~30 min antes de
la salida de cabecera. Probado aparte: elegir la llegada MÁS ANTIGUA (FIFO) sube el total a
67 % (C5 89 %, C4b 58 %): no basta, queda como línea de trabajo.

## Comprobaciones
1. ¿El tren está en el feed?
   `curl -s https://gtfsrt.renfe.com/trip_updates.json | jq '.entity[].tripUpdate.trip.tripId' | grep <tren>`
   Si no está y su salida de cabecera es en >30 min, es el caso normal.
2. `boards.json` → la salida: `delay_source`, `estimated_delay`.
3. `boards.json` → `health.departures_estimated_from_inbound` (salidas, no trenes)
   y `health.inbound_estimates_disabled_stale`.
4. `horarios/<día>.json` → la parada: `from`, `slack`, `amb`.
5. `realtime_status` de la salida y `health.departures_missing_in_window`. Un
   valor alto y sostenido con snapshot fresco = Renfe ha cambiado su ventana:
   medirla de nuevo con el archivo y ajustar `PUBLISH_WINDOW_MINUTES`.

## Arreglo / ajuste
- Activar o quitar líneas: variable `INBOUND_ESTIMATE_EXCLUDED_ROUTES` del Cloud Run job
  `cercanias-ingesta` (familias separadas por comas; vacía = todas). Revalidar
  antes con el archivo (`gs://cercanias-madrid-rt-privado/archivo/<día>/`).
- Cambios de emparejamiento (`INTERLINE_TERMINI`, `AMBIGUITY_TIE_MINUTES`)
  requieren regenerar horarios (job `cercanias-horarios`, ver DESPLIEGUE.md).

## Verificación
- `uv run pytest packages/domain/tests/test_inbound_estimate.py apps/worker/tests/test_cloud_tick_vuelta.py apps/worker/tests/test_regenerate_schedules.py`
- En producción, con C1 activa: `health.departures_estimated_from_inbound > 0`
  en hora punta y `0` con el snapshot degradado.

## Rollback
Sin redeploy: `INBOUND_ESTIMATE_EXCLUDED_ROUTES=C1,C2,C3,C4,C5,C7,C8,C9,C10`
en `cercanias-ingesta` apaga toda estimación. Completo: revertir el PR y redeplegar la imagen de
`cercanias-ingesta`/`cercanias-horarios` y la API; los horarios con `amb` son compatibles hacia atrás (se ignora).
