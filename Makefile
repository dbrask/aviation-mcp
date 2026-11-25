.PHONY: test smoke lint live-metar

# Network is blocked for the whole test run by pytest-socket
# (--disable-socket --allow-unix-socket in pyproject.toml addopts).
test:
	uv run pytest

# Start the server over stdio as a subprocess, connect with the SDK client,
# print the tool names and input schemas.
smoke:
	uv run python scripts/smoke.py

# One real call through the stdio server: get_metar for ESGG (aviationweather.gov, no auth).
live-metar:
	uv run python scripts/smoke.py --call-metar ESGG

lint:
	uv run ruff check src tests scripts
