from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import time as time_module
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import composition.attended_paper_day as apd
from composition.attended_paper_day import (
    AttendedPaperDayConfig,
    DiagnosticCounters,
    KST,
    PILOT_MARKET,
    PILOT_SYMBOL,
    build_diagnostic_stack,
    run_attended_paper_day,
    validate_attended_paper_day_inputs,
)
from composition.attended_paper_day import AttendedPaperDayInputError
from domain.enums import Currency
from market_data.kis_official_ws_parser import TR_QUOTE, TR_TRADE
from market_data.models import NormalizedBestBidAsk, NormalizedTradeTick, ProviderSequence
from market_data.replay_source import ReplayMarketEventSource

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_attended_paper_day.py"
_spec = importlib.util.spec_from_file_location("run_attended_paper_day", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def _config(tmp_path: Path, *, startup_only: bool = False) -> AttendedPaperDayConfig:
    return AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=5,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=tmp_path / "runtime" / "db",
        confirm_attended_paper=True,
        startup_only=startup_only,
    )


def _at() -> datetime:
    return datetime.combine(date(2026, 6, 17), time(9, 30), tzinfo=KST)


def _quote(sequence: int = 1) -> NormalizedBestBidAsk:
    at = _at()
    return NormalizedBestBidAsk(
        provider="kis",
        symbol=PILOT_SYMBOL,
        market=PILOT_MARKET,
        currency=Currency.KRW,
        bid_price=Decimal("70000"),
        ask_price=Decimal("70000"),
        bid_quantity=Decimal("10"),
        ask_quantity=Decimal("10"),
        quote_at=at,
        received_at=at,
        provider_sequence=ProviderSequence(
            provider="kis",
            channel=f"{TR_QUOTE}|{PILOT_SYMBOL}",
            sequence=sequence,
            received_at=at,
        ),
    )


def _trade(sequence: int = 1) -> NormalizedTradeTick:
    at = _at()
    return NormalizedTradeTick(
        provider="kis",
        symbol=PILOT_SYMBOL,
        market=PILOT_MARKET,
        currency=Currency.KRW,
        price=Decimal("70000"),
        quantity=Decimal("10"),
        trade_at=at,
        received_at=at,
        cumulative_volume=Decimal("1000"),
        provider_sequence=ProviderSequence(
            provider="kis",
            channel=f"{TR_TRADE}|{PILOT_SYMBOL}",
            sequence=sequence,
            received_at=at,
        ),
    )


def test_offline_e2e_commits_paper_order_and_writes_evidence(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="test-run",
        clock=_at,
    )

    counters = summary["counters"]["counters"]  # type: ignore[index]
    assert summary["paper_only"] is True
    assert summary["activation_authorized"] is False
    assert summary["real_order_adapter_constructed"] is False
    assert summary["nonterminal_journal"] == 0
    assert counters["normalized_trades"] == 1
    assert counters["normalized_quotes"] == 1
    assert counters["publication_slot_outcomes"] == 1
    assert counters["journal_committed"] == 1
    assert counters["orders"] == 1
    assert counters["fills"] == 1
    assert cfg.evidence_out.exists()
    assert "sensitive_data_present" in cfg.evidence_out.read_text(encoding="utf-8")
    assert cfg.summary_out.exists()


def test_no_quote_classifies_health_hold_without_execution(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_trade()]),
        run_id="no-quote",
        clock=_at,
    )

    counters = summary["counters"]["counters"]  # type: ignore[index]
    assert counters["normalized_trades"] == 1
    assert counters.get("normalized_quotes", 0) == 0
    assert counters["health_hold"] >= 1
    assert counters.get("orders", 0) == 0
    assert counters.get("fills", 0) == 0


def test_startup_only_opens_and_closes_without_execution(tmp_path: Path) -> None:
    cfg = _config(tmp_path, startup_only=True)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="startup-only",
        clock=_at,
    )

    assert summary["stop_reason"] == "startup_only"
    assert summary["outcome"] == "PASS"
    counters = summary["counters"]["counters"]  # type: ignore[index]
    assert counters.get("connected", 0) == 0
    assert counters.get("subscription_acks", 0) == 0
    assert counters.get("orders", 0) == 0


class _LiveStartupSource:
    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    async def events(self):
        at = _at()
        self._lifecycle.on_connected(at=at)
        self._lifecycle.on_subscription_requested(tr_id=TR_TRADE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(
            tr_id=TR_TRADE, symbol=PILOT_SYMBOL, accepted=True, at=at
        )
        self._lifecycle.on_subscription_requested(tr_id=TR_QUOTE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(
            tr_id=TR_QUOTE, symbol=PILOT_SYMBOL, accepted=True, at=at
        )
        self._lifecycle.on_all_subscribed(at=at)
        if False:
            yield _quote()


def test_live_startup_only_requires_actual_lifecycle_readiness(tmp_path: Path) -> None:
    cfg = AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=1,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=tmp_path / "runtime" / "db",
        confirm_attended_paper=True,
        startup_only=True,
        source_kind="kis_live",
    )

    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: _LiveStartupSource(lifecycle),
        run_id="live-startup",
        clock=_at,
    )

    counters = summary["counters"]["counters"]  # type: ignore[index]
    assert summary["outcome"] == "PASS"
    assert summary["stop_reason"] == "startup_only"
    assert counters["connect_attempts"] == 1
    assert counters["connected"] == 1
    assert counters["subscription_requests"] == 2
    assert counters["subscription_acks"] == 2
    assert counters.get("orders", 0) == 0


def test_rejects_existing_non_empty_db_dir_without_reuse(tmp_path: Path) -> None:
    db_dir = tmp_path / "runtime" / "db"
    db_dir.mkdir(parents=True)
    (db_dir / "active.sqlite3").write_text("", encoding="utf-8")
    cfg = _config(tmp_path)

    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="db-reuse",
        clock=_at,
    )

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "invalid_input"


def test_existing_runtime_lock_is_no_go_and_preserved(tmp_path: Path) -> None:
    db_dir = tmp_path / "runtime" / "db"
    db_dir.mkdir(parents=True)
    lock = db_dir / ".paper_day.lock"
    lock.write_text("owned\n", encoding="utf-8")
    cfg = _config(tmp_path)

    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="lock-conflict",
        clock=_at,
    )

    assert summary["outcome"] == "NO_GO"
    assert summary["stop_reason"] == "runtime_lock_exists"
    assert lock.read_text(encoding="utf-8") == "owned\n"


def test_cli_validate_only_no_writes_or_network(tmp_path: Path, capsys) -> None:
    rc = cli.main(
        [
            "--config",
            "config/config.toml.example",
            "--session-date",
            "2026-06-17",
            "--symbol",
            "005930",
            "--duration-seconds",
            "5",
            "--evidence-out",
            str(tmp_path / "runtime" / "evidence.jsonl"),
            "--summary-out",
            str(tmp_path / "runtime" / "summary.json"),
            "--db-dir",
            str(tmp_path / "runtime" / "db"),
            "--confirm-attended-paper",
            "--validate-only",
            "--json",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert '"mode": "validate-only"' in out
    assert not (tmp_path / "runtime").exists()


def test_cli_rejects_missing_confirmation_without_writes(tmp_path: Path, capsys) -> None:
    rc = cli.main(
        [
            "--config",
            "config/config.toml.example",
            "--session-date",
            "2026-06-17",
            "--symbol",
            "005930",
            "--duration-seconds",
            "5",
            "--evidence-out",
            str(tmp_path / "runtime" / "evidence.jsonl"),
            "--summary-out",
            str(tmp_path / "runtime" / "summary.json"),
            "--db-dir",
            str(tmp_path / "runtime" / "db"),
            "--validate-only",
            "--json",
        ]
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert '"network_called": false' in out
    assert '"filesystem_written": false' in out
    assert not (tmp_path / "runtime").exists()


# --- RTM-7c.5a/5b targeted closures ----------------------------------------


def _seed_pilot_db(tmp_path: Path) -> Path:
    """Build then close a real diagnostic stack so the pilot DB files exist."""
    db_dir = tmp_path / "runtime" / "db"
    stack = build_diagnostic_stack(
        config=_config(tmp_path),
        counters=DiagnosticCounters(),
        on_execution_evidence=lambda _ev: None,
    )
    stack.close()
    return db_dir


def test_lock_conflict_opens_no_db_and_never_calls_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_dir = _seed_pilot_db(tmp_path)
    (db_dir / ".paper_day.lock").write_text("owned\n", encoding="utf-8")
    journal_before = (db_dir / "trigger_journal.sqlite3").read_bytes()

    connects: list[Any] = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: connects.append(a) or real_connect(*a, **k)
    )
    factory_calls = {"n": 0}

    def factory(*, lifecycle: Any) -> ReplayMarketEventSource:
        factory_calls["n"] += 1
        return ReplayMarketEventSource([_quote(), _trade()])

    cfg = AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=5,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=db_dir,
        confirm_attended_paper=True,
        reuse_pilot_db=True,
    )
    summary = run_attended_paper_day(
        config=cfg, source_factory=factory, run_id="lock", clock=_at
    )

    assert summary["outcome"] == "NO_GO"
    assert summary["stop_reason"] == "runtime_lock_exists"
    assert summary["nonterminal_journal"] is None
    assert connects == []  # zero DB opens against the lock owner's database
    assert factory_calls["n"] == 0  # source factory never invoked on lock conflict
    assert (db_dir / ".paper_day.lock").read_text(encoding="utf-8") == "owned\n"
    assert (db_dir / "trigger_journal.sqlite3").read_bytes() == journal_before
    assert not cfg.summary_out.exists()  # non-owner writes no summary
    assert not cfg.evidence_out.exists()  # non-owner writes no evidence


def test_factory_internal_type_error_is_single_call_source_failed(tmp_path: Path) -> None:
    calls = {"n": 0, "side": 0}

    def factory(*, lifecycle: Any) -> ReplayMarketEventSource:
        calls["n"] += 1
        calls["side"] += 1  # stands in for an approval/auth side effect
        raise TypeError("internal boom after side effect")

    cfg = AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=1,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=tmp_path / "runtime" / "db",
        confirm_attended_paper=True,
        startup_only=True,
        source_kind="kis_live",
    )
    summary = run_attended_paper_day(
        config=cfg, source_factory=factory, run_id="factory", clock=_at
    )

    assert calls["n"] == 1  # no arity-probe retry
    assert calls["side"] == 1
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_failed"


def test_partial_stack_failure_closes_handles_and_preserves_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = {"n": 0}
    real_ads = apd.ActiveDecisionStore

    class TrackingActiveStore(real_ads):  # type: ignore[misc, valid-type]
        def close(self) -> None:
            closed["n"] += 1
            super().close()

    class BoomLedger:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("ledger constructor failed")

    monkeypatch.setattr(apd, "ActiveDecisionStore", TrackingActiveStore)
    monkeypatch.setattr(apd, "SQLiteLedger", BoomLedger)

    with pytest.raises(RuntimeError, match="ledger constructor failed"):
        build_diagnostic_stack(
            config=_config(tmp_path),
            counters=DiagnosticCounters(),
            on_execution_evidence=lambda _ev: None,
        )

    assert closed["n"] == 1  # earlier handle closed; original exception preserved


def test_db_constructor_failure_classified_db_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BoomLedger:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(apd, "SQLiteLedger", BoomLedger)
    summary = run_attended_paper_day(
        config=_config(tmp_path),
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="db",
        clock=_at,
    )
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "db_failed"


def test_summary_write_failure_classified_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link

    def boom_link(src: Any, dst: Any, *a: Any, **k: Any):
        if str(dst).endswith("summary.json"):
            raise OSError("summary publish failed")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", boom_link)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="summary",
        clock=_at,
    )
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "summary_failed"
    assert not (cfg.db_dir / ".paper_day.lock").exists()  # lock released
    assert not cfg.summary_out.exists()  # no partial/overwritten summary visible


@pytest.mark.parametrize(
    "events,reason",
    [
        ([], "trade_not_observed"),
        ([_quote()], "trade_not_observed"),
        ([_trade()], "quote_not_observed"),
    ],
)
def test_completion_verdict_no_go_reasons(
    tmp_path: Path, events: list[Any], reason: str
) -> None:
    summary = run_attended_paper_day(
        config=_config(tmp_path),
        source_factory=lambda *, lifecycle: ReplayMarketEventSource(list(events)),
        run_id="verdict",
        clock=_at,
    )
    assert summary["outcome"] == "NO_GO"
    assert summary["stop_reason"] == reason


def test_completion_verdict_pass_requires_quote_and_trade(tmp_path: Path) -> None:
    summary = run_attended_paper_day(
        config=_config(tmp_path),
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="pass",
        clock=_at,
    )
    assert summary["outcome"] == "PASS"
    assert summary["stop_reason"] == "completed"
    counters = summary["counters"]["counters"]  # type: ignore[index]
    assert counters["connect_attempts"] == 1  # single owner, not double-counted
    assert counters["all_subscribed"] == 1


def test_dangling_final_symlink_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    evidence = tmp_path / "runtime" / "evidence.jsonl"
    evidence.symlink_to(tmp_path / "runtime" / "missing_target")  # dangling
    with pytest.raises(AttendedPaperDayInputError, match="symlink"):
        validate_attended_paper_day_inputs(_config(tmp_path))


def test_rollback_journal_sidecar_is_rejected(tmp_path: Path) -> None:
    db_dir = tmp_path / "runtime" / "db"
    db_dir.mkdir(parents=True)
    (db_dir / "trigger_journal.sqlite3-journal").write_text("x", encoding="utf-8")
    cfg = AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=5,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=db_dir,
        confirm_attended_paper=True,
        reuse_pilot_db=True,
    )
    with pytest.raises(AttendedPaperDayInputError, match="sidecar"):
        validate_attended_paper_day_inputs(cfg)


class _AckThenBlockSource:
    """Sends ACK/all_subscribed, then awaits indefinitely without a market event."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    async def events(self):
        at = _at()
        self._lifecycle.on_connected(at=at)
        self._lifecycle.on_subscription_requested(tr_id=TR_TRADE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(tr_id=TR_TRADE, symbol=PILOT_SYMBOL, accepted=True, at=at)
        self._lifecycle.on_subscription_requested(tr_id=TR_QUOTE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(tr_id=TR_QUOTE, symbol=PILOT_SYMBOL, accepted=True, at=at)
        self._lifecycle.on_all_subscribed(at=at)
        await asyncio.sleep(3600)
        yield _quote()


def test_startup_probe_returns_on_ack_without_waiting_for_market_event(tmp_path: Path) -> None:
    cfg = AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=30,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=tmp_path / "runtime" / "db",
        confirm_attended_paper=True,
        startup_only=True,
        source_kind="kis_live",
    )
    started = time_module.monotonic()
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: _AckThenBlockSource(lifecycle),
        run_id="ack",
        clock=_at,
    )
    elapsed = time_module.monotonic() - started

    assert summary["outcome"] == "PASS"
    assert summary["stop_reason"] == "startup_only"
    assert elapsed < 5.0  # returned on ACK, did not wait out the 30s duration
    counters = summary["counters"]["counters"]  # type: ignore[index]
    assert counters["connect_attempts"] == 1
    assert counters["subscription_acks"] == 2


def test_credential_read_deferred_until_after_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_dir = _seed_pilot_db(tmp_path)
    (db_dir / ".paper_day.lock").write_text("owned\n", encoding="utf-8")

    import config.settings as settings_mod

    load_calls = {"n": 0}

    def spy_load(*_a: Any, **_k: Any) -> Any:
        load_calls["n"] += 1
        raise AssertionError("load_settings must not run before admission")

    monkeypatch.setattr(settings_mod, "load_settings", spy_load)

    env_reads: list[str] = []

    import os as os_mod

    _real_environ = os_mod.environ

    class _RecordingEnv:
        # Delegates to the real environ (so pytest's own reads keep working) but
        # records every key fetched via .get/[]; the test asserts that the runtime
        # performed zero credential reads before admission succeeded.
        def __getitem__(self, key: str) -> Any:
            env_reads.append(key)
            return _real_environ[key]

        def get(self, key: str, default: Any = None) -> Any:
            env_reads.append(key)
            return _real_environ.get(key, default)

        def __setitem__(self, key: str, value: str) -> None:
            _real_environ[key] = value

        def __delitem__(self, key: str) -> None:
            del _real_environ[key]

        def __getattr__(self, name: str) -> Any:
            return getattr(_real_environ, name)

    monkeypatch.setattr(os_mod, "environ", _RecordingEnv())

    factory = cli._live_source_factory(
        Path("config/config.toml.example"),
        AttendedPaperDayConfig(
            session_date=date(2026, 6, 17),
            symbol=PILOT_SYMBOL,
            duration_seconds=1,
            evidence_out=tmp_path / "runtime" / "evidence.jsonl",
            summary_out=tmp_path / "runtime" / "summary.json",
            db_dir=db_dir,
            confirm_attended_paper=True,
            startup_only=True,
            source_kind="kis_live",
        ),
    )
    cfg = AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=1,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=db_dir,
        confirm_attended_paper=True,
        startup_only=True,
        source_kind="kis_live",
        reuse_pilot_db=True,
    )
    summary = run_attended_paper_day(config=cfg, source_factory=factory, run_id="cred", clock=_at)

    assert summary["stop_reason"] == "runtime_lock_exists"
    assert load_calls["n"] == 0  # admission failed first; no settings load
    assert env_reads == []  # and no credential env read


# --- RTM-7c.5a/5b: output ownership, summary consistency, fatal lifecycle, probe ---


def test_invalid_input_returns_memory_result_without_writes(tmp_path: Path) -> None:
    # summary_out == evidence_out is an admission (path-ownership) failure: the run
    # must return an in-memory FAIL/invalid_input result and write zero files.
    shared = tmp_path / "runtime" / "out.jsonl"
    cfg = AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=5,
        evidence_out=shared,
        summary_out=shared,
        db_dir=tmp_path / "runtime" / "db",
        confirm_attended_paper=True,
    )
    factory_calls = {"n": 0}

    def factory(*, lifecycle: Any) -> ReplayMarketEventSource:
        factory_calls["n"] += 1
        return ReplayMarketEventSource([_quote(), _trade()])

    summary = run_attended_paper_day(config=cfg, source_factory=factory, run_id="inv", clock=_at)

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "invalid_input"
    assert factory_calls["n"] == 0  # never reached the source factory
    assert not shared.exists()  # validation failure writes nothing
    assert not cfg.db_dir.exists()  # and opens no pilot DB


def test_returned_summary_matches_persisted_file(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="match",
        clock=_at,
    )
    assert summary["outcome"] == "PASS"
    persisted = json.loads(cfg.summary_out.read_text(encoding="utf-8"))
    assert persisted == summary  # returned dict is exactly what was published


@pytest.mark.parametrize("fatal_type", [MemoryError, SystemExit])
def test_body_fatal_preserved_and_lock_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fatal_type: type[BaseException]
) -> None:
    class _FatalMonitor:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def run(self) -> None:
            raise fatal_type("fatal in market loop")

    monkeypatch.setattr(apd, "MarketMonitor", _FatalMonitor)
    cfg = _config(tmp_path)

    with pytest.raises(fatal_type):
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="fatal",
            clock=_at,
        )

    # Fatal identity is preserved (re-raised) yet the lock is still released as the
    # last bounded cleanup step.
    assert not (cfg.db_dir / ".paper_day.lock").exists()


class _ProbeBoomSource:
    """Connects, then the consumer raises before any subscription ACK."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    async def events(self):
        self._lifecycle.on_connected(at=_at())
        raise RuntimeError("recv failed mid-stream")
        yield


class _RejectSource:
    """Sends a rejected subscription ACK and then exhausts."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    async def events(self):
        at = _at()
        self._lifecycle.on_connected(at=at)
        self._lifecycle.on_subscription_requested(tr_id=TR_TRADE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(
            tr_id=TR_TRADE, symbol=PILOT_SYMBOL, accepted=False, at=at
        )
        if False:
            yield _quote()


class _ExhaustNoAckSource:
    """Connects but never reaches subscription readiness before exhausting."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    async def events(self):
        self._lifecycle.on_connected(at=_at())
        if False:
            yield _quote()


def _live_startup_cfg(tmp_path: Path) -> AttendedPaperDayConfig:
    return AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=2,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=tmp_path / "runtime" / "db",
        confirm_attended_paper=True,
        startup_only=True,
        source_kind="kis_live",
    )


def test_startup_probe_consumer_exception_is_source_failed(tmp_path: Path) -> None:
    summary = run_attended_paper_day(
        config=_live_startup_cfg(tmp_path),
        source_factory=lambda *, lifecycle: _ProbeBoomSource(lifecycle),
        run_id="probe-boom",
        clock=_at,
    )
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_failed"


def test_startup_probe_rejected_ack_is_subscription_rejected(tmp_path: Path) -> None:
    summary = run_attended_paper_day(
        config=_live_startup_cfg(tmp_path),
        source_factory=lambda *, lifecycle: _RejectSource(lifecycle),
        run_id="probe-reject",
        clock=_at,
    )
    assert summary["outcome"] == "NO_GO"
    assert summary["stop_reason"] == "subscription_rejected"


def test_startup_probe_exhaustion_without_ack_is_transport_not_ready(tmp_path: Path) -> None:
    summary = run_attended_paper_day(
        config=_live_startup_cfg(tmp_path),
        source_factory=lambda *, lifecycle: _ExhaustNoAckSource(lifecycle),
        run_id="probe-exhaust",
        clock=_at,
    )
    assert summary["outcome"] == "NO_GO"
    assert summary["stop_reason"] == "transport_not_ready"
