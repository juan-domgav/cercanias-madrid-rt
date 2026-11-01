from __future__ import annotations

import gzip
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cercanias_storage.publishers import (
    DEFAULT_CACHE_CONTROL,
    GcsSnapshotPublisher,
    LocalSnapshotPublisher,
    PreconditionFailedError,
    S3SnapshotPublisher,
    gunzip_if_compressed,
    publisher_from_env,
)


def test_local_escribe_y_lee(tmp_path: Path) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    assert pub.read("snap.json") is None
    pub.publish("snap.json", b'{"a":1}', content_type="application/json")
    assert pub.read("snap.json") == b'{"a":1}'


def test_local_no_deja_ficheros_temporales(tmp_path: Path) -> None:
    """La escritura atomica no debe dejar basura si se repite."""
    pub = LocalSnapshotPublisher(tmp_path)
    for i in range(3):
        pub.publish("snap.json", f'{{"n":{i}}}'.encode(), content_type="application/json")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["snap.json"]


def test_local_crea_subdirectorios(tmp_path: Path) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    pub.publish("v1/madrid/snap.json", b"{}", content_type="application/json")
    assert (tmp_path / "v1" / "madrid" / "snap.json").exists()


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}

    def put_object(self, **kwargs: Any) -> None:
        self.objects[kwargs["Key"]] = kwargs

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")

        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"Body": _Body(self.objects[Key]["Body"])}


def test_s3_publica_con_cache_control() -> None:
    """Sin Cache-Control el CDN no puede absorber el trafico de lectura (C-7)."""
    fake = _FakeS3()
    pub = S3SnapshotPublisher("mi-bucket", client=fake)
    ref = pub.publish("snap.json", b"{}", content_type="application/json")
    assert ref == "s3://mi-bucket/snap.json"
    guardado = fake.objects["snap.json"]
    assert guardado["CacheControl"] == DEFAULT_CACHE_CONTROL
    assert guardado["ContentType"] == "application/json"


def test_s3_permite_cache_control_por_objeto() -> None:
    fake = _FakeS3()
    S3SnapshotPublisher("mi-bucket", client=fake).publish(
        "horarios/1031V.json",
        b"{}",
        content_type="application/json",
        cache_control="public, max-age=3600",
    )
    assert fake.objects["horarios/1031V.json"]["CacheControl"] == "public, max-age=3600"


def test_s3_ignora_gzip_y_guarda_en_claro() -> None:
    fake = _FakeS3()
    pub = S3SnapshotPublisher("b", client=fake)
    pub.publish("s.json", b"{}", content_type="application/json", gzip=True)
    assert fake.objects["s.json"]["Body"] == b"{}"
    assert "ContentEncoding" not in fake.objects["s.json"]
    assert pub.read("s.json") == b"{}"


def test_s3_devuelve_none_si_no_existe() -> None:
    """Arranque en frio: no hay ultimo bueno conocido todavia, y no es un error."""
    assert S3SnapshotPublisher("b", client=_FakeS3()).read("no-existe.json") is None


def test_s3_propaga_errores_que_no_sean_clave_ausente() -> None:
    class _Roto(_FakeS3):
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDenied")

    with pytest.raises(RuntimeError, match="AccessDenied"):
        S3SnapshotPublisher("b", client=_Roto()).read("x.json")


class _FakeBlob:
    """Imita lo que importa de ``google.cloud.storage.Blob``.

    Cada subida crea una generación nueva con SOLO los metadatos de esa subida
    (como GCS). ``download_as_bytes`` descomprime si el objeto se guardó con
    ``Content-Encoding: gzip``, igual que la librería real con
    ``raw_download=False``; ``raw=True`` simula recibir los bytes en crudo.
    """

    def __init__(self, client: _FakeGcsClient, name: str) -> None:
        self._client, self._name = client, name
        self.cache_control: str | None = None
        self.content_encoding: str | None = None

    def upload_from_string(
        self, data: bytes, content_type: str, if_generation_match: int | None = None
    ) -> None:
        self._client.check(self._name, if_generation_match)
        self._client.store[self._name] = data
        self._client.gen[self._name] = self._client.gen.get(self._name, 0) + 1
        self._client.meta[self._name] = {
            "content_type": content_type,
            "content_encoding": self.content_encoding,
            "cache_control": self.cache_control,
        }

    def exists(self) -> bool:
        return self._name in self._client.store

    @property
    def generation(self) -> int:
        return self._client.gen[self._name]

    def download_as_bytes(self, if_generation_match: int | None = None) -> bytes:
        self._client.check(self._name, if_generation_match)
        data = self._client.store[self._name]
        if self._client.meta[self._name]["content_encoding"] == "gzip" and not self._client.raw:
            return gzip.decompress(data)
        return data


class _FakeGcsClient:
    def __init__(self, raw: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.meta: dict[str, dict[str, str | None]] = {}
        self.gen: dict[str, int] = {}
        self.raw = raw

    def check(self, name: str, if_generation_match: int | None) -> None:
        if if_generation_match is not None and self.gen.get(name, 0) != if_generation_match:
            raise _HttpError(412)

    def bucket(self, name: str) -> Any:
        client = self

        class _Bucket:
            def blob(self, key: str) -> _FakeBlob:
                return _FakeBlob(client, key)

            def get_blob(self, key: str) -> _FakeBlob | None:
                return _FakeBlob(client, key) if key in client.store else None

        return _Bucket()


class _HttpError(Exception):
    """Como ``google.api_core.exceptions``: lleva el código HTTP en ``code``."""

    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


def test_gcs_publica_y_lee() -> None:
    client = _FakeGcsClient()
    pub = GcsSnapshotPublisher("mi-bucket", client=client)
    assert pub.read("snap.json") is None
    ref = pub.publish("snap.json", b'{"a":1}', content_type="application/json")
    assert ref == "gs://mi-bucket/snap.json"
    assert pub.read("snap.json") == b'{"a":1}'
    # Por defecto, sin gzip: comportamiento previo intacto.
    assert client.store["snap.json"] == b'{"a":1}'
    assert client.meta["snap.json"]["content_encoding"] is None


PAYLOAD = '{"estación":"Atocha","trenes":[1,2,3]}'.encode()


def test_gcs_gzip_sube_comprimido_con_metadatos_correctos() -> None:
    client = _FakeGcsClient()
    pub = GcsSnapshotPublisher("mi-bucket", client=client)
    pub.publish("boards.json", PAYLOAD, content_type="application/json", gzip=True)
    guardado = client.store["boards.json"]
    assert guardado[:2] == b"\x1f\x8b"
    assert gzip.decompress(guardado) == PAYLOAD
    assert client.meta["boards.json"] == {
        # El tipo real, no application/gzip: el navegador debe ver JSON.
        "content_type": "application/json",
        "content_encoding": "gzip",
        # Se conserva y sin no-transform (rompería clientes sin gzip).
        "cache_control": "public, max-age=30",
    }


def test_gcs_gzip_conserva_cache_control_propio() -> None:
    client = _FakeGcsClient()
    pub = GcsSnapshotPublisher("b", client=client)
    pub.publish("horarios/x.json", PAYLOAD, content_type="application/json",
                cache_control="public, max-age=3600", gzip=True)
    assert client.meta["horarios/x.json"]["cache_control"] == "public, max-age=3600"
    assert client.meta["horarios/x.json"]["content_encoding"] == "gzip"


def test_gcs_gzip_es_determinista() -> None:
    client = _FakeGcsClient()
    pub = GcsSnapshotPublisher("b", client=client)
    pub.publish("a.json", PAYLOAD, content_type="application/json", gzip=True)
    primero = client.store["a.json"]
    pub.publish("a.json", PAYLOAD, content_type="application/json", gzip=True)
    assert client.store["a.json"] == primero


def test_gcs_lee_en_claro_sin_doble_descompresion() -> None:
    # La librería ya descomprime: read() no debe intentarlo otra vez.
    pub = GcsSnapshotPublisher("b", client=_FakeGcsClient())
    pub.publish("s.json", PAYLOAD, content_type="application/json", gzip=True)
    assert pub.read("s.json") == PAYLOAD


def test_gcs_lee_en_claro_aunque_lleguen_bytes_comprimidos() -> None:
    # Si llegara el flujo gzip en crudo (raw_download, no-transform...), una vez.
    pub = GcsSnapshotPublisher("b", client=_FakeGcsClient(raw=True))
    pub.publish("s.json", PAYLOAD, content_type="application/json", gzip=True)
    assert pub.read("s.json") == PAYLOAD


def test_gcs_lee_objetos_antiguos_sin_gzip() -> None:
    pub = GcsSnapshotPublisher("b", client=_FakeGcsClient(raw=True))
    pub.publish("s.json", PAYLOAD, content_type="application/json")
    assert pub.read("s.json") == PAYLOAD


def test_gcs_republicar_sin_gzip_quita_content_encoding() -> None:
    # Rollback a la imagen anterior: el objeto vuelve a quedar en claro.
    client = _FakeGcsClient()
    pub = GcsSnapshotPublisher("b", client=client)
    pub.publish("s.json", PAYLOAD, content_type="application/json", gzip=True)
    pub.publish("s.json", PAYLOAD, content_type="application/json")
    assert client.store["s.json"] == PAYLOAD
    assert client.meta["s.json"]["content_encoding"] is None


def test_gcs_gzip_con_cuerpo_vacio() -> None:
    pub = GcsSnapshotPublisher("b", client=_FakeGcsClient(raw=True))
    pub.publish("v.json", b"", content_type="application/json", gzip=True)
    assert pub.read("v.json") == b""


def test_gunzip_if_compressed_no_toca_json_ni_vacio() -> None:
    assert gunzip_if_compressed(b"") == b""
    assert gunzip_if_compressed(b"{}") == b"{}"
    assert gunzip_if_compressed(b"\x1f") == b"\x1f"
    assert gunzip_if_compressed(gzip.compress(b"[1]")) == b"[1]"


def test_local_ignora_gzip_y_guarda_en_claro(tmp_path: Path) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    pub.publish("s.json", PAYLOAD, content_type="application/json", gzip=True)
    assert (tmp_path / "s.json").read_bytes() == PAYLOAD
    assert pub.read("s.json") == PAYLOAD


def test_seleccion_por_entorno_local_por_defecto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SNAPSHOT_BUCKET_S3", raising=False)
    monkeypatch.delenv("SNAPSHOT_BUCKET_GCS", raising=False)
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path))
    assert isinstance(publisher_from_env(), LocalSnapshotPublisher)


def test_seleccion_por_entorno_prefiere_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """El mismo contenedor sirve para las dos nubes: cambia el entorno, no el codigo."""
    monkeypatch.setenv("SNAPSHOT_BUCKET_S3", "un-bucket")
    monkeypatch.setattr(
        "cercanias_storage.publishers.S3SnapshotPublisher.__init__",
        lambda self, bucket, client=None: None,
    )
    assert isinstance(publisher_from_env(), S3SnapshotPublisher)


# --- escritura condicional (if_generation_match) -----------------------------


def test_gcs_read_versioned_y_publish_condicional() -> None:
    client = _FakeGcsClient()
    pub = GcsSnapshotPublisher("b", client=client)
    assert pub.read_versioned("i.json") == (None, 0)
    # 0 = solo si no existe.
    pub.publish("i.json", PAYLOAD, content_type="application/json", gzip=True, if_generation_match=0)
    data, gen = pub.read_versioned("i.json")
    assert data == PAYLOAD
    assert gen == 1
    pub.publish("i.json", b"{}", content_type="application/json", if_generation_match=gen)
    # Una generación vieja ya no vale: 412 -> PreconditionFailedError.
    with pytest.raises(PreconditionFailedError):
        pub.publish("i.json", b"[]", content_type="application/json", if_generation_match=gen)
    with pytest.raises(PreconditionFailedError):
        pub.publish("i.json", b"[]", content_type="application/json", if_generation_match=0)
    assert pub.read("i.json") == b"{}"


def test_gcs_read_versioned_descomprime_bytes_en_crudo() -> None:
    pub = GcsSnapshotPublisher("b", client=_FakeGcsClient(raw=True))
    pub.publish("i.json", PAYLOAD, content_type="application/json", gzip=True)
    assert pub.read_versioned("i.json") == (PAYLOAD, 1)


def test_gcs_read_versioned_si_cambia_durante_la_descarga() -> None:
    client = _FakeGcsClient()
    pub = GcsSnapshotPublisher("b", client=client)
    pub.publish("i.json", PAYLOAD, content_type="application/json")
    blob = client.bucket("b").get_blob("i.json")
    assert blob is not None
    viejo = _Desfasado(blob, generation=1)
    client.gen["i.json"] = 2  # otro escribió entre los metadatos y la descarga
    pub._bucket = SimpleNamespace(get_blob=lambda _k: viejo)
    with pytest.raises(PreconditionFailedError):
        pub.read_versioned("i.json")


class _Desfasado:
    def __init__(self, blob: _FakeBlob, generation: int) -> None:
        self._blob, self.generation = blob, generation

    def download_as_bytes(self, if_generation_match: int | None = None) -> bytes:
        return self._blob.download_as_bytes(if_generation_match=if_generation_match)


def test_gcs_otros_errores_de_subida_no_se_disfrazan() -> None:
    client = _FakeGcsClient()
    pub = GcsSnapshotPublisher("b", client=client)

    def _denegado(*_a: Any, **_k: Any) -> None:
        raise _HttpError(403)

    client.check = _denegado  # type: ignore[method-assign]
    with pytest.raises(_HttpError):
        pub.publish("i.json", b"{}", content_type="application/json", if_generation_match=0)


def test_local_publish_condicional(tmp_path: Path) -> None:
    pub = LocalSnapshotPublisher(tmp_path)
    data, gen = pub.read_versioned("ops/i.json")
    assert (data, gen) == (None, 0)
    pub.publish("ops/i.json", b"{}", content_type="application/json", if_generation_match=0)
    with pytest.raises(PreconditionFailedError):
        pub.publish("ops/i.json", b"[]", content_type="application/json", if_generation_match=0)
    data, gen = pub.read_versioned("ops/i.json")
    assert data == b"{}"
    assert gen != 0
    pub.publish("ops/i.json", b"[1]", content_type="application/json", if_generation_match=gen)
    with pytest.raises(PreconditionFailedError):
        pub.publish("ops/i.json", b"[2]", content_type="application/json", if_generation_match=gen)
    assert pub.read("ops/i.json") == b"[1]"
