"""Real Intake 3G4-1 — fixture-first KR factor signal generator.

로컬 factor input artifact → 3G3-1-compatible ranking signal TOML.
live fetch/env/API key/trading 호출 없음.
"""

from __future__ import annotations

import math
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from data.dart_corp_code_resolver import DartCorpCodeResolverError, normalize_stock_code
from data.kr_candidate_ranker import (
    KrCandidateRankerError,
    KrRankingSignalEntry,
    KrRankingSignalsDocument,
    parse_ranking_signals_toml,
)

StageName = Literal["parse", "generate", "write", "validate"]

SCORE_PRECISION = 4

# 3G3-1 ranking signal document과 동일한 출력 타입 alias
KrRankingSignalSet = KrRankingSignalsDocument

_FACTOR_ROOT_KEYS = frozenset({"version", "name", "description", "as_of", "factor_score_version", "factors"})
_FACTOR_ENTRY_KEYS = frozenset(
    {
        "symbol",
        "market",
        "liquidity_percentile",
        "market_cap_percentile",
        "profitability_score",
        "balance_sheet_score",
        "momentum_percentile",
        "volatility_risk",
        "notes",
    }
)

_FORBIDDEN_FACTOR_FIELDS = frozenset(
    {
        "action",
        "side",
        "buy",
        "sell",
        "hold",
        "target_weight",
        "target_allocation",
        "quantity",
        "order",
        "order_type",
        "price_target",
        "stop_loss",
        "take_profit",
        "corp_code",
        "yfinance_provider_symbol",
    }
)


class KrFactorSignalGeneratorError(ValueError):
    """KR factor signal generator 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class KrFactorInputEntry:
    """fixture factor input entry (local-only; not live market data)."""

    symbol: str
    market: str
    liquidity_percentile: float
    market_cap_percentile: float
    profitability_score: float
    balance_sheet_score: float
    momentum_percentile: float
    volatility_risk: float
    notes: str | None


@dataclass(frozen=True)
class KrFactorInputSet:
    """factor input TOML root document."""

    version: int
    name: str
    description: str
    as_of: datetime
    factor_score_version: str
    factors: tuple[KrFactorInputEntry, ...]


def load_kr_factor_inputs_toml(path: Path) -> KrFactorInputSet:
    """로컬 factor input TOML을 strict schema로 파싱한다."""
    if not path.is_file():
        raise KrFactorSignalGeneratorError("parse", f"factor inputs file not found: {path}")

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    if not isinstance(raw, dict):
        raise KrFactorSignalGeneratorError("parse", "factor inputs TOML root must be a table")

    unknown_root = set(raw.keys()) - _FACTOR_ROOT_KEYS
    forbidden_root = unknown_root & _FORBIDDEN_FACTOR_FIELDS
    if forbidden_root:
        joined = ", ".join(sorted(forbidden_root))
        raise KrFactorSignalGeneratorError("parse", f"forbidden factor input root fields: {joined}")
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrFactorSignalGeneratorError("parse", f"unknown factor input root fields: {joined}")

    version = raw.get("version")
    if version != 1:
        raise KrFactorSignalGeneratorError("parse", "version must be exactly 1")

    name = _required_text(raw.get("name"), field_name="name")
    description = _required_text(raw.get("description"), field_name="description")
    factor_score_version = _required_text(
        raw.get("factor_score_version"),
        field_name="factor_score_version",
    )
    as_of = _parse_timezone_aware_datetime(raw.get("as_of"), field_name="as_of")

    factors_raw = raw.get("factors")
    if not isinstance(factors_raw, list) or not factors_raw:
        raise KrFactorSignalGeneratorError("parse", "factors must contain at least one entry")

    factors: list[KrFactorInputEntry] = []
    for index, entry_raw in enumerate(factors_raw):
        if not isinstance(entry_raw, dict):
            raise KrFactorSignalGeneratorError("parse", f"factors[{index}] must be a table")
        factors.append(_parse_factor_entry(entry_raw, index=index))

    return KrFactorInputSet(
        version=1,
        name=name,
        description=description,
        as_of=as_of,
        factor_score_version=factor_score_version,
        factors=tuple(factors),
    )


def generate_ranking_signals_from_factors(
    factor_inputs: KrFactorInputSet,
    *,
    output_name: str,
    output_description: str,
) -> KrRankingSignalSet:
    """factor input → 3G3-1-compatible ranking signal document."""
    output_name_clean = _required_text(output_name, field_name="output_name")
    output_description_clean = _required_text(output_description, field_name="output_description")

    signals: list[KrRankingSignalEntry] = []
    seen: set[tuple[str, str]] = set()

    for factor in factor_inputs.factors:
        key = (factor.market, factor.symbol)
        if key in seen:
            raise KrFactorSignalGeneratorError(
                "generate",
                f"duplicate normalized factor entry: market={factor.market!r}, symbol={factor.symbol!r}",
            )
        seen.add(key)

        try:
            signal = _factor_to_ranking_signal(factor)
        except KrFactorSignalGeneratorError:
            raise
        except Exception as exc:
            raise KrFactorSignalGeneratorError("generate", str(exc)) from exc
        signals.append(signal)

    ordered = sorted(signals, key=lambda entry: (entry.market, entry.symbol))
    return KrRankingSignalsDocument(
        version=1,
        name=output_name_clean,
        description=output_description_clean,
        as_of=factor_inputs.as_of,
        score_version=factor_inputs.factor_score_version,
        signals=tuple(ordered),
    )


def render_kr_ranking_signals_toml(signal_set: KrRankingSignalSet) -> str:
    """ranking signal document를 3G3-1 TOML schema로 렌더한다."""
    lines = [
        "# Generated KR ranking signals from fixture-first factor inputs (3G4-1).",
        "version = 1",
        f"name = {_toml_string(signal_set.name)}",
        f"description = {_toml_string(signal_set.description)}",
        f"as_of = {_toml_string(signal_set.as_of.isoformat())}",
        f"score_version = {_toml_string(signal_set.score_version)}",
        "",
    ]
    for entry in signal_set.signals:
        lines.extend(
            [
                "[[signals]]",
                f"symbol = {_toml_string(entry.symbol)}",
                f"market = {_toml_string(entry.market)}",
                f"liquidity_score = {_format_score(entry.liquidity_score)}",
                f"market_cap_score = {_format_score(entry.market_cap_score)}",
                f"quality_score = {_format_score(entry.quality_score)}",
                f"momentum_score = {_format_score(entry.momentum_score)}",
                f"risk_penalty = {_format_score(entry.risk_penalty)}",
            ]
        )
        if entry.notes is not None:
            lines.append(f"notes = {_toml_string(entry.notes)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_kr_ranking_signals_toml(
    signal_set: KrRankingSignalSet,
    out_path: Path,
    *,
    force: bool = False,
) -> Path:
    """ranking signal TOML을 기록하고 기존 ranker parser로 self-validate한다."""
    if out_path.exists() and not force:
        raise KrFactorSignalGeneratorError(
            "write",
            f"output already exists: {out_path} (use --force to overwrite)",
        )

    rendered = render_kr_ranking_signals_toml(signal_set)
    _validate_rendered_ranking_signals(rendered)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    return out_path


def generate_kr_factor_signals_file(
    *,
    factor_inputs_path: Path,
    out_signals: Path,
    output_name: str,
    output_description: str,
    force: bool = False,
) -> dict[str, Any]:
    """factor input path → ranking signal TOML (+ summary metadata)."""
    factor_inputs = load_kr_factor_inputs_toml(factor_inputs_path)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name=output_name,
        output_description=output_description,
    )
    written = write_kr_ranking_signals_toml(signal_set, out_signals, force=force)
    return {
        "status": "ok",
        "stage": "complete",
        "mode": "fixture-factor-signal-generator",
        "factor_score_version": factor_inputs.factor_score_version,
        "signals_path": str(written),
        "signals_count": len(signal_set.signals),
        "as_of": factor_inputs.as_of.isoformat(),
    }


def _factor_to_ranking_signal(factor: KrFactorInputEntry) -> KrRankingSignalEntry:
    quality_raw = (factor.profitability_score + factor.balance_sheet_score) / 2.0
    return KrRankingSignalEntry(
        symbol=factor.symbol,
        market=factor.market,
        liquidity_score=_round_score(factor.liquidity_percentile),
        market_cap_score=_round_score(factor.market_cap_percentile),
        quality_score=_round_score(quality_raw),
        momentum_score=_round_score(factor.momentum_percentile),
        risk_penalty=_round_score(factor.volatility_risk),
        notes=factor.notes,
    )


def _parse_factor_entry(raw: dict[str, Any], *, index: int) -> KrFactorInputEntry:
    prefix = f"factors[{index}]"
    unknown = set(raw.keys()) - _FACTOR_ENTRY_KEYS
    forbidden = unknown & _FORBIDDEN_FACTOR_FIELDS
    if forbidden:
        joined = ", ".join(sorted(forbidden))
        raise KrFactorSignalGeneratorError("parse", f"{prefix}: forbidden fields: {joined}")
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise KrFactorSignalGeneratorError("parse", f"{prefix}: unknown fields: {joined}")

    market = _required_text(raw.get("market"), field_name=f"{prefix}.market")
    if market != "KR":
        raise KrFactorSignalGeneratorError("parse", f"{prefix}.market must be 'KR'")

    symbol_raw = _required_text(raw.get("symbol"), field_name=f"{prefix}.symbol")
    try:
        symbol = normalize_stock_code(symbol_raw)
    except DartCorpCodeResolverError as exc:
        raise KrFactorSignalGeneratorError("parse", str(exc)) from exc

    notes_raw = raw.get("notes")
    notes: str | None
    if notes_raw is None:
        notes = None
    else:
        notes = _required_text(notes_raw, field_name=f"{prefix}.notes")

    return KrFactorInputEntry(
        symbol=symbol,
        market=market,
        liquidity_percentile=_parse_unit_factor(
            raw.get("liquidity_percentile"),
            field_name=f"{prefix}.liquidity_percentile",
        ),
        market_cap_percentile=_parse_unit_factor(
            raw.get("market_cap_percentile"),
            field_name=f"{prefix}.market_cap_percentile",
        ),
        profitability_score=_parse_unit_factor(
            raw.get("profitability_score"),
            field_name=f"{prefix}.profitability_score",
        ),
        balance_sheet_score=_parse_unit_factor(
            raw.get("balance_sheet_score"),
            field_name=f"{prefix}.balance_sheet_score",
        ),
        momentum_percentile=_parse_unit_factor(
            raw.get("momentum_percentile"),
            field_name=f"{prefix}.momentum_percentile",
        ),
        volatility_risk=_parse_unit_factor(
            raw.get("volatility_risk"),
            field_name=f"{prefix}.volatility_risk",
        ),
        notes=notes,
    )


def _validate_rendered_ranking_signals(rendered: str) -> KrRankingSignalsDocument:
    """rendered TOML을 기존 3G3-1 parser로 검증한다."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", encoding="utf-8", delete=True) as handle:
        handle.write(rendered)
        handle.flush()
        try:
            return parse_ranking_signals_toml(Path(handle.name))
        except KrCandidateRankerError as exc:
            raise KrFactorSignalGeneratorError("validate", exc.message) from exc


def _parse_unit_factor(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must be finite")
    if numeric < 0.0 or numeric > 1.0:
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must be between 0.0 and 1.0")
    return numeric


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrFactorSignalGeneratorError("parse", f"{field_name} contains a control character")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must be ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must be timezone-aware")
    return parsed


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise KrFactorSignalGeneratorError("parse", f"{field_name} is required")
    if not isinstance(value, str):
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrFactorSignalGeneratorError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrFactorSignalGeneratorError("parse", f"{field_name} contains a control character")
    return normalized


def _round_score(value: float) -> float:
    return round(value, SCORE_PRECISION)


def _format_score(value: float) -> str:
    return f"{_round_score(value):.{SCORE_PRECISION}f}"


def _toml_string(value: str) -> str:
    if _contains_control_character(value):
        raise KrFactorSignalGeneratorError("validate", "rendered text contains a control character")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
