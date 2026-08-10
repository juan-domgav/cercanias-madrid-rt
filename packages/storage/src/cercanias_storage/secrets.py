"""Lectura de secretos en tiempo de ejecución, con degradación limpia.

Montar un secreto como variable de entorno en Cloud Run parece cómodo, pero
acopla el **arranque** del contenedor a que el secreto resuelva: si la versión no
existe —porque se rotó, se destruyó o nunca se creó— Cloud Run se niega a lanzar
la tarea y el contenedor nunca llega a ejecutarse.

Eso ocurrió en producción: al limpiar versiones equivocadas del token de
Telegram, la ingesta dejó de arrancar durante once minutos. El snapshot dejó de
publicarse por un problema de **notificaciones**, que es exactamente al revés de
lo que se pretendía: los lectores importan más que los suscriptores, y un fallo
al notificar nunca debe dejar la aplicación sin datos.

Leyendo el secreto aquí, un fallo se convierte en ``None`` y el llamante decide
qué hacer —en nuestro caso, seguir publicando y dejar Telegram en dry-run.
"""

from __future__ import annotations

import os

try:
    from google.cloud import secretmanager
except ImportError:  # pragma: no cover - depende del extra instalado
    secretmanager = None  # type: ignore[assignment]


def read_secret(env_var: str, *, secret_env_var: str | None = None) -> str | None:
    """Valor del secreto, o ``None`` si no se puede obtener.

    Orden de resolución:

    1. La variable de entorno directa (``env_var``). Es lo que se usa en local y
       en los tests, y permite sobrescribir sin tocar la nube.
    2. El nombre de recurso en Secret Manager indicado por ``secret_env_var``
       (p. ej. ``projects/123/secrets/telegram-bot-token/versions/latest``).

    Nunca lanza: cualquier problema de permisos, red o versión inexistente
    devuelve ``None``. Quien llama decide si eso es fatal; para las
    notificaciones no lo es.
    """
    directo = os.environ.get(env_var)
    if directo:
        return directo

    nombre = os.environ.get(secret_env_var or f"{env_var}_SECRET")
    if not nombre or secretmanager is None:
        return None

    try:
        cliente = secretmanager.SecretManagerServiceClient()
        respuesta = cliente.access_secret_version(name=nombre)
        valor: str = respuesta.payload.data.decode("utf-8").strip()
        return valor or None
    except Exception:
        return None
