# Receipt Freshness Policy Contract (RTM-7c.4k)

Read-only **explicit receipt freshness policy evaluation** for the paper fast-loop
activation candidate. Compares an already-computed `ReceiptTimeAssessment` against a
**caller-supplied** `ReceiptFreshnessPolicy` and decides whether the observed receipt age
is within the explicit max-age bound.

**Runtime activation: NO-GO.** A `FRESH` outcome is **not** Operator approval, not
writer-stop proof, not receipt authenticity, and not activation authorization.

Code: `composition.receipt_freshness_policy.evaluate_receipt_freshness`

## What this lane evaluates

> **Explicit max-age comparison only.** Given a valid age observation
> (`ReceiptTimeAssessment.outcome = VALID`, `receipt_age_evaluated = true`, exact non-negative
> integer `receipt_age_microseconds`) and an explicit policy
> (`ReceiptFreshnessPolicy.max_age_microseconds`):
>
> - `receipt_age_microseconds <= max_age_microseconds` → `FRESH`
> - `receipt_age_microseconds > max_age_microseconds` → `STALE` / `receipt_age_exceeds_policy`

The boundary is **inclusive**: `age == max_age` → `FRESH`.

No clock read, no receipt payload/snapshot re-read, no verifier re-call, and no config/env
access. This lane consumes the **existing** age observation result only.

## What this lane does **not** mean

- Threshold **selection** or operational calibration (OPEN)
- Module-level default max age, config default, environment variable, or CLI flag
- Automatic integration into final preflight (`freshness_policy_evaluated` there stays `false`)
- Receipt authenticity / signing / HMAC
- Operator approval input or consumption
- Writer-stop assertion
- Runtime activation authorization

## Core invariant: explicit policy only

```text
정책 객체가 명시적으로 전달되지 않으면 freshness 평가가 존재하지 않는다.
```

There is no hidden fallback, no “reasonable” threshold, and no test-only constant promoted
to production.

## Policy model

```text
ReceiptFreshnessPolicy:
  max_age_microseconds: int    # exact built-in int, bool rejected, >= 0
```

Validation (evaluator-side, fail-closed):

- exact built-in `int` (`type(value) is int`)
- `bool` rejected (`True`/`False` are not valid max-age values)
- `>= 0` (zero is an explicit policy — only `age == 0` is FRESH when max is 0)
- no upper bound added by this lane
- no float/string coercion

## Outcome model

```text
ReceiptFreshnessOutcome = { FRESH, STALE, NO_GO }

ReceiptFreshnessEvaluation:
  outcome:                      FRESH | STALE | NO_GO
  reasons:                      tuple[str, ...]
  receipt_age_microseconds:     int | None
  max_age_microseconds:         int | None
  freshness_policy_evaluated:   bool
  activation_authorized:        bool            # constant False
  runtime_activation_outcome:   str             # constant "no_go"
```

`freshness_policy_evaluated` is `true` only when a valid policy was applied to a valid age
observation (`FRESH` or `STALE`). Invalid policy or invalid time assessment → `false`.

## Processing order (pure API)

1. **Policy strict validation** — exact `type(policy) is ReceiptFreshnessPolicy` and valid
   `max_age_microseconds` via shared `receipt_freshness_policy_is_valid` / `_validated_policy_max_age`
   (single field read per validation path); invalid → `NO_GO` /
   `freshness_policy_invalid` (`freshness_policy_evaluated=false`)
2. **Time assessment strict validation** — exact `type(time_assessment) is ReceiptTimeAssessment`;
   `VALID` with empty `reasons`, `freshness_policy_evaluated=false`, `receipt_age_evaluated=true`,
   exact non-negative int age, exact `str` `receipt_checked_at`; otherwise → `NO_GO` /
   `freshness_time_assessment_invalid` (`freshness_policy_evaluated=false`)
3. **Inclusive comparison** — `age <= max_age` → `FRESH`; else → `STALE` /
   `receipt_age_exceeds_policy` (`freshness_policy_evaluated=true`)

Wrong-object inputs (including `None`, arbitrary objects, subclasses) fail closed with stable
reasons — no `AttributeError` escape, no raw type/repr/traceback in reasons.

## Stable reason codes

| reason | meaning |
|--------|---------|
| `freshness_policy_invalid` | policy `max_age_microseconds` not an exact non-negative int |
| `freshness_time_assessment_invalid` | time assessment not usable for freshness (not VALID, age not evaluated, or invalid age shape) |
| `receipt_age_exceeds_policy` | valid observation; age strictly greater than max (STALE only) |

Raw receipt time strings, exception types/reprs, and tracebacks never appear in reasons.

## Age observation vs policy verdict

RTM-7c.4i (`receipt_time_assessment`) **observes** exact integer age and fail-closes future
receipts — it selects no threshold (`freshness_policy_evaluated=false` there).

RTM-7c.4k **verdicts** against an explicit max-age when a caller supplies a policy. The two
stages are separate; final preflight (4h) still does not compose freshness evaluation.

RTM-7c.4l (`freshness_qualify_activation_candidate`) is the **API-only** composition lane:
explicit `policy` argument required, no default threshold, no CLI integration. It reuses the
4h verified core and passes the same `ReceiptTimeAssessment` object to this evaluator.

**RTM-7c.4l closure — policy snapshot:** `snapshot_receipt_freshness_policy(policy)` validates
exact `ReceiptFreshnessPolicy`, reads `max_age_microseconds` once, and returns a new frozen
instance. The snapshot is not a default, persistence layer, or config binding. Qualified
preflight passes the snapshot (not the caller object) to `evaluate_receipt_freshness`; caller
policy mutation after snapshot does not change verdict or nested evaluation fields.

## Activation posture (every path)

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
```

## Out of scope (this lane)

- Max-age value selection / TTL calibration
- Config/env/CLI threshold binding
- Final-preflight automatic policy integration
- Freshness policy persistence
- Operator approval input/storage/consumption
- Receipt signing / HMAC
- Activation token / activation CLI / caller
- `--run` implementation
- KIS / network / broker dispatch / orders
- Operational SQLite write
- Daemon / scheduler

## Related contracts

- `PAPER_FAST_LOOP_RECEIPT_TIME_ASSESSMENT_CONTRACT.md` — age observation consumed here
- `PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md` — immutable snapshot upstream
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FINAL_PREFLIGHT_CONTRACT.md` — verified core; wrapper stays policy-neutral
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FRESHNESS_PREFLIGHT_CONTRACT.md` — API-only qualified preflight (4l)
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — activation stage model
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root + CLI modes
- `docs/TECH_DEBT.md` (OPEN items)
