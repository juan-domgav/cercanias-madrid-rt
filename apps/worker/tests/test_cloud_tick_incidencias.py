"""RB-014: supresiones, paradas omitidas y avisos en el archivo privado.

Todo va detras de banderas que por defecto estan APAGADAS
(``ARCHIVAR_CANCELACIONES=0``, ``ARCHIVAR_AVISOS=0``): con ellas apagadas el
archivo y los tablones salen identicos a antes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cercanias_connectors_renfe.alerts import AlertsFetchError
from cercanias_notifiers_telegram.telegram_notifier import TelegramNotifier, TelegramOptions
from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository
from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick, mvp
from cercanias_worker.config import WorkerConfig

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
MOMENTO = datetime(2026, 10, 27, 9, 30, 5, tzinfo=cloud_tick.MADRID_TZ)
SNAPSHOT = {"vehicles": [{"trip_id": "1093J20316C4a", "delay_seconds": 60, "next_stop_id": "17000"}]}


def _leer(base: Path, prefijo: str) -> list[tuple[str, dict[str, Any]]]:
    raiz = base / prefijo
    if not raiz.exists():
        return []
    return [
        (str(p.relative_to(base)), json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(raiz.rglob("*.json"))
    ]


def _logs(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    entradas = []
    for linea in capsys.readouterr().err.splitlines():
        try:
            entradas.append(json.loads(linea))
        except json.JSONDecodeError:
            continue
    return entradas


def _congelado() -> dict[str, Any]:
    return json.loads((FIXTURES / "alerts_congelado_2026-10-07.json").read_text(encoding="utf-8"))


# --- Banderas -----------------------------------------------------------------


def test_banderas_apagadas_por_defecto() -> None:
    assert cloud_tick._flag("NO_EXISTE_ESTA_VARIABLE") is False


@pytest.mark.parametrize(("valor", "esperado"), [("1", True), ("0", False), ("", False), ("si", False)])
def test_flag_solo_acepta_1(monkeypatch: pytest.MonkeyPatch, valor: str, esperado: bool) -> None:
    monkeypatch.setenv("BANDERA_DE_PRUEBA", valor)
    assert cloud_tick._flag("BANDERA_DE_PRUEBA") is esperado


# --- Supresiones y paradas omitidas en archivo/<dia>/<hhmmss>.json -------------


def test_archivo_sin_bandera_no_cambia(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_CANCELACIONES", False)
    pub = LocalSnapshotPublisher(tmp_path)
    cloud_tick._archive_pass(
        pub, SNAPSHOT, MOMENTO, cancelled=frozenset({"1093J20000C3"}), skipped={"x": frozenset({"1"})}
    )
    ((clave, objeto),) = _leer(tmp_path, "archivo")
    assert clave == "archivo/2026-10-27/093005.json"
    assert set(objeto) == {"ts", "n", "cols", "trains"}


def test_archivo_con_bandera_guarda_canc_y_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_CANCELACIONES", True)
    pub = LocalSnapshotPublisher(tmp_path)
    cloud_tick._archive_pass(
        pub, SNAPSHOT, MOMENTO,
        cancelled=frozenset({"1093J20002C3", "1093J20001C3"}),
        skipped={"1093J20316C4a": frozenset({"17005", "17004"})},
    )
    ((_, objeto),) = _leer(tmp_path, "archivo")
    assert objeto["canc"] == ["1093J20001C3", "1093J20002C3"]
    assert objeto["skip"] == {"1093J20316C4a": ["17004", "17005"]}
    assert objeto["cols"] == ["trip", "delay", "next_stop", "status", "platform", "lat", "lon"]


def test_archivo_con_bandera_y_sin_incidencias_lleva_listas_vacias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clave presente y vacia = 'se miro y no habia'; clave ausente = 'no se sabe'."""
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_CANCELACIONES", True)
    pub = LocalSnapshotPublisher(tmp_path)
    cloud_tick._archive_pass(pub, SNAPSHOT, MOMENTO, cancelled=frozenset(), skipped={})
    ((_, objeto),) = _leer(tmp_path, "archivo")
    assert objeto["canc"] == []
    assert objeto["skip"] == {}


def test_archivo_sin_trip_updates_omite_las_claves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Si trip_updates fallo, NO se escribe 'canc: []': seria afirmar que no hubo."""
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_CANCELACIONES", True)
    pub = LocalSnapshotPublisher(tmp_path)
    cloud_tick._archive_pass(pub, SNAPSHOT, MOMENTO, cancelled=None, skipped=None)
    ((_, objeto),) = _leer(tmp_path, "archivo")
    assert "canc" not in objeto
    assert "skip" not in objeto


def test_run_cycle_marca_si_trip_updates_fallo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vp = json.loads((FIXTURES / "vehicle_positions_madrid.json").read_text())
    monkeypatch.setattr(mvp, "fetch_vehicle_positions_payload", lambda _url: vp)

    def _falla() -> dict[str, Any]:
        raise mvp.TripUpdatesFetchError("caido")

    monkeypatch.setattr(mvp, "fetch_trip_updates_payload", _falla)
    store = mvp.SnapshotStore(tmp_path / "s.json")
    caido = mvp.run_cycle(WorkerConfig(), store, InMemoryCurrentStateRepository())
    assert caido.trip_updates_ok is False

    tu = {
        "entity": [
            {"id": "1", "tripUpdate": {"trip": {
                "tripId": "1080J20094C3", "scheduleRelationship": "CANCELED"}}},
            {"id": "2", "tripUpdate": {"trip": {"tripId": "1080J20316C4a"}, "stopTimeUpdate": [
                {"stopId": "17004", "scheduleRelationship": "SKIPPED"}]}},
        ]
    }
    monkeypatch.setattr(mvp, "fetch_trip_updates_payload", lambda: tu)
    ok = mvp.run_cycle(WorkerConfig(), store, InMemoryCurrentStateRepository())
    assert ok.trip_updates_ok is True
    assert ok.cancelled_trips == frozenset({"1080J20094C3"})
    assert ok.skipped_stops == {"1080J20316C4a": frozenset({"17004"})}


# --- Avisos: archivo/alerts/<dia>/<hhmmss>.json solo si cambian ----------------


@pytest.fixture
def avisos_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_AVISOS", True)


def test_avisos_apagados_no_descarga_ni_escribe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_AVISOS", False)

    def _no_llamar() -> dict[str, Any]:
        raise AssertionError("no debe descargar con la bandera apagada")

    salud = cloud_tick._archive_alerts(LocalSnapshotPublisher(tmp_path), MOMENTO, fetch=_no_llamar)
    assert salud == {}
    assert not any(tmp_path.rglob("*"))


@pytest.mark.usefixtures("avisos_on")
def test_primer_aviso_se_escribe_con_estado(tmp_path: Path) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    salud = cloud_tick._archive_alerts(pub, MOMENTO, fetch=_congelado)
    ((clave, objeto),) = _leer(tmp_path, "archivo/alerts")
    assert clave == "archivo/alerts/2026-10-27/093005.json"
    assert objeto["n"] == 9
    assert objeto["feed_ts"] == "2026-10-07T09:32:52+02:00"
    assert {a["id"] for a in objeto["alerts"]} >= {"AVISO_518335", "INFO_518353"}
    estado = json.loads((tmp_path / cloud_tick.ALERTS_STATE_KEY).read_text())
    assert estado["hash"] == objeto["hash"]
    assert estado["key"] == clave
    assert salud["alerts_madrid"] == 9
    assert salud["alerts_fetch_failed"] is False


@pytest.mark.usefixtures("avisos_on")
def test_hash_sin_cambios_no_escribe(tmp_path: Path) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    cloud_tick._archive_alerts(pub, MOMENTO, fetch=_congelado)
    for minutos in (1, 2, 30):
        cloud_tick._archive_alerts(pub, MOMENTO + timedelta(minutes=minutos), fetch=_congelado)
    assert len(_leer(tmp_path, "archivo/alerts")) == 1


@pytest.mark.usefixtures("avisos_on")
def test_hash_distinto_escribe_otro(tmp_path: Path) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    cloud_tick._archive_alerts(pub, MOMENTO, fetch=_congelado)
    nuevo = _congelado()
    nuevo["entity"] = nuevo["entity"][1:]
    cloud_tick._archive_alerts(pub, MOMENTO + timedelta(minutes=1), fetch=lambda: nuevo)
    claves = [c for c, _ in _leer(tmp_path, "archivo/alerts")]
    assert claves == ["archivo/alerts/2026-10-27/093005.json", "archivo/alerts/2026-10-27/093105.json"]


@pytest.mark.usefixtures("avisos_on")
def test_estado_corrupto_vuelve_a_escribir_sin_romper(tmp_path: Path) -> None:
    (tmp_path / cloud_tick.ALERTS_STATE_KEY).write_text("{no json")
    pub = LocalSnapshotPublisher(tmp_path)
    cloud_tick._archive_alerts(pub, MOMENTO, fetch=_congelado)
    assert len(_leer(tmp_path, "archivo/alerts")) == 1


@pytest.mark.usefixtures("avisos_on")
@pytest.mark.parametrize(
    "fallo",
    [AlertsFetchError("alerts.json no devolvio JSON valido"), ValueError("bug inesperado")],
)
def test_fallo_de_descarga_no_lanza_y_se_registra(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fallo: Exception
) -> None:
    def _falla() -> dict[str, Any]:
        raise fallo

    salud = cloud_tick._archive_alerts(LocalSnapshotPublisher(tmp_path), MOMENTO, fetch=_falla)
    assert salud["alerts_fetch_failed"] is True
    assert salud["alerts_feed_stale"] is None
    (entrada,) = [e for e in _logs(capsys) if e.get("alert") == "alerts_fetch_failed"]
    assert entrada["severity"] in {"WARNING", "ERROR"}
    assert entrada["component"] == "cloud-tick"
    assert not _leer(tmp_path, "archivo/alerts")


@pytest.mark.usefixtures("avisos_on")
def test_json_malformado_no_lanza(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    salud = cloud_tick._archive_alerts(
        LocalSnapshotPublisher(tmp_path), MOMENTO, fetch=lambda: {"entity": "no-lista"}
    )
    assert salud["alerts_fetch_failed"] is True
    assert any(e.get("alert") == "alerts_fetch_failed" for e in _logs(capsys))


class _PublicadorRoto(LocalSnapshotPublisher):
    def publish(self, key: str, payload: bytes, **kwargs: Any) -> str:
        if key.startswith("archivo/alerts/"):
            raise OSError("GCS caido")
        return super().publish(key, payload, **kwargs)


@pytest.mark.usefixtures("avisos_on")
def test_fallo_al_escribir_no_guarda_hash_y_reintenta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roto = _PublicadorRoto(tmp_path)
    cloud_tick._archive_alerts(roto, MOMENTO, fetch=_congelado)
    assert any(e.get("alert") == "alerts_archive_failed" for e in _logs(capsys))
    # Puede haberse guardado stale_warned_at, pero nunca el hash de algo no escrito.
    estado_path = tmp_path / cloud_tick.ALERTS_STATE_KEY
    estado = json.loads(estado_path.read_text()) if estado_path.exists() else {}
    assert "hash" not in estado
    cloud_tick._archive_alerts(LocalSnapshotPublisher(tmp_path), MOMENTO, fetch=_congelado)
    assert len(_leer(tmp_path, "archivo/alerts")) == 1


@pytest.mark.usefixtures("avisos_on")
def test_feed_congelado_avisa_una_vez_por_hora(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    salud = cloud_tick._archive_alerts(pub, MOMENTO, fetch=_congelado)
    assert salud["alerts_feed_stale"] is True
    assert salud["alerts_feed_age_seconds"] == int(
        (MOMENTO - datetime(2026, 10, 7, 7, 32, 52, tzinfo=UTC)).total_seconds()
    )
    (aviso,) = [e for e in _logs(capsys) if e.get("alert") == "alerts_feed_stale"]
    assert aviso["severity"] == "WARNING"
    assert aviso["message"] == "alerts_feed_stale"
    assert aviso["feed_timestamp"] == "2026-10-07T09:32:52+02:00"

    for minutos in (1, 30, 59):
        cloud_tick._archive_alerts(pub, MOMENTO + timedelta(minutes=minutos), fetch=_congelado)
    assert not [e for e in _logs(capsys) if e.get("alert") == "alerts_feed_stale"]

    cloud_tick._archive_alerts(pub, MOMENTO + timedelta(minutes=61), fetch=_congelado)
    assert len([e for e in _logs(capsys) if e.get("alert") == "alerts_feed_stale"]) == 1


@pytest.mark.usefixtures("avisos_on")
def test_feed_fresco_no_avisa(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fresco = _congelado()
    fresco["header"]["timestamp"] = str(int((MOMENTO - timedelta(minutes=5)).timestamp()))
    salud = cloud_tick._archive_alerts(LocalSnapshotPublisher(tmp_path), MOMENTO, fetch=lambda: fresco)
    assert salud["alerts_feed_stale"] is False
    assert salud["alerts_feed_age_seconds"] == 300
    assert not [e for e in _logs(capsys) if e.get("alert") == "alerts_feed_stale"]


@pytest.mark.usefixtures("avisos_on")
def test_sin_cabecera_la_frescura_es_desconocida(tmp_path: Path) -> None:
    sin = _congelado()
    del sin["header"]
    salud = cloud_tick._archive_alerts(LocalSnapshotPublisher(tmp_path), MOMENTO, fetch=lambda: sin)
    assert salud["alerts_feed_stale"] is None
    assert salud["alerts_feed_age_seconds"] is None


# --- Salud en boards.json y extremo a extremo -----------------------------------


def test_publish_boards_anade_la_salud_de_avisos(tmp_path: Path) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    cloud_tick._publish_boards(
        pub, SNAPSHOT, frozenset(), {}, extra_health={"alerts_feed_stale": True}
    )
    boards = json.loads((tmp_path / cloud_tick.BOARDS_KEY).read_text())
    assert boards["health"]["alerts_feed_stale"] is True
    assert "schedule_missing" in boards["health"]


def _main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alerts: Any
) -> tuple[int, Path]:
    web = tmp_path / "web"
    vp = json.loads((FIXTURES / "vehicle_positions_madrid.json").read_text())
    tu = {"entity": [
        {"id": "1", "tripUpdate": {"trip": {"tripId": "1080J29999C3", "scheduleRelationship": "CANCELED"}}},
    ]}
    monkeypatch.setattr(mvp, "fetch_vehicle_positions_payload", lambda _url: vp)
    monkeypatch.setattr(mvp, "fetch_trip_updates_payload", lambda: tu)
    monkeypatch.setattr(cloud_tick, "publisher_from_env", lambda: LocalSnapshotPublisher(web))
    monkeypatch.setattr(cloud_tick, "PRIVATE_BUCKET", "")
    monkeypatch.setattr(cloud_tick, "PASSES_PER_INVOCATION", 1)
    monkeypatch.setattr(cloud_tick, "fetch_alerts_payload", alerts)
    monkeypatch.setattr(
        cloud_tick, "_build_notifier",
        lambda: TelegramNotifier(token=None, chat_id=None, options=TelegramOptions(dry_run=True)),
    )
    return cloud_tick.main(), web


def test_main_con_banderas_y_avisos_congelados_no_degrada(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_CANCELACIONES", True)
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_AVISOS", True)
    codigo, web = _main(monkeypatch, tmp_path, _congelado)
    assert codigo == 0
    snapshot = json.loads((web / cloud_tick.SNAPSHOT_KEY).read_text())
    assert "degraded" not in snapshot
    boards = json.loads((web / cloud_tick.BOARDS_KEY).read_text())
    assert boards["health"]["alerts_feed_stale"] is True
    pasadas = [o for c, o in _leer(web, "archivo") if not c.startswith("archivo/alerts/")]
    assert pasadas[0]["canc"] == ["1080J29999C3"]
    assert len(_leer(web, "archivo/alerts")) == 1


def test_main_con_avisos_caidos_sigue_publicando(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_AVISOS", True)

    def _caido() -> dict[str, Any]:
        raise AlertsFetchError("timeout")

    codigo, web = _main(monkeypatch, tmp_path, _caido)
    assert codigo == 0
    assert (web / cloud_tick.SNAPSHOT_KEY).exists()
    boards = json.loads((web / cloud_tick.BOARDS_KEY).read_text())
    assert boards["health"]["alerts_fetch_failed"] is True


def test_main_con_banderas_apagadas_no_toca_nada_nuevo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_CANCELACIONES", False)
    monkeypatch.setattr(cloud_tick, "ARCHIVAR_AVISOS", False)

    def _no_llamar() -> dict[str, Any]:
        raise AssertionError("no debe descargar avisos")

    codigo, web = _main(monkeypatch, tmp_path, _no_llamar)
    assert codigo == 0
    boards = json.loads((web / cloud_tick.BOARDS_KEY).read_text())
    assert not any(k.startswith("alerts_") for k in boards["health"])
    pasadas = _leer(web, "archivo")
    assert pasadas
    assert all(set(o) == {"ts", "n", "cols", "trains"} for _, o in pasadas)
    assert not (web / cloud_tick.ALERTS_STATE_KEY).exists()
