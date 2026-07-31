.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help install lint format typecheck test test-cov \
        check-feed-freshness discover-data ideadif-explore ci clean

help:
	@echo "Targets disponibles:"
	@echo "  make install               - instala dependencias (uv sync) del workspace"
	@echo "  make lint                  - Ruff (lint, sin excepciones silenciadas)"
	@echo "  make format                - Ruff format"
	@echo "  make typecheck             - mypy --strict"
	@echo "  make test                  - pytest (unitarios + contrato), con cobertura de dominio"
	@echo "  make check-feed-freshness  - E2: mide el desfase real del feed (Anexo A.8)"
	@echo "  make discover-data         - E7: informe de cobertura de estaciones y nucleos"
	@echo "  make ideadif-explore       - E6: GetCapabilities WMS/WFS de IDEADIF"
	@echo "  make ci                    - lint + typecheck + test (lo que corre en GitHub Actions)"

install:
	uv sync --all-packages
	@# ANOMALIA de entorno (macOS + uv + Python 3.13, documentada en ADR-003):
	@# uv marca los .pth de instalacion editable con el flag UF_HIDDEN de macOS.
	@# CPython 3.13 anadio en site.py un chequeo que SALTA silenciosamente
	@# cualquier .pth marcado como oculto, así que los paquetes del workspace
	@# no se importaban pese a instalarse "correctamente". Se limpia el flag
	@# tras cada sync para que `uv run` / `pytest` funcionen.
	@find .venv -name "_editable_impl_*.pth" -exec chflags nohidden {} \; 2>/dev/null || true

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy \
		packages/domain/src \
		packages/connectors-renfe/src \
		packages/connectors-adif/src \
		packages/persistence/src \
		apps/worker/src

test:
	uv run pytest \
		--cov=cercanias_domain \
		--cov-report=term-missing \
		--cov-fail-under=90

test-cov:
	uv run pytest \
		--cov=cercanias_domain \
		--cov-report=html \
		--cov-report=term-missing

check-feed-freshness:
	uv run check-feed-freshness

discover-data:
	uv run discover-data

ideadif-explore:
	uv run ideadif-explore

ci: lint typecheck test

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage
