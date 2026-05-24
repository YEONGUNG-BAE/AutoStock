# Technical Debt / P3 Backlog

## P3 — Phase 7 Scout

- `ScoutInputBuilder` currently loads all records and filters fact types in Python. Later optimize by delegating single fact_type filters to `SQLiteDateIdSourceStore.list_records(fact_type=...)`.
- `SCOUT_SCHEMA_INVALID` currently returns one aggregate issue without Pydantic `loc` path. Later expand schema errors into per-field `ValidationIssue.path`.

## P3 — Phase 6 Data

- `DateIdGenerator` scans all records for date prefix. Later replace with SQL prefix/max sequence helper.

## P3 — Phase 3 Broker

- BUY insufficient-cash evaluation computes fee before rejection because cash sufficiency depends on fee-inclusive total cost. Keep fee calculators pure/no-side-effect; revisit only if fee calculation gains external dependencies or side effects.