#!/usr/bin/env python3
"""RB-012: backtest del emparejamiento de vuelta en cabecera (``from``) con maniobra minima.

Compara, por cabecera, el enlace actual (llegada sin consumir mas proxima, maniobra
>= 0) con el enlace que exige una maniobra minima de N minutos en esa cabecera
(``MIN_TURNAROUND_BY_TERMINUS``). Misma vara que ``validar_salida_cabecera.py``:

- ``real``: salida observada (``STOPPED_AT`` -> ``IN_TRANSIT_TO`` con ``next_stop`` =
  origen) menos la hora programada, en minutos.
- estimado en ``t0 = salida - A`` (A = 5 y 10 min), con lo sabido en ``t0``:
  ``max(0, llegada del entrante + MANIOBRA_MIN - salida)``. La llegada es la real
  si ya ocurrio, si no la programada + el ultimo ``delay`` publicado.
- acierto: ``|estimado - real| <= 3`` min. ``n`` = salidas con salida real observada;
  ``cob`` = las que tienen estimacion; ``ok`` = aciertos / ``n`` (sin estimacion
  cuenta como fallo) y ``prec`` = aciertos / ``cob``.

Ajuste y validacion en dias DISTINTOS: N se elige por cabecera en los dias de
ajuste (mejor ``ok`` medio en A=5 y A=10 entre ``CANDIDATOS``) y solo se adopta si
supera a N=0 en ``MEJORA_MINIMA`` puntos con ``n >= N_MINIMO``; la tabla final se
mide en los dias de validacion.

Uso::

    python scripts/backtest_emparejamiento_vuelta.py ARCHIVO HORARIOS \\
        --ajuste 2026-10-14=1085X 2026-10-15=1086J ... --validacion 2026-10-20=1091M ...

``ARCHIVO/<dia>/*.json`` es el archivo privado; ``HORARIOS/<servicio>.json`` los
horarios publicados de esos dias (su ``from`` debe coincidir con N=0: se comprueba).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cercanias_worker.regenerate_schedules import StopTime, Trip, _link_turnarounds, parse_min_turnaround

ANTELACIONES = (5, 10)
MANIOBRA_MIN = 3
TOLERANCIA = 3
DESAPARECIDO_MIN = 3
MIN_PARADAS = 4
CANDIDATOS = (0, 3, 5, 7, 9, 12)
MEJORA_MINIMA = 3.0
N_MINIMO = 30
#: "f1" (por defecto): F1 de los avisos de retraso. "ok" premia decir 0 (la mayoria
#: de salidas va en hora) y por tanto N grandes aunque no emparejen mejor.
OBJETIVO = "f1"

Obs = tuple[float, int | None, str | None, str | None]


def _min(hhmmss: str) -> float:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 60 + m + s / 60


@dataclass
class Dia:
    servicio: str
    viajes: dict[str, list[tuple[int, str, str, dict[str, Any]]]]
    obs: dict[str, list[Obs]] = field(default_factory=dict)

    def llegada(self, tid: str, parada: str, t0: float) -> float | None:
        ultimo: Obs | None = None
        for o in self.obs.get(tid, ()):
            if o[0] > t0:
                break
            if o[2] == parada and o[3] == "STOPPED_AT":
                return o[0]
            ultimo = o
        if ultimo is None or ultimo[1] is None:
            return None
        prevista = _min(self.viajes[tid][-1][2]) + ultimo[1] / 60
        return max(ultimo[0], prevista) if t0 - ultimo[0] > DESAPARECIDO_MIN else prevista

    def trips(self) -> list[Trip]:
        out = []
        for tid, paradas in self.viajes.items():
            if not tid.startswith(self.servicio):
                continue
            stops = [StopTime(p, t, t, seq) for seq, p, t, _r in paradas]
            out.append(Trip(tid, self.servicio, str(paradas[0][3]["route"]), stops))
        return out


def cargar(arch: Path, horarios: Path, dia: str, servicio: str) -> Dia:
    datos = json.loads((horarios / f"{servicio}.json").read_text())
    viajes: dict[str, list[tuple[int, str, str, dict[str, Any]]]] = defaultdict(list)
    for parada, filas in datos["stations"].items():
        for r in filas:
            viajes[r["trip"]].append((r["seq"], parada, r["t"], r))
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
    return Dia(servicio, dict(viajes), dict(obs))


def _salida_real(os_: list[Obs], origen: str) -> float | None:
    previo: Obs | None = None
    for o in os_:
        if (
            o[2] == origen and o[3] == "IN_TRANSIT_TO" and previo
            and previo[2] == origen and previo[3] == "STOPPED_AT"
        ):
            return o[0]
        previo = o
    return None


def filas(d: Dia, enlaces: dict[str, str | None]) -> list[dict[str, Any]]:
    """Una fila por salida de cabecera con salida real observada y antelacion."""
    out = []
    for tid, p in d.viajes.items():
        if not tid.startswith(d.servicio) or p[0][0] != 1 or len(p) < MIN_PARADAS:
            continue
        origen, sal = p[0][1], _min(p[0][2])
        real = _salida_real(d.obs.get(tid, []), origen)
        if real is None:
            continue
        fila: dict[str, Any] = {"trip": tid, "origen": origen, "real": real - sal}
        entrante = enlaces.get(tid)
        for a in ANTELACIONES:
            ll = d.llegada(entrante, origen, sal - a) if entrante in d.viajes else None
            fila[f"e{a}"] = None if ll is None else max(0.0, ll + MANIOBRA_MIN - sal)
        out.append(fila)
    return out


def enlaces_para(d: Dia, minimos: dict[str, int]) -> dict[str, str | None]:
    return {k: v.inbound_trip_id for k, v in _link_turnarounds(d.trips(), minimos).items()}


def metricas(fs: list[dict[str, Any]]) -> dict[str, float]:
    m: dict[str, float] = {"n": len(fs)}
    for a in ANTELACIONES:
        e = [f for f in fs if f[f"e{a}"] is not None]
        hit = sum(abs(f[f"e{a}"] - f["real"]) <= TOLERANCIA for f in e)
        m[f"cob{a}"] = len(e)
        m[f"ok{a}"] = 100 * hit / len(fs) if fs else float("nan")
        m[f"prec{a}"] = 100 * hit / len(e) if e else float("nan")
        # Lo que importa en produccion: avisos de retraso (estimado >= 3).
        avisos = [f for f in e if f[f"e{a}"] >= TOLERANCIA]
        m[f"av{a}"] = len(avisos)
        m[f"avok{a}"] = sum(abs(f[f"e{a}"] - f["real"]) <= TOLERANCIA for f in avisos)
    # Referencia ingenua: decir siempre 0 (en hora) acierta esto.
    m["cero"] = 100 * sum(abs(f["real"]) <= TOLERANCIA for f in fs) / len(fs) if fs else float("nan")
    tarde = [f for f in fs if f["real"] >= TOLERANCIA]
    m["tarde"] = len(tarde)
    for a in ANTELACIONES:
        m[f"tok{a}"] = sum(
            f[f"e{a}"] is not None and abs(f[f"e{a}"] - f["real"]) <= TOLERANCIA for f in tarde
        )
        # F1 de los avisos: acierto = aviso (>=3) a +-3 de una salida real >=3.
        vp = sum(
            f[f"e{a}"] is not None and f[f"e{a}"] >= TOLERANCIA and abs(f[f"e{a}"] - f["real"]) <= TOLERANCIA
            for f in tarde
        )
        p_, r_ = (vp / m[f"av{a}"] if m[f"av{a}"] else 0.0), (vp / len(tarde) if tarde else 0.0)
        m[f"f1{a}"] = 100 * 2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0.0
        m[f"vp{a}"] = vp
    return m


def por_origen(dias: list[Dia], minimos: dict[str, int]) -> dict[str, list[dict[str, Any]]]:
    g: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in dias:
        for f in filas(d, enlaces_para(d, minimos)):
            g[f["origen"]].append(f)
    return g


def ajustar(dias: list[Dia]) -> tuple[dict[str, int], dict[str, dict[int, float]]]:
    puntuaciones: dict[str, dict[int, float]] = defaultdict(dict)
    ns: dict[str, int] = {}
    origenes = {p[0][1] for d in dias for p in d.viajes.values() if p[0][0] == 1}
    for n in CANDIDATOS:
        # Mismo N en todas las cabeceras: cada salida solo depende de su cabecera.
        for origen, fs in por_origen(dias, dict.fromkeys(origenes, n)).items():
            m = metricas(fs)
            clave = "f1" if OBJETIVO == "f1" else "ok"
            puntuaciones[origen][n] = (m[f"{clave}5"] + m[f"{clave}10"]) / 2
            ns[origen] = len(fs)
    elegido: dict[str, int] = {}
    for origen, p in puntuaciones.items():
        mejor = max(p, key=lambda n: (p[n], -n))
        if mejor and ns[origen] >= N_MINIMO and p[mejor] - p[0] >= MEJORA_MINIMA:
            elegido[origen] = mejor
    return elegido, dict(puntuaciones)


def comprobar_from(d: Dia) -> tuple[int, int]:
    """Cuantos ``from`` publicados reproduce el enlace con N=0 (debe ser todos)."""
    actuales = enlaces_para(d, {})
    total = iguales = 0
    for tid, p in d.viajes.items():
        if tid.startswith(d.servicio) and p[0][0] == 1:
            total += 1
            iguales += p[0][3].get("from") == actuales.get(tid)
    return iguales, total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("archivo", type=Path)
    ap.add_argument("horarios", type=Path)
    ap.add_argument("--ajuste", nargs="+", required=True)
    ap.add_argument("--validacion", nargs="+", required=True)
    ap.add_argument("--fijo", default=None, help="tabla fija (p. ej. 35607=5) en vez de ajustar")
    ap.add_argument("--diferencias", action="store_true")
    ap.add_argument("--objetivo", choices=("f1", "ok"), default="f1")
    args = ap.parse_args()
    global OBJETIVO  # noqa: PLW0603
    OBJETIVO = args.objetivo

    def carga(lista: list[str]) -> list[Dia]:
        return [cargar(args.archivo, args.horarios, *x.split("=")) for x in lista]

    ajuste, validacion = carga(args.ajuste), carga(args.validacion)
    for d in ajuste + validacion:
        print(f"from N=0 vs publicado {d.servicio}: {comprobar_from(d)}")
    if args.diferencias:
        d = validacion[0]
        actuales = enlaces_para(d, {})
        for tid, viaje in sorted(d.viajes.items()):
            publicado = viaje[0][3].get("from")
            if tid.startswith(d.servicio) and viaje[0][0] == 1 and publicado != actuales.get(tid):
                print("  distinto", tid, viaje[0][1], publicado, actuales.get(tid))
    punt: dict[str, dict[int, float]] = {}
    if args.fijo is not None:
        elegido = parse_min_turnaround(args.fijo)
    else:
        elegido, punt = ajustar(ajuste)
    print("N elegido (dias de ajuste):", json.dumps(elegido))
    for origen, p in sorted(punt.items()):
        print(f"  ajuste {origen}: " + " ".join(f"N={n}:{v:.0f}%" for n, v in sorted(p.items())))
    viejo, nuevo = por_origen(validacion, {}), por_origen(validacion, elegido)
    print("\n| cabecera | N | n | ok A=5 antes→después | ok A=10 antes→después "
          "| prec A=5 antes→después | prec A=10 antes→después | cob A=10 antes→después |")
    for origen in sorted(viejo, key=lambda o: -len(viejo[o])):
        a, b = metricas(viejo[origen]), metricas(nuevo[origen])
        print(f"| {origen} | {elegido.get(origen, 0)} | {a['n']:.0f} "
              f"| {a['ok5']:.0f}→{b['ok5']:.0f} | {a['ok10']:.0f}→{b['ok10']:.0f} "
              f"| {a['prec5']:.0f}→{b['prec5']:.0f} | {a['prec10']:.0f}→{b['prec10']:.0f} "
              f"| {a['cob10']:.0f}→{b['cob10']:.0f} |")
    print("\n| cabecera | N | n | salidas reales >=3 | F1 avisos A=5 antes→después "
          "| F1 avisos A=10 antes→después | avisos A=10 (correctos) antes→después |")
    for origen in sorted(viejo, key=lambda o: -len(viejo[o])):
        a, b = metricas(viejo[origen]), metricas(nuevo[origen])
        print(f"| {origen} | {elegido.get(origen, 0)} | {a['n']:.0f} | {a['tarde']:.0f} "
              f"| {a['f15']:.0f}→{b['f15']:.0f} | {a['f110']:.0f}→{b['f110']:.0f} "
              f"| {a['av10']:.0f} ({a['vp10']:.0f})→{b['av10']:.0f} ({b['vp10']:.0f}) |")
    print("\n| cabecera | N | siempre 0 | salidas reales >=3 | aciertos en esas A=10 antes→después "
          "| avisos >=3 A=10 (aciertos) antes→después | avisos A=5 (aciertos) antes→después |")
    for origen in sorted(viejo, key=lambda o: -len(viejo[o])):
        a, b = metricas(viejo[origen]), metricas(nuevo[origen])
        print(f"| {origen} | {elegido.get(origen, 0)} | {a['cero']:.0f} | {a['tarde']:.0f} "
              f"| {a['tok10']:.0f}→{b['tok10']:.0f} "
              f"| {a['av10']:.0f} ({a['avok10']:.0f})→{b['av10']:.0f} ({b['avok10']:.0f}) "
              f"| {a['av5']:.0f} ({a['avok5']:.0f})→{b['av5']:.0f} ({b['avok5']:.0f}) |")
    todos_a = [f for fs in viejo.values() for f in fs]
    todos_b = [f for fs in nuevo.values() for f in fs]
    a, b = metricas(todos_a), metricas(todos_b)
    print(f"| TODAS | | {a['n']:.0f} | {a['ok5']:.1f}→{b['ok5']:.1f} | {a['ok10']:.1f}→{b['ok10']:.1f} "
          f"| {a['prec5']:.1f}→{b['prec5']:.1f} | {a['prec10']:.1f}→{b['prec10']:.1f} "
          f"| {a['cob10']:.0f}→{b['cob10']:.0f} |")
    print(f"TODAS F1 A=5 {a['f15']:.1f}→{b['f15']:.1f} A=10 {a['f110']:.1f}→{b['f110']:.1f} "
          f"vp10 {a['vp10']:.0f}→{b['vp10']:.0f}")
    print(f"TODAS siempre0={a['cero']:.1f} tarde={a['tarde']:.0f} tok10 {a['tok10']:.0f}→{b['tok10']:.0f} "
          f"av10 {a['av10']:.0f}({a['avok10']:.0f})→{b['av10']:.0f}({b['avok10']:.0f}) "
          f"av5 {a['av5']:.0f}({a['avok5']:.0f})→{b['av5']:.0f}({b['avok5']:.0f})")


if __name__ == "__main__":
    main()
