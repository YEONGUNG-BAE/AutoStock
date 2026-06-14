# Activation Candidate Final Preflight Contract (RTM-7c.4h)

Read-only **time-aware final preflight** for the paper fast-loop activation
candidate. Composes the RTM-7c.4g byte-state revalidation with a **fresh,
caller-supplied-time** machine precheck so that — even when every artifact byte is
**identical** to the candidate receipt's post-inspection state — an execution-inputs
snapshot or active decision whose validity window has since **opened or closed**
(or any other current-time precheck NO_GO) is caught.

**Runtime activation: NO-GO.** Mechanical preflight PASS is **not** Operator
approval, writer-stop proof, receipt authenticity, receipt-age evaluation, freshness
evaluation, or activation authorization.

Code: `composition.activation_candidate_final_preflight.final_preflight_activation_candidate`
CLI: `ops/run_paper_fast_loop.py --final-preflight-activation-candidate`

## What this lane evaluates

> **Current-state time-validity only.** On top of a 4g byte-state PASS, a fresh
> precheck at the caller-supplied `now` re-checks the snapshot / active-decision
> validity windows against that `now`.

`now` is supplied by the **caller** and must be timezone-aware. The module reads no
clock of its own.

## What this lane does **not** mean

- Receipt age / max-age (`receipt_age_evaluated` is always `false`)
- Freshness / TTL policy (`freshness_policy_evaluated` is always `false`)
- Operator approval input or consumption
- Writer-stop machine assertion (manual confirmation only)
- Receipt authenticity / signing / HMAC
- Runtime activation authorization
- Mutate-then-restore detection within a read window

## Processing order (pure API)

1. **`now` guard** — naive/malformed `now` → `NO_GO` / `candidate_invalid_now`
   (the composed `precheck_runtime` is never reached)
2. **4g byte-state revalidation** (`revalidate_activation_candidate`) — any `NO_GO`
   short-circuits with the 4g reasons preserved verbatim
3. **Fresh current-time precheck** (`precheck_runtime` at caller `now`) — machine
   `NO_GO` → each reason re-emitted with the stable `candidate_current_precheck:`
   prefix
4. **Post-revalidation drift** — per-artifact compare of the revalidation
   post-inspection fingerprints against the fresh precheck post-inspection
   fingerprints → `candidate_post_revalidation_artifact_drift:<artifact>`
5. mechanical `PASS`

The Step 4 comparison target is `revalidation.current_fingerprints_after`; a 4g PASS
guarantees that set equals the receipt `fingerprints_after`, so no payload re-parse is
needed. The fresh precheck's own within-window drift is owned by
`candidate_current_precheck:precheck_artifact_changed:<artifact>` (Step 3), not
duplicated in Step 4.

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
| `candidate_invalid_now` | `now` naive / missing UTC offset |
| `candidate_*` (4g set) | byte-state revalidation NO_GO (see revalidation contract) |
| `candidate_current_precheck:<precheck reason>` | fresh-precheck machine NO_GO at `now` |
| `candidate_post_revalidation_artifact_drift:<artifact>` | artifact changed between the revalidation read and the fresh-precheck read |

Raw paths, SHA values, DB contents, exception types, and config secrets never appear
in reasons.

## Activation posture (every path)

Constant on every path:

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
explicit_operator_approval_required = true
writers_stopped_manual_confirmation_required = true
receipt_age_evaluated = false
freshness_policy_evaluated = false
```

`receipt_age_evaluated` / `freshness_policy_evaluated` are constant `false` — this lane
never evaluates receipt age or any freshness policy.

The one **per-call** field is `fresh_precheck_executed`: it is `true` only when the
composed `precheck_runtime` actually ran (current snapshot/active-decision validity was
re-checked at the caller `now`), and `false` for every short-circuit that returns first.

| path | `fresh_precheck_executed` |
|------|:---:|
| `now` naive / malformed (`candidate_invalid_now`) | false |
| receipt invalid / not machine PASS | false |
| config disabled / market / symbol / enabled mismatch | false |
| 4g artifact unreadable / mismatch / current-window drift | false |
| fresh precheck ran → PASS | true |
| fresh precheck ran → machine NO_GO (expired / not-yet-valid / nonterminal / non-quiescent) | true |
| post-revalidation drift (fresh precheck ran) | true |

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
`fresh_precheck_executed`, the constant posture flags, and the isolation flags
`credential_read`, `network_called`, `broker_called`, `operational_db_written`,
`filesystem_written`, `runtime_file_created` (all `false`).

The CLI does **not** emit `read_only_databases_opened`, `database_opened`, or any
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

- Receipt age / max-age / TTL policy
- Freshness policy
- Operator approval input or storage
- Receipt signing / HMAC
- Activation token or activation CLI/caller
- `--run` implementation
- KIS / network / broker dispatch / orders
- **Operational** SQLite connection / write (read-only inspection only)
- Schema migration / journal reconcile
- Process scan / kill / OS lock
- Daemon / scheduler / multi-symbol / production calendar

## Related contracts

- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_REVALIDATION_CONTRACT.md` — composed 4g byte-state lane
- `PAPER_FAST_LOOP_RUNTIME_PRECHECK_CONTRACT.md` — composed fresh-precheck semantics
- `PAPER_FAST_LOOP_PRECHECK_RECEIPT_VERIFICATION_CONTRACT.md` — verifier semantics
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — activation stage model
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root + CLI modes
