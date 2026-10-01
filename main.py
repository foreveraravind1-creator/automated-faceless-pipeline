"""
main.py
Script-stage entrypoint.

Usage:
    python main.py --topic "Why is the sky blue?" --niche niches/example.json
    python main.py --topic "The science of sleep" --niche niches/example.json --job-id sleep-001

The command writes output/<job_id>/script.json with approved=false and stops.
It does not render or publish. --dry-run and --publish are refused until a later pass.
"""
import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from agents import agent1_researcher
from core.config import settings
from core.logger import get_logger
from core.models import JobState, new_job_id, validate_job_id
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write an unapproved Reel script and stop.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--topic",
        required=True,
        help="Topic for the reel.\nExample: 'Why is the sky blue?'",
    )
    parser.add_argument(
        "--niche",
        required=True,
        help="Path to a niche JSON profile.\nExample: niches/example.json",
    )
    parser.add_argument(
        "--job-id",
        default=None,
        metavar="ID",
        help="Optional job id matching ^[a-z0-9-]{1,32}$ (auto-generated if omitted).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Refused in this pass. Later: render only, no publish.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Refused in this pass. Later: the only Meta call, and only if approved.",
    )
    args = parser.parse_args(argv)

    if args.dry_run or args.publish:
        parser.error(
            "--dry-run and --publish are not implemented yet. "
            "This command only writes an unapproved script.json and stops."
        )

    try:
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


if __name__ == "__main__":
    raise SystemExit(main())
