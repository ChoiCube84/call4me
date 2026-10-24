"""Send TTS frames to the line and keep the echo gate in sync.

    sent = await speak(tts.synthesize(text), lambda f: telephony.send_audio(sid, f),
                       echo=gate, text=text)

On barge-in/takeover, cancel the task running `speak`, then call
`telephony.clear_audio()` and `gate.clear()` so the rep is heard right away.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from backend.audio.codec import SAMPLE_RATE
from backend.audio.echo import EchoGate


async def speak(
    frames: AsyncIterator[bytes],
    send: Callable[[bytes], Awaitable[None]],
    *,
    echo: EchoGate | None = None,
    text: str | None = None,
) -> int:
    """Forward every frame to `send`; returns the number of frames sent."""
    sent = 0
    try:
        async for frame in frames:
            await send(frame)
            sent += 1
            if echo is not None:
                echo.note_playback(len(frame) / SAMPLE_RATE)  # 1 mu-law byte per sample
    finally:
        aclose = getattr(frames, "aclose", None)
        if aclose is not None:
            await aclose()
        if echo is not None and text and sent:
            echo.note_playback(0.0, text)
    return sent
