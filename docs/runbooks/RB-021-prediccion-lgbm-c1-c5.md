# RB-021 — Retraso PREDICHO por el modelo (LightGBM) en C1 y C5

**Apagado por defecto.** Con `PREDICCION_ROUTES` vacía (el valor por defecto)
la ingesta no lee el artefacto, no escribe nada nuevo y `boards.json` sale
exactamente igual que sin este cambio. Encender es decisión de Juan, línea a
línea.

## Qué ve el viajero
Sólo en C1 y C5 y sólo en salidas **sin retraso propio medido** y sin nada del
entrante: el tren no está en el feed (Renfe lo publica ~30 min antes) o está
en el feed pero **aún en su andén de origen antes de su hora** (RB-009).

| Caso | Etiqueta | Aviso |
|---|---|---|
| fuera del feed, modelo +3 | `previsto +3 min` | 📊 previsto por el modelo de la línea; Renfe aún no da su retraso |
| aún en cabecera, modelo +3 | `previsto +3 min · aún sin salir` | 📊 (igual) |
| modelo 0 o negativo | `previsto sin retraso` (nunca «en hora») | 📊 |
| tren en vivo / estimado por el entrante (RB-007) / aviso o supresión del entrante | lo de siempre | sin 📊: manda lo otro |

La previsión **no mueve** `eta`, `in` ni el orden, y `realtime_status` no
cambia: sólo `delay_source: "predicted"`, `predicted_delay` y `display`. Web y
bot salen de la misma `departure_status` (RB-008).

## Cómo funciona
- `cercanias_domain.prediccion` (Python puro, sin numpy ni lightgbm en la
  imagen): evalúa los árboles exportados, calcula los rasgos y decide.
  - **C1, `estado_linea`**: histórico del tren + cómo van ahora los demás C1
    (media del retraso y del residuo sobre su mediana en la última hora).
  - **C5, `entrante_vivo`**: histórico del tren + último retraso visto del tren
    que llega a cabecera y da la vuelta (emparejamiento del backtest: primera
    salida en sentido contrario 5–90 min después).
  - Sin entrada en vivo (C1 sin observaciones de la línea, C5 sin dato del
    entrante) **no predice**: la fila queda como hoy.
  - Antelación máxima por línea (minutos hasta la salida del viaje en su
    origen), fijada por el holdout: la mayor a la que la línea pasa el listón
    (>= 30 % menos MAE que «en hora» y >= 60 % a ±2 min). Artefacto actual:
    **C1 30 min, C5 60 min**.
- `cloud_tick` (sólo con la lista no vacía y snapshot fresco):
  - Lee **`prediccion/modelo.json`** del bucket **privado** (≈1,7 MB gzip;
    ~0,2 s en cargarlo y validarlo; el tick tarda ~0,6 s más con C1 y C5).
  - Guarda en `prediccion/observaciones.json` (privado) el último retraso de
    cada (viaje, parada) de C1/C5 de las últimas 3 h: es la entrada en vivo del
    modelo. Una escritura pequeña por tick. Nunca especiales, suprimidos ni
    trenes aún en su andén de origen.
  - Artefacto ausente, ilegible, inválido o con más de 21 días = sin
    previsión y un `ERROR` estructurado (`alert=prediction_artifact_missing` /
    `_invalid` / `_expired`). Un fallo inesperado = `alert=prediction_failed`.
    Los tablones salen siempre.
  - Salud: `health.prediction` (`routes`, `status`, `artifact_version`,
    `observations`) y `health.departures_predicted`.

## Reentrenar (semanal, después del backtest; sin desplegar nada)
Días excluidos (huelgas, feed roto) en
`packages/domain/src/cercanias_domain/previsto_dias_excluidos.json` (el mismo
fichero y formato que PR #14): ni se leen. El histórico no se toca.

```bash
# 1. Histórico a local (SOLO lectura del bucket privado)
mkdir -p /tmp/historico && gcloud storage cp \
  'gs://cercanias-madrid-rt-privado/historico/2026-*.parquet' /tmp/historico/
# 2. Entrenar: holdout de la última semana (fija la antelación de cada línea),
#    paridad de árboles, desfase de rasgos y modelo final con todos los días.
uv run --with lightgbm --with pandas --with pyarrow python scripts/entrenar_prediccion.py \
  --historico /tmp/historico --desde 2026-09-08 --hasta <ayer> \
  --prueba-desde <ayer-8> --out /tmp/prediccion
```
Salida: `modelo-<versión>.json.gz` e `informe-<versión>.json`. Revisar en el
informe, antes de subir nada:
- `horizonte_max_min`: qué líneas pasan y hasta qué antelación. Una línea que
  no pasa a ninguna **no entra** en el artefacto (si no pasa ninguna, exit 3).
- `paridad_max_abs` < 1e-6 (los árboles en Python dan lo mismo que LightGBM).
- `desfase_rasgos`: `filas_con_desfase` ≈ 0 a 30 min (a 0 min, unas pocas en
  `inc_delay`: el entrenamiento ve la llegada del entrante por su hora real; la
  ingesta, cuando la observa).
- `excluded_days_skipped`: los días anómalos.

El artefacto **no se commitea** (el repo es público y sale del histórico
privado). Sí se puede commitear el informe (sólo agregados) en
`docs/informes/`.

## Subir el artefacto (con OK de Juan)
```bash
V=<versión>
gcloud storage cp /tmp/prediccion/modelo-$V.json.gz \
  gs://cercanias-madrid-rt-privado/prediccion/modelos/modelo-$V.json.gz
gcloud storage cp /tmp/prediccion/modelo-$V.json.gz \
  gs://cercanias-madrid-rt-privado/prediccion/modelo.json
```
`modelo.json` es el que lee la ingesta (el lector descomprime el gzip). Las
versiones quedan en `prediccion/modelos/` para volver atrás.

## Encender (con OK de Juan; tras mergear y desplegar la imagen, DESPLIEGUE.md)
```bash
gcloud run jobs update cercanias-ingesta --region europe-southwest1 \
  --project cercanias-en-hora --update-env-vars PREDICCION_ROUTES=C1
# verificar (abajo) y después (la coma separa variables en gcloud: hace falta
# otro delimitador, `^@^`):
#   --update-env-vars '^@^PREDICCION_ROUTES=C1,C5'
```
Efecto en el siguiente tick. En la primera hora C1 predice poco: el estado de
la línea se va llenando con las observaciones de cada tick.

## Verificar
```bash
WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
curl -s --compressed "$WEB/boards.json" | python3 -c "
import json, sys, collections
b = json.load(sys.stdin); h = b['health']
print(h.get('prediction'), 'predichas', h.get('departures_predicted'))
c = collections.Counter((d['route'][:2], d['realtime_status'])
    for e in b['stations'].values() for d in e['departures'] if d.get('delay_source') == 'predicted')
print(c)
mal = [d for e in b['stations'].values() for d in e['departures']
       if d.get('delay_source') == 'predicted' and (d['live'] and not d.get('pending_departure'))]
print('previstas con el tren en vivo (debe ser 0):', len(mal))
"
gcloud logging read 'resource.labels.job_name="cercanias-ingesta" AND jsonPayload.alert=~"^prediction_"' \
  --project cercanias-en-hora --freshness=1h --limit 20
```
- `status: ok` y `artifact_version` la subida. Otro `status` = ver la alerta.
- Sólo C1 y C5, y sólo `not_published` / `no_data` / `pending_departure`.
- Al día siguiente, el histórico (RB-010) permite medir lo mostrado: misma
  métrica que el backtest semanal sobre las filas que llevaron `predicted`.

## Rollback
1. **Apagar** (inmediato, sin desplegar): `--update-env-vars PREDICCION_ROUTES=`
   (o `--remove-env-vars PREDICCION_ROUTES`). El siguiente tick sale como antes.
2. **Volver a un artefacto anterior**: copiar
   `prediccion/modelos/modelo-<versión anterior>.json.gz` sobre
   `prediccion/modelo.json` (ojo: caduca a los 21 días de su `created_at`).
3. **Quitar el código**: revertir el merge y redesplegar la imagen anterior
   (DESPLIEGUE.md). `prediccion/observaciones.json` se puede dejar: nadie lo
   lee con la lista vacía.

## Decisiones abiertas
- El listón con antelación: C1 pasa a 30 min (30,7 % menos MAE) pero no a 60
  (29,3 %); por eso C1 casi sólo sale con el tren aún en cabecera.
- C5 se entrena con antelaciones mezcladas (0–120 min); C1 como el backtest
  (sólo en `t0`), porque mezclar lo empeoraba.
- Días excluidos compartidos con PR #14 (mismo fichero).
