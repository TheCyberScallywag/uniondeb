"""Field-level encryption for the few properties that hold raw, unvalidated input.

A row that fails ingest is kept so a reviewer can fix it (design §5.1), but it has not been through
the contract, so nothing is known about what it contains. It is stored as a Fernet token, and only
the review API holds the key (``UNIONDEB_FIELD_KEY``). There is no default key: a service that needs
one and has none fails at start-up rather than writing plaintext.
"""

import json
import os
from collections.abc import Mapping
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "UNIONDEB_FIELD_KEY"


class CipherError(RuntimeError):
    pass


class FieldCipher:
    def __init__(self, key: str | bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise CipherError(f"{ENV_KEY} is not a valid Fernet key") from exc

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FieldCipher:
        key = (os.environ if env is None else env).get(ENV_KEY)
        if not key:
            raise CipherError(f"set {ENV_KEY} (generate one with `uniondeb store keygen`)")
        return cls(key)

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise CipherError("field could not be decrypted with this key") from exc

    def encrypt_json(self, value: Any) -> str:
        return self.encrypt(json.dumps(value, default=str, ensure_ascii=False))

    def decrypt_json(self, token: str) -> Any:
        return json.loads(self.decrypt(token))
