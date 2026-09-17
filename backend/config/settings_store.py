"""Read and write the user .env configuration file."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


_LLM_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "ZAI_API_KEY",
    "MOONSHOT_API_KEY",
    "MINIMAX_API_KEY",
}


def get_env_path() -> Path:
    """Return the effective .env file path for dev or packaged mode."""
    if getattr(sys, "frozen", False):
        system = platform.system()
        if system == "Darwin":
            base = Path.home() / "Library" / "Application Support" / "AIHunter"
        elif system == "Windows":
            import os

            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "AIHunter"
        else:
            base = Path.home() / ".config" / "AIHunter"
        base.mkdir(parents=True, exist_ok=True)
        return base / ".env"
    return _BACKEND_ROOT / ".env"


def _escape_value(value: str) -> str:
    """Escape a settings value for single-line .env storage.

    Backslashes are escaped first (so the subsequent passes don't
    double-escape them), then real newlines/carriage-returns so that
    multi-line values like ``LLM_SYSTEM_PROMPT_OVERRIDE`` survive a
    write → read roundtrip without truncation.

    ``\\r\\n`` must be escaped *before* the individual ``\\n`` / ``\\r``
    replacements so that the pair is preserved as ``\\r\\n`` (two escape
    sequences) rather than collapsed into a single ``\\n``.
    """
    return (
        value
        .replace("\\", "\\\\")   # must be first — protect real backslashes
        .replace("\r\n", "\\r\\n")  # Windows line endings before individual chars
        .replace("\n", "\\n")
        .replace("\r", "\\r")    # bare CR (rare, but kept for roundtrip correctness)
    )


def _unescape_value(value: str) -> str:
    """Reverse ``_escape_value``; process escape sequences in one pass."""
    result: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "n":
                result.append("\n")
                i += 2
            elif nxt == "r":
                result.append("\r")
                i += 2
            elif nxt == "\\":
                result.append("\\")
                i += 2
            else:
                # Unknown escape sequence — keep as-is (e.g. \t, \s …)
                result.append(value[i])
                i += 1
        else:
            result.append(value[i])
            i += 1
    return "".join(result)


def read_settings() -> dict[str, str]:
    """Parse the .env file into a KEY -> value mapping.

    Values are single-line; ``_unescape_value`` restores any ``\\n`` /
    ``\\r`` / ``\\\\`` sequences that were written by ``write_settings``.
    """
    path = get_env_path()
    if not path.exists():
        return {}

    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = _unescape_value(value.strip())
    return result


def write_settings(data: dict[str, str]) -> None:
    """Overwrite the .env file with the provided mapping.

    Values are single-line; real newlines are escaped to ``\\n`` so that
    multi-line settings (e.g. ``LLM_SYSTEM_PROMPT_OVERRIDE``) survive a
    write → read roundtrip without truncation.
    """
    path = get_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={_escape_value(str(value))}\n" for key, value in data.items()]
    path.write_text("".join(lines), encoding="utf-8")


def update_settings(updates: dict[str, str]) -> None:
    """Merge updates into the existing .env file."""
    existing = read_settings()
    existing.update(updates)
    write_settings(existing)


def is_configured() -> bool:
    """Return True when at least one LLM key is configured."""
    settings = read_settings()
    return any(settings.get(key, "").strip() for key in _LLM_KEYS)
