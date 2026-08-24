# Task 2 Atomic Price Commit Test Report

## Scope

Changed only `tests/test_momentum_compression_monitor.py`. No production code,
deployment, restart, external message, or lock-file change was made.

## Prior test gap

The replaced-source test paused `_apply_prices()` before it acquired
`_lifecycle_lock`. It then let B assign `_app` before A resumed, so it only
proved that a source rejected after handover cannot commit. It did not prove
that B is blocked while an accepted A message is committing.

The prior test passed unchanged on the reviewed implementation:

```powershell
python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_replaced_source_cannot_commit_prices_or_events_after_final_boundary -v
```

Result: 1 test passed.

## Discriminator and replacement test

The replacement instruments `_commit_prices()`. A is paused there after
`_apply_prices()` has accepted it and while it holds `_lifecycle_lock`. A
separate B thread signals its takeover attempt, then must remain incomplete
until A is released. After release, the test verifies A's persisted price and
fresh-breakout state, plus the single callback event.

To prove the barrier distinguishes the incorrect lock boundary, a one-off
shadow implementation performed the ownership check under `_lifecycle_lock`
but released it before `_commit_prices()`. With the same barrier, B completed
takeover before A's commit was released:

```text
wrong-structure discriminator: B took ownership before A commit completed
```

The replacement test then passed against the reviewed implementation:

```powershell
python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_current_source_commits_before_replacement_can_take_over -v
```

Result: 1 test passed.

## Verification

```powershell
python -m unittest tests.test_momentum_compression_monitor -v
python -m py_compile momentum_compression_monitor.py tests/test_momentum_compression_monitor.py
git diff --check
```

Results: 32 monitor tests passed; compilation exited 0; `git diff --check`
exited 0 with no whitespace errors.
