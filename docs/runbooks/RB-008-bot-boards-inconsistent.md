# RB-008 — El bot (o la web) contradice el dato en vivo: «sin confirmar» / «viene con +N» con el tren localizado

## Síntoma
`/proximos <estación>` en el bot (o la tarjeta en la web / mini app) dice
«sin confirmar» y «⚠️ el tren que hace este servicio viene con +N min» para un
tren que **sí** está en el feed, en hora y con andén.

Caso real, 22/10/2026 08:36 (Madrid), Colmenar Viejo:

    08:33 → Parla
       C4b tren 20428 · Andén 3 · sin confirmar
       ⚠️ el tren que hace este servicio viene con +17 min

con el 20428 en el feed (`VP_C4b-20428`, `IN_TRANSIT_TO 17005`, andén 3,
delay 0). La app de Renfe lo dio salido a las 08:33 en hora. Igual para
20430/20432/20434 (+11/+14/+11).

## Causa
1. **El bot lee `boards.json`**, el mismo fichero que la web (caché de 20 s en la
   API, `cercanias_api.main.load_boards`). No hay otra fuente: si el bot y la web
   discrepan con el feed, el fallo está en `boards.json` o en cómo se pinta.
2. `departures._inbound_warning` trataba un `delay` 0 del propio viaje como
   «marcador de posición» (`if propio is not None and propio.delay_seconds`: 0 es
   falso) y avisaba con el retraso del **entrante** emparejado. En Colmenar el
   «entrante» del 20428 era el 20427 (+17 por Tres Cantos), que NO lo formaba:
   Renfe cambia de material en cabecera (emparejamiento C4b acierta el 47 %, ver
   RB-007). La fila quedaba `live: true, delay: 0, inbound_delay: 17,
   on_time: false`.
3. Bot y web tenían cada uno su cascada de etiquetas: `delay == 0` con
   `on_time == false` no encajaba en ningún caso y caía en el `else` final:
   **«sin confirmar»** (que en el resto del código significa otra cosa:
   `unconfirmed`, «no localizamos ni el tren ni su entrante»).

## Arreglo (rama `feat/estimate-from-inbound`, PR #8)
- **Una sola función de estado**: `cercanias_domain.board_status.departure_status`.
  Precedencia: suprimido > especial > **en vivo** > estimado > no publicado >
  sin datos. Con el viaje en vivo nunca «sin confirmar», ni aviso de entrante,
  ni «el que debía hacerlo está suprimido».
- La ingesta guarda el resultado en cada fila de `boards.json` (`display`:
  `kind`, `label`, `tone`, `approximate`, `show_scheduled`, `notices`). La web
  pinta `display`; el bot llama a la misma función sobre la fila.
- `_inbound_warning`: sólo si el propio viaje **no** está en el feed, el enlace no
  es ambiguo y su familia de línea no está en `INBOUND_ESTIMATE_EXCLUDED_ROUTES`
  (por defecto todas salvo C1, la misma lista que la estimación de RB-007).
- `inbound_cancelled` tampoco se marca con el propio viaje en el feed.
- Salud: `health.departures_contradicting_live` (filas en vivo con aviso de
  entrante / `unconfirmed` / `inbound_cancelled`). **Debe ser 0**; si no, la
  ingesta registra `ERROR alert=departures_contradicting_live`.

## Comprobaciones
1. ¿Qué dice `boards.json` de ese tren?

       curl -s https://storage.googleapis.com/cercanias-madrid-rt-web/boards.json \
         | jq '.generated_at, (.stations["17005"].departures[] | select(.train=="20428"))'

   Contradicción = `live: true` junto a `inbound_delay` no nulo o `unconfirmed:
   true`. Con el arreglo desplegado, `display.label` es lo que verá el usuario.
2. Contador global: `jq '.health.departures_contradicting_live'` (falta el campo
   = ingesta sin este arreglo).
3. ¿Está el tren en el feed?

       curl -s https://gtfsrt.renfe.com/trip_updates.json | jq '.entity[] | select(.tripUpdate.trip.tripId|test("20428"))'
       curl -s https://gtfsrt.renfe.com/vehicle_positions.json | jq '.entity[] | select(.id|test("20428"))'

4. Reproducir una pasada pasada: `gs://cercanias-madrid-rt-privado/archivo/<día>/<HHMMSS>.json`
   (hora local de Madrid, una cada ~20 s) + `horarios/<servicio>.json`; ver
   `apps/api/tests/test_bot_boards_consistency.py` para montarlo con `_build_boards`.
5. ¿Qué versión corre? `gcloud run services describe cercanias-api --region europe-southwest1`
   y `gcloud run jobs describe cercanias-ingesta --region europe-southwest1`
   (imagen y fecha). La API y la ingesta se despliegan por separado.

## Despliegue (orden)
Requiere el OK de Juan. Orden recomendado (cada paso es seguro por sí solo):
1. **API** (`cercanias-api`): el bot deja de contradecir el dato en vivo incluso
   con un `boards.json` antiguo (la función ignora avisos en filas en vivo).

       gcloud run deploy cercanias-api --source . --region europe-southwest1

2. **Ingesta** (`cercanias-ingesta`): `boards.json` corrige `inbound_delay`/
   `on_time` y añade `display` y el contador de salud.

       gcloud run jobs deploy cercanias-ingesta --source . --region europe-southwest1

3. **Web** (después de la ingesta; sin `display` pinta un modo mínimo sólo con
   el dato en vivo, sin avisos):

       gcloud storage cp apps/web/index.html gs://cercanias-madrid-rt-web/index.html \
         --cache-control="public, max-age=60" --content-type="text/html"

## Verificación
- `uv run pytest packages/domain/tests/test_board_status.py apps/api/tests/test_bot_boards_consistency.py`
  (20428 del 22/10 en vivo, delay 0, entrante +17 → «en hora» sin aviso; y bot ==
  `display` en todas las salidas).
- Producción: `health.departures_contradicting_live == 0` en cada `boards.json`;
  `/proximos Colmenar Viejo` en hora punta sin «sin confirmar» en trenes con andén.

## Rollback
- Ingesta/API: redeplegar la revisión anterior
  (`gcloud run services update-traffic cercanias-api --to-revisions <rev>=100`;
  para el job, `gcloud run jobs deploy cercanias-ingesta --image <imagen anterior>`).
- Web: volver a subir el `index.html` anterior (cualquier copia de `main`).
- `boards.json` sólo **añade** campos (`display`, `health.departures_contradicting_live`):
  las versiones anteriores de web y bot los ignoran.

## Riesgo conocido
**Resuelto en RB-009:** con el tren aún en su origen el `delay` de Renfe no es
una medida (54 % de los «0» salieron ≥3 min tarde, 19-22/10/2026). Ahora se dice
«pendiente de salir · Renfe aún no da retraso»; el dato en vivo manda en cuanto sale.

El aviso de entrante ya no aparece sobre un viaje en vivo aunque su `delay` 0
sea, de verdad, un marcador previo a la salida (caso del 21/08, Colmenar 16:12).
Se acepta: un aviso desde un emparejamiento no validado contradecía datos
correctos con más frecuencia (C4b 47 %). Si Renfe empieza a publicar retrasos
previstos en origen, el dato en vivo los traerá.
