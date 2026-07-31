from __future__ import annotations

import pytest

from cercanias_domain.station_matching import normalize_station_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Madrid-Chamartín-Clara Campoamor", "chamartin-clara-campoamor"),
        ("Madrid-Atocha Cercanías", "atocha-cercanias"),
        ("Aranjuez", "aranjuez"),
        ("Alcalá de Henares", "alcala-de-henares"),
        ("Príncipe Pío", "principe-pio"),
    ],
)
def test_normalize_station_name(raw: str, expected: str) -> None:
    assert normalize_station_name(raw) == expected
