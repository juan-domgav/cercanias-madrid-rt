# RB-010 — Histórico de retrasos (`historico/<día>.parquet`)

## Qué es
Una tabla compacta, **una fila por (día de servicio, tren, parada del horario)**,
construida a partir del archivo crudo de pasadas (`archivo/<día>/<HHMMSS>.json`,
una cada ~20 s). Vive en el bucket privado:

```
gs://cercanias-madrid-rt-privado/historico/<YYYY-MM-DD>.parquet        # ~300 KB/día, zstd
gs://cercanias-madrid-rt-privado/historico/calidad/<YYYY-MM-DD>.json   # informe de calidad del día
```

`historico/` **no tiene regla de borrado** (el archivo crudo `archivo/` se borra
a los 90 días, ver `docs/ESTADO.md`): el histórico es lo que queda cuando el crudo
ya no está. Código: `scripts/construir_historico.py` (E/S) y
`packages/domain/src/cercanias_domain/historico.py` (reglas, con tests).
El informe de calidad va también dentro del Parquet (metadato `calidad`).

### Columnas
| Columna | Qué es |
|---|---|
| `fecha_servicio`, `servicio` | Día de servicio (fecha Madrid) y su id (`1092X`). Un tren de las 00:30 es del día anterior. |
| `trip_id`, `tren`, `linea`, `familia`, `es_especial` | Id canónico (`1092X21875C7`), nº de tren, ruta (`C4a`), familia (`C4`). |
| `origen_id`, `destino_id`, `destino` | Cabecera y destino del viaje. |
| `parada_id`, `parada`, `seq`, `es_origen`, `es_destino` | Parada (id GTFS, nombre), orden en el horario. |
| `programada`, `hora`, `dia_semana` | Hora programada (GTFS, la única que publica Renfe por parada; Europe/Madrid), su hora 0-23 y día ISO 1=lunes del día de servicio. |
| `tren_visto`, `salio` | El viaje apareció en el feed / se le vio salir (alguna observación que no es marcador de cabecera). |
| `estado_medida`, `medido` | `medido` · `marcador` · `sin_confirmar` · `no_observado` (abajo). `medido` = `estado_medida == "medido"`. |
| `retraso_s` | **Retraso real en la parada** (definición abajo), en segundos. Sólo fiable si `medido`. |
| `retraso_primero_s`, `primero_marcador`, `primero_ts` | Primer `delay` visto con `next_stop` = la parada, y si era un marcador de cabecera (RB-009). Sirve para medir lo que «anunciaba» el feed. |
| `ultimo_ts`, `ultimo_estado`, `n_obs` | Observación usada para `retraso_s` y nº de observaciones con `next_stop` = la parada. |
| `llegada_obs`, `retraso_llegada_obs_s` | Primera `STOPPED_AT` en la parada (fuera de cabecera) y su diferencia con `programada`. |
| `salida_obs`, `retraso_salida_obs_s` | Primera transición `STOPPED_AT` → `IN_TRANSIT_TO` con `next_stop` aún = la parada (salida, RB-009) y su diferencia con `programada`. No en destino. Medida independiente del `delay` de Renfe, resolución ~20 s. |
| `fuente_horario` | `horarios/<svc>.json` (bucket web), `gtfs:mdb-2653-…` (GTFS archivado) o `sin_horario`. |

Trenes programados que nunca aparecen salen igual, con `tren_visto = false` y
`no_observado` en todas sus paradas: así se ven las supresiones. Trenes vistos
que no están en el horario (especiales `SPECIAL_10_*`, refuerzos) salen con las
paradas en el orden en que se vieron, `seq`/`programada` nulos y
`fuente_horario = sin_horario`.

### Definición: «retraso real en una parada» (`retraso_s`)
El feed sólo da, cada ~20 s y por tren, un `delay` y la **próxima** parada
(`next_stop`) con su estado. Por parada S:

1. Se descartan las observaciones **marcador**: tren en su origen
   (`next_stop` = origen y `STOPPED_AT`/`INCOMING_AT`). Ahí Renfe publica 0 o un
   contador de espera, no una medida (RB-009).
2. `retraso_s` = el `delay` de la **última** observación no marcador con
   `next_stop` = S. Renfe mantiene `next_stop` = S hasta ~1 min después de salir
   (`IN_TRANSIT_TO` S), así que es el retraso **a la salida** de S.
3. Es `medido` sólo si después se ve al tren camino de una parada **posterior**
   del horario (consta que pasó S). Si no (dejó de verse, hueco del archivo, tren
   parado en incidencia): `sin_confirmar`, con el valor igualmente relleno.
4. **Destino:** el `delay` de la primera `STOPPED_AT` en S (llegada; parado en
   destino Renfe sigue subiendo el retraso: 21875 C7 el 21/10 llegó +9 y a las
   00:00 marcaba +37). Si no hay `STOPPED_AT` — lo normal: Renfe retira el viaje al
   entrar — vale el último `INCOMING_AT` S, sólo si es la última observación del
   viaje y no es anterior a programada + retraso − 2 min (21/10: mediana +0,8 min).
5. Origen: `retraso_s` es el primer valor tras salir; `retraso_salida_obs_s` la
   salida medida por el cambio de estado.

Sobre los 21/10, en paradas intermedias `retraso_s` y `retraso_salida_obs_s`
difieren en mediana −10 s; 76 % a ±1 min, 90 % a ±2 min.

### Día de servicio y el desfase de medianoche
El prefijo del `tripId` es el día de servicio (`10` + índice 01..99 que envuelve
+ letra L..D; 19/10/2026 = `1090L`). Pasada la medianoche Renfe **re-etiqueta**
los trenes de la víspera con el id del día nuevo y `delay = −86400 + retraso`
(00:00 del 22/10: `1093J21875C7` −85260 = `1092X21875C7` +19 min), y por la noche
publica viajes de la madrugada siguiente con +15..18 h. Regla
(`historico.normalizar`): todo `|delay| ≥ 12 h` es ese desfase de un día; se
corrige el `delay` ±86400 y se reasigna el día (antes de las 12:00 → víspera,
p. ej. `1092X20114C3` a las 05:18 del 21/10 con −69960; después → día siguiente)
y el `tripId` canónico (`1093J…` → `1092X…`). Si no, manda la etiqueta.

### Feed congelado
El feed puede quedarse **congelado**: el 03/10 (sábado) hasta las 09:17 repitió
cada 20 s la misma foto de 28 trenes del viernes (`1073V`, mismos
`delay`/`next_stop`/posición) y luego hasta ~13:00 sólo un `SPECIAL`. Igual las
mañanas del 26/09, 28/09, 30/09 y 05/10. Una racha de
pasadas **idénticas** (≥ 5 trenes) que dura ≥ 10 min se trata como congelada:
se conserva la primera y se descartan las repeticiones (no son observaciones
nuevas; si se contaran, el último `ts` de esos trenes sería falso). Las pasadas
descartadas cuentan como hueco y salen en `congelado_detalle` /
`min_congelado_06_24` del informe; > 30 min en 06-24 h → `revisar`.

Por eso el día D se construye con `archivo/D/` **y** `archivo/D+1/` de 00:00 a
03:59. Los especiales (sin día en el id) son del día anterior hasta las 04:00.

### Horario
1. `gs://cercanias-madrid-rt-web/horarios/<svc>.json` si existe (lo que usaron los tablones).
2. Si no (todo antes del 14/10: caducaron), el GTFS de Renfe archivado por
   [Mobility Database](https://mobilitydatabase.org) (feed `mdb-2653`), listado en
   `data/mappings/gtfs-historico-mdb.json`, convertido con el mismo
   `build_schedules` del job nocturno y cacheado en `/workspace/historico/cache/`.
   Se usa el descargado ese día o el anterior más cercano que contenga el servicio.
   No hay dataset archivado del 26/09 al 02/10 ni del 05/10 y 07/10: esos días usan
   el del 25/09, 04/10 y 06/10 (`fuente_horario` lo dice).
   Comprobado: el 14/10 (`1085X`) el GTFS de MDB y `horarios/1085X.json` coinciden
   en los 1.297 viajes, paradas y horas.

### Calidad (`historico/calidad/<día>.json`)
Pasadas del día, primera/última, huecos >5 min (todos y en 06-24 h, con detalle),
minutos de feed congelado, trenes programados / vistos / programados vistos, fuera de horario, filas por
estado, `% medido` (sobre paradas con horario de trenes vistos), p50/p90 del
retraso medido y `estado`: `bueno` o `revisar` con `motivos`. Umbrales (en el
script): día parcial (primera pasada >06:00 o última <23:30), <3.500 pasadas,
>30 min de huecos o de feed congelado en 06-24 h, servicio mayoritario del feed ≠ esperado, sin
horario, <85 % de trenes programados vistos, <60 % medido. `revisar` no excluye
el día: avisa de que hay que filtrar (por hora, por `medido`).

Informe del backfill inicial: [`docs/informes/historico-calidad-2026-09-08_2026-10-22.md`](../informes/historico-calidad-2026-09-08_2026-10-22.md).

## Reconstruir un día
```bash
cd /workspace/cercanias-madrid-rt        # o el worktree con la rama
export PATH=/home/box/google-cloud-sdk/bin:$PATH
uv run --group historico python scripts/construir_historico.py 2026-10-22 --forzar
```
Sobrescribe `historico/2026-10-22.parquet` y `historico/calidad/2026-10-22.json`
(idempotente) **si se pasa `--forzar`**: sin él, los días que ya tienen
`historico/<día>.parquet` se saltan (así un backfill interrumpido se reanuda
relanzando el mismo comando). ~2-3 min por día (la descarga de ~5.000 ficheros manda). Sólo
admite días completos (anteriores a hoy). `--local DIR` escribe en local y no
sube nada. `--tmp DIR` para el temporal (se borra al acabar cada día).

**Incremental diario** (desde el box, p. ej. cron a las 05:00 Europe/Madrid, cuando
ya no quedan trenes de ayer en el feed):
```bash
cd /workspace/cercanias-madrid-rt && PATH=/home/box/google-cloud-sdk/bin:$PATH \
  uv run --group historico python scripts/construir_historico.py ayer
```
Sale con código ≠ 0 si un día falla.

## Backfill
```bash
nohup uv run --group historico python scripts/construir_historico.py 2026-09-08..2026-10-22 -j 4 \
  > /tmp/backfill-historico.log 2>&1 &
```
Reanudable: si se corta, relanzar el mismo comando (salta los días ya subidos).
Un día a medias no se sube (el Parquet y el informe se suben al final).
`-j` = días en paralelo (procesos). Imprime una línea JSON por día con estado,
filas y motivos. 45 días ≈ 25-60 min con `-j 4` según la red. `gcloud storage cp`
a veces se cuelga: cada llamada tiene límite de 7 min y 3 intentos (sale
`gcloud colgado … intento N` en el log).

## Verificar
```bash
gcloud storage ls -l gs://cercanias-madrid-rt-privado/historico/ gs://cercanias-madrid-rt-privado/historico/calidad/
gcloud storage cat gs://cercanias-madrid-rt-privado/historico/calidad/2026-10-22.json
gcloud storage cp gs://cercanias-madrid-rt-privado/historico/2026-10-22.parquet /tmp/
uv run --group historico python -c "
import pyarrow.parquet as pq, pyarrow.compute as pc
t = pq.read_table('/tmp/2026-10-22.parquet')
print(t.num_rows, pc.value_counts(t['estado_medida']))"
```
Un día normal: ~4.300 pasadas, ~1.300 trenes, ~20.000 filas, >99 % de trenes
programados vistos, ~85-90 % medido, `estado: bueno`.

## Rollback
Sólo hay que borrar lo que escribió el script, nunca `archivo/`:
```bash
gcloud storage rm gs://cercanias-madrid-rt-privado/historico/2026-10-22.parquet \
                  gs://cercanias-madrid-rt-privado/historico/calidad/2026-10-22.json
```
Se puede regenerar mientras el crudo exista (90 días).

## Límites conocidos
- Los valores son los del feed de Renfe; `retraso_s` hereda sus saltos (p. ej. un
  +19 aislado entre dos +10). `retraso_salida_obs_s` es la medida independiente.
- El horario es el GTFS planificado: modificaciones de servicio de última hora
  no están; un tren programado no visto puede ser una supresión o un tren sin GPS.
- `medido` exige ver al tren después: trenes que desaparecen a mitad (incidencia,
  hueco del archivo) dejan esas paradas en `sin_confirmar`.
- El informe lleva `version` (2 = con filtro de feed congelado). Los días
  construidos antes (sin `version`) se pueden rehacer con `--forzar`.
- El código en vivo (worker) aún no corrige el desfase −86400 de medianoche.
