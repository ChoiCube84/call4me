"""Settings from env (dev) or Azure Key Vault (prod)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_agent_number: str = ""
    twilio_fakeline_number: str = ""
    user_verified_caller_id: str = ""
    public_base_url: str = ""

    cartesia_api_key: str = ""
    cartesia_voice_id: str = ""
    typesafe_api_key: str = ""

    anthropic_api_key: str = ""
    azure_foundry_endpoint: str = ""
    azure_foundry_key: str = ""

    database_url: str = "postgresql+asyncpg://call4me:call4me@localhost:5432/call4me"

    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_auth_key_path: str = ""
    apns_topic: str = ""
    apns_auth_key: str = ""  # .p8 contents (Key Vault "apns-auth-key"); beats the path
    apns_use_sandbox: bool = True

    applicationinsights_connection_string: str = ""
    azure_key_vault_url: str = ""

    use_mocks: bool = False


# Key Vault secret names use dashes (e.g. "twilio-auth-token").
_SECRET_FIELDS = (
    "twilio_auth_token",
    "cartesia_api_key",
    "typesafe_api_key",
    "anthropic_api_key",
    "azure_foundry_key",
    "database_url",
    "applicationinsights_connection_string",
    "apns_auth_key",
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
            updates[field] = client.get_secret(field.replace("_", "-")).value or ""
        except Exception:  # noqa: BLE001 - missing secrets fall back to env
            continue
    return settings.model_copy(update={k: v for k, v in updates.items() if v})


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.azure_key_vault_url:
        settings = _load_from_key_vault(settings)
    return settings
