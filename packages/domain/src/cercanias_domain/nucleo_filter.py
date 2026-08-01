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
#: Por defecto se filtra por el PREFIJO de 2 dígitos, no por el código de 4.
#:
#: Los dos últimos dígitos del ``tripId`` NO identifican el núcleo: rotan con el
#: día, junto con la letra separadora (ver ADR-012). Observado en tres días
#: consecutivos sobre el mismo núcleo de Madrid:
#:
#:     2026-07-29 (jueves)  1080J20316C4a
#:     2026-07-31 (viernes) 1010V21348C10
#:     2026-08-01 (sábado)  1011S27455C4b
#:
#: Los dos primeros dígitos (``10`` = Madrid) son lo único estable, y es
#: coherente con que el patrón especial del Anexo A.2 sea ``SPECIAL_10_*``,
#: también de dos dígitos. Filtrar por el código de 4 rompe el sistema cada vez
#: que Renfe rota el calendario, y rompe en silencio: filtra a cero.
DEFAULT_NUCLEO_CODES: tuple[str, ...] = ("10",)

_NUCLEO_CODE_RE = re.compile(r"\d{4}")
_NUCLEO_PREFIX_RE = re.compile(r"\d{2}")


@dataclass(frozen=True)
class NucleoFilter:
    """Filtro compilado para un conjunto de códigos de núcleo."""

    pattern: re.Pattern[str]
    codes: tuple[str, ...]

    def matches(self, trip_id: str) -> bool:
        return self.pattern.match(trip_id) is not None


def build_nucleo_filter(codes: Sequence[str] | None = None) -> NucleoFilter:
    """Construye el filtro de núcleo. Acepta prefijos de 2 dígitos o códigos de 4.

    - **2 dígitos** (recomendado): identifica el núcleo y tolera la rotación
      diaria de los dos últimos dígitos. ``"10"`` genera::

          ^(?:10\\d{2}[A-Z]|SPECIAL_10_)

      que casa con ``1080J…``, ``1010V…`` y ``1011S…`` sin tocar configuración.

    - **4 dígitos**: coincidencia exacta. Sigue admitiéndose para poder fijar un
      día concreto en un test, pero NO debe usarse en producción: el código de 4
      caduca cada día (ADR-012) y el fallo es silencioso, porque un filtro que no
      reconoce nada devuelve cero sin error.

    Prohibido filtrar por etiqueta de línea (C-3): esta función nunca mira el
    sufijo del ``tripId``, solo el prefijo de núcleo.
    """
    resolved = tuple(codes) if codes is not None else DEFAULT_NUCLEO_CODES
    if not resolved:
        raise ValueError("Se requiere al menos un codigo de nucleo")

    trip_alternatives: list[str] = []
    special_prefixes: set[str] = set()
    for code in resolved:
        if _NUCLEO_PREFIX_RE.fullmatch(code):
            # Prefijo estable + comodín para los dos dígitos que rotan a diario.
            trip_alternatives.append(rf"{re.escape(code)}\d{{2}}")
            special_prefixes.add(re.escape(code))
        elif _NUCLEO_CODE_RE.fullmatch(code):
            trip_alternatives.append(re.escape(code))
            special_prefixes.add(re.escape(code[:2]))
        else:
            raise ValueError(
                f"Codigo de nucleo invalido: {code!r} "
                "(se esperan 2 digitos de prefijo, recomendado, o 4 de codigo exacto)"
            )

    trip_alt = "|".join(trip_alternatives)
    special_alt = "|".join(sorted(special_prefixes))
    pattern = re.compile(rf"^(?:(?:{trip_alt})[A-Z]|SPECIAL_(?:{special_alt})_)")
    return NucleoFilter(pattern=pattern, codes=resolved)


def parse_nucleo_codes_env(value: str | None) -> tuple[str, ...] | None:
    """Parsea ``RENFE_NUCLEO_CODES`` (lista separada por comas). ``None`` si no está definida."""
    if value is None or not value.strip():
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())
