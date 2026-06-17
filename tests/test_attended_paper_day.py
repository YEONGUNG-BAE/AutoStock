from __future__ import annotations

import importlib.util
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from composition.attended_paper_day import (
    AttendedPaperDayConfig,
    KST,
    PILOT_MARKET,
    PILOT_SYMBOL,
    run_attended_paper_day,
)
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
        source_factory=lambda: ReplayMarketEventSource([_quote(), _trade()]),
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
        source_factory=lambda: ReplayMarketEventSource([_trade()]),
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
        source_factory=lambda: ReplayMarketEventSource([_quote(), _trade()]),
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
        source_factory=lambda lifecycle: _LiveStartupSource(lifecycle),
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
        source_factory=lambda: ReplayMarketEventSource([_quote(), _trade()]),
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
        source_factory=lambda: ReplayMarketEventSource([_quote(), _trade()]),
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
