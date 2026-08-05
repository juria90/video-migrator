# Video Migrator — developer task runner.
#
# Every recipe goes through `uv run`, so no target requires a pre-activated
# virtualenv. Override the tool with `make test UV=uvx` if needed.

UV ?= uv
PKG := video_migrator
SRC := src/$(PKG)

.DEFAULT_GOAL := help

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Environment ------------------------------------------------------------

.PHONY: install
install:  ## Sync the venv with the dev dependency group (default)
	$(UV) sync

.PHONY: install-browser
install-browser:  ## Sync with the [browser] extra and fetch Chromium
	$(UV) sync --extra browser
	$(UV) run playwright install chromium

.PHONY: install-prod
install-prod:  ## Sync runtime dependencies only, no dev group
	$(UV) sync --no-default-groups

.PHONY: lock
lock:  ## Refresh uv.lock
	$(UV) lock

# --- Tests ------------------------------------------------------------------

.PHONY: test
test:  ## Run the unit tests
	$(UV) run pytest tests/

.PHONY: doctest
doctest:  ## Run only the doctests in src/
	$(UV) run pytest $(SRC)/

.PHONY: test-all
test-all:  ## Run unit tests and doctests (the pyproject testpaths default)
	$(UV) run pytest

.PHONY: coverage
coverage:  ## Run tests with an HTML + terminal coverage report
	$(UV) run pytest tests/ --cov=$(PKG) --cov-report=html --cov-report=term-missing
	@echo "HTML report: htmlcov/index.html"

# --- Lint / format ----------------------------------------------------------

.PHONY: lint
lint: no-real-data  ## Lint with ruff, and check no real-site data is committable
	$(UV) run ruff check .

.PHONY: no-real-data
no-real-data:  ## Check committable files for data identifying the real site
	$(UV) run python tools/check_no_real_data.py

.PHONY: lint-fix
lint-fix:  ## Lint with ruff and apply safe autofixes
	$(UV) run ruff check --fix .

.PHONY: format
format:  ## Format with ruff
	$(UV) run ruff format .

.PHONY: format-check
format-check:  ## Verify formatting without writing files
	$(UV) run ruff format --check .

.PHONY: check
check: lint format-check test-all  ## Everything CI should gate on

# --- Packaging / housekeeping ----------------------------------------------

.PHONY: build
build:  ## Build the sdist and wheel into dist/
	$(UV) build

.PHONY: clean
clean:  ## Remove caches, coverage output, and build artifacts
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage coverage.xml dist build
	find . -path ./.venv -prune -o -name __pycache__ -type d -print -exec rm -rf {} +
	find . -path ./.venv -prune -o -name '*.egg-info' -type d -print -exec rm -rf {} +
