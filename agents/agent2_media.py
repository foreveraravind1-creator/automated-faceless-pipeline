"""
agents/agent2_media.py
Local Kokoro-82M narration and a Pexels background clip.

The Pexels query is script.pexels_query. This module does not call a model
to rewrite it, and it does not call a model to write a caption.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from core.config import settings
from core.logger import get_logger
from core.models import CaptionCue, JobState
from core.timing import assert_audio_within_cap

logger = get_logger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
SAMPLE_RATE = 24000
MAX_CAPTION_CHARS = 42

# Portrait files at or under 1080p (1080x1920). Larger files are a fallback.
_MAX_PORTRAIT_WIDTH = 1080
_MAX_PORTRAIT_HEIGHT = 1920

_pipelines: dict[str, Any] = {}


@dataclass
class SpeechTrack:
    duration: float
    sample_rate: int
    cues: list[CaptionCue] = field(default_factory=list)
    words: list[CaptionCue] = field(default_factory=list)


def run(
    state: JobState,
    *,
    synthesize: Callable[..., SpeechTrack] | None = None,
    fetch_video: Callable[[str, Path], None] | None = None,
) -> JobState:
    """Write audio.wav, timings.json, and background.mp4 next to script.json."""
    if state.script is None:
        raise ValueError("a script is required")

    output_dir = settings.output_dir / state.job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "audio.wav"
    background_path = output_dir / "background.mp4"
    query = state.script.pexels_query

    logger.info(
        "Media stage starting",
        extra={"job_id": state.job_id, "pexels_query": query, "voice": state.script.voice},
    )

    speak = synthesize or synthesize_speech
    track = speak(
        state.script.narration,
        audio_path,
        voice=state.script.voice,
        lang=state.script.kokoro_lang,
    )
    assert_audio_within_cap(track.duration, state.script.duration_seconds)

    timings = {
        "sample_rate": track.sample_rate,
        "duration": track.duration,
        "voice": state.script.voice,
        "lang": state.script.kokoro_lang,
        "cues": [cue.model_dump() for cue in track.cues],
        "words": [cue.model_dump() for cue in track.words],
    }
    (output_dir / "timings.json").write_text(
        json.dumps(timings, indent=2) + "\n",
        encoding="utf-8",
    )

    download = fetch_video or fetch_pexels_video
    download(query, background_path)

    state.audio_path = audio_path
    state.audio_duration = track.duration
    state.background_path = background_path
    state.caption_cues = track.cues
    logger.info(
        "Media stage complete",
        extra={"job_id": state.job_id, "duration_s": round(track.duration, 2), "query": query},
    )
    return state


def synthesize_speech(
    narration: str,
    output_path: Path,
    *,
    voice: str,
    lang: str,
) -> SpeechTrack:
    """
    Render narration with Kokoro-82M.

    English voices (lang a/b) expose word start_ts/end_ts on each token.
    Those are offset by the chunk's real audio clock. Other languages yield
    one cue per audio chunk. Caption lines are grouped to about 42 characters.
    """
    try:
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline
    except ImportError as exc:
        raise RuntimeError(
            "Kokoro TTS is not installed. Use Python 3.12 and install "
            "kokoro>=0.9.4, soundfile, and the espeak-ng system package."
        ) from exc

    pipeline = _pipeline(KPipeline, lang)
    pieces: list[Any] = []
    words: list[CaptionCue] = []
    chunk_cues: list[CaptionCue] = []
    cursor = 0.0

    for result in pipeline(narration, voice=voice, speed=1.0):
        audio = getattr(result, "audio", None)
        if audio is None:
            continue
        samples = audio.detach().cpu().numpy().reshape(-1)
        if samples.size == 0:
            continue
        chunk_start = cursor
        chunk_end = cursor + (float(samples.size) / SAMPLE_RATE)
        pieces.append(samples)
        chunk_words, fallback = _cues_for_result(result, chunk_start, chunk_end)
        words.extend(chunk_words)
        if not chunk_words and fallback is not None:
            chunk_cues.append(fallback)
        cursor = chunk_end

    if not pieces:
        raise RuntimeError("Kokoro returned no audio for the narration.")

    mixed = np.concatenate(pieces)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), mixed, SAMPLE_RATE)
    duration = float(mixed.size) / SAMPLE_RATE
    cues = _group_words(words) if words else chunk_cues
    if not cues:
        cues = [CaptionCue(text=narration.strip(), start=0.0, end=max(duration, 0.05))]
    return SpeechTrack(
        duration=duration,
        sample_rate=SAMPLE_RATE,
        cues=cues,
        words=words,
    )


def _pipeline(pipeline_cls: Any, lang: str) -> Any:
    cached = _pipelines.get(lang)
    if cached is not None:
        return cached
    pipeline = pipeline_cls(lang_code=lang or "a", repo_id="hexgrad/Kokoro-82M", device="cpu")
    _pipelines[lang] = pipeline
    return pipeline


def _cues_for_result(
    result: Any,
    chunk_start: float,
    chunk_end: float,
) -> tuple[list[CaptionCue], CaptionCue | None]:
    words: list[CaptionCue] = []
    for token in getattr(result, "tokens", None) or []:
        text = (getattr(token, "text", "") or "").strip()
        start_ts = getattr(token, "start_ts", None)
        end_ts = getattr(token, "end_ts", None)
        if not text or start_ts is None or end_ts is None:
            continue
        start = min(max(chunk_start + float(start_ts), chunk_start), chunk_end)
        end = min(max(chunk_start + float(end_ts), start + 0.02), chunk_end)
        if end <= start:
            continue
        words.append(CaptionCue(text=text, start=start, end=end))
    if words:
        return words, None
    graphemes = (getattr(result, "graphemes", "") or "").strip()
    if not graphemes or chunk_end <= chunk_start:
        return [], None
    return [], CaptionCue(text=graphemes, start=chunk_start, end=max(chunk_end, chunk_start + 0.02))


def _group_words(words: list[CaptionCue], max_chars: int = MAX_CAPTION_CHARS) -> list[CaptionCue]:
    """Pack word timestamps into short on-screen lines."""
    lines: list[list[CaptionCue]] = []
    buf: list[CaptionCue] = []
    for word in words:
        trial = " ".join(item.text for item in buf + [word])
        if buf and len(trial) > max_chars:
            lines.append(buf)
            buf = []
        buf.append(word)
    if buf:
        lines.append(buf)
    return [
        CaptionCue(
            text=" ".join(item.text for item in line),
            start=line[0].start,
            end=line[-1].end,
        )
        for line in lines
    ]


def fetch_pexels_video(query: str, output_path: Path) -> None:
    """Download one portrait clip for the exact script query."""
    if not settings.pexels_api_key:
        raise RuntimeError("PEXELS_API_KEY is required to download background video.")
    headers = {"Authorization": settings.pexels_api_key}
    params = {"query": query, "per_page": 10, "orientation": "portrait"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(PEXELS_SEARCH_URL, headers=headers, params=params)
        resp.raise_for_status()
        videos = resp.json().get("videos", [])
    link = choose_pexels_link(videos)
    logger.info("Downloading Pexels video", extra={"query": query, "url": link})
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        with client.stream("GET", link) as stream_resp:
            stream_resp.raise_for_status()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as fh:
                for chunk in stream_resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)


def choose_pexels_link(videos: list[dict]) -> str:
    """
    Prefer a portrait file at or below 1080x1920.

    If every portrait file is larger, pick the smallest one. The search
    query is not changed.
    """
    files: list[tuple[int, int, str]] = []
    for video in videos:
        for item in video.get("video_files") or []:
            link = item.get("link")
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
            if link and width > 0 and height > 0:
                files.append((width, height, link))
    if not files:
        raise ValueError("Pexels returned no downloadable video files.")

    portraits = [item for item in files if item[1] > item[0]]
    capped = [
        item
        for item in portraits
        if item[0] <= _MAX_PORTRAIT_WIDTH and item[1] <= _MAX_PORTRAIT_HEIGHT
    ]
    if capped:
        return max(capped, key=lambda item: item[1])[2]
    if portraits:
        return min(portraits, key=lambda item: item[0] * item[1])[2]
    return min(files, key=lambda item: item[0] * item[1])[2]
