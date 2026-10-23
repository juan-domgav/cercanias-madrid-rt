#!/usr/bin/env python3
"""Construye el histórico de retrasos (RB-010): un Parquet por día de servicio.

Lee ``gs://cercanias-madrid-rt-privado/archivo/<día>/`` (más la madrugada del día
siguiente, donde siguen circulando trenes del día) y el horario de ese día, y
escribe una fila por (día, tren, parada) en ``historico/<día>.parquet`` y su
informe de calidad en ``historico/calidad/<día>.json``. Re-ejecutar un día
sobrescribe esos dos objetos y nada más: nunca toca ``archivo/``.

Horario del día, por orden:
1. ``gs://cercanias-madrid-rt-web/horarios/<servicio>.json`` (lo que usaron los tablones).
2. GTFS archivado por Mobility Database (``data/mappings/gtfs-historico-mdb.json``),
   convertido con el MISMO ``build_schedules`` que el job nocturno y cacheado en
   ``--cache``. Sirve para los días cuyo ``horarios/`` ya no existe (septiembre).

Uso::

    uv run --group historico python scripts/construir_historico.py 2026-10-22
    uv run --group historico python scripts/construir_historico.py 2026-09-08..2026-10-22 -j 3
    uv run --group historico python scripts/construir_historico.py ayer        # incremental diario
    ... --local /tmp/hist      # sólo escribe en local, no sube nada
    ... --forzar               # por defecto se saltan los días que ya están en historico/
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from cercanias_domain.historico import (
    FilaParada,
    Observacion,
    ParadaProgramada,
    familia,
    filas_viaje,
    huecos,
    normalizar,
    pasadas_congeladas,
    servicio_madrid,
)
from cercanias_domain.trip_id import parse_trip_id
from cercanias_worker.regenerate_schedules import build_schedules

MADRID = ZoneInfo("Europe/Madrid")
BUCKET = "cercanias-madrid-rt-privado"
ARCHIVO = f"gs://{BUCKET}/archivo"
DESTINO = f"gs://{BUCKET}/historico"
HORARIOS = "gs://cercanias-madrid-rt-web/horarios"
RAIZ = Path(__file__).resolve().parent.parent
MANIFIESTO_GTFS = RAIZ / "data" / "mappings" / "gtfs-historico-mdb.json"
NOMBRES_PARADAS = RAIZ / "data" / "mappings" / "paradas-madrid.json"
CACHE = Path("/workspace/historico/cache")
#: Trenes del día que siguen circulando pasada la medianoche (hasta ~01:30;
#: margen para fantasmas que Renfe deja en el feed).
MADRUGADA_SIGUIENTE = 4

#: Calidad. Un día normal: ~4.300 pasadas, sin huecos en 06-24 h, >95 % de los
#: trenes programados vistos. Umbrales holgados: marcan días a revisar, no excluyen.
HUECO = timedelta(minutes=5)
VENTANA_SERVICIO = (time(6, 0), time(23, 59, 59))
MIN_PASADAS = 3500
MAX_MIN_HUECOS = 30
MIN_PCT_TRENES_VISTOS = 85.0
MIN_PCT_MEDIDO = 60.0
#: Umbral de «retraso» de Renfe/ADIF para la estadística del informe (3 min).
RETRASO_NOTABLE_S = 180


#: ``gcloud storage cp`` a veces se cuelga (visto: 8 min parado con 16 procesos hijos).
LIMITE_GCLOUD_S = 420
REINTENTOS_GCLOUD = 3


def _gcloud(*args: str, entrada: str | None = None) -> subprocess.CompletedProcess[str]:
    """``gcloud storage …`` con límite de tiempo; si se cuelga, mata el grupo y reintenta.

    Todo lo que se lanza es idempotente (copias que sobrescriben, listados).
    """
    cmd = ["gcloud", "storage", *args]
    for intento in range(1, REINTENTOS_GCLOUD + 1):
        with subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as proc:
            try:
                out, err = proc.communicate(entrada, timeout=LIMITE_GCLOUD_S)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                print(
                    f"gcloud colgado ({' '.join(args[:2])}), intento {intento}", file=sys.stderr, flush=True
                )
                continue
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    return subprocess.CompletedProcess(cmd, 124, "", f"gcloud colgado {REINTENTOS_GCLOUD} veces")


# --- descarga -----------------------------------------------------------------


def descargar_archivo(dia: date, tmp: Path) -> tuple[list[Path], list[Path]]:
    """Baja ``archivo/<día>/`` entero y la madrugada de ``<día+1>``."""
    hoy, manana = tmp / "hoy", tmp / "manana"
    hoy.mkdir(parents=True)
    manana.mkdir(parents=True)
    r = _gcloud("cp", "-r", f"{ARCHIVO}/{dia:%Y-%m-%d}/*", str(hoy))
    if r.returncode != 0:
        raise RuntimeError(f"no se pudo bajar archivo/{dia}: {r.stderr.strip()[-300:]}")
    siguiente = dia + timedelta(days=1)
    lista = _gcloud("ls", f"{ARCHIVO}/{siguiente:%Y-%m-%d}/")
    madrugada = [
        u
        for u in lista.stdout.split()
        if u.endswith(".json") and int(u.rsplit("/", 1)[-1][:2]) < MADRUGADA_SIGUIENTE
    ]
    if madrugada:
        r = _gcloud("cp", "-I", str(manana), entrada="\n".join(madrugada))
        if r.returncode != 0:
            raise RuntimeError(f"no se pudo bajar la madrugada de {siguiente}: {r.stderr[-300:]}")
    return sorted(hoy.glob("*.json")), sorted(manana.glob("*.json"))


# --- horario ------------------------------------------------------------------


@dataclass
class Horario:
    fuente: str
    viajes: dict[str, list[ParadaProgramada]]
    ruta: dict[str, str]
    destino: dict[str, str]


def _horario_desde_json(datos: dict[str, Any], fuente: str) -> Horario:
    viajes: dict[str, list[ParadaProgramada]] = defaultdict(list)
    ruta: dict[str, str] = {}
    destino: dict[str, str] = {}
    for stop_id, filas in datos["stations"].items():
        for r in filas:
            viajes[r["trip"]].append(ParadaProgramada(int(r["seq"]), stop_id, r["t"]))
            ruta[r["trip"]] = r.get("route") or ""
            destino[r["trip"]] = r.get("dest") or ""
    return Horario(fuente, dict(viajes), ruta, destino)


def _horarios_desde_gtfs(url: str, servicio: str, cache: Path) -> dict[str, Any] | None:
    """Convierte un GTFS archivado con ``build_schedules`` y cachea los servicios."""
    nombre = url.rsplit("/", 1)[-1].removesuffix(".zip")
    # Varios procesos (-j) pueden pedir el mismo GTFS: se convierte una vez.
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / f".{nombre}.lock").open("w") as cerrojo:
        fcntl.flock(cerrojo, fcntl.LOCK_EX)
        return _horarios_desde_gtfs_sin_cerrojo(url, nombre, servicio, cache)


def _horarios_desde_gtfs_sin_cerrojo(
    url: str, nombre: str, servicio: str, cache: Path
) -> dict[str, Any] | None:
    destino = cache / "horarios" / nombre
    hecho = destino / "_hecho"
    if not hecho.exists():
        zips = cache / "zips"
        zips.mkdir(parents=True, exist_ok=True)
        zip_path = zips / f"{nombre}.zip"
        if not zip_path.exists():
            urllib.request.urlretrieve(url, zip_path)
        with tempfile.TemporaryDirectory() as gtfs:
            with zipfile.ZipFile(zip_path) as z:
                for m in ("calendar.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt"):
                    z.extract(m, gtfs)
            todos = build_schedules(Path(gtfs))
        destino.mkdir(parents=True, exist_ok=True)
        for svc, h in todos.items():
            (destino / f"{svc}.json").write_text(json.dumps(h, ensure_ascii=False))
        hecho.write_text(url)
    f = destino / f"{servicio}.json"
    return json.loads(f.read_text()) if f.exists() else None


def cargar_horario(dia: date, servicio: str, cache: Path) -> Horario | None:
    r = _gcloud("cat", f"{HORARIOS}/{servicio}.json")
    if r.returncode == 0 and r.stdout.strip():
        return _horario_desde_json(json.loads(r.stdout), f"horarios/{servicio}.json")
    manifiesto: dict[str, str] = json.loads(MANIFIESTO_GTFS.read_text())["datasets"]
    # El GTFS descargado el propio día (o el anterior más cercano que lo cubra).
    candidatos = sorted((d for d in manifiesto if d <= dia.isoformat()), reverse=True)
    for d in candidatos[:10]:
        datos = _horarios_desde_gtfs(manifiesto[d], servicio, cache)
        if datos is not None:
            nombre = manifiesto[d].rsplit("/", 1)[-1].removesuffix(".zip")
            return _horario_desde_json(datos, f"gtfs:{nombre}")
    return None


# --- construcción ---------------------------------------------------------------


#: Versión de las reglas; va en el informe para saber con qué se construyó cada día.
VERSION = 2
#: Menos trenes que esto no permite distinguir un feed congelado de trenes quietos.
MIN_TRENES_CONGELADO = 5


def _firma(trenes: list[list[Any]]) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted(tuple(map(str, t)) for t in trenes))


def leer_observaciones(
    ficheros: list[Path], dia: date
) -> tuple[dict[str, list[Observacion]], list[datetime], Counter[str], list[tuple[datetime, datetime]]]:
    """Observaciones del día, pasadas válidas, prefijos del feed y rachas congeladas."""
    pasadas: list[tuple[datetime, dict[str, int], list[list[Any]]]] = []
    for f in ficheros:
        try:
            a = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ts = datetime.fromisoformat(a["ts"]).astimezone(MADRID)
        pasadas.append((ts, {c: k for k, c in enumerate(a["cols"])}, [t for t in a["trains"] if t]))
    pasadas.sort(key=lambda x: x[0])
    firmas = [
        (
            ts,
            tuple(sorted(tuple(map(str, t)) for t in trenes))
            if len(trenes) >= MIN_TRENES_CONGELADO
            else None,
        )
        for ts, _, trenes in pasadas
    ]
    congeladas = pasadas_congeladas(firmas)
    rachas: list[tuple[datetime, datetime]] = []
    obs: dict[str, list[Observacion]] = defaultdict(list)
    marcas: list[datetime] = []
    prefijos: Counter[str] = Counter()
    for k, (ts, i, trenes) in enumerate(pasadas):
        if congeladas[k]:
            # La racha va desde la última pasada real (k - 1 >= 0: la primera nunca se marca).
            if k > 0 and congeladas[k - 1]:
                rachas[-1] = (rachas[-1][0], ts)
            else:
                rachas.append((pasadas[k - 1][0], ts))
            continue
        if ts.date() == dia:
            marcas.append(ts)
        for tr in trenes:
            tid = tr[i["trip"]]
            if not tid:
                continue
            if ts.date() == dia and VENTANA_SERVICIO[0] <= ts.time() and not tid.startswith("SPECIAL"):
                prefijos[tid[:5]] += 1
            n = normalizar(tid, Observacion(ts, tr[i["delay"]], tr[i["next_stop"]], tr[i["status"]]))
            if n is not None and n.fecha_servicio == dia:
                obs[n.trip_id].append(n.obs)
    return obs, marcas, prefijos, [(a, b) for a, b in rachas if b.date() == dia or a.date() == dia]


COLUMNAS = pa.schema(
    [
        ("fecha_servicio", pa.date32()),
        ("servicio", pa.string()),
        ("trip_id", pa.string()),
        ("tren", pa.string()),
        ("linea", pa.string()),
        ("familia", pa.string()),
        ("es_especial", pa.bool_()),
        ("origen_id", pa.string()),
        ("destino_id", pa.string()),
        ("destino", pa.string()),
        ("parada_id", pa.string()),
        ("parada", pa.string()),
        ("seq", pa.int16()),
        ("es_origen", pa.bool_()),
        ("es_destino", pa.bool_()),
        ("programada", pa.timestamp("s", tz="Europe/Madrid")),
        ("hora", pa.int8()),
        ("dia_semana", pa.int8()),
        ("tren_visto", pa.bool_()),
        ("salio", pa.bool_()),
        ("medido", pa.bool_()),
        ("estado_medida", pa.string()),
        ("retraso_s", pa.int32()),
        ("retraso_primero_s", pa.int32()),
        ("primero_marcador", pa.bool_()),
        ("primero_ts", pa.timestamp("s", tz="Europe/Madrid")),
        ("ultimo_ts", pa.timestamp("s", tz="Europe/Madrid")),
        ("ultimo_estado", pa.string()),
        ("n_obs", pa.int16()),
        ("llegada_obs", pa.timestamp("s", tz="Europe/Madrid")),
        ("retraso_llegada_obs_s", pa.int32()),
        ("salida_obs", pa.timestamp("s", tz="Europe/Madrid")),
        ("retraso_salida_obs_s", pa.int32()),
        ("fuente_horario", pa.string()),
    ]
)


def construir(
    dia: date,
    ficheros_hoy: list[Path],
    ficheros_manana: list[Path],
    horario: Horario | None,
    nombres: dict[str, str],
) -> tuple[pa.Table, dict[str, Any]]:
    servicio = servicio_madrid(dia)
    obs, marcas, prefijos, congelado = leer_observaciones([*ficheros_hoy, *ficheros_manana], dia)
    viajes = horario.viajes if horario else {}
    filas: list[tuple[FilaParada, str, str, str]] = []
    for tid in sorted(set(viajes) | set(obs)):
        paradas = viajes.get(tid)
        parsed = parse_trip_id(tid)
        linea = (horario.ruta.get(tid) if horario and paradas else None) or (
            parsed.route_canonical if parsed else ""
        )
        for f in filas_viaje(dia, tid, obs.get(tid, []), paradas, MADRID):
            filas.append(
                (
                    f,
                    linea,
                    horario.destino.get(tid, "") if horario and paradas else "",
                    horario.fuente if horario and paradas else "sin_horario",
                )
            )
    columnas: dict[str, list[Any]] = {c: [] for c in COLUMNAS.names}
    origen_de: dict[str, str] = {}
    destino_de: dict[str, str] = {}
    for f, _, _, _ in filas:
        if f.seq is not None or f.trip_id not in origen_de:
            origen_de.setdefault(f.trip_id, f.stop_id)
        destino_de[f.trip_id] = f.stop_id
    for f, linea, dest, fuente in filas:
        parsed = parse_trip_id(f.trip_id)
        ref = f.programada or f.ultimo_ts or f.primero_ts
        valores = {
            "fecha_servicio": dia,
            "servicio": servicio,
            "trip_id": f.trip_id,
            "tren": parsed.train_number if parsed else None,
            "linea": linea,
            "familia": familia(f.trip_id) or linea,
            "es_especial": bool(parsed and parsed.is_special),
            "origen_id": origen_de[f.trip_id],
            "destino_id": destino_de[f.trip_id],
            "destino": dest or nombres.get(destino_de[f.trip_id]),
            "parada_id": f.stop_id,
            "parada": nombres.get(f.stop_id),
            "seq": f.seq,
            "es_origen": f.es_origen,
            "es_destino": f.es_destino,
            "programada": f.programada,
            "hora": ref.astimezone(MADRID).hour if ref else None,
            "dia_semana": dia.isoweekday(),
            "tren_visto": f.tren_visto,
            "salio": f.salio,
            "medido": f.medido,
            "estado_medida": str(f.estado_medida),
            "retraso_s": f.retraso_s,
            "retraso_primero_s": f.retraso_primero_s,
            "primero_marcador": f.primero_marcador,
            "primero_ts": f.primero_ts,
            "ultimo_ts": f.ultimo_ts,
            "ultimo_estado": f.ultimo_estado,
            "n_obs": f.n_obs,
            "llegada_obs": f.llegada_obs,
            "retraso_llegada_obs_s": f.retraso_llegada_obs_s,
            "salida_obs": f.salida_obs,
            "retraso_salida_obs_s": f.retraso_salida_obs_s,
            "fuente_horario": fuente,
        }
        for c, v in valores.items():
            columnas[c].append(v)
    tabla = pa.Table.from_pydict(columnas, schema=COLUMNAS)
    calidad = informe_calidad(
        dia,
        servicio,
        [f for f, *_ in filas],
        marcas,
        prefijos=prefijos,
        congelado=congelado,
        horario=horario,
        n_hoy=len(ficheros_hoy),
        n_manana=len(ficheros_manana),
    )
    tabla = tabla.replace_schema_metadata({"calidad": json.dumps(calidad, ensure_ascii=False)})
    return tabla, calidad


def _minutos_en_ventana(a: datetime, b: datetime, dia: date) -> float:
    ini = datetime.combine(dia, VENTANA_SERVICIO[0], tzinfo=MADRID)
    fin = datetime.combine(dia + timedelta(days=1), time(0), tzinfo=MADRID)
    solape = min(b, fin) - max(a, ini)
    return max(0.0, solape.total_seconds() / 60)


def informe_calidad(  # noqa: PLR0913 - informe plano, un argumento por fuente
    dia: date,
    servicio: str,
    filas: list[FilaParada],
    marcas: list[datetime],
    *,
    prefijos: Counter[str],
    congelado: list[tuple[datetime, datetime]],
    horario: Horario | None,
    n_hoy: int,
    n_manana: int,
) -> dict[str, Any]:
    marcas = sorted(marcas)
    ini = datetime.combine(dia, VENTANA_SERVICIO[0], tzinfo=MADRID)
    fin = datetime.combine(dia, VENTANA_SERVICIO[1], tzinfo=MADRID)
    # Bordes: sin pasadas al empezar o terminar la ventana también es un hueco.
    bordes = [ini, *[m for m in marcas if ini <= m <= fin], fin]
    gaps = huecos(marcas, HUECO)
    gaps_ventana = [(a, b) for a, b in huecos(bordes, HUECO) if _minutos_en_ventana(a, b, dia) > 0]
    min_hueco = sum(_minutos_en_ventana(a, b, dia) for a, b in gaps_ventana)
    programados = set(horario.viajes) if horario else set()
    vistos = {f.trip_id for f in filas if f.tren_visto}
    regulares_vistos = vistos & programados
    con_horario = [f for f in filas if f.seq is not None and f.tren_visto]
    medidas = [f for f in con_horario if f.medido]
    retrasos = sorted(f.retraso_s for f in medidas if f.retraso_s is not None)
    estados = Counter(str(f.estado_medida) for f in filas)
    mayoritario = prefijos.most_common(1)[0][0] if prefijos else None
    pct_vistos = 100 * len(regulares_vistos) / len(programados) if programados else 0.0
    pct_medido = 100 * len(medidas) / len(con_horario) if con_horario else 0.0
    motivos = []
    if not marcas:
        motivos.append("sin pasadas")
    else:
        if marcas[0].time() > time(6, 0) or marcas[-1].time() < time(23, 30):
            motivos.append(f"día parcial ({marcas[0]:%H:%M}-{marcas[-1]:%H:%M})")
        if len(marcas) < MIN_PASADAS:
            motivos.append(f"pocas pasadas ({len(marcas)} < {MIN_PASADAS})")
        if min_hueco > MAX_MIN_HUECOS:
            motivos.append(f"{min_hueco:.0f} min de huecos >5 min entre 06 y 24 h")
    min_congelado = sum(_minutos_en_ventana(a, b, dia) for a, b in congelado)
    if min_congelado > MAX_MIN_HUECOS:
        motivos.append(f"feed congelado {min_congelado:.0f} min entre 06 y 24 h (repeticiones descartadas)")
    if mayoritario and mayoritario != servicio:
        motivos.append(f"servicio del feed {mayoritario} != esperado {servicio}")
    if horario is None:
        motivos.append("sin horario: no hay hora programada ni paradas no vistas")
    elif pct_vistos < MIN_PCT_TRENES_VISTOS:
        motivos.append(f"sólo {pct_vistos:.0f} % de trenes programados vistos")
    if con_horario and pct_medido < MIN_PCT_MEDIDO:
        motivos.append(f"sólo {pct_medido:.0f} % de paradas medidas")
    return {
        "dia": dia.isoformat(),
        "servicio": servicio,
        "servicio_feed_mayoritario": mayoritario,
        "fuente_horario": horario.fuente if horario else None,
        "pasadas": len(marcas),
        "pasadas_madrugada_siguiente": n_manana,
        "ficheros_dia": n_hoy,
        "primera_pasada": marcas[0].isoformat() if marcas else None,
        "ultima_pasada": marcas[-1].isoformat() if marcas else None,
        "huecos_5min": len(gaps),
        "huecos_5min_06_24": len(gaps_ventana),
        "min_huecos_06_24": round(min_hueco, 1),
        "huecos_06_24_detalle": [f"{a:%H:%M:%S}-{b:%H:%M:%S}" for a, b in gaps_ventana][:20],
        "congelado_detalle": [f"{a:%d %H:%M:%S}-{b:%d %H:%M:%S}" for a, b in congelado][:20],
        "min_congelado_06_24": round(min_congelado, 1),
        "trenes_programados": len(programados),
        "trenes_vistos": len(vistos),
        "trenes_programados_vistos": len(regulares_vistos),
        "pct_programados_vistos": round(pct_vistos, 1),
        "trenes_fuera_de_horario": len(vistos - programados),
        "filas": len(filas),
        "filas_por_estado": dict(estados),
        "pct_medido_trenes_vistos": round(pct_medido, 1),
        "retraso_medido_p50_s": statistics.median(retrasos) if retrasos else None,
        "retraso_medido_p90_s": retrasos[int(0.9 * (len(retrasos) - 1))] if retrasos else None,
        "pct_paradas_3min_o_mas": round(
            100 * sum(r >= RETRASO_NOTABLE_S for r in retrasos) / len(retrasos), 1
        )
        if retrasos
        else None,
        "estado": "bueno" if not motivos else "revisar",
        "motivos": motivos,
        "version": VERSION,
        "generado": datetime.now(tz=MADRID).isoformat(timespec="seconds"),
    }


# --- orquestación ----------------------------------------------------------------


def procesar_dia(dia: date, local: Path | None, tmp_base: Path, cache: Path) -> dict[str, Any]:
    nombres = json.loads(NOMBRES_PARADAS.read_text())
    tmp = Path(tempfile.mkdtemp(prefix=f"hist-{dia}-", dir=tmp_base))
    try:
        hoy, manana = descargar_archivo(dia, tmp)
        horario = cargar_horario(dia, servicio_madrid(dia), cache)
        tabla, calidad = construir(dia, hoy, manana, horario, nombres)
        salida = (local or tmp) / f"{dia:%Y-%m-%d}.parquet"
        salida.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(tabla, salida, compression="zstd")
        informe = salida.with_suffix(".calidad.json")
        informe.write_text(json.dumps(calidad, ensure_ascii=False, indent=1))
        calidad["bytes"] = salida.stat().st_size
        if local is None:
            for origen, destino in (
                (salida, f"{DESTINO}/{dia:%Y-%m-%d}.parquet"),
                (informe, f"{DESTINO}/calidad/{dia:%Y-%m-%d}.json"),
            ):
                r = _gcloud("cp", str(origen), destino)
                if r.returncode != 0:
                    raise RuntimeError(f"no se pudo subir {destino}: {r.stderr.strip()[-300:]}")
            calidad["objeto"] = f"{DESTINO}/{dia:%Y-%m-%d}.parquet"
        return calidad
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def existentes() -> set[date]:
    """Días que ya tienen ``historico/<día>.parquet``."""
    r = _gcloud("ls", f"{DESTINO}/*.parquet")
    if r.returncode != 0:
        if "matched no objects" in r.stderr:
            return set()
        raise RuntimeError(f"no se pudo listar {DESTINO}: {r.stderr.strip()[-300:]}")
    return {date.fromisoformat(Path(u).stem) for u in r.stdout.split()}


def _dias(args: list[str]) -> list[date]:
    dias: list[date] = []
    for a in args:
        if a == "ayer":
            dias.append(datetime.now(tz=MADRID).date() - timedelta(days=1))
        elif ".." in a:
            ini, fin = (date.fromisoformat(x) for x in a.split(".."))
            dias.extend(ini + timedelta(days=k) for k in range((fin - ini).days + 1))
        else:
            dias.append(date.fromisoformat(a))
    return dias


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dias", nargs="+", help="YYYY-MM-DD, YYYY-MM-DD..YYYY-MM-DD o 'ayer'")
    p.add_argument("--local", type=Path, help="escribir aquí y NO subir a GCS")
    p.add_argument("--tmp", type=Path, default=Path(tempfile.gettempdir()))
    p.add_argument("--cache", type=Path, default=CACHE, help="caché de GTFS archivados")
    p.add_argument(
        "--forzar", action="store_true", help="reconstruir aunque historico/<día>.parquet ya exista"
    )
    p.add_argument("-j", "--paralelo", type=int, default=1, help="días en paralelo")
    a = p.parse_args(argv)
    hoy = datetime.now(tz=MADRID).date()
    dias = _dias(a.dias)
    if any(d >= hoy for d in dias):
        print("ERROR: sólo días completos (anteriores a hoy)", file=sys.stderr)
        return 2
    if a.local is None and not a.forzar:
        hechos = existentes()
        saltados = [d for d in dias if d in hechos]
        dias = [d for d in dias if d not in hechos]
        for d in saltados:
            print(
                f"{d} ya existe en {DESTINO}/ (usa --forzar para reconstruirlo)", file=sys.stderr, flush=True
            )
    fallos = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=a.paralelo) as ex:
        futuros = {ex.submit(procesar_dia, d, a.local, a.tmp, a.cache): d for d in dias}
        for fut in concurrent.futures.as_completed(futuros):
            d = futuros[fut]
            try:
                c = fut.result()
            except Exception as exc:
                fallos += 1
                print(f"{d} ERROR {exc}", file=sys.stderr, flush=True)
                continue
            print(
                json.dumps(
                    {
                        k: c[k]
                        for k in (
                            "dia",
                            "estado",
                            "pasadas",
                            "trenes_vistos",
                            "filas",
                            "pct_medido_trenes_vistos",
                            "bytes",
                            "motivos",
                        )
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
