# Compression diagnostics final fixes report

Date: 2026-08-23
Worktree: `<repo-root>\.worktrees\momentum-reflow-auto-dashboard`
Base commit: `f145ecd8f058a695a490031dded68b8904a48bcc`

## Scope completed

- `GET /api/compression/status` now materializes `last_scan_failures` once from persisted compression state, sets `scan.errors` to that list's length, and returns that exact list. This keeps `scan.errors == len(scan_failures)` after process restart and during the post-save in-memory-summary race.
- The service test for more than 1,000 failures now proves `failed_symbols` is exactly the symbol projection of `failed_details`, preserving order as well as membership.
- The retry-success test now proves both `fetch_klines` calls pass `raise_errors=True`, while retaining the two-call assertion.

## TDD evidence: persisted-failure API regression

RED test added first:

```powershell
python -m unittest tests.test_momentum_compression_integration.CompressionApiTests.test_status_uses_persisted_failures_for_scan_error_count
```

RED result: expected failure observed before production code changed.

```text
FAIL: test_status_uses_persisted_failures_for_scan_error_count
AssertionError: 0 != 1
Ran 1 test in 0.008s
FAILED (failures=1)
```

The test supplied one persisted `last_scan_failures` row and an in-memory `_compression_scan_summary` with `errors: 0`; the old endpoint returned the mismatched zero.

GREEN production change: materialized `scan_failures` from the already-loaded persisted state, assigned `scan_summary["errors"] = len(scan_failures)`, and returned that same list.

GREEN command (same command): PASS.

```text
Ran 1 test in 0.007s
OK
```

## Verification

| Check | Result |
| --- | --- |
| `python -m unittest tests.test_momentum_compression_integration tests.test_momentum_compression_service -v` | PASS: 40 tests, 0 failures, 0 errors; 3.554s |
| `python -m unittest tests.test_binance_kline_routing tests.test_momentum_compression_service tests.test_momentum_compression_store tests.test_momentum_compression_integration -v` | PASS: 80 tests, 0 failures, 0 errors; 3.061s |
| `python -m unittest discover -s tests -v` | PASS: 603 tests, 0 failures, 0 errors; command exit code 0 |
| `python -m py_compile web_ui.py momentum_compression_service.py` | PASS: exit code 0, no compiler output |
| `git diff --check` | PASS: exit code 0, no whitespace errors |

## Self-review

The API uses the same persisted list for both fields, so it cannot report a count that disagrees with its returned failure details. The new regression isolates the original restart/race state rather than testing a mock alone. The two service assertions match established report behavior and do not alter production service code.

## Constraints and concerns

- No deployment, service restart, production-data change, `PROGRESS.md` update, or runtime-lock-file modification was performed.
- The pre-existing untracked `.momentum_compression_alert_delivery.lock` and `.momentum_compression_alerts.lock` remain untouched and are excluded from the commit.
- No unresolved code or verification concern was found.
