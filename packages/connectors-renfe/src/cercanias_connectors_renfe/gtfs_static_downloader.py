"""Descargador de GTFS estático — E3, corrección C-1 / Anexo A.10.

Cascada de tres orígenes, en este orden obligatorio:

1. NAP del Ministerio de Transportes — https://nap.transportes.gob.es/Files/Detail/929
2. CRTM (Consorcio Regional de Transportes de Madrid) — https://datos.crtm.es
3. ``ssl.renfe.com`` (último recurso) — la URL canónica de Renfe, que el
   Anexo A.10 documentaba como caída por fallo de certificado SSL desde
   diciembre de 2025.

ANOMALÍA verificada en Fase 0 (31/07/2026, ver docs/informes y ADR
correspondiente): la situación real observada hoy es la INVERSA de lo
esperado:

- **NAP**: la URL responde 200 pero es una página HTML que exige login
  (``/Account/Login?returnUrl=/Files/Detail/929``) para poder descargar el
  fichero. No sirve un ZIP directamente sin autenticación.
- **CRTM**: el dataset "GTFS Red de Cercanías" existe y está confirmado en
  el catálogo DCAT de ``datos.crtm.es``, pero el portal es una SPA de
  ArcGIS Hub; no se ha podido obtener mediante peticiones HTTP simples una
  URL de descarga directa del recurso (los endpoints de la API de Hub
  probados devolvieron 403/404). Se deja documentado como pendiente de
  investigación manual (inspección del tráfico del navegador), no como
  fuente utilizable hoy.
- **ssl.renfe.com**: responde 200, el certificado TLS verifica correctamente
  (``Sectigo Public Server Authentication CA``) y sirve un ZIP GTFS válido y
  completo. El problema de certificado de A.10 parece haberse resuelto
  entre el 29/07/2026 (fecha del Anexo A) y el 31/07/2026.

En consecuencia, la fuente que sirve realmente en Fase 0 es la tercera
(``ssl.renfe.com``), no la primera como cabría esperar de C-1. La cascada
se mantiene tal cual (en el orden documentado) porque es la única forma de
no depender de un solo proveedor si la situación vuelve a cambiar.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

DEFAULT_TIMEOUT_SECONDS = 30.0
HTTP_OK = 200

ZIP_MAGIC_BYTES = b"PK\x03\x04"

REQUIRED_FILES: dict[str, tuple[str, ...]] = {
    "agency.txt": ("agency_id", "agency_name"),
    "routes.txt": ("route_id", "route_short_name"),
    "trips.txt": ("route_id", "trip_id"),
    "stops.txt": ("stop_id", "stop_name", "stop_lat", "stop_lon"),
    "stop_times.txt": ("trip_id", "stop_id"),
    "calendar.txt": ("service_id",),
}
"""Ficheros y columnas minimas exigidas. NO exige calendar_dates.txt ni
shapes.txt: el ZIP servido por ssl.renfe.com en Fase 0 no incluye
calendar_dates.txt, y la especificacion base los citaba como "tal como" a
modo de ejemplo, no como obligatorios."""


@dataclass(frozen=True)
class GtfsStaticSource:
    name: str
    url: str
    verify_tls: bool = True
    requires_auth_note: str | None = None


DEFAULT_SOURCES: tuple[GtfsStaticSource, ...] = (
    GtfsStaticSource(name="nap", url="https://nap.transportes.gob.es/Files/Detail/929"),
    GtfsStaticSource(name="crtm", url="https://datos.crtm.es"),
    GtfsStaticSource(
        name="ssl.renfe.com",
        url="https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip",
        verify_tls=True,
    ),
)


@dataclass(frozen=True)
class SourceAttempt:
    source_name: str
    url: str
    ok: bool
    status_code: int | None
    reason: str
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class CsvValidationResult:
    filename: str
    present: bool
    missing_columns: tuple[str, ...]
    row_count: int

    @property
    def valid(self) -> bool:
        return self.present and not self.missing_columns


@dataclass(frozen=True)
class ZipValidationResult:
    valid: bool
    bad_file_in_zip: str | None
    files: tuple[CsvValidationResult, ...]
    namelist: tuple[str, ...]

    @property
    def errors(self) -> tuple[str, ...]:
        errors = []
        if self.bad_file_in_zip:
            errors.append(f"CRC invalido en {self.bad_file_in_zip}")
        for f in self.files:
            if not f.present:
                errors.append(f"falta {f.filename} en el ZIP")
            elif f.missing_columns:
                errors.append(f"{f.filename} no tiene columnas: {', '.join(f.missing_columns)}")
        return tuple(errors)


class GtfsStaticUnavailableError(RuntimeError):
    """Ninguna fuente de la cascada respondio con un GTFS valido.

    Fallo explicito de arranque (C-1 / Anexo A.10): nunca continuar con
    datos vacios.
    """

    def __init__(self, attempts: list[SourceAttempt]) -> None:
        self.attempts = attempts
        summary = "; ".join(f"{a.source_name}: {a.reason}" for a in attempts)
        super().__init__(
            "Ninguna de las fuentes de GTFS estatico respondio con un ZIP valido. "
            f"Intentos: {summary}"
        )


def validate_zip_bytes(data: bytes) -> ZipValidationResult:
    """Valida el ZIP (CRC) y cada CSV requerido (columnas minimas)."""
    if not data.startswith(ZIP_MAGIC_BYTES):
        return ZipValidationResult(valid=False, bad_file_in_zip=None, files=(), namelist=())

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ZipValidationResult(valid=False, bad_file_in_zip=None, files=(), namelist=())

    bad_file = zf.testzip()
    namelist = tuple(zf.namelist())

    results: list[CsvValidationResult] = []
    for filename, required_columns in REQUIRED_FILES.items():
        if filename not in namelist:
            results.append(
                CsvValidationResult(
                    filename=filename, present=False, missing_columns=required_columns, row_count=0
                )
            )
            continue
        with zf.open(filename) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig")
            reader = csv.DictReader(text)
            header = {(name or "").strip() for name in (reader.fieldnames or [])}
            missing = tuple(c for c in required_columns if c not in header)
            row_count = sum(1 for _ in reader)
        results.append(
            CsvValidationResult(
                filename=filename, present=True, missing_columns=missing, row_count=row_count
            )
        )

    all_valid = bad_file is None and all(r.valid for r in results)
    return ZipValidationResult(
        valid=all_valid, bad_file_in_zip=bad_file, files=tuple(results), namelist=namelist
    )


@dataclass(frozen=True)
class VersionManifestEntry:
    version: str
    source_name: str
    source_url: str
    etag: str | None
    last_modified: str | None
    downloaded_at: str
    directory: str


@dataclass(frozen=True)
class DownloadOutcome:
    source_used: SourceAttempt
    attempts: list[SourceAttempt]
    version_dir: Path
    changed: bool
    validation: ZipValidationResult


ManifestEntry = dict[str, object]


def _get_active_entry(manifest: dict[str, object]) -> ManifestEntry | None:
    active = manifest.get("active")
    if active is None:
        return None
    if not isinstance(active, dict):
        return None
    return active


def _read_manifest(base_dir: Path) -> dict[str, object]:
    manifest_path = base_dir / "manifest.json"
    if not manifest_path.exists():
        return {"active": None, "previous": None}
    with manifest_path.open(encoding="utf-8") as f:
        result: dict[str, object] = json.load(f)
        return result


def _write_manifest(base_dir: Path, manifest: dict[str, object]) -> None:
    manifest_path = base_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def download_gtfs_static(
    base_dir: Path,
    *,
    sources: tuple[GtfsStaticSource, ...] = DEFAULT_SOURCES,
    client: httpx.Client | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    force: bool = False,
) -> DownloadOutcome:
    """Prueba cada fuente en orden; falla explícitamente si ninguna sirve un ZIP válido."""
    base_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(base_dir)
    active_entry = _get_active_entry(manifest)

    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)

    attempts: list[SourceAttempt] = []
    try:
        for source in sources:
            try:
                response = http_client.get(source.url, timeout=timeout_seconds)
            except httpx.HTTPError as exc:
                attempts.append(
                    SourceAttempt(
                        source_name=source.name,
                        url=source.url,
                        ok=False,
                        status_code=None,
                        reason=f"error de red: {exc}",
                    )
                )
                continue

            etag = response.headers.get("etag")
            last_modified = response.headers.get("last-modified")

            if response.status_code != HTTP_OK:
                attempts.append(
                    SourceAttempt(
                        source_name=source.name,
                        url=source.url,
                        ok=False,
                        status_code=response.status_code,
                        reason=f"HTTP {response.status_code}",
                        etag=etag,
                        last_modified=last_modified,
                    )
                )
                continue

            content = response.content
            if not content.startswith(ZIP_MAGIC_BYTES):
                content_type = response.headers.get("content-type", "desconocido")
                attempts.append(
                    SourceAttempt(
                        source_name=source.name,
                        url=source.url,
                        ok=False,
                        status_code=response.status_code,
                        reason=(
                            f"respuesta HTTP 200 pero no es un ZIP "
                            f"(content-type={content_type}); probablemente una "
                            "pagina HTML (login, portal SPA, etc.)"
                        ),
                        etag=etag,
                        last_modified=last_modified,
                    )
                )
                continue

            validation = validate_zip_bytes(content)
            if not validation.valid:
                attempts.append(
                    SourceAttempt(
                        source_name=source.name,
                        url=source.url,
                        ok=False,
                        status_code=response.status_code,
                        reason=f"ZIP invalido: {'; '.join(validation.errors) or 'CRC fallido'}",
                        etag=etag,
                        last_modified=last_modified,
                    )
                )
                continue

            # Fuente valida encontrada.
            success = SourceAttempt(
                source_name=source.name,
                url=source.url,
                ok=True,
                status_code=response.status_code,
                reason="OK",
                etag=etag,
                last_modified=last_modified,
            )
            attempts.append(success)

            unchanged = (
                not force
                and active_entry is not None
                and active_entry.get("source_name") == source.name
                and active_entry.get("etag") == etag
                and active_entry.get("last_modified") == last_modified
                and etag is not None
            )

            if unchanged and active_entry is not None:
                version_dir = base_dir / str(active_entry["directory"])
                return DownloadOutcome(
                    source_used=success,
                    attempts=attempts,
                    version_dir=version_dir,
                    changed=False,
                    validation=validation,
                )

            downloaded_at = datetime.now(tz=UTC)
            version_name = downloaded_at.strftime("%Y%m%dT%H%M%S%fZ")
            version_dir = base_dir / version_name
            version_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                zf.extractall(version_dir)
            (version_dir / "_source.json").write_text(
                json.dumps(
                    {
                        "source_name": source.name,
                        "source_url": source.url,
                        "etag": etag,
                        "last_modified": last_modified,
                        "downloaded_at": downloaded_at.isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            new_entry: dict[str, object] = {
                "version": version_name,
                "source_name": source.name,
                "source_url": source.url,
                "etag": etag,
                "last_modified": last_modified,
                "downloaded_at": downloaded_at.isoformat(),
                "directory": version_name,
            }
            manifest = {"active": new_entry, "previous": active_entry}
            _write_manifest(base_dir, manifest)

            return DownloadOutcome(
                source_used=success,
                attempts=attempts,
                version_dir=version_dir,
                changed=True,
                validation=validation,
            )
    finally:
        if owns_client:
            http_client.close()

    raise GtfsStaticUnavailableError(attempts)
