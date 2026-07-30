import importlib
import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch


def wait_until_idle(web_ui, timeout=2.0):
    deadline = time.monotonic() + timeout
    while web_ui.state["scanning"] and time.monotonic() < deadline:
        time.sleep(0.01)
    return not web_ui.state["scanning"]


def render_reflow_payload(payload):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    helpers = source[
        source.index("function escapeRHtml(value)"):
        source.index("function fmtRValue(value,signed)")
    ]
    renderer = source[
        source.index("function renderMomentumReflow("):
        source.index("// ===== SCANNING =====")
    ]
    script = f"""
var nodes={{stats:{{innerHTML:''}},main:{{innerHTML:''}}}};
global.document={{getElementById:function(id){{return nodes[id];}}}};
{helpers}
{renderer}
renderMomentumReflow({json.dumps(payload)});
process.stdout.write(JSON.stringify(nodes));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


class MomentumReflowUiTests(unittest.TestCase):
    def test_sidebar_description_and_renderer_are_wired(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")

        self.assertIn("['reflow',", source)
        self.assertIn("['reflow_1h',", source)
        self.assertIn("reflow_1h:", source)
        self.assertIn("function renderMomentumReflow(", source)
        self.assertIn('mode == "reflow"', source)

    def test_route_starts_reflow_scan_and_caches_payload(self):
        web_ui = importlib.import_module("web_ui")
        payload = {"rows": [], "scanned": 2, "errors": 0, "initialized": 2}
        web_ui.state.update(scanning=False, progress="", text="")
        web_ui.cache["reflow_1h"] = []

        with patch.object(web_ui, "scan_momentum_reflow", return_value=payload):
            response = web_ui.app.test_client().get("/scan/reflow/1h")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["scanning"])
            self.assertTrue(wait_until_idle(web_ui))

        self.assertEqual(web_ui.cache["reflow_1h"], payload)

    def test_route_rejects_unsupported_reflow_intervals(self):
        web_ui = importlib.import_module("web_ui")
        web_ui.state.update(scanning=False, progress="", text="")

        response = web_ui.app.test_client().get("/scan/reflow/4h")

        self.assertEqual(response.status_code, 400)

    def test_renderer_escapes_payload_and_formats_reflow_values(self):
        result = render_reflow_payload({
            "rows": [
                {
                    "symbol": '<img src=x onerror="globalThis.pwned=1">USDT',
                    "direction": "LONG",
                    "price": 12.3456,
                    "ema50": 12.1,
                    "close_distance_atr": 0.18,
                    "window_index": 3,
                    "max_expansion_atr": 1.25,
                    "daily_kind": '<svg onload="globalThis.pwned=2">',
                    "breakout_volume_ratio": 2.4,
                },
                {
                    "symbol": "SHORTUSDT",
                    "direction": "SHORT",
                    "price": float("nan"),
                    "ema50": float("inf"),
                    "close_distance_atr": None,
                    "window_index": float("inf"),
                    "max_expansion_atr": float("nan"),
                    "daily_kind": "strong_momentum",
                    "breakout_volume_ratio": float("inf"),
                },
            ],
            "scanned": 20,
            "errors": 1,
            "initialized": 3,
        })
        rendered = result["stats"]["innerHTML"] + result["main"]["innerHTML"]

        self.assertIn("LONG", rendered)
        self.assertIn("SHORT", rendered)
        self.assertIn("3/5", rendered)
        self.assertIn("0.18 ATR", rendered)
        self.assertIn("\u5f3a\u52a8\u80fd\u65e5K", rendered)
        self.assertIn("2.4x", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("&lt;svg", rendered)
        self.assertNotRegex(rendered, r"NaN|Infinity")
        self.assertIn("copySymbol(", rendered)

    def test_renderer_shows_dedicated_empty_state(self):
        result = render_reflow_payload({"rows": [], "scanned": 0, "errors": 0, "initialized": 0})

        self.assertIn("暂无", result["main"]["innerHTML"])
        self.assertIn("首次回流", result["main"]["innerHTML"])


if __name__ == "__main__":
    unittest.main()
