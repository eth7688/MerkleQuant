# AXIOM Quant 15M 动能压缩破位扫描器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AXIOM 用户端新增独立的 15M 动能压缩破位观察池，通过已收盘 K 线筛选、池内实时价格监控、分级声音/微信警报和可恢复状态机发现真实新突破。

**Architecture:** 使用独立的纯规则模块、原子状态存储、市场扫描服务、实时价格监控器和压缩警报适配器。全市场结构只在新 15M K 线收盘后重算，观察池价格通过 Binance Futures WebSocket 实时更新并以 REST 轮询兜底；现有动能回流的策略状态不变，只复用已经验证的企业微信设置与文本发送函数。

**Tech Stack:** Python 3.12、Flask、pandas、NumPy、requests、websocket-client、原生 HTML/CSS/JavaScript、`unittest`、Binance Futures REST/WebSocket。

## Global Constraints

- 实现基线为 `codex/momentum-reflow-auto-dashboard`，计划编写时基线提交为 `821ec46`，完整回归为 `456 tests OK`。
- 数据源仅为 Binance U 本位永续合约，24 小时 USDT 成交额门槛固定为 `1_000_000`。
- 沿用 `screener.is_tradfi_or_junk()` 与有效永续交易对过滤，不把成交额门槛写入全局 `screener.MIN_VOLUME`。
- 形态、EMA8、EMA21、ATR14、枢轴、触碰、收缩和 1H/4H 确认只使用已收盘 K 线。
- 实时价格只更新已确定结构的状态，不重算形态指标或边界。
- 首次发现时已经越过方向边界或缓冲的形态不入池、不产生 FRESH、不提醒。
- 所有从池内真实产生的 FRESH 进入浏览器声音事件流；只有 `htf_alignment == "CONFIRMED"` 才排队发送企业微信。
- 同一 `compression_id` 最多产生一个 FRESH；页面刷新、重连、自动启用和服务重启均不补响、不补发。
- 质量分只排序，不能覆盖 G01-G07 硬规则。
- 新模块不得导入 `trader.py`，不得修改持仓、交易记录或发单。
- 前端保持原生 HTML/CSS/JS，不增加 npm 依赖；动画只允许 `transform` 和 `opacity`。
- Webhook 继续只保存在现有 `momentum_reflow_alert_settings.json`，公开接口不得返回密钥。
- 自动开关是服务器全局设置：GET 对已登录用户可见，修改只允许 `session["role"] == "admin"`。
- 生产部署不是本计划的隐含授权；完成本地实现后必须再次取得用户明确部署确认。

---

### Task 1: 实现无前视的压缩规则引擎

**Files:**
- Create: `momentum_compression.py`
- Create: `tests/test_momentum_compression.py`
- Read: `screener.py:165-231`（EMA 与已收盘 K 线处理模式）
- Read: `docs/superpowers/specs/2026-08-21-momentum-compression-scanner-integration-design.md`

**Interfaces:**
- Produces: `CompressionParams` frozen dataclass。
- Produces: `add_compression_indicators(frame: pd.DataFrame) -> pd.DataFrame`。
- Produces: `evaluate_side(symbol: str, side: str, closed_15m: pd.DataFrame, live_price: float, *, evaluated_at_ms: int, htf_alignment: str, params: CompressionParams = CompressionParams()) -> dict`。
- Produces: `evaluate_both_sides(symbol: str, closed_15m: pd.DataFrame, live_price: float, *, evaluated_at_ms: int, htf_alignment_by_side: dict[str, str], params: CompressionParams = CompressionParams()) -> list[dict]`，固定返回 LONG、SHORT 两个评价，失败时带 `rejection_reasons`。
- Produces: `compression_identity(evaluation: dict) -> str`。

- [ ] **Step 1: 为指标、无前视和参数写失败测试**

在 `tests/test_momentum_compression.py` 建立确定性 OHLCV fixture，并覆盖：

```python
class CompressionIndicatorTests(unittest.TestCase):
    def test_default_parameters_match_approved_spec(self):
        params = CompressionParams()
        self.assertEqual(params.pivot_span, 2)
        self.assertEqual(params.touch_tolerance_atr, 0.15)
        self.assertEqual(params.contraction_ratio_max, 0.65)
        self.assertEqual(params.max_ema_distance_atr, 1.0)
        self.assertEqual(params.pre_breakout_distance_atr, 0.35)
        self.assertEqual(params.breakout_buffer_atr, 0.05)
        self.assertEqual(params.min_bars, 15)
        self.assertEqual(params.max_bars, 100)

    def test_unclosed_bar_cannot_change_structure(self):
        closed = long_compression_frame()
        poisoned_live_bar = closed.iloc[-1].copy()
        poisoned_live_bar["ot"] += 900_000
        poisoned_live_bar["h"] *= 4
        with_live = pd.concat([closed, poisoned_live_bar.to_frame().T], ignore_index=True)
        first = evaluate_side("TESTUSDT", "LONG", closed, 101.0,
                              evaluated_at_ms=2_000_000_000_000,
                              htf_alignment="UNKNOWN")
        second = evaluate_side("TESTUSDT", "LONG", with_live.iloc[:-1], 101.0,
                               evaluated_at_ms=2_000_000_000_000,
                               htf_alignment="UNKNOWN")
        self.assertEqual(first["compression_id"], second["compression_id"])
        self.assertEqual(first["upper_boundary_price"], second["upper_boundary_price"])
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest tests.test_momentum_compression -v`
Expected: `ModuleNotFoundError: No module named 'momentum_compression'`。

- [ ] **Step 3: 实现参数、指标和输入校验**

`momentum_compression.py` 的公开参数必须从一个不可变对象读取：

```python
@dataclass(frozen=True)
class CompressionParams:
    version: str = "15m-compression-v1"
    pivot_span: int = 2
    touch_tolerance_atr: float = 0.15
    contraction_ratio_max: float = 0.65
    max_ema_distance_atr: float = 1.0
    pre_breakout_distance_atr: float = 0.35
    breakout_buffer_atr: float = 0.05
    min_directional_boundary_touches: int = 3
    min_bars: int = 15
    max_bars: int = 100

def add_compression_indicators(frame):
    out = frame.copy().reset_index(drop=True)
    previous_close = out["c"].shift(1)
    true_range = pd.concat((
        out["h"] - out["l"],
        (out["h"] - previous_close).abs(),
        (out["l"] - previous_close).abs(),
    ), axis=1).max(axis=1)
    out["ema8"] = out["c"].ewm(span=8, adjust=False).mean()
    out["ema21"] = out["c"].ewm(span=21, adjust=False).mean()
    out["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    return out
```

公开评价函数必须拒绝空数据、非有限价格、非 LONG/SHORT 和缺少 `ot/o/h/l/c/v` 列。

- [ ] **Step 4: 写枢轴包络、触碰 run 和最大连续后缀失败测试**

至少加入以下测试并写出明确断言：

- `test_touch_run_counts_once_until_non_touch_bar_separates_it`：连续三根触碰只计一个事件，中间插入一根非触碰 K 线后计为两个事件。
- `test_boundary_requires_two_pivot_highs_and_two_pivot_lows`：任一侧不足两个枢轴时返回 `REJECTED`，并包含 `INSUFFICIENT_PIVOTS`。
- `test_maximal_suffix_extends_while_non_length_rules_hold`：向左扩展直到首根违反非长度硬规则的 K 线，断言起止时间和 `compression_bars`。
- `test_14_bars_rejects_without_silent_padding`：断言 `REJECTED`、`WINDOW_TOO_SHORT` 和 `compression_bars == 14`。
- `test_101_bar_structure_rejects_without_truncating_to_100`：断言 `REJECTED`、`WINDOW_TOO_LONG` 和 `compression_bars == 101`。
- `test_long_requires_three_lower_wick_events`：只有两个独立下影触碰事件时包含 `INSUFFICIENT_DIRECTIONAL_TOUCHES`。
- `test_short_requires_three_upper_wick_events`：只有两个独立上影触碰事件时包含 `INSUFFICIENT_DIRECTIONAL_TOUCHES`。

其中 101 根测试必须断言：

```python
self.assertEqual(result["state"], "REJECTED")
self.assertIn("WINDOW_TOO_LONG", result["rejection_reasons"])
self.assertEqual(result["compression_bars"], 101)
```

- [ ] **Step 5: 实现确定性规则引擎**

实现以下带确定返回类型的私有函数并由 `evaluate_side` 组合：

- `_pivots(frame: pd.DataFrame, span: int) -> tuple[list[int], list[int]]`
- `_fit_shifted_envelope(frame: pd.DataFrame, pivot_highs: list[int], pivot_lows: list[int]) -> dict`
- `_touch_events(values: pd.Series, boundary: pd.Series, atr: pd.Series, tolerance: float) -> list[int]`
- `_swing_structure(frame: pd.DataFrame, pivot_highs: list[int], pivot_lows: list[int], side: str) -> dict`
- `_non_length_rules(frame: pd.DataFrame, side: str, params: CompressionParams) -> dict`
- `_maximal_structural_suffix(indicators: pd.DataFrame, side: str, params: CompressionParams) -> tuple[pd.DataFrame, dict]`
- `_quality_score(metrics: dict, params: CompressionParams) -> tuple[float, dict]`
- `_classify_without_episode(evaluation: dict, live_price: float, params: CompressionParams) -> str`

最小二乘边界使用 `numpy.polyfit(indexes, pivot_values, 1)`；上界平移量为 `max(high - fitted_upper)`，下界平移量为 `min(low - fitted_lower)`。连续触碰布尔值的每个最大 run 只计一次，代表点取绝对距离最小且并列时最早的 K 线。

`evaluate_side` 返回设计规格第 10 节全部审计字段，并额外返回 `compression_id`、`directional_touch_times`、`score_components`。状态初评只允许 `REJECTED`、`PRE_BREAKOUT`、`COMPRESSION_ACTIVE_*`、`BREAKOUT_UNCONFIRMED_*` 或 `OUTSIDE_AT_DISCOVERY`；`OUTSIDE_AT_DISCOVERY` 明确不得转换成 FRESH。

- [ ] **Step 6: 覆盖所有硬规则、评分和边界等号**

加入并跑通：EMA 交叉、收盘进入 EMA 带、EMA8 距离、HH/HL、LL/LH、收缩率、`live_price == U/L` 仍属区内、`live_price == U+B/L-B` 仍为未确认、评分不能挽救硬规则失败等测试。

Run: `python -m unittest tests.test_momentum_compression -v`
Expected: 所有 Task 1 测试通过。

- [ ] **Step 7: 提交 Task 1**

```powershell
git add momentum_compression.py tests/test_momentum_compression.py
git commit -m "feat: add deterministic compression rule engine"
```

---

### Task 2: 实现观察池、身份状态机和原子存储

**Files:**
- Create: `momentum_compression_store.py`
- Create: `tests/test_momentum_compression_store.py`
- Modify: `momentum_compression.py`（只在测试暴露接口缺口时修改）

**Interfaces:**
- Consumes: Task 1 评价字典与 `compression_id`。
- Produces: `load_compression_state(path: Path) -> dict`。
- Produces: `save_compression_state(path: Path, state: dict) -> None`。
- Produces: `reconcile_structure_scan(state: dict, evaluations: list[dict], now_ms: int) -> tuple[dict, dict]`。
- Produces: `apply_live_prices(state: dict, prices: dict[str, float], now_ms: int) -> tuple[dict, list[dict]]`。
- Produces: `write_compression_snapshot(directory: Path, evaluation: dict, frame: pd.DataFrame) -> str`。

- [ ] **Step 1: 写初始化不补发和状态转移失败测试**

```python
def test_first_scan_outside_does_not_enter_pool_or_create_event(self):
    state, report = reconcile_structure_scan(
        default_state(), [evaluation(state="OUTSIDE_AT_DISCOVERY")], 1_000,
    )
    self.assertEqual(state["pool"], {})
    self.assertEqual(report["fresh_events"], [])

def test_pool_crossing_creates_exactly_one_fresh_event(self):
    state, _ = reconcile_structure_scan(
        default_state(), [evaluation(state="PRE_BREAKOUT")], 1_000,
    )
    state, first = apply_live_prices(state, {"TESTUSDT": 110.1}, 2_000)
    state, second = apply_live_prices(state, {"TESTUSDT": 111.0}, 3_000)
    self.assertEqual([e["state"] for e in first], ["BREAKOUT_FRESH_LONG"])
    self.assertEqual(second, [])
```

再覆盖 UNCONFIRMED→FRESH→ACTIVE→RETRACING→FAILED、adverse exit→返回池内、结构失效退出、相同身份边界更新不重复、不同身份允许新事件。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest tests.test_momentum_compression_store -v`
Expected: 模块导入失败。

- [ ] **Step 3: 实现版本化状态与严格验证**

```python
STATE_VERSION = 1

def default_state():
    return {
        "version": STATE_VERSION,
        "auto_enabled": False,
        "next_event_id": 1,
        "pool": {},
        "episodes": {},
        "delivery_queue": [],
        "last_structure_scan_at": 0,
        "last_closed_15m_close_time": 0,
        "last_error": "",
    }
```

加载时必须验证版本、布尔类型、非负整数和字典/列表结构；损坏文件抛出 `ValueError`，不得静默重置有效状态。写入复用临时文件、`flush()`、`os.fsync()`、`os.replace()`。

- [ ] **Step 4: 实现扫描协调和实时状态机**

`reconcile_structure_scan` 只把 `PRE_BREAKOUT`、`COMPRESSION_ACTIVE_LONG`、`COMPRESSION_ACTIVE_SHORT` 加入池。`OUTSIDE_AT_DISCOVERY`、`BREAKOUT_UNCONFIRMED_*` 和 `REJECTED` 不入新池；已有身份若结构评价失败则移除，数据获取失败必须由调用方省略而不是伪造 REJECTED。

`apply_live_prices` 的比较必须严格使用：

```python
long_fresh = price > item["upper_boundary_price"] + item["breakout_buffer_price"]
short_fresh = price < item["lower_boundary_price"] - item["breakout_buffer_price"]
```

生成事件时分配单调 `event_id`，保存结构快照、`compression_id`、状态事件时间、实时价格和高周期状态；同一身份的 `fresh_emitted` 一旦为真不得复位。

- [ ] **Step 5: 实现可重放快照**

每个入池身份写一次 `momentum_compression_snapshots/<compression_id>.json`，内容为参数版本、symbol、side、窗口元数据和 `ot/o/h/l/c/v` 数组。函数返回相对于应用根目录的 `ohlcv_snapshot_ref`，重复写同一身份必须字节稳定。

- [ ] **Step 6: 验证原子性、幂等性和重启恢复**

测试 `os.replace` 前异常不改变正式文件、连续保存加载一致、重启后的 `fresh_emitted` 保留、同一批价格重放不产生第二事件。

Run: `python -m unittest tests.test_momentum_compression_store -v`
Expected: 全部通过。

- [ ] **Step 7: 提交 Task 2**

```powershell
git add momentum_compression.py momentum_compression_store.py tests/test_momentum_compression_store.py
git commit -m "feat: add persistent compression watch pool"
```

---

### Task 3: 实现 Binance 全市场结构扫描与高周期确认

**Files:**
- Create: `momentum_compression_service.py`
- Create: `tests/test_momentum_compression_service.py`
- Read: `momentum_reflow.py:377-564`（币种池、并发与失败隔离模式）
- Read: `screener.py:182-231`（已收盘 K 线接口）

**Interfaces:**
- Consumes: Task 1 规则引擎和 Task 2 存储。
- Produces: `fetch_compression_universe(*, get=requests.get) -> tuple[list[str], dict[str, float]]`。
- Produces: `evaluate_htf_alignment(symbol: str, side: str, *, fetch=fetch_klines) -> dict`。
- Produces: `scan_compression_market(state_path: Path, snapshot_dir: Path, *, progress=None, max_workers: int = 12) -> dict`。

- [ ] **Step 1: 写 100 万成交额和永续过滤失败测试**

```python
def test_universe_uses_one_million_quote_volume_floor(self):
    symbols, volumes = fetch_compression_universe(get=fake_binance_get({
        "KEEPUSDT": 1_000_000,
        "DROPUSDT": 999_999.99,
    }))
    self.assertIn("KEEPUSDT", symbols)
    self.assertNotIn("DROPUSDT", symbols)
```

同时断言仅 `PERPETUAL + TRADING + quoteAsset=USDT`，并调用现有 `is_tradfi_or_junk` 排除规则。

- [ ] **Step 2: 写高周期与失败隔离测试**

覆盖 LONG 双周期同向为 CONFIRMED、任一冲突为 CONFLICT、任一数据不足为 UNKNOWN；单币 15M 请求失败只增加错误，币种池请求失败抛错且旧状态文件字节不变。

- [ ] **Step 3: 运行测试并确认 RED**

Run: `python -m unittest tests.test_momentum_compression_service -v`
Expected: 模块导入失败。

- [ ] **Step 4: 实现币种池与高周期确认**

```python
MIN_COMPRESSION_QUOTE_VOLUME = 1_000_000.0
FUTURES_BASE = "https://fapi.binance.com"

def fetch_compression_universe(*, get=requests.get):
    exchange = get(f"{FUTURES_BASE}/fapi/v1/exchangeInfo", timeout=10)
    tickers = get(f"{FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=10)
    exchange.raise_for_status(); tickers.raise_for_status()
    volumes = {row["symbol"]: float(row["quoteVolume"]) for row in tickers.json()}
    symbols = sorted(row["symbol"] for row in exchange.json()["symbols"]
                     if row.get("status") == "TRADING"
                     and row.get("contractType") == "PERPETUAL"
                     and row.get("quoteAsset") == "USDT"
                     and volumes.get(row["symbol"], 0.0) >= MIN_COMPRESSION_QUOTE_VOLUME
                     and not is_tradfi_or_junk(row["symbol"]))
    return symbols, volumes
```

高周期确认必须分别拉最后已收盘 1H、4H，复用 Task 1 的 EMA 与摆动判定，不允许用实时 K 线。

- [ ] **Step 5: 实现并发结构扫描**

每个币先拉 220 根已收盘 15M K 线并评价 LONG/SHORT；只有硬规则通过且尚未越界者才额外拉高周期。并发数限制为 `min(12, max(1, max_workers))`，每完成一个 future 调用 `progress(completed, total)`。

成功结果统一交给 `reconcile_structure_scan` 后一次原子保存。返回：

```python
{
    "rows": eligible_rows, "events": fresh_events, "scanned": len(symbols),
    "eligible": eligible_count, "pool_size": len(state["pool"]),
    "errors": error_count, "rejection_counts": rejection_counts,
    "evaluated_at": now_ms, "last_closed_15m_close_time": close_ms,
}
```

- [ ] **Step 6: 验证结果稳定与旧池保留**

固定 fixture 下重复扫描必须保持 `compression_id`；整轮币种池失败时不得调用 `save_compression_state`；单币失败时旧池内该币保留并标记 `data_status="unavailable"`。

Run: `python -m unittest tests.test_momentum_compression_service -v`
Expected: 全部通过。

- [ ] **Step 7: 提交 Task 3**

```powershell
git add momentum_compression_service.py tests/test_momentum_compression_service.py
git commit -m "feat: scan binance compression candidates"
```

---

### Task 4: 实现实时价格监控、REST 兜底和 15M 调度器

**Files:**
- Modify: `requirements.txt`
- Create: `momentum_compression_monitor.py`
- Create: `tests/test_momentum_compression_monitor.py`
- Modify: `momentum_compression_service.py`

**Interfaces:**
- Consumes: Task 2 `apply_live_prices` 和 Task 3 `scan_compression_market`。
- Produces: `next_closed_15m_scan_at(now: datetime) -> datetime`。
- Produces: `CompressionMonitor(state_path: Path, snapshot_dir: Path, event_callback: Callable[[list[dict]], None])`。
- Produces methods: `start() -> bool`, `stop(timeout: float = 2.0) -> bool`, `status() -> dict`, `scan_now(trigger: str) -> bool`, `set_auto_enabled(enabled: bool) -> dict`。

- [ ] **Step 1: 增加 WebSocket 依赖并写调度边界失败测试**

在 `requirements.txt` 增加：

```text
websocket-client>=1.8,<2
```

测试北京时间或 UTC 输入都归一到 aware UTC；`12:14:59Z` 下一次为 `12:15:05Z`，`12:15:05Z` 下一次为 `12:30:05Z`。5 秒用于等待交易所确认收盘。

- [ ] **Step 2: 写池过滤、断线恢复和 REST 兜底失败测试**

使用注入的 fake WebSocket 消息：

```python
message = json.dumps([
    {"s": "POOLUSDT", "c": "10.5"},
    {"s": "OTHERUSDT", "c": "99"},
])
monitor.handle_message(message, now_ms=2_000)
self.assertEqual(store.applied_prices, {"POOLUSDT": 10.5})
```

再断言连接失败按 1/2/5/10/30 秒上限退避，断线期间每 5 秒调用一次 `/fapi/v1/ticker/price` 并只提交池内价格，恢复后停止 REST 兜底。

- [ ] **Step 3: 运行测试并确认 RED**

Run: `python -m unittest tests.test_momentum_compression_monitor -v`
Expected: 模块导入失败或依赖接口不存在。

- [ ] **Step 4: 实现 `!miniTicker@arr` 实时流适配**

使用 `wss://fstream.binance.com/ws/!miniTicker@arr`，通过 `websocket.WebSocketApp` 接收所有永续 mini ticker，但在进入状态机前使用当前池 symbol 集合过滤。解析只接受有限正数，未知 symbol 和畸形行丢弃并计数，不终止线程。

价格批次通过一个锁内流程完成：加载状态→`apply_live_prices`→保存状态→锁外调用 `event_callback(events)`，避免网络发送占用状态锁。

- [ ] **Step 5: 实现自动结构扫描与生命周期**

`CompressionMonitor` 使用独立结构扫描锁。`scan_now("manual")` 与自动到期调用同一函数；已有线程存活时返回 `False`。自动关闭时停止结构调度和价格连接但保留池；重新启用只启动监控并建立事件游标基线，不清空池。

状态字典至少公开 `running`、`auto_enabled`、`structure_scanning`、`price_stream_status`、`last_scan_at`、`next_scan_at`、`last_error`、`pool_size`、`today_fresh`。

- [ ] **Step 6: 运行 Task 4 和 Task 2/3 回归**

```powershell
python -m unittest tests.test_momentum_compression_monitor tests.test_momentum_compression_store tests.test_momentum_compression_service -v
python -m py_compile momentum_compression.py momentum_compression_store.py momentum_compression_service.py momentum_compression_monitor.py
```

Expected: 全部通过，编译退出 0。

- [ ] **Step 7: 提交 Task 4**

```powershell
git add requirements.txt momentum_compression_monitor.py momentum_compression_service.py tests/test_momentum_compression_monitor.py
git commit -m "feat: monitor compression pool in real time"
```

---

### Task 5: 实现独立声音事件流和分级企业微信投递

**Files:**
- Create: `momentum_compression_alerts.py`
- Create: `tests/test_momentum_compression_alerts.py`
- Read: `momentum_reflow_alerts.py:259-506`

**Interfaces:**
- Consumes: Task 4 产生的 FRESH 事件。
- Reuses: `momentum_reflow_alerts.load_alert_settings`、`send_wechat_markdown`、`RETRY_DELAYS_MS`、`MAX_ATTEMPTS`。
- Produces: `append_compression_alerts(events_path: Path, state_path: Path, events: list[dict], now_ms: int) -> list[dict]`。
- Produces: `read_public_compression_alerts(events_path: Path, after_id: int) -> dict`。
- Produces: `baseline_compression_alerts(events_path: Path) -> int`。
- Produces: `deliver_due_compression_wechat(settings_path: Path, state_path: Path, events_path: Path, now_ms: int, *, post=requests.post) -> dict`。

- [ ] **Step 1: 写所有 FRESH 有声音、只有双确认有微信的失败测试**

```python
def test_all_fresh_events_are_public_but_only_confirmed_is_queued(self):
    created = append_compression_alerts(events_path, state_path, [
        fresh("AUSDT", "CONFIRMED"),
        fresh("BUSDT", "CONFLICT"),
        fresh("CUSDT", "UNKNOWN"),
    ], 10_000)
    self.assertEqual(len(created), 3)
    self.assertEqual(public_symbols(events_path), ["AUSDT", "BUSDT", "CUSDT"])
    self.assertEqual(queued_symbols(state_path), ["AUSDT"])
```

再覆盖同一 `compression_id` 幂等、第一次读取建立游标不补响、跨进程锁竞争只发一次、Webhook 错误不泄漏 key、失败退避与最大五次。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest tests.test_momentum_compression_alerts -v`
Expected: 模块导入失败。

- [ ] **Step 3: 实现只追加事件与独立投递队列**

`momentum_compression_events.jsonl` 每行保存不可变公开事件；`momentum_compression_state.json` 的 `delivery_queue` 保存可变投递状态。追加 JSONL 时持有压缩事件锁，写完整 UTF-8 行、flush、fsync；读取时严格校验每一非空行。

公开返回最多最近 20 条：

```python
{"latest_alert_id": latest, "events": events_after_cursor[-20:]}
```

微信格式函数标题固定为 `【AXIOM 动能压缩破位警报】`，正文包含设计批准的字段，并使用 `Asia/Shanghai` 显示时间。

- [ ] **Step 4: 复用现有安全传输而不修改旧账本**

投递函数读取现有 `momentum_reflow_alert_settings.json`，调用 `send_wechat_markdown()`；不得调用或修改 `observe_reflow_alerts()`、原 `momentum_reflow_alerts.json` 或旧游标。

事件锁文件与投递锁文件使用压缩专属名称，避免两个策略互相阻塞状态写入；HTTP 发送仍在跨进程投递锁保护下。

- [ ] **Step 5: 运行新旧警报回归**

```powershell
python -m unittest tests.test_momentum_compression_alerts tests.test_momentum_reflow_alerts -v
python -m py_compile momentum_compression_alerts.py momentum_reflow_alerts.py
```

Expected: 两组全部通过。

- [ ] **Step 6: 提交 Task 5**

```powershell
git add momentum_compression_alerts.py tests/test_momentum_compression_alerts.py
git commit -m "feat: add compression breakout alerts"
```

---

### Task 6: 接入 Flask 后端、权限与服务启动

**Files:**
- Modify: `web_ui.py:6-39,440-476,3787-3791,3839-4228,4649-4654`
- Create: `tests/test_momentum_compression_integration.py`
- Modify: `tests/test_momentum_reflow_integration.py`（只增加兼容断言）

**Interfaces:**
- Consumes: Task 3-5 的扫描、监控和警报函数。
- Produces routes: `GET /api/compression/status`、`POST /api/compression/automation`、`GET /api/compression/alerts`、`GET /scan/compression/15m`。
- Produces: `_process_compression_events(events, now_ms) -> list[dict]`。

- [ ] **Step 1: 写路由权限和互斥失败测试**

测试要求：未登录读取 alerts 返回 401；普通用户修改自动开关返回 403；管理员可修改；只接受 JSON boolean；`/scan/compression/1h` 返回 400；连续两次手动请求只启动一个扫描线程。

```python
response = client.post("/api/compression/automation", json={"enabled": True})
self.assertEqual(response.status_code, 403)
```

- [ ] **Step 2: 写事件处理和服务恢复失败测试**

断言 `_process_compression_events` 创建声音事件后唤醒压缩微信 worker；模块启动只创建一个 monitor 和一个投递 worker；测试 stop helper 能在 2 秒内结束；重启加载 `auto_enabled` 但不会调用历史事件回放。

- [ ] **Step 3: 运行测试并确认 RED**

Run: `python -m unittest tests.test_momentum_compression_integration -v`
Expected: 新路由或导入不存在。

- [ ] **Step 4: 增加独立路径与控制器**

在 `_BASE_DIR` 下声明：

```python
MOMENTUM_COMPRESSION_STATE = Path(_BASE_DIR) / "momentum_compression_state.json"
MOMENTUM_COMPRESSION_EVENTS = Path(_BASE_DIR) / "momentum_compression_events.jsonl"
MOMENTUM_COMPRESSION_SNAPSHOTS = Path(_BASE_DIR) / "momentum_compression_snapshots"
```

创建一个 `CompressionMonitor` 单例。事件 callback 先调用 `append_compression_alerts`，有 CONFIRMED 待投递时设置独立 wakeup event。

- [ ] **Step 5: 实现四个接口**

`GET /api/compression/status` 返回监控状态、观察池 rows、episode rows、拒绝聚合与 `can_manage = session.get("role") == "admin"`。`POST /api/compression/automation` 必须登录且为 admin。`GET /api/compression/alerts` 复用 reflow 的非负 after 校验。`GET /scan/compression/15m` 调用 `scan_now("manual")`，错误周期明确 400。

- [ ] **Step 6: 启动与关闭测试钩子**

在 `__main__` 中原有两项启动后增加：

```python
_start_compression_monitor()
_start_compression_alert_worker()
```

提供 `_stop_compression_monitor_for_tests()` 和 `_stop_compression_alert_worker_for_tests()`，不注册破坏测试隔离的 import-time 非 daemon 线程。

- [ ] **Step 7: 跑后端与旧回流回归**

```powershell
python -m unittest tests.test_momentum_compression_integration tests.test_momentum_reflow_integration tests.test_momentum_reflow_scheduler -v
python -m py_compile web_ui.py momentum_compression_monitor.py momentum_compression_alerts.py
```

Expected: 全部通过。

- [ ] **Step 8: 提交 Task 6**

```powershell
git add web_ui.py tests/test_momentum_compression_integration.py tests/test_momentum_reflow_integration.py
git commit -m "feat: expose compression monitor APIs"
```

---

### Task 7: 新增左侧菜单、压缩看板和独立声音游标

**Files:**
- Modify: `web_ui.py:1412-1460,1515-1900,3780-3784`
- Modify: `tests/test_momentum_compression_integration.py`

**Interfaces:**
- Consumes: Task 6 API payload。
- Produces JS: `renderMomentumCompression(payload)`、`refreshCompressionStatus()`、`setCompressionAutoEnabled(enabled)`、`pollCompressionAlerts()`。

- [ ] **Step 1: 写菜单、转义、三分区和声音基线失败测试**

仿照现有 Node 执行测试抽取目标 JS，断言：

```python
self.assertIn("['compression','动能压缩破位',[['compression_15m','15M 压缩池'", source)
self.assertIn("function renderMomentumCompression", source)
self.assertIn("axiom_compression_alert_cursor_v1", source)
self.assertNotIn("<script>alert(1)</script>", rendered)
```

声音测试必须证明初次启用只保存 `latest_alert_id`，下一条新事件才调用零钱声；多条同批事件只播放一次但游标推进到最新 ID。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest tests.test_momentum_compression_integration -v`
Expected: 菜单、renderer 或声音函数断言失败。

- [ ] **Step 3: 添加菜单与描述**

在动能回流后加入：

```javascript
['compression','动能压缩破位',[['compression_15m','15M 压缩池','P']]],
```

描述明确“只监控不交易、15M 已收盘结构、池内实时突破”。`render()` 将 `compression_15m` 路由到独立 renderer；`doScan()` 对该 tab 调用 `/scan/compression/15m`，轮询独立 status，不借用回流 cache。

- [ ] **Step 4: 实现钛金 HUD 看板**

使用现有 CSS 变量和 1px 边框，渲染：顶部自动开关/状态、统计条、LONG/SHORT 观察池、新突破与当前状态、拒绝统计。表格字段严格匹配设计，不渲染全量 REJECTED 行。所有字符串经过 `escapeRHtml`，所有数字先通过 `finiteRNumber`。

自动开关在 `can_manage=false` 时禁用并显示“仅管理员可修改”；手动扫描仍对已登录用户可用。

- [ ] **Step 5: 实现独立声音游标**

使用：

```javascript
var COMPRESSION_ALERT_SOUND_KEY='axiom_compression_alert_sound_v1';
var COMPRESSION_ALERT_CURSOR_KEY='axiom_compression_alert_cursor_v1';
```

复用零钱声波形的实现代码，但状态、generation、pending promise、timer 和 endpoint 全部使用 compression 命名，不能共用 reflow cursor。每 2 秒请求 `/api/compression/alerts?after=<cursor>`；初次启用、页面加载和 generation 变化都先基线，不补响。

- [ ] **Step 6: 运行前端集成与旧页面回归**

```powershell
python -m unittest tests.test_momentum_compression_integration tests.test_momentum_reflow_integration -v
python -m py_compile web_ui.py
```

Expected: 全部通过，编译退出 0。

- [ ] **Step 7: 提交 Task 7**

```powershell
git add web_ui.py tests/test_momentum_compression_integration.py
git commit -m "feat: add compression scanner dashboard"
```

---

### Task 8: 完整验证、复审与部署就绪交付

**Files:**
- Modify only if review exposes a defect: `momentum_compression*.py`, `web_ui.py`, `requirements.txt`, relevant tests
- Update after local completion: `.superpowers/sdd/progress.md`
- Update only after an actual server deployment: `PROGRESS.md`

**Interfaces:**
- Consumes: Tasks 1-7 complete feature。
- Produces: clean branch, full verification evidence, explicit deployment manifest and rollback checklist。

- [ ] **Step 1: 运行完整本地验证门槛**

```powershell
$out=Join-Path $env:TEMP 'compression-full-tests.txt'
python -m unittest discover -s tests -q *> $out
$code=$LASTEXITCODE
Get-Content $out -Tail 15
Remove-Item -LiteralPath $out
if($code -ne 0){exit $code}
python -m py_compile momentum_compression.py momentum_compression_store.py momentum_compression_service.py momentum_compression_monitor.py momentum_compression_alerts.py web_ui.py momentum_reflow_alerts.py
git diff --check
git status --short
```

Expected: 测试数大于基线 456 且 `OK`，所有编译退出 0，diff check 无错误。

- [ ] **Step 2: 运行确定性重放和警报幂等专项**

同一 fixture 连续运行结构扫描两次，断言 `compression_id`、边界和快照引用一致；重放相同价格序列两次，断言只有一个 FRESH、一个声音事件和最多一个微信投递。

Run: `python -m unittest tests.test_momentum_compression tests.test_momentum_compression_store tests.test_momentum_compression_service tests.test_momentum_compression_monitor tests.test_momentum_compression_alerts tests.test_momentum_compression_integration -v`
Expected: 全部通过。

- [ ] **Step 3: 执行两阶段代码复审**

第一阶段逐条对照规格检查规则、无前视、100 万成交额、首次越界不提醒和 HTF 分级；第二阶段检查代码质量、线程生命周期、原子写入、锁顺序、Webhook 脱敏、前端转义和旧功能回归。发现问题时先新增失败测试，再修复并重跑完整门槛。

- [ ] **Step 4: 提交复审修复并保持工作区干净**

```powershell
git add momentum_compression*.py web_ui.py requirements.txt tests
git commit -m "fix: close compression scanner review findings"
git status --short
```

没有复审修复时跳过 commit；最终 status 必须为空。

- [ ] **Step 5: 准备但不执行生产部署**

部署清单只包含实际新增/修改源码与 `requirements.txt`。必须在用户再次明确确认部署后，才允许：服务器同批备份、在 venv 安装锁定范围的 `websocket-client`、上传 `.codex-new`、服务器编译、原子替换、重启 `macd-bot`、验证 HTTP/API/WebSocket/声音游标/微信测试和 journal。

受保护文件至少包括 `demo_bot_config.json`、`positions_<uid>.json`、`trades_<uid>.jsonl`、`signal_events_0.jsonl`、`axiom_accounts.db`、现有回流设置与警报账本；除新压缩状态文件外，部署前后哈希不得因本功能变化。

- [ ] **Step 6: 部署后记账规则**

只有实际部署成功后，使用 `apply_patch` 更新本地 `PROGRESS.md`，记录：功能提交、测试数量、服务器备份路径、安装依赖、部署文件哈希、服务状态、接口结果、真实监控/警报验证、受保护状态哈希和回滚状态。`PROGRESS.md` 不上传服务器。

---

## Execution Notes

- 每个任务完成后先做规格审查，再做质量审查；审查通过才进入下一任务。
- 实现过程中不得为了复用而改写现有动能回流策略语义。
- 若 `websocket-client` 在服务器 venv 安装或代理环境中不可用，停止部署并报告；不得静默降级为“伪实时”后宣称符合规格。
- 真实微信发送测试会产生外部消息，只能在用户已授权部署/验证且使用现有保存 Webhook 时执行，禁止在日志或回复中输出完整地址。
