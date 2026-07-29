import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from trader import SqueezeBreakoutBot


def run_r_renderer_probe(scenario):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    helpers = source[
        source.index("function getRRangeSummary(d)"):
        source.index("function fmtMoney(v, signed)")
    ]
    renderer = source[
        source.index("function renderRPerformance(d)"):
        source.index("function renderEquityReview(d)")
    ]
    script = f"""
var _equityRangeDays=7;
var nodes={{}};
[
  'rPerformanceMeta','rNetValue','rExpectancyValue','rPayoffValue',
  'rProfitFactorValue','rAverageValue','rDrawdownValue',
  'rPerformanceChart','rPerformanceDetailGrid'
].forEach(function(id){{
  nodes[id]={{textContent:'',innerHTML:'',className:''}};
}});
global.document={{getElementById:function(id){{return nodes[id]||null;}}}};
{helpers}
{renderer}
function summary(net){{
  return {{
    valid_trade_count:2,win_rate:50,max_consecutive_losses:1,
    valid_exit_record_count:2,source_record_count:2,excluded_records:0,
    net_r:net,expectancy_r:net/2,average_payoff_ratio:2,profit_factor:2,
    average_win_r:2,average_loss_r:-1,max_drawdown_r:1,
    cumulative_r_points:[{{r:-1}},{{r:net}}],
    direction_breakdown:{{LONG:{{net_r:net}}}},
    exit_reason_breakdown:{{TP:{{net_r:2}},SL:{{net_r:-1}}}},
    average_mfe_r:2.5,mfe_capture_efficiency:.6,
    largest_win_r:2,largest_loss_r:-1
  }};
}}
function snapshot(){{
  return {{
    meta:nodes.rPerformanceMeta.textContent,
    metrics:[
      nodes.rNetValue.textContent,nodes.rExpectancyValue.textContent,
      nodes.rPayoffValue.textContent,nodes.rProfitFactorValue.textContent,
      nodes.rAverageValue.textContent,nodes.rDrawdownValue.textContent
    ],
    chart:nodes.rPerformanceChart.innerHTML,
    detail:nodes.rPerformanceDetailGrid.innerHTML
  }};
}}
if({json.dumps(scenario)}==='stale'){{
  renderRPerformance({{r_performance:{{ranges:{{'7':summary(2)}}}}}});
  renderRPerformance({{r_performance:{{status:'error',ranges:{{}}}}}});
}}else{{
  var invalid=summary(Infinity);
  invalid.valid_trade_count=Infinity;
  invalid.win_rate=NaN;
  invalid.max_consecutive_losses=Infinity;
  invalid.valid_exit_record_count=NaN;
  invalid.source_record_count=Infinity;
  invalid.excluded_records=Infinity;
  invalid.expectancy_r=NaN;
  invalid.average_payoff_ratio=Infinity;
  invalid.profit_factor=NaN;
  invalid.average_win_r=null;
  invalid.average_loss_r=Infinity;
  invalid.max_drawdown_r=null;
  invalid.cumulative_r_points=[{{r:null}},{{r:Infinity}},{{r:NaN}}];
  invalid.average_mfe_r=Infinity;
  invalid.mfe_capture_efficiency=Infinity;
  invalid.largest_win_r=NaN;
  invalid.largest_loss_r=Infinity;
  renderRPerformance({{r_performance:{{ranges:{{'7':invalid}}}}}});
}}
process.stdout.write(JSON.stringify(snapshot()));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


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


class RPerformanceUiTests(unittest.TestCase):
    def test_normal_and_demo_views_reuse_one_r_panel(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")

        self.assertIn("function rPerformancePanelHtml()", source)
        self.assertIn("function getRRangeSummary(d)", source)
        self.assertIn("function renderRPerformance(d)", source)
        self.assertIn("function resetRPerformance(message)", source)
        self.assertIn("function finiteRNumber(value)", source)
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

    def test_runtime_resets_stale_panel_when_summary_disappears(self):
        state = run_r_renderer_probe("stale")

        self.assertEqual(state["meta"], "无统计数据")
        self.assertEqual(state["metrics"], ["--"] * 6)
        self.assertIn("等待有效 R 交易记录", state["chart"])
        self.assertIn("暂无复盘数据", state["detail"])
        self.assertNotRegex(
            state["meta"] + "".join(state["metrics"]) + state["chart"] + state["detail"],
            r"NaN|Infinity",
        )

    def test_runtime_rejects_non_finite_and_null_values(self):
        state = run_r_renderer_probe("non_finite")
        rendered = state["meta"] + "".join(state["metrics"]) + state["chart"] + state["detail"]

        self.assertEqual(state["metrics"], ["--", "--", "--", "--", "-- / --", "--"])
        self.assertIn("R 曲线数据无效", state["chart"])
        self.assertNotIn("<svg", state["chart"])
        self.assertNotRegex(rendered, r"NaN|Infinity")
