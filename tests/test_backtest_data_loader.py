from __future__ import annotations

import ast
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_data import (
    BacktestBenchmarkLoadResult,
    BacktestInstrumentBar,
    load_benchmark_krw_unhedged,
    load_instrument_bars,
)
from paper_review.models import BenchmarkReturnPoint

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backtest_data"
SP500_CSV = FIXTURES / "sp500_tr_usd_synthetic.csv"
USDKRW_CSV = FIXTURES / "usdkrw_synthetic.csv"
INSTRUMENT_CSV = FIXTURES / "instrument_prices_synthetic.csv"

KST = timezone(timedelta(hours=9))


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _sp_csv(tmp_path: Path, rows: str) -> Path:
    return _write(tmp_path / "sp.csv", "date,as_of,sp500_tr_usd,source_name\n" + rows)


def _fx_csv(tmp_path: Path, rows: str) -> Path:
    return _write(tmp_path / "fx.csv", "date,as_of,usdkrw,source_name\n" + rows)


def _instrument_csv(tmp_path: Path, rows: str) -> Path:
    return _write(
        tmp_path / "inst.csv",
        "date,as_of,symbol,market,close_adjusted,source_name\n" + rows,
    )


# 1. Parse well-formed synthetic benchmark and FX fixtures.
def test_parses_committed_benchmark_and_fx_fixtures() -> None:
    result = load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)

    assert isinstance(result, BacktestBenchmarkLoadResult)
    # common dates: 2020-01-02, 2020-01-06, 2020-01-07
    assert len(result.benchmark_points) == 3
    assert all(isinstance(point, BenchmarkReturnPoint) for point in result.benchmark_points)
    assert all(point.total_return_index_value > 0 for point in result.benchmark_points)


# 2. Parse well-formed synthetic instrument fixture.
def test_parses_committed_instrument_fixture() -> None:
    bars = load_instrument_bars(INSTRUMENT_CSV)

    assert len(bars) == 7
    markets = {bar.market for bar in bars}
    assert {"KR", "US", "GOLD"} <= markets
    # fixture contains an individual-security-style row too
    assert any(bar.symbol == "SYN_STOCK_001" for bar in bars)


# 3. Reject non-positive S&P TR level.
@pytest.mark.parametrize("bad_level", ["0", "-100"])
def test_rejects_non_positive_sp500_level(tmp_path: Path, bad_level: str) -> None:
    sp = _sp_csv(tmp_path, f"2020-01-02,2020-01-02T16:00:00+00:00,{bad_level},syn\n")
    fx = _fx_csv(tmp_path, "2020-01-02,2020-01-02T15:30:00+09:00,1300,syn\n")

    with pytest.raises(ValueError, match="sp500_tr_usd"):
        load_benchmark_krw_unhedged(sp, fx)


# 4. Reject non-positive USDKRW.
@pytest.mark.parametrize("bad_rate", ["0", "-1300"])
def test_rejects_non_positive_usdkrw(tmp_path: Path, bad_rate: str) -> None:
    sp = _sp_csv(tmp_path, "2020-01-02,2020-01-02T16:00:00+00:00,100,syn\n")
    fx = _fx_csv(tmp_path, f"2020-01-02,2020-01-02T15:30:00+09:00,{bad_rate},syn\n")

    with pytest.raises(ValueError, match="usdkrw"):
        load_benchmark_krw_unhedged(sp, fx)


# 5. Reject non-positive instrument adjusted close.
@pytest.mark.parametrize("bad_close", ["0", "-10"])
def test_rejects_non_positive_close_adjusted(tmp_path: Path, bad_close: str) -> None:
    inst = _instrument_csv(
        tmp_path, f"2020-01-02,2020-01-02T16:00:00+09:00,SYN_KR,KR,{bad_close},syn\n"
    )

    with pytest.raises(ValueError, match="close_adjusted"):
        load_instrument_bars(inst)


# 6. Reject malformed rows.
def test_rejects_malformed_rows(tmp_path: Path) -> None:
    sp_missing_column = _sp_csv(tmp_path, "2020-01-02,2020-01-02T16:00:00+00:00,100\n")
    fx = _fx_csv(tmp_path, "2020-01-02,2020-01-02T15:30:00+09:00,1300,syn\n")
    with pytest.raises(ValueError, match="malformed row"):
        load_benchmark_krw_unhedged(sp_missing_column, fx)

    sp_bad_date = _sp_csv(tmp_path, "not-a-date,2020-01-02T16:00:00+00:00,100,syn\n")
    with pytest.raises(ValueError, match="malformed date"):
        load_benchmark_krw_unhedged(sp_bad_date, fx)

    sp_bad_value = _sp_csv(tmp_path, "2020-01-02,2020-01-02T16:00:00+00:00,abc,syn\n")
    with pytest.raises(ValueError, match="malformed sp500_tr_usd"):
        load_benchmark_krw_unhedged(sp_bad_value, fx)

    sp_ok = _sp_csv(tmp_path, "2020-01-02,2020-01-02T16:00:00+00:00,100,syn\n")
    fx_blank_source = _fx_csv(tmp_path, "2020-01-02,2020-01-02T15:30:00+09:00,1300, \n")
    with pytest.raises(ValueError, match="source_name"):
        load_benchmark_krw_unhedged(sp_ok, fx_blank_source)

    inst_blank_symbol = _instrument_csv(
        tmp_path, "2020-01-02,2020-01-02T16:00:00+09:00, ,KR,100,syn\n"
    )
    with pytest.raises(ValueError, match="symbol"):
        load_instrument_bars(inst_blank_symbol)

    inst_wrong_header = _write(
        tmp_path / "bad_header.csv",
        "date,symbol,close\n2020-01-02,SYN,100\n",
    )
    with pytest.raises(ValueError, match="unexpected CSV header"):
        load_instrument_bars(inst_wrong_header)


# 7. Reject naive as_of timestamp.
def test_rejects_naive_as_of(tmp_path: Path) -> None:
    sp_naive = _sp_csv(tmp_path, "2020-01-02,2020-01-02T16:00:00,100,syn\n")
    fx = _fx_csv(tmp_path, "2020-01-02,2020-01-02T15:30:00+09:00,1300,syn\n")
    with pytest.raises(ValueError, match="as_of"):
        load_benchmark_krw_unhedged(sp_naive, fx)

    inst_naive = _instrument_csv(tmp_path, "2020-01-02,2020-01-02T16:00:00,SYN_KR,KR,100,syn\n")
    with pytest.raises(ValueError, match="as_of"):
        load_instrument_bars(inst_naive)


# 8. Reject duplicate benchmark date.
def test_rejects_duplicate_benchmark_date(tmp_path: Path) -> None:
    sp = _sp_csv(
        tmp_path,
        "2020-01-02,2020-01-02T16:00:00+00:00,100,syn\n"
        "2020-01-02,2020-01-02T17:00:00+00:00,101,syn\n",
    )
    fx = _fx_csv(tmp_path, "2020-01-02,2020-01-02T15:30:00+09:00,1300,syn\n")

    with pytest.raises(ValueError, match="duplicate date"):
        load_benchmark_krw_unhedged(sp, fx)


# 9. Reject duplicate FX date.
def test_rejects_duplicate_fx_date(tmp_path: Path) -> None:
    sp = _sp_csv(tmp_path, "2020-01-02,2020-01-02T16:00:00+00:00,100,syn\n")
    fx = _fx_csv(
        tmp_path,
        "2020-01-02,2020-01-02T15:30:00+09:00,1300,syn\n"
        "2020-01-02,2020-01-02T16:30:00+09:00,1301,syn\n",
    )

    with pytest.raises(ValueError, match="duplicate date"):
        load_benchmark_krw_unhedged(sp, fx)


# 10. Reject duplicate instrument (date, symbol, market).
def test_rejects_duplicate_instrument_key(tmp_path: Path) -> None:
    inst = _instrument_csv(
        tmp_path,
        "2020-01-02,2020-01-02T16:00:00+09:00,SYN_KR,KR,100,syn\n"
        "2020-01-02,2020-01-02T17:00:00+09:00,SYN_KR,KR,101,syn\n",
    )

    with pytest.raises(ValueError, match="duplicate instrument"):
        load_instrument_bars(inst)


def test_same_symbol_different_market_is_not_duplicate(tmp_path: Path) -> None:
    inst = _instrument_csv(
        tmp_path,
        "2020-01-02,2020-01-02T16:00:00+09:00,SYN,KR,100,syn\n"
        "2020-01-02,2020-01-02T17:00:00+09:00,SYN,US,101,syn\n",
    )

    assert len(load_instrument_bars(inst)) == 2


# 11. KRW conversion exactness: 100 * 1300 = 130000.
def test_krw_conversion_exactness(tmp_path: Path) -> None:
    sp = _sp_csv(tmp_path, "2020-01-02,2020-01-03T09:00:00+09:00,100,syn\n")
    fx = _fx_csv(tmp_path, "2020-01-02,2020-01-02T15:30:00+09:00,1300,syn\n")

    result = load_benchmark_krw_unhedged(sp, fx)

    assert len(result.benchmark_points) == 1
    assert result.benchmark_points[0].total_return_index_value == Decimal("130000")
    assert result.warnings == ()


def test_krw_conversion_is_exact_decimal_not_float(tmp_path: Path) -> None:
    sp = _sp_csv(tmp_path, "2020-01-02,2020-01-03T09:00:00+09:00,100.10,syn\n")
    fx = _fx_csv(tmp_path, "2020-01-02,2020-01-02T15:30:00+09:00,1300.30,syn\n")

    result = load_benchmark_krw_unhedged(sp, fx)

    assert result.benchmark_points[0].total_return_index_value == Decimal("130160.0300")


# 12. Common-date alignment: non-common dates dropped with deterministic
#     warnings, no forward-fill, no interpolation.
def test_common_date_alignment_drops_with_deterministic_warnings() -> None:
    result = load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)

    # 2020-01-03 exists only in S&P; 2020-01-08 only in FX.
    assert result.warnings == (
        "missing_fx_for_benchmark_date:2020-01-03",
        "missing_benchmark_for_fx_date:2020-01-08",
        "dropped_non_common_dates:2",
    )
    # 3 common dates only — dropped dates are NOT forward-filled or interpolated.
    assert len(result.benchmark_points) == 3
    expected_values = {
        Decimal("100") * Decimal("1300"),
        Decimal("102.25") * Decimal("1298.75"),
        Decimal("103.00") * Decimal("1302.10"),
    }
    assert {point.total_return_index_value for point in result.benchmark_points} == expected_values


def test_alignment_is_deterministic() -> None:
    first = load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)
    second = load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)

    assert first == second


# 13. Output benchmark type is exactly BenchmarkReturnPoint.
def test_benchmark_output_type_is_benchmark_return_point() -> None:
    result = load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)

    assert all(type(point) is BenchmarkReturnPoint for point in result.benchmark_points)
    assert type(result.benchmark_points) is tuple


def test_benchmark_as_of_is_conservative_max_of_sources(tmp_path: Path) -> None:
    # S&P known later (next morning KST) than FX: point as_of must be the later one.
    sp = _sp_csv(tmp_path, "2020-01-02,2020-01-03T09:00:00+09:00,100,syn\n")
    fx = _fx_csv(tmp_path, "2020-01-02,2020-01-02T15:30:00+09:00,1300,syn\n")

    result = load_benchmark_krw_unhedged(sp, fx)

    assert result.benchmark_points[0].as_of == datetime(2020, 1, 3, 9, 0, tzinfo=KST)


# 14. Instrument model preserves symbol, market, date, as_of, source_name.
def test_instrument_model_preserves_original_fields() -> None:
    bars = load_instrument_bars(INSTRUMENT_CSV)

    kr_bar = next(bar for bar in bars if bar.symbol == "SYN_KR_PROXY" and bar.date == date(2020, 1, 2))
    assert type(kr_bar) is BacktestInstrumentBar
    assert kr_bar.symbol == "SYN_KR_PROXY"
    assert kr_bar.market == "KR"
    assert kr_bar.date == date(2020, 1, 2)
    assert kr_bar.as_of == datetime(2020, 1, 2, 16, 0, tzinfo=KST)
    assert kr_bar.close_adjusted == Decimal("10000")
    assert kr_bar.source_name == "synthetic_fixture_v1"

    # date and as_of are distinct: US rows are stamped available the NEXT
    # calendar day in KST (NYSE/KRX close mismatch preserved, not normalized).
    us_bar = next(bar for bar in bars if bar.symbol == "SYN_US_PROXY" and bar.date == date(2020, 1, 2))
    assert us_bar.as_of.date() != us_bar.date


# 15. Loader does not import network/fetch libraries.
def test_backtest_data_package_has_no_network_imports() -> None:
    forbidden = {
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "websocket",
        "websockets",
        "aiohttp",
    }
    package_dir = Path(__file__).resolve().parents[1] / "src" / "backtest_data"
    imported: set[str] = set()
    for module_path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    assert not (imported & forbidden), f"forbidden imports found: {imported & forbidden}"


# 16. Loader does not fit normalization, clustering, factor models, or
#     derived feature transforms.
def test_backtest_data_package_has_no_feature_fitting() -> None:
    forbidden_tokens = ("sklearn", "normalize(", "zscore", "z_score", "kmeans", "factor_model", "pca")
    package_dir = Path(__file__).resolve().parents[1] / "src" / "backtest_data"
    for module_path in sorted(package_dir.glob("*.py")):
        source = module_path.read_text(encoding="utf-8").lower()
        for token in forbidden_tokens:
            assert token not in source, f"{module_path.name} contains forbidden token {token!r}"


def test_loader_does_not_mutate_input_files() -> None:
    before = (SP500_CSV.read_bytes(), USDKRW_CSV.read_bytes(), INSTRUMENT_CSV.read_bytes())

    load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)
    load_instrument_bars(INSTRUMENT_CSV)

    after = (SP500_CSV.read_bytes(), USDKRW_CSV.read_bytes(), INSTRUMENT_CSV.read_bytes())
    assert before == after
