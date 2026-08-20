#!/usr/bin/env python3
"""Lee el feedback enviado por el bot de Telegram.

Vive en un bucket PRIVADO porque incluye ``chat_id``, que es un identificador
personal: el bucket de la web es publico por definicion y no puede alojarlo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

BUCKET = os.environ.get("PRIVATE_BUCKET", "cercanias-madrid-rt-privado")


def main() -> int:
    try:
        salida = subprocess.run(
            ["gcloud", "storage", "cat", f"gs://{BUCKET}/feedback.jsonl"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        print("gcloud no esta instalado o no esta en el PATH", file=sys.stderr)
        return 1
    if salida.returncode != 0:
        print("Sin feedback todavia (o sin acceso al bucket privado).")
        return 0

    filas = [json.loads(linea) for linea in salida.stdout.splitlines() if linea.strip()]
    if not filas:
        print("Sin feedback todavia.")
        return 0

    print(f"{len(filas)} aviso(s), del mas reciente al mas antiguo\n")
    for f in reversed(filas):
        cuando = str(f.get("at", ""))[:16].replace("T", " ")
        print(f"[{cuando}]  chat {f.get('chat_id')}")
        print(f"   {f.get('text', '')}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
