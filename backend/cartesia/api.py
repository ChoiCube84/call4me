"""Thin client for the Cartesia Agents REST API (calls, agents, tools, webhooks, numbers).

Docs: https://docs.cartesia.ai/agents/introduction.md and
https://docs.cartesia.ai/line/integrations/telephony/outbound-dialing.md
"""

from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://api.cartesia.ai"
API_VERSION = "2026-08-14"


class CartesiaApiError(Exception):
    def __init__(self, status: int, body: str, what: str) -> None:
        super().__init__(f"Cartesia {what} failed ({status}): {body[:300]}")
        self.status = status
        self.body = body


class CartesiaAgentsApi:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Cartesia-Version": API_VERSION},
            timeout=20.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, what: str, json: Any = None) -> Any:
        r = await self._http.request(method, path, json=json)
        if r.status_code >= 400:
            raise CartesiaApiError(r.status_code, r.text, what)
        return r.json() if r.content else {}

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        items = data.get("data", data) if isinstance(data, dict) else data
        return list(items) if isinstance(items, list) else []

    # ---- calls ---------------------------------------------------------------

    async def start_call(
        self,
        *,
        agent_id: str,
        from_number_id: str,
        to_number: str,
        dynamic_variables: dict[str, str | int | float | bool],
        ringing_timeout_seconds: int = 60,
        max_call_duration_minutes: int | None = None,
    ) -> str:
        """Dial one number; returns the `agent_call_id`."""
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "from_number_id": from_number_id,
            "ringing_timeout_seconds": ringing_timeout_seconds,
            "outbound_calls": [{"to_number": to_number, "dynamic_variables": dynamic_variables}],
        }
        if max_call_duration_minutes:
            body["max_call_duration_minutes"] = max_call_duration_minutes
        data = await self._req("POST", "/agents/calls", "start call", body)
        (call,) = data.get("calls") or [{}]
        if not call.get("agent_call_id"):
            raise CartesiaApiError(200, str(call.get("error") or data), "start call")
        return str(call["agent_call_id"])

    async def get_call(self, call_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/agents/calls/{call_id}", "get call")

    async def cancel_call(self, call_id: str) -> None:
        await self._req("POST", f"/agents/calls/{call_id}/cancel", "cancel call")

    async def delete_call_data(self, call_id: str) -> None:
        """Erase Cartesia's copy of the transcript and audio (after the call ended)."""
        await self._req("DELETE", f"/agents/calls/{call_id}", "delete call data")

    # ---- setup: agents, tools, webhooks, numbers -------------------------------

    async def list_agents(self) -> list[dict[str, Any]]:
        return self._items(await self._req("GET", "/v1/agents", "list agents"))

    async def create_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._req("POST", "/v1/agents", "create agent", body)

    async def update_agent(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._req("PATCH", f"/v1/agents/{agent_id}", "update agent", body)

    async def list_tools(self) -> list[dict[str, Any]]:
        return self._items(await self._req("GET", "/v1/agents/tools", "list tools"))

    async def create_tool(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._req("POST", "/v1/agents/tools", "create tool", body)

    async def update_tool(self, tool_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._req("PATCH", f"/v1/agents/tools/{tool_id}", "update tool", body)

    async def list_webhooks(self) -> list[dict[str, Any]]:
        return self._items(await self._req("GET", "/agents/webhooks", "list webhooks"))

    async def create_webhook(self, url: str, secret: str, name: str) -> dict[str, Any]:
        body = {"url": url, "secret": secret, "display_name": name}
        return await self._req("POST", "/agents/webhooks", "create webhook", body)

    async def update_webhook(self, webhook_id: str, url: str, secret: str) -> dict[str, Any]:
        body = {"url": url, "secret": secret}
        return await self._req("PATCH", f"/agents/webhooks/{webhook_id}", "update webhook", body)

    async def list_numbers(self) -> list[dict[str, Any]]:
        return self._items(await self._req("GET", "/agents/phone-numbers", "list numbers"))

    async def provision_number(self, label: str) -> dict[str, Any]:
        return await self._req(
            "POST", "/agents/phone-numbers/provision", "provision number", {"label": label}
        )
