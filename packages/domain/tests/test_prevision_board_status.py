"""Contrato: cómo se DICE una previsión (RB-013). Bot y web leen esto mismo.

Regla de producto: nunca "en hora" sin saberlo, y una previsión siempre se
etiqueta "previsto ..." y se distingue del dato en vivo, que siempre manda.
"""

from __future__ import annotations

from typing import Any

import pytest

from cercanias_domain.board_status import (
    KIND_INBOUND_ESTIMATE,
    KIND_LIVE,
    KIND_PREDICTED,
    contradicts_live,
    departure_status,
)


def _fila(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "t": "08:20", "eta": "08:20", "in": 20, "route": "C1", "train": "19799",
        "dest": "Aeropuerto T4", "delay": None, "live": False, "realtime_status": "no_data",
        "delay_source": "predicted", "predicted_delay": 3, "predicted_samples": 12,
        "predicted_day_type": "laborable",
    }
    base.update(kw)
    return base


def test_previsto_con_retraso() -> None:
    s = departure_status(_fila())
    assert s.kind == KIND_PREDICTED
    assert s.label == "previsto +3 min"
    assert s.tone == "predicted"
    # La hora mostrada sigue siendo la del horario: la previsión no la mueve.
    assert s.approximate is False and s.show_scheduled is False
    (aviso,) = s.notices
    assert aviso.kind == "predicted" and aviso.icon == "📊"
    assert aviso.text == "previsto por su histórico; Renfe aún no lo publica en vivo"
    assert aviso.detail == (
        "Previsión, no dato en vivo: es la mediana del retraso de este tren en esta "
        "estación en 12 días laborables recientes. Cuando Renfe lo publique en vivo, "
        "manda su dato."
    )


@pytest.mark.parametrize("mediana", [0, -1])
def test_mediana_cero_o_negativa_es_previsto_sin_retraso_nunca_en_hora(mediana: int) -> None:
    s = departure_status(_fila(predicted_delay=mediana))
    assert s.label == "previsto sin retraso"
    assert "en hora" not in s.label
    assert "puntual" not in s.label


def test_festivos_en_el_detalle() -> None:
    s = departure_status(_fila(predicted_day_type="festivo", predicted_samples=6))
    assert "en 6 sábados, domingos y festivos recientes" in s.notices[0].detail


def test_nunca_en_hora_en_ninguna_prevision() -> None:
    for delay in range(-5, 61):
        for unconfirmed in (False, True):
            for rs in ("no_data", "not_published"):
                s = departure_status(_fila(predicted_delay=delay, unconfirmed=unconfirmed,
                                           realtime_status=rs))
                textos = [s.label] + [n.text for n in s.notices] + [n.detail for n in s.notices]
                assert all("en hora" not in t for t in textos)
                assert s.label.startswith("previsto ")


def test_la_prevision_manda_sobre_no_publicado_y_conserva_el_aviso() -> None:
    s = departure_status(_fila(realtime_status="not_published"))
    assert s.kind == KIND_PREDICTED
    assert "Renfe aún no lo publica" in s.notices[0].text


def test_sin_confirmar_se_sigue_diciendo() -> None:
    s = departure_status(_fila(unconfirmed=True))
    assert [n.kind for n in s.notices] == ["predicted", "unconfirmed"]


def test_el_dato_en_vivo_manda_aunque_la_fila_traiga_prevision() -> None:
    fila = _fila(live=True, delay=0, realtime_status="live", delay_source="live")
    s = departure_status(fila)
    assert s.kind == KIND_LIVE and s.label == "en hora"
    assert all(n.kind != "predicted" for n in s.notices)
    # Pero la ingesta nunca debe producir esa fila: cuenta como contradicción.
    assert contradicts_live(fila) is True


def test_la_estimacion_por_entrante_manda_sobre_la_prevision() -> None:
    s = departure_status(_fila(delay_source="inbound_estimate", realtime_status="inbound_estimate",
                               estimated_delay=5))
    assert s.kind == KIND_INBOUND_ESTIMATE


def test_fila_sin_prevision_no_cambia() -> None:
    s = departure_status(_fila(delay_source=None, predicted_delay=None, predicted_samples=None))
    assert s.kind == "no_data" and s.label == "sin datos en vivo"


def test_fila_prevista_no_es_contradiccion() -> None:
    assert contradicts_live(_fila()) is False


def test_muestras_desconocidas_no_rompen_el_detalle() -> None:
    s = departure_status(_fila(predicted_samples=None, predicted_day_type=None))
    assert s.notices[0].detail.startswith("Previsión, no dato en vivo: es la mediana")
