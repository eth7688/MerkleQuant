# 15M Compression Scanner Final Fixes

## Scope

Implemented only the final review repairs for the 15M compression scanner. No deployment was performed and no real WeCom webhook was sent.

## Design decisions

- Discovery now fetches the Binance Futures ticker price and supplies it to the existing closed-candle evaluator. The evaluator remains the single admission authority, so already-outside and unconfirmed discoveries cannot enter the pool or create FRESH.
- A valid continuation that keeps the same symbol, side, and compression start retains the prior durable pool identity. Its audit fields update, but it cannot become a concurrent pool row or emit a second FRESH. Failed symbols remain omitted from reconciliation and are marked unavailable on the latest persisted state.
- State schema v2 replaces the unused main `delivery_queue` with `fresh_outbox`. Every FRESH is written to this durable source in the same state save as its state-machine transition. Public JSONL and confirmed WeCom delivery rows are drained idempotently from the outbox; restart, callback failure, and alert-state disk failure cannot lose a FRESH or duplicate public events. v1 state is migrated on load.
- Compression uses `momentum_reflow_alert_settings.json`, the existing protected WeCom settings file. The compression API returns no setting or webhook data.
- Structural network work runs without the monitor price lock. The final load/reconcile/save and price writes use a short shared state-file lock, preserving price transitions that occur during scans.
- Snapshots write atomically into the configured snapshot directory exactly once, contain only the selected compression window, validate immutability for an existing identity, and are attached as `ohlcv_snapshot_ref` before reconciliation.
- Per-timeframe `1h` and `4h` verdicts persist with aggregate alignment. Confirmed WeCom text includes the requested audit facts and Beijing time.
- Manual compression scans require login, return immediately, and run in a single background worker. Automation remains admin-only.
- `today_fresh` now counts durable outbox events whose `event_at` falls on the current Asia/Shanghai calendar date.

## TDD record

RED command:

```text
python -m unittest tests.test_momentum_compression_service tests.test_momentum_compression_store tests.test_momentum_compression_monitor tests.test_momentum_compression_integration -v
```

Observed expected failures before implementation: missing `fetch_live_price`, missing `fresh_outbox`, duplicate same-symbol/side continuity, scan blocking a live-price batch, and the synchronous unauthenticated manual route.

Added regression coverage for live-price admission, appended-candle continuity, selected snapshot persistence, durable outbox recovery, WeCom audit fields, non-blocking price races, and authenticated asynchronous manual scans.

## GREEN verification

```text
python -m unittest tests.test_momentum_compression tests.test_momentum_compression_store tests.test_momentum_compression_service tests.test_momentum_compression_monitor tests.test_momentum_compression_alerts tests.test_momentum_compression_integration -v
102 tests OK

python -m unittest discover -v
558 tests OK

python -m py_compile momentum_compression.py momentum_compression_store.py momentum_compression_service.py momentum_compression_monitor.py momentum_compression_alerts.py web_ui.py
exit 0

git diff --check
exit 0
```

## Compatibility and remaining concerns

- Existing v1 compression state loads as v2; confirmed legacy queue entries become durable outbox entries and are idempotently reconciled with existing public alerts.
- The outbox intentionally remains durable after publication so it is a complete FRESH audit source. Public event and confirmed-delivery deduplication are keyed by immutable compression identity.
- Snapshot references are app-root-relative (`momentum_compression_snapshots/<id>.json`) while writes use the configured snapshot directory.
- This was local-only verification. Live Binance availability and actual WeCom delivery were intentionally not exercised.
