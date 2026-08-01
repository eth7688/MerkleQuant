# Momentum Reflow UTC Daily Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make only momentum-reflow daily confirmation use Bitget `1Dutc` candles so its patterns match TradingView, while preserving every other Bitget daily caller's existing `1D` behavior.

**Architecture:** Add one optional Bitget-only granularity override to the shared `fetch_klines` boundary and pass it only from momentum reflow's daily-confirmation request. Keep closed-candle filtering keyed by the logical interval `1d`, so the override changes the exchange candle boundary without bypassing incomplete-candle protection.

**Tech Stack:** Python 3.12, pandas, requests, unittest, unittest.mock

## Global Constraints

- Only momentum reflow may opt into `1Dutc`.
- The global `"1d" -> "1D"` Bitget mapping must remain unchanged.
- Do not change 1H breakout, expansion, return-window, UI, persistence, API routes, or dependencies.
- Use test-first red-green execution and keep the patch surgical.

---

### Task 1: Route momentum-reflow daily confirmation through Bitget `1Dutc`

**Files:**
- Modify: `tests/test_momentum_reflow.py:1-18, 724-756`
- Modify: `screener.py:203-228, 478-495`
- Modify: `momentum_reflow.py:546-555`

**Interfaces:**
- Consumes: `fetch_klines(symbol, interval, limit, ..., bitget_granularity=None)`
- Produces: Bitget REST parameter `granularity="1Dutc"` only when momentum reflow requests its daily confirmation.

- [ ] **Step 1: Write failing routing tests**

Add `import screener`, update the existing momentum-reflow call assertion, and add focused default/override request tests:

```python
import screener

latest.assert_any_call(
    "OLDUSDT", "1d", 40, exchange="bitget", closed_only=True,
    market_type="futures", testnet=False, bitget_granularity="1Dutc",
)

@patch("screener.requests.get")
def test_bitget_daily_defaults_to_exchange_daily_boundary(self, get):
    response = Mock(status_code=200)
    response.json.return_value = {"code": "00000", "data": []}
    get.return_value = response
    screener.fetch_klines("TRXUSDT", "1d", 40, exchange="bitget")
    self.assertEqual(get.call_args.kwargs["params"]["granularity"], "1D")

@patch("screener.requests.get")
def test_bitget_daily_accepts_utc_boundary_override(self, get):
    response = Mock(status_code=200)
    response.json.return_value = {"code": "00000", "data": []}
    get.return_value = response
    screener.fetch_klines(
        "TRXUSDT", "1d", 40, exchange="bitget",
        bitget_granularity="1Dutc",
    )
    self.assertEqual(get.call_args.kwargs["params"]["granularity"], "1Dutc")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p 'test_momentum_reflow.py'
```

Expected: FAIL because momentum reflow does not pass `bitget_granularity="1Dutc"` and `fetch_klines` does not accept that keyword yet.

- [ ] **Step 3: Add the minimal Bitget override and use it from momentum reflow**

Change the public and private fetch boundaries:

```python
def fetch_klines(..., price_type=None, bitget_granularity=None):
    if ex == "bitget":
        return _fetch_klines_bitget(
            symbol, interval, limit, closed_only=closed_only,
            granularity=bitget_granularity,
        )

def _fetch_klines_bitget(
    symbol, interval, limit=200, closed_only=True, granularity=None
):
    granularity = granularity or {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W",
    }.get(interval, "1H")
```

Pass the override only on the momentum-reflow daily request:

```python
daily = fetch_klines(
    symbol,
    "1d",
    40,
    exchange="bitget",
    closed_only=True,
    market_type="futures",
    testnet=False,
    bitget_granularity="1Dutc",
)
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
python -m unittest discover -s tests -p 'test_momentum_reflow.py'
```

Expected: all tests pass.

- [ ] **Step 5: Verify behavior and regressions**

Run:

```powershell
python -m unittest discover -s tests -p 'test_momentum_reflow*.py'
python -m unittest discover -s tests
python -m py_compile momentum_reflow.py screener.py web_ui.py
git diff --check
```

Then fetch closed Bitget `1Dutc` data for `TRXUSDT` and `INUSDT` through the modified production function and print `daily_confirmation(..., "SHORT")`; TRX must no longer derive its panel confirmation from the Beijing-midnight `1D` candles.

- [ ] **Step 6: Commit the implementation**

```powershell
git add -- momentum_reflow.py screener.py tests/test_momentum_reflow.py
git commit -m "fix: align reflow daily confirmation with UTC"
```
