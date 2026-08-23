# Momentum Compression Rule Alignment Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task, and use test-driven-development for every behavior change.

**Goal:** Make the 15-minute momentum-compression scanner apply the approved documented contraction and EMA8-distance rules, reject the historical `LUMIAUSDT` false positive, and state the Binance Futures data source in the dashboard.

**Architecture:** Keep the existing Binance Futures market-data route, suffix-selection flow, fitted envelope, pool lifecycle, alert delivery, and persistence contracts. Replace only the two mismatched scalar calculations inside the pure evaluation module, carry the corrected EMA metric through hard filtering and quality scoring, and prove the behavior with boundary tests plus an immutable real-market replay fixture.

**Tech Stack:** Python 3.12, pandas, NumPy, `unittest`, Flask's inline vanilla JavaScript dashboard, Node.js renderer harness.

---

### Task 1: Align the pure contraction and EMA-distance rules

**Files:**
- Modify: `momentum_compression.py`
- Modify: `tests/test_momentum_compression.py`

**Step 1: Add failing contraction-ratio boundary tests**

Import the new private helper in `tests/test_momentum_compression.py` and add focused tests that construct raw OHLC windows without relying on the fitted envelope:

```python
from momentum_compression import _range_contraction_ratio


def range_frame(ranges):
    return pd.DataFrame({
        "h": [100.0 + value for value in ranges],
        "l": [100.0] * len(ranges),
    })


class CompressionRuleTests(unittest.TestCase):
    def test_range_contraction_accepts_exact_threshold(self):
        frame = range_frame([2.0] * 5 + [9.0] + [1.3] * 5)
        self.assertAlmostEqual(_range_contraction_ratio(frame), 0.65)

    def test_range_contraction_rejects_value_above_threshold(self):
        above = float(np.nextafter(0.65, math.inf))
        frame = range_frame([2.0] * 5 + [9.0] + [2.0 * above] * 5)
        self.assertGreater(_range_contraction_ratio(frame), 0.65)

    def test_range_contraction_excludes_middle_remainder(self):
        frame = range_frame([2.0] * 5 + [500.0, 700.0] + [1.0] * 5)
        self.assertAlmostEqual(_range_contraction_ratio(frame), 0.5)

    def test_range_contraction_rejects_nonpositive_first_mean(self):
        frame = range_frame([0.0] * 5 + [1.0] * 5)
        self.assertTrue(math.isinf(_range_contraction_ratio(frame)))
```

The 11-bar and 12-bar cases prove that only the first and last `floor(n / 3)` bars participate; middle remainders cannot influence the result.

**Step 2: Run the focused tests and confirm they fail for the missing helper**

Run:

```powershell
python -m unittest tests.test_momentum_compression.CompressionRuleTests
```

Expected: import or assertion failures because `_range_contraction_ratio` does not exist and the old implementation still uses envelope width.

**Step 3: Implement the range-based contraction helper**

Add this helper immediately before `_common_structure` in `momentum_compression.py`:

```python
def _range_contraction_ratio(frame: pd.DataFrame) -> float:
    third = len(frame) // 3
    if third <= 0:
        return float("inf")
    ranges = (
        frame["h"].reset_index(drop=True)
        - frame["l"].reset_index(drop=True)
    )
    first_mean = float(ranges.iloc[:third].mean())
    last_mean = float(ranges.iloc[-third:].mean())
    if not math.isfinite(first_mean) or first_mean <= 0:
        return float("inf")
    if not math.isfinite(last_mean):
        return float("inf")
    return last_mean / first_mean
```

In `_common_structure`, retain pivot discovery and `_fit_shifted_envelope`, but replace the width calculation with:

```python
contraction_ratio = _range_contraction_ratio(frame)
```

Do not alter `upper`, `lower`, `upper_slope`, `lower_slope`, pivot counts, or touch detection.

**Step 4: Replace the old EMA-distance test with exact threshold tests**

Replace `test_ema_distance_is_a_hard_rejection` with tests that isolate the documented last-close-to-EMA8 metric. Supply a valid `common` object to `_non_length_rules` so contraction does not obscure the EMA assertion:

```python
    def test_close_to_ema8_distance_accepts_exactly_one_atr(self):
        indicators = add_compression_indicators(valid_compression_frame())
        last = indicators.index[-1]
        indicators.loc[last, "c"] = (
            indicators.loc[last, "ema8"] + indicators.loc[last, "atr14"]
        )
        common = _common_structure(indicators, CompressionParams())
        common["contraction_ratio"] = 0.65
        rules = _non_length_rules(
            indicators, "LONG", CompressionParams(), common=common,
        )
        self.assertNotIn("EMA_DISTANCE_TOO_WIDE", rules["rejection_reasons"])
        self.assertAlmostEqual(rules["ema_distance_atr"], 1.0)

    def test_close_to_ema8_distance_rejects_above_one_atr(self):
        indicators = add_compression_indicators(valid_compression_frame())
        last = indicators.index[-1]
        multiplier = float(np.nextafter(1.0, math.inf))
        indicators.loc[last, "c"] = (
            indicators.loc[last, "ema8"]
            + indicators.loc[last, "atr14"] * multiplier
        )
        common = _common_structure(indicators, CompressionParams())
        common["contraction_ratio"] = 0.65
        rules = _non_length_rules(
            indicators, "LONG", CompressionParams(), common=common,
        )
        self.assertIn("EMA_DISTANCE_TOO_WIDE", rules["rejection_reasons"])
        self.assertGreater(rules["ema_distance_atr"], 1.0)
```

If floating-point recomputation makes `nextafter` collapse at the price scale, use `1.000001` for the rejecting case while retaining the exact-boundary passing case.

**Step 5: Run the EMA tests and confirm the old formula fails them**

Run:

```powershell
python -m unittest tests.test_momentum_compression.CompressionRuleTests.test_close_to_ema8_distance_accepts_exactly_one_atr tests.test_momentum_compression.CompressionRuleTests.test_close_to_ema8_distance_rejects_above_one_atr
```

Expected: failures because `_non_length_rules` currently compares EMA8 with EMA21 and does not return `ema_distance_atr`.

**Step 6: Compute the EMA metric once and propagate it**

In `_non_length_rules`, compute the metric from the last closed candle:

```python
last_atr = float(atr.iloc[-1])
ema_distance_atr = (
    abs(float(frame["c"].iloc[-1] - ema8.iloc[-1])) / last_atr
    if math.isfinite(last_atr) and last_atr > 0
    else float("inf")
)
if ema_distance_atr > params.max_ema_distance_atr:
    reasons.append("EMA_DISTANCE_TOO_WIDE")
```

Add `"ema_distance_atr": ema_distance_atr` to the returned rule metrics. In `_evaluate_prepared_side`, delete the second EMA8/EMA21 calculation and pass the metric returned by `_non_length_rules` unchanged to `_quality_score` and the result payload.

This creates one source of truth for the hard rule, API result, persisted candidate, and `ema_proximity` score.

**Step 7: Update the independent reference evaluator**

Keep the test oracle independent from production helpers:

- In `legacy_common_structure`, calculate first/last-third mean `h - l` directly instead of calling `_range_contraction_ratio`.
- In `legacy_non_length_rules`, calculate `abs(last close - last EMA8) / last ATR14`, apply the existing rejection code, and return the value as `ema_distance_atr`.
- In `legacy_evaluate_prepared_side`, remove its EMA8/EMA21 recomputation and consume the returned value.

Do not import or call `_range_contraction_ratio` from the legacy oracle. The exact-public-output oracle must remain capable of detecting a production regression.

**Step 8: Run the rule module tests**

Run:

```powershell
python -m unittest tests.test_momentum_compression
```

Expected: all tests pass, including exact comparison between production output and the independently calculated reference output.

**Step 9: Commit the pure-rule change**

```powershell
git add momentum_compression.py tests/test_momentum_compression.py
git commit -m "fix: align momentum compression rules"
```

---

### Task 2: Add the immutable LUMIAUSDT Binance Futures replay

**Files:**
- Create: `tests/fixtures/lumiausdt_binance_futures_15m_20260823_1300.json`
- Modify: `tests/test_momentum_compression.py`

**Step 1: Add the failing fixture replay test**

Add `hashlib` and `json` imports and define the fixture path. Add a test that verifies provenance and content before evaluating it:

```python
LUMIA_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "lumiausdt_binance_futures_15m_20260823_1300.json"
)


class CompressionHistoricalRegressionTests(unittest.TestCase):
    def test_lumia_binance_window_is_rejected_for_insufficient_contraction(self):
        payload = json.loads(LUMIA_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(payload["source"], "Binance Futures /fapi/v1/klines")
        self.assertEqual(payload["symbol"], "LUMIAUSDT")
        self.assertEqual(payload["interval"], "15m")
        self.assertEqual(payload["query"]["endTime"], 1787461199999)
        canonical = json.dumps(
            payload["ohlcv"], sort_keys=True, separators=(",", ":"),
        )
        self.assertEqual(
            hashlib.sha256(canonical.encode()).hexdigest(),
            payload["ohlcv_sha256"],
        )
        frame = pd.DataFrame(payload["ohlcv"])
        result = evaluate_side(
            payload["symbol"],
            "SHORT",
            frame,
            payload["live_price"],
            evaluated_at_ms=payload["evaluated_at_ms"],
            htf_alignment="UNKNOWN",
        )
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn(
            "INSUFFICIENT_CONTRACTION", result["rejection_reasons"],
        )
        indicators = add_compression_indicators(frame)
        former_window = indicators[
            (indicators["ot"] >= payload["former_window_start_time"])
            & (indicators["ot"] <= payload["former_window_end_time"])
        ].reset_index(drop=True)
        self.assertEqual(len(former_window), 16)
        self.assertAlmostEqual(
            _range_contraction_ratio(former_window),
            1.416058394160586,
        )
```

**Step 2: Run the test and confirm the fixture is missing**

Run:

```powershell
python -m unittest tests.test_momentum_compression.CompressionHistoricalRegressionTests
```

Expected: `FileNotFoundError` for the new fixture.

**Step 3: Capture and freeze the real Binance Futures data**

Fetch exactly this public endpoint once:

```text
https://fapi.binance.com/fapi/v1/klines?symbol=LUMIAUSDT&interval=15m&limit=220&endTime=1787461199999
```

Normalize every returned row to `ot`, `o`, `h`, `l`, `c`, `v`, preserving all 220 rows. Write the fixture with this metadata:

```json
{
  "source": "Binance Futures /fapi/v1/klines",
  "symbol": "LUMIAUSDT",
  "interval": "15m",
  "market_type": "futures",
  "testnet": false,
  "query": {"limit": 220, "endTime": 1787461199999},
  "evaluated_at_ms": 1787461205715,
  "live_price": 0.08666,
  "former_window_start_time": 1787446800000,
  "former_window_end_time": 1787460300000,
  "ohlcv_sha256": "3e01a5d0e36af34b66953cfd3f38142ec4bd2d11b5160532fd9b8edcf3844e65",
  "ohlcv": []
}
```

Before accepting the generated file, assert:

- row count is `220`;
- first `ot` is `1787263200000`;
- last `ot` is `1787460300000`;
- SHA-256 of `json.dumps(ohlcv, sort_keys=True, separators=(",", ":"))` is `3e01a5d0e36af34b66953cfd3f38142ec4bd2d11b5160532fd9b8edcf3844e65`.

If any value differs, stop and do not overwrite the expected digest: the upstream response or normalization has changed and must be investigated.

**Step 4: Run the historical regression**

Run:

```powershell
python -m unittest tests.test_momentum_compression.CompressionHistoricalRegressionTests
```

Expected: pass; the former 16-bar candidate has range contraction about `1.416058394160586`, and the full scanner replay rejects SHORT with `INSUFFICIENT_CONTRACTION`.

**Step 5: Commit the replay fixture**

```powershell
git add tests/fixtures/lumiausdt_binance_futures_15m_20260823_1300.json tests/test_momentum_compression.py
git commit -m "test: lock LUMIA compression regression"
```

---

### Task 3: Make the market-data source visible in the dashboard

**Files:**
- Modify: `web_ui.py`
- Modify: `tests/test_momentum_compression_integration.py`

**Step 1: Add a failing renderer assertion**

In `CompressionDashboardUiTests.test_renderer_escapes_payload_and_formats_nonfinite_values`, assert against the rendered control bar:

```python
self.assertIn("数据源：Binance Futures", rendered)
```

Also add the same source text to `test_sidebar_description_and_renderer_are_wired` so a later renderer rewrite cannot silently remove the disclosure.

**Step 2: Run the focused UI test and confirm it fails**

Run:

```powershell
python -m unittest tests.test_momentum_compression_integration.CompressionDashboardUiTests
```

Expected: failure because the dashboard currently says only “15M 已收盘结构 · 池内实时突破 · 只监控不交易”.

**Step 3: Add the fixed source label**

In `renderMomentumCompression`, change only the filter-label text to:

```javascript
15M 已收盘结构 · 数据源：Binance Futures · 池内实时突破 · 只监控不交易
```

Update the `compression_15m` sidebar description to include the same source disclosure. Do not change element IDs, classes, event handlers, API calls, or payload binding.

**Step 4: Run the UI integration tests**

Run:

```powershell
python -m unittest tests.test_momentum_compression_integration
```

Expected: all tests pass, including the Node.js renderer harness.

**Step 5: Commit the UI disclosure**

```powershell
git add web_ui.py tests/test_momentum_compression_integration.py
git commit -m "fix: show compression scanner data source"
```

---

### Task 4: Verify the complete local change and prepare deployment handoff

**Files:**
- Verify: `momentum_compression.py`
- Verify: `tests/test_momentum_compression.py`
- Verify: `tests/fixtures/lumiausdt_binance_futures_15m_20260823_1300.json`
- Verify: `web_ui.py`
- Verify: `tests/test_momentum_compression_integration.py`
- Verify: `docs/superpowers/specs/2026-08-23-momentum-compression-rule-alignment-design.md`

**Step 1: Run all momentum-compression tests**

```powershell
python -m unittest discover -s tests -p 'test_momentum_compression*.py'
```

Expected: all compression rule, service, monitor, store, alert, admin, and integration tests pass.

**Step 2: Run the complete test suite**

```powershell
python -m unittest discover -s tests -p 'test_*.py'
```

Expected: all tests pass. Do not dismiss failures as unrelated without reproducing them on the pre-change commit.

**Step 3: Compile changed Python modules**

```powershell
python -m py_compile momentum_compression.py web_ui.py tests/test_momentum_compression.py tests/test_momentum_compression_integration.py
```

Expected: exit code `0` and no output.

**Step 4: Review the final diff and repository status**

```powershell
git diff 3b72659 --check
git diff 3b72659 --stat
git status --short
```

Expected:

- no whitespace errors;
- only the files named in this plan changed;
- the pre-existing runtime lock files remain untracked and untouched;
- no config, state, SQLite, alert queue, or deployment file changed.

**Step 5: Perform an independent code review**

Use the requesting-code-review skill. The reviewer must verify:

- the new formula matches the approved design and source rules;
- `<= 0.65` and `<= 1.0 ATR` are inclusive;
- the middle remainder is excluded;
- hard filtering and quality scoring consume the same EMA metric;
- the LUMIA fixture is authentic and digest-locked;
- the fitted envelope, suffix selection, alerts, pool lifecycle, and trading code were not broadened.

Address any actionable findings and rerun Steps 1–4.

**Step 6: Stop before production deployment**

Report local commits, exact test counts, and the verified LUMIA replay result. Do not upload files, restart services, send WeChat messages, modify production configuration, or alter the live observation pool until the user gives a separate explicit deployment authorization.

After an authorized deployment, back up the server first, deploy only the reviewed files, restart only `macd-bot`, verify the live status and next completed scan, then update local `PROGRESS.md` with the change, backup/deployment action, and verification result.
