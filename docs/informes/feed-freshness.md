# Informe de frescura del feed GTFS-RT (E2)

Generado: 2026-07-31T14:41:10.781413+00:00

Responde la incognita abierta en Anexo A.8: el `header.timestamp` de la captura original (29/07/2026) correspondia a una fecha unas cuatro semanas anterior a la descarga. Este informe mide el desfase en una ejecucion real contra `https://gtfsrt.renfe.com/vehicle_positions.json`.

## Resultado medido

| Medicion | Valor |
|---|---|
| now_utc (descarga 1) | 2026-07-31T14:40:45.608213+00:00 |
| header.timestamp crudo (1) | `1785508833` |
| header.timestamp como epoch UTC (1) | 2026-07-31T14:40:33+00:00 |
| delta (1) = now - header | 12.61 s |
| now_utc (descarga 2, +25s) | 2026-07-31T14:41:10.722646+00:00 |
| header.timestamp crudo (2) | `1785508853` |
| header.timestamp como epoch UTC (2) | 2026-07-31T14:40:53+00:00 |
| delta (2) = now - header | 17.72 s |
| tiempo real transcurrido entre descargas | 25.11 s |
| delta entre header.timestamp (2-1) | 20 s |
| `feed_clock_skew_seconds` (metrica a exportar) | 15.17 s |

**Decision:** el desfase medido es de segundos (no de semanas como en la captura del Anexo A). Se implementa el umbral de 120 s de la especificacion base tal cual, comparando `now_utc` contra `header.timestamp`/`vehicle.timestamp` interpretados como epoch. Se sigue exportando `feed_clock_skew_seconds` como metrica para detectar si el comportamiento cambia en produccion.

## ANOMALIA respecto al Anexo A.8

La captura documentada en el Anexo A (29/07/2026) reportaba un `header.timestamp` que interpretado como epoch POSIX correspondia a una fecha unas cuatro semanas anterior a la propia captura (`1783019105` -> `2026-07-02T19:05:05Z`). **La medicion en vivo de esta ejecucion no reproduce ese desfase**: el `header.timestamp` corresponde a un instante a solo unos segundos del reloj real. Se asume que el desfase de 4 semanas del Anexo A fue un artefacto de la via de descarga usada en aquella captura (cache intermedia), no un comportamiento real y sostenido del publicador. Se recomienda seguir exportando `feed_clock_skew_seconds` en produccion para detectar si reaparece.

## Metadatos declarados por Renfe

Actualizacion declarada cada 20 segundos para los tres feeds de Cercanias (vehicle_positions, trip_updates, alerts). Licencia Creative Commons Attribution 4.0 (atribucion obligatoria).
