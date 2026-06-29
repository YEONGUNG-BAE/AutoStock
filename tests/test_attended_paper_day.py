from __future__ import annotations

import asyncio
import contextlib
import errno
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time as time_module
import types
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
from composition.attended_paper_day import (
    LiveSourceApprovalError,
    LiveSourceConfigGateError,
    LiveSourceConnectError,
)
from domain.enums import Currency
from market_data.kis_official_ws_parser import TR_QUOTE, TR_TRADE
from market_data.monitor import (
    MonitorEvidence,
    MonitorExhaustedError,
    MonitorState,
    MonitorSummary,
)
from market_data.models import (
    MarketEventType,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)
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


def _post_close_at() -> datetime:
    return datetime.combine(date(2026, 6, 17), time(15, 42), tzinfo=KST)


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
    assert counters["trade_subscription_acks"] == 1
    assert counters["quote_subscription_acks"] == 1
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
    assert counters["quote_subscription_acks"] == 1
    assert counters.get("normalized_quotes", 0) == 0
    assert counters.get("quote_frames", 0) == 0
    assert counters["health_hold"] >= 1
    assert counters.get("orders", 0) == 0
    assert counters.get("fills", 0) == 0
    heartbeat_rows = [
        json.loads(line)
        for line in cfg.evidence_out.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "heartbeat"
    ]
    assert heartbeat_rows
    heartbeat = heartbeat_rows[-1]["snapshot"]
    assert heartbeat["quote_subscription_ready"] is True
    assert heartbeat["quote_frames"] == 0
    assert heartbeat["normalized_quotes"] == 0


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


def test_kis_live_full_pilot_post_close_is_blocked_before_source_open(tmp_path: Path) -> None:
    cfg = AttendedPaperDayConfig(
        session_date=date(2026, 6, 17),
        symbol=PILOT_SYMBOL,
        duration_seconds=5,
        evidence_out=tmp_path / "runtime" / "evidence.jsonl",
        summary_out=tmp_path / "runtime" / "summary.json",
        db_dir=tmp_path / "runtime" / "db",
        confirm_attended_paper=True,
        startup_only=False,
        source_kind="kis_live",
    )

    def source_factory(*, lifecycle: Any) -> ReplayMarketEventSource:
        raise AssertionError("live source must not be constructed outside OPEN session")

    summary = run_attended_paper_day(
        config=cfg,
        source_factory=source_factory,
        run_id="post-close",
        clock=_post_close_at,
    )

    assert summary["outcome"] == "NO_GO"
    assert summary["stop_reason"] == "invalid_session_window"
    counters = summary["counters"]["counters"]  # type: ignore[index]
    assert counters.get("connect_attempts", 0) == 0
    assert counters.get("connected", 0) == 0
    rows = [
        json.loads(line)
        for line in cfg.evidence_out.read_text(encoding="utf-8").splitlines()
    ]
    timing = [row for row in rows if row["event"] == "session_window_check"]
    assert timing
    assert timing[-1]["reason_code"] == "invalid_session_window"
    assert timing[-1]["snapshot"]["session_state"] == "POST_CLOSE"
    assert timing[-1]["snapshot"]["required_session_state"] == "OPEN"
    failures = [row for row in rows if row["event"] == "failed_closed"]
    assert failures and failures[-1]["reason_code"] == "invalid_session_window"


class _ExhaustedMonitor:
    def __init__(self, **kwargs: Any) -> None:
        self._on_evidence = kwargs["on_evidence"]
        self._session_id = kwargs["session_id"]

    async def run(self) -> MonitorSummary:
        at = _at()
        for attempt in range(1, 3):
            self._on_evidence(
                MonitorEvidence(
                    timestamp=at,
                    monitor_session_id=self._session_id,
                    state=MonitorState.CONNECTING,
                    connection_attempt=attempt,
                    consecutive_failures=attempt,
                    kind="drop",
                    reason_code="source_error",
                    reason_subcode="malformed_control_after_ack",
                )
            )
        self._on_evidence(
            MonitorEvidence(
                timestamp=at,
                monitor_session_id=self._session_id,
                state=MonitorState.EXHAUSTED,
                connection_attempt=2,
                consecutive_failures=2,
                kind="exhausted",
            )
        )
        raise MonitorExhaustedError(
            MonitorSummary(
                monitor_session_id=self._session_id,
                connection_attempts=2,
                consecutive_failures=2,
                applied=0,
                duplicate=0,
                out_of_order=0,
                stream_mismatch=0,
                future_event_error=0,
                final_state=MonitorState.EXHAUSTED,
            )
        )


class _PostReadinessExhaustedMonitor:
    def __init__(self, **kwargs: Any) -> None:
        self._on_evidence = kwargs["on_evidence"]
        self._session_id = kwargs["session_id"]

    async def run(self) -> MonitorSummary:
        at = _at()
        for event_type, channel, sequence in (
            (MarketEventType.BEST_BID_ASK.value, f"{TR_QUOTE}|{PILOT_SYMBOL}", 1),
            (MarketEventType.TRADE.value, f"{TR_TRADE}|{PILOT_SYMBOL}", 2),
        ):
            self._on_evidence(
                MonitorEvidence(
                    timestamp=at,
                    monitor_session_id=self._session_id,
                    state=MonitorState.RUNNING,
                    connection_attempt=1,
                    consecutive_failures=0,
                    kind="apply",
                    event_type=event_type,
                    provider="kis",
                    channel=channel,
                    market=PILOT_MARKET.value,
                    symbol=PILOT_SYMBOL,
                    sequence=sequence,
                    apply_status="applied",
                )
            )
        self._on_evidence(
            MonitorEvidence(
                timestamp=at,
                monitor_session_id=self._session_id,
                state=MonitorState.CONNECTING,
                connection_attempt=2,
                consecutive_failures=1,
                kind="drop",
                reason_code="source_error",
                reason_subcode="malformed_control_after_ack",
            )
        )
        self._on_evidence(
            MonitorEvidence(
                timestamp=at,
                monitor_session_id=self._session_id,
                state=MonitorState.EXHAUSTED,
                connection_attempt=2,
                consecutive_failures=1,
                kind="exhausted",
            )
        )
        raise MonitorExhaustedError(
            MonitorSummary(
                monitor_session_id=self._session_id,
                connection_attempts=2,
                consecutive_failures=1,
                applied=2,
                duplicate=0,
                out_of_order=0,
                stream_mismatch=0,
                future_event_error=0,
                final_state=MonitorState.EXHAUSTED,
            )
        )


class _UnexpectedMonitor:
    def __init__(self, **kwargs: Any) -> None:
        del kwargs

    async def run(self) -> MonitorSummary:
        raise RuntimeError("SENSITIVE_MARKER_SHOULD_NOT_APPEAR")


def test_monitor_exhaustion_snapshot_marks_pre_readiness_source_churn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apd, "MarketMonitor", _ExhaustedMonitor)
    cfg = _config(tmp_path)

    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([]),
        run_id="monitor-exhausted",
        clock=_at,
    )

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_exhausted_after_reconnects"
    assert summary["paper_only"] is True
    assert summary["activation_authorized"] is False
    assert summary["real_order_adapter_constructed"] is False
    assert summary["automatic_restart"] is False
    reason_counts = summary["counters"]["reason_counts"]  # type: ignore[index]
    assert reason_counts["source_exhausted_after_reconnects"] == 1
    assert "internal_runtime_error" not in reason_counts

    summary_text = cfg.summary_out.read_text(encoding="utf-8")
    evidence_text = cfg.evidence_out.read_text(encoding="utf-8")
    for forbidden in (
        "SENSITIVE_MARKER_SHOULD_NOT_APPEAR",
        "Traceback",
        "raw frame",
        "raw payload",
        "account=",
        "token=",
        "app_key=",
        "approval_key=",
    ):
        assert forbidden not in summary_text
        assert forbidden not in evidence_text

    rows = [json.loads(line) for line in evidence_text.splitlines() if line.strip()]
    failed = [row for row in rows if row["event"] == "failed_closed"]
    assert failed
    terminal = failed[-1]
    assert terminal["stage"] == "runtime"
    assert terminal["reason_code"] == "source_exhausted_after_reconnects"
    snapshot = terminal["snapshot"]
    assert snapshot["reason_subcode"] == "malformed_control_after_ack"
    assert snapshot["terminal_exhaustion_phase"] == "pre_market_data_readiness"
    assert snapshot["quote_readiness_reached"] is False
    assert snapshot["trade_readiness_reached"] is False
    assert snapshot["source_drop_subcode_counts"] == {"malformed_control_after_ack": 2}
    assert snapshot["quote_frames"] == 0
    assert snapshot["normalized_quotes"] == 0
    assert snapshot["trade_frames"] == 0
    assert snapshot["normalized_trades"] == 0
    assert snapshot["parse_success"] == 0
    assert snapshot["latest_heartbeat_at"] is None


def test_monitor_exhaustion_snapshot_marks_post_readiness_source_churn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apd, "MarketMonitor", _PostReadinessExhaustedMonitor)
    cfg = _config(tmp_path)

    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([]),
        run_id="monitor-exhausted-post-readiness",
        clock=_at,
    )

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_exhausted_after_reconnects"
    rows = [
        json.loads(line)
        for line in cfg.evidence_out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed = [row for row in rows if row["event"] == "failed_closed"]
    assert failed
    snapshot = failed[-1]["snapshot"]
    assert snapshot["reason_subcode"] == "malformed_control_after_ack"
    assert snapshot["terminal_exhaustion_phase"] == "post_market_data_readiness"
    assert snapshot["quote_readiness_reached"] is True
    assert snapshot["trade_readiness_reached"] is True
    assert snapshot["source_drop_subcode_counts"] == {"malformed_control_after_ack": 1}
    assert snapshot["quote_frames"] == 1
    assert snapshot["normalized_quotes"] == 1
    assert snapshot["trade_frames"] == 1
    assert snapshot["normalized_trades"] == 1
    assert snapshot["parse_success"] == 2


def test_unexpected_monitor_exception_remains_internal_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apd, "MarketMonitor", _UnexpectedMonitor)
    cfg = _config(tmp_path)

    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([]),
        run_id="monitor-unexpected",
        clock=_at,
    )

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "internal_runtime_error"
    summary_text = cfg.summary_out.read_text(encoding="utf-8")
    evidence_text = cfg.evidence_out.read_text(encoding="utf-8")
    assert "SENSITIVE_MARKER_SHOULD_NOT_APPEAR" not in summary_text
    assert "SENSITIVE_MARKER_SHOULD_NOT_APPEAR" not in evidence_text
    assert "Traceback" not in summary_text
    assert "Traceback" not in evidence_text
    rows = [json.loads(line) for line in evidence_text.splitlines() if line.strip()]
    failed = [row for row in rows if row["event"] == "failed_closed"]
    assert failed and failed[-1]["reason_code"] == "internal_runtime_error"


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
    assert summary["summary_publication_outcome"] == "WRITTEN"
    persisted = json.loads(cfg.summary_out.read_text(encoding="utf-8"))
    # F1: the persisted mechanical summary is captured verbatim in the envelope's
    # persisted_summary field — exact equality, not a subset that could hide drift.
    assert summary["persisted_summary"] == persisted
    # Envelope-only keys describe cleanup/publication/lock state and must NOT leak
    # into the persisted mechanical file.
    envelope_only = {
        "persisted_summary",
        "summary_publication_outcome",
        "summary_publication_reason_codes",
        "runtime_lock_fd_closed",
        "runtime_lock_unlinked",
        "runtime_lock_absent_confirmed",
        "runtime_lock_identity_matched",
        "runtime_lock_release_reason_code",
        "cleanup_outcome",
    }
    assert envelope_only.isdisjoint(persisted.keys())
    # The persisted file is exactly the mechanical projection of the envelope.
    assert persisted == {k: v for k, v in summary.items() if k not in envelope_only}


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


# --- RTM-7c.5a/5b: cleanup fatal, lock release, publication-state closure ---


@pytest.mark.parametrize("fatal_type", [MemoryError, SystemExit, KeyboardInterrupt])
def test_stack_resource_close_fatal_skips_pass_summary_and_preserves_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fatal_type: type[BaseException]
) -> None:
    real_build = apd.build_diagnostic_stack
    built: dict[str, Any] = {}

    def _wrap_build(**kwargs: Any) -> Any:
        stack = real_build(**kwargs)
        real_journal = stack.journal

        class _FatalJournal:
            def list_nonterminal(self) -> list[Any]:
                return real_journal.list_nonterminal()

            def close(self) -> None:
                raise fatal_type("stack close fatal")

            def __getattr__(self, name: str) -> Any:
                return getattr(real_journal, name)

        stack.journal = _FatalJournal()  # type: ignore[assignment]
        built["stack"] = stack
        return stack

    monkeypatch.setattr(apd, "build_diagnostic_stack", _wrap_build)
    cfg = _config(tmp_path)

    with pytest.raises(fatal_type):
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="stack-fatal",
            clock=_at,
        )

    assert not cfg.summary_out.exists()
    assert not (cfg.db_dir / ".paper_day.lock").exists()


def test_recorder_close_fatal_skips_pass_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = apd.EvidenceRecorder.close

    def _fatal_close(self: Any) -> None:
        raise SystemExit("recorder close fatal")

    monkeypatch.setattr(apd.EvidenceRecorder, "close", _fatal_close)
    cfg = _config(tmp_path)

    with pytest.raises(SystemExit):
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="recorder-fatal",
            clock=_at,
        )

    assert not cfg.summary_out.exists()
    assert not (cfg.db_dir / ".paper_day.lock").exists()


def test_lock_release_unlink_failure_prevents_pass_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_unlink = os.unlink

    def _boom_unlink(path: str | bytes, *a: Any, **k: Any) -> None:
        if str(path).endswith(".paper_day.lock"):
            raise OSError("unlink failed")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", _boom_unlink)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="lock-fail",
        clock=_at,
    )

    assert summary["outcome"] == "FAIL"
    assert summary["runtime_lock_release_reason_code"] == "runtime_lock_release_failed"
    assert summary["runtime_lock_absent_confirmed"] is False
    assert (cfg.db_dir / ".paper_day.lock").exists()


def test_lock_release_unlink_enoent_confirms_absent(tmp_path: Path) -> None:
    lock = apd.PilotRuntimeLock(tmp_path / ".paper_day.lock")
    lock.acquire()
    os.close(lock._fd)  # type: ignore[arg-type]
    lock._fd = None
    tmp_path.joinpath(".paper_day.lock").unlink()
    result = lock.release()
    assert result.lock_absent_confirmed is True
    assert result.reason_code is None


def test_publish_parent_fsync_failure_is_published_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        apd,
        "_fsync_directory",
        lambda _d: apd.DirectorySyncResult(
            opened=True, synced=False, closed=True, reason_code=apd._REASON_SYNC_FAILED
        ),
    )
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="fsync-fail",
        clock=_at,
    )

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "summary_published_incomplete"
    assert summary["summary_publication_outcome"] == "PUBLISHED_INCOMPLETE"
    assert cfg.summary_out.exists()
    persisted = json.loads(cfg.summary_out.read_text(encoding="utf-8"))
    assert persisted["outcome"] == "PASS"
    assert summary["outcome"] != persisted["outcome"]


def test_publish_destination_lstat_eio_is_publication_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_lstat = os.lstat
    calls = {"n": 0}

    def _lstat(path: str | bytes, *a: Any, **k: Any):
        p = str(path)
        if p.endswith("summary.json"):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("EIO")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(os, "lstat", _lstat)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="lstat-eio",
        clock=_at,
    )

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "summary_publication_uncertain"
    assert summary["summary_publication_outcome"] == "PUBLICATION_UNCERTAIN"
    if cfg.summary_out.exists():
        persisted = json.loads(cfg.summary_out.read_text(encoding="utf-8"))
        assert summary["outcome"] != persisted.get("outcome", summary["outcome"])


def test_publish_temp_unlink_failure_is_published_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_unlink = os.unlink

    def _selective_unlink(path: str | bytes, *a: Any, **k: Any) -> None:
        if ".paper_day_summary." in str(path):
            raise OSError("temp cleanup failed")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", _selective_unlink)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="temp-unlink",
        clock=_at,
    )

    assert summary["summary_publication_outcome"] == "PUBLISHED_INCOMPLETE"
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "summary_published_incomplete"


class _AckThenCloseBoomSource:
    close_calls = 0

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
        try:
            await asyncio.sleep(3600)
            yield _quote()
        finally:
            _AckThenCloseBoomSource.close_calls += 1
            raise RuntimeError("generator finally close failed")


class _AckThenCloseFatalSource:
    close_calls = 0

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
        try:
            await asyncio.sleep(3600)
            yield _quote()
        finally:
            _AckThenCloseFatalSource.close_calls += 1
            raise MemoryError("generator finally fatal")


def test_startup_probe_clean_cancel_is_pass(tmp_path: Path) -> None:
    _AckThenCloseBoomSource.close_calls = 0
    summary = run_attended_paper_day(
        config=_live_startup_cfg(tmp_path),
        source_factory=lambda *, lifecycle: _AckThenBlockSource(lifecycle),
        run_id="clean-cancel",
        clock=_at,
    )
    assert summary["outcome"] == "PASS"
    assert summary["stop_reason"] == "startup_only"


def test_startup_probe_generator_close_runtime_error_is_source_close_failed(
    tmp_path: Path,
) -> None:
    _AckThenCloseBoomSource.close_calls = 0
    summary = run_attended_paper_day(
        config=_live_startup_cfg(tmp_path),
        source_factory=lambda *, lifecycle: _AckThenCloseBoomSource(lifecycle),
        run_id="close-boom",
        clock=_at,
    )
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_close_failed"
    assert _AckThenCloseBoomSource.close_calls == 1


def test_startup_probe_generator_close_memory_error_preserves_fatal(
    tmp_path: Path,
) -> None:
    _AckThenCloseFatalSource.close_calls = 0
    cfg = _live_startup_cfg(tmp_path)
    with pytest.raises(MemoryError):
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: _AckThenCloseFatalSource(lifecycle),
            run_id="close-fatal",
            clock=_at,
        )
    assert _AckThenCloseFatalSource.close_calls == 1
    assert not cfg.summary_out.exists() or json.loads(cfg.summary_out.read_text())["outcome"] != "PASS"


def test_partial_construction_cleanup_fatal_overrides_constructor_ordinary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = {"n": 0}
    real_ads = apd.ActiveDecisionStore

    class TrackingActiveStore(real_ads):  # type: ignore[misc, valid-type]
        def close(self) -> None:
            closed["n"] += 1
            raise MemoryError("cleanup fatal")

    class BoomLedger:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("constructor ordinary")

    monkeypatch.setattr(apd, "ActiveDecisionStore", TrackingActiveStore)
    monkeypatch.setattr(apd, "SQLiteLedger", BoomLedger)

    with pytest.raises(MemoryError, match="cleanup fatal"):
        build_diagnostic_stack(
            config=_config(tmp_path),
            counters=DiagnosticCounters(),
            on_execution_evidence=lambda _ev: None,
        )
    assert closed["n"] == 1


def test_partial_construction_operation_fatal_overrides_cleanup_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = {"n": 0}
    real_ads = apd.ActiveDecisionStore

    class TrackingActiveStore(real_ads):  # type: ignore[misc, valid-type]
        def close(self) -> None:
            closed["n"] += 1
            raise RuntimeError("cleanup ordinary")

    class BoomLedger:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise MemoryError("constructor fatal")

    monkeypatch.setattr(apd, "ActiveDecisionStore", TrackingActiveStore)
    monkeypatch.setattr(apd, "SQLiteLedger", BoomLedger)

    with pytest.raises(MemoryError, match="constructor fatal"):
        build_diagnostic_stack(
            config=_config(tmp_path),
            counters=DiagnosticCounters(),
            on_execution_evidence=lambda _ev: None,
        )
    assert closed["n"] == 1


def test_cli_exit_code_matrix(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="cli-pass",
        clock=_at,
    )
    assert cli._cli_exit_code(summary) == 0

    summary_fail = {**summary, "outcome": "FAIL"}
    assert cli._cli_exit_code(summary_fail) == 1

    summary_incomplete = {
        **summary,
        "summary_publication_outcome": "PUBLISHED_INCOMPLETE",
    }
    assert cli._cli_exit_code(summary_incomplete) == 1


# --- RTM-7c.5a/5b §11: persisted/envelope, lock lifecycle, publication, fatal,
#     cleanup-outcome, and bounded-cancellation closure -------------------------


def test_persisted_summary_is_null_when_publication_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F1 corollary: when the summary file is not WRITTEN, the envelope carries
    # persisted_summary == None — the envelope never claims a persisted artifact
    # that does not exist on disk.
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
        run_id="persist-null",
        clock=_at,
    )

    assert summary["summary_publication_outcome"] == "NOT_WRITTEN"
    assert summary["persisted_summary"] is None
    assert not cfg.summary_out.exists()


def test_lock_release_fd_close_failure_blocks_clean_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An fd-close failure during release must surface fd_closed=False and a release
    # reason even when the unlink itself succeeds, so is_clean_pass/exit-0 is denied.
    import contextlib

    p = tmp_path / ".paper_day.lock"
    lock = apd.PilotRuntimeLock(p)
    lock.acquire()

    real_close = os.close
    closed: list[int] = []

    def boom_close(fd: int) -> None:
        closed.append(fd)
        raise OSError("close failed")

    monkeypatch.setattr(os, "close", boom_close)
    result = lock.release()
    monkeypatch.undo()
    for fd in closed:  # the spy never actually closed it; avoid the fd leak
        with contextlib.suppress(OSError):
            real_close(fd)

    assert result.fd_closed is False
    assert result.reason_code == "runtime_lock_release_failed"
    # unlink still proceeds: absence is confirmed, lock file removed.
    assert result.lock_unlinked is True
    assert result.lock_absent_confirmed is True
    assert not p.exists()

    envelope = {
        "outcome": "PASS",
        "summary_publication_outcome": "WRITTEN",
        "runtime_lock_fd_closed": result.fd_closed,
        "runtime_lock_absent_confirmed": result.lock_absent_confirmed,
        "runtime_lock_release_reason_code": result.reason_code,
        "cleanup_outcome": "INCOMPLETE",
    }
    assert apd.is_clean_pass(envelope) is False
    assert cli._cli_exit_code(envelope) == 1


def test_lock_release_foreign_inode_is_not_unlinked(tmp_path: Path) -> None:
    # If the lock file was replaced by a foreign inode after we acquired ours, the
    # release must refuse to unlink it and report runtime_lock_identity_mismatch.
    p = tmp_path / ".paper_day.lock"
    lock = apd.PilotRuntimeLock(p)
    lock.acquire()
    # Replace our inode with a different one (foreign owner).
    p.unlink()
    p.write_text("foreign-owner\n", encoding="utf-8")

    result = lock.release()

    assert result.identity_matched is False
    assert result.reason_code == "runtime_lock_identity_mismatch"
    assert result.lock_unlinked is False
    assert result.lock_absent_confirmed is False
    assert p.exists()  # foreign lock left intact
    assert p.read_text(encoding="utf-8") == "foreign-owner\n"


def test_lock_acquire_write_failure_leaves_no_stale_lock_or_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A post-open write failure must roll back: close the fd and unlink the inode we
    # created (identity verified), leaving no stale lock and no leaked fd.
    p = tmp_path / ".paper_day.lock"
    real_close = os.close
    closed: list[int] = []

    def spy_close(fd: int) -> None:
        closed.append(fd)
        return real_close(fd)

    def boom_write(fd: int, data: bytes) -> int:
        raise OSError("write failed")

    monkeypatch.setattr(os, "close", spy_close)
    monkeypatch.setattr(os, "write", boom_write)

    lock = apd.PilotRuntimeLock(p)
    with pytest.raises(apd.AttendedPaperDayRuntimeError) as ei:
        lock.acquire()

    assert ei.value.reason_code == "runtime_lock_acquire_failed"
    assert not p.exists()  # our inode was unlinked
    assert lock._fd is None  # type: ignore[attr-defined]
    assert lock._identity is None  # type: ignore[attr-defined]
    assert len(closed) >= 1  # the partial fd was closed exactly during rollback


def test_lock_acquire_write_fatal_preserves_fatal_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fatal during the post-open write is re-raised (identity preserved) but the
    # partial inode/fd are still rolled back first.
    p = tmp_path / ".paper_day.lock"

    def fatal_write(fd: int, data: bytes) -> int:
        raise MemoryError("oom mid-write")

    monkeypatch.setattr(os, "write", fatal_write)

    lock = apd.PilotRuntimeLock(p)
    with pytest.raises(MemoryError):
        lock.acquire()

    assert not p.exists()
    assert lock._fd is None  # type: ignore[attr-defined]
    assert lock._identity is None  # type: ignore[attr-defined]


def _release_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count PilotRuntimeLock.release invocations across a full run."""
    calls = {"n": 0}
    real_release = apd.PilotRuntimeLock.release

    def spy(self: Any) -> Any:
        calls["n"] += 1
        return real_release(self)

    monkeypatch.setattr(apd.PilotRuntimeLock, "release", spy)
    return calls


def test_publisher_ordinary_exception_releases_lock_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pre-link ordinary failure (no destination) becomes NOT_WRITTEN and still
    # releases the lock exactly once.
    calls = _release_spy(monkeypatch)
    real_open = os.open

    def boom_open(path: Any, *a: Any, **k: Any) -> int:
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            raise OSError(errno.ENOSPC, "no space for summary temp")
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", boom_open)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="pub-ordinary",
        clock=_at,
    )

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "summary_failed"
    assert summary["summary_publication_outcome"] == "NOT_WRITTEN"
    assert "summary_write_failed" in summary["summary_publication_reason_codes"]
    assert summary["cleanup_outcome"] == "CLEAN"
    assert summary["persisted_summary"] is None
    assert calls["n"] == 1
    assert not (cfg.db_dir / ".paper_day.lock").exists()


def test_publish_directory_sync_fatal_after_link_full_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RTM-7c.5a/5b repro: link lands, parent sync returns operation fatal — the
    # destination exists, publication state must be PUBLISHED_INCOMPLETE (never
    # NOT_WRITTEN), lock release runs once, then the fatal re-raises.
    calls = _release_spy(monkeypatch)
    captured: dict[str, apd.SummaryPublishResult] = {}
    real_publish = apd._publish_summary_create_new

    def spy_publish(path: Any, text: Any) -> apd.SummaryPublishResult:
        result = real_publish(path, text)
        captured["result"] = result
        return result

    sync_fatal = MemoryError("sync fatal")
    monkeypatch.setattr(apd, "_publish_summary_create_new", spy_publish)
    monkeypatch.setattr(
        apd,
        "_fsync_directory",
        lambda _d: apd.DirectorySyncResult(
            opened=True,
            synced=False,
            closed=True,
            reason_code=apd._REASON_SYNC_FAILED,
            fatal=sync_fatal,
        ),
    )
    cfg = _config(tmp_path)
    with pytest.raises(MemoryError) as ei:
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="sync-fatal-after-link",
            clock=_at,
        )

    assert ei.value is sync_fatal
    assert calls["n"] == 1
    assert not (cfg.db_dir / ".paper_day.lock").exists()
    assert cfg.summary_out.exists()
    assert captured["result"].outcome == apd.SummaryPublicationOutcome.PUBLISHED_INCOMPLETE
    assert captured["result"].fatal is sync_fatal
    assert captured["result"].outcome != apd.SummaryPublicationOutcome.NOT_WRITTEN


def test_publisher_fatal_exception_releases_lock_exactly_once_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pre-link operation fatal (no destination) returns NOT_WRITTEN with fatal
    # carried in SummaryPublishResult; lock is released once before re-raise.
    calls = _release_spy(monkeypatch)
    write_fatal = MemoryError("publisher oom")
    real_open = os.open
    real_write = os.write
    summary_temp_fds: set[int] = set()

    def spy_open(path: Any, *a: Any, **k: Any) -> int:
        fd = real_open(path, *a, **k)
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            summary_temp_fds.add(fd)
        return fd

    def fatal_write(fd: int, data: bytes) -> int:
        if fd in summary_temp_fds:
            raise write_fatal
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "write", fatal_write)
    cfg = _config(tmp_path)
    with pytest.raises(MemoryError) as ei:
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="pub-fatal",
            clock=_at,
        )

    assert ei.value is write_fatal
    assert calls["n"] == 1
    assert not (cfg.db_dir / ".paper_day.lock").exists()
    assert not cfg.summary_out.exists()


def test_publisher_directory_sync_failure_releases_lock_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A parent-dir fsync failure is a PUBLISHED_INCOMPLETE result (file linked but
    # durability unconfirmed); the lock is still released exactly once.
    calls = _release_spy(monkeypatch)
    monkeypatch.setattr(
        apd,
        "_fsync_directory",
        lambda _d: apd.DirectorySyncResult(
            opened=False, synced=False, closed=True, reason_code=apd._REASON_SYNC_FAILED
        ),
    )
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="pub-sync",
        clock=_at,
    )

    assert summary["summary_publication_outcome"] == "PUBLISHED_INCOMPLETE"
    # The cleanup itself succeeded (lock released, resources closed); only the
    # publication is incomplete, which is_clean_pass denies via the WRITTEN clause.
    assert summary["cleanup_outcome"] == "CLEAN"
    assert apd.is_clean_pass(summary) is False
    assert calls["n"] == 1
    assert not (cfg.db_dir / ".paper_day.lock").exists()


def test_recorder_open_fatal_preserved_and_lock_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fatal_open(self: Any) -> None:
        raise MemoryError("recorder open oom")

    monkeypatch.setattr(apd.EvidenceRecorder, "open", _fatal_open)
    cfg = _config(tmp_path)
    with pytest.raises(MemoryError):
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="recorder-open-fatal",
            clock=_at,
        )
    assert not (cfg.db_dir / ".paper_day.lock").exists()
    assert not cfg.summary_out.exists()


def test_build_stack_fatal_preserved_and_lock_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fatal_build(**_kwargs: Any) -> Any:
        raise MemoryError("stack build oom")

    monkeypatch.setattr(apd, "build_diagnostic_stack", _fatal_build)
    cfg = _config(tmp_path)
    with pytest.raises(MemoryError):
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="build-fatal",
            clock=_at,
        )
    assert not (cfg.db_dir / ".paper_day.lock").exists()
    assert not cfg.summary_out.exists()


def test_source_factory_fatal_in_probe_preserved_and_lock_released(
    tmp_path: Path,
) -> None:
    def fatal_factory(*, lifecycle: Any) -> Any:
        raise MemoryError("source factory oom")

    cfg = _live_startup_cfg(tmp_path)
    with pytest.raises(MemoryError):
        run_attended_paper_day(
            config=cfg,
            source_factory=fatal_factory,
            run_id="factory-fatal",
            clock=_at,
        )
    assert not (cfg.db_dir / ".paper_day.lock").exists()
    assert not cfg.summary_out.exists()


def test_publish_parent_mkdir_fatal_returns_not_written_with_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # parent.mkdir fatal is carried in SummaryPublishResult.fatal (never raised by
    # the publisher) with NOT_WRITTEN when no destination was created.
    mkdir_fatal = MemoryError("mkdir oom")

    def boom_mkdir(self: Any, *a: Any, **k: Any) -> None:
        raise mkdir_fatal

    monkeypatch.setattr(apd.Path, "mkdir", boom_mkdir)
    result = apd._publish_summary_create_new(tmp_path / "out" / "summary.json", "{}")
    assert result.outcome == apd.SummaryPublicationOutcome.NOT_WRITTEN
    assert result.fatal is mkdir_fatal


def test_ordinary_resource_close_failure_is_incomplete_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An ordinary (non-fatal) resource close failure marks cleanup INCOMPLETE and
    # forbids a clean PASS, while the summary is still published.
    real_build = apd.build_diagnostic_stack

    def _wrap_build(**kwargs: Any) -> Any:
        stack = real_build(**kwargs)
        real_journal = stack.journal

        class _BoomJournal:
            def list_nonterminal(self) -> list[Any]:
                return real_journal.list_nonterminal()

            def close(self) -> None:
                raise RuntimeError("journal close failed")

            def __getattr__(self, name: str) -> Any:
                return getattr(real_journal, name)

        stack.journal = _BoomJournal()  # type: ignore[assignment]
        return stack

    monkeypatch.setattr(apd, "build_diagnostic_stack", _wrap_build)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="ordinary-close",
        clock=_at,
    )

    assert summary["cleanup_outcome"] == "INCOMPLETE"
    assert summary["outcome"] == "NO_GO"
    assert summary["stop_reason"] == "resource_close_failure"
    assert apd.is_clean_pass(summary) is False


def test_stack_close_order_is_journal_ledger_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DiagnosticStack.close must close in reverse construction order:
    # journal -> ledger -> active_store.
    order: list[str] = []
    real_build = apd.build_diagnostic_stack

    def _wrap_build(**kwargs: Any) -> Any:
        stack = real_build(**kwargs)
        for label, attr in (
            ("journal", "journal"),
            ("ledger", "ledger"),
            ("active", "active_store"),
        ):
            real_obj = getattr(stack, attr)

            def _make(real_obj: Any, label: str) -> Any:
                class _Tracking:
                    def close(self) -> None:
                        order.append(label)
                        real_obj.close()

                    def __getattr__(self, name: str) -> Any:
                        return getattr(real_obj, name)

                return _Tracking()

            setattr(stack, attr, _make(real_obj, label))
        return stack

    monkeypatch.setattr(apd, "build_diagnostic_stack", _wrap_build)
    cfg = _config(tmp_path)
    run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="close-order",
        clock=_at,
    )

    assert order == ["journal", "ledger", "active"]


class _IgnoreFirstCancelSource:
    """ACKs, then swallows the first CancelledError so the probe must bound it.

    The second cancel (during asyncio.run shutdown) is re-raised so the event loop
    does not hang waiting on an uncancellable task.
    """

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle
        self._cancels = 0

    async def events(self):
        at = _at()
        self._lifecycle.on_connected(at=at)
        self._lifecycle.on_subscription_requested(tr_id=TR_TRADE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(tr_id=TR_TRADE, symbol=PILOT_SYMBOL, accepted=True, at=at)
        self._lifecycle.on_subscription_requested(tr_id=TR_QUOTE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(tr_id=TR_QUOTE, symbol=PILOT_SYMBOL, accepted=True, at=at)
        self._lifecycle.on_all_subscribed(at=at)
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self._cancels += 1
                if self._cancels >= 2:
                    raise
                # swallow the first cancel; keep the task alive to force a timeout
        yield _quote()


def test_startup_probe_bounds_uncancellable_consumer_with_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apd, "PROBE_CLEANUP_TIMEOUT_SECONDS", 0.2)
    cfg = _live_startup_cfg(tmp_path)
    started = time_module.monotonic()
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: _IgnoreFirstCancelSource(lifecycle),
        run_id="cancel-bound",
        clock=_at,
    )
    elapsed = time_module.monotonic() - started

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_close_timeout"
    assert elapsed < float(cfg.duration_seconds)  # bounded by cleanup timeout, not duration
    assert not (cfg.db_dir / ".paper_day.lock").exists()


# ---------------------------------------------------------------------------
# RTM-7c.5a/5b residual closure: partial lock-acquire rollback (F1)
# ---------------------------------------------------------------------------


def test_lock_acquire_fd_close_failure_is_uncertain_even_when_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # write OSError + close OSError + unlink success: the fd-close failure must not be
    # hidden. Even though the inode is unlinked, a leaked fd means the rollback is
    # uncertain, not a clean acquire_failed.
    p = tmp_path / ".paper_day.lock"
    real_close = os.close
    leaked: list[int] = []

    def boom_close(fd: int) -> None:
        leaked.append(fd)
        raise OSError("close failed")

    def boom_write(fd: int, data: bytes) -> int:
        raise OSError("write failed")

    monkeypatch.setattr(os, "close", boom_close)
    monkeypatch.setattr(os, "write", boom_write)

    lock = apd.PilotRuntimeLock(p)
    with pytest.raises(apd.AttendedPaperDayRuntimeError) as ei:
        lock.acquire()
    monkeypatch.undo()
    for fd in leaked:
        with contextlib.suppress(OSError):
            real_close(fd)

    assert ei.value.reason_code == "runtime_lock_acquire_uncertain"
    assert not p.exists()  # identity matched, so our inode was still unlinked
    assert lock._fd is None  # type: ignore[attr-defined]
    assert lock._identity is None  # type: ignore[attr-defined]


def test_lock_acquire_operation_fatal_outranks_rollback_close_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # write MemoryError (operation fatal) + close SystemExit (rollback cleanup fatal):
    # the operation fatal identity must be preserved (MemoryError), never replaced by
    # the cleanup fatal.
    p = tmp_path / ".paper_day.lock"
    real_close = os.close
    leaked: list[int] = []

    def fatal_close(fd: int) -> None:
        leaked.append(fd)
        raise SystemExit("close fatal during rollback")

    def fatal_write(fd: int, data: bytes) -> int:
        raise MemoryError("oom mid-write")

    monkeypatch.setattr(os, "close", fatal_close)
    monkeypatch.setattr(os, "write", fatal_write)

    lock = apd.PilotRuntimeLock(p)
    with pytest.raises(MemoryError):
        lock.acquire()
    monkeypatch.undo()
    for fd in leaked:
        with contextlib.suppress(OSError):
            real_close(fd)

    assert lock._fd is None  # type: ignore[attr-defined]
    assert lock._identity is None  # type: ignore[attr-defined]


def test_lock_acquire_foreign_inode_is_not_unlinked_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write failure rolls back, but if our inode was replaced by a foreign one before
    # the rollback unlink, the rollback must refuse to remove it (and report uncertain).
    p = tmp_path / ".paper_day.lock"
    real_close = os.close

    def spy_close(fd: int) -> None:
        return real_close(fd)

    def boom_write(fd: int, data: bytes) -> int:
        # Replace our inode with a foreign one before rollback inspects it.
        p.unlink()
        p.write_text("foreign\n", encoding="utf-8")
        raise OSError("write failed")

    monkeypatch.setattr(os, "close", spy_close)
    monkeypatch.setattr(os, "write", boom_write)

    lock = apd.PilotRuntimeLock(p)
    with pytest.raises(apd.AttendedPaperDayRuntimeError) as ei:
        lock.acquire()

    assert ei.value.reason_code == "runtime_lock_acquire_uncertain"
    assert p.exists()  # foreign inode left intact
    assert p.read_text(encoding="utf-8") == "foreign\n"


# ---------------------------------------------------------------------------
# Lock parent admission taxonomy (F6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PermissionError("denied"), "runtime_lock_parent_unreadable"),
        (OSError(errno.EACCES, "eacces"), "runtime_lock_parent_unreadable"),
        (OSError(errno.EIO, "eio"), "runtime_lock_parent_unreadable"),
        (OSError(errno.ENOSPC, "enospc"), "runtime_lock_acquire_failed"),
        (RuntimeError("weird"), "runtime_lock_acquire_uncertain"),
    ],
)
def test_lock_acquire_parent_mkdir_failure_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException, expected: str
) -> None:
    p = tmp_path / "missing" / ".paper_day.lock"

    def boom_mkdir(self: Any, *a: Any, **k: Any) -> None:
        raise exc

    monkeypatch.setattr(apd.Path, "mkdir", boom_mkdir)
    lock = apd.PilotRuntimeLock(p)
    with pytest.raises(apd.AttendedPaperDayRuntimeError) as ei:
        lock.acquire()

    assert ei.value.reason_code == expected
    assert ei.value.stage == "lock"
    assert lock._fd is None  # type: ignore[attr-defined]
    assert not p.exists()  # zero side effects: no lock inode created


def test_lock_acquire_parent_mkdir_fatal_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "missing" / ".paper_day.lock"

    def fatal_mkdir(self: Any, *a: Any, **k: Any) -> None:
        raise MemoryError("mkdir oom")

    monkeypatch.setattr(apd.Path, "mkdir", fatal_mkdir)
    lock = apd.PilotRuntimeLock(p)
    with pytest.raises(MemoryError):
        lock.acquire()
    assert lock._fd is None  # type: ignore[attr-defined]


def test_run_lock_parent_unreadable_writes_nothing_and_skips_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An end-to-end lock-parent failure returns a memory summary with zero writes and
    # never constructs the source (admission boundary holds before any output).
    cfg = _config(tmp_path)
    db_dir = cfg.db_dir
    real_mkdir = apd.Path.mkdir

    def selective_mkdir(self: Any, *a: Any, **k: Any) -> Any:
        if self == db_dir:
            raise PermissionError("db_dir denied")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(apd.Path, "mkdir", selective_mkdir)
    factory_calls = {"n": 0}

    def factory(*, lifecycle: Any) -> Any:
        factory_calls["n"] += 1
        return ReplayMarketEventSource([_quote(), _trade()])

    summary = run_attended_paper_day(
        config=cfg, source_factory=factory, run_id="parent-unreadable", clock=_at
    )

    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "runtime_lock_parent_unreadable"
    assert factory_calls["n"] == 0
    assert not cfg.evidence_out.exists()
    assert not cfg.summary_out.exists()
    assert not (db_dir / ".paper_day.lock").exists()


# ---------------------------------------------------------------------------
# Lock release fd-close fatal ownership (F2)
# ---------------------------------------------------------------------------


def test_lock_release_fd_close_fatal_is_captured_and_unlink_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An fd-close *fatal* during release must be captured in the result (not raised),
    # and the identity check + safe unlink must still be attempted.
    p = tmp_path / ".paper_day.lock"
    lock = apd.PilotRuntimeLock(p)
    lock.acquire()

    real_close = os.close
    leaked: list[int] = []

    def fatal_close(fd: int) -> None:
        leaked.append(fd)
        raise SystemExit("close fatal during release")

    monkeypatch.setattr(os, "close", fatal_close)
    result = lock.release()
    monkeypatch.undo()
    for fd in leaked:
        with contextlib.suppress(OSError):
            real_close(fd)

    assert isinstance(result.fatal, SystemExit)
    assert result.fd_closed is False
    # unlink was still attempted and succeeded (bounded cleanup continued past close).
    assert result.lock_unlinked is True
    assert result.lock_absent_confirmed is True
    assert not p.exists()


def test_release_close_fatal_does_not_replace_body_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # body MemoryError (operation fatal) + lock-release os.close SystemExit (cleanup
    # fatal): the body fatal must win, and the release fatal must never escape the
    # outer finalizer to replace it.
    class _FatalMonitor:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def run(self) -> None:
            raise MemoryError("fatal in market loop")

    monkeypatch.setattr(apd, "MarketMonitor", _FatalMonitor)

    real_release = apd.PilotRuntimeLock.release

    def release_with_close_fatal(self: Any) -> Any:
        saved_close = os.close
        captured: list[int] = []

        def boom(fd: int) -> None:
            captured.append(fd)
            saved_close(fd)  # actually close to avoid a leak
            raise SystemExit("close fatal during release")

        os.close = boom  # type: ignore[assignment]
        try:
            return real_release(self)
        finally:
            os.close = saved_close  # type: ignore[assignment]

    monkeypatch.setattr(apd.PilotRuntimeLock, "release", release_with_close_fatal)
    cfg = _config(tmp_path)

    with pytest.raises(MemoryError):
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="body-vs-release-fatal",
            clock=_at,
        )


# ---------------------------------------------------------------------------
# Publisher state recovery and fatal precedence (F3/F4)
# ---------------------------------------------------------------------------


def test_publish_post_link_verify_runtime_error_is_not_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-OSError raised during post-link verification must NOT collapse an actually
    # linked destination into NOT_WRITTEN — the file exists, so the outcome must be a
    # PUBLISHED_INCOMPLETE / PUBLICATION_UNCERTAIN recovery.
    real_lstat = os.lstat
    calls = {"n": 0}

    def _lstat(path: str | bytes, *a: Any, **k: Any):
        if str(path).endswith("summary.json"):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("post-link verify exploded")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(os, "lstat", _lstat)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="post-link-runtime",
        clock=_at,
    )

    assert summary["summary_publication_outcome"] in (
        "PUBLISHED_INCOMPLETE",
        "PUBLICATION_UNCERTAIN",
    )
    assert summary["summary_publication_outcome"] != "NOT_WRITTEN"
    assert summary["outcome"] == "FAIL"


def test_publish_temp_close_fatal_after_link_is_published_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # temp close MemoryError after link: PUBLISHED_INCOMPLETE + fatal preserved.
    real_open = os.open
    real_close = os.close
    temp_fds: set[int] = set()
    close_fatal = MemoryError("temp close fatal")

    def spy_open(path: Any, *a: Any, **k: Any) -> int:
        fd = real_open(path, *a, **k)
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            temp_fds.add(fd)
        return fd

    def fatal_close(fd: int) -> None:
        if fd in temp_fds:
            temp_fds.discard(fd)
            real_close(fd)
            raise close_fatal
        return real_close(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", fatal_close)
    result = apd._publish_summary_create_new(tmp_path / "out" / "summary.json", "{}")
    assert result.outcome == apd.SummaryPublicationOutcome.PUBLISHED_INCOMPLETE
    assert result.fatal is close_fatal
    assert (tmp_path / "out" / "summary.json").exists()


def test_publish_temp_close_fatal_full_run_preserves_publication_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    real_close = os.close
    temp_fds: set[int] = set()
    close_fatal = SystemExit("temp close fatal")

    def spy_open(path: Any, *a: Any, **k: Any) -> int:
        fd = real_open(path, *a, **k)
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            temp_fds.add(fd)
        return fd

    def fatal_close(fd: int) -> None:
        if fd in temp_fds:
            temp_fds.discard(fd)
            real_close(fd)
            raise close_fatal
        return real_close(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", fatal_close)
    cfg = _config(tmp_path)
    with pytest.raises(SystemExit) as ei:
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="temp-close-fatal",
            clock=_at,
        )
    assert ei.value is close_fatal
    assert cfg.summary_out.exists()
    # Envelope is not returned on fatal re-raise; verify via direct publish contract above.


def test_publish_temp_unlink_fatal_after_link_is_published_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlink_fatal = KeyboardInterrupt("temp unlink fatal")
    real_unlink = os.unlink

    def fatal_unlink(path: str | bytes, *a: Any, **k: Any) -> None:
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            raise unlink_fatal
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", fatal_unlink)
    result = apd._publish_summary_create_new(tmp_path / "out" / "summary.json", "{}")
    assert result.outcome == apd.SummaryPublicationOutcome.PUBLISHED_INCOMPLETE
    assert result.fatal is unlink_fatal
    assert (tmp_path / "out" / "summary.json").exists()


def test_publish_no_link_operation_fatal_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MemoryError before link: no destination, NOT_WRITTEN + fatal, lock released.
    open_fatal = MemoryError("open oom")
    real_open = os.open

    def fatal_open(path: Any, *a: Any, **k: Any) -> int:
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            raise open_fatal
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", fatal_open)
    result = apd._publish_summary_create_new(tmp_path / "out" / "summary.json", "{}")
    assert result.outcome == apd.SummaryPublicationOutcome.NOT_WRITTEN
    assert result.fatal is open_fatal
    assert not (tmp_path / "out" / "summary.json").exists()

    calls = _release_spy(monkeypatch)
    cfg = _config(tmp_path)
    with pytest.raises(MemoryError) as ei:
        run_attended_paper_day(
            config=cfg,
            source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
            run_id="no-link-fatal",
            clock=_at,
        )
    assert ei.value is open_fatal
    assert calls["n"] == 1
    assert not cfg.summary_out.exists()
    assert not (cfg.db_dir / ".paper_day.lock").exists()


def test_publish_operation_fatal_outranks_temp_close_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.link MemoryError (operation fatal) + temp close SystemExit (cleanup fatal):
    # operation fatal wins in SummaryPublishResult.fatal.
    real_open = os.open
    real_close = os.close
    temp_fds: set[int] = set()
    link_fatal = MemoryError("link oom")

    def spy_open(path: Any, *a: Any, **k: Any) -> int:
        fd = real_open(path, *a, **k)
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            temp_fds.add(fd)
        return fd

    def fatal_close(fd: int) -> None:
        if fd in temp_fds:
            temp_fds.discard(fd)
            real_close(fd)
            raise SystemExit("temp close fatal")
        return real_close(fd)

    def fatal_link(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        raise link_fatal

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", fatal_close)
    monkeypatch.setattr(os, "link", fatal_link)

    result = apd._publish_summary_create_new(tmp_path / "out" / "summary.json", "{}")
    assert result.fatal is link_fatal
    assert result.outcome == apd.SummaryPublicationOutcome.NOT_WRITTEN


def test_publish_directory_sync_fatal_outranks_temp_unlink_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # directory fsync MemoryError (operation fatal) + temp unlink KeyboardInterrupt
    # (cleanup fatal): operation fatal wins; destination published =>
    # PUBLISHED_INCOMPLETE.
    sync_fatal = MemoryError("dir sync oom")
    monkeypatch.setattr(
        apd,
        "_fsync_directory",
        lambda _d: apd.DirectorySyncResult(
            opened=True,
            synced=False,
            closed=True,
            reason_code=apd._REASON_SYNC_FAILED,
            fatal=sync_fatal,
        ),
    )
    real_unlink = os.unlink

    def fatal_unlink(path: str | bytes, *a: Any, **k: Any) -> None:
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            raise KeyboardInterrupt("temp unlink fatal")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", fatal_unlink)

    result = apd._publish_summary_create_new(tmp_path / "out" / "summary.json", "{}")
    assert result.fatal is sync_fatal
    assert result.outcome == apd.SummaryPublicationOutcome.PUBLISHED_INCOMPLETE
    assert (tmp_path / "out" / "summary.json").exists()


def test_publish_temp_close_runtime_error_is_published_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    real_close = os.close
    temp_fds: set[int] = set()

    def spy_open(path: Any, *a: Any, **k: Any) -> int:
        fd = real_open(path, *a, **k)
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            temp_fds.add(fd)
        return fd

    def boom_close(fd: int) -> None:
        if fd in temp_fds:
            temp_fds.discard(fd)
            real_close(fd)
            raise RuntimeError("temp close exploded")
        return real_close(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", boom_close)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="temp-close-runtime",
        clock=_at,
    )

    assert summary["summary_publication_outcome"] == "PUBLISHED_INCOMPLETE"
    assert summary["summary_publication_outcome"] != "NOT_WRITTEN"
    assert cfg.summary_out.exists()


def test_publish_temp_unlink_runtime_error_is_published_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_unlink = os.unlink

    def boom_unlink(path: str | bytes, *a: Any, **k: Any) -> None:
        if apd._SUMMARY_TEMP_PREFIX in str(path):
            raise RuntimeError("temp unlink exploded")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", boom_unlink)
    cfg = _config(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: ReplayMarketEventSource([_quote(), _trade()]),
        run_id="temp-unlink-runtime",
        clock=_at,
    )

    assert summary["summary_publication_outcome"] == "PUBLISHED_INCOMPLETE"
    assert summary["summary_publication_outcome"] != "NOT_WRITTEN"
    assert cfg.summary_out.exists()


# ---------------------------------------------------------------------------
# Startup cancellation termination contract (F5, Option B)
# ---------------------------------------------------------------------------


def test_all_cancel_ignoring_source_is_not_bounded_without_process_isolation(
    tmp_path: Path,
) -> None:
    # Honest contract boundary: a source that ignores EVERY CancelledError (including
    # the one asyncio.run delivers at loop shutdown) cannot be terminated in-process.
    # We reproduce it in a subprocess and assert it must be killed by a hard wall-clock
    # timeout — i.e. it is NOT bounded without process isolation. (The complementary
    # "compliant source is bounded" guarantee is proved by
    # tests/test_kis_ws_source.py::test_cancellation_cleans_up_and_reraises.)
    src_root = Path(__file__).resolve().parents[1] / "src"
    script = textwrap.dedent(
        """
        import asyncio
        import composition.attended_paper_day as apd

        apd.PROBE_CLEANUP_TIMEOUT_SECONDS = 0.1

        class AllIgnoreSource:
            def __init__(self, lifecycle):
                self._lifecycle = lifecycle

            async def events(self):
                from market_data.kis_official_ws_parser import TR_QUOTE, TR_TRADE
                at = apd.datetime.now(tz=apd.KST)
                self._lifecycle.on_connected(at=at)
                self._lifecycle.on_subscription_ack(
                    tr_id=TR_TRADE, symbol="005930", accepted=True, at=at
                )
                self._lifecycle.on_subscription_ack(
                    tr_id=TR_QUOTE, symbol="005930", accepted=True, at=at
                )
                self._lifecycle.on_all_subscribed(at=at)
                while True:
                    try:
                        await asyncio.sleep(3600)
                    except asyncio.CancelledError:
                        # Ignore EVERY cancel, including loop-shutdown cancellation.
                        continue
                yield  # unreachable

        lifecycle = apd.DiagnosticLifecycle(
            counters=apd.DiagnosticCounters(), tracker=None, clock=lambda: apd.datetime.now(tz=apd.KST)
        )
        apd._run_live_startup_probe(
            source_factory=lambda *, lifecycle: AllIgnoreSource(lifecycle),
            lifecycle=lifecycle,
            timeout_seconds=30.0,
        )
        print("UNEXPECTED_CLEAN_EXIT")
        """
    )
    env = {**os.environ, "PYTHONPATH": str(src_root)}
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            timeout=3.0,
            capture_output=True,
            text=True,
        )


# --- RTM-7c.6a: source-failed subreason taxonomy (fake-only) ----------------
#
# These tests pin the split of the collapsed ``source_failed`` reason into stable
# sanitized sub-reasons. They use fake source factories, a fake approval provider,
# and a fake websocket connect only — no live KIS, no network, no credentials.


class _ConfigGateBoomSource:
    """Factory-stage failure: raises the typed config-gate exception."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle


class _ApprovalBoomSource:
    """Factory-stage failure: raises the typed approval exception."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle


class _ConnectBoomSource:
    """Consumer-stage failure: the source raises the typed connect exception on the
    first await, as a lazy websocket connect would."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    async def events(self):
        raise LiveSourceConnectError("live-source websocket connect failed.")
        yield  # unreachable


class _ConnectFatalSource:
    """Consumer-stage fatal: a connect that raises MemoryError must not be swallowed
    into source_connect_failed; the fatal identity is preserved."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle

    async def events(self):
        raise MemoryError("oom during connect")
        yield  # unreachable


def test_factory_config_gate_error_is_source_config_gate_failed(tmp_path: Path) -> None:
    def factory(*, lifecycle: Any) -> Any:
        raise LiveSourceConfigGateError("enabled must be true.")

    summary = run_attended_paper_day(
        config=_live_startup_cfg(tmp_path),
        source_factory=factory,
        run_id="gate",
        clock=_at,
    )
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_config_gate_failed"


def test_factory_approval_error_is_source_approval_failed(tmp_path: Path) -> None:
    def factory(*, lifecycle: Any) -> Any:
        raise LiveSourceApprovalError("live-source approval issuance failed.")

    summary = run_attended_paper_day(
        config=_live_startup_cfg(tmp_path),
        source_factory=factory,
        run_id="approval",
        clock=_at,
    )
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_approval_failed"


def test_factory_fatal_is_preserved_not_source_failed(tmp_path: Path) -> None:
    def factory(*, lifecycle: Any) -> Any:
        raise MemoryError("oom in factory")

    with pytest.raises(MemoryError):
        run_attended_paper_day(
            config=_live_startup_cfg(tmp_path),
            source_factory=factory,
            run_id="factory-fatal",
            clock=_at,
        )
    assert not (tmp_path / "runtime" / "summary.json").exists()


def test_connect_error_is_source_connect_failed(tmp_path: Path) -> None:
    summary = run_attended_paper_day(
        config=_live_startup_cfg(tmp_path),
        source_factory=lambda *, lifecycle: _ConnectBoomSource(lifecycle),
        run_id="connect",
        clock=_at,
    )
    assert summary["outcome"] == "FAIL"
    assert summary["stop_reason"] == "source_connect_failed"


def test_connect_fatal_is_preserved_not_source_connect_failed(tmp_path: Path) -> None:
    with pytest.raises(MemoryError):
        run_attended_paper_day(
            config=_live_startup_cfg(tmp_path),
            source_factory=lambda *, lifecycle: _ConnectFatalSource(lifecycle),
            run_id="connect-fatal",
            clock=_at,
        )
    assert not (tmp_path / "runtime" / "summary.json").exists()


def test_subreason_split_preserves_evidence_subreason(tmp_path: Path) -> None:
    """The stable subreason must reach both the summary stop_reason and the evidence
    ``failed_closed`` reason — no collapse back to source_failed."""
    cfg = _live_startup_cfg(tmp_path)
    summary = run_attended_paper_day(
        config=cfg,
        source_factory=lambda *, lifecycle: _ConnectBoomSource(lifecycle),
        run_id="evidence",
        clock=_at,
    )
    assert summary["stop_reason"] == "source_connect_failed"
    lines = [
        json.loads(line)
        for line in cfg.evidence_out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed = [ev for ev in lines if ev["event"] == "failed_closed"]
    assert failed and failed[-1]["reason_code"] == "source_connect_failed"


# --- RTM-7c.6a: CLI live-source factory raise sites (fake transport/approval/ws) ---
#
# The CLI factory body owns the actual config-gate / approval / connect raise sites.
# These tests substitute a fake settings loader, a fake approval provider, and a fake
# websocket connect, so no credential is read and no socket is opened.

_APP_KEY_SENTINEL = "APPKEYSENTINEL_DO_NOT_LEAK"
_APP_SECRET_SENTINEL = "APPSECRETSENTINEL_DO_NOT_LEAK"


def _fake_kis_ws_settings(*, enabled: bool = True) -> Any:
    ws = types.SimpleNamespace(
        enabled=enabled,
        app_key_env="KIS_FAKE_APP_KEY",
        app_secret_env="KIS_FAKE_APP_SECRET",
        approval_base_url="https://example.invalid",
        websocket_url="ws://example.invalid:1/",
        connect_timeout_seconds=1,
        receive_timeout_seconds=1,
    )
    return types.SimpleNamespace(
        broker=types.SimpleNamespace(kis_ws_read_only=ws)
    )


class _CapturingWsSource:
    """Stands in for KisWsMarketEventSource: captures the constructor kwargs (notably
    the ``connect`` coroutine factory) without opening anything."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeApprovalOk:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def issue_approval_key(self) -> str:
        return "FAKE-APPROVAL-KEY"


def _fake_lifecycle() -> Any:
    return types.SimpleNamespace(on_kis_transport_event=lambda *a, **k: None)


def _patch_factory_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    provider_cls: Any = _FakeApprovalOk,
    open_ws: Any = None,
) -> None:
    if open_ws is None:
        async def open_ws(*a: Any, **k: Any) -> Any:  # noqa: ANN401
            return object()

    monkeypatch.setattr(
        "config.settings.load_settings",
        lambda *a, **k: _fake_kis_ws_settings(enabled=enabled),
    )
    monkeypatch.setattr("broker.kis_transport.StdlibKisHttpTransport", lambda *a, **k: object())
    monkeypatch.setattr("data.kis_ws_auth.KisWsApprovalProvider", provider_cls)
    monkeypatch.setattr("data.kis_ws_source.open_kis_websocket", open_ws)
    monkeypatch.setattr("data.kis_ws_source.KisWsMarketEventSource", _CapturingWsSource)


def _make_cli_factory(tmp_path: Path) -> Any:
    return cli._live_source_factory(tmp_path / "config.toml", _live_startup_cfg(tmp_path))


def test_cli_factory_disabled_is_config_gate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_factory_deps(monkeypatch, enabled=False)
    monkeypatch.setenv("KIS_FAKE_APP_KEY", _APP_KEY_SENTINEL)
    monkeypatch.setenv("KIS_FAKE_APP_SECRET", _APP_SECRET_SENTINEL)
    with pytest.raises(LiveSourceConfigGateError):
        _make_cli_factory(tmp_path)(lifecycle=_fake_lifecycle())


def test_cli_factory_missing_app_key_is_config_gate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_factory_deps(monkeypatch, enabled=True)
    monkeypatch.delenv("KIS_FAKE_APP_KEY", raising=False)
    monkeypatch.setenv("KIS_FAKE_APP_SECRET", _APP_SECRET_SENTINEL)
    with pytest.raises(LiveSourceConfigGateError):
        _make_cli_factory(tmp_path)(lifecycle=_fake_lifecycle())


def test_cli_factory_missing_app_secret_is_config_gate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_factory_deps(monkeypatch, enabled=True)
    monkeypatch.setenv("KIS_FAKE_APP_KEY", _APP_KEY_SENTINEL)
    monkeypatch.delenv("KIS_FAKE_APP_SECRET", raising=False)
    with pytest.raises(LiveSourceConfigGateError):
        _make_cli_factory(tmp_path)(lifecycle=_fake_lifecycle())


def test_cli_factory_approval_failure_is_approval_error_and_no_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeApprovalBoom:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def issue_approval_key(self) -> str:
            raise RuntimeError("approval endpoint returned non-200")

    _patch_factory_deps(monkeypatch, enabled=True, provider_cls=_FakeApprovalBoom)
    monkeypatch.setenv("KIS_FAKE_APP_KEY", _APP_KEY_SENTINEL)
    monkeypatch.setenv("KIS_FAKE_APP_SECRET", _APP_SECRET_SENTINEL)
    with pytest.raises(LiveSourceApprovalError) as ei:
        _make_cli_factory(tmp_path)(lifecycle=_fake_lifecycle())
    text = str(ei.value)
    assert _APP_KEY_SENTINEL not in text
    assert _APP_SECRET_SENTINEL not in text


def test_cli_factory_approval_fatal_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeApprovalFatal:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def issue_approval_key(self) -> str:
            raise MemoryError("oom during approval")

    _patch_factory_deps(monkeypatch, enabled=True, provider_cls=_FakeApprovalFatal)
    monkeypatch.setenv("KIS_FAKE_APP_KEY", _APP_KEY_SENTINEL)
    monkeypatch.setenv("KIS_FAKE_APP_SECRET", _APP_SECRET_SENTINEL)
    with pytest.raises(MemoryError):
        _make_cli_factory(tmp_path)(lifecycle=_fake_lifecycle())


def test_cli_factory_connect_failure_is_connect_error_and_no_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _open_ws_boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("websocket handshake refused")

    _patch_factory_deps(monkeypatch, enabled=True, open_ws=_open_ws_boom)
    monkeypatch.setenv("KIS_FAKE_APP_KEY", _APP_KEY_SENTINEL)
    monkeypatch.setenv("KIS_FAKE_APP_SECRET", _APP_SECRET_SENTINEL)
    source = _make_cli_factory(tmp_path)(lifecycle=_fake_lifecycle())
    connect = source.kwargs["connect"]
    with pytest.raises(LiveSourceConnectError) as ei:
        asyncio.run(connect())
    text = str(ei.value)
    assert _APP_KEY_SENTINEL not in text
    assert _APP_SECRET_SENTINEL not in text


def test_cli_factory_connect_fatal_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _open_ws_fatal(*a: Any, **k: Any) -> Any:
        raise MemoryError("oom during connect")

    _patch_factory_deps(monkeypatch, enabled=True, open_ws=_open_ws_fatal)
    monkeypatch.setenv("KIS_FAKE_APP_KEY", _APP_KEY_SENTINEL)
    monkeypatch.setenv("KIS_FAKE_APP_SECRET", _APP_SECRET_SENTINEL)
    source = _make_cli_factory(tmp_path)(lifecycle=_fake_lifecycle())
    connect = source.kwargs["connect"]
    with pytest.raises(MemoryError):
        asyncio.run(connect())
