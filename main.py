"""
main.py
Orchestrator - sequential multi-agent pipeline.

Usage:
    python main.py --topic "5 facts about black holes"
    python main.py --topic "The science of sleep" --job-id my-test-01
"""
import argparse
import uuid
from pathlib import Path

from agents import agent1_researcher, agent2_media, agent3_editor, agent4_publisher
from core.config import settings
from core.logger import get_logger
from core.models import JobState

logger = get_logger("orchestrator")

# Ordered pipeline definition: (display_name, agent_module.run)
_PIPELINE = [
    ("Agent 1 - Researcher & Writer  [claude-sonnet-4-5]", agent1_researcher.run),
    ("Agent 2 - Media Synthesizer    [gemini-2.5-flash]",  agent2_media.run),
    ("Agent 3 - Editor               [gemini-2.5-flash]",  agent3_editor.run),
    ("Agent 4 - Publisher            [gemini-2.5-flash]",  agent4_publisher.run),
]


def run_pipeline(topic: str, job_id: str | None = None) -> JobState:
    """
    Run the full four-agent pipeline synchronously.

    Args:
        topic:  The niche topic string to generate a Reel about.
        job_id: Optional deterministic ID; one is generated if omitted.

    Returns:
        The final JobState with all fields populated.
    """
    if not job_id:
        job_id = str(uuid.uuid4())[:8]

    # Ensure top-level output directory exists
    output_dir: Path = settings.output_dir / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Pipeline starting",
        extra={"job_id": job_id, "topic": topic, "agents": len(_PIPELINE)},
    )

    state = JobState(job_id=job_id, topic=topic)

    for step_num, (name, agent_fn) in enumerate(_PIPELINE, start=1):
        logger.info(f"[{step_num}/{len(_PIPELINE)}] Starting {name}", extra={"job_id": job_id})
        try:
            state = agent_fn(state)
        except Exception as exc:
            logger.error(
                f"[{step_num}/{len(_PIPELINE)}] {name} FAILED",
                extra={"job_id": job_id, "error": str(exc)},
                exc_info=True,
            )
            raise RuntimeError(f"{name} failed: {exc}") from exc
        logger.info(f"[{step_num}/{len(_PIPELINE)}] Completed {name}", extra={"job_id": job_id})

    logger.info(
        "Pipeline complete",
        extra={
            "job_id": job_id,
            "post_id": state.instagram_post_id,
            "gcs_url": state.gcs_public_url,
        },
    )
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Faceless Instagram Reels Pipeline",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--topic",
        required=True,
        help="Niche topic for the educational reel.\nExample: '5 fascinating facts about black holes'",
    )
    parser.add_argument(
        "--job-id",
        default=None,
        metavar="ID",
        help="Optional job identifier (auto-generated if omitted).",
    )
    args = parser.parse_args()

    final_state = run_pipeline(topic=args.topic, job_id=args.job_id)
    print(f"\n{'=' * 60}")
    print(f"  Pipeline finished!")
    print(f"  Job ID          : {final_state.job_id}")
    print(f"  Instagram Post  : {final_state.instagram_post_id}")
    print(f"  GCS URL         : {final_state.gcs_public_url}")
    print(f"  Local output    : {final_state.final_reel_path}")
    print(f"{'=' * 60}\n")
