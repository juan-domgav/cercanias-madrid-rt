from __future__ import annotations

import pytest

from cercanias_domain.nucleo_filter import (
    DEFAULT_NUCLEO_CODES,
    build_nucleo_filter,
    parse_nucleo_codes_env,
)


def test_default_filter_matches_anexo_a_example() -> None:
    f = build_nucleo_filter()
    assert f.matches("1080J20316C4a")


def test_default_filter_matches_live_2026_07_31_scheme() -> None:
    """ANOMALIA: nucleo Madrid observado en vivo como 1010 con separador V."""
    f = build_nucleo_filter()
    assert f.matches("1010V80689C9")


def test_default_filter_matches_special_pattern() -> None:
    f = build_nucleo_filter()
    assert f.matches("SPECIAL_10_91244C7")


@pytest.mark.parametrize(
    "trip_id",
    [
        "3080J23659C4",  # Sevilla
        "6280J71134C2",  # Santander
        "4080J30449C6",  # Valencia
        "6080J26177C1",  # Bilbao, misma etiqueta de linea C1 que Madrid
        "5180J28387R2S",  # Barcelona / Rodalies
    ],
)
def test_default_filter_excludes_other_nucleos(trip_id: str) -> None:
    f = build_nucleo_filter()
    assert not f.matches(trip_id)


def test_filter_does_not_use_naive_startswith_10() -> None:
    """Regresion explicita del caso de alarma: startswith('10') es incorrecto
    porque perderia SPECIAL_10_ si se implementase de forma ingenua y
    aceptaria falsos positivos de otros nucleos que empiecen por 10."""
    f = build_nucleo_filter(["1080"])
    # Un tripId de un nucleo distinto que por casualidad empieza por "10"
    # pero no es 1080 ni SPECIAL_10_ no debe colar.
    assert not f.matches("1099J99999C1")


def test_custom_codes_are_configurable_not_hardcoded() -> None:
    f = build_nucleo_filter(["9999"])
    assert f.matches("9999J1C1")
    assert not f.matches("1080J20316C4a")


def test_invalid_code_raises() -> None:
    with pytest.raises(ValueError, match="4 digitos"):
        build_nucleo_filter(["not-a-code"])


def test_empty_codes_raises() -> None:
    with pytest.raises(ValueError):
        build_nucleo_filter([])


def test_parse_nucleo_codes_env_none_or_blank() -> None:
    assert parse_nucleo_codes_env(None) is None
    assert parse_nucleo_codes_env("") is None
    assert parse_nucleo_codes_env("   ") is None


def test_parse_nucleo_codes_env_list() -> None:
    assert parse_nucleo_codes_env("1080, 1010 ,9999") == ("1080", "1010", "9999")


def test_default_codes_constant_contains_both_epochs() -> None:
    assert "1080" in DEFAULT_NUCLEO_CODES
    assert "1010" in DEFAULT_NUCLEO_CODES
