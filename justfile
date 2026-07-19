set shell := ["bash", "-cu"]

venv_bin := env_var_or_default("BUGYI_CHOPS_VENV_BIN", ".venv/bin")

install:
    @if [ -n "${BUGYI_CHOPS_VENV_BIN:-}" ]; then \
        uv pip install --python "${BUGYI_CHOPS_VENV_BIN}/python" --no-deps -e .; \
    else \
        uv sync --group dev; \
    fi

fmt:
    {{ venv_bin }}/ruff format .
    {{ venv_bin }}/ruff check --fix .

lint:
    {{ venv_bin }}/ruff format --check .
    {{ venv_bin }}/ruff check .
    {{ venv_bin }}/mypy

test:
    {{ venv_bin }}/pytest

build:
    {{ venv_bin }}/python -m build
    {{ venv_bin }}/twine check dist/*

check: lint test build
