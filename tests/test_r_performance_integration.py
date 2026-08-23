import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from trader import SqueezeBreakoutBot


def run_r_renderer_probe(scenario):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    direction_attack = json.dumps(
        """<img src=x onerror=globalThis.pwned=1> & " '"""
    )
    reason_attack = json.dumps(
        """TP <img onerror=globalThis.pwned=2> & " '"""
    )
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
    classes:[
      nodes.rNetValue.className,nodes.rExpectancyValue.className,
      nodes.rPayoffValue.className,nodes.rProfitFactorValue.className,
      nodes.rAverageValue.className,nodes.rDrawdownValue.className
    ],
    chart:nodes.rPerformanceChart.innerHTML,
    detail:nodes.rPerformanceDetailGrid.innerHTML
  }};
}}
var scenario={json.dumps(scenario)};
if(scenario==='stale'){{
  renderRPerformance({{r_performance:{{ranges:{{'7':summary(2)}}}}}});
  renderRPerformance({{r_performance:{{status:'error',ranges:{{}}}}}});
}}else if(scenario==='non_finite'){{
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
}}else if(scenario==='malicious_labels'){{
  var malicious=summary(1);
  malicious.direction_breakdown={{}};
  malicious.direction_breakdown[{direction_attack}]={{trades:1,net_r:1}};
  malicious.exit_reason_breakdown={{}};
  malicious.exit_reason_breakdown[{reason_attack}]={{trades:1,net_r:1}};
  renderRPerformance({{r_performance:{{ranges:{{'7':malicious}}}}}});
}}else if(scenario==='only_loss'){{
  var loss=summary(-2);
  loss.win_rate=0;
  loss.expectancy_r=-1;
  loss.average_win_r=null;
  loss.average_loss_r=-1;
  loss.average_payoff_ratio=null;
  loss.profit_factor=0;
  loss.max_drawdown_r=2;
  loss.cumulative_r_points=[{{r:-1}},{{r:-2}}];
  loss.exit_reason_breakdown={{SL:{{trades:2,net_r:-2}}}};
  renderRPerformance({{r_performance:{{ranges:{{'7':loss}}}}}});
}}else if(scenario==='excluded'){{
  var excluded=summary(1);
  excluded.valid_exit_record_count=2;
  excluded.source_record_count=5;
  excluded.excluded_records=3;
  renderRPerformance({{r_performance:{{ranges:{{'7':excluded}}}}}});
}}else if(scenario==='range_reason'){{
  _equityRangeDays=180;
  var ranged=summary(2);
  ranged.exit_reason_breakdown={{TP:{{trades:2,net_r:2}}}};
  renderRPerformance({{r_performance:{{ranges:{{'180':ranged}}}}}});
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


def run_r_details_state_probe():
    source = Path("web_ui.py").read_text(encoding="utf-8")
    start = source.index("var _rPerformanceDetailsOpen")
    end = source.index("function initTraderPanel()")
    script = f"""
{source[start:end]}
var initial=rPerformancePanelHtml();
rememberRPerformanceDetailsState({{open:true}});
var opened=rPerformancePanelHtml();
rememberRPerformanceDetailsState({{open:false}});
var closed=rPerformancePanelHtml();
process.stdout.write(JSON.stringify({{
  initialOpen:/<details[^>]*\\sopen(?:\\s|>)/.test(initial),
  openedOpen:/<details[^>]*\\sopen(?:\\s|>)/.test(opened),
  closedOpen:/<details[^>]*\\sopen(?:\\s|>)/.test(closed),
  hasToggleHandler:opened.indexOf('ontoggle="rememberRPerformanceDetailsState(this)"')>=0
}}));
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
        with (
            patch("trader.time.monotonic", side_effect=[100.0, 101.0]),
            patch("trader.summarize_r_performance_ranges", return_value=expected) as calculate,
        ):
            first = bot._r_performance_summary()
            second = bot._r_performance_summary()

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        calculate.assert_called_once()

    def test_engine_recalculates_r_summary_after_cache_expiry(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.trade_log = []
        bot._fast_r_performance_cache_ts = 0.0
        bot._fast_r_performance_cache = {}
        bot._log_ready = False

        first_payload = {"status": "ok", "ranges": {"all": {"net_r": 1}}}
        second_payload = {"status": "ok", "ranges": {"all": {"net_r": 2}}}
        with (
            patch("trader.time.monotonic", side_effect=[100.0, 101.99, 102.0]),
            patch(
                "trader.summarize_r_performance_ranges",
                side_effect=[first_payload, second_payload],
            ) as calculate,
        ):
            first = bot._r_performance_summary()
            cached = bot._r_performance_summary()
            refreshed = bot._r_performance_summary()

        self.assertEqual(first, first_payload)
        self.assertEqual(cached, first_payload)
        self.assertEqual(refreshed, second_payload)
        self.assertEqual(calculate.call_count, 2)

    def test_engine_returns_and_caches_error_payload_on_calculator_exception(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.trade_log = []
        bot._fast_r_performance_cache_ts = 0.0
        bot._fast_r_performance_cache = {}
        bot._log_ready = False

        with (
            patch("trader.time.monotonic", side_effect=[100.0, 101.0]),
            patch(
                "trader.summarize_r_performance_ranges",
                side_effect=RuntimeError("calculator failed"),
            ) as calculate,
        ):
            first = bot._r_performance_summary()
            second = bot._r_performance_summary()

        self.assertEqual(first, {"status": "error", "ranges": {}})
        self.assertEqual(second, first)
        calculate.assert_called_once()

    def test_both_status_methods_publish_the_same_named_payload(self):
        source = Path("trader.py").read_text(encoding="utf-8")

        self.assertEqual(source.count('"r_performance": r_performance'), 2)
        self.assertGreaterEqual(source.count("r_performance = self._r_performance_summary()"), 2)


class RPerformanceUiTests(unittest.TestCase):
    def test_details_state_survives_dashboard_html_rebuild(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")

        self.assertIn("var _rPerformanceDetailsOpen", source)
        state = run_r_details_state_probe()
        self.assertFalse(state["initialOpen"])
        self.assertTrue(state["openedOpen"])
        self.assertFalse(state["closedOpen"])
        self.assertTrue(state["hasToggleHandler"])

    def test_normal_and_demo_views_reuse_one_r_panel(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")

        self.assertIn("function rPerformancePanelHtml()", source)
        self.assertIn("function getRRangeSummary(d)", source)
        self.assertIn("function renderRPerformance(d)", source)
        self.assertIn("function resetRPerformance(message)", source)
        self.assertIn("function finiteRNumber(value)", source)
        self.assertIn("function escapeRHtml(value)", source)
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

        self.assertEqual(state["meta"], "1W · 无统计数据")
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

    def test_runtime_escapes_all_payload_derived_labels(self):
        state = run_r_renderer_probe("malicious_labels")

        self.assertNotIn("<img", state["detail"])
        self.assertIn("&lt;img", state["detail"])
        self.assertIn("&amp;", state["detail"])
        self.assertIn("&quot;", state["detail"])
        self.assertIn("&#39;", state["detail"])

    def test_runtime_renders_only_loss_state_without_inventing_win_metrics(self):
        state = run_r_renderer_probe("only_loss")

        self.assertEqual(
            state["metrics"],
            ["−2.00R", "−1.00R", "--", "0.00", "-- / −1.00R", "−2.00R"],
        )
        self.assertEqual(state["classes"][0], "r-value r")
        self.assertEqual(state["classes"][4], "r-value neu")

    def test_runtime_reports_excluded_records(self):
        state = run_r_renderer_probe("excluded")

        self.assertIn("有效记录 2 / 5", state["meta"])
        self.assertIn("未计入 3 条", state["meta"])

    def test_runtime_reports_current_range_and_exit_reason_trade_count(self):
        state = run_r_renderer_probe("range_reason")

        self.assertTrue(state["meta"].startswith("6M · "))
        self.assertIn("TP 2 笔 · +2.00R", state["detail"])
