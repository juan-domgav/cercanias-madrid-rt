"""Una invocación programada: sondea, publica y notifica. Luego termina.

Es el equivalente en la nube de ``make mvp``, pero con la forma que exige C-7:
un tick programado cada minuto —la granularidad mínima de Cloud Scheduler y
EventBridge Scheduler— con **2-3 pasadas internas separadas 20 s** dentro de la
misma invocación. Así se cubre la detección con más resolución que la que permite
el planificador, sin pagar por un proceso permanente.

El problema que resuelve este módulo y que el MVP no tenía
--------------------------------------------------------

``AlertCooldown`` y el rumbo viven en memoria. En un bucle largo eso basta; en
invocaciones programadas que arrancan de cero **cada minuto**, el enfriamiento
estaría siempre vacío y Telegram enviaría el mismo aviso una vez por minuto —
exactamente el spam que el enfriamiento existe para evitar. Es el riesgo R-3 de
la arquitectura v2 manifestándose.

Solución provisional y honesta: el estado de avisos se guarda junto al snapshot,
en el mismo almacenamiento de objetos. No necesita infraestructura nueva y
sobrevive a los arranques en frío. Lo correcto a medio plazo es DynamoDB o
Firestore con TTL, pero eso es WP-3 y no bloquea esta primera versión.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from cercanias_domain.board_status import contradicts_live, departure_status
from cercanias_domain.departures import (
    PUBLISH_WINDOW_MINUTES as DEFAULT_PUBLISH_WINDOW_MINUTES,
)
from cercanias_domain.departures import (
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    board_departures,
    destinations_available,
    now_in_operating_day,
)
from cercanias_domain.incidents import AlertCooldown, AlertPolicy, format_incident_message
from cercanias_domain.trip_id import parse_trip_id
from cercanias_notifiers_telegram.telegram_notifier import (
    TelegramConfigError,
    TelegramNotifier,
    TelegramOptions,
    TelegramSendError,
)
from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository
from cercanias_storage.publishers import GcsSnapshotPublisher, publisher_from_env
from cercanias_storage.secrets import read_secret
from cercanias_worker.config import WorkerConfig
from cercanias_worker.mvp import SnapshotStore, run_cycle

SNAPSHOT_KEY = os.environ.get("SNAPSHOT_KEY", "snapshot.json")
ALERT_STATE_KEY = os.environ.get("ALERT_STATE_KEY", "alert-state.json")
#: Estado operativo: no es personal, pero tampoco tiene por que ser publico.
PRIVATE_BUCKET = os.environ.get("PRIVATE_BUCKET", "")

#: Archivo historico. Se guarda una entrada por PASADA (no por invocacion), lo
#: que da resolucion de 20 s: suficiente para ver el momento exacto del relevo en
#: cabecera, que es lo que hay que medir.
#:
#: Un objeto por pasada, en vez de anadir a un fichero unico: en almacenamiento
#: de objetos "anadir" significa leer y reescribir entero, y el coste crece con
#: el cuadrado del dia.
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archivo")
ARCHIVE_ENABLED = os.environ.get("ARCHIVE_ENABLED", "1") == "1"

#: Cuota minima de vehiculos para considerar que un segundo dia de servicio esta
#: realmente en circulacion y no es ruido de material aparcado.
SECOND_SERVICE_MIN_SHARE = 0.15

#: Antiguedad a partir de la cual un snapshot conservado SI merece avisar al
#: usuario. El nucleo desaparece del feed en ~5 % de los sondeos (ADR-011), asi
#: que marcar "datos no actualizados" en cuanto falla una pasada llena la app de
#: avisos sobre datos de hace 80 segundos, que son perfectamente utiles. El aviso
#: tiene que significar "esto ya no sirve", no "ha fallado un sondeo".
STALE_WARNING_SECONDS = float(os.environ.get("STALE_WARNING_SECONDS", "240"))
BOARDS_KEY = os.environ.get("BOARDS_KEY", "boards.json")
#: Familias de linea en las que NO se estima la salida desde el enlace entrante
#: (RB-007). Por defecto, todas las que NO superan la validacion contra el
#: archivo del 21/10/2026 (retraso real de la vuelta a +-3 min del estimado en
#: >= 90 % de n >= 30 enlaces observados). Solo C1 la supera (91 %, n=74); C4
#: (C4a 36 %, C4b 47 %) no: alli Renfe cambia material en cabecera y el tren que
#: llega no es el que sale. La tabla completa esta en RB-007. Se sobreescribe con
#: la variable de entorno (lista separada por comas; vacia = estimar en todas).
DEFAULT_INBOUND_ESTIMATE_EXCLUDED_ROUTES = "C2,C3,C4,C5,C7,C8,C9,C10"
#: Ventana de publicacion de Renfe (RB-007): un viaje sin datos cuya salida de
#: ORIGEN esta a mas de esto se muestra como "Renfe aun no lo publica".
PUBLISH_WINDOW_MINUTES = int(
    os.environ.get("PUBLISH_WINDOW_MINUTES", str(DEFAULT_PUBLISH_WINDOW_MINUTES))
)
INBOUND_ESTIMATE_EXCLUDED_ROUTES = frozenset(
    r.strip()
    for r in os.environ.get(
        "INBOUND_ESTIMATE_EXCLUDED_ROUTES", DEFAULT_INBOUND_ESTIMATE_EXCLUDED_ROUTES
    ).split(",")
    if r.strip()
)
#: Familias en las que se estima la salida de un tren que sigue en cabecera antes
#: de su hora (RB-009). Lista PERMITIDA, vacia por defecto: con el archivo del
#: 19-22/10/2026 ninguna linea llega a >= 80 % a +-3 min (5 y 10 min antes).
ORIGIN_ESTIMATE_ROUTES = frozenset(
    r.strip() for r in os.environ.get("ORIGIN_ESTIMATE_ROUTES", "").split(",") if r.strip()
)
#: Los horarios viven en el bucket, un fichero por dia de servicio
#: (``horarios/1030J.json``). Estaban empaquetados en la imagen y caducaron sin
#: que nadie lo viera: cada regeneracion exigia reconstruir el contenedor, asi
#: que en la practica no se regeneraban. Desde el bucket basta con subirlos.
SCHEDULES_PREFIX = os.environ.get("SCHEDULES_PREFIX", "horarios")
MADRID_TZ = ZoneInfo("Europe/Madrid")

#: C-7: 2-3 pasadas internas por invocación, separadas 20 s.
PASSES_PER_INVOCATION = int(os.environ.get("PASSES_PER_INVOCATION", "3"))
SECONDS_BETWEEN_PASSES = float(os.environ.get("SECONDS_BETWEEN_PASSES", "20"))

EXIT_NO_DATA = 4
#: Anexo A.2: viajes especiales, sin dia de servicio ni horario estatico.
SPECIAL_TRIP_PREFIX = "SPECIAL_"
#: Un id de servicio es nucleo (2) + indice de dia (2) + letra: ``1085X``.
SERVICE_INDEX_END = 4


def _log(severity: str, message: str, **fields: Any) -> None:
    """Linea JSON que Cloud Logging interpreta como entrada estructurada.

    ``severity`` es el campo que Cloud Logging lee para el nivel, asi que una
    alerta basada en logs puede filtrar ``severity>=ERROR`` sin parsear texto.
    """
    entry = {"severity": severity, "message": message, "component": "cloud-tick", **fields}
    print(json.dumps(entry, ensure_ascii=False, default=str), file=sys.stderr, flush=True)


def _safe_publish(publisher: Any, key: str, payload: bytes, **kwargs: Any) -> str | None:
    """Publica sin que un fallo de almacenamiento tumbe el resto del tick."""
    try:
        ref: str = publisher.publish(key, payload, **kwargs)
        return ref
    except Exception as exc:
        _log("ERROR", f"no se pudo publicar {key}", key=key, error=repr(exc))
        return None


#: Letras del dia en el tripId de Renfe (L=lunes ... D=domingo). El indice numerico
#: envuelve 99→01 saltando 00; la letra avanza con el calendario (ADR-012).
_SERVICE_DAY_LETTERS = "LMXJVSD"
_MAX_SERVICE_INDEX = 99


def _next_service_id(service: str) -> str | None:
    """Siguiente id de servicio Madrid: indice +1 (99→01) y letra del dia siguiente."""
    if len(service) < SERVICE_INDEX_END + 1:
        return None
    index_text, letter = service[2:4], service[4]
    if not index_text.isdigit() or letter not in _SERVICE_DAY_LETTERS:
        return None
    next_index = 1 if int(index_text) >= _MAX_SERVICE_INDEX else int(index_text) + 1
    next_letter = _SERVICE_DAY_LETTERS[
        (_SERVICE_DAY_LETTERS.index(letter) + 1) % len(_SERVICE_DAY_LETTERS)
    ]
    return f"{service[:2]}{next_index:02d}{next_letter}"


def _schedule_days_left(publisher: Any, service: str) -> int | None:
    """Dias de horario por delante del servicio en curso, si es barato saberlo.

    Solo se calcula si el publicador sabe listar claves (``list_keys``). Se
    camina la cadena de ids COMPLETOS (numero + letra): contar solo el entero
    con modulo falla en la envoltura 1099→1001 y confunde ficheros viejos con
    la misma cifra y otra letra (``1010V`` junto a ``1010S``). Ver RB-004.
    """
    lister = getattr(publisher, "list_keys", None)
    if lister is None or _next_service_id(service) is None:
        return None
    try:
        keys = lister(f"{SCHEDULES_PREFIX}/")
    except Exception:
        return None
    presentes = {
        key.rsplit("/", 1)[-1].removesuffix(".json")
        for key in keys
        if key.rsplit("/", 1)[-1].endswith(".json")
    }
    if service not in presentes:
        return 0
    dias = 0
    actual = service
    while True:
        siguiente = _next_service_id(actual)
        if siguiente is None or siguiente not in presentes:
            return dias
        dias += 1
        actual = siguiente


class _MemoryStore(SnapshotStore):
    """Reutiliza la lógica de último bueno conocido sin tocar disco.

    ``previous_good`` es imprescindible y no un adorno: cada invocación arranca
    con el proceso vacío, así que sin sembrar el último bueno desde el bucket la
    lógica de ADR-011 no protege de nada. Una pasada durante una desaparición del
    núcleo publicaría el snapshot vacío encima del bueno, y la app se quedaría
    sin trenes hasta el minuto siguiente. Ocurrió en producción antes de esto.
    """

    def __init__(self, previous_good: dict[str, Any] | None = None) -> None:
        self.latest: dict[str, Any] | None = None
        self._last_good = previous_good

    def _write(self, payload: dict[str, object]) -> None:
        self.latest = dict(payload)


def _snapshot_a_publicar(
    actual: dict[str, Any], previo: dict[str, Any] | None
) -> dict[str, Any]:
    """Decide que snapshot se sirve y si hay que avisar de que esta viejo.

    ADR-011: mejor servir datos de hace un minuto, etiquetados, que un mapa
    vacio; un mapa vacio no parece un fallo, parece que no hay trenes. Pero el
    aviso solo se pone cuando los datos conservados ya NO sirven: con
    desapariciones en el ~5 % de los sondeos, avisar por cada fallo llena la app
    de advertencias sobre datos de 80 segundos y se acaba ignorando el aviso.
    """
    if actual.get("vehicles") or not previo:
        return actual
    salida = dict(previo)
    salida["stale_since"] = actual.get("generated_at")
    edad = _snapshot_age_seconds(salida)
    if edad is None or edad >= STALE_WARNING_SECONDS:
        salida["degraded"] = True
        salida["reason"] = f"sin datos nuevos del nucleo desde hace {int(edad or 0) // 60} min"
        print(f"PASADA VACIA: snapshot conservado y MARCADO viejo ({edad or -1:.0f}s)")
    else:
        salida.pop("degraded", None)
        salida.pop("reason", None)
        print(f"pasada vacia: se conserva el anterior ({edad:.0f}s), aun vigente")
    return salida


def _snapshot_age_seconds(snapshot: dict[str, Any]) -> float | None:
    """Antiguedad real de los datos que se van a servir."""
    marca = snapshot.get("generated_at")
    if not isinstance(marca, str):
        return None
    try:
        return (datetime.now(tz=UTC) - datetime.fromisoformat(marca)).total_seconds()
    except ValueError:
        return None


def _read_previous_snapshot(publisher: Any) -> dict[str, Any] | None:
    """Último snapshot publicado, si lo hay y es legible."""
    raw = publisher.read(SNAPSHOT_KEY)
    if not raw:
        return None
    try:
        previous = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(previous, dict) or not previous.get("vehicles"):
        # Un snapshot vacío o degradado no sirve como "último bueno".
        return None
    return previous


def _private_publisher(publisher: Any) -> Any:
    if PRIVATE_BUCKET:
        return GcsSnapshotPublisher(PRIVATE_BUCKET)
    return publisher


def _load_cooldown(publisher: Any) -> AlertCooldown:
    """Reconstruye el enfriamiento desde el almacenamiento de objetos."""
    cooldown = AlertCooldown()
    raw = publisher.read(ALERT_STATE_KEY)
    if not raw:
        return cooldown
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        # Estado corrupto: mejor arrancar limpio que abortar el tick. Como mucho
        # se manda un aviso de mas, que es preferible a no publicar el snapshot.
        print("aviso: alert-state.json ilegible; se reinicia el enfriamiento", file=sys.stderr)
        return cooldown
    return AlertCooldown.from_dict(state.get("lines") or {})


def _save_cooldown(publisher: Any, cooldown: AlertCooldown) -> None:
    state = {"lines": cooldown.to_dict()}
    publisher.publish(
        ALERT_STATE_KEY,
        json.dumps(state, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )


def _load_day_schedule(publisher: Any, service: str) -> dict[str, list[ScheduledStop]]:
    """Horario completo de un dia de servicio, por estacion."""
    if not service:
        return {}
    raw = publisher.read(f"{SCHEDULES_PREFIX}/{service}.json")
    if not raw:
        return {}
    try:
        datos = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log("ERROR", f"horario {service} ilegible", service=service, error=repr(exc))
        return {}
    if not isinstance(datos, dict):
        _log("ERROR", f"horario {service} con forma inesperada", service=service)
        return {}
    estaciones = datos.get("stations") or {}
    # Salida de cada viaje en su origen (seq 1): la ventana de publicacion de
    # Renfe se mide desde ahi, no desde la estacion del tablon.
    origenes = {
        d["trip"]: d["t"] for paradas in estaciones.values() for d in paradas if d.get("seq") == 1
    }
    # Estacion de origen de cada viaje: si el tren sigue ahi, aun no ha salido y
    # el delay del feed no es una medida (RB-009).
    paradas_origen = {
        d["trip"]: stop_id
        for stop_id, paradas in estaciones.items()
        for d in paradas
        if d.get("seq") == 1
    }
    return {
        stop_id: [
            ScheduledStop(
                d["trip"], d["t"], d.get("route", ""), d.get("dest"),
                bool(d.get("terminates")), d.get("from"), d.get("slack"),
                d.get("seq") == 1, paradas_origen.get(d["trip"]),
                inbound_ambiguous=bool(d.get("amb")),
                origin_departure_hhmmss=origenes.get(d["trip"]),
            )
            for d in paradas
        ]
        for stop_id, paradas in estaciones.items()
    }


def _snapshot_is_fresh(snapshot: dict[str, Any], ahora: datetime) -> bool:
    """¿Sirve el snapshot para proyectar? Mismo guard que los especiales (PR #7)."""
    return _specials_observed_at(snapshot, ahora) is not None


def _specials_observed_at(snapshot: dict[str, Any], ahora: datetime) -> datetime | None:
    """Momento de la observacion si los especiales del snapshot siguen vigentes.

    Un especial no tiene horario: lo unico que lo sostiene es haberlo visto. Si
    el snapshot es uno CONSERVADO (pasada vacia, ADR-011) o es mas viejo que
    ``STALE_WARNING_SECONDS``, mostrarlo seria un fantasma: un tren "en el anden"
    a las 23:59 porque se le vio a las 08:41. Los programados no tienen ese
    problema: su hora sale del horario y caducan solos. ``None`` = no mostrar.
    """
    if snapshot.get("degraded"):
        return None
    marca = snapshot.get("generated_at")
    if not isinstance(marca, str):
        return None
    try:
        visto = datetime.fromisoformat(marca)
    except ValueError:
        return None
    if visto.tzinfo is None:
        return None
    if abs((ahora - visto).total_seconds()) >= STALE_WARNING_SECONDS:
        return None
    return visto.astimezone(MADRID_TZ)


def _is_special(trip_id: str) -> bool:
    parsed = parse_trip_id(trip_id)
    return parsed is not None and parsed.is_special


def _service_days(vehiculos: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Dia de servicio mayoritario y dias cuyo horario hay que cargar."""
    conteo: dict[str, int] = {}
    for v in vehiculos:
        tid = v.get("trip_id") or ""
        # Los especiales no llevan dia de servicio: su "prefijo" seria "SPECI",
        # que no es un horario. Con muchos especiales se elegia "SPECI" como dia
        # y los tablones salian sin horario (RB-006).
        if tid.startswith(SPECIAL_TRIP_PREFIX):
            continue
        p = tid[:5]
        if p:
            conteo[p] = conteo.get(p, 0) + 1
    servicio = max(conteo, key=lambda k: conteo[k]) if conteo else ""

    # En la transicion de jornada pueden convivir dos dias de servicio: trenes de
    # madrugada del dia anterior y material ya publicado para el siguiente. Elegir
    # solo el mayoritario hacia desaparecer la otra mitad del tablon sin avisar.
    total = sum(conteo.values()) or 1
    candidatos = [p for p, n in sorted(conteo.items(), key=lambda kv: -kv[1])[:2]
                  if n / total >= SECOND_SERVICE_MIN_SHARE]
    return servicio, candidatos


def _build_boards(
    snapshot: dict[str, Any],
    publisher: Any,
    cancelled: frozenset[str] = frozenset(),
    skipped: dict[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    """Precalcula el tablon de salidas de cada estacion.

    Se hace AQUI, en Python probado, y no en el JavaScript de la pagina: tener la
    misma logica dos veces garantiza que se separen. Ademas mantiene la ruta de
    lectura como un fichero estatico tras CDN (C-7): la pagina solo pinta, no
    calcula, y el coste no crece con los lectores.
    """
    vehiculos = snapshot.get("vehicles", [])
    live = {
        v["trip_id"]: LiveTrain(
            v["trip_id"], v.get("delay_seconds"), v.get("platform"), v.get("next_stop_id"),
            cancelled=v["trip_id"] in cancelled,
            skips_stops=bool((skipped or {}).get(v["trip_id"])),
            status=v.get("status"),
        )
        for v in vehiculos
        if v.get("trip_id")
    }
    servicio, candidatos = _service_days(vehiculos)

    ahora = datetime.now(tz=MADRID_TZ)
    # Coordenada de jornada de servicio, no reloj de pared: a las 00:36 hay que
    # comparar contra un horario que llama "24:36" a ese mismo tren.
    minutos = now_in_operating_day(ahora.hour, ahora.minute)
    estaciones: dict[str, Any] = {}

    # Especiales: solo si el snapshot es fresco, y con la hora en que se vieron.
    visto = _specials_observed_at(snapshot, ahora)
    fresco = _snapshot_is_fresh(snapshot, ahora)
    if visto is None:
        live_tablon = {k: t for k, t in live.items() if not _is_special(k)}
        minuto_visto = None
    else:
        live_tablon = live
        minuto_visto = now_in_operating_day(visto.hour, visto.minute)

    dia: dict[str, list[ScheduledStop]] = {}
    for cand in candidatos:
        for stop_id, paradas in _load_day_schedule(publisher, cand).items():
            dia.setdefault(stop_id, []).extend(paradas)
    if len(candidatos) > 1:
        print(f"transicion de jornada: se cargan {candidatos}")

    # Los especiales (SPECIAL_10_*) no estan en el horario: su parada actual o
    # proxima tiene tablon aunque el horario del dia no la incluya. Ver RB-006.
    paradas_especiales = {
        t.next_stop_id for t in live_tablon.values()
        if t.next_stop_id and _is_special(t.trip_id)
    }
    for stop_id in sorted(set(dia) | paradas_especiales):
        horario = dia.get(stop_id, [])
        if not horario and stop_id not in paradas_especiales:
            continue
        salidas = board_departures(
            horario, live_tablon,
            DepartureQuery(
                now_minutes=minutos, service_prefix=servicio, limit=10,
                # Estimar desde el entrante SOLO con datos frescos (RB-007).
                estimate_from_inbound=fresco,
                estimate_excluded_routes=INBOUND_ESTIMATE_EXCLUDED_ROUTES,
                origin_estimate_routes=ORIGIN_ESTIMATE_ROUTES,
                # Sin snapshot fresco no sabemos que hay en el feed: no se
                # afirma "no publicado".
                publish_window_minutes=PUBLISH_WINDOW_MINUTES if fresco else None,
            ),
            stop_id,
            specials_observed_minutes=minuto_visto,
        )
        estaciones[stop_id] = {
            "destinations": list(destinations_available(horario, service_prefix=servicio)),
            "departures": [
                {
                    # Un especial no tiene hora de horario: null, no una inventada.
                    "t": None if d.special else d.scheduled_hhmm,
                    "eta": d.expected_hhmm,
                    "route": d.route,
                    "dest": d.destination,
                    "train": d.train_number,
                    "delay": d.delay_minutes,
                    # RB-007: retraso ESTIMADO desde el entrante, ya incluido en
                    # "eta". "delay_source" dice de donde sale el retraso:
                    # "live", "inbound_estimate" o null.
                    "estimated_delay": d.estimated_delay_minutes,
                    "delay_source": d.delay_source,
                    # live > inbound_estimate > not_published > no_data (RB-007).
                    "realtime_status": d.realtime_status,
                    "platform": d.platform,
                    "in": d.minutes_until,
                    "live": d.located,
                    # Aviso, no prediccion: el tren que hara este servicio viene
                    # con retraso, pero la hora prevista no se toca.
                    "inbound_delay": d.inbound_delay_minutes,
                    "unconfirmed": d.unconfirmed,
                    "on_time": d.shows_on_time,
                    "cancelled": d.cancelled,
                    "inbound_cancelled": d.inbound_cancelled,
                    "shortened": d.shortened,
                    "special": d.special,
                    "status": d.status,
                    "at_terminus": d.at_terminus,
                    # RB-009: en el feed pero aun en cabecera sin salir.
                    "pending_departure": d.pending_departure,
                }
                for d in salidas
            ],
        }
        # RB-008: el estado presentable lo decide UNA funcion (la misma que usa el
        # bot). La web solo pinta "display"; no recalcula etiquetas ni avisos.
        for fila in estaciones[stop_id]["departures"]:
            fila["display"] = departure_status(fila).to_json()
    # Los horarios se generan por lotes de dias (ADR-012: el indice de calendario
    # avanza a diario). Cuando el servicio en circulacion cae fuera del lote, los
    # tablones salen vacios SIN error: es el mismo fallo silencioso de siempre, y
    # hay que gritarlo antes de que llegue al anden.
    #
    # La salud cuenta SOLO salidas del horario. Un especial no demuestra que haya
    # horario: con horarios/<dia>.json ausente, un unico especial ponia
    # stations_with_departures a 1 y no_departures a False, y RB-001 no saltaba.
    con_salidas = sum(
        1 for e in estaciones.values() if any(not d["special"] for d in e["departures"])
    )
    # Cuenta salidas (no trenes): un mismo viaje estimado aparece en varias
    # estaciones de su recorrido.
    estimadas = sum(
        1 for e in estaciones.values() for d in e["departures"]
        if d["delay_source"] == "inbound_estimate"
    )
    no_publicadas = sum(
        1 for e in estaciones.values() for d in e["departures"]
        if d["realtime_status"] == "not_published"
    )
    # Dentro de la ventana y sin rastro: la anomalia de verdad (para el vigia).
    ausentes_en_ventana = sum(
        1 for e in estaciones.values() for d in e["departures"]
        if fresco and not d["special"] and d["realtime_status"] == "no_data"
    )
    # Invariante RB-008: una fila en vivo nunca lleva aviso de entrante ni
    # "sin confirmar". Tiene que ser 0; si no, el tablon contradice al feed.
    contradictorias = sum(
        1 for e in estaciones.values() for d in e["departures"] if contradicts_live(d)
    )
    if contradictorias:
        _log(
            "ERROR",
            f"{contradictorias} salidas en vivo con aviso que las contradice (RB-008)",
            alert="departures_contradicting_live", count=contradictorias,
        )
    especiales_en_tablon = sum(
        1 for e in estaciones.values() for d in e["departures"] if d["special"]
    )
    schedule_missing = bool(servicio) and not dia
    # Sin ningun viaje con dia de servicio (p. ej. solo circulan especiales) no
    # hay horario que cargar y las otras alertas callan: se grita aparte.
    service_unknown = not servicio and any(v.get("trip_id") for v in vehiculos)
    if service_unknown:
        _log(
            "ERROR",
            "AVISO GRAVE: hay trenes en el feed pero ninguno con dia de servicio "
            "(solo especiales o ids desconocidos). Los tablones salen sin horario.",
            alert="service_unknown_with_vehicles", vehicles=len(vehiculos),
        )
    if schedule_missing:
        _log(
            "ERROR",
            f"AVISO GRAVE: no existe horarios/{servicio}.json. El dia de servicio en "
            "circulacion no esta cubierto y la app se queda SIN TRENES. Revisar el job "
            "cercanias-horarios (ver docs/DESPLIEGUE.md).",
            alert="schedule_missing", service=servicio,
        )
    elif servicio and con_salidas == 0:
        _log(
            "ERROR",
            f"AVISO GRAVE: servicio {servicio} sin ninguna salida en {len(estaciones)} estaciones.",
            alert="no_departures", service=servicio, stations=len(estaciones),
        )
    return {
        "generated_at": ahora.isoformat(),
        "service": servicio,
        #: Estaciones con al menos una salida DEL HORARIO (los especiales no cuentan).
        "stations_with_departures": con_salidas,
        "specials_on_boards": especiales_en_tablon,
        "health": {
            "schedule_missing": schedule_missing,
            "schedule_days_left": _schedule_days_left(publisher, servicio) if servicio else None,
            "no_departures": bool(servicio) and con_salidas == 0,
            "service_unknown_with_vehicles": service_unknown,
            "specials_hidden_stale": visto is None and any(_is_special(k) for k in live),
            "departures_estimated_from_inbound": estimadas,
            "inbound_estimates_disabled_stale": not fresco,
            "departures_not_published": no_publicadas,
            "departures_missing_in_window": ausentes_en_ventana,
            "departures_contradicting_live": contradictorias,
            "departures_pending_departure": sum(
                1 for e in estaciones.values() for d in e["departures"] if d["pending_departure"]
            ),
        },
        "stations": estaciones,
    }


def _archive_pass(publisher: Any, snapshot: dict[str, Any], momento: datetime) -> None:
    """Guarda una foto compacta de la pasada para el analisis posterior.

    Solo lo que hace falta para responder dos preguntas que hoy no se pueden
    responder: cuanto retraso sobrevive al cambio de sentido en cabecera, y con
    que frecuencia un servicio programado sencillamente no circula.

    Se escribe en el bucket PRIVADO: no es dato personal, pero es material de
    trabajo y no tiene por que servirse en la web.
    """
    if not ARCHIVE_ENABLED:
        return
    vehiculos = snapshot.get("vehicles") or []
    if not vehiculos:
        return
    compacto = {
        "ts": momento.isoformat(),
        "n": len(vehiculos),
        # Listas posicionales en vez de objetos: reduce el tamano a la mitad y
        # una semana de archivo se lee entera sin pensar en el coste.
        "cols": ["trip", "delay", "next_stop", "status", "platform", "lat", "lon"],
        "trains": [
            [
                v.get("trip_id"), v.get("delay_seconds"), v.get("next_stop_id"),
                v.get("status"), v.get("platform"), v.get("lat"), v.get("lon"),
            ]
            for v in vehiculos
        ],
    }
    clave = f"{ARCHIVE_PREFIX}/{momento:%Y-%m-%d}/{momento:%H%M%S}.json"
    try:
        publisher.publish(
            clave,
            # Salto final para que concatenar objetos siga siendo NDJSON valido:
            # `gcloud storage cat` con comodin no separa los ficheros, y sin esto
            # dos pasadas se funden en una linea ilegible.
            (json.dumps(compacto, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
            content_type="application/x-ndjson",
        )
    except Exception as exc:
        # El archivo es material de estudio: que falle no debe tumbar la ingesta.
        print(f"aviso: no se pudo archivar {clave}: {exc}", file=sys.stderr)


def _build_notifier() -> TelegramNotifier:
    """Notificador real si hay token; dry-run si no.

    El token se resuelve en ejecucion y no montado como variable de entorno: si
    se monta, Cloud Run se niega a ARRANCAR el contenedor cuando el secreto no
    resuelve, y un problema de notificaciones deja la aplicacion sin datos.
    """
    token = read_secret("TELEGRAM_BOT_TOKEN", secret_env_var="TELEGRAM_BOT_TOKEN_SECRET")
    if not token:
        print("Telegram en dry-run: no hay token disponible", file=sys.stderr)
        return TelegramNotifier(token=None, chat_id=None, options=TelegramOptions(dry_run=True))
    try:
        return TelegramNotifier(token=token)
    except TelegramConfigError as exc:
        print(f"Telegram en dry-run: {exc.args[0].split('.')[0]}", file=sys.stderr)
        return TelegramNotifier(token=None, chat_id=None, options=TelegramOptions(dry_run=True))


def _publish_boards(
    publisher: Any,
    salida: dict[str, Any],
    cancelled: frozenset[str],
    skipped: dict[str, frozenset[str]],
) -> None:
    """Construye y publica los tablones; nunca lanza.

    Un fallo aqui (horario corrupto, bug, GCS caido) no debe saltarse los avisos
    de Telegram que vienen despues en ``main``.
    """
    try:
        boards = _build_boards(salida, publisher, cancelled, skipped)
    except Exception as exc:
        _log("ERROR", "fallo construyendo los tablones", error=repr(exc))
    else:
        if _safe_publish(
            publisher,
            BOARDS_KEY,
            json.dumps(boards, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
        ):
            print(f"tablones publicados: {len(boards['stations'])} estaciones "
                  f"(servicio {boards['service']})")


def main() -> int:
    config = WorkerConfig()
    publisher = publisher_from_env()
    notifier = _build_notifier()
    policy = AlertPolicy()
    estado = _private_publisher(publisher)
    cooldown = _load_cooldown(estado)

    # El estado de rumbo sí es intra-invocación: con 3 pasadas hay 2
    # oportunidades de calcularlo. Persistirlo entre invocaciones es WP-3.
    state = InMemoryCurrentStateRepository()
    previo = _read_previous_snapshot(publisher)
    if previo:
        print(f"ultimo bueno conocido: {previo.get('generated_at')} "
              f"({len(previo.get('vehicles', []))} trenes)")
    store = _MemoryStore(previous_good=previo)

    ultimo = None
    archivadas = 0
    for i in range(1, PASSES_PER_INVOCATION + 1):
        ultimo = run_cycle(config, store, state)
        print(
            f"pasada {i}/{PASSES_PER_INVOCATION}: trenes={ultimo.madrid_vehicles} "
            f"retrasos={ultimo.madrid_delays} rumbos={ultimo.with_bearing} "
            f"salud={ultimo.health.worst} incidencias={len(ultimo.incidents)}"
        )
        if store.latest:
            _archive_pass(estado, store.latest, datetime.now(tz=MADRID_TZ))
            archivadas += 1
        if i < PASSES_PER_INVOCATION:
            time.sleep(SECONDS_BETWEEN_PASSES)

    if ultimo is None or store.latest is None:
        print("ninguna pasada produjo snapshot", file=sys.stderr)
        return EXIT_NO_DATA

    salida = _snapshot_a_publicar(store.latest, previo)

    ref = _safe_publish(
        publisher,
        SNAPSHOT_KEY,
        json.dumps(salida, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )
    if ref:
        print(f"snapshot publicado en {ref} ({len(salida.get('vehicles', []))} trenes)")

    _publish_boards(publisher, salida, ultimo.cancelled_trips, ultimo.skipped_stops)
    if ARCHIVE_ENABLED:
        print(f"pasadas archivadas: {archivadas}")

    ahora = datetime.now(tz=UTC)
    notificables = [inc for inc in ultimo.incidents if policy.should_notify(inc)]
    nuevas = [inc for inc in notificables if cooldown.should_alert(inc, ahora)]
    if nuevas:
        try:
            outcome = notifier.send(format_incident_message(tuple(nuevas)))
            for inc in nuevas:
                cooldown.record(inc, ahora)
            try:
                _save_cooldown(estado, cooldown)
            except Exception as exc:
                _log("ERROR", "no se pudo guardar el enfriamiento", error=repr(exc))
            print(f"Telegram: {outcome.detail} ({len(nuevas)} lineas)")
        except TelegramSendError as exc:
            # Un fallo de notificación no debe invalidar el snapshot ya
            # publicado: los lectores importan más que los suscriptores.
            print(f"Telegram fallo (snapshot ya publicado): {exc}", file=sys.stderr)
    else:
        print(f"sin avisos nuevos ({len(notificables)} en enfriamiento)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
