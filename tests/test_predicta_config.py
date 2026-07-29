import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trader import SqueezeBreakoutBot, TradeConfig


ROOT = Path(__file__).resolve().parents[1]


class PredictaConfigTest(unittest.TestCase):
    def test_predicta_defaults_match_approved_strategy(self):
        cfg = TradeConfig()

        self.assertEqual(cfg.predicta_confirm_bars, 6)
        self.assertEqual(cfg.predicta_choppy_filter_mode, "hard")
        self.assertEqual(cfg.predicta_ewo_fast, 5)
        self.assertEqual(cfg.predicta_ewo_slow, 35)
        self.assertEqual(cfg.predicta_confirm_atr_buffer, 0.08)
        self.assertEqual(cfg.predicta_stop_atr_mult, 0.5)
        self.assertEqual(cfg.predicta_min_stop_pct, 0.003)
        self.assertEqual(cfg.predicta_max_stop_pct, 0.08)
        self.assertEqual(cfg.predicta_max_symbols, 500)
        self.assertEqual(cfg.predicta_scan_workers, 4)

    def test_signal_source_normalizes_predicta_aliases(self):
        bot = object.__new__(SqueezeBreakoutBot)
        for value in ("predicta_ewo", "predicta-ewo", "predicta"):
            bot.cfg = TradeConfig(entry_signal_source=value)
            self.assertEqual(bot._entry_signal_source(), "predicta_ewo")

    def test_admin_and_user_config_offer_predicta_source(self):
        for name in ("admin_server.py", "web_ui.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('option value="predicta_ewo"', source)

    def test_admin_offers_predicta_choppy_intercept_toggle(self):
        source = (ROOT / "admin_server.py").read_text(encoding="utf-8")
        self.assertIn('id="dePredictaChoppy"', source)
        self.assertIn(
            "predicta_choppy_filter_mode: document.getElementById('dePredictaChoppy').value==='1'?'hard':'off'",
            source,
        )
        self.assertIn("(cfg.predicta_choppy_filter_mode||'off')==='hard'", source)

    def test_half_risk_defaults_off_and_both_config_screens_expose_one_parameter(self):
        self.assertEqual(TradeConfig().half_risk_trigger_r, 0.0)

        admin_source = (ROOT / "admin_server.py").read_text(encoding="utf-8")
        self.assertIn('id="deHalfRiskR"', admin_source)
        self.assertIn(
            "half_risk_trigger_r: parseFloat(document.getElementById('deHalfRiskR').value)||0",
            admin_source,
        )
        self.assertIn("(cfg.half_risk_trigger_r??0)", admin_source)

        user_source = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        self.assertIn('id="cfg_half_risk_r"', user_source)
        self.assertIn(
            "half_risk_trigger_r: parseFloat(document.getElementById('cfg_half_risk_r').value)||0",
            user_source,
        )
        self.assertIn("(cfg.half_risk_trigger_r??0)", user_source)

    def test_half_risk_config_round_trips_and_updates_runtime_bot(self):
        with patch.object(SqueezeBreakoutBot, "start", return_value=None), patch(
            "account_manager.admin_get_all_users", return_value=[]
        ):
            web_ui = importlib.import_module("web_ui")

        runtime = SimpleNamespace(
            cfg=TradeConfig(),
            client=None,
            init_calls=0,
            restore_calls=0,
            sync_calls=0,
        )
        lifecycle = []

        def refresh_client():
            runtime.init_calls += 1
            runtime.client = object()
            lifecycle.append("init")

        def restore_stop_ids():
            runtime.restore_calls += 1
            lifecycle.append("restore")

        def sync_positions():
            runtime.sync_calls += 1
            lifecycle.append("sync")

        runtime._init_client = refresh_client
        runtime._restore_stop_ids = restore_stop_ids
        runtime._sync_positions = sync_positions

        with tempfile.TemporaryDirectory() as directory:
            manager = web_ui.UserBotManager()
            manager._demo_bot = runtime
            manager._configs[0] = runtime.cfg
            with patch.object(web_ui, "_BASE_DIR", directory), patch.object(
                web_ui, "bot_manager", manager
            ):
                client = web_ui.app.test_client()
                for value in (0, 0.01):
                    response = client.post(
                        "/api/admin/demo/config",
                        json={"half_risk_trigger_r": value},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.get_json()["ok"])
                    self.assertEqual(runtime.cfg.half_risk_trigger_r, value)
                    persisted = json.loads(
                        (Path(directory) / "demo_bot_config.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(persisted["half_risk_trigger_r"], value)
                    manager._configs.clear()
                    self.assertEqual(
                        client.get("/api/admin/demo/config").get_json()["half_risk_trigger_r"],
                        value,
                    )

                legacy_path = Path(directory) / "demo_bot_config.json"
                legacy_path.write_text(
                    json.dumps({"enable_time_stop": False}),
                    encoding="utf-8",
                )
                manager._configs.clear()
                legacy = client.get("/api/admin/demo/config").get_json()

        self.assertEqual(runtime.init_calls, 2)
        self.assertEqual(runtime.restore_calls, 2)
        self.assertEqual(runtime.sync_calls, 2)
        self.assertEqual(
            lifecycle,
            ["init", "restore", "sync", "init", "restore", "sync"],
        )
        self.assertEqual(legacy["half_risk_trigger_r"], 0.0)


if __name__ == "__main__":
    unittest.main()
