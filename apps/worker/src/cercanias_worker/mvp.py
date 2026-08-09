"""``make mvp`` — bucle mínimo ejecutable de extremo a extremo.

Junta lo construido hasta ahora en algo que se puede *arrancar y mirar*:

1. sondea ``vehicle_positions`` y ``trip_updates``;
2. filtra el núcleo de Madrid y normaliza;
3. evalúa los invariantes de negocio (WP-1);
4. publica un snapshot con semántica de **último bueno conocido** (ADR-011);
5. notifica por Telegram si el fallo **persiste** (C-8).

No es la Fase 1: no hay API HTTP ni frontend. Es el mínimo que permite ver el
sistema funcionando contra el feed real y comprobar la integración de Telegram.

El punto 4 es el que no es obvio: el núcleo de Madrid desaparece del feed en
~5 % de los sondeos (ADR-011). Sobrescribir el snapshot bueno con uno vacío
serviría un mapa sin trenes sin que nada fallase. Aquí no se sobrescribe: se
conserva el anterior y se marca ``stale_since``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cercanias_connectors_renfe.gtfs_rt_client import (
    GtfsRtFetchError,
    fetch_vehicle_positions_payload,
    normalize_feed_payload,
)
from cercanias_connectors_renfe.trip_updates import (
    TripUpdatesFetchError,
    fetch_trip_updates_payload,
    parse_trip_updates,
)
from cercanias_domain.health import (
    FeedHealthInput,
    HealthReport,
    Severity,
    evaluate_feed_health,
)
from cercanias_domain.incidents import (
    AlertCooldown,
    AlertPolicy,
    LineIncident,
    TrainDelay,
    detect_incidents,
    format_incident_message,
)
from cercanias_domain.nucleo_filter import build_nucleo_filter
from cercanias_notifiers_telegram.telegram_notifier import (
    TelegramConfigError,
    TelegramNotifier,
    TelegramOptions,
)
from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository
from cercanias_worker.config import WorkerConfig

MADRID_TZ = ZoneInfo("Europe/Madrid")

#: Pasadas CRITICAS consecutivas antes de notificar. Con ~5 % de desapariciones
#: por sondeo (ADR-011), avisar a la primera produce un aviso cada ~20 minutos y
#: la alarma se acaba silenciando, que es la forma habitual en que muere.
CONSECUTIVE_FAILURES_BEFORE_ALERT = 3

EXIT_UNHEALTHY = 4


@dataclass
class SnapshotStore:
    """Snapshot en disco con semántica de último bueno conocido (ADR-011)."""

    path: Path
    _last_good: dict[str, object] | None = field(default=None, init=False)

    def publish(self, snapshot: dict[str, object], *, is_good: bool) -> tuple[bool, str]:
        """Publica, o conserva el anterior si la pasada no es buena.

        Devuelve ``(publicado, motivo)``.
        """
        if is_good:
            self._last_good = snapshot
            self._write(snapshot)
            return True, "snapshot publicado"

        if self._last_good is None:
            # Nunca hubo uno bueno: se publica el vacío marcado, porque no hay
            # nada mejor y ocultarlo sería peor que mostrarlo etiquetado.
            degraded = dict(snapshot)
            degraded["degraded"] = True
            degraded["reason"] = "sin snapshot bueno previo"
            self._write(degraded)
            return True, "publicado en estado degradado (no habia anterior)"

        stale = dict(self._last_good)
        stale["stale_since"] = snapshot.get("generated_at")
        stale["degraded"] = True
        stale["reason"] = "pasada sin datos de Madrid; se conserva el ultimo bueno"
        self._write(stale)
        return False, "CONSERVADO el snapshot anterior (la pasada venia vacia)"

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        tmp.replace(self.path)  # publicación atómica


@dataclass
class CycleResult:
    health: HealthReport
    madrid_vehicles: int
    madrid_delays: int
    with_bearing: int
    incidents: tuple[LineIncident, ...]
    published: bool
    note: str


def run_cycle(
    config: WorkerConfig,
    store: SnapshotStore,
    state: InMemoryCurrentStateRepository,
) -> CycleResult:
    nucleo_filter = build_nucleo_filter(config.renfe_nucleo_codes)
    prefixes = tuple(config.renfe_nucleo_codes)
    now = datetime.now(tz=UTC)
    local_time = now.astimezone(MADRID_TZ).time()

    try:
        vp_payload = fetch_vehicle_positions_payload(config.renfe_vehicle_positions_url)
    except GtfsRtFetchError as exc:
        print(f"  ERROR posiciones: {exc}", file=sys.stderr)
        vp_payload = {"entity": []}

    # El repositorio de estado persiste ENTRE pasadas: es lo que permite calcular
    # rumbo (C-4). En la primera pasada sera None para todos, por definicion.
    result = normalize_feed_payload(
        vp_payload,
        nucleo_filter=nucleo_filter,
        static_index=None,
        state_repository=state,
        min_displacement_meters=config.bearing_min_displacement_meters,
    )

    try:
        tu_payload = fetch_trip_updates_payload()
        delays = parse_trip_updates(tu_payload, nucleo_prefixes=prefixes)
    except TripUpdatesFetchError as exc:
        print(f"  ERROR retrasos: {exc}", file=sys.stderr)
        delays = parse_trip_updates({"entity": []}, nucleo_prefixes=prefixes)

    health = evaluate_feed_health(
        FeedHealthInput(
            local_time=local_time,
            entities_in_payload=result.total_entities_in_payload,
            entities_after_nucleo_filter=len(result.observations),
            # Sin GTFS estatico en el MVP: no se evalua resolucion de trip_id.
            trips_resolved=len(result.observations),
            trip_updates_total=delays.matched_nucleo,
            trip_updates_with_delay=delays.with_delay,
            feed_skew_seconds=0.0,
        )
    )

    by_trip = delays.by_trip_id()

    # Incidencias por linea: solo dato oficial, sin reportes de comunidad, asi
    # que la corroboracion de C-6 no aplica.
    incidents = detect_incidents(
        [
            TrainDelay(
                trip_id=o.trip_id,
                route_group=o.route_group,
                delay_seconds=by_trip[o.trip_id].delay_seconds,
            )
            for o in result.observations
            if o.trip_id in by_trip
        ]
    )

    snapshot: dict[str, object] = {
        "generated_at": now.isoformat(),
        "local_time": local_time.isoformat(),
        "nucleo_codes": list(prefixes),
        "vehicles": [
            {
                "trip_id": o.trip_id,
                "route": o.route_raw,
                "lat": o.latitude,
                "lon": o.longitude,
                "platform": o.platform,
                "bearing": o.bearing_degrees,
                # Necesario para la vista por estacion: permite saber que trenes
                # tienen TU estacion como proxima parada, unico caso en que el
                # sentido es seguro sin topologia de linea.
                "next_stop_id": o.stop_id_resolvable,
                "status": str(o.current_status),
                "delay_seconds": (by_trip[o.trip_id].delay_seconds if o.trip_id in by_trip else None),
            }
            for o in result.observations
        ],
        "incidents": [
            {
                "linea": inc.route_group,
                "afectados": inc.affected_trains,
                "total": inc.total_trains,
                "max_retraso_s": inc.max_delay_seconds,
                "severidad": str(inc.severity),
            }
            for inc in incidents
        ],
        "health": {i.name: {"severity": str(i.severity), "detail": i.detail} for i in health.invariants},
    }

    # "Bueno" = hay trenes. Una pasada vacia con payload lleno es justo el caso
    # de ADR-011 y no debe pisar el snapshot anterior.
    published, note = store.publish(snapshot, is_good=len(result.observations) > 0)
    return CycleResult(
        health=health,
        madrid_vehicles=len(result.observations),
        madrid_delays=delays.with_delay,
        with_bearing=sum(1 for o in result.observations if o.bearing_degrees is not None),
        incidents=incidents,
        published=published,
        note=note,
    )


def _build_notifier(config: WorkerConfig) -> TelegramNotifier:
    """Notificador real si hay credenciales; dry-run si no."""
    try:
        notifier = TelegramNotifier()
        print("  Telegram: configurado (se enviaran mensajes reales)")
        return notifier
    except TelegramConfigError as exc:
        print(f"  Telegram: modo dry-run ({exc.args[0].split('.')[0]})")
        return TelegramNotifier(token=None, chat_id=None, options=TelegramOptions(dry_run=True))


def _report_incidents(
    cycle: CycleResult,
    policy: AlertPolicy,
    cooldown: AlertCooldown,
    notifier: TelegramNotifier,
) -> None:
    """Imprime las incidencias y notifica las que corresponda.

    Detectar y notificar son decisiones distintas: se detecta desde 5 minutos,
    porque eso alimenta el snapshot y el mapa, pero sólo se notifica lo grave o
    lo suficientemente extendido. Un mensaje que no aporta enseña a ignorar los
    siguientes.
    """
    ahora = datetime.now(tz=UTC)
    notificables = [inc for inc in cycle.incidents if policy.should_notify(inc)]
    nuevas = [inc for inc in notificables if cooldown.should_alert(inc, ahora)]

    for inc in cycle.incidents:
        if inc in nuevas:
            marca = "AVISO"
        elif inc in notificables:
            marca = "en enfriamiento"
        else:
            marca = "solo snapshot, no se notifica"
        print(
            f"    ~ {inc.route_group}: {inc.affected_trains}/{inc.total_trains} trenes "
            f"({inc.affected_ratio:.0%}), max {inc.max_delay_seconds // 60} min "
            f"[{inc.severity}] ({marca})"
        )

    if nuevas:
        outcome = notifier.send(format_incident_message(tuple(nuevas)))
        for inc in nuevas:
            cooldown.record(inc, ahora)
        print(f"    -> Telegram (incidencias): {outcome.detail}")


def main() -> int:
    config = WorkerConfig()
    cycles = int(_env("MVP_CYCLES", "5"))
    interval = float(_env("MVP_INTERVAL_SECONDS", "20"))
    store = SnapshotStore(Path(config.reports_dir) / "mvp-snapshot.json")
    state = InMemoryCurrentStateRepository()
    notifier = _build_notifier(config)

    print(f"\nMVP: {cycles} pasadas cada {interval:.0f}s, nucleos={config.renfe_nucleo_codes}")
    print(f"Snapshot: {store.path}\n")

    consecutive_critical = 0
    worst_seen = Severity.OK
    cooldown = AlertCooldown()
    policy = AlertPolicy()

    for i in range(1, cycles + 1):
        print(f"[{i}/{cycles}] {datetime.now(tz=MADRID_TZ):%H:%M:%S}")
        cycle = run_cycle(config, store, state)
        estado = cycle.health.worst
        if estado is Severity.CRITICAL:
            worst_seen = Severity.CRITICAL
        elif estado is Severity.WARNING and worst_seen is not Severity.CRITICAL:
            worst_seen = Severity.WARNING

        print(
            f"  trenes={cycle.madrid_vehicles}  retrasos={cycle.madrid_delays}  "
            f"rumbos={cycle.with_bearing}  incidencias={len(cycle.incidents)}  "
            f"salud={estado.upper()}  {cycle.note}"
        )
        for inv in cycle.health.failures:
            print(f"    ! {inv.name}: {inv.detail}")

        _report_incidents(cycle, policy, cooldown, notifier)

        if estado is Severity.CRITICAL:
            consecutive_critical += 1
            if consecutive_critical == CONSECUTIVE_FAILURES_BEFORE_ALERT:
                texto = (
                    f"<b>Cercanias Madrid — incidencia de datos</b>\n"
                    f"{consecutive_critical} pasadas consecutivas en estado CRITICO.\n"
                    + "\n".join(f"• {inv.detail}" for inv in cycle.health.failures)
                )
                outcome = notifier.send(texto)
                print(f"    -> Telegram: {outcome.detail}")
        else:
            consecutive_critical = 0

        if i < cycles:
            time.sleep(interval)

    print(f"\nPeor estado observado: {worst_seen.upper()}")
    return EXIT_UNHEALTHY if worst_seen is Severity.CRITICAL else 0


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


if __name__ == "__main__":
    raise SystemExit(main())
