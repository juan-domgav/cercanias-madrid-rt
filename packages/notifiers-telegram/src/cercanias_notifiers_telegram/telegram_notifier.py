"""Adaptador de Telegram (correccion C-8).

C-8 fija las reglas y aqui se implementan:

- Envio **siempre** con throttling configurable. Cifras de partida del orden de
  30 mensajes/segundo globales y ~20 por minuto al mismo grupo.
- Reintento con backoff exponencial ante ``429``, **respetando ``retry_after``**
  cuando Telegram lo indica: es la unica cifra fiable, y un backoff propio que
  la ignore empeora el problema en vez de arreglarlo.
- Preferir publicacion en **canal** (un envio) frente a difusion individual
  siempre que el contenido no sea personalizado.

Sobre el secreto: el token NUNCA se escribe en el repositorio, que ademas es
publico. Se lee de ``TELEGRAM_BOT_TOKEN`` y no se registra en ningun log ni
mensaje de error (ver :func:`_redact`).

``dry_run`` permite ejercitar todo el camino (formato, throttling, reintentos)
sin enviar nada ni necesitar token, que es como se prueba en CI.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

TELEGRAM_API_BASE = "https://api.telegram.org"

#: Cifras de partida de C-8, a validar contra la documentacion vigente.
MAX_MESSAGES_PER_SECOND_GLOBAL = 30
MAX_MESSAGES_PER_MINUTE_PER_CHAT = 20

DEFAULT_MAX_RETRIES = 4
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_BACKOFF_SECONDS = 30.0

HTTP_TOO_MANY_REQUESTS = 429
HTTP_CLIENT_ERROR_START = 400
HTTP_SERVER_ERROR_START = 500

_GLOBAL_WINDOW_SECONDS = 1.0
_CHAT_WINDOW_SECONDS = 60.0
_LIMITER_MAX_ROUNDS = 100


class TelegramConfigError(RuntimeError):
    """Falta configuracion obligatoria (token o destino)."""


class TelegramSendError(RuntimeError):
    """No se pudo entregar el mensaje tras agotar los reintentos."""


def _redact(text: str, token: str | None) -> str:
    """Evita que el token acabe en un log o en el texto de una excepcion."""
    if token and token in text:
        return text.replace(token, "***REDACTED***")
    return text


@dataclass(frozen=True)
class TelegramOptions:
    """Ajustes de comportamiento. Agrupados para no inflar el constructor."""

    dry_run: bool = False
    max_retries: int = DEFAULT_MAX_RETRIES
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


@dataclass
class _RateLimiter:
    """Ventanas deslizantes: una global por segundo y otra por chat y minuto."""

    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _global: deque[float] = field(default_factory=deque)
    _per_chat: dict[str, deque[float]] = field(default_factory=dict)

    def acquire(self, chat_id: str) -> float:
        waited = 0.0
        for _ in range(_LIMITER_MAX_ROUNDS):  # cota dura: nunca bloquear para siempre
            now = self.monotonic()
            self._prune(now)
            chat_window = self._per_chat.setdefault(chat_id, deque())
            global_ok = len(self._global) < MAX_MESSAGES_PER_SECOND_GLOBAL
            chat_ok = len(chat_window) < MAX_MESSAGES_PER_MINUTE_PER_CHAT
            if global_ok and chat_ok:
                self._global.append(now)
                chat_window.append(now)
                return waited
            delay = 0.05 if not global_ok else 1.0
            self.sleep(delay)
            waited += delay
        raise TelegramSendError("el limitador de ritmo no consiguio hueco para enviar")

    def _prune(self, now: float) -> None:
        while self._global and now - self._global[0] >= _GLOBAL_WINDOW_SECONDS:
            self._global.popleft()
        for window in self._per_chat.values():
            while window and now - window[0] >= _CHAT_WINDOW_SECONDS:
                window.popleft()


@dataclass(frozen=True)
class SendOutcome:
    ok: bool
    attempts: int
    dry_run: bool
    detail: str


@dataclass(frozen=True)
class _AttemptResult:
    """Resultado de un intento: entregar, reintentar tras N s, o rendirse."""

    delivered: bool
    give_up: bool
    wait_seconds: float
    detail: str


class TelegramNotifier:
    """Envia mensajes a un chat o canal de Telegram.

    Preferir un ``chat_id`` de canal: un solo envio alcanza a todos los
    suscriptores, frente a N envios en difusion individual (C-8).
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        chat_id: str | None = None,
        client: httpx.Client | None = None,
        options: TelegramOptions | None = None,
    ) -> None:
        self._options = options or TelegramOptions()
        self._token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self._chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
        self._client = client
        self._owns_client = client is None
        self._limiter = _RateLimiter(
            monotonic=self._options.monotonic, sleep=self._options.sleep
        )

        if not self._options.dry_run:
            if not self._token:
                raise TelegramConfigError(
                    "falta TELEGRAM_BOT_TOKEN. Crea un bot con @BotFather y exporta el "
                    "token; NUNCA lo escribas en el repositorio (es publico). "
                    "Alternativamente usa dry_run para probar sin enviar."
                )
            if not self._chat_id:
                raise TelegramConfigError(
                    "falta TELEGRAM_CHAT_ID (id numerico de chat, o @nombre_del_canal)"
                )

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str, *, chat_id: str | None = None) -> SendOutcome:
        target = chat_id or self._chat_id or "<dry-run>"

        if self._options.dry_run:
            return SendOutcome(
                ok=True, attempts=0, dry_run=True, detail=f"[dry-run] a {target}: {text[:120]}"
            )

        self._limiter.acquire(target)
        client = self._client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        payload = {"chat_id": target, "text": text, "parse_mode": "HTML"}

        try:
            detail = "sin intentos"
            for attempt in range(1, self._options.max_retries + 1):
                result = self._attempt(client, url, payload, attempt)
                detail = result.detail
                if result.delivered:
                    return SendOutcome(
                        ok=True, attempts=attempt, dry_run=False, detail=f"entregado a {target}"
                    )
                if result.give_up:
                    break
                self._options.sleep(result.wait_seconds)
            raise TelegramSendError(
                f"no se pudo entregar tras {self._options.max_retries} intentos: {detail}"
            )
        finally:
            if self._owns_client:
                client.close()

    def _attempt(
        self, client: httpx.Client, url: str, payload: dict[str, str], attempt: int
    ) -> _AttemptResult:
        backoff = min(2.0**attempt, MAX_BACKOFF_SECONDS)
        try:
            response = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            return _AttemptResult(
                delivered=False,
                give_up=False,
                wait_seconds=backoff,
                detail=_redact(f"error de red: {exc}", self._token),
            )

        if response.status_code == HTTP_TOO_MANY_REQUESTS:
            # `retry_after` de Telegram manda sobre cualquier backoff propio.
            retry_after = _retry_after_seconds(response)
            return _AttemptResult(
                delivered=False,
                give_up=False,
                wait_seconds=retry_after if retry_after is not None else backoff,
                detail=f"429; retry_after={retry_after}s",
            )

        if response.is_success:
            return _AttemptResult(True, False, 0.0, "ok")

        detail = _redact(f"HTTP {response.status_code}: {response.text[:200]}", self._token)
        client_error = (
            HTTP_CLIENT_ERROR_START <= response.status_code < HTTP_SERVER_ERROR_START
        )
        # Un 4xx (chat inexistente, bot bloqueado) no mejora reintentando.
        return _AttemptResult(False, client_error, backoff, detail)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """``retry_after`` viaja en el cuerpo JSON y, a veces, en la cabecera."""
    try:
        body = response.json()
        value = (body.get("parameters") or {}).get("retry_after")
        if isinstance(value, int | float):
            return float(value)
    except (ValueError, AttributeError):
        pass
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    return None
