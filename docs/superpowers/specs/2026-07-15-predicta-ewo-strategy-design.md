# Predicta + EWO 自动交易策略设计

**日期：** 2026-07-15
**状态：** 已获用户设计批准，等待书面规格复核
**范围：** 新增独立 `predicta_ewo` 入场信号源；复用现有风险、止损与出场基础设施；不修改或删除 RJ 策略。

## 1. 目标

新增独立的 Predicta + EWO 自动交易策略，用 Predicta 原版 `bullSignal` / `bearSignal` 定义信号 K，以现有自适应震荡过滤器去除震荡假信号。信号 K 收盘时 EWO 已同向则走快速开仓；EWO 反向或等于 0 时，等待最多 6 根已收盘 K 线完成关键 K 突破和 EWO 转向确认。

新策略只替换开仓信号链路。仓位计算、初始止损、交易所止损单、提前保护、阶梯锁利、分批止盈和追踪退出继续复用现有系统。新策略不执行任何时间止损。

## 2. 非目标

- 不删除、覆盖或改写 RJ-only 策略。
- 不把 Predicta 的 `Perfect Time`、Prediction 百分比或 8 项共振作为开仓硬条件。
- 不在本次工作中优化 Predicta、EWO 或震荡过滤参数。
- 不使用未收盘 K 线、盘中瞬时标签或 near-close 近似收盘确认。
- 不把快速路径回测成交价倒填为信号 K 收盘价；信号只在收盘后可知，成交必须发生在下一笔可成交价。
- 不改变底层交易所 API、账户系统或现有 RJ 历史统计。
- 不在缺少同口径回放证据时宣称新策略具有正收益。

## 3. 策略标识与兼容性

新增信号源和持仓来源：

```text
entry_signal_source = predicta_ewo
source_strategy = predicta_ewo
```

RJ 继续作为独立可选信号源存在。Predicta 的候选池、事件名称、信号指纹、交易记录和状态展示不得使用 RJ 名称，以免污染 RJ 统计或造成复盘混淆。

继续复用现有可配置项：

- `scan_interval`
- `max_positions`
- `risk_per_trade`
- `max_position_usdt`
- 全局风险限制与交易所数量精度规则

新增最少量的策略配置：

```text
predicta_confirm_bars = 6
predicta_choppy_filter_mode = hard
predicta_confirm_atr_buffer = 0.08
predicta_ewo_fast = 5
predicta_ewo_slow = 35
```

Predicta 第一版固定复刻源码参数：EMA 8/21、Supertrend ATR 周期 10、Supertrend 系数 3.0。第一版不开放这些参数，避免尚未验证前扩大优化空间。

## 4. 指标定义

所有指标只在已收盘 K 线上计算，数据源为当前策略周期的 OHLCV。

### 4.1 Predicta 趋势与标签

复刻 Predicta V4 自定义 Supertrend、EMA 和 Delta 公式。

LONG 信号 K：

```text
EMA8 上穿 EMA21
AND Custom Supertrend 为多头
AND Predicta Delta > 0
```

SHORT 信号 K：

```text
EMA8 下穿 EMA21
AND Custom Supertrend 为空头
AND Predicta Delta < 0
```

Predicta Delta 保持原源码口径：

```text
range = high - low
buy_volume  = volume * (close - low) / range
sell_volume = volume * (high - close) / range
delta = buy_volume - sell_volume
```

当 `high == low` 时，买卖量各按总量的 50% 处理，Delta 为 0，因此不能形成方向信号。

### 4.2 EWO

EWO 使用收盘价：

```text
EWO = SMA(close, 5) - SMA(close, 35)
```

是否换算为当前价格百分比不影响颜色判断。确认规则只使用符号：

- LONG：`EWO > 0`
- SHORT：`EWO < 0`
- `EWO == 0`：不确认

## 5. 信号与双路径状态机

### 5.1 信号 K 阶段

在一根已收盘 K 线上出现 `bullSignal` 或 `bearSignal` 后，以该 K 线为震荡过滤锚点，调用现有自适应震荡过滤器。

- `hard` 且判定震荡：记录拒绝事件，不进入候选池。
- 非震荡：根据同一根信号 K 的 EWO 方向进入快速路径或等待路径。

震荡过滤只在信号 K 上决定是否入池，不在确认阶段重复改变历史判定。RJ 当前的 `rj_choppy_filter_mode` 保持不变。

等待路径的候选项至少保存：

- symbol、direction、interval
- 信号 K 开盘时间、高点、低点
- 信号 K 的 Predicta 趋势、EMA、Delta 快照
- 信号 K 当时的 ATR 诊断值、确认价
- 已等待 K 数、到期时间
- 唯一 Predicta 信号指纹
- 震荡过滤证据

### 5.2 路径 A：EWO 同向快速开仓

信号 K 收盘时：

- LONG 且 `EWO(signal_bar) > 0`：无需等待关键 K 突破，立即生成可执行入场决策。
- SHORT 且 `EWO(signal_bar) < 0`：无需等待关键 K 突破，立即生成可执行入场决策。

快速路径仍必须通过现有 BTC 方向、风险限制、最大持仓、同币持仓、信号去重和交易所可交易性检查。实盘在信号 K 确认收盘后下市价单；回放在信号决策时间之后的下一根可用 1 分钟 K 线开盘价成交，禁止使用信号 K 收盘价倒填成交。

快速路径若因 BTC 过滤、风险限制、限仓、重复信号、不可交易合约或下单失败被拒绝，不降级进入等待候选池。下单失败继续使用现有失败信号短冷却。

### 5.3 路径 B：EWO 反向等待确认

信号 K 收盘时，LONG 的 EWO 小于等于 0，或 SHORT 的 EWO 大于等于 0，才创建候选并进入确认窗口。

只检查信号 K 之后的第 1 至第 6 根已收盘 K 线。信号 K 自身不算确认窗口。

LONG 同时满足以下条件时确认：

```text
confirm_close > signal_high + ATR * predicta_confirm_atr_buffer
AND EWO(confirm_bar) > 0
```

SHORT 同时满足以下条件时确认：

```text
confirm_close < signal_low - ATR * predicta_confirm_atr_buffer
AND EWO(confirm_bar) < 0
```

突破缓冲和最终初始止损均使用确认 K 收盘时可获得的 ATR，禁止未来数据。信号 K 高低点始终是价格结构锚点；信号 K 当时的 ATR 只用于诊断，不锁定最终止损距离。

如果价格已突破但 EWO 不同向，候选继续保留到第 6 根。后续 K 线只要收盘仍位于确认线外且 EWO 转为同向，即可确认；不要求价格再次穿越确认线。

### 5.4 失效与超时

- LONG 候选在确认前若任一已收盘 K 线收盘价低于信号 K 低点，立即失效。
- SHORT 候选在确认前若任一已收盘 K 线收盘价高于信号 K 高点，立即失效。
- 第 1 至第 6 根均未确认时，候选在第 6 根检查完成后超时删除。
- 第 7 根及之后禁止使用该候选开仓。
- 同一信号指纹最多成交一次。
- 同一币种已有持仓时，不积压可在持仓结束后复用的旧候选。

## 6. 仓位与初始止损

信号 K 始终作为止损结构锚点。快速路径使用信号 K 收盘时可获得的 ATR；等待路径使用确认 K 收盘时可获得的 ATR。

LONG：

```text
raw_stop = signal_low - 0.5 * ATR
```

SHORT：

```text
raw_stop = signal_high + 0.5 * ATR
```

继续执行现有约束：

- 最小止损距离 0.3%。
- 最大止损距离 8%；超过则拒绝开仓。
- 按 `risk_per_trade / abs(entry - stop)` 计算风险数量。
- 同时受 `max_position_usdt / entry` 限制。
- 按交易所 step size 向下取整。
- 低于 minQty 或违反交易所限制时拒绝。
- 数量沿用字符串格式约束。
- 继续执行全局限仓、日风险、重复信号和下单失败冷却规则。

## 7. 出场策略

Predicta 持仓复用现有非时间型出场与保护：

- 初始交易所止损单及止损单去重、自愈、持久化。
- 0.8R 提前保护。
- 1.2R 阶梯防守。
- 现有分批止盈规则。
- EMA 或 ATR 追踪止盈。
- 手动平仓、交易所侧清仓识别和真实 PnL 反查。

Predicta 持仓必须跳过全部时间止损分支：

- 普通未起爆超时。
- RJ 两段式延长观察。
- 任何由持仓 K 数或持仓分钟数直接触发的强制退出。

跳过时间止损不得影响止损、保护、减仓或追踪退出。

## 8. 事件、状态与界面

新增独立事件：

```text
predicta_signal
predicta_choppy_reject
predicta_fast_confirm
predicta_fast_reject
predicta_setup_add
predicta_wait_ewo
predicta_confirm
predicta_invalidated
predicta_timeout
predicta_entry_filled
```

事件记录必须包含信号 K、确认 K、突破线、EWO、震荡过滤证据、止损、风险和信号指纹。后台信号源选择增加“Predicta + EWO”，状态接口展示候选数量和最近一次拒绝/确认原因。

现有 ID、API 兼容结构和 RJ 展示不得被破坏。

## 9. 错误处理

- 指标数据不足：不产生信号，并记录可诊断原因。
- OHLCV 存在非数值或缺失：拒绝当前计算，不使用猜测值。
- EWO 尚未积累 35 根有效 K 线：不得确认。
- ATR 无效、入场价或止损价非正、止损方向错误：拒绝开仓。
- 震荡过滤计算失败：硬模式下安全拒绝，不静默放行。
- 网络或交易所下单失败：沿用现有失败事件和短冷却，不把候选标记为已成交。

## 10. 验证标准

### 10.1 单元与集成测试

- Python 的 EMA、Supertrend、Delta、EWO方向与固定 Pine 样本一致。
- 未收盘 K 线不能产生信号或确认。
- `bullSignal` / `bearSignal` 只在交叉 K 出现，不要求确认 K 再次交叉。
- 震荡信号无法进入候选池。
- 第 1 至第 6 根可以确认，第 7 根不能确认。
- 突破但 EWO 不符时继续等待。
- EWO 为 0 时不确认。
- 反向收盘突破使候选立即失效。
- LONG 与 SHORT 规则镜像。
- 同一信号不能重复成交。
- 信号 K EWO 同向时生成快速入场，不进入候选池。
- 信号 K EWO 反向或为 0 时才进入候选池。
- 快速路径按下一笔可成交价成交，回放不得使用信号 K 收盘价。
- 快速路径被风险、BTC、限仓或下单失败拒绝后不得降级入池。
- 初始止损、最小/最大止损和仓位向下取整保持正确。
- Predicta 持仓不触发任何时间止损。
- 提前保护、阶梯锁利、减仓、追踪和交易所止损继续生效。
- RJ 策略原测试继续通过。

### 10.2 回放报告

使用现有事件驱动回放基础设施，至少报告：

- Predicta 原始标签数量。
- EWO 同向快速决策、快速成交和快速拒绝数量。
- 震荡过滤拒绝数量和比例。
- 候选池数量。
- 突破但等待 EWO 的数量。
- 最终确认和成交数量。
- 胜率、平均 R、中位 R、总 R、Profit Factor。
- 最大回撤 R、MFE、MAE、手续费和滑点后权益。

低于项目规定的独立持仓样本门槛时，结果继续标记 `SAMPLE_NOT_READY`。首轮结果用于验证实现和发现明显问题，不作为正收益证明。

## 11. 完成定义

满足以下全部条件才算实现完成：

1. 新增 `predicta_ewo`，且 RJ 可继续独立运行。
2. 已收盘K线、震荡硬过滤、EWO同向快速开仓、EWO反向6K候选、关键K突破和EWO转向确认按本规格执行。
3. Predicta 持仓复用现有风险与非时间型退出，但不执行任何时间止损。
4. 新旧策略事件和交易记录可清晰区分。
5. 相关测试、现有回归测试和 Python 编译通过。
6. 本地事件回放产出规定指标，并如实标记样本状态。
7. 未经用户另行确认，不进行生产服务器部署。
