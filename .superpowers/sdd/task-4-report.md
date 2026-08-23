# Task 4 report: momentum compression performance verification

## Scope

Worktree: `<repo-root>\.worktrees\momentum-reflow-auto-dashboard`

Only `tests/benchmark_momentum_compression.py` was added. No production strategy code, parameters, rules, services, or deployment state were changed.

## Benchmark

The benchmark follows the task brief: 500 deterministic 220-bar frames, each evaluated with `evaluate_both_sides`, and a nonzero exit status above 300 seconds.

The first exact command exposed an import-path issue when invoked as a script:

```text
$ python tests/benchmark_momentum_compression.py
Traceback (most recent call last):
  File "...\\tests\\benchmark_momentum_compression.py", line 7, in <module>
    from momentum_compression import evaluate_both_sides
ModuleNotFoundError: No module named 'momentum_compression'
```

The benchmark now inserts the repository root derived from `__file__` before importing the module, keeping the required command self-contained and reproducible.

Final raw benchmark output:

```text
$ python tests/benchmark_momentum_compression.py
500-symbol offline calculation: 4.771s
```

Exit code: `0` (4.771 seconds <= 300 seconds).

## RED/GREEN

No production behavior changed, so no production RED/GREEN cycle applies. The benchmark itself was initially RED due to the script import-path failure, then GREEN after the minimal path bootstrap correction; the final benchmark exited 0 and measured 4.771 seconds.

## Full verification

Command:

```text
python -m unittest discover -s tests -v
```

Result: exit code `0`; `Ran 591 tests in 16.400s`; `OK`.

Command:

```text
python -m py_compile momentum_compression.py momentum_compression_service.py momentum_compression_monitor.py web_ui.py admin_server.py
```

Result: exit code `0`, no output.

Command:

```text
git diff --check
```

Result: exit code `0`, no output.

## Files changed

- `tests/benchmark_momentum_compression.py` — deterministic offline benchmark and repository-root import bootstrap.

## Self-review

- The frame construction, timing loop, evaluation arguments, threshold, and output format match the task brief.
- No strategy parameters or production logic were modified.
- The path insertion is limited to the benchmark script and is required for the exact documented invocation to work from the repository root.
- Existing untracked runtime lock files were not staged or modified.

## Commit

`5963466 test: benchmark compression scan calculation`

## Concerns

The benchmark wall-clock time is machine-dependent, but the measured result is far below the 300-second gate. The initial import-path failure would recur without the benchmark-local path bootstrap; it is documented above and corrected.
