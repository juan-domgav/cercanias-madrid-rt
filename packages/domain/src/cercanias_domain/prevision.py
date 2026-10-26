"""Retraso PREVISTO por el histórico, para trenes que Renfe aún no publica (RB-013).

Problema: muchas salidas aparecen como "sin datos en vivo" porque el tren todavía
no está en el feed de Renfe. Sí sabemos cómo suele ir ESE tren en ESA estación:
el backtest del 26/10/2026 (histórico 08/09-18/10, prueba 19-25/10) mostró que la
mejor previsión en todas las líneas es la mediana de su retraso histórico por
(número de tren, estación, tipo de día), sin atípicos (> 60 min o < -5 min).

Reglas de producto (no negociables):

* Una previsión NUNCA es un dato en vivo: se etiqueta "previsto ..." y el dato de
  Renfe manda en cuanto existe. Nunca se dice "en hora" por una previsión.
* Solo líneas en lista blanca (``PREVISTO_ROUTES``, vacía por defecto) y solo con
  al menos ``MIN_OBSERVATIONS`` días de muestra para ese tren/estación/tipo de día.
* Fallo seguro: un artefacto ausente o roto significa "sin previsión", nada más.

Este módulo es Python puro (sin pyarrow): la tabla se construye offline con
``scripts/construir_previsto.py`` y el worker solo carga el JSON resultante.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

SCHEMA = "cercanias-previsto/1"
DAY_TYPE_LABORABLE = "laborable"
DAY_TYPE_FESTIVO = "festivo"
DAY_TYPES = (DAY_TYPE_LABORABLE, DAY_TYPE_FESTIVO)
OUTLIER_MIN_MINUTES = -5.0
OUTLIER_MAX_MINUTES = 60.0
MIN_OBSERVATIONS = 5
# La jornada de Cercanías empieza de madrugada: antes de las 04:00 aún es el día anterior.
OPERATING_DAY_START_HOUR = 4
WITHIN_MINUTES = 2
_SATURDAY = 5  # date.weekday(): sábado y domingo son 5 y 6
_ENTRY_LEN = 2  # [mediana, n]


class PredictionTableError(ValueError):
    """El artefacto de previsión no es válido: se trata como si no existiera."""


@dataclass(frozen=True, slots=True)
class Observation:
    """Un paso medido de un tren por una estación (una fila del histórico)."""

    line: str
    train: str
    stop_id: str
    service_date: date
    delay_seconds: float


@dataclass(frozen=True, slots=True)
class Prediction:
    line: str
    train: str
    stop_id: str
    day_type: str
    median_minutes: float
    observations: int

    @property
    def delay_minutes(self) -> int:
        return predicted_minutes(self.median_minutes)


def predicted_minutes(median: float) -> int:
    """Redondeo al minuto (0,5 hacia arriba; nunca redondeo bancario)."""
    return math.floor(median + 0.5)


def day_type(service_date: date, holidays: frozenset[date]) -> str:
    if service_date.weekday() >= _SATURDAY or service_date in holidays:
        return DAY_TYPE_FESTIVO
    return DAY_TYPE_LABORABLE


def operating_date(now: datetime) -> date:
    """Fecha de la jornada de servicio a la que pertenece ``now`` (hora local)."""
    if now.hour < OPERATING_DAY_START_HOUR:
        return (now - timedelta(days=1)).date()
    return now.date()


def _in_range(minutes: float) -> bool:
    return OUTLIER_MIN_MINUTES <= minutes <= OUTLIER_MAX_MINUTES


def build_table(
    observations: Iterable[Observation],
    *,
    lines: frozenset[str],
    holidays: frozenset[date],
    generated_at: datetime,
    min_observations: int = MIN_OBSERVATIONS,
) -> dict[str, Any]:
    """Tabla de medianas lista para serializar a JSON (el artefacto)."""
    grupos: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    dias: set[date] = set()
    for obs in observations:
        if obs.line not in lines:
            continue
        minutos = obs.delay_seconds / 60
        if not math.isfinite(minutos) or not _in_range(minutos):
            continue
        dias.add(obs.service_date)
        grupos[(obs.line, obs.stop_id, obs.train, day_type(obs.service_date, holidays))].append(minutos)

    entries: dict[str, dict[str, dict[str, dict[str, list[float | int]]]]] = {}
    for (linea, parada, tren, tipo), valores in sorted(grupos.items()):
        if len(valores) < min_observations:
            continue
        mediana = round(statistics.median(valores), 2)
        entries.setdefault(linea, {}).setdefault(parada, {}).setdefault(tren, {})[tipo] = [
            mediana, len(valores),
        ]
    return {
        "schema": SCHEMA,
        "generated_at": generated_at.isoformat(),
        "method": "mediana del retraso por (tren, estación, tipo de día); RB-013",
        "lines": sorted(lines),
        "holidays": sorted(d.isoformat() for d in holidays),
        "min_observations": min_observations,
        "outliers_minutes": {"min": int(OUTLIER_MIN_MINUTES), "max": int(OUTLIER_MAX_MINUTES)},
        "history": {
            "from": min(dias).isoformat() if dias else None,
            "to": max(dias).isoformat() if dias else None,
            "days": len(dias),
            "festivo_days": sum(1 for d in dias if day_type(d, holidays) == DAY_TYPE_FESTIVO),
        },
        "entries": entries,
    }


def _reject(motivo: str) -> PredictionTableError:
    return PredictionTableError(motivo)


def _no_constants(token: str) -> float:
    raise _reject(f"constante JSON no permitida: {token}")


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class PredictionTable:
    generated_at: datetime
    lines: frozenset[str]
    holidays: frozenset[date]
    min_observations: int
    history: Mapping[str, Any]
    # parada -> tren -> tipo de día -> previsión
    _by_stop: Mapping[str, Mapping[str, Mapping[str, Prediction]]]

    @property
    def entry_count(self) -> int:
        return sum(len(tipos) for trenes in self._by_stop.values() for tipos in trenes.values())

    @classmethod
    def from_bytes(cls, raw: bytes) -> PredictionTable:
        try:
            datos = json.loads(raw.decode("utf-8"), parse_constant=_no_constants)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _reject(f"JSON ilegible: {exc}") from exc
        return cls.from_json(datos)

    @classmethod
    def from_json(cls, datos: object) -> PredictionTable:
        if not isinstance(datos, dict):
            raise _reject("la raíz no es un objeto")
        if datos.get("schema") != SCHEMA:
            raise _reject(f"schema desconocido: {datos.get('schema')!r}")
        lineas = datos.get("lines")
        if not isinstance(lineas, list) or not all(isinstance(x, str) for x in lineas):
            raise _reject("lines debe ser una lista de textos")
        minimo = datos.get("min_observations")
        if not isinstance(minimo, int) or isinstance(minimo, bool) or minimo < 1:
            raise _reject("min_observations debe ser un entero >= 1")
        return cls(
            generated_at=_parse_generated_at(datos.get("generated_at")),
            lines=frozenset(lineas),
            holidays=_parse_holidays(datos.get("holidays")),
            min_observations=minimo,
            history=dict(datos.get("history") or {}),
            _by_stop=_parse_entries(datos.get("entries"), frozenset(lineas), minimo),
        )

    def lookup(self, stop_id: str, train: str, tipo: str) -> Prediction | None:
        return self._by_stop.get(stop_id, {}).get(train, {}).get(tipo)

    def for_station(self, stop_id: str, tipo: str, routes: frozenset[str]) -> dict[str, Prediction]:
        """Previsiones de una estación (tren -> previsión) para las líneas permitidas."""
        if not routes:
            return {}
        salida: dict[str, Prediction] = {}
        for tren, tipos in self._by_stop.get(stop_id, {}).items():
            pred = tipos.get(tipo)
            if pred is not None and pred.line in routes:
                salida[tren] = pred
        return salida


def _parse_generated_at(valor: object) -> datetime:
    try:
        generado = datetime.fromisoformat(str(valor))
    except ValueError as exc:
        raise _reject("generated_at no es una fecha ISO") from exc
    if generado.tzinfo is None:
        raise _reject("generated_at sin zona horaria")
    return generado


def _parse_holidays(valor: object) -> frozenset[date]:
    if not isinstance(valor, list):
        raise _reject("holidays debe ser una lista")
    try:
        return frozenset(date.fromisoformat(str(x)) for x in valor)
    except ValueError as exc:
        raise _reject("holidays con una fecha inválida") from exc


def _parse_entries(
    entradas: object, lineas: frozenset[str], minimo: int,
) -> dict[str, dict[str, dict[str, Prediction]]]:
    if not isinstance(entradas, dict):
        raise _reject("entries debe ser un objeto")
    por_parada: dict[str, dict[str, dict[str, Prediction]]] = {}
    for linea, paradas in entradas.items():
        if linea not in lineas:
            raise _reject(f"entries trae la línea no declarada {linea!r}")
        if not isinstance(paradas, dict):
            raise _reject(f"{linea}: se esperaba un objeto de estaciones")
        for parada, trenes in paradas.items():
            if not isinstance(trenes, dict):
                raise _reject(f"{linea}/{parada}: se esperaba un objeto de trenes")
            for tren, tipos in trenes.items():
                if not isinstance(tipos, dict):
                    raise _reject(f"{linea}/{parada}/{tren}: se esperaba un objeto")
                for tipo, valor in tipos.items():
                    por_parada.setdefault(parada, {}).setdefault(tren, {})[tipo] = _entry(
                        (linea, parada, tren, tipo), valor, minimo,
                    )
    return por_parada


def _entry(clave: tuple[str, str, str, str], valor: object, minimo: int) -> Prediction:
    linea, parada, tren, tipo = clave
    donde = "/".join(clave)
    if tipo not in DAY_TYPES:
        raise _reject(f"{donde}: tipo de día desconocido")
    if not isinstance(valor, list) or len(valor) != _ENTRY_LEN:
        raise _reject(f"{donde}: se esperaba [mediana, n]")
    mediana, n = valor
    if not _is_number(mediana) or not math.isfinite(mediana):
        raise _reject(f"{donde}: mediana no numérica")
    if not _in_range(float(mediana)):
        raise _reject(f"{donde}: mediana fuera de [-5, 60]")
    if not isinstance(n, int) or isinstance(n, bool) or n < minimo:
        raise _reject(f"{donde}: muestras insuficientes")
    return Prediction(linea, tren, parada, tipo, float(mediana), n)


def load_prediction_table(raw: bytes | None) -> tuple[PredictionTable | None, str | None]:
    """Carga a prueba de fallos: nunca lanza. Devuelve (tabla, motivo_del_error)."""
    if raw is None:
        return None, "missing"
    try:
        return PredictionTable.from_bytes(raw), None
    except PredictionTableError as exc:
        return None, f"invalid: {exc}"


def evaluate(
    table: PredictionTable, observations: Iterable[Observation], holidays: frozenset[date],
) -> dict[str, dict[str, float | int]]:
    """Mide la tabla contra un periodo de prueba, por línea.

    ``*_predicted`` / ``*_on_time`` comparan, SOLO en las salidas con previsión,
    la previsión redondeada frente a suponer "en hora" (0). ``mae_all_*`` mide la
    línea entera con "sin previsión" = 0, comparable con el backtest.
    """
    por_linea: dict[str, list[tuple[float, int | None]]] = defaultdict(list)
    for obs in observations:
        real = obs.delay_seconds / 60
        if not math.isfinite(real) or not _in_range(real):
            continue
        pred = table.lookup(obs.stop_id, obs.train, day_type(obs.service_date, holidays))
        prevision = pred.delay_minutes if pred is not None and pred.line == obs.line else None
        por_linea[obs.line].append((real, prevision))

    resultado: dict[str, dict[str, float | int]] = {}
    for linea, filas in sorted(por_linea.items()):
        cubiertas = [(r, p) for r, p in filas if p is not None]
        err_pred = [abs(r - p) for r, p in cubiertas]
        err_cero = [abs(r) for r, _ in cubiertas]
        mae_pred = statistics.fmean(err_pred) if err_pred else 0.0
        mae_cero = statistics.fmean(err_cero) if err_cero else 0.0
        resultado[linea] = {
            "n": len(filas),
            "covered": len(cubiertas),
            "coverage_pct": round(100 * len(cubiertas) / len(filas), 1),
            "mae_predicted": round(mae_pred, 2),
            "mae_on_time": round(mae_cero, 2),
            "within2_predicted_pct": _pct(err_pred),
            "within2_on_time_pct": _pct(err_cero),
            "gain_pct": round(100 * (1 - mae_pred / mae_cero), 1) if mae_cero else 0.0,
            "mae_all_fallback_on_time": round(
                statistics.fmean(abs(r - (p or 0)) for r, p in filas), 2),
            "mae_all_on_time": round(statistics.fmean(abs(r) for r, _ in filas), 2),
        }
    return resultado


def _pct(errores: list[float]) -> float:
    if not errores:
        return 0.0
    return round(100 * sum(1 for e in errores if e <= WITHIN_MINUTES) / len(errores), 1)
