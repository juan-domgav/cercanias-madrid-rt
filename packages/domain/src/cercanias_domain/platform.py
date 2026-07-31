"""Parser de andén (vía) — Anexo A.6.

El andén viene embebido en ``vehicle.vehicle.label``, nunca en un campo
propio. Es ``str | None``, JAMÁS ``int``: valores como ``"8AM"`` o ``"7AM"``
son reales (observados en la captura, núcleo Santander) y ``"0"`` es un
valor válido distinto de "sin andén".
"""

from __future__ import annotations

import re

RE_PLATFORM = re.compile(r"PLATF\.\((?P<platform>[^)]+)\)")


def parse_platform(label: str | None) -> str | None:
    """Extrae el andén del label del vehículo.

    - ``"C4a-20316-PLATF.(7)"``   -> ``"7"``
    - ``"C4b-20510-PLATF.(0)"``   -> ``"0"``    (cero es un valor real, no None)
    - ``"C2-71134-PLATF.(8AM)"``  -> ``"8AM"``  (no numérico, no debe lanzar)
    - ``"C4-23659"``              -> ``None``   (sin andén, caso muy frecuente)
    - ``None``                    -> ``None``
    """
    if not label:
        return None
    match = RE_PLATFORM.search(label)
    if match is None:
        return None
    return match.group("platform")
