# Task 1 Report

## Files changed

- `tests/fixtures/arcusdt_binance_futures_15m_20260825_2215.json`: added the verified 220-candle ARCUSDT Binance Futures 15m snapshot.
- `tests/test_momentum_compression.py`: added `ARC_FIXTURE`, `ARC_OHLCV_SHA256`, and the historical regression test.

No production code, server code, or configuration was modified. The two pre-existing lock files were preserved.

Fixture verification: 220 candles; first open time `1787470200000`; last open time `1787667300000`; evaluated time `1787668200000`; live price `0.07141`; former window `1787643900000..1787667300000`; canonical OHLCV SHA256 `2e759524f63b70d777e706fea737d58db0ab9f5df6ece446d285f2461c6e1072`.

## TDD red verification

Command:

```text
python -m unittest tests.test_momentum_compression.CompressionHistoricalRegressionTests.test_arc_binance_trend_channel_is_rejected -v
```

Output:

```text
test_arc_binance_trend_channel_is_rejected (tests.test_momentum_compression.CompressionHistoricalRegressionTests.test_arc_binance_trend_channel_is_rejected) ... FAIL

======================================================================
FAIL: test_arc_binance_trend_channel_is_rejected (tests.test_momentum_compression.CompressionHistoricalRegressionTests.test_arc_binance_trend_channel_is_rejected)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\\<repo-root>\\.worktrees\\compression-compact-guards\\tests\\test_momentum_compression.py", line 536, in test_arc_binance_trend_channel_is_rejected
    self.assertEqual(result["state"], "REJECTED")
AssertionError: 'COMPRESSION_ACTIVE_SHORT' != 'REJECTED'
- COMPRESSION_ACTIVE_SHORT
+ REJECTED

----------------------------------------------------------------------
Ran 1 test in 0.342s

FAILED (failures=1)
```

This is the expected pre-fix failure.

## Self-review

- Scope is limited to the requested fixture and regression test.
- Metadata and the fixture's independently recomputed canonical SHA256 match the brief exactly.
- `git diff --check` passed.
- No production code or server was touched.

## Commit

Commit message: `test: reproduce ARC compression false positive`

Commit SHA: `0a09a6159c4dbbc2746dd3cbf8308b90587e078c` (before adding this report; final commit SHA is supplied below after amend).
