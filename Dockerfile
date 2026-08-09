# Imagen OCI del worker de ingesta. La misma sirve para Cloud Run y para Lambda:
# el destino del snapshot se elige por variable de entorno
# (SNAPSHOT_BUCKET_GCS / SNAPSHOT_BUCKET_S3), no por build.
FROM python:3.13-slim AS base

# uv resuelve el workspace de varios paquetes; pip solo no lo haria sin publicar
# cada paquete por separado.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Primero solo los manifiestos: si no cambian, Docker reutiliza la capa de
# dependencias y la build baja de minutos a segundos.
COPY pyproject.toml uv.lock ./
COPY packages/domain/pyproject.toml            packages/domain/
COPY packages/connectors-renfe/pyproject.toml  packages/connectors-renfe/
COPY packages/connectors-adif/pyproject.toml   packages/connectors-adif/
COPY packages/persistence/pyproject.toml       packages/persistence/
COPY packages/notifiers-telegram/pyproject.toml packages/notifiers-telegram/
COPY packages/storage/pyproject.toml           packages/storage/
COPY apps/worker/pyproject.toml                apps/worker/

# Los directorios src deben existir para que el backend de build resuelva el
# workspace, aunque el codigo llegue despues.
RUN mkdir -p packages/domain/src packages/connectors-renfe/src \
             packages/connectors-adif/src packages/persistence/src \
             packages/notifiers-telegram/src packages/storage/src apps/worker/src

COPY . .

# El extra 'gcp' trae google-cloud-storage; en la imagen de AWS bastaria con
# 'aws'. Se instalan los dos: la diferencia de tamano no compensa mantener dos
# Dockerfile, y el adaptador que no se use no se importa.
RUN uv sync --frozen --all-packages --extra gcp --extra aws

# El fixture congelado se usa como respaldo si el feed en vivo no responde.
ENV FIXTURES_DIR=/app/fixtures \
    SNAPSHOT_KEY=snapshot.json \
    PASSES_PER_INVOCATION=3 \
    SECONDS_BETWEEN_PASSES=20

# Usuario sin privilegios: el proceso solo necesita leer su propio codigo.
RUN useradd --create-home --uid 10001 worker && chown -R worker:worker /app
USER worker

ENTRYPOINT ["uv", "run", "--frozen", "--no-sync", "cloud-tick"]
