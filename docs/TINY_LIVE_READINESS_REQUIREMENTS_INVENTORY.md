# Tiny-live Readiness Requirements Inventory (2F)

> **Requirements inventory only, no execution authorized.** This defines what must
> exist before any future tiny-live readiness work can begin. It is not a runbook,
> not an implementation, and authorizes no run.

## Scope and what this is NOT

- **This is NOT Paper-Day.** It is not the Paper-Day KIS live market-data validation track.
- **This is NOT Controlled Day 1 no-write readiness.** It is not the no-write order-decision track.
- **This is NOT tiny-live order validation.** It is not the tiny-live order path and must not become one.
- **This is NOT a live / tiny / write runbook.** It contains no executable live, tiny, or write command.
- **This is a requirements inventory only** — a static list of what future tiny-live readiness would require.

It authorizes nothing. The following stay prohibited:

```text
no live KIS
no network
no live orders
no activation
no daemon
no automatic restart
no live adapter construction
no submit_order implementation
no tiny-live order path
```

## Track separation

- Paper-Day KIS live market-data validation remains separate.
- Controlled Day 1 No-Write Order-Decision Readiness remains separate.
- Tiny-live readiness is a later requirements/planning track only.
- Tiny-live order validation is later than tiny-live readiness and requires explicit human approval.
- Completion of Paper-Day or Controlled Day 1 no-write does not authorize tiny-live.

## Prerequisites before tiny-live readiness work can begin

```text
- the Paper-Day market-data track remains green or explicitly reviewed
- the Controlled Day 1 no-write 2A/2B/2C/2D/2E artifacts are reviewed
- the no-write contract tests remain green
- the safety-block emitter gap is accepted or resolved on its own track
- explicit human approval exists for tiny-live readiness planning
- no live adapter construction path exists outside an approved later track
- full acceptance is green
```

## Status vocabulary

Each requirement row carries exactly one status from this closed set:

- **Not started** — no work and no evidence yet.
- **Existing partial evidence** — some checked-in artifact partially covers it (named in the row).
- **Future design required** — needs a design decision before implementation.
- **Future implementation required** — design may exist but runtime work is deferred to a later track.
- **Future approval required** — gated on explicit human approval.

No tiny-live requirement is marked **Complete**: there is no checked-in tiny-live
order-path evidence in this repo, so no row may claim completion. **Do not mark any
tiny-live requirement as Complete** unless the repo already has explicit checked-in
evidence and this document names that evidence. Do not invent evidence.

## Requirements inventory

| Requirement area | Requirement | Why it matters | Evidence needed before future tiny-live validation | Status |
| --- | --- | --- | --- | --- |
| Human approval | Explicit human go/no-go before any tiny-live planning or run | Tiny-live moves real money; no automatic progression | A signed-off approval record on its own track | Future approval required |
| Activation gate | A reviewed activation gate that is NO-GO by default | Activation must never flip true accidentally | Checked-in gate proving `activation_authorized=false` default | Existing partial evidence (`src/composition/verified_precheck_receipt.py`) |
| Paper/live mode separation | Hard separation of paper vs live execution paths | Prevents a paper bundle from reaching a live venue | `PaperLoopInput` rejects non-PAPER role (2B contract test) | Existing partial evidence (2B `test_..._contract.py`) |
| Live adapter construction authorization | An explicit, approved-only construction point for any live adapter | Uncontrolled construction would open a write path | Static proof no `src/` path constructs the live adapter today (2B) | Existing partial evidence (2B static guard) |
| Submit_order implementation boundary | A defined boundary where submit is blocked until approved | Submit is the only write entry point | `KisLiveReadOnlyBrokerAdapter.submit_order` raises `KisLiveOrderBlockedError` (2B) | Existing partial evidence (2B) |
| Max notional cap | A configurable maximum order notional | Bounds worst-case capital at risk | Specified caps in a separate later risk-limits doc | Future design required |
| Max order count | A maximum number of orders per session/day | Bounds runaway order submission | Specified count limits in a later risk-limits doc | Future design required |
| Max daily loss | A maximum daily realized/unrealized loss limit | Caps downside per day | Specified loss limit + enforcement design | Future design required |
| One-symbol constraint | Restrict tiny-live to a single approved symbol | Limits blast radius for first live validation | Approved single-symbol scope record | Future approval required |
| One-action / one-order constraint | Restrict to a single action / single order | Smallest possible live footprint | Documented single-order scope + enforcement design | Future design required |
| Kill switch | A reviewed kill switch / MDD killswitch path | Must be able to halt immediately | Killswitch detection exists; live wiring deferred | Existing partial evidence (`src/emergency/mdd.py`) |
| Cancel path | A defined order-cancel path | Must be able to cancel a live order | Cancel-path design + test on a later track | Future implementation required |
| Order reject handling | Deterministic handling of broker rejects | Rejects must not corrupt state | Reject-handling design + tests | Future implementation required |
| Fill handling | Deterministic handling of fills | Fills must reconcile to positions/cash | Fill-handling design + tests | Future implementation required |
| Position reconciliation | Reconcile internal positions vs broker | Detect drift between book and venue | Reconciliation design + tests | Future implementation required |
| Cash reconciliation | Reconcile internal cash vs broker | Detect cash drift | Cash-reconciliation design + tests | Future implementation required |
| Duplicate order prevention | Idempotency / duplicate-submit prevention | Prevent double-submitting the same intent | Idempotency design + tests | Future design required |
| Broker disconnect handling | Safe behavior on broker/network disconnect | Disconnect must fail safe, not double-act | Disconnect-handling design + tests | Future design required |
| Evidence schema | A defined tiny-live evidence schema | Reviewer must verify what happened | Schema definition + sample sanitized evidence | Future design required |
| Secret/log redaction | Proven redaction of secrets and raw frames | No secret/account/token leakage in logs | Redaction proof on a later track | Future implementation required |
| Runtime artifact policy | A policy that runtime artifacts stay untracked | Artifacts must not enter git | Acceptance already blocks tracked runtime artifacts | Existing partial evidence (`ops/acceptance_check.sh`) |
| Operator abort criteria | Clear operator abort criteria for a tiny-live run | Operator must know when to stop | Abort-criteria doc for tiny-live on its own track | Not started |
| Reviewer acceptance criteria | Clear reviewer pass/fail criteria | Reviewer needs an objective bar | Acceptance-criteria doc for tiny-live | Not started |
| Rollback / disable switch | A rollback / disable switch for the live path | Must be able to disable quickly | Disable-switch design + tests | Future design required |
| No daemon / no automatic restart | Guarantee no daemon / no automatic restart | Prevents unattended live re-entry | Documented prohibition + guard on a later track | Future design required |

## Hard blockers for future tiny-live validation

Any one of the following blocks future tiny-live validation:

```text
- no explicit human approval
- safety-block emitter gap unresolved or not accepted
- activation gate unclear
- live adapter construction path unclear
- submit_order boundary unclear
- risk caps unspecified
- cancel/reconciliation path unspecified
- evidence schema missing
- secrets/log redaction unproven
- acceptance not green
- any runtime artifact/config/secret leakage
```

## Forbidden in this 2F task

```text
no live KIS
no network
no live orders
no activation
no submit_order implementation
no live adapter wiring
no tiny-live runbook
no executable commands for tiny-live
no account/secret/token/URL/app key/approval key/config value/raw frame/payload/field value/traceback collection
no runtime code changes
```

## Recommended next step after 2F

- **Do not start tiny-live automatically.**
- The recommended next step is a final pre-Monday offline handoff index if needed,
  or stop and wait for the Operator-run Paper-Day market-data validation.
- Actual tiny-live readiness planning remains gated by explicit human approval.

## References

- `docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md` — 2A static boundary inventory.
- `tests/test_controlled_day1_no_write_order_decision_contract.py` — 2B no-write contract tests.
- `docs/CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md` — 2C operator evidence checklist.
- `docs/CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md` — 2D readiness rollup / exit criteria.
- `docs/CONTROLLED_DAY1_NO_WRITE_SAFETY_BLOCK_EMITTER_PROPOSAL.md` — 2E safety-block emitter design proposal.
