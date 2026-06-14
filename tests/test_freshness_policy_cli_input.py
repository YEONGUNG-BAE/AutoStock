"""RTM-7c.4m — strict ASCII decimal max-age CLI input parser tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.freshness_policy_cli_input import parse_max_age_microseconds_cli_input


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("1", 1),
        ("300000000", 300000000),
        ("999999999999999999999", 999999999999999999999),
    ],
    ids=["zero", "one", "typical", "large"],
)
def test_accepts_ascii_decimal_integers(raw: str, expected: int) -> None:
    value, error = parse_max_age_microseconds_cli_input(raw)
    assert error is None
    assert value == expected
    assert type(value) is int


@pytest.mark.parametrize(
    "raw",
    [
        "-1",
        "+1",
        "1.0",
        "1e6",
        " 1",
        "1 ",
        "１２３",
        "١٢٣",
        "",
        "abc",
    ],
    ids=[
        "negative",
        "plus_sign",
        "decimal",
        "exponent",
        "leading_space",
        "trailing_space",
        "fullwidth",
        "arabic_indic",
        "empty",
        "alphabetic",
    ],
)
def test_rejects_invalid_tokens(raw: str) -> None:
    value, error = parse_max_age_microseconds_cli_input(raw)
    assert value is None
    assert error == "freshness_policy_input_invalid"


def test_accepts_leading_zero_ascii_decimal() -> None:
    value, error = parse_max_age_microseconds_cli_input("01")
    assert error is None
    assert value == 1


def test_missing_argument() -> None:
    value, error = parse_max_age_microseconds_cli_input(None)
    assert value is None
    assert error == "freshness_policy_input_missing"


def test_module_has_no_side_effects() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "freshness_policy_cli_input.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "os.environ",
        "load_settings",
        "datetime.now",
        "sqlite3",
        "open(",
        "requests",
    ):
        assert forbidden not in source
