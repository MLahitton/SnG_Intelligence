from pydantic import SecretStr

from app.agent.errors import SensitiveContent


class CredentialGuard:
    """Request-scoped boundary check; never passed into the model adapter."""

    def __init__(self, *secrets: SecretStr) -> None:
        self._secrets = secrets

    def check(self, content: str) -> None:
        if any(s.get_secret_value() and s.get_secret_value() in content for s in self._secrets):
            raise SensitiveContent()

    def check_bytes(self, content: bytes) -> None:
        if any(
            s.get_secret_value() and s.get_secret_value().encode("utf-8") in content
            for s in self._secrets
        ):
            raise SensitiveContent()
