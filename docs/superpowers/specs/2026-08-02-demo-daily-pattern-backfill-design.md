# 演示引擎昨日 UTC 日线形态修复与回填设计

## 目标

修复演示引擎只为 `rj_only` 入场记录日线快照、却遗漏实际使用的 `predicta_ewo` 入场的问题；同时回填该功能首次上线后已经产生、但仍标记为 `not_recorded` 的持仓和交易记录。

成功后：

- 演示引擎的 `structure`、`rj_only`、`predicta_ewo` 三种有效入场来源都在启用日线影子记录时保存不可变快照。
- 持仓卡片和交易记录读取同一份入场快照，不根据当前行情动态重算。
- 回填只使用每笔交易入场时刻之前已经收盘的 Bitget `1Dutc` K线，不引入未来数据。
- 功能上线前的旧历史继续显示“未记录”。
- 日线形态继续保持 `log_only`，不改变演示引擎是否开仓。

## 已确认根因

`trader.py` 目前只在 `source_strategy == "rj_only"` 分支调用 `_daily_pattern_state_for_entry()`。线上功能部署后的 8 笔入场全部是 `predicta_ewo`，因此 8 笔均未产生 `rj_daily_pattern_shadow` 事件，持仓构造时只能把空值标准化为 `recorded=false / reason=not_recorded`，平仓后交易记录继续复制该空快照。

## 方案选择

采用“公共入场快照 + 一次性确定性迁移”。

未采用：

- 页面实时计算：同一笔交易会随当前日K变化，破坏入场证据的不可变性。
- 每次启动自动回填：会让生产启动隐式修改历史文件，并重复产生网络与数据风险。
- 只修未来交易：无法满足本次同时修复已有持仓和交易记录的要求。

## 新入场数据流

在公共订单入场链路完成基础预检后、创建订单前读取 `rj_daily_pattern_filter_mode`：

1. 模式为 `log_only` 或 `soft` 时，不再按 `source_strategy` 限制；`structure` 与共用关键K线链路通过同一个附加/决策帮助函数，统一拉取 Bitget 最近已收盘的 `1Dutc` K线。
2. 以当前决策时间调用 `evaluate_daily_pattern_state()`，把形态、方向、同向关系、日K开收时间和模式写入 `signal["daily_pattern"]`。
3. 写入 `rj_daily_pattern_shadow` 审计事件。
4. 成交后将同一快照复制到 `Position.daily_pattern`；平仓时继续复制到交易记录。
5. 保留现有行为边界：演示引擎固定 `log_only`，不拦截任何交易；其他引擎默认 `off`。现有 `soft` 拦截语义仍只作用于原来的 `rj_only` 路径，本次不扩大交易决策变化。

行情拉取失败时保存 `daily_fetch_failed`，不伪造成“无形态”，也不阻止 `log_only` 交易。

## 一次性回填边界

回填目标同时满足：

- 实体来自 `positions_<uid>.json` 或 `trades_<uid>.jsonl`。
- 入场时间不早于日线影子功能首次生产部署时间 `2026-08-01T08:10:32Z`。
- 当前 `daily_pattern.recorded` 为 `false`，且原因为 `not_recorded` 或字段缺失。
- 记录具备可解析的币种、方向和入场时间。持仓直接使用`entry_time`；交易记录的`time`是平仓时间，必须通过唯一`signal_key`关联`signal_events_0.jsonl`中的`entry_filled.time`，严禁把平仓时间当作入场时间。

每条目标记录按其原始入场时间作为 `decision_time`。评估器只允许选择 `candle_close_time <= decision_time` 的日K；即使拉取结果包含后来K线，也不得读取。

项目通过 `bj_now()` 保存的持仓、信号事件和交易时间，是“北京时间墙上时间 + `+00:00` 标记”，并不是真实 UTC。迁移读取 `positions_<uid>.json.entry_time`、`signal_events_0.jsonl` 的 `entry_filled.time`，以及交易缺失关联时用于 fail-closed 判断的退出 `time` 时，必须在 ISO 解析后减去 8 小时再参与截止时间和日K判断。CLI 的 `--cutoff` 是真实 UTC，必须使用普通 UTC 解析，不能减 8 小时。

功能上线前的记录、已有真实快照均不修改。功能上线后可能符合回填条件、但无法唯一关联入场事件的交易会使整次迁移中止，不猜测时间。迁移重复执行时结果为零变更。

## 迁移安全性

- 先停止 `macd-bot`，避免运行中引擎覆盖持仓或追加交易记录；`macd-admin`无需写这些文件。
- 在服务器创建带时间戳备份，包含代码、`positions_<uid>.json`、`trades_<uid>.jsonl`、只读关联源`signal_events_0.jsonl`、`demo_bot_config.json`和数据库。
- 迁移工具先执行 dry-run，输出目标数、成功数、失败数和每笔快照摘要，不写文件。
- 只要任何目标记录无法拉取或评估，正式迁移整体中止，不写任何文件。
- 正式应用先生成并校验两个临时文件，再用 `os.replace` 替换；未变更的 JSONL 行保持原文，避免无关重写。进程内第二次替换失败时尽力回滚已替换文件；操作系统在两次替换之间崩溃仍须从部署前备份恢复。
- 应用后做结构化对比：除目标记录的 `daily_pattern` 外，持仓、交易、盈亏、价格、止损和数量字段必须完全一致。
- 任一步失败时从本次备份恢复并重新启动原服务。

## 测试与验收

自动测试覆盖：

- `structure`、`rj_only`、`predicta_ewo` 入场都会通过共用帮助函数生成并持久化日线快照。
- `rj_only` 原行为保持。
- `off` 模式不拉取日K、不改变交易决策。
- 回填严格按截止时间筛选，未来日K不参与评估。
- 回填将项目保存的北京墙上时间转换为真实 UTC；覆盖部署截止点前后 1 秒，以及真实 20:00 UTC 入场不得读取随后 00:00 UTC 才收盘日K的跨日场景。
- 已记录、部署前和不可解析记录不变。
- dry-run 不写文件；重复 apply 为零变更；任一目标失败时全量不写。

生产验收：

- 本地完整测试、`py_compile`和`git diff --check`通过。
- 服务器暂存代码编译通过，dry-run 目标与线上真实遗漏一致。
- 回填后当前持仓卡片和对应交易记录不再显示“未记录”。
- 回填记录的日K日期早于或等于其入场决策时间，并与 Bitget `1Dutc`边界一致。
- 两项服务正常，演示引擎运行，日志无新增错误。
- 受保护文件中，配置和数据库哈希不变；持仓与交易文件的结构化差异仅限批准的 `daily_pattern` 字段。
