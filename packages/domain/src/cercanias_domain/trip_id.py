"""Parsing del ``tripId`` del feed en tiempo real — Anexo A.2, A.3, A.4.

Formato estándar observado en el Anexo A (captura 2026-07-29):

    1080J20316C4a
    └┬─┘│└─┬──┘└┬┘
     │  │  │    └── etiqueta de línea (route_raw)
     │  │  └─────── número de tren
     │  └────────── separador (un carácter, "J" en el Anexo A)
     └───────────── código de núcleo (4 dígitos)

Formato especial:

    SPECIAL_10_91244C7

ANOMALÍA (ver ``nucleo_filter.py`` y docs/informes/feed-freshness.md): una
descarga en vivo del 31/07/2026 usa el separador ``V`` y el código de núcleo
``1010`` para Madrid en vez de ``J``/``1080``. Este módulo NO fija el
separador a un carácter concreto; solo exige que sea una letra mayúscula.

Regla de canonicalización obligatoria (A.3):

    route_raw   = "C4a"   # se conserva SIEMPRE, es la clave de correlación
    route_group = "C4"    # solo para agrupar en UI y suscripciones

Una suscripción a "C4" debe expandirse a {C4a, C4b}. El sufijo de línea puede
contener dígitos (p. ej. "R2S", "ER04", "T1", observados en otros núcleos o
en el propio GTFS estático de Madrid), así que no se puede asumir que la
etiqueta de línea es puramente alfabética.

En los especiales la etiqueta de línea debe EMPEZAR por letra: sin ese ancla,
``SPECIAL_10_91887`` (sin línea) se partía en tren ``9188`` y línea ``"7"``.
El feed escribe además el ramal en mayúscula (``C4B``); ``route_canonical``
lo iguala al horario (``C4b``) y ``route_group`` lo agrupa en ``C4`` igual que
un viaje normal, para que las suscripciones a "C4" incluyan los especiales.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_RE_STANDARD = re.compile(r"^(?P<nucleo>\d{4})(?P<sep>[A-Z])(?P<train>\d+)(?P<line>[A-Za-z0-9]+)$")
_RE_SPECIAL = re.compile(
    r"^SPECIAL_(?P<nucleo_prefix>\d{2})_(?P<train>\d+)(?P<line>[A-Za-z][A-Za-z0-9]*)$"
)
_RE_GROUPABLE = re.compile(r"^(?P<base>C\d{1,2})[abAB]$")
_RE_BRANCH_UPPER = re.compile(r"^(?P<base>C\d{1,2})(?P<branch>[AB])$")


@dataclass(frozen=True)
class ParsedTripId:
    trip_id: str
    nucleo_code: str | None  # 4 dígitos; None en trips especiales (solo se conocen los 2 primeros)
    nucleo_prefix: str  # primeros 2 dígitos, siempre disponible
    separator: str | None  # letra separadora observada; None en trips especiales
    train_number: str
    route_raw: str
    route_group: str
    is_special: bool
    #: ``route_raw`` con el ramal en minúscula (``C4B`` -> ``C4b``), la forma del
    #: horario. ``route_raw`` se conserva intacto (A.3).
    route_canonical: str


def canonical_route(route_raw: str) -> str:
    """``"C4B"`` -> ``"C4b"``. El feed escribe el ramal de los especiales en
    mayúscula, el horario en minúscula; sin igualarlos el tablón mezcla
    ``C4b`` y ``C4B`` como si fueran líneas distintas."""
    match = _RE_BRANCH_UPPER.match(route_raw)
    if match:
        return match.group("base") + match.group("branch").lower()
    return route_raw


def _route_group(route_raw: str) -> str:
    match = _RE_GROUPABLE.match(route_raw)
    if match:
        return match.group("base")
    return route_raw


def parse_trip_id(trip_id: str) -> ParsedTripId | None:
    """Descompone un ``tripId`` del feed. ``None`` si no coincide con ningún formato conocido."""
    match = _RE_STANDARD.match(trip_id)
    if match:
        line = match.group("line")
        return ParsedTripId(
            trip_id=trip_id,
            nucleo_code=match.group("nucleo"),
            nucleo_prefix=match.group("nucleo")[:2],
            separator=match.group("sep"),
            train_number=match.group("train"),
            route_raw=line,
            route_group=_route_group(line),
            is_special=False,
            route_canonical=canonical_route(line),
        )

    match = _RE_SPECIAL.match(trip_id)
    if match:
        line = match.group("line")
        return ParsedTripId(
            trip_id=trip_id,
            nucleo_code=None,
            nucleo_prefix=match.group("nucleo_prefix"),
            separator=None,
            train_number=match.group("train"),
            route_raw=line,
            route_group=_route_group(line),
            is_special=True,
            route_canonical=canonical_route(line),
        )

    return None
