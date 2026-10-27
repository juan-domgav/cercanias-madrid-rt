"""Supresiones, paradas omitidas y avisos de línea para el histórico (RB-010, RB-014).

Fuente: lo que el job de ingesta guarda con ``ARCHIVAR_CANCELACIONES=1`` y
``ARCHIVAR_AVISOS=1`` (PR del archivo de incidencias). Sin E/S: el script
``scripts/construir_historico.py`` descarga, llama a esto y escribe Parquet.

Nulo ≠ falso. Cada objeto de pasada lleva ``canc``/``skip`` sólo si se
registraron (bandera encendida y trip_updates leído). Por eso:

- Ninguna pasada del día con registro → todas las columnas a ``None`` (días
  anteriores a encender la bandera: no hay backfill posible).
- Alguna pasada con registro → ``True``/``False``. Si la bandera se encendió a
  media jornada, lo anterior no consta: ``pasadas_con_registro`` va en el
  informe de calidad para saberlo.

Los avisos se archivan sólo cuando cambian: cada objeto es una «foto» del
conjunto vigente desde su ``ts`` hasta la foto siguiente.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from cercanias_domain.historico import Observacion, familia, normalizar


@dataclass(frozen=True)
class PasadaIncidencias:
    ts: datetime
    #: ``None`` = no registrado en esta pasada (clave ausente o malformada).
    canc: frozenset[str] | None
    skip: dict[str, frozenset[str]] | None


def _lista_str(valor: Any) -> frozenset[str] | None:
    if not isinstance(valor, list) or not all(isinstance(x, str) for x in valor):
        return None
    return frozenset(valor)


def pasada_desde_objeto(objeto: Mapping[str, Any], ts: datetime) -> PasadaIncidencias:
    """Lee ``canc``/``skip`` de un objeto de ``archivo/<día>/``; ambos o ninguno."""
    canc = _lista_str(objeto.get("canc"))
    crudo = objeto.get("skip")
    skip: dict[str, frozenset[str]] | None = None
    if isinstance(crudo, dict):
        convertidas = {t: _lista_str(p) for t, p in crudo.items()}
        if all(p is not None for p in convertidas.values()):
            skip = {t: p for t, p in convertidas.items() if p is not None}
    if canc is None or skip is None:
        return PasadaIncidencias(ts, None, None)
    return PasadaIncidencias(ts, canc, skip)


@dataclass
class IncidenciasDia:
    registrado: bool = False
    pasadas_con_registro: int = 0
    #: ``trip_id`` canónico -> primera vez que se vio suprimido.
    cancelados: dict[str, datetime] = field(default_factory=dict)
    omitidas: dict[str, frozenset[str]] = field(default_factory=dict)

    def cancelado(self, trip_id: str) -> bool | None:
        return trip_id in self.cancelados if self.registrado else None

    def parada_omitida(self, trip_id: str, stop_id: str) -> bool | None:
        return stop_id in self.omitidas.get(trip_id, frozenset()) if self.registrado else None

    def recorrido_acortado(self, trip_id: str, origen: str | None, destino: str | None) -> bool | None:
        """El viaje omite su origen o su destino: empieza o termina antes (recorrido limitado)."""
        if not self.registrado:
            return None
        extremos = {s for s in (origen, destino) if s}
        return bool(self.omitidas.get(trip_id, frozenset()) & extremos)

    def cancelados_por_linea_hora(self, hora_salida: Mapping[str, int]) -> Counter[tuple[str, int]] | None:
        """Suprimidos por (familia, hora de salida programada).

        Sin horario para el viaje, la hora es la de la primera vez que se vio
        suprimido (Renfe los publica antes de su salida, así que es aproximada).
        """
        if not self.registrado:
            return None
        conteo: Counter[tuple[str, int]] = Counter()
        for trip_id, visto in self.cancelados.items():
            fam = familia(trip_id)
            if fam is None:
                continue
            conteo[(fam, hora_salida.get(trip_id, visto.hour))] += 1
        return conteo


def _canonico(trip_id: str, ts: datetime, dia: date) -> str | None:
    n = normalizar(trip_id, Observacion(ts, None, None, None))
    return n.trip_id if n is not None and n.fecha_servicio == dia else None


def incidencias_del_dia(pasadas: Iterable[PasadaIncidencias], dia: date) -> IncidenciasDia:
    """Acumula supresiones y paradas omitidas de los viajes del día de servicio ``dia``."""
    inc = IncidenciasDia()
    omitidas: dict[str, set[str]] = {}
    for p in sorted(pasadas, key=lambda x: x.ts):
        if p.canc is None or p.skip is None:
            continue
        inc.registrado = True
        inc.pasadas_con_registro += 1
        for tid in p.canc:
            canon = _canonico(tid, p.ts, dia)
            if canon is not None:
                inc.cancelados.setdefault(canon, p.ts)
        for tid, paradas in p.skip.items():
            canon = _canonico(tid, p.ts, dia)
            if canon is not None:
                omitidas.setdefault(canon, set()).update(paradas)
    inc.omitidas = {t: frozenset(s) for t, s in omitidas.items()}
    return inc


# --- avisos ---------------------------------------------------------------------


@dataclass(frozen=True)
class FotoAvisos:
    ts: datetime
    alerts: tuple[dict[str, Any], ...]


def _fecha(valor: Any) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None


def foto_desde_objeto(objeto: Mapping[str, Any]) -> FotoAvisos | None:
    """Lee un objeto de ``archivo/alerts/<día>/``; ``None`` si no es legible."""
    momento = _fecha(objeto.get("ts"))
    alerts = objeto.get("alerts")
    if momento is None or not isinstance(alerts, list):
        return None
    return FotoAvisos(momento, tuple(a for a in alerts if isinstance(a, dict)))


@dataclass(frozen=True)
class AlertaLinea:
    activa: bool | None
    tipo: str | None
    min_desde: int | None


_DESCONOCIDO = AlertaLinea(None, None, None)
_NINGUNA = AlertaLinea(False, None, None)
_RE_FAMILIA = re.compile(r"C\d+")


def _familias(lineas: Any) -> set[str]:
    if not isinstance(lineas, list):
        return set()
    return {m.group(0) for x in lineas if isinstance(x, str) and (m := _RE_FAMILIA.match(x))}


def alerta_linea(fotos: Sequence[FotoAvisos], fam: str | None, momento: datetime | None) -> AlertaLinea:
    """Aviso vigente en la familia ``fam`` (``C3`` cubre ``C3a``) en ``momento``.

    ``fotos`` ordenadas por ``ts``. Vale la foto más reciente con ``ts <= momento``;
    antes de la primera foto no se sabe (``None``). Si hay varios avisos vigentes
    gana el de ``start`` más reciente (el más nuevo explica mejor lo que pasa);
    sin ``start`` legible cuenta como el más antiguo y ``min_desde`` es ``None``.
    """
    if momento is None or fam is None:
        return _DESCONOCIDO
    k = bisect_right([f.ts for f in fotos], momento)
    if k == 0:
        return _DESCONOCIDO
    vigentes: list[tuple[datetime | None, str, dict[str, Any]]] = []
    for a in fotos[k - 1].alerts:
        if fam not in _familias(a.get("lines")):
            continue
        ini, fin = _fecha(a.get("start")), _fecha(a.get("end"))
        if (ini is not None and ini > momento) or (fin is not None and momento > fin):
            continue
        vigentes.append((ini, str(a.get("id", "")), a))
    if not vigentes:
        return _NINGUNA
    ini, _, elegido = max(vigentes, key=lambda v: (v[0] is not None, v[0] or momento, v[1]))
    minutos = int((momento - ini).total_seconds() // 60) if ini is not None else None
    tipo = elegido.get("tipo")
    return AlertaLinea(True, tipo if isinstance(tipo, str) else None, minutos)
