"""
agents/agent2_media.py
Agent 2 - Media Synthesizer
Model: gemini-2.5-flash (orchestration and Pexels query refinement)
APIs : Google Cloud Text-to-Speech (en-US-Journey-D), Pexels Videos API

Input  : JobState.script (ScriptPayload)
Output : JobState.audio_path (audio.mp3), JobState.background_path (background.mp4)
"""
from pathlib import Path

import google.generativeai as genai
import httpx
from google.cloud import texttospeech

from core.config import settings
from core.logger import get_logger
from core.models import JobState

logger = get_logger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"


# ---------------------------------------------------------------------------
# TTS helper
# ---------------------------------------------------------------------------

def _synthesize_speech(narration: str, output_path: Path) -> None:
    """Call Google Cloud TTS with en-US-Journey-D and write the audio as MP3."""
    client = texttospeech.TextToSpeechClient()

    synthesis_input = texttospeech.SynthesisInput(text=narration)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Journey-D",  # Male Journey voice profile
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
        pitch=0.0,
    )

    resp = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
    )
    output_path.write_bytes(resp.audio_content)
    logger.info(
        "TTS audio saved",
        extra={"path": str(output_path), "bytes": len(resp.audio_content)},
    )


# ---------------------------------------------------------------------------
# Pexels helper
# ---------------------------------------------------------------------------

def _fetch_pexels_video(query: str, output_path: Path) -> None:
    """Search Pexels for a portrait-oriented background video and stream it to disk."""
    headers = {"Authorization": settings.pexels_api_key}
    params = {
        "query": query,
        "per_page": 10,
        "orientation": "portrait",
        "size": "large",
    }

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(PEXELS_SEARCH_URL, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    videos = data.get("videos", [])
    if not videos:
        raise ValueError(f"Pexels returned no videos for query: '{query}'")

    # Prefer portrait-oriented HD files (width < height, height >= 720p)
    chosen_url: str | None = None
    for video in videos:
        files = video.get("video_files", [])
        portrait_hd = [
            f for f in files
            if f.get("width", 1) < f.get("height", 0) and f.get("height", 0) >= 720
        ]
        if portrait_hd:
            best = sorted(portrait_hd, key=lambda x: x.get("height", 0), reverse=True)[0]
            chosen_url = best["link"]
            break

    # Fallback: highest-resolution file from first video
    if not chosen_url:
        files = videos[0].get("video_files", [])
        if not files:
            raise ValueError("Pexels video has no downloadable files.")
        chosen_url = sorted(files, key=lambda x: x.get("height", 0), reverse=True)[0]["link"]

    logger.info("Downloading Pexels video", extra={"url": chosen_url})
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        with client.stream("GET", chosen_url) as stream_resp:
            stream_resp.raise_for_status()
            with open(output_path, "wb") as fh:
                for chunk in stream_resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)

    logger.info("Background video saved", extra={"path": str(output_path)})


# ---------------------------------------------------------------------------
# Agent entry-point
# ---------------------------------------------------------------------------

def run(state: JobState) -> JobState:
    """Run Agent 2 and populate state.audio_path + state.background_path."""
    logger.info("Agent 2 starting", extra={"job_id": state.job_id})

    # Use Gemini 2.5 Flash to refine the visual prompt into a sharp Pexels search term
    genai.configure(api_key=settings.google_api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    refine_prompt = (
        f'The following is a raw visual concept for a background video: "{state.script.visual_prompt}"\n'
        "Convert it into a single, clean Pexels video search query of 2-4 words.\n"
        "Rules: no quotes, no special characters, avoid vague adjectives (beautiful, stunning).\n"
        "Return ONLY the search query string, nothing else."
    )
    refinement = model.generate_content(refine_prompt)
    pexels_query = refinement.text.strip().strip('"').strip("'")
    logger.info("Refined Pexels query", extra={"query": pexels_query})

    output_dir: Path = settings.output_dir / state.job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_path = output_dir / "audio.mp3"
    background_path = output_dir / "background.mp4"

    # Step 1: Text-to-Speech narration
    _synthesize_speech(state.script.narration, audio_path)

    # Step 2: Background video (with fallback to original visual_prompt)
    try:
        _fetch_pexels_video(pexels_query, background_path)
    except Exception as exc:
        logger.warning(
            "Refined query failed - retrying with original visual_prompt",
            extra={"error": str(exc), "fallback_query": state.script.visual_prompt},
        )
        _fetch_pexels_video(state.script.visual_prompt, background_path)

    state.audio_path = audio_path
    state.background_path = background_path

    logger.info("Agent 2 complete", extra={"job_id": state.job_id})
    return state
