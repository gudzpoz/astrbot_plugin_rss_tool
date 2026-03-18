ASTRBOT_ROOT := $(shell git rev-parse --show-toplevel)/../../..
PYTEST_OPTS ?= -v --tb=short

.PHONY: test lint format

test:
	cd $(ASTRBOT_ROOT) && uv run python -m pytest $(CURDIR)/tests/ $(PYTEST_OPTS)

lint:
	cd $(ASTRBOT_ROOT) && uv run ruff check $(CURDIR) --exclude="__pycache__,.Trash*"

format:
	cd $(ASTRBOT_ROOT) && uv run ruff format $(CURDIR) --exclude="__pycache__,.Trash*"
