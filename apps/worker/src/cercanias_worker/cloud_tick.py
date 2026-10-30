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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from cercanias_connectors_renfe.alerts import (
    AlertsFetchError,
    alerts_hash,
    fetch_alerts_payload,
    parse_madrid_alerts,
)
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
from cercanias_domain.prevision import (
    Prediction,
    PredictionTable,
    day_type,
    load_prediction_table,
    operating_date,
)
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


def _flag(nombre: str) -> bool:
    """Bandera de entorno APAGADA salvo que valga exactamente ``"1"``."""
    return os.environ.get(nombre, "0") == "1"


#: RB-014. Anade ``canc`` (supresiones) y ``skip`` (paradas omitidas) a cada
#: objeto de ``archivo/<dia>/``. Apagado por defecto: Juan decide cuando.
ARCHIVAR_CANCELACIONES = _flag("ARCHIVAR_CANCELACIONES")
#: RB-014. Descarga ``alerts.json`` una vez por invocacion y lo archiva en
#: ``archivo/alerts/<dia>/<hhmmss>.json`` SOLO cuando cambia. Apagado por defecto.
ARCHIVAR_AVISOS = _flag("ARCHIVAR_AVISOS")
ALERTS_ARCHIVE_PREFIX = os.environ.get("ALERTS_ARCHIVE_PREFIX", f"{ARCHIVE_PREFIX}/alerts")
#: Estado en el bucket privado (mismo patron que alert-state.json):
#: ``{"hash", "key", "written_at", "stale_warned_at"}``. Si se pierde o se
#: corrompe, lo unico que pasa es que el siguiente tick reescribe los avisos.
ALERTS_STATE_KEY = os.environ.get("ALERTS_STATE_KEY", "alerts-state.json")
#: El feed de avisos lleva congelado desde el 7-oct-2026: se avisa (WARNING
#: ``alerts_feed_stale``) cuando su cabecera supera esta edad, como mucho una
#: vez por hora para no inundar Cloud Logging con un aviso por minuto.
ALERTS_STALE_HOURS = float(os.environ.get("ALERTS_STALE_HOURS", "6"))
ALERTS_STALE_WARN_EVERY = timedelta(hours=1)

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
#: Retraso PREVISTO por el historico (RB-013) para trenes que Renfe aun no publica.
#: Lista PERMITIDA de familias, vacia por defecto: sin configurar no cambia nada.
#: Backtest del 26/10/2026 (prueba 19-25/10): C1 MAE 1,65 vs 3,03 y 79,8 % a
#: +-2 min; C5 3,14 vs 5,70 y 62,1 %. El resto no llega al 60 % a +-2 min.
PREVISTO_ROUTES = frozenset(
    r.strip() for r in os.environ.get("PREVISTO_ROUTES", "").split(",") if r.strip()
)
#: Artefacto (tabla de medianas) en el bucket PRIVADO; se genera offline con
#: ``scripts/construir_previsto.py`` y se sube a mano (RB-013).
PREVISTO_KEY = os.environ.get("PREVISTO_KEY", "prevision/previsto.json")
#: Un artefacto mas viejo que esto no se usa: los horarios y el comportamiento
#: de la red cambian y una mediana antigua deja de describir al tren de hoy.
PREVISTO_MAX_AGE_DAYS = float(os.environ.get("PREVISTO_MAX_AGE_DAYS", "21"))
#: Festivos adicionales a los del artefacto (``2026-11-09,2026-12-07``).
PREVISTO_FESTIVOS_ENV = os.environ.get("PREVISTO_FESTIVOS", "")
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


def _parse_holidays(raw: str) -> frozenset[date]:
    fechas: set[date] = set()
    for bruto in raw.split(","):
        trozo = bruto.strip()
        if not trozo:
            continue
        try:
            fechas.add(date.fromisoformat(trozo))
        except ValueError:
            _log("WARNING", f"PREVISTO_FESTIVOS: fecha ignorada {trozo!r}", alert="prediction_bad_holiday")
    return frozenset(fechas)


PREVISTO_EXTRA_HOLIDAYS = _parse_holidays(PREVISTO_FESTIVOS_ENV)


@dataclass(frozen=True)
class _Prevision:
    """Resultado de cargar el artefacto de prevision. ``table`` None = sin prevision."""

    table: PredictionTable | None = None
    routes: frozenset[str] = frozenset()
    health: dict[str, Any] = field(default_factory=lambda: {"status": "disabled", "routes": []})


def _prevision_error(alert: str, message: str, **fields: Any) -> _Prevision:
    _log("ERROR", f"prevision desactivada: {message} (RB-013)", alert=alert, key=PREVISTO_KEY, **fields)
    return _Prevision(health={
        "status": "error", "alert": alert, "routes": sorted(PREVISTO_ROUTES),
    })


def _load_prevision(estado: Any, ahora: datetime) -> _Prevision:
    """Carga el artefacto de prevision del bucket PRIVADO. Nunca lanza (RB-013).

    Fallo seguro: ausente, ilegible, invalido o caducado -> ERROR estructurado y
    SIN prevision; el resto del tablon no cambia.
    """
    if not PREVISTO_ROUTES:
        return _Prevision()
    try:
        raw = estado.read(PREVISTO_KEY)
    except Exception as exc:
        return _prevision_error("prediction_artifact_unreadable", "no se pudo leer el artefacto",
                                error=repr(exc))
    table, error = load_prediction_table(raw)
    if table is None:
        if error == "missing":
            return _prevision_error("prediction_artifact_missing", "no existe el artefacto")
        return _prevision_error("prediction_artifact_invalid", "artefacto invalido", error=error)
    edad = ahora - table.generated_at
    if edad > timedelta(days=PREVISTO_MAX_AGE_DAYS):
        return _prevision_error(
            "prediction_artifact_stale", "artefacto caducado",
            generated_at=table.generated_at.isoformat(), max_age_days=PREVISTO_MAX_AGE_DAYS,
        )
    rutas = PREVISTO_ROUTES & table.lines
    health: dict[str, Any] = {
        "status": "ok",
        "routes": sorted(rutas),
        "artifact_generated_at": table.generated_at.isoformat(),
        "history_to": table.history.get("to"),
    }
    faltan = PREVISTO_ROUTES - table.lines
    if faltan:
        health["routes_missing_in_artifact"] = sorted(faltan)
        _log("WARNING", "lineas habilitadas que el artefacto no trae: sin prevision en ellas",
             alert="prediction_routes_missing", routes=sorted(faltan), key=PREVISTO_KEY)
    return _Prevision(table=table, routes=rutas, health=health)


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
    if actual.get("vehicles") and actual.get("stale_since"):
        # La ultima pasada de ESTA invocacion vino vacia y run_cycle conservo una
        # anterior de la misma invocacion marcandola ``degraded``. Esos datos
        # tienen segundos: se aplica el mismo umbral que entre invocaciones para
        # no publicar un aviso falso de datos viejos.
        salida = dict(actual)
        edad = _snapshot_age_seconds(salida)
        if edad is not None and edad < STALE_WARNING_SECONDS:
            salida.pop("degraded", None)
            salida.pop("reason", None)
            print(f"pasada final vacia: se conserva la anterior ({edad:.0f}s), aun vigente")
        return salida
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


def _prevision_del_tick(
    prevision: _Prevision, fresco: bool, ahora: datetime,
) -> tuple[Callable[[str], dict[str, Prediction]], dict[str, Any]]:
    """RB-013: previsiones por estacion para este tick y su bloque de salud.

    SOLO con snapshot fresco (con datos viejos no sabemos que trenes estan en el
    feed) y solo en las lineas permitidas (PREVISTO_ROUTES).
    """
    salud = dict(prevision.health)
    tabla = prevision.table if fresco else None
    tipo_dia = ""
    if prevision.table is not None:
        tipo_dia = day_type(operating_date(ahora), prevision.table.holidays | PREVISTO_EXTRA_HOLIDAYS)
        salud.update(applied=tabla is not None, day_type=tipo_dia)

    def _previsiones(stop_id: str) -> dict[str, Prediction]:
        if tabla is None:
            return {}
        return tabla.for_station(stop_id, tipo_dia, prevision.routes)

    return _previsiones, salud


def _build_boards(
    snapshot: dict[str, Any],
    publisher: Any,
    cancelled: frozenset[str] = frozenset(),
    skipped: dict[str, frozenset[str]] | None = None,
    prevision: _Prevision | None = None,
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

    _previsiones, salud_prevision = _prevision_del_tick(prevision or _Prevision(), fresco, ahora)

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
                predictions=_previsiones(stop_id),
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
                    # "live", "inbound_estimate", "predicted" (RB-013) o null.
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
                    # RB-013: retraso PREVISTO por el historico (mediana, n dias,
                    # tipo de dia). No toca "eta" ni "delay": no es dato en vivo.
                    "predicted_delay": d.predicted_delay_minutes,
                    "predicted_samples": d.prediction.observations if d.prediction else None,
                    "predicted_day_type": d.prediction.day_type if d.prediction else None,
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
            "departures_predicted": sum(
                1 for e in estaciones.values() for d in e["departures"]
                if d["predicted_delay"] is not None
            ),
            "prediction": salud_prevision,
        },
        "stations": estaciones,
    }


def _archive_pass(
    publisher: Any,
    snapshot: dict[str, Any],
    momento: datetime,
    cancelled: frozenset[str] | None = None,
    skipped: dict[str, frozenset[str]] | None = None,
) -> None:
    """Guarda una foto compacta de la pasada para el analisis posterior.

    Solo lo que hace falta para responder dos preguntas que hoy no se pueden
    responder: cuanto retraso sobrevive al cambio de sentido en cabecera, y con
    que frecuencia un servicio programado sencillamente no circula.

    Se escribe en el bucket PRIVADO: no es dato personal, pero es material de
    trabajo y no tiene por que servirse en la web.

    Con ``ARCHIVAR_CANCELACIONES=1`` se anaden ``canc`` (trip_ids suprimidos) y
    ``skip`` (``{trip_id: [stop_id, ...]}``). Clave presente y vacia significa
    "se consulto y no habia"; clave ausente, "no se sabe" (bandera apagada o
    trip_updates caido): ``None`` aqui, nunca una lista vacia inventada.
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
    if ARCHIVAR_CANCELACIONES and cancelled is not None and skipped is not None:
        compacto["canc"] = sorted(cancelled)
        compacto["skip"] = {trip: sorted(paradas) for trip, paradas in sorted(skipped.items())}
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


def _load_alerts_state(publisher: Any) -> dict[str, Any]:
    try:
        raw = publisher.read(ALERTS_STATE_KEY)
        estado = json.loads(raw) if raw else {}
    except Exception as exc:
        # Estado ilegible: se reescriben los avisos una vez, que es inocuo.
        _log("WARNING", "estado de avisos ilegible; se reinicia", alert="alerts_state_unreadable",
             error=repr(exc))
        return {}
    return estado if isinstance(estado, dict) else {}


def _stale_health(
    estado: dict[str, Any], feed_ts: datetime | None, ahora: datetime
) -> tuple[dict[str, Any], bool]:
    """Salud de frescura del feed y si hay que guardar el estado (aviso emitido)."""
    if feed_ts is None:
        return {"alerts_feed_stale": None, "alerts_feed_age_seconds": None}, False
    edad = int((ahora - feed_ts).total_seconds())
    stale = edad >= ALERTS_STALE_HOURS * 3600
    avisar = False
    if stale:
        previo = estado.get("stale_warned_at")
        try:
            avisar = previo is None or ahora - datetime.fromisoformat(previo) >= ALERTS_STALE_WARN_EVERY
        except (TypeError, ValueError):
            avisar = True
    if avisar:
        _log(
            "WARNING", "alerts_feed_stale", alert="alerts_feed_stale",
            feed_timestamp=feed_ts.astimezone(MADRID_TZ).isoformat(), age_seconds=edad,
            threshold_hours=ALERTS_STALE_HOURS,
        )
        estado["stale_warned_at"] = ahora.isoformat()
    return {"alerts_feed_stale": stale, "alerts_feed_age_seconds": edad}, avisar


def _archive_alerts(
    publisher: Any,
    ahora: datetime,
    fetch: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Archiva los avisos de Madrid si han cambiado; devuelve claves de salud.

    Nunca lanza: los avisos son material de estudio y un fallo aqui no puede
    tumbar el tick ni los tablones. Devuelve ``{}`` con ``ARCHIVAR_AVISOS``
    apagado, de modo que boards.json sale identico a antes.
    """
    if not ARCHIVAR_AVISOS:
        return {}
    descargar = fetch or fetch_alerts_payload
    salud: dict[str, Any] = {
        "alerts_fetch_failed": True, "alerts_feed_stale": None,
        "alerts_feed_age_seconds": None, "alerts_madrid": None,
    }
    try:
        resultado = parse_madrid_alerts(descargar())
    except AlertsFetchError as exc:
        _log("WARNING", "no se pudo leer alerts.json", alert="alerts_fetch_failed", error=str(exc))
        return salud
    except Exception as exc:
        _log("ERROR", "fallo inesperado leyendo alerts.json", alert="alerts_fetch_failed",
             error=repr(exc))
        return salud

    try:
        estado = _load_alerts_state(publisher)
        frescura, guardar = _stale_health(estado, resultado.feed_timestamp, ahora)
        salud.update(frescura, alerts_fetch_failed=False, alerts_madrid=len(resultado.alerts))
        huella = alerts_hash(resultado.alerts)
        if huella != estado.get("hash"):
            clave = f"{ALERTS_ARCHIVE_PREFIX}/{ahora:%Y-%m-%d}/{ahora:%H%M%S}.json"
            objeto = {
                "ts": ahora.isoformat(),
                "feed_ts": (resultado.feed_timestamp.astimezone(MADRID_TZ).isoformat()
                            if resultado.feed_timestamp else None),
                "hash": huella,
                "n": len(resultado.alerts),
                "alerts": list(resultado.alerts),
            }
            payload = json.dumps(objeto, ensure_ascii=False, separators=(",", ":")) + "\n"
            try:
                publisher.publish(clave, payload.encode("utf-8"), content_type="application/x-ndjson")
            except Exception as exc:
                # Sin actualizar el hash: el siguiente tick lo vuelve a intentar.
                _log("ERROR", f"no se pudo archivar {clave}", alert="alerts_archive_failed",
                     key=clave, error=repr(exc))
            else:
                estado.update(hash=huella, key=clave, written_at=ahora.isoformat())
                guardar = True
                print(f"avisos archivados: {clave} ({len(resultado.alerts)} de Madrid)")
        if guardar:
            _safe_publish(
                publisher, ALERTS_STATE_KEY,
                json.dumps(estado, ensure_ascii=False).encode("utf-8"),
                content_type="application/json",
            )
    except Exception as exc:
        _log("ERROR", "fallo archivando avisos", alert="alerts_archive_failed", error=repr(exc))
    return salud


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


def _publish_boards(  # noqa: PLR0913 - salud de avisos (RB-014) y bucket privado (RB-013) opcionales
    publisher: Any,
    salida: dict[str, Any],
    cancelled: frozenset[str],
    skipped: dict[str, frozenset[str]],
    *,
    extra_health: dict[str, Any] | None = None,
    estado: Any | None = None,
) -> None:
    """Construye y publica los tablones; nunca lanza.

    ``extra_health`` se mezcla en ``boards["health"]`` (frescura de avisos,
    RB-014). Es informativo: no marca el snapshot como degradado.

    Un fallo aqui (horario corrupto, bug, GCS caido) no debe saltarse los avisos
    de Telegram que vienen despues en ``main``. El artefacto de prevision
    (RB-013) se lee de ``estado`` (bucket privado).
    """
    try:
        prevision = _load_prevision(estado if estado is not None else publisher,
                                    datetime.now(tz=MADRID_TZ))
        boards = _build_boards(salida, publisher, cancelled, skipped, prevision=prevision)
        if extra_health:
            boards["health"].update(extra_health)
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
            _archive_pass(
                estado, store.latest, datetime.now(tz=MADRID_TZ),
                cancelled=ultimo.cancelled_trips if ultimo.trip_updates_ok else None,
                skipped=ultimo.skipped_stops if ultimo.trip_updates_ok else None,
            )
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

    salud_avisos = _archive_alerts(estado, datetime.now(tz=MADRID_TZ), fetch=fetch_alerts_payload)
    _publish_boards(
        publisher,
        salida,
        ultimo.cancelled_trips,
        ultimo.skipped_stops,
        extra_health=salud_avisos,
        estado=estado,
    )
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
