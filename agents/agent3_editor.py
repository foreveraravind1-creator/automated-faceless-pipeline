"""
agents/agent3_editor.py
ffmpeg render. No model call.

Burns script.on_screen_hook for the first 3 seconds and narration captions
timed from the speech track. Output is 1080x1920 H.264 + AAC.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from core.logger import get_logger
from core.models import CaptionCue, JobState
from core.timing import assert_audio_within_cap

logger = get_logger(__name__)

WIDTH = 1080
HEIGHT = 1920
HOOK_SECONDS = 3.0
_ENCODER: str | None = None

_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)


def run(
    state: JobState,
    *,
    run_ffmpeg: Callable[[list[str]], None] | None = None,
    encoder: str | None = None,
    fontfile: Path | None = None,
) -> JobState:
    """Mux the background and narration into final_reel.mp4."""
    if state.script is None:
        raise ValueError("a script is required")
    if state.audio_path is None or state.background_path is None:
        raise ValueError("audio and background video are required")
    if state.audio_duration is None:
        raise ValueError("audio duration is required")

    assert_audio_within_cap(state.audio_duration, state.script.duration_seconds)
    font = fontfile or find_font()
    job_dir = state.audio_path.parent
    ass_path = job_dir / "captions.ass"
    hook_path = job_dir / "hook.txt"
    output_path = job_dir / "final_reel.mp4"
    cues = state.caption_cues
    write_ass(ass_path, cues)
    hook_path.write_text(_one_line(state.script.on_screen_hook), encoding="utf-8")

    chosen = encoder or select_encoder()
    command = build_ffmpeg_command(
        background=state.background_path,
        audio=state.audio_path,
        output=output_path,
        ass_path=ass_path,
        hook_path=hook_path,
        fontfile=font,
        encoder=chosen,
        audio_duration=state.audio_duration,
    )
    logger.info(
        "Rendering reel",
        extra={"job_id": state.job_id, "encoder": chosen, "duration_s": round(state.audio_duration, 2)},
    )
    (run_ffmpeg or _run_ffmpeg)(command)
    state.final_reel_path = output_path
    logger.info("Render complete", extra={"job_id": state.job_id, "output": str(output_path)})
    return state


def build_ffmpeg_command(
    *,
    background: Path,
    audio: Path,
    output: Path,
    ass_path: Path,
    hook_path: Path,
    fontfile: Path,
    encoder: str,
    audio_duration: float,
) -> list[str]:
    """
    Loop and cover-crop the background to 1080x1920, trim it to the audio,
    and burn captions plus the hook overlay.

    Template (encoder is libx264 or h264_nvenc):

        ffmpeg -y -stream_loop -1 -i BACKGROUND -i AUDIO -t DURATION
          -map 0:v:0 -map 1:a:0
          -vf scale=1080x1920:force_original_aspect_ratio=increase,crop=1080:1920,
              subtitles=CAPTIONS,drawtext=fontfile=FONT:textfile=HOOK:enable='lt(t,3)'
          -r 30 -c:v ENCODER -pix_fmt yuv420p -c:a aac -movflags +faststart
          OUTPUT
    """
    fontsdir = fontfile.parent
    video_filter = ",".join(
        [
            f"scale={WIDTH}x{HEIGHT}:force_original_aspect_ratio=increase",
            f"crop={WIDTH}:{HEIGHT}",
            "subtitles={}:fontsdir={}".format(
                _escape_filter(ass_path),
                _escape_filter(fontsdir),
            ),
            "drawtext=fontfile={}:textfile={}:fontsize=64:fontcolor=white:"
            "borderw=4:bordercolor=black:x=(w-text_w)/2:y=160:enable='lt(t,{})'".format(
                _escape_filter(fontfile),
                _escape_filter(hook_path),
                int(HOOK_SECONDS) if HOOK_SECONDS == int(HOOK_SECONDS) else HOOK_SECONDS,
            ),
        ]
    )
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stream_loop",
        "-1",
        "-i",
        str(background),
        "-i",
        str(audio),
        "-t",
        f"{audio_duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        video_filter,
        "-r",
        "30",
        "-c:v",
        encoder,
        "-pix_fmt",
        "yuv420p",
    ]
    if encoder == "libx264":
        command.extend(["-preset", "veryfast", "-crf", "23"])
    else:
        command.extend(["-preset", "p4", "-rc", "vbr", "-cq", "23"])
    command.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


def select_encoder(runner: Callable[..., subprocess.CompletedProcess] | None = None) -> str:
    """Use h264_nvenc only after a one-frame test encode succeeds."""
    global _ENCODER
    if runner is None and _ENCODER is not None:
        return _ENCODER
    chosen = "h264_nvenc" if nvenc_works(runner or subprocess.run) else "libx264"
    if runner is None:
        _ENCODER = chosen
        logger.info("Video encoder selected", extra={"encoder": chosen})
    return chosen


def nvenc_works(runner: Callable[..., subprocess.CompletedProcess]) -> bool:
    """Tiny encode. Listing encoders is not enough; the device has to encode."""
    if shutil.which("ffmpeg") is None and runner is subprocess.run:
        return False
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:d=0.1",
        "-frames:v",
        "1",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    try:
        result = runner(command, capture_output=True, check=False)
    except (OSError, FileNotFoundError):
        return False
    return result.returncode == 0


def find_font() -> Path:
    for path in _FONT_CANDIDATES:
        if path.is_file():
            return path
    raise RuntimeError(
        "No caption font found. Install fonts-dejavu-core "
        "(DejaVuSans-Bold.ttf) or liberation fonts."
    )


def write_ass(path: Path, cues: list[CaptionCue]) -> None:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {WIDTH}",
        f"PlayResY: {HEIGHT}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Caption,DejaVu Sans,58,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,4,0,2,70,70,280,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Caption,,0,0,0,,"
            f"{_ass_text(cue.text)}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ffmpeg failed ({result.returncode}): {detail[-2000:]}")


def _escape_filter(path: Path) -> str:
    text = path.resolve().as_posix()
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(seconds * 100)))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def _one_line(text: str) -> str:
    return " ".join(text.split())
