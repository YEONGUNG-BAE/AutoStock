# Attended Bounded Fast-Loop Runtime Precheck Contract (RTM-7c.4c)

Read-only operator precheck **only**. This lane defines a mechanical readiness
check and proves it is read-only; it does **not** activate a runtime, open KIS,
start any daemon/scheduler, read any credential, touch the network, or place any
order. `precheck_runtime` reuses the RTM-7c.4b `inspect_paper_fast_loop` body and
wraps it with before/after artifact fingerprints to prove the check mutated
nothing.

**Runtime activation: NO-GO.** Even a machine `PASS` is not permission to run.
The result always carries `activation_authorized=false` /
`runtime_activation_outcome="no_go"`, and both manual requirements
(`explicit_operator_approval_required=true`,
`writers_stopped_manual_confirmation_required=true`) always hold. Even when every
gate is green, the change is left for the Operator to commit.

## Four distinct gates — do not conflate

A live activation would require **all four** of the following, in this order.
This lane mechanically decides only the first.

1. **Machine-check outcome** (`MachineCheckOutcome`: `PASS` / `NO_GO`) — the only
   thing `precheck_runtime` decides. `PASS` iff the reused inspection is `OK`
   **and** every artifact fingerprint is byte-identical before and after the
   inspection. Mechanical, necessary, **not sufficient**.
2. **Manual writer-stop confirmation** (`writers_stopped_manual_confirmation_required`
   — always `true`). A machine-unverified human attestation that every writer
   process is actually stopped. See the quiescence semantic limit below.
3. **Explicit Operator approval** (`explicit_operator_approval_required` — always
   `true`). A separate human decision to proceed.
4. **Runtime activation authorization** (`activation_authorized` — always
   `false`; `runtime_activation_outcome` always `"no_go"`). Not granted anywhere
   in this lane.

`outcome` in the CLI/JSON summary reports the **machine verdict only**
(`PASS`/`NO_GO`). It is never a runtime go.

## Machine-check semantics

`precheck_runtime(*, settings, now, base_dir=".")` (`now` must be
timezone-aware; naive → `ValueError`):

1. Resolve the four artifact paths from settings.
2. Fingerprint all four artifacts (**before**).
3. Run `inspect_paper_fast_loop` (config + snapshot + DB readiness; **constructs
   no store**, no schema create/migrate, no reconcile).
4. Fingerprint all four artifacts (**after**).
5. Emit precheck-specific reasons (below), then
   `reasons = inspection.reasons + precheck_reasons`.
6. `machine_outcome = NO_GO` iff `inspection.outcome is NO_GO` **or** any precheck
   reason fired; else `PASS`.

### Artifacts (4)

| name | path attr | SQLite? |
|------|-----------|---------|
| `execution_inputs_snapshot` | `snapshot_path` | no (JSON) |
| `ledger` | `ledger_path` | yes |
| `trigger_journal` | `trigger_journal_path` | yes |
| `active_decision_store` | `active_decision_store_path` | yes |

### Artifact fingerprint (side-effect-free)

`present`, `is_regular_file`, `size`, byte SHA-256 of the main file, SQLite
`user_version`, and the sidecar-suffix set. The `user_version` is read from the
**SQLite header bytes** (4-byte big-endian integer at offset 60) by reading the
file's bytes — **not** by opening a connection. Opening a WAL-mode database, even
read-only, can materialize a `-shm`/`-wal` sidecar; reading header bytes never
does. A non-SQLite artifact (the JSON snapshot) has `user_version=0` and an empty
sidecar set.

### Precheck-specific reasons

- `precheck_artifact_changed:<name>` — the before fingerprint differs from the
  after fingerprint for that artifact. Proves the precheck (or anything it
  invoked) mutated operator state → fail closed `NO_GO`, regardless of the
  inspection verdict.
- `precheck_artifact_not_regular_file:<name>` — the artifact is present but not a
  regular file (directory/socket/fifo) and cannot be trusted read-only. Inspect's
  `open_read_only` catches this for the DBs it opens (`sqlite_not_a_file`), but
  not for the JSON snapshot, so the fingerprint covers all four uniformly.

### `precheck_artifact_missing` is deliberately NOT emitted — drift avoidance

A missing artifact is reported **only** by the reused inspection layer
(`missing_database:<db>` for the three DBs, `missing_execution_inputs_snapshot`
for the snapshot). `precheck_runtime` does **not** re-report a missing artifact
under a `precheck_*` code. This follows the RTM-7c.4b P1-B dangling-dedup
precedent: one canonical reason per condition, owned by one layer, so verdicts do
not drift between two codes for the same fact. A precheck reason fires only for
conditions the inspect layer does **not** already own (post-hoc mutation;
present-but-irregular file).

## Quiescence semantic limit (why writer-stop stays manual)

The read-only proof rests on SQLite **sidecar quiescence**: a quiescent DB (no
live `-wal`/`-shm`/`-journal`) can be read with `immutable=1`, creating no
sidecar, and a fingerprint that is byte-identical before/after proves the read
touched nothing. But sidecar absence proves only **momentary** quiescence at the
instant of the check. It does **not** prove that every writer process is stopped:
there is no process scan, no PID inspection, and no OS-level lock acquisition in
scope. A writer could be paused, or could start immediately after the check.
Therefore writer-stop is a **machine-unverified manual requirement**
(`writers_stopped_manual_confirmation_required=true`), never inferred from a
machine `PASS`.

## Read-only filesystem invariants (proven by tests)

- Fingerprints captured before and after the inspection are byte-identical on a
  PASS (`fingerprints_before == fingerprints_after`).
- Zero new sidecars are created for any of the three DBs.
- The JSON snapshot bytes are unchanged.
- No store (`ActiveDecisionStore` / `SQLiteLedger` / `SqliteTriggerJournal`) is
  constructed (constructor-spy asserts zero calls) — their `__init__` would
  create/migrate schema, i.e. a write.
- A simulated mutation during the inspection window is detected as
  `precheck_artifact_changed:<name>` and fails closed even when the inspection
  itself reports `OK`.

## Hard scope-out (this lane does none of these)

KIS websocket / DNS / HTTP / socket; credential or env read; market-event
consumption; monitor/supervisor start; broker call; order; operational DB write;
schema migration; journal reconcile; process scan/kill; lock acquisition;
daemon/launchd/systemd/cron; scheduler runtime wiring; multi-symbol; production
calendar; threshold calibration; throughput/latency tuning; unattended pilot;
persistent activation-epoch restore.

## CLI contract (`ops/run_paper_fast_loop.py --precheck-runtime`)

- Mutually exclusive with `--validate-only` / `--inspect-existing` / `--replay` /
  `--run`; a collision → exit 1, `reason_code` containing `mutually exclusive`.
- Exit `0` iff machine `PASS`, else `1` (`NO_GO`).
- JSON summary fields: `outcome` (`PASS`/`NO_GO`), `mode`,
  `machine_check_outcome` (`pass`/`no_go`), `activation_authorized` (`false`),
  `runtime_activation_outcome` (`"no_go"`), `explicit_operator_approval_required`
  (`true`), `writers_stopped_manual_confirmation_required` (`true`), `reasons`,
  `inspection_outcome`, `inspection_reasons`, `missing_databases`,
  `fingerprints_before` / `fingerprints_after`, and the no-side-effect
  attestations `network_called` / `credential_read` / `broker_called` /
  `production_db_written` / `runtime_file_created`, all `false`.
- Internal failures are caught in `main()` and emitted as sanitized
  `outcome=FAIL` / `reason_code=precheck error: <ExceptionType>` with exit 1 —
  never a traceback or raw sqlite text.
