#!/usr/bin/env python3
"""Entrena los modelos de retraso de C1 y C5 y exporta el artefacto (RB-021).

Reproduce el backtest semanal del 23/11/2026 (``ml.py``): mismo objetivo
(retraso medido en paradas no finales, [-5, 60] min), mismos rasgos y mismos
hiperparámetros de LightGBM. Dos modelos:

- ``estado_linea`` (C1): base + estado de la línea en la última hora.
- ``entrante_vivo`` (C5): base + último retraso visto del entrante.

Lee los Parquet diarios del histórico (RB-010) de un directorio LOCAL; no sube
nada. Los días de ``previsto_dias_excluidos.json`` ni se leen. El repo es
público: el artefacto deriva del histórico privado y NO se commitea.

Con ``--prueba-desde`` evalúa antes en un holdout (entrena con los días
anteriores) a varias antelaciones (rasgos vistos ``h`` min antes de la salida de
origen, que es lo que pasa en producción: el viaje aún no está en el feed) y
comprueba que:

- la evaluación en Python puro de ``cercanias_domain.prediccion`` da lo mismo
  que ``Booster.predict`` (paridad de árboles), y
- los rasgos que calcula el dominio desde el horario y las observaciones son
  los mismos que los de pandas (sin desfase entrenamiento/servicio).

Uso::

    uv run --with lightgbm --with pandas --with pyarrow python scripts/entrenar_prediccion.py \\
        --historico /ruta/historico --desde 2026-09-08 --hasta 2026-11-22 \\
        --prueba-desde 2026-11-14 --out /tmp/prediccion
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import lightgbm as lgb  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
import pandas as pd  # type: ignore[import-not-found]

from cercanias_domain import prediccion as P
from cercanias_domain.departures import ScheduledStop

EXCLUIDOS_JSON = (
    Path(__file__).resolve().parent.parent
    / "packages/domain/src/cercanias_domain/previsto_dias_excluidos.json"
)
#: Festivos laborales de la Comunidad de Madrid (los mismos que el backtest y PR #14).
FESTIVOS_CAM = (
    "2026-10-12",
    "2026-11-02",
    "2026-12-07",
    "2026-12-08",
    "2026-12-25",
    "2027-01-01",
    "2027-01-06",
    "2027-03-19",
    "2027-03-25",
    "2027-03-26",
    "2027-05-01",
    "2027-08-16",
    "2027-10-12",
    "2027-11-01",
    "2027-12-06",
    "2027-12-08",
    "2027-12-25",
)
LINEAS = {"C1": P.MODELO_ESTADO_LINEA, "C5": P.MODELO_ENTRANTE_VIVO}
COLS = [
    "fecha_servicio",
    "trip_id",
    "tren",
    "linea",
    "familia",
    "es_especial",
    "origen_id",
    "destino_id",
    "destino",
    "parada_id",
    "es_origen",
    "es_destino",
    "programada",
    "hora",
    "medido",
    "retraso_s",
    "ultimo_ts",
    "seq",
]
#: Hiperparámetros del backtest (``ml.py``), objetivo L1 (el MAE).
PARAMS: dict[str, Any] = {
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbose": -1,
    "seed": 7,
    "cat_smooth": 20,
    "objective": "l1",
}
#: «Acierto»: a +-2 min del retraso real.
MARGEN_ACIERTO_MIN = 2
#: Tolerancia al comparar rasgos de pandas y del dominio (redondeo a 4 decimales).
TOLERANCIA_RASGO = 1e-3
NFOLD = 4
MIN = 60e9
MANIOBRA_MIN_NS = P.MANIOBRA_MIN_MIN * MIN
MANIOBRA_MAX_NS = P.MANIOBRA_MAX_MIN * MIN
#: Listón aprobado por Juan: >= 30 % menos MAE que «en hora» y >= 60 % a +-2 min.
LISTON_MEJORA = 30.0
LISTON_W2 = 60.0
#: Antelaciones (min antes de la salida de origen) con las que se entrena cada
#: modelo. El backtest vio los rasgos justo en ``t0``; en producción se predice
#: con el tren aún en cabecera (0-30 min antes) o sin publicar (>= ~30 min).
#: C5: cada fila toma UNA antelación al azar (semilla fija; mismo tamaño y mismos
#: rasgos) y el modelo aprende cuánto pesa un dato del entrante visto con 30 o 60
#: min de antelación. Sin esto, en el holdout del 14-22/11 C5 a 60 min quedaba
#: PEOR que «en hora» a +-2 min (27 % vs 44 %); con esto, 67 % vs 44 %.
#: C1: igual que el backtest (sólo ``t0``): mezclar antelaciones lo empeoraba en
#: todas (a 30 min, MAE 1,87 frente a 1,83).
HORIZONTES_ENTRENO: dict[str, tuple[float, ...]] = {
    P.MODELO_ESTADO_LINEA: (0.0,),
    P.MODELO_ENTRANTE_VIVO: (0.0, 15.0, 30.0, 45.0, 60.0, 90.0, 120.0),
}


def dias_excluidos(ruta: Path = EXCLUIDOS_JSON) -> dict[str, str]:
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    dias = {}
    for e in datos["excluded_days"]:
        dias[date.fromisoformat(e["date"]).isoformat()] = str(e["reason"])
    return dias


def cargar(directorio: Path, desde: date, hasta: date, excluidos: dict[str, str]) -> pd.DataFrame:
    partes, saltados = [], []
    dia = desde
    while dia <= hasta:
        p = directorio / f"{dia.isoformat()}.parquet"
        if dia.isoformat() in excluidos:
            saltados.append(dia.isoformat())
        elif p.exists():
            partes.append(pd.read_parquet(p, columns=COLS))
        dia += timedelta(days=1)
    if not partes:
        raise SystemExit(f"sin Parquet en {directorio} entre {desde} y {hasta}")
    d = pd.concat(partes, ignore_index=True)
    for c in ["trip_id", "tren", "familia", "origen_id", "destino_id", "parada_id", "linea"]:
        d[c] = d[c].astype(str)
    d["fecha"] = pd.to_datetime(d.fecha_servicio.astype(str))
    assert not d.fecha.dt.strftime("%Y-%m-%d").isin(list(excluidos)).any()
    hol = {pd.Timestamp(s) for s in FESTIVOS_CAM}
    dow = d.fecha.dt.day_of_week
    d["hol"] = d.fecha.isin(hol)
    d["fin"] = (dow >= 5) | d.hol  # noqa: PLR2004
    d["daytype"] = np.where(d.hol | (dow == 6), 2, np.where(dow == 5, 1, 0))  # noqa: PLR2004
    d["dow"] = dow
    d["ts"] = _ns(d.programada)
    # Medianoche (Madrid) del día de servicio: los minutos del dominio cuentan desde ahí.
    d["m0"] = _ns(d.fecha.dt.tz_localize("Europe/Madrid"))
    d.attrs["saltados"] = saltados
    return d


def _ns(s: pd.Series) -> pd.Series:
    return s.dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]").astype("int64")


def _conocido(tabla: pd.DataFrame) -> pd.Series:
    """Cuándo se supo la medida (``ml.py``): nunca antes del suceso + 1 min."""
    tk = tabla.ultimo_ts.where(
        tabla.ultimo_ts.notna(), tabla.programada + pd.to_timedelta(tabla.retraso_s + 120, unit="s")
    )
    tk = np.maximum(tk, tabla.programada + pd.to_timedelta(tabla.retraso_s + 60, unit="s"))
    return _ns(tk)


KB, KC, KT = ["tren", "parada_id", "fin"], ["familia", "parada_id", "hora", "fin"], ["tren", "fin"]


def stats(H: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    g = H.groupby(KB).y
    b = pd.DataFrame(
        {
            "h_med": g.median(),
            "h_q10": g.quantile(0.1),
            "h_q25": g.quantile(0.25),
            "h_q75": g.quantile(0.75),
            "h_q90": g.quantile(0.9),
            "h_mean": g.mean(),
            "h_n": g.size(),
        }
    )
    c = H.groupby(KC).y.median().rename("h_lsh_med").to_frame()
    t = H.groupby(KT).y.median().rename("h_train_med").to_frame()
    return b, c, t


def preparar(d: pd.DataFrame, fin_entreno: pd.Timestamp) -> dict[str, Any]:  # noqa: PLR0915 - como ml.py
    """Filas objetivo, histórico OOF, emparejamiento y observaciones (como ``ml.py``)."""
    D = d[d.medido & ~d.es_destino & d.programada.notna() & ~d.es_especial & d.retraso_s.notna()].copy()
    D["y"] = D.retraso_s / 60
    D = D[(D.y <= P.RETRASO_MAX_MIN) & (D.y >= P.RETRASO_MIN_MIN)]
    D["split"] = np.where(D.fecha <= fin_entreno, "train", "test")
    D = D.reset_index(drop=True)
    D["tk"] = _conocido(D)
    sched = d[d.programada.notna() & ~d.es_especial]
    orig = sched[sched.es_origen][["fecha", "trip_id", "origen_id", "ts", "familia", "tren"]]
    orig = orig.drop_duplicates(["fecha", "trip_id"])
    orig["par"] = orig.tren.str[-1].astype(int) % 2
    D = D.merge(
        orig[["fecha", "trip_id", "ts"]].rename(columns={"ts": "t0"}), on=["fecha", "trip_id"], how="left"
    )
    D["tiene_origen"] = D.t0.notna()
    miss = D.t0.isna()
    D.loc[miss, "t0"] = D[miss].groupby(["fecha", "trip_id"]).ts.transform("min")
    D["t0"] = D.t0.astype("int64")

    hcols = list(P.RASGOS_HISTORICOS)
    for c in hcols:
        D[c] = np.nan
    tr_days = sorted(D[D.split == "train"].fecha.unique())
    fold = {t: i % NFOLD for i, t in enumerate(tr_days)}
    D["fold"] = D.fecha.map(fold).fillna(-1).astype(int)

    def fill(sel: pd.Series, H: pd.DataFrame) -> None:
        b, c, t = stats(H)
        s = D.loc[sel, [*KB, "familia", "hora"]].join(b, on=KB).join(c, on=KC).join(t, on=KT)
        D.loc[sel, hcols] = s[hcols].values

    Htr = D[D.split == "train"]
    for f in range(NFOLD):
        fill((D.split == "train") & (D.fold == f), Htr[Htr.fold != f])
    if (D.split != "train").any():
        fill(D.split != "train", Htr)
    D["p_med"] = D.h_med.fillna(D.h_lsh_med).fillna(0.0)
    D["res"] = D.y - D.p_med

    # Emparejamiento de cabecera (solo horario), igual que ml.py y que el dominio.
    arr = sched[sched.es_destino][
        ["fecha", "trip_id", "destino_id", "ts", "familia", "tren", "retraso_s", "medido"]
    ]
    arr = arr.drop_duplicates(["fecha", "trip_id"]).rename(
        columns={"trip_id": "trip_in", "destino_id": "term", "ts": "arr"}
    )
    arr["par"] = arr.tren.str[-1].astype(int) % 2
    dep = orig.rename(columns={"origen_id": "term", "ts": "dep"})
    m = arr.merge(
        dep[["fecha", "trip_id", "term", "dep", "familia", "par"]],
        on=["fecha", "term", "familia"],
        suffixes=("_in", ""),
    )
    m = m[
        (m.par != m.par_in)
        & (m.dep >= m.arr + MANIOBRA_MIN_NS)
        & (m.dep <= m.arr + MANIOBRA_MAX_NS)
        & (m.trip_id != m.trip_in)
    ]
    m = m.sort_values("dep", kind="stable").groupby(["fecha", "trip_in"]).head(1)
    m = m.sort_values("arr", kind="stable").groupby(["fecha", "trip_id"]).tail(1)
    m["lay"] = (m.dep - m.arr) / MIN
    m["arr_act"] = m.arr + (m.retraso_s.fillna(0) * 1e9).astype("int64")
    obs = d[d.medido & d.retraso_s.notna()][
        ["fecha", "trip_id", "parada_id", "programada", "retraso_s", "ultimo_ts", "m0"]
    ].copy()
    obs["tk"] = _conocido(obs)
    D = D.merge(m[["fecha", "trip_id", "lay"]], on=["fecha", "trip_id"], how="left")
    D["has_pair"] = D.lay.notna().astype(float)
    for c in ["familia", "parada_id", "origen_id", "destino_id"]:
        D[c + "_c"] = D[c].astype("category")
    D["tren_n"] = pd.to_numeric(D.tren, errors="coerce")
    D["dir"] = D.tren.str[-1].astype(int) % 2
    D["mod"] = D.programada.dt.hour * 60 + D.programada.dt.minute
    D["hol_f"] = D.hol.astype(int)
    D["fin_f"] = D.fin.astype(int)
    return {"D": D, "m": m, "obs": obs}


def rasgos_vivos(prep: dict[str, Any], h: float) -> pd.DataFrame:
    """Rasgos del vivo vistos ``h`` min antes de ``t0`` (``h = 0`` = backtest)."""
    D, m, obs = prep["D"], prep["m"].copy(), prep["obs"]
    hns = h * MIN
    m["vista"] = m.dep - hns
    ok = (m.medido & m.retraso_s.notna() & (m.arr_act + MIN <= m.vista)).values
    m["inc_delay"] = np.where(ok, m.retraso_s / 60, np.nan)
    m["inc_prop"] = np.where(ok, np.maximum(0, m.retraso_s / 60 - (m.lay - P.MANIOBRA_FISICA_MIN)), np.nan)
    m["inc_arrived"] = ok.astype(float)
    x = m[["fecha", "trip_id", "trip_in", "dep", "vista"]].merge(
        obs[["fecha", "trip_id", "retraso_s", "tk"]].rename(columns={"trip_id": "trip_in"}),
        on=["fecha", "trip_in"],
    )
    x = x[x.tk <= x.vista].sort_values("tk", kind="stable").groupby(["fecha", "trip_id"]).tail(1)
    x["inc_live_delay"] = x.retraso_s / 60
    x["inc_live_age"] = (x.dep - x.tk) / MIN
    m = m.merge(
        x[["fecha", "trip_id", "inc_live_delay", "inc_live_age"]], on=["fecha", "trip_id"], how="left"
    )
    m["inc_live_eta_late"] = np.where(
        m.inc_live_delay.notna(),
        np.maximum(0, (m.arr + m.inc_live_delay.fillna(0) * MIN - m.dep) / MIN + P.MANIOBRA_FISICA_MIN),
        np.nan,
    )
    cols = ["inc_delay", "inc_prop", "inc_live_delay", "inc_live_age", "inc_arrived", "inc_live_eta_late"]
    out = D[["fecha", "trip_id"]].merge(m[["fecha", "trip_id", *cols]], on=["fecha", "trip_id"], how="left")
    out.index = D.index

    # Estado de la línea: observaciones de la misma familia en [asof - 60, asof), otros viajes.
    ls_mean = np.full(len(D), np.nan)
    ls_res = np.full(len(D), np.nan)
    ls_n = np.zeros(len(D))
    W = P.VENTANA_ESTADO_LINEA_MIN * MIN
    for (_, _), g in D.groupby(["fecha", "familia"]):
        o = g.sort_values("tk", kind="stable")
        tks = o.tk.values
        cy = np.r_[0, np.cumsum(o.y.values)]
        cr = np.r_[0, np.cumsum(o.res.values)]
        asof = g.t0.values - hns
        lo = np.searchsorted(tks, asof - W, "left")
        hi = np.searchsorted(tks, asof, "left")
        n = (hi - lo).astype(float)
        sy = cy[hi] - cy[lo]
        sr = cr[hi] - cr[lo]
        tw = g[["trip_id", "t0"]].reset_index()
        tw["vista"] = tw.t0 - hns
        j = tw.merge(g[["trip_id", "tk", "y", "res"]], on="trip_id")
        j = j[(j.tk >= j.vista - W) & (j.tk < j.vista)]
        if len(j):
            a = (
                j.groupby("index")
                .agg(ny=("y", "size"), sy=("y", "sum"), sr=("res", "sum"))
                .reindex(g.index)
                .fillna(0)
            )
            n -= a.ny.values
            sy -= a.sy.values
            sr -= a.sr.values
        pos = D.index.get_indexer(g.index)
        with np.errstate(invalid="ignore", divide="ignore"):
            ls_mean[pos] = np.where(n > 0, sy / n, np.nan)
            ls_res[pos] = np.where(n > 0, sr / n, np.nan)
        ls_n[pos] = n
    out["ls_mean"], out["ls_res"], out["ls_n"] = ls_mean, ls_res, ls_n
    return out.drop(columns=["fecha", "trip_id"])


def vivos_mezclados(prep: dict[str, Any], horizontes: tuple[float, ...], semilla: int = 11) -> pd.DataFrame:
    """Rasgos del vivo con una antelación al azar por fila (ver ``HORIZONTES_ENTRENO``)."""
    D = prep["D"]
    eleccion = np.random.default_rng(semilla).integers(0, len(horizontes), size=len(D))
    clave = ("vivos", horizontes)
    if clave in prep:
        return prep[clave]
    out: pd.DataFrame | None = None
    for i, h in enumerate(horizontes):
        V = rasgos_vivos(prep, h)
        if out is None:
            out = V.copy()
        else:
            sel = eleccion == i
            out.loc[sel] = V.loc[sel].values
    assert out is not None
    prep[clave] = out
    return out


def matriz(prep: dict[str, Any], vivos: pd.DataFrame, rasgos: tuple[str, ...]) -> pd.DataFrame:
    D = prep["D"]
    X = pd.concat([D, vivos], axis=1)
    return X[list(rasgos)]


def entrenar(X: pd.DataFrame, y: pd.Series, rondas: int, hilos: int) -> lgb.Booster:
    return lgb.train(dict(PARAMS, num_threads=hilos), lgb.Dataset(X, y), rondas)


# ----------------------------------------------------------------- exportar


def _hijo(nodo: dict[str, Any], hojas: list[float], pila: list[dict[str, Any]]) -> int:
    """Índice del hijo con la convención de LightGBM (hoja = ``~índice``)."""
    if "leaf_index" in nodo:
        hojas[nodo["leaf_index"]] = float(nodo["leaf_value"])
        return ~int(nodo["leaf_index"])
    pila.append(nodo)
    return int(nodo["split_index"])


def exportar_arboles(bst: lgb.Booster) -> list[dict[str, Any]]:
    arboles = []
    for info in bst.dump_model()["tree_info"]:
        raiz = info["tree_structure"]
        n = info["num_leaves"] - 1
        cols: dict[str, list[Any]] = {k: [None] * n for k in ("f", "t", "c", "m", "d", "l", "r")}
        hojas = [0.0] * info["num_leaves"]
        if "leaf_value" in raiz:
            hojas[0] = float(raiz["leaf_value"])
        pila = [raiz] if "split_index" in raiz else []

        while pila:
            nodo = pila.pop()
            i = nodo["split_index"]
            cols["f"][i] = int(nodo["split_feature"])
            if nodo["decision_type"] == "==":
                cols["c"][i] = sorted(int(v) for v in str(nodo["threshold"]).split("||"))
                cols["t"][i] = 0.0
            else:
                cols["t"][i] = float(nodo["threshold"])
            cols["m"][i] = P.TIPOS_AUSENTE[nodo["missing_type"]]
            cols["d"][i] = bool(nodo["default_left"])
            cols["l"][i] = _hijo(nodo["left_child"], hojas, pila)
            cols["r"][i] = _hijo(nodo["right_child"], hojas, pila)
        arboles.append({**cols, "v": hojas})
    return arboles


def exportar_modelo(bst: lgb.Booster, X: pd.DataFrame, rasgos: tuple[str, ...]) -> dict[str, Any]:
    cats = {c: [str(v) for v in X[c].cat.categories] for c in P.RASGOS_CATEGORICOS}
    return {
        "features": list(rasgos),
        "categories": cats,
        "trees": exportar_arboles(bst),
        "objective": "l1",
        "num_boost_round": bst.num_trees(),
        "params": PARAMS,
    }


def exportar_stats(D: pd.DataFrame, familias: set[str]) -> dict[str, Any]:
    H = D[D.split == "train"]
    b, c, t = stats(H)
    trenes = set(H[H.familia.isin(familias)].tren)
    b = b[b.index.get_level_values("tren").isin(trenes)]
    c = c[c.index.get_level_values("familia").isin(familias)]
    t = t[t.index.get_level_values("tren").isin(trenes)]
    r = lambda v: round(float(v), 4)  # noqa: E731
    return {
        "tren_parada": [
            [[k[0], k[1], bool(k[2])], [r(x) for x in row]] for k, row in zip(b.index, b.values, strict=True)
        ],
        "franja": [
            [[k[0], k[1], int(k[2]), bool(k[3])], r(v)] for k, v in zip(c.index, c.h_lsh_med, strict=True)
        ],
        "tren": [[[k[0], bool(k[1])], r(v)] for k, v in zip(t.index, t.h_train_med, strict=True)],
    }


# ----------------------------------------------------------------- evaluar


def met(y: Any, p: Any) -> dict[str, Any] | None:
    e = np.abs(np.asarray(y) - np.asarray(p))
    if len(e) == 0:
        return None
    return {
        "mae": round(float(e.mean()), 2),
        "w2": round(float(100 * (e <= MARGEN_ACIERTO_MIN).mean()), 1),
        "n": len(e),
    }


def cubre(nombre: str, V: pd.DataFrame) -> pd.Series:
    """Filas en las que producción SÍ predice (hay entrada en vivo)."""
    if nombre == P.MODELO_ESTADO_LINEA:
        return V.ls_n > 0
    return V.inc_live_delay.notna()


def evaluar(prep: dict[str, Any], modelos: dict[str, lgb.Booster], horizontes: list[float]) -> dict[str, Any]:
    D = prep["D"]
    te = (D.split == "test").values
    out: dict[str, Any] = {}
    for h in horizontes:
        V = rasgos_vivos(prep, h)
        res: dict[str, Any] = {}
        for linea, nombre in LINEAS.items():
            sel = te & (D.familia == linea).values & D.tiene_origen.values
            X = matriz(prep, V, P.RASGOS_POR_MODELO[nombre])[sel]
            y = D.y[sel]
            pred = np.round(modelos[nombre].predict(X))
            c = cubre(nombre, V[sel]).values
            # Lo que vería el viajero: modelo donde predice, «en hora» donde no (como hoy).
            mostrado = np.where(c, pred, 0.0)
            base = met(y, np.zeros(len(y)))
            r = {
                "cobertura_pct": round(float(100 * c.mean()), 1) if len(c) else None,
                "todas": {
                    "en_hora": base,
                    "p_med": met(y, np.round(D.p_med[sel])),
                    "modelo": met(y, pred),
                    "mostrado": met(y, mostrado),
                },
                "cubiertas": {"en_hora": met(y[c], np.zeros(int(c.sum()))), "modelo": met(y[c], pred[c])},
            }
            # El listón se mide donde el viajero VE la previsión (filas cubiertas),
            # contra lo que ve hoy en esas mismas filas (sin retraso = «en hora»).
            b, mm = r["cubiertas"]["en_hora"], r["cubiertas"]["modelo"]
            r["pasa_liston"] = False
            if b and mm:
                mejora = 100 * (b["mae"] - mm["mae"]) / b["mae"] if b["mae"] else 0.0
                r["mejora_pct"] = round(mejora, 1)
                r["pasa_liston"] = bool(mejora >= LISTON_MEJORA and mm["w2"] >= LISTON_W2)
            res[linea] = r
        out[str(int(h))] = res
    return out


def horizonte_maximo(por_horizonte: dict[str, Any], linea: str) -> float | None:
    """Mayor antelación tal que la línea pasa el listón en ella y en todas las menores."""
    maximo = None
    for h in sorted(por_horizonte, key=float):
        r = por_horizonte[h][linea]
        if not r["pasa_liston"]:
            break
        maximo = float(h)
    return maximo


def paridad(
    prep: dict[str, Any], modelos: dict[str, lgb.Booster], exportados: dict[str, Any], n: int = 3000
) -> dict[str, float]:
    """Máxima diferencia entre la evaluación en Python puro y ``Booster.predict``."""
    D = prep["D"]
    V = rasgos_vivos(prep, 0)
    out = {}
    for nombre, bst in modelos.items():
        rasgos = P.RASGOS_POR_MODELO[nombre]
        X = matriz(prep, V, rasgos)
        X = X[(D.split == "test").values].sample(min(n, int((D.split == "test").sum())), random_state=1)
        ref = bst.predict(X)
        mod = P.ModeloLgbm.from_json(exportados[nombre], rasgos)
        dif = 0.0
        for (_, fila), r in zip(X.iterrows(), ref, strict=True):
            vals = {
                k: (None if pd.isna(v) else (str(v) if k in P.RASGOS_CATEGORICOS else float(v)))
                for k, v in fila.items()
            }
            dif = max(dif, abs(mod.predecir(vals) - float(r)))
        out[nombre] = dif
    return out


def _hhmmss(minuto: float) -> str:
    m = round(minuto)
    return f"{m // 60:02d}:{m % 60:02d}:00"


def desfase(
    prep: dict[str, Any], d: pd.DataFrame, artefacto: P.Artefacto, h: float, n_viajes: int = 60
) -> dict[str, Any]:
    """Rasgos del DOMINIO (horario + observaciones) frente a los de pandas, fila a fila."""
    D = prep["D"]
    V = rasgos_vivos(prep, h)
    rng = np.random.default_rng(3)
    informe: dict[str, Any] = {}
    for linea, nombre in LINEAS.items():
        rasgos = P.RASGOS_POR_MODELO[nombre]
        X = matriz(prep, V, rasgos)
        sel = D[(D.split == "test") & (D.familia == linea) & D.tiene_origen]
        claves = sel[["fecha", "trip_id"]].drop_duplicates().values
        elegidos = claves[rng.choice(len(claves), size=min(n_viajes, len(claves)), replace=False)]
        filas = distintas = 0
        peores: dict[str, int] = {}
        for fecha, trip in elegidos:
            dia = d[(d.fecha == fecha) & d.programada.notna() & ~d.es_especial]
            m0 = int(dia.m0.iloc[0])
            horario: dict[str, list[ScheduledStop]] = {}
            for r in dia.itertuples():
                horario.setdefault(r.parada_id, []).append(
                    ScheduledStop(
                        r.trip_id,
                        _hhmmss((r.ts - m0) / MIN),
                        r.linea,
                        r.destino,
                        bool(r.es_destino),
                        is_origin=bool(r.es_origen),
                        stop_id=r.parada_id,
                        seq=int(r.seq),
                    )
                )
            viajes = P.indice_viajes(horario)
            ob = prep["obs"][prep["obs"].fecha == fecha]
            observaciones = [
                P.Observacion(t, s, int(rs), (tk - m0) / MIN)
                for t, s, rs, tk in zip(ob.trip_id, ob.parada_id, ob.retraso_s, ob.tk, strict=True)
            ]
            viaje = viajes[trip]
            pred = P.Predictor(
                artefacto,
                viajes,
                P.emparejar_entrantes(viajes.values()),
                observaciones,
                fecha_servicio=pd.Timestamp(fecha).date(),
                ahora_min=viaje.salida_min - h,
                rutas=frozenset({linea}),
            )
            for idx in sel[(sel.fecha == fecha) & (sel.trip_id == trip)].index:
                vals = pred.rasgos(trip, D.parada_id[idx])
                if vals is None:  # producción no predice: la fila no cuenta como desfase
                    continue
                filas += 1
                fila_ok = True
                for k in rasgos:
                    a, b = vals.get(k), X.at[idx, k]
                    b = None if pd.isna(b) else (str(b) if k in P.RASGOS_CATEGORICOS else float(b))
                    igual = (a is None and b is None) or (
                        a is not None
                        and b is not None
                        and (a == b if isinstance(b, str) else abs(float(a) - b) <= TOLERANCIA_RASGO)
                    )
                    if not igual:
                        fila_ok = False
                        peores[k] = peores.get(k, 0) + 1
                distintas += not fila_ok
        informe[linea] = {"filas": filas, "filas_con_desfase": distintas, "por_rasgo": peores}
    return informe


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--historico", type=Path, required=True, help="directorio con <día>.parquet")
    ap.add_argument("--desde", type=date.fromisoformat, required=True)
    ap.add_argument("--hasta", type=date.fromisoformat, required=True)
    ap.add_argument(
        "--prueba-desde",
        type=date.fromisoformat,
        required=True,
        help="holdout [prueba-desde, hasta]: fija la antelación máxima de cada línea",
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--horizontes", default="0,30,60,90,120")
    ap.add_argument("--rondas", type=int, default=600)
    ap.add_argument("--hilos", type=int, default=8)
    ap.add_argument("--solo-evaluar", action="store_true")
    a = ap.parse_args(argv)
    t_ini = time.time()
    excluidos = dias_excluidos()
    d = cargar(a.historico, a.desde, a.hasta, excluidos)
    horizontes = [float(x) for x in a.horizontes.split(",") if x]
    informe: dict[str, Any] = {"excluded_days_skipped": d.attrs["saltados"]}

    fin = pd.Timestamp(a.prueba_desde - timedelta(days=1))
    prep = preparar(d, fin)
    tr = (prep["D"].split == "train").values
    modelos, exportados = {}, {}
    for nombre in sorted(set(LINEAS.values())):
        V0 = vivos_mezclados(prep, HORIZONTES_ENTRENO[nombre])
        X = matriz(prep, V0, P.RASGOS_POR_MODELO[nombre])
        modelos[nombre] = entrenar(X[tr], prep["D"].y[tr], a.rondas, a.hilos)
        exportados[nombre] = exportar_modelo(modelos[nombre], X, P.RASGOS_POR_MODELO[nombre])
    por_h = evaluar(prep, modelos, horizontes)
    informe["holdout"] = {
        "train": [str(a.desde), str(fin.date())],
        "test": [str(a.prueba_desde), str(a.hasta)],
        "liston": {"mejora_mae_pct": LISTON_MEJORA, "w2_pct": LISTON_W2},
        "por_horizonte_min": por_h,
    }
    informe["horizonte_max_min"] = {linea: horizonte_maximo(por_h, linea) for linea in LINEAS}
    informe["paridad_max_abs"] = paridad(prep, modelos, exportados)
    sin_tope = dict.fromkeys(LINEAS, 1e9)
    art_eval = P.Artefacto.from_json(
        _artefacto(prep, exportados, a=a, excluidos=excluidos, topes=sin_tope, informe={})
    )
    informe["desfase_rasgos"] = {str(int(h)): desfase(prep, d, art_eval, h) for h in (0.0, 30.0)}
    print(json.dumps(informe, ensure_ascii=False, indent=1))
    if a.solo_evaluar:
        return 0
    topes = {k: v for k, v in informe["horizonte_max_min"].items() if v is not None}
    if not topes:
        print("ninguna línea pasa el listón en el holdout: no se escribe artefacto", file=sys.stderr)
        return 3

    prep = preparar(d, pd.Timestamp(a.hasta))
    exportados = {}
    for nombre in sorted({LINEAS[k] for k in topes}):
        V0 = vivos_mezclados(prep, HORIZONTES_ENTRENO[nombre])
        X = matriz(prep, V0, P.RASGOS_POR_MODELO[nombre])
        bst = entrenar(X, prep["D"].y, a.rondas, a.hilos)
        exportados[nombre] = exportar_modelo(bst, X, P.RASGOS_POR_MODELO[nombre])
    art = _artefacto(prep, exportados, a=a, excluidos=excluidos, topes=topes, informe=informe)
    P.Artefacto.from_json(art)  # el mismo validador que la ingesta
    a.out.mkdir(parents=True, exist_ok=True)
    crudo = json.dumps(art, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    destino = a.out / f"modelo-{art['version']}.json.gz"
    destino.write_bytes(gzip.compress(crudo, mtime=0))
    (a.out / f"informe-{art['version']}.json").write_text(json.dumps(informe, ensure_ascii=False, indent=1))
    print(
        f"artefacto {destino} ({destino.stat().st_size / 1e6:.2f} MB gz, {len(crudo) / 1e6:.2f} MB json) "
        f"en {time.time() - t_ini:.0f} s"
    )
    return 0


def _artefacto(  # noqa: PLR0913 - todo con nombre
    prep: dict[str, Any],
    exportados: dict[str, Any],
    *,
    a: argparse.Namespace,
    excluidos: dict[str, str],
    topes: dict[str, float],
    informe: dict[str, Any],
) -> dict[str, Any]:
    D = prep["D"]
    ahora = datetime.now(tz=UTC)
    huella = hashlib.sha256(json.dumps(exportados, sort_keys=True).encode()).hexdigest()[:8]
    dias = sorted(str(x.date()) for x in D[D.split == "train"].fecha.unique())
    return {
        "format": P.FORMATO,
        "version": f"{a.hasta.isoformat()}-{huella}",
        "created_at": ahora.isoformat(),
        "trained_on": {
            "from": dias[0],
            "to": dias[-1],
            "days": len(dias),
            "rows": int((D.split == "train").sum()),
        },
        "excluded_days": sorted(excluidos),
        "lines": {
            k: {
                "model": LINEAS[k],
                "max_horizon_min": v,
                "train_horizons_min": list(HORIZONTES_ENTRENO[LINEAS[k]]),
            }
            for k, v in topes.items()
        },
        "models": exportados,
        "stats": exportar_stats(D, set(LINEAS)),
        "holidays": list(FESTIVOS_CAM),
        "metrics": informe,
    }


if __name__ == "__main__":
    sys.exit(main())
