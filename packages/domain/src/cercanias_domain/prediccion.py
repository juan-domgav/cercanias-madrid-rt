"""Retraso PREDICHO con LightGBM para C1 y C5 (RB-021). Sin dependencias.

El backtest semanal del 23/11/2026 (prueba 14-22/11, sin el 16 ni el 17) dio
dos modelos que superan el listón (>= 30 % menos error que «en hora» y >= 60 %
a +-2 min) dos semanas seguidas:

- **C1, modelo de estado de la línea** (``estado_linea``): MAE 1,71 min, 82 % a
  +-2 min (en hora: 2,64 / 58 %). Usa cómo van AHORA los demás trenes de la
  línea: media del retraso medido y del residuo sobre su mediana histórica en
  la última hora (``ls_mean``, ``ls_res``, ``ls_n``).
- **C5, modelo del entrante en vivo** (``entrante_vivo``): MAE 3,26 min, 64 % a
  +-2 min (en hora: 5,99 / 39 %). Usa el último retraso visto del tren que llega
  a cabecera para dar la vuelta (``inc_live_*``).

El modelo se entrena fuera (``scripts/entrenar_prediccion.py``, con LightGBM y
pandas) y se exporta a un JSON con los árboles. Aquí se EVALÚAN esos árboles en
Python puro: la imagen de la ingesta no gana numpy, lightgbm ni libgomp. El
script comprueba que esta evaluación coincide con ``Booster.predict``.

Reglas que no son evidentes:

- **Sólo predice cuando hay entrada en vivo.** C1 sin observaciones de la línea
  en la última hora (``ls_n == 0``) o C5 sin dato del entrante = ``None``: el
  tablón se queda exactamente como estaba. Nunca se predice «a ciegas».
- **Sólo hasta la antelación que pasó el listón.** En producción el viaje aún
  no ha salido de su origen (fuera del feed o en el andén de cabecera), así que
  los rasgos se ven ANTES de ``t0``. El script mide el holdout a 0, 30, 60…
  min y el artefacto trae, por línea, la mayor antelación a la que pasa
  (14-22/11/2026: C1 30 min, C5 60 min). Más allá, ``None``.
- **Los rasgos se calculan como en el entrenamiento**, a la hora de salida del
  viaje en su ORIGEN (``t0``) o antes: ``asof = min(ahora, t0)``. Nada de lo que
  ocurra después de ``t0`` entra, igual que en el backtest.
- **El emparejamiento con el entrante es el del entrenamiento**
  (:func:`emparejar_entrantes`), no el ``from`` del horario: si el modelo
  aprendió con otro tren entrante, darle el del horario sería otro rasgo.
- **Artefacto inválido = sin predicción.** :meth:`Artefacto.from_json` valida
  todo y falla cerrado con :class:`ArtefactoInvalido`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from cercanias_domain.departures import ScheduledStop, parse_hhmm_to_minutes, train_number
from cercanias_domain.trip_id import parse_trip_id

FORMATO = "cercanias-prediccion-lgbm/1"

MODELO_ESTADO_LINEA = "estado_linea"
MODELO_ENTRANTE_VIVO = "entrante_vivo"
MODELOS = (MODELO_ESTADO_LINEA, MODELO_ENTRANTE_VIVO)

#: Rasgos comunes (mismo orden y nombre que ``ml.py`` del backtest).
RASGOS_CATEGORICOS = ("familia_c", "parada_id_c", "origen_id_c", "destino_id_c")
RASGOS_HISTORICOS = (
    "h_med", "h_q10", "h_q25", "h_q75", "h_q90", "h_mean", "h_n", "h_lsh_med", "h_train_med",
)
RASGOS_BASE = (
    *RASGOS_CATEGORICOS, "tren_n", "dir", "hora", "mod", "dow", "hol_f", "fin_f", "daytype", "seq",
    *RASGOS_HISTORICOS,
)
RASGOS_ESTADO_LINEA = ("ls_mean", "ls_res", "ls_n")
RASGOS_ENTRANTE = ("lay", "has_pair", "inc_delay", "inc_prop")
RASGOS_ENTRANTE_VIVO = (
    *RASGOS_ENTRANTE, "inc_live_delay", "inc_live_age", "inc_arrived", "inc_live_eta_late",
)
RASGOS_POR_MODELO: dict[str, tuple[str, ...]] = {
    MODELO_ESTADO_LINEA: (*RASGOS_BASE, *RASGOS_ESTADO_LINEA),
    MODELO_ENTRANTE_VIVO: (*RASGOS_BASE, *RASGOS_ENTRANTE_VIVO),
}

#: Estado de la línea: observaciones de la última hora (backtest: 60 min).
VENTANA_ESTADO_LINEA_MIN = 60.0
#: Emparejamiento de cabecera del backtest: la primera salida en sentido
#: contrario >= 5 min y <= 90 min después de la llegada, misma familia.
MANIOBRA_MIN_MIN = 5.0
MANIOBRA_MAX_MIN = 90.0
#: ``inc_prop = max(0, retraso_entrante - (maniobra - 3))``.
MANIOBRA_FISICA_MIN = 3.0
#: Población del backtest: retrasos fuera de [-5, 60] min no son medidas útiles.
RETRASO_MIN_MIN = -5.0
RETRASO_MAX_MIN = 60.0
#: Un dato del entrante sólo vale si su llegada real + 1 min ya pasó (estricto).
MARGEN_LLEGADA_MIN = 1.0
#: Un artefacto más viejo que esto no se usa (como RB-013): el modelo aprende de
#: la temporada y de los horarios vigentes.
CADUCIDAD_DIAS = 21
#: Tipo de día (``daytype``): 0 laborable, 1 sábado, 2 domingo o festivo.
_SABADO = 5
_DOMINGO = 6

_FALTA_NINGUNA = 0
_FALTA_CERO = 1
_FALTA_NAN = 2
TIPOS_AUSENTE = {"None": _FALTA_NINGUNA, "Zero": _FALTA_CERO, "NaN": _FALTA_NAN}
#: ``kZeroThreshold`` de LightGBM.
_CERO = 1e-35

Valor = float | str | None


class ArtefactoInvalido(ValueError):
    """El artefacto no tiene la forma esperada. Nunca se usa a medias."""


def _exigir(condicion: bool, mensaje: str) -> None:
    if not condicion:
        raise ArtefactoInvalido(mensaje)


# ---------------------------------------------------------------- árboles


@dataclass(frozen=True)
class Arbol:
    """Un árbol de LightGBM en arrays planos (convención de LightGBM).

    ``izq``/``der``: >= 0 es otro nodo interno; < 0 es la hoja ``~valor``.
    """

    rasgo: tuple[int, ...]
    umbral: tuple[float, ...]
    #: Conjunto de categorías que van a la izquierda; ``None`` = nodo numérico.
    categorias: tuple[frozenset[int] | None, ...]
    falta: tuple[int, ...]
    izq_por_defecto: tuple[bool, ...]
    izq: tuple[int, ...]
    der: tuple[int, ...]
    hojas: tuple[float, ...]

    def evaluar(self, x: Sequence[float]) -> float:
        """Valor de la hoja para ``x`` (``nan`` = dato ausente)."""
        if not self.rasgo:
            return self.hojas[0]
        nodo = 0
        while nodo >= 0:
            v = x[self.rasgo[nodo]]
            cats = self.categorias[nodo]
            if cats is not None:
                ir_izq = not math.isnan(v) and v >= 0 and int(v) in cats
            else:
                falta = self.falta[nodo]
                if math.isnan(v) and falta != _FALTA_NAN:
                    v = 0.0
                if (falta == _FALTA_CERO and abs(v) <= _CERO) or (falta == _FALTA_NAN and math.isnan(v)):
                    ir_izq = self.izq_por_defecto[nodo]
                else:
                    ir_izq = v <= self.umbral[nodo]
            nodo = self.izq[nodo] if ir_izq else self.der[nodo]
        return self.hojas[~nodo]

    @classmethod
    def from_json(cls, datos: Any) -> Arbol:
        _exigir(isinstance(datos, dict), "árbol: no es un objeto")
        hojas = datos.get("v")
        _exigir(isinstance(hojas, list) and len(hojas) >= 1, "árbol: sin hojas")
        claves = ("f", "t", "c", "m", "d", "l", "r")
        columnas = {k: datos.get(k) for k in claves}
        n = len(hojas) - 1
        for k, col in columnas.items():
            _exigir(isinstance(col, list) and len(col) == n, f"árbol: '{k}' no tiene {n} nodos")
        cats: list[frozenset[int] | None] = []
        for c in columnas["c"]:
            _exigir(c is None or (isinstance(c, list) and all(isinstance(i, int) for i in c)),
                    "árbol: categorías mal formadas")
            cats.append(None if c is None else frozenset(c))
        hijos = [*columnas["l"], *columnas["r"]]
        _exigir(all(isinstance(h, int) and -len(hojas) <= h < n for h in hijos), "árbol: hijo fuera de rango")
        _exigir(all(isinstance(m, int) and m in TIPOS_AUSENTE.values() for m in columnas["m"]),
                "árbol: tipo de ausente desconocido")
        return cls(
            rasgo=tuple(int(f) for f in columnas["f"]),
            umbral=tuple(float(t) for t in columnas["t"]),
            categorias=tuple(cats),
            falta=tuple(columnas["m"]),
            izq_por_defecto=tuple(bool(d) for d in columnas["d"]),
            izq=tuple(columnas["l"]),
            der=tuple(columnas["r"]),
            hojas=tuple(float(v) for v in hojas),
        )


@dataclass(frozen=True)
class ModeloLgbm:
    """Suma de árboles sobre un vector de rasgos con nombre."""

    rasgos: tuple[str, ...]
    #: Rasgo categórico -> categoría -> índice (el orden de ``pandas_categorical``).
    categorias: Mapping[str, Mapping[str, int]]
    arboles: tuple[Arbol, ...]

    def vector(self, valores: Mapping[str, Valor]) -> list[float]:
        x: list[float] = []
        for nombre in self.rasgos:
            v = valores.get(nombre)
            if nombre in self.categorias:
                idx = self.categorias[nombre].get(str(v)) if v is not None else None
                x.append(math.nan if idx is None else float(idx))
            elif v is None or isinstance(v, str):
                x.append(math.nan)
            else:
                x.append(float(v))
        return x

    def predecir(self, valores: Mapping[str, Valor]) -> float:
        x = self.vector(valores)
        return math.fsum(a.evaluar(x) for a in self.arboles)

    @classmethod
    def from_json(cls, datos: Any, esperados: tuple[str, ...]) -> ModeloLgbm:
        _exigir(isinstance(datos, dict), "modelo: no es un objeto")
        rasgos = datos.get("features")
        _exigir(isinstance(rasgos, list) and tuple(rasgos) == esperados,
                "modelo: rasgos distintos de los que calcula la ingesta")
        cats = datos.get("categories")
        _exigir(isinstance(cats, dict) and set(cats) == set(RASGOS_CATEGORICOS),
                "modelo: faltan categorías")
        categorias: dict[str, dict[str, int]] = {}
        for nombre, lista in cats.items():
            _exigir(isinstance(lista, list) and all(isinstance(c, str) for c in lista),
                    f"modelo: categorías de {nombre} mal formadas")
            categorias[nombre] = {c: i for i, c in enumerate(lista)}
        arboles = datos.get("trees")
        _exigir(isinstance(arboles, list) and len(arboles) > 0, "modelo: sin árboles")
        modelo = cls(tuple(esperados), categorias, tuple(Arbol.from_json(a) for a in arboles))
        n = len(esperados)
        _exigir(all(0 <= f < n for a in modelo.arboles for f in a.rasgo), "modelo: rasgo fuera de rango")
        return modelo


# ----------------------------------------------------------- estadísticas


ClaveTrenParada = tuple[str, str, bool]


@dataclass(frozen=True)
class Estadisticas:
    """Histórico de entrenamiento: las mismas tablas que ``ml.py`` (``stats``)."""

    #: (tren, parada, fin) -> (med, q10, q25, q75, q90, mean, n).
    tren_parada: Mapping[ClaveTrenParada, tuple[float, ...]]
    #: (familia, parada, hora, fin) -> mediana.
    franja: Mapping[tuple[str, str, int, bool], float]
    #: (tren, fin) -> mediana.
    tren: Mapping[tuple[str, bool], float]

    def historicos(self, tren: str, familia: str, parada: str, hora: int, fin: bool) -> dict[str, Valor]:
        b = self.tren_parada.get((tren, parada, fin))
        valores: dict[str, Valor] = dict.fromkeys(RASGOS_HISTORICOS)
        if b is not None:
            valores.update(zip(RASGOS_HISTORICOS[:7], b, strict=True))
        valores["h_lsh_med"] = self.franja.get((familia, parada, hora, fin))
        valores["h_train_med"] = self.tren.get((tren, fin))
        return valores

    def mediana(self, tren: str, familia: str, parada: str, hora: int, fin: bool) -> float:
        """``p_med`` del backtest: mediana del tren en la parada, o de la franja, o 0."""
        b = self.tren_parada.get((tren, parada, fin))
        if b is not None:
            return b[0]
        return self.franja.get((familia, parada, hora, fin), 0.0)

    @classmethod
    def from_json(cls, datos: Any) -> Estadisticas:
        _exigir(isinstance(datos, dict), "estadísticas: no es un objeto")
        try:
            tp = {
                (str(k[0]), str(k[1]), bool(k[2])): tuple(float(x) for x in v)
                for k, v in datos["tren_parada"]
            }
            fr = {(str(k[0]), str(k[1]), int(k[2]), bool(k[3])): float(v) for k, v in datos["franja"]}
            tr = {(str(k[0]), bool(k[1])): float(v) for k, v in datos["tren"]}
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ArtefactoInvalido(f"estadísticas mal formadas: {exc!r}") from exc
        _exigir(all(len(v) == 7 for v in tp.values()), "estadísticas: tren_parada sin 7 valores")  # noqa: PLR2004
        return cls(tp, fr, tr)


# ------------------------------------------------------------- artefacto


@dataclass(frozen=True)
class LineaArtefacto:
    """Qué modelo sirve una línea y hasta qué antelación pasó el listón."""

    modelo: str
    #: Mayor antelación (min entre ahora y la salida de ORIGEN del viaje) a la que
    #: el holdout pasó el listón de Juan (>= 30 % menos MAE que «en hora», >= 60 %
    #: a +-2 min) en ella y en todas las menores. Más allá, no se predice.
    horizonte_max_min: float


@dataclass(frozen=True)
class Artefacto:
    version: str
    creado: datetime
    #: Familia de línea -> modelo y antelación máxima (``{"C1": ..., "C5": ...}``).
    lineas: Mapping[str, LineaArtefacto]
    modelos: Mapping[str, ModeloLgbm]
    estadisticas: Estadisticas
    festivos: frozenset[date]
    dias_excluidos: tuple[str, ...] = ()
    metricas: Mapping[str, Any] = field(default_factory=dict)

    def caducado(self, ahora: datetime) -> bool:
        return ahora - self.creado > timedelta(days=CADUCIDAD_DIAS)

    @classmethod
    def from_json(cls, datos: Any) -> Artefacto:
        _exigir(isinstance(datos, dict), "artefacto: no es un objeto")
        _exigir(datos.get("format") == FORMATO, f"artefacto: formato distinto de {FORMATO}")
        version = datos.get("version")
        _exigir(isinstance(version, str) and bool(version), "artefacto: sin versión")
        try:
            creado = datetime.fromisoformat(str(datos.get("created_at")))
        except ValueError as exc:
            raise ArtefactoInvalido("artefacto: created_at ilegible") from exc
        _exigir(creado.tzinfo is not None, "artefacto: created_at sin zona")
        crudas = datos.get("lines")
        _exigir(isinstance(crudas, dict) and bool(crudas), "artefacto: sin líneas")
        lineas: dict[str, LineaArtefacto] = {}
        for familia, linea in crudas.items():
            _exigir(isinstance(linea, dict) and linea.get("model") in MODELOS,
                    f"artefacto: modelo desconocido para {familia}")
            tope = linea.get("max_horizon_min")
            _exigir(isinstance(tope, int | float) and not isinstance(tope, bool) and tope >= 0,
                    f"artefacto: antelación máxima inválida para {familia}")
            lineas[str(familia)] = LineaArtefacto(str(linea["model"]), float(tope))
        crudos = datos.get("models")
        _exigir(isinstance(crudos, dict), "artefacto: sin modelos")
        modelos = {
            nombre: ModeloLgbm.from_json(crudos.get(nombre), RASGOS_POR_MODELO[nombre])
            for nombre in sorted({linea.modelo for linea in lineas.values()})
        }
        try:
            festivos = frozenset(date.fromisoformat(f) for f in datos.get("holidays", []))
        except (TypeError, ValueError) as exc:
            raise ArtefactoInvalido("artefacto: festivos ilegibles") from exc
        excluidos = datos.get("excluded_days", [])
        _exigir(isinstance(excluidos, list), "artefacto: excluded_days no es una lista")
        metricas = datos.get("metrics", {})
        return cls(
            version=str(version),
            creado=creado.astimezone(UTC),
            lineas=lineas,
            modelos=modelos,
            estadisticas=Estadisticas.from_json(datos.get("stats")),
            festivos=festivos,
            dias_excluidos=tuple(str(e) for e in excluidos),
            metricas=metricas if isinstance(metricas, dict) else {},
        )


# ---------------------------------------------------------------- viajes


@dataclass(frozen=True)
class Viaje:
    """Un viaje del horario del día, reunido desde los tablones por estación."""

    trip_id: str
    tren: str
    familia: str
    origen_id: str
    destino_id: str
    #: Minutos de la jornada (pueden pasar de 1440, como en el horario).
    salida_min: float
    llegada_min: float
    #: parada -> (seq, minuto programado).
    paradas: Mapping[str, tuple[int, float]]

    @property
    def paridad(self) -> int:
        return int(self.tren[-1]) % 2 if self.tren[-1:].isdigit() else -1


def familia_de(trip_id: str, route: str = "") -> str:
    parsed = parse_trip_id(trip_id)
    return parsed.route_group if parsed else route


def indice_viajes(horario: Mapping[str, Sequence[ScheduledStop]]) -> dict[str, Viaje]:
    """Viajes completos (con origen y final) a partir del horario por estación.

    Los especiales no tienen horario y no entran. Un viaje sin origen o sin final
    conocido (horario incompleto) tampoco: sin ``t0`` no hay rasgos.
    """
    paradas: dict[str, dict[str, tuple[int, float]]] = {}
    origen: dict[str, tuple[str, float]] = {}
    final: dict[str, tuple[str, float]] = {}
    ruta: dict[str, str] = {}
    for stop_id, lista in horario.items():
        for s in lista:
            if s.seq is None:
                continue
            minuto = float(parse_hhmm_to_minutes(s.time_hhmmss))
            paradas.setdefault(s.trip_id, {})[stop_id] = (s.seq, minuto)
            ruta.setdefault(s.trip_id, s.route)
            if s.is_origin:
                origen[s.trip_id] = (stop_id, minuto)
            if s.terminates_here:
                final[s.trip_id] = (stop_id, minuto)
    viajes: dict[str, Viaje] = {}
    for trip_id, ps in paradas.items():
        if trip_id not in origen or trip_id not in final:
            continue
        viajes[trip_id] = Viaje(
            trip_id=trip_id,
            tren=train_number(trip_id),
            familia=familia_de(trip_id, ruta[trip_id]),
            origen_id=origen[trip_id][0],
            destino_id=final[trip_id][0],
            salida_min=origen[trip_id][1],
            llegada_min=final[trip_id][1],
            paradas=ps,
        )
    return viajes


@dataclass(frozen=True)
class Enlace:
    """El tren que llega a cabecera y da la vuelta para hacer este viaje."""

    entrante: str
    llegada_min: float
    maniobra_min: float


def emparejar_entrantes(viajes: Iterable[Viaje]) -> dict[str, Enlace]:
    """Emparejamiento del backtest (``ml.py``, ``LAYOVER_MIN = 5``).

    1. Cada llegada forma la PRIMERA salida desde esa estación, misma familia y
       paridad contraria, entre 5 y 90 min después.
    2. Si varias llegadas forman la misma salida, se queda la más tardía.
    """
    lista = list(viajes)
    salidas: dict[tuple[str, str], list[Viaje]] = {}
    for v in lista:
        salidas.setdefault((v.origen_id, v.familia), []).append(v)
    for grupo in salidas.values():
        grupo.sort(key=lambda v: v.salida_min)
    elegido: dict[str, tuple[Viaje, Viaje]] = {}
    for llegada in lista:
        for salida in salidas.get((llegada.destino_id, llegada.familia), []):
            maniobra = salida.salida_min - llegada.llegada_min
            if (
                salida.trip_id != llegada.trip_id
                and salida.paridad != llegada.paridad
                and MANIOBRA_MIN_MIN <= maniobra <= MANIOBRA_MAX_MIN
            ):
                actual = elegido.get(salida.trip_id)
                if actual is None or llegada.llegada_min > actual[0].llegada_min:
                    elegido[salida.trip_id] = (llegada, salida)
                break
    return {
        sid: Enlace(ll.trip_id, ll.llegada_min, sal.salida_min - ll.llegada_min)
        for sid, (ll, sal) in elegido.items()
    }


# ----------------------------------------------------------- observaciones


@dataclass(frozen=True)
class Observacion:
    """Retraso visto de un tren en una parada (``next_stop`` del feed)."""

    trip_id: str
    stop_id: str
    retraso_s: int
    #: Minuto de la jornada en que se vio (misma escala que el horario).
    minuto: float


def tipo_de_dia(fecha: date, festivos: frozenset[date]) -> tuple[bool, bool, int]:
    """``(festivo, fin_de_semana_o_festivo, daytype)`` como en el backtest."""
    dow = fecha.weekday()
    festivo = fecha in festivos
    fin = dow >= _SABADO or festivo
    daytype = 2 if festivo or dow == _DOMINGO else 1 if dow == _SABADO else 0
    return festivo, fin, daytype


def _hora(minuto: float) -> int:
    return int(minuto // 60) % 24


def estado_linea(  # noqa: PLR0913 - rasgos con nombre, todos keyword-only
    observaciones: Iterable[Observacion],
    viajes: Mapping[str, Viaje],
    estadisticas: Estadisticas,
    *,
    familia: str,
    excluir_trip: str,
    asof: float,
    fin: bool,
) -> tuple[float | None, float | None, float]:
    """``(ls_mean, ls_res, ls_n)``: cómo va la línea en ``[asof - 60, asof)``.

    Como en el backtest: sólo paradas no finales de OTROS viajes de la misma
    familia, con retraso en [-5, 60] min. ``ls_res`` es la media del residuo
    sobre la mediana histórica de cada tren en esa parada.
    """
    n = 0
    suma = 0.0
    suma_res = 0.0
    for o in observaciones:
        if o.trip_id == excluir_trip or not (asof - VENTANA_ESTADO_LINEA_MIN <= o.minuto < asof):
            continue
        v = viajes.get(o.trip_id)
        if v is None or v.familia != familia or o.stop_id == v.destino_id or o.stop_id not in v.paradas:
            continue
        y = o.retraso_s / 60
        if not (RETRASO_MIN_MIN <= y <= RETRASO_MAX_MIN):
            continue
        hora = _hora(v.paradas[o.stop_id][1])
        n += 1
        suma += y
        suma_res += y - estadisticas.mediana(v.tren, v.familia, o.stop_id, hora, fin)
    if n == 0:
        return None, None, 0.0
    return suma / n, suma_res / n, float(n)


def rasgos_entrante(
    enlace: Enlace | None,
    observaciones: Iterable[Observacion],
    viajes: Mapping[str, Viaje],
    *,
    t0: float,
    asof: float,
) -> dict[str, Valor]:
    """Rasgos del entrante (``INC_LIVE`` del backtest) vistos en ``asof``."""
    valores: dict[str, Valor] = dict.fromkeys(RASGOS_ENTRANTE_VIVO)
    valores["has_pair"] = 0.0
    if enlace is None:
        return valores
    valores["has_pair"] = 1.0
    valores["lay"] = enlace.maniobra_min
    propias = sorted(
        (o for o in observaciones if o.trip_id == enlace.entrante and o.minuto <= asof),
        key=lambda o: o.minuto,
    )
    entrante = viajes.get(enlace.entrante)
    terminal = entrante.destino_id if entrante else None
    # Estricto: retraso en la llegada a cabecera, y la llegada real + 1 min ya pasó.
    en_cabecera = [o for o in propias if o.stop_id == terminal]
    llegado = False
    if en_cabecera:
        retraso = en_cabecera[-1].retraso_s / 60
        if enlace.llegada_min + retraso + MARGEN_LLEGADA_MIN <= asof:
            llegado = True
            valores["inc_delay"] = retraso
            valores["inc_prop"] = max(0.0, retraso - (enlace.maniobra_min - MANIOBRA_FISICA_MIN))
    valores["inc_arrived"] = 1.0 if llegado else 0.0
    if propias:
        ultima = propias[-1]
        vivo = ultima.retraso_s / 60
        valores["inc_live_delay"] = vivo
        valores["inc_live_age"] = t0 - ultima.minuto
        valores["inc_live_eta_late"] = max(0.0, enlace.llegada_min + vivo - t0 + MANIOBRA_FISICA_MIN)
    return valores


def rasgos_base(
    viaje: Viaje,
    stop_id: str,
    estadisticas: Estadisticas,
    fecha_servicio: date,
    festivos: frozenset[date],
) -> dict[str, Valor] | None:
    """Rasgos que no dependen del vivo: horario, calendario e histórico."""
    if stop_id not in viaje.paradas:
        return None
    seq, minuto = viaje.paradas[stop_id]
    festivo, fin, daytype = tipo_de_dia(fecha_servicio, festivos)
    hora = _hora(minuto)
    valores: dict[str, Valor] = {
        "familia_c": viaje.familia,
        "parada_id_c": stop_id,
        "origen_id_c": viaje.origen_id,
        "destino_id_c": viaje.destino_id,
        "tren_n": float(viaje.tren) if viaje.tren.isdigit() else None,
        "dir": float(viaje.paridad) if viaje.paridad >= 0 else None,
        "hora": float(hora),
        "mod": float(hora * 60 + int(minuto % 60)),
        "dow": float(fecha_servicio.weekday()),
        "hol_f": float(festivo),
        "fin_f": float(fin),
        "daytype": float(daytype),
        "seq": float(seq),
    }
    valores.update(estadisticas.historicos(viaje.tren, viaje.familia, stop_id, hora, fin))
    return valores


# -------------------------------------------------------------- predictor


@dataclass
class Predictor:
    """Retraso predicho (minutos, redondeado) de un viaje en una parada, o ``None``.

    Se construye una vez por tick con el horario del día y las observaciones
    recientes; ``departures`` lo llama sólo para filas sin ningún dato en vivo.
    """

    artefacto: Artefacto
    viajes: Mapping[str, Viaje]
    enlaces: Mapping[str, Enlace]
    observaciones: Sequence[Observacion]
    fecha_servicio: date
    ahora_min: float
    rutas: frozenset[str]
    #: Tope adicional de antelación (min) además del de cada línea en el artefacto.
    horizonte_max_min: float | None = None
    _cache: dict[tuple[str, str], int | None] = field(default_factory=dict, repr=False)

    def linea_para(self, trip_id: str) -> LineaArtefacto | None:
        familia = familia_de(trip_id)
        if familia not in self.rutas:
            return None
        return self.artefacto.lineas.get(familia)

    def rasgos(self, trip_id: str, stop_id: str) -> dict[str, Valor] | None:
        """Rasgos del modelo de la línea, o ``None`` si falta alguna entrada en vivo."""
        linea = self.linea_para(trip_id)
        viaje = self.viajes.get(trip_id)
        if linea is None or viaje is None:
            return None
        nombre = linea.modelo
        t0 = viaje.salida_min
        antelacion = t0 - self.ahora_min
        if antelacion > linea.horizonte_max_min or (
            self.horizonte_max_min is not None and antelacion > self.horizonte_max_min
        ):
            return None
        valores = rasgos_base(
            viaje, stop_id, self.artefacto.estadisticas, self.fecha_servicio, self.artefacto.festivos
        )
        if valores is None:
            return None
        asof = min(self.ahora_min, t0)
        if nombre == MODELO_ESTADO_LINEA:
            fin = bool(valores["fin_f"])
            ls_mean, ls_res, ls_n = estado_linea(
                self.observaciones, self.viajes, self.artefacto.estadisticas,
                familia=viaje.familia, excluir_trip=trip_id, asof=asof, fin=fin,
            )
            if ls_n == 0:
                return None
            valores.update({"ls_mean": ls_mean, "ls_res": ls_res, "ls_n": ls_n})
        else:
            inc = rasgos_entrante(
                self.enlaces.get(trip_id), self.observaciones, self.viajes, t0=t0, asof=asof
            )
            if inc["inc_live_delay"] is None:
                return None
            valores.update(inc)
        return valores

    def __call__(self, stop: ScheduledStop) -> int | None:
        if stop.stop_id is None:
            return None
        clave = (stop.trip_id, stop.stop_id)
        if clave not in self._cache:
            self._cache[clave] = self._predecir(stop.trip_id, stop.stop_id)
        return self._cache[clave]

    def _predecir(self, trip_id: str, stop_id: str) -> int | None:
        valores = self.rasgos(trip_id, stop_id)
        linea = self.linea_para(trip_id)
        if valores is None or linea is None:
            return None
        return round(self.artefacto.modelos[linea.modelo].predecir(valores))
