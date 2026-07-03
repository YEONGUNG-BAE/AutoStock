"""Phase 2a — offline backtest data loaders and as-of look-ahead guard.

Phase 2a is strategy-agnostic. It preserves original symbols, markets,
timestamps, source names, and value fields. It does not implement LLM input
masking, strategy execution, benchmark scoring, derived feature fitting, or
normalization. A later Phase 2c LLM input adapter may create anonymized or
feature-rich masked views from these preserved original fields.

The as-of guard is a backtest-only read-only view. It does not modify
ScoutInputBuilder, SQLiteDateIdSourceStore, or runtime scout behavior.

This package is isolated from allocator/risk/broker/emergency/scout/
composition/ops/config runtime paths. It reads local CSV fixtures only:
no network, no current-time dependence, no config access.
"""

from backtest_data.asof_guard import AsOfFilteredSourceView
from backtest_data.csv_loader import (
    load_benchmark_krw_unhedged,
    load_instrument_bars,
)
from backtest_data.models import (
    BacktestBenchmarkLoadResult,
    BacktestInstrumentBar,
)

__all__ = [
    "AsOfFilteredSourceView",
    "BacktestBenchmarkLoadResult",
    "BacktestInstrumentBar",
    "load_benchmark_krw_unhedged",
    "load_instrument_bars",
]
