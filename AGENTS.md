# AGENTS.md — AXIOM Quant 全局架构与绝对红线

## 项目标识
**AXIOM Quant** — 顶级量化执行终端 (Titanium Build)
机构级加密资产量化交易平台，多用户 SaaS 系统。

## 技术栈
- **后端**: Python 3.12 + Flask (web_ui.py:5000 / admin_server.py:5001)
- **前端**: 纯原生 HTML/CSS/JS，零外部前端框架，无 npm 依赖
- **数据库**: SQLite (axiom_accounts.db)
- **交易**: 币安主网 API + 币安测试网 API + Bitget V2 API (hedge mode)
- **策略**: 均线粘合起爆点 (6-line MA squeeze-breakout) + 严格顶底分型确认

## 核心文件索引
| 文件 | 职责 | 行数 |
|------|------|------|
| `trader.py` | 交易引擎核心 (SqueezeBreakoutBot, BinanceClient, BitgetClient, Position) | ~1920 |
| `screener.py` | 币种扫描器 (均线粘合检测, 分型确认, 回踩判定) | ~850 |
| `web_ui.py` | Flask 用户端 Web UI (交易面板, 手动扫描, BTC监控) | ~2620 |
| `admin_server.py` | Flask 管理后台 (用户管理, 演示引擎控制, 持仓管理) | ~520 |
| `account_manager.py` | 多用户账户与许可系统 | ~320 |

## 记忆系统
| 文件 | 内容 |
|------|------|
| `memory/MEMORY.md` | 记忆索引 |
| `memory/entry-chain.md` | 12步开仓链路 |
| `memory/exit-chain.md` | 11步出场链路 + 止损去重 |

## 视觉与设计基调
- **风格**: 极其克制的 Linear 极简风格
- **主色调**: 钛金灰 (Titanium Gray) + 深邃黑 (#0B0D0F)
- **质感**: 机构级"重金打造"
  - 1px 极细发光边框 (box-shadow 微光晕)
  - 高频数据呼吸灯 (纯 CSS pulse 动画)
  - 全息网格背景 (Holographic Grid)
  - 战术级 HUD 仪表盘 (6格数据卡 + 持仓卡片 + 今日战报横排双栏)
  - 悬浮活体卡片 (3D transform 微动效)
- **动画规范**: 纯 CSS `transform` / `opacity` 实现 60fps，严禁使用触发 Reflow 的属性 (width, height, top, left 等)
- **移动端**: 三级响应式断点 (>768px / 480-768px / <480px), HUD从6列→3列→2列

## AI 编码绝对红线

### ❌ 绝对禁止
1. **除非明确要求，禁止修改底层 Python 逻辑和 API 路由**
2. **前端视效重构时，严禁破坏原有数据绑定逻辑** — ID 和 Class 结构需向后兼容
3. **禁止引入外部前端框架或 npm 依赖**
4. **禁止使用触发重排的 CSS 动画** (width/height/top/left/margin/padding 动画)
5. **禁止未经确认的删除操作** — 生产数据不可逆
6. **禁止在 onclick 中使用内联复杂 JS** — 必须抽成独立函数，避免转义问题

### ✅ 允许与鼓励
1. 前端视觉增强：纯 CSS 动效、布局优化、卡片质感提升
2. 代码复用：遵循已有模式 (命名、注释密度、代码风格)
3. 精简输出：直接给可运行的高级代码，不废话
4. 每次会话结束前，提醒用户更新 PROGRESS.md
5. UI 修改前先对齐 AXIOM 品牌调性 (极简克制、钛金质感、战术HUD)

## 架构关键约束

### 数据与配置
- **数据源分离**: 手动扫描/BTC监控 → 币安主网；自动交易 → 用户配置的交易所
- **配置存储**: 演示引擎→`demo_bot_config.json`; 用户→SQLite `user_configs`; 共享默认→`trade_config.json`
- **配置保存防护**: uid=0 保存前先加载已有文件合并; 空值API密钥不覆盖已有值
- **API热加载**: `save_user_config` 对 `_demo_bot`(uid=0) 和 `_bots[uid]`(uid>0) 分别更新

### 日志与时间
- **日志独立**: 每用户独立日志文件 `bot_{uid}.log`，`propagate=False`
- **席位**: `bj_now()` 北京时间显示, `_utcnow()` 内部计算

### 仓位计算 (详见 memory/exit-chain.md)
- **风险公式**: `max_safe_qty = min(risk_per_trade / sl_dist, max_position_usdt / entry_price)`
- **floor取整**: `math.floor(qty/step)*step`, 受交易所 maxQty/minQty 约束, minQty超标放弃
- **数量格式**: 始终 `str(quantity)`, 不要用 f-string 去零 — 不同交易所兼容性不一

### 止损全链路 (详见 memory/exit-chain.md)
- **SL结构外侧**: LONG→min(fractal_sl, band_sl); SHORT→max(fractal_sl, band_sl)
- **分型SL**: 信号自带 ×0.998(LONG)/1.002(SHORT) 或实时 `_find_fractal_sl`
- **均线边缘兜底**: 开仓当前确认K线六线真实上下轨 ×0.995(LONG)/1.005(SHORT)
- **保本**: PnL≥risk×breakeven_r → SL→入场价, cooldown=3
- **1R锁利**: 保本时PnL≥1R → SL直接锁50%利润; 追踪中持续阶梯锁利
- **冷却**: cooldown>0时跳过SL检查+TP_EMA检查, 每轮递减
- **追踪**: EMA20(默认) 或 ATR(`atr_trail_mult`×ATR) 持续推SL

### 止损单管理
- **去重**: `_active_stop_ids[symbol]=orderId`, 挂新前先取消旧, 成功才清ID
- **持久化**: 每次 `_save_positions` 附带 `active_stop_id`, 重启后 `_restore_stop_ids` 恢复
- **Bitget**: plan orders `reduceOnly="YES"`, `cancel-plan-order` 单个取消, `cancel-all-plan-orders` 不可用不要调用
- **补挂**: 启动后所有持仓统一补挂止损 (配合ID去重, 不重复)

### 风险计算
- **仓位**: `max_safe_qty = risk_per_trade / |entry - SL|` → `math.floor(qty/step)*step` → minQty超标放弃
- **永不超过**: 向下取整 + 超标直接return 0
- **日亏损**: `max_daily_loss > 0` 才限制, 0=不限

### 纸笔模式
- `mode="paper"` 时跳过API检查, 全链路 `if self.client is not None` 守卫
- 无API时 `close_position`/`close_all_positions` 直接清本地记录

### 池子系统
- 回踩无分型信号入池, 每2轮重扫6个最早入池的(FIFO, 0.5s间隔防429), 2h超时清除
- 重扫时全新拉K线 → 实时搜分型 → 实时算当前确认K线六线边缘 → 取结构外侧 → 始终用最新SL, 不用旧数据
- 限仓: `available_slots = max_positions - len(positions)`
- 去重: 分型形成后检查已有持仓, 跳过重复

## 部署与重启
```bash
systemctl restart macd-bot     # 交易面板 (web_ui.py, 端口5000)
systemctl restart macd-admin   # 管理后台 (admin_server.py, 端口5001)
```
快速备份: `cd <deploy-dir> && tar czf <backup-archive> *.py *.json *.jsonl *.db`
主动杀旧进程: `sed -i '/ExecStart/i ExecStartPre=-/bin/bash -c "fuser -k 5001/tcp 2>/dev/null; sleep 0.5"' /etc/systemd/system/macd-admin.service`
