#!/usr/bin/env python3
"""RTM-6 operator KIS websocket read-only smoke (bounded; public quote/trade only).

Does not submit orders / move money / touch balances or ledger.
Does not connect broker / paper execution / scheduler.
Default mode is --validate-only: no credentials, no network, no filesystem writes.
--run is an explicit manual opt-in that REQUIRES a confirmation env phrase AND a bound
(--max-events or --duration-seconds). It is never invoked automatically.

This CLI reuses MarketMonitor as the single reconnect/backoff owner and the official
KIS frame parser. Raw frames and credentials are never printed or stored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from broker.kis_transport import StdlibKisHttpTransport
from config.settings import AppSettings, KisWsReadOnlySettings, SettingsError, load_settings
from data.kis_ws_auth import KisWsApprovalProvider, KisWsAuthError
from data.kis_ws_source import (
    KisWsMarketEventSource,
    KisWsSubscription,
    KisWsTransportEvent,
    open_kis_websocket,
)
from market_data.kis_official_ws_parser import TR_QUOTE, TR_TRADE
from market_data.latest_state import LatestMarketStateStore
from market_data.monitor import MarketMonitor, MonitorEvidence, MonitorSummary
from market_data.protocols import MarketEventSource

DEFAULT_CONFIG_PATH = "config/config.toml.example"
_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_SYMBOL = "005930"
# 두 TR(체결/호가)을 한 종목에 구독한다. 1 종목 권장.
_TR_IDS = (TR_TRADE, TR_QUOTE)
_KRX_SYMBOL_RE = re.compile(r"^\d{6}$")


class SmokeInputError(Exception):
    """CLI 입력/게이트 위반. 메시지에 credential을 담지 않는다."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KIS websocket read-only smoke (bounded; no orders, no money).",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"config path (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="config/plan preflight only; no credentials, no network, no fs writes (default)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="perform the bounded read-only websocket smoke (explicit manual opt-in)",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        help=f"KRX symbol to subscribe (repeatable; default: {_DEFAULT_SYMBOL})",
    )
    parser.add_argument("--max-events", type=int, default=None, help="stop after N applied events (bound)")
    parser.add_argument("--duration-seconds", type=float, default=None, help="stop after N seconds (bound)")
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=None,
        help="append-only JSONL evidence path; must be under runtime/ (ignored in validate-only)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary")
    return parser


def _resolve_symbols(raw: Sequence[str] | None) -> list[str]:
    symbols = list(raw) if raw else [_DEFAULT_SYMBOL]
    cleaned: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        value = symbol.strip()
        if not value:
            raise SmokeInputError("symbol must be non-empty.")
        if not _KRX_SYMBOL_RE.match(value):
            raise SmokeInputError("symbol must be a 6-digit KRX code.")
        if value in seen:
            continue  # 중복 종목은 조용히 제거(같은 구독 중복 방지).
        seen.add(value)
        cleaned.append(value)
    return cleaned


def build_subscriptions(symbols: Sequence[str], *, max_subscriptions: int) -> list[KisWsSubscription]:
    subscriptions: list[KisWsSubscription] = []
    seen: set[tuple[str, str]] = set()
    for symbol in symbols:
        for tr_id in _TR_IDS:
            key = (tr_id, symbol)
            if key in seen:
                continue  # (tr_id, symbol) 중복 구독 제거.
            seen.add(key)
            subscriptions.append(KisWsSubscription(tr_id=tr_id, symbol=symbol))
    if len(subscriptions) > max_subscriptions:
        raise SmokeInputError(
            f"requested {len(subscriptions)} subscriptions exceeds max_subscriptions={max_subscriptions}."
        )
    return subscriptions


def _validate_bounds(max_events: int | None, duration_seconds: float | None) -> None:
    if max_events is None and duration_seconds is None:
        raise SmokeInputError("--run requires a bound: set --max-events and/or --duration-seconds.")
    if max_events is not None and max_events < 1:
        raise SmokeInputError("--max-events must be >= 1.")
    if duration_seconds is not None and duration_seconds <= 0:
        raise SmokeInputError("--duration-seconds must be > 0.")


def _confirmation_ok(ws_settings: KisWsReadOnlySettings, environ: Mapping[str, str]) -> bool:
    return environ.get(ws_settings.confirmation_env_var) == ws_settings.confirmation_phrase


def _validate_evidence_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.resolve()
    runtime_root = (Path.cwd() / "runtime").resolve()
    if runtime_root not in resolved.parents:
        raise SmokeInputError("--evidence-out must be a path under runtime/.")
    return resolved


def _plan_summary(
    *,
    mode: str,
    config_path: str,
    ws_settings: KisWsReadOnlySettings,
    symbols: Sequence[str],
    subscriptions: Sequence[KisWsSubscription],
    max_events: int | None,
    duration_seconds: float | None,
) -> dict[str, Any]:
    return {
        "outcome": "PASS",
        "mode": mode,
        "config": config_path,
        "enabled": ws_settings.enabled,
        "websocket_url": ws_settings.websocket_url,
        "approval_base_url": ws_settings.approval_base_url,
        "symbols": list(symbols),
        "subscriptions": [{"tr_id": s.tr_id, "symbol": s.symbol} for s in subscriptions],
        "max_subscriptions": ws_settings.max_subscriptions,
        "max_events": max_events,
        "duration_seconds": duration_seconds,
        "confirmation_env_var": ws_settings.confirmation_env_var,
        "orders_called": False,
        "http_called": False,
        "network_called": False,
    }


def execute_run(
    *,
    ws_settings: KisWsReadOnlySettings,
    environ: Mapping[str, str],
    subscriptions: Sequence[KisWsSubscription],
    max_events: int | None,
    duration_seconds: float | None,
    connect: Callable[[], Any],
    approval_transport: Any,
    clock: Callable[[], datetime] | None = None,
    evidence_out: Path | None = None,
) -> dict[str, Any]:
    """주입 가능한 bounded run 코어. connect/approval_transport를 주입해 네트워크 없이도
    배선을 검증할 수 있다. 실제 --run은 real connect/transport를 주입한다."""
    app_key = environ.get(ws_settings.app_key_env, "")
    app_secret = environ.get(ws_settings.app_secret_env, "")
    provider = KisWsApprovalProvider(
        transport=approval_transport,
        approval_base_url=ws_settings.approval_base_url,
        app_key=app_key,
        app_secret=app_secret,
        timeout_seconds=ws_settings.connect_timeout_seconds,
    )
    approval_key = provider.issue_approval_key()

    transport_events: list[KisWsTransportEvent] = []
    evidence_log: list[MonitorEvidence] = []
    run_clock = clock or (lambda: datetime.now(tz=_KST))

    def source_factory() -> MarketEventSource:
        return KisWsMarketEventSource(
            connect=connect,
            approval_key=approval_key,
            subscriptions=subscriptions,
            clock=run_clock,
            receive_timeout_seconds=ws_settings.receive_timeout_seconds,
            max_events=max_events,
            on_transport_event=transport_events.append,
        )

    store = LatestMarketStateStore()
    monitor = MarketMonitor(
        store=store,
        source_factory=source_factory,
        clock=run_clock,
        session_id="kis-ws-readonly-smoke",
        max_events=max_events,
        max_runtime_seconds=duration_seconds,
        on_evidence=evidence_log.append,
    )
    started_at = run_clock()
    summary = asyncio.run(monitor.run())
    duration_seconds_observed = (run_clock() - started_at).total_seconds()

    health = Counter(event.kind for event in transport_events)
    market_health = _tally_market_health(evidence_log)
    result = _run_summary(
        summary,
        health,
        market_health=market_health,
        subscriptions=subscriptions,
        connection_duration_seconds=duration_seconds_observed,
    )
    if evidence_out is not None:
        _write_evidence(evidence_out, result, transport_events)
    return result


def _tally_market_health(evidence: Sequence[MonitorEvidence]) -> dict[str, Any]:
    """trade/quote parsed·applied, heartbeat 수, 마지막 시세 이벤트 시각을 집계한다.

    transport-health(connected/subscribed…)와 분리된 market-data health다. heartbeat는
    parsed/applied에 섞지 않고 별도 카운트한다(heartbeat-only false PASS 방지의 근거)."""
    trade_parsed = trade_applied = 0
    quote_parsed = quote_applied = 0
    heartbeat = 0
    last_market_event_at: datetime | None = None
    for ev in evidence:
        applied = ev.apply_status == "applied"
        if ev.event_type == "trade":
            trade_parsed += 1
            trade_applied += int(applied)
        elif ev.event_type == "best_bid_ask":
            quote_parsed += 1
            quote_applied += int(applied)
        elif ev.event_type == "heartbeat":
            heartbeat += 1
            continue  # heartbeat는 시세 이벤트가 아니므로 last_market_event_at 갱신에서 제외.
        else:
            continue
        if last_market_event_at is None or ev.timestamp > last_market_event_at:
            last_market_event_at = ev.timestamp
    return {
        "trade_parsed": trade_parsed,
        "trade_applied": trade_applied,
        "quote_parsed": quote_parsed,
        "quote_applied": quote_applied,
        "heartbeat_count": heartbeat,
        "last_market_event_at": (
            last_market_event_at.isoformat() if last_market_event_at is not None else None
        ),
    }


def _run_summary(
    summary: MonitorSummary,
    health: Counter,
    *,
    market_health: Mapping[str, Any],
    subscriptions: Sequence[KisWsSubscription],
    connection_duration_seconds: float,
) -> dict[str, Any]:
    # PASS = 모든 구독 ack(all_subscribed) AND quote applied >= 1.
    # heartbeat-only(quote_applied == 0)는 FAIL로 떨어진다.
    all_subscribed = health.get("all_subscribed", 0) >= 1
    quote_applied = int(market_health["quote_applied"])
    passed = all_subscribed and quote_applied >= 1
    return {
        "outcome": "PASS" if passed else "FAIL",
        "mode": "run",
        "final_state": summary.final_state.value,
        "connection_attempts": summary.connection_attempts,
        "connection_duration_seconds": connection_duration_seconds,
        "subscriptions_expected": len(subscriptions),
        "subscriptions_acked": health.get("subscribed", 0),
        "all_subscribed_epochs": health.get("all_subscribed", 0),
        "applied": summary.applied,
        "duplicate": summary.duplicate,
        "out_of_order": summary.out_of_order,
        "stream_mismatch": summary.stream_mismatch,
        "future_event_error": summary.future_event_error,
        "market_health": dict(market_health),
        "transport_health": dict(health),
        "orders_called": False,
        "network_called": True,
    }


def _write_evidence(path: Path, summary: dict[str, Any], events: Sequence[KisWsTransportEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            record = {
                "kind": event.kind,
                "tr_id": event.tr_id,
                "symbol": event.symbol,
                "rt_cd": event.rt_cd,
                "detail": event.detail,
                "at": event.at.isoformat() if event.at is not None else None,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")


def _emit(summary: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False), file=out)
        return
    print(f"KIS WS read-only smoke: {summary.get('outcome', 'FAIL')}", file=out)
    for key, value in summary.items():
        if key == "outcome":
            continue
        print(f"{key}: {value}", file=out)


def _fail(reason: str, *, as_json: bool, out: TextIO) -> int:
    _emit({"outcome": "FAIL", "reason": reason}, as_json=as_json, out=out)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    if args.run and args.validate_only:
        return _fail("--run and --validate-only are mutually exclusive.", as_json=as_json, out=out)
    mode = "run" if args.run else "validate-only"

    try:
        settings: AppSettings = load_settings(args.config)
    except (SettingsError, OSError) as exc:
        return _fail(f"config error: {exc}", as_json=as_json, out=out)
    ws_settings = settings.broker.kis_ws_read_only

    try:
        symbols = _resolve_symbols(args.symbol)
        subscriptions = build_subscriptions(symbols, max_subscriptions=ws_settings.max_subscriptions)
    except SmokeInputError as exc:
        return _fail(str(exc), as_json=as_json, out=out)

    if mode == "validate-only":
        summary = _plan_summary(
            mode=mode,
            config_path=args.config,
            ws_settings=ws_settings,
            symbols=symbols,
            subscriptions=subscriptions,
            max_events=args.max_events,
            duration_seconds=args.duration_seconds,
        )
        _emit(summary, as_json=as_json, out=out)
        return 0

    # mode == "run": explicit gates before any network.
    environ = dict(os.environ)
    try:
        if not ws_settings.enabled:
            raise SmokeInputError("config broker.kis_ws_read_only.enabled must be true for --run.")
        if not _confirmation_ok(ws_settings, environ):
            raise SmokeInputError(
                f"--run requires env {ws_settings.confirmation_env_var}={ws_settings.confirmation_phrase}."
            )
        _validate_bounds(args.max_events, args.duration_seconds)
        evidence_out = _validate_evidence_path(args.evidence_out)
    except SmokeInputError as exc:
        return _fail(str(exc), as_json=as_json, out=out)

    def connect() -> Any:
        return open_kis_websocket(
            ws_settings.websocket_url, connect_timeout_seconds=ws_settings.connect_timeout_seconds
        )

    try:
        summary = execute_run(
            ws_settings=ws_settings,
            environ=environ,
            subscriptions=subscriptions,
            max_events=args.max_events,
            duration_seconds=args.duration_seconds,
            connect=connect,
            approval_transport=StdlibKisHttpTransport(),
            evidence_out=evidence_out,
        )
    except (KisWsAuthError, SmokeInputError) as exc:
        return _fail(str(exc), as_json=as_json, out=out)
    except Exception:  # noqa: BLE001 — ops entrypoint fail-closed, no raw frame/credential leak
        return _fail("run failed (sanitized).", as_json=as_json, out=out)

    _emit(summary, as_json=as_json, out=out)
    return 0 if summary.get("outcome") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
