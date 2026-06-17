# Paper-Day Operator Runbook

Diagnostic mode is attended and bounded. It is not activation.

Validate only:

```bash
PYTHONPATH=src uv run python ops/run_attended_paper_day.py \
  --config config/config.toml.example \
  --session-date YYYY-MM-DD \
  --symbol 005930 \
  --duration-seconds 60 \
  --evidence-out runtime/paper-day/YYYY-MM-DD/evidence.jsonl \
  --summary-out runtime/paper-day/YYYY-MM-DD/summary.json \
  --db-dir runtime/paper-day/YYYY-MM-DD/db \
  --confirm-attended-paper \
  --validate-only \
  --json
```

Offline fixture check:

```bash
PYTHONPATH=src uv run python ops/run_attended_paper_day.py \
  --config config/config.toml.example \
  --session-date YYYY-MM-DD \
  --symbol 005930 \
  --duration-seconds 60 \
  --evidence-out runtime/paper-day/YYYY-MM-DD/evidence.jsonl \
  --summary-out runtime/paper-day/YYYY-MM-DD/summary.json \
  --db-dir runtime/paper-day/YYYY-MM-DD/db \
  --confirm-attended-paper \
  --offline-fixture deterministic \
  --json
```

Operator-only KIS startup/run path uses `--live-kis` and requires KIS websocket
read-only config plus app key/secret environment variables. Cursor tests do not
execute this path.

Immediate stop conditions:

```text
real-order adapter constructed
credential/raw frame leak
unexpected network route
journal uncertain
nonterminal journal stuck
ledger invariant failure
evidence write failure
resource close failure
activation_authorized=true
```

After the day, review `summary.json` first, then the earliest evidence record
whose stage/reason explains the first failure.
