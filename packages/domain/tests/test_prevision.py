"""Contrato: retraso PREVISTO por el histórico (RB-013).

Backtest del 26/10/2026 (entrenado 08/09-18/10, probado 19-25/10): la mejor
previsión en todas las líneas es la mediana del retraso histórico de ese tren
en esa estación, separando laborables de fines de semana y festivos, sin
valores atípicos (> 60 min o < -5 min). Aquí se prueba la tabla, su carga
segura y la evaluación con la que se verifica el artefacto.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from cercanias_domain.prevision import (
    DAY_TYPE_FESTIVO,
    DAY_TYPE_LABORABLE,
    SCHEMA,
    Observation,
    Prediction,
    PredictionTable,
    PredictionTableError,
    build_table,
    day_type,
    evaluate,
    load_prediction_table,
    operating_date,
    predicted_minutes,
)

LUNES = date(2026, 10, 19)
SABADO = date(2026, 10, 24)
FESTIVO = date(2026, 10, 12)  # lunes, Fiesta Nacional
GEN = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


def _obs(dia: date, minutos: float, tren: str = "19799", parada: str = "18000",
         linea: str = "C1") -> Observation:
    return Observation(linea, tren, parada, dia, minutos * 60)


def _laborables(n: int, desde: date = date(2026, 9, 14)) -> list[date]:
    dias: list[date] = []
    d = desde
    while len(dias) < n:
        if d.weekday() < 5 and d != FESTIVO:
            dias.append(d)
        d += timedelta(days=1)
    return dias


def _tabla(obs: list[Observation], **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "lines": frozenset({"C1", "C5"}), "holidays": frozenset({FESTIVO}),
        "min_observations": 5, "generated_at": GEN,
    }
    base.update(kw)
    return build_table(obs, **base)  # type: ignore[arg-type]


# --- tipo de día ------------------------------------------------------------------

def test_tipo_de_dia_separa_laborables_de_fines_de_semana_y_festivos() -> None:
    festivos = frozenset({FESTIVO})
    assert day_type(LUNES, festivos) == DAY_TYPE_LABORABLE
    assert day_type(SABADO, festivos) == DAY_TYPE_FESTIVO
    assert day_type(SABADO + timedelta(days=1), festivos) == DAY_TYPE_FESTIVO
    assert day_type(FESTIVO, festivos) == DAY_TYPE_FESTIVO


def test_la_madrugada_pertenece_a_la_jornada_anterior() -> None:
    # Un tren de las 00:36 del sábado es del servicio del viernes.
    assert operating_date(datetime(2026, 10, 24, 0, 36)) == date(2026, 10, 23)
    assert operating_date(datetime(2026, 10, 24, 5, 0)) == date(2026, 10, 24)


# --- redondeo ---------------------------------------------------------------------

@pytest.mark.parametrize(("mediana", "minutos"), [(2.5, 3), (2.4, 2), (-0.5, 0), (-3.0, -3), (0.49, 0)])
def test_redondeo_al_minuto_sin_redondeo_bancario(mediana: float, minutos: int) -> None:
    assert predicted_minutes(mediana) == minutos


# --- construcción -----------------------------------------------------------------

def test_mediana_por_tren_parada_y_tipo_de_dia() -> None:
    obs = [_obs(d, m) for d, m in zip(_laborables(5), [1, 2, 3, 4, 10], strict=True)]
    obs += [_obs(SABADO - timedelta(days=7 * k), 0.0) for k in range(5)]
    tabla = PredictionTable.from_json(_tabla(obs))
    lab = tabla.lookup("18000", "19799", DAY_TYPE_LABORABLE)
    fin = tabla.lookup("18000", "19799", DAY_TYPE_FESTIVO)
    assert lab == Prediction("C1", "19799", "18000", DAY_TYPE_LABORABLE, 3.0, 5)
    assert lab is not None and lab.delay_minutes == 3
    assert fin is not None and fin.median_minutes == 0.0 and fin.observations == 5


def test_los_atipicos_no_entran_en_la_mediana_ni_en_la_cuenta() -> None:
    obs = [_obs(d, 2) for d in _laborables(5)]
    obs += [_obs(d, 75) for d in _laborables(3, date(2026, 10, 5))]   # > 60: incidencia
    obs += [_obs(d, -9) for d in _laborables(3, date(2026, 10, 13))]  # < -5: dato roto
    pred = PredictionTable.from_json(_tabla(obs)).lookup("18000", "19799", DAY_TYPE_LABORABLE)
    assert pred is not None and pred.median_minutes == 2.0 and pred.observations == 5


def test_limites_de_atipicos_incluidos() -> None:
    obs = [_obs(d, m) for d, m in zip(_laborables(5), [60, 60, 60, -5, -5], strict=True)]
    pred = PredictionTable.from_json(_tabla(obs)).lookup("18000", "19799", DAY_TYPE_LABORABLE)
    assert pred is not None and pred.observations == 5 and pred.median_minutes == 60.0


def test_sin_muestras_suficientes_no_hay_prevision() -> None:
    obs = [_obs(d, 4) for d in _laborables(4)]
    tabla = PredictionTable.from_json(_tabla(obs))
    assert tabla.lookup("18000", "19799", DAY_TYPE_LABORABLE) is None


def test_solo_las_lineas_pedidas() -> None:
    obs = [_obs(d, 4, tren="20432", linea="C4") for d in _laborables(6)]
    obs += [_obs(d, 1, tren="35001", linea="C5") for d in _laborables(6)]
    datos = _tabla(obs)
    tabla = PredictionTable.from_json(datos)
    assert tabla.lines == frozenset({"C1", "C5"})
    assert tabla.lookup("18000", "20432", DAY_TYPE_LABORABLE) is None
    assert tabla.lookup("18000", "35001", DAY_TYPE_LABORABLE) is not None


def test_metadatos_del_artefacto() -> None:
    obs = [_obs(d, 1) for d in _laborables(5)]
    datos = _tabla(obs)
    assert datos["schema"] == SCHEMA
    assert datos["history"] == {
        "from": "2026-09-14", "to": "2026-09-18", "days": 5, "festivo_days": 0,
    }
    assert datos["outliers_minutes"] == {"min": -5, "max": 60}
    tabla = PredictionTable.from_json(json.loads(json.dumps(datos)))
    assert tabla.generated_at == GEN
    assert tabla.holidays == frozenset({FESTIVO})
    assert tabla.entry_count == 1


def test_tabla_vacia_sin_historico() -> None:
    datos = _tabla([])
    assert datos["history"] == {"from": None, "to": None, "days": 0, "festivo_days": 0}
    assert PredictionTable.from_json(datos).entry_count == 0


# --- consulta por estación ---------------------------------------------------------

def test_for_station_filtra_por_linea_permitida_y_tipo_de_dia() -> None:
    obs = [_obs(d, 3) for d in _laborables(5)]
    obs += [_obs(d, 5, tren="35001", linea="C5") for d in _laborables(5)]
    tabla = PredictionTable.from_json(_tabla(obs))
    solo_c5 = tabla.for_station("18000", DAY_TYPE_LABORABLE, frozenset({"C5"}))
    assert set(solo_c5) == {"35001"}
    assert tabla.for_station("18000", DAY_TYPE_FESTIVO, frozenset({"C1", "C5"})) == {}
    assert tabla.for_station("99999", DAY_TYPE_LABORABLE, frozenset({"C1"})) == {}
    assert tabla.for_station("18000", DAY_TYPE_LABORABLE, frozenset()) == {}


# --- carga a prueba de fallos --------------------------------------------------------

def _valido() -> dict[str, object]:
    return _tabla([_obs(d, 1) for d in _laborables(5)])


def _roto(**cambios: object) -> bytes:
    datos = _valido()
    datos.update(cambios)
    return json.dumps(datos).encode()


@pytest.mark.parametrize(
    "raw",
    [
        b"{no es json",
        b"\xff\xfe",
        b"[]",
        _roto(schema="otra-cosa/9"),
        _roto(lines="C1"),
        _roto(lines=[1]),
        _roto(generated_at="ayer"),
        _roto(generated_at="2026-10-26T08:00:00"),  # sin zona
        _roto(holidays=["no-fecha"]),
        _roto(holidays="2026-10-12"),
        _roto(entries=[]),
        _roto(entries={"C1": []}),
        _roto(entries={"C1": {"18000": []}}),
        _roto(entries={"C1": {"18000": {"19799": []}}}),
        _roto(entries={"C1": {"18000": {"19799": {"laborable": [1.0]}}}}),
        _roto(entries={"C1": {"18000": {"19799": {"laborable": ["x", 5]}}}}),
        _roto(entries={"C1": {"18000": {"19799": {"laborable": [1.0, 2]}}}}),  # < min_obs
        _roto(entries={"C1": {"18000": {"19799": {"laborable": [99.0, 5]}}}}),  # fuera de rango
        _roto(entries={"C1": {"18000": {"19799": {"laborable": [float("nan"), 5]}}}}),
        _roto(entries={"C1": {"18000": {"19799": {"laborable": [True, 5]}}}}),
        _roto(entries={"C1": {"18000": {"19799": {"lunes": [1.0, 5]}}}}),
        _roto(entries={"C9": {"18000": {"19799": {"laborable": [1.0, 5]}}}}),  # línea no declarada
        _roto(min_observations="5"),
        _roto(min_observations=0),
    ],
)
def test_artefacto_roto_no_carga(raw: bytes) -> None:
    with pytest.raises(PredictionTableError):
        PredictionTable.from_bytes(raw)


def test_load_prediction_table_nunca_lanza() -> None:
    tabla, error = load_prediction_table(json.dumps(_valido()).encode())
    assert tabla is not None and error is None
    assert load_prediction_table(None) == (None, "missing")
    tabla, error = load_prediction_table(b"{no es json")
    assert tabla is None and error is not None and error.startswith("invalid:")


# --- evaluación (verificación del artefacto) -------------------------------------------

def test_evaluate_mide_mae_y_acierto_a_dos_minutos_solo_donde_hay_prevision() -> None:
    hist = [_obs(d, 3) for d in _laborables(5)]
    tabla = PredictionTable.from_json(_tabla(hist))
    prueba = [
        _obs(LUNES, 3),            # acierto exacto
        _obs(LUNES, 6),            # error 3
        _obs(LUNES, 70),           # atípico: fuera de la evaluación
        _obs(LUNES, 0, tren="1"),  # sin previsión: cuenta en la cobertura
    ]
    res = evaluate(tabla, prueba, frozenset({FESTIVO}))
    c1 = res["C1"]
    assert c1["n"] == 3
    assert c1["covered"] == 2
    assert c1["coverage_pct"] == pytest.approx(66.7)
    assert c1["mae_predicted"] == pytest.approx(1.5)
    assert c1["mae_on_time"] == pytest.approx(4.5)
    assert c1["within2_predicted_pct"] == pytest.approx(50.0)
    assert c1["within2_on_time_pct"] == pytest.approx(0.0)
    # Toda la línea, con "sin previsión" = 0 (lo que ve el viajero hoy).
    assert c1["mae_all_fallback_on_time"] == pytest.approx((0 + 3 + 0) / 3)
    assert c1["mae_all_on_time"] == pytest.approx((3 + 6 + 0) / 3)
    assert c1["gain_pct"] == pytest.approx(66.7)


def test_evaluate_linea_sin_datos() -> None:
    tabla = PredictionTable.from_json(_tabla([]))
    assert evaluate(tabla, [], frozenset()) == {}
