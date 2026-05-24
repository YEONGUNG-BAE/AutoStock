from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from broker.kis_transport import _append_query_params


def test_append_query_params_returns_url_when_params_empty() -> None:
    assert _append_query_params("https://example.com/path", None) == "https://example.com/path"
    assert _append_query_params("https://example.com/path", {}) == "https://example.com/path"


def test_append_query_params_uses_question_mark_for_first_query() -> None:
    result = _append_query_params("https://example.com/path", {"SYMB": "AAPL"})

    assert result == "https://example.com/path?SYMB=AAPL"


def test_append_query_params_uses_ampersand_when_query_exists() -> None:
    result = _append_query_params("https://example.com/path?existing=1", {"SYMB": "AAPL"})

    assert result == "https://example.com/path?existing=1&SYMB=AAPL"


def test_append_query_params_url_encodes_special_characters() -> None:
    result = _append_query_params(
        "https://example.com/path",
        {"SYMB": "A&B", "NAME": "삼성 전자"},
    )

    assert "A%26B" in result
    assert " " not in result.split("?", 1)[1]
    assert "삼성" not in result
    assert "SYMB=A%26B" in result
