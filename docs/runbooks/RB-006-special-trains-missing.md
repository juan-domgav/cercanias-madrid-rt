# RB-006 — Trenes especiales (`SPECIAL_10_*`) ausentes del tablón

## Sintoma
`snapshot.json` trae un vehículo `SPECIAL_10_<tren><linea>` con `next_stop_id`
y andén, pero `boards.json` no lo lista en ninguna estación. El tablón de esa
parada sólo muestra trenes del horario, a menudo `unconfirmed: true` (el
programado al que el especial sustituye no circula). Una app de terceros sí
muestra el tren. Observado el 21/10/2026 08:41: `SPECIAL_10_91887C4B` parado en
Universidad Cantoblanco (17009), andén 1, ausente del tablón.

## Causa
Los especiales pasan el filtro de núcleo (Anexo A.2) pero **no existen en el
GTFS estático**. `next_departures` (`cercanias_domain.departures`) recorre el
horario de la estación y sólo después busca el tren en vivo por `trip_id`
exacto, así que un viaje sin horario no puede aparecer nunca. Además
`train_number()` daba `"10"` para todo especial y `_build_boards` contaba
`"SPECI"` como día de servicio.

Desde el arreglo, `board_departures` añade `live_special_departures`: cada
especial sale en el tablón de su parada actual o próxima (`next_stop_id`) con
`special: true`, `live: true`, `unconfirmed: false`, `t: null`, `delay: null`,
`eta` = hora en que se le vio (`generated_at` del snapshot), `status` del feed
y `at_terminus: true` si la estación es sólo cabecera de su línea (puede ser una
llegada). El destino es `null` (el feed no lo trae). Web y bot de Telegram lo
pintan como «tren especial · en el andén / llegando».

Guardas:
- **Fantasmas:** si el snapshot está `degraded`, no tiene `generated_at` o tiene
  `STALE_WARNING_SECONDS` (240 s) o más, los especiales NO se publican
  (`health.specials_hidden_stale: true`). Los programados sí, como siempre.
- **Salud:** `stations_with_departures` y `health.no_departures` cuentan sólo
  salidas del horario; los especiales van en `specials_on_boards`. Si sólo
  circulan especiales (ningún viaje con día de servicio), `service` queda `""` y
  salta `health.service_unknown_with_vehicles` (log `alert=service_unknown_with_vehicles`).
- **Duplicado conocido:** el programado al que sustituye un especial sigue en el
  tablón (normalmente `unconfirmed`). El feed no los relaciona y no hay forma
  segura de deduplicarlos; es esperado, no un fallo.

## Checks
```bash
WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
SERVICIO=$(curl -s "$WEB/boards.json" | jq -r .service)
curl -s "$WEB/snapshot.json" | jq '{generated_at,degraded,especiales:[.vehicles[]
  | select(.trip_id|startswith("SPECIAL_")) | {trip_id,route,next_stop_id,status,platform}]}'
curl -s "$WEB/boards.json" | jq '{service,stations_with_departures,specials_on_boards,health}'
curl -s "$WEB/boards.json" | jq --arg s 17009 '.stations[$s].departures[]
  | {t,eta,train,route,live,unconfirmed,special,status,at_terminus}'
# ¿Lo conoce el horario? (debe dar 0: los especiales no están)
gcloud storage cat "gs://cercanias-madrid-rt-web/horarios/${SERVICIO}.json" | grep -c 91887
# Recorrido del especial en el archivo privado (una entrada por pasada):
gcloud storage cat "gs://cercanias-madrid-rt-privado/archivo/$(date +%F)/08*.json" \
  | jq -c '.ts as $t | .trains[] | select(.[0]=="SPECIAL_10_91887C4B") | [$t,.[2],.[3],.[4]]'
```
- Especial en `snapshot.json` pero no en `boards.json` y `health.specials_hidden_stale: true`:
  es la guarda de fantasmas; el problema real es el feed viejo → RB-002.
- `service: ""` con `service_unknown_with_vehicles: true`: sólo hay especiales o
  ids desconocidos en el feed; no hay horario que casar.

## Fix
Imagen anterior al arreglo: desplegar la versión con `board_departures`
(PR `fix/special-trains-on-boards`) según `docs/DESPLIEGUE.md`. No hay nada que
regenerar: los horarios no cambian, el arreglo sólo lee el feed.

## Verify
En el siguiente tick, con snapshot fresco: para cada especial de `snapshot.json`
con `next_stop_id`, `boards.json.stations[next_stop_id].departures` contiene una
entrada con `train` = número del especial (no `"10"`), `special: true`,
`live: true`, `unconfirmed: false`, `t: null`. `specials_on_boards` coincide con
ese número. `service` sigue siendo el día real (`10xxX`), nunca `SPECI`. Los
programados conservan `t` y su casado por `trip_id`; `stations_with_departures`
no cambia respecto a antes del arreglo.

## Rollback
Redesplegar la imagen anterior. Los campos nuevos (`special`, `status`,
`at_terminus`, `specials_on_boards`, `health.service_unknown_with_vehicles`,
`health.specials_hidden_stale`) son aditivos y `t: null` sólo aparece en
especiales. Con la imagen anterior los especiales vuelven a desaparecer del
tablón; no hay datos del bucket que tocar.
