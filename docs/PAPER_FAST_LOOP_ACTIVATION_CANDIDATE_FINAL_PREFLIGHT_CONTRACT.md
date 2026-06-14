# Activation Candidate Final Preflight Contract (RTM-7c.4h, +7c.4i receipt time, +7c.4j snapshot)

Read-only **time-aware final preflight** for the paper fast-loop activation
candidate. Composes the RTM-7c.4g byte-state revalidation with a **fresh,
caller-supplied-time** machine precheck so that — even when every artifact byte is
**identical** to the candidate receipt's post-inspection state — an execution-inputs
snapshot or active decision whose validity window has since **opened or closed**
(or any other current-time precheck NO_GO) is caught. RTM-7c.4i interposes a
**policy-neutral receipt time observation** between the 4g revalidation and the fresh
precheck: it records the exact receipt age and **fail-closes a future receipt**
(`checked_at` after the caller `now`) before any fresh precheck runs.

**Runtime activation: NO-GO.** Mechanical preflight PASS is **not** Operator
approval, writer-stop proof, receipt authenticity, **receipt-age/TTL/max-age policy**,
freshness evaluation, or activation authorization. Computing `receipt_age_microseconds`
is an **observation**, not a threshold verdict.

Code: `composition.activation_candidate_final_preflight.final_preflight_activation_candidate`
CLI: `ops/run_paper_fast_loop.py --final-preflight-activation-candidate`

## What this lane evaluates

> **Current-state time-validity + receipt age observation.** On top of a 4g byte-state
> PASS, this lane (a) observes the receipt's age and fail-closes a future `checked_at`
> (RTM-7c.4i), then (b) runs a fresh precheck at the caller-supplied `now` that re-checks
> the snapshot / active-decision validity windows against that `now`.

`now` is supplied by the **caller** and must be timezone-aware. The module reads no
clock of its own.

## What this lane does **not** mean

- Receipt **age threshold** / max-age / TTL (`freshness_policy_evaluated` is always
  `false`; `receipt_age_evaluated`/`receipt_age_microseconds` are an **observation**, not
  a verdict)
- Freshness / TTL policy decision
- Operator approval input or consumption
- Writer-stop machine assertion (manual confirmation only)
- Receipt authenticity / signing / HMAC
- Runtime activation authorization
- Mutate-then-restore detection within a read window

## Processing order (pure API)

1. **`now` guard** — non-`datetime` / naive / `None`-offset / `utcoffset`-raising `now`
   → `NO_GO` / `candidate_invalid_now` (revalidation and `precheck_runtime` never reached;
   no raw exception/type/repr escapes)
2. **4g byte-state revalidation** (`revalidate_activation_candidate`) — any `NO_GO`
   short-circuits with the 4g reasons preserved verbatim
3. **Receipt time observation** (`assess_receipt_time`, RTM-7c.4i) — records
   `receipt_age_microseconds`; a **future** `checked_at` short-circuits with
   `candidate_receipt_time_in_future` **before** the fresh precheck (so
   `fresh_precheck_executed=false`); any other assessment `NO_GO` maps to
   `candidate_receipt_time_invalid`
4. **Fresh current-time precheck** (`precheck_runtime` at caller `now`) — machine
   `NO_GO` → each reason re-emitted with the stable `candidate_current_precheck:`
   prefix
5. **Post-revalidation drift** — per-artifact compare of the revalidation
   post-inspection fingerprints against the fresh precheck post-inspection
   fingerprints → `candidate_post_revalidation_artifact_drift:<artifact>`
6. mechanical `PASS`

The Step 5 comparison target is `revalidation.current_fingerprints_after`; a 4g PASS
guarantees that set equals the receipt `fingerprints_after`, so no payload re-parse is
needed. The fresh precheck's own within-window drift is owned by
`candidate_current_precheck:precheck_artifact_changed:<artifact>` (Step 4), not
duplicated in Step 5.

> **Ordering note (RTM-7c.4i):** because the future-receipt fail-close (Step 3) precedes
> the fresh precheck (Step 4), a `now` *earlier* than the receipt `checked_at` is reported
> as `candidate_receipt_time_in_future` with `fresh_precheck_executed=false` — it never
> reaches a `candidate_current_precheck:*` reason.

## Stable reason codes

One canonical reason per root cause. Artifact reasons follow canonical order:

```text
execution_inputs_snapshot
ledger
trigger_journal
active_decision_store
```

| reason | meaning |
|--------|---------|
| `candidate_invalid_now` | `now` non-datetime / naive / missing UTC offset / malformed tz |
| `candidate_*` (4g set) | byte-state revalidation NO_GO (see revalidation contract) |
| `candidate_receipt_time_in_future` | receipt `checked_at` strictly after the caller `now` (RTM-7c.4i) |
| `candidate_receipt_time_invalid` | other receipt time assessment NO_GO (RTM-7c.4i; not reachable after a 4g PASS) |
| `candidate_current_precheck:<precheck reason>` | fresh-precheck machine NO_GO at `now` |
| `candidate_post_revalidation_artifact_drift:<artifact>` | artifact changed between the revalidation read and the fresh-precheck read |

Raw paths, SHA values, DB contents, **raw `checked_at` values**, exception types, and
config secrets never appear in reasons.

## Activation posture (every path)

Constant on every path:

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
explicit_operator_approval_required = true
writers_stopped_manual_confirmation_required = true
freshness_policy_evaluated = false
```

`freshness_policy_evaluated` is constant `false` — this lane applies **no** TTL / max-age /
freshness threshold. `receipt_age_microseconds` is a policy-neutral **observation**, never a
verdict.

Two **per-call** fields:

- `fresh_precheck_executed` — `true` only when the composed `precheck_runtime` actually ran
  (current snapshot/active-decision validity was re-checked at the caller `now`), `false`
  for every short-circuit that returns first (including the future-receipt fail-close).
- `receipt_age_evaluated` — `true` once the verified receipt `checked_at` was compared
  against `now` (RTM-7c.4i), with `receipt_age_microseconds` the exact `now − checked_at`
  integer microseconds (`>= 0`), or `null` for a future receipt / pre-comparison
  short-circuit.

| path | `fresh_precheck_executed` | `receipt_age_evaluated` | `receipt_age_microseconds` |
|------|:---:|:---:|:---:|
| `now` non-datetime / naive / malformed (`candidate_invalid_now`) | false | false | null |
| receipt invalid / not machine PASS | false | false | null |
| config disabled / market / symbol / enabled mismatch | false | false | null |
| 4g artifact unreadable / mismatch / current-window drift | false | false | null |
| future receipt (`candidate_receipt_time_in_future`) | false | true | null |
| fresh precheck ran → PASS | true | true | `>= 0` |
| fresh precheck ran → machine NO_GO (expired / nonterminal / non-quiescent) | true | true | `>= 0` |
| post-revalidation drift (fresh precheck ran) | true | true | `>= 0` |

Mechanical `PASS` does **not** set `activation_authorized=true`.

## CLI contract

Mode: `--final-preflight-activation-candidate` (8th mutually-exclusive mode).

| input | rule |
|-------|------|
| receipt | stdin JSON only (strict bounded parser; 1 MiB) |
| config | `--config` (default `config/config.toml.example`) |
| `now` | CLI reads `datetime.now(tz=Asia/Seoul)` and passes it in |
| envelope | none — raw receipt object only |
| file input | none |

| outcome | exit code |
|---------|-----------|
| mechanical `PASS` | 0 |
| `NO_GO` | 1 |
| config / stdin / internal sanitized failure | 1 |

JSON fields include `current_precheck_outcome`, `current_precheck_reasons`,
`fresh_precheck_executed`, `receipt_age_evaluated`, `receipt_age_microseconds`,
`freshness_policy_evaluated`, the constant posture flags, and the isolation flags
`credential_read`, `network_called`, `broker_called`, `operational_db_written`,
`filesystem_written`, `runtime_file_created` (all `false`).

The CLI summary is **path-free**: it does **not** emit a `config` path field (RTM-7c.4i
H1), no absolute artifact path, no raw receipt JSON, and no raw `checked_at` value. The CLI
also does **not** emit `read_only_databases_opened`, `database_opened`, or any
connection-count field. Production code does not collect connection-open telemetry, so no
such claim is made. `fresh_precheck_executed=true` means the fresh `precheck_runtime` call
ran — during which a **read-only** SQLite inspection may open the configured DBs
(`mode=ro` + `PRAGMA query_only`) — but it does **not** assert a connection count.
`operational_db_written=false` is the write-side guarantee.

CLI loads config with `load_settings(config_path, environ={})` — no `os.environ`
access; `${...}` substitution fails closed.

## SQLite access

```text
read-only SQLite inspection: allowed (on fresh-precheck paths)
write-capable SQLite connections: 0
SQLite writes: 0
schema changes: 0
journal reconcile: 0
store constructors: 0
new sidecars: 0
```

A fresh-precheck path opens the configured DBs read-only (`mode=ro`); no write-capable
connection is ever opened and the DB bytes / `user_version` / sidecar set are unchanged.

## Out of scope (this lane)

- Receipt **age threshold** / max-age / TTL policy (age is observed, never thresholded)
- Freshness policy decision
- Operator approval input or storage
- Receipt signing / HMAC
- Activation token or activation CLI/caller
- `--run` implementation
- KIS / network / broker dispatch / orders
- **Operational** SQLite connection / write (read-only inspection only)
- Schema migration / journal reconcile
- Process scan / kill / OS lock
- Daemon / scheduler / multi-symbol / production calendar

## RTM-7c.4j — single verified snapshot

The untrusted `receipt_payload` is verified and frozen into one immutable
`VerifiedPrecheckReceipt` **once** (`verify_and_snapshot_precheck_receipt`) after the `now`
guard; a non-VALID snapshot fails closed to `candidate_receipt_invalid` before any stage runs.
The 4g byte-state revalidation (`revalidate_verified_activation_candidate`) and the 4i receipt
time observation (`assess_verified_receipt_time`) then read that **same** snapshot. The raw
receipt verifier is therefore called exactly once per preflight, and a cross-stage mutation of
the raw payload cannot mix observations (hash from one read, age from another). See
`PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md`.

## Related contracts

- `PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md` — single immutable snapshot all stages read
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_REVALIDATION_CONTRACT.md` — composed 4g byte-state lane
- `PAPER_FAST_LOOP_RECEIPT_TIME_ASSESSMENT_CONTRACT.md` — composed 4i receipt time observation
- `PAPER_FAST_LOOP_RUNTIME_PRECHECK_CONTRACT.md` — composed fresh-precheck semantics
- `PAPER_FAST_LOOP_PRECHECK_RECEIPT_VERIFICATION_CONTRACT.md` — verifier semantics
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — activation stage model
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root + CLI modes
