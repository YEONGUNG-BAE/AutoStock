# Controlled Day 1 — No-Write Operator Evidence Checklist

> **Operator-facing checklist, no execution authorized.** This tells the Operator
> which offline/synthetic evidence to collect for the *future* Controlled Day 1
> no-write order-decision rehearsal and when to stop. **No live / tiny / write
> command is authorized** by this document. It authorizes no run.

## Scope and what this is NOT

- **This is NOT Paper-Day.** It is not the Paper-Day KIS live market-data
  validation track (live quote read / parsing / evidence / envelope / operator
  flow). Do not mix this with Paper-Day docs or commands.
- **This is NOT tiny-live order validation.** It is not the tiny-live order path
  and must not become one. No tiny-live order command is provided or authorized.
- **This IS for Controlled Day 1 no-write order-decision readiness**: collecting
  offline/synthetic evidence that the strategy / risk / order-decision flow can
  reach a *hypothetical* order intent while every live broker write path stays
  impossible and no live adapter is constructed.
- **It is operator-facing but no live/tiny/write command is authorized.** Nothing
  here runs live KIS, opens the network, constructs a live adapter, or submits an
  order. Forbidden operational tokens appear only as prohibition or boundary
  assertions.
- **It does not replace the boundary inventory or contract tests.** It is an
  evidence-collection and stop-boundary aid layered on top of them.

## Hard prohibitions (this track)

```text
no live KIS
no network
no live orders
no tiny-live order command or runbook
no activation (activation_authorized must stay false)
no daemon
no automatic restart
no live adapter wiring / construction
no submit_order implementation or invocation against a live broker
no runtime / ops / src / config behavior change
no secrets / config.toml contents / runtime artifacts
no raw frame / payload / field-value / URL / token / app key / approval key /
  account / traceback logging
```

## Current prerequisites

Before collecting any no-write evidence, confirm these checked-in artifacts exist
and the offline gate is green:

- [ ] Boundary inventory exists:
      `docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md`
- [ ] Contract tests exist:
      `tests/test_controlled_day1_no_write_order_decision_contract.py`
- [ ] Full acceptance should be green before proceeding
      (`PYTHONPATH=src uv run pytest tests/ -q` and `uv run bash ops/acceptance_check.sh`).

If any prerequisite is missing or acceptance is not green, **stop** — do not
collect evidence and do not proceed.

## Allowed evidence

Collect only offline/synthetic, checked-in, sanitized evidence:

- [ ] Test command output from the offline/synthetic no-write tests
      (`tests/test_controlled_day1_no_write_order_decision_contract.py`).
- [ ] Checked-in docs/tests references (paths and symbol names only).
- [ ] A sanitized summary of hypothetical order intent generation
      (that `OrderIntentGenerator` reaches a GENERATED `OrderIntent` with no broker
      constructed) — values synthetic, no live data.
- [ ] Proof that `broker_order_result is absent` (a `PaperLoopResult` carries
      `generated_order_intent` while `broker_order_result` stays `None`).
- [ ] Proof that no `KisLiveReadOnlyBrokerAdapter` construction exists in `src/`
      (the static-guard contract test passes).
- [ ] Proof that live submit is blocked by `KisLiveOrderBlockedError`
      (`KisLiveReadOnlyBrokerAdapter.submit_order` raises).
- [ ] Proof that non-PAPER `PaperLoopInput` is rejected
      (`broker_account_role` other than PAPER raises `ValueError`).
- [ ] The documented future gap for a run-free safety-block emitter
      (recorded in the boundary inventory; not invented here).

## Forbidden evidence / forbidden requests

Never collect, paste, request, or log any of the following:

- [ ] No secrets.
- [ ] No `config/config.toml` contents (or any secret-bearing config values).
- [ ] No runtime artifacts (evidence/log files under `runtime/`).
- [ ] No raw frames / payloads / field values.
- [ ] No URLs / tokens / app keys / approval keys / accounts / tracebacks.
- [ ] No live KIS output.
- [ ] No broker / order endpoint output.
- [ ] No tiny-live order output.

If any forbidden item is requested or appears, treat it as an abort condition.

## Abort criteria

Stop immediately and report (do not continue collecting evidence) if any of the
following occurs:

- [ ] Any live / KIS / network command was run.
- [ ] Any live order or tiny-live command was run.
- [ ] Any `submit_order` implementation or live adapter wiring was added.
- [ ] `activation_authorized` true appears as an executable path.
- [ ] `KisLiveReadOnlyBrokerAdapter` is constructed from a `src/` execution path.
- [ ] `PaperLoopInput` accepts a non-PAPER `broker_account_role`.
- [ ] `broker_order_result` or `fill` appears in no-write evidence.
- [ ] Secrets / raw frames / log payloads appear.
- [ ] Acceptance fails.

## Stop boundary

A successful 2C means **documentation/evidence checklist only** — no live, tiny,
or write path is opened.

**Do not proceed to tiny-live until** all of the following hold:

```text
- the boundary inventory is reviewed
- the contract tests are green
- this operator checklist is reviewed
- the future safety-block emitter gap is accepted or resolved on its own track
- explicit human approval for tiny-live readiness exists
```

Tiny-live order validation is a **separate, later** track. Nothing in this
checklist authorizes it.

## References

- `docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md` — static boundary inventory.
- `tests/test_controlled_day1_no_write_order_decision_contract.py` — no-write contract tests.
- `docs/CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md` — no-write readiness rollup / exit criteria.
