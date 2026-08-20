from __future__ import annotations

from cercanias_api.bot import BotContext, find_station, handle_update, normalize

from cercanias_domain.station_view import Station

SERV = "1020L"
TRES_CANTOS = Station("17004", "Tres Cantos", 40.6011, -3.7098)
COLMENAR = Station("17005", "Colmenar Viejo", 40.6452, -3.7766)
ATOCHA = Station("18000", "Madrid-Atocha Cercanías", 40.4067, -3.6903)
STATIONS = [TRES_CANTOS, COLMENAR, ATOCHA]

# Tablon ya calculado por la ingesta: el bot pinta, no calcula.
BOARDS = {"stations": {"17004": {
    "destinations": ["Colmenar Viejo", "Parla"],
    "departures": [
        {"t": "09:05", "eta": "09:08", "route": "C4b", "dest": "Colmenar Viejo",
         "train": "20432", "delay": 3, "platform": "3", "in": 8, "live": True},
        {"t": "09:07", "eta": "09:07", "route": "C4b", "dest": "Parla",
         "train": "20433", "delay": None, "platform": None, "in": 7, "live": False},
    ]}}}


def _msg(text: str = "", chat_id: int = 42, **extra: object) -> dict:
    m: dict = {"chat": {"id": chat_id}}
    if text:
        m["text"] = text
    m.update(extra)
    return {"message": m}


def _run(update: dict, boards: dict | None = None):
    return handle_update(update, BotContext(
        stations=STATIONS,
        boards=BOARDS if boards is None else boards,
    ))


def test_normalizacion_ignora_tildes_y_mayusculas() -> None:
    assert normalize("Madrid-Atocha Cercanías") == normalize("madrid atocha cercanias")


def test_busqueda_de_estacion_tolerante() -> None:
    assert find_station("tres cantos", STATIONS) is TRES_CANTOS
    assert find_station("TRES CANTOS", STATIONS) is TRES_CANTOS
    assert find_station("colmenar", STATIONS) is COLMENAR
    assert find_station("atocha", STATIONS) is ATOCHA
    assert find_station("zaragoza", STATIONS) is None


def test_start_ofrece_el_boton_de_ubicacion() -> None:
    r = _run(_msg("/start"))
    assert r.reply is not None
    assert "request_location" in str(r.reply.reply_markup)


def test_proximos_devuelve_anden_y_destino() -> None:
    r = _run(_msg("/proximos Tres Cantos"))
    assert r.reply is not None
    t = r.reply.text
    assert "Tres Cantos" in t
    assert "Colmenar Viejo" in t, "el destino tiene que verse"
    assert "Andén <b>3</b>" in t, "el anden es lo mas accionable"
    assert "+3 min" in t


def test_proximos_sin_datos_en_vivo_no_inventa_anden() -> None:
    """El segundo tren del tablon no esta localizado: no debe inventarse anden."""
    r = _run(_msg("/proximos Tres Cantos"))
    assert "Andén —" in r.reply.text
    assert "sin datos en vivo" in r.reply.text


def test_estacion_desconocida_no_falla() -> None:
    r = _run(_msg("/proximos Cuenca"))
    assert "No conozco" in r.reply.text
    assert "request_location" in str(r.reply.reply_markup)


def test_ubicacion_devuelve_estaciones_cercanas() -> None:
    r = _run(_msg(location={"latitude": 40.6015, "longitude": -3.7100}))
    assert r.reply is not None
    assert "Tres Cantos" in r.reply.text
    assert "/proximos" in r.reply.text, "debe dejar el siguiente paso a un toque"


def test_bug_registra_feedback() -> None:
    r = _run(_msg("/bug en Colmenar sale un tren ya pasado"))
    assert r.feedback is not None
    assert r.feedback.chat_id == 42
    assert "Colmenar" in r.feedback.text
    assert r.reply is not None


def test_bug_sin_texto_pide_detalle_y_no_registra() -> None:
    r = _run(_msg("/bug"))
    assert r.feedback is None
    assert "Cuéntame" in r.reply.text


def test_texto_muy_largo_se_recorta() -> None:
    """Un mensaje enorme no debe convertirse en un objeto enorme en el bucket."""
    r = _run(_msg("/bug " + "x" * 5000))
    assert r.feedback is not None
    assert len(r.feedback.text) <= 1000


def test_update_sin_mensaje_se_ignora() -> None:
    assert _run({"channel_post": {"text": "hola"}}).reply is None
    assert _run({}).reply is None
