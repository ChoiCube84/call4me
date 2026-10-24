"""Render every fake-line prompt to speech once, with Cartesia TTS.

The in-process mock line (`backend/telephony/mock_client.py`) plays
`fakeline/audio/<key>.ulaw` when present instead of a speech-like placeholder,
so with these files real Cartesia STT can transcribe the mock line: the whole
pipeline runs on real speech with no phone account.

    uv run python -m fakeline.render_audio          # needs CARTESIA_API_KEY + CARTESIA_VOICE_ID
    uv run python -m fakeline.render_audio --force  # re-render existing files
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from fakeline import script
from fakeline.sounds import AUDIO_DIR

MAX_ANNOUNCEMENTS = 8  # hold announcements are keyed by count; long holds repeat the texts


def all_segments() -> list[tuple[str, str]]:
    """(key, text) for everything the mock line can say."""
    out: list[tuple[str, str]] = [
        ("welcome", script.WELCOME),
        ("goodbye", script.NO_INPUT_GOODBYE),
        ("invalid", script.INVALID_OPTION),
        ("queue:intro", script.QUEUE_INTRO),
        ("callback:offer", script.CALLBACK_OFFER),
        ("callback:ask", script.CALLBACK_ASK_NUMBER),
        ("callback:confirm", script.CALLBACK_CONFIRM),
        ("connecting", script.CONNECTING),
    ]
    for name, node in script.NODES.items():
        seg = (
            script.menu_segment(name)
            if isinstance(node, script.Menu)
            else script.message_segment(name)
        )
        out.append((seg.key, seg.text))
    for i in range(1, MAX_ANNOUNCEMENTS + 1):
        text = script.HOLD_ANNOUNCEMENTS[(i - 1) % len(script.HOLD_ANNOUNCEMENTS)]
        out.append((f"hold:announce:{i}", text))
    for turn in range(len(script.REP_SCRIPT)):
        out.append((f"rep:{turn}", script.REP_SCRIPT[turn].text))
        out.append((f"rep:{turn}:nudge", script.REP_NUDGE))
        out.append((f"rep:{turn}:wait", script.REP_TAKE_YOUR_TIME))
    return out


async def render(force: bool) -> int:
    from backend.config import get_settings
    from backend.speech.tts import CartesiaTTSClient

    s = get_settings()
    if not (s.cartesia_api_key and s.cartesia_voice_id):
        print("CARTESIA_API_KEY and CARTESIA_VOICE_ID must be set", file=sys.stderr)
        return 1
    tts = CartesiaTTSClient(s.cartesia_api_key, s.cartesia_voice_id)
    AUDIO_DIR.mkdir(exist_ok=True)
    done = skipped = 0
    for key, text in all_segments():
        path = AUDIO_DIR / f"{key.replace(':', '_')}.ulaw"
        if path.exists() and not force:
            skipped += 1
            continue
        audio = b"".join([f async for f in tts.synthesize(text)])
        path.write_bytes(audio)
        done += 1
        print(f"{path.name:28} {len(audio) / 8000:5.1f}s  {text[:60]}")
    await tts.close()
    print(f"rendered {done}, kept {skipped} existing, in {AUDIO_DIR}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--force", action="store_true", help="re-render files that already exist")
    sys.exit(asyncio.run(render(p.parse_args().force)))


if __name__ == "__main__":
    main()
