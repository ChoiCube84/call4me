"""Fake line settings (env vars prefixed FAKELINE_)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class FakeLineSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FAKELINE_", env_file=".env", extra="ignore")

    hold_min_s: int = 30
    hold_max_s: int = 120
    callback_probability: float = 0.3  # chance a given call hears a callback offer
    # Where the hold queue hands off. First non-empty wins:
    rep_number: str = ""  # a teammate's phone, dialed with <Dial>
    rep_url: str = ""  # an external bot-rep TwiML URL (e.g. from evals/)
    # otherwise the built-in scripted rep in fakeline/script.py answers.
    hold_music_url: str = ""  # override the synthesized hold music
    seed: int | None = None  # fix randomness for repeatable evals


@lru_cache
def get_fakeline_settings() -> FakeLineSettings:
    return FakeLineSettings()
