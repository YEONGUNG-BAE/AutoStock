# Operator Approval Intent Verification Contract (RTM-7c.4q)

Read-only **standalone verification** of an externally supplied Operator approval-intent JSON
object. Validates strict schema v1, semantic posture, and `approval_intent_sha256` canonical hash
parity against the same rules used by the builder.

**Runtime activation: NO-GO.** VALID means schema/semantic/hash consistency only — not Operator
identity authentication, signature/HMAC, evidence content revalidation, approval consumption,
replay prevention, freshness, persistence, or activation authorization.

Code:

- `composition.operator_approval_intent_verifier.verify_operator_approval_intent_payload`
- `ops/run_paper_fast_loop.py` mode `--verify-operator-approval-intent`

## What VALID means

> The input JSON conforms to the supported approval-intent schema v1, field semantics match the
> constant attended-paper posture, and the stored `approval_intent_sha256` matches independent
> recomputation over the 12 non-digest hash fields.

## What VALID does **not** mean

- Operator identity authentication
- Signature / HMAC authenticity
- Proof the intent came from a real Operator
- Evidence content revalidation (only `evidence_schema_version` + `evidence_sha256` binding)
- Writer-stop machine proof
- Approval consumption
- Replay / nonce / idempotency
- Freshness judgment
- Persistence (verifier stores nothing)
- Activation authorization (`activation_authorized` stays `false`; `runtime_activation_outcome`
  stays `"no_go"`)

## Strict schema (exactly 13 fields)

```text
schema_version
declared_at
evidence_schema_version
evidence_sha256
market
symbol
approval_scope
operator_approval_declared
writers_stopped_manually_confirmed
live_orders_forbidden_confirmed
activation_authorized
runtime_activation_outcome
approval_intent_sha256
```

Unknown or missing field → `INVALID`.

## Scalar and semantic contract

Shared with the builder via `composition.operator_approval_intent`:

```text
schema_version                         = exact built-in int == 1
declared_at                            = exact built-in str, ISO parseable, timezone-aware
evidence_schema_version                = exact built-in int == 2
evidence_sha256                        = exact built-in str, lowercase hex64
market                                 = exact built-in str == "KR"
symbol                                 = exact built-in str, ASCII [0-9]{6}
approval_scope                         = exact built-in str == "attended_paper_fast_loop_candidate"
operator_approval_declared             = exact built-in True
writers_stopped_manually_confirmed     = exact built-in True
live_orders_forbidden_confirmed        = exact built-in True
activation_authorized                  = exact built-in False
runtime_activation_outcome             = exact built-in str == "no_go"
approval_intent_sha256                 = exact built-in str, lowercase hex64
```

`bool` as `int` is rejected. String/int/bool subclasses and custom equality objects are rejected.

**RTM-7c.4q exact digest closure:** `evidence_sha256` and `approval_intent_sha256` require
`type(value) is str` **and** lowercase hex64 via shared `_is_exact_hex64` — str subclasses with
matching content are rejected (`isinstance(value, str)` alone is insufficient).

## Detached payload snapshot (RTM-7c.4q closure)

On verifier entry, caller-owned dict is observed **once** via `tuple(payload.items())` and copied
into a detached built-in dict with exact built-in string keys only:

```text
1. type(payload) is dict          (exact built-in dict — subclass → not_object)
2. tuple(payload.items())         (single observation; RuntimeError/KeyError → unknown_field)
3. type(key) is str for each key  (before any set/hash membership on keys)
4. key in canonical field set
5. copy values into new built-in dict
6. all subsequent schema/semantic/hash reads use detached dict only
```

- Non-exact / non-string / unknown key → `approval_intent_unknown_field` (before missing-field)
- Missing canonical key → `approval_intent_missing_field`
- Caller mutation after snapshot cannot change verdict (point-in-time snapshot; not a concurrent
  atomicity guarantee)
- Malformed custom key objects fail closed — no raw exception escape; key `__hash__`/`__eq__`
  hooks are not invoked for non-exact keys (type guard precedes membership checks)
- `MemoryError` / `KeyboardInterrupt` / `SystemExit` re-raised; broad `BaseException` catch forbidden
- CLI verify mode (RTM-7c.4r H1): same three exceptions re-raised — not swallowed by `except Exception`

## Shared detached verification core (RTM-7c.4r)

Both `verify_operator_approval_intent_payload` and
`verify_and_snapshot_operator_approval_intent` call `_verify_detached_operator_approval_intent`
after a single `_snapshot_operator_approval_intent_payload`. The snapshot API does **not** call
the public verifier or re-read caller payload. See
`PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_INTENT_CONTRACT.md`.

## Canonical hash verification

Hash payload reuses `operator_approval_intent_hash_payload` (same 12 fields as builder; excludes
`approval_intent_sha256`). Recomputation uses `decision.canonical_json.payload_sha256`.

Hash format valid but recomputation mismatch → `INVALID` / `approval_intent_hash_mismatch`.

## Verifier API

```python
class OperatorApprovalIntentVerificationOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"

@dataclass(frozen=True)
class OperatorApprovalIntentVerification:
    outcome: OperatorApprovalIntentVerificationOutcome
    schema_version: int | None
    evidence_schema_version: int | None
    evidence_sha256: str | None
    approval_intent_sha256: str | None
    reason_codes: tuple[str, ...]

verify_operator_approval_intent_payload(payload: object) -> OperatorApprovalIntentVerification
```

Public API never raises except `MemoryError`, `KeyboardInterrupt`, `SystemExit`. Other unexpected
`Exception` → stable `INVALID` / `approval_intent_invalid_field`.

## Stable verifier reason codes

```text
approval_intent_not_object
approval_intent_unknown_field
approval_intent_missing_field
approval_intent_unsupported_schema
approval_intent_invalid_field
approval_intent_invalid_declared_at
approval_intent_invalid_evidence_binding
approval_intent_invalid_scope
approval_intent_invalid_declaration
approval_intent_invalid_activation_posture
approval_intent_hash_mismatch
```

Reason codes never embed raw field values, hashes, datetimes, paths, or exception text.

## Builder → verifier invariant

Every builder `CREATED` intent serialized to a JSON-compatible dict MUST verify `VALID`.
The verifier MUST NOT accept objects the builder would not emit under normal operation.

## CLI mode

11th mutually-exclusive mode:

```text
--verify-operator-approval-intent
```

Requires explicit `--json`. Forbidden: `--config`, `--max-age-microseconds`, three manual
confirmation flags.

Processing order:

```text
1. argparse / mode resolution
2. --run early refusal (exit 2)
3. verify-mode applicability (--json required; forbidden args)
4. bounded stdin read (1 MiB)
5. strict JSON parse (receipt stdin parser internally; external reasons mapped)
6. verify_operator_approval_intent_payload exactly once
7. sanitized JSON output
```

Early applicability/input failures occur before config/env/clock/DB/filesystem access.

### Stdin input reason mapping

Internal receipt parser reasons are mapped — never exposed verbatim:

```text
approval_intent_input_empty
approval_intent_input_not_utf8
approval_intent_input_not_json
approval_intent_input_too_deep
approval_intent_input_duplicate_key
approval_intent_input_too_large
approval_intent_input_read_error
```

Duplicate keys rejected at all nesting depths. `NaN` / `Infinity` / `-Infinity` rejected.
Pathological `ValueError` / `RecursionError` do not escape.

### CLI JSON output

VALID (exit 0):

```json
{
  "outcome": "VALID",
  "mode": "verify-operator-approval-intent",
  "schema_version": 1,
  "evidence_schema_version": 2,
  "evidence_sha256": "<hex64>",
  "approval_intent_sha256": "<hex64>",
  "reason_codes": [],
  "approval_intent_authenticated": false,
  "approval_intent_consumed": false,
  "approval_intent_persisted": false,
  "activation_authorized": false,
  "runtime_activation_outcome": "no_go"
}
```

INVALID / input failure (exit 1): digest fields null unless verification completed; stable
`reason_codes`; posture fields remain false / `"no_go"`.

Applicability failures use the same envelope with `reason_codes` such as:

```text
approval_intent_verification_json_required
approval_intent_verification_argument_not_applicable
approval_intent_verification_mode_conflict
```

### Mode conflict normalization (RTM-7c.4q)

When `--build-operator-approval-intent` or `--verify-operator-approval-intent` participates in
a mutually-exclusive mode conflict, the CLI emits an approval-specific JSON envelope instead of
the global `reason_code` invocation error:

- verify flag present → verify envelope / `approval_intent_verification_mode_conflict`
- build-only conflict → build envelope / `approval_intent_mode_conflict`
- other mode pairs → global `FAIL` / `reason_code` (unchanged)

## Isolation contract (verify mode)

Per call:

```text
stdin read   = 1
verifier     = 1
load_settings = 0
env access   = 0
clock read   = 0
SQLite       = 0
filesystem write = 0
pipeline     = 0
intent builder = 0
```

Early input failure: verifier = 0; config/env/clock/DB = 0.

## Output prohibitions

Never emit on stdout/stderr:

- raw intent JSON (on invalid input)
- raw evidence hash (on invalid input)
- raw datetime strings from invalid payloads
- exception type/message/repr
- traceback
- config path
- `/home/`, `KIS_`, `APP_KEY`, `APP_SECRET`
- SQLite raw error text

Verified digests **may** appear on VALID paths and on hash-mismatch paths where verification
completed.

## Related contracts

- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CONTRACT.md` — builder + shared scalar/hash helpers
- `PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_INTENT_CONTRACT.md` — immutable verified snapshot (RTM-7c.4r)
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CLI_CONTRACT.md` — build CLI (RTM-7c.4p)
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — CLI wiring root
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — attended activation posture inventory

## Still OPEN (unchanged posture)

Intent persistence, approval consumption, signing/HMAC, Operator identity authentication,
evidence database lookup, approval pre-consumption revalidation, activation token/epoch, `--run`,
KIS/network, broker/order, operational DB write, replay/nonce/idempotency, daemon/scheduler,
unattended pilot, writer-stop machine proof, default runtime activation beyond constant NO-GO.
