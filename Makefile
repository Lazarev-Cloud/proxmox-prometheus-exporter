PYTHON ?= python3
VENV   ?= .venv
BIN     = $(VENV)/bin

.PHONY: help dev lint fmt test docs dist check clean

help:  ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-8s %s\n", $$1, $$2}'

dev:  ## Create .venv with the pinned development tools
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --require-hashes -r requirements-dev.txt
	$(BIN)/pip install --no-deps -e .

fmt:  ## Format code
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

lint:  ## Lint Python, types, shell scripts and workflows
	$(BIN)/ruff format --check .
	$(BIN)/ruff check .
	$(BIN)/mypy
	$(BIN)/shellcheck install.sh tests/integration/installer.sh
	$(BIN)/actionlint

test:  ## Run the unit tests with coverage
	$(BIN)/pytest --cov

docs:  ## Regenerate docs/metrics.md and the generated dashboard
	$(BIN)/python tools/gen_metrics_doc.py
	$(BIN)/python tools/gen_health_dashboard.py

dist:  ## Build the zipapp, release tarball, wheel and SHA256SUMS into dist/
	rm -rf dist
	$(BIN)/python -m build --sdist --wheel --outdir dist/
	cp install.sh dist/
	$(BIN)/python tools/build_dist.py

check: lint test  ## Everything CI runs locally (except the installer e2e test)
	$(BIN)/python tools/gen_metrics_doc.py --check
	$(BIN)/python tools/gen_health_dashboard.py --check

clean:  ## Remove build and test artefacts
	rm -rf build dist .coverage coverage.xml htmlcov .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -prune -exec rm -rf {} +
