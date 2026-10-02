"""Pass 2: approved --dry-run renders locally and never publishes."""
from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from agents.agent2_media import SpeechTrack, choose_pexels_link
from agents.agent3_editor import find_font, select_encoder
from agents import agent2_media, agent3_editor
from core.config import settings
from core.models import CaptionCue, JobState, ScriptPayload
from main import main, run_dry

ROOT = Path(__file__).resolve().parents[1]
NICHE = ROOT / "niches" / "example.json"


def _script(approved: bool, **overrides) -> dict:
    payload = {
        "title": "Why the sky looks blue",
        "hook": "The sky is not a painted ceiling.",
        "on_screen_hook": "The sky is not blue paint",
        "narration": "Sunlight looks white, but air scatters the blue waves.",
        "pexels_query": "blue sky clouds",
        "caption": "A clear sky is scattered sunlight.",
        "hashtags": ["#science", "#sciencefacts"],
        "claims_to_verify": ["Air scatters blue light more than red light."],
        "approved": approved,
        "topic": "Why is the sky blue?",
        "niche": "everyday-science",
        "language": "en",
        "duration_seconds": 45,
        "voice": "af_heart",
        "kokoro_lang": "a",
    }
    payload.update(overrides)
    return payload


def _write_script(directory: Path, job_id: str, approved: bool, **overrides) -> None:
    job = directory / job_id
    job.mkdir(parents=True, exist_ok=True)
    (job / "script.json").write_text(json.dumps(_script(approved, **overrides)), encoding="utf-8")


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModels:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        body = _script(False)
        body.pop("approved")
        body.pop("topic")
        body.pop("niche")
        body.pop("language")
        body.pop("duration_seconds")
        body.pop("voice")
        body.pop("kokoro_lang")
        return _Response(json.dumps(body))


class _FakeClient:
    def __init__(self) -> None:
        self.models = _FakeModels()

    def close(self) -> None:
        return None


def test_source_has_no_legacy_model_or_editor_imports():
    media = (ROOT / "agents" / "agent2_media.py").read_text(encoding="utf-8")
    publisher = (ROOT / "agents" / "agent4_publisher.py").read_text(encoding="utf-8")
    editor = (ROOT / "agents" / "agent3_editor.py").read_text(encoding="utf-8")
    legacy_editor = "movie" + "py"
    for text in (media, publisher):
        assert "generativeai" not in text
        assert "genai" not in text
    assert legacy_editor not in editor
    assert legacy_editor not in (ROOT / "requirements.txt").read_text(encoding="utf-8")


def test_pexels_chooser_caps_at_1080p_portrait():
    videos = [
        {
            "video_files": [
                {"width": 2160, "height": 3840, "link": "https://cdn.example/4k.mp4"},
                {"width": 1080, "height": 1920, "link": "https://cdn.example/1080.mp4"},
                {"width": 720, "height": 1280, "link": "https://cdn.example/720.mp4"},
            ]
        }
    ]
    assert choose_pexels_link(videos) == "https://cdn.example/1080.mp4"


def test_encoder_falls_back_when_nvenc_test_fails():
    def fail(cmd, **kwargs):
        assert "h264_nvenc" in cmd
        assert "lavfi" in cmd
        assert "-encoders" not in cmd
        return subprocess.CompletedProcess(cmd, 1, b"", b"no device")

    assert select_encoder(runner=fail) == "libx264"


def test_encoder_uses_nvenc_when_test_encode_succeeds():
    def ok(cmd, **kwargs):
        assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    assert select_encoder(runner=ok) == "h264_nvenc"


def test_dry_run_refuses_when_approved_is_false(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    _write_script(tmp_path, "job-no", approved=False)
    sys.modules.pop("agents.agent2_media", None)
    sys.modules.pop("agents.agent3_editor", None)
    sys.modules.pop("agents.agent4_publisher", None)

    with pytest.raises(ValueError, match="approved is false"):
        run_dry("job-no")

    assert "agents.agent2_media" not in sys.modules
    assert "agents.agent4_publisher" not in sys.modules
    assert not (tmp_path / "job-no" / "final_reel.mp4").exists()


def test_dry_run_approved_renders_without_publisher_or_gemini(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    _write_script(tmp_path, "job-ok", approved=True)
    queries: list[str] = []
    commands: list[list[str]] = []

    def synthesize(narration, output_path, *, voice, lang):
        assert voice == "af_heart"
        assert lang == "a"
        output_path.write_bytes(b"RIFFfake")
        cue = CaptionCue(text="Sunlight looks white", start=0.0, end=1.2)
        return SpeechTrack(duration=1.2, sample_rate=24000, cues=[cue], words=[cue])

    def fetch(query, path):
        queries.append(query)
        path.write_bytes(b"video")

    def ffmpeg(cmd):
        commands.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"mp4")

    sys.modules.pop("agents.agent4_publisher", None)
    sys.modules.pop("google.generativeai", None)
    state = run_dry(
        "job-ok",
        synthesize=synthesize,
        fetch_video=fetch,
        run_ffmpeg=ffmpeg,
        encoder="libx264",
        fontfile=Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )

    assert queries == ["blue sky clouds"]
    assert len(commands) == 1
    joined = " ".join(commands[0])
    for token in ("libx264", "aac", "yuv420p", "+faststart", "1080x1920", "subtitles", "drawtext"):
        assert token in joined
    assert state.final_reel_path == tmp_path / "job-ok" / "final_reel.mp4"
    assert "agents.agent4_publisher" not in sys.modules
    assert "google.generativeai" not in sys.modules


def test_overlong_audio_stops_before_pexels(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    fetched: list[str] = []

    def synthesize(narration, output_path, *, voice, lang):
        output_path.write_bytes(b"RIFFfake")
        cue = CaptionCue(text="too long", start=0.0, end=1.0)
        return SpeechTrack(duration=100.0, sample_rate=24000, cues=[cue])

    def fetch(query, path):
        fetched.append(query)

    state = JobState(
        job_id="job-long",
        topic="Why is the sky blue?",
        script=ScriptPayload.model_validate(_script(True)),
    )
    with pytest.raises(RuntimeError, match="over the"):
        agent2_media.run(state, synthesize=synthesize, fetch_video=fetch)
    assert fetched == []


def test_default_command_writes_unapproved_script_and_exits(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    fake = _FakeClient()
    monkeypatch.setattr("agents.agent1_researcher._default_client", lambda: fake)
    for name in ("agents.agent2_media", "agents.agent3_editor", "agents.agent4_publisher"):
        sys.modules.pop(name, None)

    code = main(
        ["--topic", "Why is the sky blue?", "--niche", str(NICHE), "--job-id", "cli-1"]
    )

    assert code == 0
    assert len(fake.models.calls) == 1
    saved = json.loads((tmp_path / "cli-1" / "script.json").read_text(encoding="utf-8"))
    assert saved["approved"] is False
    assert not (tmp_path / "cli-1" / "final_reel.mp4").exists()
    assert "agents.agent2_media" not in sys.modules
    assert "agents.agent3_editor" not in sys.modules
    assert "agents.agent4_publisher" not in sys.modules


def test_cli_dry_run_without_job_id_refuses():
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "job id" in result.stderr.lower()


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg missing")
def test_smoke_render_color_clip(tmp_path: Path):
    font = find_font()
    job = tmp_path / "smoke"
    job.mkdir()
    _write_tone(job / "audio.wav", seconds=1.0)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x224466:s=720x1280:d=0.4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(job / "background.mp4"),
        ],
        check=True,
    )
    script = ScriptPayload.model_validate(_script(True))
    state = JobState(
        job_id="smoke",
        topic=script.topic,
        script=script,
        audio_path=job / "audio.wav",
        audio_duration=1.0,
        background_path=job / "background.mp4",
        caption_cues=[CaptionCue(text="scattered blue light", start=0.1, end=0.9)],
    )
    agent3_editor.run(state, encoder="libx264", fontfile=font)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_name,codec_type,width,height,pix_fmt",
            "-of", "json",
            str(state.final_reel_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in info["streams"] if stream["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert video["width"] == 1080
    assert video["height"] == 1920
    assert audio["codec_name"] == "aac"
    assert 0.9 <= float(info["format"]["duration"]) <= 1.2
    blob = state.final_reel_path.read_bytes()
    assert blob.find(b"moov") != -1
    assert blob.find(b"moov") < blob.find(b"mdat")
    report = {
        "codec": video["codec_name"],
        "audio": audio["codec_name"],
        "pix_fmt": video["pix_fmt"],
        "width": video["width"],
        "height": video["height"],
        "duration": float(info["format"]["duration"]),
        "size_bytes": state.final_reel_path.stat().st_size,
        "moov_before_mdat": True,
    }
    Path("/tmp/reel-smoke-ffprobe.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _write_tone(path: Path, seconds: float) -> None:
    rate = 24000
    count = int(rate * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(count):
            sample = int(8000 * math.sin(2 * math.pi * 440 * index / rate))
            frames.extend(struct.pack("<h", sample))
        handle.writeframes(frames)
