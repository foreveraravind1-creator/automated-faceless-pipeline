"""
agents/agent3_editor.py
Agent 3 - Editor
Tool: MoviePy 1.x + ffmpeg (100% deterministic - no LLM calls)

Rationale: video assembly (resize, crop, loop, mux) is pure algorithmic work.
Routing it through an LLM adds latency, token cost, and a failure surface with
zero benefit - the logic is fixed regardless of what a model says.

Input  : JobState.audio_path, JobState.background_path
Output : JobState.final_reel_path (final_reel.mp4, 1080x1920, H.264/AAC, 30fps)
"""
from pathlib import Path

from moviepy.editor import AudioFileClip, VideoFileClip, concatenate_videoclips

from core.config import settings
from core.logger import get_logger
from core.models import JobState

logger = get_logger(__name__)

TARGET_W = 1080
TARGET_H = 1920
TARGET_RATIO = TARGET_W / TARGET_H  # 0.5625  (9:16 portrait)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _crop_to_916(source_path: Path) -> VideoFileClip:
    """
    Load a video and resize+center-crop it to exactly TARGET_W x TARGET_H.
    Opens a fresh VideoFileClip each call to avoid reader conflicts when
    the same source file is concatenated multiple times for looping.
    """
    clip = VideoFileClip(str(source_path), audio=False)
    src_ratio = clip.w / clip.h

    if src_ratio > TARGET_RATIO:
        # Source is wider than 9:16 -> scale by height, crop excess width
        clip = clip.resize(height=TARGET_H)
        x_c = clip.w / 2
        clip = clip.crop(x1=x_c - TARGET_W / 2, x2=x_c + TARGET_W / 2)
    else:
        # Source is taller or equal -> scale by width, crop excess height
        clip = clip.resize(width=TARGET_W)
        y_c = clip.h / 2
        clip = clip.crop(y1=y_c - TARGET_H / 2, y2=y_c + TARGET_H / 2)

    # Guard against sub-pixel rounding errors producing wrong dimensions
    if (clip.w, clip.h) != (TARGET_W, TARGET_H):
        clip = clip.resize((TARGET_W, TARGET_H))

    return clip


def _build_background_clip(video_path: Path, target_duration: float) -> VideoFileClip:
    """
    Produce a 9:16 background clip of exactly target_duration seconds.
    If the source is shorter than target_duration, it is looped.
    Each loop segment opens the source file independently to avoid
    MoviePy reader-state conflicts on the same underlying file handle.
    """
    # Probe duration without keeping a reader open
    probe = VideoFileClip(str(video_path), audio=False)
    source_duration = probe.duration
    probe.close()

    if source_duration >= target_duration:
        clip = _crop_to_916(video_path)
        return clip.subclip(0, target_duration)

    # Source is shorter than audio: calculate needed loops then concatenate
    n_loops = int(target_duration / source_duration) + 1
    logger.info(
        "Background shorter than audio - looping",
        extra={"source_s": round(source_duration, 2), "loops_needed": n_loops},
    )
    segments = [_crop_to_916(video_path) for _ in range(n_loops)]
    looped = concatenate_videoclips(segments, method="compose")
    return looped.subclip(0, target_duration)


# ---------------------------------------------------------------------------
# Agent entry-point
# ---------------------------------------------------------------------------

def run(state: JobState) -> JobState:
    """Run Agent 3 and populate state.final_reel_path."""
    logger.info("Agent 3 starting", extra={"job_id": state.job_id})

    output_dir: Path = settings.output_dir / state.job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / "final_reel.mp4"

    # Load audio to determine the exact target duration
    audio = AudioFileClip(str(state.audio_path))
    audio_duration = audio.duration
    logger.info("Audio loaded", extra={"duration_s": round(audio_duration, 2)})

    # Build 9:16 background - looped/trimmed to match audio exactly
    bg = _build_background_clip(state.background_path, audio_duration)

    # Attach TTS audio and lock clip duration to audio length
    final = bg.set_audio(audio).set_duration(audio_duration)

    # Export: H.264 + AAC, 30fps, CRF 23, yuv420p for broad device compatibility
    logger.info("Exporting final_reel.mp4", extra={"job_id": state.job_id})
    final.write_videofile(
        str(final_path),
        fps=30,
        codec="libx264",
        audio_codec="aac",
        preset="fast",
        ffmpeg_params=["-crf", "23", "-pix_fmt", "yuv420p"],
        logger=None,   # suppress MoviePy's verbose ffmpeg progress output
    )

    # Release all file handles to free memory on Cloud Run
    audio.close()
    bg.close()
    final.close()

    state.final_reel_path = final_path
    logger.info(
        "Agent 3 complete",
        extra={
            "job_id": state.job_id,
            "output": str(final_path),
            "duration_s": round(audio_duration, 2),
        },
    )
    return state
