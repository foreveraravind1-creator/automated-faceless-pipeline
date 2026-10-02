"""
core/timing.py
Shared duration cap for the local render. No network and no model calls.
"""
from __future__ import annotations

# A rendered reel may run this far past the niche target, and never past 90s.
DURATION_MARGIN_SECONDS = 5
MAX_REEL_SECONDS = 90


def reel_duration_limit(duration_seconds: int) -> float:
    """Niche target plus a short margin, capped at 90 seconds."""
    return float(min(MAX_REEL_SECONDS, duration_seconds + DURATION_MARGIN_SECONDS))


def assert_audio_within_cap(audio_seconds: float, duration_seconds: int) -> float:
    """Raise a clear error when speech is longer than the niche allows."""
    limit = reel_duration_limit(duration_seconds)
    if audio_seconds > limit:
        raise RuntimeError(
            f"Audio is {audio_seconds:.1f}s, over the {limit:.0f}s cap "
            f"(niche duration_seconds={duration_seconds} plus "
            f"{DURATION_MARGIN_SECONDS}s margin, never above {MAX_REEL_SECONDS}s). "
            "Shorten the narration or raise the niche duration, then render again."
        )
    return limit
