# Demo RJ Daily Pattern Shadow Plan

## Goal

Measure whether the latest fully closed Bitget UTC daily candle improves the
existing RJ-only 30-minute strategy without changing demo entries during data
collection.

## Approved behavior

- Demo normalization forces `rj_daily_pattern_filter_mode=log_only`.
- Other engines remain `off` unless explicitly configured.
- The daily source is Bitget `1Dutc`, with `closed_only=True`.
- Historical evaluation keeps only candles where `ot + 86_400_000 <= decision_time`.
- `log_only` records aligned, opposed, mixed, none, and unavailable states but
  never blocks an entry.
- Missing optional replay daily history uses an auditable `unavailable` marker
  and fails open; malformed present history still fails validation.
- A future `soft` experiment blocks only an opposed pattern with rank 2 or 3;
  rank-1 fractals, mixed states, missing data, and no-pattern states fail open.

## Audit surface

The entry snapshot is stored once on the position and copied to the final trade
record. Both position-card renderers and both trade-list renderers display the
same persisted snapshot. Existing positions without a snapshot display
`未记录`; they are not reconstructed from later market data.

## Backtest comparison

Bitget history and both replay engines accept `1d` data. Entry events preserve
the point-in-time daily snapshot, and metrics report `daily_pattern_breakdown`
by alignment with trade count, win rate, sum R, and mean R. Baseline and soft
experiments differ only by `rj_daily_pattern_filter_mode`.

## Acceptance criteria

- No current/unclosed daily candle is visible to a historical decision.
- Demo `log_only` mode cannot reject an order.
- Daily fetch failure remains auditable and fail-open.
- Position persistence, exit trade records, cards, and trade lists retain the
  entry-time snapshot.
- Full unit suite and Python compilation pass.
