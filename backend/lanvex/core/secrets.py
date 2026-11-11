"""Encryption at rest for credentials Lanvex holds (SSH keys and passwords).

The key comes from LANVEX_SECRET_KEY (any string of 32+ characters; `lanvex generate-secret-key`
prints a suitable one). A Fernet key is derived from it with HKDF-SHA256, so the same secret can
also serve other purposes later under a different `info` label. LANVEX_SECRET_KEY_FALLBACKS lists
previous keys during rotation; `lanvex rotate-credentials` re-encrypts everything with the current key.
"""

import base64
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from lanvex.core.config import Settings
from lanvex.core.errors import LanvexError, NotConfigured

MIN_SECRET_LENGTH = 32
_INFO = b"lanvex/credentials/v1"


class VaultError(LanvexError):
    status_code, code = 500, "credential_decryption_failed"


def generate_secret_key() -> str:
    return secrets.token_urlsafe(48)


def _derive(secret: str) -> Fernet:
    if len(secret) < MIN_SECRET_LENGTH:
        raise NotConfigured(
            f"LANVEX_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters.", code="secret_key_too_short"
        )
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(raw))


class Vault:
    def __init__(self, primary: str, fallbacks: list[str] | None = None):
        self._fernet = MultiFernet([_derive(primary), *(_derive(f) for f in fallbacks or [])])

    @classmethod
    def from_settings(cls, settings: Settings) -> "Vault":
        if settings.secret_key is None:
            raise NotConfigured(
                "Set LANVEX_SECRET_KEY to store server credentials. Generate one with `lanvex generate-secret-key`.",
                code="secret_key_not_configured",
            )
        return cls(
            settings.secret_key.get_secret_value(),
            [f.get_secret_value() for f in settings.secret_key_fallbacks],
        )

    def encrypt(self, data: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(token.encode()))
        except InvalidToken as e:
            raise VaultError(
                "Stored credentials could not be decrypted. LANVEX_SECRET_KEY may have changed; "
                "add the previous key to LANVEX_SECRET_KEY_FALLBACKS."
            ) from e

    def rotate(self, token: str) -> str:
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken as e:
            raise VaultError("Stored credentials could not be decrypted with any configured key.") from e
