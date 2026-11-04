# RB-017 — El tablón se corta a los pocos minutos y falta el próximo tren de un sentido

## Síntoma
- En una estación con mucho tráfico (Chamartín, Atocha, Nuevos Ministerios…)
  el tablón de la web o del bot no enseña el próximo tren a un destino aunque
  salga en pocos minutos y vaya en hora.
- En la web, al pulsar el chip «→ *destino*» sale «No hay salidas programadas en
  las próximas 2 h» aunque el horario sí tiene trenes a ese destino.
- `boards.json` tiene el mismo número de salidas (10) en casi todas las
  estaciones, y la última salida está a pocos minutos.

Visto el 2026-11-04 13:44 (Madrid): en Madrid-Chamartín (17000) el 20460 (C4b →
Parla, 13:47, en hora) no salía en la web ni en el bot. Con el corte fijo de 10
salidas el tablón de Chamartín acababa en las 13:50 (~7 min) y lo llenaban los
trenes de otros destinos, incluidos retrasados cuya hora programada ya había
pasado. 77 de 93 estaciones tocaban el tope de 10.

## Causa
`cloud_tick._build_boards` pedía `board_departures(..., DepartureQuery(limit=10))`:
las 10 salidas más próximas por hora prevista, sin más. En un hub 10 salidas son
unos minutos. Los chips de sentido de la web filtran **dentro** de esas 10, así
que elegir un destino no recupera nada. El bot (`/proximos`) pintaba las 6
primeras.

## Checks
```bash
# 1. Salidas por estación, última ETA y destinos cubiertos vs. destinos con horario
curl -s --compressed https://storage.googleapis.com/cercanias-madrid-rt-web/boards.json \
| python3 -c "
import json, sys, collections
b = json.load(sys.stdin); print('generated_at', b['generated_at'], 'servicio', b['service'])
n = collections.Counter(len(s['departures']) for s in b['stations'].values())
print('estaciones por nº de salidas:', sorted(n.items()))
for sid in ('17000', '18000', '18002', '10000'):
    s = b['stations'].get(sid) or {}; d = s.get('departures') or []
    sin = sorted(set(s.get('destinations') or []) - {x['dest'] for x in d})
    print(sid, len(d), 'salidas; última eta', d[-1]['eta'] if d else '-', '(en', d[-1]['in'] if d else '-', 'min);',
          'destinos sin salida:', sin)
"
# 2. ¿Un tren concreto está en el tablón? (p. ej. 20460 en Chamartín)
curl -s --compressed https://storage.googleapis.com/cercanias-madrid-rt-web/boards.json \
| python3 -c "
import json, sys
d = json.load(sys.stdin)['stations']['17000']['departures']
print([x for x in d if x['train'] == '20460'] or 'NO ESTÁ')"
```
Es este runbook si (1) da casi todas las estaciones con exactamente 10 salidas y
la última `in` a menos de ~15 min en los hubs, o el tren de (2) falta teniendo
`in` menor que la última del tablón de otros destinos.

Con el arreglo desplegado lo normal es: los hubs con 40-60 salidas, la última a
~60 min (Atocha Cercanías puede tocar el tope de 60 en hora punta y quedarse en
~40 min), y «destinos sin salida» vacío o solo con destinos sin tren en las
próximas 2 h (comprobarlo en el horario). Si un destino con tren en <2 h falta,
**no** es este runbook: mirar RB-001 (horario) o RB-006 (especiales).

## Fix
PR «Tablones: ventana de 60 min + próxima salida por destino (RB-017)»:

- `DepartureQuery.board_window_minutes` (dominio, `departures.py`). Con valor,
  el tablón incluye **todas** las salidas con hora prevista en los próximos N
  minutos y, además, **la próxima salida de cada destino** aunque caiga más
  lejos, dentro del horizonte de 2 h (`window_minutes`). Se salta un suprimido:
  se garantiza la próxima que sí sale. `limit` pasa a ser un tope duro; si se
  alcanza, primero entra la próxima de cada destino y luego la ventana, de la
  más próxima a la más lejana. Sin valor (`None`, por defecto) el corte es el
  clásico: el bot y cualquier otro llamador no cambian.
- `cloud_tick.py`: `BOARD_WINDOW_MINUTES = 60`, `BOARD_MAX_DEPARTURES = 60`.
  Orden, precedencia de especiales (RB-006) y estados (`display`, RB-008) sin
  cambios: los especiales siguen entrando primero y el horario llena el hueco
  con el mismo criterio.
- Bot (`bot.py`): siguen las 6 salidas con detalle y se añade «Siguiente por
  sentido:», una línea compacta por cada destino cuya próxima salida no está
  entre esas 6.
- Web: sin cambios de lógica. El texto vacío «No hay salidas programadas en las
  próximas 2 h» vuelve a ser cierto con un sentido elegido.

Tope de 60 y no 40: con 40, Atocha Cercanías (18000) en hora punta solo cubría
~27 min y Chamartín ~66 min justos; con 60 solo Atocha toca el tope en la punta
(~42 min, y aun así con la próxima salida de cada destino).

Tamaño de `boards.json` (gzip es lo que viaja; en claro es lo que parsea el móvil):

| Caso | Antes (10) | Después (60 min, tope 60) |
|---|---|---|
| Real, 04/11 13:49, snapshot en vivo (84 trenes), 1007X | 15,0 KB gz · 462 KB · 846 salidas | 20,5 KB gz · 670 KB · 1.236 salidas |
| Jueves 1008J, 07:30 (punta), sin vivo | 12,2 KB gz · 510 KB | 19,2 KB gz · 843 KB |
| Jueves 1008J, 22:30, sin vivo | 11,2 KB gz · 446 KB | 12,5 KB gz · 502 KB |

## Verificar
Tras desplegar la ingesta (no lo hace el PR):
```bash
# El check 1 de arriba: hubs con >10 salidas, última a ~60 min, sin destinos huérfanos
# Tamaño publicado (comprimido) del objeto:
curl -sI -H 'Accept-Encoding: gzip' https://storage.googleapis.com/cercanias-madrid-rt-web/boards.json \
  | grep -i -E 'content-length|content-encoding'
```
En la web, elegir Madrid-Chamartín y cada chip de sentido: todos deben enseñar al
menos un tren (salvo destinos sin servicio en las próximas 2 h). En el bot,
`/proximos chamartin` debe acabar con «Siguiente por sentido:» cuando algún
destino no cabe en las 6 primeras.

## Rollback
- Del código: revertir el commit del PR (`git revert <sha>` en `main`) y volver
  a desplegar la ingesta, o directamente volver a la imagen anterior de
  `cercanias-ingesta`:
  ```bash
  gcloud run jobs describe cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
    --format='value(spec.template.spec.template.spec.containers[0].image)'   # anotar ANTES de desplegar
  gcloud run jobs update cercanias-ingesta --region europe-southwest1 --project cercanias-en-hora \
    --image <IMAGEN_ANTERIOR@sha256:...>
  ```
  El siguiente tick (≤1 min) publica de nuevo `boards.json` con 10 salidas por
  estación. El bot público (`cercanias-api`) solo pinta: con el tablón viejo su
  «Siguiente por sentido» sale con lo que haya en esas 10, sin romper nada.
- Ajuste fino sin revertir: bajar `BOARD_MAX_DEPARTURES` o
  `BOARD_WINDOW_MINUTES` en `cloud_tick.py` si el tamaño o el tiempo de pintado
  en móvil molestan; la garantía de la próxima por destino se mantiene.
