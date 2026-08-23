# 动能压缩规则对齐修复设计

日期：2026-08-23

## 问题与边界

线上扫描曾把 `LUMIAUSDT` 判定为 `COMPRESSION_ACTIVE_SHORT`。该候选使用 Binance U 本位合约在北京时间 2026-08-23 09:00 至 13:00 的 16 根已收盘 15 分钟 K 线。现有代码用拟合包络首尾宽度比计算 `contraction_ratio`，得到 `0.458` 并通过 `0.65` 上限；规则文档要求的前后各三分之一平均 K 线振幅比为 `1.416`，应当拒绝。

现有代码还用 `abs(EMA8 - EMA21) / ATR14` 检查 EMA 距离，而规则文档要求 `abs(last_close - EMA8) / ATR14`。这不是 LUMIA 误报的直接原因，但属于同一规则对齐缺陷。

本修复继续使用 Binance U 本位合约作为动能压缩扫描数据源，不切换 Bitget，不改变成交额门槛、窗口长度、枢轴边界、触碰次数、摆动结构、突破状态机、声音提醒、企业微信投递或交易逻辑。

## 规则修复

### 振幅收缩率

- 令 `n = len(W)`，`third = floor(n / 3)`。
- 前段为窗口最前 `third` 根，后段为窗口最后 `third` 根；中间余数 K 线不进入两端平均。
- 每根振幅为 `high - low`。
- `contraction_ratio = mean(last_third_range) / mean(first_third_range)`。
- 前段平均振幅小于等于零时，结果为无穷大并拒绝。
- `contraction_ratio <= 0.65` 通过；大于 `0.65` 使用现有拒绝码 `INSUFFICIENT_CONTRACTION`。
- 拟合包络宽度继续用于边界几何，不再参与收缩率计算。

### EMA8 距离

- `ema_distance_atr = abs(last_closed_close - last_ema8) / last_atr14`。
- `ema_distance_atr <= 1.0` 通过；大于 `1.0` 使用现有拒绝码 `EMA_DISTANCE_TOO_WIDE`。
- EMA8/EMA21 全窗口顺序、收盘价位于均线区外等其他硬筛保持不变。
- 质量分中的 `ema_proximity` 使用同一个修正后的 `ema_distance_atr`，避免硬筛与评分口径分裂。

## 数据源可见性

- 扫描服务继续显式请求 `exchange="binance"`、`market_type="futures"`、`testnet=False`。
- 动能压缩页面固定显示“数据源：Binance Futures”。
- 本修复不把 Bitget K 线与 Binance 候选混算；用户需要用 Binance 合约图表核验形态。

## 数据流与兼容性

1. 扫描服务获取 Binance U 本位合约的已收盘 15 分钟 K 线。
2. 指标准备阶段继续基于完整历史计算 EMA8、EMA21 和 ATR14。
3. 最大结构后缀选择对每个候选窗口应用修正后的收缩率和 EMA8 距离规则。
4. 只有所有硬筛通过的窗口进入观察池；现有池条目会在下一次成功扫描时按新规则重新评价并自然移除或延续。
5. 状态文件格式和历史记录格式不变，不做数据回填或破坏性迁移。

## 测试与验收

- 保存可重放的 LUMIA Binance 历史 OHLCV 测试夹具，带来源、交易对、周期和截止时间元数据，不伪造市场数据。
- 端到端重放该窗口，断言 SHORT 结果为 `REJECTED`，包含 `INSUFFICIENT_CONTRACTION`，并且收缩率约为 `1.416`。
- 单元测试验证收缩率恰好 `0.65` 通过，超过 `0.65` 拒绝；窗口长度不能整除 3 时，中间余数不参与两端平均。
- 单元测试验证收盘价距 EMA8 恰好 `1.0 ATR` 通过，超过 `1.0 ATR` 拒绝。
- 回归测试验证拟合包络边界、方向触碰和突破状态机没有改变。
- 页面测试验证固定显示“数据源：Binance Futures”。
- 运行动能压缩相关测试、完整测试集、Python 编译和差异检查。
- 本地验证完成后单独请求生产部署授权；部署前备份，部署后更新本地 `PROGRESS.md`。
