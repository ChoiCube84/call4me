from __future__ import annotations

import json

import httpx
import pytest

from backend.audit import log as audit
from backend.contracts import LiveMessage
from backend.db.convex_client import ConvexClient, StorageConflict
from backend.db.models import CallSessionRow, UserRequestRow
from backend.db.store import ConvexStore, deserialize, serialize
from backend.notify.convex_live import ConvexLiveWriter


class FakeClient:
    def __init__(self):
        self.calls = []
        self.row = None

    async def call(self, kind, path, **args):
        self.calls.append((kind, path, args))
        return self.row if path == "storage:get" else None


async def test_convex_store_stages_one_atomic_commit_without_sql():
    client = FakeClient()
    store = ConvexStore(client)
    row = CallSessionRow(
        user_id="u",
        company="test",
        phone_number="+15550100100",
        goal="refund",
        disclosure_text="AI",
        state="DIALING",
    )
    store.add(row)
    await store.flush()
    assert row.id and row.started_at and row.limits_json == {}
    assert not client.calls
    req = UserRequestRow(session_id=row.id, question="PIN?", sensitivity="medium", status="pending")
    store.add(req)
    audit.register_secret(row.id, "secret4821")
    await audit.append(store, actor="test", action="request", reason="secret4821")
    await store.commit()
    assert len(client.calls) == 1
    writes = client.calls[0][2]["writes"]
    assert len(writes) == 3
    assert "secret4821" not in json.dumps(writes)
    assert "answer" not in writes[1]["value"]
    assert all(w["expected"] is None for w in writes)
    await store.commit()
    assert len(client.calls) == 1  # no accidental rewrites of the audit entry


async def test_convex_store_preserves_datetime_and_optimistic_snapshot():
    client = FakeClient()
    store = ConvexStore(client)
    row = CallSessionRow(
        user_id="u",
        company="test",
        phone_number="+15550100100",
        goal="refund",
        disclosure_text="AI",
        state="DIALING",
    )
    store.add(row)
    original = serialize(row)
    client.row = original
    store = ConvexStore(client)
    loaded = await store.get(CallSessionRow, row.id)
    assert loaded is not None
    assert serialize(deserialize(CallSessionRow, original)) == original
    loaded.state = "HOLD"
    await store.commit()
    write = client.calls[-1][2]["writes"][0]
    assert write["expected"]["state"] == "DIALING"
    assert write["value"]["state"] == "HOLD"


async def test_http_client_surfaces_conflicts_without_echoing_sensitive_errors():
    async def handler(request):
        body = json.loads(request.content)
        assert body["args"]["token"] == "backend-secret"
        return httpx.Response(
            200, json={"status": "error", "errorData": "CONFLICT", "errorMessage": "sensitive-body"}
        )

    client = ConvexClient(
        "https://example.convex.cloud", "backend-secret", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(StorageConflict, match="Record changed") as exc:
            await client.call("mutation", "storage:commit", writes=[])
        assert "sensitive-body" not in str(exc.value)
    finally:
        await client.close()


async def test_replay_writer_redacts_before_enqueue():
    audit.register_secret("call", "private4821")
    writer = ConvexLiveWriter()
    writer.publish(LiveMessage(type="caption", session_id="call", data={"text": "my private4821"}))
    message = writer.queue.get_nowait()
    assert "private4821" not in json.dumps(message)
    assert message["data"]["text"] == "my [REDACTED]"
    assert message["id"]


async def test_nested_record_edits_do_not_mutate_the_optimistic_snapshot():
    client = FakeClient()
    store = ConvexStore(client)
    row = CallSessionRow(
        user_id="u",
        company="test",
        phone_number="+15550100100",
        goal="refund",
        disclosure_text="AI",
        state="DIALING",
        limits_json={"max_spend_usd": 0},
    )
    store.add(row)
    await store.commit()
    row.limits_json["max_spend_usd"] = 10
    await store.commit()
    write = client.calls[-1][2]["writes"][0]
    assert write["expected"]["limits_json"] == {"max_spend_usd": 0}
    assert write["value"]["limits_json"] == {"max_spend_usd": 10}
