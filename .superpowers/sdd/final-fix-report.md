# AXIOM R Performance Final Fix Report

Date: 2026-07-29

Reviewed range: `8725e6f..0749faa`

## Scope

Implemented every Important and Minor item from `final-review-findings.md`:

- preserve missing or invalid MFE/MAE as unmeasured (`None`);
- average MFE across measured lifecycles only;
- cap only each range's `cumulative_r_points` array at 500 deterministic samples while preserving its first and last points;
- escape every direction and exit-reason label before detail HTML interpolation;
- show the selected `1W/1M/3M/6M/1Y/ALL` range in the R header;
- show exit-reason trade counts alongside net R;
- use `time.monotonic()` for the two-second R cache age;
- add focused coverage for exact cutoff inclusion, excursion data states, only-loss/excluded UI states, cache exceptions/expiry, malicious labels, reason counts, and range labels.

No trading behavior, API route, deployment file, or `PROGRESS.md` was changed.

## RED evidence

Command:

```powershell
python -m unittest discover -s tests -p "test_performance_metrics.py" -v
```

Result before production changes: exit `1`; 16 tests ran with 10 expected failures. The failures proved that missing/invalid excursions were reported as `0.0`, MFE averages included unmeasured lifecycles, and all six range arrays returned 600 points instead of 500.

The first integration RED attempt exposed a quoting error in the new test probe itself (`SyntaxError: unterminated string literal`). The test-only fixture was corrected before evaluating production behavior.

Command:

```powershell
python -m unittest discover -s tests -p "test_r_performance_integration.py" -v
```

Result after correcting the test fixture and before production changes: exit `1`; 12 tests ran with 5 expected failures. The failures proved that cache expiry ignored mocked monotonic time, `escapeRHtml` was absent, malicious `<img onerror=...>` labels reached `innerHTML`, the selected range label was absent, and exit-reason counts were absent. The new calculator-exception, only-loss, and excluded-record checks passed against existing behavior and now protect those states.

## GREEN evidence

Commands:

```powershell
python -m unittest discover -s tests -p "test_performance_metrics.py" -v
python -m unittest discover -s tests -p "test_r_performance_integration.py" -v
```

Results:

- metric tests: exit `0`; 16/16 passed in 0.008s;
- integration/runtime tests: exit `0`; 12/12 passed in 0.467s;
- the runtime file executes extracted production JavaScript in Node, including HTML metacharacters and `<img onerror=...>` payload labels.

## Final verification evidence

Command:

```powershell
python -m py_compile performance_metrics.py trader.py web_ui.py
```

Result: exit `0`; no compiler output.

Command:

```powershell
python -m unittest discover -s tests -v
```

Result: exit `0`; 147/147 tests passed in 0.675s.

Command:

```powershell
git diff --check
```

Result: exit `0`; no whitespace errors. Git emitted informational Windows LF-to-CRLF working-copy warnings for the five modified source/test files.

Command:

```powershell
Get-FileHash -Algorithm SHA256 ".deploy\r-baseline-2026-07-29.jsonl"
```

Result:

```text
EC60C5789800D4C3B34E5BBEEAC751D799B389FB362AFFE5C214D5C1148A4D1D
```

Command:

```powershell
python -c "import json; from datetime import datetime, timezone; from pathlib import Path; from performance_metrics import summarize_r_performance_ranges; rows=[json.loads(x) for x in Path('.deploy/r-baseline-2026-07-29.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]; print(json.dumps(summarize_r_performance_ranges(rows, datetime(2026,7,29,12,0,tzinfo=timezone.utc))['ranges']['all'], ensure_ascii=False, indent=2))"
```

Approved immutable metrics remained unchanged:

```text
valid_exit_record_count = 54
valid_trade_count = 52
net_r = 3.1941
expectancy_r = 0.061425  (approved 4dp: 0.0614)
average_win_r = 1.283362 (approved 4dp: 1.2834)
average_loss_r = -0.666912 (approved 4dp: -0.6669)
average_payoff_ratio = 1.924337 (approved 4dp: 1.9243)
profit_factor = 1.184207 (approved 4dp: 1.1842)
max_drawdown_r = 6.3729
```

Strategy-isolation command:

```powershell
git diff --unified=3 6409a8c -- trader.py
```

Result: `trader.py` changes are limited to the pure calculator import, two cache fields, `_r_performance_summary()`, and one `r_performance` payload calculation/key in each summary method. No entry evaluation, stop management, take-profit, quantity calculation, exchange-client, or order-placement function changed.

Final intended files:

```text
performance_metrics.py
trader.py
web_ui.py
tests/test_performance_metrics.py
tests/test_r_performance_integration.py
.superpowers/sdd/final-fix-report.md
```

Deployment was not performed, and `PROGRESS.md` was not modified.
