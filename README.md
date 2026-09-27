# AXIOM Quant

机构级加密资产量化交易终端 —— 多用户 SaaS 架构，支持币安（主网 / 测试网）与 Bitget V2 双向持仓模式。

---

## 风险提示（请先阅读）

本项目涉及**真实资金交易**，使用实盘接口可能造成**全部本金损失**。

- 本项目按 "AS IS" 提供，不含任何盈利承诺或投资建议
- 历史回测表现不代表未来收益
- 请先在测试网或纸笔模式（paper mode）充分验证后再考虑实盘
- 作者不对使用本项目产生的任何资金损失负责

---

## 核心策略

**均线粘合起爆点**（6-line MA squeeze-breakout）配合严格顶底分型确认：

1. 六条均线收敛识别横盘压紧结构（标的能量积蓄）
2. 严格顶底分型确认入场时机，过滤假突破
3. 回踩判定 + 候选池机制（无分型信号入池 FIFO 重扫，2 小时超时）
4. 结构外侧止损 + 保本移动 + 1R 锁利 + ATR/EMA 阶梯追踪止盈

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12 |
| Web | Flask（用户端 `web_ui.py` :5000 / 管理后台 `admin_server.py` :5001） |
| 前端 | 原生 HTML / CSS / JS，零前端框架依赖 |
| 存储 | SQLite |
| 交易所 | 币安主网、币安测试网、Bitget V2（hedge mode） |

## 核心模块

| 文件 | 职责 |
|---|---|
| `trader.py` | 交易引擎核心（订单执行、仓位管理、止损链路） |
| `screener.py` | 币种扫描器（均线粘合检测、分型确认、回踩判定） |
| `web_ui.py` | 用户端交易面板（手动扫描、BTC 环境监控） |
| `admin_server.py` | 管理后台（用户管理、引擎控制、持仓管理） |
| `account_manager.py` | 多用户账户与许可系统 |

## 风控设计

仓位规模由风险公式严格约束，向下取整，**永不超过上限**：

```
max_safe_qty = min(risk_per_trade / |entry - SL|,
                   max_position_usdt / entry_price)
```

- **结构外侧止损**：LONG 取 `min(fractal_sl, band_sl)`，SHORT 取 `max(...)`
- **均线边缘兜底**：无分型时回退到确认 K 线六线真实上下轨
- **保本移动**：浮盈达 `risk × breakeven_r` 时止损移至入场价
- **1R 锁利**：保本瞬间若浮盈已达 1R，直接锁定 50% 利润
- **止损单去重**：按 symbol 维护活跃止损单 ID，挂新前先撤旧并持久化，重启自动恢复
- **日亏损熔断**：`max_daily_loss` 大于 0 时启用（0 表示不限）

## 数据源分离

- 手动扫描 / BTC 环境监控 → 币安主网
- 自动交易 → 用户各自配置的交易所与密钥

纸笔模式（`mode="paper"`）下跳过全部交易所校验，可用于零风险演练。

## 快速开始

```bash
pip install flask requests pandas numpy

# 纸笔模式演练（无需 API 密钥）
python web_ui.py

# 管理后台
python admin_server.py
```

生产部署建议使用 systemd：

```bash
systemctl restart macd-bot     # 交易面板 :5000
systemctl restart macd-admin   # 管理后台 :5001
```

> 交易所密钥通过 Web 面板按用户单独配置，保存在 SQLite 中，**不写入代码库**。

## 项目文档

- `AGENTS.md` —— 全局架构约束与开发红线
- `docs/` —— 策略设计与迭代计划归档

---

## License

[MIT](LICENSE) —— 但请注意：代码可自由使用，**交易风险由使用者自行承担**。
