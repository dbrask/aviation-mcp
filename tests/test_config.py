"""OpenSky credentials: environment first, then a 0600 credentials file, else not configured."""

from __future__ import annotations

import os

import pytest

from aviation_mcp.config import CredentialsError, load_opensky_credentials
from aviation_mcp.logs import redact

ENV = {"OPENSKY_CLIENT_ID": "env-id", "OPENSKY_CLIENT_SECRET": "env-secret-value"}


def write_creds(path, text, mode=0o600):
    path.write_text(text)
    os.chmod(path, mode)
    return path


def test_reads_environment_first(tmp_path):
    f = write_creds(tmp_path / "opensky.env", "OPENSKY_CLIENT_ID=file-id\nOPENSKY_CLIENT_SECRET=x\n")
    creds = load_opensky_credentials(env=ENV, path=f)
    assert creds.client_id == "env-id"
    assert creds.client_secret.get_secret_value() == "env-secret-value"


def test_reads_private_credentials_file(tmp_path):
    f = write_creds(
        tmp_path / "opensky.env",
        "# OpenSky API client\nOPENSKY_CLIENT_ID=file-id\nOPENSKY_CLIENT_SECRET='file-secret-value'\n",
    )
    creds = load_opensky_credentials(env={}, path=f)
    assert creds.client_id == "file-id"
    assert creds.client_secret.get_secret_value() == "file-secret-value"


def test_secret_repr_is_masked_and_registered_for_redaction(tmp_path):
    creds = load_opensky_credentials(env=ENV, path=tmp_path / "none.env")
    assert "env-secret-value" not in repr(creds)
    assert "env-secret-value" not in redact("oops env-secret-value")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_refuses_file_readable_by_group_or_others(tmp_path, mode):
    f = write_creds(
        tmp_path / "opensky.env", "OPENSKY_CLIENT_ID=a\nOPENSKY_CLIENT_SECRET=bbbbbb\n", mode
    )
    with pytest.raises(CredentialsError) as exc:
        load_opensky_credentials(env={}, path=f)
    assert "chmod 600" in str(exc.value)
    assert "bbbbbb" not in str(exc.value)


def test_missing_file_and_env_means_not_configured(tmp_path):
    assert load_opensky_credentials(env={}, path=tmp_path / "missing.env") is None


def test_half_configured_is_an_error(tmp_path):
    with pytest.raises(CredentialsError) as exc:
        load_opensky_credentials(env={"OPENSKY_CLIENT_ID": "only-id"}, path=tmp_path / "x.env")
    assert "OPENSKY_CLIENT_SECRET" in str(exc.value)
