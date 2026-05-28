from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from domain._strings import normalize_required_string

BaseMarket = Literal["KR", "US", "BOTH"]
_VALID_BASE_MARKETS = frozenset({"KR", "US", "BOTH"})


class UniverseSymbol(BaseModel):
    """Paper pilot Universe v0 symbol entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: str
    display_name: str | None = None
    enabled: bool = True
    tags: tuple[str, ...] = ()
    notes: str | None = None

    @field_validator("symbol", "market", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("display_name", "notes", mode="before")
    @classmethod
    def validate_optional_strings(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ValueError("tags must be a list of strings.")
        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(normalize_required_string(item, field_name=f"tags[{index}]"))
        return tuple(normalized)


class UniverseDefinition(BaseModel):
    """Paper pilot Universe v0 definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    name: str
    description: str
    base_market: BaseMarket
    symbols: tuple[UniverseSymbol, ...]

    @field_validator("name", "description", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("base_market", mode="before")
    @classmethod
    def validate_base_market(cls, value: Any) -> str:
        normalized = normalize_required_string(value, field_name="base_market")
        if normalized not in _VALID_BASE_MARKETS:
            raise ValueError("base_market must be one of: KR, US, BOTH.")
        return normalized

    @model_validator(mode="after")
    def validate_universe(self) -> Self:
        if self.version != 1:
            raise ValueError("version must be exactly 1.")

        if not self.symbols:
            raise ValueError("symbols must contain at least one entry.")

        seen: set[tuple[str, str]] = set()
        for entry in self.symbols:
            key = (entry.market, entry.symbol)
            if key in seen:
                raise ValueError(
                    f"duplicate universe symbol entry: market={entry.market!r}, symbol={entry.symbol!r}"
                )
            seen.add(key)

        if not self.enabled_symbols:
            raise ValueError("universe must contain at least one enabled symbol.")

        return self

    @property
    def enabled_symbols(self) -> tuple[UniverseSymbol, ...]:
        """enabled=true인 symbol entry만 반환한다."""
        return tuple(entry for entry in self.symbols if entry.enabled)


def load_universe_toml(path: Path | str) -> UniverseDefinition:
    """Universe v0 TOML 파일을 읽어 UniverseDefinition으로 검증한다."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"universe file not found: {file_path}")

    with file_path.open("rb") as handle:
        raw = tomllib.load(handle)

    if not isinstance(raw, dict):
        raise ValueError("universe TOML root must be a table.")

    symbols_raw = raw.get("symbols")
    if not isinstance(symbols_raw, list) or not symbols_raw:
        raise ValueError("symbols must contain at least one entry.")

    payload: dict[str, Any] = {
        "version": raw.get("version"),
        "name": raw.get("name"),
        "description": raw.get("description"),
        "base_market": raw.get("base_market"),
        "symbols": symbols_raw,
    }
    return UniverseDefinition.model_validate(payload)


__all__ = [
    "BaseMarket",
    "UniverseDefinition",
    "UniverseSymbol",
    "load_universe_toml",
]
