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
`writers_stopped_manual_confirmation_required=true`) always hold. Neither manual
gate is consumed or stored in this lane. See
`docs/PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md`.

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
**SQLite header bytes** (4-byte big-endian integer at offset 60) — captured from
the first 100 bytes of the file — **not** by opening a connection. Opening a
WAL-mode database, even read-only, can materialize a `-shm`/`-wal` sidecar;
reading header bytes never does. A non-SQLite artifact (the JSON snapshot), an
absent file, and a file too short or without the SQLite magic all yield
`user_version = null` (Python `None`) and an empty sidecar set.

**Absent-main / orphan-sidecar limit.** Sidecar suffixes are probed only when the
main artifact path exists. If the main file is absent, `fingerprint_artifact`
returns `present=false` and `sidecar_suffixes=[]` even when orphan
`-wal`/`-shm`/`-journal` files remain on disk (`sidecar_files` observes them
separately). Missing main already yields machine `NO_GO`; there is no false
activation PASS. Orphan-sidecar binding improvement is **OPEN** (RTM-7c.4f).

**Memory-bounded hashing.** The SHA-256 is computed by streaming the file in
fixed-size (1 MiB) chunks; the file is never loaded into memory in full, so peak
memory is bounded regardless of how large the ledger/journal/active-store grows.
`size` is the actual byte count read in that single sequential pass, so it cannot
disagree with the bytes that were hashed.

### Precheck-specific reasons

- `precheck_artifact_changed:<name>` — the before fingerprint differs from the
  after fingerprint for that artifact. Proves observable state changed during the
  precheck window → fail closed `NO_GO`, regardless of the inspection verdict.
  Does **not** by itself prove the precheck wrote (a concurrent writer or
  mutate-then-restore within the window can also differ); see net-equality limits
  below.
- `precheck_artifact_not_regular_file:<name>` — the artifact is present but not a
  regular file (directory/socket/fifo) and cannot be trusted read-only. Inspect's
  `open_read_only` catches this for the DBs it opens (`sqlite_not_a_file`), but
  not for the JSON snapshot, so the fingerprint covers all four uniformly.

### Single canonical reason per root cause (irregular artifact)

When a present-but-irregular artifact triggers `precheck_artifact_not_regular_file:<name>`,
that precheck reason **owns** the condition: the inspection layer's generic
unreadable/invalid reason for the *same* artifact (`<db>_unreadable:sqlite_not_a_file`
for a DB, `execution_inputs_invalid` for the snapshot) is **dropped from the
aggregate `reasons`** so a single root cause surfaces as a single reason. The raw
inspection reason is retained verbatim in `inspection.reasons` (the
`RuntimePrecheckResult.inspection` sub-object) for diagnostics. This matches the
missing / dangling-pointer / identity / plan-consistency single-reason precedents.
A *missing* artifact is different: precheck adds no `precheck_artifact_*` reason
for it, so no dedup applies and the inspection's `missing_database:<db>` /
`missing_execution_inputs_snapshot` passes through unchanged.

### `precheck_artifact_missing` is deliberately NOT emitted — drift avoidance

A missing artifact is reported **only** by the reused inspection layer
(`missing_database:<db>` for the three DBs, `missing_execution_inputs_snapshot`
for the snapshot). `precheck_runtime` does **not** re-report a missing artifact
under a `precheck_*` code. This follows the RTM-7c.4b P1-B dangling-dedup
precedent: one canonical reason per condition, owned by one layer, so verdicts do
not drift between two codes for the same fact. A precheck reason fires only for
conditions the inspect layer does **not** already own (post-hoc mutation;
present-but-irregular file).

## Credential / environment isolation (zero env read through config loading)

`credential_read=false` is a **constant** in the precheck summary, so the
config-loading path must actually read no environment variable or secret — the
constant must not be able to lie. The standard CLI loads settings with
`load_settings(args.config)` (`environ=None`), which resolves `${ENV}`
placeholders and runtime safety gates against `os.environ`; a config containing
`${KIS_LIVE_APP_KEY}` would therefore read the live secret during precheck. To
make the constant true, **precheck mode loads config with an explicitly empty
environ**: `load_settings(args.config, environ={})`. The conditional
`os.environ if environ is None else environ` short-circuits — with `environ={}`
(not `None`) the `os.environ` operand is never evaluated, so the process
environment is never touched.

Consequences, all fail-closed and all sanitized:

- A config with **any** `${...}` placeholder raises `ConfigEnvironmentError`
  (empty environ → unresolved) → CLI `except (SettingsError, OSError)` →
  `config error: ConfigEnvironmentError`. The placeholder name and any secret
  value never appear in output.
- A live-mode config that depends on env confirmation/credential gates fails the
  runtime safety gate (`RuntimeGateError`, `{}.get(...)=None`) → same sanitized
  `config error: <Type>`.
- A fully literal (no-`${...}`) seeded config loads and prechecks normally with
  **zero** environ access.

This is proven by real env-access spy tests (`tests/test_run_paper_fast_loop.py`)
that replace `config.settings.os` with a shim whose `.environ` raises on
`__getitem__` / `__contains__` / `get` / iteration / `keys` / `copy`. The spy
records **zero** access on a normal precheck and the two fail-closed configs
above never touch it before raising.

## Quiescence semantic limit (why writer-stop stays manual)

The read-only proof rests on SQLite **sidecar quiescence**: a quiescent DB (no
live `-wal`/`-shm`/`-journal`) can be read with `immutable=1`, creating no
sidecar. Before/after fingerprint **equality** proves no **net observable
fingerprint drift** across the precheck window — not that every read path
touched nothing at every instant, and not that concurrent writers were absent.
Sidecar absence proves only **momentary** quiescence at the instant of the
check. It does **not** prove that every writer process is stopped: there is no
process scan, no PID inspection, and no OS-level lock acquisition in scope. A
writer could be paused, or could start immediately after the check. Mutate-then-
restore within the window is also not detected (net fingerprints would still
match). Therefore writer-stop is a **machine-unverified manual requirement**
(`writers_stopped_manual_confirmation_required=true`), never inferred from a
machine `PASS` or from fingerprint equality alone.

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

### What net before/after equality does and does not prove

Byte-identical before/after fingerprints prove that, **across the precheck
window as a whole**, the four artifacts ended in the same observable state they
started in — sufficient to show the precheck's own reused inspection did not
leave a net mutation. It is a net, end-to-end check, not a continuous one, so it
deliberately does **not** claim:

- **Concurrent-writer absence.** Equality at two instants says nothing about
  whether some *other* process is writing; that is exactly why writer-stop stays
  a manual requirement (see quiescence limit above).
- **Mutate-then-restore detection.** If something wrote a byte and then restored
  the identical bytes (and any SQLite sidecars) entirely within the window, the
  net fingerprints would still match. The check detects *net* drift, not every
  transient intermediate state. This is acceptable because the precheck itself
  performs no writes and the manual writer-stop gate, not the fingerprint,
  carries the no-concurrent-writer guarantee.

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
  `fingerprints_before` / `fingerprints_after`, nested `precheck_receipt` (RTM-7c.4d;
  ephemeral stdout-only observation binding — see
  `docs/PAPER_FAST_LOOP_PRECHECK_RECEIPT_CONTRACT.md`), and the no-side-effect
  attestations `network_called` / `credential_read` / `broker_called` /
  `production_db_written` / `runtime_file_created`, all `false`.
- Internal failures are caught in `main()` and emitted as sanitized
  `outcome=FAIL` / `reason_code=precheck error: <ExceptionType>` with exit 1 —
  never a traceback or raw sqlite text.
