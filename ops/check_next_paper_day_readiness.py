#!/usr/bin/env python3
"""Offline, read-only next-session paper-day readiness checker.

Confirms the local repo + Operator-selected run variables are in a sane shape
*before* a future attended paper-day run, without ever touching the network, live
KIS, startup smoke, or an attended pilot. It is strictly offline and read-only:

- never opens a network connection or imports a live KIS source/client path,
- never reads or prints ``config/config.toml`` contents or any secret value,
- only inspects environment variable *metadata* (presence / length /
  strip-cleanliness / placeholder status) — never the value itself,
- never mutates, creates, or deletes any file.

It shells out to ``git`` only for read-only queries (``rev-parse``,
``status --short``, ``ls-files``) and returns stdout only (never stderr, which
could carry a filesystem path). The verdict is advisory; the authoritative live
run sheet is ``docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

# Required KIS environment variables. We inspect only their metadata, never values.
_REQUIRED_ENV = (
    "KIS_LIVE_APP_KEY",
    "KIS_LIVE_APP_SECRET",
    "KIS_LIVE_ACCOUNT",
    "KIS_WS_READONLY_CONFIRM",
)

# Values that mean "not really set". Matched against the env value to flag a
# leftover template; the value itself is never printed.
_PLACEHOLDERS = frozenset({"", "...", "YOUR_KEY", "YOUR_SECRET", "PLACEHOLDER"})

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._-]+$")
_DEFAULT_CONFIG = "config/config.toml"
_GITIGNORED_CONFIG = "config/config.toml"

_RUN_DIR_ARTIFACTS = ("summary.json", "evidence.jsonl", "stdout-envelope.json", "db")

_OPERATOR_REMINDER = (
    "Operator must confirm at run time: a regular KR market session with "
    "session_state=OPEN. This offline checker cannot and does not verify live "
    "session state."
)


def _run_git(args: list[str]) -> tuple[int, str]:
    """Run a read-only git query. Returns (returncode, stdout). Never raises and
    never returns stderr (which could carry a path)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout


def _check(name: str, status: str, detail: str, *, hard: bool) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "hard": hard}


def _env_metadata(value: str | None) -> dict[str, Any]:
    """Compute non-secret metadata for one env var. The value never leaves here."""
    present = value is not None and value != ""
    length = len(value) if value is not None else 0
    strip_same = value == value.strip() if value is not None else False
    # An empty/template value (including unset) counts as a placeholder hit.
    placeholder = (value if value is not None else "") in _PLACEHOLDERS
    return {
        "present": present,
        "length": length,
        "strip_same": strip_same,
        "placeholder": placeholder,
    }


def evaluate_readiness(
    *,
    session_date: str,
    run_label: str,
    duration_seconds: str,
    run_dir: str,
    config_path: str,
    environ: dict[str, str],
) -> dict[str, Any]:
    """Pure-ish evaluation: git queries via _run_git (monkeypatchable), filesystem
    existence via pathlib, env metadata via the supplied mapping. No network, no
    secret values, no file mutation."""
    checks: list[dict[str, Any]] = []
    env_report: list[dict[str, Any]] = []

    # --- repo HEAD readable -------------------------------------------------
    rc, out = _run_git(["rev-parse", "HEAD"])
    head = out.strip()
    head_ok = rc == 0 and len(head) == 40 and all(c in "0123456789abcdef" for c in head)
    checks.append(
        _check(
            "repo_head_readable",
            "ok" if head_ok else "fail",
            f"HEAD={head[:12]}" if head_ok else "could not read a valid git HEAD",
            hard=True,
        )
    )

    # --- git status --short clean ------------------------------------------
    rc, out = _run_git(["status", "--short"])
    status_lines = [ln for ln in out.splitlines() if ln.strip()]
    if rc != 0:
        checks.append(_check("git_status_clean", "fail", "git status failed", hard=True))
    elif status_lines:
        # Report the porcelain status lines (path + status code only — no file
        # contents, no secrets). config/config.toml is gitignored and will not appear.
        checks.append(
            _check(
                "git_status_clean",
                "fail",
                f"{len(status_lines)} dirty entries: " + "; ".join(status_lines),
                hard=True,
            )
        )
    else:
        checks.append(_check("git_status_clean", "ok", "working tree clean", hard=True))

    # --- git ls-files runtime empty ----------------------------------------
    rc, out = _run_git(["ls-files", "runtime"])
    runtime_files = [ln for ln in out.splitlines() if ln.strip()]
    if rc != 0:
        checks.append(_check("runtime_untracked", "fail", "git ls-files failed", hard=True))
    elif runtime_files:
        checks.append(
            _check(
                "runtime_untracked",
                "fail",
                f"{len(runtime_files)} tracked runtime files: " + "; ".join(runtime_files),
                hard=True,
            )
        )
    else:
        checks.append(_check("runtime_untracked", "ok", "no tracked runtime files", hard=True))

    # --- config path exists (contents never read) --------------------------
    config_file = Path(config_path)
    if config_file.is_file():
        checks.append(_check("config_exists", "ok", f"{config_path} present", hard=True))
    else:
        checks.append(
            _check("config_exists", "fail", f"{config_path} not found", hard=True)
        )

    # --- config/config.toml must stay untracked/gitignored (if applicable) --
    normalized_config = str(Path(config_path)).replace("\\", "/")
    if normalized_config == _GITIGNORED_CONFIG:
        rc, out = _run_git(["ls-files", "--", _GITIGNORED_CONFIG])
        tracked = bool([ln for ln in out.splitlines() if ln.strip()])
        if rc != 0:
            checks.append(
                _check("config_untracked", "warn", "could not query git tracking", hard=False)
            )
        elif tracked:
            checks.append(
                _check(
                    "config_untracked",
                    "fail",
                    f"{_GITIGNORED_CONFIG} is tracked by git; it must stay gitignored",
                    hard=True,
                )
            )
        else:
            checks.append(
                _check("config_untracked", "ok", f"{_GITIGNORED_CONFIG} not tracked", hard=True)
            )
    else:
        checks.append(
            _check(
                "config_untracked",
                "info",
                f"{config_path} is not the gitignored secret config; tracking check skipped",
                hard=False,
            )
        )

    # --- required env vars (metadata only) ---------------------------------
    for name in _REQUIRED_ENV:
        meta = _env_metadata(environ.get(name))
        env_report.append({"name": name, **meta})
        good = meta["present"] and meta["strip_same"] and not meta["placeholder"]
        if good:
            status, detail = "ok", f"present len={meta['length']}"
        elif not meta["present"]:
            status, detail = "fail", "missing"
        elif meta["placeholder"]:
            status, detail = "fail", "placeholder value"
        else:
            status, detail = "fail", "leading/trailing whitespace (strip_same=false)"
        checks.append(_check(f"env:{name}", status, detail, hard=True))

    # --- SESSION_DATE parses YYYY-MM-DD ------------------------------------
    try:
        datetime.strptime(session_date, "%Y-%m-%d")
        checks.append(_check("session_date_valid", "ok", session_date, hard=True))
    except ValueError:
        checks.append(
            _check("session_date_valid", "fail", "not a YYYY-MM-DD date", hard=True)
        )

    # --- RUN_LABEL safe path component -------------------------------------
    label_ok = (
        bool(run_label)
        and run_label not in (".", "..")
        and _SAFE_LABEL.match(run_label) is not None
    )
    checks.append(
        _check(
            "run_label_valid",
            "ok" if label_ok else "fail",
            run_label if label_ok else "empty or unsafe path component",
            hard=True,
        )
    )

    # --- DURATION_SECONDS positive integer ---------------------------------
    duration_ok = bool(re.fullmatch(r"[0-9]+", duration_seconds)) and int(duration_seconds) > 0
    checks.append(
        _check(
            "duration_valid",
            "ok" if duration_ok else "fail",
            duration_seconds if duration_ok else "not a positive integer",
            hard=True,
        )
    )

    # --- RUN_DIR equals runtime/paper-day/$SESSION_DATE/$RUN_LABEL ----------
    expected_run_dir = f"runtime/paper-day/{session_date}/{run_label}"
    actual_norm = str(Path(run_dir)).replace("\\", "/")
    expected_norm = str(Path(expected_run_dir)).replace("\\", "/")
    if actual_norm == expected_norm:
        checks.append(_check("run_dir_matches", "ok", expected_run_dir, hard=True))
    else:
        checks.append(
            _check(
                "run_dir_matches",
                "fail",
                f"expected {expected_run_dir}",
                hard=True,
            )
        )

    # --- RUN_DIR must not already hold run artifacts -----------------------
    run_dir_path = Path(run_dir)
    stale: list[str] = []
    if run_dir_path.exists():
        for artifact in _RUN_DIR_ARTIFACTS:
            if (run_dir_path / artifact).exists():
                stale.append(artifact)
    if stale:
        checks.append(
            _check(
                "run_dir_no_stale_artifacts",
                "fail",
                "RUN_DIR already contains: " + ", ".join(stale),
                hard=True,
            )
        )
    else:
        checks.append(
            _check("run_dir_no_stale_artifacts", "ok", "no pre-existing run artifacts", hard=True)
        )

    hard_failures = [c["name"] for c in checks if c["hard"] and c["status"] == "fail"]
    ok = not hard_failures

    return {
        "ok": ok,
        "hard_failures": hard_failures,
        "checks": checks,
        "env": env_report,
        "reminders": [_OPERATOR_REMINDER],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline, read-only next-session paper-day readiness checker.",
    )
    parser.add_argument("--session-date", required=True, help="SESSION_DATE (YYYY-MM-DD)")
    parser.add_argument("--run-label", required=True, help="RUN_LABEL (safe path component)")
    parser.add_argument("--duration-seconds", required=True, help="DURATION_SECONDS (positive int)")
    parser.add_argument(
        "--run-dir",
        required=True,
        help="RUN_DIR (must equal runtime/paper-day/$SESSION_DATE/$RUN_LABEL)",
    )
    parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help="config path (existence only; contents never read; default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _emit(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False), file=out)
        return
    print(f"readiness: {'READY' if payload['ok'] else 'NOT_READY'}", file=out)
    print("", file=out)
    print("env vars (metadata only — values never printed):", file=out)
    for entry in payload["env"]:
        print(
            f"  {entry['name']}: present={entry['present']} length={entry['length']} "
            f"strip_same={entry['strip_same']} placeholder={entry['placeholder']}",
            file=out,
        )
    print("", file=out)
    print("checks:", file=out)
    for chk in payload["checks"]:
        print(f"  [{chk['status']}] {chk['name']}: {chk['detail']}", file=out)
    if payload["hard_failures"]:
        print("", file=out)
        print("hard failures: " + ", ".join(payload["hard_failures"]), file=out)
    print("", file=out)
    for reminder in payload["reminders"]:
        print(f"REMINDER: {reminder}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    import os

    payload = evaluate_readiness(
        session_date=args.session_date,
        run_label=args.run_label,
        duration_seconds=args.duration_seconds,
        run_dir=args.run_dir,
        config_path=args.config,
        environ=dict(os.environ),
    )
    _emit(payload, as_json=args.json, out=sys.stdout)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
