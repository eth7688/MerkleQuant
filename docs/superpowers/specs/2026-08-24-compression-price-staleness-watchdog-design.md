# 动能压缩实时价格失活与冗余监控修复设计

**日期：** 2026-08-24  
**状态：** 待实施  
**范围：** 15M 动能压缩观察池的实时价格监控与突破事件生成

## 1. 背景与根因

生产扫描器能够发现合格压缩结构，但曾出现候选入池后价格已经越过突破阈值、系统仍未生成 `BREAKOUT_FRESH_LONG/SHORT` 的情况。历史重放确认状态转换函数本身能够正确生成事件，断点位于实时价格进入状态机之前。

当前监控器只用 `_stream_connected` 判断 WebSocket 是否健康。连接一旦表面保持打开，即使长时间没有收到行情，REST 兜底也不会运行；同时系统没有保存最后一条有效 WebSocket 行情时间，因此无法识别或展示静默失活。

## 2. 目标

1. WebSocket 静默失活时，观察池仍能通过 REST 价格继续监控突破。
2. 超过 15 秒没有有效 WebSocket 行情时，状态明确变为 `stale` 并主动重连。
3. 观察池非空时，每 5 秒执行一次冗余 REST 价格补查，不等待 WebSocket 失活。
4. WebSocket 与 REST 同时发现同一突破时，只生成一个 FRESH 事件。
5. 不改变压缩选币规则、观察池数据模型、企业微信配置或交易逻辑。

## 3. 方案选择

采用 A+ 双通道方案：

- WebSocket 是低延迟主行情源。
- REST 是观察池非空时的固定 5 秒冗余行情源。
- WebSocket 连续 15 秒没有有效行情时，监控器标记 `stale`、关闭当前连接并进入既有重连循环。
- 两路价格统一调用现有 `_apply_prices()`，继续使用状态文件锁、`fresh_emitted` 和 `emitted_event_ids` 去重。

不采用“只在断线后请求 REST”，因为静默连接不会进入兜底；不采用“始终全市场高频轮询”，因为观察池为空时没有业务收益。

## 4. 组件与状态

### 4.1 WebSocket 行情新鲜度

`CompressionMonitor` 增加运行时字段：

- `_last_stream_message_at_ms`：最近一条格式正确、至少包含一个有效价格的 miniTicker 消息时间。
- `stream_stale_after_ms = 15_000`：失活阈值，保持为构造参数默认值以便确定性测试，不增加用户配置项。

连接打开时以当前时间建立 15 秒宽限期；收到有效消息后刷新时间。格式错误或完全无有效价格的消息不得刷新心跳。

### 4.2 冗余 REST 补价

既有 fallback worker 保持 5 秒循环：

- 观察池为空：不请求 REST。
- 观察池非空：无论 WebSocket 是否 connected，都请求一次 Binance Futures ticker 价格并只保留池内交易对。
- REST 价格与 WebSocket 价格走同一个 `_apply_prices()` 入口。
- REST 失败只记录错误并等待下一轮，不停止 WebSocket、扫描器或调度线程。

### 4.3 静默失活与重连

fallback worker 每轮同时检查 WebSocket 新鲜度：

- 未连接：状态沿用 `connecting/reconnecting/unavailable`，REST 继续补价。
- 已连接且最后有效消息距当前时间不超过 15 秒：状态为 `connected`。
- 已连接但超过 15 秒：状态置为 `stale`，将连接标记为不可用并关闭当前 WebSocket；stream worker 随后进入既有退避重连流程。

重复检查不得对同一连接反复执行 close。新连接成功后重新建立宽限期。

### 4.4 状态输出

监控状态增加：

- `last_price_message_at`
- `price_stream_status` 允许 `stale`

现有用户端和管理员端继续使用同一状态对象展示，不新增配置表单。REST 补价成功不得把 WebSocket 的 `stale` 状态伪装成 `connected`。

## 5. 数据流

```text
WebSocket miniTicker ──┐
                      ├─> _apply_prices() ─> apply_live_prices()
5秒 REST ticker ──────┘                         │
                                               ├─> 原子保存观察池状态
                                               └─> 单次 FRESH 事件与警报回调

WebSocket 心跳超过15秒
        └─> stale ─> close 当前连接 ─> 既有重连循环
```

## 6. 并发与去重

- WebSocket 与 REST 可能在相邻时刻提交同一交易对价格。
- `_apply_prices()` 继续使用进程内状态锁和跨工作者文件锁串行化读取、转换与保存。
- 第一条越线价格设置 `fresh_emitted=True` 并登记事件ID；后续价格只能进入 ACTIVE/RETRACING，不得再次生成 FRESH。
- 不新增第二套事件队列或独立去重逻辑。

## 7. 错误处理

- WebSocket 消息格式错误：保持既有丢弃计数与错误信息，不刷新心跳。
- REST 请求失败：保留 WebSocket 工作，5 秒后重试。
- 主动关闭失活连接失败：记录 WebSocket 错误，stream worker仍按现有生命周期处理。
- 状态文件或事件回调异常：保持现有错误边界，不吞掉已原子保存的事件。

## 8. 测试策略

实施必须按测试驱动完成：

1. **RED：** connected 但 15 秒无有效消息时，fallback 必须执行 REST、状态变为 stale 并关闭连接。
2. **RED：** connected 且新鲜时，只要观察池非空，5 秒循环仍执行一次冗余 REST 补价。
3. **RED：** 观察池为空时跳过 REST 请求。
4. **RED：** 有效 miniTicker 刷新 `last_price_message_at`；畸形消息不刷新。
5. **RED：** 用 TREE 历史结构和越线价格重放，REST 路径必须生成一个 FRESH 事件。
6. **RED：** WebSocket 与 REST 连续提交同一越线价格，只允许一个事件。
7. 验证现有自动扫描、停止/重启、状态输出和警报集成测试不回归。
8. 运行项目完整测试、`py_compile` 与 `git diff --check`。

## 9. 明确不在本次范围

- 不调整 `contraction_ratio_max=0.65` 或其他选币阈值。
- 不修改 G04 EMA 区间语义；该项会改变策略判定，需单独设计与回测。
- 不增加最近24小时候选历史页面。
- 不修改快照预热K线格式。
- 不发送测试微信消息，不改生产配置，不自动部署。

## 10. 验收条件

- 观察池非空时，REST 补价间隔不超过 5 秒。
- WebSocket 15 秒无有效行情后可观测为 stale，并触发单次关闭与重连。
- TREE 历史越线场景生成且仅生成一个 FRESH 事件。
- 两路行情并发不会产生重复事件。
- 现有扫描和警报测试全部通过，生产代码改动仅限实时监控及必要状态展示。
