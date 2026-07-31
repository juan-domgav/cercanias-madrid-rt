"""Conectores Adif.

En Fase 0 el ÚNICO código habilitado es el explorador de capacidades de
IDEADIF (``ideadif_explorer``): consulta ``GetCapabilities`` de WMS y WFS y
enumera lo que existe. NINGÚN conector de datos de Adif está habilitado
todavía (ver corrección C-12 y ``docs/adr/ADR-009-ideadif.md``): no se
inventan capas ni typeNames.
"""

from __future__ import annotations
