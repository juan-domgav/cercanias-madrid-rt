from __future__ import annotations

from cercanias_domain.station_view import (
    Approach,
    Station,
    _Positioned,
    haversine_km,
    nearest_stations,
    trains_for_station,
)

# Coordenadas reales del catalogo generado en Fase 0.
TRES_CANTOS = Station("17004", "Tres Cantos", 40.6011, -3.7098)
COLMENAR = Station("17005", "Colmenar Viejo", 40.6448, -3.7657)
CHAMARTIN = Station("17000", "Madrid-Chamartín", 40.4721, -3.6828)
ATOCHA = Station("18000", "Madrid-Atocha Cercanías", 40.4067, -3.6903)
CATALOGO = [TRES_CANTOS, COLMENAR, CHAMARTIN, ATOCHA]


def _tren(trip_id: str, lat: float, lon: float, next_stop: str | None = None) -> _Positioned:
    return _Positioned(trip_id, "C4b", lat, lon, next_stop, None)


def test_haversine_contra_distancia_conocida() -> None:
    """Chamartin a Tres Cantos son ~14-15 km en linea recta."""
    d = haversine_km(CHAMARTIN.latitude, CHAMARTIN.longitude, TRES_CANTOS.latitude, TRES_CANTOS.longitude)
    assert 13.0 < d < 16.0, d


def test_estacion_mas_cercana_a_una_posicion() -> None:
    """El usuario esta en el anden de Tres Cantos."""
    cercanas = nearest_stations(40.6015, -3.7100, CATALOGO, limit=2)
    assert cercanas[0][0].stop_id == TRES_CANTOS.stop_id
    assert cercanas[0][1] < 0.5, "deberia estar a menos de 500 m"
    assert cercanas[1][0].stop_id == COLMENAR.stop_id


def test_orden_por_distancia() -> None:
    cercanas = nearest_stations(ATOCHA.latitude, ATOCHA.longitude, CATALOGO, limit=4)
    distancias = [d for _, d in cercanas]
    assert distancias == sorted(distancias)
    assert cercanas[0][0].stop_id == ATOCHA.stop_id


def test_tren_con_proxima_parada_en_mi_estacion_va_primero() -> None:
    """Es el unico caso en el que se puede afirmar el sentido con certeza."""
    trenes = [
        _tren("lejos", CHAMARTIN.latitude, CHAMARTIN.longitude, next_stop="17001"),
        _tren("viene", 40.58, -3.71, next_stop=TRES_CANTOS.stop_id),
    ]
    resultado = trains_for_station(trenes, TRES_CANTOS)
    assert resultado[0].trip_id == "viene"
    assert resultado[0].approach is Approach.NEXT_STOP


def test_sentido_por_geometria_sin_direction_id() -> None:
    """Al sur de Tres Cantos y con parada en Tres Cantos = va hacia Colmenar.

    No se usa `direction_id`: en el GTFS de Renfe es una convencion inventada
    (R-2), asi que se deduce de la posicion relativa.
    """
    desde_madrid = _tren("sur", 40.55, -3.71, next_stop=TRES_CANTOS.stop_id)
    desde_colmenar = _tren("norte", 40.63, -3.74, next_stop=TRES_CANTOS.stop_id)
    resultado = {t.trip_id: t for t in trains_for_station([desde_madrid, desde_colmenar], TRES_CANTOS)}
    assert resultado["sur"].is_south_of_station is True, "viene de Madrid, va a Colmenar"
    assert resultado["norte"].is_south_of_station is False, "viene de Colmenar, va a Madrid"


def test_trenes_lejanos_se_descartan() -> None:
    """Un tren a 40 km tardara tanto que mostrarlo es ruido."""
    lejano = _tren("lejisimos", 40.05, -3.70, next_stop="37012")
    assert trains_for_station([lejano], TRES_CANTOS, radius_km=30.0) == ()


def test_se_adjunta_el_retraso_conocido() -> None:
    trenes = [_tren("t1", 40.58, -3.71, next_stop=TRES_CANTOS.stop_id)]
    resultado = trains_for_station(trenes, TRES_CANTOS, delays_by_trip={"t1": 380})
    assert resultado[0].delay_seconds == 380
    assert resultado[0].delay_minutes == 6


def test_sin_retraso_conocido_no_se_inventa() -> None:
    trenes = [_tren("t1", 40.58, -3.71, next_stop=TRES_CANTOS.stop_id)]
    resultado = trains_for_station(trenes, TRES_CANTOS)
    assert resultado[0].delay_seconds is None
    assert resultado[0].delay_minutes is None


def test_sin_proxima_parada_el_sentido_queda_desconocido() -> None:
    """Preferimos 'no lo se' a adivinar: sin topologia de linea no hay certeza."""
    trenes = [_tren("t1", 40.58, -3.71, next_stop=None)]
    assert trains_for_station(trenes, TRES_CANTOS)[0].approach is Approach.UNKNOWN
