"""OpenSky API client credentials.

Read from OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET in the environment, or else from a
KEY=VALUE credentials file (default ~/.config/aviation-mcp/opensky.env). The file is
refused if group or others can read it. Credentials never live in the repo.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from aviation_mcp.logs import register_secret

DEFAULT_CREDENTIALS_FILE = Path("~/.config/aviation-mcp/opensky.env").expanduser()
ID_KEY = "OPENSKY_CLIENT_ID"
SECRET_KEY = "OPENSKY_CLIENT_SECRET"


class CredentialsError(Exception):
    """Misconfigured credentials. Messages name keys and paths, never values."""


@dataclass(frozen=True)
class OpenSkyCredentials:
    client_id: str
    client_secret: SecretStr  # repr is '**********'


def _parse_env_file(path: Path) -> dict[str, str]:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialsError(
            f"{path} is readable by group or others; run: chmod 600 {path}"
        )
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_opensky_credentials(
    env: Mapping[str, str] | None = None, path: Path | None = None
) -> OpenSkyCredentials | None:
    """Return credentials, or None when OpenSky is simply not configured."""
    env = os.environ if env is None else env
    path = DEFAULT_CREDENTIALS_FILE if path is None else Path(path).expanduser()

    source: Mapping[str, str] = env
    if not (env.get(ID_KEY) or env.get(SECRET_KEY)):
        if not path.exists():
            return None
        source = _parse_env_file(path)

    client_id = source.get(ID_KEY, "").strip()
    secret = source.get(SECRET_KEY, "").strip()
    missing = [k for k, v in ((ID_KEY, client_id), (SECRET_KEY, secret)) if not v]
    if len(missing) == 2:
        return None
    if missing:
        raise CredentialsError(f"OpenSky credentials are incomplete: {missing[0]} is not set")
    register_secret(secret)
    return OpenSkyCredentials(client_id=client_id, client_secret=SecretStr(secret))
