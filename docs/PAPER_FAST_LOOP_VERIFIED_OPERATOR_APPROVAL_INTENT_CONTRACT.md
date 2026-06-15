# Verified Operator Approval Intent Contract (RTM-7c.4r)

Immutable **verified snapshot** of a schema/semantic/hash-valid Operator approval-intent payload.
Freezes the 13 canonical scalar fields so a future consumer can observe the same verified intent
without retaining or re-reading the caller-owned mutable JSON object.

**Runtime activation: NO-GO.** A verified snapshot is a validation observation — not Operator
identity authentication, signature/HMAC, evidence content revalidation, approval consumption,
replay/nonce/idempotency, persistence, or activation authorization.

Code:

- `composition.operator_approval_intent_verifier.verify_and_snapshot_operator_approval_intent`
- shared core `_verify_detached_operator_approval_intent` (also used by the standalone verifier)

## What a verified snapshot means

> The input passed strict schema v1, semantic posture, and canonical hash parity; the returned
> `VerifiedOperatorApprovalIntent` holds exact built-in immutable scalars copied from that
> single detached observation.

## What a verified snapshot does **not** mean

- Operator identity authentication
- Signature / HMAC authenticity
- Evidence content revalidation (only digest binding was checked)
- Approval consumption
- Replay / nonce / idempotency
- Freshness judgment
- Persistence (no file/DB write)
- Activation authorization (`activation_authorized` stays `false`; `runtime_activation_outcome`
  stays `"no_go"`)

## Snapshot model (exactly 13 fields)

```python
@dataclass(frozen=True)
class VerifiedOperatorApprovalIntent:
    schema_version: int
    declared_at: str
    evidence_schema_version: int
    evidence_sha256: str
    market: str
    symbol: str
    approval_scope: str
    operator_approval_declared: bool
    writers_stopped_manually_confirmed: bool
    live_orders_forbidden_confirmed: bool
    activation_authorized: bool
    runtime_activation_outcome: str
    approval_intent_sha256: str
```

Field set matches schema v1 exactly. Snapshot excludes: raw payload, original dict, config/path,
verification exception, evidence body, authentication state, consumption state, persistence state.

## Result model

```python
class VerifiedOperatorApprovalIntentResult:
    outcome: OperatorApprovalIntentVerificationOutcome  # valid | invalid
    reason_codes: tuple[str, ...]
    snapshot: VerifiedOperatorApprovalIntent | None
```

Invariants:

```text
VALID   → reason_codes == (), snapshot is not None
INVALID → len(reason_codes) == 1, snapshot is None
```

## Single-pass shared verification core

Both public APIs share one detached verification path:

```text
caller payload
→ _snapshot_operator_approval_intent_payload (exactly once)
→ _verify_detached_operator_approval_intent (schema + semantic + hash, exactly once)
```

**RTM-7c.4s carry-over H1:** `_verify_detached_operator_approval_intent` delegates all semantic
field checks to `validate_operator_approval_intent_scalars_detailed` exactly once (no duplicate
declared-at / evidence-binding / identity / declaration / posture helpers). Hash recomputation
remains a separate single pass after scalar validation succeeds.

| API | Returns |
|-----|---------|
| `verify_operator_approval_intent_payload` | verification verdict only |
| `verify_and_snapshot_operator_approval_intent` | verdict + immutable snapshot |

Forbidden:

- `verify_and_snapshot` calling the public verifier then re-reading raw payload
- duplicate snapshot / schema validation / hash recomputation
- caller payload access after snapshot

Per normal call:

```text
payload snapshot       = 1
schema/semantic validation = 1  (validate_operator_approval_intent_scalars_detailed)
hash recomputation     = 1
public verifier recall = 0   (snapshot API path)
```

(Pre-4s duplicate per-field semantic helpers removed — single detailed owner.)

## Caller mutation isolation

Snapshot helper observes `tuple(payload.items())` once into a detached built-in dict. Caller
mutation after snapshot cannot change verdict or returned snapshot values (point-in-time
observation; not a concurrent atomicity guarantee).

## Exception contract

Both public APIs:

| Input / condition | Behavior |
|-------------------|----------|
| Malformed / semantic-invalid input | stable `INVALID` + single reason |
| `MemoryError` / `KeyboardInterrupt` / `SystemExit` | re-raised |
| Other unexpected `Exception` | stable `INVALID` / `approval_intent_invalid_field` |

No raw exception text, traceback, or path escape.

CLI `--verify-operator-approval-intent` applies the same re-raise rule for
`MemoryError` / `KeyboardInterrupt` / `SystemExit` (RTM-7c.4r carry-over H1).

## Builder → snapshot invariant

Every builder `CREATED` intent serialized to a JSON-compatible dict MUST produce
`VALID` + snapshot whose 13 fields exactly match the builder intent.

## Verifier ↔ snapshot parity

For the same payload, `verify_operator_approval_intent_payload` and
`verify_and_snapshot_operator_approval_intent` MUST agree on:

```text
outcome
reason_codes
verified schema/digest metadata (verifier fields on VALID / hash-mismatch paths)
```

VALID adds immutable snapshot only on the snapshot API.

Stable invalid reason codes are identical to the standalone verifier (see
`PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_VERIFICATION_CONTRACT.md`).

## Still OPEN (unchanged posture)

Intent persistence, approval consumption, signing/HMAC, Operator identity authentication,
evidence database lookup, approval pre-consumption revalidation, activation token/epoch, `--run`,
KIS/network, broker/order, operational DB write, replay/nonce/idempotency, daemon/scheduler,
unattended pilot, writer-stop machine proof, default runtime activation beyond constant NO-GO.

## Related contracts

- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_VERIFICATION_CONTRACT.md` — standalone verifier
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CONTRACT.md` — builder + shared scalar/hash helpers
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CLI_CONTRACT.md` — build CLI
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — attended activation posture inventory
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_CONTRACT.md` — RTM-7c.4s eligibility preflight (consumes the verified snapshot)
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md` — RTM-7c.4t canonical eligibility artifact (observation, not consumption)
