# Operator Approval Intent CLI Contract (RTM-7c.4p)

Read-only **CLI input boundary** that explicitly generates an Operator approval intent to
**stdout JSON only**. Composes one freshness-qualified mechanical PASS with CREATED canonical
evidence, then invokes `build_operator_approval_intent` exactly once.

**Runtime activation: NO-GO.** The CLI does **not** authenticate Operator identity, sign/HMAC,
consume approval, persist intent, prove writer-stop mechanically, or authorize activation.

Code: `ops/run_paper_fast_loop.py` mode `--build-operator-approval-intent`

## Mode

10th mutually-exclusive CLI mode (alongside the existing nine):

```text
--build-operator-approval-intent
```

Requires:

```text
--config
--max-age-microseconds
--json
--operator-approval-declared
--writers-stopped-manually-confirmed
--live-orders-forbidden-confirmed
```

All three confirmation flags must be **explicitly present** on the command line. Absence is
**not** interpreted as approval — each flag must be present so argparse sets the value to exact
built-in `True`.

## What the three confirmations mean

Manual Operator declarations only:

```text
operator_approval_declared
writers_stopped_manually_confirmed
live_orders_forbidden_confirmed
```

They do **not** mean:

- Operator identity authentication
- Writer-stop machine proof
- Live-order capability removal
- Consumed approval
- Activation authorization

## What this mode does **not** do

- Intent persistence / file output
- Approval consumption / replay prevention
- Signing / HMAC
- Operator identity authentication
- Activation token / runtime activation authorization
- `--run` (still refused with exit 2 before any other processing)
- KIS / network / broker / order
- Operational DB write / schema migration / reconcile
- Runtime artifact creation

## Processing order

```text
1. argparse / mutually-exclusive mode resolution
2. --run early refusal (exit 2)
3. confirmation-flag applicability (wrong mode → FAIL)
4. --max-age-microseconds applicability (wrong mode → FAIL)
5. explicit max-age parse (invalid/missing → FAIL)
6. three confirmation presence checks (missing → FAIL)
7. stdin bounded receipt JSON read/parse
8. load_settings(config, environ={})
9. KST now read exactly once
10. freshness_qualify_and_build_candidate_evidence exactly once
11. build_operator_approval_intent exactly once (only on combined PASS)
12. sanitized JSON output to stdout
```

### Early-failure isolation

The following fail **before** stdin/config/env/clock/DB/filesystem access:

- confirmation flag on a non-build mode → `approval_intent_argument_not_applicable`
- max-age on a non-freshness/non-build mode → `freshness_policy_argument_not_applicable`
- max-age missing/invalid on build mode → `freshness_policy_input_missing` /
  `freshness_policy_input_invalid`
- any confirmation missing on build mode → stable missing-reason codes below

`--run` is refused before all of the above (exit 2).

### Missing confirmation reasons

```text
approval_intent_operator_declaration_missing
approval_intent_writer_stop_confirmation_missing
approval_intent_live_order_prohibition_confirmation_missing
```

Wrong-mode confirmation:

```text
approval_intent_argument_not_applicable
```

## Declared-at binding

The single KST `now` read in step 9 is passed to:

```text
freshness_qualify_and_build_candidate_evidence(..., now=now)
build_operator_approval_intent(..., declared_at=now)
```

Normal CLI generation therefore satisfies:

```text
declared_at == evidence.evaluated_at
```

The API contract remains `declared_at >= evidence.evaluated_at`.

## Aggregate outcomes

Top-level:

```text
outcome = PASS | NO_GO | FAIL
reasons = [...]
```

| case | outcome | exit | intent digest |
|------|---------|------|---------------|
| combined PASS + intent CREATED | PASS | 0 | hex64 schema 1 |
| upstream mechanical NO_GO | NO_GO | 1 | null |
| combined PASS + intent not CREATED | NO_GO | 1 | null |
| CLI input failure | FAIL | 1 | null |

Upstream NO_GO preserves exact upstream reasons verbatim. Do **not** append
`approval_intent_not_eligible` to aggregate reasons.

Intent generation failure (INVALID / exception):

```text
reasons = ["approval_intent_generation_invalid"]
```

Raw builder internals and exceptions are never printed.

## PASS JSON shape

```json
{
  "outcome": "PASS",
  "reasons": [],
  "candidate_evidence_schema_version": 2,
  "candidate_evidence_sha256": "<hex64>",
  "approval_intent_schema_version": 1,
  "approval_intent_sha256": "<hex64>",
  "approval_scope": "attended_paper_fast_loop_candidate",
  "declared_at": "<aware ISO>",
  "operator_approval_declared": true,
  "writers_stopped_manually_confirmed": true,
  "live_orders_forbidden_confirmed": true,
  "activation_authorized": false,
  "runtime_activation_outcome": "no_go",
  "approval_intent_authenticated": false,
  "approval_intent_consumed": false,
  "approval_intent_persisted": false
}
```

NO_GO / FAIL / input failure:

```text
approval_intent_schema_version = null
approval_intent_sha256 = null
declared_at = null
candidate_evidence_sha256 = null
candidate_evidence_schema_version = null
```

Constant NO-GO / unauthenticated posture fields remain `false` / `"no_go"` even when intent
generation fails after an upstream PASS.

## Output prohibitions

Never emit on stdout/stderr:

- raw precheck receipt
- full evidence object
- artifact fingerprints
- DB/config absolute path
- raw max-age token
- KIS key names or values
- exception message/repr
- traceback
- environment variable names/values
- SQLite raw error text

`approval_intent_sha256` and `candidate_evidence_sha256` **may** be emitted.

## Filesystem / DB contract

Allowed:

- read-only SQLite inspection inside the existing final-preflight path (same as freshness mode)

Forbidden:

- write-capable SQLite connection
- schema creation/change / reconcile
- intent file write / runtime artifact creation / approval DB/table
- persistence / logging raw intent or receipt

## Single-execution rule (PASS path)

Per successful call:

```text
stdin parse           = 1
settings load         = 1
clock read            = 1
receipt verifier      = 1  (inside pipeline)
receipt snapshot      = 1  (inside pipeline)
fresh precheck        = 1  (inside pipeline)
freshness evaluator   = 1  (inside pipeline)
evidence builder      = 1  (inside pipeline, PASS only)
approval-intent builder = 1 (PASS only)
```

Upstream NO_GO must skip the approval-intent builder (`0`).

## Related contracts

- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CONTRACT.md` — API-only intent builder
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FRESHNESS_PREFLIGHT_CONTRACT.md` — upstream qualified preflight
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_EVIDENCE_CONTRACT.md` — evidence schema v2
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — CLI wiring root
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — attended activation posture inventory

## Still OPEN (unchanged posture)

Approval consumption, intent persistence/file output, signing/HMAC, Operator identity
authentication, replay/nonce/idempotency, approval pre-consumption revalidation, activation
token/caller, `--run`, KIS/network, broker/order, operational DB write / schema migration /
reconcile, daemon/scheduler/process lock, unattended pilot, writer-stop machine proof, default
runtime activation beyond constant NO-GO.
