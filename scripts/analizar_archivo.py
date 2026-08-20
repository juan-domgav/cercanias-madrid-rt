#!/usr/bin/env python3
"""Analiza el archivo histórico para entender los enlaces de cabecera.

Responde las dos preguntas que hoy no se pueden responder y que condicionan la
previsión:

1. **¿Cuánto retraso sobrevive al cambio de sentido?** Un tren que llega tarde a
   cabecera no puede salir a su hora en sentido contrario, pero parte del retraso
   lo absorbe el tiempo de maniobra. Cuánto exactamente es lo que hay que medir,
   no suponer.

2. **¿Con qué frecuencia un servicio programado no circula?** Si las supresiones
   son habituales, propagar el retraso del enlace sería peor que no decir nada:
   estaríamos avisando de un tren que ni siquiera va a salir.

Se ejecuta sobre lo que haya acumulado; con pocos días los resultados son
indicativos y el propio informe lo dice.
"""

from __future__ import annotations

import collections
import json
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUCKET = "cercanias-madrid-rt-privado"
#: Mediana de maniobra observada en el horario de 1030J. Sirve de referencia
#: para saber si el retraso medido se absorbe o se propaga.
TURNAROUND_MEDIAN_MIN = 7

#: Una semana completa, con fin de semana, es el minimo para distinguir un
#: patron de una mala tarde.
DIAS_PARA_CONCLUIR = 7


def _run(args: list[str]) -> str:
    out = subprocess.run(args, capture_output=True, text=True, check=False)
    return out.stdout if out.returncode == 0 else ""


def listar_dias() -> list[str]:
    salida = _run(["gcloud", "storage", "ls", f"gs://{BUCKET}/archivo/"])
    return sorted({linea.rstrip("/").split("/")[-1] for linea in salida.splitlines() if linea.strip()})


def cargar_dia(dia: str) -> list[dict[str, Any]]:
    """Descarga un día completo y lo lee fichero a fichero.

    No se usa ``gcloud storage cat`` con comodín: concatena los objetos sin
    separador, así que dos pasadas se funden en una línea que no parsea. Bajar el
    día entero de una vez es además mucho más rápido que catear miles de objetos.
    """
    pasadas: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        _run(["gcloud", "storage", "cp", "-r", f"gs://{BUCKET}/archivo/{dia}", tmp])
        for fichero in sorted(Path(tmp).rglob("*.json")):
            try:
                pasadas.append(json.loads(fichero.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return pasadas


@dataclass
class Trayectoria:
    """Cómo evolucionó un tren a lo largo del día."""

    primer_visto: str
    ultimo_visto: str
    retraso_max: int
    retraso_final: int | None
    paradas: list[str]


def trayectorias(pasadas: list[dict[str, Any]]) -> dict[str, Trayectoria]:
    datos: dict[str, dict[str, Any]] = {}
    for p in sorted(pasadas, key=lambda x: x.get("ts", "")):
        ts = p.get("ts", "")
        for fila in p.get("trains", []):
            trip, delay, next_stop = fila[0], fila[1], fila[2]
            if not trip:
                continue
            d = datos.setdefault(trip, {"first": ts, "last": ts, "max": 0, "final": None, "stops": []})
            d["last"] = ts
            if isinstance(delay, int):
                d["max"] = max(d["max"], delay)
                d["final"] = delay
            if next_stop and (not d["stops"] or d["stops"][-1] != next_stop):
                d["stops"].append(next_stop)
    return {
        t: Trayectoria(d["first"], d["last"], d["max"], d["final"], d["stops"])
        for t, d in datos.items()
    }


def informe(dias: list[str]) -> None:
    total_pasadas = 0
    todos: dict[str, Trayectoria] = {}
    for dia in dias:
        pasadas = cargar_dia(dia)
        total_pasadas += len(pasadas)
        for trip, tr in trayectorias(pasadas).items():
            todos[f"{dia}:{trip}"] = tr

    if not todos:
        print("Todavía no hay archivo suficiente. Vuelve dentro de unas horas.")
        return

    print(f"Días analizados: {len(dias)} ({', '.join(dias)})")
    print(f"Pasadas archivadas: {total_pasadas}")
    print(f"Trenes-día observados: {len(todos)}\n")

    retrasos = [t.retraso_max for t in todos.values() if t.retraso_max]
    if retrasos:
        retrasos.sort()
        print("Retraso máximo por tren (segundos):")
        print(f"  mediana {statistics.median(retrasos):.0f} · p90 {retrasos[int(len(retrasos)*0.9)]:.0f} "
              f"· max {retrasos[-1]:.0f}")
        graves = sum(1 for r in retrasos if r > TURNAROUND_MEDIAN_MIN * 60)
        print(f"  por encima de la maniobra mediana ({TURNAROUND_MEDIAN_MIN} min): "
              f"{graves} de {len(retrasos)} ({100*graves/len(retrasos):.0f}%)")
        print("  -> son los que NO pueden devolver el enlace a su hora\n")

    vistas = collections.Counter(len(t.paradas) for t in todos.values())
    fugaces = sum(n for k, n in vistas.items() if k <= 1)
    print(f"Trenes vistos en 1 parada o menos: {fugaces} ({100*fugaces/len(todos):.0f}%)")
    print("  (candidatos a servicio suprimido o a aparición tardía; hace falta")
    print("   cruzarlos con el horario para distinguirlo)")

    if len(dias) < DIAS_PARA_CONCLUIR:
        print(f"\nAVISO: sólo {len(dias)} día(s) de datos. Para distinguir un patrón")
        print("de una mala tarde hacen falta al menos una semana, incluido fin de semana.")


def main() -> int:
    dias = listar_dias()
    if not dias:
        print("No hay archivo todavía. ¿Está ARCHIVE_ENABLED=1 en el job?", file=sys.stderr)
        return 1
    informe(dias)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
