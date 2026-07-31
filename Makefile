.DEFAULT_GOAL := help
SHELL := /bin/bash

# ANOMALIA DE ENTORNO (macOS + uv + CPython 3.13) — ver ADR-003.
# uv marca los .pth de instalacion editable con el flag UF_HIDDEN de macOS, y
# site.py de CPython 3.13 SALTA EN SILENCIO cualquier .pth oculto: los paquetes
# del workspace quedan instalados pero no importables, sin ningun error.
#
# No basta con limpiar el flag tras `uv sync`: cada `uv run` re-sincroniza el
# entorno y vuelve a marcarlos ANTES de ejecutar el comando. Por eso se
# desactiva la sincronizacion automatica de `uv run` (UV_NO_SYNC) y se limpia
# el flag como prerequisito de cada target. `make install` es el unico punto
# que sincroniza.
#
# En Linux (CI) UF_HIDDEN no existe y `chflags` tampoco: el paso es un no-op.
export UV_NO_SYNC := 1

.PHONY: help install unhide lint format typecheck test test-cov \
        check-feed-freshness discover-data ideadif-explore ci clean

unhide:
	@find .venv -name "_editable_impl_*.pth" -exec chflags nohidden {} \; 2>/dev/null || true

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
	@echo "  make mvp                   - bucle MVP end-to-end (snapshot + salud + Telegram)"
	@echo "  make ci                    - lint + typecheck + test (lo que corre en GitHub Actions)"

install:
	UV_NO_SYNC=0 uv sync --all-packages
	@# ANOMALIA de entorno (macOS + uv + Python 3.13, documentada en ADR-003):
	@# uv marca los .pth de instalacion editable con el flag UF_HIDDEN de macOS.
	@# CPython 3.13 anadio en site.py un chequeo que SALTA silenciosamente
	@# cualquier .pth marcado como oculto, así que los paquetes del workspace
	@# no se importaban pese a instalarse "correctamente". Se limpia el flag
	@# tras cada sync para que `uv run` / `pytest` funcionen.
	@find .venv -name "_editable_impl_*.pth" -exec chflags nohidden {} \; 2>/dev/null || true

lint: unhide
	uv run ruff check .

format: unhide
	uv run ruff format .

typecheck: unhide
	uv run mypy \
		packages/domain/src \
		packages/connectors-renfe/src \
		packages/connectors-adif/src \
		packages/persistence/src \
		apps/worker/src

test: unhide
	uv run pytest \
		--cov=cercanias_domain \
		--cov-report=term-missing \
		--cov-fail-under=90

test-cov: unhide
	uv run pytest \
		--cov=cercanias_domain \
		--cov-report=html \
		--cov-report=term-missing

check-feed-freshness: unhide
	uv run check-feed-freshness

discover-data: unhide
	uv run discover-data

ideadif-explore: unhide
	uv run ideadif-explore

# MVP ejecutable: sondea, evalua invariantes, publica snapshot y notifica.
# Sin TELEGRAM_BOT_TOKEN funciona en dry-run.
mvp: unhide
	uv run mvp

# Cada etapa en su propia invocacion de make: `unhide` es prerequisito de todas,
# pero make solo lo ejecuta UNA vez por invocacion. Como cualquier `uv run`
# intermedio puede volver a marcar los .pth como ocultos (ver ADR-003), encadenar
# lint/typecheck/test dentro de una sola invocacion hacia fallar el ultimo target.
ci:
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory typecheck
	@$(MAKE) --no-print-directory test

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage
