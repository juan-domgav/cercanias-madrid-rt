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


# --- Trenes especiales (RB-006) -----------------------------------------------

UNIVERSIDAD = Station("17009", "Universidad Cantoblanco", 40.5437, -3.7003)
ESPECIAL = {"t": None, "eta": "08:41", "route": "C4b", "dest": None, "train": "91887",
            "delay": None, "platform": "1", "in": 0, "live": True, "unconfirmed": False,
            "on_time": False, "special": True, "status": "STOPPED_AT", "at_terminus": False}


def _proximos_especial(**cambios: object) -> str:
    d = {**ESPECIAL, **cambios}
    boards = {"stations": {"17009": {"destinations": [], "departures": [d]}}}
    r = handle_update(_msg("/proximos Universidad Cantoblanco"),
                      BotContext(stations=[UNIVERSIDAD], boards=boards))
    assert r.reply is not None
    return r.reply.text


def test_especial_parado_se_pinta_como_en_la_web() -> None:
    t = _proximos_especial()
    assert "tren especial" in t
    assert "en el andén" in t
    assert "Andén <b>1</b>" in t, "el anden se conserva"
    assert "C4b tren 91887" in t
    assert "sin datos en vivo" not in t, "esta localizado"
    assert "<b>08:41</b>" not in t, "sin hora de salida inventada"
    assert "→ ?" not in t


def test_especial_en_camino_dice_llegando() -> None:
    t = _proximos_especial(status="IN_TRANSIT_TO")
    assert "llegando" in t
    assert "en el andén" not in t


def test_especial_sin_anden_no_lo_inventa() -> None:
    assert "Andén —" in _proximos_especial(platform=None)


def test_especial_en_cabecera_avisa_de_posible_llegada() -> None:
    assert "final de línea" in _proximos_especial(at_terminus=True)
    assert "final de línea" not in _proximos_especial()


def test_especial_suprimido() -> None:
    assert "SUPRIMIDO" in _proximos_especial(cancelled=True)


def test_salida_estimada_desde_el_entrante_se_marca_como_estimada() -> None:
    """RB-007: la vuelta en cabecera aun no esta en el feed; se estima."""
    boards = {"stations": {"17004": {"destinations": ["Parla"], "departures": [
        {"t": "16:03", "eta": "16:12", "route": "C4b", "dest": "Parla", "train": "20476",
         "delay": None, "estimated_delay": 9, "delay_source": "inbound_estimate",
         "platform": None, "in": 46, "live": False},
        {"t": "16:18", "eta": "16:18", "route": "C4b", "dest": "Parla", "train": "20478",
         "delay": None, "estimated_delay": 0, "delay_source": "inbound_estimate",
         "platform": None, "in": 52, "live": False},
    ]}}}
    r = _run(_msg("/proximos Tres Cantos"), boards)
    assert r.reply is not None
    t = r.reply.text
    assert "≈ +9 min (estimado)" in t
    assert "≈ en hora (estimado)" in t
    assert "<b>≈ 16:12</b> <s>16:03</s>" in t
    assert "· sin datos en vivo" not in t, "no es 'sin datos' si hay estimado"
    assert "+9 min ·" not in t.replace("≈ +9 min (estimado)", ""), "nunca como retraso en vivo"


def test_viaje_no_publicado_no_dice_sin_datos() -> None:
    boards = {"stations": {"17004": {"destinations": ["Parla"], "departures": [
        {"t": "16:18", "eta": "16:18", "route": "C4b", "dest": "Parla", "train": "20478",
         "delay": None, "platform": None, "in": 52, "live": False,
         "realtime_status": "not_published"},
        {"t": "15:40", "eta": "15:40", "route": "C4b", "dest": "Parla", "train": "20476",
         "delay": None, "platform": None, "in": 14, "live": False,
         "realtime_status": "no_data"},
    ]}}}
    r = _run(_msg("/proximos Tres Cantos"), boards)
    assert r.reply is not None
    lineas = r.reply.text.split("\n")
    l20478 = next(x for x in lineas if "20478" in x)
    l20476 = next(x for x in lineas if "20476" in x)
    assert "programado · Renfe aún no lo publica" in l20478
    assert "sin datos en vivo" not in l20478
    assert "sin datos en vivo" in l20476, "dentro de la ventana, faltar SI es anomalia"


# --- RB-017: la lista corta no esconde la próxima salida de ningún sentido ------


def _chamartin_bot() -> dict:
    otros = ["Alcalá de Henares", "Madrid-Aeropuerto T4", "Aranjuez"]
    salidas = [
        {"t": f"13:{43 + i // 3}", "eta": f"13:{43 + i // 3}", "route": "C3", "dest": otros[i % 3],
         "train": f"2200{i}", "delay": 0, "platform": "3", "in": i // 3, "live": True}
        for i in range(8)
    ]
    salidas.append({"t": "13:47", "eta": "13:47", "route": "C4b", "dest": "Parla", "train": "20460",
                    "delay": 0, "platform": "6", "in": 4, "live": True})
    salidas.append({"t": "13:48", "eta": "13:48", "route": "C3", "dest": "Aranjuez", "train": "22099",
                    "delay": 0, "platform": "3", "in": 5, "live": True})
    salidas.append({"t": "14:55", "eta": "14:55", "route": "C4b", "dest": "Colmenar Viejo",
                    "train": "20555", "delay": None, "platform": None, "in": 72, "live": False})
    return {"stations": {"17000": {"destinations": [*otros, "Colmenar Viejo", "Parla"],
                                   "departures": salidas}}}


CHAMARTIN = Station("17000", "Madrid-Chamartín-Clara Campoamor", 40.4721, -3.6825)


def test_proximos_en_chamartin_no_esconde_el_20460_a_parla() -> None:
    r = handle_update(_msg("/proximos chamartin"),
                      BotContext(stations=[CHAMARTIN], boards=_chamartin_bot()))
    assert r.reply is not None
    t = r.reply.text
    assert "20460" in t and "Parla" in t
    assert "20555" in t, "la próxima a Colmenar, aunque salga en 72 min"
    assert "22099" not in t, "Aranjuez ya tiene su próxima en la lista corta"
    # La lista detallada sigue siendo corta: 6 salidas con su línea de andén.
    assert t.count("Andén <b>") == 6


def test_sin_sentidos_ocultos_no_se_anade_nada() -> None:
    r = _run(_msg("/proximos Tres Cantos"))
    assert r.reply is not None
    assert "Siguiente por sentido" not in r.reply.text
