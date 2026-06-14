# Activation Candidate Freshness Preflight Contract (RTM-7c.4l)

Read-only **explicit freshness-qualified** activation candidate preflight for the paper
fast-loop. Composes verified final preflight (RTM-7c.4h core) with an explicit caller-supplied
`ReceiptFreshnessPolicy`.

**Runtime activation: NO-GO.** A freshness-qualified mechanical PASS is **not** Operator
approval, writer-stop proof, receipt authenticity, or activation authorization.

Code: `composition.activation_candidate_freshness_preflight.freshness_qualify_activation_candidate`

## What this lane evaluates

> **Final preflight mechanical PASS AND explicit freshness policy FRESH** →
> freshness-qualified mechanical PASS.

Processing requires:

1. Valid explicit policy (`ReceiptFreshnessPolicy` exact type, valid max-age)
2. Valid receipt snapshot (one verifier call)
3. Verified final-preflight mechanical PASS (4g + 4i + fresh precheck + drift)
4. Explicit freshness evaluation on the **same** `ReceiptTimeAssessment` object from final
   preflight (`age <= max_age` inclusive → FRESH)

## What this lane does **not** mean

- Max-age **value selection** or operational calibration (OPEN)
- Config/env/CLI threshold binding (OPEN)
- Automatic integration into `--final-preflight-activation-candidate` (existing final
  preflight stays policy-neutral; `freshness_policy_evaluated=false` there)
- Writer-stop machine assertion
- Receipt authenticity / signing / HMAC
- Runtime activation authorization

## Core invariant: explicit policy argument only

```text
policy는 required argument다. optional/default/config fallback 없음.
```

## Public API

```python
freshness_qualify_activation_candidate(
    *,
    settings: RuntimePaperFastLoopSettings,
    receipt_payload: object,
    now: datetime,
    policy: ReceiptFreshnessPolicy,   # required — no default
    base_dir: str | Path | None = None,
) -> ActivationCandidateFreshnessPreflightResult
```

## Processing order

1. **Policy strict validation + snapshot** (`snapshot_receipt_freshness_policy`) —
   invalid → `NO_GO` / `candidate_freshness_policy_invalid`,
   `freshness_policy_evaluated=false`; no `now` guard, receipt snapshot, verifier,
   filesystem, or SQLite. Valid policy is frozen once; caller policy is not re-read.
2. **`now` strict validation** (`final_preflight_now_is_invalid`, shared with 4h) —
   invalid → `NO_GO` / `candidate_invalid_now`, `freshness_policy_evaluated=false`;
   no receipt snapshot, verifier, final core, revalidation, filesystem, SQLite, or evaluator
3. **Receipt snapshot once** — `verify_and_snapshot_precheck_receipt`; INVALID →
   `candidate_receipt_invalid`
4. **Verified final preflight core** — `final_preflight_verified_activation_candidate` on the
   frozen snapshot (verifier 0, raw payload 0); any `NO_GO` preserves existing final-preflight
   reasons verbatim; freshness evaluator **not** called; `freshness_policy_evaluated=false`
5. **Explicit freshness evaluation** — only on final PASS:
   `evaluate_receipt_freshness(time_assessment=final_result.receipt_time_assessment, policy_snapshot)`
   - FRESH → qualified PASS, `freshness_policy_evaluated=true`
   - STALE → `candidate_receipt_stale`, `freshness_policy_evaluated=true`
   - evaluator defensive NO_GO → `candidate_freshness_evaluation_invalid`,
     `freshness_policy_evaluated=false` (raw evaluator reason not duplicated)

### Reason precedence (early short-circuits)

| policy | now | receipt | result |
|--------|-----|---------|--------|
| invalid | any | any | `candidate_freshness_policy_invalid` |
| valid | invalid | any | `candidate_invalid_now` |
| valid | valid | invalid | `candidate_receipt_invalid` |
| valid | valid | valid final NO_GO | existing final reason (verbatim) |
| valid | valid | final PASS + stale | `candidate_receipt_stale` |
| valid | valid | final PASS + fresh | PASS |

Raw exception/type/repr never appear in reasons.

## Stable reason codes (this lane)

| reason | meaning |
|--------|---------|
| `candidate_freshness_policy_invalid` | policy not exact `ReceiptFreshnessPolicy` or invalid max-age |
| `candidate_invalid_now` | `now` non-datetime / naive / missing UTC offset / malformed tz |
| `candidate_receipt_invalid` | snapshot build INVALID |
| *(final-preflight reasons preserved)* | e.g. `candidate_receipt_time_in_future`, `candidate_current_precheck:*`, `candidate_post_revalidation_artifact_drift:*` |
| `candidate_receipt_stale` | final PASS but age strictly greater than max |
| `candidate_freshness_evaluation_invalid` | evaluator defensive NO_GO after final PASS |

## Single-observation invariants

Per `freshness_qualify_activation_candidate` call:

```text
policy snapshot builds = 1          (valid policy only; caller policy frozen once)
receipt verifier calls = 1
receipt snapshot builds = 1
verified final-preflight core verifier calls = 0
freshness evaluator verifier calls = 0
```

Policy snapshot is **not** a default, persistence layer, or config binding — it freezes the
caller-supplied `max_age_microseconds` once at call start. Caller policy mutation after snapshot
does not change the verdict or nested `freshness_evaluation.max_age_microseconds`.

Invalid `now` short-circuits after policy snapshot with zero receipt observation (no receipt
snapshot, verifier, filesystem, SQLite, final core, or evaluator).

Raw receipt is not re-read after snapshot. `ReceiptTimeAssessment` object identity from final
preflight equals evaluator input.

## Activation posture (every path)

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
explicit_operator_approval_required = true
writers_stopped_manual_confirmation_required = true
```

## CLI mode (RTM-7c.4m)

Operator CLI: `ops/run_paper_fast_loop.py --freshness-preflight-activation-candidate`

9th mutually-exclusive mode. Requires stdin raw receipt object, `--config`, and **required**
`--max-age-microseconds`. The token is validated as an **entire-string** ASCII decimal
(`re.fullmatch(r"[0-9]+", token)`), so **all** whitespace — including a trailing newline,
`\r`, `\t`, `\v`, `\f`, and leading/embedded whitespace — is rejected (`re.match` + `$` would
have accepted a trailing `\n`). Only an exact built-in `str` is accepted (`type(raw) is str`),
so a non-`str` object or a `str` subclass fails closed rather than raising. Integer conversion
is guarded: a Python integer-string-conversion `ValueError` (a token longer than the runtime
digit limit) is normalized to `freshness_policy_input_invalid`, never escaping as a traceback;
only `ValueError` is caught, so `MemoryError`/`KeyboardInterrupt`/`SystemExit` are not
swallowed. A rejected over-long token is a **CLI-input-invalid** event, **not** a selection of
any freshness max-age upper bound. No default/config/env threshold.

Recommended invocation:

```bash
PYTHONPATH=src uv run python ops/run_paper_fast_loop.py \
  --freshness-preflight-activation-candidate \
  --config path/to/config.toml \
  --max-age-microseconds 300000000 \
  --json
```

### CLI processing order

1. Args parse / mode resolution
2. `--run` early refusal (exit 2)
3. `--max-age-microseconds` applicability (other modes → `freshness_policy_argument_not_applicable`)
4. Max-age strict parse (`freshness_policy_input_missing` / `freshness_policy_input_invalid`)
5. Stdin receipt read + strict JSON parse (reuse existing reader)
6. `load_settings(config, environ={})`
7. `now = datetime.now(tz=KST)` exactly once
8. `freshness_qualify_activation_candidate(..., policy=ReceiptFreshnessPolicy(...))`
9. Path-free sanitized JSON/text output

Invalid max-age (wrong-object, non-ASCII/whitespace token, trailing newline, over-long token,
or integer-conversion `ValueError`): stdin read 0, config load 0, env access 0, clock read 0,
DB/filesystem 0, freshness evaluator 0. The raw token, exception type/message, and any
traceback never appear in stdout/stderr.

### CLI exit codes

| outcome | exit |
|---------|------|
| freshness-qualified mechanical `PASS` | 0 |
| `NO_GO` (incl. stale, final-preflight short-circuit, input/config failure) | 1 |
| `--run` | 2 |

Mechanical FRESH PASS is **not** activation authorization (`activation_authorized=false`,
`runtime_activation_outcome="no_go"` always).

### CLI JSON (path-free)

Top-level fields include: `mode=freshness_preflight_activation_candidate`, `outcome`,
`reasons`, `receipt_sha256`, `market`, `symbol`, `freshness_policy_evaluated`,
`receipt_age_microseconds`, `max_age_microseconds`, `final_preflight_outcome`,
`final_preflight_reasons`, constant NO-GO activation posture, and side-effect attestations
(`credential_read`, `network_called`, `broker_called`, `operational_db_written`,
`filesystem_written`, `runtime_file_created` all `false`). No config path, artifact path,
raw receipt, raw `checked_at`, fingerprint payload, or secret/env identifier.

`max_age_microseconds` is `null` when CLI input itself is missing/invalid; when a valid
explicit policy was parsed and the API path ran, the parsed integer is emitted even on
final-preflight `NO_GO` short-circuits.

`--final-preflight-activation-candidate` remains policy-neutral and unchanged.

## Out of scope (unchanged)

- Default max-age / config/env max-age field
- Max-age value selection / TTL calibration
- Policy persistence
- Operator approval input/storage/consumption
- Receipt signing / HMAC
- Activation token / activation caller / `--run` implementation
- KIS / network / broker dispatch / orders
- Operational SQLite write
- Daemon / scheduler

## Related contracts

- `PAPER_FAST_LOOP_RECEIPT_FRESHNESS_POLICY_CONTRACT.md` — pure evaluator + shared policy validation
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FINAL_PREFLIGHT_CONTRACT.md` — verified core reused here
- `PAPER_FAST_LOOP_RECEIPT_TIME_ASSESSMENT_CONTRACT.md` — age observation reused via final preflight
- `PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md` — single snapshot build
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — activation stage model
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root + CLI modes
- `docs/TECH_DEBT.md` (OPEN items)
