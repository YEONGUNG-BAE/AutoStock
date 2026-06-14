# Receipt Time Assessment Contract (RTM-7c.4i)

Read-only **policy-neutral receipt time observation** for the paper fast-loop activation
candidate. Observes the time relationship between a **verified** precheck receipt's
`checked_at` and a **caller-supplied** `now`: it computes the exact receipt age and
fail-closes a **future** `checked_at`. It selects **no** max-age / TTL / freshness
threshold.

**Runtime activation: NO-GO.** Computing a receipt age is **not** a freshness verdict, not
receipt authenticity, not Operator approval, not writer-stop proof, and not activation
authorization.

Code: `composition.receipt_time_assessment.assess_receipt_time`

## What this lane evaluates

> **Age observation + future fail-close only.** Given a receipt that the existing verifier
> accepts as `VALID`, compare its `checked_at` against the caller `now`:
> - `checked_at > now` → `NO_GO` / `receipt_time_in_future` (clock/ordering fault)
> - otherwise → `VALID` with the exact integer `now − checked_at` microseconds

`now` is supplied by the **caller** and must be timezone-aware. The module reads no clock
of its own.

## What this lane does **not** mean

- Max age / TTL / freshness threshold (`freshness_policy_evaluated` is always `false`)
- Receipt authenticity / signing / HMAC
- Operator approval input or consumption
- Writer-stop assertion
- Runtime activation authorization

## Outcome model

```text
ReceiptTimeAssessmentOutcome = { VALID, NO_GO }

ReceiptTimeAssessment:
  outcome:                    VALID | NO_GO
  reasons:                    tuple[str, ...]
  receipt_checked_at:         str | None
  receipt_age_microseconds:   int | None     # >= 0 on VALID; None otherwise
  receipt_age_evaluated:      bool
  freshness_policy_evaluated: bool            # constant False
```

`receipt_age_microseconds` is the **exact** integer microseconds of `now − checked_at`,
computed from `timedelta.days/seconds/microseconds` (never a float `total_seconds`). It is
`>= 0` on `VALID` and `None` otherwise.

## Processing order (pure API)

1. **`now` guard** — non-`datetime` / naive / `None`-offset / `utcoffset`-raising → `NO_GO`
   / `receipt_time_invalid_now` (verifier never reached)
2. **Verifier reuse** — `verify_runtime_precheck_receipt_payload`; not `VALID` → `NO_GO` /
   `receipt_time_receipt_invalid` (no new canonical verifier or JSON parser is built)
3. **`checked_at` parse** — defensive aware-datetime parse of the verified `checked_at`;
   unparseable / naive → `NO_GO` / `receipt_time_invalid_checked_at`
4. **Future fail-close** — `checked_at > now` → `NO_GO` / `receipt_time_in_future`
   (`receipt_age_evaluated=true`, `receipt_age_microseconds=None`)
5. **Exact age** — `VALID` with `receipt_age_microseconds >= 0`

## Stable reason codes

| reason | meaning |
|--------|---------|
| `receipt_time_invalid_now` | `now` non-datetime / naive / malformed timezone |
| `receipt_time_receipt_invalid` | receipt verifier returned `INVALID` |
| `receipt_time_invalid_checked_at` | verified `checked_at` not a parseable aware datetime |
| `receipt_time_in_future` | `checked_at` strictly after `now` |

Raw `checked_at` strings, raw payload values, exception types/reprs, and tracebacks never
appear in reasons.

## `receipt_age_evaluated` matrix

| path | `receipt_age_evaluated` | `receipt_age_microseconds` |
|------|:--:|:--:|
| invalid `now` | false | None |
| receipt invalid | false | None |
| invalid `checked_at` | false | None |
| future receipt | true | None |
| valid age | true | `>= 0` int |

## Out of scope (this lane)

- Max-age / TTL / freshness threshold selection or evaluation
- Receipt signing / HMAC / authenticity
- Operator approval input or storage
- Activation token / activation CLI / caller
- `--run` implementation
- KIS / network / broker dispatch / orders
- Operational SQLite write
- Daemon / scheduler / multi-symbol / production calendar

## RTM-7c.4j — verified snapshot core

`assess_verified_receipt_time(*, receipt, now)` is the snapshot-based core: it reads the
already-aware `checked_at` off an immutable `VerifiedPrecheckReceipt` and performs **no**
verifier call and **no** `checked_at` parse. The public `assess_receipt_time` is now a
raw-payload wrapper that guards `now` first (preserving the 4i precedence — an invalid `now`
fail-closes before the receipt is verified), then builds a snapshot once (non-VALID →
`receipt_time_receipt_invalid`) and delegates to the core. The snapshot builder strict-clones
the caller payload to a detached built-in JSON tree before verify/extract (no `copy.deepcopy` /
caller hooks); once clone completes, caller mutation cannot affect the frozen observation.
Because the verifier already guarantees a parseable aware `checked_at`, the former
`receipt_time_invalid_checked_at` reason is absorbed at snapshot-build time and no longer
exists in this lane. See `PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md`.

## Related contracts

- `PAPER_FAST_LOOP_PRECHECK_RECEIPT_VERIFICATION_CONTRACT.md` — reused verifier semantics
- `PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md` — immutable snapshot consumed by the core
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FINAL_PREFLIGHT_CONTRACT.md` — composes this lane
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — activation stage model
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root + CLI modes
- `docs/TECH_DEBT.md` (OPEN items)
