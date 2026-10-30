from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from cercanias_domain.historico_incidencias import (
    AlertaLinea,
    FotoAvisos,
    PasadaIncidencias,
    alerta_linea,
    foto_desde_objeto,
    incidencias_del_dia,
    pasada_desde_objeto,
)

MAD = ZoneInfo("Europe/Madrid")
D = date(2026, 10, 21)  # servicio 1092X


def ts(h: int, m: int, s: int = 0, dia: date = D) -> datetime:
    return datetime(dia.year, dia.month, dia.day, h, m, s, tzinfo=MAD)


# --- lectura del archivo -----------------------------------------------------------


def test_pasada_sin_claves_es_desconocida() -> None:
    p = pasada_desde_objeto({"ts": "x", "cols": [], "trains": []}, ts(8, 0))
    assert p == PasadaIncidencias(ts(8, 0), None, None)


def test_pasada_con_claves() -> None:
    p = pasada_desde_objeto({"canc": ["1092X1C3"], "skip": {"1092X2C4a": ["17004"]}}, ts(8, 0))
    assert p.canc == frozenset({"1092X1C3"})
    assert p.skip == {"1092X2C4a": frozenset({"17004"})}


@pytest.mark.parametrize(
    "objeto",
    [{"canc": "no-lista", "skip": {}}, {"canc": [], "skip": []}, {"canc": [1], "skip": {}},
     {"canc": [], "skip": {"t": "17004"}}, {"canc": []}],
)
def test_pasada_malformada_es_desconocida(objeto: dict[str, object]) -> None:
    p = pasada_desde_objeto(objeto, ts(8, 0))
    assert p.canc is None
    assert p.skip is None


# --- supresiones y paradas omitidas -------------------------------------------------


def test_sin_registro_todo_es_nulo() -> None:
    inc = incidencias_del_dia([PasadaIncidencias(ts(8, 0), None, None)], D)
    assert inc.registrado is False
    assert inc.pasadas_con_registro == 0
    assert inc.cancelado("1092X20001C3") is None
    assert inc.parada_omitida("1092X20001C3", "17004") is None
    assert inc.recorrido_acortado("1092X20001C3", "17000", "17010") is None
    assert inc.cancelados_por_linea_hora({}) is None


def test_registro_vacio_es_false() -> None:
    inc = incidencias_del_dia([PasadaIncidencias(ts(8, 0), frozenset(), {})], D)
    assert inc.registrado is True
    assert inc.cancelado("1092X20001C3") is False
    assert inc.parada_omitida("1092X20001C3", "17004") is False
    assert inc.recorrido_acortado("1092X20001C3", "17000", "17010") is False


def test_cancelados_y_omitidas_se_acumulan_en_el_dia() -> None:
    inc = incidencias_del_dia(
        [
            PasadaIncidencias(ts(8, 0), frozenset({"1092X20001C3"}), {"1092X20002C4a": frozenset({"17004"})}),
            PasadaIncidencias(ts(8, 1), frozenset(), {"1092X20002C4a": frozenset({"17010"})}),
            PasadaIncidencias(ts(8, 2), None, None),
        ],
        D,
    )
    assert inc.pasadas_con_registro == 2
    assert inc.cancelado("1092X20001C3") is True
    assert inc.cancelados == {"1092X20001C3": ts(8, 0)}
    assert inc.parada_omitida("1092X20002C4a", "17004") is True
    assert inc.parada_omitida("1092X20002C4a", "17010") is True
    assert inc.parada_omitida("1092X20002C4a", "17005") is False


def test_otro_dia_y_otros_nucleos_se_ignoran() -> None:
    inc = incidencias_del_dia(
        [PasadaIncidencias(ts(8, 0), frozenset({"1093J20001C3", "3092X1C1", "basura"}),
                           {"1093J20002C4a": frozenset({"1"})})],
        D,
    )
    assert inc.cancelados == {}
    assert inc.omitidas == {}


def test_madrugada_siguiente_cuenta_para_el_dia() -> None:
    inc = incidencias_del_dia(
        [PasadaIncidencias(ts(0, 30, dia=date(2026, 10, 22)), frozenset({"1092X21875C7"}), {})], D
    )
    assert inc.cancelado("1092X21875C7") is True


def test_especiales_por_hora_de_cambio_de_jornada() -> None:
    inc = incidencias_del_dia(
        [PasadaIncidencias(ts(9, 0), frozenset({"SPECIAL_10_C3_1"}), {})], D
    )
    assert inc.cancelado("SPECIAL_10_C3_1") is True


@pytest.mark.parametrize(
    ("omitidas", "esperado"),
    [({"17000"}, True), ({"17010"}, True), ({"17005"}, False), (set(), False)],
)
def test_recorrido_acortado_si_omite_origen_o_destino(omitidas: set[str], esperado: bool) -> None:
    skip = {"1092X20002C4a": frozenset(omitidas)} if omitidas else {}
    inc = incidencias_del_dia([PasadaIncidencias(ts(8, 0), frozenset(), skip)], D)
    assert inc.recorrido_acortado("1092X20002C4a", "17000", "17010") is esperado


def test_recorrido_acortado_sin_origen_ni_destino_conocidos() -> None:
    inc = incidencias_del_dia(
        [PasadaIncidencias(ts(8, 0), frozenset(), {"1092X20002C4a": frozenset({"17000"})})], D
    )
    assert inc.recorrido_acortado("1092X20002C4a", None, None) is False


def test_cancelados_por_linea_hora_usa_horario_y_si_no_primera_vez() -> None:
    inc = incidencias_del_dia(
        [
            PasadaIncidencias(ts(7, 50), frozenset({"1092X20001C3", "1092X20003C3a"}), {}),
            PasadaIncidencias(ts(9, 10), frozenset({"1092X20005C4b", "1092X20007C3", "1092Xraro"}), {}),
        ],
        D,
    )
    conteo = inc.cancelados_por_linea_hora({"1092X20001C3": 8, "1092X20003C3a": 8})
    # 1092Xraro es de Madrid y se marca cancelado, pero sin familia no se cuenta.
    assert inc.cancelado("1092Xraro") is True
    assert conteo == {("C3", 8): 2, ("C4", 9): 1, ("C3", 9): 1}


# --- avisos -----------------------------------------------------------------------


def _aviso(aid: str, lineas: list[str], start: str | None, end: str | None, tipo: str) -> dict[str, object]:
    return {"id": aid, "start": start, "end": end, "lines": lineas, "stops": [], "text": "t", "tipo": tipo}


FOTOS = [
    FotoAvisos(ts(6, 0), (
        _aviso("A1", ["C3"], "2026-10-21T05:00:00+02:00", "2026-10-21T10:00:00+02:00", "obras"),
        _aviso("A2", ["C4a"], None, None, "otros"),
    )),
    FotoAvisos(ts(9, 0), (
        _aviso("A1", ["C3"], "2026-10-21T05:00:00+02:00", "2026-10-21T10:00:00+02:00", "obras"),
        _aviso("A3", ["C3a", "C5"], "2026-10-21T08:45:00+02:00", None, "averia"),
    )),
]


def test_foto_desde_objeto() -> None:
    foto = foto_desde_objeto(
        {"ts": "2026-10-21T09:00:00+02:00", "alerts": [_aviso("A", [], None, None, "x"), "basura"]}
    )
    assert foto is not None
    assert foto.ts == ts(9, 0)
    assert [a["id"] for a in foto.alerts] == ["A"]


@pytest.mark.parametrize(
    "objeto", [{}, {"ts": "no-fecha", "alerts": []}, {"ts": "2026-10-21T09:00:00+02:00", "alerts": "x"}]
)
def test_foto_malformada(objeto: dict[str, object]) -> None:
    assert foto_desde_objeto(objeto) is None


def test_sin_fotos_o_antes_de_la_primera_es_desconocido() -> None:
    assert alerta_linea([], "C3", ts(8, 0)) == AlertaLinea(None, None, None)
    assert alerta_linea(FOTOS, "C3", ts(5, 59)) == AlertaLinea(None, None, None)
    assert alerta_linea(FOTOS, "C3", None) == AlertaLinea(None, None, None)
    assert alerta_linea(FOTOS, None, ts(8, 0)) == AlertaLinea(None, None, None)


def test_alerta_activa_por_familia() -> None:
    assert alerta_linea(FOTOS, "C3", ts(8, 0)) == AlertaLinea(True, "obras", 180)
    assert alerta_linea(FOTOS, "C4", ts(8, 0)) == AlertaLinea(True, "otros", None)
    assert alerta_linea(FOTOS, "C1", ts(8, 0)) == AlertaLinea(False, None, None)


def test_gana_la_alerta_mas_reciente_de_la_foto_vigente() -> None:
    assert alerta_linea(FOTOS, "C3", ts(9, 30)) == AlertaLinea(True, "averia", 45)
    # A2 ya no esta en la foto de las 09:00.
    assert alerta_linea(FOTOS, "C4", ts(9, 30)) == AlertaLinea(False, None, None)


def test_fuera_de_vigencia_no_cuenta() -> None:
    fotos = [FotoAvisos(ts(6, 0), (
        _aviso("A1", ["C3"], "2026-10-21T09:00:00+02:00", "2026-10-21T10:00:00+02:00", "obras"),
    ))]
    assert alerta_linea(fotos, "C3", ts(8, 59)) == AlertaLinea(False, None, None)
    assert alerta_linea(fotos, "C3", ts(10, 1)) == AlertaLinea(False, None, None)
    assert alerta_linea(fotos, "C3", ts(10, 0)) == AlertaLinea(True, "obras", 60)


def test_fechas_ilegibles_se_tratan_como_abiertas() -> None:
    fotos = [FotoAvisos(ts(6, 0), (_aviso("A1", ["C3", 7], "ayer", "mañana", "obras"),))]  # type: ignore[list-item]
    assert alerta_linea(fotos, "C3", ts(8, 0)) == AlertaLinea(True, "obras", None)


def test_lineas_que_no_son_lista_no_casan() -> None:
    fotos = [FotoAvisos(ts(6, 0), ({"id": "A", "lines": "C3", "tipo": "obras"},))]
    assert alerta_linea(fotos, "C3", ts(8, 0)) == AlertaLinea(False, None, None)


def test_c1_no_casa_con_c10() -> None:
    fotos = [FotoAvisos(ts(6, 0), (_aviso("A", ["C10"], None, None, "obras"),))]
    assert alerta_linea(fotos, "C1", ts(8, 0)) == AlertaLinea(False, None, None)
    assert alerta_linea(fotos, "C10", ts(8, 0)) == AlertaLinea(True, "obras", None)
