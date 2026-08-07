"""Puerto de publicación del snapshot.

El dominio define *qué* significa publicar; cada nube trae su propio adaptador
(S3, Cloud Storage, disco local). Nada de aquí importa boto3 ni google-cloud.

La semántica de **último bueno conocido** vive en este puerto y no en cada
adaptador, porque es una regla de negocio, no un detalle de almacenamiento: el
núcleo de Madrid desaparece del feed en ~5 % de los sondeos (ADR-011), y publicar
un snapshot vacío encima de uno bueno serviría un mapa sin trenes sin que nada
fallase. Si esto viviera en el adaptador habría que reimplementarlo —y acertar—
tres veces.
"""

from __future__ import annotations

from typing import Protocol


class SnapshotPublisher(Protocol):
    """Destino de un snapshot ya serializado."""

    def publish(self, key: str, payload: bytes, *, content_type: str) -> str:
        """Publica y devuelve una referencia legible (URL o ruta).

        Debe ser atómica en la medida en que el destino lo permita: un lector
        nunca debe ver un snapshot a medio escribir.
        """
        ...

    def read(self, key: str) -> bytes | None:
        """Contenido actual, o ``None`` si no existe.

        Necesario para el último bueno conocido: al arrancar en frío (una
        invocación programada sin estado previo) hay que poder recuperar el
        último snapshot válido antes de decidir si el actual lo sustituye.
        """
        ...
