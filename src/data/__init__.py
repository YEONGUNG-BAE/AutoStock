from __future__ import annotations

from typing import Any

__all__ = [
    "DateIdGenerator",
    "DateIdValidator",
    "DartDisclosureAdapter",
    "DisclosureDataAdapter",
    "DisclosureRecord",
    "DuplicateDateIdError",
    "FredMacroAdapter",
    "MacroDataAdapter",
    "MacroDataPoint",
    "MarketDataAdapter",
    "MarketDataPoint",
    "SQLiteDateIdSourceStore",
    "YFinancePriceAdapter",
    "disclosure_record_to_source_record",
    "extract_date_ids_from_reasons",
    "macro_data_point_to_source_record",
    "market_data_point_to_source_record",
]


def __getattr__(name: str) -> Any:
    if name == "DuplicateDateIdError":
        from data.date_id_store import DuplicateDateIdError

        return DuplicateDateIdError
    if name == "SQLiteDateIdSourceStore":
        from data.date_id_store import SQLiteDateIdSourceStore

        return SQLiteDateIdSourceStore
    if name == "DateIdValidator":
        from data.date_id_validator import DateIdValidator

        return DateIdValidator
    if name == "extract_date_ids_from_reasons":
        from data.date_id_validator import extract_date_ids_from_reasons

        return extract_date_ids_from_reasons
    if name == "DateIdGenerator":
        from data.date_id_generator import DateIdGenerator

        return DateIdGenerator
    if name == "MarketDataPoint":
        from data.market_data import MarketDataPoint

        return MarketDataPoint
    if name == "MacroDataPoint":
        from data.market_data import MacroDataPoint

        return MacroDataPoint
    if name == "DisclosureRecord":
        from data.market_data import DisclosureRecord

        return DisclosureRecord
    if name == "market_data_point_to_source_record":
        from data.market_data import market_data_point_to_source_record

        return market_data_point_to_source_record
    if name == "macro_data_point_to_source_record":
        from data.market_data import macro_data_point_to_source_record

        return macro_data_point_to_source_record
    if name == "disclosure_record_to_source_record":
        from data.market_data import disclosure_record_to_source_record

        return disclosure_record_to_source_record
    if name == "MarketDataAdapter":
        from data.adapters import MarketDataAdapter

        return MarketDataAdapter
    if name == "MacroDataAdapter":
        from data.adapters import MacroDataAdapter

        return MacroDataAdapter
    if name == "DisclosureDataAdapter":
        from data.adapters import DisclosureDataAdapter

        return DisclosureDataAdapter
    if name == "YFinancePriceAdapter":
        from data.yfinance_adapter import YFinancePriceAdapter

        return YFinancePriceAdapter
    if name == "FredMacroAdapter":
        from data.fred_adapter import FredMacroAdapter

        return FredMacroAdapter
    if name == "DartDisclosureAdapter":
        from data.dart_adapter import DartDisclosureAdapter

        return DartDisclosureAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
