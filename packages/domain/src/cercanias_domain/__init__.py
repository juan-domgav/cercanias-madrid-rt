"""Dominio de Cercanías Madrid Tiempo Real.

Este paquete no importa nada de AWS, GCP, Telegram, httpx ni ningún SDK de
infraestructura. Solo depende de Pydantic para la validación de modelos.
Los conectores y la persistencia son adaptadores que dependen de este
paquete, nunca al revés (puertos y adaptadores).
"""

from __future__ import annotations
