"""Operating-system credential storage with a non-persistent fallback."""

import hashlib
from dataclasses import dataclass, field
from uuid import UUID

import keyring
from keyring.errors import KeyringError

_SERVICE_NAME = "OpenFotos Desktop"


@dataclass
class RefreshTokenStore:
    """Never falls back to plaintext files when the platform keyring is unavailable."""

    _memory: dict[str, str] = field(default_factory=dict)
    persistence_warning: str | None = None

    def save(self, server_url: str, installation_id: UUID, refresh_token: str) -> bool:
        account = self._account(server_url, installation_id)
        self._memory[account] = refresh_token
        try:
            keyring.set_password(_SERVICE_NAME, account, refresh_token)
        except KeyringError:
            self.persistence_warning = (
                "The operating-system credential store is unavailable. "
                "This session will require sign-in after OpenFotos closes."
            )
            return False
        self.persistence_warning = None
        return True

    def load(self, server_url: str, installation_id: UUID) -> str | None:
        account = self._account(server_url, installation_id)
        if account in self._memory:
            return self._memory[account]
        try:
            token = keyring.get_password(_SERVICE_NAME, account)
        except KeyringError:
            self.persistence_warning = (
                "The operating-system credential store is unavailable. Sign in again to continue."
            )
            return None
        self.persistence_warning = None
        if token:
            self._memory[account] = token
        return token

    def delete(self, server_url: str, installation_id: UUID) -> None:
        account = self._account(server_url, installation_id)
        self._memory.pop(account, None)
        try:
            keyring.delete_password(_SERVICE_NAME, account)
        except KeyringError:
            return

    @staticmethod
    def _account(server_url: str, installation_id: UUID) -> str:
        server_digest = hashlib.sha256(server_url.encode()).hexdigest()[:24]
        return f"{server_digest}:{installation_id}"
