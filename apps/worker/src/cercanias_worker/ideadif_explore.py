"""``make ideadif-explore`` — E6.

Consulta ``GetCapabilities`` de WMS y WFS de IDEADIF sobre https y escribe
un snapshot legible en ``docs/informes/ideadif-capacidades.json``. La
narrativa/decision se documenta a mano en ``docs/adr/ADR-009-ideadif.md``
(las ADR son decisiones registradas, no un informe regenerado).

No implementa ningun conector de datos de Adif (C-12): solo explora.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from cercanias_connectors_adif.ideadif_explorer import (
    WFS_CAPABILITIES_URL,
    WMS_CAPABILITIES_URL,
    explore_ideadif_capabilities,
)
from cercanias_worker.config import WorkerConfig

# Los dos "servicios INSPIRE genericos" documentados publicamente, MAS los
# dos "servicios de tramificacion" descubiertos en Fase 0 inspeccionando el
# bundle JS de la propia aplicacion IDEADIF (ver ADR-009). Se consultan los
# 4 para dar el cuadro completo, no solo los 2 asumidos por C-12.
TRAMIFICACION_WMS_URL = "https://ideadif.adif.es/gservices/Tramificacion/wms?request=GetCapabilities"
TRAMIFICACION_WFS_URL = "https://ideadif.adif.es/gservices/Tramificacion/wfs?request=GetCapabilities"


def main() -> int:
    out_dir = Path(WorkerConfig().reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "ideadif-capacidades.json"

    print("Consultando GetCapabilities INSPIRE generico (WMS + WFS)...")
    generic = explore_ideadif_capabilities(wms_url=WMS_CAPABILITIES_URL, wfs_url=WFS_CAPABILITIES_URL)

    print("Consultando GetCapabilities de Tramificacion (WMS + WFS)...")
    tramificacion = explore_ideadif_capabilities(wms_url=TRAMIFICACION_WMS_URL, wfs_url=TRAMIFICACION_WFS_URL)

    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "generic_inspire": {
            "wms": asdict(generic.wms),
            "wfs": asdict(generic.wfs),
        },
        "tramificacion": {
            "wms": asdict(tramificacion.wms),
            "wfs": asdict(tramificacion.wfs),
        },
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    for label, result in [
        ("WMS generico (INSPIRE)", generic.wms),
        ("WFS generico (INSPIRE)", generic.wfs),
        ("WMS Tramificacion", tramificacion.wms),
        ("WFS Tramificacion", tramificacion.wfs),
    ]:
        status = "OK" if result.ok else f"FALLO ({result.reason})"
        print(f"{label}: {status} - {len(result.layer_or_typenames)} capas/typeNames")
        for name in result.layer_or_typenames:
            print(f"    - {name}")

    print(f"\nSnapshot escrito en {out_path}")
    print("Ver docs/adr/ADR-009-ideadif.md para la decision registrada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
