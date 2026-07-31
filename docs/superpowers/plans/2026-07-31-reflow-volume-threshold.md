# 动能回流50万成交额门槛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将Bitget动能回流扫描池的24小时USDT成交额门槛从200万降至50万，在不改变信号和交易链路的前提下扩大扫描覆盖。

**Architecture:** 保持现有`fetch_futures_universe()`数据流和品种分类，仅修改单一成交额常量。使用边界回归测试固定“等于50万包含、低于50万排除”，然后完成全量测试、服务器备份、单文件部署和真实扫描验收。

**Tech Stack:** Python 3.12、`unittest`、Flask、Bitget V2公共行情API、systemd、SQLite状态文件。

## Global Constraints

- 固定门槛必须为`500_000 USDT`，不新增管理员配置项，不改为动态Top-N。
- 继续排除股票、稳定币、杠杆代币和未知RWA；继续保留允许的加密资产、商品和外汇。
- 不修改Predicta、EWO、RJ、震荡过滤、动能回流信号定义、自动交易、仓位、止损或止盈。
- 保留3线程并发、分页退避重试、短历史回退和指标历史不足跳过。
- 本地文件先更新；服务器部署后更新本地`PROGRESS.md`。
- 部署前必须备份，且`demo_bot_config.json`、`positions_<uid>.json`、`trades_<uid>.jsonl`、`axiom_accounts.db`部署前后哈希必须一致。

---

### Task 1: 修改成交额边界并完成回归验证

**Files:**
- Modify: `tests/test_momentum_reflow.py:578-592`
- Modify: `momentum_reflow.py:37`

**Interfaces:**
- Consumes: `fetch_futures_universe() -> tuple[list[str], dict[str, float], dict[str, str]]`
- Produces: `REFLOW_MIN_VOLUME_USDT = 500_000`；成交额等于门槛的品种进入扫描池，低于门槛的品种不进入。

- [ ] **Step 1: 先修改边界测试，制造失败**

将`test_reflow_volume_boundary_is_inclusive`改为同时提供两个普通加密合约：

```python
    @patch("momentum_reflow.requests.get")
    def test_reflow_volume_boundary_is_inclusive(self, get):
        contracts = Mock()
        contracts.raise_for_status.return_value = None
        contracts.json.return_value = {"data": [
            {
                "symbol": "PASSUSDT", "baseCoin": "PASS", "quoteCoin": "USDT",
                "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO",
            },
            {
                "symbol": "FAILUSDT", "baseCoin": "FAIL", "quoteCoin": "USDT",
                "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO",
            },
        ]}
        tickers = Mock()
        tickers.raise_for_status.return_value = None
        tickers.json.return_value = {"data": [
            {"symbol": "PASSUSDT", "quoteVolume": "500000"},
            {"symbol": "FAILUSDT", "quoteVolume": "499999"},
        ]}
        get.side_effect = [contracts, tickers]

        symbols, _, _ = fetch_futures_universe()

        self.assertEqual(symbols, ["PASSUSDT"])
```

- [ ] **Step 2: 运行专项测试并确认它因旧200万门槛失败**

Run:

```powershell
python -m unittest discover -s tests -p test_momentum_reflow.py -q
```

Expected: FAIL；`PASSUSDT`未进入结果，实际为`[]`。

- [ ] **Step 3: 实施最小修改**

在`momentum_reflow.py`中只修改常量：

```python
REFLOW_MIN_VOLUME_USDT = 500_000
```

- [ ] **Step 4: 运行专项测试并确认通过**

Run:

```powershell
python -m unittest discover -s tests -p test_momentum_reflow.py -q
```

Expected: 该文件全部测试通过，且边界测试证明50万包含、499999排除。

- [ ] **Step 5: 运行完整本地验证**

Run:

```powershell
python -m unittest discover -s tests -q
python -m py_compile momentum_reflow.py screener.py web_ui.py
git diff --check
```

Expected: 全部测试通过；编译与diff检查退出码均为0。

- [ ] **Step 6: 提交功能改动**

```powershell
git add momentum_reflow.py tests/test_momentum_reflow.py
git commit -m "feat: expand Bitget reflow scan universe"
```

Expected: 创建一个只包含常量和边界测试的功能提交。

---

### Task 2: 备份、部署并完成真实扫描验收

**Files:**
- Deploy: `momentum_reflow.py` -> `<deploy-dir>/momentum_reflow.py`
- Modify after deployment: `PROGRESS.md`
- Protect unchanged: `<deploy-dir>/demo_bot_config.json`
- Protect unchanged: `<deploy-dir>/positions_<uid>.json`
- Protect unchanged: `<deploy-dir>/trades_<uid>.jsonl`
- Protect unchanged: `<deploy-dir>/axiom_accounts.db`

**Interfaces:**
- Consumes: Task 1已通过测试的`momentum_reflow.py`。
- Produces: 服务器运行50万门槛版本；`/data`中的`reflow_1h.scanned`明显高于原70个基准且`errors=0`。

- [ ] **Step 1: 记录服务器部署前状态并创建备份**

在服务器执行：

```bash
set -e
stamp=$(date +%Y%m%d_%H%M%S)
backup=<deploy-dir>/backups/reflow_volume_500k_$stamp
mkdir -p "$backup"
cd <deploy-dir>
cp -a momentum_reflow.py demo_bot_config.json positions_<uid>.json trades_<uid>.jsonl axiom_accounts.db "$backup/"
sha256sum momentum_reflow.py demo_bot_config.json positions_<uid>.json trades_<uid>.jsonl axiom_accounts.db
echo "$backup"
```

Expected: 输出备份绝对路径及五个文件的部署前SHA256。

- [ ] **Step 2: 上传暂存文件并在替换前编译**

从本地上传：

```powershell
scp -i <repo-root>\<ssh-key> momentum_reflow.py root@<production-host>:<deploy-dir>/momentum_reflow.py.codex-new
```

在服务器执行：

```bash
cd <deploy-dir>
venv/bin/python3 -m py_compile momentum_reflow.py.codex-new
mv momentum_reflow.py.codex-new momentum_reflow.py
venv/bin/python3 -m py_compile momentum_reflow.py screener.py web_ui.py
systemctl restart macd-bot
systemctl is-active macd-bot macd-admin
```

Expected: 编译成功；两项服务均输出`active`。只重启`macd-bot`。

- [ ] **Step 3: 主动触发完整动能回流扫描**

在服务器执行：

```bash
curl -fsS http://127.0.0.1:5000/scan/reflow/1h
```

随后轮询`http://127.0.0.1:5000/data`，直到顶层`scanning=false`。

Expected: `data.reflow_1h.scanned`明显高于原70个基准，`data.reflow_1h.errors=0`，`data.reflow_1h.automation.last_auto_error=""`。候选数允许为0，因为是否出现信号由市场状态决定。

- [ ] **Step 4: 验证运行文件、状态文件和日志**

在服务器执行：

```bash
cd <deploy-dir>
sha256sum momentum_reflow.py demo_bot_config.json positions_<uid>.json trades_<uid>.jsonl axiom_accounts.db
systemctl is-active macd-bot macd-admin
journalctl -u macd-bot --since "10 minutes ago" --no-pager | grep -E "Traceback|ERROR|CRITICAL|Exception|ImportError|SyntaxError" || true
```

Expected: `momentum_reflow.py`与本地SHA256一致；四个受保护状态文件与部署前一致；两项服务为`active`；journal无新增匹配错误。

- [ ] **Step 5: 更新本地部署记录**

使用`apply_patch`向`PROGRESS.md`追加`2026-07-31 - 动能回流50万成交额门槛`，准确记录：

```markdown
- 修改：动能回流24小时USDT成交额门槛由2,000,000降为500,000；其余分类、信号和交易逻辑不变。
- 测试：专项与完整测试数量、py_compile和git diff --check结果。
- 部署：服务器备份路径、仅部署momentum_reflow.py、只重启macd-bot。
- 验收：最终扫描数量、errors、last_auto_error、服务状态和运行文件SHA256。
- 数据保护：四个受保护状态文件部署前后SHA256一致。
```

- [ ] **Step 6: 提交部署记录并做最终检查**

```powershell
git add PROGRESS.md
git commit -m "docs: record 500k reflow universe deployment"
git status --short
git log -3 --oneline
```

Expected: 部署记录提交成功，工作树为空，最近提交包含功能改动与部署记录。
