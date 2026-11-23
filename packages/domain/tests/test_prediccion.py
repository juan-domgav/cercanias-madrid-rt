"""Contrato: predicción LightGBM de C1 y C5 en Python puro (RB-021).

Los árboles reales salen de ``scripts/entrenar_prediccion.py``; aquí se prueban
con árboles a mano que cubren cada regla de decisión de LightGBM, y los rasgos
del vivo con un horario y observaciones mínimos.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from cercanias_domain import prediccion as P
from cercanias_domain.departures import ScheduledStop

SERV = "1092X"
FECHA = date(2026, 10, 21)  # miércoles, laborable


def arbol(  # noqa: PLR0913 - columnas del árbol con nombre
    f: list[int],
    t: list[float],
    v: list[float],
    *,
    c: list[Any] | None = None,
    m: list[int] | None = None,
    d: list[bool] | None = None,
    l: list[int] | None = None,  # noqa: E741 - nombre de la columna del artefacto
    r: list[int] | None = None,
) -> dict[str, Any]:
    n = len(f)
    return {
        "f": f,
        "t": t,
        "c": c or [None] * n,
        "m": m or [0] * n,
        "d": d or [False] * n,
        "l": l if l is not None else [-1],
        "r": r if r is not None else [-2],
        "v": v,
    }


def umbral(nombre: str, modelo: str, corte: float, izq: float, der: float, **kw: Any) -> dict[str, Any]:
    """Árbol de un corte: ``nombre <= corte`` -> ``izq``; si no, ``der``."""
    return arbol([P.RASGOS_POR_MODELO[modelo].index(nombre)], [corte], [izq, der], **kw)


def artefacto_json(
    *,
    creado: datetime | None = None,
    arboles: dict[str, list[dict[str, Any]]] | None = None,
    lineas: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arboles = arboles or {
        P.MODELO_ESTADO_LINEA: [umbral("ls_mean", P.MODELO_ESTADO_LINEA, 2.0, 0.4, 4.2)],
        P.MODELO_ENTRANTE_VIVO: [umbral("inc_live_delay", P.MODELO_ENTRANTE_VIVO, 5.0, 1.0, 7.0)],
    }
    return {
        "format": P.FORMATO,
        "version": "2026-11-22-test",
        "created_at": (creado or datetime.now(tz=UTC)).isoformat(),
        "lines": lineas
        or {
            "C1": {"model": P.MODELO_ESTADO_LINEA, "max_horizon_min": 30},
            "C5": {"model": P.MODELO_ENTRANTE_VIVO, "max_horizon_min": 60},
        },
        "models": {
            nombre: {
                "features": list(P.RASGOS_POR_MODELO[nombre]),
                "categories": {c: ["C1", "C5", "18000", "35600"] for c in P.RASGOS_CATEGORICOS},
                "trees": ts,
            }
            for nombre, ts in arboles.items()
        },
        "stats": stats
        or {
            "tren_parada": [[["19801", "18002", False], [3.0, 0, 1, 4, 5, 3.2, 20]]],
            "franja": [[["C1", "18002", 8, False], 1.0]],
            "tren": [[["19801", False], 2.5]],
        },
        "holidays": ["2026-11-02"],
        "excluded_days": ["2026-11-17"],
    }


# ------------------------------------------------------------------ árboles


def test_arbol_numerico_menor_o_igual_va_a_la_izquierda() -> None:
    a = P.Arbol.from_json(arbol([0], [2.0], [10.0, 20.0]))
    assert a.evaluar([2.0]) == 10.0
    assert a.evaluar([2.5]) == 20.0


def test_arbol_ausente_sin_tipo_cuenta_como_cero() -> None:
    a = P.Arbol.from_json(arbol([0], [-1.0], [10.0, 20.0]))
    assert a.evaluar([math.nan]) == 20.0, "NaN -> 0.0, y 0 > -1"


def test_arbol_ausente_nan_sigue_el_lado_por_defecto() -> None:
    izq = P.Arbol.from_json(arbol([0], [-1.0], [10.0, 20.0], m=[2], d=[True]))
    der = P.Arbol.from_json(arbol([0], [100.0], [10.0, 20.0], m=[2], d=[False]))
    assert izq.evaluar([math.nan]) == 10.0
    assert der.evaluar([math.nan]) == 20.0
    assert der.evaluar([3.0]) == 10.0


def test_arbol_ausente_cero_trata_cero_y_nan_como_por_defecto() -> None:
    a = P.Arbol.from_json(arbol([0], [5.0], [10.0, 20.0], m=[1], d=[False]))
    assert a.evaluar([0.0]) == 20.0
    assert a.evaluar([math.nan]) == 20.0
    assert a.evaluar([1.0]) == 10.0


def test_arbol_categorico_y_hoja_unica() -> None:
    a = P.Arbol.from_json(arbol([0], [0.0], [10.0, 20.0], c=[[1, 3]]))
    assert [a.evaluar([x]) for x in (1.0, 3.0, 2.0, math.nan, -1.0)] == [10.0, 10.0, 20.0, 20.0, 20.0]
    hoja = P.Arbol.from_json({"f": [], "t": [], "c": [], "m": [], "d": [], "l": [], "r": [], "v": [0.5]})
    assert hoja.evaluar([]) == 0.5


def test_arbol_de_dos_niveles() -> None:
    # nodo 0: x0 <= 1 -> nodo 1 ; si no hoja 2. nodo 1: x1 <= 0 -> hoja 0 ; si no hoja 1.
    a = P.Arbol.from_json(arbol([0, 1], [1.0, 0.0], [1.0, 2.0, 3.0], l=[1, -1], r=[-3, -2]))
    assert [a.evaluar(x) for x in ([0.0, -1.0], [0.0, 1.0], [5.0, 0.0])] == [1.0, 2.0, 3.0]


@pytest.mark.parametrize(
    "roto",
    [
        {"v": []},
        {"f": [0, 0]},
        {"l": [7]},
        {"m": [9]},
        {"c": ["x"]},
    ],
)
def test_arbol_mal_formado_falla_cerrado(roto: dict[str, Any]) -> None:
    with pytest.raises(P.ArtefactoInvalido):
        P.Arbol.from_json({**arbol([0], [1.0], [1.0, 2.0]), **roto})
    with pytest.raises(P.ArtefactoInvalido):
        P.Arbol.from_json([1, 2])


def test_modelo_suma_arboles_y_mapea_categorias() -> None:
    rasgos = P.RASGOS_POR_MODELO[P.MODELO_ESTADO_LINEA]
    i = rasgos.index("familia_c")
    datos = {
        "features": list(rasgos),
        "categories": {c: ["C1", "C5"] for c in P.RASGOS_CATEGORICOS},
        "trees": [arbol([i], [0.0], [1.0, -1.0], c=[[1]]), arbol([0], [0.0], [0.25, 0.25])],
    }
    m = P.ModeloLgbm.from_json(datos, rasgos)
    assert m.predecir({"familia_c": "C5"}) == 1.25
    assert m.predecir({"familia_c": "C1"}) == -0.75
    assert m.predecir({"familia_c": "C9"}) == -0.75, "categoría desconocida = ausente -> derecha"
    assert math.isnan(m.vector({"ls_mean": "texto"})[rasgos.index("ls_mean")])


def test_modelo_con_otros_rasgos_no_se_usa() -> None:
    rasgos = P.RASGOS_POR_MODELO[P.MODELO_ESTADO_LINEA]
    datos = {
        "features": [*rasgos[:-1], "otro"],
        "categories": {c: [] for c in P.RASGOS_CATEGORICOS},
        "trees": [arbol([0], [0.0], [1.0, 2.0])],
    }
    with pytest.raises(P.ArtefactoInvalido, match="rasgos"):
        P.ModeloLgbm.from_json(datos, rasgos)
    with pytest.raises(P.ArtefactoInvalido, match="fuera de rango"):
        P.ModeloLgbm.from_json(
            {**datos, "features": list(rasgos), "trees": [arbol([999], [0.0], [1, 2])]}, rasgos
        )
    with pytest.raises(P.ArtefactoInvalido, match="categorías"):
        P.ModeloLgbm.from_json({**datos, "features": list(rasgos), "categories": {}}, rasgos)
    with pytest.raises(P.ArtefactoInvalido, match="sin árboles"):
        P.ModeloLgbm.from_json({**datos, "features": list(rasgos), "trees": []}, rasgos)


# ---------------------------------------------------------------- artefacto


def test_artefacto_valido() -> None:
    a = P.Artefacto.from_json(artefacto_json())
    assert a.lineas["C1"] == P.LineaArtefacto(P.MODELO_ESTADO_LINEA, 30.0)
    assert a.festivos == frozenset({date(2026, 11, 2)})
    assert a.dias_excluidos == ("2026-11-17",)
    assert not a.caducado(datetime.now(tz=UTC))
    assert a.caducado(datetime.now(tz=UTC) + timedelta(days=P.CADUCIDAD_DIAS + 1))


@pytest.mark.parametrize(
    ("cambio", "motivo"),
    [
        ({"format": "otro/9"}, "formato"),
        ({"version": ""}, "versión"),
        ({"created_at": "ayer"}, "created_at"),
        ({"created_at": "2026-11-22T10:00:00"}, "zona"),
        ({"lines": {}}, "líneas"),
        ({"lines": {"C1": {"model": "magia", "max_horizon_min": 30}}}, "modelo desconocido"),
        ({"lines": {"C1": {"model": "estado_linea", "max_horizon_min": -1}}}, "antelación"),
        ({"lines": {"C1": {"model": "estado_linea", "max_horizon_min": True}}}, "antelación"),
        ({"models": []}, "sin modelos"),
        ({"holidays": ["no"]}, "festivos"),
        ({"excluded_days": "2026-11-17"}, "excluded_days"),
        ({"stats": {"tren_parada": [[["1", "2", False], [1, 2]]], "franja": [], "tren": []}}, "7 valores"),
        ({"stats": {"franja": []}}, "estadísticas"),
        ({"stats": []}, "estadísticas"),
    ],
)
def test_artefacto_invalido_falla_cerrado(cambio: dict[str, Any], motivo: str) -> None:
    with pytest.raises(P.ArtefactoInvalido, match=motivo):
        P.Artefacto.from_json({**artefacto_json(), **cambio})


def test_artefacto_que_no_es_objeto() -> None:
    with pytest.raises(P.ArtefactoInvalido):
        P.Artefacto.from_json(["no"])


def test_estadisticas_historicas_y_mediana() -> None:
    st = P.Artefacto.from_json(artefacto_json()).estadisticas
    h = st.historicos("19801", "C1", "18002", 8, False)
    assert (h["h_med"], h["h_n"], h["h_lsh_med"], h["h_train_med"]) == (3.0, 20.0, 1.0, 2.5)
    vacio = st.historicos("1", "C1", "x", 8, True)
    assert all(v is None for v in vacio.values())
    assert st.mediana("19801", "C1", "18002", 8, False) == 3.0
    assert st.mediana("99999", "C1", "18002", 8, False) == 1.0, "sin tren: la franja"
    assert st.mediana("99999", "C1", "18002", 9, False) == 0.0, "sin nada: 0"


# ------------------------------------------------------------------- viajes


def parada(  # noqa: PLR0913 - parada del horario con nombre
    trip: str,
    stop: str,
    hhmm: str,
    seq: int,
    *,
    origen: bool = False,
    final: bool = False,
    route: str | None = None,
) -> ScheduledStop:
    return ScheduledStop(
        trip, f"{hhmm}:00", route or trip[-2:], "X", final, is_origin=origen, stop_id=stop, seq=seq
    )


def horario(*paradas: ScheduledStop) -> dict[str, list[ScheduledStop]]:
    h: dict[str, list[ScheduledStop]] = {}
    for p in paradas:
        assert p.stop_id is not None
        h.setdefault(p.stop_id, []).append(p)
    return h


ENTRANTE = f"{SERV}20941C5"  # impar: llega a T 15:40
SALIENTE = f"{SERV}20942C5"  # par: sale de T 15:50
OTRA = f"{SERV}20944C5"  # par: sale de T 16:10
HORARIO_C5 = horario(
    parada(ENTRANTE, "A", "15:00", 1, origen=True),
    parada(ENTRANTE, "B", "15:20", 2),
    parada(ENTRANTE, "T", "15:40", 3, final=True),
    parada(SALIENTE, "T", "15:50", 1, origen=True),
    parada(SALIENTE, "B", "16:10", 2),
    parada(SALIENTE, "A", "16:30", 3, final=True),
    parada(OTRA, "T", "16:10", 1, origen=True),
    parada(OTRA, "A", "16:50", 2, final=True),
)


def test_indice_de_viajes_desde_el_horario_por_estacion() -> None:
    v = P.indice_viajes(HORARIO_C5)[SALIENTE]
    assert (v.tren, v.familia, v.origen_id, v.destino_id) == ("20942", "C5", "T", "A")
    assert (v.salida_min, v.llegada_min) == (950.0, 990.0)
    assert v.paradas["B"] == (2, 970.0)
    assert v.paridad == 0


def test_viaje_sin_origen_o_sin_seq_no_entra() -> None:
    h = horario(parada("1092X1C5", "A", "10:00", 2), parada("1092X1C5", "B", "10:10", 3, final=True))
    h["C"] = [ScheduledStop("1092X2C5", "10:00:00", "C5", "X", False, is_origin=True, stop_id="C")]
    assert P.indice_viajes(h) == {}


def test_emparejamiento_del_backtest() -> None:
    enlaces = P.emparejar_entrantes(P.indice_viajes(HORARIO_C5).values())
    assert enlaces == {SALIENTE: P.Enlace(ENTRANTE, 940.0, 10.0)}, (
        "la PRIMERA salida >= 5 min, paridad contraria"
    )


def test_emparejamiento_exige_maniobra_minima_y_familia() -> None:
    h = horario(
        parada("1092X1C5", "A", "10:00", 1, origen=True),
        parada("1092X1C5", "T", "10:30", 2, final=True),
        parada("1092X2C5", "T", "10:33", 1, origen=True),
        parada("1092X2C5", "A", "11:00", 2, final=True),
        parada("1092X4C1", "T", "10:40", 1, origen=True),
        parada("1092X4C1", "A", "11:00", 2, final=True),
        parada("1092X3C5", "T", "10:45", 1, origen=True),
        parada("1092X3C5", "A", "11:00", 2, final=True),
    )
    assert P.emparejar_entrantes(P.indice_viajes(h).values()) == {}, (
        "2: < 5 min; 4: otra familia; 3: misma paridad"
    )


def test_si_dos_llegadas_forman_la_misma_salida_gana_la_mas_tardia() -> None:
    h = horario(
        parada("1092X1C5", "A", "10:00", 1, origen=True),
        parada("1092X1C5", "T", "10:30", 2, final=True),
        parada("1092X3C5", "A", "10:10", 1, origen=True),
        parada("1092X3C5", "T", "10:40", 2, final=True),
        parada("1092X2C5", "T", "10:50", 1, origen=True),
        parada("1092X2C5", "A", "11:20", 2, final=True),
    )
    assert P.emparejar_entrantes(P.indice_viajes(h).values())["1092X2C5"].entrante == "1092X3C5"


# ------------------------------------------------------------ estado de línea


C1_A = f"{SERV}19801C1"
C1_B = f"{SERV}19803C1"
C1_C = f"{SERV}19805C1"
HORARIO_C1 = horario(
    parada(C1_A, "18000", "08:00", 1, origen=True),
    parada(C1_A, "18002", "08:10", 2),
    parada(C1_A, "98305", "08:30", 3, final=True),
    parada(C1_B, "18000", "08:20", 1, origen=True),
    parada(C1_B, "18002", "08:30", 2),
    parada(C1_B, "98305", "08:50", 3, final=True),
    parada(C1_C, "18000", "09:00", 1, origen=True),
    parada(C1_C, "18002", "09:10", 2),
    parada(C1_C, "98305", "09:30", 3, final=True),
    parada(ENTRANTE, "A", "08:00", 1, origen=True),
    parada(ENTRANTE, "T", "08:40", 2, final=True),
)
VIAJES_C1 = P.indice_viajes(HORARIO_C1)
ESTADISTICAS = P.Artefacto.from_json(artefacto_json()).estadisticas


def obs(trip: str, stop: str, retraso_min: float, minuto: float) -> P.Observacion:
    return P.Observacion(trip, stop, round(retraso_min * 60), minuto)


def test_estado_de_linea_ultima_hora_otros_trenes_y_residuo() -> None:
    o = [
        obs(C1_A, "18002", 5, 8 * 60 + 12),  # cuenta: residuo 5 - 3 (mediana tren-parada)
        obs(C1_B, "18000", 1, 8 * 60 + 21),  # cuenta: sin estadística -> residuo 1 - 0
        obs(C1_C, "18000", 9, 8 * 60 + 50),  # el propio viaje: fuera
        obs(C1_A, "98305", 7, 8 * 60 + 31),  # parada final: fuera
        obs(C1_B, "18002", 70, 8 * 60 + 31),  # fuera de [-5, 60]
        obs(ENTRANTE, "A", 4, 8 * 60 + 10),  # otra familia
        obs(C1_A, "18000", 2, 7 * 60 + 40),  # más de una hora antes
        obs(C1_B, "18002", 2, 9 * 60 + 0),  # en asof o después
        obs("1092X99999C1", "18002", 2, 8 * 60 + 30),  # viaje sin horario
    ]
    m, r, n = P.estado_linea(
        o, VIAJES_C1, ESTADISTICAS, familia="C1", excluir_trip=C1_C, asof=9 * 60, fin=False
    )
    assert (m, r, n) == (3.0, 1.5, 2.0)


def test_estado_de_linea_vacio() -> None:
    assert P.estado_linea(
        [], VIAJES_C1, ESTADISTICAS, familia="C1", excluir_trip=C1_C, asof=540, fin=False
    ) == (None, None, 0.0)


# ---------------------------------------------------------------- entrante


ENLACE = P.Enlace(ENTRANTE, 8 * 60 + 40, 10.0)


def test_sin_enlace_solo_dice_que_no_hay_pareja() -> None:
    r = P.rasgos_entrante(None, [], VIAJES_C1, t0=530, asof=500)
    assert r["has_pair"] == 0.0
    assert r["inc_arrived"] is None and r["inc_live_delay"] is None


def test_entrante_en_ruta_da_el_ultimo_retraso_visto_hasta_asof() -> None:
    o = [
        obs(ENTRANTE, "A", 2, 8 * 60 + 1),
        obs(ENTRANTE, "A", 6, 8 * 60 + 5),
        obs(ENTRANTE, "A", 30, 8 * 60 + 25),
    ]
    r = P.rasgos_entrante(ENLACE, o, VIAJES_C1, t0=8 * 60 + 50, asof=8 * 60 + 20)
    assert r["lay"] == 10.0 and r["has_pair"] == 1.0
    assert (r["inc_live_delay"], r["inc_live_age"]) == (6.0, 45.0)
    assert r["inc_live_eta_late"] == 0.0 + max(0.0, 520 + 6 - 530 + 3)
    assert (r["inc_arrived"], r["inc_delay"], r["inc_prop"]) == (0.0, None, None)


def test_entrante_ya_llegado_estricto() -> None:
    o = [obs(ENTRANTE, "T", 12, 8 * 60 + 51)]
    llegado = P.rasgos_entrante(ENLACE, o, VIAJES_C1, t0=8 * 60 + 50, asof=8 * 60 + 53)
    assert (llegado["inc_arrived"], llegado["inc_delay"], llegado["inc_prop"]) == (1.0, 12.0, 5.0)
    aun_no = P.rasgos_entrante(ENLACE, o, VIAJES_C1, t0=9 * 60, asof=8 * 60 + 52)
    assert aun_no["inc_arrived"] == 0.0, "llegada real 08:52 + 1 min aún no ha pasado"


# ---------------------------------------------------------------- predictor


def predictor(
    observaciones: list[P.Observacion],
    ahora: float,
    *,
    rutas: frozenset[str] = frozenset({"C1", "C5"}),
    horario_dia: dict[str, list[ScheduledStop]] | None = None,
    **kw: Any,
) -> P.Predictor:
    viajes = P.indice_viajes(horario_dia or HORARIO_C1)
    return P.Predictor(
        P.Artefacto.from_json(artefacto_json()),
        viajes,
        P.emparejar_entrantes(viajes.values()),
        observaciones,
        fecha_servicio=FECHA,
        ahora_min=ahora,
        rutas=rutas,
        **kw,
    )


def _stop(trip: str, stop: str | None = "18002") -> ScheduledStop:
    return ScheduledStop(trip, "09:10:00", "C1", "X", False, stop_id=stop, seq=2)


def test_c1_predice_con_la_linea_y_redondea() -> None:
    p = predictor([obs(C1_A, "18002", 5, 8 * 60 + 40)], 8 * 60 + 45)
    assert p(_stop(C1_C)) == 4, "ls_mean 5 > 2 -> 4,2 -> 4"
    v = p.rasgos(C1_C, "18002")
    assert v is not None
    assert (v["familia_c"], v["tren_n"], v["dir"], v["hora"], v["mod"], v["seq"]) == (
        "C1",
        19805.0,
        1.0,
        9.0,
        550.0,
        2.0,
    )
    assert (v["dow"], v["hol_f"], v["fin_f"], v["daytype"]) == (2.0, 0.0, 0.0, 0.0)
    assert p(_stop(C1_C)) == 4, "segunda vez, de la caché"


def test_c1_sin_observaciones_de_la_linea_no_predice() -> None:
    assert predictor([], 8 * 60 + 45)(_stop(C1_C)) is None


def test_c1_mas_alla_de_su_antelacion_maxima_no_predice() -> None:
    p = predictor([obs(C1_A, "18002", 5, 8 * 60 + 20)], 8 * 60 + 25)
    assert p(_stop(C1_C)) is None, "sale de origen a las 09:00: 35 min > 30 del artefacto"
    tope = predictor([obs(C1_A, "18002", 5, 8 * 60 + 40)], 8 * 60 + 45, horizonte_max_min=10)
    assert tope(_stop(C1_C)) is None, "tope adicional de la ingesta"


def test_linea_no_habilitada_o_sin_modelo_no_predice() -> None:
    o = [obs(C1_A, "18002", 5, 8 * 60 + 40)]
    assert predictor(o, 8 * 60 + 45, rutas=frozenset({"C5"}))(_stop(C1_C)) is None
    assert predictor(o, 8 * 60 + 45)(_stop(f"{SERV}21001C7")) is None
    assert predictor(o, 8 * 60 + 45)(_stop(C1_C, None)) is None, "sin estación no hay rasgos"
    assert predictor(o, 8 * 60 + 45)(_stop(C1_C, "99999")) is None, "parada fuera de su recorrido"


def test_c5_con_el_entrante_en_vivo() -> None:
    o = [obs(ENTRANTE, "B", 8, 15 * 60 + 25)]
    p = predictor(o, 15 * 60 + 26, horario_dia=HORARIO_C5)
    s = ScheduledStop(SALIENTE, "16:10:00", "C5", "X", False, stop_id="B", seq=2)
    assert p(s) == 7
    assert predictor([obs(ENTRANTE, "B", 2, 15 * 60 + 25)], 15 * 60 + 26, horario_dia=HORARIO_C5)(s) == 1


def test_c5_sin_dato_del_entrante_no_predice() -> None:
    p = predictor([], 15 * 60 + 26, horario_dia=HORARIO_C5)
    assert p(ScheduledStop(SALIENTE, "16:10:00", "C5", "X", False, stop_id="B", seq=2)) is None
    sin_pareja = ScheduledStop(OTRA, "16:10:00", "C5", "X", False, stop_id="T", seq=1)
    assert (
        predictor([obs(ENTRANTE, "B", 8, 15 * 60 + 25)], 15 * 60 + 26, horario_dia=HORARIO_C5)(sin_pareja)
        is None
    )


def test_festivo_y_fin_de_semana() -> None:
    assert P.tipo_de_dia(date(2026, 11, 2), frozenset({date(2026, 11, 2)})) == (True, True, 2)
    assert P.tipo_de_dia(date(2026, 11, 21), frozenset()) == (False, True, 1)
    assert P.tipo_de_dia(date(2026, 11, 22), frozenset()) == (False, True, 2)
    assert P.tipo_de_dia(FECHA, frozenset()) == (False, False, 0)


def test_rasgos_base_parada_fuera_del_viaje() -> None:
    assert P.rasgos_base(VIAJES_C1[C1_A], "nope", ESTADISTICAS, FECHA, frozenset()) is None
    assert P.familia_de("raro", "C9") == "C9"
