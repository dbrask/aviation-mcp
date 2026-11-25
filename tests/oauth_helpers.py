"""Test identity provider: RSA keys generated per test run, a JWKS, and a token minter.

Nothing here talks to a network: the JWKS, discovery and token endpoints are served by
respx in the tests that need them.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "http://localhost:8180/realms/aviation-demo"
RESOURCE = "http://localhost:8000/mcp"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
LOGBOOK_URL = "http://localhost:8001"

SUB_A = "0b7f6a1e-0000-4000-8000-00000000000a"
SUB_B = "0b7f6a1e-0000-4000-8000-00000000000b"


class TestIdp:
    __test__ = False  # not a pytest class

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "sig-key-1"
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self, *, extra: list[dict] | None = None) -> dict:
        pub = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        pub.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        # Keycloak's realm JWKS also holds an encryption key; it must never verify a token.
        enc = json.loads(RSAAlgorithm.to_jwk(self.other_key.public_key()))
        enc.update({"kid": "enc-key-1", "use": "enc", "alg": "RSA-OAEP"})
        return {"keys": [pub, enc, *(extra or [])]}

    def claims(self, **overrides: Any) -> dict:
        now = int(time.time())
        base = {
            "iss": ISSUER,
            "sub": SUB_A,
            "aud": [RESOURCE, "aviation-mcp"],
            "azp": "aviation-demo-cli",
            "scope": "mcp:tools mcp:logbook",
            "iat": now,
            "exp": now + 300,
            "jti": str(uuid.uuid4()),
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def mint(self, *, key=None, kid: str | None = "default", alg: str = "RS256", **claims: Any):
        headers = {} if kid is None else {"kid": self.kid if kid == "default" else kid}
        return jwt.encode(
            self.claims(**claims), key if key is not None else self.key, algorithm=alg,
            headers=headers,
        )
