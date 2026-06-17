#!/usr/bin/env python3
"""Operator CLI for RTM-7c.5 attended paper-day diagnostics.

Default mode is validate-only: no credentials, no network, no DB open, no writes.
Actual live KIS startup/run is an explicit operator action via --live-kis and is
never used by tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from market_data.kis_official_ws_parser import TR_QUOTE, TR_TRADE
from market_data.models import (
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)
from market_data.replay_source import ReplayMarketEventSource

from composition.attended_paper_day import (
    AttendedPaperDayConfig,
    AttendedPaperDayInputError,
    KST,
    PILOT_MARKET,
    PILOT_SYMBOL,
    run_attended_paper_day,
    validate_attended_paper_day_inputs,
)
from domain.enums import Currency


class CliError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-symbol attended paper-day diagnostic.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--evidence-out", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--db-dir", type=Path, required=True)
    parser.add_argument("--confirm-attended-paper", action="store_true")
    parser.add_argument("--startup-only", action="store_true")
    parser.add_argument("--offline-fixture", choices=("deterministic",), default=None)
    parser.add_argument("--live-kis", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args)
        validate_attended_paper_day_inputs(config)
        if args.validate_only or (not args.offline_fixture and not args.live_kis):
            _emit(
                {
                    "outcome": "PASS",
                    "mode": "validate-only",
                    "network_called": False,
                    "credential_read": False,
                    "db_open": False,
                    "filesystem_written": False,
                    "activation_authorized": False,
                    "symbol": config.symbol,
                },
                json_mode=args.json,
            )
            return 0
        if bool(args.offline_fixture) == bool(args.live_kis):
            raise CliError("choose exactly one of --offline-fixture or --live-kis for run mode.")
        if args.offline_fixture:
            source_factory = lambda *, lifecycle: ReplayMarketEventSource(
                _fixture_events(config.session_date)
            )
            clock = _fixture_clock(config.session_date)
        else:
            source_factory = _live_source_factory(args.config, config)
            clock = lambda: datetime.now(tz=KST)
        summary = run_attended_paper_day(
            config=config,
            source_factory=source_factory,
            clock=clock,
        )
        _emit(summary, json_mode=args.json)
        return _cli_exit_code(summary)
    except (AttendedPaperDayInputError, CliError) as exc:
        _emit(
            {
                "outcome": "FAIL",
                "reason": str(exc),
                "network_called": False,
                "credential_read": False,
                "db_open": False,
                "filesystem_written": False,
                "activation_authorized": False,
            },
            json_mode=args.json,
        )
        return 1
    except Exception:
        _emit(
            {
                "outcome": "FAIL",
                "reason": "internal_runtime_error",
                "network_called": False,
                "credential_read": False,
                "db_open": False,
                "filesystem_written": False,
                "activation_authorized": False,
                "paper_only": True,
                "real_order_adapter_constructed": False,
            },
            json_mode=args.json,
        )
        return 1


def _cli_exit_code(summary: dict[str, object]) -> int:
    """PASS는 summary WRITTEN + lock absent confirmed에서만 exit 0."""
    if summary.get("outcome") != "PASS":
        return 1
    if summary.get("summary_publication_outcome") != "WRITTEN":
        return 1
    if summary.get("runtime_lock_absent_confirmed") is not True:
        return 1
    return 0


def _config_from_args(args: argparse.Namespace) -> AttendedPaperDayConfig:
    try:
        session_date = date.fromisoformat(args.session_date)
    except ValueError as exc:
        raise CliError("session-date must be YYYY-MM-DD.") from exc
    if not args.config.exists():
        raise CliError("config path must exist.")
    return AttendedPaperDayConfig(
        session_date=session_date,
        symbol=args.symbol,
        duration_seconds=args.duration_seconds,
        evidence_out=args.evidence_out,
        summary_out=args.summary_out,
        db_dir=args.db_dir,
        confirm_attended_paper=args.confirm_attended_paper,
        startup_only=args.startup_only,
        source_kind="kis_live" if args.live_kis else "replay",
    )


def _fixture_clock(session_date: date):
    now = datetime.combine(session_date, time(9, 30), tzinfo=KST)
    return lambda: now


def _fixture_events(session_date: date) -> list[object]:
    at = datetime.combine(session_date, time(9, 30), tzinfo=KST)
    trade_ch = f"{TR_TRADE}|{PILOT_SYMBOL}"
    quote_ch = f"{TR_QUOTE}|{PILOT_SYMBOL}"
    return [
        NormalizedBestBidAsk(
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
                provider="kis", channel=quote_ch, sequence=1, received_at=at
            ),
        ),
        NormalizedTradeTick(
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
                provider="kis", channel=trade_ch, sequence=1, received_at=at
            ),
        ),
    ]


def _live_source_factory(config_path: Path, config: AttendedPaperDayConfig):
    # Lazy by contract: settings load, credential-env reads, and approval issuance all
    # happen inside the factory body, which the runtime invokes only AFTER admission
    # (lock acquisition + path/DB ownership) succeeds. A lock/path/DB admission failure
    # therefore reads zero secrets and opens zero network connections.
    def factory(*, lifecycle) -> "object":
        import os

        from broker.kis_transport import StdlibKisHttpTransport
        from config.settings import load_settings
        from data.kis_ws_auth import KisWsApprovalProvider
        from data.kis_ws_source import (
            KisWsMarketEventSource,
            KisWsSubscription,
            open_kis_websocket,
        )

        settings = load_settings(config_path)
        ws = settings.broker.kis_ws_read_only
        if not ws.enabled:
            raise CliError("broker.kis_ws_read_only.enabled must be true for --live-kis.")
        app_key = os.environ.get(ws.app_key_env)
        app_secret = os.environ.get(ws.app_secret_env)
        if not app_key or not app_secret:
            raise CliError("KIS app key/secret env vars are required for --live-kis.")
        if config.symbol != PILOT_SYMBOL:
            raise CliError("only symbol 005930 is allowed.")
        approval = KisWsApprovalProvider(
            transport=StdlibKisHttpTransport(),
            approval_base_url=ws.approval_base_url,
            app_key=app_key,
            app_secret=app_secret,
            timeout_seconds=ws.connect_timeout_seconds,
        ).issue_approval_key()
        return KisWsMarketEventSource(
            connect=lambda: open_kis_websocket(
                ws.websocket_url, connect_timeout_seconds=ws.connect_timeout_seconds
            ),
            approval_key=approval,
            subscriptions=(
                KisWsSubscription(tr_id=TR_TRADE, symbol=config.symbol),
                KisWsSubscription(tr_id=TR_QUOTE, symbol=config.symbol),
            ),
            clock=lambda: datetime.now(tz=KST),
            receive_timeout_seconds=ws.receive_timeout_seconds,
            on_transport_event=lifecycle.on_kis_transport_event,
        )

    return factory


def _emit(payload: object, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(payload)


if __name__ == "__main__":
    sys.exit(main())
