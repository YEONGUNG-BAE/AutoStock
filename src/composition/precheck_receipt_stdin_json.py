"""Fail-closed untrusted stdin JSON parsing for receipt verification CLI (RTM-7c.4e).

Duplicate object keys, non-standard JSON constants, pathological nesting/integers는
sanitized reason code로만 fail-closed — raw token/key/value/traceback 미포함.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "ReceiptStdinJsonError",
    "parse_receipt_stdin_json",
]


class ReceiptStdinJsonError(Exception):
    """stdin JSON parse 실패 — ``reason_code``만 carry."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _DuplicateKeyError(ReceiptStdinJsonError):
    """Duplicate object member — key 이름을 메시지에 넣지 않는다."""

    def __init__(self) -> None:
        super().__init__("receipt_input_duplicate_key")


def _reject_non_finite(_: str) -> None:
    raise ValueError("non-finite constant rejected")


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKeyError()
        seen.add(key)
        result[key] = value
    return result


def parse_receipt_stdin_json(text: str) -> object:
    """Strict receipt stdin JSON parse — 예외는 ``ReceiptStdinJsonError``로만 surface."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs_no_duplicates,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateKeyError:
        raise
    except RecursionError as exc:
        raise ReceiptStdinJsonError("receipt_input_too_deep") from exc
    except json.JSONDecodeError as exc:
        raise ReceiptStdinJsonError("receipt_input_not_json") from exc
    except ValueError as exc:
        raise ReceiptStdinJsonError("receipt_input_not_json") from exc
