# Paper-Day Mobile Remote Operator Packet

## Scope

This is a phone-friendly paste packet for a human Operator.
It does not authorize unattended execution.
It does not authorize live orders.
It does not authorize runtime activation.
It does not change runtime behavior.

The authoritative criteria and runtime freeze remain in
`docs/PAPER_DAY_MONDAY_EXECUTION_CHECKLIST.md`.

## Assumptions

- Operator has lawful/approved remote access to their own machine.
- Operator can keep the phone session open and observe the terminal.
- Operator prepared environment variables before market session.
- Operator will not paste or display secret values.
- Operator will not leave the command running unattended.
- Full pilot timing is regular-session only: `session_state=OPEN`.
  `PRE_OPEN`, `POST_CLOSE`, `CLOSED`, or `UNKNOWN` means invalid timing and
  must be treated as `NO_GO/invalid_session_window`.

Use only an approved remote-control method. Do not bypass workplace policy.

## Prohibitions

This packet is for the human Operator. Cursor/Claude/Codex must not execute the
market-session command, actual KIS network, an actual startup smoke retry, an
actual 1-day pilot, a live order, or a paper diagnostic live run. It does not
authorize a daemon, automatic restart, runtime activation, credential value
logging, raw HTTP response logging, raw websocket frame logging, secret
persistence, or runtime behavior changes.

The runtime hot-path freeze covers:

```text
src/composition/attended_paper_day.py
ops/run_attended_paper_day.py
src/data/kis_ws_source.py
src/data/kis_ws_auth.py
src/broker/kis_transport.py
```

## Phone/AnyDesk operating rules

- Use landscape mode if possible.
- Disable phone auto-lock for the 30-minute diagnostic window.
- Keep the terminal visible.
- Do not switch away during the run unless necessary.
- Do not edit long commands on the phone.
- Paste whole blocks only.
- Do not save secrets in phone notes.
- Do not paste credential values into chat, docs, logs, or issue comments.
- If remote session disconnects during the run, do not assume PASS; treat as NEEDS_REVIEW until artifacts are validated.

## Variables to confirm before pasting

Only these three values are intended for confirmation:

```bash
SESSION_DATE="2026-06-23"
RUN_LABEL="day-1"
DURATION_SECONDS="1800"
```

Do not edit the long command body from the phone.
Only confirm SESSION_DATE, RUN_LABEL, and DURATION_SECONDS before the session.

## Paste block 1 — preflight

Paste this as one block:

```bash
SESSION_DATE="2026-06-23"
RUN_LABEL="day-1"
DURATION_SECONDS="1800"
RUN_DIR="runtime/paper-day/${SESSION_DATE}/${RUN_LABEL}"

echo "SESSION_DATE=$SESSION_DATE"
echo "RUN_LABEL=$RUN_LABEL"
echo "DURATION_SECONDS=$DURATION_SECONDS"
echo "RUN_DIR=$RUN_DIR"

git rev-parse HEAD
git status --short
git ls-files runtime

PYTHONPATH=src uv run python - <<'PY'
import os
for k in ["KIS_LIVE_APP_KEY", "KIS_LIVE_APP_SECRET", "KIS_LIVE_ACCOUNT", "KIS_WS_READONLY_CONFIRM"]:
    v = os.environ.get(k, "")
    print(k, "len=", len(v), "strip_same=", v == v.strip(), "placeholder=", v in {"...", "YOUR_KEY", "YOUR_SECRET", "PLACEHOLDER"})
print("confirm_ok=", os.environ.get("KIS_WS_READONLY_CONFIRM") == "ENABLE_KIS_WS_READONLY")
PY

PYTHONPATH=src uv run python - <<'PY'
from config.settings import load_settings
ws = load_settings("config/config.toml").broker.kis_ws_read_only
print("enabled=", ws.enabled)
print("approval_base_url_set=", bool(ws.approval_base_url))
print("websocket_url_set=", bool(ws.websocket_url))
PY
```

Do not print secret values.
Do not proceed if git status is unexpected.
Do not proceed if git ls-files runtime is non-empty.
Do not proceed if confirm_ok is false.
Do not proceed if strip_same is false.

## Paste block 2 — market-session run

Operator must stay present while this runs.
Do not pipe through tee.
Do not use PIPESTATUS.
Do not close the terminal.
Do not let the phone disconnect intentionally.

Paste this as one block during the approved regular market session only:

```bash
SESSION_DATE="2026-06-23"
RUN_LABEL="day-1"
DURATION_SECONDS="1800"
RUN_DIR="runtime/paper-day/${SESSION_DATE}/${RUN_LABEL}"
mkdir -p "$RUN_DIR"

PYTHONPATH=src uv run python ops/run_attended_paper_day.py \
  --config config/config.toml \
  --session-date "$SESSION_DATE" \
  --symbol 005930 \
  --duration-seconds "$DURATION_SECONDS" \
  --evidence-out "$RUN_DIR/evidence.jsonl" \
  --summary-out "$RUN_DIR/summary.json" \
  --db-dir "$RUN_DIR/db" \
  --confirm-attended-paper \
  --live-kis \
  --json > "$RUN_DIR/stdout-envelope.json"

PILOT_EXIT=$?
cat "$RUN_DIR/stdout-envelope.json"
echo "PILOT_EXIT=$PILOT_EXIT"
```

## Paste block 3 — validation and report

Paste this as one block after the run exits:

```bash
SESSION_DATE="2026-06-23"
RUN_LABEL="day-1"
RUN_DIR="runtime/paper-day/${SESSION_DATE}/${RUN_LABEL}"

PYTHONPATH=src uv run python ops/validate_paper_day_summary.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --json

VALIDATOR_EXIT=$?
echo "VALIDATOR_EXIT=$VALIDATOR_EXIT"

PYTHONPATH=src uv run python ops/render_paper_day_report.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --out "$RUN_DIR/review-report.md"

REPORT_EXIT=$?
echo "REPORT_EXIT=$REPORT_EXIT"
cat "$RUN_DIR/review-report.md"
```

## Paste block 4 — handoff bundle

Before pasting this block, review the validator/report result. Do not paste raw
`evidence.jsonl` into chat if it contains `sensitive_data_present=true`. If
`sensitive_data_present=true` appears, stop and escalate; preserve artifacts and
do not print contents further.

```bash
SESSION_DATE="2026-06-23"
RUN_LABEL="day-1"
RUN_DIR="runtime/paper-day/${SESSION_DATE}/${RUN_LABEL}"

echo "HEAD=$(git rev-parse HEAD)"
echo "RUN_DIR=$RUN_DIR"
git status --short
git ls-files runtime
find "$RUN_DIR" -maxdepth 2 -type f | sort

echo "---- summary.json ----"
cat "$RUN_DIR/summary.json"

echo "---- stdout-envelope.json ----"
cat "$RUN_DIR/stdout-envelope.json"

echo "---- review-report.md ----"
cat "$RUN_DIR/review-report.md"
```

## Abort conditions

| condition | action |
| --- | --- |
| git status unexpected | stop |
| git ls-files runtime non-empty | stop |
| confirm_ok=false | stop |
| strip_same=false | stop and re-export env |
| enabled=false | stop |
| websocket_url_set=false | stop |
| approval_base_url_set=false | stop |
| remote session unstable before run | delay run |
| remote session disconnects during run | treat as NEEDS_REVIEW until artifacts validated |
| PILOT_EXIT non-zero | run validator/report, then triage |
| session_state is not OPEN | stop; invalid full-pilot timing |
| sensitive_data_present=true | stop/escalate, do not paste artifact contents |

## What not to do from the phone

Do not edit Python files.
Do not edit config values.
Do not change strategy.
Do not retry blindly.
Do not run startup-only retry.
Do not run all-day unattended.
Do not use nohup, daemon, background job, tmux detach, screen detach, cron,
launchd, or systemd.
Do not save KIS secrets in phone notes.
Do not paste KIS secrets into chat.

## Safety proof

```text
actual KIS network run by Cursor 0
actual 1-day pilot run by Cursor 0
live order 0
paper diagnostic live run 0
runtime hot-path file modified 0
activation_authorized false
automatic_restart false
secret persisted 0
raw HTTP response persisted 0
raw websocket frame persisted 0
traceback persisted 0
tracked runtime 0
```

Do not commit. The Operator commits after review. The 1-day pilot remains NO-GO
until a Reviewer PASS.
