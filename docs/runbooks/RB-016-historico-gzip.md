# RB-016 — Histórico nocturno falla con `'utf-8' codec can't decode byte 0x8b` (objetos con gzip)

## Síntoma
- El incremental diario de `scripts/construir_historico.py` (RB-010) sale con
  código ≠ 0 y una línea como:
  ```
  2026-11-02 ERROR 'utf-8' codec can't decode byte 0x8b in position 1: invalid start byte
  ```
- Falta `gs://cercanias-madrid-rt-privado/historico/<día>.parquet` (y
  `historico/calidad/<día>.json`) para ese día; los anteriores están.

Visto el 2026-11-02 (primer día cuyo `horarios/<svc>.json` se publicó ya
comprimido). El 2026-11-01 se construyó bien porque el build corrió (04:39 Madrid)
antes de que el job nocturno de horarios reescribiera `horarios/` con gzip
(05:19 Madrid del 02/11).

## Causa
Desde el PR #20 (merge `7a5bff5`, 2026-11-01) `horarios/*.json`, `snapshot.json`,
`boards.json` y `ops/inbox.json` se guardan con `Content-Encoding: gzip`.
`gcloud storage cat` entrega el objeto **tal cual está guardado** (comprimido, sin
transcodificar), y el script leía la salida de `gcloud` como texto UTF-8:
`0x8b` es el segundo byte de la cabecera gzip (`1f 8b`). Falla al cargar el
horario, antes de tocar el archivo.

`archivo/<día>/*.json` y `archivo/alerts/` **siguen en claro** (comprobado el
03/11 en 2026-11-01, -02 y -03: ningún objeto con `Content-Encoding`), pero el
script ya tolera ambos formatos por si se comprimen en el futuro.

## Checks
```bash
export PATH=/home/box/google-cloud-sdk/bin:$PATH
DIA=2026-11-02
# 1. ¿Falta el día en historico/?
gcloud storage ls "gs://cercanias-madrid-rt-privado/historico/$DIA.parquet"   # "matched no objects" = falta
# 2. Servicio del día y si su horario está comprimido (content_encoding: gzip)
SVC=$(uv run python -c "from datetime import date; from cercanias_domain.historico import servicio_madrid; print(servicio_madrid(date.fromisoformat('$DIA')))")
gcloud storage objects describe "gs://cercanias-madrid-rt-web/horarios/$SVC.json" --format="yaml(content_encoding,update_time)"
gcloud storage cat "gs://cercanias-madrid-rt-web/horarios/$SVC.json" | head -c 2 | od -An -tx1   # " 1f 8b" = gzip crudo
# 3. ¿El archivo del día está completo? (~4.300 objetos) y en qué formato
gcloud storage objects list "gs://cercanias-madrid-rt-privado/archivo/$DIA/*" --format="value(content_encoding)" | sort | uniq -c
# 4. ¿El checkout desde el que corre el cron tiene el arreglo?
grep -c "def json_guardado" scripts/construir_historico.py   # 1 = sí; 0 = no
```
Es este runbook si 1 falta, 2 da `gzip`/`1f 8b` y 4 da 0. Si 4 da 1 y aun así
falla, el error es otro: mirar el mensaje completo (RB-010).

## Fix
PR «fix(historico): leer JSON guardado con o sin gzip (RB-016)»: un único helper
`json_guardado(bytes)` en el script (sobre `gunzip_if_compressed` de
`cercanias_storage.publishers`, el mismo que usa `GcsSnapshotPublisher.read`):
si los bytes empiezan por `1f 8b` se descomprimen una vez; si no, se usan tal cual.
Lo usan el horario (`gcloud storage cat`, ahora en binario con `_gcloud_binario`),
las pasadas de `archivo/` y las fotos de `archivo/alerts/`. Da igual que el objeto
llegue crudo o ya descomprimido por la transcodificación de GCS (cliente Python,
`gcloud storage cp`): nunca descomprime dos veces. Un fichero de archivo roto
(JSON inválido, gzip truncado o corrupto, bytes no UTF-8) se salta como antes, sin
tumbar el día.

Tras mergear, actualizar el checkout desde el que corre el incremental diario
(`git pull` en `main`) **antes** de las 05:00, o el día siguiente fallará igual.

## Backfill del día que falta
El script es idempotente: sólo escribe `historico/<día>.parquet` y
`historico/calidad/<día>.json`, nunca `archivo/`. Desde un checkout con el arreglo:
```bash
cd /workspace/cercanias-madrid-rt        # en main con el arreglo, o el worktree de la rama
export PATH=/home/box/google-cloud-sdk/bin:$PATH
uv run --group historico python scripts/construir_historico.py 2026-11-02
```
Sin `--forzar` basta (el día no existe); con `--forzar` lo sobrescribe si ya se
hubiera subido. Si fallaron varios días seguidos: `2026-11-02..2026-11-03`
(sólo días completos, anteriores a hoy). ~2 min por día.

Ensayo sin escribir en el bucket (sólo lee; escribe en local):
```bash
uv run --group historico python scripts/construir_historico.py 2026-11-02 \
  --local /workspace/historico-ensayo --tmp /workspace/historico-ensayo/tmp
```
Resultado del ensayo del 03/11 para 2026-11-02 (día de huelga): `estado: bueno`,
4.325 pasadas, 16.049 filas, 1.013/1.038 trenes programados vistos (97,6 %),
76,9 % medido, 170 trenes suprimidos, p50 retraso medido 300 s.

## Verificar
```bash
DIA=2026-11-02
gcloud storage ls -l "gs://cercanias-madrid-rt-privado/historico/$DIA.parquet" \
                     "gs://cercanias-madrid-rt-privado/historico/calidad/$DIA.json"
gcloud storage cat "gs://cercanias-madrid-rt-privado/historico/calidad/$DIA.json" \
  | jq '{estado, motivos, pasadas, filas, pct_programados_vistos, pct_medido_trenes_vistos, trenes_cancelados, fuente_horario}'
gcloud storage cp "gs://cercanias-madrid-rt-privado/historico/$DIA.parquet" /tmp/
uv run --group historico python -c "
import pyarrow.parquet as pq
t = pq.read_table('/tmp/$DIA.parquet'); print(t.num_rows)"
```
Esperado: el Parquet y el JSON de calidad existen, `num_rows` = `filas` del JSON
(~16.000 en un día laborable de noviembre; 16.049 el 02/11), `fuente_horario` =
`horarios/<svc>.json` (no `gtfs:…` ni nulo) y `pasadas` ~4.300. Un día de huelga
puede salir con más suprimidos y menos `% medido` de lo normal: no es este fallo.

## Rollback
- Del backfill: borrar sólo lo que escribió el script (nunca `archivo/`):
  ```bash
  gcloud storage rm gs://cercanias-madrid-rt-privado/historico/2026-11-02.parquet \
                    gs://cercanias-madrid-rt-privado/historico/calidad/2026-11-02.json
  ```
- Del código: revertir el commit del PR. El script vuelve a fallar con los
  horarios comprimidos; como alternativa temporal, republicar `horarios/` sin gzip
  revirtiendo `gzip=True` en `regenerate_schedules.py` (PR #20) y relanzando el job
  de horarios (RB-003).
