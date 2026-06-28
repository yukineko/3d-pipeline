"""
render/obs.py - Lightweight observability helpers (pure Python, no bpy).

Provides:
  * get_logger(name)      -> a stderr logging.Logger honoring VRM_LOG_LEVEL,
                             idempotent (no duplicate handlers), non-propagating.
  * write_apply_log(...)  -> persist an apply-report dict as apply_log.json next
                             to a ledger record (best-effort, schema-free).

This module is intentionally free of any Blender (bpy) imports so it can be used
from both the host wrapper and unit tests in a bare (no-Blender) environment.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Matches the project's existing "[name] LEVEL message" stderr convention.
_LOG_FORMAT = "[%(name)s] %(levelname)s %(message)s"

_DEFAULT_LEVEL = "INFO"


def get_logger(name: str = "vrm") -> logging.Logger:
    """
    Return a logger that writes to stderr using the project's
    "[name] LEVEL message" format.

    Level is taken from the ``VRM_LOG_LEVEL`` environment variable (default
    "INFO"); an invalid/unknown value falls back to INFO rather than crashing.

    Idempotent: calling twice for the same name does NOT add duplicate handlers.
    """
    logger = logging.getLogger(name)

    level_name = os.environ.get("VRM_LOG_LEVEL", _DEFAULT_LEVEL)
    level = logging.getLevelName(str(level_name).upper())
    # logging.getLevelName returns an int for known names, else a str like
    # "Level NOTALEVEL" — fall back to INFO for anything non-int.
    if not isinstance(level, int):
        level = logging.INFO
    logger.setLevel(level)

    # Only attach a handler once; avoid duplicate output on repeated calls.
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)

    logger.propagate = False
    return logger


def write_apply_log(out_dir: str | Path, payload: dict) -> str:
    """
    Persist ``payload`` as JSON to ``out_dir/apply_log.json`` and return the
    file path as a str.

    ``out_dir`` is created (parents=True, exist_ok=True) if missing. No schema is
    enforced on ``payload`` — the dict is persisted exactly as given.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    log_path = out_path / "apply_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return str(log_path)
