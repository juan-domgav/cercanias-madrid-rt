from __future__ import annotations

import pytest

from cercanias_domain.station_matching import is_five_digit_code, normalize_station_name


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


@pytest.mark.parametrize(
    ("stop_id", "expected"),
    [
        ("17000", True),
        ("00000", True),
        ("A1234", False),
        ("123", False),
        ("123456", False),
    ],
)
def test_is_five_digit_code(stop_id: str, expected: bool) -> None:
    assert is_five_digit_code(stop_id) is expected
