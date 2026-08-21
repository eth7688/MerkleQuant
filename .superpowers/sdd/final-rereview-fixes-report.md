# Compression Scanner Final Re-review Fixes

## Scope

- Reconciled successful scans by `symbol + side`: unchanged compression starts retain their identity; changed starts and successful rejections retire the stale pool identity before live ticks can emit from it. A failed symbol fetch remains explicitly unavailable and is preserved.
- Resolved the durable identity while holding the state transaction, before any snapshot write. Same-start continuations retain the original immutable snapshot reference; no competing snapshot identity is written.
- Migrated v1 `delivery_queue` state into v2 `fresh_outbox` records for all reconstructable FRESH identities, including CONFIRMED, UNKNOWN, and CONFLICT. Historical emitted IDs with no recoverable event facts are retained in `legacy_unpublished_event_ids`, which prevents a duplicate FRESH without fabricating a public event.
- WeCom delivery persists `in_flight` intent before HTTP. A restart converts that state to `indeterminate` and does not resend. This intentionally favors at-most-once external delivery over an unknowable duplicate send because WeCom exposes no idempotency key. Pre-send HTTP failures remain retryable with existing backoff.
- HTF now records `ALIGNED`, `OPPOSITE`, or `UNKNOWN` per timeframe. Aggregate result is CONFIRMED only for two aligned frames, CONFLICT for any explicit opposite frame, otherwise UNKNOWN.
- Rejected non-finite live ticker values and supplied the actual scan timestamp as `evaluated_at` rather than the candle close.

## TDD evidence

- RED: `python -m unittest tests.test_momentum_compression_store tests.test_momentum_compression_service tests.test_momentum_compression_alerts` — 54 tests; 7 assertion failures and 3 expected missing-behavior errors (lineage, snapshot identity, migration, HTF, finite price, scan time, and post-send crash window).
- GREEN (final compression): same command — 55 tests passed.
- GREEN (compression + reflow compatibility): 284 tests passed.
- Full discovery: `python -m unittest discover -s tests` — 568 tests passed.
- Compile: `python -m py_compile momentum_compression.py momentum_compression_store.py momentum_compression_service.py momentum_compression_monitor.py momentum_compression_alerts.py web_ui.py` passed.
- Whitespace: `git diff --check` passed.

## Remaining minor

`CompressionMonitor.status()` still performs a bounded-state-file load on repeated status polling. The compression state is a single bounded JSON document in this design; no cache was added to avoid widening lifecycle/state invalidation behavior for this review-only change.
