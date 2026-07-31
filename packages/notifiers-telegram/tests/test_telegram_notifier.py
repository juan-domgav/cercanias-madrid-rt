from __future__ import annotations

import httpx
import pytest

from cercanias_notifiers_telegram.telegram_notifier import (
    TelegramConfigError,
    TelegramNotifier,
    TelegramOptions,
    TelegramSendError,
)

TOKEN = "123456:FAKE-TOKEN-PARA-TESTS"


def _notifier(handler, *, sleep=None, monotonic=None):  # type: ignore[no-untyped-def]
    """Notificador con transporte simulado y sin esperas reales."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    options = TelegramOptions(
        sleep=sleep or (lambda _s: None),
        monotonic=monotonic or (lambda: 0.0),
    )
    return TelegramNotifier(token=TOKEN, chat_id="@canal_pruebas", client=client, options=options)


def test_dry_run_no_necesita_token_ni_envia() -> None:
    """Permite ejercitar el camino completo en CI sin secretos ni red."""
    notifier = TelegramNotifier(token=None, chat_id=None, options=TelegramOptions(dry_run=True))
    outcome = notifier.send("hola")
    assert outcome.ok
    assert outcome.dry_run
    assert outcome.attempts == 0


def test_sin_token_falla_pronto_y_explica_como_obtenerlo() -> None:
    with pytest.raises(TelegramConfigError) as exc:
        TelegramNotifier(token=None, chat_id="@x")
    assert "BotFather" in str(exc.value)
    assert "publico" in str(exc.value), "debe advertir de que el repo es publico"


def test_envio_correcto() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(200, json={"ok": True})

    outcome = _notifier(handler).send("Incidencia en C4")
    assert outcome.ok
    assert outcome.attempts == 1


def test_429_respeta_retry_after_del_cuerpo() -> None:
    """C-8: el retry_after de Telegram manda sobre cualquier backoff propio."""
    esperas: list[float] = []
    intentos = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        intentos["n"] += 1
        if intentos["n"] == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
        return httpx.Response(200, json={"ok": True})

    notifier = _notifier(handler, sleep=esperas.append)
    outcome = notifier.send("x")
    assert outcome.ok
    assert esperas == [7.0], f"debia esperar exactamente los 7 s indicados, espero {esperas}"


def test_429_sin_retry_after_usa_backoff_exponencial() -> None:
    esperas: list[float] = []
    intentos = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        intentos["n"] += 1
        if intentos["n"] <= 2:
            return httpx.Response(429, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    _notifier(handler, sleep=esperas.append).send("x")
    assert esperas == [2.0, 4.0], f"backoff exponencial esperado, obtenido {esperas}"


def test_error_4xx_no_se_reintenta() -> None:
    """Un chat_id invalido no mejora reintentando; gastar reintentos es ruido."""
    intentos = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        intentos["n"] += 1
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    with pytest.raises(TelegramSendError):
        _notifier(handler).send("x")
    assert intentos["n"] == 1, "un 400 no debe reintentarse"


def test_el_token_nunca_aparece_en_el_error() -> None:
    """El repositorio es publico: un token en un traceback es un token filtrado."""

    def handler(request: httpx.Request) -> httpx.Response:
        # El servidor devuelve la URL completa, que contiene el token.
        return httpx.Response(500, text=f"error en https://api.telegram.org/bot{TOKEN}/sendMessage")

    with pytest.raises(TelegramSendError) as exc:
        _notifier(handler).send("x")
    assert TOKEN not in str(exc.value), "TOKEN FILTRADO EN EL MENSAJE DE ERROR"
    assert "REDACTED" in str(exc.value)


def test_throttling_por_chat_limita_el_ritmo() -> None:
    """C-8: ~20 mensajes por minuto al mismo destino."""
    reloj = {"t": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    def sleep(seconds: float) -> None:
        reloj["t"] += seconds

    notifier = _notifier(handler, sleep=sleep, monotonic=lambda: reloj["t"])
    for _ in range(20):
        notifier.send("x")
    assert reloj["t"] == 0.0, "los primeros 20 no deben esperar"

    notifier.send("el que pasa del limite")
    assert reloj["t"] > 0.0, "el mensaje 21 al mismo chat debe esperar"
