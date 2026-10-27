from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cercanias_domain.historico import (
    EstadoMedida,
    Observacion,
    ParadaProgramada,
    familia,
    fecha_de_servicio,
    filas_viaje,
    hora_programada,
    huecos,
    normalizar,
    pasadas_congeladas,
    servicio_madrid,
)

MAD = ZoneInfo("Europe/Madrid")
D = date(2026, 10, 21)


def ts(h: int, m: int, s: int = 0, dia: date = D) -> datetime:
    return datetime(dia.year, dia.month, dia.day, h, m, s, tzinfo=MAD)


def ob(t: datetime, delay: int | None, stop: str | None, status: str) -> Observacion:
    return Observacion(t, delay, stop, status)


@pytest.mark.parametrize(
    ("fecha", "servicio"),
    [
        (date(2026, 10, 19), "1090L"),
        (date(2026, 10, 21), "1092X"),
        (date(2026, 9, 8), "1049M"),
        (date(2026, 9, 10), "1051J"),
        (date(2026, 10, 28), "1099X"),
        (date(2026, 10, 29), "1001J"),
    ],
)
def test_servicio_madrid(fecha: date, servicio: str) -> None:
    assert servicio_madrid(fecha) == servicio
    assert fecha_de_servicio(servicio, fecha + timedelta(days=3)) == fecha


def test_fecha_de_servicio_desconocida() -> None:
    assert fecha_de_servicio("1092Q", D) is None


def test_normalizar_normal() -> None:
    n = normalizar("1092X21875C7", ob(ts(22, 0), 120, "70112", "IN_TRANSIT_TO"))
    assert n is not None
    assert (n.fecha_servicio, n.trip_id, n.obs.delay) == (D, "1092X21875C7", 120)


def test_normalizar_vispera_reetiquetada_tras_medianoche() -> None:
    # 00:00 del 22/10: el tren del 21 aparece como 1093J con -86400 + 19 min.
    n = normalizar("1093J21875C7", ob(ts(0, 0, 39, D + timedelta(days=1)), -85260, "10000", "INCOMING_AT"))
    assert n is not None
    assert (n.fecha_servicio, n.trip_id, n.obs.delay) == (D, "1092X21875C7", 1140)


def test_normalizar_madrugada_siguiente_vista_de_noche() -> None:
    # 23:30 del 21: un tren de las 05:30 del 22, en cabecera, etiquetado 1092X con
    # +16 h (= ahora - 05:30 del 21 + ...). Tras corregir: -8 h, marcador de cabecera.
    n = normalizar("1092X21001C5", ob(ts(23, 30), 57_600, "18000", "STOPPED_AT"))
    assert n is not None
    assert (n.fecha_servicio, n.trip_id, n.obs.delay) == (D + timedelta(days=1), "1093J21001C5", -28_800)


def test_normalizar_negativo_tarde_mismo_dia() -> None:
    # 23:55 del 21 con -86400: sigue siendo del 21.
    n = normalizar("1092X21875C7", ob(ts(23, 55), -86_340, "10000", "IN_TRANSIT_TO"))
    assert n is not None
    assert (n.fecha_servicio, n.obs.delay) == (D, 60)


def test_normalizar_positivo_de_madrugada_es_del_dia() -> None:
    n = normalizar("1092X21001C5", ob(ts(4, 30), 50_000, "18000", "STOPPED_AT"))
    assert n is not None
    assert n.fecha_servicio == D


def test_normalizar_rezagado_de_la_vispera_por_la_manana() -> None:
    # 21/10 05:18: el de las 00:44 de la víspera, aún en el feed con -86400 + 4,5 h.
    n = normalizar("1092X20114C3", ob(ts(5, 18), -69_960, "1", "IN_TRANSIT_TO"))
    assert n is not None
    assert (n.fecha_servicio, n.trip_id, n.obs.delay) == (D - timedelta(days=1), "1091M20114C3", 16_440)


def test_normalizar_madrugada_con_etiqueta_del_dia_que_empieza() -> None:
    n = normalizar("1092X19793C5", ob(ts(2, 0), 0, "1", "STOPPED_AT"))
    assert n is not None and n.fecha_servicio == D
    vispera = normalizar("1091M19793C5", ob(ts(0, 30), 600, "1", "IN_TRANSIT_TO"))
    assert vispera is not None and vispera.fecha_servicio == D - timedelta(days=1)


def test_normalizar_especiales_y_otros_nucleos() -> None:
    especial = normalizar("SPECIAL_10_X", ob(ts(1, 0), 0, "1", "STOPPED_AT"))
    assert especial is not None and especial.fecha_servicio == D - timedelta(days=1)
    tarde = normalizar("SPECIAL_10_X", ob(ts(10, 0), 0, "1", "STOPPED_AT"))
    assert tarde is not None and tarde.fecha_servicio == D
    assert normalizar("SPECIAL_51_X", ob(ts(10, 0), 0, "1", "STOPPED_AT")) is None
    assert normalizar("5192X21875R1", ob(ts(10, 0), 0, "1", "STOPPED_AT")) is None
    assert normalizar("1092Q21875C7", ob(ts(10, 0), 0, "1", "STOPPED_AT")) is None


def test_normalizar_delay_nulo() -> None:
    n = normalizar("1092X21875C7", ob(ts(10, 0), None, "1", "STOPPED_AT"))
    assert n is not None and n.obs.delay is None


def test_hora_programada_pasadas_las_24() -> None:
    assert hora_programada(D, "24:14:00", MAD) == datetime(2026, 10, 22, 0, 14, tzinfo=MAD)
    assert hora_programada(D, " 7:05:30", MAD) == ts(7, 5, 30)


HORARIO = [
    ParadaProgramada(1, "A", "10:00:00"),
    ParadaProgramada(2, "B", "10:05:00"),
    ParadaProgramada(3, "C", "10:10:00"),
]


def _viaje() -> list[Observacion]:
    return [
        ob(ts(9, 50), 0, "A", "STOPPED_AT"),  # marcador
        ob(ts(9, 59), 300, "A", "STOPPED_AT"),  # contador de espera: marcador
        ob(ts(10, 1), 60, "A", "IN_TRANSIT_TO"),  # sale de A
        ob(ts(10, 3), 60, "B", "INCOMING_AT"),
        ob(ts(10, 6), 60, "B", "STOPPED_AT"),
        ob(ts(10, 7), 120, "B", "IN_TRANSIT_TO"),  # sale de B
        ob(ts(10, 11), 120, "C", "STOPPED_AT"),  # llega a C
        ob(ts(10, 30), 1300, "C", "STOPPED_AT"),  # aparcado: Renfe sigue contando
    ]


def test_filas_viaje_completo() -> None:
    a, b, c = filas_viaje(D, "1092X1C1", _viaje(), HORARIO, MAD)
    assert a.es_origen and not a.es_destino and c.es_destino
    assert all(f.tren_visto and f.salio and f.medido for f in (a, b, c))
    assert (a.retraso_s, a.retraso_primero_s, a.primero_marcador) == (60, 0, True)
    assert a.salida_obs == ts(10, 1) and a.retraso_salida_obs_s == 60
    assert a.llegada_obs is None
    assert (b.retraso_s, b.retraso_primero_s, b.primero_marcador, b.n_obs) == (120, 60, False, 3)
    assert b.llegada_obs == ts(10, 6) and b.retraso_llegada_obs_s == 60
    assert b.salida_obs == ts(10, 7) and b.retraso_salida_obs_s == 120
    assert b.ultimo_estado == "IN_TRANSIT_TO" and b.ultimo_ts == ts(10, 7)
    # En destino: el de la llegada, no el del contador de aparcado.
    assert c.retraso_s == 120 and c.retraso_salida_obs_s is None
    assert c.programada == ts(10, 10)


def test_filas_viaje_solo_en_cabecera_es_marcador() -> None:
    obs = _viaje()[:2]
    a, b, c = filas_viaje(D, "1092X1C1", obs, HORARIO, MAD)
    assert a.estado_medida is EstadoMedida.MARCADOR and a.retraso_s is None
    assert not a.salio and a.tren_visto
    assert b.estado_medida is EstadoMedida.NO_OBSERVADO and c.estado_medida is EstadoMedida.NO_OBSERVADO


def test_filas_viaje_desaparece_sin_confirmar() -> None:
    obs = _viaje()[:4]  # se deja de ver yendo a B
    _, b, c = filas_viaje(D, "1092X1C1", obs, HORARIO, MAD)
    assert b.estado_medida is EstadoMedida.SIN_CONFIRMAR and b.retraso_s == 60
    assert c.estado_medida is EstadoMedida.NO_OBSERVADO


def test_filas_viaje_destino_sin_llegar() -> None:
    obs = [*_viaje()[:6], ob(ts(10, 9), 120, "C", "IN_TRANSIT_TO")]
    c = filas_viaje(D, "1092X1C1", obs, HORARIO, MAD)[2]
    assert c.estado_medida is EstadoMedida.SIN_CONFIRMAR and c.retraso_s == 120


def test_filas_viaje_destino_retirado_al_llegar() -> None:
    # Renfe retira el viaje entrando en destino: último INCOMING_AT a su hora.
    obs = [*_viaje()[:6], ob(ts(10, 11), 120, "C", "INCOMING_AT")]
    c = filas_viaje(D, "1092X1C1", obs, HORARIO, MAD)[2]
    assert c.estado_medida is EstadoMedida.MEDIDO and c.retraso_s == 120
    # Desaparece 10 min antes de lo previsto: corte, no llegada.
    pronto = [*_viaje()[:6], ob(ts(10, 1), 120, "C", "INCOMING_AT")]
    assert filas_viaje(D, "1092X1C1", pronto, HORARIO, MAD)[2].estado_medida is EstadoMedida.SIN_CONFIRMAR


def test_filas_viaje_delay_nulo_sin_confirmar() -> None:
    obs = [ob(ts(10, 1), 60, "A", "IN_TRANSIT_TO"), ob(ts(10, 3), None, "B", "INCOMING_AT")]
    b = filas_viaje(D, "1092X1C1", obs, HORARIO, MAD)[1]
    assert b.estado_medida is EstadoMedida.SIN_CONFIRMAR and b.primero_marcador is False


def test_filas_viaje_no_visto() -> None:
    filas = filas_viaje(D, "1092X1C1", [], HORARIO, MAD)
    assert [f.estado_medida for f in filas] == [EstadoMedida.NO_OBSERVADO] * 3
    assert not any(f.tren_visto or f.salio for f in filas)


def test_filas_viaje_sin_horario() -> None:
    a, b, c = filas_viaje(D, "SPECIAL_10_X", _viaje(), None, MAD)
    assert [f.stop_id for f in (a, b, c)] == ["A", "B", "C"]
    assert a.seq is None and a.programada is None and a.es_origen
    assert a.estado_medida is EstadoMedida.MEDIDO and a.retraso_s == 60
    assert c.es_destino and c.retraso_s == 120


def test_filas_viaje_sin_horario_visto_ya_en_marcha() -> None:
    (b,) = filas_viaje(D, "SPECIAL_10_X", _viaje()[3:6], None, MAD)
    assert not b.es_origen and b.es_destino and b.stop_id == "B"
    assert b.estado_medida is EstadoMedida.MEDIDO  # última vista y STOPPED_AT: llegada


def test_huecos_y_familia() -> None:
    marcas = [ts(10, 0), ts(10, 4), ts(10, 12), ts(10, 13)]
    assert huecos(marcas, timedelta(minutes=5)) == [(ts(10, 4), ts(10, 12))]
    assert familia("1092X20531C4b") == "C4"
    assert familia("basura") is None


def test_pasadas_congeladas() -> None:
    t0 = ts(6, 0)
    m = timedelta(minutes=1)
    pasadas: list[tuple[datetime, str | None]] = [
        (t0, "a"),
        (t0 + 5 * m, "a"),  # racha "a" de 5 min: no es congelado
        (t0 + 6 * m, "b"),
        (t0 + 10 * m, "b"),
        (t0 + 16 * m, "b"),  # racha "b" de 10 min: se descartan las repeticiones
        (t0 + 17 * m, None),
        (t0 + 40 * m, None),  # sin firma: nunca
        (t0 + 41 * m, "c"),
    ]
    assert pasadas_congeladas(pasadas) == [False, False, False, True, True, False, False, False]
    assert pasadas_congeladas([]) == []
