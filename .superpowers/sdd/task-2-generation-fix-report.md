# Task 2 Generation Fix Report

## Root cause

The prior lifecycle lock serialized individual writes but did not bind them to
the active websocket instance. A stale check could mark a stream stale, release
the lock, and then an arriving message from that same app refreshed the
heartbeat before `app.close()` ran. Delayed callbacks from an old websocket
could also overwrite the health state of its replacement.

## Minimal protocol

- `_app` is the current websocket identity.
- `_close_intent_app` records the app selected by `_close_stale_stream()` while
  holding `_lifecycle_lock`. That decision is irreversible for the app.
- `_on_message()` passes its app to `handle_message(..., source_app=app)`.
  Source-bound messages refresh the heartbeat only if they remain current and
  have no close intent. Calls without `source_app` retain the direct test API.
- Every websocket callback rejects non-current apps and apps with a close
  intent.
- The stream loop assigns current ownership before `run_forever()`, releases it
  after every session (including stop), and clears the prior close intent only
  when a new app becomes current.

## TDD evidence

RED command:

```powershell
python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_fresh_message_linearized_before_stale_check_keeps_current_stream_open tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_stale_close_intent_prevents_late_current_message_from_restoring_health tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_delayed_callbacks_from_old_app_cannot_mutate_current_app_state -v
```

The first two tests failed because `handle_message()` had no `source_app`
parameter. The old-app test failed because old A set B's connection state to
false.

A separate ownership-cleanup regression first failed with `UnboundLocalError`
when the socket factory threw; after initialization of the local app identity,
it passed. A stop-path cleanup regression then failed because `_app` remained
owned after `run_forever()` returned; it passed after ownership release was
moved before the stop branch.

## Verification

```powershell
python -m unittest tests.test_momentum_compression_monitor -v
python -m py_compile momentum_compression_monitor.py tests/test_momentum_compression_monitor.py
git diff --check
```

Results: 29 monitor tests passed, compilation exited 0, and the whitespace
check reported no errors.

## Scope

Changed only the monitor lifecycle, its focused tests, and this report. No
deployment, restart, external message, or lock-file change was performed.
