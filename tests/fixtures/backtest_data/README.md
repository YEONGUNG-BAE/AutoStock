# Backtest Data Fixtures (Phase 2a)

## Synthetic status

- ALL CSV files in this directory are SYNTHETIC fixtures.
- They exist ONLY for loader / as-of-guard testing.
- They are NOT real vendor or market data. No value here is an actual
  market observation.
- Real long-horizon market data must NOT be committed to this repository
  until the license/size policy is resolved. Future real data should
  likely be provided via a local path input or an external artifact,
  not committed fixtures.

## Files and schema

### `sp500_tr_usd_synthetic.csv`

| column | meaning |
| --- | --- |
| `date` | normalized alignment key (ISO date) |
| `as_of` | timezone-aware ISO-8601 source availability timestamp |
| `sp500_tr_usd` | S&P 500 TOTAL-RETURN index level in USD (positive Decimal) |
| `source_name` | source identifier, preserved by the loader |

The benchmark MUST be total-return, not price return. This fixture
represents a total-return level series.

### `usdkrw_synthetic.csv`

| column | meaning |
| --- | --- |
| `date` | normalized alignment key (ISO date) |
| `as_of` | timezone-aware ISO-8601 source availability timestamp |
| `usdkrw` | KRW per USD rate (positive Decimal) |
| `source_name` | source identifier, preserved by the loader |

### `instrument_prices_synthetic.csv`

| column | meaning |
| --- | --- |
| `date` | normalized alignment key (ISO date) |
| `as_of` | timezone-aware ISO-8601 source availability timestamp |
| `symbol` | original instrument symbol, preserved |
| `market` | original market label (KR / US / GOLD), preserved |
| `close_adjusted` | split/dividend-ADJUSTED close (positive Decimal) |
| `source_name` | source identifier, preserved |

Price series must be split/dividend adjusted; `close_adjusted` is the
adjusted series, never a raw close.

Instrument rows may later represent EITHER asset-class proxy
instruments (e.g. `SYN_KR_PROXY`) OR individual securities
(e.g. `SYN_STOCK_001`) in diagnostic stock-level tests. The loader must
not bake in asset-class-only assumptions.

## `date` vs `as_of`

Both columns are preserved and have DIFFERENT meanings:

- `date`: normalized alignment key, used for common-date benchmark/FX
  alignment.
- `as_of`: source availability timestamp, the look-ahead safety key.

`as_of` must be stamped conservatively: if unsure when data became
known, mark it as known LATER, never earlier. `date` and `as_of` are
not assumed to fall on the same calendar day across time zones;
NYSE/KRX/FX close mismatch is allowed, documented here, and preserved
(the synthetic S&P rows are deliberately stamped as available the next
KST morning).

## Alignment rules

- No forward-fill and no interpolation are allowed for primary aligned
  scoring. Non-common dates are dropped with deterministic warnings.
- Duplicates are rejected fail-fast (benchmark/FX: duplicate `date`;
  instruments: duplicate `(date, symbol, market)`).
- The committed S&P and USDKRW fixtures intentionally each contain one
  non-common date (2020-01-03 S&P-only, 2020-01-08 FX-only) so the
  alignment-drop warnings are exercised.
