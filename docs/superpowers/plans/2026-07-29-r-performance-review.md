# R Performance Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shared R-based performance review panel to the normal trading dashboard and demo engine without changing any trading behavior.

**Architecture:** Put all R normalization, partial-exit grouping, range filtering, and summary formulas in a new pure Python module. `SqueezeBreakoutBot` exposes the same cached `r_performance` payload from both status methods, while `web_ui.py` renders one reusable panel synchronized with the existing equity range selector.

**Tech Stack:** Python 3.12, standard library `datetime`/`math`/`collections`, Flask status payloads, native HTML/CSS/JavaScript, Python `unittest`.

## Global Constraints

- Do not modify entry, stop-loss, take-profit, position sizing, exchange order, or strategy execution behavior.
- Do not add external Python, JavaScript, npm, or frontend-framework dependencies.
- Prefer persisted `r`; only fall back to `pnl / risk` when `r` is absent and `risk > 0`.
- Exclude records with invalid time, invalid/non-positive risk, voided status, or non-finite R; expose the exclusion count.
- Group partial exits by exact `signal_key`; records without `signal_key` remain independent.
- Attribute a lifecycle to a range by its final exit time, after grouping all of its exits.
- Open-position floating R must not affect realized R statistics.
- Normal and demo dashboards must use the same backend payload and frontend renderer.
- Preserve all existing element IDs and data bindings.
- CSS animation may only animate `transform` and `opacity`.
- Use Beijing wall-clock semantics already established by `bj_now()`; do not change project-wide timestamp behavior in this feature.

---

## File Structure

- Create `performance_metrics.py`: pure R record normalization, lifecycle grouping, summary calculations, and predefined range aggregation.
- Create `tests/test_performance_metrics.py`: formula, grouping, range, invalid-data, MFE, reason, and drawdown unit tests.
- Create `tests/test_r_performance_integration.py`: engine payload and shared UI wiring regression tests.
- Modify `trader.py`: cache and expose `r_performance` from both `get_fast_summary()` and `get_summary()`.
- Modify `web_ui.py`: shared R panel markup, styling, range selection, chart rendering, and expandable diagnostics.
- Modify `PROGRESS.md` only after an explicitly approved server deployment; record code changes, backup, deployment, restart, and verification.

### Task 1: Normalize exit records and group complete trade lifecycles

**Files:**
- Create: `performance_metrics.py`
- Create: `tests/test_performance_metrics.py`

**Interfaces:**
- Produces: `build_r_trade_lifecycles(trade_log: list[dict]) -> dict`
- Return keys: `source_record_count`, `valid_exit_record_count`, `excluded_records`, `lifecycles`
- Each lifecycle exposes: `key`, `direction`, `time`, `timestamp`, `r`, `mfe_r`, `mae_r`, `reasons`, `final_reason`, `reason_r`

- [ ] **Step 1: Write failing normalization and grouping tests**

```python
# tests/test_performance_metrics.py
import math
import unittest

from performance_metrics import build_r_trade_lifecycles


class RLifecycleTests(unittest.TestCase):
    def test_groups_partial_exits_by_signal_key_and_sums_actual_r(self):
        rows = [
            {
                "time": "2026-07-27T17:29:16+00:00",
                "signal_key": "PREDICTA|DIA|LONG|30m|1",
                "direction": "LONG",
                "risk": 74,
                "pnl": 234.12,
                "r": 3.1639,
                "mfe_r": 6.3385,
                "mae_r": 0.5956,
                "reason": "二阶减仓50%",
            },
            {
                "time": "2026-07-27T20:44:24+00:00",
                "signal_key": "PREDICTA|DIA|LONG|30m|1",
                "direction": "LONG",
                "risk": 74,
                "pnl": 107.17,
                "r": 1.4482,
                "mfe_r": 6.7555,
                "mae_r": 0.5956,
                "reason": "击穿动态追踪防线",
            },
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual(result["source_record_count"], 2)
        self.assertEqual(result["valid_exit_record_count"], 2)
        self.assertEqual(result["excluded_records"], 0)
        self.assertEqual(len(result["lifecycles"]), 1)
        trade = result["lifecycles"][0]
        self.assertAlmostEqual(trade["r"], 4.6121, places=4)
        self.assertEqual(trade["mfe_r"], 6.7555)
        self.assertEqual(trade["mae_r"], 0.5956)
        self.assertEqual(trade["final_reason"], "击穿动态追踪防线")
        self.assertEqual(trade["reason_r"]["二阶减仓50%"], 3.1639)
        self.assertEqual(trade["reason_r"]["击穿动态追踪防线"], 1.4482)

    def test_legacy_records_without_signal_key_remain_independent(self):
        rows = [
            {"time": "2026-07-20T10:00:00+00:00", "risk": 100, "pnl": 50, "direction": "LONG"},
            {"time": "2026-07-20T10:05:00+00:00", "risk": 100, "pnl": 25, "direction": "LONG"},
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual(len(result["lifecycles"]), 2)
        self.assertEqual([x["r"] for x in result["lifecycles"]], [0.5, 0.25])

    def test_excludes_voided_invalid_risk_time_and_non_finite_r(self):
        rows = [
            {"time": "2026-07-20T10:00:00+00:00", "risk": 100, "r": 1},
            {"time": "2026-07-20T10:01:00+00:00", "risk": 0, "r": 1},
            {"time": "bad-time", "risk": 100, "r": 1},
            {"time": "2026-07-20T10:03:00+00:00", "risk": 100, "r": math.inf},
            {"time": "2026-07-20T10:04:00+00:00", "risk": 100, "r": 1, "voided": True},
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual(result["valid_exit_record_count"], 1)
        self.assertEqual(result["excluded_records"], 4)
        self.assertEqual(len(result["lifecycles"]), 1)
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run:

```powershell
python -m unittest discover -s tests -p "test_performance_metrics.py" -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'performance_metrics'`.

- [ ] **Step 3: Implement exact normalization and lifecycle grouping**

```python
# performance_metrics.py
import math
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _record_r(record: dict) -> Optional[float]:
    risk = _finite_float(record.get("risk"))
    if risk is None or risk <= 0:
        return None
    persisted = _finite_float(record.get("r"))
    if persisted is not None:
        return persisted
    pnl = _finite_float(record.get("pnl"))
    return None if pnl is None else pnl / risk


def build_r_trade_lifecycles(trade_log: list[dict]) -> dict:
    groups: OrderedDict[str, dict] = OrderedDict()
    valid_exit_record_count = 0
    excluded_records = 0

    for index, record in enumerate(trade_log or []):
        if not isinstance(record, dict) or record.get("voided"):
            excluded_records += 1
            continue
        closed_at = _parse_time(record.get("time"))
        exit_r = _record_r(record)
        if closed_at is None or exit_r is None:
            excluded_records += 1
            continue

        valid_exit_record_count += 1
        signal_key = str(record.get("signal_key") or "").strip()
        group_key = signal_key or f"legacy:{index}"
        reason = str(record.get("reason") or "未标注").strip() or "未标注"
        mfe_r = _finite_float(record.get("mfe_r"))
        mae_r = _finite_float(record.get("mae_r"))

        trade = groups.setdefault(group_key, {
            "key": group_key,
            "direction": str(record.get("direction") or "").upper(),
            "time": closed_at.isoformat(),
            "timestamp": closed_at.timestamp(),
            "r": 0.0,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "reasons": [],
            "final_reason": reason,
            "reason_r": {},
        })
        trade["r"] += exit_r
        trade["mfe_r"] = max(trade["mfe_r"], mfe_r or 0.0)
        trade["mae_r"] = max(trade["mae_r"], mae_r or 0.0)
        trade["reason_r"][reason] = trade["reason_r"].get(reason, 0.0) + exit_r
        if reason not in trade["reasons"]:
            trade["reasons"].append(reason)
        if closed_at.timestamp() >= trade["timestamp"]:
            trade["time"] = closed_at.isoformat()
            trade["timestamp"] = closed_at.timestamp()
            trade["final_reason"] = reason
            if record.get("direction"):
                trade["direction"] = str(record["direction"]).upper()

    lifecycles = sorted(groups.values(), key=lambda item: item["timestamp"])
    for trade in lifecycles:
        trade["r"] = round(trade["r"], 6)
        trade["mfe_r"] = round(trade["mfe_r"], 6)
        trade["mae_r"] = round(trade["mae_r"], 6)
        trade["reason_r"] = {
            key: round(value, 6) for key, value in trade["reason_r"].items()
        }

    return {
        "source_record_count": len(trade_log or []),
        "valid_exit_record_count": valid_exit_record_count,
        "excluded_records": excluded_records,
        "lifecycles": lifecycles,
    }
```

- [ ] **Step 4: Run lifecycle tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_performance_metrics.py" -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit lifecycle normalization**

```powershell
git add performance_metrics.py tests/test_performance_metrics.py
git commit -m "feat: group exits into R trade lifecycles"
```

### Task 2: Calculate summaries and synchronized time ranges

**Files:**
- Modify: `performance_metrics.py`
- Modify: `tests/test_performance_metrics.py`

**Interfaces:**
- Consumes: `build_r_trade_lifecycles(trade_log)`
- Produces: `summarize_r_performance_ranges(trade_log: list[dict], now: datetime | None = None) -> dict`
- Result shape: `{"status": str, "ranges": {"7": summary, "30": summary, "90": summary, "180": summary, "365": summary, "all": summary}}`

- [ ] **Step 1: Add failing formula and range tests**

```python
# Append to tests/test_performance_metrics.py
from datetime import datetime, timezone

from performance_metrics import summarize_r_performance_ranges


class RSummaryTests(unittest.TestCase):
    NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    def test_calculates_core_metrics_drawdown_streak_and_breakdowns(self):
        rows = [
            {"time": "2026-07-20T10:00:00+00:00", "risk": 100, "r": 2.0, "mfe_r": 3.0, "direction": "LONG", "reason": "ATR"},
            {"time": "2026-07-21T10:00:00+00:00", "risk": 100, "r": -1.0, "mfe_r": 0.2, "direction": "SHORT", "reason": "SL"},
            {"time": "2026-07-22T10:00:00+00:00", "risk": 100, "r": -0.5, "mfe_r": 0.4, "direction": "SHORT", "reason": "SL"},
            {"time": "2026-07-23T10:00:00+00:00", "risk": 100, "r": 1.0, "mfe_r": 2.0, "direction": "LONG", "reason": "结构退出"},
            {"time": "2026-07-24T10:00:00+00:00", "risk": 100, "r": 0.0, "mfe_r": 0.8, "direction": "LONG", "reason": "保本"},
        ]

        summary = summarize_r_performance_ranges(rows, self.NOW)["ranges"]["all"]

        self.assertEqual(summary["valid_trade_count"], 5)
        self.assertAlmostEqual(summary["net_r"], 1.5)
        self.assertAlmostEqual(summary["expectancy_r"], 0.3)
        self.assertAlmostEqual(summary["win_rate"], 40.0)
        self.assertAlmostEqual(summary["average_win_r"], 1.5)
        self.assertAlmostEqual(summary["average_loss_r"], -0.75)
        self.assertAlmostEqual(summary["average_payoff_ratio"], 2.0)
        self.assertAlmostEqual(summary["profit_factor"], 2.0)
        self.assertAlmostEqual(summary["max_drawdown_r"], 1.5)
        self.assertEqual(summary["max_consecutive_losses"], 2)
        self.assertEqual(summary["largest_win_r"], 2.0)
        self.assertEqual(summary["largest_loss_r"], -1.0)
        self.assertEqual(summary["direction_breakdown"]["LONG"]["net_r"], 3.0)
        self.assertEqual(summary["direction_breakdown"]["SHORT"]["net_r"], -1.5)
        self.assertEqual(summary["exit_reason_breakdown"]["SL"]["net_r"], -1.5)
        self.assertAlmostEqual(summary["average_mfe_r"], 1.28)
        self.assertAlmostEqual(summary["mfe_capture_efficiency"], 0.6)

    def test_groups_before_filtering_and_uses_final_exit_time_for_range(self):
        key = "PREDICTA|PARTIAL|LONG|30m|1"
        rows = [
            {"time": "2026-07-01T10:00:00+00:00", "signal_key": key, "risk": 100, "r": 1.0},
            {"time": "2026-07-28T10:00:00+00:00", "signal_key": key, "risk": 100, "r": 0.5},
            {"time": "2026-07-10T10:00:00+00:00", "risk": 100, "r": -1.0},
        ]

        result = summarize_r_performance_ranges(rows, self.NOW)

        self.assertEqual(result["ranges"]["7"]["valid_trade_count"], 1)
        self.assertAlmostEqual(result["ranges"]["7"]["net_r"], 1.5)
        self.assertEqual(result["ranges"]["30"]["valid_trade_count"], 2)

    def test_returns_none_for_undefined_ratios(self):
        rows = [
            {"time": "2026-07-28T10:00:00+00:00", "risk": 100, "r": 1.0},
        ]

        summary = summarize_r_performance_ranges(rows, self.NOW)["ranges"]["all"]

        self.assertIsNone(summary["average_loss_r"])
        self.assertIsNone(summary["average_payoff_ratio"])
        self.assertIsNone(summary["profit_factor"])
```

- [ ] **Step 2: Run summary tests and verify the function is missing**

Run:

```powershell
python -m unittest discover -s tests -p "test_performance_metrics.py" -v
```

Expected: FAIL with `ImportError` for `summarize_r_performance_ranges`.

- [ ] **Step 3: Implement summary helpers and predefined ranges**

Add to `performance_metrics.py`:

```python
from datetime import timedelta

RANGE_DAYS = (7, 30, 90, 180, 365)


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _summarize_lifecycles(lifecycles: list[dict], metadata: dict) -> dict:
    values = [float(item["r"]) for item in lifecycles]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average_win = gross_profit / len(wins) if wins else None
    average_loss = sum(losses) / len(losses) if losses else None

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    current_losses = 0
    max_losses = 0
    points = []
    directions = {}
    reasons = {}

    for trade in lifecycles:
        trade_r = float(trade["r"])
        cumulative += trade_r
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        points.append({"time": trade["time"], "r": _rounded(cumulative)})

        if trade_r < 0:
            current_losses += 1
            max_losses = max(max_losses, current_losses)
        else:
            current_losses = 0

        direction = trade["direction"] or "UNKNOWN"
        direction_row = directions.setdefault(direction, {"trades": 0, "net_r": 0.0})
        direction_row["trades"] += 1
        direction_row["net_r"] += trade_r

        for reason, reason_r in trade["reason_r"].items():
            reason_row = reasons.setdefault(reason, {"trades": 0, "net_r": 0.0})
            reason_row["trades"] += 1
            reason_row["net_r"] += reason_r

    positive_mfe = [trade for trade in lifecycles if trade["r"] > 0 and trade["mfe_r"] > 0]
    mfe_denominator = sum(trade["mfe_r"] for trade in positive_mfe)
    capture = (
        sum(trade["r"] for trade in positive_mfe) / mfe_denominator
        if mfe_denominator > 0 else None
    )

    for row in directions.values():
        row["net_r"] = _rounded(row["net_r"])
    for row in reasons.values():
        row["net_r"] = _rounded(row["net_r"])

    count = len(values)
    return {
        **metadata,
        "valid_trade_count": count,
        "net_r": _rounded(sum(values)),
        "expectancy_r": _rounded(sum(values) / count) if count else None,
        "win_rate": _rounded(len(wins) / count * 100) if count else None,
        "average_win_r": _rounded(average_win) if average_win is not None else None,
        "average_loss_r": _rounded(average_loss) if average_loss is not None else None,
        "average_payoff_ratio": (
            _rounded(average_win / abs(average_loss))
            if average_win is not None and average_loss not in (None, 0) else None
        ),
        "profit_factor": _rounded(gross_profit / gross_loss) if gross_loss > 0 else None,
        "max_drawdown_r": _rounded(max_drawdown),
        "max_consecutive_losses": max_losses,
        "largest_win_r": _rounded(max(wins)) if wins else None,
        "largest_loss_r": _rounded(min(losses)) if losses else None,
        "direction_breakdown": directions,
        "exit_reason_breakdown": reasons,
        "average_mfe_r": (
            _rounded(sum(trade["mfe_r"] for trade in lifecycles) / count)
            if count else None
        ),
        "mfe_capture_efficiency": _rounded(capture) if capture is not None else None,
        "cumulative_r_points": points,
    }


def summarize_r_performance_ranges(
    trade_log: list[dict],
    now: Optional[datetime] = None,
) -> dict:
    grouped = build_r_trade_lifecycles(trade_log)
    current = now or (datetime.now(timezone.utc) + timedelta(hours=8))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    metadata = {
        "source_record_count": grouped["source_record_count"],
        "valid_exit_record_count": grouped["valid_exit_record_count"],
        "excluded_records": grouped["excluded_records"],
    }
    ranges = {
        "all": _summarize_lifecycles(grouped["lifecycles"], metadata),
    }
    now_timestamp = current.timestamp()
    for days in RANGE_DAYS:
        cutoff = now_timestamp - days * 86400
        selected = [
            trade for trade in grouped["lifecycles"]
            if trade["timestamp"] >= cutoff
        ]
        ranges[str(days)] = _summarize_lifecycles(selected, metadata)

    return {
        "status": "ok" if grouped["lifecycles"] else "empty",
        "ranges": ranges,
    }
```

- [ ] **Step 4: Run all metric tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_performance_metrics.py" -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit R formulas and ranges**

```powershell
git add performance_metrics.py tests/test_performance_metrics.py
git commit -m "feat: calculate ranged R performance metrics"
```

### Task 3: Expose one cached payload from both engine summaries

**Files:**
- Modify: `trader.py:20-35`
- Modify: `trader.py:1105-1125`
- Modify: `trader.py:2158-2170`
- Modify: `trader.py:8213-8448`
- Modify: `trader.py:8450-8640`
- Create: `tests/test_r_performance_integration.py`

**Interfaces:**
- Consumes: `summarize_r_performance_ranges(self.trade_log, now=bj_now())`
- Produces: `SqueezeBreakoutBot._r_performance_summary() -> dict`
- Adds status field: `"r_performance": r_performance`

- [ ] **Step 1: Write failing cache and source-wiring tests**

```python
# tests/test_r_performance_integration.py
import unittest
from pathlib import Path
from unittest.mock import patch

from trader import SqueezeBreakoutBot


class RPerformanceIntegrationTests(unittest.TestCase):
    def test_engine_caches_r_summary_for_fast_polling(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.trade_log = [{"time": "2026-07-29T10:00:00+00:00", "risk": 100, "r": 1}]
        bot._fast_r_performance_cache_ts = 0.0
        bot._fast_r_performance_cache = {}
        bot._log_ready = False

        expected = {"status": "ok", "ranges": {"all": {"net_r": 1}}}
        with patch("trader.summarize_r_performance_ranges", return_value=expected) as calculate:
            first = bot._r_performance_summary()
            second = bot._r_performance_summary()

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        calculate.assert_called_once()

    def test_both_status_methods_publish_the_same_named_payload(self):
        source = Path("trader.py").read_text(encoding="utf-8")

        self.assertEqual(source.count('"r_performance": r_performance'), 2)
        self.assertGreaterEqual(source.count("r_performance = self._r_performance_summary()"), 2)
```

- [ ] **Step 2: Run integration tests and verify missing engine method**

Run:

```powershell
python -m unittest discover -s tests -p "test_r_performance_integration.py" -v
```

Expected: FAIL because `_r_performance_summary` and the payload fields do not exist.

- [ ] **Step 3: Import the pure calculator and initialize cache fields**

Add near the existing local imports:

```python
from performance_metrics import summarize_r_performance_ranges
```

Add beside the equity/drawdown caches in `SqueezeBreakoutBot.__init__`:

```python
self._fast_r_performance_cache_ts: float = 0.0
self._fast_r_performance_cache: dict = {}
```

- [ ] **Step 4: Add the guarded two-second cache method**

Add beside `_trade_pnl_value`:

```python
def _r_performance_summary(self) -> dict:
    now_ts = time.time()
    if (
        self._fast_r_performance_cache
        and now_ts - self._fast_r_performance_cache_ts < 2
    ):
        return self._fast_r_performance_cache
    try:
        payload = summarize_r_performance_ranges(self.trade_log, now=bj_now())
    except Exception as exc:
        if getattr(self, "_log_ready", False):
            self._log.warning(f"R绩效统计失败: {exc}")
        payload = {"status": "error", "ranges": {}}
    self._fast_r_performance_cache = payload
    self._fast_r_performance_cache_ts = now_ts
    return payload
```

- [ ] **Step 5: Add the identical payload to both summary methods**

In both `get_fast_summary()` and `get_summary()`, calculate once before the returned dictionary:

```python
r_performance = self._r_performance_summary()
```

Add this key to each returned dictionary:

```python
"r_performance": r_performance,
```

- [ ] **Step 6: Run integration and existing summary regression tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_r_performance_integration.py" -v
python -m unittest discover -s tests -p "test_exchange_entry_price_sync.py" -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit engine payload integration**

```powershell
git add trader.py tests/test_r_performance_integration.py
git commit -m "feat: expose cached R performance summary"
```

### Task 4: Render the balanced R review panel in both dashboards

**Files:**
- Modify: `web_ui.py:1111-1140`
- Modify: `web_ui.py:1711-1718`
- Modify: `web_ui.py:1892-1902`
- Modify: `web_ui.py:2085-2140`
- Modify: `web_ui.py:3175-3182`
- Modify: `tests/test_r_performance_integration.py`

**Interfaces:**
- Consumes: `d.r_performance.ranges["7"|"30"|"90"|"180"|"365"|"all"]`
- Produces: `rPerformancePanelHtml()`, `getRRangeSummary(d)`, `renderRPerformance(d)`
- Reuses: `_equityRangeDays`, `setEquityRange(days, btn)`, `renderEquityReview(d)`

- [ ] **Step 1: Add failing shared-renderer source tests**

Append to `tests/test_r_performance_integration.py`:

```python
class RPerformanceUiTests(unittest.TestCase):
    def test_normal_and_demo_views_reuse_one_r_panel(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")

        self.assertIn("function rPerformancePanelHtml()", source)
        self.assertIn("function getRRangeSummary(d)", source)
        self.assertIn("function renderRPerformance(d)", source)
        self.assertEqual(source.count("h+=rPerformancePanelHtml();"), 2)
        self.assertIn("renderRPerformance(d);", source)

    def test_panel_contains_confirmed_core_metrics_and_diagnostics(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")

        for token in (
            "rNetValue",
            "rExpectancyValue",
            "rPayoffValue",
            "rProfitFactorValue",
            "rAverageValue",
            "rDrawdownValue",
            "rPerformanceChart",
            "rPerformanceDetails",
        ):
            self.assertIn(token, source)
```

- [ ] **Step 2: Run UI tests and verify renderer is missing**

Run:

```powershell
python -m unittest discover -s tests -p "test_r_performance_integration.py" -v
```

Expected: FAIL because the shared renderer and IDs do not exist.

- [ ] **Step 3: Add responsive R panel styling**

Add after the existing `.eq-*` styles:

```css
.r-panel{background:linear-gradient(180deg,rgba(18,22,27,.97),rgba(11,14,18,.99));border:1px solid rgba(52,211,153,.18);border-radius:10px;padding:14px 16px;margin:-8px 0 18px;box-shadow:inset 0 1px 0 rgba(255,255,255,.025),0 8px 24px rgba(0,0,0,.28)}
.r-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.r-title{font-size:12px;font-weight:800;letter-spacing:.08em;color:var(--s-green)}
.r-meta{font-size:9px;color:var(--muted);font-family:var(--font-mono)}
.r-metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:7px}
.r-card{min-width:0;padding:9px;background:rgba(4,7,10,.72);border:1px solid rgba(255,255,255,.045);border-radius:8px}
.r-label{font-size:8px;color:var(--text2);font-weight:700;white-space:nowrap;margin-bottom:4px}
.r-value{font:800 15px var(--font-mono);font-variant-numeric:tabular-nums}
.r-value.g{color:var(--s-green)}.r-value.r{color:var(--s-red)}.r-value.neu{color:#fff}.r-value.p{color:var(--s-purple)}
.r-chart-wrap{height:150px;margin-top:10px;border-top:1px solid rgba(255,255,255,.04)}
.r-chart{width:100%;height:100%;display:block}
.r-details{margin-top:8px;border-top:1px solid rgba(255,255,255,.04);padding-top:7px}
.r-details summary{cursor:pointer;color:var(--text2);font-size:10px}
.r-detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}
.r-detail-box{font-size:9px;color:var(--muted);background:rgba(4,7,10,.55);padding:8px;border-radius:7px}
@media(max-width:900px){.r-metrics{grid-template-columns:repeat(3,1fr)}}
@media(max-width:600px){.r-metrics{grid-template-columns:repeat(2,1fr)}.r-detail-grid{grid-template-columns:1fr}.r-head{align-items:flex-start;flex-direction:column}}
```

- [ ] **Step 4: Add one reusable markup factory and insert it twice**

Add before the trader/dashboard render functions:

```javascript
function rPerformancePanelHtml(){
  return '<section class="r-panel" id="rPerformancePanel">'
    +'<div class="r-head"><div class="r-title">R PERFORMANCE</div><div class="r-meta" id="rPerformanceMeta">--</div></div>'
    +'<div class="r-metrics">'
    +'<div class="r-card"><div class="r-label">累计净 R</div><div class="r-value" id="rNetValue">--</div></div>'
    +'<div class="r-card"><div class="r-label">每笔期望</div><div class="r-value" id="rExpectancyValue">--</div></div>'
    +'<div class="r-card" title="平均盈利R ÷ 平均亏损R绝对值"><div class="r-label">平均盈亏比</div><div class="r-value p" id="rPayoffValue">--</div></div>'
    +'<div class="r-card" title="全部盈利R ÷ 全部亏损R绝对值"><div class="r-label">Profit Factor</div><div class="r-value p" id="rProfitFactorValue">--</div></div>'
    +'<div class="r-card"><div class="r-label">平均盈利 / 亏损</div><div class="r-value" id="rAverageValue">--</div></div>'
    +'<div class="r-card"><div class="r-label">最大回撤 R</div><div class="r-value r" id="rDrawdownValue">--</div></div>'
    +'</div>'
    +'<div class="r-chart-wrap" id="rPerformanceChart"><div class="eq-empty">等待有效 R 交易记录</div></div>'
    +'<details class="r-details" id="rPerformanceDetails"><summary>更多复盘</summary><div class="r-detail-grid" id="rPerformanceDetailGrid"></div></details>'
    +'</section>';
}
```

Immediately after each existing equity panel closes, add:

```javascript
h+=rPerformancePanelHtml();
```

- [ ] **Step 5: Implement range selection, safe formatting, SVG curve, and diagnostics**

Add near `setEquityRange` and `renderEquityReview`:

```javascript
function getRRangeSummary(d){
  var ranges=((d||{}).r_performance||{}).ranges||{};
  var key=Number(_equityRangeDays||0)>0?String(_equityRangeDays):'all';
  return ranges[key]||null;
}
function fmtRValue(value,signed){
  if(value===null||value===undefined||!isFinite(Number(value))) return '--';
  var n=Number(value), prefix=signed?(n>0?'+':n<0?'−':''):(n<0?'−':'');
  return prefix+Math.abs(n).toFixed(2)+'R';
}
function fmtRatio(value){
  return value===null||value===undefined||!isFinite(Number(value))?'--':Number(value).toFixed(2);
}
function setRValue(id,text,value,tone){
  var el=document.getElementById(id);
  if(!el) return;
  el.textContent=text;
  var n=Number(value);
  el.className='r-value '+(tone||(isFinite(n)?(n>0?'g':n<0?'r':'neu'):'neu'));
}
function renderRPerformance(d){
  var s=getRRangeSummary(d), meta=document.getElementById('rPerformanceMeta');
  var chart=document.getElementById('rPerformanceChart');
  if(!chart) return;
  if(!s){
    if(meta) meta.textContent='无统计数据';
    chart.innerHTML='<div class="eq-empty">等待有效 R 交易记录</div>';
    return;
  }
  if(meta){
    meta.textContent='完整交易 '+Number(s.valid_trade_count||0)
      +' · 胜率 '+(s.win_rate===null||s.win_rate===undefined?'--':Number(s.win_rate).toFixed(1)+'%')
      +' · 连亏峰值 '+Number(s.max_consecutive_losses||0)
      +' · 有效记录 '+Number(s.valid_exit_record_count||0)+' / '+Number(s.source_record_count||0)
      +(Number(s.excluded_records||0)>0?' · 未计入 '+Number(s.excluded_records)+' 条':'');
  }
  setRValue('rNetValue',fmtRValue(s.net_r,true),s.net_r);
  setRValue('rExpectancyValue',fmtRValue(s.expectancy_r,true),s.expectancy_r);
  setRValue('rPayoffValue',fmtRatio(s.average_payoff_ratio),s.average_payoff_ratio,'p');
  setRValue('rProfitFactorValue',fmtRatio(s.profit_factor),s.profit_factor,'p');
  document.getElementById('rAverageValue').textContent=fmtRValue(s.average_win_r,true)+' / '+fmtRValue(s.average_loss_r,true);
  setRValue('rDrawdownValue',fmtRValue(-Math.abs(Number(s.max_drawdown_r||0)),true),-Math.abs(Number(s.max_drawdown_r||0)),'r');

  var points=(s.cumulative_r_points||[]).slice();
  if(!points.length){
    chart.innerHTML='<div class="eq-empty">等待有效 R 交易记录</div>';
  }else{
    var W=860,H=150,L=44,R=12,T=12,B=22, values=points.map(function(p){return Number(p.r||0);});
    values.push(0);
    var min=Math.min.apply(null,values),max=Math.max.apply(null,values);
    if(max-min<1){max+=.5;min-=.5}
    var span=max-min,pad=span*.12;min-=pad;max+=pad;
    var coords=points.map(function(p,i){
      return [L+(points.length===1?0:i/(points.length-1))*(W-L-R),T+(max-Number(p.r||0))/(max-min)*(H-T-B)];
    });
    var zero=T+(max-0)/(max-min)*(H-T-B);
    var poly=coords.map(function(c){return c[0].toFixed(1)+','+c[1].toFixed(1)}).join(' ');
    chart.innerHTML='<svg class="r-chart" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'
      +'<line x1="'+L+'" y1="'+zero.toFixed(1)+'" x2="'+(W-R)+'" y2="'+zero.toFixed(1)+'" stroke="rgba(148,163,184,.45)" stroke-dasharray="4 5"/>'
      +'<polyline points="'+poly+'" fill="none" stroke="'+(Number(s.net_r||0)>=0?'#34d399':'#f87171')+'" stroke-width="2"/>'
      +'</svg>';
  }

  var directions=s.direction_breakdown||{}, reasons=s.exit_reason_breakdown||{};
  var directionText=Object.keys(directions).map(function(k){return k+' '+fmtRValue(directions[k].net_r,true)}).join('<br>')||'--';
  var reasonText=Object.keys(reasons).sort(function(a,b){return Math.abs(reasons[b].net_r)-Math.abs(reasons[a].net_r)}).map(function(k){return k+' '+fmtRValue(reasons[k].net_r,true)}).join('<br>')||'--';
  var detail=document.getElementById('rPerformanceDetailGrid');
  if(detail) detail.innerHTML='<div class="r-detail-box"><b>方向贡献</b><br>'+directionText+'</div>'
    +'<div class="r-detail-box"><b>平仓原因贡献</b><br>'+reasonText+'</div>'
    +'<div class="r-detail-box"><b>MFE 与极值</b><br>平均 MFE '+fmtRValue(s.average_mfe_r,false)
    +'<br>捕获效率 '+(s.mfe_capture_efficiency===null||s.mfe_capture_efficiency===undefined?'--':(Number(s.mfe_capture_efficiency)*100).toFixed(1)+'%')
    +'<br>最佳 '+fmtRValue(s.largest_win_r,true)+' · 最差 '+fmtRValue(s.largest_loss_r,true)+'</div>';
}
```

At the end of `renderEquityReview(d)`, call:

```javascript
renderRPerformance(d);
```

This automatically keeps R and equity ranges synchronized because `setEquityRange()` already calls `renderEquityReview(_lastTraderData)`.

- [ ] **Step 6: Run UI and backend regression tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_r_performance_integration.py" -v
python -m unittest discover -s tests -v
```

Expected: all tests PASS.

- [ ] **Step 7: Perform local browser verification**

Run the existing Flask UI using the project’s normal local command:

```powershell
python web_ui.py
```

Verify in a real browser:

- normal trader view and demo view both show one R panel;
- 1W, 1M, and ALL update the R metrics and curve together with equity;
- the current 54-record server snapshot, when supplied to the pure calculator, produces 52 lifecycles and the approved baseline values;
- desktop shows six columns, tablet three columns, and phone two columns;
- “更多复盘” expands without shifting or breaking existing data bindings;
- no browser console error contains `NaN`, `Infinity`, or missing-element exceptions.

Stop the local server after verification.

- [ ] **Step 8: Commit the shared R panel**

```powershell
git add web_ui.py tests/test_r_performance_integration.py
git commit -m "feat: add R performance review panel"
```

### Task 5: Final verification and deployment handoff

**Files:**
- Verify: `performance_metrics.py`
- Verify: `trader.py`
- Verify: `web_ui.py`
- Verify: `tests/test_performance_metrics.py`
- Verify: `tests/test_r_performance_integration.py`
- Modify after approved deployment only: `PROGRESS.md`

**Interfaces:**
- Verifies the complete `r_performance` data path from trade records to both dashboards.
- Does not authorize or perform server deployment without a separate explicit approval.

- [ ] **Step 1: Run syntax and full test verification**

```powershell
python -m py_compile performance_metrics.py trader.py web_ui.py
python -m unittest discover -s tests -v
git diff --check
git status --short
```

Expected:

- all three files compile;
- all tests PASS;
- `git diff --check` returns no errors;
- only intentional feature files are modified.

- [ ] **Step 2: Recalculate the approved production snapshot**

Verify the immutable approval-time snapshot captured on 2026-07-29:

```powershell
Get-FileHash -Algorithm SHA256 ".deploy\r-baseline-2026-07-29.jsonl"
```

Expected SHA-256:

```text
EC60C5789800D4C3B34E5BBEEAC751D799B389FB362AFFE5C214D5C1148A4D1D
```

Then calculate its ALL summary:

```powershell
python -c "import json; from datetime import datetime, timezone; from pathlib import Path; from performance_metrics import summarize_r_performance_ranges; rows=[json.loads(x) for x in Path('.deploy/r-baseline-2026-07-29.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]; print(json.dumps(summarize_r_performance_ranges(rows, datetime(2026,7,29,12,0,tzinfo=timezone.utc))['ranges']['all'], ensure_ascii=False, indent=2))"
```

Expected approved baseline for the 2026-07-29 snapshot:

```text
valid_exit_record_count = 54
valid_trade_count = 52
net_r = 3.1941
expectancy_r = 0.0614
average_win_r = 1.2834
average_loss_r = -0.6669
average_payoff_ratio = 1.9243
profit_factor = 1.1842
max_drawdown_r = 6.3729
```

- [ ] **Step 3: Review the diff for strategy isolation**

```powershell
git diff 6409a8c..HEAD -- performance_metrics.py trader.py web_ui.py tests/test_performance_metrics.py tests/test_r_performance_integration.py
```

Expected: no changes inside entry evaluation, stop management, take-profit, quantity calculation, exchange client, or order placement functions.

- [ ] **Step 4: Request explicit deployment approval**

Report local verification evidence and ask the user to approve server backup, upload, and service restart. Do not deploy in this step.

- [ ] **Step 5: After approval, deploy and record the result**

Follow the established AXIOM workflow:

1. back up the server code and runtime files;
2. upload only the locally verified feature files;
3. restart `macd-bot` and `macd-admin`;
4. verify both services are active and both status endpoints contain `r_performance`;
5. compare live ALL metrics to a direct server-side calculation;
6. update local `PROGRESS.md` with the exact changes, backup path, deployed files, service actions, and verification result;
7. commit the `PROGRESS.md` deployment record.
