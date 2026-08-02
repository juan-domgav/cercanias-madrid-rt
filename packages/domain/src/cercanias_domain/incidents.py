"""Detección de incidencias por línea a partir de retrasos oficiales.

Alcance deliberadamente acotado. La Fase 3 de la especificación describe
notificaciones con suscripciones por línea y estación, expansión de grupos
(``C4`` → ``{C4a, C4b}``) y reglas de corroboración. Aquí sólo está la primera
mitad: **detectar** que una línea va mal y decidir **si toca avisar**. El
enrutado por suscriptor no existe todavía; hay un único destino.

Dos decisiones que no son obvias:

1. **Una incidencia es un patrón, no un tren.** Un solo tren con 20 minutos de
   retraso es ruido operativo normal. Varios trenes de la misma línea por encima
   del umbral es una incidencia. Por eso hace falta ``min_trains``.

2. **El enfriamiento mira la severidad, no sólo el reloj.** Silenciar una línea
   30 minutos está bien si la situación es la misma, pero si empeora de forma
   material hay que volver a avisar: quien recibió «retrasos de 6 minutos» debe
   enterarse de que ya van por 40.

No se usan reportes de la comunidad, así que la corroboración de C-6 no aplica:
todo lo que entra aquí es dato oficial de Renfe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


class IncidentSeverity(StrEnum):
    MINOR = "minor"
    MAJOR = "major"


@dataclass(frozen=True)
class TrainDelay:
    """Retraso de un tren, ya asociado a su grupo de línea."""

    trip_id: str
    route_group: str
    delay_seconds: int


@dataclass(frozen=True)
class IncidentThresholds:
    """Umbrales de detección.

    Los valores por defecto son un punto de partida razonable, no una verdad
    medida: Renfe considera puntual un Cercanías con menos de 3 minutos, y la
    mediana observada en el feed fue de 60 s, así que 5 minutos deja fuera el
    ruido de fondo sin ocultar problemas reales.
    """

    delay_seconds: int = 300  # 5 min: por encima, el tren cuenta como afectado
    min_trains: int = 3  # trenes afectados para que sea incidencia de linea
    severe_delay_seconds: int = 900  # 15 min: eleva la severidad a MAJOR


@dataclass(frozen=True)
class LineIncident:
    route_group: str
    affected_trains: int
    total_trains: int
    max_delay_seconds: int
    median_delay_seconds: int
    severity: IncidentSeverity

    @property
    def affected_ratio(self) -> float:
        return self.affected_trains / self.total_trains if self.total_trains else 0.0


def detect_incidents(
    delays: list[TrainDelay], thresholds: IncidentThresholds | None = None
) -> tuple[LineIncident, ...]:
    """Agrupa por línea y devuelve las que superan los umbrales.

    Ordenadas por retraso máximo descendente: si hay que recortar el mensaje,
    que sobreviva lo peor.
    """
    limits = thresholds or IncidentThresholds()
    by_line: dict[str, list[TrainDelay]] = {}
    for delay in delays:
        by_line.setdefault(delay.route_group, []).append(delay)

    incidents: list[LineIncident] = []
    for route_group, trains in by_line.items():
        affected = [t for t in trains if t.delay_seconds >= limits.delay_seconds]
        if len(affected) < limits.min_trains:
            continue
        magnitudes = sorted(t.delay_seconds for t in affected)
        max_delay = magnitudes[-1]
        incidents.append(
            LineIncident(
                route_group=route_group,
                affected_trains=len(affected),
                total_trains=len(trains),
                max_delay_seconds=max_delay,
                median_delay_seconds=magnitudes[len(magnitudes) // 2],
                severity=(
                    IncidentSeverity.MAJOR
                    if max_delay >= limits.severe_delay_seconds
                    else IncidentSeverity.MINOR
                ),
            )
        )
    return tuple(sorted(incidents, key=lambda i: -i.max_delay_seconds))


#: Proporción de la línea afectada que, por sí sola, justifica un aviso aunque
#: los retrasos sean leves. Una disrupción amplia y suave —media línea con 6
#: minutos— no la ve un umbral de retraso, pero el viajero sí.
#:
#: 0,5 y no 0,4 por una observación concreta: el 02/08/2026 la C4 osciló entre
#: 6/19 y 8/19 trenes afectados (32 %-42 %) en cinco pasadas consecutivas. Con el
#: listón en 0,4 habría entrado y salido del aviso entre pasadas. El umbral tiene
#: que quedar por encima de la banda de ruido que genera el propio feed.
DEFAULT_NOTIFY_AFFECTED_RATIO = 0.5


@dataclass(frozen=True)
class AlertPolicy:
    """Qué incidencias merecen notificación.

    Deliberadamente separado de la detección: ``detect_incidents`` sigue
    devolviendo todo lo que supera el umbral de 5 minutos, porque eso alimenta el
    snapshot y el mapa. Notificar es otra decisión, con un listón más alto: un
    mensaje que no aporta enseña a ignorar los siguientes.
    """

    notify_affected_ratio: float = DEFAULT_NOTIFY_AFFECTED_RATIO

    def should_notify(self, incident: LineIncident) -> bool:
        if incident.severity is IncidentSeverity.MAJOR:
            return True
        return incident.affected_ratio >= self.notify_affected_ratio


#: Silencio por línea tras avisar, salvo empeoramiento material.
DEFAULT_COOLDOWN = timedelta(minutes=30)

#: Un aviso repetido sólo se justifica si el retraso máximo crece de forma
#: perceptible. Un salto de 5 min a 6 min no le sirve a nadie.
MATERIAL_WORSENING_SECONDS = 600

_MINUTES_PER_HOUR = 60


@dataclass
class _LastAlert:
    at: datetime
    severity: IncidentSeverity
    max_delay_seconds: int


@dataclass
class AlertCooldown:
    """Decide si una incidencia ya avisada merece un aviso nuevo.

    El estado vive en memoria: para el MVP basta, porque el proceso es un bucle
    único. En un despliegue con escalado a cero habría que externalizarlo, igual
    que el estado de rumbo (riesgo R-3 de la arquitectura v2).
    """

    cooldown: timedelta = DEFAULT_COOLDOWN
    _last: dict[str, _LastAlert] = field(default_factory=dict)

    def should_alert(self, incident: LineIncident, now: datetime) -> bool:
        previous = self._last.get(incident.route_group)
        if previous is None:
            return True
        if now - previous.at >= self.cooldown:
            return True
        if incident.severity is IncidentSeverity.MAJOR and previous.severity is IncidentSeverity.MINOR:
            return True  # escalada de severidad: siempre se avisa
        worsening = incident.max_delay_seconds - previous.max_delay_seconds
        return worsening >= MATERIAL_WORSENING_SECONDS

    def record(self, incident: LineIncident, now: datetime) -> None:
        self._last[incident.route_group] = _LastAlert(
            at=now,
            severity=incident.severity,
            max_delay_seconds=incident.max_delay_seconds,
        )


def _format_minutes(seconds: int) -> str:
    minutes = seconds // 60
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes} min"
    return f"{minutes // _MINUTES_PER_HOUR} h {minutes % _MINUTES_PER_HOUR:02d} min"


def format_incident_message(incidents: tuple[LineIncident, ...]) -> str:
    """Mensaje HTML para Telegram.

    Incluye la aclaración de origen porque el proyecto es independiente de Renfe
    y Adif, y un aviso de retrasos sin atribución se lee como oficial.
    """
    lines = ["<b>Cercanías Madrid — retrasos detectados</b>", ""]
    for incident in incidents:
        icono = "🔴" if incident.severity is IncidentSeverity.MAJOR else "🟠"
        lines.append(
            f"{icono} <b>{incident.route_group}</b>: "
            f"{incident.affected_trains} de {incident.total_trains} trenes con retraso. "
            f"Máximo {_format_minutes(incident.max_delay_seconds)}, "
            f"mediana {_format_minutes(incident.median_delay_seconds)}."
        )
    lines += [
        "",
        "<i>Datos en tiempo real de Renfe (CC BY 4.0). Proyecto independiente, "
        "sin relación con Renfe ni Adif.</i>",
    ]
    return "\n".join(lines)
