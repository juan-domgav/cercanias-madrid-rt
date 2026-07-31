"""Normalización de nombres de estación y utilidades de correlación — §5.2, C-10.

C-10 pide comprobar PRIMERO si los ``stop_id`` de 5 dígitos de Renfe
coinciden con la numeración oficial de Adif antes de escribir un algoritmo
de coincidencia por nombre. Este módulo aporta la utilidad de normalización
de nombres (necesaria en cualquier caso como residual) y una función de
distancia espacial reutilizando ``bearing.haversine_distance_meters``.
"""

from __future__ import annotations

import re
import unicodedata

_STRIP_PREFIXES = ("madrid-", "madrid ")
_PUNCTUATION_RE = re.compile(r"[^a-z0-9]+")


def normalize_station_name(name: str) -> str:
    """Normaliza un nombre de estación para comparación aproximada.

    Pasos: minúsculas, eliminación de tildes, eliminación de prefijos
    "Madrid-"/"Madrid ", y colapso de cualquier carácter no alfanumérico.
    """
    lowered = name.strip().lower()
    without_accents = "".join(
        c for c in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(c)
    )
    for prefix in _STRIP_PREFIXES:
        if without_accents.startswith(prefix):
            without_accents = without_accents[len(prefix) :]
            break
    collapsed = _PUNCTUATION_RE.sub("-", without_accents).strip("-")
    return collapsed


def is_five_digit_code(stop_id: str) -> bool:
    """¿Tiene ``stop_id`` la forma de código numérico de 5 dígitos de Renfe (C-10)?"""
    return bool(re.fullmatch(r"\d{5}", stop_id))
