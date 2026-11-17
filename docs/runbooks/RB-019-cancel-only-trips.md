# RB-019 — Trenes suprimidos sin posición salen «sin datos en vivo» en vez de SUPRIMIDO

## Síntoma
- Día de huelga o de supresiones masivas. Renfe publica muchos viajes como
  `CANCELED` en `trip_updates`, pero el tablón (web y bot `/proximos`) los
  enseña con su hora de horario y «sin datos en vivo» · «aún no localizado», a
  veces con el aviso amarillo «sin confirmar», en lugar de **SUPRIMIDO**.
- `snapshot.json` trae pocos trenes; la salud cuenta decenas de suprimidos
  (`delay_coverage.n_cancelados`, RB-015), pero las filas de `boards.json` de
  esos viajes llevan `cancelled: false`.
- El filtro «→ destino» de la web sólo enseña esos falsos «sin datos»: la
  próxima salida que **sí** sale no está, porque un suprimido sin marcar
  «cubría» el sentido (RB-017/RB-018).

Visto el **2026-11-17 ~08:55 (Madrid)**, huelga, en **Colmenar Viejo (17005)**:
`1020M20432C4b` (08:55 → Parla) y `1020M20436C4b` (09:18 → Parla) eran
`CANCELED` en `trip_updates` y no tenían posición. El tablón decía «sin datos
en vivo». 121 filas del C4 sin marcar mientras la salud contaba 43–50
suprimidos. Juan lo reportó con una captura al bot de ops (que además se
perdía: ver «Relacionado» abajo).

## Causa
`_build_boards` (`apps/worker/src/cercanias_worker/cloud_tick.py`) construía
los `LiveTrain` **sólo** desde `snapshot["vehicles"]` (vehicle_positions) y
marcaba `cancelled` si el viaje estaba también en `cancelled_trips`. Un viaje
suprimido que no va a circular **no tiene posición**, así que nunca llegaba a
`LiveTrain`: el dominio (`_all_departures`) lo veía como «sin tren» y caía en
«sin datos» / «sin confirmar». El camino que pinta SUPRIMIDO no se ejecutaba.

El bot público y la web no calculan: pintan `boards.json`, así que heredaban
el fallo (y su lógica «un suprimido no cubre el sentido» ya era correcta).

## Checks
```bash
WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
# 1. ¿Cuántas filas suprimidas hay en el tablón y cuántos suprimidos sin
#    posición? (campos nuevos de este arreglo; si faltan, la ingesta es anterior)
curl -s --compressed "$WEB/boards.json" | python3 -c "
import json, sys
b = json.load(sys.stdin); h = b['health']
print('servicio', b['service'], b['generated_at'])
print('departures_cancelled', h.get('departures_cancelled'),
      'cancelled_trips_without_position', h.get('cancelled_trips_without_position'))
"
# 2. Supresiones de la última pasada archivada (bucket PRIVADO, solo lectura;
#    requiere ARCHIVAR_CANCELACIONES=1, RB-014) frente a las filas sin marcar:
DIA=$(TZ=Europe/Madrid date +%F)
ULT=$(gcloud storage ls gs://cercanias-madrid-rt-privado/archivo/$DIA/ | tail -1)
gcloud storage cat "$ULT" > /tmp/pasada.json
curl -s --compressed "$WEB/boards.json" > /tmp/boards.json
python3 - <<'PY'
import json
a = json.load(open('/tmp/pasada.json')); b = json.load(open('/tmp/boards.json'))
canc = set(a.get('canc') or []); vivos = {t[0] for t in a['trains']}
num = {t[5:10] for t in canc}
mal = [(s, d['train']) for s, e in b['stations'].items() for d in e['departures']
       if d['train'] in num and not d['cancelled']]
print('suprimidos', len(canc), 'sin posición', len(canc - vivos), 'filas sin marcar', len(mal))
print(mal[:10])
PY
```
Es este runbook si hay suprimidos **sin posición** y filas de esos trenes con
`cancelled: false`. Con la ingesta arreglada, «filas sin marcar» debe ser 0
(salvo desfase de una pasada entre archivo y tablón). Si no hay supresiones en
el archivo, mirar RB-014 (archivo apagado) o RB-015.

## Fix
PR «fix(boards): suprimidos sin posición salen SUPRIMIDO (RB-019)»:

- Dominio (`departures.py`): `LiveTrain.positioned` (por defecto `True`). Un
  suprimido sin posición es `cancelled=True, positioned=False`: la salida sale
  `cancelled`, **no** `located` (sin andén ni retraso), no `unconfirmed`, sin
  estimación ni aviso de entrante, y `realtime_status = "cancelled"` (nuevo
  valor; un suprimido CON posición sigue siendo `live`).
- Ingesta (`_build_boards`): cada viaje de `cancelled_trips` que no está en
  `snapshot["vehicles"]` entra como suprimido sin posición. Los especiales no
  (sin horario ni parada no hay dónde pintarlos). Salud nueva:
  `departures_cancelled` y `cancelled_trips_without_position`. Un suprimido ya
  no cuenta en `departures_missing_in_window`.
- Próxima por destino: sin cambios de lógica; al venir marcado, el suprimido
  ya no cubre el sentido en `boards.json` (RB-017) ni en «Siguiente por
  sentido» del bot (RB-018).
- Web: no añade «aún no localizado» a una fila SUPRIMIDO (sólo cosmético; la
  etiqueta ya sale de `display`).
- Tests con datos reales de la huelga: `fixtures/archivo_huelga_2026-11-17T085451.json`
  y `fixtures/horarios_1020M_colmenar_huelga_2026-11-17.json`.

Desplegar (con OK de Juan) **`cercanias-ingesta`** (genera `boards.json`; es
el arreglo). El bot no necesita cambios de lógica para esto, pero
**`cercanias-api`** lleva en el mismo PR el arreglo de las fotos del bot de ops.
La web (`index.html`) se sube a mano y es opcional.

## Verificar
Tras la primera pasada de la ingesta nueva (≤ 3 min, `cercanias-tick`):
1. Check 1: aparecen `departures_cancelled` y `cancelled_trips_without_position`
   en `boards.json`. Con supresiones en el feed, ambos > 0.
2. Check 2: «filas sin marcar» = 0.
3. Una fila suprimida: `cancelled: true`, `live: false`,
   `realtime_status: "cancelled"`, `display.label: "SUPRIMIDO"`.
4. Bot: `/proximos <estación>` dice SUPRIMIDO en esos trenes y, si el primero
   a un destino está suprimido, ofrece el siguiente que sí sale.
5. `departures_contradicting_live` sigue en 0 (RB-008).

Sin supresiones en el feed (día normal) no cambia nada visible: los dos campos
salen a 0.

## Rollback
Volver la imagen de la ingesta a la anterior (anotarla al desplegar; hoy, PR #24):
```bash
gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
  --image europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-ingesta@sha256:8391cba91205bc0c06c101bb868b2f339f00e6ac3de91d8698b91cf489fcd00c
```
Si también se desplegó la API, devolver el tráfico a la revisión anterior
(hoy `cercanias-api-00010-gdd`, `cercanias-api@sha256:57711bac…a2a64f`):
```bash
gcloud run services update-traffic cercanias-api --region europe-southwest1 \
  --project cercanias-en-hora --to-revisions cercanias-api-00010-gdd=100
```
Sin desplegar nada, `git revert` del commit en `main` basta para el siguiente
build. El campo `realtime_status: "cancelled"` es aditivo: una web o bot
anteriores lo pintan igual (miran `cancelled`/`display`).

## Relacionado: capturas al bot de ops
El mismo 17/11 a las 08:58 Juan mandó una **foto** al bot de ops. El webhook
(`process_update`) solo leía `message.text`: una foto (con o sin pie) devolvía
200 y se perdía sin rastro. Ahora va a `ops/inbox.json` con `text` = pie o
`[foto]`, más `type` y `file_id` (los items de texto no cambian), se registra
`mensaje con adjunto para el buzón` y se contesta el ACK de siempre. Detalle
en RB-005.
