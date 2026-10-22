from __future__ import annotations

import pytest

from cercanias_domain.trip_id import parse_trip_id


def test_parse_standard_trip_id_anexo_a() -> None:
    parsed = parse_trip_id("1080J20316C4a")
    assert parsed is not None
    assert parsed.nucleo_code == "1080"
    assert parsed.nucleo_prefix == "10"
    assert parsed.separator == "J"
    assert parsed.train_number == "20316"
    assert parsed.route_raw == "C4a"
    assert parsed.route_group == "C4"
    assert parsed.is_special is False


def test_parse_standard_trip_id_live_scheme() -> None:
    """ANOMALIA: separador V, nucleo 1010 observado en vivo 31/07/2026."""
    parsed = parse_trip_id("1010V80689C9")
    assert parsed is not None
    assert parsed.nucleo_code == "1010"
    assert parsed.separator == "V"
    assert parsed.route_raw == "C9"
    assert parsed.route_group == "C9"


def test_parse_special_trip_id() -> None:
    parsed = parse_trip_id("SPECIAL_10_91244C7")
    assert parsed is not None
    assert parsed.is_special is True
    assert parsed.nucleo_code is None
    assert parsed.nucleo_prefix == "10"
    assert parsed.separator is None
    assert parsed.train_number == "91244"
    assert parsed.route_raw == "C7"
    assert parsed.route_group == "C7"


@pytest.mark.parametrize(
    ("trip_id", "route_raw", "route_group"),
    [
        ("1080J20316C4a", "C4a", "C4"),
        ("1080J20510C4b", "C4b", "C4"),
        ("1080J21047C8a", "C8a", "C8"),
        ("1080J20639C8b", "C8b", "C8"),
        ("1080J19920C1", "C1", "C1"),
        ("1080J21361C10", "C10", "C10"),
    ],
)
def test_route_group_canonicalization(trip_id: str, route_raw: str, route_group: str) -> None:
    parsed = parse_trip_id(trip_id)
    assert parsed is not None
    assert parsed.route_raw == route_raw
    assert parsed.route_group == route_group


def test_route_label_with_digits_in_suffix() -> None:
    """Etiquetas de linea como R2S, ER04, T1 contienen digitos: el parser no
    debe confundirlos con el numero de tren."""
    parsed = parse_trip_id("5180J28387R2S")
    assert parsed is not None
    assert parsed.train_number == "28387"
    assert parsed.route_raw == "R2S"

    parsed_er = parse_trip_id("1010V85503ER04")
    assert parsed_er is not None
    assert parsed_er.train_number == "85503"
    assert parsed_er.route_raw == "ER04"


def test_unparseable_trip_id_returns_none() -> None:
    assert parse_trip_id("not-a-valid-trip-id") is None
    assert parse_trip_id("") is None


def test_especial_agrupa_su_ramal_en_mayuscula() -> None:
    """El feed escribe C4B: una suscripcion a "C4" tiene que incluirlo."""
    parsed = parse_trip_id("SPECIAL_10_91887C4B")
    assert parsed is not None
    assert parsed.train_number == "91887"
    assert parsed.route_raw == "C4B", "route_raw se conserva intacto (A.3)"
    assert parsed.route_canonical == "C4b"
    assert parsed.route_group == "C4"


def test_viaje_normal_route_canonical_igual_a_raw() -> None:
    parsed = parse_trip_id("1080J20316C4a")
    assert parsed is not None
    assert parsed.route_canonical == "C4a"


def test_especial_sin_linea_no_se_parte_mal() -> None:
    """Sin ancla de letra, SPECIAL_10_91887 daba tren 9188 y linea "7"."""
    assert parse_trip_id("SPECIAL_10_91887") is None
