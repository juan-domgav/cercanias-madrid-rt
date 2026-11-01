"""Adaptadores de :class:`cercanias_domain.snapshot_port.SnapshotPublisher`.

Tres destinos con la misma interfaz: disco local, S3 y Cloud Storage. Los SDK
de nube son opcionales y se degradan a ``None`` si no estan instalados, de modo
que el adaptador local funciona sin ninguno de los dos y la imagen de cada nube
no arrastra el SDK de la otra.
"""

from __future__ import annotations

import gzip as gzip_lib
import os
import tempfile
from pathlib import Path
from typing import Any

# SDKs de nube opcionales. Se importan a nivel de modulo y se degradan a None si
# no estan: asi la imagen de AWS no arrastra el SDK de GCP ni al reves, y el
# adaptador local funciona sin ninguno de los dos. Un import perezoso dentro del
# constructor haria lo mismo, pero incumpliria PLC0415 y habria que silenciarlo.
try:
    import boto3
except ImportError:  # pragma: no cover - depende del extra instalado
    boto3 = None  # type: ignore[assignment]

try:
    from google.cloud import storage as gcs_storage
except ImportError:  # pragma: no cover - depende del extra instalado
    gcs_storage = None  # type: ignore[assignment]

#: El snapshot se sirve tras CDN y se regenera cada minuto (C-7). Una caché de
#: 30 s deja que el CDN absorba el tráfico de lectura sin servir datos con más de
#: una generación de antigüedad.
DEFAULT_CACHE_CONTROL = "public, max-age=30"

#: Cabecera de cualquier flujo gzip (RFC 1952). Ningún JSON ni NDJSON empieza
#: por 0x1f, así que sirve para distinguir sin ambigüedad un cuerpo comprimido.
GZIP_MAGIC = b"\x1f\x8b"


def gunzip_if_compressed(data: bytes) -> bytes:
    """Descomprime solo si ``data`` es un flujo gzip; si no, lo devuelve tal cual.

    Defensa ante la doble descompresión: Cloud Storage descomprime al servir
    (``decompressive transcoding``) y la librería de Python también, así que lo
    normal es recibir ya el JSON en claro. Si algún día llega comprimido (p. ej.
    ``raw_download=True`` o ``Cache-Control: no-transform``), se descomprime una
    vez; si llega en claro, NO se intenta descomprimir otra vez.
    """
    if data[:2] == GZIP_MAGIC:
        return gzip_lib.decompress(data)
    return data


class LocalSnapshotPublisher:
    """Escribe en disco. Para desarrollo y para los tests."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir

    def publish(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        cache_control: str = DEFAULT_CACHE_CONTROL,
        gzip: bool = False,
    ) -> str:
        # ``gzip`` se ignora: en disco no hay transcodificación que lo
        # descomprima al leer, así que se guarda en claro.
        del gzip
        target = self._base / key
        target.parent.mkdir(parents=True, exist_ok=True)
        # Escritura atómica: rename dentro del mismo sistema de ficheros.
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return str(target)

    def read(self, key: str) -> bytes | None:
        target = self._base / key
        return target.read_bytes() if target.exists() else None

    def list_keys(self, prefix: str) -> list[str]:
        """Claves bajo ``prefix``, relativas a la base (como en un bucket)."""
        base = self._base / prefix
        if not base.is_dir():
            return []
        return [str(p.relative_to(self._base)) for p in base.rglob("*") if p.is_file()]


class S3SnapshotPublisher:
    """Publica en S3. La atomicidad la da el propio PUT de S3."""

    def __init__(self, bucket: str, *, client: Any | None = None) -> None:
        if client is None:
            if boto3 is None:
                raise RuntimeError(
                    "S3SnapshotPublisher necesita boto3: instala el extra 'aws'"
                )
            client = boto3.client("s3")
        self._client = client
        self._bucket = bucket

    def publish(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        cache_control: str = DEFAULT_CACHE_CONTROL,
        gzip: bool = False,
    ) -> str:
        # ``gzip`` se ignora a propósito: S3 no descomprime al servir a clientes
        # sin ``Accept-Encoding`` ni ``get_object`` al leer. Sin desplegar en AWS,
        # no compensa; se guarda en claro como hasta ahora.
        del gzip
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
            CacheControl=cache_control,
        )
        return f"s3://{self._bucket}/{key}"

    def read(self, key: str) -> bytes | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
                return None
            raise
        body: bytes = response["Body"].read()
        return body


class GcsSnapshotPublisher:
    """Publica en Cloud Storage."""

    def __init__(self, bucket: str, *, client: Any | None = None) -> None:
        if client is None:
            if gcs_storage is None:
                raise RuntimeError(
                    "GcsSnapshotPublisher necesita google-cloud-storage: instala el extra 'gcp'"
                )
            client = gcs_storage.Client()
        self._bucket = client.bucket(bucket)
        self._bucket_name = bucket

    def publish(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        cache_control: str = DEFAULT_CACHE_CONTROL,
        gzip: bool = False,
    ) -> str:
        """Sube el objeto; con ``gzip=True`` lo guarda comprimido.

        Comprimido significa ``Content-Encoding: gzip`` con el ``Content-Type``
        real (p. ej. ``application/json``). Cloud Storage lo sirve comprimido a
        quien manda ``Accept-Encoding: gzip`` (navegadores) y descomprimido al
        resto (decompressive transcoding). Por eso ``cache_control`` nunca debe
        llevar ``no-transform``: rompería a los clientes que no aceptan gzip.
        """
        blob = self._bucket.blob(key)
        blob.cache_control = cache_control
        if gzip:
            # mtime=0: misma entrada, mismos bytes (y mismo md5) en cada subida.
            payload = gzip_lib.compress(payload, mtime=0)
            blob.content_encoding = "gzip"
        blob.upload_from_string(payload, content_type=content_type)
        return f"gs://{self._bucket_name}/{key}"

    def read(self, key: str) -> bytes | None:
        """Contenido en claro, esté o no guardado con ``Content-Encoding: gzip``.

        ``download_as_bytes`` (``raw_download=False``) ya descomprime: pide
        ``Accept-Encoding: gzip``, valida el checksum sobre los bytes
        comprimidos y devuelve el cuerpo descomprimido. ``gunzip_if_compressed``
        solo actúa si aun así llegara un flujo gzip, nunca dos veces.
        """
        blob = self._bucket.blob(key)
        if not blob.exists():
            return None
        data: bytes = blob.download_as_bytes()
        return gunzip_if_compressed(data)

    def list_keys(self, prefix: str) -> list[str]:
        """Nombres de objeto bajo ``prefix``; una sola llamada de listado."""
        return [blob.name for blob in self._bucket.list_blobs(prefix=prefix)]


def publisher_from_env() -> Any:
    """Elige el adaptador según el entorno, sin que el código de negocio lo sepa.

    - ``SNAPSHOT_BUCKET_S3``  -> S3
    - ``SNAPSHOT_BUCKET_GCS`` -> Cloud Storage
    - en otro caso            -> disco local bajo ``SNAPSHOT_DIR``

    Es lo que permite que el mismo contenedor sirva para las dos nubes y para
    local: cambia una variable, no el código.
    """
    if bucket := os.environ.get("SNAPSHOT_BUCKET_S3"):
        return S3SnapshotPublisher(bucket)
    if bucket := os.environ.get("SNAPSHOT_BUCKET_GCS"):
        return GcsSnapshotPublisher(bucket)
    return LocalSnapshotPublisher(Path(os.environ.get("SNAPSHOT_DIR", "docs/informes")))
