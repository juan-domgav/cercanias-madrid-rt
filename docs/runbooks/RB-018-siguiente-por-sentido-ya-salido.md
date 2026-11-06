# RB-018 — El bot omite el próximo tren a un destino cuando uno ya salido está en las 6 primeras

## Síntoma
- En el bot de Telegram, `/proximos <estación>` muestra las 6 primeras salidas y
  el bloque «Siguiente por sentido», pero **falta** el próximo tren a un destino
  que el andén de la estación sí anuncia (p. ej. Colmenar en 18–19 min).
- En `boards.json` ese tren **sí** está (más abajo de la sexta salida); el
  tablón de la web, al filtrar por ese sentido, también lo enseña.
- Entre las 6 primeras del bot hay un tren a ese mismo destino con `in < 0`
  (ya salió; el margen de gracia de 3 min lo deja en el tablón).

Visto el 2026-11-05 ~15:46 (Madrid), en **Tres Cantos (17001)**: el andén
anunciaba el 20483 → Colmenar Viejo (16:04, ~19 min). El bot no lo mostraba.
En `boards.json` las 6 primeras incluían el 20481 → Colmenar con `eta` 15:45 e
`in=-1`; el 20483 estaba en la posición 7 (`in=18`).

## Causa
`_next_per_destination_hidden` (`apps/api/src/cercanias_api/bot.py`, RB-017)
construía `vistos` (destinos ya cubiertos en las 6 detalladas) con
`not cancelled`, sin mirar `in`. Un tren ya salido (`in < 0`) contaba como
cobertura del sentido, así que la siguiente salida a ese destino no entraba en
«Siguiente por sentido».

El mismo criterio se aplicaba en el dominio al garantizar la «próxima por
destino» del tablón (`_board_cut` en `departures.py`): un tren con
`minutes_until < 0` podía ser la «próxima» y dejar fuera la siguiente útil si
caía más allá de la ventana de 60 min. La web no tiene lógica propia: solo
filtra `boards.json`.

## Checks
```bash
WEB=https://storage.googleapis.com/cercanias-madrid-rt-web
# 1. Salidas de Tres Cantos (17001): ¿hay un destino con in<0 en las 6 primeras
#    y otra salida al mismo destino más abajo?
curl -s --compressed "$WEB/boards.json" | python3 -c "
import json, sys
d = json.load(sys.stdin)['stations']['17001']['departures']
print('generated via boards; n=', len(d))
for i, x in enumerate(d[:12]):
    print(f\"{i:2d} tren={x['train']} dest={x['dest']!r:40s} eta={x['eta']} in={x['in']}\")
"
# 2. Misma inspección contra el objeto en GCS (por si el CDN/cache engaña):
#    gsutil cat gs://cercanias-madrid-rt-web/boards.json | ...  (puede ir gzip)
curl -s --compressed https://storage.googleapis.com/cercanias-madrid-rt-web/boards.json \
  | python3 -c "
import json, sys
from collections import defaultdict
d = json.load(sys.stdin)['stations']['17001']['departures']
by = defaultdict(list)
for i, x in enumerate(d):
    by[x['dest']].append((i, x['train'], x['in']))
for dest, rows in by.items():
    if any(i < 6 and inn < 0 for i, _, inn in rows) and any(i >= 6 for i, _, _ in rows):
        print('candidato RB-018:', dest, rows[:4])
"
```
Es este runbook si el tren anunciado en el andén aparece en `boards.json` con
`in >= 0` **fuera** de las 6 primeras, y dentro de esas 6 hay otro al mismo
destino con `in < 0`. Si el tren no está en `boards.json`, mirar RB-017 (corte
del tablón) o RB-001 (horario).

## Fix
PR «fix(bot): un tren ya salido no cubre el sentido en Siguiente por sentido (RB-018)»:

- Bot: `_covers_destination` — un destino solo cuenta como visto si la salida
  no está `cancelled` y `in >= 0` (o `in` ausente). **Solo `in < 0` es «ya
  salió»**; `in == 0` («saliendo ahora») sigue cubriendo el sentido.
- Dominio: `_board_cut` ignora `minutes_until < 0` al elegir la próxima por
  destino (misma regla). Esos trenes siguen entrando por la ventana.
- Web: sin cambios de lógica (solo consume `boards.json`).
- Tests de regresión: Tres Cantos en `apps/api/tests/test_bot.py` y el análogo
  de dominio en `packages/domain/tests/test_board_window.py`.

Desplegar (con OK de Juan) **`cercanias-api`** (el bot). Si también se quiere
el arreglo de la garantía en `boards.json` (destino cuya única «próxima» dentro
del horizonte caía fuera de la ventana y estaba tapada por un ya salido),
desplegar además **`cercanias-ingesta`**. La web estática no cambia.

## Verificar
Tras desplegar la API:
```bash
# El check 1 de arriba: anotar un candidato con in<0 en las 6 + siguiente útil
# Luego /proximos en el bot sobre esa estación: «Siguiente por sentido» debe
# listar el tren con in>=0 que faltaba.
```
En Tres Cantos, con el 20481 a `in < 0` entre las 6 y el 20483 más abajo, el
bot debe mostrar el 20483 bajo «Siguiente por sentido». Con `in == 0` en el
20481, el 20483 no aparece ahí (el «saliendo ahora» sí cubre).

## Rollback
Volver la API a la revisión anterior (`cercanias-api-00009-tft`):
```bash
gcloud run services update cercanias-api --region europe-southwest1 --project cercanias-en-hora \
  --image $(gcloud run revisions describe cercanias-api-00009-tft \
    --region europe-southwest1 --project cercanias-en-hora \
    --format='value(status.imageDigest)' | sed 's#^#europe-southwest1-docker.pkg.dev/cercanias-en-hora/cloud-run-source-deploy/cercanias-api@#')
# o, si se anotó la imagen completa al desplegar:
# gcloud run services update cercanias-api --region europe-southwest1 --project cercanias-en-hora \
#   --image <IMAGEN_ANTERIOR@sha256:...>
```
Si también se desplegó la ingesta, revertir su imagen como en RB-017. Sin
desplegar nada, `git revert` del commit en `main` basta para el siguiente build.
