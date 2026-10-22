# RB-009 — «En hora» en cabecera que no lo es: el retraso de Renfe antes de salir no es una medida

## Síntoma
En una cabecera (p. ej. Colmenar Viejo, C4b) los trenes que **salen** se ven
«en hora» mientras los que **llegan** van +11..+17 (22/10/2026, Juan: «los de
Tres Cantos → Colmenar van con retraso y los de Colmenar → Parla parecen en hora;
no me lo creo»). Tenía razón: 20430 (08:43) salió +9, 20432 +4, 20434 +6, 20438
+10, 20440 +21, 20444 +17, todos con delay 0 publicado 5 min antes.

## Causa
Renfe publica cada viaje ~30 min antes (RB-007) con el tren `STOPPED_AT` en su
origen y un `delay` que **no mide nada**: 0, o el retraso que arrastra la unidad.
Hasta la hora de salida no cambia; pasada la hora, cuenta los minutos de espera.
La salida real es la transición `STOPPED_AT` → `IN_TRANSIT_TO` con `next_stop`
todavía = origen (Renfe tarda unos minutos en cambiar de parada).
RB-008 hizo que el dato en vivo mandase siempre, y así ese 0 se pintaba «en hora».

## Datos (archivo 19-22/10/2026, `scripts/validar_salida_cabecera.py`)
3.736 salidas de cabecera con la salida observada. «0 publicado» = lo que Renfe
decía 5 min antes de la hora con el tren aún en el origen.

| Línea | n (0 publicado) | sale ≥3 min tarde | en punta (07-09:30, 17-20) | «en hora» acertado (±3) |
|---|---|---|---|---|
| C4 | 551 | 67 % | 75 % | 33 % |
| C5 | 549 | 46 % | 56 % | 53 % |
| C10 | 308 | 67 % | 69 % | 32 % |
| C1 | 263 | 38 % | 39 % | 61 % |
| C2 | 260 | 48 % | 62 % | 52 % |
| C3 | 244 | 51 % | 62 % | 49 % |
| C7 | 135 | 54 % | 57 % | 46 % |
| C8 | 77 | 52 % | 53 % | 46 % |
| **Todas** | 2.387 | **54 %** | 62 % | 45 % |

C4 en Colmenar Viejo (17005): 99 salidas con 0, 60 % salen ≥3 tarde (86 % en
punta), mediana de esos retrasos 7,8 min, p90 17. C4 en Parla (37012): 77 %.
Con 10 min de antelación: 46 % en total (C4 54 %). Y un retraso >0 publicado
antes de salir tampoco vale: el 81 % de esos trenes salieron con <3 min.

### ¿Se puede estimar? (umbral: ≥80 % a ±3 min, n ≥ 30, con 5 Y 10 min de antelación)

| Línea | entrante enlazado (pair) 5 / 10 min | primera unidad que llega (pool) | congestión (mediana llegadas 30 min) |
|---|---|---|---|
| C1 | 76 % / 80 % | 69 % / 77 % | 54 % / 57 % |
| C4 | 47 % / 51 % (precisión «≥3 tarde» 81 % / 73 %) | 32 % / 43 % | 43 % / 41 % |
| C8 | 61 % / 57 % (n=54) | 48 % / 52 % | 11 % / 21 % |
| resto | ≤ 57 % | ≤ 61 % | ≤ 49 % |
| Todas | 47 % / 54 % | 46 % / 54 % | 40 % / 41 % |

Maniobra mínima 3 min (también probados 0, 1 y 5: ninguno pasa). **Ninguna línea
pasa el umbral en las dos antelaciones**: no se estima ni se avisa por defecto.

## Arreglo (rama `fix/origin-delay-placeholder`)
- Dominio: `pending_departure` = el tren está en el feed, `next_stop_id` = origen
  del viaje y estado `STOPPED_AT`/`INCOMING_AT`. Vale en TODAS sus paradas (en
  Tres Cantos tampoco es «en hora» un tren que aún no ha salido de Colmenar).
  - Antes de su hora: `delay = null`, `realtime_status = "pending_departure"`.
    Etiqueta: **«pendiente de salir · Renfe aún no da retraso»**. Nunca «en hora».
  - Pasada su hora y aún en el andén: `delay = max(delay del feed, minutos de
    espera)` → **«+N min · aún sin salir»** (cota inferior cierta).
  - En cuanto sale (`IN_TRANSIT_TO`), el dato en vivo manda (RB-008).
- `ORIGIN_ESTIMATE_ROUTES` (job `cercanias-ingesta`, lista PERMITIDA, vacía):
  familias en las que, con el tren aún en cabecera, se estima «≈ +N min (estimado)»
  y se avisa «posible retraso: el tren de llegada viene con +N». Sólo activar una
  línea si vuelve a pasar el umbral con el script.
- Un tren con retraso ya no desaparece del tablón 3 min después de su hora
  **programada**: el margen se mide sobre la prevista (antes, un +12 en Tres Cantos
  se borraba a los 3 min).
- `departures_contradicting_live`: un aviso de entrante sólo contradice un
  retraso **medido**; un tren localizado con `unconfirmed` siempre. Nuevo contador
  `health.departures_pending_departure`.

## Comprobaciones
1. `boards.json` → la salida: `pending_departure`, `realtime_status`, `delay`,
   `display.label`.
2. Feed: `curl -s https://gtfsrt.renfe.com/vehicle_positions.json | jq '.entity[] | select(.id|test("<tren>")) | .vehicle | {currentStatus, stopId}'`
   — `STOPPED_AT` en el origen = aún no ha salido.
3. Revalidar con días nuevos: bajar `archivo/<día>/` y `horarios/<servicio>.json`
   y ejecutar `python scripts/validar_salida_cabecera.py <arch> <dir_horarios> <día>=<servicio> …`.

## Verificación
- `uv run pytest packages/domain/tests/test_pending_departure.py apps/api/tests/test_pending_departure_colmenar.py`
- Producción: en hora punta en Colmenar, los C4b en el andén salen «pendiente de
  salir»; `health.departures_pending_departure > 0` y
  `health.departures_contradicting_live == 0`.

## Rollback
Redeplegar la revisión anterior de `cercanias-api` y la imagen anterior de
`cercanias-ingesta` (ver `docs/ESTADO.md`) y el `index.html` anterior. Los campos
nuevos (`pending_departure`, `health.departures_pending_departure`) se ignoran en
versiones anteriores.

## Riesgo conocido
- «Pendiente de salir» informa menos que un número: es honesto, no preciso.
- Si Renfe deja `STOPPED_AT` en el origen después de salir, el tren se verá
  pendiente unos minutos de más (en el archivo, 4.121 de 4.126 viajes siguen el
  patrón `STOPPED_AT` → `IN_TRANSIT_TO`).
- C1 sigue estimando con el viaje **sin publicar** (RB-007, validado al 91 % el
  21/10); con esta métrica a 5 min da 76 %. Revisar `INBOUND_ESTIMATE_EXCLUDED_ROUTES`.
