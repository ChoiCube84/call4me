"""Settings from env (dev) or Azure Key Vault (prod)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The server's public https URL. Cartesia calls our webhook and tools here.
    public_base_url: str = ""

    cartesia_api_key: str = ""
    cartesia_voice_id: str = ""
    # The model that runs the phone agent on Cartesia (GET /v1/agents/models).
    cartesia_agent_model: str = "claude-haiku-5.5"
    # Shared secret for Cartesia's webhook and tool calls; derived from the API key if unset.
    cartesia_webhook_secret: str = ""
    typesafe_api_key: str = ""

    anthropic_api_key: str = ""
    azure_foundry_endpoint: str = ""
    azure_foundry_key: str = ""

    database_url: str = "postgresql+asyncpg://call4me:call4me@localhost:5432/call4me"
    storage_backend: Literal["sql", "convex"] = "sql"
    convex_url: str = ""
    convex_backend_token: str = ""

    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_auth_key_path: str = ""
    apns_topic: str = ""
    apns_auth_key: str = ""  # .p8 contents (Key Vault "apns-auth-key"); beats the path
    apns_use_sandbox: bool = True

    # Required on every API call when set (backend/auth.py). Always set on Azure.
    api_token: str = ""

    applicationinsights_connection_string: str = ""
    azure_key_vault_url: str = ""

    use_mocks: bool = False
    # Who places calls: "cartesia" (a Cartesia agent dials a real number), "phone" (our
    # own pipeline; a person on the /phone page is the rep), or "mock" (scripted bot
    # rep). Empty means "mock" with USE_MOCKS, else "cartesia".
    telephony: str = ""

    @property
    def telephony_mode(self) -> str:
        return self.telephony.lower() or ("mock" if self.use_mocks else "cartesia")

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        # Pasted keys and Key Vault values often end in a newline, which breaks auth headers.
        return v.strip() if isinstance(v, str) else v


# Key Vault secret names use dashes (e.g. "cartesia-api-key").
_SECRET_FIELDS = (
    "cartesia_api_key",
    "cartesia_webhook_secret",
    "typesafe_api_key",
    "anthropic_api_key",
    "azure_foundry_key",
    "database_url",
    "convex_backend_token",
    "applicationinsights_connection_string",
    "apns_auth_key",
    "api_token",
)


def _load_from_key_vault(settings: Settings) -> Settings:
    # Imported lazily so dev doesn't need the azure extra installed.
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(
        vault_url=settings.azure_key_vault_url, credential=DefaultAzureCredential()
    )
    updates: dict[str, str] = {}
    for field in _SECRET_FIELDS:
        try:
            updates[field] = (client.get_secret(field.replace("_", "-")).value or "").strip()
        except Exception:  # noqa: BLE001 - missing secrets fall back to env
            continue
    return settings.model_copy(update={k: v for k, v in updates.items() if v})


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.azure_key_vault_url:
        settings = _load_from_key_vault(settings)
    return settings
