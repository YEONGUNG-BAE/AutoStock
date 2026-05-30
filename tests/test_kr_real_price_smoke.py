"""Real Intake 3E2 — KR real sample live PRICE smoke tests (injected ticker only)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
INTAKE_SCRIPT = REPO_ROOT / "ops" / "research_source_intake.py"
SMOKE_SCRIPT = REPO_ROOT / "ops" / "run_date_md_smoke.py"
OPS_SCRIPT = REPO_ROOT / "ops" / "run_kr_real_price_smoke.py"

KST = timezone(timedelta(hours=9))
AS_OF = "2026-05-30T13:00:00+09:00"
FETCHED_AT = datetime(2026, 5, 30, 4, 0, 0, tzinfo=UTC)

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType


class _FakeILoc:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def __getitem__(self, idx: int) -> float:
        return self._values[idx]


class _FakeCloseSeries:
    def __init__(self, values: list[float], indices: list[object]) -> None:
        self._values = values
        self.index = indices
        self.iloc = _FakeILoc(values)

    def dropna(self) -> _FakeCloseSeries:
        return self

    def __len__(self) -> int:
        return len(self._values)


class _FakeHistory:
    def __init__(self, closes: list[float], indices: list[object]) -> None:
        self._closes = closes
        self._indices = indices
        self.columns = ["Close"]

    def __len__(self) -> int:
        return len(self._closes)

    @property
    def empty(self) -> bool:
        return len(self._closes) == 0

    def __getitem__(self, key: str) -> _FakeCloseSeries:
        if key == "Close":
            return _FakeCloseSeries(self._closes, self._indices)
        raise KeyError(key)


class _FakeTicker:
    def __init__(self, *, close: float) -> None:
        self._close = close
        self.info = {"currency": "KRW"}

    def history(self, period: str, interval: str) -> _FakeHistory:
        return _FakeHistory(
            closes=[self._close],
            indices=[datetime(2026, 5, 30, tzinfo=UTC)],
        )


def _kr_real_fake_ticker_factory(provider_symbol: str) -> _FakeTicker:
    if provider_symbol == "005930.KS":
        return _FakeTicker(close=70000.0)
    if provider_symbol == "000660.KS":
        return _FakeTicker(close=130000.0)
    raise AssertionError(f"unexpected provider_symbol in test: {provider_symbol!r}")


def _patch_injected_smoke_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() 경로에 fake ticker + 고정 fetched_at을 주입한다."""
    import data.price_live_client as price_live_client
    import run_kr_real_price_smoke as smoke_module

    real_fetch = price_live_client.fetch_live_price_snapshot

    def bound_fetch(**kwargs: object) -> Path:
        if kwargs.get("ticker_factory") is None:
            kwargs["ticker_factory"] = _kr_real_fake_ticker_factory
        return real_fetch(**kwargs)

    monkeypatch.setattr(price_live_client, "fetch_live_price_snapshot", bound_fetch)

    original_run = smoke_module.run_kr_real_price_smoke

    def inject(**kwargs: object) -> dict[str, object]:
        kwargs = dict(kwargs)
        kwargs["fetched_at"] = FETCHED_AT
        kwargs["ticker_factory"] = _kr_real_fake_ticker_factory
        return original_run(**kwargs)

    monkeypatch.setattr(smoke_module, "run_kr_real_price_smoke", inject)


def _smoke_argv(
    *,
    snapshot_dir: Path,
    out_jsonl: Path,
    store_path: Path,
    universe_path: Path | None = None,
    mapping_path: Path | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    argv = [
        "--universe",
        str(universe_path or KR_REAL_UNIVERSE),
        "--provider-mapping",
        str(mapping_path or KR_REAL_MAPPING),
        "--store",
        str(store_path),
        "--snapshot-dir",
        str(snapshot_dir),
        "--out-jsonl",
        str(out_jsonl),
        "--as-of",
        AS_OF,
        "--json",
    ]
    if extra:
        argv.extend(extra)
    return argv


def _run_smoke_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_intake_validate(jsonl_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(INTAKE_SCRIPT),
            "--source-jsonl",
            str(jsonl_path),
            "--validate-only",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_intake_normal(
    *,
    jsonl_path: Path,
    store_path: Path,
    date_md_out: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(INTAKE_SCRIPT),
            "--source-jsonl",
            str(jsonl_path),
            "--store",
            str(store_path),
            "--date-md-out",
            str(date_md_out),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_date_md_smoke_cli(
    *,
    universe_path: Path,
    date_md_path: Path,
    store_path: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(SMOKE_SCRIPT),
            "--universe",
            str(universe_path),
            "--date-md",
            str(date_md_path),
            "--store",
            str(store_path),
            "--require-symbol-coverage",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _empty_store(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    store.close()
    return store_path


def _seed_store(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()
    return store_path


def _sample_seed_record(*, date_id: str = "260530-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-test",
        source_timestamp=datetime(2026, 5, 30, 9, 0, 0, tzinfo=KST),
        created_at=datetime(2026, 5, 30, 9, 5, 0, tzinfo=KST),
        summary="seed record",
        payload={"note": "seed"},
        symbol="005930",
        market="KR",
    )


def _store_record_count(store_path: Path) -> int:
    conn = sqlite3.connect(store_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM date_id_sources").fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_smoke_loads_kr_real_universe_and_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_price_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["universe"] == "kr-real-sample-v0"
    assert payload["provider_mapping"] == "kr-real-provider-mappings-v1"


def test_smoke_fake_yfinance_writes_two_snapshots_and_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_price_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "ok"
    assert payload["mode"] == "live-price-smoke"
    assert payload["records_count"] == 2

    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    assert len(snapshot_files) == 2
    assert out_jsonl.is_file()

    records = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
    assert {record["symbol"] for record in records} == {"005930", "000660"}
    date_ids = [record["date_id"] for record in records]
    assert date_ids == ["260530-1", "260530-2"]
    for record in records:
        assert record["fact_type"] == "price"
        assert record["market"] == "KR"
        assert record["source_name"] == "yfinance"


def test_smoke_date_ids_continue_after_store_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_price_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    store_path = _seed_store(tmp_path, _sample_seed_record(date_id="260530-1"))
    count_before = _store_record_count(store_path)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert [entry["date_id"] for entry in payload["symbols"]] == ["260530-2", "260530-3"]
    assert _store_record_count(store_path) == count_before


def test_smoke_jsonl_validates_through_8b_validate_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_kr_real_price_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    intake = _run_intake_validate(out_jsonl)
    assert intake.returncode == 0, intake.stderr
    payload = json.loads(intake.stdout)
    assert payload["records_valid"] == 2


def test_smoke_8b_normal_and_8c_symbol_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_kr_real_price_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"
    fetch_store_path = _empty_store(tmp_path / "fetch")
    intake_store_path = tmp_path / "intake" / "date_id_sources.sqlite3"
    intake_store_path.parent.mkdir(parents=True, exist_ok=True)
    date_md_path = tmp_path / "Date.md"

    assert (
        main(
            _smoke_argv(
                snapshot_dir=snapshot_dir,
                out_jsonl=out_jsonl,
                store_path=fetch_store_path,
            )
        )
        == 0
    )

    intake = _run_intake_normal(
        jsonl_path=out_jsonl,
        store_path=intake_store_path,
        date_md_out=date_md_path,
    )
    assert intake.returncode == 0, intake.stderr

    smoke = _run_date_md_smoke_cli(
        universe_path=KR_REAL_UNIVERSE,
        date_md_path=date_md_path,
        store_path=intake_store_path,
    )
    assert smoke.returncode == 0, smoke.stderr
    payload = json.loads(smoke.stdout)
    assert payload["missing_symbols"] == []


def test_smoke_snapshot_collision_fails_before_jsonl_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_price_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    capsys.readouterr()
    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    original_bytes = {path: path.read_bytes() for path in snapshot_files}
    out_jsonl.unlink()

    exit_code = main(
        _smoke_argv(
            snapshot_dir=snapshot_dir,
            out_jsonl=out_jsonl,
            store_path=store_path,
            extra=["--force"],
        )
    )
    payload = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 1
    assert payload["stage"] == "snapshot"
    for path, content in original_bytes.items():
        assert path.read_bytes() == content
    assert not out_jsonl.exists()


def test_smoke_force_overwrites_out_jsonl_not_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_kr_real_price_smoke as smoke_module
    from run_kr_real_price_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    first_snapshots = {path: path.read_bytes() for path in snapshot_dir.glob("raw_*.json")}
    fetched = []
    for path in sorted(snapshot_dir.glob("raw_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched.append(
            smoke_module._FetchedPriceSnapshot(
                symbol=payload["symbol"],
                market=payload["market"],
                provider_symbol=payload["provider_symbol"],
                snapshot_path=path,
            )
        )
    out_jsonl.write_text("stale-jsonl\n", encoding="utf-8")

    monkeypatch.setattr(
        smoke_module,
        "_fetch_price_snapshots",
        lambda **kwargs: fetched,
    )
    _patch_injected_smoke_run(monkeypatch)

    assert (
        main(
            _smoke_argv(
                snapshot_dir=snapshot_dir,
                out_jsonl=out_jsonl,
                store_path=store_path,
                extra=["--force"],
            )
        )
        == 0
    )
    records = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    for path, content in first_snapshots.items():
        assert path.read_bytes() == content


def test_smoke_missing_yfinance_mapping_fails_at_mapping(tmp_path: Path) -> None:
    mapping_path = tmp_path / "mapping.toml"
    mapping_path.write_text(
        """
version = 1
name = "missing-yfinance"
description = "missing yfinance"

[[mappings]]
symbol = "005930"
market = "KR"
enabled = true
stock_code = "005930"

[[mappings]]
symbol = "000660"
market = "KR"
enabled = true
stock_code = "000660"

[mappings.yfinance]
provider_symbol = "000660.KS"
currency = "KRW"

[mappings.dart]
corp_code = "00164779"
stock_code = "000660"
corp_name = "SK하이닉스"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    result = _run_smoke_cli(
        *_smoke_argv(
            snapshot_dir=tmp_path / "sources" / "price",
            out_jsonl=tmp_path / "out.jsonl",
            store_path=_empty_store(tmp_path),
            mapping_path=mapping_path,
        )
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "mapping"
    assert "yfinance" in payload["error"]


def test_smoke_blank_provider_symbol_fails_at_mapping(tmp_path: Path) -> None:
    mapping_path = tmp_path / "mapping.toml"
    mapping_path.write_text(
        """
version = 1
name = "blank-provider-symbol"
description = "blank provider symbol"

[[mappings]]
symbol = "005930"
market = "KR"
enabled = true

[mappings.yfinance]
provider_symbol = "   "
currency = "KRW"

[[mappings]]
symbol = "000660"
market = "KR"
enabled = true

[mappings.yfinance]
provider_symbol = "000660.KS"
currency = "KRW"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    result = _run_smoke_cli(
        *_smoke_argv(
            snapshot_dir=tmp_path / "sources" / "price",
            out_jsonl=tmp_path / "out.jsonl",
            store_path=_empty_store(tmp_path),
            mapping_path=mapping_path,
        )
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] in {"args", "mapping"}


def test_ops_script_has_no_forbidden_tokens() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "dart_api_key",
        "fred_api_key",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
        "import yfinance",
        "from yfinance",
        "os.environ",
        "getenv",
    )
    for token in forbidden:
        assert token not in source, f"run_kr_real_price_smoke.py must not reference {token!r}"
