.PHONY: test smoke lint live-metar demo-obo

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
	uv run ruff check src tests scripts demo

# On-behalf-of demo (needs Docker): Keycloak in Compose, the logbook API and the MCP server
# over HTTP on 127.0.0.1, PASS/FAIL per check. The stack is always torn down (also on
# failure or Ctrl-C). Users and secrets are throwaway demo values (demo/keycloak/).
DEMO_COMPOSE = docker compose -f demo/compose.yaml
demo-obo:
	@trap '$(DEMO_COMPOSE) down --volumes --remove-orphans >/dev/null 2>&1; echo "demo: stack torn down"' EXIT; \
	$(DEMO_COMPOSE) up -d && uv run python -m demo.obo_demo
