"""Contrato: días anómalos (huelgas) excluidos del previsto (RB-013).

Un día con horario reducido o supresiones masivas no es «cómo suele ir ese
tren»: si entra en la mediana, el «previsto» publicado se contamina. La lista
vive versionada en ``cercanias_domain/previsto_dias_excluidos.json``; el
constructor de la tabla salta esos días por completo y lo deja anotado.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from cercanias_domain.prevision import (
    DEFAULT_EXCLUDED_DAYS_PATH,
    ExcludedDay,
    ExcludedDaysError,
    Observation,
    PredictionTable,
    build_table,
    load_excluded_days,
    parse_excluded_days,
)

GEN = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
HUELGA = date(2026, 10, 29)  # jueves
LABORABLES = [date(2026, 10, d) for d in (19, 20, 21, 22, 23, 26, 27, 28)]


def _obs(dia: date, minutos: float, tren: str = "19799", parada: str = "18000") -> Observation:
    return Observation("C1", tren, parada, dia, minutos * 60)


def _tabla(obs: list[Observation], **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "lines": frozenset({"C1"}), "holidays": frozenset(), "min_observations": 5, "generated_at": GEN,
    }
    base.update(kw)
    return build_table(obs, **base)  # type: ignore[arg-type]


def _entrada(tabla: dict[str, object]) -> list[float | int]:
    return tabla["entries"]["C1"]["18000"]["19799"]["laborable"]  # type: ignore[index]


# --- la mediana ignora el día excluido --------------------------------------

def test_la_mediana_ignora_las_filas_de_un_dia_excluido() -> None:
    # 5 días normales a 2 min y 5 pasos el día de huelga a 45 min.
    normales = [_obs(d, 2) for d in LABORABLES[:5]]
    huelga = [_obs(HUELGA, 45, parada="18000") for _ in range(5)]
    sin_excluir = _tabla(normales + huelga)
    assert _entrada(sin_excluir)[0] > 2  # la huelga contamina la mediana

    excluida = _tabla(normales + huelga, excluded_days=frozenset({HUELGA}))
    assert _entrada(excluida) == [2.0, 5]


def test_el_dia_excluido_no_cuenta_en_el_historico_y_queda_anotado() -> None:
    obs = [_obs(d, 2) for d in LABORABLES] + [_obs(HUELGA, 30), _obs(HUELGA, 40, tren="19801")]
    tabla = _tabla(obs, excluded_days=frozenset({HUELGA, date(2026, 12, 1)}))
    hist = tabla["history"]
    assert hist["to"] == "2026-10-28"  # type: ignore[index]
    assert hist["days"] == len(LABORABLES)  # type: ignore[index]
    # Sólo se informa como saltado el día que de verdad tenía filas.
    assert hist["excluded_days_skipped"] == ["2026-10-29"]  # type: ignore[index]
    assert hist["excluded_rows"] == 2  # type: ignore[index]
    assert tabla["excluded_days"] == ["2026-10-29", "2026-12-01"]


def test_un_dia_excluido_tampoco_alcanza_el_minimo_de_muestras() -> None:
    # 4 días normales + la huelga = 5: sin la huelga no llega al mínimo.
    obs = [_obs(d, 2) for d in LABORABLES[:4]] + [_obs(HUELGA, 2)]
    assert _tabla(obs)["entries"] != {}
    assert _tabla(obs, excluded_days=frozenset({HUELGA}))["entries"] == {}


def test_el_artefacto_con_exclusiones_sigue_cargando_en_el_worker() -> None:
    obs = [_obs(d, 2) for d in LABORABLES] + [_obs(HUELGA, 30)]
    raw = json.dumps(_tabla(obs, excluded_days=frozenset({HUELGA}))).encode()
    tabla = PredictionTable.from_bytes(raw)
    assert tabla.entry_count == 1
    assert tabla.history["excluded_days_skipped"] == ["2026-10-29"]


# --- lista vacía o ausente = como hoy ----------------------------------------

def test_sin_lista_se_comporta_como_antes() -> None:
    obs = [_obs(d, 2) for d in LABORABLES] + [_obs(HUELGA, 30)]
    antes = _tabla(obs)
    vacia = _tabla(obs, excluded_days=frozenset())
    assert antes["entries"] == vacia["entries"]
    assert _entrada(antes)[1] == len(LABORABLES) + 1
    assert antes["history"]["excluded_days_skipped"] == []  # type: ignore[index]
    assert antes["history"]["excluded_rows"] == 0  # type: ignore[index]
    assert antes["excluded_days"] == []


def test_fichero_ausente_es_lista_vacia(tmp_path: Path) -> None:
    assert load_excluded_days(tmp_path / "no-existe.json") == ()


@pytest.mark.parametrize("contenido", ["", "  \n", '{"excluded_days": []}', "[]"])
def test_fichero_vacio_es_lista_vacia(tmp_path: Path, contenido: str) -> None:
    ruta = tmp_path / "dias.json"
    ruta.write_text(contenido, encoding="utf-8")
    assert load_excluded_days(ruta) == ()


# --- entradas malformadas fallan con claridad --------------------------------

@pytest.mark.parametrize(
    ("datos", "motivo"),
    [
        ({"excluded_days": [{"reason": "huelga"}]}, "#0: falta 'date'"),
        ({"excluded_days": [{"date": "29/10/2026", "reason": "huelga"}]}, "#0: fecha inválida"),
        ({"excluded_days": [{"date": "2026-10-29"}]}, "#0: falta 'reason'"),
        ({"excluded_days": [{"date": "2026-10-29", "reason": "  "}]}, "#0: falta 'reason'"),
        ({"excluded_days": ["2026-10-29"]}, "#0: debe ser un objeto"),
        ({"excluded_days": [
            {"date": "2026-10-29", "reason": "a"}, {"date": "2026-10-29", "reason": "b"},
        ]}, "#1: fecha repetida 2026-10-29"),
        ({"excluded_days": [{"date": "2026-10-29", "reason": "a", "motivo": "x"}]}, "#0: clave desconocida"),
        ({"excluded_days": "2026-10-29"}, "debe ser una lista"),
        ({"dias": []}, "falta 'excluded_days'"),
        (42, "debe ser una lista"),
    ],
)
def test_entrada_malformada_falla_con_claridad(datos: object, motivo: str) -> None:
    with pytest.raises(ExcludedDaysError, match=motivo):
        parse_excluded_days(datos)


def test_json_ilegible_falla_con_claridad_y_dice_el_fichero(tmp_path: Path) -> None:
    ruta = tmp_path / "dias.json"
    ruta.write_text('{"excluded_days": [', encoding="utf-8")
    with pytest.raises(ExcludedDaysError, match=r"dias\.json.*JSON ilegible"):
        load_excluded_days(ruta)


def test_entrada_malformada_en_fichero_dice_el_fichero(tmp_path: Path) -> None:
    ruta = tmp_path / "dias.json"
    ruta.write_text('{"excluded_days": [{"date": "2026-13-01", "reason": "x"}]}', encoding="utf-8")
    with pytest.raises(ExcludedDaysError, match=r"dias\.json.*#0: fecha inválida"):
        load_excluded_days(ruta)


# --- la lista versionada ------------------------------------------------------

def test_la_lista_versionada_carga_y_contiene_la_huelga_del_29_10() -> None:
    assert DEFAULT_EXCLUDED_DAYS_PATH.name == "previsto_dias_excluidos.json"
    assert DEFAULT_EXCLUDED_DAYS_PATH.is_file()
    dias = load_excluded_days()
    assert ExcludedDay(HUELGA, "huelga Renfe: horario reducido, 302 trenes suprimidos") in dias
    assert list(dias) == sorted(dias, key=lambda d: d.date)
