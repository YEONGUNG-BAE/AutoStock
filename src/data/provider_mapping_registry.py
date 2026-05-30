from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data.dart_corp_code_resolver import DartCorpCodeResolverError, normalize_stock_code
from domain._strings import normalize_required_string
from domain.universe import UniverseDefinition

# 3D1: fixture-first provider mapping registry. network/env/API key/read/write 없음.

_KR_YFINANCE_SUFFIXES = (".KS", ".KQ")
_SUPPORTED_MARKETS = frozenset({"KR", "US"})


class ProviderMappingError(ValueError):
    """provider mapping registry 파싱·조회·universe coverage 검증 실패."""


@dataclass(frozen=True)
class YFinanceProviderMapping:
    """yfinance provider 식별자 매핑."""

    provider_symbol: str
    currency: str | None = None


@dataclass(frozen=True)
class DartProviderMapping:
    """OpenDART provider 식별자 매핑."""

    corp_code: str
    stock_code: str
    corp_name: str | None = None


@dataclass(frozen=True)
class ProviderMappingEntry:
    """단일 (market, symbol) provider routing entry."""

    symbol: str
    market: str
    display_name: str | None
    stock_code: str | None
    yfinance: YFinanceProviderMapping | None
    dart: DartProviderMapping | None
    enabled: bool = True
    notes: str | None = None


@dataclass(frozen=True)
class ProviderMappingRegistry:
    """provider mapping registry root."""

    version: int
    name: str
    description: str | None
    mappings: tuple[ProviderMappingEntry, ...]

    def resolve(self, *, symbol: str, market: str) -> ProviderMappingEntry:
        """(market, symbol)로 registry entry를 조회한다."""
        normalized_symbol = normalize_required_string(symbol, field_name="symbol")
        normalized_market = normalize_required_string(market, field_name="market")
        for entry in self.mappings:
            if entry.symbol == normalized_symbol and entry.market == normalized_market:
                return entry
        raise ProviderMappingError(
            f"no provider mapping for market={normalized_market!r}, symbol={normalized_symbol!r}"
        )

    @property
    def enabled_mappings(self) -> tuple[ProviderMappingEntry, ...]:
        """enabled=true registry entry만 반환한다."""
        return tuple(entry for entry in self.mappings if entry.enabled)


def load_provider_mapping_toml(path: Path | str) -> ProviderMappingRegistry:
    """provider mapping registry TOML을 읽어 검증한다."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"provider mapping file not found: {file_path}")

    with file_path.open("rb") as handle:
        raw = tomllib.load(handle)

    if not isinstance(raw, dict):
        raise ProviderMappingError("provider mapping TOML root must be a table")

    version = raw.get("version")
    if version != 1:
        raise ProviderMappingError("version must be exactly 1")

    name = _required_text(raw.get("name"), field_name="name")
    description = _optional_text(raw.get("description"), field_name="description")

    mappings_raw = raw.get("mappings")
    if not isinstance(mappings_raw, list) or not mappings_raw:
        raise ProviderMappingError("mappings must contain at least one entry")

    entries: list[ProviderMappingEntry] = []
    seen: set[tuple[str, str]] = set()
    for index, mapping_raw in enumerate(mappings_raw):
        if not isinstance(mapping_raw, dict):
            raise ProviderMappingError(f"mappings[{index}] must be a table")
        entry = _parse_mapping_entry(mapping_raw, index=index)
        key = (entry.market, entry.symbol)
        if key in seen:
            raise ProviderMappingError(
                f"duplicate provider mapping entry: market={entry.market!r}, symbol={entry.symbol!r}"
            )
        seen.add(key)
        entries.append(entry)

    return ProviderMappingRegistry(
        version=1,
        name=name,
        description=description,
        mappings=tuple(entries),
    )


def validate_provider_mappings_cover_universe(
    registry: ProviderMappingRegistry,
    universe: UniverseDefinition,
    *,
    require_yfinance: bool = True,
    require_dart: bool = True,
) -> None:
    """enabled universe symbol마다 enabled provider mapping coverage를 검증한다."""
    for universe_symbol in universe.enabled_symbols:
        try:
            mapping = registry.resolve(
                symbol=universe_symbol.symbol,
                market=universe_symbol.market,
            )
        except ProviderMappingError as exc:
            raise ProviderMappingError(
                "enabled universe symbol missing provider mapping: "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}"
            ) from exc

        if not mapping.enabled:
            raise ProviderMappingError(
                "enabled universe symbol mapped to disabled registry entry: "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}"
            )

        if require_yfinance and mapping.yfinance is None:
            raise ProviderMappingError(
                "enabled universe symbol missing yfinance provider mapping: "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}"
            )

        if require_dart and universe_symbol.market == "KR" and mapping.dart is None:
            raise ProviderMappingError(
                "enabled KR universe symbol missing DART provider mapping: "
                f"symbol={universe_symbol.symbol!r}"
            )


def _parse_mapping_entry(raw: dict[str, Any], *, index: int) -> ProviderMappingEntry:
    prefix = f"mappings[{index}]"
    symbol = _required_text(raw.get("symbol"), field_name=f"{prefix}.symbol")
    market = _required_text(raw.get("market"), field_name=f"{prefix}.market")
    if market not in _SUPPORTED_MARKETS:
        raise ProviderMappingError(f"{prefix}.market must be one of: KR, US")

    display_name = _optional_text(raw.get("display_name"), field_name=f"{prefix}.display_name")
    notes = _optional_text(raw.get("notes"), field_name=f"{prefix}.notes")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ProviderMappingError(f"{prefix}.enabled must be a boolean")

    stock_code: str | None
    if "stock_code" in raw and raw.get("stock_code") is not None:
        stock_code = _normalize_stock_code_field(
            raw.get("stock_code"),
            field_name=f"{prefix}.stock_code",
        )
    else:
        stock_code = None

    yfinance = _parse_yfinance_section(raw.get("yfinance"), prefix=f"{prefix}.yfinance")
    dart = _parse_dart_section(raw.get("dart"), prefix=f"{prefix}.dart")

    entry = ProviderMappingEntry(
        symbol=symbol,
        market=market,
        display_name=display_name,
        stock_code=stock_code,
        yfinance=yfinance,
        dart=dart,
        enabled=enabled,
        notes=notes,
    )
    _validate_entry_market_rules(entry, prefix=prefix)
    return entry


def _parse_yfinance_section(raw: Any, *, prefix: str) -> YFinanceProviderMapping | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProviderMappingError(f"{prefix} must be a table")
    provider_symbol = _required_text(
        raw.get("provider_symbol"),
        field_name=f"{prefix}.provider_symbol",
    )
    currency = _optional_text(raw.get("currency"), field_name=f"{prefix}.currency")
    return YFinanceProviderMapping(provider_symbol=provider_symbol, currency=currency)


def _parse_dart_section(raw: Any, *, prefix: str) -> DartProviderMapping | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProviderMappingError(f"{prefix} must be a table")
    corp_code = _required_text(raw.get("corp_code"), field_name=f"{prefix}.corp_code")
    stock_code = _normalize_stock_code_field(
        raw.get("stock_code"),
        field_name=f"{prefix}.stock_code",
    )
    corp_name = _optional_text(raw.get("corp_name"), field_name=f"{prefix}.corp_name")
    return DartProviderMapping(corp_code=corp_code, stock_code=stock_code, corp_name=corp_name)


def _validate_entry_market_rules(entry: ProviderMappingEntry, *, prefix: str) -> None:
    if entry.market == "US" and entry.dart is not None:
        raise ProviderMappingError(f"{prefix}: US mapping must not include DART provider")

    if entry.market != "KR":
        return

    if entry.yfinance is not None and not entry.yfinance.provider_symbol.endswith(_KR_YFINANCE_SUFFIXES):
        raise ProviderMappingError(
            f"{prefix}.yfinance.provider_symbol must end with .KS or .KQ for KR market"
        )

    if entry.dart is not None:
        if len(entry.dart.corp_code) != 8 or not entry.dart.corp_code.isdigit():
            raise ProviderMappingError(f"{prefix}.dart.corp_code must be exactly 8 digits")


def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise ProviderMappingError(f"{field_name} is required")
    try:
        return normalize_required_string(value, field_name=field_name)
    except ValueError as exc:
        raise ProviderMappingError(str(exc)) from exc


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        return normalize_required_string(value, field_name=field_name)
    except ValueError as exc:
        raise ProviderMappingError(str(exc)) from exc


def _normalize_stock_code_field(value: Any, *, field_name: str) -> str:
    if value is None:
        raise ProviderMappingError(f"{field_name} is required")
    if not isinstance(value, str):
        raise ProviderMappingError(f"{field_name} must be a string")
    try:
        return normalize_stock_code(value)
    except DartCorpCodeResolverError as exc:
        raise ProviderMappingError(f"{field_name}: {exc}") from exc
