# Paper-Day Operator Runbook

Diagnostic mode is attended and bounded. It is not activation.

For a one-page go/no-go status summary before the session, read
`docs/PAPER_DAY_CURRENT_STATUS.md` first.

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
  classifies the startup fully:

  ```text
  both ACKs accepted               -> PASS/startup_only
  rejected ACK                     -> NO_GO/subscription_rejected
  stream ending before readiness   -> NO_GO/transport_not_ready
  receive timeout without readiness-> NO_GO/health_not_ready
  config/env gate failure          -> FAIL/source_config_gate_failed
  KIS approval key issuance failure-> FAIL/source_approval_failed
  websocket open/connect failure   -> FAIL/source_connect_failed
  unclassified source/factory/consumer error -> FAIL/source_failed
  source close timeout             -> FAIL/source_close_timeout
  ```

  A source error is never downgraded to `health_not_ready`. The three live-source
  startup subreasons (`source_config_gate_failed`, `source_approval_failed`,
  `source_connect_failed`) are sanitized: they persist **no** secret, approval key,
  raw HTTP response, raw websocket frame, traceback, or credentialed URL — only the
  stable reason string reaches the summary `stop_reason` and the evidence
  `failed_closed.reason_code`. `source_failed` is now a fallback only, used for an
  unclassified factory/consumer error (i.e. a bug or unexpected source error).
  `MemoryError` / `KeyboardInterrupt` / `SystemExit` are never converted into any of
  these source reasons; fatal identity is preserved.

  Operator action per subreason (never print secret values, raw HTTP responses, or
  raw frames):

  ```text
  source_config_gate_failed:
    - confirm config/config.toml broker.kis_ws_read_only.enabled = true
    - confirm KIS_LIVE_APP_KEY / KIS_LIVE_APP_SECRET env vars are present
    - confirm symbol is 005930
    - confirm config path / approval_base_url / websocket_url are set
    - do not print secret values

  source_approval_failed:
    - check live vs. mock app-key / domain mismatch
    - check approval_base_url
    - check app key/secret permissions
    - do not print raw HTTP response or secret
    - do NOT immediately retry with the same settings

  source_connect_failed:
    - check websocket_url
    - check DNS / TLS / network / firewall
    - treat as a failure reached before the subscription stage
    - do not print raw frames
    - do NOT immediately retry with the same settings

  source_failed:
    - fallback only
    - an unexpected source error not classified into a subreason (likely a bug)
    - inspect evidence/summary and review code
  ```

  After `source_approval_failed` or `source_connect_failed`, a live retry must not
  be repeated immediately: separate the config / domain / account / network cause
  first, then retry. The 1-day pilot remains NO-GO until a KIS startup-only smoke
  reaches `PASS/startup_only`.

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
`summary_publication_uncertain` as appropriate; operation/cleanup fatal before
publish writes no summary file. `_publish_summary_create_new` returns
`SummaryPublishResult` (outcome + optional `fatal`); fatal propagation does not
erase confirmed publication state — link landed + fatal cleanup/sync ⇒
`PUBLISHED_INCOMPLETE` or `PUBLICATION_UNCERTAIN`, never a false `NOT_WRITTEN`.
Lock release runs exactly once on every path. The persisted `summary.json` holds only the mechanical summary;
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

Known post-run classification note: the 2026-06-29
`internal_runtime_error`/`MonitorExhaustedError` gap is documented in
`docs/PAPER_DAY_INTERNAL_RUNTIME_ERROR_CLASSIFICATION.md`. Future monitor
exhaustion is normalized distinctly, but that historical run remains formal FAIL,
is not a PASS conversion, and does not authorize full paper or a live rerun by
itself.

Quote-contamination troubleshooting: if KIS approval fails with
`source_approval_failed` while the env vars are present, check for copied quote
characters around `APP_KEY` / `APP_SECRET`. Use length and `strip_same` — never
print the value. In the observed startup-3 failure the `APP_KEY` / `APP_SECRET`
lengths were `38` / `182`; after re-exporting from the KIS portal with plain
shell quotes they were `36` / `180` and startup-4 passed.

Offline summary/evidence validator: after a run (or to inspect any prior run's
artifacts), classify `summary.json` + `evidence.jsonl` with the offline,
network-free, secret-free, read-only helper:

```bash
PYTHONPATH=src uv run python ops/validate_paper_day_summary.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --json
```

It emits `PASS` / `NO_GO` / `FAIL` / `NEEDS_REVIEW`. Because the persisted
`summary.json` holds only the mechanical summary, the cleanup/publication/lock
clauses must come from the captured stdout envelope (`--envelope`). Without it,
the validator reports `missing_from_persisted_summary` and returns
`NEEDS_REVIEW` rather than inventing those fields. The validator never infers or
repairs envelope-only fields. If the envelope file is missing, empty, or malformed,
it returns `NEEDS_REVIEW`/`FAIL` per its existing rules.

The envelope must belong to the **same run** as the summary/evidence. The
validator runs an envelope identity check, cross-checking the envelope's `run_id`,
`session_date`, `symbol`, and reserved `_envelope_capture.run_id` against the
persisted summary. A wrong-run or mismatched envelope — copied, reused, or
hand-edited from a different run, `RUN_DIR`, symbol, or date — is blocked as
`envelope_run_mismatch` and returns `NEEDS_REVIEW`; it can never be PASS. The
verdict is advisory; the authoritative PASS/NO_GO/FAIL criteria live in the
operator packet.

Envelope capture: pass `--stdout-envelope-out "$RUN_DIR/stdout-envelope.json"` so
the tool itself persists the envelope (the full `--json` payload plus a sanitized
`_envelope_capture` block: exit code, run_id, summary/evidence/db paths, sanitized
command args, captured_at, git HEAD). This is the primary, redirect-independent
capture and removes the dependency on a manual shell redirect that can be
forgotten (the 2026-06-26 pilot-3 gap). The builder reads no environment and
redacts secret-like argv tokens (`KEY=<redacted>`, `--secret-flag <redacted>`), so
no secret value, env value, URL, app key/secret, approval key, account, raw frame,
or traceback reaches the envelope.

Keep a belt-and-suspenders console capture with plain stdout redirection
(`--json > "$RUN_DIR/stdout-envelope.shell.json"`), then read `PILOT_EXIT=$?`. Do
not pipe through `tee` unless the shell and pipe-status handling are explicitly
verified: a pipeline's `$?` reflects `tee`, and the bash-only `${PIPESTATUS[0]}`
is not safe under macOS's default zsh. Redirection makes `$?` capture the Python
process exit code in both bash and zsh. For the full run command, use the current
reusable next-session packet `docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md`; the
2026-06-22 `docs/PAPER_DAY_MONDAY_OPERATOR_PACKET.md` is retained only as
historical safety/runbook reference.

Result collection existence gate: before validating, confirm all four artifacts
exist and the tree is clean of tracked runtime —
`test -f "$RUN_DIR/summary.json"`, `test -f "$RUN_DIR/evidence.jsonl"`,
`test -f "$RUN_DIR/stdout-envelope.json"`, `test -d "$RUN_DIR/db"`,
`test -z "$(git status --short)"`, and `test -z "$(git ls-files runtime)"`. A
missing envelope yields `NEEDS_REVIEW`; do not hand-edit the envelope to backfill
the clean-exit clauses — re-capture from a fresh run.

KIS startup-only readiness is **PASS** (`runtime/paper-day/2026-06-18/startup-4`:
`PASS/startup_only/kis_live`). The 1-day attended paper diagnostic **has been
performed**: pilot-3 on 2026-06-26 reached a clean operator-observed terminal
**PASS**, and the H0STASP0 62-field live quote parser fix is **verified** by the
on-disk evidence (46,865 live quote frames normalized 1:1, zero parse failures).
The **formal reproducible verdict for pilot-3 remains `NEEDS_REVIEW`** because
`stdout-envelope.json` was not captured that day, so the five clean-exit clauses
cannot be confirmed from disk (see `docs/PAPER_DAY_PILOT_EVIDENCE_LOG.md`).

That capture gap is now closed at the tooling level: the CLI persists the
tool-written envelope on both PASS and FAIL paths via `--stdout-envelope-out`
(hardening commit `7d70ba0`, locally verified). **Any future live run is for
envelope/runbook validation only — not for parser verification, which is already
complete.** A future run still requires a fresh, Operator-selected
session/date/duration during a regular KR market session: use the current reusable
next-session packet `docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md` (the 2026-06-22 Monday
run sheet is **historical**). Cursor/test work remains limited to validate-only,
offline fixtures, and lifecycle-aware fakes.
