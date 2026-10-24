"""Run a synthetic full call against the configured Convex deployment.

Run from the repo root: uv run python -m scripts.smoke_convex
Never calls paid services or real phone numbers. Writes synthetic test records.
"""

from __future__ import annotations

import asyncio
import json
import os

# Override real-service settings even when .env contains production credentials.
os.environ["USE_MOCKS"] = "true"
os.environ["TELEPHONY"] = "mock"
os.environ["STORAGE_BACKEND"] = "convex"


async def main() -> None:
    import httpx

    from backend import calls
    from backend.config import get_settings
    from backend.main import app
    from backend.notify.live import hub
    from backend.telephony import set_telephony
    from backend.telephony.mock_client import MockTelephonyClient

    settings = get_settings()
    set_telephony(MockTelephonyClient(speed=3, hold_seconds=(2, 2), callback_offer=False))
    app.state.start_calls = True
    pin = "smoke-secret-4821"
    order = "synthetic-order-private-9612"
    messages = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://smoke",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        ) as client:
            result = await client.post(
                "/calls",
                json={
                    "user_id": "convex-smoke-test",
                    "company": "Synthetic support",
                    "phone_number": "+15550100100",
                    "goal": "Refund damaged headphones",
                    "disclosure_text": "Hi, I am an AI assistant calling for Sam.",
                    "known_info": [{"label": "Customer email", "value": order}],
                },
            )
            result.raise_for_status()
            call_id = result.json()["id"]
            queue = hub.subscribe(call_id, replay=True)
            answered = 0
            try:
                async with asyncio.timeout(240):
                    while True:
                        msg = await queue.get()
                        messages.append(msg.model_dump(mode="json"))
                        if msg.type == "ask_user":
                            response = await client.post(
                                f"/requests/{msg.data['request_id']}/answer", json={"answer": pin}
                            )
                            response.raise_for_status()
                            assert response.json()["detail"] == "delivered"
                            answered += 1
                            duplicate = await client.post(
                                f"/requests/{msg.data['request_id']}/answer", json={"answer": pin}
                            )
                            assert duplicate.status_code == 409
                        if msg.type == "ended" and "minutes_saved" in msg.data:
                            break
                live = calls.get_call(call_id)
                if live:
                    await asyncio.wait_for(live.finished.wait(), 20)
                detail = (await client.get(f"/calls/{call_id}")).json()
                audit = (await client.get(f"/calls/{call_id}/audit")).json()
                assert detail["session"]["state"] == "ENDED", detail["session"]["state"]
                assert answered > 0, "ask_user was not exercised"
                payload = json.dumps([detail, audit, messages])
                assert pin not in payload and order not in payload
            finally:
                hub.unsubscribe(call_id, queue)
    # Lifespan exit drains live writes, then close/reconnect using a fresh HTTP client.
    async with httpx.AsyncClient() as client:
        response = await client.post(
            settings.convex_url + "/api/query",
            json={
                "path": "live:recent",
                "format": "json",
                "args": {"accessCode": settings.api_token, "call_id": call_id},
            },
        )
        response.raise_for_status()
        replay = response.json()
        assert replay["status"] == "success", "Replay query failed"
        replay = replay["value"]
        assert any(m["type"] == "ended" for m in replay)
        assert pin not in json.dumps(replay) and order not in json.dumps(replay)
        sequences = [m["sequence"] for m in replay]
        assert sequences == sorted(set(sequences))
        denied = await client.post(
            settings.convex_url + "/api/query",
            json={
                "path": "live:recent",
                "format": "json",
                "args": {"accessCode": "wrong", "call_id": call_id},
            },
        )
        assert denied.json()["status"] == "error"
    print(
        json.dumps(
            {
                "status": "passed",
                "call_id": call_id,
                "state": "ENDED",
                "answered": answered,
                "timeline_events": len(detail["timeline"]),
                "audit_rows": len(audit),
                "replayed_messages": len(replay),
                "secrets_absent": True,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
