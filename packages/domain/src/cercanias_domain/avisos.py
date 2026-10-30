"""Reglas puras sobre el feed de avisos de Renfe (``alerts.json``), RB-014.

El feed "Incidencias y avisos" (data.renfe.com, CC BY 4.0) publica las
incidencias en TEXTO LIBRE: ``cause``, ``effect`` y ``headerText`` vienen
vacios (medido el 27/10/2026). Lo unico estructurado es el ``routeId`` de las
entidades informadas (``10T0011C4``: prefijo ``10`` = Madrid, sufijo = linea) y
algun ``stopId`` suelto. El tipo de incidencia hay que deducirlo del castellano
con reglas de palabras clave, sabiendo que es una aproximacion: lo que no
encaja va a ``otros`` en vez de inventarse.
"""

from __future__ import annotations

import re
import unicodedata

#: Tipos posibles, en orden de PRECEDENCIA: un aviso que nombra la causa
#: ("por averia...") y el efecto ("...reajuste de servicio") es de la causa.
TIPOS_AVISO: tuple[str, ...] = (
    "personas_en_via",
    "meteorologia",
    "averia",
    "obras",
    "reajuste",
    "otros",
)

#: Palabras clave por tipo, ya sin tildes y en minusculas. Cada clave casa al
#: INICIO de palabra (``meteorolog`` cubre meteorologia/meteorologicas); las que
#: terminan en ``$`` exigen palabra completa (``temporal`` no es
#: ``temporalmente``).
_REGLAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "personas_en_via",
        (
            "personas en la via", "persona en la via", "personas en las vias",
            "persona en las vias", "personas ajenas", "persona ajena", "arrollamiento",
            "atropello", "arrollado",
        ),
    ),
    (
        "meteorologia",
        (
            "meteorolog", "temporal$", "lluvia", "nieve", "nevada", "viento", "inundacion",
            "tormenta", "niebla", "hielo", "granizo",
        ),
    ),
    (
        "averia",
        (
            "averia", "incidencia tecnica", "incidencia en la infraestructura",
            "problema tecnico", "motivos tecnicos", "fallo en", "fallo de",
        ),
    ),
    ("obras", ("obras", "trabajos de mejora", "trabajos en la via", "mantenimiento")),
    ("reajuste", ("reajuste",)),
)

#: Prefijos de ``stopId`` de las estaciones de Cercanias Madrid. Sacados de las
#: 95 paradas de Madrid del GTFS (``paradas-madrid.json`` del PR #10): 10, 12,
#: 17, 18, 19, 35, 37, 60, 70, 97 y 98. Los ``stopId`` de otros nucleos vistos
#: en el feed (13 Bilbao, 43 Sevilla, 64/65/66/69 Valencia) no los comparten.
MADRID_STOP_PREFIXES: tuple[str, ...] = (
    "10", "12", "17", "18", "19", "35", "37", "60", "70", "97", "98",
)
_STOP_ID_LENGTH = 5
#: ``routeId`` de Madrid: ``10T`` + 4 digitos + linea (``C4``, ``C4a``, ``C10``).
MADRID_ROUTE_PREFIX = "10T"
_ROUTE_RE = re.compile(r"^\d{2}T\d{4}(C\d+[a-z]?)$")
_HASHTAG_RE = re.compile(r"#Mad(C\d+[a-z]?)\b")



def _patron(claves: tuple[str, ...]) -> re.Pattern[str]:
    partes = [
        r"\b" + re.escape(c[:-1]) + r"\b" if c.endswith("$") else r"\b" + re.escape(c)
        for c in claves
    ]
    return re.compile("|".join(partes))


_PATRONES = tuple((tipo, _patron(claves)) for tipo, claves in _REGLAS)


def _sin_tildes(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def classify_tipo(texto: str) -> str:
    """Tipo de incidencia deducido del texto en castellano; ``otros`` si no encaja."""
    limpio = _sin_tildes(texto or "")
    for tipo, patron in _PATRONES:
        if patron.search(limpio):
            return tipo
    return "otros"


def line_from_route_id(route_id: str) -> str | None:
    """``10T0014C4a`` -> ``C4a``; ``None`` si el id no tiene esa forma."""
    m = _ROUTE_RE.match(route_id or "")
    return m.group(1) if m else None


def lines_from_hashtags(texto: str) -> tuple[str, ...]:
    """Lineas de las etiquetas ``#MadC4b`` del texto, sin repetir y en orden."""
    vistas: list[str] = []
    for linea in _HASHTAG_RE.findall(texto or ""):
        if linea not in vistas:
            vistas.append(linea)
    return tuple(vistas)


def is_madrid_route(route_id: str) -> bool:
    return bool(route_id) and route_id.startswith(MADRID_ROUTE_PREFIX)


def is_madrid_stop(stop_id: str) -> bool:
    return (
        len(stop_id or "") == _STOP_ID_LENGTH
        and stop_id.isdigit()
        and stop_id.startswith(MADRID_STOP_PREFIXES)
    )
