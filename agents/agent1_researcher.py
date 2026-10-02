"""
agents/agent1_researcher.py
Script stage.

One Gemini Flash call (google-genai) returns the full script JSON. Caption and
Pexels query come from that same object. Nothing is approved here.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.config import settings
from core.logger import get_logger
from core.models import JobState, ScriptDraft, ScriptPayload, validate_job_id

logger = get_logger(__name__)

# Keys accepted by Gemini structured output via response_json_schema
# (google-genai GenerateContentConfig). Local pydantic constraints such as
# minLength are enforced after the response, not sent to the API.
_SCHEMA_KEYS = {
    "$id",
    "$defs",
    "$ref",
    "$anchor",
    "type",
    "format",
    "title",
    "description",
    "enum",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "anyOf",
    "oneOf",
    "properties",
    "additionalProperties",
    "required",
    "propertyOrdering",
}

_SYSTEM_PROMPT = """\
You write one Instagram Reel script as a single JSON object.

Facts are not retrieval-grounded. Do not invent citations. Every factual
statement in the narration must be repeated, in plain language, in
claims_to_verify so a human can fact-check it before anything is recorded
or posted.

Follow the niche profile exactly: audience, tone, language, duration,
do/don't rules, and hashtag pool. The niche is configuration supplied by
the caller, not a suggestion.

Do not include an approval field. You cannot approve the script.
"""


def run(state: JobState, client: Any | None = None) -> JobState:
    """Generate one script and write output/<job_id>/script.json."""
    validate_job_id(state.job_id)
    if state.niche is None:
        raise ValueError("a niche profile is required")

    logger.info(
        "Script stage starting",
        extra={"job_id": state.job_id, "topic": state.topic, "niche": state.niche.name},
    )

    owns_client = client is None
    client = client or _default_client()
    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=_user_prompt(state),
            config={
                "system_instruction": _SYSTEM_PROMPT,
                "response_mime_type": "application/json",
                "response_json_schema": _schema_for_gemini(ScriptDraft),
            },
        )
        raw_text = getattr(response, "text", "")
    finally:
        if owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    draft = ScriptDraft.model_validate_json(_json_text(raw_text))
    script = ScriptPayload(
        title=draft.title,
        hook=draft.hook,
        on_screen_hook=draft.on_screen_hook,
        narration=draft.narration,
        pexels_query=draft.pexels_query,
        caption=draft.caption,
        hashtags=_align_hashtags(draft.hashtags, state.niche.hashtag_pool),
        claims_to_verify=draft.claims_to_verify,
        approved=False,
        topic=state.topic,
        niche=state.niche.name,
        language=state.niche.language,
        duration_seconds=state.niche.duration_seconds,
        voice=state.niche.voice or settings.kokoro_voice or "af_heart",
        kokoro_lang=state.niche.kokoro_lang or settings.kokoro_lang or "a",
    )
    # The model cannot approve the script. Stamp false again at write time.
    script.approved = False

    output_dir: Path = settings.output_dir / state.job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    script_path = output_dir / "script.json"
    script_path.write_text(script.model_dump_json(indent=2) + "\n", encoding="utf-8")

    logger.info(
        "Script stage complete",
        extra={
            "job_id": state.job_id,
            "title": script.title,
            "approved": script.approved,
            "path": str(script_path),
        },
    )
    state.script = script
    return state


def _default_client() -> Any:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required to generate a script.")
    from google import genai

    return genai.Client(api_key=settings.gemini_api_key)


def _user_prompt(state: JobState) -> str:
    niche = state.niche
    assert niche is not None
    low, high = _word_range(niche.duration_seconds)
    return (
        f"Topic: {state.topic}\n\n"
        "Niche profile:\n"
        f"{niche.model_dump_json(indent=2)}\n\n"
        f"Write one Reel script in {niche.language}.\n"
        "- Spoken hook: one or two concrete sentences.\n"
        "- On-screen hook: at most 8 words, different wording from the spoken hook.\n"
        f"- Narration: about {low}-{high} words of natural speech for "
        f"{niche.duration_seconds} seconds.\n"
        "- pexels_query: 2-5 words for a vertical stock clip. "
        "No people, faces, logos, or on-screen text.\n"
        "- caption: caption body without hashtags, no leading emoji, "
        "with one save-or-share line.\n"
        "- hashtags: 4 to 8 tags chosen only from hashtag_pool.\n"
        "- claims_to_verify: every factual claim the narration makes, one string each.\n"
    )


def _word_range(duration_seconds: int) -> tuple[int, int]:
    target = max(40, round(duration_seconds * 130 / 60))
    return max(30, target - 15), target + 15


def _align_hashtags(tags: list[str], pool: list[str]) -> list[str]:
    """Keep model hashtags that belong to the niche pool. No second model call."""
    allowed = {tag.lower(): tag for tag in pool}
    chosen: list[str] = []
    for tag in tags:
        text = tag.strip()
        if not text.startswith("#"):
            text = "#" + text
        canonical = allowed.get(text.lower())
        if canonical and canonical not in chosen:
            chosen.append(canonical)
    if not chosen:
        chosen = list(pool[: min(5, len(pool))])
    return chosen[:8]


def _json_text(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        raise ValueError("Gemini returned an empty script response")
    text = str(raw).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    return text


def _schema_for_gemini(model: type[BaseModel]) -> dict:
    """Drop JSON Schema keywords Gemini structured output does not accept.

    `properties` and `$defs` are maps of user field names to schemas. Those
    names are kept; only the schema objects under them are filtered.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key not in _SCHEMA_KEYS:
                continue
            if key in {"properties", "$defs"} and isinstance(value, dict):
                cleaned[key] = {name: walk(subschema) for name, subschema in value.items()}
            else:
                cleaned[key] = walk(value)
        return cleaned

    schema = walk(model.model_json_schema())
    # Never ask the model to emit an approval flag.
    properties = schema.get("properties") or {}
    properties.pop("approved", None)
    required = [name for name in schema.get("required", []) if name != "approved"]
    if required:
        schema["required"] = required
    return schema
