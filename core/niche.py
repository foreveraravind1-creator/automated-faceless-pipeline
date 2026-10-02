"""
core/niche.py
Load a niche JSON profile from disk and validate it.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.models import NicheProfile


def load_niche(path: str | Path) -> NicheProfile:
    """Read a niche profile. Raises FileNotFoundError or ValidationError."""
    niche_path = Path(path)
    if not niche_path.is_file():
        raise FileNotFoundError(f"niche profile not found: {path}")
    try:
        data = json.loads(niche_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"niche profile is not valid JSON: {path}") from exc
    return NicheProfile.model_validate(data)
