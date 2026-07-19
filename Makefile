PYTHON ?= python3

.PHONY: help install dev run format lint typecheck test coverage check build audit kosit docker clean

help:
	@echo "make dev       Entwicklungsumgebung installieren"
	@echo "make run       Anwendung lokal starten"
	@echo "make format    Python-Code formatieren"
	@echo "make check     Version, Stil, Typen und Tests prüfen"
	@echo "make build     Wheel, sdist und Repository-ZIP bauen"

install:
	$(PYTHON) -m pip install -r requirements.txt

dev:
	$(PYTHON) -m pip install -e ".[dev]"

run:
	$(PYTHON) -m app --open

format:
	$(PYTHON) -m ruff check --fix app tests scripts
	$(PYTHON) -m ruff format app tests scripts

lint:
	$(PYTHON) -m ruff check app tests scripts
	$(PYTHON) -m ruff format --check app tests scripts

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest

coverage:
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing --cov-report=xml

check:
	$(PYTHON) scripts/verify_version.py
	$(PYTHON) -m ruff check app tests scripts
	$(PYTHON) -m ruff format --check app tests scripts
	$(PYTHON) -m mypy
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing

build:
	$(PYTHON) scripts/build_release.py

audit:
	$(PYTHON) -m pip_audit

kosit:
	$(PYTHON) scripts/install_kosit.py

docker:
	docker compose up --build

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
