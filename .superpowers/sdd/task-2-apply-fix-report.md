# Task 2 Apply Fix Report

## Root cause

`handle_message()` refreshed a source websocket's heartbeat while holding
`_lifecycle_lock`, then released that lock before `_apply_prices()` committed
pool state. A replacement websocket could become `_app` in that gap, while the
old websocket still wrote prices and emitted a fresh-breakout event.

## Fix

- Kept REST and websocket updates on the existing `_apply_prices()` entrypoint.
- Split state persistence into `_commit_prices()` so source-bound calls can
  hold `_lifecycle_lock` across the final owner/close-intent check and the
  matching state commit (`lifecycle -> state -> file lock`).
- Kept `event_callback()` after the lifecycle lock is released.
- Calls without `source_app` retain the existing two-argument `_apply_prices()`
  invocation and bypass websocket ownership checks, so REST remains available
  during a websocket close intent.

## TDD evidence

RED command:

```powershell
python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_replaced_source_cannot_commit_prices_or_events_after_final_boundary tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_current_source_can_commit_prices tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_rest_fallback_commits_when_current_source_has_close_intent -v
```

Before the fix, the barrier test failed because A wrote `live_price` after B
became `_app`; the current-source and REST controls passed.

The barrier blocks A after parsing at the final apply boundary, switches
ownership to B under `_lifecycle_lock`, then permits A to continue. It asserts
that A neither persists a price nor invokes the event callback. Separate tests
prove that the current source still persists prices and REST remains ownership
independent.

## Verification

```powershell
python -m unittest tests.test_momentum_compression_monitor -v
python -m py_compile momentum_compression_monitor.py tests/test_momentum_compression_monitor.py
git diff --check
```

Results: 32 monitor tests passed, compilation exited 0, and `git diff --check`
reported no errors.

## Scope

Changed only the compression monitor, focused monitor tests, and this report.
No deployment, restart, external message, or lock-file change was performed.
