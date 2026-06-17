# Paper-Day Operator Runbook

Diagnostic mode is attended and bounded. It is not activation.

Validate only:

```bash
PYTHONPATH=src uv run python ops/run_attended_paper_day.py \
  --config config/config.toml.example \
  --session-date YYYY-MM-DD \
  --symbol 005930 \
  --duration-seconds 60 \
  --evidence-out runtime/paper-day/YYYY-MM-DD/evidence.jsonl \
  --summary-out runtime/paper-day/YYYY-MM-DD/summary.json \
  --db-dir runtime/paper-day/YYYY-MM-DD/db \
  --confirm-attended-paper \
  --validate-only \
  --json
```

Offline fixture check:

```bash
PYTHONPATH=src uv run python ops/run_attended_paper_day.py \
  --config config/config.toml.example \
  --session-date YYYY-MM-DD \
  --symbol 005930 \
  --duration-seconds 60 \
  --evidence-out runtime/paper-day/YYYY-MM-DD/evidence.jsonl \
  --summary-out runtime/paper-day/YYYY-MM-DD/summary.json \
  --db-dir runtime/paper-day/YYYY-MM-DD/db \
  --confirm-attended-paper \
  --offline-fixture deterministic \
  --json
```

Operator-only KIS startup/run path uses `--live-kis` and requires KIS websocket
read-only config plus app key/secret environment variables. Cursor tests do not
execute this path.

Startup-only modes:

- `--offline-fixture deterministic --startup-only`: validates, acquires the
  single-process lock, opens/closes pilot DB resources, and writes a post-close
  summary. It does not claim transport connected or subscription ACK readiness.
- `--live-kis --startup-only`: additionally obtains KIS approval, connects the
  websocket, waits for trade and quote subscription ACKs, closes the source, and
  writes the post-close summary. It must not execute paper decisions or broker
  calls. The probe returns as soon as both subscription ACKs are observed (or the
  receive timeout elapses, yielding `health_not_ready`); it does not wait out the
  full `--duration-seconds` for a market event.

Summary outcome and CLI exit:

```text
PASS -> exit 0
NO_GO -> exit 1
FAIL -> exit 1
legacy ops/run_paper_fast_loop.py --run -> exit 2
```

Use a fresh explicit `--db-dir`, `--evidence-out`, and `--summary-out` per run.
The diagnostic runtime rejects output overlap, final symlink components (including
dangling symlinks), DB sidecars (`-wal`, `-shm`, `-journal`), existing non-empty
pilot DB directories without explicit reuse policy, and duplicate runtime locks.
When a duplicate runtime lock is detected the run is refused as `runtime_lock_exists`
before any DB is opened or any credential env var is read.

Immediate stop conditions:

```text
real-order adapter constructed
credential/raw frame leak
unexpected network route
journal uncertain
reconcile required
nonterminal journal stuck
ledger invariant failure
evidence write failure
resource close failure
activation_authorized=true
```

After the day, review `summary.json` first, then the earliest evidence record
whose stage/reason explains the first failure.

The actual live KIS startup/run and the 1-day pilot have **not** been performed.
A 1-day pilot remains **NO-GO** until Reviewer PASS; Cursor/test work is limited
to validate-only, offline fixtures, and lifecycle-aware fakes.
