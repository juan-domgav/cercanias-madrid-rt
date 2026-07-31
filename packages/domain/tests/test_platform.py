from __future__ import annotations

import pytest

from cercanias_domain.platform import parse_platform


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("C4a-20316-PLATF.(7)", "7"),
        ("C4b-20510-PLATF.(0)", "0"),  # cero es un valor real, no None
        ("C2-71134-PLATF.(8AM)", "8AM"),  # no numerico, no debe lanzar
        ("C1-19920-PLATF.(12)", "12"),
        ("C4-23659", None),  # sin anden, caso muy frecuente
        (None, None),
        ("", None),
    ],
)
def test_parse_platform(label: str | None, expected: str | None) -> None:
    assert parse_platform(label) == expected


def test_platform_is_never_int() -> None:
    result = parse_platform("C4b-20510-PLATF.(0)")
    assert isinstance(result, str)
    assert result == "0"
    assert result != 0  # explicito: no es el entero 0
