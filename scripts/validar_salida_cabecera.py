#!/usr/bin/env python3
"""RB-009: ¿el retraso que Renfe publica ANTES de salir de cabecera es una medida?

Por cada viaje que empieza en una cabecera (``seq == 1``) y cada día del archivo:

- ``reportado``: el ``delay`` publicado mientras el tren seguía en su origen, en el
  momento de decidir (``t0 = salida programada - ANTELACION``).
- ``real``: la salida observada, transición ``STOPPED_AT`` -> ``IN_TRANSIT_TO`` con
  ``next_stop`` = origen (así marca Renfe la salida), menos la hora programada.
- Estimadores con lo que se sabía en ``t0``:
  ``pair`` (entrante enlazado en el horario, ``from``: block_id o 1:1),
  ``pool`` (primera unidad que llega a esa cabecera, asignación por orden),
  ``cong`` (mediana del retraso de los que llegan a esa cabecera en ~30 min).
  Estimado = ``max(0, llegada + maniobra mínima - salida programada)``.

Uso (tras bajar el archivo y los horarios de esos días)::

    gcloud storage cp -r gs://cercanias-madrid-rt-privado/archivo/2026-10-19 ... /tmp/arch/
    curl -o /tmp/h_1090L.json https://storage.googleapis.com/cercanias-madrid-rt-web/horarios/1090L.json
    python scripts/validar_salida_cabecera.py /tmp/arch /tmp 2026-10-19=1090L 2026-10-20=1091M ...
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ANTELACIONES = (5, 10)
MANIOBRA_MIN = 3
TARDE = 3
TOLERANCIA = 3
#: Sin observación hace más de esto = ya llegó (desaparece del feed al terminar).
DESAPARECIDO_MIN = 3
#: Ventana y frescura del indicador de congestión.
CONG_VENTANA_MIN = 30
CONG_FRESCO_MIN = 5
MIN_PARADAS = 4
CERO = 0.5
#: Horas punta (minutos del día): 07:00-09:30 y 17:00-20:00.
PUNTA = ((420, 570), (1020, 1200))
FAMILIAS = ("C10", "C1", "C2", "C3", "C4", "C5", "C7", "C8", "C9")

Obs = tuple[float, int | None, str | None, str | None]


def _min(hhmmss: str) -> float:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 60 + m + s / 60


def _familia(route: str) -> str:
    return next((f for f in FAMILIAS if route.startswith(f)), route)


@dataclass
class Dia:
    viajes: dict[str, list[tuple[int, str, float, dict[str, Any]]]]
    obs: dict[str, list[Obs]]
    llegadas: dict[str, list[tuple[float, str]]] = field(default_factory=dict)

    def estado(self, tid: str, t0: float) -> Obs | None:
        ultimo = None
        for o in self.obs.get(tid, ()):
            if o[0] > t0:
                break
            ultimo = o
        return ultimo

    def llegada(self, tid: str, parada: str, t0: float) -> float | None:
        """Llegada (real si ya ocurrió) a ``parada`` según lo sabido en ``t0``."""
        for o in self.obs.get(tid, ()):
            if o[0] > t0:
                break
            if o[2] == parada and o[3] == "STOPPED_AT":
                return o[0]
        s = self.estado(tid, t0)
        if s is None or s[1] is None:
            return None
        prevista = self.viajes[tid][-1][2] + s[1] / 60
        return max(s[0], prevista) if t0 - s[0] > DESAPARECIDO_MIN else prevista


def cargar_dia(arch: Path, horarios: Path, dia: str, servicio: str) -> Dia:
    datos = json.loads((horarios / f"h_{servicio}.json").read_text())
    viajes: dict[str, list[tuple[int, str, float, dict[str, Any]]]] = defaultdict(list)
    for parada, filas in datos["stations"].items():
        for r in filas:
            viajes[r["trip"]].append((r["seq"], parada, _min(r["t"]), r))
    for v in viajes.values():
        v.sort(key=lambda x: x[0])
    obs: dict[str, list[Obs]] = defaultdict(list)
    for f in sorted((arch / dia).glob("*.json")):
        a = json.loads(f.read_text())
        ts = datetime.fromisoformat(a["ts"])
        t = ts.hour * 60 + ts.minute + ts.second / 60
        i = {c: k for k, c in enumerate(a["cols"])}
        for tr in a["trains"]:
            if tr[i["trip"]]:
                obs[tr[i["trip"]]].append((t, tr[i["delay"]], tr[i["next_stop"]], tr[i["status"]]))
    d = Dia(dict(viajes), dict(obs))
    llegadas: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for tid, paradas in viajes.items():
        if paradas[-1][3].get("terminates"):
            llegadas[paradas[-1][1]].append((paradas[-1][2], tid))
    d.llegadas = dict(llegadas)
    return d


def _salida_real(os_: list[Obs], origen: str) -> float | None:
    previo: Obs | None = None
    for o in os_:
        if o[2] == origen and o[3] == "IN_TRANSIT_TO" and previo and previo[2] == origen \
                and previo[3] == "STOPPED_AT":
            return o[0]
        previo = o
    return None


def _reportado(os_: list[Obs], origen: str, t0: float, salida: float) -> float | None:
    mejor = None
    for o in os_:
        if o[0] > t0:
            break
        if o[2] == origen and o[0] < salida:
            mejor = o
    return None if mejor is None or mejor[1] is None else mejor[1] / 60


@dataclass(frozen=True)
class Salida:
    tid: str
    origen: str
    sal: float
    t0: float


def _estimadores(d: Dia, x: Salida, usadas: set[str]) -> dict[str, float | None]:
    tid, origen, sal, t0 = x.tid, x.origen, x.sal, x.t0
    r0 = d.viajes[tid][0][3]
    pair = None
    if r0.get("from") in d.viajes:
        ll = d.llegada(r0["from"], origen, t0)
        pair = None if ll is None else max(0.0, ll + MANIOBRA_MIN - sal)
    cands = []
    for prog, itid in d.llegadas.get(origen, ()):
        if sal - 60 <= prog <= sal and itid not in usadas \
                and _familia(d.viajes[itid][-1][3]["route"]) == _familia(r0["route"]):
            ll = d.llegada(itid, origen, t0)
            if ll is not None:
                cands.append((ll, itid))
    pool = None
    if cands:
        ll, itid = min(cands)
        usadas.add(itid)
        pool = max(0.0, ll + MANIOBRA_MIN - sal)
    retrasos = []
    for prog, itid in d.llegadas.get(origen, ()):
        s = d.estado(itid, t0)
        if t0 - CONG_VENTANA_MIN <= prog <= t0 + CONG_FRESCO_MIN and s and s[1] is not None \
                and t0 - s[0] < CONG_FRESCO_MIN:
            retrasos.append(s[1] / 60)
    return {"pair": pair, "pool": pool, "cong": statistics.median(retrasos) if retrasos else None}


def analizar(d: Dia, servicio: str, antelacion: int) -> list[dict[str, Any]]:
    salidas = sorted(
        (p[0][2], tid, p[0][1]) for tid, p in d.viajes.items()
        if tid.startswith(servicio) and p[0][0] == 1 and len(p) >= MIN_PARADAS
    )
    usadas: dict[str, set[str]] = defaultdict(set)
    filas = []
    for sal, tid, origen in salidas:
        os_ = d.obs.get(tid, [])
        real = _salida_real(os_, origen)
        t0 = sal - antelacion
        rep = _reportado(os_, origen, t0, real) if real is not None else None
        if real is None or rep is None:
            continue
        r0 = d.viajes[tid][0][3]
        filas.append({"trip": tid, "fam": _familia(r0["route"]), "origen": origen, "D": sal,
                      "rep": rep, "real": real - sal,
                      **_estimadores(d, Salida(tid, origen, sal, t0), usadas[origen])})
    return filas


def _acierto(g: list[dict[str, Any]], k: str) -> str:
    e = [r for r in g if r[k] is not None]
    if not e:
        return "—"
    hit = sum(abs(r[k] - r["real"]) <= TOLERANCIA for r in e) / len(e)
    pred = [r for r in e if r[k] >= TARDE]
    prec = sum(r["real"] >= TARDE for r in pred) / len(pred) if pred else float("nan")
    return f"{100 * hit:.0f}% (n={len(e)}, P{100 * prec:.0f})"


def informe(filas: list[dict[str, Any]], antelacion: int) -> None:
    ceros = [r for r in filas if abs(r["rep"]) < CERO]
    print(f"\nAntelación {antelacion} min: {len(filas)} salidas, {len(ceros)} con 0 publicado")
    print("| línea | n | sale ≥3 tarde | punta ≥3 | pair | pool | cong |")
    por: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ceros:
        por[r["fam"]].append(r)
    for fam, g in [*sorted(por.items(), key=lambda kv: -len(kv[1])), ("TODAS", ceros)]:
        punta = [r for r in g if any(a <= r["D"] < b for a, b in PUNTA)]
        tarde = 100 * sum(r["real"] >= TARDE for r in g) / len(g)
        tp = 100 * sum(r["real"] >= TARDE for r in punta) / len(punta) if punta else 0
        print(f"| {fam} | {len(g)} | {tarde:.0f}% | {tp:.0f}% | {_acierto(g, 'pair')} | "
              f"{_acierto(g, 'pool')} | {_acierto(g, 'cong')} |")


def main(argv: list[str]) -> None:
    arch, horarios = Path(argv[1]), Path(argv[2])
    dias = [a.split("=") for a in argv[3:]]
    cargados = [(cargar_dia(arch, horarios, dia, svc), svc) for dia, svc in dias]
    for antelacion in ANTELACIONES:
        filas = [f for d, svc in cargados for f in analizar(d, svc, antelacion)]
        informe(filas, antelacion)


if __name__ == "__main__":
    main(sys.argv)
