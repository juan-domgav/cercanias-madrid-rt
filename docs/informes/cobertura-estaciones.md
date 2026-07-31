# Informe de cobertura de estaciones y resolucion de trip_id (E7)

Generado: 2026-07-31T14:55:47.852188+00:00

- Fuente del feed en tiempo real usada en esta ejecucion: **vivo**
- Fuente del GTFS estatico usada en esta ejecucion: **ssl.renfe.com**

## Puerta 0-A: resolucion de trip_id contra el GTFS estatico

- Total de entidades del nucleo de Madrid tras el filtro: **97** (codigos configurados: 1080, 1010; la regla estructural es la del Anexo A.2, pero el codigo `1080` que el Anexo documenta ya no aparece en el feed — ver ADR-010)
- Resueltas contra `trips.txt` (`resolution=trip_static`): **97**
- Sin resolver, fallback a etiqueta del `tripId` (`resolution=fallback_label`): **0**
- Tasa de resolucion: **100.0%**
- Entidades del payload descartadas antes de normalizar (sin `vehicle`/`tripId`/`vehicle.vehicle.id`): 0
- `tripId` que no coincide con ningun formato conocido (ni estandar ni SPECIAL_): 0

### Causas de trip_id no resueltos

Ninguno: el 100% de los trip_id vistos resolvieron contra el GTFS estatico.

## Lineas detectadas

- Etiquetas `route_raw` distintas vistas: C1, C10, C2, C3, C4a, C4b, C5, C7, C8a, C8b, C9
- Grupos `route_group` distintos vistos: C1, C10, C2, C3, C4, C5, C7, C8, C9

### Verificacion explicita: ¿emite C9 posicion GPS?

**SI.** Se han observado 1 entidad(es) con `route_group=C9` en esta ejecucion, de las cuales 1 incluyen bloque `position` (GPS). Esto contradice la nota del Anexo A.3 ("C9 no aparecio en la captura"): en una descarga en vivo del 31/07/2026, C9 SI emitio posicion. Se recomienda actualizar el Anexo A / eliminar cualquier logica de UI que oculte C9 por "sin tiempo real".

## Puerta 0-B (no bloqueante): correlacion Renfe <-> Adif (C-10)

- Estaciones distintas vistas en el feed de Madrid: **51**
- Resueltas por `exact_code` (coincidencia literal `stop_id` == `cod_depend` de IDEADIF): **51** (100.0%)
- Resueltas por `exact_name` (nombre normalizado, residual): **0**
- Sin resolver (`unresolved`): **0**

**Confirmacion de la pista de C-10: el 100% de las estaciones de Madrid vistas en esta ejecucion tienen un `stop_id` de Renfe identico al `cod_depend` de la capa IDEADIF `Tramificacion:Dependencias` (tipo estacion/apeadero). El matching por nombre y el matching espacial quedan confirmados como residuales, tal y como anticipaba la corrección C-10.**

| renfe_stop_id | nombre | mapping_method | adif_dependency_code | confidence |
|---|---|---|---|---|
| `10000` | Madrid-Príncipe Pío | exact_code | 10000 | 1.00 |
| `10001` | Aravaca | exact_code | 10001 | 1.00 |
| `10005` | Las Rozas | exact_code | 10005 | 1.00 |
| `10010` | El Barrial-Centro Comercial Pozuelo | exact_code | 10010 | 1.00 |
| `10100` | Pinar de las Rozas | exact_code | 10100 | 1.00 |
| `10103` | Torrelodones | exact_code | 10103 | 1.00 |
| `10200` | Villalba de Guadarrama | exact_code | 10200 | 1.00 |
| `10203` | El Escorial | exact_code | 10203 | 1.00 |
| `12023` | Los Cotos | exact_code | 12023 | 1.00 |
| `17000` | Madrid-Chamartín-Clara Campoamor | exact_code | 17000 | 1.00 |
| `17001` | Fuencarral | exact_code | 17001 | 1.00 |
| `17003` | El Goloso | exact_code | 17003 | 1.00 |
| `17004` | Tres Cantos | exact_code | 17004 | 1.00 |
| `17005` | Colmenar Viejo | exact_code | 17005 | 1.00 |
| `18000` | Madrid-Atocha Cercanías | exact_code | 18000 | 1.00 |
| `18001` | Madrid-Recoletos | exact_code | 18001 | 1.00 |
| `18002` | Madrid-Nuevos Ministerios | exact_code | 18002 | 1.00 |
| `18003` | Méndez Álvaro P.V. | exact_code | 18003 | 1.00 |
| `18004` | Delicias | exact_code | 18004 | 1.00 |
| `18101` | Sol | exact_code | 18101 | 1.00 |
| `19003` | Alcobendas-San Sebastián de los Reyes | exact_code | 19003 | 1.00 |
| `35001` | Leganés | exact_code | 35001 | 1.00 |
| `35002` | Fuenlabrada | exact_code | 35002 | 1.00 |
| `35009` | Zarzaquemada | exact_code | 35009 | 1.00 |
| `35012` | Humanes | exact_code | 35012 | 1.00 |
| `35600` | Aluche | exact_code | 35600 | 1.00 |
| `35603` | Cuatro Vientos | exact_code | 35603 | 1.00 |
| `35605` | Alcorcón | exact_code | 35605 | 1.00 |
| `35606` | Móstoles | exact_code | 35606 | 1.00 |
| `35607` | Móstoles-El Soto | exact_code | 35607 | 1.00 |
| `35608` | Laguna | exact_code | 35608 | 1.00 |
| `35609` | Embajadores | exact_code | 35609 | 1.00 |
| `35703` | Orcasitas | exact_code | 35703 | 1.00 |
| `37001` | Madrid-Villaverde Alto | exact_code | 37001 | 1.00 |
| `37002` | Getafe-Centro | exact_code | 37002 | 1.00 |
| `37012` | Parla | exact_code | 37012 | 1.00 |
| `60100` | Madrid-Villaverde Bajo | exact_code | 60100 | 1.00 |
| `60102` | Getafe-Industrial | exact_code | 60102 | 1.00 |
| `60103` | Pinto | exact_code | 60103 | 1.00 |
| `60104` | Valdemoro | exact_code | 60104 | 1.00 |
| `60200` | Aranjuez | exact_code | 60200 | 1.00 |
| `70002` | Asamblea de Madrid-Entrevías | exact_code | 70002 | 1.00 |
| `70101` | San Fernando de Henares | exact_code | 70101 | 1.00 |
| `70103` | Alcalá de Henares | exact_code | 70103 | 1.00 |
| `70108` | Coslada | exact_code | 70108 | 1.00 |
| `70111` | La Garena | exact_code | 70111 | 1.00 |
| `70200` | Guadalajara | exact_code | 70200 | 1.00 |
| `97100` | Pitis | exact_code | 97100 | 1.00 |
| `97201` | Madrid-Ramón y Cajal | exact_code | 97201 | 1.00 |
| `98003` | Fuente de la Mora | exact_code | 98003 | 1.00 |
| `98305` | Madrid-Aeropuerto T4 | exact_code | 98305 | 1.00 |

## ANOMALIAS detectadas en esta ejecucion

- **ANOMALIA:** Fuente GTFS 'nap' fallo: respuesta HTTP 200 pero no es un ZIP (content-type=text/html; charset=utf-8); probablemente una pagina HTML (login, portal SPA, etc.)
- **ANOMALIA:** Fuente GTFS 'crtm' fallo: respuesta HTTP 200 pero no es un ZIP (content-type=text/html; charset=utf-8); probablemente una pagina HTML (login, portal SPA, etc.)

## SUPUESTOS ASUMIDOS

- `direction_id` no existe en `trips.txt`: se deriva ordenando los `route_id` que comparten `route_short_name` (ver `cercanias_connectors_renfe.gtfs_static_index`). La marca `direction_derived=True` viaja tanto en `TripStaticInfo` como en `VehicleObservation`, de modo que ningun consumidor puede tomarlo por dato oficial sin ignorar el flag explicitamente.
