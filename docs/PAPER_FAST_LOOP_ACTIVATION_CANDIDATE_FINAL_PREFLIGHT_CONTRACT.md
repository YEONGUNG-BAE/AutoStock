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

## Fixed activation posture (every path)

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
explicit_operator_approval_required = true
writers_stopped_manual_confirmation_required = true
current_validity_evaluated = true
receipt_age_evaluated = false
freshness_policy_evaluated = false
```

The three `*_evaluated` flags describe the **lane's evaluation policy** (it checks
current-time validity; it never checks receipt age or freshness), not per-call
completion. Mechanical `PASS` does **not** set `activation_authorized=true`.

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

JSON fields include `current_precheck_outcome`, `current_precheck_reasons`, the seven
posture flags, and the isolation flags `credential_read`, `network_called`,
`broker_called`, `operational_db_written`, `filesystem_written`,
`runtime_file_created` (all `false`), plus **`read_only_databases_opened`** — which
honestly reflects whether the composed precheck ran (it opens the configured DBs
**read-only**: `mode=ro` + `PRAGMA query_only`). There is **no** `database_opened=false`
claim, because the composed precheck does open read-only connections.

CLI loads config with `load_settings(config_path, environ={})` — no `os.environ`
access; `${...}` substitution fails closed.

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
