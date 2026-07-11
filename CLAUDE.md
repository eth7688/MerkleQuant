# CLAUDE.md - AXIOM Quant 系统介绍与开发红线

## 项目标识

**AXIOM Quant** 是面向加密货币永续合约的多用户量化执行终端，当前定位为机构级 SaaS 自动交易系统。

当前版本: **商业版 v1.9.28 / 2026-07-04 BJT**

核心策略: **六线均线密集区突破 + 回踩/反弹防线确认 + 顶底分型入场 + 三阶动态风控**。
双引擎运行: **原版结构策略 (eth7688 Bitget 实盘 1h:$10) + RJ-only 30m 演示引擎 (测试网/25x)**。
新增模块: **CryptoRank 中文雷达**（加密市场数据工作台，侧边栏集成）。

系统服务:

| 服务 | 文件 | 端口 | 职责 |
|---|---|---:|---|
| 用户交易面板 | `web_ui.py` | 5000 | 用户登录、配置、自动交易、持仓与日志 + CryptoRank 静态服务 |
| 管理后台 | `admin_server.py` | 5001 | 用户管理、许可、演示引擎、后台监控 |
| CryptoRank API | `crypot-rank-bot-main/api/` | 3001 | 中文加密数据雷达 (Express + Python 回退) |
| 交易引擎 | `trader.py` | 内嵌线程 | 多用户实盘/演示自动交易 |
| 扫描器 | `screener.py` | 模块 | 多周期选币、完整入场链路复核、盯防池 |
| RJ 指标 | `rj_indicator.py` | 模块 | RJ=(3K-2D)+RSI9 原线动能计算、历史胜率统计 |

## 技术栈

- 后端: Python 3.12 + Flask
- 前端: 原生 HTML/CSS/JS, 不引入 npm 或外部前端框架
- 数据库: SQLite (`axiom_accounts.db`)
- 交易所: Binance 主网/测试网 API + Bitget V2 API
- Bitget 模式: USDT 永续, hedge mode, 支持带单 API
- 配置:
  - 演示引擎: `demo_bot_config.json`
  - 用户实盘: SQLite `user_configs`
  - 默认配置: `trade_config.json`
- 日志:
  - 每用户独立 `bot_{uid}.log`
  - 演示引擎 `bot_demo.log`
  - logger 必须 `propagate=False`
- 数据工具:
  - 信号事件: `signal_events_*.jsonl`
  - 权益快照: `equity_*.jsonl`
  - 交易记录: `trades_*.jsonl`
  - 回放引擎: `replay_engine.py`
  - 交易分析: `analyze_trades.py`
  - RJ 优选池: `rj_watchlist_*.json`

## 核心文件索引

| 文件 | 职责 |
|---|---|
| `trader.py` | `SqueezeBreakoutBot`, `Position`, Binance/Bitget 客户端, 开仓、出场、止损单、持仓同步 |
| `screener.py` | 多周期扫描、六线密集区检测、回踩/反弹判定、顶底分型、盯防池复核 |
| `rj_indicator.py` | RJ 动能计算 (绿J=3K-2D + 紫RSI9)、RJ 历史胜率统计、RJ 优选池评分 |
| `web_ui.py` | 用户端 Web UI, 用户 bot 管理, 配置保存, 自动恢复实盘引擎 |
| `admin_server.py` | 管理后台, 用户/许可/演示引擎控制 |
| `account_manager.py` | 用户账户、许可、燃料、配置持久化 |
| `replay_engine.py` | 本地 CSV 滚动离线回放, 复用实盘扫描/复核函数 |
| `analyze_trades.py` | 按周期/方向/退出原因/BTC环境拆账统计 |
| `memory/exit-chain.md` | 出场链路、止损去重、三阶风控、时间止损最新规则 |
| `memory/entry-chain.md` | 13步完整开仓链路 |
| `PROGRESS.md` | 动态开发进度和历史修复记录 |

## 当前核心策略

### 原版结构策略 — 完整开仓链路

AXIOM Quant 的开仓基础逻辑不可改变:

```text
找到 6 条均线密集区
-> 收盘价突破密集区
-> 等待回踩或反弹
-> 回踩/反弹不能破均线密集区防线
-> 先形成反向分型（顶→底或底→顶完整序列）
-> 最新顶/底分型确认回踩/反弹结束
-> 突破后顺序硬校验（禁止借用突破前旧分型）
-> 结构指纹去重（同一突破/回归/确认分型只允许成交一次）
-> RJ 动能过滤（可选 hard/soft/log_only/off）
-> 开仓进场
```

所有扫描、盯防池、候选池、最终下单前复核，都必须围绕这条核心链路。

### RJ-only 30m 演示引擎 (v1.9.27)

演示引擎新增独立信号源 `entry_signal_source=rj_only`，不走旧结构链路:

```text
RJ 金叉/死叉出现 → 记录关键K（高/低点）
→ 后续收盘价突破关键K高点(LONG) / 跌破关键K低点(SHORT)
→ 支撑压力 + J线背离过滤
→ 历史胜率评分（样本数/胜率/平均R/利润因子）
→ RJ 优选池持久监控
→ 关键K候选池 near_close 确认
→ enter_rj_position() 模拟开仓
```

- RJ 计算: 绿色 `J = 3K - 2D` (快线) + 紫色 `RSI(9)` 原线 (慢线)
- 默认周期 `30m`, `paper` 模式
- 开仓复用: 仓位计算、提前保护、未起爆超时、1.2R防守、追踪止盈、交易日志
- RJ 优选池 `rj_watchlist_*.json`: 默认盯 80 个优选币 + 30 个高成交额发现位
- 候选池每 10s 轻量检查，距收盘 120s 内且价格突破确认价才触发
- 复盘事件: `rj_setup_add / rj_setup_near_trigger / rj_setup_trigger_touch / rj_setup_trigger / rj_setup_invalidated / rj_setup_timeout`

### RJ 自动交易入场过滤 (v1.9.24)

在原版结构策略最终开仓前叠加 RJ 动能闸门:

```text
rj_entry_filter=hard (默认)
→ LONG: 最近 N 根内 RJ 金叉 && 当前 J > R
→ SHORT: 最近 N 根内 RJ 死叉 && 当前 J < R
```

- 支持 `hard` / `soft` (仅要求同向) / `log_only` (只记录) / `off`
- 默认交叉有效窗口 `8` 根K, J/R 最小差值 `1.0`
- RJ 拦截写入 `signal_events`: `entry_reject reason=rj_filter_failed`
- 复盘日志 `rj_filter_check` 记录每次 RJ 闸门评估完整快照

### 多周期并发

默认扫描:

```text
scan_interval="15m,1h,4h,1d"
```

规则:

- 每个信号必须烙印 `source_interval`。
- 跨周期同币去重，保留最高分信号。
- 开仓、止损、未起爆K线计数、EMA/ATR追踪，都优先使用持仓自己的 `source_interval`。
- 交易所同步恢复持仓时必须继承本地 `source_interval`，禁止无条件回落到默认 `15m`。

### 入场悬空拦截

v1.9.0 起，系统不只检查分型止损是否贴边，还检查最终开仓价是否已经远离均线密集区。

入场悬空阈值:

```text
15m = 1.6%
1h  = 2.5%
4h  = 4.5%
1d  = 6.5%
1w  = 6.5%
```

分型悬空阈值:

```text
15m = 2.5%
1h  = 4.0%
4h  = 6.5%
1d  = 9.5%
1w  = 9.5%
```

含义:

- LONG 当前收盘价不能高出均线上轨太多。
- SHORT 当前收盘价不能低于均线下轨太多。
- 防止瀑布后深坑追空、拉升后高位追多。
- 池子诊断日志会输出 `入场悬空` 或 `悬空拦截`。

### 突破后确认顺序硬校验 (v1.9.5)

- `verify_pool_signal()` 强制顺序: 同方向有效突破 → 回归确认 → 突破后最新顶/底结构确认。
- 禁止借用突破前旧分型。
- 当前K线刚刚穿越带边但尚未完成回归确认时，不允许直接拿旧信号开仓。
- 盯防池诊断新增 `无回归确认`、`无突破后分型`。

### 顶底/底顶结构序列确认 (v1.9.9)

- LONG: 收盘有效突破后，必须先形成顶分型 → 再由最新底分型确认回踩结束。
- SHORT: 收盘有效跌破后，必须先形成底分型 → 再由最新顶分型确认反弹结束。
- 禁止孤立分型直接开仓。
- 盯防池诊断新增 `结构序列不完整` 拒绝原因。

## 15m 短线狙击逻辑

15m 周期属于短线狙击和起爆验证单。

核心含义:

```text
给 15m 信号约 6 根K线, 也就是约90分钟
如果没有跑出趋势, 当前推进R仍不足阈值
就先平掉, 释放仓位
```

这不是普通网格持仓逻辑，而是起爆点验证:

- 真起爆通常应尽快顺势推进。
- 假突破和弱延续不允许长时间占用仓位。
- 资金优先留给最有希望进入保本/追踪的品种。

默认未起爆K数:

```text
time_stop_bars="15m:6,1h:5,4h:4,1d:3"
time_stop_min_r=0.6
```

## 出场与风控

### 未起爆超时退出 v1.9.4

当前最终规则:

```text
达到 time_stop_bars 后
如果未保本、未减仓
且当前推进R < time_stop_min_r
自动平仓
```

重要变化:

- 不再只看历史最大推进 `max_favorable_r`。
- 现在看 **当前推进R**。
- 曾经到过 0.6R 但后来回落到 0.6R 以下的仓位，也会退出。
- `max_favorable_r` 仍落盘，用于日志诊断。

日志示例:

```text
未起爆超时退出(15m 12/6K, 当前推进0.20R<0.60R, 最大推进0.67R)
```

接口保护:

- 未起爆检查尽量前置，减少 Bitget 429 限频影响。
- 恢复持仓时记录 `current_price`，本地优先判断可使用当前价计算当前R。
- K线接口不可用时，仍可按 `entry_time + source_interval` 做本地计时兜底。

### 提前保本保护 (v1.9.26)

在三阶动态风控之前新增一层提前保护:

```text
enable_early_protect=true
early_protect_r=0.8
early_protect_lock_r=0.0
```

- 仓位当前推进达到 `early_protect_r` 后，先把 `current_sl` 推到入场价附近。
- 默认只做保本 (`early_protect_lock_r=0`)，不立即启动 EMA/ATR 三阶追踪。
- 三阶追踪仍等 `tier1_defense_r` 或二阶减仓后启动，避免 0.8R 被 EMA 棘轮过早扫出。
- 1.2R 防守可升级：如果 0.8R 已保本，后续到 1.2R 仍会升级到入场价 ±0.2R。
- 每次保护升级写入 `position_protect` 事件。

### 三阶动态风控

执行框架:

```text
二阶 2.0R 减仓/锁利
→ 未起爆超时检查
→ SL 检查
→ 提前保本保护 (0.8R, 可选)
→ 一阶 1.2R 防守
→ 三阶 EMA/ATR 追踪
```

核心:

- 一阶: 达到 `tier1_defense_r` 后进入保本/锁利防守。
- 二阶: 达到 `tier2_partial_r` 后减仓 50%, 剩余仓位进入追踪。
- 三阶:
  - `use_atr_trail=false`: EMA 棘轮追踪
  - `use_atr_trail=true`: ATR 吊灯追踪
- ATR 吊灯必须按 `Position.source_interval` 拉K线。
- 日志必须显示周期, 如 `ATR吊灯 SHORT: SOLUSDT (4h) ...`。

### 初始止损

SL 结构外侧原则:

```text
LONG  -> min(fractal_sl, band_sl)
SHORT -> max(fractal_sl, band_sl)
```

来源:

- 分型 SL: 必须来自本次突破之后、回踩/反弹确认之后形成的最新顶/底分型；LONG 使用底分型低点 `×0.998`, SHORT 使用顶分型高点 `×1.002`。
- 均线边缘兜底: 开仓当前确认K线六线真实上下轨 `×0.995`(LONG) / `×1.005`(SHORT)。不使用原始密集区最紧K线的边缘。
- 下单前全链路复核返回 `fractal_sl + band_sl` 两个锚点。
- 若最终止损落在入场价错误一侧，直接放弃开仓，禁止用固定 2% 人造止损兜底。
- 防滑点: SL 距离小于入场价 0.8% 时，按 0.8% 计算仓位。

### 结构指纹去重 (v1.9.15)

- `signal_key` = 币种+方向+周期+突破K+回归K+确认分型K+确认分型价。
- 同一结构只允许成交一次；盯防池触发前必须先检查 `_used_signal_keys`。
- 兼容旧日志时使用近似指纹（币种+方向+周期+确认分型价），同时兼容原始价与缓冲价。
- 新突破或新确认分型生成新指纹时可正常交易，不做时间冷却。

### 下单失败冷却 (v1.9.17)

- `order_failed` 写入 `_failed_signal_keys`，默认 30 分钟冷却。
- 盯防池触发前检查冷却；命中时记录 `order_failed_cooldown`，不再重复下单。
- 服务重启后从 `signal_events` 的 `entry_reject/order_failed` 恢复冷却状态。
- 成功开仓后清除同结构失败冷却。

### 止损单管理

- `_active_stop_ids[symbol]=orderId`
- 挂新止损前先取消旧止损。
- 成功挂新后才更新活动止损 ID。
- `_save_positions()` 必须持久化 `active_stop_id`。
- 重启后 `_restore_stop_ids()` 恢复止损ID。
- Bitget plan order 必须使用单个 `cancel-plan-order`，不要依赖 `cancel-all-plan-orders`。

### Bitget 计划止损缺失自愈 (v1.9.21)

- `_reconcile_exchange_stop_orders()` 每 60 秒核对一次本地持仓的 `active_stop_id` 是否真实存在于 Bitget 计划止损列表。
- 若已存在: 不处理。
- 若交易所有计划单但本地无 ID: 恢复最新 ID。
- 若本地有持仓但交易所无计划止损: 立即用 `current_sl + quantity + direction` 重挂。
- 自愈成功后必须 `_save_positions()`。

### Bitget 平仓后真实 PnL 反查 (v1.9.19)

- 平仓/减仓后不能只相信 `market_order()` 返回体；Bitget 常只返回 `orderId`。
- `BitgetClient.resolve_close_trade()` 按顺序反查:
  1. `/api/v2/mix/order/detail` → `priceAvg / totalProfits / fee`
  2. `/api/v2/mix/order/fills` 和 `fill-history` → 汇总成交均价、`profit` 与手续费
  3. `/api/v2/mix/position/history-position` → 止损/交易所侧清仓兜底
- `_record_exit_trade()` 优先使用交易所真实值；查不到才回退到本地估算并标记 `pnl_source`。

### 账户权益快照净值曲线 (v1.9.18)

- 设置 `account_initial_equity > 0` 后，每分钟或权益变化时记录 `equity_*.jsonl`。
- 前端净值曲线优先使用 `equity_history` 的交易所权益快照。
- 交易记录 `pnl` 不等于交易所真实盈亏；每条记录标注 `pnl_source`（交易所/估算）。
- `get_summary()` 展示三口径: 已实现盈亏 / 持仓浮动 / 账户净盈。

### 前方目标区/理论R (v1.9.23)

- 每笔开仓记录前方目标区: `target_zone_type / target_zone_price / target_r / target_distance_pct`。
- 目标区只用于复盘统计，不参与开仓、不参与平仓。
- 目标候选: 历史顶/底拐点压力支撑 + 入场前均线密集区供应/需求区。
- `target_r = |目标价-入场价| / |入场价-初始止损价|`，用于判断理论空间。

## 持仓同步与重启恢复

v1.8.7 起，普通用户实盘 bot 必须在 `macd-bot` 重启后自动恢复。

恢复规则:

- `/trader/start` 持久化 `cfg.enabled=true`。
- `/trader/stop` 持久化 `cfg.enabled=false`。
- `web_ui.py` 启动时自动恢复:
  - `enabled=true`
  - API/许可有效
  - 或旧版本存在非空 `positions_{uid}.json`
- 持仓恢复时必须保留:
  - `entry_time`
  - `source_interval`
  - `initial_sl`
  - `current_sl`
  - `max_favorable_r`
  - `partial_tp_triggered`
  - `highest_price` / `lowest_price`
  - `signal_key`
  - `btc_regime_fields`

Bitget `openTime` 可能缺失或无效。若本地已有匹配仓位，必须优先使用本地原始 `entry_time`，禁止用重启时间覆盖，否则会导致未起爆K线计数被重置。

## 仓位与风险

分层风险示例:

```text
risk_per_trade="15m:10,1h:20,4h:40,1d:80"
```

仓位计算:

```text
dynamic_risk = get_risk_for_interval(source_interval)
stop_distance = max(abs(entry - stop), entry * 0.008)
max_safe_qty = min(dynamic_risk / stop_distance, max_position_usdt / entry)
quantity = floor(max_safe_qty / step) * step
```

红线:

- 数量必须向下取整。
- 低于 minQty 或超出交易所约束直接放弃。
- 数量格式始终使用 `str(quantity)`。
- 二阶减仓必须走 `_floor_qty()`。
- 如果 `close_qty >= total_qty`，跳过减仓，只推进保护SL，禁止误全平。

## 盯防池

入池条件:

- 信号包含 `等待回踩` / `等待反弹` / `回踩确认` / `反弹确认`
- 但尚无最新分型确认

运行规则:

- 每 2 轮重扫最多 6 个最早入池信号。
- 使用 FIFO。
- 每个重扫间隔 0.5 秒，降低 Bitget 429 风险。
- 重扫时重新拉K线、重新验证完整链路、重新计算最新SL。

池子超时:

```text
15m = 2h
1h  = 8h
4h  = 24h
1d  = 72h
```

池子复核必须通过:

```text
有效突破（顺序校验）
-> 回踩/反弹期间防线未破
-> 完整顶底/底顶结构序列
-> 最新顶/底分型
-> 入场价不悬空
-> 分型不悬空
-> 结构指纹未成交过
-> 下单失败冷却已过
```

## BTC 市场环境观察 (v1.9.8)

- 第一版只记录、不干预交易。
- `get_btc_market_regime()` 固定读取 Binance 主网 `BTCUSDT` 的 `1h/4h/1d/1w` 已收盘K线。
- `btc_regime`: `btc_strong_bull / btc_bull_bias / btc_neutral / btc_bear_bias / btc_strong_bear`。
- `signal_events_*.jsonl` 在扫描、入池、入场、出场事件中写入 BTC 环境字段。
- 新仓入场时 BTC 环境写入 `Position.btc_regime_fields` 并落盘。
- `analyze_trades.py` 支持 By BTC Regime 分组报表。

## 已收盘K线/信号事件/复盘体系 (v1.9.7)

- `fetch_klines()` 默认 `closed_only=True`，丢弃未收盘K线。
- 结构化信号事件 `signal_events_*.jsonl`：扫描候选、拒绝原因、入池、池子复核、入场成功/拒绝、出场事件。
- `replay_engine.py`：本地 CSV 滚动离线回放，复用实盘扫描/复核函数。
- `analyze_trades.py`：按周期/方向/退出原因/BTC环境拆账，输出 MFE/MAE 参数诊断。
- 持仓新增 `max_adverse_r` (MAE)，交易记录新增 `r/mfe_r/mae_r/hold_minutes/interval`。

## CryptoRank 中文雷达 (v1.9.28)

将 CryptoRank 中文加密数据 Demo 集成到 AXIOM 侧边栏菜单：

```text
CryptoRank 雷达（侧边栏菜单组）
├── 首页工作台 (H)  → /cryptorank/      → 市场总览 + 头部币种 + 涨跌榜 + Upcoming + 融资
├── 融资雷达   (F)  → /cryptorank/funding → 中文融资信号面板
└── 机会页     (O)  → /cryptorank/opportunities → Upcoming + 空投活动
```

- **前端**: React + TypeScript + TailwindCSS，Vite 构建为静态文件，通过 Flask `/cryptorank/` 路由服务
- **API**: Express (端口 3001) 独立服务，双通道数据：优先官方 v2 API → 失败回退 Python 免费层抓取
- **集成方式**: iframe 嵌入 + Flask API 代理（`/api/radar` 等 6 条路由 → `localhost:3001`）
- **菜单样式**: `menu-glow-cyber` 绿色发光边框
- **构建**: `cd crypot-rank-bot-main && npm run build`（base=`/cryptorank/`）
- **Python 回退**: `scripts/cryptorank_demo.py`，需设置 `PYTHONIOENCODING=utf-8`（Windows GBK 兼容）
- **服务器部署**: 需额外 `systemctl` 服务 `cryptorank-api` 管理 Express 进程

## 数据源约束

- 手动扫描/BTC监控默认使用 Binance 主网。
- 自动交易使用用户配置的交易所。
- Bitget 合约扫描必须过滤:
  - `isRwa == YES`
  - 稳定币
  - 法币
  - 股票/ETF/商品合成资产
  - `STOCK`, `BULL`, `BEAR`, `UP`, `DOWN`, `XAU`, `XAG` 等关键词

## 演示引擎

- 双引擎: 原版结构策略 (演示 `uid=0`) + RJ-only 30m 演示引擎。
- RJ-only 引擎: `entry_signal_source=rj_only`, `scan_interval=30m`, `mode=paper` (可选 testnet live)。
- `demo_bot_config.json` 独立配置，包含信号源、RJ参数、优选池、历史胜率等。
- 演示引擎测试网保护: live/testnet 客户端未就绪时禁止本地假平仓；API 字段掩码且不覆盖已有密钥。

## UI 与品牌

AXIOM Quant 前端基调:

- 极简克制
- Titanium Gray + 深邃黑
- 战术 HUD
- 高频数据呼吸灯
- 1px 极细发光边框
- 纯 CSS 动效
- 禁止前端框架和 npm 依赖

UI 红线:

- 不破坏已有 DOM id/class 数据绑定。
- 不使用触发 reflow 的动画属性。
- 禁止复杂内联 onclick，逻辑必须抽成函数。
- 移动端保持三级断点适配。

## AI 编码绝对红线

1. 除非明确要求，禁止随意修改底层 Python 策略逻辑和 API 路由。
2. 前端视效改造不能破坏现有数据绑定。
3. 禁止引入外部前端框架或 npm 依赖。
4. 禁止未经确认删除生产数据。
5. 禁止重置用户持仓、日志、数据库。
6. 修改交易逻辑后必须同步更新 `PROGRESS.md` 和相关 memory 文档。
7. 涉及服务器上传时，必须附带重启命令。
8. 每次更新代码或配置，必须先同步到本地电脑工作文件夹并完成本地留底，再进行服务器部署；禁止只在服务器热改导致本地文件落后。

## 部署与重启

上传代码到服务器路径:

```text
<deploy-dir>
```

重启:

```bash
cd <deploy-dir>
./venv/bin/python3 -m py_compile trader.py screener.py web_ui.py admin_server.py rj_indicator.py replay_engine.py analyze_trades.py
systemctl restart macd-bot
systemctl restart macd-admin
systemctl restart cryptorank-api
```

快速备份:

```bash
cd <deploy-dir>
tar czf <backup-archive> *.py *.json *.jsonl *.db
```

常用排查:

```bash
cd <deploy-dir>
systemctl status macd-bot --no-pager -l
tail -n 120 bot_<uid>.log
grep -nE "未起爆|当前推进|本地计时|ATR吊灯|平SHORT|平LONG|ERROR|rj_filter|early_protect|RJ-only" bot_<uid>.log | tail -n 160
```
