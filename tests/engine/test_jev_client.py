"""JevDecisionClient reads the chosen option's probability, not `confidence`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from backend.engine.jev_checks import JevDecisionClient


class FakeTypeSafe:
    async def system_one(self, state: dict[str, Any], questions: dict[str, Any]) -> Any:
        (q,) = questions
        answer = SimpleNamespace(
            choice="human", confidence=0.36, probabilities={"human": 0.68, "recording": 0.32}
        )
        return SimpleNamespace(choices={q: answer})


async def test_probability_is_the_chosen_options_probability() -> None:
    client = JevDecisionClient(api_key="k")
    client._client = FakeTypeSafe()
    d = await client.choose("human_confirmation", ["human", "recording"], {})
    assert d.choice == "human"
    assert d.probability == 0.68
    assert d.alternatives == {"human": 0.68, "recording": 0.32}
