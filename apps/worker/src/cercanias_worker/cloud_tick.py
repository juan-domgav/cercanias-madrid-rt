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

from cercanias_domain.incidents import AlertCooldown, AlertPolicy, format_incident_message
from cercanias_notifiers_telegram.telegram_notifier import (
    TelegramConfigError,
    TelegramNotifier,
    TelegramOptions,
    TelegramSendError,
)
from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository
from cercanias_storage.publishers import publisher_from_env
from cercanias_worker.config import WorkerConfig
from cercanias_worker.mvp import SnapshotStore, run_cycle

SNAPSHOT_KEY = os.environ.get("SNAPSHOT_KEY", "snapshot.json")
ALERT_STATE_KEY = os.environ.get("ALERT_STATE_KEY", "alert-state.json")

#: C-7: 2-3 pasadas internas por invocación, separadas 20 s.
PASSES_PER_INVOCATION = int(os.environ.get("PASSES_PER_INVOCATION", "3"))
SECONDS_BETWEEN_PASSES = float(os.environ.get("SECONDS_BETWEEN_PASSES", "20"))

EXIT_NO_DATA = 4


class _MemoryStore(SnapshotStore):
    """Reutiliza la lógica de último bueno conocido sin tocar disco."""

    def __init__(self) -> None:
        self.latest: dict[str, Any] | None = None
        self.published_ok = False

    def _write(self, payload: dict[str, object]) -> None:
        self.latest = dict(payload)


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


def _build_notifier() -> TelegramNotifier:
    try:
        return TelegramNotifier()
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
    store = _MemoryStore()

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

    ref = publisher.publish(
        SNAPSHOT_KEY,
        json.dumps(store.latest, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )
    print(f"snapshot publicado en {ref}")

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
