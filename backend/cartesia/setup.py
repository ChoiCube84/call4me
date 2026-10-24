"""Create or update everything call4me needs on Cartesia, idempotently.

Resources are found by name (agent "call4me", tools "call4me_*", webhook
"call4me-events", number labelled "call4me outbound"), so this is safe to run on
every server start and again whenever PUBLIC_BASE_URL changes (e.g. a new tunnel).

    uv run python -m backend.cartesia.setup     # prints the agent, number, and URLs
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from dataclasses import dataclass
from typing import Any

from backend.cartesia import spec
from backend.cartesia.api import CartesiaAgentsApi

log = logging.getLogger("call4me.cartesia")


@dataclass(frozen=True)
class CartesiaSetup:
    agent_id: str
    from_number_id: str
    from_number: str


_current: CartesiaSetup | None = None


def current() -> CartesiaSetup | None:
    return _current


def shared_secret(settings: Any) -> str:
    """Secret Cartesia sends back on webhooks and tool calls. Configurable; by default
    derived from the API key so it is stable without extra setup."""
    if settings.cartesia_webhook_secret:
        return str(settings.cartesia_webhook_secret)
    digest = hashlib.sha256(f"call4me:{settings.cartesia_api_key}".encode()).hexdigest()
    return digest[:40]


def _check(settings: Any) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    if not settings.cartesia_api_key:
        raise RuntimeError("CARTESIA_API_KEY is not set")
    if not settings.cartesia_voice_id:
        raise RuntimeError("CARTESIA_VOICE_ID is not set")
    if not base.startswith("https://"):
        raise RuntimeError(
            "PUBLIC_BASE_URL must be the server's public https URL (Cartesia calls our "
            "webhook and tools there), e.g. a cloudflared tunnel or the Azure URL"
        )
    return base


async def ensure(settings: Any, api: CartesiaAgentsApi | None = None) -> CartesiaSetup:
    global _current
    base = _check(settings)
    secret = shared_secret(settings)
    own = api is None
    api = api or CartesiaAgentsApi(settings.cartesia_api_key)
    try:
        webhook_id = await _ensure_webhook(api, f"{base}/cartesia/webhook", secret)
        tool_ids = await _ensure_tools(api, spec.tool_specs(base, secret))
        config = spec.agent_config(
            voice_id=settings.cartesia_voice_id,
            model_id=settings.cartesia_agent_model,
            tool_ids=tool_ids,
        )
        agent_id = await _ensure_agent(api, config, webhook_id)
        number_id, number = await _ensure_number(api)
    finally:
        if own:
            await api.aclose()
    _current = CartesiaSetup(agent_id=agent_id, from_number_id=number_id, from_number=number)
    log.info("Cartesia ready: agent %s, calling from %s", agent_id, number)
    return _current


async def _ensure_webhook(api: CartesiaAgentsApi, url: str, secret: str) -> str:
    for hook in await api.list_webhooks():
        if hook.get("display_name") == spec.WEBHOOK_NAME:
            await api.update_webhook(hook["id"], url, secret)
            return str(hook["id"])
    return str((await api.create_webhook(url, secret, spec.WEBHOOK_NAME))["id"])


async def _ensure_tools(api: CartesiaAgentsApi, specs: list[dict[str, Any]]) -> list[str]:
    existing = {t.get("name"): t.get("id") for t in await api.list_tools()}
    ids = []
    for body in specs:
        tool_id = existing.get(body["name"])
        if tool_id:
            update = {k: v for k, v in body.items() if k != "type"}
            await api.update_tool(tool_id, update)
        else:
            tool_id = (await api.create_tool(body))["id"]
        ids.append(str(tool_id))
    return ids


async def _ensure_agent(api: CartesiaAgentsApi, config: dict[str, Any], webhook_id: str) -> str:
    for agent in await api.list_agents():
        if agent.get("name") == spec.AGENT_NAME:
            agent_id = str(agent["id"])
            break
    else:
        agent_id = str((await api.create_agent({"name": spec.AGENT_NAME, "config": config}))["id"])
    # The call webhook is linked through the agent's config (event_webhook_id).
    config = {**config, "event_webhook_id": webhook_id}
    # No version_description: Cartesia rejects it when the config is unchanged (restarts).
    await api.update_agent(agent_id, {"config": config})
    return agent_id


async def _ensure_number(api: CartesiaAgentsApi) -> tuple[str, str]:
    numbers = await api.list_numbers()
    for n in numbers:
        if n.get("label") == spec.NUMBER_LABEL:
            return str(n["id"]), str(n.get("number", ""))
    if numbers:  # any number on the account can place outbound calls
        n = numbers[0]
        return str(n["id"]), str(n.get("number", ""))
    n = await api.provision_number(spec.NUMBER_LABEL)
    return str(n["id"]), str(n.get("number", ""))


def main() -> None:
    from backend.config import get_settings

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    try:
        s = asyncio.run(ensure(settings))
    except Exception as e:  # noqa: BLE001 - a CLI: print the reason, exit non-zero
        print(f"Cartesia setup failed: {e}", file=sys.stderr)
        sys.exit(1)
    base = settings.public_base_url.rstrip("/")
    print(f"agent:   {s.agent_id} (model {settings.cartesia_agent_model})")
    print(f"number:  {s.from_number} ({s.from_number_id})")
    print(f"webhook: {base}/cartesia/webhook")
    print(f"tools:   {base}/cartesia/tools/<name>")


if __name__ == "__main__":
    main()
