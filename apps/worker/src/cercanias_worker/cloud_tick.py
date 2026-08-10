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
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cercanias_domain.departures import (
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    destinations_available,
    next_departures,
)
from cercanias_domain.incidents import AlertCooldown, AlertPolicy, format_incident_message
from cercanias_notifiers_telegram.telegram_notifier import (
    TelegramConfigError,
    TelegramNotifier,
    TelegramOptions,
    TelegramSendError,
)
from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository
from cercanias_storage.publishers import publisher_from_env
from cercanias_storage.secrets import read_secret
from cercanias_worker.config import WorkerConfig
from cercanias_worker.mvp import SnapshotStore, run_cycle

SNAPSHOT_KEY = os.environ.get("SNAPSHOT_KEY", "snapshot.json")
ALERT_STATE_KEY = os.environ.get("ALERT_STATE_KEY", "alert-state.json")
BOARDS_KEY = os.environ.get("BOARDS_KEY", "boards.json")
#: Horarios empaquetados en la imagen: cambian una vez al dia, asi que leerlos
#: del bucket en cada invocacion seria pagar por algo estatico.
SCHEDULES_DIR = Path(os.environ.get("SCHEDULES_DIR", "data/horarios"))
MADRID_TZ = ZoneInfo("Europe/Madrid")

#: C-7: 2-3 pasadas internas por invocación, separadas 20 s.
PASSES_PER_INVOCATION = int(os.environ.get("PASSES_PER_INVOCATION", "3"))
SECONDS_BETWEEN_PASSES = float(os.environ.get("SECONDS_BETWEEN_PASSES", "20"))

EXIT_NO_DATA = 4


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


def _load_schedule(stop_id: str) -> list[ScheduledStop]:
    ruta = SCHEDULES_DIR / f"{stop_id}.json"
    if not ruta.exists():
        return []
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    return [
        ScheduledStop(d["trip"], d["t"], d.get("route", ""), d.get("dest"), bool(d.get("terminates")))
        for d in datos.get("departures", [])
    ]


def _build_boards(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Precalcula el tablon de salidas de cada estacion.

    Se hace AQUI, en Python probado, y no en el JavaScript de la pagina: tener la
    misma logica dos veces garantiza que se separen. Ademas mantiene la ruta de
    lectura como un fichero estatico tras CDN (C-7): la pagina solo pinta, no
    calcula, y el coste no crece con los lectores.
    """
    vehiculos = snapshot.get("vehicles", [])
    live = {
        v["trip_id"]: LiveTrain(v["trip_id"], v.get("delay_seconds"), v.get("platform"))
        for v in vehiculos
        if v.get("trip_id")
    }
    conteo: dict[str, int] = {}
    for v in vehiculos:
        p = (v.get("trip_id") or "")[:5]
        if p:
            conteo[p] = conteo.get(p, 0) + 1
    servicio = max(conteo, key=lambda k: conteo[k]) if conteo else ""

    ahora = datetime.now(tz=MADRID_TZ)
    minutos = ahora.hour * 60 + ahora.minute
    estaciones: dict[str, Any] = {}

    for ruta in sorted(SCHEDULES_DIR.glob("*.json")):
        stop_id = ruta.stem
        horario = _load_schedule(stop_id)
        if not horario:
            continue
        salidas = next_departures(
            horario, live, DepartureQuery(now_minutes=minutos, service_prefix=servicio, limit=10)
        )
        estaciones[stop_id] = {
            "destinations": list(destinations_available(horario, service_prefix=servicio)),
            "departures": [
                {
                    "t": d.scheduled_hhmm,
                    "eta": d.expected_hhmm,
                    "route": d.route,
                    "dest": d.destination,
                    "train": d.train_number,
                    "delay": d.delay_minutes,
                    "platform": d.platform,
                    "in": d.minutes_until,
                    "live": d.located,
                }
                for d in salidas
            ],
        }
    # Los horarios se generan por lotes de dias (ADR-012: el indice de calendario
    # avanza a diario). Cuando el servicio en circulacion cae fuera del lote, los
    # tablones salen vacios SIN error: es el mismo fallo silencioso de siempre, y
    # hay que gritarlo antes de que llegue al anden.
    con_salidas = sum(1 for e in estaciones.values() if e["departures"])
    if servicio and con_salidas == 0 and estaciones:
        print(
            f"AVISO GRAVE: servicio {servicio} sin ninguna salida en {len(estaciones)} "
            "estaciones. Los horarios empaquetados no cubren el dia de hoy: hay que "
            "regenerarlos (ver docs/DESPLIEGUE.md).",
            file=sys.stderr,
        )
    return {
        "generated_at": ahora.isoformat(),
        "service": servicio,
        "stations_with_departures": con_salidas,
        "stations": estaciones,
    }


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


def main() -> int:
    config = WorkerConfig()
    publisher = publisher_from_env()
    notifier = _build_notifier()
    policy = AlertPolicy()
    cooldown = _load_cooldown(publisher)

    # El estado de rumbo sí es intra-invocación: con 3 pasadas hay 2
    # oportunidades de calcularlo. Persistirlo entre invocaciones es WP-3.
    state = InMemoryCurrentStateRepository()
    previo = _read_previous_snapshot(publisher)
    if previo:
        print(f"ultimo bueno conocido: {previo.get('generated_at')} "
              f"({len(previo.get('vehicles', []))} trenes)")
    store = _MemoryStore(previous_good=previo)

    ultimo = None
    for i in range(1, PASSES_PER_INVOCATION + 1):
        ultimo = run_cycle(config, store, state)
        print(
            f"pasada {i}/{PASSES_PER_INVOCATION}: trenes={ultimo.madrid_vehicles} "
            f"retrasos={ultimo.madrid_delays} rumbos={ultimo.with_bearing} "
            f"salud={ultimo.health.worst} incidencias={len(ultimo.incidents)}"
        )
        if i < PASSES_PER_INVOCATION:
            time.sleep(SECONDS_BETWEEN_PASSES)

    if ultimo is None or store.latest is None:
        print("ninguna pasada produjo snapshot", file=sys.stderr)
        return EXIT_NO_DATA

    salida = store.latest
    if not salida.get("vehicles") and previo:
        # ADR-011: mejor servir datos de hace un minuto, etiquetados, que un mapa
        # vacio. Un mapa vacio no parece un fallo, parece que no hay trenes.
        salida = dict(previo)
        salida["degraded"] = True
        salida["stale_since"] = store.latest.get("generated_at")
        salida["reason"] = "el feed no publico el nucleo en esta pasada; se conserva el anterior"
        print("PASADA VACIA: se conserva el snapshot anterior")

    ref = publisher.publish(
        SNAPSHOT_KEY,
        json.dumps(salida, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )
    print(f"snapshot publicado en {ref} ({len(salida.get('vehicles', []))} trenes)")

    boards = _build_boards(salida)
    publisher.publish(
        BOARDS_KEY,
        json.dumps(boards, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
    )
    print(f"tablones publicados: {len(boards['stations'])} estaciones (servicio {boards['service']})")

    ahora = datetime.now(tz=UTC)
    notificables = [inc for inc in ultimo.incidents if policy.should_notify(inc)]
    nuevas = [inc for inc in notificables if cooldown.should_alert(inc, ahora)]
    if nuevas:
        try:
            outcome = notifier.send(format_incident_message(tuple(nuevas)))
            for inc in nuevas:
                cooldown.record(inc, ahora)
            _save_cooldown(publisher, cooldown)
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
