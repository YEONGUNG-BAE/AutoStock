"""Strict ASCII decimal CLI input for explicit freshness max-age (RTM-7c.4m).

Parses ``--max-age-microseconds`` only. No default threshold, no config/env binding,
no implicit coercion of non-ASCII digits or scientific notation.
"""

from __future__ import annotations

import re

__all__ = [
    "parse_max_age_microseconds_cli_input",
]

_ASCII_DECIMAL = re.compile(r"^[0-9]+$")


def parse_max_age_microseconds_cli_input(raw: str | None) -> tuple[int | None, str | None]:
    """Parse explicit CLI max-age microsecond token.

    Returns ``(value, None)`` on success or ``(None, reason_code)`` on failure.
    Reason codes are stable and sanitized — no raw token echo."""

    if raw is None:
        return None, "freshness_policy_input_missing"
    if not _ASCII_DECIMAL.match(raw):
        return None, "freshness_policy_input_invalid"
    # ``^[0-9]+$`` guarantees a non-empty ASCII decimal; ``int`` is exact built-in.
    return int(raw), None
