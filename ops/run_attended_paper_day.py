#!/usr/bin/env python3
"""Operator CLI for RTM-7c.5 attended paper-day diagnostics.

Default mode is validate-only: no credentials, no network, no DB open, no writes.
Actual live KIS startup/run is an explicit operator action via --live-kis and is
never used by tests.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence
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
    LiveSourceApprovalError,
    LiveSourceConfigGateError,
    LiveSourceConnectError,
    PILOT_MARKET,
    PILOT_SYMBOL,
    is_clean_pass,
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
    parser.add_argument(
        "--stdout-envelope-out",
        type=Path,
        default=None,
        help=(
            "persist the structured stdout envelope (full --json payload plus a "
            "sanitized _envelope_capture block) to this path after a run completes. "
            "Removes the dependency on a manual shell redirect."
        ),
    )
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
        exit_code = _cli_exit_code(summary)
        _emit(summary, json_mode=args.json)
        if args.stdout_envelope_out is not None:
            _persist_stdout_envelope(
                out_path=args.stdout_envelope_out,
                summary=summary,
                exit_code=exit_code,
                config=config,
                command_args=list(argv) if argv is not None else sys.argv[1:],
            )
        return exit_code
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
    """Exit 0 only on the shared clean-pass predicate (mechanical PASS + WRITTEN +
    fd closed + lock absent confirmed + no release reason + CLEAN cleanup)."""
    return 0 if is_clean_pass(summary) else 1


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

        # Stage 1 — config/env gate. Settings load, the enabled flag, credential-env
        # reads, and the symbol allow-list are all config-shaped failures. They carry
        # no secret value; the runtime maps the typed exception to the sanitized reason
        # ``source_config_gate_failed``. Fatal signals propagate untouched.
        try:
            settings = load_settings(config_path)
            ws = settings.broker.kis_ws_read_only
            if not ws.enabled:
                raise LiveSourceConfigGateError(
                    "broker.kis_ws_read_only.enabled must be true for --live-kis."
                )
            app_key = os.environ.get(ws.app_key_env)
            app_secret = os.environ.get(ws.app_secret_env)
            if not app_key or not app_secret:
                raise LiveSourceConfigGateError(
                    "KIS app key/secret env vars are required for --live-kis."
                )
            if config.symbol != PILOT_SYMBOL:
                raise LiveSourceConfigGateError("only symbol 005930 is allowed.")
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except LiveSourceConfigGateError:
            raise
        except Exception as exc:
            raise LiveSourceConfigGateError("live-source config gate failed.") from exc

        # Stage 2 — approval-key issuance. Any failure here (HTTP, auth, parse) is mapped
        # to the sanitized reason ``source_approval_failed``. The message carries no app
        # key/secret, approval key, or raw HTTP response. Fatal signals propagate.
        try:
            approval = KisWsApprovalProvider(
                transport=StdlibKisHttpTransport(),
                approval_base_url=ws.approval_base_url,
                app_key=app_key,
                app_secret=app_secret,
                timeout_seconds=ws.connect_timeout_seconds,
            ).issue_approval_key()
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise LiveSourceApprovalError("live-source approval issuance failed.") from exc

        # Stage 3 — websocket connect. ``open_kis_websocket`` is a coroutine, so connect
        # failures only surface at await time inside the consumer. The wrapper must be
        # ``async def`` so it can catch the await-time error and re-raise it as the typed
        # ``LiveSourceConnectError`` (mapped to ``source_connect_failed``). A sync lambda
        # returning the coroutine would let the raw error escape unclassified. The message
        # carries no raw frame or credentialed URL. Fatal signals propagate.
        async def _connect() -> object:
            try:
                return await open_kis_websocket(
                    ws.websocket_url, connect_timeout_seconds=ws.connect_timeout_seconds
                )
            except (MemoryError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                raise LiveSourceConnectError("live-source websocket connect failed.") from exc

        return KisWsMarketEventSource(
            connect=_connect,
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


# --- stdout-envelope capture (RTM-7c.12 evidence hardening) -----------------
#
# The persisted summary.json holds only the mechanical summary; the clean-exit
# clauses (summary_publication_outcome, cleanup_outcome, runtime_lock_*) live only
# in the run's stdout --json payload. A missing stdout-envelope.json is why an
# otherwise-clean run cannot be verified to PASS offline. Writing the envelope from
# the tool itself removes the dependency on a manual shell redirect.
#
# The envelope is the full stdout payload (clean-exit clauses stay at top level so
# validate/render --envelope read them unchanged) plus a reserved _envelope_capture
# block of collection metadata. It carries no secrets, no env values, no raw KIS
# frames, no URLs, no app keys/secrets, no approval keys, no account values, and no
# tracebacks — only structural paths, a sanitized argv, a timestamp, and the git sha.

STDOUT_ENVELOPE_SCHEMA_VERSION = "paper_day_stdout_envelope.v1"

_REDACTED = "<redacted>"
_SECRET_ARG_MARKERS = frozenset(
    {
        "secret",
        "token",
        "password",
        "passwd",
        "credential",
        "approval",
        "appkey",
        "app_key",
        "apikey",
        "api_key",
        "key",
    }
)


def _looks_secret(name: str) -> bool:
    norm = name.strip().lstrip("-").lower().replace("-", "_")
    return any(marker in norm for marker in _SECRET_ARG_MARKERS)


def _sanitize_command_args(argv: Sequence[str]) -> list[str]:
    """Echo the observed argv with secret-like values redacted. Never reads the
    environment; only redacts tokens whose name looks secret. Real CLI flags
    (paths, dates, symbol, durations) pass through unchanged."""
    out: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            out.append(_REDACTED)
            redact_next = False
            continue
        name, sep, _value = token.partition("=")
        if sep and _looks_secret(name):
            out.append(f"{name}={_REDACTED}")
            continue
        out.append(token)
        if token.startswith("-") and not sep and _looks_secret(token):
            # `--app-key VALUE` form: the value is the next token.
            redact_next = True
    return out


def _git_head() -> str | None:
    """Return the current git HEAD sha, or None if unavailable. Never raises, never
    returns stderr (which could carry a path); only a validated 40-char hex sha."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip()
    if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
        return head
    return None


def build_stdout_envelope(
    *,
    summary: Mapping[str, object],
    exit_code: int,
    summary_path: Path,
    evidence_path: Path,
    db_dir: Path,
    command_args: Sequence[str],
    captured_at: str,
    git_head: str | None,
) -> dict[str, object]:
    """Assemble the structured stdout envelope. Pure: clock, git, and argv are
    supplied by the caller so the result is fully deterministic and testable."""
    envelope: dict[str, object] = dict(summary)
    envelope["_envelope_capture"] = {
        "schema_version": STDOUT_ENVELOPE_SCHEMA_VERSION,
        "captured_at": captured_at,
        "exit_code": exit_code,
        "run_id": summary.get("run_id"),
        "summary_path": str(summary_path),
        "evidence_path": str(evidence_path),
        "db_dir": str(db_dir),
        "command_args": _sanitize_command_args(command_args),
        "git_head": git_head,
    }
    return envelope


def write_stdout_envelope(path: Path, envelope: Mapping[str, object]) -> None:
    """Write the envelope JSON atomically (temp + replace) so a partial write never
    leaves a half-formed envelope that the validator would treat as malformed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _persist_stdout_envelope(
    *,
    out_path: Path,
    summary: Mapping[str, object],
    exit_code: int,
    config: AttendedPaperDayConfig,
    command_args: Sequence[str],
) -> None:
    envelope = build_stdout_envelope(
        summary=summary,
        exit_code=exit_code,
        summary_path=config.summary_out,
        evidence_path=config.evidence_out,
        db_dir=config.db_dir,
        command_args=command_args,
        captured_at=datetime.now(tz=timezone.utc).isoformat(),
        git_head=_git_head(),
    )
    write_stdout_envelope(out_path, envelope)


if __name__ == "__main__":
    sys.exit(main())
