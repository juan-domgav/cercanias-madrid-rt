"""Tablón por ventana + próxima salida por destino (RB-017).

Caso real: 04/11/2026 13:43, Madrid-Chamartín (17000). El tablón se cortaba en
10 salidas, que en una estación así cubren ~7 minutos (acababa en 13:50) y en
las que además ocupan sitio los trenes con retraso cuya hora ya pasó. El 20460
(C4b → Parla, 13:47, en hora) quedaba fuera del corte justo antes de salir, y el
filtro «→ Parla» de la web solo filtraba dentro de esas 10.
"""

from __future__ import annotations

from cercanias_domain.departures import (
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    board_departures,
    next_departures,
)

SERV = "1308X"
AHORA = 13 * 60 + 43  # 13:43
CHAMARTIN = "17000"
OTROS = ("Alcalá de Henares", "Madrid-Aeropuerto T4", "Aranjuez", "Villalba de Guadarrama")


def _p(num: str, hora: str, dest: str, ruta: str = "C4b") -> ScheduledStop:
    return ScheduledStop(f"{SERV}{num}{ruta}", hora, ruta, dest, False)


def _chamartin() -> tuple[list[ScheduledStop], dict[str, LiveTrain]]:
    """Doce salidas previstas antes de las 13:47 (tres con retraso cuya hora
    ya pasó), el 20460 a Parla a las 13:47, una salida cada 2 min hasta las 15:40
    y destinos que solo salen tarde."""
    horario: list[ScheduledStop] = []
    vivos: dict[str, LiveTrain] = {}
    # Tres retrasados con hora programada ya pasada: prevista 13:44-13:47.
    for i, (hora, retraso) in enumerate((("13:38", 6), ("13:39", 7), ("13:40", 7))):
        s = _p(f"2100{i}", f"{hora}:00", OTROS[i % 4], "C1")
        horario.append(s)
        vivos[s.trip_id] = LiveTrain(s.trip_id, retraso * 60, "5", CHAMARTIN)
    # Nueve en hora entre las 13:43 y las 13:46.
    for i in range(9):
        s = _p(f"2200{i}", f"13:{43 + i % 4}:00", OTROS[i % 4], "C3")
        horario.append(s)
        vivos[s.trip_id] = LiveTrain(s.trip_id, 0, "3", CHAMARTIN)
    # El tren del aviso.
    parla = _p("20460", "13:47:00", "Parla")
    horario.append(parla)
    vivos[parla.trip_id] = LiveTrain(parla.trip_id, 0, "6", CHAMARTIN)
    # Una salida cada 2 minutos, 13:48-15:40, a los destinos de siempre.
    for k, m in enumerate(range(13 * 60 + 48, 15 * 60 + 41, 2)):
        horario.append(_p(f"23{k:03d}", f"{m // 60:02d}:{m % 60:02d}:00", OTROS[k % 4], "C2"))
    # Colmenar: su próxima salida cae fuera de la ventana de 60 min pero dentro de 2 h.
    horario.append(_p("20555", "14:55:00", "Colmenar Viejo"))
    horario.append(_p("20557", "15:25:00", "Colmenar Viejo"))
    # Guadalajara: más allá del horizonte de 2 h, no entra.
    horario.append(_p("24001", "15:50:00", "Guadalajara", "C2"))
    horario.sort(key=lambda s: s.time_hhmmss)
    return horario, vivos


def _consulta(**cambios: object) -> DepartureQuery:
    base: dict[str, object] = {
        "now_minutes": AHORA, "service_prefix": SERV, "limit": 40, "board_window_minutes": 60,
    }
    base.update(cambios)
    return DepartureQuery(**base)  # type: ignore[arg-type]


def test_precondicion_el_corte_fijo_de_10_deja_fuera_al_20460() -> None:
    horario, vivos = _chamartin()
    clasico = board_departures(
        horario, vivos, DepartureQuery(now_minutes=AHORA, service_prefix=SERV, limit=10), CHAMARTIN
    )
    assert len(clasico) == 10
    assert "20460" not in {d.train_number for d in clasico}
    assert clasico[-1].expected_hhmm <= "13:47"


def test_el_20460_a_parla_sale_en_el_tablon_de_chamartin() -> None:
    horario, vivos = _chamartin()
    salidas = board_departures(horario, vivos, _consulta(), CHAMARTIN)
    parla = next(d for d in salidas if d.train_number == "20460")
    assert parla.destination == "Parla"
    assert parla.expected_hhmm == "13:47"
    assert parla.delay_minutes == 0


def test_entran_todas_las_salidas_de_la_ventana_y_en_orden() -> None:
    horario, vivos = _chamartin()
    salidas = board_departures(horario, vivos, _consulta(limit=200), CHAMARTIN)
    en_ventana = [d for d in salidas if d.minutes_until <= 60]
    todas = next_departures(horario, vivos, _consulta(limit=200, board_window_minutes=None))
    assert len(en_ventana) == sum(1 for d in todas if d.minutes_until <= 60)
    horas = [d.expected_hhmm for d in salidas]
    assert horas == sorted(horas)


def test_cada_destino_tiene_su_proxima_salida_aunque_caiga_fuera_de_la_ventana() -> None:
    horario, vivos = _chamartin()
    salidas = board_departures(horario, vivos, _consulta(), CHAMARTIN)
    colmenar = [d for d in salidas if d.destination == "Colmenar Viejo"]
    assert [d.train_number for d in colmenar] == ["20555"], "la próxima, y solo la próxima"
    assert colmenar[0].minutes_until > 60
    destinos = {d.destination for d in salidas}
    assert {"Parla", "Colmenar Viejo", *OTROS} <= destinos


def test_mas_alla_del_horizonte_de_2_h_no_se_garantiza_nada() -> None:
    horario, vivos = _chamartin()
    salidas = board_departures(horario, vivos, _consulta(), CHAMARTIN)
    assert "Guadalajara" not in {d.destination for d in salidas}


def test_el_tope_duro_se_respeta_y_conserva_la_proxima_por_destino() -> None:
    horario, vivos = _chamartin()
    salidas = board_departures(horario, vivos, _consulta(limit=15), CHAMARTIN)
    assert len(salidas) == 15
    trenes = {d.train_number for d in salidas}
    assert {"20460", "20555"} <= trenes
    horas = [d.expected_hhmm for d in salidas]
    assert horas == sorted(horas)


def test_la_proxima_por_destino_salta_un_suprimido() -> None:
    horario, vivos = _chamartin()
    primero = next(s for s in horario if s.destination == "Colmenar Viejo")
    vivos[primero.trip_id] = LiveTrain(primero.trip_id, None, None, None, cancelled=True)
    salidas = board_departures(horario, vivos, _consulta(), CHAMARTIN)
    colmenar = [d.train_number for d in salidas if d.destination == "Colmenar Viejo"]
    assert colmenar == ["20557"], "se garantiza la próxima que SÍ sale"


def test_sin_ventana_el_comportamiento_clasico_no_cambia() -> None:
    horario, vivos = _chamartin()
    todas = next_departures(horario, vivos, _consulta(limit=500, board_window_minutes=None))
    clasico = next_departures(horario, vivos, DepartureQuery(now_minutes=AHORA, service_prefix=SERV))
    assert clasico == todas[:6]


def test_con_especiales_tienen_prioridad_y_el_tope_sigue_siendo_duro() -> None:
    horario, vivos = _chamartin()
    especial = "SPECIAL_10_91887C4B"
    vivos[especial] = LiveTrain(especial, None, "1", CHAMARTIN, status="STOPPED_AT")
    salidas = board_departures(horario, vivos, _consulta(limit=15), CHAMARTIN)
    assert len(salidas) == 15
    assert salidas[0].special
    assert {"20460", "20555"} <= {d.train_number for d in salidas}


def test_con_destino_pedido_la_ventana_tambien_aplica() -> None:
    horario, vivos = _chamartin()
    salidas = board_departures(horario, vivos, _consulta(destination="Colmenar Viejo"), CHAMARTIN)
    assert [d.train_number for d in salidas] == ["20555"]

def test_la_proxima_por_destino_salta_un_tren_ya_salido() -> None:
    """Un tren con ``minutes_until < 0`` no cubre el sentido (RB-018).

    El margen de gracia lo deja en el tablón, pero la garantía de «próxima por
    destino» debe apuntar a la siguiente útil. ``minutes_until == 0`` sí cubre.
    """
    horario, vivos = _chamartin()
    primero = next(s for s in horario if s.destination == "Colmenar Viejo")
    assert primero.trip_id.endswith("20555C4b")
    # Hora prevista hace 1 min → minutes_until = -1 (dentro del margen de gracia).
    horario[horario.index(primero)] = ScheduledStop(
        primero.trip_id, f"{AHORA // 60:02d}:{(AHORA % 60) - 1:02d}:00",
        primero.route, primero.destination, False,
    )
    vivos[primero.trip_id] = LiveTrain(primero.trip_id, 0, "3", CHAMARTIN)

    salidas = board_departures(horario, vivos, _consulta(), CHAMARTIN)
    colmenar = [d for d in salidas if d.destination == "Colmenar Viejo"]
    nums = [d.train_number for d in colmenar]
    assert "20555" in nums and next(d.minutes_until for d in colmenar if d.train_number == "20555") < 0
    assert "20557" in nums, "la próxima útil queda garantizada aunque caiga fuera de la ventana"

    # ``minutes_until == 0`` sí cubre: no hace falta garantizar la siguiente.
    horario[horario.index(next(s for s in horario if s.trip_id.endswith("20555C4b")))] = ScheduledStop(
        primero.trip_id, f"{AHORA // 60:02d}:{AHORA % 60:02d}:00",
        primero.route, primero.destination, False,
    )
    salidas0 = board_departures(horario, vivos, _consulta(), CHAMARTIN)
    col0 = [d.train_number for d in salidas0 if d.destination == "Colmenar Viejo"]
    assert col0 == ["20555"]

