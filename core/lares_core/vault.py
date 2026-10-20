"""The key vault: API keys, passwords and tokens, encrypted on the data partition.

Values are never shown again in the Atrium and never written to a log; a room only gets the keys its rights name.
"""

import json
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import storage
from .manifest import NAME

# API keys, secrets and tokens never contain spaces: line breaks or quotes from copying are removed.
TOKEN_NAME = re.compile(r"(_key|_secret|_token)$")


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, directory: Path):
        self.key_file, self.file = directory / "vault.key", directory / "vault.bin"
        if not self.key_file.exists():
            if self.file.exists():
                raise VaultError(f"{self.key_file} fehlt – ohne ihn lässt sich der Tresor nicht öffnen")
            storage.write(self.key_file, Fernet.generate_key())
        self.fernet = Fernet(self.key_file.read_bytes().strip())
        self.data: dict[str, str] = {}
        if self.file.exists():
            try:
                self.data = json.loads(self.fernet.decrypt(self.file.read_bytes()))
            except (InvalidToken, ValueError):
                raise VaultError("Tresor lässt sich mit diesem Schlüssel nicht öffnen") from None

    def _save(self) -> None:
        storage.write(self.file, self.fernet.encrypt(json.dumps(self.data).encode()))

    def names(self) -> list[str]:
        return sorted(self.data)

    def get(self, name: str) -> str:
        return self.data.get(name, "")

    def set(self, name: str, value: str) -> None:
        if not NAME.match(name):
            raise ValueError("Name: Kleinbuchstaben, Ziffern und _")
        value = str(value).strip()
        if TOKEN_NAME.search(name):
            value = re.sub(r"\s+", "", value).strip("\"'`")
        if value:
            self.data[name] = value
        else:
            self.data.pop(name, None)
        self._save()

    def delete(self, name: str) -> None:
        if self.data.pop(name, None) is not None:
            self._save()
