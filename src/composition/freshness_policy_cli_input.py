"""Strict ASCII decimal CLI input for explicit freshness max-age (RTM-7c.4m).

Parses ``--max-age-microseconds`` only. No default threshold, no config/env binding,
no implicit coercion of non-ASCII digits or scientific notation.

The token is validated as an **entire-string** ASCII decimal (``fullmatch``), so any
surrounding/embedded whitespace — including a trailing newline — is rejected; ``re.match``
plus ``$`` would have accepted a trailing ``\\n``. Only an exact built-in ``str`` is accepted
(``type(raw) is str``), so a non-``str`` object or a ``str`` subclass fails closed rather than
raising. Integer conversion is guarded: a Python integer-string-conversion ``ValueError`` (a
token longer than the runtime digit limit) is normalized to the stable invalid reason, never
escaping as a traceback. A rejected over-long token is a CLI-input-invalid event, **not** a
selection of any freshness max-age upper bound.
"""

from __future__ import annotations

import re

__all__ = [
    "parse_max_age_microseconds_cli_input",
]

_ASCII_DECIMAL = re.compile(r"[0-9]+")


def parse_max_age_microseconds_cli_input(raw: object) -> tuple[int | None, str | None]:
    """Parse explicit CLI max-age microsecond token.

    Returns ``(value, None)`` on success or ``(None, reason_code)`` on failure.
    Reason codes are stable and sanitized — no raw token echo."""

    if raw is None:
        return None, "freshness_policy_input_missing"
    if type(raw) is not str:
        return None, "freshness_policy_input_invalid"
    if _ASCII_DECIMAL.fullmatch(raw) is None:
        return None, "freshness_policy_input_invalid"
    try:
        value = int(raw)
    except ValueError:
        return None, "freshness_policy_input_invalid"
    return value, None
