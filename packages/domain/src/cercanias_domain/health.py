"""WP-1 — invariantes de negocio del feed (riesgo R-1).

El modo de fallo más caro que se detectó en Fase 0 no produce ningún error: si
Renfe cambia la numeración de núcleos —como hizo entre el 29 y el 31 de julio de
2026, ver ADR-010— el filtro deja de encontrar entidades, la ingesta responde
correctamente y el mapa se queda vacío. *Filtrar a cero es un resultado válido a
las 4 de la madrugada*, así que ninguna métrica de infraestructura lo distingue
de una avería.

Este módulo convierte expectativas de negocio en aserciones comprobables. Es
deliberadamente parte del dominio: no sabe de HTTP, de relojes del sistema ni de
almacenamiento. Recibe los números ya contados y la hora local ya resuelta, para
poder probar la madrugada, la hora punta y el feed vacío sin tocar el reloj.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import time
from enum import StrEnum

#: Cercanías Madrid no presta servicio de madrugada. Cero trenes entre estas
#: horas es normal y no debe alertar; cero trenes fuera de ellas es una avería.
SERVICE_START = time(5, 0)
SERVICE_END = time(1, 30)


class Severity(StrEnum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ExpectedRange:
    """Rango esperado de trenes en circulación para una franja horaria."""

    label: str
    minimum: int
    maximum: int

    def contains(self, value: int) -> bool:
        return self.minimum <= value <= self.maximum


#: Bandas horarias en hora local de Madrid. Los máximos son holgados a
#: propósito: el objetivo es detectar "se ha roto el filtro", no vigilar la
#: puntualidad de Renfe. Un rango demasiado estrecho genera ruido y acaba
#: ignorándose, que es la forma habitual en que muere una alarma.
DEFAULT_BANDS: tuple[tuple[time, time, ExpectedRange], ...] = (
    (time(1, 30), time(5, 0), ExpectedRange("madrugada (sin servicio)", 0, 15)),
    (time(5, 0), time(7, 0), ExpectedRange("primera hora", 5, 120)),
    (time(7, 0), time(10, 0), ExpectedRange("punta de mañana", 40, 250)),
    (time(10, 0), time(17, 0), ExpectedRange("valle", 25, 200)),
    (time(17, 0), time(21, 0), ExpectedRange("punta de tarde", 40, 250)),
    (time(21, 0), time(1, 30), ExpectedRange("última hora", 5, 150)),
)


def band_for(
    local_time: time,
    bands: tuple[tuple[time, time, ExpectedRange], ...] = DEFAULT_BANDS,
) -> ExpectedRange:
    """Banda aplicable a una hora local, tratando el cruce de medianoche."""
    for start, end, expected in bands:
        if start <= end:
            if start <= local_time < end:
                return expected
        elif local_time >= start or local_time < end:  # cruza medianoche
            return expected
    return DEFAULT_BANDS[0][2]


def is_service_hours(local_time: time) -> bool:
    """¿Hay servicio a esta hora local? (el intervalo cruza medianoche)."""
    return local_time >= SERVICE_START or local_time < SERVICE_END


@dataclass(frozen=True)
class Invariant:
    name: str
    severity: Severity
    detail: str
    #: Recuentos adicionales que se publican junto a ``severity``/``detail`` en
    #: ``snapshot.json → health.<name>`` (claves aditivas; hoy sólo
    #: ``delay_coverage.n_cancelados``, RB-015).
    metrics: Mapping[str, int] = field(default_factory=dict, compare=False, hash=False)

    @property
    def failed(self) -> bool:
        return self.severity is not Severity.OK


@dataclass(frozen=True)
class FeedHealthInput:
    """Recuentos ya calculados sobre una pasada de ingesta."""

    local_time: time
    entities_in_payload: int
    entities_after_nucleo_filter: int
    trips_resolved: int
    trip_updates_total: int
    trip_updates_with_delay: int
    feed_skew_seconds: float
    #: Viajes ``CANCELED`` incluidos en ``trip_updates_total`` (RB-015). Un tren
    #: suprimido nunca publica retraso, así que no entra en la cobertura. Sólo
    #: supresiones de viaje completo: un viaje con paradas ``SKIPPED`` (acortado)
    #: sigue circulando y SÍ cuenta.
    trip_updates_cancelled: int = 0
    #: Cuántos de esos suprimidos traen aun así ``delay`` (y por tanto están en
    #: ``trip_updates_with_delay``): se restan también del numerador.
    trip_updates_cancelled_with_delay: int = 0


@dataclass(frozen=True)
class HealthReport:
    invariants: tuple[Invariant, ...]

    @property
    def worst(self) -> Severity:
        if any(i.severity is Severity.CRITICAL for i in self.invariants):
            return Severity.CRITICAL
        if any(i.severity is Severity.WARNING for i in self.invariants):
            return Severity.WARNING
        return Severity.OK

    @property
    def failures(self) -> tuple[Invariant, ...]:
        return tuple(i for i in self.invariants if i.failed)

    @property
    def healthy(self) -> bool:
        return self.worst is Severity.OK


MIN_RESOLUTION_RATE = 0.90
MIN_DELAY_COVERAGE_RATE = 0.90
MAX_ACCEPTABLE_SKEW_SECONDS = 120.0


def evaluate_feed_health(data: FeedHealthInput) -> HealthReport:
    """Evalúa los invariantes de negocio sobre una pasada de ingesta."""
    invariants: list[Invariant] = []
    expected = band_for(data.local_time)
    in_service = is_service_hours(data.local_time)

    # --- R-1: el invariante que da nombre a este módulo ---
    count = data.entities_after_nucleo_filter
    if count == 0 and in_service:
        invariants.append(
            Invariant(
                name="nucleo_filter_yield",
                severity=Severity.CRITICAL,
                detail=(
                    f"0 entidades tras el filtro de núcleo a las {data.local_time:%H:%M} "
                    f"(banda: {expected.label}), con {data.entities_in_payload} entidades en "
                    "el payload. El feed trae datos pero NINGUNO se reconoce como Madrid: "
                    "el sintoma exacto de un cambio de numeracion de nucleo (ADR-010). "
                    "NO es 'no hay trenes'."
                ),
            )
        )
    elif count == 0:
        invariants.append(
            Invariant(
                name="nucleo_filter_yield",
                severity=Severity.OK,
                detail=f"0 entidades, pero son las {data.local_time:%H:%M}: fuera de servicio.",
            )
        )
    elif not expected.contains(count):
        invariants.append(
            Invariant(
                name="nucleo_filter_yield",
                severity=Severity.WARNING,
                detail=(
                    f"{count} entidades fuera del rango esperado "
                    f"[{expected.minimum}, {expected.maximum}] para {expected.label}."
                ),
            )
        )
    else:
        invariants.append(
            Invariant(
                name="nucleo_filter_yield",
                severity=Severity.OK,
                detail=f"{count} entidades, dentro de lo esperado para {expected.label}.",
            )
        )

    # --- El payload llega vacío: distinto problema, distinto responsable ---
    if data.entities_in_payload == 0:
        invariants.append(
            Invariant(
                name="payload_not_empty",
                severity=Severity.CRITICAL,
                detail="El feed no trae ninguna entidad: fallo del origen, no del filtro.",
            )
        )
    else:
        invariants.append(
            Invariant(
                name="payload_not_empty",
                severity=Severity.OK,
                detail=f"{data.entities_in_payload} entidades en el payload.",
            )
        )

    invariants.append(
        _rate_invariant(
            name="trip_id_resolution",
            observed=(data.trips_resolved, count),
            minimum=MIN_RESOLUTION_RATE,
            what="trip_id resueltos contra el GTFS estatico",
            hint="sintoma de GTFS estatico desactualizado o de cambio de formato de tripId",
        )
    )

    invariants.append(_delay_coverage_invariant(data))

    skew = abs(data.feed_skew_seconds)
    invariants.append(
        Invariant(
            name="feed_freshness",
            severity=Severity.OK if skew <= MAX_ACCEPTABLE_SKEW_SECONDS else Severity.WARNING,
            detail=f"desfase de {skew:.1f} s (umbral {MAX_ACCEPTABLE_SKEW_SECONDS:.0f} s).",
        )
    )

    return HealthReport(invariants=tuple(invariants))


def _delay_coverage_invariant(data: FeedHealthInput) -> Invariant:
    """Cobertura de retrasos sobre los viajes que circulan (sin suprimidos).

    El 29/10/2026 dio crítico con 52/65 (80 %): los 13 sin retraso eran todos
    ``CANCELED``. Un suprimido no puede publicar retraso; contarlo convertía
    cada jornada con supresiones en una alarma falsa (RB-015). Si todos los
    viajes están suprimidos, el denominador es 0 y se aplica el criterio común
    de «sin datos que evaluar» (OK): la falta de trenes ya la vigila
    ``nucleo_filter_yield``.
    """
    cancelados = max(data.trip_updates_cancelled, 0)
    denominator = max(data.trip_updates_total - cancelados, 0)
    numerator = max(data.trip_updates_with_delay - max(data.trip_updates_cancelled_with_delay, 0), 0)
    suffix = f" ({cancelados} suprimidos excluidos)" if cancelados else ""
    invariant = _rate_invariant(
        name="delay_coverage",
        observed=(numerator, denominator),
        minimum=MIN_DELAY_COVERAGE_RATE,
        what=f"tripUpdate con retraso publicado{suffix}",
        hint="sin esto el producto no puede responder a su pregunta central",
    )
    return replace(invariant, metrics={"n_cancelados": cancelados})


def _rate_invariant(
    *, name: str, observed: tuple[int, int], minimum: float, what: str, hint: str
) -> Invariant:
    numerator, denominator = observed
    if denominator == 0:
        return Invariant(
            name=name,
            severity=Severity.OK,
            detail=f"sin datos que evaluar ({what}).",
        )
    rate = numerator / denominator
    if rate >= minimum:
        return Invariant(
            name=name,
            severity=Severity.OK,
            detail=f"{numerator}/{denominator} ({rate:.1%}) {what}.",
        )
    return Invariant(
        name=name,
        severity=Severity.CRITICAL,
        detail=(f"solo {numerator}/{denominator} ({rate:.1%}) {what}; minimo exigido {minimum:.0%}. {hint}."),
    )
