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
  calls. The probe returns as soon as both subscription ACKs are observed; it does
  not wait out the full `--duration-seconds` for a market event. The probe
  classifies the startup fully: both ACKs accepted -> `PASS/startup_only`; a
  rejected ACK -> `NO_GO/subscription_rejected`; the stream ending before
  readiness -> `NO_GO/transport_not_ready`; a consumer/source error ->
  `FAIL/source_failed`; the receive timeout without readiness ->
  `NO_GO/health_not_ready`. A source error is never downgraded to
  `health_not_ready`.

Summary outcome and CLI exit:

```text
clean PASS (is_clean_pass predicate) -> exit 0
NO_GO -> exit 1
FAIL  -> exit 1
PUBLISHED_INCOMPLETE / PUBLICATION_UNCERTAIN -> exit 1
any lock fd-close / identity / release failure -> exit 1
legacy ops/run_paper_fast_loop.py --run -> exit 2
```

Exit 0 requires **every** clause of the shared `is_clean_pass` predicate:
`outcome == PASS`, `summary_publication_outcome == WRITTEN`,
`runtime_lock_fd_closed == true`, `runtime_lock_absent_confirmed == true`,
`runtime_lock_release_reason_code is None`, and `cleanup_outcome == CLEAN`.
A failing clause downgrades the returned `outcome` to `FAIL` (the persisted
mechanical file is left untouched and may still read `PASS`).

Use a fresh explicit `--db-dir`, `--evidence-out`, and `--summary-out` per run.
The diagnostic runtime rejects output overlap, final symlink components (including
dangling symlinks), DB sidecars (`-wal`, `-shm`, `-journal`), existing non-empty
pilot DB directories without explicit reuse policy, and duplicate runtime locks.
When a duplicate runtime lock is detected the run is refused as `runtime_lock_exists`
before any DB is opened or any credential env var is read. Any admission refusal
(`invalid_input` or `runtime_lock_exists`) returns an in-memory result and writes
**zero** output files — no evidence, no summary, no symlink target. Output files
are written by the lock owner only. The summary is published create-new and
atomically (same-dir temp, fsync, hard-link, no overwrite, no symlink follow); a
publish failure yields `FAIL/summary_failed`/`summary_published_incomplete`/
`summary_publication_uncertain` as appropriate; operation/cleanup fatal writes no
summary file. No publisher exception can skip lock release — it runs exactly once
on every path. The persisted `summary.json` holds only the mechanical summary;
the returned envelope adds `persisted_summary` + cleanup/publication/lock keys
(never written to disk), and `persisted_summary` byte-equals the file **only for
`WRITTEN`** (else `null`). The runtime lock is always released as the last bounded
cleanup step (identity-safe: a replaced/foreign lock is never unlinked and is
reported `runtime_lock_identity_mismatch`); lock residue, fd-close failure,
identity mismatch, or uncertain release forbids PASS return, including under a
fatal. A consumer that ignores cancellation during a startup probe is bounded at
the **verdict level** (Option B) and reported `FAIL/source_close_timeout`; the
real `KisWsMarketEventSource` is cancellation-compliant, but a source that truly
refuses `CancelledError` leaves a pending task that only process isolation can
terminate — in-process bounding is guaranteed only for compliant sources. If a
startup probe reports `source_close_timeout`, treat the source as defective and do
not retry in-process.

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
