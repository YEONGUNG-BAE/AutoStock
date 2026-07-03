"""Phase 2a backtest data models.

Phase 2a is strategy-agnostic. It preserves original symbols, markets,
timestamps, source names, and value fields. It does not implement LLM input
masking, strategy execution, benchmark scoring, derived feature fitting, or
normalization. A later Phase 2c LLM input adapter may create anonymized or
feature-rich masked views from these preserved original fields.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from domain._datetime import parse_timezone_aware_datetime
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from paper_review.models import BenchmarkReturnPoint


class BacktestInstrumentBar(BaseModel):
    """Neutral offline instrument observation (adjusted close).

    Generic by design: a row may represent an asset-class proxy instrument
    or an individual security in later diagnostic stock-level tests. The
    original symbol, market, date, as_of, source_name, and value are
    preserved unmasked for a later Phase 2c masking adapter.

    ``date`` is the normalized alignment key; ``as_of`` is the source
    availability timestamp (look-ahead safety key). They are not assumed
    to fall on the same calendar day across time zones.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    as_of: datetime
    symbol: str
    market: str
    close_adjusted: Decimal
    source_name: str

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        return parse_timezone_aware_datetime(value, field_name="as_of")

    @field_validator("symbol", "market", "source_name", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("close_adjusted", mode="before")
    @classmethod
    def validate_close_adjusted(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="close_adjusted")

    @model_validator(mode="after")
    def validate_positive_close(self) -> Self:
        if self.close_adjusted <= Decimal("0"):
            raise ValueError("close_adjusted must be greater than 0.")
        return self


class BacktestBenchmarkLoadResult(BaseModel):
    """Benchmark load output: KRW-unhedged points plus deterministic warnings.

    ``benchmark_points`` are scoring-only; the bot never trades this series.
    Warnings record dropped non-common dates deterministically — no
    forward-fill, no interpolation, no silent drops.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_points: tuple[BenchmarkReturnPoint, ...]
    warnings: tuple[str, ...]
