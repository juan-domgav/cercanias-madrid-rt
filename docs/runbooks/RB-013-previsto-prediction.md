# RB-013 — «previsto +N min»: retraso previsto por el histórico (C1 y C5)

## Qué es
Cuando Renfe **aún no publica en vivo** un tren (no está en el feed, no hay
estimado por el entrante, no está suprimido ni con aviso del entrante), el
tablón y el bot pueden mostrar el retraso **previsto**: la mediana del retraso
de *ese tren en esa estación* en los días recientes del mismo tipo (laborable o
sábado/domingo/festivo).

- Etiqueta: `previsto +3 min`; mediana 0 → `previsto puntual`. **Nunca** `en hora`.
- Aviso: `📊 previsto por su histórico; Renfe aún no lo publica en vivo`; detalle
  en la web: «Previsión, no dato en vivo: es la mediana del retraso de este tren
  en esta estación en N días laborables recientes. Cuando Renfe lo publique en
  vivo, manda su dato.»
- Bot: si en la respuesta hay alguna previsión, el pie dice
  «Andén y retraso en vivo vienen del feed de Renfe. «previsto» = previsión por
  el histórico de ese tren, no dato en vivo.»
- **No** cambia `eta`, `in`, orden ni `realtime_status`: sólo etiqueta y aviso.
  El dato en vivo y el estimado por entrante (RB-007) siempre mandan.
- Tablón y bot salen de la MISMA `departure_status` (RB-008): `boards.json` lleva
  `predicted_delay`, `predicted_samples`, `predicted_day_type`,
  `delay_source: "predicted"` y `display`.
- Sólo con snapshot fresco y sólo en las líneas de `PREVISTO_ROUTES` (vacío por
  defecto = desactivado). Las demás líneas no pasan el listón (ver abajo).

Config (env del job `cercanias-ingesta`):

| Variable | Defecto | Qué hace |
|---|---|---|
| `PREVISTO_ROUTES` | `""` (apagado) | familias permitidas, p. ej. `C1,C5` |
| `PREVISTO_KEY` | `prevision/previsto.json` | objeto en el bucket **privado** (`PRIVATE_BUCKET`) |
| `PREVISTO_MAX_AGE_DAYS` | `21` | artefacto más viejo → ERROR y sin previsión |
| `PREVISTO_FESTIVOS` | `""` | festivos extra (AAAA-MM-DD,…) además de los del artefacto |

El artefacto se lee en cada tick (un GET pequeño, ~0,5 MB, al bucket privado).

## Síntoma
- Falta «previsto» donde se espera: `boards.json` → `health.prediction.status`
  ≠ `ok`, o `health.departures_predicted` = 0 con C1/C5 sin datos en vivo.
- Logs `ERROR … prevision desactivada … (RB-013)` con `alert` =
  `prediction_artifact_missing` | `_invalid` | `_stale` | `_unreadable`, o
  `WARNING prediction_routes_missing` / `prediction_bad_holiday`.
- Un rider dice que «previsto» no se parece a la realidad.

Fallo seguro: con el artefacto ausente, corrupto o caducado **no se muestra
ninguna previsión y nada más cambia**; el tablón sigue como sin RB-013.

## Checks
```bash
WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
curl -s "$WEB/boards.json?t=$(date +%s)" | jq '.health | {prediction, departures_predicted}'
# Filas previstas de una estación (Atocha 18000):
curl -s "$WEB/boards.json?t=$(date +%s)" | jq '.stations["18000"].departures[]
  | select(.predicted_delay != null) | {t, route, train, predicted_delay, predicted_samples, display}'
gcloud logging read 'resource.labels.job_name="cercanias-ingesta" jsonPayload.alert=~"^prediction_"' \
  --project cercanias-en-hora --limit 10 --format='value(timestamp,jsonPayload.alert,jsonPayload.message)'
# Artefacto (sólo lectura):
gcloud storage ls -l gs://cercanias-madrid-rt-privado/prevision/previsto.json
gcloud storage cat gs://cercanias-madrid-rt-privado/prevision/previsto.json \
  | jq '{schema, generated_at, lines, history, min_observations}'
# El job debe tener PRIVATE_BUCKET; si no, el artefacto se buscaría en el bucket web.
gcloud run jobs describe cercanias-ingesta --region europe-southwest1 --format=yaml | grep -A1 -E 'PRIVATE_BUCKET|PREVISTO_'
```

| `health.prediction.status` | Significa | Qué hacer |
|---|---|---|
| `disabled` | `PREVISTO_ROUTES` vacío | nada (o habilitar) |
| `ok` | cargado; `applied` false si el snapshot no es fresco | si no es fresco: RB-002 |
| `error` + `prediction_artifact_missing` | no hay objeto en `PREVISTO_KEY` | regenerar y subir |
| `error` + `prediction_artifact_invalid` | JSON o esquema inválido (motivo en `error` del log) | regenerar y subir |
| `error` + `prediction_artifact_stale` | `generated_at` > `PREVISTO_MAX_AGE_DAYS` | regenerar y subir |
| `error` + `prediction_artifact_unreadable` | fallo leyendo GCS | permisos de la SA / RB-002 |
| `ok` + `routes_missing_in_artifact` | una línea permitida no está en el artefacto | regenerar con `--lineas` |

## Generar el artefacto (offline; el repo es público: NO se commitea)
Necesita el histórico de RB-010 (PR #10).
```bash
uv run --with pyarrow python scripts/construir_previsto.py --origen gs \
  --desde <hoy-42d> --hasta <ayer> --lineas C1,C5 --out /tmp/previsto.json
# Comprobar contra la última semana (entrena con lo anterior):
cd scripts && uv run --with pyarrow python verificar_previsto.py --origen <dir con los parquet> \
  --entreno <desde>..<hace 8d> --prueba <hace 7d>..<ayer>
```
Listón para habilitar una línea: ≥14 días de histórico con ≥4 de fin de
semana/festivo, mejora de MAE ≥30 % frente a «en hora» y ≥60 % de salidas a
±2 min. Semana 19-25/10/2026: C1 1,65 min / 79,8 % (en hora: 3,03 / 54,6 %),
C5 3,14 / 62,1 % (5,70 / 31,0 %). Ver `docs/informes/previsto-holdout-2026-10-19.json`.
Festivos: los de la Comunidad de Madrid (2026) van en el script; los locales de
Madrid capital (09/11, 15/05) no, se añaden con `--festivos` o `PREVISTO_FESTIVOS`.

## Habilitar (con OK de Juan; línea a línea)
```bash
gcloud storage cp /tmp/previsto.json gs://cercanias-madrid-rt-privado/prevision/previsto.json
gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
  --update-env-vars PREVISTO_ROUTES=C1          # primero C1; C5 tras verificar
```
Requiere una imagen con este cambio desplegada (DESPLIEGUE.md). Regenerar el
artefacto al menos cada 2 semanas (caduca a los 21 días).

## Verify
- `health.prediction.status == "ok"`, `routes` = las habilitadas, `applied: true`,
  `day_type` correcto para hoy, `departures_predicted` > 0 en hora punta.
- Una fila C1 sin vivo muestra `previsto +N min` en web y `/proximos` del bot,
  con el mismo texto; ninguna previsión dice `en hora`.
- Una fila con dato en vivo NO lleva aviso 📊 (manda el vivo).
- Ninguna línea fuera de `PREVISTO_ROUTES` tiene `predicted_delay`.

## Desactivar / rollback
```bash
gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
  --update-env-vars PREVISTO_ROUTES=
```
Efecto en el siguiente tick (≤1 min): sin previsiones, todo lo demás igual.
Alternativa sin tocar el job: borrar o renombrar el artefacto → ERROR
`prediction_artifact_missing` y sin previsión (útil, pero alerta en logs).
Rollback de código: imagen anterior (ver `docs/ESTADO.md`).
