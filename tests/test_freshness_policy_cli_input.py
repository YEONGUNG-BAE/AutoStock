"""RTM-7c.4m — strict ASCII decimal max-age CLI input parser tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import composition.freshness_policy_cli_input as parser_mod
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


@pytest.mark.parametrize(
    "raw",
    [
        "1\n",
        "1\r",
        "1\r\n",
        "1\t",
        "1\v",
        "1\f",
        "\n1",
        "1\n2",
    ],
    ids=["lf", "cr", "crlf", "tab", "vtab", "ff", "leading_lf", "embedded_lf"],
)
def test_rejects_all_whitespace_including_trailing_newline(raw: str) -> None:
    # ``re.match`` + ``$`` would have accepted ``"1\n"``; ``fullmatch`` rejects it.
    value, error = parse_max_age_microseconds_cli_input(raw)
    assert value is None
    assert error == "freshness_policy_input_invalid"


class _StrSubclass(str):
    pass


class _HasStr:
    def __str__(self) -> str:  # pragma: no cover - must never be called by the parser
        return "1"


@pytest.mark.parametrize(
    "raw",
    [1, b"1", object(), _StrSubclass("1"), _HasStr()],
    ids=["int", "bytes", "object", "str_subclass", "custom_str"],
)
def test_rejects_wrong_object_without_raising(raw: object) -> None:
    value, error = parse_max_age_microseconds_cli_input(raw)
    assert value is None
    assert error == "freshness_policy_input_invalid"


def test_overlong_token_is_fail_closed_not_value_error() -> None:
    # A token longer than the runtime int-string-conversion digit limit must not escape.
    value, error = parse_max_age_microseconds_cli_input("9" * 5000)
    assert value is None
    assert error == "freshness_policy_input_invalid"


def test_deterministic_conversion_failure_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raising_int(_: str) -> int:
        raise ValueError("POISON_MAX_AGE")

    monkeypatch.setattr(parser_mod, "int", _raising_int, raising=False)
    value, error = parse_max_age_microseconds_cli_input("1")
    assert value is None
    assert error == "freshness_policy_input_invalid"


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_non_value_error_conversion_exceptions_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
) -> None:
    def _raising_int(_: str) -> int:
        raise exc()

    monkeypatch.setattr(parser_mod, "int", _raising_int, raising=False)
    with pytest.raises(exc):
        parse_max_age_microseconds_cli_input("1")


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
