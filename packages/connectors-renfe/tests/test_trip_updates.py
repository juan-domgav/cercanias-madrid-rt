"""Contrato del conector de trip_updates (WP-5).

El fixture ``trip_updates_sin_madrid.json`` es una captura REAL del feed del
31/07/2026 en un momento en que el núcleo de Madrid había desaparecido por
completo del feed (ver ADR-011). Se usa aquí por partida doble: para probar el
parser contra estructura real usando otro núcleo (3010, Sevilla), y como fixture
adversarial del caso "el núcleo pedido no está".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cercanias_connectors_renfe.trip_updates import (
    TripUpdatesFetchError,
    parse_trip_updates,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _sin_madrid() -> dict:
    with (FIXTURES / "trip_updates_sin_madrid.json").open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def test_extrae_retrasos_de_estructura_real() -> None:
    """Sobre captura real, con el núcleo que sí está presente."""
    result = parse_trip_updates(_sin_madrid(), nucleo_prefixes=("3010",))
    assert result.matched_nucleo > 0
    assert result.with_delay > 0
    delay = result.delays[0]
    assert delay.trip_id.startswith("3010")
    assert isinstance(delay.delay_seconds, int)


def test_nucleo_ausente_devuelve_cero_sin_reventar() -> None:
    """La anomalía real capturada: Madrid no está en el payload.

    El parser NO debe fallar: devolver cero es correcto. Detectar que cero es
    anómalo es responsabilidad de los invariantes de negocio (health), no del
    parser. Confundir ambas capas es lo que hace que R-1 sea invisible.
    """
    result = parse_trip_updates(_sin_madrid(), nucleo_prefixes=("1010",))
    assert result.matched_nucleo == 0
    assert result.with_delay == 0
    assert result.delays == ()
    assert result.total_entities > 0, "el payload SÍ traía entidades, solo que de otros núcleos"
    assert result.delay_coverage == 0.0


def test_un_solo_stop_time_update_por_viaje() -> None:
    """Hecho medido: el feed publica una parada por viaje, no la secuencia."""
    result = parse_trip_updates(_sin_madrid(), nucleo_prefixes=("3010",))
    trip_ids = [d.trip_id for d in result.delays]
    assert len(trip_ids) == len(set(trip_ids)), "no debe haber mas de un retraso por trip_id"


def test_retraso_a_nivel_de_viaje_como_segunda_fuente() -> None:
    """`tripUpdate.delay` cubre entidades sin stopTimeUpdate."""
    payload = {
        "entity": [
            {"id": "x", "tripUpdate": {"trip": {"tripId": "1010V1C1"}, "delay": 300}},
        ]
    }
    result = parse_trip_updates(payload, nucleo_prefixes=("1010",))
    assert result.with_delay == 1
    assert result.delays[0].delay_seconds == 300
    assert result.delays[0].stop_id is None


def test_retraso_negativo_se_conserva() -> None:
    """Se observaron adelantos de hasta -120 s; no deben truncarse a cero."""
    payload = {
        "entity": [
            {
                "id": "x",
                "tripUpdate": {
                    "trip": {"tripId": "1010V1C1"},
                    "stopTimeUpdate": [{"arrival": {"delay": -120}, "stopId": "17000"}],
                },
            }
        ]
    }
    result = parse_trip_updates(payload, nucleo_prefixes=("1010",))
    assert result.delays[0].delay_seconds == -120


@pytest.mark.parametrize(
    "entidad",
    [
        "no soy un dict",
        {"id": "x"},
        {"id": "x", "tripUpdate": []},
        {"id": "x", "tripUpdate": {"trip": []}},
        {"id": "x", "tripUpdate": {"trip": {"tripId": None}}},
        {"id": "x", "tripUpdate": {"trip": {"tripId": ""}}},
    ],
)
def test_entidad_malformada_se_descarta_sin_tumbar_el_lote(entidad: object) -> None:
    """R-8: una entidad mala se cuenta y se descarta; el resto se procesa."""
    buena = {
        "id": "ok",
        "tripUpdate": {
            "trip": {"tripId": "1010V9C1"},
            "stopTimeUpdate": [{"arrival": {"delay": 60}, "stopId": "17000"}],
        },
    }
    result = parse_trip_updates({"entity": [entidad, buena]}, nucleo_prefixes=("1010",))
    assert result.with_delay == 1, "la entidad buena debe sobrevivir a la mala"
    assert result.skipped_malformed >= 1


@pytest.mark.parametrize("entity_value", [{}, "texto", 42, None])
def test_sobre_invalido_falla_explicitamente(entity_value: object) -> None:
    """Si el sobre no tiene forma, se rechaza el feed entero (no se adivina)."""
    with pytest.raises(TripUpdatesFetchError):
        parse_trip_updates({"entity": entity_value}, nucleo_prefixes=("1010",))


def test_delay_no_entero_se_ignora() -> None:
    payload = {
        "entity": [
            {
                "id": "x",
                "tripUpdate": {
                    "trip": {"tripId": "1010V1C1"},
                    "stopTimeUpdate": [{"arrival": {"delay": "120"}, "stopId": "17000"}],
                },
            }
        ]
    }
    result = parse_trip_updates(payload, nucleo_prefixes=("1010",))
    assert result.with_delay == 0, "un delay string no debe colarse como entero"
