"""``make telegram-test`` — verifica las credenciales sin lanzar el MVP.

Depurar la integración dentro del bucle del MVP es incómodo: hay que esperar a
que se cumpla la condición de aviso para saber si el token siquiera vale. Este
comando envía un mensaje y traduce los errores típicos de la Bot API, que son
poco descriptivos por sí solos.
"""

from __future__ import annotations

import os
import sys

from cercanias_notifiers_telegram.telegram_notifier import (
    TelegramConfigError,
    TelegramNotifier,
    TelegramSendError,
)

EXIT_MISSING_CONFIG = 2
EXIT_SEND_FAILED = 3

#: Errores frecuentes de la Bot API y qué hacer con ellos. El texto que devuelve
#: Telegram es correcto pero no dice cómo arreglarlo.
_DIAGNOSTICOS: tuple[tuple[str, str], ...] = (
    (
        "unauthorized",
        "El token no es valido. Comprueba que lo copiaste entero de @BotFather, "
        "incluyendo la parte anterior a los dos puntos.",
    ),
    (
        "chat not found",
        "El chat_id no existe o el bot nunca lo ha visto.\n"
        "  - Si es un canal: usa @nombre_del_canal y anade el bot como ADMINISTRADOR.\n"
        "  - Si es un chat privado: escribe primero al bot, y saca el id numerico de\n"
        "    https://api.telegram.org/bot<TOKEN>/getUpdates",
    ),
    (
        "not enough rights",
        "El bot esta en el canal pero no puede publicar. Dale permiso de "
        "'Publicar mensajes' en la configuracion de administradores.",
    ),
    (
        "not a member",
        "El bot no pertenece al canal o grupo. Anadelo como administrador.",
    ),
    (
        "bot was blocked",
        "El usuario ha bloqueado al bot. Desbloquealo desde el chat.",
    ),
)


def _diagnostico(mensaje: str) -> str | None:
    bajo = mensaje.lower()
    for patron, ayuda in _DIAGNOSTICOS:
        if patron in bajo:
            return ayuda
    return None


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    print("Comprobando configuracion de Telegram\n")
    print(f"  TELEGRAM_BOT_TOKEN: {'definido' if token else 'NO DEFINIDO'}")
    print(f"  TELEGRAM_CHAT_ID:   {chat_id or 'NO DEFINIDO'}")
    # El token no se imprime nunca, ni truncado: este repositorio es publico y
    # la salida de este comando acabara pegada en algun sitio.

    if not token or not chat_id:
        print(
            "\nFalta configuracion. Exportalas en tu shell (NUNCA en un fichero del repo):\n"
            '  export TELEGRAM_BOT_TOKEN="123456:AA..."\n'
            '  export TELEGRAM_CHAT_ID="@tu_canal"',
            file=sys.stderr,
        )
        return EXIT_MISSING_CONFIG

    try:
        notifier = TelegramNotifier()
    except TelegramConfigError as exc:
        print(f"\nERROR de configuracion: {exc}", file=sys.stderr)
        return EXIT_MISSING_CONFIG

    # 1. ¿Es valido el token? getMe no envia nada y responde quien es el bot.
    print("\n[1/3] Identificando el bot (getMe)...")
    try:
        me = notifier.get_me()
    except TelegramSendError as exc:
        print(f"  FALLO: {exc}", file=sys.stderr)
        ayuda = _diagnostico(str(exc))
        print(f"\nProbable causa:\n  {ayuda or 'token no valido o sin conexion.'}", file=sys.stderr)
        return EXIT_SEND_FAILED
    print(f"  OK  bot: @{me.get('username')} ({me.get('first_name')})")

    # 2. ¿Existe el destino y lo ve el bot? Aqui es donde falla casi siempre.
    print("[2/3] Resolviendo el destino (getChat)...")
    try:
        chat = notifier.get_chat()
    except TelegramSendError as exc:
        print(f"  FALLO: {exc}", file=sys.stderr)
        ayuda = _diagnostico(str(exc))
        if ayuda:
            print(f"\nProbable causa:\n  {ayuda}", file=sys.stderr)
        print(
            "\nRecuerda: TELEGRAM_CHAT_ID NO es el nombre visible del canal.\n"
            "  - Canal publico:  @nombre_de_usuario_del_canal (sin espacios)\n"
            "  - Canal privado:  id numerico que empieza por -100\n"
            "  - Chat contigo:   id numerico, tras escribir al bot",
            file=sys.stderr,
        )
        return EXIT_SEND_FAILED
    titulo = chat.get("title") or chat.get("username") or chat.get("first_name")
    print(f"  OK  destino: {titulo!r} (tipo: {chat.get('type')}, id: {chat.get('id')})")

    mensaje = (
        "<b>Cercanias Madrid — prueba de integracion</b>\n"
        "Si lees esto, el token y el chat_id funcionan.\n"
        "<i>Mensaje de prueba; no corresponde a ninguna incidencia real.</i>"
    )

    print(f"[3/3] Enviando mensaje de prueba a {chat_id}...")
    try:
        outcome = notifier.send(mensaje)
    except TelegramSendError as exc:
        print(f"\nFALLO: {exc}", file=sys.stderr)
        ayuda = _diagnostico(str(exc))
        if ayuda:
            print(f"\nProbable causa:\n  {ayuda}", file=sys.stderr)
        return EXIT_SEND_FAILED

    print(f"OK: {outcome.detail} (intentos: {outcome.attempts})")
    print("\nTelegram esta listo. `make mvp` enviara avisos reales.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
