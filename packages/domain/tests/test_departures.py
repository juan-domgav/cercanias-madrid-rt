from __future__ import annotations

from cercanias_domain.departures import (
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    board_departures,
    canonical_route,
    destinations_available,
    format_minutes,
    live_special_departures,
    next_departures,
    now_in_operating_day,
    parse_hhmm_to_minutes,
    train_number,
)

SERV = "1020L"


def _parada(num: str, hora: str, dest: str = "Parla", termina: bool = False) -> ScheduledStop:
    return ScheduledStop(f"{SERV}{num}C4b", hora, "C4b", dest, termina)


def test_numero_de_tren_desde_el_trip_id() -> None:
    assert train_number("1020L20432C4b") == "20432"
    assert train_number("1019D27446C4b") == "27446"


def test_horas_mayores_de_medianoche() -> None:
    """R-4: GTFS expresa los servicios de madrugada como 24:15, 25:03..."""
    assert parse_hhmm_to_minutes("25:14:00") == 1514
    assert format_minutes(1514) == "01:14", "debe presentarse como hora de reloj"


def test_tren_que_termina_aqui_no_es_una_salida() -> None:
    """En Colmenar la mitad de las paradas del horario son llegadas."""
    paradas = [_parada("20430", "09:00:00", termina=True),
               _parada("20432", "09:05:00")]
    salidas = next_departures(paradas, {}, DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))
    assert [d.train_number for d in salidas] == ["20432"]


def test_se_combina_horario_con_retraso_en_vivo() -> None:
    paradas = [_parada("20432", "09:05:00")]
    vivos = {f"{SERV}20432C4b": LiveTrain(f"{SERV}20432C4b", 180, "3")}
    d = next_departures(paradas, vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.scheduled_hhmm == "09:05"
    assert d.expected_hhmm == "09:08", "programada + retraso publicado"
    assert d.delay_minutes == 3
    assert d.platform == "3"
    assert d.located is True


def test_sin_tren_localizado_no_se_inventa_anden() -> None:
    """El anden solo existe en el feed en vivo. Mandar a alguien al anden
    equivocado es peor que decirle que no se sabe."""
    d = next_departures([_parada("20432", "09:05:00")], {},
                        DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.platform is None
    assert d.delay_minutes is None
    assert d.located is False
    assert d.expected_hhmm == d.scheduled_hhmm, "sin dato en vivo, la prevista es la programada"


def test_ordena_por_hora_prevista_no_por_programada() -> None:
    """Un tren con 20 min de retraso sale despues que otro puntual posterior."""
    paradas = [_parada("20430", "09:00:00"), _parada("20432", "09:10:00")]
    vivos = {f"{SERV}20430C4b": LiveTrain(f"{SERV}20430C4b", 1200, "1")}
    salidas = next_departures(paradas, vivos, DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))
    assert [d.train_number for d in salidas] == ["20432", "20430"]


def test_filtra_por_destino() -> None:
    """En Tres Cantos conviven los dos sentidos."""
    paradas = [_parada("1", "09:05:00", dest="Colmenar Viejo"),
               _parada("2", "09:06:00", dest="Parla")]
    salidas = next_departures(paradas, {}, DepartureQuery(
        now_minutes=9*60, service_prefix=SERV, destination="Colmenar Viejo"))
    assert len(salidas) == 1
    assert salidas[0].destination == "Colmenar Viejo"


def test_ignora_otros_dias_de_servicio() -> None:
    """El indice de calendario avanza a diario (ADR-012)."""
    ajena = ScheduledStop("1021M20432C4b", "09:05:00", "C4b", "Parla", False)
    assert next_departures([ajena], {}, DepartureQuery(now_minutes=9*60, service_prefix=SERV)) == ()


def test_margen_para_el_que_llega_corriendo() -> None:
    paradas = [_parada("20430", "09:00:00")]
    assert len(next_departures(paradas, {}, DepartureQuery(now_minutes=9*60+2, service_prefix=SERV))) == 1
    assert len(next_departures(paradas, {}, DepartureQuery(now_minutes=9*60+30, service_prefix=SERV))) == 0


def test_destinos_disponibles_excluye_los_que_terminan() -> None:
    paradas = [_parada("1", "09:00:00", dest="Colmenar Viejo"),
               _parada("2", "09:01:00", dest="Parla"),
               _parada("3", "09:02:00", dest="Colmenar Viejo", termina=True)]
    assert destinations_available(paradas, service_prefix=SERV) == ("Colmenar Viejo", "Parla")


# --- aviso del enlace de cabecera ---------------------------------------------


def _con_enlace(num: str, hora: str, entrante: str, dest: str = "Parla") -> ScheduledStop:
    # is_origin=True: el aviso de enlace SOLO aplica en el origen del viaje.
    return ScheduledStop(f"{SERV}{num}C4b", hora, "C4b", dest, False,
                         f"{SERV}{entrante}C4b", None, True, "17005")


def test_avisa_si_el_enlace_entrante_viene_tarde() -> None:
    """El tren que hara este servicio todavia no ha llegado a cabecera."""
    paradas = [_con_enlace("20432", "09:05:00", "20431")]
    vivos = {f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 1500, "1", "17005")}  # +25
    d = next_departures(paradas, vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 25
    assert d.at_risk is True


def test_el_aviso_NO_altera_la_hora_prevista() -> None:
    """Es un aviso, no una prediccion: parte del retraso lo absorbe la maniobra
    y a veces el servicio se suprime."""
    paradas = [_con_enlace("20432", "09:05:00", "20431")]
    vivos = {f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 1500, "1", "17005")}
    d = next_departures(paradas, vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.expected_hhmm == "09:05", "la hora prevista sigue siendo la programada"
    assert d.delay_minutes is None, "no se inventa un retraso propio"


def test_el_dato_propio_manda_sobre_el_del_enlace() -> None:
    """En cuanto el tren aparece con su retraso, el aviso del enlace sobra."""
    paradas = [_con_enlace("20432", "09:05:00", "20431")]
    vivos = {
        f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 1500, "1", "17005"),
        f"{SERV}20432C4b": LiveTrain(f"{SERV}20432C4b", 120, "3", "17005"),
    }
    d = next_departures(paradas, vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.delay_minutes == 2
    assert d.inbound_delay_minutes is None
    assert d.at_risk is False


def test_retraso_absorbido_por_la_maniobra_de_ese_enlace_no_avisa() -> None:
    """Sustituye a un test del umbral global de 8 min.

    Aquel fijaba una mediana de poblacion como umbral para CADA enlace, que es
    justo el error que encontro la revision adversarial: con 12 min de maniobra
    un retraso de 6 se absorbe entero y avisar seria una falsa alarma.
    """
    parada = ScheduledStop(f"{SERV}20432C4b", "09:05:00", "C4b", "Parla", False,
                           f"{SERV}20431C4b", 12, True, "17005")
    vivos = {f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 360, "1", "17005")}  # +6
    d = next_departures([parada], vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None
    assert d.at_risk is False


def test_sin_enlace_conocido_no_se_avisa() -> None:
    paradas = [_parada("20432", "09:05:00")]
    d = next_departures(paradas, {}, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None


def test_enlace_no_localizado_no_avisa() -> None:
    """Si el entrante tampoco se ve, no hay nada que decir: el silencio es
    preferible a insinuar un problema que no consta."""
    paradas = [_con_enlace("20432", "09:05:00", "20431")]
    d = next_departures(paradas, {}, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None


# --- umbral relativo a la maniobra de CADA enlace ------------------------------
# Hallazgo de la revision adversarial: un umbral global invertia la decision en
# los dos extremos del rango real de maniobras (0 a 40 min).


def _enlace(num: str, hora: str, entrante: str, slack: int | None) -> ScheduledStop:
    return ScheduledStop(f"{SERV}{num}C4b", hora, "C4b", "Parla", False,
                         f"{SERV}{entrante}C4b", slack, True, "17005")


def _vivo(entrante: str, minutos: int, next_stop: str | None = "17005") -> dict[str, LiveTrain]:
    tid = f"{SERV}{entrante}C4b"
    return {tid: LiveTrain(tid, minutos * 60, "1", next_stop)}


def test_maniobra_larga_absorbe_el_retraso_y_no_avisa() -> None:
    """35 min de margen y +10 de retraso: el tren llega con 25 de sobra.
    El umbral global de 8 min daba aqui una falsa alarma."""
    d = next_departures([_enlace("20432", "09:35:00", "20431", 35)], _vivo("20431", 10),
                        DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None
    assert d.at_risk is False


def test_maniobra_nula_avisa_con_retraso_pequeno() -> None:
    """Sin margen, +7 significa que el tren NO puede estar en el anden.
    El umbral global de 8 min guardaba silencio en este caso."""
    d = next_departures([_enlace("20432", "09:00:00", "20431", 0)], _vivo("20431", 7),
                        DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 7
    assert d.at_risk is True


def test_retraso_trivial_con_maniobra_cero_si_avisa() -> None:
    """Sustituye a un test que exigia silencio con +2 y maniobra 0.

    Ese silencio venia de comparar el retraso contra un umbral. Ahora se proyecta
    la fisica: el entrante llega +2, necesita 3 min minimos para dar la vuelta y
    no tiene maniobra programada, asi que la salida se va a ~5 min. Eso si le
    cambia la decision a quien espera.
    """
    d = next_departures([_enlace("20432", "09:00:00", "20431", 0)], _vivo("20431", 2),
                        DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 2


def test_sin_maniobra_conocida_se_usa_el_suelo() -> None:
    d = next_departures([_enlace("20432", "09:05:00", "20431", None)], _vivo("20431", 10),
                        DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 10


# --- hallazgos de la revision adversarial de grok ------------------------------


def test_parada_intermedia_no_hereda_el_aviso_de_cabecera() -> None:
    """El enlace solo significa algo en el ORIGEN del viaje.

    El generador copiaba `from` en las 10.098 paradas del viaje, de las que 9.020
    no eran origen. En una parada intermedia el tren ya circula y su propio
    retraso es el dato bueno; el enlace de una cabecera lejana no dice nada.
    """
    intermedia = ScheduledStop(f"{SERV}20432C4b", "09:05:00", "C4b", "Parla", False,
                               f"{SERV}20431C4b", 0, False, "17005")
    d = next_departures([intermedia], _vivo("20431", 30),
                        DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None


def test_retraso_propio_de_cero_no_silencia_el_aviso() -> None:
    """Un `delay` de 0 en un viaje que aun no ha salido es un marcador de
    posicion, no una lectura. Tomarlo por bueno pintaba "en hora" con el tren
    del enlace a 25 minutos, que es la peor mentira posible en un anden."""
    parada = _enlace("20432", "09:05:00", "20431", 0)
    vivos = dict(_vivo("20431", 25))
    vivos[f"{SERV}20432C4b"] = LiveTrain(f"{SERV}20432C4b", 0, None, "17005")
    d = next_departures([parada], vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 25, "el 0 propio no debe silenciar el aviso"


def test_retraso_medido_lejos_de_cabecera_tambien_cuenta() -> None:
    """Sustituye a un test que exigia que el entrante estuviera YA en cabecera.

    Aquella regla evitaba presentar un dato remoto como si fuera local, pero a
    costa de eliminar el aviso justo cuando sirve: un tren a +21 tres paradas
    antes llegara tarde igual. La proyeccion (llegada prevista + maniobra minima)
    no depende de donde se haya medido el retraso. Se comprobo en vivo el 21/08.
    """
    parada = _enlace("20432", "09:05:00", "20431", 0)
    lejos = _vivo("20431", 21, next_stop="97100")   # Pitis, aun en ruta
    d = next_departures([parada], lejos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 21


def test_maniobra_cero_avisa_justo_en_el_umbral() -> None:
    """Con maniobra 0 y +3, el andén está vacío a la hora de salida.
    El `>` estricto mas el suelo de 3 dejaba pasar exactamente este caso."""
    d = next_departures([_enlace("20432", "09:00:00", "20431", 0)], _vivo("20431", 3),
                        DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 3


# --- frontera de medianoche ---------------------------------------------------


def test_madrugada_se_expresa_en_la_jornada_anterior() -> None:
    """A las 00:36 el tren de las "24:36" esta saliendo AHORA, no dentro de un dia."""
    assert now_in_operating_day(0, 36) == 1476
    assert now_in_operating_day(0, 5) == 1445
    assert parse_hhmm_to_minutes("24:36:00") == 1476


def test_despues_del_corte_vuelve_al_reloj_normal() -> None:
    assert now_in_operating_day(4, 0) == 240
    assert now_in_operating_day(8, 30) == 510


def test_a_las_0005_se_ve_el_servicio_de_las_2408() -> None:
    """Regresion: con reloj de pared (ahora=5) el 24:08 quedaba a 1443 minutos
    y se descartaba. La viajera no veia un tren que salia en tres minutos."""
    nocturno = ScheduledStop(f"{SERV}79585C2", "24:08:00", "C2", "Atocha", False)
    ahora = now_in_operating_day(0, 5)
    salidas = next_departures([nocturno], {}, DepartureQuery(now_minutes=ahora, service_prefix=SERV))
    assert len(salidas) == 1
    assert salidas[0].scheduled_hhmm == "00:08", "se presenta como hora de reloj"
    assert salidas[0].minutes_until == 3


def test_antes_de_medianoche_el_servicio_nocturno_sigue_visible() -> None:
    nocturno = ScheduledStop(f"{SERV}79585C2", "24:08:00", "C2", "Atocha", False)
    salidas = next_departures([nocturno], {},
                              DepartureQuery(now_minutes=now_in_operating_day(23, 50),
                                             service_prefix=SERV))
    assert len(salidas) == 1
    assert salidas[0].minutes_until == 18


# --- servicios sin respaldo (posible supresion) --------------------------------


def test_salida_inminente_sin_rastro_se_marca_sin_confirmar() -> None:
    """Ni el tren ni su enlace aparecen y la hora se echa encima.

    No se afirma que este suprimido —sin feed de supresiones no se puede saber—
    pero presentar la hora limpia deja a alguien esperando un tren que quiza no
    exista.
    """
    parada = _enlace("20432", "09:05:00", "20431", 5)
    d = next_departures([parada], {}, DepartureQuery(now_minutes=9*60+2, service_prefix=SERV))[0]
    assert d.unconfirmed is True
    assert d.delay_minutes is None


def test_salida_lejana_sin_rastro_es_normal() -> None:
    """Faltando una hora, que no aparezca todavia no significa nada."""
    parada = _enlace("20432", "10:00:00", "20431", 5)
    d = next_departures([parada], {}, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.unconfirmed is False


def test_con_aviso_de_enlace_no_se_marca_sin_confirmar() -> None:
    """Si el enlace esta localizado y viene tarde, SI hay algo que respalda la
    hora: el tren existe y se sabe donde esta."""
    parada = _enlace("20432", "09:05:00", "20431", 0)
    d = next_departures([parada], _vivo("20431", 20),
                        DepartureQuery(now_minutes=9*60+2, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 20
    assert d.unconfirmed is False


def test_tren_localizado_no_se_marca_sin_confirmar() -> None:
    parada = _enlace("20432", "09:05:00", "20431", 5)
    vivos = dict(_vivo("20431", 1))
    vivos[f"{SERV}20432C4b"] = LiveTrain(f"{SERV}20432C4b", 60, "3", "17005")
    d = next_departures([parada], vivos, DepartureQuery(now_minutes=9*60+2, service_prefix=SERV))[0]
    assert d.unconfirmed is False


# --- caso real del 21/08: Colmenar 16:12 decia "En hora" -----------------------


def test_entrante_retrasado_en_ruta_si_avisa() -> None:
    """El tren del 16:12 de Colmenar iba +16 con tres paradas por delante.

    Programado: llega a cabecera 16:04, maniobra 8 min, sale 16:12.
    Real: +16, luego llega 16:20 y no puede salir a las 16:12.
    La regla anterior exigia que el entrante estuviera YA en la cabecera y
    silenciaba justo el caso para el que existe el aviso.
    """
    parada = ScheduledStop(f"{SERV}20478C4b", "16:12:00", "C4b", "Parla", False,
                           f"{SERV}20481C4b", 8, True, "17005")
    vivos = {f"{SERV}20481C4b": LiveTrain(f"{SERV}20481C4b", 16 * 60, None, "17001")}
    d = next_departures([parada], vivos,
                        DepartureQuery(now_minutes=16*60+11, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 16
    assert d.at_risk is True


def test_retraso_absorbido_por_maniobra_larga_sigue_sin_avisar() -> None:
    """+6 con 20 min de maniobra: llega de sobra. No se avisa."""
    parada = ScheduledStop(f"{SERV}20478C4b", "16:12:00", "C4b", "Parla", False,
                           f"{SERV}20481C4b", 20, True, "17005")
    vivos = {f"{SERV}20481C4b": LiveTrain(f"{SERV}20481C4b", 6 * 60, None, "17001")}
    d = next_departures([parada], vivos,
                        DepartureQuery(now_minutes=16*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None


def test_cero_de_marcador_no_se_presenta_como_en_hora() -> None:
    """El feed publica delay=0 para viajes que aun no han salido.

    Con el enlace en riesgo, decir "En hora" deja a alguien tranquilo en el anden
    mientras su tren esta a dieciseis minutos. Es el fallo que se vio en vivo.
    """
    parada = ScheduledStop(f"{SERV}20478C4b", "16:12:00", "C4b", "Parla", False,
                           f"{SERV}20481C4b", 8, True, "17005")
    vivos = {
        f"{SERV}20481C4b": LiveTrain(f"{SERV}20481C4b", 16 * 60, None, "17001"),
        f"{SERV}20478C4b": LiveTrain(f"{SERV}20478C4b", 0, "1", "17005"),
    }
    d = next_departures([parada], vivos,
                        DepartureQuery(now_minutes=16*60+11, service_prefix=SERV))[0]
    assert d.delay_minutes == 0
    assert d.inbound_delay_minutes == 16, "el 0 del feed no silencia el enlace"
    assert d.shows_on_time is False, "NO se puede decir 'en hora'"


def test_cero_real_sin_enlace_en_riesgo_si_es_en_hora() -> None:
    parada = ScheduledStop(f"{SERV}20478C4b", "16:12:00", "C4b", "Parla", False, None, None, True, "17005")
    vivos = {f"{SERV}20478C4b": LiveTrain(f"{SERV}20478C4b", 0, "1", "17005")}
    d = next_departures([parada], vivos,
                        DepartureQuery(now_minutes=16*60+11, service_prefix=SERV))[0]
    assert d.shows_on_time is True


# --- supresiones y recorridos acortados ---------------------------------------
# El feed publica CANCELED y SKIPPED. Antes se adivinaban.


def test_servicio_suprimido_se_marca_como_tal() -> None:
    parada = _enlace("20432", "09:05:00", "20431", 8)
    vivos = {f"{SERV}20432C4b": LiveTrain(f"{SERV}20432C4b", 0, "3", "17005", cancelled=True)}
    d = next_departures([parada], vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.cancelled is True
    assert d.shows_on_time is False, "un tren suprimido nunca esta 'en hora'"


def test_entrante_suprimido_es_mas_grave_que_un_retraso() -> None:
    """Si el tren que debia formar el servicio esta suprimido, no va a salir."""
    parada = _enlace("20432", "09:05:00", "20431", 8)
    vivos = {f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 0, None, "17005", cancelled=True)}
    d = next_departures([parada], vivos, DepartureQuery(now_minutes=9*60+3, service_prefix=SERV))[0]
    assert d.inbound_cancelled is True
    assert d.unconfirmed is False, "no es incertidumbre: es un dato publicado"
    assert d.shows_on_time is False


def test_recorrido_acortado_se_senala() -> None:
    """Caso de Tres Cantos: el tren termina antes de su destino anunciado."""
    parada = _enlace("20432", "09:05:00", "20431", 8)
    vivos = {f"{SERV}20432C4b": LiveTrain(f"{SERV}20432C4b", 120, "3", "17004", skips_stops=True)}
    d = next_departures([parada], vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.shortened is True
    assert d.delay_minutes == 2, "sigue habiendo retraso utilizable"


def test_tren_normal_no_hereda_ninguna_de_las_marcas() -> None:
    parada = _enlace("20432", "09:05:00", "20431", 8)
    vivos = {f"{SERV}20432C4b": LiveTrain(f"{SERV}20432C4b", 0, "3", "17005")}
    d = next_departures([parada], vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert (d.cancelled, d.inbound_cancelled, d.shortened) == (False, False, False)
    assert d.shows_on_time is True


# --- Trenes especiales (SPECIAL_10_*, Anexo A.2). Ver RB-006. ---------------

ESPECIAL = "SPECIAL_10_91887C4B"


def _especial(
    trip_id: str = ESPECIAL, parada: str | None = "17009", status: str | None = "STOPPED_AT"
) -> LiveTrain:
    return LiveTrain(trip_id, None, "1", parada, status=status)


def test_numero_de_tren_de_un_especial() -> None:
    """El recorte fijo de 5 caracteres daba "10" (el prefijo de nucleo)."""
    assert train_number(ESPECIAL) == "91887"
    assert train_number("SPECIAL_10_91244C7") == "91244"


def test_ruta_canonica_iguala_el_ramal_del_feed_al_del_horario() -> None:
    assert canonical_route("C4B") == "C4b"
    assert canonical_route("C10") == "C10"
    assert canonical_route("C4b") == "C4b"


def test_especial_no_aparece_en_next_departures() -> None:
    """La causa raiz: el tablon recorre el horario y el especial no esta en el."""
    paradas = [_parada("20431", "08:38:00", dest="Colmenar Viejo")]
    salidas = next_departures(
        paradas, {ESPECIAL: _especial()}, DepartureQuery(now_minutes=8 * 60 + 41, service_prefix=SERV)
    )
    assert all(d.train_number != "91887" for d in salidas)


def test_especial_parado_en_la_estacion_sale_en_su_tablon() -> None:
    (d,) = live_special_departures({ESPECIAL: _especial()}, "17009", 8 * 60 + 41)
    assert d.train_number == "91887"
    assert d.route == "C4b"
    assert d.special is True
    assert d.located is True
    assert d.unconfirmed is False
    assert d.delay_minutes is None, "sin horario no hay retraso que calcular"
    assert d.shows_on_time is False, "no se afirma 'en hora' sin dato"
    assert d.expected_hhmm == "08:41"
    assert d.minutes_until == 0
    assert d.platform == "1"
    assert d.status == "STOPPED_AT"
    assert d.destination is None


def test_especial_solo_en_su_parada_actual_o_proxima() -> None:
    vivos = {ESPECIAL: _especial(parada="17003", status="INCOMING_AT")}
    assert live_special_departures(vivos, "17009", 520) == ()
    (d,) = live_special_departures(vivos, "17003", 520)
    assert d.status == "INCOMING_AT"


def test_tren_normal_no_se_cuela_como_especial() -> None:
    vivos = {f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 60, "1", "17009")}
    assert live_special_departures(vivos, "17009", 520) == ()


def test_especial_sin_parada_no_va_a_ningun_tablon() -> None:
    assert live_special_departures({ESPECIAL: _especial(parada=None)}, "17009", 520) == ()


def test_especial_suprimido_conserva_la_marca() -> None:
    vivos = {ESPECIAL: LiveTrain(ESPECIAL, None, "1", "17009", cancelled=True)}
    (d,) = live_special_departures(vivos, "17009", 520)
    assert d.cancelled is True


def test_board_sin_especiales_es_identico_a_next_departures() -> None:
    paradas = [_parada("20431", "08:38:00"), _parada("20435", "09:03:00")]
    vivos = {f"{SERV}20435C4b": LiveTrain(f"{SERV}20435C4b", 120, "2", "17009")}
    q = DepartureQuery(now_minutes=8 * 60 + 41, service_prefix=SERV)
    assert board_departures(paradas, vivos, q, "17009") == next_departures(paradas, vivos, q)


def test_board_combina_especial_y_horario_sin_romper_el_casado() -> None:
    paradas = [_parada("20431", "08:38:00"), _parada("20435", "09:03:00")]
    vivos = {
        ESPECIAL: _especial(),
        f"{SERV}20435C4b": LiveTrain(f"{SERV}20435C4b", 120, "2", "17009"),
    }
    q = DepartureQuery(now_minutes=8 * 60 + 41, service_prefix=SERV)
    salidas = board_departures(paradas, vivos, q, "17009")
    assert [d.train_number for d in salidas] == ["20431", "91887", "20435"]
    assert salidas[0].unconfirmed is True, "el 20431 sigue sin rastro"
    assert salidas[2].delay_minutes == 2, "el casado por trip_id no cambia"
    assert not salidas[2].special


def test_board_el_especial_no_se_pierde_por_el_limite() -> None:
    paradas = [_parada(str(20400 + i), f"08:{42 + i}:00") for i in range(6)]
    q = DepartureQuery(now_minutes=8 * 60 + 41, service_prefix=SERV, limit=3)
    salidas = board_departures(paradas, {ESPECIAL: _especial()}, q, "17009")
    assert len(salidas) == 3
    assert salidas[0].train_number == "91887"
