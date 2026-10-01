"""
agents/agent1_researcher.py
Agent 1 - Researcher & Writer
Model: claude-sonnet-4-5 with extended thinking (budget_tokens=10000)

Input  : JobState.topic (niche topic string)
Output : JobState.script (ScriptPayload), script.json written to disk
"""
import json
import re
from pathlib import Path

import anthropic

from core.config import settings
from core.logger import get_logger
from core.models import JobState, ScriptPayload

logger = get_logger(__name__)

_SYSTEM_PROMPT = """\
You are an expert educational content creator specialising in short-form vertical
video scripts for Instagram Reels.

Your task: write a compelling 60-second educational script about the given topic.

Rules
-----
- Hook   : first 3-5 seconds, must be immediately attention-grabbing.
- Narration: 130-150 words (natural speaking pace ~130 wpm = ~60 s).
- Content : factual, engaging, PG-rated, accessible to a general audience.
- visual_prompt: 3-5 words describing a peaceful nature/abstract scene suitable
  as a non-copyrighted background video (no faces, no logos, no text).

IMPORTANT
---------
Your ENTIRE response MUST be a single valid JSON object and absolutely nothing else.
Do NOT wrap it in markdown fences. Do NOT add explanatory text.

JSON schema:
{
  "title":         "<string, max 100 chars>",
  "hook":          "<string, max 150 chars>",
  "narration":     "<string, 130-150 words>",
  "visual_prompt": "<string, 3-5 words>",
  "duration_hint": 60
}
"""


def run(state: JobState) -> JobState:
    """Run Agent 1 and populate state.script."""
    logger.info("Agent 1 starting", extra={"job_id": state.job_id, "topic": state.topic})

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=16000,
        thinking={
            "type": "enabled",
            "budget_tokens": 10000,
        },
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Create a 60-second educational Instagram Reel script about: {state.topic}"
                ),
            }
        ],
    )

    # Collect only text blocks (skip thinking blocks)
    text_parts = [
        block.text
        for block in response.content
        if block.type == "text"
    ]
    raw_text = "\n".join(text_parts).strip()

    # Extract JSON - handle both bare JSON and fenced code blocks
    json_match = re.search(r"```(?:json)?\s*({.*?})\s*```", raw_text, re.DOTALL)
    if json_match:
        raw_json = json_match.group(1)
    else:
        # Try to find a bare JSON object
        json_match = re.search(r"(\{.*\})", raw_text, re.DOTALL)
        if not json_match:
            raise ValueError(
                f"Agent 1: No valid JSON found in model response:\n{raw_text[:500]}"
            )
        raw_json = json_match.group(1)

    script_dict = json.loads(raw_json)
    script = ScriptPayload(**script_dict)

    # Persist to disk
    output_dir: Path = settings.output_dir / state.job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    script_path = output_dir / "script.json"
    script_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")

    logger.info(
        "Agent 1 complete",
        extra={"job_id": state.job_id, "title": script.title, "words": len(script.narration.split())},
    )
    state.script = script
    return state
