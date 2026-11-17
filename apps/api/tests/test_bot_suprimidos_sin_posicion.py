"""RB-019: el bot público dice SUPRIMIDO para un viaje cancelado sin posición.

El bot no recalcula: pinta ``boards.json``. Con datos reales de la huelga del
17/11/2026 08:54:51 en Colmenar Viejo, el 20432 y el 20436 (CANCELED en
trip_updates, sin posición) salían «sin datos en vivo» y «sin confirmar».
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest
from cercanias_api.bot import BotContext, handle_update

from cercanias_domain.station_view import Station
from cercanias_worker import cloud_tick

AHORA = datetime(2026, 11, 17, 8, 54, 51, tzinfo=cloud_tick.MADRID_TZ)
COLMENAR = Station("17005", "Colmenar Viejo", 40.6452, -3.7766)
_FIXTURE_MOD = (
    Path(__file__).resolve().parents[2] / "worker" / "tests" / "test_cloud_tick_suprimidos_sin_posicion.py"
)


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:  # type: ignore[override]
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


@pytest.fixture
def texto(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    spec = importlib.util.spec_from_file_location("_fixture_huelga", _FIXTURE_MOD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    boards: dict[str, Any] = mod.construir_tablones(tmp_path / "web")
    r = handle_update({"message": {"chat": {"id": 1}, "text": "/proximos Colmenar Viejo"}},
                      BotContext(stations=[COLMENAR], boards=boards))
    assert r.reply is not None
    return r.reply.text


def _bloque(texto: str, tren: str) -> str:
    return next(linea for linea in texto.split("\n") if f"tren {tren} " in linea)


@pytest.mark.parametrize("tren", ["20432", "20436"])
def test_bot_dice_suprimido(texto: str, tren: str) -> None:
    linea = _bloque(texto, tren)
    assert "SUPRIMIDO" in linea
    assert "sin datos en vivo" not in linea


def test_bot_no_dice_sin_confirmar_de_un_suprimido(texto: str) -> None:
    assert "sin confirmar" not in texto


def test_bot_ofrece_el_proximo_a_parla_que_si_sale(texto: str) -> None:
    """El 20440 no está suprimido (su entrante, el 20439, sí: eso es un aviso
    aparte, ya existente). Antes ni aparecía: el 20432 «cubría» Parla."""
    linea = _bloque(texto, "20440")
    assert not linea.endswith("SUPRIMIDO")
    assert "Renfe aún no lo publica" in linea
