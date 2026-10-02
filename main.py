"""
main.py

Script command (default):
    python main.py --topic "Why is the sky blue?" --niche niches/example.json
    python main.py --topic "The science of sleep" --niche niches/example.json --job-id sleep-001

    Writes output/<job_id>/script.json with approved=false and exits.
    It does not render or publish.

Dry run (render only an already approved script):
    python main.py --dry-run --job-id sleep-001
    python main.py --dry-run sleep-001

    Loads output/<job_id>/script.json. Renders only when approved is true.
    Does not upload or publish.

--publish is refused. It is reserved for a later pass.
"""
import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from agents import agent1_researcher
from core.config import settings
from core.logger import get_logger
from core.models import JobState, ScriptPayload, new_job_id, validate_job_id
from core.niche import load_niche

logger = get_logger("orchestrator")


def run_script(
    topic: str,
    niche_path: str | Path,
    job_id: str | None = None,
    client: object | None = None,
) -> JobState:
    """
    Generate one unapproved script and return.

    Media, render, and publish are not imported and not called.
    """
    cleaned_topic = topic.strip()
    if not cleaned_topic:
        raise ValueError("topic is required")

    resolved_job_id = new_job_id() if job_id is None else job_id
    validate_job_id(resolved_job_id)
    niche = load_niche(niche_path)

    logger.info(
        "Script run starting",
        extra={"job_id": resolved_job_id, "topic": cleaned_topic, "niche": niche.name},
    )
    state = JobState(job_id=resolved_job_id, topic=cleaned_topic, niche=niche)
    state = agent1_researcher.run(state, client=client)
    logger.info(
        "Script run finished",
        extra={"job_id": resolved_job_id, "approved": False},
    )
    return state


def run_dry(
    job_id: str,
    *,
    synthesize=None,
    fetch_video=None,
    run_ffmpeg=None,
    encoder: str | None = None,
    fontfile: Path | None = None,
) -> JobState:
    """
    Render an approved job. Refuse when approved is false.

    The publisher module is not imported.
    """
    validated = validate_job_id(job_id)
    script = _load_job_script(validated)
    if script.approved is not True:
        raise ValueError(
            f"Refusing to render job {validated}: approved is false in script.json. "
            "Set approved to true after review, then re-run --dry-run. "
            "Nothing was uploaded or published."
        )

    from agents import agent2_media, agent3_editor

    logger.info("Dry run starting", extra={"job_id": validated})
    state = JobState(job_id=validated, topic=script.topic, script=script)
    state = agent2_media.run(state, synthesize=synthesize, fetch_video=fetch_video)
    state = agent3_editor.run(
        state,
        run_ffmpeg=run_ffmpeg,
        encoder=encoder,
        fontfile=fontfile,
    )
    logger.info(
        "Dry run finished",
        extra={"job_id": validated, "output": str(state.final_reel_path)},
    )
    return state


def _load_job_script(job_id: str) -> ScriptPayload:
    script_path = settings.output_dir / job_id / "script.json"
    if not script_path.is_file():
        raise FileNotFoundError(f"No script.json for job {job_id} under {settings.output_dir}")
    return ScriptPayload.model_validate_json(script_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write an unapproved Reel script, or render an approved one.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="Topic for the reel.\nExample: 'Why is the sky blue?'",
    )
    parser.add_argument(
        "--niche",
        default=None,
        help="Path to a niche JSON profile.\nExample: niches/example.json",
    )
    parser.add_argument(
        "--job-id",
        default=None,
        metavar="ID",
        help="Job id matching ^[a-z0-9-]{1,32}$.\nRequired with --dry-run unless the id is passed as --dry-run ID.",
    )
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const=True,
        default=False,
        metavar="JOB_ID",
        help=(
            "Render an existing approved script and stop.\n"
            "Examples:\n"
            "  python main.py --dry-run --job-id sleep-001\n"
            "  python main.py --dry-run sleep-001"
        ),
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Refused. Later: the only Meta call, and only if approved.",
    )
    args = parser.parse_args(argv)

    if args.publish:
        parser.error(
            "--publish is not implemented. It will be the only Meta call, "
            "and only when approved is true."
        )

    try:
        if args.dry_run:
            job_id = _dry_run_job_id(args.dry_run, args.job_id)
            state = run_dry(job_id)
            print(f"\n{'=' * 60}")
            print("  Rendered. Not published.")
            print(f"  Job ID       : {state.job_id}")
            print(f"  Reel         : {state.final_reel_path}")
            print("  Approved     : true")
            print(f"{'=' * 60}\n")
            return 0

        if not args.topic or not args.niche:
            parser.error("--topic and --niche are required to write a script.")
        state = run_script(topic=args.topic, niche_path=args.niche, job_id=args.job_id)
    except (ValueError, FileNotFoundError, ValidationError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    script_path = settings.output_dir / state.job_id / "script.json"
    print(f"\n{'=' * 60}")
    print("  Script written. Not approved.")
    print(f"  Job ID       : {state.job_id}")
    print(f"  Niche        : {state.niche.name if state.niche else ''}")
    print(f"  Script       : {script_path}")
    print("  Approved     : false")
    print(f"{'=' * 60}\n")
    return 0


def _dry_run_job_id(dry_run: str | bool, job_id: str | None) -> str:
    if isinstance(dry_run, str):
        if job_id and job_id != dry_run:
            raise ValueError("--dry-run job id and --job-id disagree")
        return dry_run
    if not job_id:
        raise ValueError(
            "--dry-run needs a job id. Use --dry-run JOB_ID or --dry-run --job-id JOB_ID."
        )
    return job_id


if __name__ == "__main__":
    raise SystemExit(main())
