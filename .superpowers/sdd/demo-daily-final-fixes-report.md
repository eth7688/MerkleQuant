# Demo Daily Pattern Final Review Fixes

Date: 2026-08-02

## Scope

Implemented the final review wave on base `2fe3adb` in
`<repo-root>\.worktrees\momentum-reflow-auto-dashboard`.
No production host, service, or production data was accessed or changed.

## Changes

- Added `parse_stored_project_time_ms()`. It parses the project's persisted
  Beijing wall-clock value and subtracts eight hours. Position `entry_time`,
  `entry_filled.time`, and the trade exit-time fail-closed check use it. CLI
  `--cutoff` remains true UTC through `parse_time_ms()`.
- Added `_attach_daily_pattern_for_entry()` and reused it from both entry
  implementations. `structure`, `rj_only`, and `predicta_ewo` now attach and
  audit the same immutable snapshot; soft blocking remains RJ-only.
- Changed the migration CLI to return `0`/`1`, print exactly one structured
  JSON report, and bound its diagnostic `error` to 240 characters. External
  history-loader exceptions are replaced with a safe symbol-scoped message;
  common credential assignments are also redacted.
- Preserved two-phase output staging and added best-effort rollback of any
  first file replaced in-process if the second `os.replace` fails.
- Updated the design and plan only for stored timestamp conversion, structure
  entry coverage, and the accepted two-replace crash boundary.

Files changed:

- `trader.py`
- `tools/backfill_demo_daily_patterns.py`
- `tests/test_daily_pattern_shadow.py`
- `tests/test_daily_pattern_backfill.py`
- `docs/superpowers/specs/2026-08-02-demo-daily-pattern-backfill-design.md`
- `docs/superpowers/plans/2026-08-02-demo-daily-pattern-backfill.md`
- `.superpowers/sdd/demo-daily-final-fixes-report.md`

## TDD evidence

### RED: migration review cases

Executed before production edits:

```powershell
python -m unittest discover -s tests -p test_daily_pattern_backfill.py -q
```

Terminal result:

```text
Ran 21 tests in 0.624s

FAILED (failures=7, errors=4)
Exit code: 1
```

The failures/errors were the expected missing behaviors:

- no `parse_stored_project_time_ms` function;
- stored `16:10:31/33+00:00` values remained eight hours late;
- the next UTC-midnight candle was admitted for a real 20:00 UTC fill;
- the pre-cutoff trade exit triggered the recent-trade linkage abort;
- successful CLI paths returned `None` instead of `0`;
- loader/staging/final-replace exceptions escaped instead of returning a JSON
  failure report;
- a second final replace failure had no rollback path.

The deliberately long secret-bearing fixture text from the failure-report test
is not reproduced here. Its regression assertion requires the final report to
omit the supplied secret and cap the diagnostic length.

### RED: structure entry

Executed before production edits:

```powershell
python -m unittest discover -s tests -p test_daily_pattern_shadow.py -q
```

Terminal result:

```text
ERROR: test_structure_entry_attaches_audited_snapshot_to_position
KeyError: 'kind'

Ran 25 tests in 0.161s

FAILED (errors=1)
Exit code: 1
```

The test reached a valid structure `Position`; the missing `kind` proved that
the daily snapshot had not been attached.

### GREEN: focused review tests

Executed after implementation and final test refinement:

```powershell
python -m unittest discover -s tests -p test_daily_pattern_backfill.py -q
python -m unittest discover -s tests -p test_daily_pattern_shadow.py -q
```

Terminal results:

```text
Ran 21 tests in 0.562s

OK

Ran 25 tests in 0.156s

OK
Exit code: 0
```

Additional focused entry regressions:

```powershell
python -m unittest discover -s tests -p 'test_*entry*.py' -q
python -m unittest discover -s tests -p test_predicta_pipeline.py -q
```

Terminal results:

```text
Ran 7 tests in 0.024s

OK

Ran 9 tests in 0.063s

OK
Exit code: 0
```

## Compilation and full regression

Compilation command:

```powershell
python -m py_compile trader.py strategy_filters.py web_ui.py tools/backfill_demo_daily_patterns.py
```

Output: no output; exit code `0`.

The complete suite was run once after focused GREEN and compilation:

```powershell
$out = Join-Path $env:TEMP 'daily-pattern-final-fixes-full-suite.txt'
python -m unittest discover -s tests -q *> $out
$code = $LASTEXITCODE
Get-Content -LiteralPath $out -Tail 40
Write-Output "FULL_SUITE_EXIT=$code"
if ($code -ne 0) { exit $code }
```

Terminal summary:

```text
----------------------------------------------------------------------
Ran 456 tests in 11.971s

OK
[自动启动] 演示引擎已就绪
FULL_SUITE_EXIT=0
```

The preceding tail contained expected localized test logs, including injected
warning/error paths from stop-order safety tests; unittest reported zero test
failures.

## Self-review

- Exact cutoff: stored `2026-08-01T16:10:31+00:00` maps to one second before
  `2026-08-01T08:10:32Z`; stored `16:10:33+00:00` maps to one second after.
- UTC midnight: a real 20:00 UTC fill stored as next-day `04:00+00:00` selects
  only the candle closed at the preceding 00:00 UTC and excludes the candle
  that closes four hours after entry.
- Timestamp routing: only project-persisted position/event/trade values receive
  the eight-hour correction; user-supplied cutoff does not.
- Source coverage: the attachment helper is called by the shared RJ/Predicta
  key-candle path and the structure path. The structure integration test checks
  both the audit event and `Position.daily_pattern`.
- Decision preservation: off mode skips the daily fetch for all three sources;
  only exact `rj_only` + `soft` + `would_block` can reject an entry.
- Failure safety: linkage, fetch, evaluation, and JSON exceptions are caught
  before writes and produce `failures=1` plus a bounded diagnostic.
- Apply safety: both new outputs are staged and validated before replacement;
  the injected second-replace failure restores the already replaced positions
  file and leaves both original files byte-identical.
- Scope: no Python routes, frontend files, dependencies, configuration, data,
  or production assets were changed.

## Concerns

No blocking concern.

The two final `os.replace` calls are not a cross-file transaction. The new
rollback covers an ordinary in-process exception on the second replace, but an
operating-system or power crash between replacements can bypass Python cleanup.
Production use therefore still requires the stopped-service backup and
operational restoration procedure described in the design.

No deployment occurred, so `PROGRESS.md` was not updated; its post-deployment
bookkeeping rule does not apply to this local-only fix wave.
