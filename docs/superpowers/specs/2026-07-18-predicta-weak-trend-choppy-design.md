# Predicta 弱趋势震荡过滤增强设计

## 目标

在保留现有低波动压缩过滤的基础上，仅为 Predicta 开仓链路增加“弱趋势、低方向效率”识别，拦截 DEXE 这类波动幅度不小但长期来回摆动的假趋势信号。

本次增强只影响部署后产生的新 Predicta 信号。RJ、已有持仓、止损、止盈、追踪和出场逻辑均不改变。

## 已验证的问题

DEXEUSDT 2026-07-18 04:30（北京时间）的 30 分钟信号 K 在现有过滤中记录为：

- `predicta_choppy_filter_mode=hard`
- `choppy_filter_is_choppy=false`
- `choppy_atr_ratio=0.875601`
- `choppy_box_amplitude=0.086185`
- `choppy_box_threshold=0.059208`
- `choppy_box_position=0.737615`

因此，ATR 未低于 0.70、箱体振幅未小于动态阈值、收盘位置也不在箱体 40%～60% 中部，现有三项条件全部未命中。

使用服务器真实闭合 K 线重新计算，该信号 K 的：

- `ADX(14)=9.648332`
- `ER(20)=0.044614`
- 最近 24 根收盘价穿越 EMA20 共 8 次

这说明它不是低波动压缩，而是趋势强度极弱、方向效率极低的来回摆动行情。

对演示引擎重置后最近 12 个真实 Predicta 候选进行同口径抽样，`ADX(14) < 18 AND ER(20) < 0.20` 只拦截两次 DEXE；ADA、TRUMP、SENT、AVAX、APT、SOL、XPL、T、GALA 均未被新增条件拦截。

## 判定规则

Predicta 的最终震荡判定调整为：

```text
现有压缩型震荡成立
OR
(ADX(14) < 18 AND ER(20) < 0.20)
```

其中方向效率 ER 使用：

```text
ER(20) = abs(close[t] - close[t-20])
         / sum(abs(close[i] - close[i-1]), i=t-19..t)
```

ER 分母为 0、ADX/ER 非有限值或历史数据不足时，不启用新增弱趋势条件，保留现有过滤结果，避免数据异常造成误拦截。

所有计算以 Predicta 信号 K 为锚点，只使用信号 K 及以前的已收盘 K 线，不读取确认后的 K 线或实时未收盘 K 线。

## 架构与隔离

### 公共压缩过滤保持不变

`strategy_filters.evaluate_choppy_market_adaptive()` 继续提供现有 ATR、箱体振幅和箱体位置判断。RJ 继续只调用该函数，因此 RJ 行为完全不变。

### Predicta 专用增强器

在 `strategy_filters.py` 增加 Predicta 专用函数：

```python
evaluate_predicta_choppy_market(df, anchor_idx=None) -> dict[str, Any]
```

该函数先取得 `evaluate_choppy_market_adaptive()` 的结果，再在同一锚点计算 ADX(14) 和 ER(20)。如果两项同时低于阈值，则：

- `choppy_filter_is_choppy=True`
- 向 `choppy_filter_reasons` 追加 `weak_directional_efficiency`
- 当原结果为 `pass` 时，将 `choppy_filter_reason` 改为 `weak_directional_efficiency`

`SqueezeBreakoutBot._predicta_choppy_filter_state()` 改用专用函数；`_rj_choppy_filter_state()` 保持调用公共函数。

## 审计字段

Predicta 状态新增以下只读审计字段：

- `choppy_adx_period=14`
- `choppy_adx`
- `choppy_efficiency_period=20`
- `choppy_efficiency_ratio`

这些字段随 Predicta 候选、入场事件和持仓入场快照持久化，便于复盘为什么放行或拦截。不得使用当前行情为旧持仓补算，旧记录缺失时保持 `None`。

现有管理后台仍只暴露“震荡过滤拦截”两态开关：

- `hard`：压缩型或弱趋势型震荡均拦截。
- `off`：两类震荡判断均不拦截。

不新增阈值配置项，不暴露 `log_only`，避免本次范围扩张。

## 数据流

1. Predicta 在闭合 K 线上生成信号标签。
2. 以信号 K 索引调用 `evaluate_predicta_choppy_market()`。
3. 先运行原压缩过滤，再计算 ADX(14) 和 ER(20)。
4. 将合并结果写入 Predicta setup/signal。
5. `hard` 且最终 `choppy_filter_is_choppy=True` 时，不进入快速开仓或等待确认池。
6. `off` 时继续生成候选，但仍保留禁用状态语义；已有持仓不重新评估。

## 测试

### 指标单元测试

- 真实 DEXE 30 分钟闭合 K 线固定样本应命中 `weak_directional_efficiency`。
- 明确单边趋势样本即使起步阶段 ADX偏低，只要 ER 不低于 0.20，就不得被新增组合条件拦截。
- ADX低但 ER高时放行。
- ER低但 ADX高时放行。
- 数据不足或 ER 分母为 0 时保持原过滤结果，不抛异常。
- 输入 DataFrame 不得被修改。

### 链路测试

- Predicta `hard` 模式拦截 DEXE 类信号。
- Predicta `off` 模式放行同一信号。
- RJ 对同一输入的过滤结果与修改前保持一致。
- 审计字段进入信号事件和持仓入场快照。

### 完整验证

- Python 编译通过。
- 完整 `unittest` 回归无失败。
- 冻结 DEXE 信号重放返回无开仓信号。
- 最近 12 个已记录候选离线重放中，新增平衡条件只命中两次 DEXE，结果需与设计阶段抽样一致；若行情接口无法重建完全相同的历史窗口，则以冻结样本测试为部署门槛，并明确记录无法复核的项目。

## 部署与安全

- 先更新本地文件与测试，再部署服务器。
- 部署前备份服务器 `trader.py`、`strategy_filters.py` 和 `demo_bot_config.json`。
- 只上传本次实际修改的 Python 文件，不上传测试、设计文档、计划、`PROGRESS.md` 或记忆文件。
- 服务器编译通过后只重启 `macd-bot`；`macd-admin` 无需重启。
- 部署后验证两个服务均为 `active`，演示引擎仍为 `predicta_ewo`、震荡过滤仍为 `hard`，API 凭证存在但绝不输出内容。
- 不平掉 DEXE 或其他已有持仓，不清理盈亏数据。
- 部署验证后更新本地 `PROGRESS.md`，记录变更、备份路径、部署文件和验证结果。
