# Offline Paper Fast-Loop Composition Contract (RTM-7c.4a)

Offline composition root + operator verification tooling only.
**Runtime activation: NO-GO.** `--run` is refused (`outcome=NO_GO`,
`reason_code=live_run_not_implemented`) before any credential read, network
socket, production-DB write, or runtime-directory creation.

Attended one-shot activation is **not implemented** — see
`docs/PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` (RTM-7c.4f).

This lane builds the offline wiring and the operator's read-only/replay tools.
It does NOT turn on a live runtime. Even when every gate is green, the change is
left for the operator to commit.

## Scope (hard boundaries)

- Single KR symbol, 6-digit **ASCII** code (`[0-9]{6}`), `PAPER` account role, `KRW` currency.
- Network calls: **0**. KIS frames/transport: **0**. Credential reads: **0**.
- Production runtime DB writes: **0**. Runtime directory creation: **0**.
- Slow loop is manual/offline; a prepublished active decision is assumed.
- No scheduler process, no daemon, no calendar auto-exec, no migration, no
  journal reconcile.

## Package layout

`src/composition/` is the **only** place allowed to wire broker + ledger +
execution + orchestration into a runnable stack. `src/orchestration` purity is
preserved — orchestration never imports broker/ledger/execution directly; the
composition root does.

- `src/composition/sqlite_inspector.py` — strictly read-only SQLite inspection
  (`file:...?mode=ro` URI + `PRAGMA query_only = ON`). Never constructs
  `SQLiteLedger` / `SqliteTriggerJournal` / `ActiveDecisionStore` (their
  constructors create/migrate schema), never writes, never returns raw payloads,
  credentials, exception reprs, or tracebacks. Output is sanitized counts plus a
  small set of non-secret identifiers (`decision_id` / `plan_id`) and
  integer-quantity strings. None of the inspected DBs store credentials.
  - **Quiescent-only, sidecar-creating-free reads.** A plain `mode=ro` open of a
    WAL-mode database materializes a `-shm`/`-wal` sidecar even for a reader
    (SQLite takes a WAL read lock via shared memory), which would mutate the
    operator's filesystem. `open_read_only` therefore appends `immutable=1`
    **only when the database is already quiescent** (no live
    `-wal`/`-shm`/`-journal` sidecar): for a quiescent file `immutable=1` reads
    the main file directly and creates no sidecar, and is safe precisely because
    nothing else holds the file. When a sidecar *is* present the DB is
    non-quiescent — the inspection fails closed with
    `database_not_quiescent:<db>` — so a faithful `mode=ro` read is kept there
    rather than blindly ignoring the live WAL with `immutable=1`. The inspector
    never creates or deletes a sidecar.
- `src/composition/paper_fast_loop.py` — the composition root. Three offline
  capabilities (below).
- `ops/run_paper_fast_loop.py` — operator CLI over the three capabilities plus
  the refused `--run`.

## Three offline capabilities

### `build_paper_fast_loop_plan` (validate-only, CLI default)

Pure config + snapshot validation. Loads + validates the on-disk
execution-inputs snapshot and checks the validity window. **Opens no database**
(no ledger/journal/active-store access, and the ledger file is never created or
scanned) — validate-only is fully side-effect free. Position/account-role/
currency preflight lives in `inspect_paper_fast_loop`. Returns
`PaperFastLoopPlan` with outcome `READY` / `NOT_READY` and sanitized reason
codes:

- snapshot: `snapshot_file_missing`, `snapshot_not_yet_valid`,
  `snapshot_expired`, plus any typed `reason_code` surfaced by the snapshot
  loader (e.g. `snapshot_allocator_created_after`).

No execution, no DB open/create/writes, no network.

### `inspect_paper_fast_loop` (read-only, fail-closed inspection)

Read-only summaries of the configured ledger / trigger journal /
active-decision-store via `sqlite_inspector`. Returns a typed
`PaperFastLoopInspection` with `outcome` (`InspectionOutcome.OK` / `NO_GO`) and
sanitized `reasons`. The verdict is `NO_GO` (and the CLI exits non-zero) on any
of:

- `missing_database:<ledger|trigger_journal|active_decision_store>` — a required
  DB file does not exist (fail-closed, not fail-open).
- `database_not_quiescent:<db>` — a live `-wal`/`-shm`/`-journal` sidecar is
  present, so a clean read-only snapshot cannot be proven (read-only inspection
  is trustworthy only against a quiescent DB).
- `<db>_missing_table:<t>` / `<db>_missing_column:<t>.<c>` — required schema is
  absent (DB is not the expected store), checked via read-only `PRAGMA`.
- `<db>_unreadable:<reason_code>` — a sanitized sqlite open/read failure
  (`SqliteInspectionError`); never raw exception text or a traceback.
- `dangling_active_pointer` — an `active_decision_pointers` row whose
  `publication_id` has no matching `decision_bundle_versions` row (LEFT-JOIN
  detection distinguishes corruption from "正常적으로 active 없음").
- `nonterminal_journal_entries` — any `reserved`/`dispatching` (in-flight/
  crashed) journal row.
- position preflight: `unsupported_market`, `unsupported_account_role`,
  `unsupported_currency`, `foreign_position_present` (any position whose
  symbol ≠ the configured symbol).

#### Execution-inputs snapshot readiness

The on-disk execution-inputs snapshot is read (loaded fail-closed; a hash
mismatch never loads) and its validity window checked against the caller-supplied
timezone-aware `now`:

- `missing_execution_inputs_snapshot`, `execution_inputs_hash_mismatch`,
  `execution_inputs_universe_mismatch`, `execution_inputs_invalid` (any other
  loader failure), `execution_inputs_not_yet_valid`, `execution_inputs_expired`.

#### Active-decision readiness (pointer ↔ version ↔ bundle identity)

The configured `(market, symbol)` active pointer is reconciled read-only,
replicating `ActiveDecisionStore._row_to_active_bundle` **without constructing the
store** (its `__init__` creates/migrates schema). Identity is verified end to
end: the configured pointer key, the referenced version's `market`/`symbol`
columns, the bundle's internal `decision.market`/`decision.symbol`/
`decision_id`/`created_at`, and (when present) the plan's
`market`/`symbol`/`decision_id` must all agree — a pointer referencing a foreign
version is `identity_mismatch` even if that version is itself internally
consistent. Validity is treated as **integrity, not best-effort**:
`valid_from`/`expires_at` must be present, ISO-parseable, timezone-aware, and
satisfy `valid_from <= expires_at`; a missing/unparseable/naive/reversed value is
corruption (never silently downgraded to "currently valid").

**Full model-restoration parity (no drift):** the stored bundle payload is run
through `orchestration.active_decision_store.deserialize_validated_bundle` — the
*same* pure helper the runtime reader uses — so it must restore into a valid
`DecisionTriggerBundle` (`AnalysisDecision` + `TriggerPlan` model validation,
plan/decision time binding). A payload that clears the hash / publication-id /
identity / validity checks but is **not** a restorable bundle (incomplete model,
an unknown action, plan/decision time binding, etc.) is `active_bundle_corrupt`,
never reported as integrity-OK — inspect can never accept a bundle the runtime
reader would reject. **Plan-presence consistency is classified separately** (see
`active_plan_consistency_mismatch` below): a *recognized* action carrying the
wrong plan presence is checked **before** model restoration, so it surfaces as the
distinct, actionable plan-consistency reason rather than being collapsed into the
generic corrupt bucket. The required schema also includes the columns the runtime
reader reads (`source_payload_hash`, `published_at`); a store missing them fails
the schema check rather than being treated as inspectable. Beyond presence,
`published_at` is **value-validated** for runtime-reader parity — it must be a
present, ISO-parseable, timezone-aware datetime (the runtime reader parses it as
one, and it is *not* part of the hashed bundle payload, so a malformed/naive value
would be unreadable at activation time yet invisible to the hash check) — a bad
value is `active_bundle_corrupt`. Reasons:

- `missing_active_decision` — no pointer for the configured key.
- `active_pointer_identity_mismatch` — pointer/version/bundle/plan identity
  disagree (distinct from `dangling_active_pointer`, which is a pointer to a
  missing version row).
- `active_bundle_corrupt` — bundle JSON / hash / publication-id / validity
  columns / validity datetimes / `published_at` datetime / **full model
  restoration** (including an unknown action) do not reconcile. A missing-version
  pointer is **not** reported here: it is exactly one `dangling_active_pointer`
  (the store summary detects it via LEFT JOIN), never also `active_bundle_corrupt`.
  A *recognized* action with the wrong plan presence is **not** reported here
  either: it is exactly one `active_plan_consistency_mismatch` (below).
- `active_decision_not_yet_valid`, `active_decision_expired` — `now` is outside a
  well-formed validity window.
- `active_execution_universe_mismatch` — active decision universe ≠ snapshot
  universe.
- `active_plan_consistency_mismatch` — a recognized action carries the wrong plan
  presence (BUY/SELL without a plan, or HOLD with one). Checked **before** model
  restoration so it stays distinct from `active_bundle_corrupt`; an *unknown*
  action is `active_bundle_corrupt`, not this.

A single root cause emits a single reason — no duplicate/contradictory codes for
the same fault.

Ledger summary reports per-`OrderStatus` counts (`order_result_count`,
`filled_result_count`, `rejected_result_count`, `pending_result_count`,
`cancelled_result_count`) — never a non-existent `COMMITTED` status. `now` must
be timezone-aware (the CLI reads it once and passes it; a naive `now` raises
`ValueError`). No writes, no schema creation, no reconciliation, no network.

### `replay_offline` (deterministic offline replay)

Deterministic replay of the fast-loop execution stack against a built-in
normalized-event fixture, using **caller-provided temp paths only** — never the
configured `runtime/` paths. Raises `ValueError` for an unknown fixture or a
missing temp dir.

Execution inputs flow through the **real** `ValidatedExecutionInputsProvider`:
replay writes a canonical hash-stamped snapshot into the temp dir (or uses a
caller-supplied `snapshot_path`), loads it fail-closed via
`load_execution_inputs_snapshot`, and feeds it to the
`FastLoopExecutionOrchestrator`. A snapshot that fails to load returns a
zero-execution result with `snapshot_loaded=False` and a sanitized
`snapshot_reason`; a snapshot that loads but is stale/universe-mismatched at the
event time yields `first_status=execution_inputs_unavailable` and zero fills.

Replay drives **three phases** on the *same* on-disk databases to prove
idempotency, surfaced as `first_status` / `repeat_status` / `restart_status`:

1. first event → `committed`,
2. repeat event on the same stack (within-arming) → `suppressed`
   (max-fires-per-decision),
3. event after rebuilding the whole stack against the same DBs (composition
   restart) → `skipped_terminal` (the same idempotency key is already terminal
   in the journal, so the journal dedups it).

`buy_fill` net result: `committed_count=1`, `order_result_count=1`,
`filled_result_count=1`, `fill_count=1`, final position quantity `"57"`, final
cash `"96010000"`, exactly **1** terminal journal row (`committed`), **0**
nonterminal rows. `AVAILABLE_REPLAY_FIXTURES = ("buy_fill", "hold_noop")`:

- `buy_fill` — BUY decision + `LAST_TRADE_PRICE <= 70000` plan → the result
  above; the repeat/restart phases add no duplicate fill.
- `hold_noop` — HOLD decision, no plan → no fill, no position.

Determinism anchors: KST date 2026-06-16, OPEN window, decision at 09:00, events
at 09:30, price 70000, threshold 70000, universe `KR_LARGE`, NAV 100M, target
weight 4% → 57 shares.

#### Replay health/quote preconditions (why the stack must be primed)

`is_execution_ready == is_healthy == transport HEALTHY AND market_data HEALTHY`:

1. Transport HEALTHY needs connected + all_subscribed **and**
   `since_connect >= minimum_stable_uptime_seconds`. Replay records
   connect/all_subscribed at the earlier decision time so uptime accrues.
2. Market-data HEALTHY (when the session expects quotes) needs a recorded QUOTE
   (`_last_quote_at`); a trade alone yields starvation. Replay records a
   `best_bid_ask` market event.
3. The coordinator must be able to price, so replay seeds a
   `NormalizedBestBidAsk` quote into the latest store before applying the trade
   (the quote is seeded, not routed as an applied update), avoiding
   `quote_unavailable` / `FAILED_CLOSED`.

## Wiring path (replay)

```
TriggerOrderBridge(journal, generator=OrderIntentGenerator(),
                   resolver=QuantityResolver(), broker=PaperBrokerAdapter,
                   ledger=SQLiteLedger)
  → PaperExecutionCoordinator(engine=TriggerEngine(), bridge,
        portfolio_context_service=PaperPortfolioContextService(
            ledger_source=ledger, market_state_source=adapter))
  → FastLoopExecutionOrchestrator(active_reader, latest_store, rolling_store,
        execution_gate=SessionHealthExecutionGate(calendar, tracker),
        execution_inputs_provider=StaticExecutionInputsProvider(...), coordinator)
```

All SQLite databases live under the caller-provided temp dir.

### Resource lifecycle (`PaperFastLoopStack`)

The composed stack owns three durable SQLite handles (ledger / trigger journal /
active-decision-store); the in-memory stores need no teardown. `close()` releases
every handle exactly once (idempotent) and attempts all three even if one raises,
re-raising the first error, so the temp dir is deletable with zero pending handles
(Windows-safe). Teardown closes in **construction-reverse order**
(active-store → journal → ledger), identical on both the normal `close()` path and
the partial-construction cleanup path. `build_offline_paper_fast_loop_stack` is a
context manager that always closes on exit (including on a body exception).

Construction is fail-closed: if any step after the first SQLite handle is opened
raises — a later store constructor or an in-memory dependency — `_build_stack`
closes every already-opened handle in that same reverse order (active-store →
journal → ledger) and re-raises the **original** construction exception; a
cleanup-time `close` failure is swallowed so it never masks the original error. A
partial construction therefore never leaks a handle. Replay's composition-restart
phase fully closes the first stack before opening the next against the same files.
The resource-lifecycle tests assert the exact teardown order via a shared ordered
close-event log across all three handles (not merely a per-handle close count).

## CLI contract (`ops/run_paper_fast_loop.py`)

Mutually-exclusive modes (default `--validate-only`):

| mode | exit code | notes |
|------|-----------|-------|
| `--validate-only` | 0 if `PASS` (READY), else 1 | snapshot only (no DB open) |
| `--inspect-existing` | 0 if `PASS` (`OK`), else 1 (`NO_GO`) | read-only, fail-closed |
| `--precheck-runtime` | 0 if machine `PASS`, else 1 (`NO_GO`) | read-only attended precheck; machine PASS is **not** an activation authorization; JSON includes nested `precheck_receipt` (RTM-7c.4d — see `PAPER_FAST_LOOP_PRECHECK_RECEIPT_CONTRACT.md`) |
| `--verify-precheck-receipt` | 0 if `VALID`, else 1 | stdin-only receipt schema + hash verification; no config/env/DB/fs write (RTM-7c.4e — see `PAPER_FAST_LOOP_PRECHECK_RECEIPT_VERIFICATION_CONTRACT.md`) |
| `--revalidate-activation-candidate` | 0 if mechanical `PASS`, else 1 (`NO_GO`) | stdin receipt + config (`environ={}`); read-only approval-time state revalidation; mechanical PASS is **not** activation authorization (RTM-7c.4g — see `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_REVALIDATION_CONTRACT.md`) |
| `--final-preflight-activation-candidate` | 0 if mechanical `PASS`, else 1 (`NO_GO`) | stdin receipt + config (`environ={}`); composes 4g revalidation + policy-neutral receipt time observation (exact `receipt_age_microseconds`; future `checked_at` fail-closed) + fresh current-time precheck (`now=datetime.now(tz=KST)`); catches byte-identical time-window expiry; the untrusted receipt is verified and frozen into one immutable snapshot **once** and both the revalidation and receipt-time stages read that same snapshot (RTM-7c.4j — receipt verifier called exactly once per preflight, no cross-stage mixed observation); path-free summary (no `config` field); `freshness_policy_evaluated=false` always — explicit max-age evaluation is **not** composed into this CLI mode; mechanical PASS is **not** activation authorization (RTM-7c.4h + 7c.4i + 7c.4j — see `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FINAL_PREFLIGHT_CONTRACT.md`, `PAPER_FAST_LOOP_RECEIPT_TIME_ASSESSMENT_CONTRACT.md`, `PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md`) |
| `--freshness-preflight-activation-candidate` | 0 if freshness-qualified mechanical `PASS`, else 1 (`NO_GO`) | stdin receipt + config (`environ={}`) + **required** `--max-age-microseconds` (strict entire-token ASCII decimal parser via `re.fullmatch` — rejects trailing newline / all whitespace / non-`str` / over-long-token `ValueError`; no default/config/env threshold); composes verified final preflight + explicit freshness policy (`now=datetime.now(tz=KST)`); path-free summary; on a freshness-qualified PASS emits optional `candidate_evidence_sha256` / `candidate_evidence_schema_version` (canonical digest, RTM-7c.4n; `null` on `NO_GO`/`STALE`/input failure; qualified preflight run once, same `now`); mechanical FRESH PASS is **not** activation authorization (RTM-7c.4m/4n — see `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FRESHNESS_PREFLIGHT_CONTRACT.md`, `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_EVIDENCE_CONTRACT.md`) |
| `--build-operator-approval-intent` | 0 if combined PASS + intent CREATED, else 1 (`NO_GO`/`FAIL`) | stdin receipt + **explicit** `--config` (no default fallback) + **explicit** `--json` (stdout JSON only) + **required** `--max-age-microseconds` + three explicit manual confirmation flags; composes freshness-qualified evidence then `build_operator_approval_intent` once; stable envelope `mode=build-operator-approval-intent` with `reasons` list only; input/config failure → `FAIL`, mechanical rejection → `NO_GO` (RTM-7c.4p — see `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CLI_CONTRACT.md`) |
| `--verify-operator-approval-intent` | 0 if `VALID`, else 1 | stdin-only approval-intent schema + hash verification; **explicit** `--json`; no config/env/DB/fs write/clock read; detached payload snapshot + exact built-in hex64 digests; `MemoryError`/`KeyboardInterrupt`/`SystemExit` re-raised (RTM-7c.4q + 7c.4r H1 — see `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_VERIFICATION_CONTRACT.md`) |
| `--verify-approval-consumption-eligibility-artifact` | 0 if `VALID`, else 1 | stdin-only serialized eligibility-artifact schema·semantic·hash verification; **explicit** `--json`; forbidden `--config`/`--max-age-microseconds`/confirmation flags (→ `eligibility_artifact_verification_argument_not_applicable`); no config/env/DB/fs write/clock read/builder/eligibility/intent/evidence rerun; verifier called exactly once; consistency-not-authenticity (Category C recomputed → VALID); constant NO-GO; `MemoryError`/`KeyboardInterrupt`/`SystemExit` re-raised (RTM-7c.4v — see `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_VERIFICATION_CLI_CONTRACT.md`) |
| `--replay FIXTURE` | 0 (1 on unknown fixture) | OS temp dir only |
| `--run` | **2** | **REFUSED** before any side effect (early refusal precedes mode resolution, applicability, stdin read) |

- Mode collisions → exit 1 (except `--run`, which is refused with exit 2 before conflict
  resolution). When `--verify-approval-consumption-eligibility-artifact` participates, emit the
  artifact-specific FAIL envelope (`outcome=FAIL`,
  `reason_codes=["eligibility_artifact_verification_mode_conflict"]`, full stable artifact key
  set + constant NO-GO posture, all metadata null) — this takes precedence over the
  approval-intent envelope. Otherwise when `--build-operator-approval-intent` or
  `--verify-operator-approval-intent` participates, emit the approval-specific JSON envelope
  (`approval_intent_mode_conflict` / `approval_intent_verification_mode_conflict`); otherwise
  global `reason_code` containing `mutually exclusive`.
- `--max-age-microseconds` is valid **only** with `--freshness-preflight-activation-candidate`;
  on any other mode → exit 1, `reason_code=freshness_policy_argument_not_applicable`
  (existing mode behavior unchanged). On the freshness mode without the argument → exit 1,
  `freshness_policy_input_missing`. Invalid token → exit 1, `freshness_policy_input_invalid`
  before stdin/config/clock/DB access.
- `inspect-existing` / `replay` internal failures are caught in `main()` and
  emitted as a sanitized `outcome=FAIL` / `reason_code=<inspect|replay> error:
  <ExceptionType>` with exit 1 — never a traceback or raw sqlite text.
- `--run` emits `outcome=NO_GO`, `reason_code=live_run_not_implemented`,
  `activation_authorized=false`, `runtime_activation_outcome="no_go"`,
  `credential_read/network_called/production_db_touched/filesystem_written` all
  `false`, and returns exit **2** **before** loading settings, reading any
  credential, opening any socket, touching the production DB, or creating any
  path.
- `--json` emits a sanitized machine-readable summary. Credentials, raw frames,
  exception reprs, tracebacks, and DB dumps are never printed.

## Import boundary (guarded)

`tests/test_composition_import_guard.py` walks every `src/composition/*.py` and
`ops/run_paper_fast_loop.py` and asserts:

- **Forbidden, even at the root:** `socket`, `websocket`, `websockets`, `http`,
  `httpx`, `urllib`, `requests`, `data`, `llm` (network/transport/credential/
  live-data/LLM surfaces).

### RTM-7c.4l / 4m — freshness-qualified preflight

API: `freshness_qualify_activation_candidate` (required explicit `ReceiptFreshnessPolicy`).
CLI (RTM-7c.4m): `--freshness-preflight-activation-candidate` with required
`--max-age-microseconds`. The parser validates the **entire token** as ASCII decimal
(`re.fullmatch`), rejecting all whitespace (incl. a trailing newline) and any non-`str` /
`str`-subclass object, and normalizes an integer-conversion `ValueError` (over-long token) to
`freshness_policy_input_invalid` without leaking a traceback — a rejected over-long token is a
CLI-input-invalid event, not a max-age upper-bound policy. No default/config/env threshold.
Invalid max-age fails closed before stdin read, config load, env access, clock read, DB/
filesystem access, and freshness evaluation. Closure processing order: max-age parse → policy
snapshot → shared `now` guard → receipt snapshot → verified final core → freshness evaluation
(snapshot policy).
`--final-preflight-activation-candidate` and the policy-neutral wrapper remain unchanged
(`freshness_policy_evaluated=false`). See
`PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FRESHNESS_PREFLIGHT_CONTRACT.md`.

### RTM-7c.4n — canonical candidate evidence

API: `build_activation_candidate_evidence(*, qualified_result, evaluated_at)` and the
composition wrapper `freshness_qualify_and_build_candidate_evidence` (runs the qualified
preflight once, then builds evidence on PASS only, reusing the same `now`). A qualified
PASS/FRESH is frozen into one immutable `ActivationCandidateEvidence` (schema version 2;
`evidence_sha256` via `decision.canonical_json.payload_sha256` over the 15 non-digest fields,
which bind **both** the original candidate `receipt_sha256` and the fresh-precheck
`fresh_precheck_receipt_sha256`) **only** when the outer/final/freshness/time-assessment
observations are mutually consistent — matching identity (sha/market/symbol), one agreed receipt
age across all three stages, a policy-neutral final preflight (`final.reasons == ()`,
`final.freshness_policy_evaluated is False`) + explicit FRESH freshness, a policy-neutral time
assessment (`time_assessment.freshness_policy_evaluated is False` — the policy verdict belongs
only to the freshness evaluation, so the nested semantic roles never overlap), a constant NO-GO
posture on every nested stage, an `evaluated_at` of exact `type datetime` whose exact integer
microseconds from the verified `checked_at` equal the observed age, **and** the bound *actual*
machine-proof result objects — a real `revalidation_result` PASS and a real
`current_precheck_result` PASS (exact types, OK inspection, fresh receipt with
`checked_at == evaluated_at.isoformat()` and full shared
`validate_runtime_precheck_receipt_object` schema/semantic/hash validation), sharing one
canonical 4-artifact observation held identical from revalidation through the fresh precheck.
Hash equality alone is insufficient. Any mismatch — including a final PASS that still carries
failure reasons, a time assessment that claims to have evaluated a freshness policy, a
boolean-only `fresh_precheck_executed=True` with `None` machine-proof objects, unsupported
fresh-receipt schema, invalid market/symbol, or semantically malformed fingerprints with a
matching hash — fails closed to `INVALID` (RTM-7c.4n consistency + nested PASS semantic + fresh
machine-proof binding + fresh receipt verifier-parity closure).
The wrapper returns a combined `FreshnessQualifiedEvidenceOutcome`: a
qualified PASS is combined `PASS` only when evidence is `CREATED`; a qualified PASS whose
evidence is not created is combined `NO_GO` with `candidate_evidence_generation_invalid` (no
upstream rerun); a qualified `NO_GO`/`STALE` keeps its reasons and produces no digest. The CLI
`--freshness-preflight-activation-candidate` `outcome`/exit follow the combined verdict: PASS
JSON adds `candidate_evidence_sha256` / `candidate_evidence_schema_version` (both `null` on
combined `NO_GO`/`STALE`/evidence failure/input failure); the qualified preflight is not re-run
and no extra clock is read. The digest is **not** authenticity, signing, approval, an
activation token, or activation authorization, and is never persisted. See
`PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_EVIDENCE_CONTRACT.md`.

### RTM-7c.4o — canonical Operator approval intent (API-only)

API: `build_operator_approval_intent(*, combined_result, declared_at,
operator_approval_declared, writers_stopped_manually_confirmed,
live_orders_forbidden_confirmed)`. Binds one combined `PASS` whose evidence is `CREATED` and
passes the full schema-v2 semantic contract via shared
`validate_activation_candidate_evidence_scalars` (matching hash alone is insufficient; both
receipt hashes lowercase hex64; PASS/FRESH outcomes; all observation flags exact `True`;
receipt age `<= max_age`) plus three exact-`True` manual declarations and a timezone-aware
caller `declared_at` (`declared_at >= evidence.evaluated_at`) into one immutable
`OperatorApprovalIntent` (`approval_intent_sha256` via `decision.canonical_json.payload_sha256`
over 12 non-digest fields; fixed `approval_scope="attended_paper_fast_loop_candidate"`).
Production validation does not use `asdict`/`deepcopy` on caller evidence. Combined `NO_GO` →
`NOT_ELIGIBLE`; a contradictory combined `PASS`, semantically invalid evidence, hash mismatch,
non-exact declarations, invalid `declared_at`, or qualified identity/posture mismatch →
`INVALID`. **Declared-time snapshot:** `snapshot_declared_at` freezes caller `declared_at` via
one `isoformat()` call; stateful tzinfo cannot escape after snapshot. **Qualified consistency:**
combined `PASS` requires qualified `PASS` identity/posture matching validated evidence.
**Strict scalar comparison:** PASS reasons exact empty tuple; qualified strings exact built-in
`str` before equality — no caller comparison hooks. Pure function: no clock read, no verifier/precheck/evaluator/evidence-builder re-invocation, no
persistence. Intent is **not** identity, signature, writer-stop machine proof, approval
consumption, replay prevention, or activation authorization. The existing CLI
`--freshness-preflight-activation-candidate` is unchanged (evidence fields only). See
`PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CONTRACT.md`.

### RTM-7c.4r — immutable verified Operator approval-intent snapshot (API-only)

API: `verify_and_snapshot_operator_approval_intent(payload)` →
`VerifiedOperatorApprovalIntentResult` with frozen 13-field `VerifiedOperatorApprovalIntent` on
VALID. Shares `_verify_detached_operator_approval_intent` with
`verify_operator_approval_intent_payload` — one detached payload snapshot, one
schema/semantic/hash pass per call; snapshot API does not re-call the public verifier or
re-read caller payload. Raw intent dict not retained; caller mutation after snapshot cannot
change verdict or snapshot values. Not authentication, signature, consumption, persistence, or
activation authorization. CLI verify mode carry-over H1: `MemoryError`/`KeyboardInterrupt`/
`SystemExit` not swallowed. No new CLI mode. See
`PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_INTENT_CONTRACT.md`.

### RTM-7c.4s — Operator approval consumption eligibility preflight (API-only)

`assess_operator_approval_consumption_eligibility(*, intent_payload, evidence, now)` — pure
preflight over one verified intent snapshot + one validated `ActivationCandidateEvidence`. Judges
digest/identity binding and time ordering only; **does not consume approval**. No consumed
marker, replay protection, persistence, authentication, TTL/freshness re-evaluation, or activation
authorization. Constant NO-GO posture on every path. Carry-over H1: intent semantic validation
single owner (`validate_operator_approval_intent_scalars_detailed`). See
`PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_CONTRACT.md`.

### RTM-7c.4u — standalone serialized eligibility-artifact verification + verified snapshot (API-only)

`verify_operator_approval_consumption_eligibility_artifact_payload(payload)` and
`verify_and_snapshot_operator_approval_consumption_eligibility_artifact(payload)` — pure
verification of an untrusted **serialized** eligibility artifact (4t output round-tripped through
JSON). Strict `type(payload) is dict` root, exact 13-field set, exact scalar types, semantic
constants, aware-timestamp ordering, and canonical `eligibility_artifact_sha256` recomputed over
the actual 12 serialized content fields; VALID converts to a frozen 13-field
`VerifiedOperatorApprovalConsumptionEligibilityArtifact`. The builder and verifier share a single
content semantic owner
(`validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed`, wrapped by
the full `validate_operator_approval_consumption_eligibility_artifact_scalars_detailed`) and the
canonical hash owner (`operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars`);
neither re-implements content-field semantics inline. One detached
payload snapshot + one scalar/semantic + one hash pass per call; snapshot API does not re-call the
public verifier or re-read caller payload. The verifier is a **consistency checker, not an
authenticator**: a semantically valid content change with a correctly recomputed digest is VALID by
design, while malformed input or a stale stored digest is INVALID — VALID never implies
authenticity/provenance. Not actual consumption, consumed marker, replay
protection, signing/HMAC, authentication, persistence, TTL/freshness re-evaluation, or activation
authorization. Constant NO-GO. `MemoryError`/`KeyboardInterrupt`/`SystemExit`
re-raised. See `PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md`.

### RTM-7c.4v — operator-facing eligibility-artifact verification CLI (stdin-only, read-only)

`ops/run_paper_fast_loop.py --verify-approval-consumption-eligibility-artifact --json` exposes the
4u `verify_operator_approval_consumption_eligibility_artifact_payload` API (called **exactly once**)
as a mutually-exclusive operator CLI mode. Stdin-only via the bounded strict JSON parser (1 MiB,
`read(limit+1)`, exact JSON-object root); parser receipt-namespace reasons are mapped into the
`eligibility_artifact_input_*` namespace. Required `--json`; forbidden `--config`,
`--max-age-microseconds`, and the three confirmation flags (→
`eligibility_artifact_verification_argument_not_applicable`); missing `--json` →
`eligibility_artifact_verification_json_required`. `--run` is refused with exit 2 **before** mode
resolution / applicability / stdin read. No `load_settings`/`os.environ`/`datetime.now`/`time.time`/
SQLite/store/precheck/evidence-builder/eligibility/intent-verifier/broker/network/fs-write. Every
path emits the constant posture (`activation_authorized=false`, `runtime_activation_outcome="no_go"`,
`artifact_authenticated=false`, `artifact_persisted=false`, `approval_consumed=false`,
`replay_prevented=false`). Consistency-not-authenticity (Category C recomputed payload → VALID); no
raw stdin/path/secret/exception leak; only verified exact lowercase hex64 digests echoed. Carry-over:
the 4t builder now hashes and constructs the artifact from `content.validated` (one observation
source; byte-equivalent output/digest). `MemoryError`/`KeyboardInterrupt`/`SystemExit` re-raised.
See `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_VERIFICATION_CLI_CONTRACT.md`.

- **Allowed (composition IS the wiring root):** `broker`, `ledger`, `execution`,
  `orchestration`, `market_data`, `risk`, `paper_loop`, `domain`, `allocator`,
  `decision`, `analysis`, `config`, `composition`. Any first-party package
  outside this allowlist fails the guard so a new dependency gets reviewed.

### Documented deviation (Section-17)

GPT's original prompt wanted `execution` forbidden from the snapshot/composition
layer. The repo already allowlists `execution` for
`orchestration/fast_loop_execution.py`, and the keystone snapshot module imports
`execution` for `PaperPortfolioPolicy`; the composition root must construct the
real `PaperExecutionCoordinator`. Therefore `execution` is **allowed at the
composition boundary by design, not oversight**. The network/credential roots
above remain hard-forbidden regardless.

### Downstream pure approval APIs (no runtime activation)

The `composition` package also hosts pure, IO-free approval-pipeline APIs that never
touch network/credentials/DB/clock and never activate runtime:

- RTM-7c.4s `operator_approval_consumption_eligibility` — eligibility preflight
  (`PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_CONTRACT.md`).
- RTM-7c.4t `operator_approval_consumption_eligibility_artifact` — freezes an `ELIGIBLE`
  result into a canonical immutable observation artifact with a stable digest
  (`PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md`).
  The artifact is **not** consumption: no consumed marker, replay prevention, persistence,
  authentication/signature, TTL/freshness re-evaluation, or activation authorization;
  malformed `NO_GO` maps to `INVALID`. Runtime activation stays NO-GO.
- RTM-7c.4u `operator_approval_consumption_eligibility_artifact_verifier` — standalone
  verification of a serialized eligibility artifact + immutable verified snapshot
  (`PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md`).
  VALID/snapshot means schema·semantic·hash consistency only — **not** actual consumption,
  consumed marker, replay prevention, persistence, authentication, or activation authorization.
- RTM-7c.4v `--verify-approval-consumption-eligibility-artifact` CLI mode — stdin-only read-only
  exposure of the 4u verifier API, same consistency-not-authenticity semantics and constant NO-GO
  posture (`PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_VERIFICATION_CLI_CONTRACT.md`).
