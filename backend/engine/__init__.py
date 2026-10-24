"""Call state engine (owned by `call-engine`).

- `states`: the state table and the single `transition()` function.
- `jev_checks`: Jev decisions (state check, menu choice, human confirmation) + mock twin.
- `menu`: menu option parsing, navigation, saved routes.
- `timers`: silence re-check and max-hold timers.
- `call_engine`: `CallEngine`, one per call, wiring the above to the contracts clients.
"""
