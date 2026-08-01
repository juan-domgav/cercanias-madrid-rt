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
    with pytest.raises(ValueError, match="invalido"):
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


def test_default_codes_constant_is_the_stable_prefix() -> None:
    """El defecto es el prefijo estable, NO una lista de codigos por dia.

    Este test sustituye a uno que exigia que DEFAULT_NUCLEO_CODES contuviera
    "1080" y "1010". Esa expectativa codificaba justamente la averia: ir
    anadiendo el codigo de cada dia a mano es una carrera que se pierde, y se
    pierde en silencio. Ver ADR-012.
    """
    assert DEFAULT_NUCLEO_CODES == ("10",)


# --- ADR-012: los 2 ultimos digitos del tripId rotan con el dia ---------------

_DIAS_OBSERVADOS = [
    ("1080J20316C4a", "2026-07-29 jueves (Anexo A y fixtures congeladas)"),
    ("1010V21348C10", "2026-07-31 viernes"),
    ("1011S27455C4b", "2026-08-01 sabado"),
    ("SPECIAL_10_91244C7", "patron especial del Anexo A.2"),
]


@pytest.mark.parametrize(("trip_id", "dia"), _DIAS_OBSERVADOS)
def test_prefijo_de_dos_digitos_sobrevive_a_la_rotacion_diaria(trip_id: str, dia: str) -> None:
    """El filtro por defecto debe reconocer Madrid CUALQUIER dia.

    Regresion de la averia real del 01/08/2026: con el codigo de 4 digitos
    fijado a ("1080","1010"), el feed del sabado (1011S...) devolvia CERO
    entidades. Y lo hacia en silencio, porque filtrar a cero no es un error.
    """
    assert build_nucleo_filter().matches(trip_id), f"no reconoce Madrid en {dia}"


@pytest.mark.parametrize(
    "trip_id",
    [
        "5111S25642R1",  # Barcelona
        "3011S23534C1",  # Sevilla
        "2011S70414C5",
        "6011S71584C5",  # Bilbao
        "7011S31516C1",  # Zaragoza
        "4511S71714C1",
    ],
)
def test_el_prefijo_no_ensancha_el_filtro_a_otros_nucleos(trip_id: str) -> None:
    """Tolerar la rotacion no debe convertirse en aceptar media España."""
    assert not build_nucleo_filter().matches(trip_id)


def test_codigo_de_cuatro_digitos_sigue_siendo_exacto() -> None:
    """Se mantiene para fijar un dia concreto en un test, no para produccion."""
    solo_viernes = build_nucleo_filter(["1010"])
    assert solo_viernes.matches("1010V21348C10")
    assert not solo_viernes.matches("1011S27455C4b")


def test_codigo_de_longitud_no_admitida_falla_explicitamente() -> None:
    with pytest.raises(ValueError, match="invalido"):
        build_nucleo_filter(["101"])
