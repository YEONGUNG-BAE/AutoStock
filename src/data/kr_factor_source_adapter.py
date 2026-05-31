"""Real Intake 3G4-4 — fixture-first KR factor source adapter.

source-specific local factor payload → canonical 3G4-1 factor input TOML.
live fetch/env/API key/trading 호출 없음.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from data.dart_corp_code_resolver import DartCorpCodeResolverError, normalize_stock_code
from data.kr_factor_signal_generator import (
    KrFactorInputEntry,
    KrFactorInputSet,
    KrFactorSignalGeneratorError,
    load_kr_factor_inputs_toml,
)

StageName = Literal["parse", "map", "write", "validate"]

_EXPECTED_SOURCE_KEY = "kr_factor_source"
_EXPECTED_SOURCE_FORMAT = "synthetic_factor_v1"
_EXPECTED_MARKET = "KR"

_SOURCE_ROOT_KEYS = frozenset(
    {
        "source_key",
        "source_format",
        "snapshot_version",
        "market",
        "as_of",
        "external_service",
        "universe_hint",
        "items",
    }
)
_SOURCE_ITEM_KEYS = frozenset(
    {
        "ticker",
        "displayName",
        "liquidityPercentile",
        "marketCapPercentile",
        "profitabilityScore",
        "balanceSheetScore",
        "momentumPercentile",
        "volatilityRisk",
        "note",
        "sectorCode",
        "lastUpdated",
    }
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "corp_code",
        "corpCode",
        "yfinance_provider_symbol",
        "provider_symbol",
        "providerSymbol",
        "stockProviderSymbol",
        "action",
        "side",
        "buy",
        "sell",
        "hold",
        "target_weight",
        "targetAllocation",
        "target_allocation",
        "quantity",
        "order",
        "order_type",
        "price_target",
        "stop_loss",
        "take_profit",
    }
)


class KrFactorSourceAdapterError(ValueError):
    """KR factor source adapter 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class KrFactorSourceItem:
    """source-specific factor item (provider-shaped; not canonical)."""

    ticker: str
    liquidity_percentile: float
    market_cap_percentile: float
    profitability_score: float
    balance_sheet_score: float
    momentum_percentile: float
    volatility_risk: float
    note: str | None


@dataclass(frozen=True)
class KrFactorSourcePayload:
    """source-specific KR factor payload root document."""

    source_key: str
    source_format: str
    snapshot_version: int
    market: str
    as_of: datetime
    external_service: str
    universe_hint: str
    items: tuple[KrFactorSourceItem, ...]


def load_kr_factor_source_payload(path: Path) -> KrFactorSourcePayload:
    """로컬 source-specific factor JSON payload를 strict schema로 파싱한다."""
    if not path.is_file():
        raise KrFactorSourceAdapterError("parse", f"factor source payload file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KrFactorSourceAdapterError("parse", f"invalid factor source payload JSON: {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise KrFactorSourceAdapterError("parse", "factor source payload root must be a JSON object")

    _reject_forbidden_fields(set(raw.keys()), prefix="")

    unknown_root = set(raw.keys()) - _SOURCE_ROOT_KEYS
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrFactorSourceAdapterError("parse", f"unknown factor source root fields: {joined}")

    source_key = _required_text(raw.get("source_key"), field_name="source_key")
    if source_key != _EXPECTED_SOURCE_KEY:
        raise KrFactorSourceAdapterError("parse", f"source_key must be {_EXPECTED_SOURCE_KEY!r}")

    source_format = _required_text(raw.get("source_format"), field_name="source_format")
    if source_format != _EXPECTED_SOURCE_FORMAT:
        raise KrFactorSourceAdapterError(
            "parse",
            f"source_format must be {_EXPECTED_SOURCE_FORMAT!r}",
        )

    snapshot_version = raw.get("snapshot_version")
    if snapshot_version != 1:
        raise KrFactorSourceAdapterError("parse", "snapshot_version must be exactly 1")

    market = _required_text(raw.get("market"), field_name="market")
    if market != _EXPECTED_MARKET:
        raise KrFactorSourceAdapterError("parse", f"market must be {_EXPECTED_MARKET!r}")

    as_of = _parse_timezone_aware_datetime(raw.get("as_of"), field_name="as_of")
    external_service = _required_text(raw.get("external_service"), field_name="external_service")
    universe_hint = _required_text(raw.get("universe_hint"), field_name="universe_hint")

    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise KrFactorSourceAdapterError("parse", "items must contain at least one entry")

    items: list[KrFactorSourceItem] = []
    for index, entry_raw in enumerate(items_raw):
        if not isinstance(entry_raw, dict):
            raise KrFactorSourceAdapterError("parse", f"items[{index}] must be a JSON object")
        items.append(_parse_source_item(entry_raw, index=index))

    return KrFactorSourcePayload(
        source_key=source_key,
        source_format=source_format,
        snapshot_version=1,
        market=market,
        as_of=as_of,
        external_service=external_service,
        universe_hint=universe_hint,
        items=tuple(items),
    )


def map_kr_factor_source_payload_to_factor_inputs(
    payload: KrFactorSourcePayload,
    *,
    output_name: str,
    output_description: str,
    factor_score_version: str,
) -> KrFactorInputSet:
    """source payload → canonical 3G4-1 factor input document."""
    name = _validate_output_text(output_name, field_name="output_name")
    description = _validate_output_text(output_description, field_name="output_description")
    score_version = _validate_output_text(factor_score_version, field_name="factor_score_version")

    factors: list[KrFactorInputEntry] = []
    seen: set[tuple[str, str]] = set()

    for index, item in enumerate(payload.items):
        prefix = f"items[{index}]"
        try:
            symbol = normalize_stock_code(item.ticker)
        except DartCorpCodeResolverError as exc:
            raise KrFactorSourceAdapterError("map", f"{prefix}.ticker: {exc}") from exc

        key = (payload.market, symbol)
        if key in seen:
            raise KrFactorSourceAdapterError(
                "map",
                f"duplicate normalized factor entry: market={payload.market!r}, symbol={symbol!r}",
            )
        seen.add(key)

        factors.append(
            KrFactorInputEntry(
                symbol=symbol,
                market=payload.market,
                liquidity_percentile=item.liquidity_percentile,
                market_cap_percentile=item.market_cap_percentile,
                profitability_score=item.profitability_score,
                balance_sheet_score=item.balance_sheet_score,
                momentum_percentile=item.momentum_percentile,
                volatility_risk=item.volatility_risk,
                notes=item.note,
            )
        )

    ordered = sorted(factors, key=lambda entry: (entry.market, entry.symbol))
    return KrFactorInputSet(
        version=1,
        name=name,
        description=description,
        as_of=payload.as_of,
        factor_score_version=score_version,
        factors=tuple(ordered),
    )


def render_kr_factor_inputs_toml(factor_inputs: KrFactorInputSet) -> str:
    """canonical factor input document를 3G4-1 TOML schema로 렌더한다."""
    lines = [
        "# Generated KR factor inputs from fixture-first source adapter (3G4-4).",
        "version = 1",
        f"name = {_toml_string(factor_inputs.name)}",
        f"description = {_toml_string(factor_inputs.description)}",
        f"as_of = {_toml_string(factor_inputs.as_of.isoformat())}",
        f"factor_score_version = {_toml_string(factor_inputs.factor_score_version)}",
        "",
    ]
    for entry in factor_inputs.factors:
        lines.extend(
            [
                "[[factors]]",
                f"symbol = {_toml_string(entry.symbol)}",
                f"market = {_toml_string(entry.market)}",
                f"liquidity_percentile = {_format_unit_float(entry.liquidity_percentile)}",
                f"market_cap_percentile = {_format_unit_float(entry.market_cap_percentile)}",
                f"profitability_score = {_format_unit_float(entry.profitability_score)}",
                f"balance_sheet_score = {_format_unit_float(entry.balance_sheet_score)}",
                f"momentum_percentile = {_format_unit_float(entry.momentum_percentile)}",
                f"volatility_risk = {_format_unit_float(entry.volatility_risk)}",
            ]
        )
        if entry.notes is not None:
            lines.append(f"notes = {_toml_string(entry.notes)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_kr_factor_inputs_toml(
    factor_inputs: KrFactorInputSet,
    out_path: Path,
    *,
    force: bool = False,
) -> Path:
    """canonical factor input TOML을 기록하고 3G4-1 parser로 self-validate한다."""
    if out_path.exists() and not force:
        raise KrFactorSourceAdapterError(
            "write",
            f"factor input output already exists: {out_path} (use --force to overwrite)",
        )

    rendered = render_kr_factor_inputs_toml(factor_inputs)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")

    try:
        load_kr_factor_inputs_toml(out_path)
    except KrFactorSignalGeneratorError as exc:
        raise KrFactorSourceAdapterError("validate", exc.message) from exc

    return out_path


def replay_kr_factor_source_payload(
    *,
    source_path: Path,
    factor_inputs_out: Path,
    output_name: str,
    output_description: str | None = None,
    factor_score_version: str,
    force: bool = False,
) -> dict[str, Any]:
    """source payload → canonical factor input TOML replay (local files only)."""
    payload = load_kr_factor_source_payload(source_path)
    effective_description = (
        output_description
        if output_description is not None
        else f"Mapped KR factor inputs from {payload.external_service}."
    )
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name=output_name,
        output_description=effective_description,
        factor_score_version=factor_score_version,
    )
    written = write_kr_factor_inputs_toml(factor_inputs, factor_inputs_out, force=force)
    return {
        "status": "ok",
        "stage": "complete",
        "mode": "fixture-factor-source-adapter",
        "source": str(source_path),
        "factor_inputs_out": str(written),
        "factors_count": len(factor_inputs.factors),
        "factor_score_version": factor_inputs.factor_score_version,
        "as_of": factor_inputs.as_of.isoformat(),
    }


def _parse_source_item(raw: dict[str, Any], *, index: int) -> KrFactorSourceItem:
    prefix = f"items[{index}]"
    _reject_forbidden_fields(set(raw.keys()), prefix=f"{prefix}: ")

    unknown = set(raw.keys()) - _SOURCE_ITEM_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise KrFactorSourceAdapterError("parse", f"{prefix}: unknown fields: {joined}")

    ticker = _required_text(raw.get("ticker"), field_name=f"{prefix}.ticker")

    note_raw = raw.get("note")
    note: str | None
    if note_raw is None:
        note = None
    else:
        note = _required_text(note_raw, field_name=f"{prefix}.note")

    # source-only optional fields: displayName, sectorCode, lastUpdated — parse if present, do not emit
    display_name_raw = raw.get("displayName")
    if display_name_raw is not None:
        _required_text(display_name_raw, field_name=f"{prefix}.displayName")

    sector_code_raw = raw.get("sectorCode")
    if sector_code_raw is not None:
        _required_text(sector_code_raw, field_name=f"{prefix}.sectorCode")

    last_updated_raw = raw.get("lastUpdated")
    if last_updated_raw is not None:
        _parse_timezone_aware_datetime(last_updated_raw, field_name=f"{prefix}.lastUpdated")

    return KrFactorSourceItem(
        ticker=ticker,
        liquidity_percentile=_parse_unit_factor(
            raw.get("liquidityPercentile"),
            field_name=f"{prefix}.liquidityPercentile",
        ),
        market_cap_percentile=_parse_unit_factor(
            raw.get("marketCapPercentile"),
            field_name=f"{prefix}.marketCapPercentile",
        ),
        profitability_score=_parse_unit_factor(
            raw.get("profitabilityScore"),
            field_name=f"{prefix}.profitabilityScore",
        ),
        balance_sheet_score=_parse_unit_factor(
            raw.get("balanceSheetScore"),
            field_name=f"{prefix}.balanceSheetScore",
        ),
        momentum_percentile=_parse_unit_factor(
            raw.get("momentumPercentile"),
            field_name=f"{prefix}.momentumPercentile",
        ),
        volatility_risk=_parse_unit_factor(
            raw.get("volatilityRisk"),
            field_name=f"{prefix}.volatilityRisk",
        ),
        note=note,
    )


def _reject_forbidden_fields(keys: set[str], *, prefix: str) -> None:
    forbidden_present = keys & _FORBIDDEN_FIELDS
    if forbidden_present:
        joined = ", ".join(sorted(forbidden_present))
        raise KrFactorSourceAdapterError("parse", f"{prefix}forbidden fields: {joined}")


def _parse_unit_factor(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KrFactorSourceAdapterError("parse", f"{field_name} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise KrFactorSourceAdapterError("parse", f"{field_name} must be finite")
    if numeric < 0.0 or numeric > 1.0:
        raise KrFactorSourceAdapterError("parse", f"{field_name} must be between 0.0 and 1.0")
    return numeric


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise KrFactorSourceAdapterError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrFactorSourceAdapterError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrFactorSourceAdapterError("parse", f"{field_name} contains a control character")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise KrFactorSourceAdapterError("parse", f"{field_name} must be ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise KrFactorSourceAdapterError("parse", f"{field_name} must be timezone-aware")
    return parsed


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise KrFactorSourceAdapterError("parse", f"{field_name} is required")
    if not isinstance(value, str):
        raise KrFactorSourceAdapterError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrFactorSourceAdapterError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrFactorSourceAdapterError("parse", f"{field_name} contains a control character")
    return normalized


def _validate_output_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise KrFactorSourceAdapterError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrFactorSourceAdapterError("parse", f"{field_name} contains a control character")
    return normalized


def _toml_string(value: str) -> str:
    if _contains_control_character(value):
        raise KrFactorSourceAdapterError("validate", "rendered text contains a control character")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_unit_float(value: float) -> str:
    rounded = float(value)
    if rounded.is_integer():
        return str(int(rounded))
    text = f"{rounded:.10f}".rstrip("0").rstrip(".")
    return text if text else "0"
