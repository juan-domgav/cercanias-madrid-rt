"""Filtrado del núcleo de Madrid — Anexo A.2 / corrección C-3.

Regla definitiva (NO simplificar a ``startswith("10")``): el ``tripId`` lleva
un código de núcleo de 4 dígitos como prefijo (``1080`` en la captura del
Anexo A del 29/07/2026), seguido de un separador de una letra y el resto del
identificador. Existen además viajes especiales con formato
``SPECIAL_<2 dígitos>_<resto>`` que no llevan el código de 4 dígitos, solo
sus dos primeros dígitos.

ANOMALÍA detectada en Fase 0 (ver docs/informes/feed-freshness.md y
docs/adr para el detalle): una descarga en vivo del feed realizada el
31/07/2026 (dos días después de la captura del Anexo A) muestra el código de
núcleo de Madrid como ``1010`` con separador ``V``, no ``1080``/``J``. Todas
las posiciones bajo ``1010`` caen dentro de la Comunidad de Madrid, igual que
ocurría con ``1080`` en el Anexo A. Se asume que Renfe ha rotado el
esquema de codificación entre ambas fechas. En consecuencia:

- El separador NO se valida de forma literal (ni ``J`` ni ``V``): la regla
  original de Anexo A.2 solo especifica el prefijo numérico, así que aquí se
  exige únicamente que tras el código de núcleo venga una letra mayúscula,
  sin fijar cuál.
- La lista de códigos de núcleo es configurable y coge por defecto AMBOS
  valores conocidos (``1080`` y ``1010``) para no romper ni contra las
  fixtures congeladas del Anexo A ni contra el feed en producción a fecha de
  hoy. Ver ``RENFE_NUCLEO_CODES``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

#: Códigos de núcleo por defecto si no se configura ``RENFE_NUCLEO_CODES``.
#: 1080: código documentado en el Anexo A (captura 2026-07-29), usado por las
#:       fixtures congeladas de test.
#: 1010: código observado en una descarga en vivo el 2026-07-31 (ver
#:       ANOMALÍA en docs/informes/feed-freshness.md). Se asume vigente hoy.
DEFAULT_NUCLEO_CODES: tuple[str, ...] = ("1080", "1010")

_NUCLEO_CODE_RE = re.compile(r"\d{4}")


@dataclass(frozen=True)
class NucleoFilter:
    """Filtro compilado para un conjunto de códigos de núcleo."""

    pattern: re.Pattern[str]
    codes: tuple[str, ...]

    def matches(self, trip_id: str) -> bool:
        return self.pattern.match(trip_id) is not None


def build_nucleo_filter(codes: Sequence[str] | None = None) -> NucleoFilter:
    """Construye el filtro de núcleo a partir de una lista de códigos de 4 dígitos.

    Genera un patrón equivalente al de Anexo A.2 pero generalizado a N
    códigos y sin fijar el carácter separador:

        ^(?:(?:1080|1010)[A-Z]|SPECIAL_10_)

    Prohibido filtrar por etiqueta de línea (C-3): esta función nunca mira
    el sufijo del ``tripId``, solo el prefijo de núcleo.
    """
    resolved = tuple(codes) if codes is not None else DEFAULT_NUCLEO_CODES
    if not resolved:
        raise ValueError("Se requiere al menos un codigo de nucleo")

    trip_alternatives: list[str] = []
    special_prefixes: set[str] = set()
    for code in resolved:
        if not _NUCLEO_CODE_RE.fullmatch(code):
            raise ValueError(f"Codigo de nucleo invalido: {code!r} (se esperan 4 digitos)")
        trip_alternatives.append(re.escape(code))
        special_prefixes.add(re.escape(code[:2]))

    trip_alt = "|".join(trip_alternatives)
    special_alt = "|".join(sorted(special_prefixes))
    pattern = re.compile(rf"^(?:(?:{trip_alt})[A-Z]|SPECIAL_(?:{special_alt})_)")
    return NucleoFilter(pattern=pattern, codes=resolved)


def parse_nucleo_codes_env(value: str | None) -> tuple[str, ...] | None:
    """Parsea ``RENFE_NUCLEO_CODES`` (lista separada por comas). ``None`` si no está definida."""
    if value is None or not value.strip():
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())
