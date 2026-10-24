"""Call timers (CLAUDE.md section 6).

- Silence longer than `silence_recheck_s` triggers an immediate state re-check
  (once per silent stretch).
- Total time waiting (HOLD / RECORDED_MESSAGE) beyond the user's max hold
  triggers one "still on hold, keep waiting?" alert.

The menu-loop counter lives in `MenuNavigator` because it needs the parsed
menu to know a prompt repeated. Time comes from an injected monotonic clock so
tests never sleep.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class TimerEvent(enum.StrEnum):
    SILENCE_RECHECK = "silence_recheck"
    MAX_HOLD = "max_hold"


@dataclass
class CallTimers:
    max_hold_s: float
    silence_recheck_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _last_sound_at: float = field(init=False)
    _silence_fired: bool = field(init=False, default=False)
    _hold_started_at: float | None = field(init=False, default=None)
    _hold_accumulated_s: float = field(init=False, default=0.0)
    _max_hold_fired: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._last_sound_at = self.clock()

    # -- inputs --------------------------------------------------------------

    def heard_sound(self) -> None:
        """Speech or music on the line resets the silence timer."""
        self._last_sound_at = self.clock()
        self._silence_fired = False

    def hold_started(self) -> None:
        if self._hold_started_at is None:
            self._hold_started_at = self.clock()

    def hold_stopped(self) -> None:
        if self._hold_started_at is not None:
            self._hold_accumulated_s += self.clock() - self._hold_started_at
            self._hold_started_at = None

    def extend_hold(self, extra_s: float) -> None:
        """User chose to keep waiting: allow another `extra_s` before asking again."""
        self.max_hold_s = self.hold_seconds + extra_s
        self._max_hold_fired = False

    # -- readings ------------------------------------------------------------

    @property
    def silence_seconds(self) -> float:
        return self.clock() - self._last_sound_at

    @property
    def hold_seconds(self) -> float:
        running = self.clock() - self._hold_started_at if self._hold_started_at is not None else 0
        return self._hold_accumulated_s + running

    def due(self) -> list[TimerEvent]:
        events: list[TimerEvent] = []
        if not self._silence_fired and self.silence_seconds > self.silence_recheck_s:
            self._silence_fired = True
            events.append(TimerEvent.SILENCE_RECHECK)
        if not self._max_hold_fired and self.hold_seconds > self.max_hold_s:
            self._max_hold_fired = True
            events.append(TimerEvent.MAX_HOLD)
        return events
