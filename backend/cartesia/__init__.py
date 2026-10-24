"""Real phone calls through Cartesia Managed Agents (TELEPHONY=cartesia, the default).

- `api`: thin REST client (calls, agents, tools, webhooks, numbers).
- `spec`: the agent's instructions, tools, voice, and turn settings.
- `setup`: creates/updates those on Cartesia, idempotently.
- `call`: one call (`CartesiaCall`), same surface as `calls.LiveCall`.
- `routes`: the event webhook and our webhook tools.
"""
