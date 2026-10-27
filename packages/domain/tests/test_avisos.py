"""Reglas puras sobre el feed de avisos (Incidencias y avisos de Renfe)."""

from __future__ import annotations

import pytest

from cercanias_domain.avisos import (
    TIPOS_AVISO,
    classify_tipo,
    is_madrid_route,
    is_madrid_stop,
    line_from_route_id,
    lines_from_hashtags,
)


@pytest.mark.parametrize(
    ("texto", "tipo"),
    [
        ("#MadC5 Por obras de mejora en la infraestructura, los trenes inician en Atocha.", "obras"),
        ("#MadC9 Debido a las OBRAS que se están realizando en la línea C-9...", "obras"),
        ("#MadC3 Por avería en un tren, circulación con retrasos.", "averia"),
        ("#MadC4 Por una incidencia técnica en la infraestructura, retrasos.", "averia"),
        ("Por la presencia de personas en la vía entre Atocha y Sol, retrasos.", "personas_en_via"),
        ("#MadC2 Por el arrollamiento de una persona, servicio interrumpido.", "personas_en_via"),
        ("#MadC4b Por reajuste de servicio, tren con salida de Colmenar Viejo...", "reajuste"),
        ("#MadC8 Por las intensas lluvias, circulación restringida.", "meteorologia"),
        ("Por condiciones meteorológicas adversas (nieve), servicio limitado.", "meteorologia"),
        ("Ascensor de la estación de Leganés se encuentra fuera de servicio.", "otros"),
        ("", "otros"),
    ],
)
def test_classify_tipo(texto: str, tipo: str) -> None:
    assert classify_tipo(texto) == tipo
    assert tipo in TIPOS_AVISO


def test_temporalmente_no_es_un_temporal() -> None:
    texto = "#MadC9 Debido a las obras, los aseos permanecen temporalmente inoperativos."
    assert classify_tipo(texto) == "obras"


def test_la_causa_gana_al_efecto() -> None:
    """'Por avería ... reajuste de servicio': la causa es la avería."""
    assert classify_tipo("Por avería en un tren, se realiza un reajuste de servicio.") == "averia"


@pytest.mark.parametrize(
    ("route_id", "linea"),
    [
        ("10T0011C4", "C4"),
        ("10T0014C4a", "C4a"),
        ("10T0016C4b", "C4b"),
        ("10T0042C8", "C8"),
        ("10T0099C10", "C10"),
        ("10T0099", None),
        ("", None),
        ("XX", None),
    ],
)
def test_line_from_route_id(route_id: str, linea: str | None) -> None:
    assert line_from_route_id(route_id) == linea


def test_lines_from_hashtags() -> None:
    assert lines_from_hashtags("#MadC3 #MadC4 Los trenes recuperan frecuencias") == ("C3", "C4")
    assert lines_from_hashtags("#MadC4b reajuste #MadC4b") == ("C4b",)
    assert lines_from_hashtags("#BcnR2 avería") == ()
    assert lines_from_hashtags("sin etiquetas") == ()


def test_madrid_route_and_stop() -> None:
    assert is_madrid_route("10T0011C4")
    assert not is_madrid_route("30T0011C1")
    assert not is_madrid_route("")
    assert is_madrid_stop("35001")  # Leganés
    assert is_madrid_stop("70100")  # Vicálvaro
    assert is_madrid_stop("18002")  # Nuevos Ministerios
    assert not is_madrid_stop("43000")  # Sevilla San Jerónimo
    assert not is_madrid_stop("64103")  # Carcaixent (Valencia)
    assert not is_madrid_stop("13400")  # Barakaldo (Bilbao)
    assert not is_madrid_stop("3500")
    assert not is_madrid_stop("")
