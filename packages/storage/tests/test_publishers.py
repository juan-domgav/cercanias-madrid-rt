from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cercanias_storage.publishers import (
    DEFAULT_CACHE_CONTROL,
    GcsSnapshotPublisher,
    LocalSnapshotPublisher,
    S3SnapshotPublisher,
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
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store, self._name = store, name
        self.cache_control: str | None = None

    def upload_from_string(self, data: bytes, content_type: str) -> None:
        self._store[self._name] = data

    def exists(self) -> bool:
        return self._name in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._name]


class _FakeGcsClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> Any:
        store = self.store

        class _Bucket:
            def blob(self, key: str) -> _FakeBlob:
                return _FakeBlob(store, key)

        return _Bucket()


def test_gcs_publica_y_lee() -> None:
    pub = GcsSnapshotPublisher("mi-bucket", client=_FakeGcsClient())
    assert pub.read("snap.json") is None
    ref = pub.publish("snap.json", b'{"a":1}', content_type="application/json")
    assert ref == "gs://mi-bucket/snap.json"
    assert pub.read("snap.json") == b'{"a":1}'


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
