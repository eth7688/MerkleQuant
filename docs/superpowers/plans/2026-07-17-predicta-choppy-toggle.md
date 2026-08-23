# Predicta Choppy Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-state “震荡过滤拦截” control to the demo-engine admin form so Predicta signals can switch safely between `hard` interception and `off` without restarting the engine.

**Architecture:** Reuse the existing `/api/admin/demo/config` read/write and BotManager hot-update path. The admin page maps the UI values `1/0` to the existing backend values `hard/off`; the trading engine and all unrelated strategy, risk, position, stop-loss, and exit logic remain unchanged.

**Tech Stack:** Python 3.12, Flask, native HTML/CSS/JavaScript, `unittest`/`pytest`, SQLite/JSON configuration already used by AXIOM Quant.

## Global Constraints

- The toggle is shown beside the demo engine signal-source setting and is labeled `震荡过滤拦截`.
- Enabled saves `predicta_choppy_filter_mode="hard"`; disabled saves `predicta_choppy_filter_mode="off"`.
- Only `hard` renders as enabled. Historical `log_only` and every other value render as disabled and are not rewritten until the user saves.
- Saving must reuse `/api/admin/demo/config`, preserve API credentials and all unrelated settings, and hot-update the running demo bot.
- The change affects only new Predicta signals; it must not close or alter existing positions.
- Do not expose `log_only`, modify RJ filters, introduce frontend dependencies, or change the Predicta signal algorithm.
- Update local `PROGRESS.md` after server deployment with change, backup, deployment, and verification evidence.

---

### Task 1: Lock the Toggle Contract with Tests

**Files:**
- Modify: `tests/test_predicta_config.py`
- Modify: `tests/test_predicta_pipeline.py`

**Interfaces:**
- Consumes: `admin_server.py` as UTF-8 source text and `SqueezeBreakoutBot._predicta_candidates_from_df(symbol, interval, frame)`.
- Produces: regression coverage for the DOM id `dePredictaChoppy`, UI-to-config mapping, `hard` blocking, and `off` pass-through.

- [ ] **Step 1: Add the failing admin contract test**

```python
def test_admin_offers_predicta_choppy_intercept_toggle(self):
    source = (ROOT / "admin_server.py").read_text(encoding="utf-8")
    self.assertIn('id="dePredictaChoppy"', source)
    self.assertIn(
        "predicta_choppy_filter_mode: document.getElementById('dePredictaChoppy').value==='1'?'hard':'off'",
        source,
    )
    self.assertIn("(cfg.predicta_choppy_filter_mode||'off')==='hard'", source)
```

- [ ] **Step 2: Run the admin contract test and confirm RED**

Run: `python -m pytest tests/test_predicta_config.py::PredictaConfigTest::test_admin_offers_predicta_choppy_intercept_toggle -q`

Expected: FAIL because `admin_server.py` does not yet contain `dePredictaChoppy`.

- [ ] **Step 3: Add backend `off` characterization coverage**

```python
def test_off_choppy_filter_allows_same_signal_key(self):
    frame = _frame()
    with patch("trader.compute_predicta", return_value=_lines(frame, 1.0)), patch.object(
        self.bot, "_predicta_choppy_filter_state", return_value={
            "choppy_filter_mode": "off", "choppy_filter_is_choppy": True,
        },
    ):
        fast, waiting = self.bot._predicta_candidates_from_df("BTCUSDT", "30m", frame)

    self.assertEqual(len(fast), 1)
    self.assertEqual(waiting, [])
```

- [ ] **Step 4: Run the backend filter pair**

Run: `python -m pytest tests/test_predicta_pipeline.py::PredictaPipelineTest::test_hard_choppy_filter_blocks_signal_key tests/test_predicta_pipeline.py::PredictaPipelineTest::test_off_choppy_filter_allows_same_signal_key -q`

Expected: `2 passed`, proving the existing engine already distinguishes `hard` from `off`.

---

### Task 2: Add the Admin Toggle and Save Mapping

**Files:**
- Modify: `admin_server.py`
- Test: `tests/test_predicta_config.py`

**Interfaces:**
- Consumes: `cfg.predicta_choppy_filter_mode` returned by `/api/admin/demo/config`.
- Produces: `<select id="dePredictaChoppy">` and POST field `predicta_choppy_filter_mode: "hard" | "off"`.

- [ ] **Step 1: Add the save mapping beside the signal-source field**

```javascript
entry_signal_source: document.getElementById('deSignalSource').value,
predicta_choppy_filter_mode: document.getElementById('dePredictaChoppy').value==='1'?'hard':'off',
scan_interval: document.getElementById('deInt').value,
```

- [ ] **Step 2: Render the two-state control beside signal source**

```javascript
h+='<div class="field"><label>震荡过滤拦截</label><select id="dePredictaChoppy"><option value="1"'+((cfg.predicta_choppy_filter_mode||'off')==='hard'?' selected':'')+'>开启｜震荡信号不进入开仓链路</option><option value="0"'+((cfg.predicta_choppy_filter_mode||'off')!=='hard'?' selected':'')+'>关闭｜不使用震荡过滤拦截</option></select></div>';
```

- [ ] **Step 3: Make the save confirmation reflect hot update**

```javascript
m.textContent='已保存并生效';
```

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/test_predicta_config.py tests/test_predicta_pipeline.py -q`

Expected: all Predicta configuration and pipeline tests pass.

- [ ] **Step 5: Commit the feature**

```powershell
git add admin_server.py tests/test_predicta_config.py tests/test_predicta_pipeline.py
git commit -m "feat: add Predicta choppy intercept toggle"
```

---

### Task 3: Validate the Complete Local Build

**Files:**
- Verify: `admin_server.py`
- Verify: `trader.py`
- Verify: `tests/`

**Interfaces:**
- Consumes: the complete working tree including the earlier stale-recovery fix.
- Produces: compile and regression evidence before deployment.

- [ ] **Step 1: Compile affected Python modules**

Run: `python -m py_compile admin_server.py web_ui.py trader.py predicta_indicator.py`

Expected: exit code 0 with no output.

- [ ] **Step 2: Run the complete test suite**

Run: `python -m pytest -q`

Expected: all tests pass with zero failures.

- [ ] **Step 3: Inspect the surgical diff**

Run: `git diff --check; git status --short`

Expected: no whitespace errors; only intentional strategy recovery, toggle, test, plan, and progress files are present.

---

### Task 4: Back Up, Deploy, and Verify the Admin Control

**Files:**
- Deploy: `admin_server.py` to `<deploy-dir>/admin_server.py`
- Preserve: `<deploy-dir>/demo_bot_config.json`
- Modify locally after verification: `PROGRESS.md`

**Interfaces:**
- Consumes: SSH access to `root@<production-host>` and the existing `macd-admin`/`macd-bot` systemd services.
- Produces: deployed admin UI, active services, preserved credentials, and a running demo engine still configured with `hard` unless explicitly changed by the user.

- [ ] **Step 1: Create a timestamped server backup**

```powershell
ssh -i .\<ssh-key> root@<production-host> "ts=\$(date +%Y%m%d_%H%M%S); d=<deploy-dir>/backups/predicta_choppy_toggle_\$ts; mkdir -p \$d; cp <deploy-dir>/admin_server.py <deploy-dir>/demo_bot_config.json \$d/; echo \$d"
```

Expected: prints one backup directory containing both files.

- [ ] **Step 2: Upload only the admin server change**

```powershell
scp -i .\<ssh-key> .\admin_server.py root@<production-host>:<deploy-dir>/admin_server.py
```

Expected: exit code 0.

- [ ] **Step 3: Compile and restart only the admin service**

```powershell
ssh -i .\<ssh-key> root@<production-host> "cd <deploy-dir> && python3 -m py_compile admin_server.py && systemctl restart macd-admin && systemctl is-active macd-admin macd-bot"
```

Expected: compile succeeds and both services report `active`.

- [ ] **Step 4: Verify deployed source and current config without printing secrets**

```powershell
ssh -i .\<ssh-key> root@<production-host> "cd <deploy-dir> && grep -q 'dePredictaChoppy' admin_server.py && python3 - <<'PY'
import json
cfg = json.load(open('demo_bot_config.json', encoding='utf-8'))
print({'predicta_choppy_filter_mode': cfg.get('predicta_choppy_filter_mode'), 'api_key_present': bool(cfg.get('testnet_api_key')), 'api_secret_present': bool(cfg.get('testnet_api_secret'))})
PY"
```

Expected: deployed control exists, mode is `hard`, and both credential-presence flags are `True`; secret values are never printed.

- [ ] **Step 5: Record deployment evidence in `PROGRESS.md`**

Add dated entries covering the stale-recovery fix, demo-engine reset, choppy-toggle implementation, server backup paths, deployed files, service status, tests, zero open demo positions, reset PnL baseline, and credential-presence verification.

- [ ] **Step 6: Commit deployment documentation and any remaining stale-recovery work**

```powershell
git add trader.py tests/test_predicta_pipeline.py PROGRESS.md docs/superpowers/plans/2026-07-17-predicta-choppy-toggle.md
git commit -m "fix: reject stale Predicta recovery entries"
```

Expected: commit succeeds without staging unrelated user changes.
