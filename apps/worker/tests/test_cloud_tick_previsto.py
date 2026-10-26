"""Contrato de la ingesta con previsión por histórico (RB-013).

- Lista blanca ``PREVISTO_ROUTES`` vacía por defecto: sin configurar, NADA cambia.
- Artefacto ausente, roto o caducado: ERROR estructurado, sin previsión, y el
  resto del tablón exactamente igual que sin la función.
- Solo con snapshot fresco, solo en trenes sin dato en vivo, y el dato en vivo manda.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest

from cercanias_domain.prevision import Observation, build_table
from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick

SERV = "1019L"
ATOCHA = "18000"
# Lunes 19/10/2026, 08:00 en Madrid: laborable.
AHORA = datetime(2026, 10, 19, 8, 0, 30, tzinfo=cloud_tick.MADRID_TZ)
C1_SIN_VIVO = f"{SERV}19799C1"    # 08:20, no está en el feed
C5_SIN_VIVO = f"{SERV}35001C5"    # 08:25, no está en el feed
C5_EN_VIVO = f"{SERV}35003C5"     # 08:10, en vivo con +0
C3_SIN_VIVO = f"{SERV}23001C3"    # 08:30, línea no habilitada


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:  # type: ignore[override]
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


def _parada(trip: str, hora: str, route: str) -> dict[str, Any]:
    return {"trip": trip, "t": hora, "seq": 5, "dest": "Destino", "route": route, "terminates": False}


def _horario() -> dict[str, Any]:
    return {"service": SERV, "stations": {ATOCHA: [
        _parada(C5_EN_VIVO, "08:10:00", "C5"),
        _parada(C1_SIN_VIVO, "08:20:00", "C1"),
        _parada(C5_SIN_VIVO, "08:25:00", "C5"),
        _parada(C3_SIN_VIVO, "08:30:00", "C3"),
    ]}}


def _snapshot(edad_s: float = 30) -> dict[str, Any]:
    return {
        "generated_at": (AHORA - timedelta(seconds=edad_s)).isoformat(),
        "vehicles": [{"trip_id": C5_EN_VIVO, "delay_seconds": 0, "next_stop_id": ATOCHA,
                      "status": "IN_TRANSIT_TO", "platform": "6"}],
    }


def _artefacto(generado: datetime = AHORA, **cambios: Any) -> dict[str, Any]:
    dias = [date(2026, 9, 14) + timedelta(days=i) for i in range(28)]
    lab = [d for d in dias if d.weekday() < 5]
    obs: list[Observation] = []
    for d in lab:
        obs.append(Observation("C1", "19799", ATOCHA, d, 180))   # +3
        obs.append(Observation("C5", "35001", ATOCHA, d, 0))     # sin retraso
        obs.append(Observation("C5", "35003", ATOCHA, d, 600))   # +10 (pero va en vivo)
        obs.append(Observation("C3", "23001", ATOCHA, d, 300))   # línea no habilitada
    datos = build_table(obs, lines=frozenset({"C1", "C3", "C5"}), holidays=frozenset({date(2026, 10, 12)}),
                        generated_at=generado.astimezone(UTC))
    datos.update(cambios)
    return datos


def _escribir(base: Path, clave: str, contenido: bytes) -> None:
    destino = base / clave
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(contenido)


@pytest.fixture
def entorno(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    web, privado = tmp_path / "web", tmp_path / "privado"
    _escribir(web, f"horarios/{SERV}.json", json.dumps(_horario()).encode())
    privado.mkdir()
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    monkeypatch.setattr(cloud_tick, "PREVISTO_ROUTES", frozenset({"C1", "C5"}))
    return web, privado


def _boards(web: Path, privado: Path, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    prevision = cloud_tick._load_prevision(LocalSnapshotPublisher(privado), AHORA)
    return cloud_tick._build_boards(snapshot or _snapshot(), LocalSnapshotPublisher(web), prevision=prevision)


def _filas(boards: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["train"]: d for d in boards["stations"][ATOCHA]["departures"]}


def _errores(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    salida = []
    for linea in capsys.readouterr().err.splitlines():
        try:
            salida.append(json.loads(linea))
        except json.JSONDecodeError:
            continue
    return [e for e in salida if e.get("severity") == "ERROR"]


def _con_artefacto(privado: Path, datos: dict[str, Any] | None = None) -> None:
    _escribir(privado, cloud_tick.PREVISTO_KEY, json.dumps(datos or _artefacto()).encode())


def test_previsto_solo_en_lineas_habilitadas_y_sin_dato_en_vivo(entorno: tuple[Path, Path]) -> None:
    web, privado = entorno
    _con_artefacto(privado)
    filas = _filas(_boards(web, privado))

    c1 = filas["19799"]
    assert c1["predicted_delay"] == 3 and c1["predicted_samples"] == 20
    assert c1["predicted_day_type"] == "laborable"
    assert c1["delay_source"] == "predicted"
    assert c1["display"]["label"] == "previsto +3 min"
    assert c1["display"]["tone"] == "predicted"
    # La hora y el estado del feed no cambian.
    assert c1["eta"] == "08:20" and c1["delay"] is None and c1["live"] is False
    assert c1["on_time"] is False

    assert filas["35001"]["display"]["label"] == "previsto sin retraso"
    assert "en hora" not in filas["35001"]["display"]["label"]
    # En vivo: manda Renfe, aunque su histórico diga +10.
    vivo = filas["35003"]
    assert vivo["predicted_delay"] is None and vivo["display"]["label"] == "en hora"
    # C3 no está en la lista blanca aunque el artefacto la traiga.
    assert filas["23001"]["predicted_delay"] is None
    assert filas["23001"]["display"]["label"] == "sin datos en vivo"


def test_salud_de_la_prevision(entorno: tuple[Path, Path]) -> None:
    web, privado = entorno
    _con_artefacto(privado)
    salud = _boards(web, privado)["health"]
    assert salud["departures_predicted"] == 2
    assert salud["departures_contradicting_live"] == 0
    assert salud["prediction"] == {
        "status": "ok", "routes": ["C1", "C5"], "applied": True, "day_type": "laborable",
        "artifact_generated_at": AHORA.astimezone(UTC).isoformat(),
        "history_to": "2026-10-09",
    }
    # Las cuentas del feed no se tocan: siguen diciendo lo que dice Renfe.
    assert salud["departures_missing_in_window"] == 3


def test_por_defecto_desactivado_y_sin_leer_nada(
    entorno: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    web, privado = entorno
    _con_artefacto(privado)
    monkeypatch.setattr(cloud_tick, "PREVISTO_ROUTES", frozenset())
    boards = _boards(web, privado)
    assert all(d["predicted_delay"] is None for d in _filas(boards).values())
    assert boards["health"]["prediction"] == {"status": "disabled", "routes": []}
    assert boards["health"]["departures_predicted"] == 0


def test_sin_argumento_es_lo_mismo_que_desactivado(entorno: tuple[Path, Path]) -> None:
    web, _ = entorno
    boards = cloud_tick._build_boards(_snapshot(), LocalSnapshotPublisher(web))
    assert boards["health"]["prediction"] == {"status": "disabled", "routes": []}


def _sin_prevision(boards: dict[str, Any]) -> dict[str, Any]:
    """El tablón sin los campos de previsión, para comparar 'nada más cambia'."""
    copia = json.loads(json.dumps(boards))
    copia["health"].pop("prediction")
    return copia


@pytest.mark.parametrize(
    ("contenido", "alerta"),
    [
        (None, "prediction_artifact_missing"),
        (b"{no es json", "prediction_artifact_invalid"),
        (json.dumps({"schema": "otra/9"}).encode(), "prediction_artifact_invalid"),
        (json.dumps(_artefacto(AHORA - timedelta(days=30))).encode(), "prediction_artifact_stale"),
    ],
)
def test_artefacto_ausente_roto_o_caducado_falla_seguro(
    entorno: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    contenido: bytes | None, alerta: str,
) -> None:
    web, privado = entorno
    if contenido is not None:
        _escribir(privado, cloud_tick.PREVISTO_KEY, contenido)
    boards = _boards(web, privado)
    (error,) = [e for e in _errores(capsys) if e.get("alert", "").startswith("prediction_")]
    assert error["alert"] == alerta
    assert error["key"] == cloud_tick.PREVISTO_KEY
    assert boards["health"]["prediction"]["status"] == "error"
    assert boards["health"]["prediction"]["alert"] == alerta
    assert boards["health"]["departures_predicted"] == 0

    # Nada más cambia: idéntico al tablón con la función desactivada.
    monkeypatch.setattr(cloud_tick, "PREVISTO_ROUTES", frozenset())
    desactivado = _boards(web, privado)
    assert _sin_prevision(boards) == _sin_prevision(desactivado)


def test_error_leyendo_el_bucket_no_tumba_el_tablon(
    entorno: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    web, _ = entorno

    class _Roto:
        def read(self, key: str) -> bytes | None:
            raise OSError("GCS caído")

    prevision = cloud_tick._load_prevision(_Roto(), AHORA)
    boards = cloud_tick._build_boards(_snapshot(), LocalSnapshotPublisher(web), prevision=prevision)
    assert boards["health"]["prediction"]["alert"] == "prediction_artifact_unreadable"
    assert [e["alert"] for e in _errores(capsys)] == ["prediction_artifact_unreadable"]
    assert _filas(boards)["19799"]["display"]["label"] == "sin datos en vivo"


def test_linea_habilitada_que_el_artefacto_no_trae_avisa(
    entorno: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    web, privado = entorno
    _con_artefacto(privado)
    monkeypatch.setattr(cloud_tick, "PREVISTO_ROUTES", frozenset({"C1", "C10"}))
    boards = _boards(web, privado)
    assert boards["health"]["prediction"]["routes"] == ["C1"]
    assert boards["health"]["prediction"]["routes_missing_in_artifact"] == ["C10"]
    assert _filas(boards)["35001"]["predicted_delay"] is None  # C5 ya no está permitida
    capsys.readouterr()


def test_con_snapshot_viejo_no_se_preve(entorno: tuple[Path, Path]) -> None:
    web, privado = entorno
    _con_artefacto(privado)
    boards = _boards(web, privado, _snapshot(edad_s=cloud_tick.STALE_WARNING_SECONDS + 60))
    assert all(d["predicted_delay"] is None for d in _filas(boards).values())
    assert boards["health"]["prediction"]["applied"] is False


def test_festivo_usa_la_mediana_de_festivos(
    entorno: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El artefacto solo trae laborables: en festivo, sin previsión (sin muestras).
    web, privado = entorno
    _con_artefacto(privado)
    monkeypatch.setattr(cloud_tick, "PREVISTO_EXTRA_HOLIDAYS", frozenset({date(2026, 10, 19)}))
    boards = _boards(web, privado)
    assert boards["health"]["prediction"]["day_type"] == "festivo"
    assert boards["health"]["departures_predicted"] == 0


def test_publish_boards_lee_el_artefacto_del_bucket_privado(
    entorno: tuple[Path, Path],
) -> None:
    web, privado = entorno
    _con_artefacto(privado)
    cloud_tick._publish_boards(LocalSnapshotPublisher(web), _snapshot(), frozenset(), {},
                               estado=LocalSnapshotPublisher(privado))
    publicado = json.loads((web / cloud_tick.BOARDS_KEY).read_text())
    assert publicado["health"]["departures_predicted"] == 2


def test_festivos_extra_desde_el_entorno() -> None:
    assert cloud_tick._parse_holidays("2026-11-09, 2026-05-15,") == frozenset(
        {date(2026, 11, 9), date(2026, 5, 15)})
    assert cloud_tick._parse_holidays("") == frozenset()
    assert cloud_tick._parse_holidays("no-fecha,2026-11-09") == frozenset({date(2026, 11, 9)})
