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
`train_number()` recortaba 5 caracteres fijos y daba `"10"` para todo especial,
y `_build_boards` contaba `"SPECI"` como día de servicio.

Desde el arreglo, `board_departures` añade `live_special_departures`: cada
especial sale en el tablón de su parada actual o próxima (`next_stop_id`) con
`special: true`, `live: true`, `unconfirmed: false`, `t: null`, `eta` = ahora,
`delay: null` y `status` del feed. El destino es `null` (el feed no lo trae).

## Checks
```bash
# WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
curl -s "$WEB/snapshot.json" | jq '[.vehicles[] | select(.trip_id|startswith("SPECIAL_"))
  | {trip_id,route,next_stop_id,status,platform}]'
curl -s "$WEB/boards.json" | jq --arg s 17009 '.stations[$s].departures[]
  | {t,eta,train,route,live,unconfirmed,special,status}'
# ¿Lo conoce el horario? (debe salir vacío: los especiales no están)
gcloud storage cat "gs://cercanias-madrid-rt-web/horarios/${SERVICIO}.json" | grep -c 91887
# Recorrido del especial en el archivo privado (una entrada por pasada):
gcloud storage cat "gs://cercanias-madrid-rt-privado/archivo/$(date +%F)/08*.json" \
  | jq -c '.ts as $t | .trains[] | select(.[0]=="SPECIAL_10_91887C4B") | [$t,.[2],.[3],.[4]]'
```
Si el especial **sí** está en `boards.json` con `special: true`, el arreglo está
activo y el problema es otro (p. ej. la web filtra por sentido: un especial no
tiene destino y sólo se ve con el filtro «todos»).

## Fix
Imagen anterior al arreglo: desplegar la versión con `board_departures`
(PR `fix/special-trains-on-boards`) según `docs/DESPLIEGUE.md`. No hay nada que
regenerar: los horarios no cambian, el arreglo sólo lee el feed.

## Verify
En el siguiente tick, para cada especial de `snapshot.json` con `next_stop_id`,
`boards.json.stations[next_stop_id].departures` contiene una entrada con
`train` = número del especial (no `"10"`), `special: true`, `live: true`,
`unconfirmed: false`. `boards.json.service` sigue siendo el día real
(`10xxX`), nunca `SPECI`. Los programados conservan `t` y su casado por `trip_id`.

## Rollback
Redesplegar la imagen anterior. Los campos nuevos (`special`, `status`) son
aditivos y `t: null` sólo aparece en especiales; la web anterior los ignora
(mostraría el especial como «sin datos en vivo»), así que no hay que tocar
datos del bucket.
