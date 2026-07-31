# ADR-009 — IDEADIF: servicios realmente disponibles

- **Estado:** Aceptada
- **Fecha:** 2026-07-31
- **Contexto normativo:** C-12 (§4.3 de la especificación), Puerta 0-B de C-10
- **Evidencia:** `docs/informes/ideadif-capacidades.json` (snapshot de `GetCapabilities`)

## Contexto

La especificación v1.0 asumía la disponibilidad de un servicio WFS en IDEADIF. La
corrección **C-12** rebajó esa afirmación: lo único documentado públicamente en los
metadatos INSPIRE era un **WMS** (OGC 1.1.1 y 1.3.0), y el **WFS quedaba sin confirmar**.
La tarea E6 de Fase 0 consistía en consultar `GetCapabilities` de ambos sobre `https` y
registrar aquí lo que realmente existe, sin implementar todavía el conector.

## Decisión

Consultados los cuatro endpoints el 2026-07-31, el resultado es:

| Servicio | Endpoint | Resultado | Versión |
|---|---|---|---|
| WMS genérico | `https://ideadif.adif.es/geoserver/wms` | **HTTP 404** | — |
| WFS genérico | `https://ideadif.adif.es/geoserver/wfs` | **HTTP 404** | — |
| WMS Tramificación | `https://ideadif.adif.es/gservices/Tramificacion/wms` | **OK** | 1.3.0 |
| WFS Tramificación | `https://ideadif.adif.es/gservices/Tramificacion/wfs` | **OK** | 2.0.0 |

### Capas WMS disponibles (5)

`TramificacionComun`, `Dependencias`, `PKTeoricos`, `TramosFueraServicio`, `TramosServicio`

### typeNames WFS disponibles (4)

`Tramificacion:Dependencias`, `Tramificacion:PKTeoricos`,
`Tramificacion:TramosFueraServicio`, `Tramificacion:TramosServicio`

## Consecuencias

1. **C-12 queda resuelta: el WFS SÍ existe**, en versión 2.0.0, y sirve datos vectoriales.
   La cautela de C-12 era correcta como postura de partida pero el servicio está operativo.
   El conector puede implementarse contra WFS y no está limitado a consumir imágenes WMS.

2. **La ruta de acceso no es la genérica de GeoServer.** Los endpoints
   `/geoserver/wms` y `/geoserver/wfs` devuelven 404. Hay que dirigirse al servicio
   concreto bajo `/gservices/Tramificacion/`. Cualquier código que asuma la convención
   habitual de GeoServer fallará.

3. **`Tramificacion:Dependencias` es la capa que resuelve la Puerta 0-B.** De ella se
   descargaron 3.085 dependencias, y su campo `cod_depend` coincide **literalmente** con
   el `stop_id` del GTFS de Renfe en el 100 % de las 51 estaciones de Madrid observadas
   (ver `docs/informes/cobertura-estaciones.md`). Esto confirma la pista de C-10 y degrada
   el matching por nombre y el espacial a mecanismos residuales.

4. **`TramificacionComun` sólo está en WMS**, no en WFS. Si se necesita su geometría en
   vectorial habrá que obtenerla por otra vía o renderizarla como teselas.

5. Se mantiene `https` en todos los accesos, según C-12.

## Alternativas consideradas

- **Descarga directa en GeoJSON/KML desde `adif-equisyzeta.hub.arcgis.com`** (sugerida por
  C-12 como alternativa). No es necesaria: con WFS 2.0.0 operativo, se obtiene vectorial
  directamente del origen y sin depender de un portal intermedio. Queda registrada como
  plan de contingencia si el WFS dejara de responder.

## Pendiente

- No se implementa el conector en esta fase (E6 es exploratorio por diseño).
- Revisar las condiciones de uso de los datos de Adif antes de republicar geometría. Los
  contenidos de adif.es no están bajo licencia abierta (C-9); los metadatos INSPIRE de
  Tramificación se declaran sin limitaciones de acceso público, que no es lo mismo que una
  licencia de redistribución.
