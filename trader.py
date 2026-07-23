"""
均线粘合起爆点 — 自动量化交易引擎

策略规则:
  入场: screener 起爆点评分 > min_score(默认60), 方向 LONG/SHORT
  止损: 均线带边缘 (LONG=下轨, SHORT=上轨) — "场外止损"
  仓位: 固定风险金额 / (入场价 - 止损价) — 亏多少你说了算
  止盈: EMA20 动态追踪 — 收盘价反向突破 EMA20 即平仓
  保本: 浮盈 ≥ 初始风险(1:1盈亏比) → 移动止损至入场价
  风控: 最大持仓数 / 单日最大亏损 / 连续止损暂停

用法:
  python trader.py                      # 启动交易引擎(需先配置API)
  python trader.py --paper              # 模拟交易
  python trader.py --backtest SYMBOL    # 单币回测(开发中)
"""

import os, sys, time, json, hmac, hashlib, threading, logging, subprocess, shlex, re
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any

import requests, urllib.parse
import pandas as pd
import numpy as np

from btc_stage import classify_btc_stage, evaluate_btc_gate
from strategy_filters import evaluate_choppy_market_adaptive, evaluate_predicta_choppy_market
from predicta_indicator import (
    PredictaParams,
    compute_predicta,
    evaluate_predicta_setup,
    make_predicta_setup,
)

from screener import (
    fetch_klines, ema, calc_ma_band, verify_pool_signal, verify_pool_signal_details,
    scan_squeeze_breakout, fetch_pairs, EMA_LENS, MA_LENS, MIN_VOLUME,
    _find_fractal_sl_in_window, _find_fractal_structure_sequence, is_tradfi_or_junk,
    get_entry_float_limit, get_breakout_confirm_window, get_squeeze_max
)

# 盯防池超时: 按周期给予合理的K线等位时间
POOL_TIMEOUT = {
    "15m": 2 * 3600,    # 2h  = 8根K线
    "1h":  8 * 3600,    # 8h  = 8根K线
    "4h":  24 * 3600,   # 24h = 6根K线
    "1d":  72 * 3600,   # 72h = 3根K线
}

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("trader")
# 默认文件日志(web_ui请求等)
_default_file_handler = logging.FileHandler("bot.log", encoding="utf-8")
_default_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_default_file_handler)

def bj_now():
    # 北京时间显示 (UTC+8)
    return (datetime.now(timezone.utc) + timedelta(hours=8)).replace(microsecond=0)


def _entry_ms_for_market_data(entry_time: datetime, latest_open_ms: int) -> int:
    """Convert the project's Beijing display timestamp at the UTC market-data boundary."""
    raw_ms = int(entry_time.timestamp() * 1000)
    if raw_ms - int(latest_open_ms) >= 6 * 60 * 60 * 1000:
        return raw_ms - 8 * 60 * 60 * 1000
    return raw_ms

POSITIONS_PATH = "positions.json"
TRADE_LOG_PATH = "trades.jsonl"
SIGNAL_LOG_PATH = "signal_events.jsonl"
HERMES_CONFIRM_LOCK = threading.Lock()


# ============================================================
# Config — 交易参数 (可通过 Web UI 修改)
# ============================================================
@dataclass
class TradeConfig:
    # 交易所
    exchange: str = "bitget"            # binance / bitget
    market_type: str = "futures"        # spot / futures
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = False               # 是否使用测试网
    testnet_api_key: str = ""           # 测试网独立API Key
    testnet_api_secret: str = ""        # 测试网独立API Secret

    # Bitget
    bitget_api_key: str = ""
    bitget_api_secret: str = ""
    bitget_api_pass: str = ""

    # 策略
    scan_interval: str = "30m"          # 扫描周期: 30m / 1h / 4h / 1d
    min_score: float = 70.0             # 最低起爆点评分
    max_positions: int = 3              # 同时最大持仓数
    entry_signal_source: str = "rj_only"  # structure / rj_only / predicta_ewo
    rj_entry_filter: str = "off"         # off / log_only / soft / hard
    rj_cross_lookback_bars: int = 8      # RJ金叉/死叉有效窗口
    rj_min_jr_spread: float = 0.0        # J/R最小同向差值, 0=不额外要求
    rj_kdj_len: int = 9
    rj_kdj_len: int = 9
    rj_k_smooth: int = 3
    rj_d_smooth: int = 3
    rj_rsi_len: int = 9
    rj_rsi_smooth: int = 1
    rj_kd_ma_type: str = "sma"          # sma=对齐原版KD V8/Pine; rma=旧后端口径
    rj_slow_line_mode: str = "k"        # k / rsi / stoch_rsi / j_rsi
    rj_slow_line_scale: float = 0.88    # 紫线围绕50缩放, 对齐TradingView微调
    rj_slow_line_offset: float = 0.0
    rj_slow_line_clamp: bool = True
    rj_only_confirm_bars: int = 6        # RJ交叉后等待关键K突破的最大K数
    rj_only_max_symbols: int = 80        # RJ-only每轮最多扫描成交额前N个交易对
    rj_only_max_symbols: int = 500
    rj_only_scan_interval_sec: int = 1800
    rj_only_scan_workers: int = 4
    rj_only_scan_batch_size: int = 500
    rj_only_min_volume_usdt: float = 3_000_000.0
    rj_only_volume_filter: bool = True
    rj_only_volume_len: int = 20
    rj_only_volume_mult: float = 1.1
    rj_only_atr_sl_mult: float = 0.5     # 关键K止损外侧ATR缓冲
    rj_only_confirm_atr_buffer: float = 0.08  # 确认突破缓冲, 对齐TradingView
    rj_only_invalidate_on_opposite_break: bool = True  # 确认前反向破关键K则失效
    rj_only_min_stop_pct: float = 0.003  # 止损太近时按该比例兜底
    rj_only_max_stop_pct: float = 0.08   # 止损过宽则跳过
    rj_only_stats_enabled: bool = True   # RJ-only按历史胜率筛币
    rj_only_stats_lookback_bars: int = 1000
    rj_only_min_stop_pct: float = 0.003
    rj_only_max_stop_pct: float = 0.08
    rj_only_stats_enabled: bool = True
    rj_only_stats_lookback_bars: int = 1000
    rj_only_stats_horizon_bars: int = 12
    rj_only_stats_target_r: float = 1.0
    rj_only_stats_min_samples: int = 8
    rj_only_stats_min_win_rate: float = 52.0
    rj_only_stats_min_avg_r: float = 0.0
    rj_only_watchlist_enabled: bool = True      # 按RJ历史胜率筛出最契合代币并持久监控
    rj_only_watchlist_refresh_minutes: int = 60
    rj_only_watchlist_refresh_minutes: int = 60
    rj_only_watchlist_size: int = 80
    rj_only_watchlist_workers: int = 4
    rj_only_watchlist_eval_symbols: int = 80
    rj_only_watchlist_min_samples: int = 8
    rj_only_watchlist_min_win_rate: float = 52.0
    rj_only_watchlist_min_avg_r: float = 0.0
    rj_only_watchlist_discovery_top_n: int = 500
    rj_only_sr_filter: bool = True       # RJ-only要求靠近支撑/压力
    rj_only_sr_pivot_left: int = 5
    rj_only_sr_pivot_right: int = 3
    rj_only_sr_near_mode: str = "atr_or_pct"  # atr_or_pct / atr / pct
    rj_only_sr_near_atr_mult: float = 0.8
    rj_only_sr_near_pct: float = 0.8
    rj_only_require_divergence: bool = True
    rj_only_div_lookback_bars: int = 80
    rj_only_use_early_divergence: bool = True
    rj_only_setup_pool_enabled: bool = True     # RJ关键K候选池: 收盘形成候选, 盘中/临近收盘触发
    rj_only_setup_confirm_mode: str = "near_close"  # near_close / hold / touch
    rj_only_setup_close_confirm_sec: int = 120  # near_close: 当前K线收盘前N秒确认
    rj_only_setup_trigger_hold_sec: int = 10    # hold: 实时突破后维持秒数, 防盘中刺破
    rj_only_setup_check_interval_sec: int = 60  # 候选池检查间隔, 控制行情请求频率
    rj_only_setup_near_pct: float = 0.15        # 距离触发价多少%内写near日志
    rj_only_setup_max_pool: int = 40            # RJ候选池最大数量
    rj_choppy_filter_mode: str = "off"          # off / log_only / hard

    # Predicta V4 + EWO entry path (closed candles only)
    predicta_confirm_bars: int = 6
    predicta_choppy_filter_mode: str = "hard"
    predicta_ewo_fast: int = 5
    predicta_ewo_slow: int = 35
    predicta_confirm_atr_buffer: float = 0.08
    predicta_stop_atr_mult: float = 0.5
    predicta_min_stop_pct: float = 0.003
    predicta_max_stop_pct: float = 0.08
    predicta_max_symbols: int = 500
    predicta_min_volume_usdt: float = 3_000_000.0
    predicta_scan_workers: int = 4
    predicta_scan_interval_sec: int = 1800
    predicta_setup_max_pool: int = 40

    # 风控 — 仓位
    risk_per_trade: Any = 10.0          # 每笔风险 (USDT) 支持 "15m:10,1h:20,4h:40,1d:80"
    max_position_usdt: float = 1000.0   # 单笔安全上限(防止极紧粘合时仓位膨胀)
    leverage: int = 3                   # 杠杆(期货)

    # 风控 — 止损/止盈
    atr_mult_sl: float = 0.5           # SL额外偏移(ATR倍数), 0=纯均线边缘
    # 三阶止盈参数
    half_risk_trigger_r: float = 0.0   # >0启用: 达阈值后把初始1R风险收窄到0.5R
    enable_early_protect: bool = True  # 提前保护: 未到1.2R前先保本
    early_protect_r: float = 0.8       # 提前保护触发R
    early_protect_lock_r: float = 0.0  # 提前保护锁定R, 0=SL推到入场价
    tier1_defense_r: float = 1.2       # 第一阶: R倍数触发防守 (SL→入场价+0.2R)
    tier2_partial_r: float = 2.0       # 第二阶: R倍数触发减仓50%
    use_atr_trail: bool = False        # 第三阶模式: False=EMA棘轮 / True=ATR吊灯
    ema_ratchet: int = 20              # 第三阶EMA周期 (EMA棘轮模式)
    atr_trail_mult: float = 3.5        # 第三阶ATR倍数 (ATR吊灯模式)
    atr_trail_period: int = 14         # 第三阶ATR回溯周期
    enable_time_stop: bool = True      # 未起爆超时退出开关
    time_stop_bars: Any = "15m:6,30m:6,1h:5,4h:4,1d:3"  # 各周期入场后允许横盘K数
    time_stop_min_r: float = 0.6       # 超时前最大顺势推进低于该R才退出
    rj_time_stop_extend_bars: int = 6  # RJ-only到达基础超时后额外观察K数
    rj_time_stop_hard_loss_r: float = -0.8  # RJ-only基础超时时当前R低于该值直接退出
    rj_time_stop_final_min_r: float = 0.3   # RJ-only延长观察结束时仍低于该R才退出
    # 兼容旧字段
    ema_trail: int = 20
    rj_only_setup_max_chase_pct: float = 2.0
    hermes_confirm_enabled: bool = False
    hermes_confirm_mode: str = "log_only"      # log_only / hard_filter
    hermes_confirm_min_confidence: float = 65.0
    hermes_confirm_timeout_sec: int = 240
    hermes_confirm_fail_open: bool = False
    hermes_confirm_cache_ttl_sec: int = 1800
    hermes_confirm_queue_wait_sec: int = 300
    hermes_confirm_cmd: str = "hermes"
    hermes_confirm_skills: str = "kline-indicator"
    btc_direction_filter_enabled: bool = True
    breakeven_r: float = 0.8
    use_atr_trail: bool = False
    lock_profit_pct: float = 0.5

    # 风控 — 全局
    max_daily_loss: float = 30.0        # 单日最大亏损(暂停交易)
    max_consecutive_loss: int = 3       # 连续止损后暂停
    cooldown_minutes: int = 90          # 止损后冷却时间
    account_initial_equity: float = 0.0 # 前端账本校准本金; >0 时按交易所权益差计算账户净盈

    # 燃料费系统
    fuel_enabled: bool = False          # 是否启用燃料费
    fuel_balance: float = 0.0           # 燃料余额
    commission_rate: float = 10.0       # 盈利分成比例(%)

    # 通知
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # Web安全
    web_password: str = ""             # Web UI交易面板密码(留空=不验证)

    # 状态
    mode: str = "paper"                 # paper / live
    enabled: bool = False               # 是否启用自动交易
    is_lead_trader: bool = False        # Bitget 合约带单模式

    def save(self, path="trade_config.json"):
        d = {k: v for k, v in self.__dict__.items()}
        with open(path, "w") as f:
            json.dump(d, f, indent=2, default=str)

    @classmethod
    def load(cls, path="trade_config.json"):
        if not Path(path).exists():
            return cls()
        with open(path) as f:
            d = json.load(f)
        cfg = cls()
        for k, v in d.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg


# ============================================================
# Binance REST Client (现货 + 合约)
# ============================================================
class BinanceClient:
    """币安 REST API 封装"""
    def __init__(self, api_key="", api_secret="", testnet=False, market_type="spot"):
        self._log = log
        self.key = api_key
        self.secret = api_secret
        self.testnet = testnet
        self.market_type = market_type  # spot / futures
        self._exchange_info_cache = None
        self._symbol_info_cache = {}
        self._trading_symbols_cache = None

        if market_type == "futures":
            self.rest = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        else:
            self.rest = "https://testnet.binance.vision" if testnet else "https://api.binance.com"

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(sorted(params.items()))
        params["signature"] = hmac.new(
            self.secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        return params

    def _req(self, method, path, params=None, signed=False):
        url = f"{self.rest}{path}"
        headers = {"X-MBX-APIKEY": self.key} if self.key else {}
        if signed and self.secret:
            params = self._sign(params or {})
            # 提取签名, 拼参数时不含签名, 最后追加以确保签名在末尾
            signature = params.pop("signature", "")
            query = urllib.parse.urlencode(sorted(params.items()))
            url = f"{url}?{query}&signature={signature}"
            params = None  # 已经拼到URL上了
        try:
            if method == "GET":
                r = requests.get(url, params=params, headers=headers, timeout=15)
            elif method == "DELETE":
                r = requests.delete(url, params=params, headers=headers, timeout=15)
            else:
                r = requests.post(url, params=params, headers=headers, timeout=15)
            if r.status_code != 200:
                self._log.error(f"API error {r.status_code}: {r.text[:200]}")
                return None
            return r.json()
        except Exception as e:
            self._log.error(f"Request error: {e}")
            return None

    def _exchange_info_path(self):
        return "/fapi/v1/exchangeInfo" if self.market_type == "futures" else "/api/v3/exchangeInfo"

    def get_exchange_info(self):
        if self._exchange_info_cache is not None:
            return self._exchange_info_cache
        data = self._req("GET", self._exchange_info_path())
        if data:
            self._exchange_info_cache = data
        return data

    def get_trading_symbols(self) -> set:
        if self._trading_symbols_cache is not None:
            return set(self._trading_symbols_cache)
        data = self.get_exchange_info()
        symbols = set()
        if data:
            for item in data.get("symbols", []):
                try:
                    if item.get("status") != "TRADING":
                        continue
                    symbol = str(item.get("symbol", "") or "")
                    quote = str(item.get("quoteAsset", "") or item.get("quoteAssetName", "") or "")
                    if symbol.endswith("USDT") and (not quote or quote == "USDT"):
                        symbols.add(symbol)
                except Exception:
                    continue
        self._trading_symbols_cache = symbols
        return set(symbols)

    # ---- 账户 ----
    def get_balance(self, asset="USDT"):
        if self.market_type == "futures":
            account = self._req("GET", "/fapi/v2/account", signed=True)
            if account:
                try:
                    wallet = float(account.get("totalWalletBalance", 0) or 0)
                    unrealized = float(account.get("totalUnrealizedProfit", 0) or 0)
                    if wallet or unrealized:
                        return wallet + unrealized
                except Exception:
                    pass
            data = self._req("GET", "/fapi/v2/balance", signed=True)
            if not data: return 0
            for b in data:
                if b["asset"] == asset:
                    return float(b.get("balance", 0) or 0) + float(b.get("crossUnPnl", 0) or 0)
        else:
            data = self._req("GET", "/api/v3/account", signed=True)
            if not data: return 0
            for b in data.get("balances", []):
                if b["asset"] == asset:
                    return float(b["free"])
        return 0

    def get_positions(self):
        """获取期货持仓 (含未实现盈亏)"""
        if self.market_type != "futures":
            return []
        data = self._req("GET", "/fapi/v2/positionRisk", signed=True)
        if not data: return []
        result = []
        for p in data:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                result.append({
                    "symbol": p["symbol"],
                    "positionAmt": amt,
                    "entryPrice": float(p.get("entryPrice", 0)),
                    "markPrice": float(p.get("markPrice", 0)),
                    "unRealizedProfit": float(p.get("unRealizedProfit", 0)),
                })
        return result

    def get_spot_balances(self):
        """获取现货所有非零余额 {symbol: {free, locked, total_usdt}}"""
        if self.market_type != "spot":
            return {}
        data = self._req("GET", "/api/v3/account", signed=True)
        if not data:
            return {}
        balances = {}
        for b in data.get("balances", []):
            free = float(b["free"])
            locked = float(b["locked"])
            total = free + locked
            if total > 0:
                balances[b["asset"]] = {"free": free, "locked": locked, "total": total}
        return balances

    # ---- 订单 ----
    def market_order(self, symbol, side, quantity, reduce_only=False, tracking_no=""):
        """市价单"""
        if self.market_type == "futures":
            path = "/fapi/v1/order"
            params = {
                "symbol": symbol, "side": side.upper(),
                "type": "MARKET", "quantity": str(quantity)
            }
            if reduce_only:
                params["reduceOnly"] = "true"
        else:
            path = "/api/v3/order"
            params = {
                "symbol": symbol, "side": side.upper(),
                "type": "MARKET"
            }
            # Spot: quantity or quoteOrderQty
            params["quoteOrderQty"] = str(round(quantity, 2))

        result = self._req("POST", path, params, signed=True)
        if result is None:
            self._log.error(f"下单API返回None({symbol} {side} qty={quantity} path={path} params={params})")
        elif "orderId" not in str(result):
            self._log.error(f"下单无orderId({symbol} {side} qty={quantity}): {result}")
        return result

    def stop_order(self, symbol, side, stop_price, quantity, reduce_only=True, tracking_no=""):
        """止损单: 挂交易所, 失败则依赖bot内部10秒检查兜底"""
        if self.market_type == "futures":
            # 测试网不支持任何止损单端点, 直接走内部兜底
            if self.testnet:
                self._log.info(f"[测试网] 跳过交易所止损单, bot内部监控 SL={stop_price:.4f}")
                return None

            # 实盘: 先尝试 Algo 端点
            params = {
                "symbol": symbol,
                "side": side.upper(),
                "type": "STOP_MARKET",
                "stopPrice": str(round(stop_price, 6)),
                "quantity": str(quantity),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            }
            result = self._req("POST", "/fapi/v1/algo/order", params, signed=True)
            if result and isinstance(result, dict) and "code" in result:
                self._log.error(f"止损单失败({symbol}): {result.get('msg','')}")
                return None
            if result:
                self._log.info(f"止损单已挂: {symbol} {side} @{stop_price:.4f}")
            else:
                self._log.warning(f"止损单挂单失败({symbol}), bot内部检查兜底")
            return result
        else:
            params = {
                "symbol": symbol,
                "side": side.upper(),
                "type": "STOP_LOSS_LIMIT",
                "stopPrice": str(round(stop_price, 6)),
                "price": str(round(stop_price * 0.995, 6)),
                "quantity": str(quantity),
            }
            result = self._req("POST", "/api/v3/order", params, signed=True)
            if result:
                self._log.info(f"止损单已挂: {symbol} {side} @{stop_price:.4f}")
            return result

    def set_leverage(self, symbol, leverage):
        """设置合约杠杆"""
        if self.market_type != "futures":
            return True  # 现货不需要设杠杆
        return self._req("POST", "/fapi/v1/leverage", {
            "symbol": symbol, "leverage": leverage
        }, signed=True)

    def set_compatible_leverage(self, symbol, leverage):
        """设置请求杠杆；不支持时逐级降到更低的安全档位。"""
        requested = max(1, int(float(leverage or 1)))
        candidates = []
        for candidate in (requested, 20, 10, 5, 3, 2, 1):
            candidate = min(requested, candidate)
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            result = self.set_leverage(symbol, candidate)
            if result:
                if candidate != requested:
                    self._log.info(
                        f"Binance杠杆按合约兼容降档: {symbol} {requested}x -> {candidate}x"
                    )
                return result
        return None

    def cancel_all_orders(self, symbol):
        if self.market_type == "futures":
            r1 = self._req("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)
            if not self.testnet:
                self._req("DELETE", "/fapi/v1/algo/openOrders", {"symbol": symbol}, signed=True)
            return r1
        else:
            return self._req("DELETE", "/api/v3/openOrders", {"symbol": symbol}, signed=True)

    # ---- 行情 ----
    def get_price(self, symbol):
        if self.market_type == "futures":
            data = self._req("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        else:
            data = self._req("GET", "/api/v3/ticker/price", {"symbol": symbol})
        if data and "price" in data:
            return float(data["price"])
        return None

    def get_symbol_info(self, symbol):
        """获取交易对精度信息"""
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]
        data = self.get_exchange_info()
        if not data:
            return None
        for s in data.get("symbols", []):
            if s.get("symbol") == symbol:
                self._symbol_info_cache[symbol] = s
                return s
        return None


# ============================================================
# Bitget REST Client
# ============================================================
class BitgetClient:
    """Bitget REST API 封装 (支持带单模式)"""
    def __init__(self, api_key="", api_secret="", api_pass="", market_type="futures", is_lead_trader=False):
        self._log = log
        self.key = api_key; self.secret = api_secret; self.passphrase = api_pass
        self.market_type = market_type
        self.is_lead_trader = is_lead_trader
        self.rest = "https://api.bitget.com"

    def _sign(self, method, path, query_str="", body=""):
        ts = str(int(time.time() * 1000))
        # Bitget: timestamp + METHOD + path + ?query + body (无body则不拼)
        prehash = ts + method.upper() + path
        if query_str: prehash += "?" + query_str
        if body: prehash += body
        raw = hmac.new(self.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        sig = __import__('base64').b64encode(raw).decode()
        return {"ACCESS-KEY": self.key, "ACCESS-SIGN": sig, "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self.passphrase, "Content-Type": "application/json"}

    def _req(self, method, path, params=None):
        url = f"{self.rest}{path}"
        headers = {}
        qs = ""
        body = ""
        if params:
            qs = urllib.parse.urlencode(sorted(params.items(), key=lambda x: x[0]))
            if method == "POST":
                body = _json.dumps(params)
        if self.key:
            headers = self._sign(method, path, qs, body)
        try:
            if method == "GET":
                r = requests.get(url + ("?"+qs if qs else ""), headers=headers, timeout=15)
            else:
                r = requests.post(url + ("?"+qs if qs else ""), data=body, headers=headers, timeout=15)
            if r.status_code != 200:
                self._log.error(f"Bitget API error {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
            if data.get("code") != "00000":
                self._log.error(f"Bitget API error code={data.get('code')} msg={data.get('msg','')}")
                return None
            return data.get("data")
        except: return None

    def get_klines(self, symbol, interval, limit=200):
        """获取K线, 返回DataFrame"""
        granularity = {"1m":"1min","5m":"5min","15m":"15min","30m":"30min",
                       "1h":"1H","4h":"4H","1d":"1D","1w":"1W"}.get(interval,"1H")
        if self.market_type == "futures":
            sym = symbol  # Bitget V2 直接用 USDT
            path = "/api/v2/mix/market/candles"
            params = {"symbol": sym, "granularity": granularity, "limit": str(limit), "productType": "USDT-FUTURES"}
        else:
            path = "/api/v2/spot/market/candles"
            params = {"symbol": symbol, "granularity": granularity, "limit": str(limit)}
        data = self._req("GET", path, params)
        if not data: return None
        import pandas as pd, numpy as np
        rows = [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                  "c": float(r[4]), "v": float(r[5])} for r in data if len(r) >= 6]
        if not rows: return None
        df = pd.DataFrame(rows); df["t"] = pd.to_datetime(df["t"], unit="ms")
        return df

    def get_balance(self, asset="USDT"):
        if self.market_type == "futures":
            data = self._req("GET", "/api/v2/mix/account/account", {"symbol": "BTCUSDT", "marginCoin": "USDT", "productType": "USDT-FUTURES"})
            if data: return float(data.get("usdtEquity", 0))
        else:
            data = self._req("GET", "/api/v2/spot/account/assets")
            if data:
                for b in data:
                    if b.get("coinName") == asset: return float(b.get("available", 0))
        return 0

    def get_positions(self):
        if self.market_type != "futures": return []
        data = self._req("GET", "/api/v2/mix/position/all-position", {"symbol": "BTCUSDT", "marginCoin": "USDT", "productType": "USDT-FUTURES"})
        if not data: return []
        result = []
        for p in data:
            total = float(p.get("total", 0))
            if total == 0: continue
            result.append({"symbol": p["symbol"].replace("USDT_UMCBL","USDT"),
                "positionAmt": total if p.get("holdSide")=="long" else -total,
                "entryPrice": float(p.get("openPriceAvg", 0)),
                "markPrice": float(p.get("markPrice", 0)),
                "unRealizedProfit": float(p.get("unrealizedPL", 0)),
                "openTime": p.get("cTime", "")})
        return result

    def market_order(self, symbol, side, quantity, reduce_only=False, tracking_no=""):
        try:
            if self.market_type == "futures":
                if side == "BUY":
                    order_side = "buy"
                    pos_side = "short" if reduce_only else "long"
                else:
                    order_side = "sell"
                    pos_side = "long" if reduce_only else "short"

                if self.is_lead_trader:
                    # 带单模式
                    if reduce_only and tracking_no:
                        # 平仓: 用带单专属平仓接口
                        path = "/api/v2/copy/mix-trader/order/close-order"
                        params = {"symbol": symbol, "trackingNo": tracking_no}
                        result = self._req("POST", path, params)
                        if result and isinstance(result, dict) and result.get("code") == "00000":
                            self._log.info(f"[带单平仓] {symbol} trackingNo={tracking_no}")
                        else:
                            err = result.get("msg","") if isinstance(result, dict) else str(result)[:100]
                            self._log.error(f"[带单平仓失败] {symbol}: {err}")
                        return result
                    else:
                        # 开仓: 用带单专属开仓接口
                        path = "/api/v2/copy/mix-trader/order/place-order"
                        params = {"symbol": symbol, "productType": "USDT-FUTURES",
                                  "marginMode": "isolated", "marginCoin": "USDT",
                                  "size": str(quantity), "side": order_side,
                                  "posSide": pos_side, "orderType": "market"}
                        result = self._req("POST", path, params)
                        if result and isinstance(result, dict) and result.get("code") == "00000":
                            tn = result.get("data", {}).get("trackingNo", "") if isinstance(result.get("data"), dict) else ""
                            if tn:
                                self._log.info(f"[带单开仓] {symbol} trackingNo={tn}")
                                result["trackingNo"] = tn
                        else:
                            err = result.get("msg","") if isinstance(result, dict) else str(result)[:100]
                            self._log.error(f"[带单开仓失败] {symbol}: {err}")
                        return result
                else:
                    # 普通模式
                    path = "/api/v2/mix/order/place-order"
                    params = {"symbol": symbol, "productType": "USDT-FUTURES", "marginMode": "isolated",
                              "marginCoin": "USDT", "size": str(quantity), "side": order_side,
                              "posSide": pos_side, "orderType": "market",
                              "reduceOnly": "YES" if reduce_only else "NO"}
            else:
                path = "/api/v2/spot/trade/place-order"
                params = {"symbol": symbol, "side": side.lower(), "orderType": "market",
                          "force": "normal", "size": str(quantity)}
            return self._req("POST", path, params)
        except Exception as e:
            self._log.error(f"[market_order异常] {symbol}: {e}")
            return None

    @staticmethod
    def _num(v):
        try:
            if v is None or v == "":
                return None
            return float(v)
        except Exception:
            return None

    @staticmethod
    def _first_key(obj, keys):
        if obj is None:
            return None
        key_set = {str(k) for k in keys}
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k) in key_set and v not in (None, ""):
                    return v
            for v in obj.values():
                found = BitgetClient._first_key(v, key_set)
                if found not in (None, ""):
                    return found
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                found = BitgetClient._first_key(item, key_set)
                if found not in (None, ""):
                    return found
        return None

    def get_order_detail(self, symbol, order_id="", client_oid=""):
        if self.market_type != "futures":
            return None
        params = {"symbol": symbol, "productType": "USDT-FUTURES"}
        if order_id:
            params["orderId"] = str(order_id)
        if client_oid:
            params["clientOid"] = str(client_oid)
        if "orderId" not in params and "clientOid" not in params:
            return None
        return self._req("GET", "/api/v2/mix/order/detail", params)

    def get_order_fills(self, symbol="", order_id="", start_time=None, end_time=None, limit=100, history=False):
        if self.market_type != "futures":
            return None
        path = "/api/v2/mix/order/fill-history" if history else "/api/v2/mix/order/fills"
        params = {"productType": "USDT-FUTURES", "limit": str(min(max(int(limit or 100), 1), 100))}
        if symbol:
            params["symbol"] = symbol
        if order_id:
            params["orderId"] = str(order_id)
        if start_time:
            params["startTime"] = str(int(start_time))
        if end_time:
            params["endTime"] = str(int(end_time))
        return self._req("GET", path, params)

    def get_position_history(self, symbol="", start_time=None, end_time=None, limit=20):
        if self.market_type != "futures":
            return None
        params = {"productType": "USDT-FUTURES", "limit": str(min(max(int(limit or 20), 1), 100))}
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = str(int(start_time))
        if end_time:
            params["endTime"] = str(int(end_time))
        return self._req("GET", "/api/v2/mix/position/history-position", params)

    def get_pending_plan_orders(self, symbol="", plan_type="normal_plan"):
        if self.market_type != "futures":
            return []
        params = {
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "planType": plan_type,
        }
        if symbol:
            params["symbol"] = symbol
        data = self._req("GET", "/api/v2/mix/order/orders-plan-pending", params)
        if isinstance(data, dict):
            return data.get("entrustedList") or []
        if isinstance(data, list):
            return data
        return []

    def resolve_close_trade(self, symbol, order_response=None, direction="", quantity=0.0):
        """平仓后反查 Bitget 成交/历史仓位, 获取真实已实现PnL。"""
        if self.market_type != "futures":
            return {}
        now_ms = int(time.time() * 1000)
        out = {}
        order_id = self._first_key(order_response, ("orderId", "order_id"))
        client_oid = self._first_key(order_response, ("clientOid", "client_oid"))

        if order_id or client_oid:
            time.sleep(0.25)
            detail = self.get_order_detail(symbol, order_id=order_id or "", client_oid=client_oid or "")
            if isinstance(detail, dict):
                out["order_detail"] = detail
                avg = self._num(detail.get("priceAvg") or detail.get("avgPrice"))
                if avg:
                    out["exit_price"] = avg
                profit = self._num(detail.get("totalProfits"))
                if profit is not None:
                    out["exchange_pnl"] = profit
                    out["pnl_source"] = "bitget_order_detail"
                fee = self._num(detail.get("fee"))
                if fee is not None:
                    out["exchange_fee"] = fee

            fills_data = self.get_order_fills(symbol=symbol, order_id=order_id or "", limit=100)
            if not fills_data:
                fills_data = self.get_order_fills(
                    symbol=symbol, order_id=order_id or "",
                    start_time=now_ms - 7 * 24 * 3600 * 1000,
                    end_time=now_ms + 60 * 1000,
                    limit=100, history=True,
                )
            fills = []
            if isinstance(fills_data, dict):
                fills = fills_data.get("fillList") or fills_data.get("list") or []
            elif isinstance(fills_data, list):
                fills = fills_data
            if fills:
                profit_sum = 0.0
                has_profit = False
                fee_sum = 0.0
                px_qty = 0.0
                qty_sum = 0.0
                for f in fills:
                    profit = self._num(f.get("profit"))
                    if profit is not None:
                        profit_sum += profit
                        has_profit = True
                    price = self._num(f.get("price"))
                    qty = self._num(f.get("baseVolume") or f.get("size"))
                    if price and qty:
                        px_qty += price * qty
                        qty_sum += qty
                    for fee in f.get("feeDetail", []) or []:
                        fee_val = self._num(fee.get("totalFee"))
                        if fee_val is not None:
                            fee_sum += fee_val
                if qty_sum > 0:
                    out["exit_price"] = px_qty / qty_sum
                    out["filled_qty"] = qty_sum
                if has_profit:
                    out["exchange_pnl"] = profit_sum
                    out["pnl_source"] = "bitget_order_fills"
                out["exchange_fee"] = fee_sum

        # 计划止损/交易所侧清仓有时拿不到平仓订单号, 用历史仓位兜底。
        if "exchange_pnl" not in out:
            hist = self.get_position_history(
                symbol=symbol,
                start_time=now_ms - 24 * 3600 * 1000,
                end_time=now_ms + 60 * 1000,
                limit=20,
            )
            items = hist.get("list", []) if isinstance(hist, dict) else (hist or [])
            want_side = "long" if str(direction).upper() == "LONG" else "short"
            candidates = []
            for item in items:
                if str(item.get("symbol", "")).upper() != str(symbol).upper():
                    continue
                if str(item.get("holdSide", "")).lower() not in ("", want_side):
                    continue
                if self._num(item.get("closeTotalPos")) in (None, 0):
                    continue
                candidates.append(item)
            candidates.sort(key=lambda x: int(float(x.get("utime") or x.get("uTime") or x.get("ctime") or x.get("cTime") or 0)), reverse=True)
            if candidates:
                item = candidates[0]
                pnl = self._num(item.get("pnl"))
                net = self._num(item.get("netProfit"))
                close_avg = self._num(item.get("closeAvgPrice"))
                if close_avg:
                    out["exit_price"] = close_avg
                if pnl is not None:
                    out["exchange_pnl"] = pnl
                    out["pnl_source"] = "bitget_position_history"
                if net is not None:
                    out["exchange_net_profit"] = net
                open_fee = self._num(item.get("openFee")) or 0.0
                close_fee = self._num(item.get("closeFee")) or 0.0
                funding = self._num(item.get("totalFunding")) or 0.0
                out["exchange_fee"] = open_fee + close_fee
                out["exchange_funding"] = funding
                out["position_history"] = item
        return out

    def stop_order(self, symbol, side, stop_price, quantity, reduce_only=True, tracking_no=""):
        """Bitget 止损单 (带单模式使用 modify-tpsl, 普通模式使用 plan-order)"""
        if self.market_type != "futures":
            return None
        try:
            # 带单模式: 使用 modify-tpsl 接口
            if self.is_lead_trader and tracking_no:
                path = "/api/v2/copy/mix-trader/order/modify-tpsl"
                params = {
                    "symbol": symbol,
                    "trackingNo": tracking_no,
                    "stopLossPrice": str(round(stop_price, 8)),
                }
                result = self._req("POST", path, params)
                if result and isinstance(result, dict) and result.get("code") == "00000":
                    self._log.info(f"[带单止损] {symbol} SL={stop_price:.4f} trackingNo={tracking_no}")
                else:
                    err = result.get("msg","") if isinstance(result, dict) else str(result)[:100]
                    self._log.error(f"[带单止损失败] {symbol}: {err}")
                return result

            # 普通模式: plan-order
            if side == "SELL":
                order_side = "sell"
                pos_side = "long"
            else:
                order_side = "buy"
                pos_side = "short"
            if not hasattr(self, '_active_stop_ids'): self._active_stop_ids = {}
            old_oid = self._active_stop_ids.get(symbol)
            if old_oid:
                try:
                    r = self._req("POST", "/api/v2/mix/order/cancel-plan-order", {
                        "symbol": symbol, "productType": "USDT-FUTURES",
                        "marginCoin": "USDT", "orderId": old_oid
                    })
                    if r is not None and isinstance(r, dict) and r.get("code") == "00000":
                        self._active_stop_ids.pop(symbol, None)
                except: pass
            path = "/api/v2/mix/order/place-plan-order"
            sym_info = self.get_symbol_info(symbol)
            if sym_info and "pricePlace" in sym_info:
                pp = sym_info["pricePlace"]
                trig = f"{round(stop_price, pp):.{pp}f}"
            elif stop_price >= 1000:
                trig = f"{round(stop_price, 1):.1f}"
            elif stop_price >= 1:
                trig = f"{round(stop_price, 2):.2f}"
            else:
                trig = f"{round(stop_price, 4):.4f}"
            params = {
                "symbol": symbol, "productType": "USDT-FUTURES",
                "marginMode": "isolated", "marginCoin": "USDT",
                "size": str(round(quantity, 8)),
                "side": order_side, "posSide": pos_side,
                "orderType": "market",
                "triggerPrice": trig,
                "triggerType": "fill_price",
                "reduceOnly": "YES" if reduce_only else "NO",
                "planType": "normal_plan"
            }
            result = self._req("POST", path, params)
            if result and isinstance(result, dict) and result.get("orderId"):
                self._active_stop_ids[symbol] = result["orderId"]
                self._log.info(f"Bitget 止损单已挂: {symbol} {side} @{stop_price:.4f} id={result['orderId']}")
            else:
                err_code = result.get('code','?') if isinstance(result, dict) else 'N/A'
                err_msg = result.get('msg','') if isinstance(result, dict) else str(result)[:100]
                self._log.warning(f"Bitget 止损单失败({symbol} {side} @{stop_price} qty={quantity}): code={err_code} msg={err_msg}")
            return result
        except Exception as e:
            self._log.error(f"[stop_order异常] {symbol}: {e}")
            return None

    def cancel_all_orders(self, symbol):
        """撤销指定symbol所有挂单 (取消成功才清除ID, 失败则保留供下次重试)"""
        if self.market_type != "futures":
            return None
        if not hasattr(self, '_active_stop_ids'): self._active_stop_ids = {}
        known_oid = self._active_stop_ids.get(symbol)
        if known_oid:
            try:
                r = self._req("POST", "/api/v2/mix/order/cancel-plan-order", {
                    "symbol": symbol, "productType": "USDT-FUTURES",
                    "marginCoin": "USDT", "orderId": known_oid
                })
                if r is not None and isinstance(r, dict) and r.get("code") == "00000":
                    self._active_stop_ids.pop(symbol, None)  # 取消成功才清除
                # 如果返回非成功码, 保留ID供 stop_order 内部再试
            except: pass  # 网络异常也保留ID

    def cleanup_all_stops(self, symbols: list):
        """批量清理止损单 — 逐个symbol尝试取消所有计划+普通挂单"""
        cleaned = 0
        for sym in symbols:
            # 尝试批量取消 (部分Bitget版本支持)
            for ep in ["/api/v2/mix/order/cancel-all-plan-orders", "/api/v2/mix/order/cancel-all-orders"]:
                try:
                    r = self._req("POST", ep, {"symbol": sym, "productType": "USDT-FUTURES", "marginCoin": "USDT"})
                    if r is not None:
                        cleaned += 1
                except: pass
            # 清掉已知ID
            oid = self._active_stop_ids.pop(sym, None) if hasattr(self, '_active_stop_ids') else None
            if oid:
                try:
                    self._req("POST", "/api/v2/mix/order/cancel-plan-order", {"symbol": sym, "productType": "USDT-FUTURES", "marginCoin": "USDT", "orderId": oid})
                except: pass
        return cleaned

    def set_leverage(self, symbol, leverage):
        """Bitget 设置杠杆"""
        if self.market_type != "futures":
            return True
        lev = int(float(leverage or 1))
        try:
            info = self.get_symbol_info(symbol) or {}
            max_lev = int(float(info.get("maxLever") or lev))
            min_lev = int(float(info.get("minLever") or 1))
            safe_lev = max(min_lev, min(lev, max_lev))
            if safe_lev != lev:
                self._log.info(f"Bitget杠杆按合约上限降档: {symbol} {lev}x -> {safe_lev}x")
            lev = safe_lev
        except Exception as e:
            self._log.warning(f"Bitget杠杆上限读取失败, 使用配置杠杆: {symbol} {leverage}x ({e})")
        return self._req("POST", "/api/v2/mix/account/set-leverage", {
            "symbol": symbol, "productType": "USDT-FUTURES",
            "marginCoin": "USDT", "leverage": str(lev)
        })
    def get_price(self, symbol):
        data = self._req("GET", "/api/v2/spot/market/tickers", {"symbol": symbol})
        if data and len(data) > 0: return float(data[0].get("lastPr", 0))
        return None
    def get_symbol_info(self, symbol):
        """从Bitget合约信息获取精度, 带缓存"""
        if not hasattr(self, '_sym_cache'):
            self._sym_cache = {}
        if symbol not in self._sym_cache:
            try:
                data = self._req("GET", "/api/v2/mix/market/contracts", {"symbol": symbol, "productType": "USDT-FUTURES"})
                if data and len(data) > 0:
                    item = data[0]
                    pp = int(item.get("pricePlace", "0"))
                    vp = int(item.get("volumePlace", "0"))
                    self._sym_cache[symbol] = {
                        "pricePlace": pp,
                        "volumePlace": vp,
                        "minTradeNum": item.get("minTradeNum", "0.01"),
                        "minLever": item.get("minLever"),
                        "maxLever": item.get("maxLever"),
                        "filters": [
                            {"filterType": "LOT_SIZE", "stepSize": str(10**-vp), "minQty": item.get("minTradeNum", "0.01")}
                        ]
                    }
                else:
                    self._sym_cache[symbol] = None
            except:
                self._sym_cache[symbol] = None
        return self._sym_cache.get(symbol)

    def get_mark_price(self, symbol): return None


import json as _json

# ============================================================
# Position Tracker — 持仓管理
# ============================================================
@dataclass
class Position:
    symbol: str
    direction: str          # LONG / SHORT
    entry_price: float
    entry_time: datetime
    quantity: float
    sl_price: float         # 初始止损价 (1R锚点, 永不改变)
    current_sl: float       # 当前止损(动态追踪)
    risk_usdt: float        # 初始风险金额
    signal_score: float     # 入场时评分
    initial_band_hi: float  # 入场时均线上轨
    initial_band_lo: float  # 入场时均线下轨
    half_risk_protected: bool = False
    breakeven_triggered: bool = False
    breakeven_cooldown: int = 0  # 保本后冷却计数, 防秒碰止损
    partial_tp_triggered: bool = False  # 2.0R减仓50%已执行
    initial_sl: float = 0.0             # 开仓时记录的初始止损锚点
    highest_price: float = 0.0          # ATR吊灯: 持仓期间最高价(LONG)
    lowest_price: float = 999999.0      # ATR吊灯: 持仓期间最低价(SHORT)
    pnl: float = 0.0
    exit_reason: str = ""
    exit_time: Optional[datetime] = None
    tracking_no: str = ""  # Bitget 带单订单追踪号
    source_interval: str = "15m"  # 信号触发周期
    source_strategy: str = ""  # structure / rj_only, 用于策略差异化出场
    max_favorable_r: float = 0.0  # 入场后最大顺势推进R倍数, 用于未起爆超时退出
    max_adverse_r: float = 0.0    # 入场后最大逆势推进R倍数(MAE), 用于参数复盘
    time_stop_armed: bool = True  # 兼容旧持仓文件; 实际执行统一由 enable_time_stop 控制
    time_stop_armed_at: Optional[datetime] = None  # 兼容旧持仓文件; 超时计数统一使用 entry_time
    time_stop_watch: bool = False  # RJ-only基础超时后进入观察态
    time_stop_watch_started_at: Optional[datetime] = None
    time_stop_first_seen_bars: int = 0
    time_stop_first_seen_r: float = 0.0
    time_stop_first_seen_mfe: float = 0.0
    btc_regime_fields: dict = field(default_factory=dict)
    signal_key: str = ""  # 本次开仓结构指纹, 防止同一突破/回归/分型被重复复用
    target_zone_type: str = ""          # 前方最近目标区类型: swing/squeeze/none
    target_zone_price: float = 0.0      # 前方最近目标价/目标区第一触达边缘
    target_zone_low: float = 0.0        # 目标区下沿
    target_zone_high: float = 0.0       # 目标区上沿
    target_r: float = 0.0               # 入场到目标区的理论R倍数
    target_distance_pct: float = 0.0    # 入场到目标区距离百分比
    target_zone_bars_ago: int = 0       # 目标区距离当前多少根K线
    hermes_confirm: dict = field(default_factory=dict)
    excursion_price_source: str = ""
    choppy_filter: dict = field(default_factory=dict)  # 入场时震荡过滤快照, 禁止持仓后重算


# ============================================================
# Trade Engine — 主循环
# ============================================================
class SqueezeBreakoutBot:
    def __init__(self, config: TradeConfig):
        self.cfg = config
        self.client: Optional[BinanceClient] = None
        self.positions: List[Position] = []
        self.trade_log: List[dict] = []
        self.daily_pnl: float = 0.0
        self.daily_loss: float = 0.0
        self.consecutive_losses: int = 0
        self.last_trade_day: str = ""
        self.cooldown_until: Optional[datetime] = None

        # 状态 (Web UI 读取)
        self.running = False
        self.status_text = "就绪"
        self.last_scan_time: Optional[datetime] = None
        self.last_signal_count = 0
        self.last_signals_data: List[dict] = []
        self.thread: Optional[threading.Thread] = None
        self._closed_positions: dict = {}  # {symbol: {time, price, direction, reason}} 二次入场用
        self._pending_signals: dict = {}   # {symbol: {first_seen, direction}} 跨扫描追踪
        self._rj_setup_pool: dict = {}      # RJ-only候选池: 关键K候选 -> 实时突破触发
        self._rj_only_latest_setups: List[dict] = []
        self._predicta_setup_pool: dict = {}
        self._predicta_latest_setups: List[dict] = []
        self._rj_watchlist: dict = {"updated_ts": 0.0, "rows": [], "symbols": []}
        self._rj_watchlist_path: str = "rj_watchlist.json"
        self._last_rj_setup_check_ts: float = 0.0
        self._used_signal_keys: dict = {}  # {signal_key: {time, symbol, direction, interval}} 已用结构, 防同形态复进
        self._failed_signal_keys: dict = {}  # {signal_key: {ts, symbol, direction, interval}} 下单失败短冷却, 防交易所None反复打单
        self._scan_count: int = 0          # 扫描计数器(用于池子重扫)
        self.trade_log: list = []           # 交易记录 (内存 + JSONL文件)
        self._btc_regime_cache: dict = {"ts": 0, "data": {}}
        self._last_equity_snapshot_ts: float = 0.0
        self._last_equity_snapshot_balance: float = 0.0
        self._last_equity_snapshot_record_net: float = 0.0
        self._fast_equity_cache_ts: float = 0.0
        self._fast_equity_cache: list = []
        self._fast_drawdown_cache_ts: float = 0.0
        self._fast_drawdown_cache: dict = {}
        self._fast_position_snapshot_ts: float = 0.0
        self._last_stop_reconcile_ts: float = 0.0
        self._hermes_confirm_cache: dict = {}
        self._signal_event_cache: dict = {"ts": 0.0, "mtime": 0.0, "data": {}}

        self.start_time: Optional[datetime] = None
        self._log = log  # 默认共享日志, setup_instance_log会替换为独立logger
        self._log_handler = None
        self._log_ready = False  # setup_instance_log调用后设为True
        self._init_client()
        self._load_trade_history()

    def setup_instance_log(self, uid: int):
        """为该bot实例创建独立日志文件"""
        import logging as _logging
        logger_name = f"trader.bot{uid}" if uid > 0 else "trader.demo"
        self._log = _logging.getLogger(logger_name)
        self._log.setLevel(_logging.INFO)
        self._log.propagate = False  # 不传播到父logger, 日志独立
        if self._log_handler:
            self._log.removeHandler(self._log_handler)
        fname = f"bot_{uid}.log" if uid > 0 else "bot_demo.log"
        self._log_handler = _logging.FileHandler(fname, encoding="utf-8")
        self._log_handler.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self._log.addHandler(self._log_handler)
        self._log.addHandler(logging.StreamHandler())
        self._log_ready = True
        if self.client is not None:
            self.client._log = self._log  # 客户端日志同步到用户独立文件

    def _load_trade_history(self):
        """从文件加载历史交易记录(去重)"""
        if Path(getattr(self, '_trade_log_path', 'trades.jsonl')).exists():
            try:
                seen = set()
                with open(getattr(self, '_trade_log_path', 'trades.jsonl'), 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        d = json.loads(line)
                        key = d.get('time','') + d.get('symbol','') + d.get('direction','') + str(d.get('pnl',''))
                        if key not in seen:
                            seen.add(key)
                            self.trade_log.append(d)
                if getattr(self, '_log_ready', False):
                    self._log.info(f"加载 {len(self.trade_log)} 条历史记录 (去重后)")
                self._restore_recent_closed_positions_from_trades()
                self._restore_used_signal_keys_from_trades()
            except Exception as e:
                if getattr(self, '_log_ready', False):
                    self._log.warning(f"加载交易记录失败: {e}")

    @staticmethod
    def _norm_signal_num(v, digits: int = 8) -> str:
        try:
            f = float(v)
            if abs(f) < 1e-12:
                return ""
            return f"{f:.{digits}f}".rstrip("0").rstrip(".")
        except Exception:
            return ""

    @staticmethod
    def _as_bool(v) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() in ("1", "true", "yes", "on", "enabled")

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict]:
        if not text:
            return None
        s = str(text).strip()
        try:
            data = json.loads(s)
            return data if isinstance(data, dict) else None
        except Exception:
            pass
        match = re.search(r"\{.*\}", s, flags=re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _compact_candles_for_ai(self, df, limit: int = 60) -> list:
        if df is None or len(df) <= 0:
            return []
        out = []
        try:
            cols = set(df.columns)
            rows = df.tail(max(5, int(limit))).to_dict("records")
            for r in rows:
                ts = r.get("ot") if "ot" in cols else r.get("time", "")
                out.append([
                    int(float(ts)) if ts not in ("", None) else "",
                    round(float(r.get("o", 0) or 0), 8),
                    round(float(r.get("h", 0) or 0), 8),
                    round(float(r.get("l", 0) or 0), 8),
                    round(float(r.get("c", 0) or 0), 8),
                    round(float(r.get("v", 0) or 0), 4),
                ])
        except Exception:
            return []
        return out

    def _build_hermes_direction_prompt(self, symbol: str, interval: str) -> str:
        base_symbol = re.sub(r"(?:[-_/]?(?:USDT|USDC|USD))$", "", str(symbol or "").strip().upper())
        prompt_payload = {
            "task": "AXIOM_SYMBOL_DIRECTION_CHECK",
            "rule": "Blind direction check. AXIOM does not disclose its planned order direction.",
            "symbol": base_symbol,
            "axiom_observation_interval": interval,
            "market": str(getattr(self.cfg, "market_type", "futures") or "futures"),
        }
        return (
            f"{base_symbol} 完整分析\n"
            "请严格调用已安装的 kline-indicator 技能，以 full 模式完成宏观周期、量价因子、"
            "衍生品三大支柱全量分析后再得出结论。不得下单，也不得询问或推测 AXIOM 的计划方向。\n"
            "分析成功时，dominant_direction 必须给出相对占优的 LONG 或 SHORT；"
            "tradeable 单独表示当前是否值得交易，因此证据冲突时不要用 NEUTRAL 代替结论。"
            "只有技能、数据或调用失败时才返回非 OK 状态，禁止伪装成技术面中性。\n"
            "Return JSON only, no markdown, no prose. Schema: "
            "{\"analysis_status\":\"OK|NO_DATA|SKILL_ERROR\","
            "\"dominant_direction\":\"LONG|SHORT\",\"tradeable\":true,"
            "\"market_regime\":\"TREND|RANGE|REVERSAL|CONFLICT\","
            "\"reason\":\"short reason\",\"risk_flags\":[],"
            "\"skill_used\":\"kline-indicator\",\"mode_used\":\"full|quick|unknown\","
            "\"data_source\":\"actual source\",\"pillars_checked\":[],"
            "\"indicators_checked\":[],\"evidence\":{}}.\n"
            "pillars_checked 必须列出技能实际完成的三大支柱；evidence 必须记录各支柱的结论。\n"
            f"Input:\n{json.dumps(prompt_payload, ensure_ascii=False, default=str)}"
        )

    def _hermes_direction_gate(self, direction: str, parsed: dict, fail_open: bool) -> tuple[bool, str]:
        decision = str(parsed.get("decision", "") or "").strip().lower()
        analysis_status = str(parsed.get("analysis_status", "") or "").strip().upper()
        ai_direction = str(
            parsed.get("dominant_direction", parsed.get("direction", "")) or ""
        ).strip().upper()
        risk_flags = parsed.get("risk_flags", [])
        if not isinstance(risk_flags, list):
            risk_flags = [str(risk_flags)]
        flags = {str(flag or "").strip().lower() for flag in risk_flags}
        pillars_checked = parsed.get("pillars_checked", [])
        evidence = parsed.get("evidence", {})
        pillar_keys = {
            re.sub(r"[^a-z0-9]+", "_", str(x or "").strip().lower()).strip("_")
            for x in pillars_checked
        } if isinstance(pillars_checked, list) else set()
        evidence_keys = {
            re.sub(r"[^a-z0-9]+", "_", str(x or "").strip().lower()).strip("_")
            for x in evidence
        } if isinstance(evidence, dict) else set()

        failure_reason = ""
        try:
            process_returncode = int(parsed.get("_process_returncode", 0) or 0)
        except (TypeError, ValueError):
            process_returncode = -1
        if process_returncode != 0:
            failure_reason = "process_error"
        elif not analysis_status:
            failure_reason = "status_missing"
        elif analysis_status in {"NO_DATA", "DATA_UNAVAILABLE"}:
            failure_reason = "data_unavailable"
        elif analysis_status in {"SKILL_ERROR", "SKILL_UNAVAILABLE"}:
            failure_reason = "skill_unavailable"
        elif analysis_status in {"TIMEOUT", "QUEUE_TIMEOUT"}:
            failure_reason = analysis_status.lower()
        elif analysis_status and analysis_status != "OK":
            failure_reason = "invalid_status"
        elif decision in {"timeout", "queue_timeout", "error"}:
            failure_reason = decision
        elif "skill_unavailable" in flags:
            failure_reason = "skill_unavailable"
        elif "data_unavailable" in flags or any(flag.startswith("symbol_not_") for flag in flags):
            failure_reason = "data_unavailable"
        elif str(parsed.get("skill_used", "") or "").strip().lower() != "kline-indicator":
            failure_reason = "skill_incomplete"
        elif str(parsed.get("mode_used", "") or "").strip().lower() != "full":
            failure_reason = "mode_incomplete"
        elif str(parsed.get("data_source", "") or "").strip().lower() in {"", "unknown"}:
            failure_reason = "data_unavailable"
        elif not parsed.get("indicators_checked"):
            failure_reason = "indicators_missing"
        elif not {
            "macro_cycle", "price_volume_factors", "derivatives"
        }.issubset(pillar_keys) or not (
            "macro_cycle" in evidence_keys
            and "derivatives" in evidence_keys
            and any(key == "price_volume_factors" or key.startswith("price_volume_factors_") for key in evidence_keys)
        ):
            failure_reason = "pillars_incomplete"
        elif ai_direction not in {"LONG", "SHORT"}:
            failure_reason = "invalid_direction"
        elif "tradeable" not in parsed or not isinstance(parsed.get("tradeable"), bool):
            failure_reason = "tradeable_missing"

        if failure_reason:
            suffix = ":fail_open" if fail_open else ""
            return bool(fail_open), f"operational_failure:{failure_reason}{suffix}"
        if not parsed["tradeable"]:
            return False, "market_not_tradeable"
        if ai_direction == str(direction or "").strip().upper():
            return True, "direction_match"
        return False, "direction_opposite"

    def _hermes_confirm_entry(self, signal: dict, df, entry_price: float, sl_price: float,
                              qty: float, pos_usdt: float, risk: float) -> dict:
        enabled = self._as_bool(getattr(self.cfg, "hermes_confirm_enabled", False))
        mode = str(getattr(self.cfg, "hermes_confirm_mode", "log_only") or "log_only").strip().lower()
        if not enabled or mode not in ("log_only", "hard_filter"):
            return {"active": False, "pass": True, "mode": mode if enabled else "disabled"}

        symbol = str(signal.get("symbol", "") or "")
        direction = str(signal.get("direction", "") or "").upper()
        interval = str(signal.get("source_interval") or getattr(self.cfg, "scan_interval", "30m")).split(",")[0].strip()
        signal_key = str(signal.get("signal_key") or signal.get("rj_setup_key") or "")
        try:
            last_ot = int(float(df["ot"].iloc[-1])) if df is not None and "ot" in df.columns and len(df) else 0
        except Exception:
            last_ot = 0
        cache_key = "|".join([symbol, direction, interval, signal_key, str(last_ot)])
        ttl = max(0, int(getattr(self.cfg, "hermes_confirm_cache_ttl_sec", 1800) or 0))
        cached = self._hermes_confirm_cache.get(cache_key)
        now_ts = time.time()
        if cached and ttl > 0 and now_ts - float(cached.get("ts", 0) or 0) <= ttl:
            data = dict(cached.get("data") or {})
            data["cached"] = True
            return data

        timeout_sec = max(3, min(300, int(getattr(self.cfg, "hermes_confirm_timeout_sec", 240) or 240)))
        queue_wait_sec = max(0, min(300, int(getattr(self.cfg, "hermes_confirm_queue_wait_sec", 300) or 300)))
        fail_open = self._as_bool(getattr(self.cfg, "hermes_confirm_fail_open", False))
        cmd = str(getattr(self.cfg, "hermes_confirm_cmd", "hermes") or "hermes").strip()
        if cmd == "hermes" and os.path.exists("/root/.hermes/hermes-agent/venv/bin/hermes"):
            cmd = "/root/.hermes/hermes-agent/venv/bin/hermes"
        skills = str(getattr(self.cfg, "hermes_confirm_skills", "kline-indicator") or "").strip()

        prompt = self._build_hermes_direction_prompt(symbol, interval)

        result = {
            "active": True,
            "pass": fail_open if mode == "hard_filter" else True,
            "mode": mode,
            "analysis_status": "ERROR",
            "decision": "error",
            "direction": "NEUTRAL",
            "dominant_direction": "",
            "tradeable": False,
            "market_regime": "",
            "confidence": 0.0,
            "reason": "",
            "risk_flags": [],
            "skill_used": "",
            "mode_used": "",
            "data_source": "",
            "indicators_checked": [],
            "pillars_checked": [],
            "evidence": {},
            "skills_requested": skills,
            "cached": False,
            "queued_sec": 0.0,
        }
        lock_acquired = False
        wait_started = time.time()
        try:
            lock_acquired = HERMES_CONFIRM_LOCK.acquire(timeout=queue_wait_sec)
            result["queued_sec"] = round(time.time() - wait_started, 3)
            if not lock_acquired:
                result.update({
                    "decision": "queue_timeout",
                    "reason": f"hermes queue wait timeout {queue_wait_sec}s",
                })
                if mode == "log_only" or fail_open:
                    result["pass"] = True
                self._hermes_confirm_cache[cache_key] = {"ts": now_ts, "data": result}
                return result
            args = shlex.split(cmd, posix=(os.name != "nt"))
            if not args:
                raise RuntimeError("empty hermes command")
            if skills:
                args.extend(["--skills", skills])
            args.extend(["--oneshot", prompt])
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
            )
            raw = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
            parsed = self._extract_json_object(raw)
            if not parsed:
                raise RuntimeError(f"no json from hermes rc={completed.returncode}: {raw[-500:]}")
            parsed["_process_returncode"] = completed.returncode
            analysis_status = str(parsed.get("analysis_status", "") or "").strip().upper()
            ai_direction = str(
                parsed.get("dominant_direction", parsed.get("direction", "")) or ""
            ).strip().upper()
            parsed["analysis_status"] = analysis_status
            parsed["dominant_direction"] = ai_direction
            tradeable = self._as_bool(parsed.get("tradeable", False))
            decision = "allow" if analysis_status == "OK" and tradeable else "block"
            confidence = float(parsed.get("confidence", 0) or 0)
            reason = str(parsed.get("reason", "") or "")[:300]
            risk_flags = parsed.get("risk_flags", [])
            if not isinstance(risk_flags, list):
                risk_flags = [str(risk_flags)]
            skill_used = str(parsed.get("skill_used", "") or "").strip()
            mode_used = str(parsed.get("mode_used", "") or "").strip()
            data_source = str(parsed.get("data_source", "") or "").strip()
            indicators_checked = parsed.get("indicators_checked", [])
            if not isinstance(indicators_checked, list):
                indicators_checked = [str(indicators_checked)]
            pillars_checked = parsed.get("pillars_checked", [])
            if not isinstance(pillars_checked, list):
                pillars_checked = [str(pillars_checked)]
            evidence = parsed.get("evidence", {})
            if not isinstance(evidence, dict):
                evidence = {}
            allowed, gate_reason = self._hermes_direction_gate(direction, parsed, fail_open)
            if mode == "log_only":
                allowed = True
            result.update({
                "pass": bool(allowed),
                "gate_reason": gate_reason,
                "analysis_status": analysis_status,
                "decision": decision,
                "direction": ai_direction,
                "dominant_direction": ai_direction,
                "tradeable": tradeable,
                "market_regime": str(parsed.get("market_regime", "") or "").upper()[:20],
                "confidence": confidence,
                "reason": reason,
                "risk_flags": risk_flags[:8],
                "skill_used": skill_used[:80],
                "mode_used": mode_used[:40],
                "data_source": data_source[:60],
                "indicators_checked": [str(x)[:80] for x in indicators_checked[:20]],
                "pillars_checked": [str(x)[:80] for x in pillars_checked[:6]],
                "evidence": {str(k)[:40]: str(v)[:160] for k, v in list(evidence.items())[:8]},
                "skills_requested": skills,
                "returncode": completed.returncode,
            })
        except subprocess.TimeoutExpired:
            result.update({
                "analysis_status": "TIMEOUT",
                "decision": "timeout",
                "reason": f"hermes timeout {timeout_sec}s",
            })
            if mode == "log_only" or fail_open:
                result["pass"] = True
        except Exception as e:
            result.update({"analysis_status": "ERROR", "decision": "error", "reason": str(e)[:300]})
            if mode == "log_only" or fail_open:
                result["pass"] = True
        finally:
            if lock_acquired:
                try:
                    HERMES_CONFIRM_LOCK.release()
                except RuntimeError:
                    pass

        self._hermes_confirm_cache[cache_key] = {"ts": now_ts, "data": result}
        return result

    def _public_hermes_confirm(self, state: Optional[dict]) -> dict:
        state = state or {}
        out = {
            "active": bool(state.get("active", False)),
            "pass": bool(state.get("pass", True)),
            "mode": str(state.get("mode", "disabled") or "disabled"),
            "analysis_status": str(state.get("analysis_status", "") or "").strip().upper()[:24],
            "decision": str(state.get("decision", "") or ""),
            "direction": str(state.get("direction", "NEUTRAL") or "NEUTRAL").strip().upper(),
            "dominant_direction": str(
                state.get("dominant_direction", state.get("direction", "")) or ""
            ).strip().upper()[:12],
            "tradeable": self._as_bool(state.get("tradeable", False)),
            "market_regime": str(state.get("market_regime", "") or "").upper()[:20],
            "confidence": round(float(state.get("confidence", 0.0) or 0.0), 1),
            "reason": str(state.get("reason", "") or "")[:160],
            "skill_used": str(state.get("skill_used", "") or "")[:80],
            "mode_used": str(state.get("mode_used", "") or "")[:40],
            "data_source": str(state.get("data_source", "") or "")[:60],
            "skills_requested": str(state.get("skills_requested", "") or "")[:80],
            "cached": bool(state.get("cached", False)),
            "queued_sec": round(float(state.get("queued_sec", 0.0) or 0.0), 3),
            "gate_reason": str(state.get("gate_reason", "") or "")[:80],
        }
        pillars = state.get("pillars_checked", [])
        out["pillars_checked"] = (
            [str(x)[:80] for x in pillars[:6]] if isinstance(pillars, list) else []
        )
        evidence = state.get("evidence", {})
        out["evidence"] = (
            {str(k)[:40]: str(v)[:160] for k, v in list(evidence.items())[:8]}
            if isinstance(evidence, dict) else {}
        )
        flags = state.get("risk_flags", [])
        if isinstance(flags, list):
            out["risk_flags"] = [str(x)[:80] for x in flags[:5]]
        elif flags:
            out["risk_flags"] = [str(flags)[:80]]
        else:
            out["risk_flags"] = []
        indicators = state.get("indicators_checked", [])
        if isinstance(indicators, list):
            out["indicators_checked"] = [str(x)[:80] for x in indicators[:12]]
        elif indicators:
            out["indicators_checked"] = [str(indicators)[:80]]
        else:
            out["indicators_checked"] = []
        return out

    def _position_choppy_filter(self, state: Optional[dict]) -> dict:
        """压缩入场时震荡判定，供持仓、交易记录和前端复盘共用。"""
        state = state or {}
        source_reason = str(state.get("reason", state.get("choppy_filter_reason", "")) or "")
        if source_reason == "not_recorded":
            recorded = False
        elif "recorded" in state:
            recorded = bool(state.get("recorded"))
        else:
            recorded = bool(
                "choppy_filter_available" in state
                or "choppy_filter_is_choppy" in state
                or "choppy_filter_mode" in state
                or "available" in state
                or "is_choppy" in state
            )
        if not recorded:
            return {
                "recorded": False,
                "mode": "",
                "anchor": "",
                "available": False,
                "is_choppy": False,
                "reason": "not_recorded",
                "reasons": [],
                "atr_ratio": None,
                "box_position": None,
                "box_amplitude": None,
                "adx_period": None,
                "adx": None,
                "efficiency_period": None,
                "efficiency_ratio": None,
            }

        reasons = state.get("reasons", state.get("choppy_filter_reasons", []))
        if not isinstance(reasons, list):
            reasons = [reasons] if reasons else []

        def optional_float(name, source_name):
            value = state.get(name, state.get(source_name))
            try:
                return round(float(value), 6) if value is not None else None
            except (TypeError, ValueError):
                return None

        def optional_int(name, source_name):
            value = state.get(name, state.get(source_name))
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "recorded": True,
            "mode": str(state.get("mode", state.get("choppy_filter_mode", "")) or "")[:16],
            "anchor": str(state.get("anchor", state.get("choppy_filter_anchor", "")) or "")[:24],
            "available": bool(state.get("available", state.get("choppy_filter_available", False))),
            "is_choppy": bool(state.get("is_choppy", state.get("choppy_filter_is_choppy", False))),
            "reason": str(state.get("reason", state.get("choppy_filter_reason", "")) or "")[:40],
            "reasons": [str(reason)[:40] for reason in reasons[:3]],
            "atr_ratio": optional_float("atr_ratio", "choppy_atr_ratio"),
            "box_position": optional_float("box_position", "choppy_box_position"),
            "box_amplitude": optional_float("box_amplitude", "choppy_box_amplitude"),
            "adx_period": optional_int("adx_period", "choppy_adx_period"),
            "adx": optional_float("adx", "choppy_adx"),
            "efficiency_period": optional_int("efficiency_period", "choppy_efficiency_period"),
            "efficiency_ratio": optional_float(
                "efficiency_ratio", "choppy_efficiency_ratio"
            ),
        }

    def _entry_choppy_audit_map(self) -> dict:
        """从成交事件恢复旧持仓缺失的入场快照，不用当前行情补算。"""
        audits = {}
        path = Path(getattr(self, "_signal_log_path", SIGNAL_LOG_PATH))
        if not path.exists():
            return audits
        try:
            lines = deque(maxlen=5000)
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    lines.append(line)
            for line in lines:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("event") != "entry_filled":
                    continue
                audit = self._position_choppy_filter(event)
                if not audit.get("recorded"):
                    continue
                symbol = str(event.get("symbol", "") or "")
                signal_key = str(event.get("signal_key", "") or "")
                if symbol:
                    audits[(symbol, "")] = audit
                    if signal_key:
                        audits[(symbol, signal_key)] = audit
        except Exception as exc:
            if getattr(self, "_log_ready", False):
                self._log.warning(f"恢复持仓震荡快照失败: {exc}")
        return audits

    def _bar_marker(self, df, idx):
        try:
            if idx is None:
                return ""
            i = int(idx)
            if df is not None and "ot" in df.columns and 0 <= i < len(df):
                return str(int(float(df["ot"].iloc[i])))
            return str(i)
        except Exception:
            return str(idx or "")

    def _build_signal_key(self, symbol: str, direction: str, interval: str, payload: Optional[dict] = None, df=None) -> str:
        """生成结构级指纹: 同一突破/回归/确认分型只能成交一次。"""
        payload = payload or {}
        existing = str(payload.get("signal_key", "") or "")
        if existing:
            return existing
        parts = [
            symbol,
            direction,
            interval,
            self._bar_marker(df, payload.get("breakout_bar")),
            self._bar_marker(df, payload.get("retest_bar")),
            self._bar_marker(df, payload.get("first_fractal_bar")),
            self._bar_marker(df, payload.get("confirm_fractal_bar")),
            self._norm_signal_num(payload.get("fractal_sl")),
        ]
        key = "|".join(parts)
        # 旧记录/旧信号可能没有结构索引, 用分型止损做兜底, 不把 current band_sl 放进核心键。
        if key.count("|") >= 7 and any(parts[3:7]):
            return key
        fractal_key = self._norm_signal_num(payload.get("fractal_sl"))
        if not fractal_key:
            return ""
        return "|".join([symbol, direction, interval, fractal_key])

    def _build_signal_keys(self, symbol: str, direction: str, interval: str, payload: Optional[dict] = None, df=None) -> list:
        """生成精确键+兼容旧日志的近似键。"""
        payload = payload or {}
        keys = []
        exact = self._build_signal_key(symbol, direction, interval, payload, df=df)
        if exact:
            keys.append(exact)

        # Old logs sometimes stored the buffered stop value while fresh scans use
        # the raw confirmation fractal. Keep both forms under the same structure.
        raw_fractal = payload.get("fractal_sl")
        fractal_variants = []
        try:
            base = float(raw_fractal)
            if base > 0:
                fractal_variants.append(base)
                side = str(direction).upper()
                if side == "LONG":
                    fractal_variants.extend([base * 0.998, base / 0.998])
                elif side == "SHORT":
                    fractal_variants.extend([base * 1.002, base / 1.002])
        except Exception:
            if raw_fractal not in (None, ""):
                fractal_variants.append(raw_fractal)

        for fractal in fractal_variants:
            fractal_key = self._norm_signal_num(fractal)
            if not fractal_key:
                continue
            approx = "|".join([symbol, direction, interval, fractal_key])
            if approx not in keys:
                keys.append(approx)
        return keys

    def _cleanup_used_signal_keys(self):
        now_ts = time.time()
        ttl = 7 * 24 * 3600
        for k, v in list(getattr(self, "_used_signal_keys", {}).items()):
            try:
                if now_ts - float(v.get("ts", 0) or 0) > ttl:
                    del self._used_signal_keys[k]
            except Exception:
                del self._used_signal_keys[k]

    def _mark_signal_used(self, signal_key: str, symbol: str, direction: str, interval: str, payload: Optional[dict] = None):
        if not signal_key:
            return
        payload = payload or {}
        self._used_signal_keys[signal_key] = {
            "ts": time.time(),
            "time": bj_now().isoformat(),
            "symbol": symbol,
            "direction": direction,
            "interval": interval,
            "fractal_sl": payload.get("fractal_sl"),
            "band_sl": payload.get("band_sl"),
        }
        self._cleanup_used_signal_keys()

    def _signal_already_used(self, signal_key: str) -> bool:
        if not signal_key:
            return False
        self._cleanup_used_signal_keys()
        return signal_key in self._used_signal_keys

    def _any_signal_key_used(self, signal_keys: list) -> bool:
        self._cleanup_used_signal_keys()
        return any(k in self._used_signal_keys for k in signal_keys if k)

    def _signal_failure_ttl_seconds(self) -> int:
        return 30 * 60

    def _cleanup_failed_signal_keys(self):
        now_ts = time.time()
        ttl = self._signal_failure_ttl_seconds()
        for k, v in list(getattr(self, "_failed_signal_keys", {}).items()):
            try:
                if now_ts - float(v.get("ts", 0) or 0) > ttl:
                    del self._failed_signal_keys[k]
            except Exception:
                del self._failed_signal_keys[k]

    def _mark_signal_failed(self, signal_key: str, symbol: str, direction: str, interval: str,
                            payload: Optional[dict] = None, response=None):
        if not signal_key:
            return
        payload = payload or {}
        self._failed_signal_keys[signal_key] = {
            "ts": time.time(),
            "time": bj_now().isoformat(),
            "symbol": symbol,
            "direction": direction,
            "interval": interval,
            "fractal_sl": payload.get("fractal_sl"),
            "band_sl": payload.get("band_sl"),
            "response": str(response)[:200],
        }
        self._cleanup_failed_signal_keys()

    def _failed_signal_key(self, signal_keys: list) -> str:
        self._cleanup_failed_signal_keys()
        for k in signal_keys:
            if k and k in self._failed_signal_keys:
                return k
        return ""

    def _restore_used_signal_keys_from_trades(self):
        """重启后从交易记录恢复结构指纹；兼容旧记录的近似键。"""
        restored = 0
        failed_restored = 0
        cutoff = bj_now() - timedelta(days=7)
        failed_cutoff = bj_now() - timedelta(seconds=self._signal_failure_ttl_seconds())
        for d in self.trade_log[-1000:]:
            symbol = d.get("symbol", "")
            direction = d.get("direction", "")
            interval = d.get("interval") or d.get("source_interval") or ""
            if not symbol or not direction:
                continue
            try:
                t = datetime.fromisoformat(str(d.get("time", "")))
                if t.tzinfo is None and cutoff.tzinfo is not None:
                    t = t.replace(tzinfo=cutoff.tzinfo)
            except Exception:
                t = bj_now()
            if t < cutoff:
                continue
            keys = [str(d.get("signal_key", "") or "")]
            keys += self._build_signal_keys(symbol, direction, interval, d)
            for key in [k for k in dict.fromkeys(keys) if k]:
                self._used_signal_keys[key] = {
                    "ts": time.time(),
                    "time": d.get("time", ""),
                    "symbol": symbol,
                    "direction": direction,
                    "interval": interval,
                    "fractal_sl": d.get("fractal_sl"),
                    "band_sl": d.get("band_sl"),
                }
                restored += 1
        try:
            sig_path = getattr(self, "_signal_log_path", SIGNAL_LOG_PATH)
            if Path(sig_path).exists():
                with open(sig_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-3000:]
                for line in lines:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    symbol = d.get("symbol", "")
                    direction = d.get("direction", "")
                    interval = d.get("interval") or d.get("source_interval") or ""
                    if not symbol or not direction:
                        continue
                    try:
                        t = datetime.fromisoformat(str(d.get("time", "")))
                        if t.tzinfo is None and cutoff.tzinfo is not None:
                            t = t.replace(tzinfo=cutoff.tzinfo)
                    except Exception:
                        t = bj_now()
                    if d.get("event") == "entry_reject" and d.get("reason") == "order_failed":
                        if t < failed_cutoff:
                            continue
                        keys = self._build_signal_keys(symbol, direction, interval, d)
                        for key in [k for k in dict.fromkeys(keys) if k and k not in self._failed_signal_keys]:
                            self._failed_signal_keys[key] = {
                                "ts": time.time(),
                                "time": d.get("time", ""),
                                "symbol": symbol,
                                "direction": direction,
                                "interval": interval,
                                "fractal_sl": d.get("fractal_sl"),
                                "band_sl": d.get("band_sl"),
                                "response": str(d.get("order_response"))[:200],
                            }
                            failed_restored += 1
                        continue
                    if d.get("event") != "entry_filled" or t < cutoff:
                        continue
                    keys = self._build_signal_keys(symbol, direction, interval, d)
                    for key in [k for k in dict.fromkeys(keys) if k and k not in self._used_signal_keys]:
                        self._used_signal_keys[key] = {
                            "ts": time.time(),
                            "time": d.get("time", ""),
                            "symbol": symbol,
                            "direction": direction,
                            "interval": interval,
                            "fractal_sl": d.get("fractal_sl"),
                            "band_sl": d.get("band_sl"),
                        }
                        restored += 1
        except Exception as e:
            if getattr(self, '_log_ready', False):
                self._log.warning(f"恢复已用结构指纹事件失败: {e}")
        if restored and getattr(self, '_log_ready', False):
            self._log.info(f"恢复已用结构指纹 {restored} 条")
        if failed_restored and getattr(self, '_log_ready', False):
            self._log.info(f"恢复下单失败冷却指纹 {failed_restored} 条")

    def _remember_closed_position(self, symbol: str, direction: str, exit_price: float, reason: str,
                                  pnl: float = 0.0, interval: str = "", closed_at: Optional[datetime] = None,
                                  clear_pending: bool = True):
        """统一记录最近平仓, 用于冷却/二次入场过滤。"""
        if not symbol:
            return
        self._closed_positions[symbol] = {
            "time": closed_at or bj_now(),
            "price": float(exit_price or 0.0),
            "direction": direction,
            "reason": str(reason or ""),
            "pnl": float(pnl or 0.0),
            "interval": interval or "",
        }
        if clear_pending:
            self._pending_signals.pop(symbol, None)

    def _restore_recent_closed_positions_from_trades(self):
        """从最近交易记录恢复平仓冷却, 避免服务重启后同币种立刻复进。"""
        if not self.trade_log:
            return
        cutoff = bj_now() - timedelta(hours=24)
        restored = 0
        for d in self.trade_log[-500:]:
            symbol = d.get("symbol", "")
            reason = str(d.get("reason", "") or "")
            if not symbol or "减仓" in reason:
                continue
            try:
                closed_at = datetime.fromisoformat(str(d.get("time", "")))
                if closed_at.tzinfo is None and cutoff.tzinfo is not None:
                    closed_at = closed_at.replace(tzinfo=cutoff.tzinfo)
                elif closed_at.tzinfo is not None and cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=closed_at.tzinfo)
            except Exception:
                continue
            if closed_at < cutoff:
                continue
            self._remember_closed_position(
                symbol=symbol,
                direction=d.get("direction", ""),
                exit_price=float(d.get("exit", 0.0) or 0.0),
                reason=reason,
                pnl=float(d.get("pnl", 0.0) or 0.0),
                interval=d.get("interval", ""),
                closed_at=closed_at,
                clear_pending=False,
            )
            restored += 1
        if restored and getattr(self, '_log_ready', False):
            self._log.info(f"恢复最近平仓冷却记录 {restored} 条")

    def _append_trade(self, record: dict):
        """追加一条交易记录到文件"""
        record.setdefault("pnl_source", "estimate")
        if not any(str(k).startswith("btc_") for k in record):
            try:
                record.update(self._btc_regime_fields())
            except Exception:
                record.update({"btc_regime": "btc_unknown", "btc_score": 0})
        self.trade_log.append(record)
        try:
            with open(getattr(self, '_trade_log_path', 'trades.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
        except Exception as e:
            self._log.error(f"写入交易记录失败: {e}")

    def _append_signal_event(self, event_type: str, symbol: str = "", payload: Optional[dict] = None):
        """追加结构化信号事件，用于回测/拆账/拒绝原因分析。"""
        payload = payload or {}
        record = {
            "time": bj_now().isoformat(),
            "event": event_type,
            "symbol": symbol or payload.get("symbol", ""),
            "exchange": getattr(self.cfg, "exchange", ""),
            "mode": getattr(self.cfg, "mode", ""),
        }
        for k, v in payload.items():
            if isinstance(v, (np.integer, np.floating)):
                v = v.item()
            record[k] = v
        try:
            with open(getattr(self, '_signal_log_path', SIGNAL_LOG_PATH), 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        except Exception as e:
            self._log.error(f"写入信号事件失败: {e}")

    def _equity_log_path(self) -> str:
        # Do not use getattr(self, "_equity_log_path"): this method has the same
        # name, so getattr would return the bound method and break Path/open().
        path = self.__dict__.get("_equity_log_path", "")
        if path:
            return path
        trade_path = str(getattr(self, "_trade_log_path", TRADE_LOG_PATH) or TRADE_LOG_PATH)
        name = os.path.basename(trade_path)
        folder = os.path.dirname(trade_path)
        if name.startswith("trades"):
            name = "equity" + name[len("trades"):]
        else:
            name = "equity.jsonl"
        return os.path.join(folder, name) if folder else name

    def _equity_state_path(self) -> str:
        base = self._equity_log_path()
        folder = os.path.dirname(base)
        name = os.path.basename(base).replace("equity", "equity_state", 1).replace(".jsonl", ".json")
        return os.path.join(folder, name) if folder else name

    def _load_equity_state(self) -> dict:
        try:
            path = self._equity_state_path()
            if not Path(path).exists():
                return {}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_equity_state(self, state: dict):
        try:
            with open(self._equity_state_path(), "w", encoding="utf-8") as f:
                json.dump(state or {}, f, ensure_ascii=False)
        except Exception as e:
            if getattr(self, "_log_ready", False):
                self._log.warning(f"写入权益状态缓存失败: {e}")

    def _append_equity_snapshot(self, account_balance: float, account_initial_equity: float,
                                record_net_pnl: float, open_pnl: float, reason: str = "summary"):
        """记录交易所权益快照；前端净值曲线以它为准，避免交易估算PnL污染总账。"""
        try:
            account_balance = float(account_balance or 0.0)
            account_initial_equity = float(account_initial_equity or 0.0)
            record_net_pnl = float(record_net_pnl or 0.0)
            open_pnl = float(open_pnl or 0.0)
        except Exception:
            return
        if account_balance <= 0 or account_initial_equity <= 0:
            return
        now_ts = time.time()
        force = reason != "summary"
        changed = (
            abs(account_balance - self._last_equity_snapshot_balance) >= 0.01 or
            abs(record_net_pnl - self._last_equity_snapshot_record_net) >= 0.01
        )
        if not force and self._last_equity_snapshot_ts and now_ts - self._last_equity_snapshot_ts < 60 and not changed:
            return
        record = {
            "time": bj_now().isoformat(),
            "account_balance": round(account_balance, 4),
            "account_initial_equity": round(account_initial_equity, 4),
            "account_pnl": round(account_balance - account_initial_equity, 4),
            "record_net_pnl": round(record_net_pnl, 4),
            "open_pnl": round(open_pnl, 4),
            "reconcile_diff": round((account_balance - account_initial_equity) - record_net_pnl, 4),
            "reason": reason,
        }
        try:
            with open(self._equity_log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._last_equity_snapshot_ts = now_ts
            self._last_equity_snapshot_balance = account_balance
            self._last_equity_snapshot_record_net = record_net_pnl
        except Exception as e:
            if getattr(self, "_log_ready", False):
                self._log.warning(f"写入权益快照失败: {e}")

    def _load_equity_history(self, limit: int = 1000) -> list:
        path = self._equity_log_path()
        if not Path(path).exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-max(int(limit), 1):]
            out = []
            seen = set()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                t = str(d.get("time", "") or "")
                bal = float(d.get("account_balance", 0) or 0)
                if not t or bal <= 0:
                    continue
                key = (t, bal)
                if key in seen:
                    continue
                seen.add(key)
                out.append(d)
            return out
        except Exception as e:
            if getattr(self, "_log_ready", False):
                self._log.warning(f"读取权益快照失败: {e}")
            return []

    @staticmethod
    def _calc_max_drawdown_from_values(values: list) -> dict:
        peak = None
        max_abs = 0.0
        max_pct = 0.0
        for raw in values or []:
            try:
                val = float(raw)
            except Exception:
                continue
            if not np.isfinite(val) or val <= 0:
                continue
            if peak is None or val > peak:
                peak = val
            if peak and peak > 0:
                dd_abs = max(0.0, peak - val)
                dd_pct = dd_abs / peak * 100.0
                if dd_abs > max_abs:
                    max_abs = dd_abs
                    max_pct = dd_pct
        return {
            "max_drawdown_abs": round(max_abs, 2),
            "max_drawdown_pct": round(max_pct, 2),
        }

    def _max_drawdown_summary(self, equity_history: list, account_balance: float,
                              account_initial_equity: float, account_pnl: float,
                              open_pnl: float) -> dict:
        values = []
        basis = "trade_log"
        try:
            target_base = float(account_initial_equity or 0.0)
            hist_all = sorted(
                [x for x in (equity_history or []) if x and float(x.get("account_balance", 0) or 0) > 0],
                key=lambda x: str(x.get("time", "")),
            )
            hist = hist_all
            if target_base > 0:
                same_base = []
                for x in hist_all:
                    try:
                        x_base = float(x.get("account_initial_equity", 0) or 0.0)
                    except Exception:
                        x_base = 0.0
                    if x_base > 0 and abs(x_base - target_base) < 0.01:
                        same_base.append(x)
                if same_base:
                    hist = same_base
            if hist:
                basis = "equity"
                base = target_base or float(hist[0].get("account_initial_equity", account_initial_equity) or 0.0)
                if base <= 0:
                    base = float(hist[0].get("account_balance", 0) or 0.0) - float(hist[0].get("account_pnl", 0) or 0.0)
                if base > 0:
                    values.append(base)
                values.extend(float(x.get("account_balance", 0) or 0.0) for x in hist)
                if account_balance > 0 and (not values or abs(account_balance - values[-1]) > 0.001):
                    values.append(float(account_balance))
            else:
                base = float(account_initial_equity or 0.0)
                if base <= 0 and account_balance > 0:
                    base = float(account_balance) - float(account_pnl or 0.0)
                if base > 0:
                    values.append(base)
                    equity = base
                    trades = sorted(self.trade_log, key=lambda x: str(x.get("time", "")))
                    for record in trades:
                        equity += self._trade_pnl_value(record)
                        values.append(equity)
                    if open_pnl:
                        values.append(equity + float(open_pnl or 0.0))
                    if account_balance > 0:
                        basis = "equity_live"
                        values.append(float(account_balance))
        except Exception as e:
            if getattr(self, "_log_ready", False):
                self._log.warning(f"最大回撤计算失败: {e}")
        out = self._calc_max_drawdown_from_values(values)
        out["max_drawdown_basis"] = basis
        return out

    @staticmethod
    def _trade_pnl_value(record: dict) -> float:
        """交易记录统计值: 有交易所已实现PnL优先用交易所值，否则用策略估算值。"""
        if record.get("voided"):
            return 0.0
        for key in ("exchange_pnl", "pnl"):
            try:
                value = record.get(key)
                if value is not None and value != "":
                    return float(value)
            except Exception:
                pass
        return 0.0

    @staticmethod
    def _extract_order_number(obj, keys, allow_negative: bool = False):
        if obj is None:
            return None
        if isinstance(keys, str):
            keys = (keys,)
        key_set = {str(k) for k in keys}
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k) in key_set:
                    try:
                        f = float(v)
                        if allow_negative or f > 0:
                            return f
                    except Exception:
                        pass
            for v in obj.values():
                found = SqueezeBreakoutBot._extract_order_number(v, keys, allow_negative=allow_negative)
                if found is not None:
                    return found
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                found = SqueezeBreakoutBot._extract_order_number(item, keys, allow_negative=allow_negative)
                if found is not None:
                    return found
        return None

    def _estimate_exit_price(self, pos: Position, reason: str):
        symbol = pos.symbol
        exit_price = pos.entry_price
        if '初始止损' in reason:
            exit_price = (
                pos.initial_sl if getattr(pos, 'initial_sl', 0) > 0
                else pos.sl_price if pos.sl_price > 0
                else pos.current_sl if pos.current_sl > 0
                else pos.entry_price
            )
        elif '击穿' in reason or 'SL' in reason or '止损' in reason:
            exit_price = pos.current_sl if pos.current_sl > 0 else pos.entry_price
        else:
            try:
                df = fetch_klines(symbol, self.cfg.scan_interval.split(',')[0].strip(), 5, exchange=self.cfg.exchange)
                if df is not None and len(df) > 0:
                    exit_price = df['c'].iloc[-1]
            except Exception:
                pass
        return float(exit_price or pos.entry_price)

    @staticmethod
    def _calc_trade_pnl(direction: str, entry_price: float, exit_price: float, quantity: float) -> float:
        if direction == 'LONG':
            return (exit_price - entry_price) * quantity
        return (entry_price - exit_price) * quantity

    @staticmethod
    def _empty_target_zone() -> dict:
        return {
            "target_zone_type": "none",
            "target_zone_price": 0.0,
            "target_zone_low": 0.0,
            "target_zone_high": 0.0,
            "target_r": 0.0,
            "target_distance_pct": 0.0,
            "target_zone_bars_ago": 0,
        }

    def _calc_target_zone(self, df, direction: str, entry_price: float, sl_price: float,
                          interval: str = "", signal: Optional[dict] = None) -> dict:
        """Record the narrowest forward historical MA squeeze zone for review only."""
        target = self._empty_target_zone()
        try:
            if df is None or len(df) < 80:
                return target
            entry_price = float(entry_price)
            sl_price = float(sl_price)
            risk_unit = abs(entry_price - sl_price)
            if entry_price <= 0 or risk_unit <= 0:
                return target

            closes = df["c"].astype(float)
            n = len(df)
            lookback = {"15m": 140, "1h": 180, "4h": 200, "1d": 220, "1w": 220}.get(interval, 180)
            start = max(122, n - lookback)
            end = max(start + 4, n - 4)  # 排除最近几根确认K, 避免把当前结构当目标
            min_gap = max(entry_price * 0.001, risk_unit * 0.05)
            candidates = []

            def add_candidate(kind: str, price: float, low: float, high: float, idx: int,
                              zone_width_pct: float, zone_bars: int):
                try:
                    price = float(price)
                    low = float(low)
                    high = float(high)
                    idx = int(idx)
                except Exception:
                    return
                if not np.isfinite(price) or price <= 0:
                    return
                if direction == "LONG":
                    distance = price - entry_price
                else:
                    distance = entry_price - price
                if distance <= min_gap:
                    return
                candidates.append({
                    "target_zone_type": kind,
                    "target_zone_price": price,
                    "target_zone_low": min(low, high),
                    "target_zone_high": max(low, high),
                    "target_r": distance / risk_unit,
                    "target_distance_pct": distance / entry_price * 100,
                    "target_zone_bars_ago": max(0, n - 1 - idx),
                    "_zone_width_pct": float(zone_width_pct),
                    "_zone_bars": int(zone_bars),
                })

            # 1) 历史顶/底拐点: 最近阻力/支撑
            # 2) 历史均线密集区: 前方供应/需求区
            max_band, min_band, spread_arr = calc_ma_band(closes)
            squeeze_max = get_squeeze_max(interval)
            sig = signal or {}
            sig_s0 = int(sig.get("squeeze_start", -999999) or -999999)
            sig_s1 = int(sig.get("squeeze_end", -999999) or -999999)
            i = start
            while i <= end:
                sp = spread_arr[i] if i < len(spread_arr) else np.nan
                if np.isnan(sp) or sp > squeeze_max:
                    i += 1
                    continue
                z0 = i
                while i <= end:
                    sp2 = spread_arr[i] if i < len(spread_arr) else np.nan
                    if np.isnan(sp2) or sp2 > squeeze_max:
                        break
                    i += 1
                z1 = i - 1
                if z1 - z0 + 1 < 5:
                    continue
                # 跳过本次入场前的同一段密集区, 只记录更早的前方目标。
                if not (z1 < sig_s0 or z0 > sig_s1):
                    continue
                zone_low = float(np.nanmin(min_band[z0:z1 + 1]))
                zone_high = float(np.nanmax(max_band[z0:z1 + 1]))
                if not np.isfinite(zone_low) or not np.isfinite(zone_high) or zone_low <= 0:
                    continue
                mid_idx = (z0 + z1) // 2
                zone_mid = (zone_low + zone_high) / 2
                zone_width_pct = (zone_high - zone_low) / zone_mid * 100 if zone_mid > 0 else float("inf")
                zone_bars = z1 - z0 + 1
                if direction == "LONG":
                    if zone_low - entry_price > min_gap:
                        add_candidate("squeeze_resistance", zone_low, zone_low, zone_high, mid_idx,
                                      zone_width_pct, zone_bars)
                else:
                    if entry_price - zone_high > min_gap:
                        add_candidate("squeeze_support", zone_high, zone_low, zone_high, mid_idx,
                                      zone_width_pct, zone_bars)

            if not candidates:
                return target
            candidates.sort(key=lambda x: (
                x["_zone_width_pct"], -x["_zone_bars"], x["target_r"], x["target_zone_bars_ago"]
            ))
            best = candidates[0]
            best.pop("_zone_width_pct", None)
            best.pop("_zone_bars", None)
            for k in ("target_zone_price", "target_zone_low", "target_zone_high",
                      "target_r", "target_distance_pct"):
                best[k] = round(float(best[k]), 6 if k != "target_r" else 4)
            return best
        except Exception as e:
            self._log.warning(f"目标区计算异常: {e}")
            return target

    def _refresh_target_metrics(self, pos):
        try:
            if str(getattr(pos, "target_zone_type", "") or "").startswith("swing_"):
                pos.target_zone_type = "none"
                pos.target_zone_price = 0.0
                pos.target_zone_low = 0.0
                pos.target_zone_high = 0.0
                pos.target_zone_bars_ago = 0
                pos.target_r = 0.0
                pos.target_distance_pct = 0.0
                return pos
            entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
            stop = float(
                getattr(pos, "initial_sl", 0.0)
                or getattr(pos, "sl_price", 0.0)
                or getattr(pos, "current_sl", 0.0)
                or 0.0
            )
            target = float(getattr(pos, "target_zone_price", 0.0) or 0.0)
            risk_unit = abs(entry - stop)
            direction = str(getattr(pos, "direction", "") or "").upper()
            distance = target - entry if direction == "LONG" else entry - target
            if entry <= 0 or target <= 0 or risk_unit <= 0 or distance <= 0:
                pos.target_r = 0.0
                pos.target_distance_pct = 0.0
                return pos
            pos.target_r = round(distance / risk_unit, 4)
            pos.target_distance_pct = round(distance / entry * 100, 6)
        except Exception:
            pos.target_r = 0.0
            pos.target_distance_pct = 0.0
        return pos

    def _rj_filter_mode(self) -> str:
        raw = str(getattr(self.cfg, "rj_entry_filter", "off") or "off").strip().lower().replace("-", "_")
        aliases = {
            "0": "off",
            "false": "off",
            "disabled": "off",
            "disable": "off",
            "none": "off",
            "log": "log_only",
            "logonly": "log_only",
            "observe": "log_only",
            "soft_filter": "soft",
            "hard_filter": "hard",
        }
        return aliases.get(raw, raw if raw in ("off", "log_only", "soft", "hard") else "off")

    @staticmethod
    def _tv_rma(series: pd.Series, period: int) -> pd.Series:
        """TradingView-style ta.rma: SMA seed, then Wilder smoothing."""
        p = max(1, int(period) if period else 1)
        s = pd.to_numeric(series, errors="coerce").reset_index(drop=True)
        if p <= 1:
            return s.copy()
        out = pd.Series(np.nan, index=s.index, dtype=float)
        seed_vals = []
        prev = np.nan
        for i, raw in enumerate(s.to_numpy(dtype=float)):
            if not np.isfinite(raw):
                continue
            if not np.isfinite(prev):
                seed_vals.append(float(raw))
                if len(seed_vals) >= p:
                    prev = float(np.mean(seed_vals[-p:]))
                    out.iloc[i] = prev
                continue
            prev = (prev * (p - 1) + float(raw)) / p
            out.iloc[i] = prev
        return out

    def _rj_ma(self, series: pd.Series, period: int, ma_type: Optional[str] = None) -> pd.Series:
        p = max(1, int(period) if period else 1)
        s = pd.to_numeric(series, errors="coerce").reset_index(drop=True)
        kind = str(ma_type or getattr(self.cfg, "rj_kd_ma_type", "sma") or "sma").strip().lower()
        if kind in ("rma", "wilder", "wilder_rma"):
            return self._tv_rma(s, p)
        if p <= 1:
            return s.copy()
        return s.rolling(p).mean()

    def _rj_slow_mode(self) -> str:
        raw = str(getattr(self.cfg, "rj_slow_line_mode", "k") or "k").strip().lower()
        raw = raw.replace("-", "_").replace(" ", "_")
        aliases = {
            "kline": "k",
            "k_line": "k",
            "k_scaled": "k",
            "k线": "k",
            "k线(贴近原版)": "k",
            "r": "rsi",
            "rsi9": "rsi",
            "purple": "k",
            "紫线": "k",
            "普通rsi": "rsi",
            "stochrsi": "stoch_rsi",
            "stoch_rsi": "stoch_rsi",
            "随机rsi": "stoch_rsi",
            "jrsi": "j_rsi",
            "j_rsi": "j_rsi",
            "j线rsi": "j_rsi",
        }
        return aliases.get(raw, raw if raw in ("k", "rsi", "stoch_rsi", "j_rsi") else "k")

    def _rj_rsi_series(self, source: pd.Series, period: int, smooth: int) -> pd.Series:
        src = pd.to_numeric(source, errors="coerce").reset_index(drop=True)
        delta = src.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = self._tv_rma(gain, period)
        avg_loss = self._tv_rma(loss, period)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi = rsi.where(avg_loss != 0, 100.0)
        rsi = rsi.where(avg_gain != 0, 0.0)
        rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0).fillna(50.0)
        return rsi if smooth <= 1 else rsi.ewm(span=smooth, adjust=False).mean()

    def _compute_rj_entry_state(self, df, direction: str) -> dict:
        """RJ entry confirmation using the same J/purple-line口径 as TradingView."""
        state = {
            "rj_available": False,
            "rj_reason": "not_computed",
            "rj_current_ok": False,
            "rj_recent_cross": False,
            "rj_cross_bars_ago": None,
        }
        if df is None or len(df) < 30:
            state["rj_reason"] = "kline_insufficient"
            return state
        try:
            kdj_len = max(1, int(getattr(self.cfg, "rj_kdj_len", 9) or 9))
            k_smooth = max(1, int(getattr(self.cfg, "rj_k_smooth", 3) or 3))
            d_smooth = max(1, int(getattr(self.cfg, "rj_d_smooth", 3) or 3))
            rsi_len = max(1, int(getattr(self.cfg, "rj_rsi_len", 9) or 9))
            rsi_smooth = max(1, int(getattr(self.cfg, "rj_rsi_smooth", 1) or 1))
            lookback = max(1, int(getattr(self.cfg, "rj_cross_lookback_bars", 8) or 8))
            min_spread = max(0.0, float(getattr(self.cfg, "rj_min_jr_spread", 0.0) or 0.0))

            close = pd.to_numeric(df["c"], errors="coerce").reset_index(drop=True)
            lines = self._compute_rj_lines(df)
            if not lines:
                state["rj_reason"] = "rj_lines_unavailable"
                return state
            j_line = lines["j"]
            r_line = lines["r"]
            line_params = lines.get("params", {})

            j_last = float(j_line.iloc[-1])
            r_last = float(r_line.iloc[-1])
            spread = j_last - r_last
            if not np.isfinite(j_last) or not np.isfinite(r_last):
                state["rj_reason"] = "rj_nan"
                return state

            cross_up = (j_line.shift(1) <= r_line.shift(1)) & (j_line > r_line)
            cross_down = (j_line.shift(1) >= r_line.shift(1)) & (j_line < r_line)
            cross_series = cross_up if direction == "LONG" else cross_down
            tail = cross_series.tail(lookback).fillna(False).to_numpy()
            hit_idx = np.where(tail)[0]
            recent_cross = len(hit_idx) > 0
            bars_ago = int(len(tail) - 1 - hit_idx[-1]) if recent_cross else None
            cross_abs_idx = int(len(close) - len(tail) + hit_idx[-1]) if recent_cross else None
            cross_time = None
            last_kline_time = None
            try:
                if "ot" in df.columns:
                    last_kline_time = int(float(df["ot"].iloc[-1]))
                    if cross_abs_idx is not None and 0 <= cross_abs_idx < len(df):
                        cross_time = int(float(df["ot"].iloc[cross_abs_idx]))
            except Exception:
                cross_time = None

            if direction == "LONG":
                current_ok = spread >= min_spread
                cross_type = "golden"
                required = "J>=R"
            else:
                current_ok = -spread >= min_spread
                cross_type = "death"
                required = "J<=R"

            state.update({
                "rj_available": True,
                "rj_reason": "ok",
                "rj_j": round(j_last, 4),
                "rj_r": round(r_last, 4),
                "rj_spread": round(spread, 4),
                "rj_bg": "bull" if j_last >= r_last else "bear",
                "rj_cross_type": cross_type,
                "rj_current_ok": bool(current_ok),
                "rj_recent_cross": bool(recent_cross),
                "rj_cross_bars_ago": bars_ago,
                "rj_cross_time": cross_time,
                "rj_last_kline_time": last_kline_time,
                "rj_cross_lookback_bars": lookback,
                "rj_min_jr_spread": min_spread,
                "rj_required": required,
                "rj_kdj_len": kdj_len,
                "rj_k_smooth": k_smooth,
                "rj_d_smooth": d_smooth,
                "rj_rsi_len": rsi_len,
                "rj_rsi_smooth": rsi_smooth,
                **line_params,
            })
            return state
        except Exception as e:
            state["rj_reason"] = f"error:{e}"
            return state

    def _apply_rj_entry_filter(self, df, symbol: str, direction: str,
                               interval: str, signal: dict) -> tuple[bool, dict]:
        """Return (allow_entry, RJ state). hard=recent cross + current same side; soft=current same side."""
        mode = self._rj_filter_mode()
        state = self._compute_rj_entry_state(df, direction)
        state["rj_filter_mode"] = mode

        if mode == "off":
            state["rj_rule_pass"] = True
            state["rj_filter_pass"] = True
            state["rj_filter_reason"] = "disabled"
            signal.update(state)
            return True, state

        soft_pass = bool(state.get("rj_available") and state.get("rj_current_ok"))
        hard_pass = bool(soft_pass and state.get("rj_recent_cross"))
        rule_pass = hard_pass if mode in ("hard", "log_only") else soft_pass
        state["rj_rule_pass"] = bool(rule_pass)
        state["rj_filter_pass"] = bool(rule_pass or mode == "log_only")
        if not state.get("rj_available"):
            state["rj_filter_reason"] = state.get("rj_reason", "unavailable")
        elif mode in ("hard", "log_only") and not state.get("rj_recent_cross"):
            state["rj_filter_reason"] = "no_recent_rj_cross"
        elif not state.get("rj_current_ok"):
            state["rj_filter_reason"] = "rj_not_same_direction"
        else:
            state["rj_filter_reason"] = "pass"

        signal.update(state)
        if mode == "log_only":
            return True, state
        return bool(rule_pass), state

    def _entry_signal_source(self) -> str:
        raw = str(getattr(self.cfg, "entry_signal_source", "structure") or "structure").strip().lower().replace("-", "_")
        aliases = {
            "rj": "rj_only",
            "rjonly": "rj_only",
            "rj_only_demo": "rj_only",
            "demo_rj": "rj_only",
            "structure_rj": "structure",
            "squeeze": "structure",
            "breakout": "structure",
            "predicta": "predicta_ewo",
        }
        return aliases.get(raw, raw if raw in ("structure", "rj_only", "predicta_ewo") else "structure")

    def _compute_rj_lines(self, df) -> Optional[dict]:
        if df is None or len(df) < 30:
            return None
        try:
            kdj_len = max(1, int(getattr(self.cfg, "rj_kdj_len", 9) or 9))
            k_smooth = max(1, int(getattr(self.cfg, "rj_k_smooth", 3) or 3))
            d_smooth = max(1, int(getattr(self.cfg, "rj_d_smooth", 3) or 3))
            rsi_len = max(1, int(getattr(self.cfg, "rj_rsi_len", 9) or 9))
            rsi_smooth = max(1, int(getattr(self.cfg, "rj_rsi_smooth", 1) or 1))
            kd_ma_type = str(getattr(self.cfg, "rj_kd_ma_type", "sma") or "sma").strip().lower()
            slow_mode = self._rj_slow_mode()
            slow_scale = float(getattr(self.cfg, "rj_slow_line_scale", 0.88) or 0.88)
            slow_offset = float(getattr(self.cfg, "rj_slow_line_offset", 0.0) or 0.0)
            slow_clamp = bool(getattr(self.cfg, "rj_slow_line_clamp", True))

            high = pd.to_numeric(df["h"], errors="coerce").reset_index(drop=True)
            low = pd.to_numeric(df["l"], errors="coerce").reset_index(drop=True)
            close = pd.to_numeric(df["c"], errors="coerce").reset_index(drop=True)

            low_n = low.rolling(kdj_len).min()
            high_n = high.rolling(kdj_len).max()
            den = (high_n - low_n).replace(0, np.nan)
            rsv = ((close - low_n) / den * 100.0).fillna(50.0)
            k_line = self._rj_ma(rsv, k_smooth, kd_ma_type)
            d_line = self._rj_ma(k_line, d_smooth, kd_ma_type)
            j_line = 3.0 * k_line - 2.0 * d_line

            rsi_line = self._rj_rsi_series(close, rsi_len, rsi_smooth)
            rsi_low = rsi_line.rolling(rsi_len).min()
            rsi_high = rsi_line.rolling(rsi_len).max()
            stoch_rsi = ((rsi_line - rsi_low) / (rsi_high - rsi_low).replace(0, np.nan) * 100.0).fillna(50.0)
            j_rsi = self._rj_rsi_series(j_line.fillna(50.0), rsi_len, rsi_smooth)
            if slow_mode == "rsi":
                r_base = rsi_line
            elif slow_mode == "stoch_rsi":
                r_base = stoch_rsi
            elif slow_mode == "j_rsi":
                r_base = j_rsi
            else:
                r_base = k_line
            r_line = 50.0 + (r_base - 50.0) * slow_scale + slow_offset
            if slow_clamp:
                r_line = r_line.clip(lower=0.0, upper=100.0)
            return {
                "j": j_line,
                "r": r_line,
                "k": k_line,
                "d": d_line,
                "r_base": r_base,
                "params": {
                    "rj_kd_ma_type": kd_ma_type,
                    "rj_slow_line_mode": slow_mode,
                    "rj_slow_line_scale": round(slow_scale, 6),
                    "rj_slow_line_offset": round(slow_offset, 6),
                    "rj_slow_line_clamp": slow_clamp,
                },
            }
        except Exception:
            return None

    @staticmethod
    def _rj_original_level_triggers(j_line) -> tuple:
        """Original RJ triggers: J recovers above 0 for LONG, falls back below 100 for SHORT."""
        j = pd.to_numeric(j_line, errors="coerce").reset_index(drop=True)
        long_sig = pd.Series(False, index=j.index)
        short_sig = pd.Series(False, index=j.index)
        armed_long = False
        armed_short = False
        for i, v in enumerate(j):
            try:
                val = float(v)
            except Exception:
                continue
            if not np.isfinite(val):
                continue
            if armed_long and val >= 0.0:
                long_sig.iloc[i] = True
                armed_long = False
            if armed_short and val <= 100.0:
                short_sig.iloc[i] = True
                armed_short = False
            if val < 0.0:
                armed_long = True
            if val > 100.0:
                armed_short = True
        return long_sig, short_sig

    def _rj_only_volume_state(self, df, idx: Optional[int] = None, anchor: str = "signal_key") -> dict:
        """Relative volume filter anchored to the RJ signal key candle."""
        enabled = bool(getattr(self.cfg, "rj_only_volume_filter", True))
        vol_len = max(1, int(getattr(self.cfg, "rj_only_volume_len", 20) or 20))
        vol_mult = max(0.0, float(getattr(self.cfg, "rj_only_volume_mult", 1.1) or 0.0))
        state = {
            "rj_volume_filter_enabled": enabled,
            "rj_volume_filter_pass": True,
            "rj_volume_reason": "disabled",
            "rj_volume_len": vol_len,
            "rj_volume_mult": vol_mult,
        }
        if not enabled:
            return state
        try:
            if df is None or "v" not in df.columns:
                state.update({"rj_volume_filter_pass": False, "rj_volume_reason": "volume_missing"})
                return state
            vols = pd.to_numeric(df["v"], errors="coerce").reset_index(drop=True)
            n = len(vols)
            idx = n - 1 if idx is None else int(idx)
            if idx <= 0 or idx >= n:
                state.update({"rj_volume_filter_pass": False, "rj_volume_reason": "volume_idx_invalid"})
                return state
            start = max(0, idx - vol_len)
            hist = vols.iloc[start:idx].replace([np.inf, -np.inf], np.nan).dropna()
            if len(hist) < min(vol_len, 5):
                state.update({"rj_volume_filter_pass": False, "rj_volume_reason": "volume_history_insufficient"})
                return state
            cur_vol = float(vols.iloc[idx])
            avg_vol = float(hist.mean())
            if not np.isfinite(cur_vol) or not np.isfinite(avg_vol) or avg_vol <= 0:
                state.update({"rj_volume_filter_pass": False, "rj_volume_reason": "volume_invalid"})
                return state
            ratio = cur_vol / avg_vol
            pass_filter = ratio >= vol_mult
            state.update({
                "rj_volume_filter_pass": bool(pass_filter),
                "rj_volume_reason": "pass" if pass_filter else "volume_ratio_low",
                "rj_volume": round(cur_vol, 8),
                "rj_volume_ma": round(avg_vol, 8),
                "rj_volume_ratio": round(ratio, 4),
                "rj_volume_bar": idx,
                "rj_volume_anchor": anchor,
            })
            return state
        except Exception as e:
            state.update({"rj_volume_filter_pass": False, "rj_volume_reason": f"volume_error:{e}"})
            return state

    def _rj_only_live_volume_state(self, symbol: str, interval: str) -> dict:
        vol_len = max(1, int(getattr(self.cfg, "rj_only_volume_len", 20) or 20))
        try:
            df = fetch_klines(symbol, interval, max(30, vol_len + 5), exchange=self.cfg.exchange, closed_only=False)
            state = self._rj_only_volume_state(df)
            state["rj_volume_checked_live"] = True
            return state
        except Exception as e:
            state = self._rj_only_volume_state(None)
            state.update({"rj_volume_checked_live": True, "rj_volume_reason": f"live_volume_error:{e}"})
            return state

    def _rj_only_sr_divergence_state(self, df, j_line, direction: str, idx: int) -> dict:
        """RJ-only support/resistance + J-line divergence filter at the setup candle."""
        enabled = bool(getattr(self.cfg, "rj_only_sr_filter", True))
        state = {
            "rj_sr_filter_enabled": enabled,
            "rj_sr_filter_pass": True,
            "rj_sr_reason": "disabled",
        }
        if not enabled:
            return state
        try:
            df = df.reset_index(drop=True)
            n = len(df)
            idx = int(idx)
            if idx < 0 or idx >= n:
                state.update({"rj_sr_filter_pass": False, "rj_sr_reason": "idx_out_of_range"})
                return state

            left = max(1, int(getattr(self.cfg, "rj_only_sr_pivot_left", 5) or 5))
            right = max(1, int(getattr(self.cfg, "rj_only_sr_pivot_right", 3) or 3))
            div_lookback = max(1, int(getattr(self.cfg, "rj_only_div_lookback_bars", 80) or 80))
            require_div = bool(getattr(self.cfg, "rj_only_require_divergence", True))
            use_early_div = bool(getattr(self.cfg, "rj_only_use_early_divergence", True))
            near_mode = str(getattr(self.cfg, "rj_only_sr_near_mode", "atr_or_pct") or "atr_or_pct").strip().lower()
            near_mode = near_mode.replace("-", "_").replace(" ", "_")
            if near_mode in ("atrpct", "both", "or", "atr_or_percent"):
                near_mode = "atr_or_pct"
            elif near_mode in ("percent", "percentage"):
                near_mode = "pct"
            elif near_mode not in ("atr_or_pct", "atr", "pct"):
                near_mode = "atr_or_pct"
            near_atr_mult = max(0.0, float(getattr(self.cfg, "rj_only_sr_near_atr_mult", 0.8) or 0.0))
            near_pct = max(0.0, float(getattr(self.cfg, "rj_only_sr_near_pct", 0.8) or 0.0))
            atr_period = max(2, int(getattr(self.cfg, "atr_trail_period", 14) or 14))

            high = pd.to_numeric(df["h"], errors="coerce").reset_index(drop=True).to_numpy(dtype=float)
            low = pd.to_numeric(df["l"], errors="coerce").reset_index(drop=True).to_numpy(dtype=float)
            close = pd.to_numeric(df["c"], errors="coerce").reset_index(drop=True).to_numpy(dtype=float)
            j_arr = pd.to_numeric(j_line, errors="coerce").reset_index(drop=True).to_numpy(dtype=float)
            if idx < left + right or idx >= len(j_arr):
                state.update({"rj_sr_filter_pass": False, "rj_sr_reason": "sr_history_insufficient"})
                return state

            prev_close = np.roll(close, 1)
            tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
            tr[0] = high[0] - low[0]
            atr_arr = pd.Series(tr).rolling(atr_period).mean().bfill().fillna(0.0).to_numpy(dtype=float)

            last_support = np.nan
            last_support_bar = None
            last_resistance = np.nan
            last_resistance_bar = None
            prev_low_price = np.nan
            prev_low_j = np.nan
            prev_high_price = np.nan
            prev_high_j = np.nan
            last_bull_div_bar = None
            last_bear_div_bar = None

            max_pivot = idx - right
            for pivot_i in range(left, max_pivot + 1):
                lo = float(low[pivot_i])
                hi = float(high[pivot_i])
                jv = float(j_arr[pivot_i])
                if not (np.isfinite(lo) and np.isfinite(hi) and np.isfinite(jv)):
                    continue
                low_window = low[pivot_i - left:pivot_i + right + 1]
                high_window = high[pivot_i - left:pivot_i + right + 1]
                if len(low_window) == left + right + 1 and lo <= float(np.nanmin(low_window)):
                    if np.isfinite(prev_low_price) and np.isfinite(prev_low_j) and lo < prev_low_price and jv > prev_low_j:
                        last_bull_div_bar = pivot_i
                    last_support = lo
                    last_support_bar = pivot_i
                    prev_low_price = lo
                    prev_low_j = jv
                if len(high_window) == left + right + 1 and hi >= float(np.nanmax(high_window)):
                    if np.isfinite(prev_high_price) and np.isfinite(prev_high_j) and hi > prev_high_price and jv < prev_high_j:
                        last_bear_div_bar = pivot_i
                    last_resistance = hi
                    last_resistance_bar = pivot_i
                    prev_high_price = hi
                    prev_high_j = jv

            price = float(close[idx])
            atr_val = float(atr_arr[idx]) if idx < len(atr_arr) and np.isfinite(atr_arr[idx]) else 0.0
            support_dist_pct = (
                abs(float(low[idx]) - last_support) / price * 100.0
                if price > 0 and np.isfinite(last_support) else np.nan
            )
            resistance_dist_pct = (
                abs(float(high[idx]) - last_resistance) / price * 100.0
                if price > 0 and np.isfinite(last_resistance) else np.nan
            )
            support_near_atr = np.isfinite(last_support) and atr_val > 0 and abs(float(low[idx]) - last_support) <= atr_val * near_atr_mult
            resistance_near_atr = np.isfinite(last_resistance) and atr_val > 0 and abs(float(high[idx]) - last_resistance) <= atr_val * near_atr_mult
            support_near_pct = np.isfinite(support_dist_pct) and support_dist_pct <= near_pct
            resistance_near_pct = np.isfinite(resistance_dist_pct) and resistance_dist_pct <= near_pct
            if near_mode == "atr":
                long_near = bool(support_near_atr)
                short_near = bool(resistance_near_atr)
            elif near_mode == "pct":
                long_near = bool(support_near_pct)
                short_near = bool(resistance_near_pct)
            else:
                long_near = bool(support_near_atr or support_near_pct)
                short_near = bool(resistance_near_atr or resistance_near_pct)

            current_j = float(j_arr[idx])
            early_bull = bool(
                use_early_div and np.isfinite(prev_low_price) and np.isfinite(prev_low_j)
                and float(low[idx]) < prev_low_price and current_j > prev_low_j
            )
            early_bear = bool(
                use_early_div and np.isfinite(prev_high_price) and np.isfinite(prev_high_j)
                and float(high[idx]) > prev_high_price and current_j < prev_high_j
            )
            bull_recent = last_bull_div_bar is not None and idx - int(last_bull_div_bar) <= div_lookback
            bear_recent = last_bear_div_bar is not None and idx - int(last_bear_div_bar) <= div_lookback
            bull_active = bool(bull_recent or early_bull)
            bear_active = bool(bear_recent or early_bear)
            long_div_ok = (not require_div) or bull_active
            short_div_ok = (not require_div) or bear_active
            if direction == "LONG":
                passed = bool(long_near and long_div_ok)
                if not long_near:
                    reason = "not_near_support"
                elif not long_div_ok:
                    reason = "no_bull_divergence"
                else:
                    reason = "pass"
            else:
                passed = bool(short_near and short_div_ok)
                if not short_near:
                    reason = "not_near_resistance"
                elif not short_div_ok:
                    reason = "no_bear_divergence"
                else:
                    reason = "pass"

            state.update({
                "rj_sr_filter_pass": passed,
                "rj_sr_reason": reason,
                "rj_sr_near_mode": near_mode,
                "rj_sr_support": round(float(last_support), 8) if np.isfinite(last_support) else None,
                "rj_sr_resistance": round(float(last_resistance), 8) if np.isfinite(last_resistance) else None,
                "rj_sr_support_bar": last_support_bar,
                "rj_sr_resistance_bar": last_resistance_bar,
                "rj_sr_support_dist_pct": round(float(support_dist_pct), 4) if np.isfinite(support_dist_pct) else None,
                "rj_sr_resistance_dist_pct": round(float(resistance_dist_pct), 4) if np.isfinite(resistance_dist_pct) else None,
                "rj_sr_near_support": long_near,
                "rj_sr_near_resistance": short_near,
                "rj_sr_require_divergence": require_div,
                "rj_sr_bull_div": bull_active,
                "rj_sr_bear_div": bear_active,
                "rj_sr_bull_div_recent": bool(bull_recent),
                "rj_sr_bear_div_recent": bool(bear_recent),
                "rj_sr_early_bull_div": early_bull,
                "rj_sr_early_bear_div": early_bear,
                "rj_sr_last_bull_div_bar": last_bull_div_bar,
                "rj_sr_last_bear_div_bar": last_bear_div_bar,
                "rj_sr_pivot_left": left,
                "rj_sr_pivot_right": right,
            })
            return state
        except Exception as e:
            state.update({"rj_sr_filter_pass": False, "rj_sr_reason": f"sr_exception:{str(e)[:60]}"})
            return state

    def _choppy_filter_state(
        self,
        df,
        anchor_idx: int,
        mode: str,
        evaluator=evaluate_choppy_market_adaptive,
    ) -> dict:
        mode = str(mode or "off").strip().lower()
        if mode not in ("off", "log_only", "hard"):
            mode = "off"
        if mode == "off":
            return {
                "choppy_filter_mode": mode,
                "choppy_filter_anchor": "signal_key",
                "choppy_filter_available": False,
                "choppy_filter_is_choppy": False,
                "choppy_filter_reason": "disabled",
                "choppy_filter_reasons": [],
            }
        state = evaluator(df, anchor_idx=anchor_idx)
        state["choppy_filter_mode"] = mode
        state["choppy_filter_anchor"] = "signal_key"
        return state

    def _rj_choppy_filter_state(self, df, anchor_idx: int) -> dict:
        return self._choppy_filter_state(
            df, anchor_idx, getattr(self.cfg, "rj_choppy_filter_mode", "off")
        )

    def _predicta_choppy_filter_state(self, df, anchor_idx: int) -> dict:
        return self._choppy_filter_state(
            df,
            anchor_idx,
            getattr(self.cfg, "predicta_choppy_filter_mode", "hard"),
            evaluator=evaluate_predicta_choppy_market,
        )

    def _predicta_params(self) -> PredictaParams:
        return PredictaParams(
            ewo_fast=max(1, int(getattr(self.cfg, "predicta_ewo_fast", 5) or 5)),
            ewo_slow=max(1, int(getattr(self.cfg, "predicta_ewo_slow", 35) or 35)),
            confirm_bars=max(1, int(getattr(self.cfg, "predicta_confirm_bars", 6) or 6)),
            confirm_atr_buffer=max(0.0, float(getattr(self.cfg, "predicta_confirm_atr_buffer", 0.08) or 0.0)),
            stop_atr_mult=max(0.0, float(getattr(self.cfg, "predicta_stop_atr_mult", 0.5) or 0.0)),
        )

    def _predicta_normalize_stop(self, direction: str, entry_price: float, raw_stop: float) -> Optional[float]:
        entry_price = float(entry_price or 0.0)
        raw_stop = float(raw_stop or 0.0)
        if entry_price <= 0:
            return None
        min_pct = max(0.0, float(getattr(self.cfg, "predicta_min_stop_pct", 0.003) or 0.0))
        max_pct = max(min_pct, float(getattr(self.cfg, "predicta_max_stop_pct", 0.08) or 0.08))
        if direction == "LONG":
            stop = min(raw_stop, entry_price * (1.0 - min_pct))
            distance = entry_price - stop
        else:
            stop = max(raw_stop, entry_price * (1.0 + min_pct))
            distance = stop - entry_price
        stop_pct = distance / entry_price
        if stop <= 0 or stop_pct <= 0 or stop_pct > max_pct:
            return None
        return float(stop)

    def _predicta_candidates_from_df(
        self, symbol: str, interval: str, df,
        signal_idx: Optional[int] = None, lines=None,
    ) -> tuple[list, list]:
        """Build a fast signal or waiting setup from the latest closed candle."""
        if df is None or len(df) < 40:
            return [], []
        frame = df.reset_index(drop=True)
        params = self._predicta_params()
        lines = compute_predicta(frame, params) if lines is None else lines
        signal_idx = len(lines) - 1 if signal_idx is None else int(signal_idx)
        direction = ""
        if bool(lines["bull_signal"].iloc[signal_idx]):
            direction = "LONG"
        elif bool(lines["bear_signal"].iloc[signal_idx]):
            direction = "SHORT"
        if not direction:
            return [], []
        choppy = self._predicta_choppy_filter_state(frame, signal_idx)
        if choppy.get("choppy_filter_mode") == "hard" and choppy.get("choppy_filter_is_choppy"):
            return [], []
        raw_ewo = lines["ewo"].iloc[signal_idx]
        signal_ewo = float(raw_ewo) if pd.notna(raw_ewo) else 0.0
        setup = make_predicta_setup(
            symbol, direction, interval, frame, signal_idx, signal_ewo, choppy, params
        )
        setup["price"] = float(frame["c"].iloc[signal_idx])
        setup["score"] = 100.0
        setup["retest"] = (
            "Predicta信号K EWO同向"
            if setup["predicta_entry_path"] == "fast"
            else "Predicta等待突破+EWO"
        )
        if setup["predicta_entry_path"] != "fast":
            return [], [setup]
        atr_value = float(self._calc_atr(frame.iloc[:signal_idx + 1], 14) or 0.0)
        raw_stop = (
            float(setup["predicta_key_low"]) - atr_value * params.stop_atr_mult
            if direction == "LONG"
            else float(setup["predicta_key_high"]) + atr_value * params.stop_atr_mult
        )
        stop = self._predicta_normalize_stop(direction, setup["price"], raw_stop)
        if stop is None:
            return [], []
        setup.update({
            "predicta_atr": atr_value,
            "predicta_stop_price": round(stop, 8),
            "predicta_stop_anchor": "signal_key",
            "fractal_sl": float(
                setup["predicta_key_low"] if direction == "LONG" else setup["predicta_key_high"]
            ),
            "band_sl": round(stop, 8),
        })
        return [setup], []

    def _predicta_confirmed_signal(self, setup: dict, df) -> Optional[dict]:
        """Evaluate one waiting setup against the latest closed candle."""
        if df is None or len(df) < 40:
            return None
        frame = df.reset_index(drop=True)
        params = self._predicta_params()
        atr_value = float(self._calc_atr(frame, 14) or 0.0)
        decision = evaluate_predicta_setup(setup, frame, params, atr_value)
        if decision.status != "confirmed":
            return None
        stop = self._predicta_normalize_stop(
            str(setup.get("direction", "")), decision.confirm_price, decision.stop_price
        )
        if stop is None:
            return None
        signal = dict(setup)
        signal.update({
            "price": float(decision.confirm_price),
            "score": 100.0,
            "source_strategy": "predicta_ewo",
            "predicta_entry_path": "wait",
            "predicta_confirm_time": int(decision.confirm_time or 0),
            "predicta_confirm_ewo": float(decision.ewo),
            "predicta_confirm_reason": decision.reason,
            "predicta_confirm_age_bars": int(decision.age_bars),
            "predicta_atr": atr_value,
            "predicta_stop_price": round(stop, 8),
            "predicta_stop_anchor": "signal_key",
            "fractal_sl": float(
                setup.get("predicta_key_low", 0.0)
                if setup.get("direction") == "LONG"
                else setup.get("predicta_key_high", 0.0)
            ),
            "band_sl": round(stop, 8),
            "retest": "Predicta关键K突破 + EWO确认",
        })
        return signal

    def _predicta_signal_from_df(self, symbol: str, interval: str, df) -> Optional[dict]:
        """Stateless adapter used by replay and live restart recovery."""
        if df is None or len(df) < 40:
            return None
        frame = df.reset_index(drop=True)
        current_idx = len(frame) - 1
        confirm_bars = max(1, int(getattr(self.cfg, "predicta_confirm_bars", 6) or 6))
        lines = compute_predicta(frame, self._predicta_params())
        for signal_idx in range(current_idx, max(-1, current_idx - confirm_bars - 1), -1):
            fast, waiting = self._predicta_candidates_from_df(
                symbol, interval, frame, signal_idx=signal_idx, lines=lines
            )
            if fast:
                if signal_idx == current_idx:
                    return fast[0]
                continue
            if waiting:
                setup = waiting[0]
                for decision_idx in range(signal_idx + 1, current_idx + 1):
                    decision_frame = frame.iloc[:decision_idx + 1].copy()
                    atr_value = float(self._calc_atr(decision_frame, 14) or 0.0)
                    decision = evaluate_predicta_setup(
                        setup, decision_frame, self._predicta_params(), atr_value
                    )
                    if decision.status == "confirmed":
                        if decision_idx != current_idx:
                            return None
                        return self._predicta_confirmed_signal(setup, decision_frame)
                    if decision.status in ("invalidated", "timeout"):
                        return None
        return None

    def _sync_predicta_setup_pool(self, setups: list) -> None:
        now_ts = time.time()
        max_pool = max(1, int(getattr(self.cfg, "predicta_setup_max_pool", 40) or 40))
        for raw in setups or []:
            key = str(raw.get("signal_key", "") or "")
            symbol = str(raw.get("symbol", "") or "")
            if not key or not symbol or self._any_signal_key_used([key]):
                continue
            if any(position.symbol == symbol for position in self.positions):
                continue
            existing = self._predicta_setup_pool.get(key, {})
            item = dict(existing)
            item.update(raw)
            item["predicta_setup_first_seen_ts"] = float(
                existing.get("predicta_setup_first_seen_ts", now_ts) or now_ts
            )
            self._predicta_setup_pool[key] = item
        while len(self._predicta_setup_pool) > max_pool:
            oldest_key = min(
                self._predicta_setup_pool,
                key=lambda key: float(
                    self._predicta_setup_pool[key].get("predicta_setup_first_seen_ts", now_ts) or now_ts
                ),
            )
            self._predicta_setup_pool.pop(oldest_key, None)

    def _check_predicta_setup_pool(self) -> list:
        """Return newly confirmed signals and remove terminal waiting setups."""
        if self._entry_signal_source() != "predicta_ewo" or not self._predicta_setup_pool:
            return []
        confirmed = []
        params = self._predicta_params()
        for key, setup in list(self._predicta_setup_pool.items()):
            symbol = str(setup.get("symbol", "") or "")
            interval = str(setup.get("source_interval", self.cfg.scan_interval) or self.cfg.scan_interval)
            if not symbol or any(position.symbol == symbol for position in self.positions):
                self._predicta_setup_pool.pop(key, None)
                continue
            try:
                frame = fetch_klines(
                    symbol, interval, 160,
                    exchange=self.cfg.exchange,
                    closed_only=True,
                )
                if frame is None or len(frame) < 40:
                    continue
                atr_value = float(self._calc_atr(frame, 14) or 0.0)
                decision = evaluate_predicta_setup(setup, frame, params, atr_value)
                if decision.status == "confirmed":
                    signal = self._predicta_confirmed_signal(setup, frame)
                    self._predicta_setup_pool.pop(key, None)
                    if signal is not None:
                        confirmed.append(signal)
                elif decision.status in ("invalidated", "timeout"):
                    self._predicta_setup_pool.pop(key, None)
            except Exception as exc:
                self._log.warning(f"Predicta候选池检查异常 {symbol} {interval}: {exc}")
        return confirmed

    def _rj_only_signal_from_df(self, symbol: str, interval: str, df) -> Optional[dict]:
        """RJ-only demo entry: RJ cross -> key candle -> latest close confirms key break."""
        if df is None or len(df) < 60:
            return None
        try:
            df = df.reset_index(drop=True)
            lines = self._compute_rj_lines(df)
            if not lines:
                return None
            j_line = lines["j"]
            r_line = lines["r"]
            line_params = lines.get("params", {})
            close = pd.to_numeric(df["c"], errors="coerce").reset_index(drop=True)
            high = pd.to_numeric(df["h"], errors="coerce").reset_index(drop=True)
            low = pd.to_numeric(df["l"], errors="coerce").reset_index(drop=True)
            confirm_idx = len(df) - 1
            if confirm_idx < 2:
                return None

            j_last = float(j_line.iloc[confirm_idx])
            r_last = float(r_line.iloc[confirm_idx])
            if not np.isfinite(j_last) or not np.isfinite(r_last):
                return None
            spread = j_last - r_last
            min_spread = max(0.0, float(getattr(self.cfg, "rj_min_jr_spread", 0.0) or 0.0))
            confirm_bars = max(1, int(getattr(self.cfg, "rj_only_confirm_bars", 6) or 6))
            start_idx = max(1, confirm_idx - confirm_bars)
            cross_up = (j_line.shift(1) <= r_line.shift(1)) & (j_line > r_line)
            cross_down = (j_line.shift(1) >= r_line.shift(1)) & (j_line < r_line)
            level_up, level_down = self._rj_original_level_triggers(j_line)
            atr_val = max(0.0, float(self._calc_atr(df, getattr(self.cfg, "atr_trail_period", 14)) or 0.0))
            atr_mult = max(0.0, float(getattr(self.cfg, "rj_only_atr_sl_mult", 0.5) or 0.0))
            confirm_atr_buffer = max(0.0, float(getattr(self.cfg, "rj_only_confirm_atr_buffer", 0.08) or 0.0))
            max_chase_pct = max(0.0, float(getattr(self.cfg, "rj_only_setup_max_chase_pct", 2.0) or 0.0))
            invalidate_on_opposite = bool(getattr(self.cfg, "rj_only_invalidate_on_opposite_break", True))
            min_stop_pct = max(0.0, float(getattr(self.cfg, "rj_only_min_stop_pct", 0.003) or 0.0))
            max_stop_pct = max(min_stop_pct, float(getattr(self.cfg, "rj_only_max_stop_pct", 0.08) or 0.08))
            entry_price = float(close.iloc[confirm_idx])
            if entry_price <= 0:
                return None

            def bar_time(idx: int):
                try:
                    return int(float(df["ot"].iloc[idx])) if "ot" in df.columns else idx
                except Exception:
                    return idx

            candidates = []
            for direction, cross_series, level_series in (
                ("LONG", cross_up.fillna(False), level_up.fillna(False)),
                ("SHORT", cross_down.fillna(False), level_down.fillna(False)),
            ):
                direction_candidates = []
                for source_name, source_series, is_level_source in (
                    ("level", level_series, True),
                    ("cross_fallback", cross_series, False),
                ):
                    for cross_idx in range(confirm_idx - 1, start_idx - 1, -1):
                        if not bool(source_series.iloc[cross_idx]):
                            continue
                        is_level = bool(is_level_source)
                        is_cross = not is_level
                        if not is_level:
                            direction_ok = spread >= min_spread if direction == "LONG" else -spread >= min_spread
                            if not direction_ok:
                                continue
                        trigger_source = (
                            "j0_recover" if direction == "LONG" and is_level else
                            "j100_recover" if direction == "SHORT" and is_level else
                            "jr_cross_fallback"
                        )
                        key_high = float(high.iloc[cross_idx])
                        key_low = float(low.iloc[cross_idx])
                        if key_high <= 0 or key_low <= 0:
                            continue
                        prior_closes = close.iloc[cross_idx + 1:confirm_idx]
                        if direction == "LONG":
                            confirm_level = key_high + atr_val * confirm_atr_buffer
                            invalidated = bool((prior_closes < key_low).any()) if invalidate_on_opposite else False
                            already_confirmed = bool((prior_closes > confirm_level).any())
                            if invalidated or already_confirmed or entry_price <= confirm_level:
                                continue
                            chase_pct = (entry_price - confirm_level) / confirm_level * 100.0 if confirm_level > 0 else 0.0
                            if max_chase_pct > 0 and chase_pct > max_chase_pct:
                                continue
                            sl_price = key_low - atr_val * atr_mult
                            if sl_price <= 0:
                                sl_price = key_low * 0.995
                            if entry_price - sl_price < entry_price * min_stop_pct:
                                sl_price = entry_price * (1.0 - min_stop_pct)
                            stop_pct = (entry_price - sl_price) / entry_price
                        else:
                            confirm_level = key_low - atr_val * confirm_atr_buffer
                            invalidated = bool((prior_closes > key_high).any()) if invalidate_on_opposite else False
                            already_confirmed = bool((prior_closes < confirm_level).any())
                            if invalidated or already_confirmed or entry_price >= confirm_level:
                                continue
                            chase_pct = (confirm_level - entry_price) / confirm_level * 100.0 if confirm_level > 0 else 0.0
                            if max_chase_pct > 0 and chase_pct > max_chase_pct:
                                continue
                            sl_price = key_high + atr_val * atr_mult
                            if sl_price - entry_price < entry_price * min_stop_pct:
                                sl_price = entry_price * (1.0 + min_stop_pct)
                            stop_pct = (sl_price - entry_price) / entry_price
                        if stop_pct <= 0 or stop_pct > max_stop_pct:
                            continue
                        choppy_state = self._rj_choppy_filter_state(df, cross_idx)
                        if (
                            choppy_state.get("choppy_filter_mode") == "hard"
                            and choppy_state.get("choppy_filter_is_choppy", False)
                        ):
                            continue
                        volume_state = self._rj_only_volume_state(df, cross_idx, anchor="signal_key")
                        if not volume_state.get("rj_volume_filter_pass", True):
                            continue
                        sr_state = self._rj_only_sr_divergence_state(df, j_line, direction, cross_idx)
                        if not sr_state.get("rj_sr_filter_pass", True):
                            continue
                        bars_ago = confirm_idx - cross_idx
                        score = max(float(self.cfg.min_score) + 1.0, 70.0)
                        score += max(0.0, min(12.0, abs(spread) / 3.0))
                        score += max(0.0, min(8.0, confirm_bars - bars_ago + 1))
                        signal_key = (
                            f"RJ|{symbol}|{direction}|{interval}|"
                            f"{bar_time(cross_idx)}|{bar_time(confirm_idx)}|{self._norm_signal_num(sl_price)}"
                        )
                        setup_key = (
                            f"RJSETUP|{symbol}|{direction}|{interval}|"
                            f"{bar_time(cross_idx)}|{self._norm_signal_num(sl_price)}"
                        )
                        retest_text = (
                            "RJ J线回0关键K突破" if direction == "LONG" and is_level else
                            "RJ J线回100关键K跌破" if direction == "SHORT" and is_level else
                            "RJ金叉兜底关键K突破" if direction == "LONG" else
                            "RJ死叉兜底关键K跌破"
                        )
                        direction_candidates.append({
                            "symbol": symbol,
                            "direction": direction,
                            "price": entry_price,
                            "score": round(score, 2),
                            "source_interval": interval,
                            "source_strategy": "rj_only",
                            "retest": retest_text,
                            "signal_key": signal_key,
                            "rj_setup_key": setup_key,
                            "fractal_sl": key_low if direction == "LONG" else key_high,
                            "band_sl": sl_price,
                            "rj_filter_mode": "rj_only",
                            "rj_filter_pass": True,
                            "rj_rule_pass": True,
                            "rj_filter_reason": "rj_only_key_break",
                            "rj_available": True,
                            "rj_reason": "ok",
                            "rj_j": round(j_last, 4),
                            "rj_r": round(r_last, 4),
                            "rj_spread": round(spread, 4),
                            "rj_bg": "bull" if j_last >= r_last else "bear",
                            "rj_current_ok": True,
                            "rj_recent_cross": True,
                            "rj_cross_type": "golden" if direction == "LONG" else "death",
                            "rj_trigger_source": trigger_source,
                            "rj_trigger_is_level": is_level,
                            "rj_trigger_is_cross": is_cross,
                            "rj_trigger_priority": source_name,
                            "rj_cross_bars_ago": bars_ago,
                            "rj_cross_time": bar_time(cross_idx),
                            "rj_last_kline_time": bar_time(confirm_idx),
                            "rj_required": "key_high_break" if direction == "LONG" else "key_low_break",
                            "rj_only_key_bar": cross_idx,
                            "rj_only_confirm_bar": confirm_idx,
                            "rj_only_key_time": bar_time(cross_idx),
                            "rj_only_confirm_time": bar_time(confirm_idx),
                            "rj_only_key_high": round(key_high, 8),
                            "rj_only_key_low": round(key_low, 8),
                            "rj_only_stop_anchor": "signal_key",
                            "rj_only_confirm_close": round(entry_price, 8),
                            "rj_only_confirm_level": round(confirm_level, 8),
                            "rj_only_stop_price": round(sl_price, 8),
                            "rj_only_stop_pct": round(stop_pct * 100.0, 4),
                            "rj_only_atr": round(atr_val, 8),
                            "rj_only_atr_sl_mult": atr_mult,
                            "rj_entry_chase_pct": round(chase_pct, 4),
                            "rj_only_setup_max_chase_pct": round(max_chase_pct, 4),
                            "rj_only_confirm_atr_buffer": confirm_atr_buffer,
                            "rj_only_invalidate_on_opposite_break": invalidate_on_opposite,
                            "rj_only_confirm_bars": confirm_bars,
                            **choppy_state,
                            **line_params,
                            **volume_state,
                            **sr_state,
                        })
                        break
                    if direction_candidates:
                        break
                candidates.extend(direction_candidates)
            if not candidates:
                return None
            for item in candidates:
                stats = self._rj_only_history_stats(df, item["direction"])
                item.update(stats)
                stats_enabled = bool(getattr(self.cfg, "rj_only_stats_enabled", True))
                if not stats_enabled:
                    item["rj_only_stats_pass"] = True
                    item["rj_only_stats_reason"] = "disabled"
                else:
                    samples = int(item.get("rj_only_hist_samples", 0) or 0)
                    win_rate = float(item.get("rj_only_hist_win_rate", 0.0) or 0.0)
                    avg_r = float(item.get("rj_only_hist_avg_r", 0.0) or 0.0)
                    min_samples = max(1, int(getattr(self.cfg, "rj_only_stats_min_samples", 8) or 8))
                    min_win = max(0.0, float(getattr(self.cfg, "rj_only_stats_min_win_rate", 52.0) or 52.0))
                    min_avg = float(getattr(self.cfg, "rj_only_stats_min_avg_r", 0.0) or 0.0)
                    pass_stats = samples >= min_samples and win_rate >= min_win and avg_r >= min_avg
                    item["rj_only_stats_pass"] = bool(pass_stats)
                    if samples < min_samples:
                        item["rj_only_stats_reason"] = "hist_samples_insufficient"
                    elif win_rate < min_win:
                        item["rj_only_stats_reason"] = "hist_win_rate_low"
                    elif avg_r < min_avg:
                        item["rj_only_stats_reason"] = "hist_avg_r_low"
                    else:
                        item["rj_only_stats_reason"] = "pass"
                item["score"] = self._score_rj_only_signal(item)
                item["rj_only_score_source"] = "hist_win_rate"
            candidates.sort(key=lambda x: x["score"], reverse=True)
            best = candidates[0]
            return best
        except Exception as e:
            self._log.warning(f"RJ-only信号计算异常 {symbol} {interval}: {e}")
            return None

    def _rj_only_setup_from_df(self, symbol: str, interval: str, df) -> Optional[dict]:
        """Find an unconfirmed RJ key-candle setup for the realtime trigger pool."""
        if df is None or len(df) < 60:
            return None
        try:
            df = df.reset_index(drop=True)
            lines = self._compute_rj_lines(df)
            if not lines:
                return None
            j_line = lines["j"]
            r_line = lines["r"]
            line_params = lines.get("params", {})
            close = pd.to_numeric(df["c"], errors="coerce").reset_index(drop=True)
            high = pd.to_numeric(df["h"], errors="coerce").reset_index(drop=True)
            low = pd.to_numeric(df["l"], errors="coerce").reset_index(drop=True)
            watch_idx = len(df) - 1
            if watch_idx < 2:
                return None

            j_last = float(j_line.iloc[watch_idx])
            r_last = float(r_line.iloc[watch_idx])
            if not np.isfinite(j_last) or not np.isfinite(r_last):
                return None
            spread = j_last - r_last
            min_spread = max(0.0, float(getattr(self.cfg, "rj_min_jr_spread", 0.0) or 0.0))
            confirm_bars = max(1, int(getattr(self.cfg, "rj_only_confirm_bars", 6) or 6))
            start_idx = max(1, watch_idx - confirm_bars)
            cross_up = (j_line.shift(1) <= r_line.shift(1)) & (j_line > r_line)
            cross_down = (j_line.shift(1) >= r_line.shift(1)) & (j_line < r_line)
            level_up, level_down = self._rj_original_level_triggers(j_line)
            atr_val = max(0.0, float(self._calc_atr(df, getattr(self.cfg, "atr_trail_period", 14)) or 0.0))
            atr_mult = max(0.0, float(getattr(self.cfg, "rj_only_atr_sl_mult", 0.5) or 0.0))
            confirm_atr_buffer = max(0.0, float(getattr(self.cfg, "rj_only_confirm_atr_buffer", 0.08) or 0.0))
            max_chase_pct = max(0.0, float(getattr(self.cfg, "rj_only_setup_max_chase_pct", 2.0) or 0.0))
            invalidate_on_opposite = bool(getattr(self.cfg, "rj_only_invalidate_on_opposite_break", True))
            min_stop_pct = max(0.0, float(getattr(self.cfg, "rj_only_min_stop_pct", 0.003) or 0.0))
            max_stop_pct = max(min_stop_pct, float(getattr(self.cfg, "rj_only_max_stop_pct", 0.08) or 0.08))
            latest_close = float(close.iloc[watch_idx])
            if latest_close <= 0:
                return None

            def bar_time(idx: int):
                try:
                    return int(float(df["ot"].iloc[idx])) if "ot" in df.columns else idx
                except Exception:
                    return idx

            setups = []
            for direction, cross_series, level_series in (
                ("LONG", cross_up.fillna(False), level_up.fillna(False)),
                ("SHORT", cross_down.fillna(False), level_down.fillna(False)),
            ):
                direction_setups = []
                for source_name, source_series, is_level_source in (
                    ("level", level_series, True),
                    ("cross_fallback", cross_series, False),
                ):
                    for cross_idx in range(watch_idx, start_idx - 1, -1):
                        if not bool(source_series.iloc[cross_idx]):
                            continue
                        is_level = bool(is_level_source)
                        is_cross = not is_level
                        if not is_level:
                            direction_ok = spread >= min_spread if direction == "LONG" else -spread >= min_spread
                            if not direction_ok:
                                continue
                        trigger_source = (
                            "j0_recover" if direction == "LONG" and is_level else
                            "j100_recover" if direction == "SHORT" and is_level else
                            "jr_cross_fallback"
                        )
                        key_high = float(high.iloc[cross_idx])
                        key_low = float(low.iloc[cross_idx])
                        if key_high <= 0 or key_low <= 0:
                            continue
                        after_closes = close.iloc[cross_idx + 1:watch_idx + 1]
                        if direction == "LONG":
                            confirm_level = key_high + atr_val * confirm_atr_buffer
                            invalidated = bool((after_closes < key_low).any()) if invalidate_on_opposite else False
                            already_confirmed = bool((after_closes > confirm_level).any())
                            if invalidated or already_confirmed:
                                continue
                            sl_price = key_low - atr_val * atr_mult
                            if sl_price <= 0:
                                sl_price = key_low * 0.995
                            if confirm_level - sl_price < confirm_level * min_stop_pct:
                                sl_price = confirm_level * (1.0 - min_stop_pct)
                            stop_pct = (confirm_level - sl_price) / confirm_level if confirm_level > 0 else 0.0
                        else:
                            confirm_level = key_low - atr_val * confirm_atr_buffer
                            invalidated = bool((after_closes > key_high).any()) if invalidate_on_opposite else False
                            already_confirmed = bool((after_closes < confirm_level).any())
                            if invalidated or already_confirmed:
                                continue
                            sl_price = key_high + atr_val * atr_mult
                            if sl_price - confirm_level < confirm_level * min_stop_pct:
                                sl_price = confirm_level * (1.0 + min_stop_pct)
                            stop_pct = (sl_price - confirm_level) / confirm_level if confirm_level > 0 else 0.0
                        if stop_pct <= 0 or stop_pct > max_stop_pct:
                            continue
                        choppy_state = self._rj_choppy_filter_state(df, cross_idx)
                        if (
                            choppy_state.get("choppy_filter_mode") == "hard"
                            and choppy_state.get("choppy_filter_is_choppy", False)
                        ):
                            continue
                        volume_state = self._rj_only_volume_state(df, cross_idx, anchor="signal_key")
                        if not volume_state.get("rj_volume_filter_pass", True):
                            continue
                        sr_state = self._rj_only_sr_divergence_state(df, j_line, direction, cross_idx)
                        if not sr_state.get("rj_sr_filter_pass", True):
                            continue
                        bars_ago = watch_idx - cross_idx
                        setup_key = (
                            f"RJSETUP|{symbol}|{direction}|{interval}|"
                            f"{bar_time(cross_idx)}|{self._norm_signal_num(sl_price)}"
                        )
                        retest_text = (
                            "RJ J线回0关键K候选等待实时突破" if direction == "LONG" and is_level else
                            "RJ J线回100关键K候选等待实时跌破" if direction == "SHORT" and is_level else
                            "RJ金叉兜底候选等待实时突破" if direction == "LONG" else
                            "RJ死叉兜底候选等待实时跌破"
                        )
                        setup = {
                        "symbol": symbol,
                        "direction": direction,
                        "price": latest_close,
                        "score": max(float(self.cfg.min_score) + 1.0, 70.0),
                        "source_interval": interval,
                        "source_strategy": "rj_only",
                        "retest": retest_text,
                        "signal_key": setup_key,
                        "rj_setup_key": setup_key,
                        "fractal_sl": key_low if direction == "LONG" else key_high,
                        "band_sl": sl_price,
                        "rj_filter_mode": "rj_only",
                        "rj_filter_pass": True,
                        "rj_rule_pass": True,
                        "rj_filter_reason": "rj_only_setup_wait_trigger",
                        "rj_available": True,
                        "rj_reason": "ok",
                        "rj_j": round(j_last, 4),
                        "rj_r": round(r_last, 4),
                        "rj_spread": round(spread, 4),
                        "rj_bg": "bull" if j_last >= r_last else "bear",
                        "rj_current_ok": True,
                        "rj_recent_cross": True,
                        "rj_cross_type": "golden" if direction == "LONG" else "death",
                        "rj_trigger_source": trigger_source,
                        "rj_trigger_is_level": is_level,
                        "rj_trigger_is_cross": is_cross,
                        "rj_trigger_priority": source_name,
                        "rj_cross_bars_ago": bars_ago,
                        "rj_cross_time": bar_time(cross_idx),
                        "rj_last_kline_time": bar_time(watch_idx),
                        "rj_required": "live_key_high_break" if direction == "LONG" else "live_key_low_break",
                        "rj_only_key_bar": cross_idx,
                        "rj_only_confirm_bar": "live",
                        "rj_only_key_time": bar_time(cross_idx),
                        "rj_only_confirm_time": "",
                        "rj_only_key_high": round(key_high, 8),
                        "rj_only_key_low": round(key_low, 8),
                        "rj_only_confirm_close": round(latest_close, 8),
                        "rj_only_confirm_level": round(confirm_level, 8),
                        "rj_only_stop_price": round(sl_price, 8),
                        "rj_only_stop_pct": round(stop_pct * 100.0, 4),
                        "rj_only_atr": round(atr_val, 8),
                        "rj_only_atr_sl_mult": atr_mult,
                        "rj_only_confirm_atr_buffer": confirm_atr_buffer,
                        "rj_only_invalidate_on_opposite_break": invalidate_on_opposite,
                        "rj_only_confirm_bars": confirm_bars,
                        "rj_setup_trigger_price": round(confirm_level, 8),
                        "rj_setup_live_price": round(latest_close, 8),
                        "rj_setup_confirm_mode": str(getattr(self.cfg, "rj_only_setup_confirm_mode", "near_close") or "near_close"),
                        "rj_setup_close_confirm_sec": int(getattr(self.cfg, "rj_only_setup_close_confirm_sec", 45) or 45),
                        "rj_setup_trigger_hold_sec": int(getattr(self.cfg, "rj_only_setup_trigger_hold_sec", 10) or 0),
                        **choppy_state,
                        **line_params,
                        **volume_state,
                        **sr_state,
                        }
                        stats = self._rj_only_history_stats(df, direction)
                        setup.update(stats)
                        stats_enabled = bool(getattr(self.cfg, "rj_only_stats_enabled", True))
                        if not stats_enabled:
                            setup["rj_only_stats_pass"] = True
                            setup["rj_only_stats_reason"] = "disabled"
                        else:
                            samples = int(setup.get("rj_only_hist_samples", 0) or 0)
                            win_rate = float(setup.get("rj_only_hist_win_rate", 0.0) or 0.0)
                            avg_r = float(setup.get("rj_only_hist_avg_r", 0.0) or 0.0)
                            min_samples = max(1, int(getattr(self.cfg, "rj_only_stats_min_samples", 8) or 8))
                            min_win = max(0.0, float(getattr(self.cfg, "rj_only_stats_min_win_rate", 52.0) or 52.0))
                            min_avg = float(getattr(self.cfg, "rj_only_stats_min_avg_r", 0.0) or 0.0)
                            pass_stats = samples >= min_samples and win_rate >= min_win and avg_r >= min_avg
                            setup["rj_only_stats_pass"] = bool(pass_stats)
                            if samples < min_samples:
                                setup["rj_only_stats_reason"] = "hist_samples_insufficient"
                            elif win_rate < min_win:
                                setup["rj_only_stats_reason"] = "hist_win_rate_low"
                            elif avg_r < min_avg:
                                setup["rj_only_stats_reason"] = "hist_avg_r_low"
                            else:
                                setup["rj_only_stats_reason"] = "pass"
                        setup["score"] = self._score_rj_only_signal(setup)
                        setup["rj_only_score_source"] = "hist_win_rate"
                        if setup.get("rj_only_stats_pass", True) and setup.get("score", 0) >= self.cfg.min_score:
                            direction_setups.append(setup)
                            break
                    if direction_setups:
                        break
                setups.extend(direction_setups)
            if not setups:
                return None
            setups.sort(key=lambda x: x.get("score", 0), reverse=True)
            return setups[0]
        except Exception as e:
            self._log.warning(f"RJ-only候选计算异常 {symbol} {interval}: {e}")
            return None

    @staticmethod
    def _epoch_from_bar_time(value) -> float:
        try:
            v = float(value)
            if v > 100000000000:
                return v / 1000.0
            if v > 1000000000:
                return v
        except Exception:
            pass
        return 0.0

    def _rj_setup_expiry_ts(self, setup: dict, now_ts: Optional[float] = None) -> float:
        now_ts = now_ts or time.time()
        interval = str(setup.get("source_interval") or getattr(self.cfg, "scan_interval", "30m")).split(",")[0].strip() or "30m"
        sec = self._interval_seconds(interval)
        confirm_bars = max(1, int(setup.get("rj_only_confirm_bars", getattr(self.cfg, "rj_only_confirm_bars", 6)) or 6))
        start_ts = self._epoch_from_bar_time(setup.get("rj_cross_time") or setup.get("rj_only_key_time"))
        if start_ts > 0:
            return start_ts + (confirm_bars + 1) * sec
        bars_ago = max(0, int(setup.get("rj_cross_bars_ago", 0) or 0))
        return now_ts + max(sec, (confirm_bars - bars_ago + 1) * sec)

    def _format_setup_ts(self, ts: float) -> str:
        try:
            return (datetime.fromtimestamp(float(ts), timezone.utc) + timedelta(hours=8)).replace(microsecond=0).isoformat()
        except Exception:
            return ""

    def _seconds_to_interval_close(self, interval: str, now_ts: Optional[float] = None) -> int:
        try:
            now_ts = float(now_ts or time.time())
            sec = max(60, int(self._interval_seconds(interval)))
            remaining = sec - int(now_ts % sec)
            return 0 if remaining == sec else remaining
        except Exception:
            return 0

    def _live_trade_symbol_set(self) -> Optional[set]:
        if self.client is None or str(getattr(self.cfg, "mode", "") or "").lower() == "paper":
            return None
        if str(getattr(self.cfg, "exchange", "") or "").lower() != "binance":
            return None
        if str(getattr(self.cfg, "market_type", "") or "").lower() != "futures":
            return None
        getter = getattr(self.client, "get_trading_symbols", None)
        if not callable(getter):
            return None
        try:
            symbols = getter()
            return set(symbols) if symbols else None
        except Exception as e:
            self._log.warning(f"RJ可交易合约过滤读取失败: {e}")
            return None

    def _is_live_trade_symbol(self, symbol: str) -> bool:
        tradable = self._live_trade_symbol_set()
        if tradable is None:
            return True
        return str(symbol or "") in tradable

    def _filter_live_trade_symbols(self, symbols: list) -> list:
        items = [str(s or "") for s in symbols or [] if s]
        tradable = self._live_trade_symbol_set()
        if tradable is None:
            return items
        filtered = [s for s in items if s in tradable]
        skipped = len(items) - len(filtered)
        if skipped > 0:
            now_ts = time.time()
            last_ts = float(getattr(self, "_last_live_symbol_filter_log_ts", 0.0) or 0.0)
            if now_ts - last_ts >= 300:
                self._last_live_symbol_filter_log_ts = now_ts
                self._log.info(f"RJ live合约过滤: 跳过{skipped}个不可交易币种, 保留{len(filtered)}个")
        return filtered

    def _sync_rj_setup_pool(self, setups: list, btc_fields: dict):
        if not bool(getattr(self.cfg, "rj_only_setup_pool_enabled", True)):
            if self._rj_setup_pool:
                self._log.info(f"RJ候选池关闭, 清空{len(self._rj_setup_pool)}个候选")
            self._rj_setup_pool = {}
            return
        now_ts = time.time()
        max_pool = max(1, int(getattr(self.cfg, "rj_only_setup_max_pool", 40) or 40))
        for raw in setups or []:
            symbol = str(raw.get("symbol", "") or "")
            if symbol and not self._is_live_trade_symbol(symbol):
                continue
            key = str(raw.get("rj_setup_key") or raw.get("signal_key") or "")
            if not key:
                continue
            if self._any_signal_key_used([key]):
                continue
            if any(p.symbol == raw.get("symbol") for p in self.positions):
                continue
            expires_at = self._rj_setup_expiry_ts(raw, now_ts)
            if expires_at <= now_ts:
                continue
            existing = self._rj_setup_pool.get(key, {})
            item = dict(existing)
            item.update(raw)
            item["signal_key"] = key
            item["rj_setup_key"] = key
            item["rj_setup_first_seen_ts"] = float(existing.get("rj_setup_first_seen_ts", now_ts) or now_ts)
            item["rj_setup_first_seen"] = existing.get("rj_setup_first_seen") or bj_now().isoformat()
            item["rj_setup_expires_at_ts"] = expires_at
            item["rj_setup_expires_at"] = self._format_setup_ts(expires_at)
            item["rj_setup_trigger_started_ts"] = float(existing.get("rj_setup_trigger_started_ts", 0.0) or 0.0)
            item["rj_setup_trigger_started_at"] = existing.get("rj_setup_trigger_started_at", "")
            item["rj_setup_last_near_ts"] = float(existing.get("rj_setup_last_near_ts", 0.0) or 0.0)
            self._rj_setup_pool[key] = item
            if not existing:
                self._log.info(
                    f"RJ候选入池: {raw.get('symbol')} {raw.get('direction')} "
                    f"{raw.get('source_interval')} trigger={raw.get('rj_setup_trigger_price')}"
                )
                self._append_signal_event("rj_setup_add", raw.get("symbol", ""), self._signal_snapshot(item, {
                    "pool_size": len(self._rj_setup_pool),
                    **btc_fields,
                }))
                if item.get("choppy_filter_mode") == "log_only" and item.get("choppy_filter_is_choppy"):
                    self._append_signal_event(
                        "rj_choppy_shadow",
                        raw.get("symbol", ""),
                        self._signal_snapshot(item, {"source_event": "rj_setup_add", **btc_fields}),
                    )
        while len(self._rj_setup_pool) > max_pool:
            oldest_key, oldest = min(
                self._rj_setup_pool.items(),
                key=lambda kv: float(kv[1].get("rj_setup_first_seen_ts", now_ts) or now_ts),
            )
            self._append_signal_event("rj_setup_timeout", oldest.get("symbol", ""), self._signal_snapshot(oldest, {
                "reason": "pool_overflow",
                "pool_size": len(self._rj_setup_pool),
                **btc_fields,
            }))
            self._rj_setup_pool.pop(oldest_key, None)

    def _get_rj_setup_live_price(self, symbol: str, interval: str) -> Optional[float]:
        """Return the latest closed candle close for RJ setup confirmation.

        Candidate setups are shape-based. Using ticker/current-candle prices can
        turn a temporary wick into a false key-candle break.
        """
        try:
            df = fetch_klines(symbol, interval, 2, exchange=self.cfg.exchange, closed_only=True)
            if df is not None and len(df) > 0:
                return float(df["c"].iloc[-1])
        except Exception:
            pass
        return None

    def _get_rj_setup_closed_kline(self, symbol: str, interval: str) -> Optional[dict]:
        try:
            df = fetch_klines(symbol, interval, 2, exchange=self.cfg.exchange, closed_only=True)
            if df is None or len(df) <= 0:
                return None
            row = df.iloc[-1]
            return {
                "open": float(row.get("o", 0.0) or 0.0),
                "high": float(row.get("h", 0.0) or 0.0),
                "low": float(row.get("l", 0.0) or 0.0),
                "close": float(row.get("c", 0.0) or 0.0),
                "ot": int(float(row.get("ot", 0) or 0)) if "ot" in df.columns else 0,
            }
        except Exception:
            return None

    def _check_rj_setup_pool(self):
        if self._entry_signal_source() != "rj_only":
            return
        if not bool(getattr(self.cfg, "rj_only_setup_pool_enabled", True)):
            return
        if not self._rj_setup_pool:
            return
        now_ts = time.time()
        check_interval = max(60, int(getattr(self.cfg, "rj_only_setup_check_interval_sec", 60) or 60))
        if now_ts - float(getattr(self, "_last_rj_setup_check_ts", 0.0) or 0.0) < check_interval:
            return
        self._last_rj_setup_check_ts = now_ts
        try:
            btc_fields = self._btc_regime_fields()
        except Exception:
            btc_fields = {"btc_regime": "btc_unknown", "btc_score": 0}
        confirm_mode = str(getattr(self.cfg, "rj_only_setup_confirm_mode", "near_close") or "near_close").lower()
        close_confirm_sec = max(0, int(getattr(self.cfg, "rj_only_setup_close_confirm_sec", 45) or 0))
        hold_sec = max(0, int(getattr(self.cfg, "rj_only_setup_trigger_hold_sec", 10) or 0))
        near_pct = max(0.0, float(getattr(self.cfg, "rj_only_setup_near_pct", 0.15) or 0.0))
        for key, setup in list(self._rj_setup_pool.items()):
            symbol = str(setup.get("symbol", "") or "")
            direction = str(setup.get("direction", "") or "")
            interval = str(setup.get("source_interval") or getattr(self.cfg, "scan_interval", "30m")).split(",")[0].strip() or "30m"
            if not symbol or direction not in ("LONG", "SHORT"):
                self._rj_setup_pool.pop(key, None)
                continue
            if not self._is_live_trade_symbol(symbol):
                self._append_signal_event("rj_setup_invalidated", symbol, self._signal_snapshot(setup, {
                    "reason": "symbol_unavailable",
                    "pool_size": len(self._rj_setup_pool),
                    **btc_fields,
                }))
                self._rj_setup_pool.pop(key, None)
                continue
            if now_ts >= float(setup.get("rj_setup_expires_at_ts", 0.0) or 0.0):
                self._append_signal_event("rj_setup_timeout", symbol, self._signal_snapshot(setup, {
                    "reason": "expired",
                    "pool_size": len(self._rj_setup_pool),
                    **btc_fields,
                }))
                self._rj_setup_pool.pop(key, None)
                continue
            if any(p.symbol == symbol for p in self.positions):
                self._rj_setup_pool.pop(key, None)
                continue
            if len(self.positions) >= self.cfg.max_positions:
                break
            closed_bar = self._get_rj_setup_closed_kline(symbol, interval)
            price = float((closed_bar or {}).get("close", 0.0) or 0.0)
            if price is None or price <= 0:
                continue
            setup["rj_setup_live_price"] = round(float(price), 8)
            trigger_price = float(setup.get("rj_setup_trigger_price") or setup.get("rj_only_confirm_level") or 0.0)
            key_high = float(setup.get("rj_only_key_high") or 0.0)
            key_low = float(setup.get("rj_only_key_low") or 0.0)
            if trigger_price <= 0 or key_high <= 0 or key_low <= 0:
                self._rj_setup_pool.pop(key, None)
                continue
            if direction == "LONG" and price < key_low:
                self._append_signal_event("rj_setup_invalidated", symbol, self._signal_snapshot(setup, {
                    "reason": "live_opposite_break",
                    "live_price": round(price, 8),
                    **btc_fields,
                }))
                self._rj_setup_pool.pop(key, None)
                continue
            if direction == "SHORT" and price > key_high:
                self._append_signal_event("rj_setup_invalidated", symbol, self._signal_snapshot(setup, {
                    "reason": "live_opposite_break",
                    "live_price": round(price, 8),
                    **btc_fields,
                }))
                self._rj_setup_pool.pop(key, None)
                continue

            distance_pct = abs(price - trigger_price) / trigger_price * 100.0
            if near_pct > 0 and distance_pct <= near_pct and now_ts - float(setup.get("rj_setup_last_near_ts", 0.0) or 0.0) >= 60:
                setup["rj_setup_last_near_ts"] = now_ts
                self._append_signal_event("rj_setup_near_trigger", symbol, self._signal_snapshot(setup, {
                    "live_price": round(price, 8),
                    "distance_pct": round(distance_pct, 4),
                    "pool_size": len(self._rj_setup_pool),
                    **btc_fields,
                }))

            triggered = price >= trigger_price if direction == "LONG" else price <= trigger_price
            if not triggered:
                if setup.get("rj_setup_trigger_started_ts"):
                    setup["rj_setup_trigger_started_ts"] = 0.0
                    setup["rj_setup_trigger_started_at"] = ""
                continue
            max_chase_pct = max(0.0, float(getattr(self.cfg, "rj_only_setup_max_chase_pct", 2.0) or 0.0))
            chase_pct = (
                (price - trigger_price) / trigger_price * 100.0
                if direction == "LONG" else
                (trigger_price - price) / trigger_price * 100.0
            )
            if max_chase_pct > 0 and chase_pct > max_chase_pct:
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(setup, {
                    "reason": "rj_setup_chase_too_far",
                    "live_price": round(price, 8),
                    "trigger_price": round(trigger_price, 8),
                    "chase_pct": round(chase_pct, 4),
                    "max_chase_pct": round(max_chase_pct, 4),
                    **btc_fields,
                }))
                self._rj_setup_pool.pop(key, None)
                continue
            closed_kline_confirm = True
            seconds_to_close = 0 if closed_kline_confirm else self._seconds_to_interval_close(interval, now_ts)
            setup["rj_setup_seconds_to_close"] = int(seconds_to_close)
            setup["rj_setup_confirm_mode"] = "closed_close" if closed_kline_confirm else confirm_mode
            setup["rj_setup_close_confirm_sec"] = close_confirm_sec
            started_ts = float(setup.get("rj_setup_trigger_started_ts", 0.0) or 0.0)
            if started_ts <= 0:
                setup["rj_setup_trigger_started_ts"] = now_ts
                setup["rj_setup_trigger_started_at"] = bj_now().isoformat()
                self._append_signal_event("rj_setup_trigger_touch", symbol, self._signal_snapshot(setup, {
                    "live_price": round(price, 8),
                    "seconds_to_close": int(seconds_to_close),
                    "confirm_mode": confirm_mode,
                    "close_confirm_sec": close_confirm_sec,
                    "hold_sec": hold_sec,
                    "pool_size": len(self._rj_setup_pool),
                    **btc_fields,
                }))
                if not closed_kline_confirm and confirm_mode == "near_close" and seconds_to_close > close_confirm_sec:
                    continue
                if not closed_kline_confirm and confirm_mode == "hold" and hold_sec > 0:
                    continue
                started_ts = now_ts
            if closed_kline_confirm:
                pass
            elif confirm_mode == "near_close":
                if seconds_to_close > close_confirm_sec:
                    continue
            elif confirm_mode == "hold" and now_ts - started_ts < hold_sec:
                continue

            signal = dict(setup)
            signal["price"] = float(price)
            signal["signal_key"] = key
            signal["rj_setup_key"] = key
            signal["rj_filter_reason"] = "rj_setup_live_break"
            signal["retest"] = "RJ候选池实时突破" if direction == "LONG" else "RJ候选池实时跌破"
            atr_val = max(0.0, float(setup.get("rj_only_atr", 0.0) or 0.0))
            atr_mult = max(0.0, float(getattr(self.cfg, "rj_only_atr_sl_mult", 0.5) or 0.0))
            min_stop_pct = max(0.0, float(getattr(self.cfg, "rj_only_min_stop_pct", 0.003) or 0.0))
            max_stop_pct = max(min_stop_pct, float(getattr(self.cfg, "rj_only_max_stop_pct", 0.08) or 0.08))
            signal_key_high = float(setup.get("rj_only_key_high") or 0.0)
            signal_key_low = float(setup.get("rj_only_key_low") or 0.0)
            if signal_key_high <= 0 or signal_key_low <= 0:
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(setup, {
                    "reason": "rj_signal_key_missing",
                    "live_price": round(price, 8),
                    **btc_fields,
                }))
                self._rj_setup_pool.pop(key, None)
                continue
            if direction == "LONG":
                sl_price = signal_key_low - atr_val * atr_mult
                if sl_price <= 0:
                    sl_price = signal_key_low * 0.995
                if price - sl_price < price * min_stop_pct:
                    sl_price = price * (1.0 - min_stop_pct)
                stop_pct = (price - sl_price) / price if price > 0 else 0.0
                signal["fractal_sl"] = signal_key_low
            else:
                sl_price = signal_key_high + atr_val * atr_mult
                if sl_price - price < price * min_stop_pct:
                    sl_price = price * (1.0 + min_stop_pct)
                stop_pct = (sl_price - price) / price if price > 0 else 0.0
                signal["fractal_sl"] = signal_key_high
            if stop_pct <= 0 or stop_pct > max_stop_pct:
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "rj_confirm_key_stop_invalid",
                    "live_price": round(price, 8),
                    "sl": round(sl_price, 8),
                    "stop_pct": round(stop_pct * 100.0, 4),
                    "max_stop_pct": round(max_stop_pct * 100.0, 4),
                    **btc_fields,
                }))
                self._rj_setup_pool.pop(key, None)
                continue
            signal["band_sl"] = sl_price
            signal["rj_only_stop_price"] = round(sl_price, 8)
            signal["rj_only_stop_pct"] = round(stop_pct * 100.0, 4)
            signal["rj_only_stop_anchor"] = "signal_key"
            signal["rj_setup_chase_pct"] = round(chase_pct, 4)
            signal["rj_only_confirm_close"] = round(float(price), 8)
            signal["rj_only_confirm_time"] = bj_now().isoformat()
            signal["rj_setup_age_sec"] = round(now_ts - float(setup.get("rj_setup_first_seen_ts", now_ts) or now_ts), 2)
            self._append_signal_event("rj_setup_trigger", symbol, self._signal_snapshot(signal, {
                "live_price": round(price, 8),
                "seconds_to_close": int(seconds_to_close),
                "confirm_mode": confirm_mode,
                "close_confirm_sec": close_confirm_sec,
                "held_sec": round(now_ts - started_ts, 2),
                "pool_size": len(self._rj_setup_pool),
                **btc_fields,
            }))
            pos = self.enter_rj_position(signal)
            self._rj_setup_pool.pop(key, None)
            if pos is not None:
                self._log.info(f"RJ候选池触发开仓: {symbol} {direction} live={price:.8f}")

    def _rj_only_history_stats(self, df, direction: str) -> dict:
        """Backtest RJ key-candle confirmations on this symbol/interval before allowing a fresh demo trade."""
        empty = {
            "rj_only_hist_samples": 0,
            "rj_only_hist_wins": 0,
            "rj_only_hist_losses": 0,
            "rj_only_hist_win_rate": 0.0,
            "rj_only_hist_avg_r": 0.0,
            "rj_only_hist_profit_factor": 0.0,
        }
        try:
            lookback = max(80, int(getattr(self.cfg, "rj_only_stats_lookback_bars", 220) or 220))
            horizon = max(1, int(getattr(self.cfg, "rj_only_stats_horizon_bars", 12) or 12))
            target_r = max(0.2, float(getattr(self.cfg, "rj_only_stats_target_r", 1.0) or 1.0))
            confirm_bars = max(1, int(getattr(self.cfg, "rj_only_confirm_bars", 6) or 6))
            atr_period = max(2, int(getattr(self.cfg, "atr_trail_period", 14) or 14))
            atr_mult = max(0.0, float(getattr(self.cfg, "rj_only_atr_sl_mult", 0.5) or 0.0))
            confirm_atr_buffer = max(0.0, float(getattr(self.cfg, "rj_only_confirm_atr_buffer", 0.08) or 0.0))
            invalidate_on_opposite = bool(getattr(self.cfg, "rj_only_invalidate_on_opposite_break", True))
            min_stop_pct = max(0.0, float(getattr(self.cfg, "rj_only_min_stop_pct", 0.003) or 0.0))
            max_stop_pct = max(min_stop_pct, float(getattr(self.cfg, "rj_only_max_stop_pct", 0.08) or 0.08))
            min_spread = max(0.0, float(getattr(self.cfg, "rj_min_jr_spread", 0.0) or 0.0))
            dfx = df.tail(lookback).reset_index(drop=True)
            if dfx is None or len(dfx) < max(60, atr_period + horizon + confirm_bars + 5):
                return empty
            lines = self._compute_rj_lines(dfx)
            if not lines:
                return empty
            j_line = lines["j"]
            r_line = lines["r"]
            close = pd.to_numeric(dfx["c"], errors="coerce").reset_index(drop=True)
            high = pd.to_numeric(dfx["h"], errors="coerce").reset_index(drop=True)
            low = pd.to_numeric(dfx["l"], errors="coerce").reset_index(drop=True)
            h = high.to_numpy(dtype=float)
            l = low.to_numpy(dtype=float)
            c = close.to_numpy(dtype=float)
            prev_c = np.roll(c, 1)
            tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
            tr[0] = h[0] - l[0]
            atr_arr = pd.Series(tr).rolling(atr_period).mean().bfill().fillna(0.0).to_numpy(dtype=float)
            level_up, level_down = self._rj_original_level_triggers(j_line)
            if direction == "LONG":
                cross_series = ((j_line.shift(1) <= r_line.shift(1)) & (j_line > r_line)).fillna(False)
                level_series = level_up.fillna(False)
            else:
                cross_series = ((j_line.shift(1) >= r_line.shift(1)) & (j_line < r_line)).fillna(False)
                level_series = level_down.fillna(False)
            trigger = (cross_series | level_series).fillna(False).to_numpy()
            level_trigger = level_series.to_numpy()

            samples = []
            raw_triggers = 0
            confirmed_triggers = 0
            risk_ok_triggers = 0
            n = len(dfx)
            last_confirm = -999999
            for cross_idx in range(1, n - horizon - 1):
                if not bool(trigger[cross_idx]):
                    continue
                raw_triggers += 1
                if cross_idx <= last_confirm:
                    continue
                key_high = float(h[cross_idx])
                key_low = float(l[cross_idx])
                if key_high <= 0 or key_low <= 0:
                    continue
                confirm_idx = None
                max_confirm = min(n - horizon - 1, cross_idx + confirm_bars)
                for j in range(cross_idx + 1, max_confirm + 1):
                    spread = float(j_line.iloc[j] - r_line.iloc[j])
                    if direction == "LONG":
                        if invalidate_on_opposite and float(c[j]) < key_low:
                            confirm_idx = -1
                            break
                        if not bool(level_trigger[cross_idx]) and spread < min_spread:
                            continue
                        confirm_level = key_high + float(atr_arr[j]) * confirm_atr_buffer
                        if float(c[j]) > confirm_level:
                            confirm_idx = j
                            break
                    else:
                        if invalidate_on_opposite and float(c[j]) > key_high:
                            confirm_idx = -1
                            break
                        if not bool(level_trigger[cross_idx]) and -spread < min_spread:
                            continue
                        confirm_level = key_low - float(atr_arr[j]) * confirm_atr_buffer
                        if float(c[j]) < confirm_level:
                            confirm_idx = j
                            break
                if confirm_idx is None or confirm_idx < 0:
                    continue
                confirmed_triggers += 1
                entry = float(c[confirm_idx])
                if entry <= 0:
                    continue
                atr_val = float(atr_arr[confirm_idx]) if np.isfinite(atr_arr[confirm_idx]) else 0.0
                if direction == "LONG":
                    sl = key_low - atr_val * atr_mult
                    if sl <= 0:
                        sl = key_low * 0.995
                    if entry - sl < entry * min_stop_pct:
                        sl = entry * (1.0 - min_stop_pct)
                    risk = entry - sl
                    stop_pct = risk / entry
                    target = entry + risk * target_r
                else:
                    sl = key_high + atr_val * atr_mult
                    if sl - entry < entry * min_stop_pct:
                        sl = entry * (1.0 + min_stop_pct)
                    risk = sl - entry
                    stop_pct = risk / entry
                    target = entry - risk * target_r
                if risk <= 0 or stop_pct > max_stop_pct:
                    continue
                risk_ok_triggers += 1
                result_r = None
                end_idx = min(n - 1, confirm_idx + horizon)
                for k in range(confirm_idx + 1, end_idx + 1):
                    if direction == "LONG":
                        stop_hit = float(l[k]) <= sl
                        target_hit = float(h[k]) >= target
                    else:
                        stop_hit = float(h[k]) >= sl
                        target_hit = float(l[k]) <= target
                    if stop_hit:
                        result_r = -1.0
                        break
                    if target_hit:
                        result_r = target_r
                        break
                if result_r is None:
                    exit_close = float(c[end_idx])
                    result_r = (exit_close - entry) / risk if direction == "LONG" else (entry - exit_close) / risk
                samples.append(float(result_r))
                last_confirm = confirm_idx
            if not samples:
                empty.update({
                    "rj_only_hist_raw_triggers": raw_triggers,
                    "rj_only_hist_confirmed": confirmed_triggers,
                    "rj_only_hist_risk_ok": risk_ok_triggers,
                    "rj_only_hist_filter_mode": "core_rj",
                    "rj_only_hist_target_r": target_r,
                    "rj_only_hist_horizon_bars": horizon,
                    "rj_only_hist_lookback_bars": lookback,
                })
                return empty
            wins = sum(1 for x in samples if x > 0)
            losses = sum(1 for x in samples if x <= 0)
            gross_win = sum(x for x in samples if x > 0)
            gross_loss = abs(sum(x for x in samples if x < 0))
            pf = gross_win / gross_loss if gross_loss > 0 else (gross_win if gross_win > 0 else 0.0)
            return {
                "rj_only_hist_samples": len(samples),
                "rj_only_hist_raw_triggers": raw_triggers,
                "rj_only_hist_confirmed": confirmed_triggers,
                "rj_only_hist_risk_ok": risk_ok_triggers,
                "rj_only_hist_filter_mode": "core_rj",
                "rj_only_hist_wins": wins,
                "rj_only_hist_losses": losses,
                "rj_only_hist_win_rate": round(wins / len(samples) * 100.0, 2),
                "rj_only_hist_avg_r": round(sum(samples) / len(samples), 4),
                "rj_only_hist_profit_factor": round(pf, 4),
                "rj_only_hist_target_r": target_r,
                "rj_only_hist_horizon_bars": horizon,
                "rj_only_hist_lookback_bars": lookback,
            }
        except Exception as e:
            self._log.warning(f"RJ-only历史胜率统计异常: {e}")
            return empty

    def _score_rj_only_signal(self, signal: dict) -> float:
        try:
            win_rate = float(signal.get("rj_only_hist_win_rate", 0.0) or 0.0)
            avg_r = float(signal.get("rj_only_hist_avg_r", 0.0) or 0.0)
            pf = float(signal.get("rj_only_hist_profit_factor", 0.0) or 0.0)
            samples = int(signal.get("rj_only_hist_samples", 0) or 0)
            spread = abs(float(signal.get("rj_spread", 0.0) or 0.0))
            bars_ago = int(signal.get("rj_cross_bars_ago", 0) or 0)
            confirm_bars = max(1, int(signal.get("rj_only_confirm_bars", getattr(self.cfg, "rj_only_confirm_bars", 6)) or 6))

            score = 35.0
            score += max(0.0, min(35.0, (win_rate - 40.0) * 1.25))
            score += max(-12.0, min(18.0, avg_r * 18.0))
            score += max(0.0, min(8.0, (pf - 1.0) * 5.0))
            score += max(0.0, min(8.0, samples / 2.0))
            score += max(0.0, min(7.0, spread / 4.0))
            score += max(0.0, min(5.0, confirm_bars - bars_ago + 1))
            return float(round(max(0.0, min(100.0, score)), 2))
        except Exception:
            return float(signal.get("score", 0.0) or 0.0)

    def _rj_watchlist_file(self) -> Path:
        return Path(str(getattr(self, "_rj_watchlist_path", "rj_watchlist.json") or "rj_watchlist.json"))

    def _load_rj_watchlist(self) -> dict:
        try:
            path = self._rj_watchlist_file()
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                rows = data.get("rows", []) if isinstance(data, dict) else []
                symbols = data.get("symbols", []) if isinstance(data, dict) else []
                if not symbols:
                    symbols = [r.get("symbol") for r in rows if r.get("symbol")]
                self._rj_watchlist = {
                    "updated_ts": float(data.get("updated_ts", 0.0) or 0.0),
                    "updated_at": data.get("updated_at", ""),
                    "rows": rows,
                    "symbols": [s for s in symbols if s],
                }
        except Exception as e:
            self._log.warning(f"RJ优选池加载失败: {e}")
        return self._rj_watchlist

    def _save_rj_watchlist(self):
        try:
            path = self._rj_watchlist_file()
            payload = dict(self._rj_watchlist or {})
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        except Exception as e:
            self._log.warning(f"RJ优选池保存失败: {e}")

    def _score_rj_watchlist_row(self, stats: dict, volume_usdt: float = 0.0) -> float:
        try:
            samples = int(stats.get("rj_only_hist_samples", 0) or 0)
            win = float(stats.get("rj_only_hist_win_rate", 0.0) or 0.0)
            avg_r = float(stats.get("rj_only_hist_avg_r", 0.0) or 0.0)
            pf = float(stats.get("rj_only_hist_profit_factor", 0.0) or 0.0)
            score = 0.0
            score += max(0.0, min(55.0, (win - 35.0) * 1.35))
            score += max(-15.0, min(20.0, avg_r * 20.0))
            score += max(0.0, min(12.0, (pf - 1.0) * 6.0))
            score += max(0.0, min(10.0, samples * 0.8))
            if volume_usdt > 0:
                score += max(0.0, min(3.0, np.log10(max(volume_usdt, 1.0)) - 6.0))
            return float(round(max(0.0, min(100.0, score)), 2))
        except Exception:
            return 0.0

    def _refresh_rj_watchlist_if_needed(self, intervals: list, ranked: list, vols: dict, btc_fields: dict):
        if not bool(getattr(self.cfg, "rj_only_watchlist_enabled", True)):
            return
        self._load_rj_watchlist()
        now_ts = time.time()
        refresh_minutes = max(5, int(getattr(self.cfg, "rj_only_watchlist_refresh_minutes", 60) or 60))
        current = getattr(self, "_rj_watchlist", {}) or {}
        last_refresh_ts = float(current.get("updated_ts", 0.0) or 0.0)
        if last_refresh_ts > 0 and now_ts - last_refresh_ts < refresh_minutes * 60:
            return

        eval_limit = max(10, int(getattr(self.cfg, "rj_only_watchlist_eval_symbols", 200) or 200))
        watch_size = max(5, int(getattr(self.cfg, "rj_only_watchlist_size", 80) or 80))
        min_samples = max(1, int(getattr(self.cfg, "rj_only_watchlist_min_samples", getattr(self.cfg, "rj_only_stats_min_samples", 8)) or 8))
        min_win = max(0.0, float(getattr(self.cfg, "rj_only_watchlist_min_win_rate", getattr(self.cfg, "rj_only_stats_min_win_rate", 52.0)) or 52.0))
        min_avg = float(getattr(self.cfg, "rj_only_watchlist_min_avg_r", getattr(self.cfg, "rj_only_stats_min_avg_r", 0.0)) or 0.0)
        lookback = max(80, int(getattr(self.cfg, "rj_only_stats_lookback_bars", 220) or 220))
        universe = self._filter_live_trade_symbols(list(ranked[:eval_limit]))
        rows = []

        def worker(sym: str, inv: str):
            df = fetch_klines(sym, inv, lookback, exchange=self.cfg.exchange)
            if df is None or len(df) < 80:
                return []
            out = []
            for direction in ("LONG", "SHORT"):
                stats = self._rj_only_history_stats(df, direction)
                samples = int(stats.get("rj_only_hist_samples", 0) or 0)
                win_rate = float(stats.get("rj_only_hist_win_rate", 0.0) or 0.0)
                avg_r = float(stats.get("rj_only_hist_avg_r", 0.0) or 0.0)
                if samples <= 0:
                    continue
                volume = float(vols.get(sym, 0.0) or 0.0)
                pass_filter = samples >= min_samples and win_rate >= min_win and avg_r >= min_avg
                if samples < min_samples:
                    reason = "samples_low"
                elif win_rate < min_win:
                    reason = "win_rate_low"
                elif avg_r < min_avg:
                    reason = "avg_r_low"
                else:
                    reason = "pass"
                row = {
                    "symbol": sym,
                    "direction": direction,
                    "interval": inv,
                    "score": self._score_rj_watchlist_row(stats, volume),
                    "watchlist_pass": bool(pass_filter),
                    "watchlist_reason": reason,
                    "volume_usdt": round(volume, 2),
                    "samples": samples,
                    "wins": int(stats.get("rj_only_hist_wins", 0) or 0),
                    "losses": int(stats.get("rj_only_hist_losses", 0) or 0),
                    "win_rate": win_rate,
                    "avg_r": avg_r,
                    "profit_factor": float(stats.get("rj_only_hist_profit_factor", 0.0) or 0.0),
                    "target_r": float(stats.get("rj_only_hist_target_r", getattr(self.cfg, "rj_only_stats_target_r", 1.0)) or 1.0),
                    "horizon_bars": int(stats.get("rj_only_hist_horizon_bars", getattr(self.cfg, "rj_only_stats_horizon_bars", 12)) or 12),
                }
                out.append(row)
            return out

        try:
            worker_cap = max(1, int(getattr(self.cfg, "rj_only_watchlist_workers", 4) or 4))
            max_workers = min(worker_cap, max(1, len(universe)))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {}
                for inv in intervals:
                    for sym in universe:
                        futures[pool.submit(worker, sym, inv)] = (sym, inv)
                for fut in as_completed(futures):
                    try:
                        rows.extend(fut.result() or [])
                    except Exception as e:
                        sym, inv = futures[fut]
                        self._log.warning(f"RJ优选池统计异常 {sym} {inv}: {e}")
        except Exception as e:
            self._log.warning(f"RJ优选池刷新异常: {e}")
            return

        qualified_rows = [row for row in rows if row.get("watchlist_pass")]
        source_rows = qualified_rows if qualified_rows else rows
        best_by_symbol = {}
        for row in source_rows:
            sym = row.get("symbol", "")
            if not sym:
                continue
            if sym not in best_by_symbol or row.get("score", 0) > best_by_symbol[sym].get("score", 0):
                best_by_symbol[sym] = row
        best_rows = sorted(best_by_symbol.values(), key=lambda x: x.get("score", 0), reverse=True)[:watch_size]
        self._rj_watchlist = {
            "updated_ts": now_ts,
            "updated_at": bj_now().isoformat(),
            "intervals": ",".join(intervals),
            "eval_symbols": len(universe),
            "qualified_rows": len(qualified_rows),
            "fallback_used": not bool(qualified_rows),
            "rows": best_rows,
            "symbols": [r.get("symbol") for r in best_rows if r.get("symbol")],
            "filters": {
                "min_samples": min_samples,
                "min_win_rate": min_win,
                "min_avg_r": min_avg,
            },
        }
        self._save_rj_watchlist()
        self._append_signal_event("rj_watchlist_refresh", payload={
            "intervals": ",".join(intervals),
            "eval_symbols": len(universe),
            "history_rows": len(rows),
            "qualified_rows": len(qualified_rows),
            "qualified_symbols": len(best_rows),
            "fallback_used": not bool(qualified_rows),
            "top_symbols": ",".join(self._rj_watchlist.get("symbols", [])[:10]),
            **btc_fields,
        })
        self._log.info(
            f"RJ watchlist refreshed: evaluated={len(universe)} qualified_rows={len(qualified_rows)} "
            f"selected={len(best_rows)} fallback={not bool(qualified_rows)} "
            f"top={','.join(self._rj_watchlist.get('symbols', [])[:5]) or '-'}"
        )

    def _scan_predicta_signals(self, intervals: list, btc_fields: dict) -> list:
        """Scan the liquid universe for closed-candle Predicta labels."""
        try:
            symbols, vols = fetch_pairs(exchange=self.cfg.exchange)
        except Exception as exc:
            self._log.warning(f"Predicta扫描读取交易对失败: {exc}")
            return []
        min_volume = max(0.0, float(getattr(self.cfg, "predicta_min_volume_usdt", MIN_VOLUME) or 0.0))
        max_symbols = max(5, int(getattr(self.cfg, "predicta_max_symbols", 500) or 500))
        ranked = sorted(
            [
                symbol for symbol in symbols
                if symbol and symbol.endswith("USDT") and symbol != "USDCUSDT"
                and not is_tradfi_or_junk(symbol)
                and float(vols.get(symbol, 0.0) or 0.0) >= min_volume
            ],
            key=lambda symbol: float(vols.get(symbol, 0.0) or 0.0),
            reverse=True,
        )
        candidates = self._filter_live_trade_symbols(ranked)[:max_symbols]
        fast_signals = []
        waiting_setups = []
        lookback = 160

        def worker(symbol: str, interval: str):
            frame = fetch_klines(
                symbol, interval, lookback,
                exchange=self.cfg.exchange,
                closed_only=True,
            )
            if frame is None or len(frame) < 40:
                return [], []
            fast, waiting = self._predicta_candidates_from_df(symbol, interval, frame)
            if not fast:
                recovered = self._predicta_signal_from_df(symbol, interval, frame)
                if recovered is not None:
                    fast = [recovered]
            return fast, waiting

        worker_cap = max(1, int(getattr(self.cfg, "predicta_scan_workers", 4) or 4))
        with ThreadPoolExecutor(max_workers=min(worker_cap, max(1, len(candidates)))) as pool:
            futures = {
                pool.submit(worker, symbol, interval): (symbol, interval)
                for interval in intervals
                for symbol in candidates
                if not any(position.symbol == symbol for position in self.positions)
            }
            for future in as_completed(futures):
                try:
                    fast, waiting = future.result()
                    volume = round(float(vols.get(futures[future][0], 0.0) or 0.0), 2)
                    for item in fast + waiting:
                        item["volume_usdt"] = volume
                    fast_signals.extend(fast)
                    waiting_setups.extend(waiting)
                except Exception as exc:
                    symbol, interval = futures[future]
                    self._log.warning(f"Predicta扫描异常 {symbol} {interval}: {exc}")
        fast_unique = {item["signal_key"]: item for item in fast_signals}
        waiting_unique = {item["signal_key"]: item for item in waiting_setups}
        self._predicta_latest_setups = sorted(
            waiting_unique.values(), key=lambda item: item.get("volume_usdt", 0.0), reverse=True
        )
        return sorted(
            fast_unique.values(), key=lambda item: item.get("volume_usdt", 0.0), reverse=True
        )

    def _scan_rj_only_signals(self, intervals: list, btc_fields: dict) -> list:
        try:
            symbols, vols = fetch_pairs(exchange=self.cfg.exchange)
        except Exception as e:
            self._append_signal_event("rj_only_scan_error", payload={"reason": f"fetch_pairs:{e}", **btc_fields})
            return []
        min_vol = max(0.0, float(getattr(self.cfg, "rj_only_min_volume_usdt", MIN_VOLUME) or 0.0))
        max_symbols = max(5, int(getattr(self.cfg, "rj_only_max_symbols", 80) or 80))
        ranked = sorted(
            [s for s in symbols if s and s.endswith("USDT") and s != "USDCUSDT" and not is_tradfi_or_junk(s)],
            key=lambda s: float(vols.get(s, 0.0) or 0.0),
            reverse=True,
        )
        ranked = self._filter_live_trade_symbols(ranked)
        eligible = [s for s in ranked if float(vols.get(s, 0.0) or 0.0) >= min_vol]
        self._refresh_rj_watchlist_if_needed(intervals, eligible, vols, btc_fields)
        watch_symbols = []
        if bool(getattr(self.cfg, "rj_only_watchlist_enabled", True)):
            self._load_rj_watchlist()
            watch_size = max(5, int(getattr(self.cfg, "rj_only_watchlist_size", 80) or 80))
            eligible_set = set(eligible)
            saved_symbols = list((getattr(self, "_rj_watchlist", {}) or {}).get("symbols", []) or [])
            watch_symbols = [s for s in saved_symbols[:watch_size] if s in eligible_set]
        discovery_n = max(0, int(getattr(self.cfg, "rj_only_watchlist_discovery_top_n", 500) or 0))
        candidates = []
        seen = set()
        for sym in watch_symbols + eligible[:discovery_n]:
            if sym not in seen:
                candidates.append(sym)
                seen.add(sym)
        if not candidates:
            full_candidates = eligible[:max_symbols]
        else:
            full_candidates = candidates[:max_symbols]
        batch_size = max(5, int(getattr(self.cfg, "rj_only_scan_batch_size", 500) or 500))
        scan_universe_symbols = len(full_candidates)
        scan_cursor_start = 0
        if batch_size < len(full_candidates):
            cursor_key = f"{self.cfg.exchange}|{','.join(intervals)}|{scan_universe_symbols}|{full_candidates[0] if full_candidates else ''}"
            if getattr(self, "_rj_scan_cursor_key", "") != cursor_key:
                self._rj_scan_cursor_key = cursor_key
                self._rj_scan_cursor = 0
            scan_cursor_start = int(getattr(self, "_rj_scan_cursor", 0) or 0) % len(full_candidates)
            end = scan_cursor_start + batch_size
            if end <= len(full_candidates):
                candidates = full_candidates[scan_cursor_start:end]
            else:
                candidates = full_candidates[scan_cursor_start:] + full_candidates[:end - len(full_candidates)]
            self._rj_scan_cursor = end % len(full_candidates)
        else:
            candidates = full_candidates
        lookback = max(220, min(1000, int(getattr(self.cfg, "rj_only_stats_lookback_bars", 1000) or 1000)))
        out = []
        setup_out = []

        def worker(sym: str, inv: str):
            df = fetch_klines(sym, inv, lookback, exchange=self.cfg.exchange)
            if df is None or len(df) < 60:
                return None
            sig = self._rj_only_signal_from_df(sym, inv, df)
            if sig:
                sig["volume_usdt"] = round(float(vols.get(sym, 0.0) or 0.0), 2)
            setup = self._rj_only_setup_from_df(sym, inv, df)
            if setup:
                setup["volume_usdt"] = round(float(vols.get(sym, 0.0) or 0.0), 2)
            return sig, setup

        worker_cap = max(1, int(getattr(self.cfg, "rj_only_scan_workers", 4) or 4))
        max_workers = min(worker_cap, max(1, len(candidates)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for inv in intervals:
                for sym in candidates:
                    if any(p.symbol == sym for p in self.positions):
                        continue
                    futures[pool.submit(worker, sym, inv)] = (sym, inv)
            for fut in as_completed(futures):
                try:
                    result = fut.result()
                    if not result:
                        continue
                    sig, setup = result
                    if sig:
                        out.append(sig)
                    if setup:
                        setup_out.append(setup)
                except Exception as e:
                    sym, inv = futures[fut]
                    self._log.warning(f"RJ-only扫描异常 {sym} {inv}: {e}")

        unique = {}
        for sig in out:
            sym = sig.get("symbol", "")
            if sym not in unique or sig.get("score", 0) > unique[sym].get("score", 0):
                unique[sym] = sig
        signals = list(unique.values())
        signals.sort(key=lambda x: x.get("score", 0), reverse=True)
        setup_unique = {}
        for setup in setup_out:
            key = setup.get("rj_setup_key") or setup.get("signal_key") or f"{setup.get('symbol')}|{setup.get('direction')}"
            if key not in setup_unique or setup.get("score", 0) > setup_unique[key].get("score", 0):
                setup_unique[key] = setup
        self._rj_only_latest_setups = sorted(setup_unique.values(), key=lambda x: x.get("score", 0), reverse=True)
        self._append_signal_event("rj_only_scan_cycle", payload={
            "intervals": ",".join(intervals),
            "scan_symbols": len(candidates),
            "scan_universe_symbols": scan_universe_symbols,
            "scan_batch_size": batch_size,
            "scan_cursor_start": scan_cursor_start,
            "raw_signals": len(out),
            "unique_signals": len(signals),
            "raw_setups": len(setup_out),
            "unique_setups": len(self._rj_only_latest_setups),
            "setup_pool_size": len(getattr(self, "_rj_setup_pool", {}) or {}),
            "watchlist_enabled": bool(getattr(self.cfg, "rj_only_watchlist_enabled", True)),
            "watchlist_symbols": len(watch_symbols),
            "watchlist_updated_at": (getattr(self, "_rj_watchlist", {}) or {}).get("updated_at", ""),
            "discovery_symbols": discovery_n,
            "lookback_bars": lookback,
            "min_volume_usdt": min_vol,
            "rj_volume_filter": bool(getattr(self.cfg, "rj_only_volume_filter", True)),
            "rj_volume_len": int(getattr(self.cfg, "rj_only_volume_len", 20) or 20),
            "rj_volume_mult": float(getattr(self.cfg, "rj_only_volume_mult", 1.1) or 1.1),
            "max_symbols": max_symbols,
            "positions": len(self.positions),
            **btc_fields,
        })
        return signals

    def _run_predicta_cycle(self, now, btc_fields: dict, intervals: list):
        confirmed = self._check_predicta_setup_pool()
        fast_signals = self._scan_predicta_signals(intervals, btc_fields)
        waiting_setups = list(self._predicta_latest_setups)
        self._sync_predicta_setup_pool(waiting_setups)
        signals_by_key = {
            str(signal.get("signal_key", "")): signal
            for signal in confirmed + fast_signals
            if signal.get("signal_key")
        }
        signals = list(signals_by_key.values())
        self.last_scan_time = now
        self.last_signal_count = len(signals)
        self._append_signal_event("predicta_scan_cycle", payload={
            "intervals": ",".join(intervals),
            "fast_signals": len(fast_signals),
            "confirmed_signals": len(confirmed),
            "waiting_setups": len(waiting_setups),
            "setup_pool_size": len(self._predicta_setup_pool),
            "positions": len(self.positions),
            **btc_fields,
        })
        for signal in signals:
            self._append_signal_event(
                "predicta_candidate", signal.get("symbol", ""),
                self._signal_snapshot(signal, btc_fields),
            )
        for setup in waiting_setups:
            self._append_signal_event(
                "predicta_setup_wait", setup.get("symbol", ""),
                self._signal_snapshot(setup, btc_fields),
            )
        display = signals[:10]
        if len(display) < 10:
            display += waiting_setups[:10 - len(display)]
        self.last_signals_data = [dict(item) for item in display]
        for signal in signals:
            if len(self.positions) >= self.cfg.max_positions:
                break
            if any(position.symbol == signal.get("symbol") for position in self.positions):
                continue
            self.enter_predicta_position(signal)

    def _run_rj_only_cycle(self, now, btc_fields: dict, intervals: list):
        signals = self._scan_rj_only_signals(intervals, btc_fields)
        setups = list(getattr(self, "_rj_only_latest_setups", []) or [])
        self._sync_rj_setup_pool(setups, btc_fields)
        self.last_scan_time = now
        self.last_signal_count = len(signals)
        for s in signals:
            self._append_signal_event("rj_only_candidate", s.get("symbol", ""), self._signal_snapshot(s, btc_fields))
            if s.get("choppy_filter_mode") == "log_only" and s.get("choppy_filter_is_choppy"):
                self._append_signal_event(
                    "rj_choppy_shadow",
                    s.get("symbol", ""),
                    self._signal_snapshot(s, {"source_event": "rj_only_candidate", **btc_fields}),
                )
        display_rows = signals[:10]
        if len(display_rows) < 10:
            display_rows = display_rows + setups[:max(0, 10 - len(display_rows))]
        self.last_signals_data = [
            {k: v for k, v in s.items() if k in (
                "symbol", "direction", "price", "score", "source_interval", "source_strategy",
                "retest", "rj_j", "rj_r", "rj_spread", "rj_cross_bars_ago",
                "rj_only_key_high", "rj_only_key_low", "rj_only_stop_pct",
                "rj_setup_trigger_price", "rj_setup_live_price", "rj_setup_confirm_mode",
                "rj_setup_close_confirm_sec", "rj_setup_expires_at",
                "rj_volume_filter_pass", "rj_volume_ratio", "rj_volume_mult", "rj_volume_reason",
                "rj_sr_filter_pass", "rj_sr_reason", "rj_sr_support", "rj_sr_resistance",
                "rj_sr_near_support", "rj_sr_near_resistance", "rj_sr_bull_div", "rj_sr_bear_div",
                "rj_only_hist_samples", "rj_only_hist_win_rate", "rj_only_hist_avg_r",
                "rj_only_hist_profit_factor", "rj_only_stats_pass",
                "choppy_filter_mode", "choppy_filter_is_choppy", "choppy_filter_reason",
                "choppy_atr_ratio", "choppy_box_amplitude", "choppy_box_position"
            )}
            for s in display_rows
        ]
        for sig in signals:
            if len(self.positions) >= self.cfg.max_positions:
                break
            if not sig.get("rj_only_stats_pass", True):
                self._append_signal_event("entry_reject", sig.get("symbol", ""), self._signal_snapshot(sig, {
                    "reason": "rj_only_stats_failed",
                    "stats_reason": sig.get("rj_only_stats_reason", ""),
                }))
                continue
            if sig.get("score", 0) < self.cfg.min_score:
                self._append_signal_event("entry_reject", sig.get("symbol", ""), self._signal_snapshot(sig, {
                    "reason": "score_below_min",
                    "min_score": self.cfg.min_score,
                }))
                continue
            if any(p.symbol == sig["symbol"] for p in self.positions):
                continue
            self.enter_rj_position(sig)

    def _signal_snapshot(self, sig: dict, extra: Optional[dict] = None) -> dict:
        """压缩信号字段，避免日志膨胀，同时保留后续统计需要的核心特征。"""
        keys = (
            "symbol", "direction", "price", "score", "source_interval", "source_strategy", "retest",
            "entry_precheck_price", "entry_precheck_band_hi", "entry_precheck_band_lo",
            "entry_precheck_band_spread_pct", "entry_precheck_kline_time",
            "chain_verified",
            "min_spread", "current_spread", "breakout_pct", "bars_since",
            "squeeze_bars", "vol_surge", "body_pct", "ht_trend", "ht_fractal",
            "tight", "duration", "fresh", "retest_score", "strength",
            "vol_score", "ht_score", "ht_frac", "fractal_sl", "band_sl",
            "squeeze_edge", "signal_key", "breakout_bar", "retest_bar",
            "squeeze_start", "squeeze_end", "first_fractal_bar", "confirm_fractal_bar",
            "target_zone_type", "target_zone_price", "target_zone_low", "target_zone_high",
            "target_r", "target_distance_pct", "target_zone_bars_ago",
            "predicta_entry_path", "predicta_key_time", "predicta_key_high", "predicta_key_low",
            "predicta_signal_ewo", "predicta_confirm_time", "predicta_confirm_ewo",
            "predicta_confirm_reason", "predicta_confirm_age_bars", "predicta_confirm_bars",
            "predicta_atr", "predicta_stop_price", "predicta_stop_anchor",
            "rj_filter_mode", "rj_filter_pass", "rj_rule_pass", "rj_filter_reason",
            "rj_available", "rj_reason", "rj_j", "rj_r", "rj_spread", "rj_bg",
            "rj_current_ok", "rj_recent_cross", "rj_cross_type", "rj_cross_bars_ago",
            "rj_trigger_source", "rj_trigger_is_level", "rj_trigger_is_cross",
            "rj_cross_time", "rj_last_kline_time", "rj_cross_lookback_bars",
            "rj_min_jr_spread", "rj_required", "rj_kdj_len", "rj_k_smooth",
            "rj_d_smooth", "rj_rsi_len", "rj_rsi_smooth", "rj_kd_ma_type",
            "rj_slow_line_mode", "rj_slow_line_scale", "rj_slow_line_offset",
            "rj_slow_line_clamp",
            "volume_usdt", "rj_volume_filter_enabled", "rj_volume_filter_pass", "rj_volume_reason",
            "rj_volume_len", "rj_volume_mult", "rj_volume", "rj_volume_ma",
            "rj_volume_ratio", "rj_volume_bar", "rj_volume_checked_live",
            "rj_only_score_source", "rj_only_key_bar", "rj_only_confirm_bar",
            "rj_setup_key", "rj_setup_first_seen", "rj_setup_expires_at", "rj_setup_trigger_hold_sec",
            "rj_setup_confirm_mode", "rj_setup_close_confirm_sec", "rj_setup_check_interval_sec",
            "rj_setup_seconds_to_close",
            "rj_setup_trigger_started_at", "rj_setup_trigger_price", "rj_setup_live_price",
            "rj_setup_pool_size", "rj_setup_age_sec",
            "rj_only_key_time", "rj_only_confirm_time", "rj_only_key_high", "rj_only_key_low",
            "rj_only_confirm_close", "rj_only_confirm_level", "rj_only_stop_price", "rj_only_stop_pct",
            "rj_only_atr", "rj_only_atr_sl_mult", "rj_only_confirm_atr_buffer",
            "rj_only_invalidate_on_opposite_break", "rj_only_confirm_bars",
            "rj_entry_chase_pct", "rj_setup_chase_pct", "rj_only_setup_max_chase_pct",
            "rj_sr_filter_enabled", "rj_sr_filter_pass", "rj_sr_reason", "rj_sr_near_mode",
            "rj_sr_support", "rj_sr_resistance", "rj_sr_support_bar", "rj_sr_resistance_bar",
            "rj_sr_support_dist_pct", "rj_sr_resistance_dist_pct",
            "rj_sr_near_support", "rj_sr_near_resistance", "rj_sr_require_divergence",
            "rj_sr_bull_div", "rj_sr_bear_div", "rj_sr_bull_div_recent", "rj_sr_bear_div_recent",
            "rj_sr_early_bull_div", "rj_sr_early_bear_div",
            "rj_sr_last_bull_div_bar", "rj_sr_last_bear_div_bar",
            "rj_sr_pivot_left", "rj_sr_pivot_right",
            "choppy_filter_mode", "choppy_filter_anchor", "choppy_filter_available",
            "choppy_filter_is_choppy", "choppy_filter_reason", "choppy_filter_reasons",
            "choppy_filter_anchor_idx", "choppy_filter_anchor_time",
            "choppy_atr", "choppy_atr_baseline", "choppy_atr_ratio",
            "choppy_box_high", "choppy_box_low", "choppy_box_amplitude",
            "choppy_box_threshold", "choppy_box_position",
            "choppy_adx_period", "choppy_adx",
            "choppy_efficiency_period", "choppy_efficiency_ratio",
            "btc_coin_reversal_pass",
            "rj_only_stats_pass", "rj_only_stats_reason", "rj_only_hist_samples",
            "rj_only_hist_raw_triggers", "rj_only_hist_confirmed", "rj_only_hist_risk_ok",
            "rj_only_hist_filter_mode", "rj_only_hist_wins", "rj_only_hist_losses", "rj_only_hist_win_rate",
            "rj_only_hist_avg_r", "rj_only_hist_profit_factor", "rj_only_hist_target_r",
            "rj_only_hist_horizon_bars", "rj_only_hist_lookback_bars"
        )
        out = {k: sig.get(k) for k in keys if k in sig}
        try:
            out.update(self._btc_regime_fields())
        except Exception:
            out.update({"btc_regime": "btc_unknown", "btc_score": 0})
        if extra:
            out.update(extra)
        return out

    def _btc_interval_regime(self, interval: str) -> dict:
        """BTC单周期环境快照。只记录环境, 不参与交易决策。"""
        df = fetch_klines("BTCUSDT", interval, 200, exchange="binance")
        if df is None or len(df) < 130:
            return {"error": "数据不足"}
        closes = df["c"]
        highs = df["h"]
        lows = df["l"]
        price = float(closes.iloc[-1])

        emas = {p: ema(closes, p).iloc[-1] for p in EMA_LENS}
        mas = {p: closes.rolling(p).mean().iloc[-1] for p in MA_LENS}
        all_mas = list(emas.values()) + list(mas.values())
        band_hi, band_lo = max(all_mas), min(all_mas)
        spread_pct = (band_hi - band_lo) / price * 100 if price else 0

        e20, e60, e120 = emas[20], emas[60], emas[120]
        if e20 > e60 > e120:
            alignment = "bull"
            score = 30
        elif e20 < e60 < e120:
            alignment = "bear"
            score = -30
        else:
            alignment = "neutral"
            score = 0

        tight_threshold = {"1h": 6, "4h": 10, "1d": 8, "1w": 12}.get(interval, 8)
        if spread_pct <= tight_threshold * 0.5:
            squeeze_state = "tight"
        elif spread_pct <= tight_threshold:
            squeeze_state = "compressing"
        elif spread_pct <= tight_threshold * 2:
            squeeze_state = "expanding"
        else:
            squeeze_state = "trending"

        if price > band_hi:
            position = "above"
            breakout = "up"
            score += 20
        elif price < band_lo:
            position = "below"
            breakout = "down"
            score -= 20
        else:
            position = "inside"
            breakout = "none"

        fractal = "--"
        n = len(closes)
        for i in range(n - 3, max(n - 9, 3), -1):
            if lows.iloc[i] < lows.iloc[i-1] and lows.iloc[i] < lows.iloc[i+1]:
                if i + 2 < n and (closes.iloc[i+1] > highs.iloc[i] or closes.iloc[i+2] > highs.iloc[i]):
                    fractal = "bottom"
                    score += 10
                    break
            if highs.iloc[i] > highs.iloc[i-1] and highs.iloc[i] > highs.iloc[i+1]:
                if i + 2 < n and (closes.iloc[i+1] < lows.iloc[i] or closes.iloc[i+2] < lows.iloc[i]):
                    fractal = "top"
                    score -= 10
                    break

        if score >= 40:
            overall = "strong_bull"
        elif score >= 15:
            overall = "bull_bias"
        elif score <= -40:
            overall = "strong_bear"
        elif score <= -15:
            overall = "bear_bias"
        else:
            overall = "neutral"

        return {
            "price": round(price, 2),
            "alignment": alignment,
            "squeeze": squeeze_state,
            "position": position,
            "breakout": breakout,
            "fractal": fractal,
            "spread_pct": round(spread_pct, 2),
            "score": score,
            "overall": overall,
        }

    def get_btc_market_regime(self, force: bool = False) -> dict:
        """BTC multi-timeframe snapshot with a closed-candle stage decision."""
        now_ts = time.time()
        cache = getattr(self, "_btc_regime_cache", {"ts": 0, "data": {}})
        if not force and cache.get("data") and now_ts - cache.get("ts", 0) < 300:
            return cache["data"]
        data = {}
        for inv in ("1h", "4h", "1d", "1w"):
            try:
                data[inv] = self._btc_interval_regime(inv)
            except Exception as e:
                data[inv] = {"error": str(e)}
        try:
            frames = {}
            now_ms = int(time.time() * 1000)
            for inv in ("1h", "4h"):
                frame = fetch_klines("BTCUSDT", inv, 220, exchange="binance")
                if frame is None or frame.empty:
                    raise ValueError(f"btc_{inv}_data_unavailable")
                interval_ms = self._interval_seconds(inv) * 1000
                ot = pd.to_numeric(frame["ot"], errors="coerce")
                frames[inv] = frame.loc[(ot + interval_ms) <= now_ms].copy().reset_index(drop=True)
            stage = classify_btc_stage(frames["1h"], frames["4h"])
        except Exception as exc:
            stage = {
                "stage": "unknown",
                "direction": "unknown",
                "extreme_veto": False,
                "evidence": [str(exc)],
                "exhaustion_flags": [],
                "closed_1h_at": None,
                "closed_4h_at": None,
                "rule_version": "btc_stage_v1",
            }
        data["stage"] = stage
        data["summary"] = {
            "regime": f"btc_{stage.get('stage', 'unknown')}",
            "score": 0,
            **stage,
        }
        self._btc_regime_cache = {"ts": now_ts, "data": data}
        return data

    def _btc_regime_fields(self, regime: Optional[dict] = None) -> dict:
        """扁平化BTC环境字段，便于 jsonl 统计分组。"""
        regime = regime or self.get_btc_market_regime()
        summary = regime.get("summary", {})
        out = {
            "btc_regime": summary.get("regime", "btc_unknown"),
            "btc_score": summary.get("score", 0),
            "btc_stage": summary.get("stage", "unknown"),
            "btc_direction": summary.get("direction", "unknown"),
            "btc_extreme_veto": bool(summary.get("extreme_veto", False)),
            "btc_stage_evidence": summary.get("evidence", []),
            "btc_exhaustion_flags": summary.get("exhaustion_flags", []),
            "btc_stage_rule_version": summary.get("rule_version", "btc_stage_v1"),
            "btc_closed_1h_at": summary.get("closed_1h_at"),
            "btc_closed_4h_at": summary.get("closed_4h_at"),
        }
        for inv in ("1h", "4h", "1d", "1w"):
            row = regime.get(inv, {})
            prefix = f"btc_{inv}"
            out[f"{prefix}_overall"] = row.get("overall", row.get("error", "unknown"))
            out[f"{prefix}_alignment"] = row.get("alignment", "unknown")
            out[f"{prefix}_breakout"] = row.get("breakout", "unknown")
            out[f"{prefix}_position"] = row.get("position", "unknown")
            out[f"{prefix}_squeeze"] = row.get("squeeze", "unknown")
            out[f"{prefix}_fractal"] = row.get("fractal", "unknown")
            out[f"{prefix}_spread"] = row.get("spread_pct", 0)
        return out

    @staticmethod
    def _btc_coin_reversal_pass(signal: dict, direction: str) -> bool:
        """Use evidence already anchored to the RJ signal candle."""
        signal = signal or {}
        volume_ok = bool(signal.get("rj_volume_filter_pass", False))
        if direction == "LONG":
            location_ok = bool(signal.get("rj_sr_near_support", False))
            divergence_ok = any(bool(signal.get(key, False)) for key in (
                "rj_sr_bull_div", "rj_sr_bull_div_recent", "rj_sr_early_bull_div",
            ))
        else:
            location_ok = bool(signal.get("rj_sr_near_resistance", False))
            divergence_ok = any(bool(signal.get(key, False)) for key in (
                "rj_sr_bear_div", "rj_sr_bear_div_recent", "rj_sr_early_bear_div",
            ))
        return bool(volume_ok and location_ok and divergence_ok)

    def _btc_direction_filter(
        self,
        direction: str,
        btc_fields: dict,
        coin_reversal_pass: bool = False,
    ) -> tuple[bool, str]:
        if not self._as_bool(getattr(self.cfg, "btc_direction_filter_enabled", True)):
            return True, "disabled"
        stage = {
            "stage": (btc_fields or {}).get("btc_stage", "unknown"),
            "direction": (btc_fields or {}).get("btc_direction", "unknown"),
            "extreme_veto": bool((btc_fields or {}).get("btc_extreme_veto", False)),
        }
        allowed, reason = evaluate_btc_gate(direction, stage, coin_reversal_pass)
        return (True, "pass") if reason == "btc_unknown_pass" else (allowed, reason)

    def _position_btc_fields(self, pos: Position) -> dict:
        """Return entry-time BTC regime fields carried by a position."""
        fields = getattr(pos, "btc_regime_fields", {}) or {}
        if isinstance(fields, dict) and fields:
            return dict(fields)
        return {"btc_regime": "btc_unknown", "btc_score": 0}

    def _send_telegram(self, msg: str):
        """发送 Telegram 通知"""
        if not self.cfg.telegram_token or not self.cfg.telegram_chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.cfg.telegram_chat_id,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            self._log.error(f"Telegram通知失败: {e}")

    def _save_positions(self):
        """持久化当前持仓 + 活动止损单ID到文件"""
        try:
            data = []
            stop_ids = getattr(self.client, '_active_stop_ids', {}) if self.client else {}
            for p in self.positions:
                entry = {
                    "symbol": p.symbol, "direction": p.direction,
                    "entry_price": p.entry_price, "quantity": p.quantity,
                    "sl_price": p.sl_price, "current_sl": p.current_sl,
                    "risk_usdt": p.risk_usdt, "signal_score": p.signal_score,
                    "entry_time": p.entry_time.isoformat() if p.entry_time else "",
                    "half_risk_protected": bool(getattr(p, "half_risk_protected", False)),
                    "breakeven_triggered": p.breakeven_triggered,
                    "breakeven_cooldown": p.breakeven_cooldown,
                    "partial_tp_triggered": p.partial_tp_triggered,
                    "initial_sl": p.initial_sl,
                    "initial_band_hi": p.initial_band_hi, "initial_band_lo": p.initial_band_lo,
                    "highest_price": p.highest_price, "lowest_price": p.lowest_price,
                    "tracking_no": p.tracking_no,
                    "source_interval": p.source_interval,
                    "source_strategy": getattr(p, "source_strategy", ""),
                    "max_favorable_r": p.max_favorable_r,
                    "max_adverse_r": p.max_adverse_r,
                    "excursion_price_source": getattr(p, "excursion_price_source", ""),
                    "time_stop_armed": p.time_stop_armed,
                    "time_stop_armed_at": p.time_stop_armed_at.isoformat() if getattr(p, "time_stop_armed_at", None) else "",
                    "time_stop_watch": bool(getattr(p, "time_stop_watch", False)),
                    "time_stop_watch_started_at": p.time_stop_watch_started_at.isoformat() if getattr(p, "time_stop_watch_started_at", None) else "",
                    "time_stop_first_seen_bars": int(getattr(p, "time_stop_first_seen_bars", 0) or 0),
                    "time_stop_first_seen_r": float(getattr(p, "time_stop_first_seen_r", 0.0) or 0.0),
                    "time_stop_first_seen_mfe": float(getattr(p, "time_stop_first_seen_mfe", 0.0) or 0.0),
                    "btc_regime_fields": getattr(p, "btc_regime_fields", {}) or {},
                    "signal_key": getattr(p, "signal_key", ""),
                    "target_zone_type": getattr(p, "target_zone_type", ""),
                    "target_zone_price": getattr(p, "target_zone_price", 0.0),
                    "target_zone_low": getattr(p, "target_zone_low", 0.0),
                    "target_zone_high": getattr(p, "target_zone_high", 0.0),
                    "target_r": getattr(p, "target_r", 0.0),
                    "target_distance_pct": getattr(p, "target_distance_pct", 0.0),
                    "target_zone_bars_ago": getattr(p, "target_zone_bars_ago", 0),
                    "hermes_confirm": self._public_hermes_confirm(getattr(p, "hermes_confirm", {})),
                    "choppy_filter": self._position_choppy_filter(getattr(p, "choppy_filter", {})),
                    "active_stop_id": stop_ids.get(p.symbol, ""),
                }
                data.append(entry)
            with open(getattr(self, '_positions_path', 'positions.json'), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self._log.error(f"保存持仓失败: {e}")

    def _restore_stop_ids(self):
        """从持仓文件恢复 active_stop_ids (跨重启去重)"""
        if self.client is None: return
        if not hasattr(self.client, '_active_stop_ids'): self.client._active_stop_ids = {}
        try:
            path = getattr(self, '_positions_path', 'positions.json')
            if Path(path).exists():
                with open(path) as f:
                    for d in json.load(f):
                        oid = d.get("active_stop_id", "")
                        if oid:
                            self.client._active_stop_ids[d["symbol"]] = oid
        except: pass

    def _load_positions(self):
        """从文件恢复持仓"""
        if not Path(getattr(self, '_positions_path', 'positions.json')).exists():
            return []
        try:
            with open(getattr(self, '_positions_path', 'positions.json')) as f:
                data = json.load(f)
            loaded = []
            entry_choppy_audits = self._entry_choppy_audit_map()
            for d in data:
                pos = Position(
                    symbol=d["symbol"], direction=d["direction"],
                    entry_price=d["entry_price"], entry_time=datetime.fromisoformat(d["entry_time"]) if d.get("entry_time") else bj_now(),
                    quantity=d["quantity"], sl_price=d["sl_price"], current_sl=d["current_sl"],
                    risk_usdt=d.get("risk_usdt", 0), signal_score=d.get("signal_score", 0),
                    initial_band_hi=d.get("initial_band_hi", d["entry_price"]*1.02),
                    initial_band_lo=d.get("initial_band_lo", d["entry_price"]*0.98),
                )
                pos.half_risk_protected = bool(d.get("half_risk_protected", False))
                pos.breakeven_triggered = d.get("breakeven_triggered", False)
                pos.breakeven_cooldown = d.get("breakeven_cooldown", 0)
                pos.partial_tp_triggered = d.get("partial_tp_triggered", False)
                pos.initial_sl = d.get('initial_sl', d.get('sl_price', 0))
                pos.highest_price = d.get('highest_price', 0.0)
                pos.lowest_price = d.get('lowest_price', 999999.0)
                pos.tracking_no = d.get('tracking_no', '')
                pos.source_interval = d.get('source_interval', '15m')
                pos.max_favorable_r = float(d.get('max_favorable_r', 0.0) or 0.0)
                pos.max_adverse_r = float(d.get('max_adverse_r', 0.0) or 0.0)
                pos.excursion_price_source = str(d.get('excursion_price_source', '') or '')
                pos.btc_regime_fields = d.get('btc_regime_fields', {}) or {k: v for k, v in d.items() if str(k).startswith('btc_')}
                pos.signal_key = d.get('signal_key', '')
                raw_strategy = str(d.get('source_strategy', '') or '').strip().lower()
                sig_upper = str(pos.signal_key or '').upper()
                pos.source_strategy = raw_strategy or ('rj_only' if sig_upper.startswith('RJ') or 'RJSETUP' in sig_upper else 'structure')
                pos.target_zone_type = d.get('target_zone_type', '')
                pos.target_zone_price = float(d.get('target_zone_price', 0.0) or 0.0)
                pos.target_zone_low = float(d.get('target_zone_low', 0.0) or 0.0)
                pos.target_zone_high = float(d.get('target_zone_high', 0.0) or 0.0)
                pos.target_r = float(d.get('target_r', 0.0) or 0.0)
                pos.target_distance_pct = float(d.get('target_distance_pct', 0.0) or 0.0)
                pos.target_zone_bars_ago = int(d.get('target_zone_bars_ago', 0) or 0)
                pos.hermes_confirm = self._public_hermes_confirm(d.get('hermes_confirm', {}))
                pos.choppy_filter = self._position_choppy_filter(d.get('choppy_filter', {}))
                if not pos.choppy_filter.get("recorded"):
                    pos.choppy_filter = entry_choppy_audits.get(
                        (pos.symbol, pos.signal_key),
                        entry_choppy_audits.get((pos.symbol, ""), pos.choppy_filter),
                    )
                raw_armed_at = d.get('time_stop_armed_at', '')
                pos.time_stop_armed_at = datetime.fromisoformat(raw_armed_at) if raw_armed_at else None
                pos.time_stop_armed = True
                raw_watch_started = d.get('time_stop_watch_started_at', '')
                pos.time_stop_watch = bool(d.get('time_stop_watch', False))
                pos.time_stop_watch_started_at = datetime.fromisoformat(raw_watch_started) if raw_watch_started else None
                pos.time_stop_first_seen_bars = int(d.get('time_stop_first_seen_bars', 0) or 0)
                pos.time_stop_first_seen_r = float(d.get('time_stop_first_seen_r', 0.0) or 0.0)
                pos.time_stop_first_seen_mfe = float(d.get('time_stop_first_seen_mfe', 0.0) or 0.0)
                loaded.append(pos)
            return loaded
        except Exception as e:
            self._log.warning(f"加载持仓文件失败: {e}")
            return []

    def _sync_positions(self):
        """启动时恢复持仓: 优先从交易所同步, 其次从本地文件"""
        exchange_positions = []
        original_syms = {p.symbol for p in self.positions}  # 同步前快照, 用于清理已平仓

        if self.client is not None:
            # ---- 合约: 从交易所API同步 ----
            if self.cfg.market_type == "futures":
                try:
                    raw = self.client.get_positions()
                    if raw:
                        for p in raw:
                            amt = float(p.get("positionAmt", 0))
                            if amt == 0: continue
                            symbol = p["symbol"]
                            entry_price = float(p.get("entryPrice", 0))
                            direction = "LONG" if amt > 0 else "SHORT"
                            qty = abs(amt)
                            exchange_positions.append(symbol)
                            existing_pos = next((pp for pp in self.positions if pp.symbol == symbol), None)
                            if existing_pos is not None:
                                old_entry = float(getattr(existing_pos, "entry_price", 0.0) or 0.0)
                                if entry_price > 0:
                                    existing_pos.entry_price = entry_price
                                    self._refresh_target_metrics(existing_pos)
                                existing_pos.direction = direction
                                existing_pos.quantity = qty
                                try:
                                    mark_price = float(p.get("markPrice") or p.get("mark_price") or 0)
                                    if mark_price > 0:
                                        existing_pos.current_price = mark_price
                                except Exception:
                                    pass
                                try:
                                    existing_pos.pnl = float(p.get("unRealizedProfit", getattr(existing_pos, "pnl", 0.0)) or 0.0)
                                except Exception:
                                    pass
                                existing_pos._exchange_position_synced = True
                                try:
                                    if entry_price > 0 and abs(old_entry - entry_price) > max(entry_price * 0.000001, 1e-10):
                                        self._log.info(f"[交易所持仓同步] {symbol} 入场均价 {old_entry:.8f}->{entry_price:.8f}")
                                except Exception:
                                    pass
                                continue
                            # 从本地文件恢复当前止损价(含保本/追踪), 保留分型SL
                            sl_price = None
                            breached = False
                            matched_local = None
                            local_positions = self._load_positions()
                            for lp in local_positions:
                                if lp.symbol == symbol:
                                    matched_local = lp
                                    sl_price = lp.current_sl  # 使用当前止损(已含保本/追踪)
                                    breached = lp.breakeven_triggered
                                    self._log.info(f"{symbol} 恢复SL: {sl_price:.4f} 保本={breached}")
                                    break
                            restore_interval = (
                                str(getattr(matched_local, 'source_interval', '') or '').strip()
                                if matched_local else ''
                            )
                            if not restore_interval:
                                restore_interval = str(getattr(self.cfg, 'scan_interval', '15m')).split(',')[0].strip() or '15m'
                            # 本地无记录: 补搜分型 + 均线边缘, 取两者中更紧的
                            if sl_price is None:
                                fractal_sl = None
                                band_sl = None
                                try:
                                    df2 = fetch_klines(symbol, restore_interval, 200, exchange=self.cfg.exchange)
                                    if df2 is not None and len(df2) >= 100:
                                        details = verify_pool_signal_details(df2, direction, interval=restore_interval)
                                        if details:
                                            raw_fractal = details.get("fractal_sl")
                                            if raw_fractal:
                                                fractal_sl = raw_fractal * (0.998 if direction == "LONG" else 1.002)
                                            band_sl = details.get("band_sl")
                                except: pass
                                if fractal_sl is None:
                                    fractal_sl = self._find_fractal_sl_from_exchange(symbol, direction, entry_price)
                                # 分型 vs 边缘, 取结构外侧, 避免SL挂进确认分型/均线带内部
                                if fractal_sl is not None and band_sl is not None:
                                    if direction == "LONG":
                                        sl_price = min(fractal_sl, band_sl)
                                    else:
                                        sl_price = max(fractal_sl, band_sl)
                                elif fractal_sl is not None:
                                    sl_price = fractal_sl
                                elif band_sl is not None:
                                    sl_price = band_sl
                            if sl_price is None:
                                sl_price = entry_price * 0.95 if direction == "LONG" else entry_price * 1.05
                            restore_interval = (
                                str(getattr(matched_local, 'source_interval', '') or '').strip()
                                if matched_local else ''
                            )
                            if not restore_interval:
                                restore_interval = str(getattr(self.cfg, 'scan_interval', '15m')).split(',')[0].strip() or '15m'

                            # 从交易所获取真实开仓时间(转为北京时间 UTC+8)。
                            # Bitget持仓同步可能缺少openTime；这种情况下必须保留本地原始entry_time,
                            # 否则每次重启都会把未起爆超时计数重新开始。
                            local_entry_time = getattr(matched_local, 'entry_time', None) if matched_local else None
                            et = local_entry_time or bj_now()
                            open_ts = p.get("openTime", "")
                            try:
                                open_ms = int(open_ts)
                                if open_ms > 0:
                                    et = datetime.fromtimestamp(open_ms/1000, tz=timezone.utc) + timedelta(hours=8)
                            except:
                                pass
                            # 计算实际风险 (qty × SL距离), 超标时用配置风险做保本/锁利参考
                            actual_risk = qty * abs(entry_price - sl_price)
                            sync_risk = self.get_risk_for_interval(restore_interval)
                            ref_risk = round(min(actual_risk, sync_risk), 2)
                            if actual_risk > sync_risk * 1.1:
                                self._log.warning(f"[合约同步] {symbol} 恢复后风险${actual_risk:.2f}远超预算${self.cfg.risk_per_trade}, "
                                                f"SL={sl_price:.4f} 距入场{abs(entry_price-sl_price):.4f}, 保本参考${ref_risk}")
                            initial_sl_price = getattr(matched_local, 'initial_sl', 0.0) if matched_local else 0.0
                            if not initial_sl_price:
                                initial_sl_price = sl_price
                            pos = Position(
                                symbol=symbol, direction=direction, entry_price=entry_price,
                                entry_time=et, quantity=qty,
                                sl_price=initial_sl_price, initial_sl=initial_sl_price, current_sl=sl_price,
                                risk_usdt=ref_risk, signal_score=0,
                                initial_band_hi=entry_price*1.02, initial_band_lo=entry_price*0.98,
                                source_interval=restore_interval,
                            )
                            pos.half_risk_protected = bool(
                                getattr(matched_local, "half_risk_protected", False)
                            ) if matched_local else False
                            pos.breakeven_triggered = breached
                            pos.breakeven_cooldown = matched_local.breakeven_cooldown if (breached and matched_local) else 0
                            pos.partial_tp_triggered = getattr(matched_local, 'partial_tp_triggered', False) if matched_local else False
                            pos.highest_price = getattr(matched_local, 'highest_price', 0.0) if matched_local else 0.0
                            pos.lowest_price = getattr(matched_local, 'lowest_price', 999999.0) if matched_local else 999999.0
                            pos.max_favorable_r = getattr(matched_local, 'max_favorable_r', 0.0) if matched_local else 0.0
                            pos.max_adverse_r = getattr(matched_local, 'max_adverse_r', 0.0) if matched_local else 0.0
                            pos.excursion_price_source = getattr(matched_local, 'excursion_price_source', '') if matched_local else ''
                            pos.source_strategy = getattr(matched_local, 'source_strategy', '') if matched_local else ''
                            pos.signal_key = getattr(matched_local, 'signal_key', '') if matched_local else ''
                            if not pos.source_strategy:
                                sig_upper = str(pos.signal_key or '').upper()
                                pos.source_strategy = 'rj_only' if sig_upper.startswith('RJ') or 'RJSETUP' in sig_upper else 'structure'
                            pos.btc_regime_fields = getattr(matched_local, 'btc_regime_fields', {}) if matched_local else {}
                            pos.target_zone_type = getattr(matched_local, 'target_zone_type', '') if matched_local else ''
                            pos.target_zone_price = getattr(matched_local, 'target_zone_price', 0.0) if matched_local else 0.0
                            pos.target_zone_low = getattr(matched_local, 'target_zone_low', 0.0) if matched_local else 0.0
                            pos.target_zone_high = getattr(matched_local, 'target_zone_high', 0.0) if matched_local else 0.0
                            pos.target_r = getattr(matched_local, 'target_r', 0.0) if matched_local else 0.0
                            pos.target_distance_pct = getattr(matched_local, 'target_distance_pct', 0.0) if matched_local else 0.0
                            pos.target_zone_bars_ago = getattr(matched_local, 'target_zone_bars_ago', 0) if matched_local else 0
                            self._refresh_target_metrics(pos)
                            pos.hermes_confirm = self._public_hermes_confirm(getattr(matched_local, 'hermes_confirm', {}) if matched_local else {})
                            pos.time_stop_armed = True
                            pos.time_stop_armed_at = getattr(matched_local, 'time_stop_armed_at', None) if matched_local else None
                            pos.time_stop_watch = getattr(matched_local, 'time_stop_watch', False) if matched_local else False
                            pos.time_stop_watch_started_at = getattr(matched_local, 'time_stop_watch_started_at', None) if matched_local else None
                            pos.time_stop_first_seen_bars = getattr(matched_local, 'time_stop_first_seen_bars', 0) if matched_local else 0
                            pos.time_stop_first_seen_r = getattr(matched_local, 'time_stop_first_seen_r', 0.0) if matched_local else 0.0
                            pos.time_stop_first_seen_mfe = getattr(matched_local, 'time_stop_first_seen_mfe', 0.0) if matched_local else 0.0
                            if matched_local and pos.time_stop_armed and pos.time_stop_armed_at is None:
                                pos.time_stop_armed_at = matched_local.entry_time
                            try:
                                mark_price = float(p.get("markPrice") or p.get("mark_price") or 0)
                                if mark_price > 0:
                                    pos.current_price = mark_price
                            except:
                                pass
                            try:
                                pos.pnl = float(p.get("unRealizedProfit", getattr(pos, 'pnl', 0.0)) or 0.0)
                            except:
                                pass
                            pos._exchange_position_synced = True
                            self.positions.append(pos)
                            self._log.info(f"{symbol} restore source_interval={restore_interval}")
                            # 恢复持仓后立即挂止损单到交易所
                            sl_side = "SELL" if direction == "LONG" else "BUY"
                            sl_result = self.client.stop_order(symbol, sl_side, round(sl_price, 8), round(qty, 8))
                            if sl_result:
                                self._log.info(f"{symbol} 止损单确认已挂 SL={sl_price:.4f} id={sl_result.get('orderId','?')}")
                            else:
                                self._log.warning(f"{symbol} 止损单挂单失败, bot内部兜底 SL={sl_price:.4f}")
                            self._log.warning(f"[合约同步] {symbol} {direction} 价{entry_price} 量{qty} "
                                            f"SL={sl_price:.4f} 风险${actual_risk:.2f}")
                except Exception as e:
                    self._log.error(f"合约同步失败: {e}")

            # ---- 现货: 从账户余额反推持仓 ----
            elif self.cfg.market_type == "spot":
                try:
                    balances = self.client.get_spot_balances()
                    if balances:
                        for asset, info in balances.items():
                            if asset == "USDT":
                                continue
                            symbol = f"{asset}USDT"
                            exchange_positions.append(symbol)
                            if any(pp.symbol == symbol for pp in self.positions):
                                continue
                            # 估算入场价(当前价作为参考)
                            entry_price = self.client.get_price(symbol) or 0
                            qty = info["total"]
                            sl_price = entry_price * 0.95
                            actual_risk = qty * abs(entry_price - sl_price)
                            ref_risk = round(min(actual_risk, sync_risk), 2)
                            pos = Position(
                                symbol=symbol, direction="LONG", entry_price=entry_price,
                                entry_time=bj_now(), quantity=qty,
                                sl_price=sl_price, current_sl=sl_price,
                                risk_usdt=ref_risk, signal_score=0,
                                initial_band_hi=entry_price*1.02, initial_band_lo=entry_price*0.98,
                            )
                            pos.time_stop_armed = True
                            pos.time_stop_armed_at = bj_now()
                            self.positions.append(pos)
                            self._log.warning(f"[现货同步] {symbol} 持仓{qty}枚 价{entry_price} SL{sl_price:.4f} 风险${actual_risk:.2f}")
                except Exception as e:
                    self._log.error(f"现货同步失败: {e}")

        # ---- 2. 只恢复交易所没有的本地持仓 ----
        local = self._load_positions()
        for pos in local:
            if pos.symbol not in exchange_positions and not any(p.symbol == pos.symbol for p in self.positions):
                self.positions.append(pos)
                self._log.info(f"[本地恢复] {pos.symbol} {pos.direction}")

        # 清除交易所已不存在的本地持仓 (仅清理同步前从文件加载的, 不碰新恢复的)
        if self.client is not None and exchange_positions:
            for sym in original_syms:
                if sym not in exchange_positions:
                    for pos in list(self.positions):
                        if pos.symbol == sym:
                            self._log.info(f"[同步清理] {sym} 交易所已无持仓, 移除本地记录")
                            self.positions.remove(pos)
                            if hasattr(self.client, '_active_stop_ids'):
                                self.client._active_stop_ids.pop(sym, None)

        # 最终去重
        seen = set()
        deduped = []
        for p in self.positions:
            if p.symbol not in seen:
                seen.add(p.symbol); deduped.append(p)
            else:
                self._log.warning(f"去重丢弃: {p.symbol}")
        self.positions = deduped

        if self.positions:
            # 给所有持仓补挂止损单 (stop_order 内部已去重, 不会重复创建)
            if self.client is not None and self.cfg.market_type == "futures":
                self._log.info(f"开始补挂止损: 共{len(self.positions)}个持仓")
                for pos in self.positions:
                    try:
                        sl_side = "SELL" if pos.direction == "LONG" else "BUY"
                        self.client.stop_order(pos.symbol, sl_side, round(pos.current_sl, 8), round(pos.quantity, 8))
                    except Exception as e:
                        self._log.error(f"{pos.symbol} 补挂止损异常: {e}")
            # 恢复后保留原始止损价(分型止损), 仅刷新市场数据供UI显示
            for pos in self.positions:
                if self.cfg.market_type == "futures" and getattr(pos, "_exchange_position_synced", False):
                    continue
                try:
                    df = fetch_klines(pos.symbol, self.cfg.scan_interval, 100, exchange=self.cfg.exchange)
                    if df is not None and len(df) >= 50:
                        current_price = df["c"].iloc[-1]
                        pos.current_price = current_price
                        if pos.direction == "LONG":
                            pos.pnl = (current_price - pos.entry_price) * pos.quantity
                        else:
                            pos.pnl = (pos.entry_price - current_price) * pos.quantity
                        self._log.info(f"{pos.symbol} 恢复持仓 SL={pos.current_sl:.4f} PnL=${pos.pnl:.2f}")
                except Exception as e:
                    self._log.warning(f"刷新{pos.symbol}失败: {e}")
            self.status_text = f"已恢复 {len(self.positions)} 个持仓"
            # 重启不重挂止损单 (计划委托在交易所持久保留)
            self._save_positions()

    def _init_client(self):
        if getattr(self.cfg, "mode", "paper") == "paper" and str(getattr(self.cfg, "entry_signal_source", "structure")).lower() == "rj_only":
            self.client = None
            return

        # 测试网优先使用独立凭证, 否则用主网凭证
        if self.cfg.testnet:
            key = self.cfg.testnet_api_key or self.cfg.api_key
            secret = self.cfg.testnet_api_secret or self.cfg.api_secret
        else:
            key = self.cfg.api_key
            secret = self.cfg.api_secret

        if self.cfg.exchange == "bitget":
            if self.cfg.bitget_api_key and self.cfg.bitget_api_secret:
                self.client = BitgetClient(
                    self.cfg.bitget_api_key, self.cfg.bitget_api_secret,
                    self.cfg.bitget_api_pass, self.cfg.market_type,
                    is_lead_trader=self.cfg.is_lead_trader
                )
                self.client._log = self._log
            else:
                self.client = None
        elif key and secret:
            self.client = BinanceClient(
                key, secret, self.cfg.testnet, self.cfg.market_type
            )
            self.client._log = self._log
        else:
            self.client = None

    # ========== 仓位计算 ==========
    def get_risk_for_interval(self, interval: str) -> float:
        """解析多周期风险: 纯数字=统一风险, 或 '15m:10,1h:20,4h:40,1d:80' 分层风险"""
        raw_risk = self.cfg.risk_per_trade
        try:
            return float(raw_risk)
        except (ValueError, TypeError):
            try:
                risk_map = {}
                parts = str(raw_risk).replace(' ', '').split(',')
                for p in parts:
                    if ':' in p:
                        k, v = p.split(':')
                        risk_map[k] = float(v)
                return risk_map.get(interval, list(risk_map.values())[0] if risk_map else 10.0)
            except Exception as e:
                self._log.error(f'解析多周期风险失败: {e}, 回退10U')
                return 10.0

    @staticmethod
    def _interval_seconds(interval: str) -> int:
        """将 15m/1h/4h/1d 这类周期转换为秒数, 作为K线计数的兜底换算。"""
        text = str(interval or "15m").strip().lower()
        try:
            if text.endswith("m"):
                return max(60, int(float(text[:-1]) * 60))
            if text.endswith("h"):
                return max(60, int(float(text[:-1]) * 3600))
            if text.endswith("d"):
                return max(60, int(float(text[:-1]) * 86400))
            return max(60, int(float(text) * 60))
        except Exception:
            return 15 * 60

    def _elapsed_closed_bars_since(self, df, start_time, interval: str) -> int:
        """统计 start_time 之后实际已经收盘的K线数量。"""
        if start_time is None:
            return 0
        try:
            start_dt = start_time if isinstance(start_time, datetime) else datetime.fromisoformat(str(start_time))
            now_dt = bj_now()
            if start_dt.tzinfo is None and now_dt.tzinfo is not None:
                start_dt = start_dt.replace(tzinfo=now_dt.tzinfo)
            elif start_dt.tzinfo is not None and now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=start_dt.tzinfo)
            fallback = max(0, int((now_dt - start_dt).total_seconds() // self._interval_seconds(interval)))
        except Exception:
            fallback = 0
        try:
            if df is None or len(df) == 0 or "ot" not in df.columns:
                return fallback
            open_ms = pd.to_numeric(df["ot"], errors="coerce").dropna()
            if open_ms.empty:
                return fallback
            # 项目内部统一使用 bj_now() 的北京时间显示值；K线开盘时间同步平移到同一时间基准。
            opens = pd.to_datetime(open_ms.astype("int64"), unit="ms", utc=True) + pd.Timedelta(hours=8)
            bar_delta = pd.Timedelta(seconds=self._interval_seconds(interval))
            closes = opens + bar_delta
            start_ts = pd.Timestamp(start_time)
            if start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize("UTC")
            now_ts = pd.Timestamp(bj_now())
            return int(((closes > start_ts) & (closes <= now_ts)).sum())
        except Exception:
            return fallback

    def get_time_stop_bars(self, interval: str) -> int:
        """解析未起爆超时K数: 纯数字=统一K数, 或 '15m:6,1h:5,4h:4,1d:3'。"""
        raw = getattr(self.cfg, "time_stop_bars", "15m:6,1h:5,4h:4,1d:3")
        try:
            return int(float(raw))
        except (ValueError, TypeError):
            try:
                bar_map = {}
                parts = str(raw).replace(' ', '').split(',')
                for p in parts:
                    if ':' in p:
                        k, v = p.split(':', 1)
                        bar_map[k] = int(float(v))
                return bar_map.get(interval, list(bar_map.values())[0] if bar_map else 6)
            except Exception as e:
                self._log.error(f'解析未起爆超时K数失败: {e}, 回退6根')
                return 6

    def calc_position_size(self, symbol, direction, entry_price, sl_price, source_interval='15m',
                           min_stop_pct: Optional[float] = None) -> tuple:
        """
        固定风险金额计算仓位 — 防滑点+强制抹零确保风险不超标
        返回: (quantity, position_usdt, actual_risk)
        """
        import math
        # 止损距离
        sl_dist = abs(entry_price - sl_price)
        if sl_dist <= 0:
            return 0, 0, 0

        # 防滑点风险放大: 强制最小止损空间。
        # 结构旧策略默认0.8%; RJ-only显式传入rj_only_min_stop_pct, 避免实盘RJ仓位与SL口径错配。
        if min_stop_pct is None:
            min_stop_pct = 0.008
        try:
            min_stop_pct = max(0.0, float(min_stop_pct or 0.0))
        except Exception:
            min_stop_pct = 0.008
        min_distance = entry_price * min_stop_pct
        if sl_dist < min_distance:
            self._log.info(
                f'{symbol} SL距离{sl_dist*100/entry_price:.2f}%<{min_stop_pct*100:.2f}%, '
                f'按{min_stop_pct*100:.2f}%计算仓位'
            )
            sl_dist = min_distance

        # 最大安全数量 = 动态风险金额 / 止损距离, 受单笔名义价值上限约束
        dynamic_risk = self.get_risk_for_interval(source_interval)
        max_safe_qty = dynamic_risk / sl_dist
        max_val_qty = self.cfg.max_position_usdt / entry_price if self.cfg.max_position_usdt > 0 else float('inf')
        max_safe_qty = min(max_safe_qty, max_val_qty)

        # 强制精度截断防错配: 高价币小数位过多会导致止损单挂单失败
        if max_safe_qty >= 100:
            max_safe_qty = float(math.floor(max_safe_qty))
        elif max_safe_qty >= 10:
            max_safe_qty = float(math.floor(max_safe_qty * 10) / 10.0)
        else:
            max_safe_qty = float(math.floor(max_safe_qty * 100) / 100.0)

        if max_safe_qty <= 0:
            self._log.warning(f'[{symbol}] 抹零后数量为0, 放弃开仓')
            return 0, 0, 0

        # 向下取整到交易对精度(防止取整导致风险超标)
        quantity = self._floor_qty(symbol, max_safe_qty)
        if quantity <= 0:
            return 0, 0, 0

        position_usdt = quantity * entry_price
        if position_usdt < 10:
            return 0, 0, 0

        actual_risk = quantity * sl_dist
        if actual_risk > dynamic_risk * 1.02:
            self._log.warning(f"{symbol} 取整后风险${actual_risk:.2f}超标 "
                            f"(预算${dynamic_risk}), 放弃此次交易")
            return 0, 0, 0

        return quantity, position_usdt, actual_risk

    def _floor_qty(self, symbol, qty, market_order=True):
        """按交易对精度向下取整 (floor), 受交易所minQty/maxQty/step约束"""
        import math
        try:
            info = self.client.get_symbol_info(symbol)
            if info:
                filters = {
                    f.get("filterType"): f
                    for f in info.get("filters", [])
                    if f.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE")
                }
                preferred = "MARKET_LOT_SIZE" if market_order else "LOT_SIZE"
                f = filters.get(preferred) or filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
                if f:
                    step = float(f["stepSize"])
                    min_qty = float(f.get("minQty", step))
                    max_qty = float(f.get("maxQty", 0))  # 0 = 不限
                    decimals = max(0, min(8, len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0))
                    floored = round(math.floor(qty / step) * step, decimals)
                    if max_qty > 0 and floored > max_qty:
                        floored = math.floor(max_qty / step) * step
                        self._log.info(f"{symbol} 数量超交易所上限, 限制至{floored}")
                    if floored < min_qty:
                        self._log.warning(f"{symbol} 安全数量{floored:.6f}<最小{min_qty}, 仓位过小")
                        return 0
                    return floored
        except:
            pass
        if qty > 100: return math.floor(qty * 10) / 10
        if qty > 1: return math.floor(qty * 1000) / 1000
        if qty > 0.01: return math.floor(qty * 100000) / 100000
        return math.floor(qty * 100000000) / 100000000

    def _prepare_futures_entry(self, symbol, entry_price, sl_price, qty):
        """下市价单前应用Binance杠杆和最大名义价值约束。"""
        entry_price = float(entry_price or 0.0)
        sl_price = float(sl_price or 0.0)
        qty = float(qty or 0.0)
        pos_usdt = qty * entry_price
        risk = qty * abs(entry_price - sl_price)
        requested = max(1, int(float(getattr(self.cfg, "leverage", 1) or 1)))
        meta = {
            "requested_leverage": requested,
            "effective_leverage": requested,
            "max_notional_value": 0.0,
        }
        if self.client is None or self.cfg.market_type != "futures":
            return qty, pos_usdt, risk, meta
        if str(getattr(self.cfg, "exchange", "") or "").lower() != "binance":
            self.client.set_leverage(symbol, requested)
            return qty, pos_usdt, risk, meta

        result = self.client.set_compatible_leverage(symbol, requested)
        if not result:
            meta["reason"] = "leverage_unavailable"
            return 0.0, 0.0, 0.0, meta
        if isinstance(result, dict):
            meta["effective_leverage"] = int(float(result.get("leverage") or requested))
            try:
                meta["max_notional_value"] = float(result.get("maxNotionalValue") or 0.0)
            except (TypeError, ValueError):
                meta["max_notional_value"] = 0.0

        max_notional = meta["max_notional_value"]
        if max_notional > 0 and pos_usdt > max_notional * 0.98:
            qty = self._floor_qty(symbol, max_notional * 0.98 / entry_price)
            pos_usdt = qty * entry_price
            risk = qty * abs(entry_price - sl_price)
            meta["notional_capped"] = True
        else:
            meta["notional_capped"] = False
        if qty <= 0:
            meta["reason"] = "qty_too_small_after_leverage"
            return 0.0, 0.0, 0.0, meta
        return qty, pos_usdt, risk, meta

    # ========== 风控检查 ==========
    def _check_risk_limits(self) -> bool:
        """全局风控: 返回是否允许开新仓"""
        today = bj_now().strftime("%Y-%m-%d")
        if today != self.last_trade_day:
            self.daily_loss = 0.0
            self.last_trade_day = today

        if self.cfg.max_daily_loss > 0 and self.daily_loss >= self.cfg.max_daily_loss:
            self._log.warning(f"日亏损已达上限 ${self.cfg.max_daily_loss}, 暂停开仓")
            return False

        if self.consecutive_losses >= self.cfg.max_consecutive_loss:
            self._log.warning(f"连续止损 {self.consecutive_losses} 次, 暂停开仓")
            return False

        if self.cooldown_until and bj_now() < self.cooldown_until:
            return False

        if self.cfg.fuel_enabled and self.cfg.fuel_balance <= 0:
            self._log.warning(f"燃料余额不足 (${self.cfg.fuel_balance:.2f}), 暂停开仓")
            return False

        if len(self.positions) >= self.cfg.max_positions:
            return False

        return True

    def _enter_key_candle_position(self, signal: dict, source_strategy: str) -> Optional[Position]:
        """Shared key-candle entry path for RJ-only and Predicta/EWO."""
        source_strategy = str(source_strategy or "").strip().lower()
        if source_strategy not in ("rj_only", "predicta_ewo"):
            return None
        strategy_label = "RJ-only" if source_strategy == "rj_only" else "Predicta/EWO"
        signal["source_strategy"] = source_strategy
        symbol = signal.get("symbol", "")
        direction = signal.get("direction", "")
        signal_interval = str(signal.get("source_interval") or getattr(self.cfg, "scan_interval", "30m")).split(",")[0].strip() or "30m"
        if not symbol or direction not in ("LONG", "SHORT"):
            return None
        if not self._check_risk_limits():
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "risk_limits",
                "positions": len(self.positions),
                "daily_loss": self.daily_loss,
                "consecutive_losses": self.consecutive_losses,
            }))
            return None
        if source_strategy == "rj_only" and not signal.get("rj_only_stats_pass", True):
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "rj_only_stats_failed",
                "stats_reason": signal.get("rj_only_stats_reason", ""),
            }))
            return None
        if float(signal.get("score", 0.0) or 0.0) < float(self.cfg.min_score):
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "score_below_min",
                "min_score": self.cfg.min_score,
            }))
            return None
        if self.client is None and self.cfg.mode != "paper":
            self._log.warning(f"{strategy_label}未配置测试网API, 无法真实模拟下单")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "api_not_configured",
                "mode": self.cfg.mode,
                "testnet": self.cfg.testnet,
            }))
            return None
        if symbol in self._closed_positions and not self._can_reenter(symbol, direction, None, interval=signal_interval):
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "reentry_filter",
                "interval": signal_interval,
            }))
            return None
        if any(p.symbol == symbol for p in self.positions):
            return None
        if not self._is_live_trade_symbol(symbol):
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "symbol_unavailable",
                "market_type": self.cfg.market_type,
                "exchange": self.cfg.exchange,
                "testnet": self.cfg.testnet,
            }))
            return None

        lookback = (
            max(220, min(1000, int(getattr(self.cfg, "rj_only_stats_lookback_bars", 1000) or 1000)))
            if source_strategy == "rj_only" else 160
        )
        df = fetch_klines(
            symbol, signal_interval, lookback,
            exchange=self.cfg.exchange,
            closed_only=True,
        )
        if df is None or len(df) < 40:
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "kline_fetch_failed",
                "kline_len": len(df) if df is not None else 0,
            }))
            return None
        if source_strategy == "rj_only" and bool(getattr(self.cfg, "rj_only_volume_filter", True)):
            if not signal.get("rj_volume_filter_pass", True):
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "rj_volume_filter_failed",
                    "interval": signal_interval,
                }))
                return None
        entry_price = float(signal.get("price") or df["c"].iloc[-1])
        stop_field = "rj_only_stop_price" if source_strategy == "rj_only" else "predicta_stop_price"
        stop_raw = signal.get(stop_field)
        sl_price = float(stop_raw or 0.0)
        if entry_price <= 0 or sl_price <= 0:
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": f"missing_{stop_field}" if not stop_raw else "invalid_key_candle_sl",
                "entry": entry_price,
                "sl": sl_price,
            }))
            return None
        if direction == "LONG" and sl_price >= entry_price:
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "invalid_sl",
                "entry": entry_price,
                "sl": sl_price,
            }))
            return None
        if direction == "SHORT" and sl_price <= entry_price:
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "invalid_sl",
                "entry": entry_price,
                "sl": sl_price,
            }))
            return None

        signal_key = str(signal.get("signal_key", "") or "")
        setup_key = str(signal.get("rj_setup_key", "") or "")
        signal_keys = []
        for key in (signal_key, setup_key):
            if key and key not in signal_keys:
                signal_keys.append(key)
        if self._any_signal_key_used(signal_keys):
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "same_signal_reuse",
                "interval": signal_interval,
                "signal_key": signal_key,
                "rj_setup_key": setup_key,
            }))
            return None
        failed_key = self._failed_signal_key(signal_keys)
        if failed_key:
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "order_failed_cooldown",
                "interval": signal_interval,
                "signal_key": signal_key,
                "failed_key": failed_key,
            }))
            return None

        min_stop_field = "rj_only_min_stop_pct" if source_strategy == "rj_only" else "predicta_min_stop_pct"
        key_min_stop_pct = max(0.0, float(getattr(self.cfg, min_stop_field, 0.003) or 0.0))
        qty, pos_usdt, risk = self.calc_position_size(
            symbol, direction, entry_price, sl_price,
            source_interval=signal_interval,
            min_stop_pct=key_min_stop_pct,
        )
        if qty <= 0:
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "qty_too_small",
                "entry": entry_price,
                "sl": sl_price,
                "pos_usdt": pos_usdt,
                "risk": risk,
            }))
            return None

        # RJ-only 不使用旧均线带作为入场/止损依据；这里仅保留Position字段兼容。
        initial_band_hi = entry_price
        initial_band_lo = entry_price
        target_zone = self._calc_target_zone(df, direction, entry_price, sl_price, signal_interval, signal)
        signal.update(target_zone)
        try:
            btc_regime_fields = self._btc_regime_fields()
        except Exception:
            btc_regime_fields = {"btc_regime": "btc_unknown", "btc_score": 0}
        coin_reversal_pass = self._btc_coin_reversal_pass(signal, direction)
        signal["btc_coin_reversal_pass"] = coin_reversal_pass
        btc_filter_ok, btc_filter_reason = self._btc_direction_filter(
            direction,
            btc_regime_fields,
            coin_reversal_pass=coin_reversal_pass,
        )
        if not btc_filter_ok:
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "btc_direction_filter",
                "btc_filter_reason": btc_filter_reason,
                "interval": signal_interval,
                "entry": entry_price,
                "sl": sl_price,
                "qty": qty,
                "pos_usdt": pos_usdt,
                "risk": risk,
                "btc_coin_reversal_pass": coin_reversal_pass,
                **btc_regime_fields,
            }))
            return None

        leverage_meta = {}
        if self.client is not None:
            info = self.client.get_symbol_info(symbol)
            if info is None or (isinstance(info, dict) and info.get("status") == "BREAK"):
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "symbol_unavailable",
                    "market_type": self.cfg.market_type,
                    "exchange": self.cfg.exchange,
                    "testnet": self.cfg.testnet,
                }))
                return None
            if self.cfg.market_type == "futures":
                qty, pos_usdt, risk, leverage_meta = self._prepare_futures_entry(
                    symbol, entry_price, sl_price, qty
                )
                if qty <= 0:
                    self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                        "reason": leverage_meta.get("reason", "leverage_unavailable"),
                        "entry": entry_price,
                        "sl": sl_price,
                        **leverage_meta,
                    }))
                    return None
            if pos_usdt < 5.5 and self.cfg.market_type == "futures":
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "min_notional",
                    "pos_usdt": pos_usdt,
                    "min_notional": 5.5,
                }))
                return None

        hermes_state = (
            self._hermes_confirm_entry(signal, df, entry_price, sl_price, qty, pos_usdt, risk)
            if source_strategy == "rj_only" else {"active": False, "pass": True}
        )
        if hermes_state.get("active"):
            event_type = "hermes_confirm_pass" if hermes_state.get("pass") else "hermes_confirm_block"
            hermes_public = self._public_hermes_confirm(hermes_state)
            self._append_signal_event(event_type, symbol, self._signal_snapshot(signal, {
                "entry": entry_price,
                "sl": sl_price,
                "qty": qty,
                "pos_usdt": pos_usdt,
                "risk": risk,
                "interval": signal_interval,
                "signal_key": signal_key,
                "hermes_confirm": hermes_public,
                "hermes_direction": hermes_public.get("direction", "NEUTRAL"),
                "hermes_decision": hermes_public.get("decision", ""),
                "hermes_confidence": hermes_public.get("confidence", 0.0),
            }))
            if not hermes_state.get("pass"):
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "hermes_confirm_block",
                    "interval": signal_interval,
                    "signal_key": signal_key,
                    "hermes_confirm": hermes_public,
                    "hermes_direction": hermes_public.get("direction", "NEUTRAL"),
                    "hermes_decision": hermes_public.get("decision", ""),
                    "hermes_confidence": hermes_public.get("confidence", 0.0),
                }))
                return None

        self._append_signal_event("entry_precheck_pass", symbol, self._signal_snapshot(signal, {
            "entry": entry_price,
            "sl": sl_price,
            "qty": qty,
            "pos_usdt": pos_usdt,
            "risk": risk,
            "interval": signal_interval,
            "signal_key": signal_key,
            "market_type": self.cfg.market_type,
            "exchange": self.cfg.exchange,
            "mode": self.cfg.mode,
            "testnet": self.cfg.testnet,
            **leverage_meta,
        }))

        fill_price = entry_price
        tracking_no = ""
        if self.client is not None:
            side = "BUY" if direction == "LONG" else "SELL"
            order = self.client.market_order(symbol, side, qty if self.cfg.market_type == "futures" else pos_usdt)
            if order is None or ("orderId" not in str(order) and "trackingNo" not in str(order)):
                self._log.error(f"{strategy_label}下单失败({symbol} {direction} qty={qty:.4f} pos=${pos_usdt:.0f}): {order}")
                for k in signal_keys:
                    self._mark_signal_failed(k, symbol, direction, signal_interval, signal, response=order)
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "order_failed",
                    "qty": qty,
                    "pos_usdt": pos_usdt,
                    "order_response": order,
                }))
                return None
            if isinstance(order, dict):
                tracking_no = order.get("trackingNo", "")
                fills = order.get("fills", [])
                if fills:
                    fill_prices = [float(f["price"]) for f in fills]
                    fill_qty = [float(f["qty"]) for f in fills]
                    fill_price = sum(p * q for p, q in zip(fill_prices, fill_qty)) / sum(fill_qty)
                else:
                    order_fill = self._extract_order_number(
                        order,
                        ("priceAvg", "avgPrice", "fillPrice", "dealAvgPrice", "tradePrice"),
                    )
                    if order_fill:
                        fill_price = order_fill
            sl_side = "SELL" if direction == "LONG" else "BUY"
            try:
                sl_result = self.client.stop_order(symbol, sl_side, round(sl_price, 8), round(qty, 8), tracking_no=tracking_no)
                if sl_result:
                    self._log.info(f"{strategy_label}交易所止损单已挂: {symbol} {sl_price:.4f}")
                else:
                    self._log.warning(f"{strategy_label}交易所止损单未挂出, bot内部兜底: {symbol} SL={sl_price:.4f}")
            except Exception as e:
                self._log.error(f"{strategy_label}[{symbol}] 止损单挂单异常: {e}, 该单目前无交易所止损保护!")

        pos = Position(
            symbol=symbol,
            direction=direction,
            entry_price=fill_price,
            entry_time=bj_now(),
            quantity=qty,
            sl_price=sl_price,
            initial_sl=sl_price,
            current_sl=sl_price,
            risk_usdt=risk,
            signal_score=float(signal.get("score", 0.0) or 0.0),
            initial_band_hi=initial_band_hi,
            initial_band_lo=initial_band_lo,
            tracking_no=tracking_no,
            source_interval=signal_interval,
            source_strategy=source_strategy,
            btc_regime_fields=btc_regime_fields,
            signal_key=signal_key,
            target_zone_type=target_zone.get("target_zone_type", "none"),
            target_zone_price=float(target_zone.get("target_zone_price", 0.0) or 0.0),
            target_zone_low=float(target_zone.get("target_zone_low", 0.0) or 0.0),
            target_zone_high=float(target_zone.get("target_zone_high", 0.0) or 0.0),
            target_r=float(target_zone.get("target_r", 0.0) or 0.0),
            target_distance_pct=float(target_zone.get("target_distance_pct", 0.0) or 0.0),
            target_zone_bars_ago=int(target_zone.get("target_zone_bars_ago", 0) or 0),
            hermes_confirm=self._public_hermes_confirm(hermes_state),
            choppy_filter=self._position_choppy_filter(signal),
        )
        pos.time_stop_armed = True
        pos.time_stop_armed_at = pos.entry_time
        self.positions.append(pos)
        for k in signal_keys:
            self._failed_signal_keys.pop(k, None)
            self._mark_signal_used(k, symbol, direction, signal_interval, signal)
        self._save_positions()
        self._append_signal_event("entry_filled", symbol, self._signal_snapshot(signal, {
            "entry": fill_price,
            "sl": sl_price,
            "fractal_sl": signal.get("fractal_sl"),
            "band_sl": sl_price,
            "qty": qty,
            "pos_usdt": pos_usdt,
            "risk": risk,
            "interval": signal_interval,
            "signal_key": signal_key,
            "tracking_no": tracking_no,
            "hermes_confirm": self._public_hermes_confirm(hermes_state),
            **target_zone,
            **btc_regime_fields,
            **leverage_meta,
        }))
        mode_label = "测试网真实模拟" if self.client is not None else "纸笔模拟"
        self._log.info(
            f"{strategy_label}{mode_label}开{direction}: {symbol} {signal_interval} "
            f"entry={fill_price:.6f} SL={sl_price:.6f} "
            f"score={float(signal.get('score', 0.0) or 0.0):.1f} "
            f"hist={signal.get('rj_only_hist_win_rate', 0)}%/{signal.get('rj_only_hist_samples', 0)}样本"
        )
        self.status_text = f"{strategy_label}开仓 {symbol} {direction}"
        return pos

    # ========== 入场 ==========
    def enter_rj_position(self, signal: dict) -> Optional[Position]:
        return self._enter_key_candle_position(signal, "rj_only")

    def enter_predicta_position(self, signal: dict) -> Optional[Position]:
        return self._enter_key_candle_position(signal, "predicta_ewo")

    def enter_position(self, signal: dict) -> Optional[Position]:
        """根据起爆点信号开仓"""
        if not self._check_risk_limits():
            self._append_signal_event("entry_reject", signal.get("symbol", ""), self._signal_snapshot(signal, {
                "reason": "risk_limits",
                "positions": len(self.positions),
                "daily_loss": self.daily_loss,
                "consecutive_losses": self.consecutive_losses,
            }))
            return None
        if signal["score"] < self.cfg.min_score:
            self._append_signal_event("entry_reject", signal.get("symbol", ""), self._signal_snapshot(signal, {
                "reason": "score_below_min",
                "min_score": self.cfg.min_score,
            }))
            return None
        if self.client is None and self.cfg.mode != "paper":
            self._log.warning("未配置API, 无法开仓")
            self._append_signal_event("entry_reject", signal.get("symbol", ""), self._signal_snapshot(signal, {
                "reason": "api_not_configured",
            }))
            return None

        symbol = signal["symbol"]
        direction = signal["direction"]
        signal_interval = str(signal.get("source_interval") or getattr(self.cfg, 'scan_interval', '15m').split(',')[0]).strip()
        # 最近平仓记录只用于防止旧盯防池信号复用；是否能再开仓交给当前形态全链路复核。
        if symbol in self._closed_positions:
            if not self._can_reenter(symbol, direction, None, interval=signal_interval):
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "reentry_filter",
                    "interval": signal_interval,
                }))
                return None
        entry_price = signal["price"]

        # HT分型结论由扫描器统一判定, 交易器不再重复检查避免矛盾

        # 获取入场时的最新均线带 (重新取K线确保准确)
        df = fetch_klines(symbol, signal_interval, 200, exchange=self.cfg.exchange)
        if df is None or len(df) < 150:
            self._log.info(f"{symbol} 入场前取K线失败 (len={len(df) if df is not None else 0}), 放弃")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "kline_fetch_failed",
                "kline_len": len(df) if df is not None else 0,
            }))
            return None

        closes = df["c"]
        max_band, min_band, spread_arr = calc_ma_band(closes)
        n = len(closes)
        current_band_hi = max_band[-1]
        current_band_lo = min_band[-1]
        current_close = float(closes.iloc[-1])
        if np.isnan(current_band_hi) or np.isnan(current_band_lo):
            self._log.info(f"{symbol} 入场前均线带无效({signal_interval}), 放弃")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "band_invalid",
                "interval": signal_interval,
            }))
            return None
        chain_details = verify_pool_signal_details(df, direction, interval=signal_interval)
        if chain_details is None:
            self._log.info(
                f"{symbol} 入场前全链路复核失败({signal_interval} {direction}): "
                f"未满足 突破→回踩/反弹不破防线→分型确认"
            )
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "chain_verify_failed",
                "interval": signal_interval,
            }))
            return None
        chain_fractal_sl = chain_details.get("fractal_sl")
        chain_band_sl = chain_details.get("band_sl")
        if not chain_fractal_sl or not chain_band_sl:
            self._log.info(f"{symbol} 入场前止损锚点缺失({signal_interval}), 放弃")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "sl_anchor_missing",
                "interval": signal_interval,
            }))
            return None
        signal["fractal_sl"] = chain_fractal_sl
        signal["band_sl"] = chain_band_sl
        for k in (
            "breakout_bar", "retest_bar", "squeeze_start", "squeeze_end",
            "squeeze_tight_idx", "squeeze_edge", "first_fractal", "confirm_fractal",
            "first_fractal_bar", "confirm_fractal_bar"
        ):
            if k in chain_details:
                signal[k] = chain_details.get(k)

        try:
            band_spread_pct = (
                (float(current_band_hi) - float(current_band_lo)) / float(current_close) * 100.0
                if current_close > 0 else 0.0
            )
        except Exception:
            band_spread_pct = 0.0
        try:
            kline_time = int(float(df["ot"].iloc[-1])) if "ot" in df.columns else None
        except Exception:
            kline_time = None
        signal.update({
            "chain_verified": True,
            "entry_precheck_price": float(current_close),
            "entry_precheck_band_hi": float(current_band_hi),
            "entry_precheck_band_lo": float(current_band_lo),
            "entry_precheck_band_spread_pct": round(float(band_spread_pct), 4),
            "entry_precheck_kline_time": kline_time,
        })

        signal_keys = self._build_signal_keys(symbol, direction, signal_interval, signal, df=df)
        signal_key = signal_keys[0] if signal_keys else ""
        signal["signal_key"] = signal_key

        rj_ok, rj_state = self._apply_rj_entry_filter(df, symbol, direction, signal_interval, signal)
        self._append_signal_event("rj_filter_check", symbol, self._signal_snapshot(signal, {
            "reason": "rj_filter_pass" if rj_ok else "rj_filter_failed",
            "interval": signal_interval,
            "signal_key": signal_key,
            **rj_state,
        }))
        if not rj_ok:
            self._log.info(
                f"{symbol} RJ开仓过滤拒绝({signal_interval} {direction}): "
                f"mode={rj_state.get('rj_filter_mode')} "
                f"reason={rj_state.get('rj_filter_reason')} "
                f"J={rj_state.get('rj_j')} R={rj_state.get('rj_r')} "
                f"spread={rj_state.get('rj_spread')} "
                f"recent={rj_state.get('rj_recent_cross')} "
                f"barsAgo={rj_state.get('rj_cross_bars_ago')} key={signal_key}"
            )
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "rj_filter_failed",
                "interval": signal_interval,
                "signal_key": signal_key,
                **rj_state,
            }))
            return None

        if rj_state.get("rj_filter_mode") != "off":
            self._log.info(
                f"{symbol} RJ开仓过滤通过({signal_interval} {direction}): "
                f"mode={rj_state.get('rj_filter_mode')} "
                f"J={rj_state.get('rj_j')} R={rj_state.get('rj_r')} "
                f"spread={rj_state.get('rj_spread')} "
                f"cross={rj_state.get('rj_cross_type')} "
                f"barsAgo={rj_state.get('rj_cross_bars_ago')} key={signal_key}"
            )

        if self._any_signal_key_used(signal_keys):
            self._log.info(f"{symbol} 同结构信号已使用({signal_interval}), 拒绝旧形态复进 key={signal_key}")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "same_signal_reuse",
                "interval": signal_interval,
                "signal_key": signal_key,
            }))
            return None
        failed_key = self._failed_signal_key(signal_keys)
        if failed_key:
            meta = self._failed_signal_keys.get(failed_key, {})
            self._log.info(f"{symbol} 同结构下单失败冷却中({signal_interval}), 暂停重试 key={failed_key}")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "order_failed_cooldown",
                "interval": signal_interval,
                "signal_key": signal_key,
                "failed_key": failed_key,
                "failed_at": meta.get("time", ""),
            }))
            return None
        entry_price = current_close

        # 止损: 突破后确认分型 + 入场确认K线六线边缘, 取结构外侧
        band_sl = float(chain_band_sl)
        scan_sl = signal.get("fractal_sl")
        if scan_sl is not None and isinstance(scan_sl, (int, float)) and scan_sl > 0:
            fractal_sl = scan_sl * (0.998 if direction == "LONG" else 1.002)
        else:
            fractal_sl = self._find_fractal_sl(df, direction, entry_price)
        if fractal_sl is not None:
            if direction == "LONG":
                sl_price = min(fractal_sl, band_sl)
            else:
                sl_price = max(fractal_sl, band_sl)
            self._log.info(f"{symbol} SL=分型{fractal_sl:.4f} 边缘{band_sl:.4f} → 外侧{sl_price:.4f}")
        else:
            sl_price = band_sl
            self._log.info(f"{symbol} SL=均线边缘兜底 {sl_price:.4f}")

        # 确保SL合理。结构止损落在入场价错误一侧时, 直接放弃, 不做人造2%止损。
        if direction == "LONG" and sl_price >= entry_price:
            self._log.info(f"{symbol} 止损无效(LONG SL{sl_price:.4f}>=入场{entry_price:.4f}), 放弃")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "invalid_sl",
                "entry": entry_price,
                "sl": sl_price,
            }))
            return None
        if direction == "SHORT" and sl_price <= entry_price:
            self._log.info(f"{symbol} 止损无效(SHORT SL{sl_price:.4f}<=入场{entry_price:.4f}), 放弃")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "invalid_sl",
                "entry": entry_price,
                "sl": sl_price,
            }))
            return None

        # 计算仓位
        qty, pos_usdt, risk = self.calc_position_size(
            symbol, direction, entry_price, sl_price,
            source_interval=signal_interval
        )
        if qty <= 0:
            self._log.info(f"{symbol} 仓位过小跳过 (pos=${pos_usdt:.0f} risk=${risk:.2f})")
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "qty_too_small",
                "entry": entry_price,
                "sl": sl_price,
                "pos_usdt": pos_usdt,
                "risk": risk,
            }))
            return None

        target_zone = self._calc_target_zone(df, direction, entry_price, sl_price, signal_interval, signal)
        signal.update(target_zone)
        if target_zone.get("target_zone_type") != "none":
            self._log.info(
                f"{symbol} 目标区={target_zone.get('target_zone_type')} "
                f"price={target_zone.get('target_zone_price'):.6f} "
                f"space={target_zone.get('target_r'):.2f}R"
            )
        else:
            self._log.info(f"{symbol} 前方{signal_interval}目标区未识别, 仅记录为none")

        # 验证交易对存在 + 名义价值满足交易所最低要求
        if self.client is not None:
            info = self.client.get_symbol_info(symbol)
            if info is None or (isinstance(info, dict) and info.get("status") == "BREAK"):
                self._log.info(f"{symbol} 测试网合约不存在, 跳过")
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "symbol_unavailable",
                }))
                return None
            if pos_usdt < 5.5 and self.cfg.market_type == "futures":
                self._log.info(f"{symbol} 名义价值${pos_usdt:.0f}<$5.5, 低于交易所最低限额, 跳过")
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "min_notional",
                    "pos_usdt": pos_usdt,
                    "min_notional": 5.5,
                }))
                return None

        # 合约: 先设置杠杆
        if self.client is not None and self.cfg.market_type == "futures":
            self.client.set_leverage(symbol, self.cfg.leverage)

        self._append_signal_event("entry_precheck_pass", symbol, self._signal_snapshot(signal, {
            "reason": "ready_to_order",
            "interval": signal_interval,
            "signal_key": signal_key,
            "entry": entry_price,
            "sl": sl_price,
            "fractal_sl": fractal_sl,
            "band_sl": band_sl,
            "qty": qty,
            "pos_usdt": pos_usdt,
            "risk": risk,
            "risk_pct_of_notional": round((risk / pos_usdt * 100.0), 4) if pos_usdt else 0.0,
            "market_type": self.cfg.market_type,
            "exchange": self.cfg.exchange,
            "mode": self.cfg.mode,
            "leverage": self.cfg.leverage,
            **target_zone,
            **rj_state,
        }))

        # 下单 (纸笔模式跳过交易所)
        fill_price = entry_price
        tracking_no = ""
        if self.client is not None:
            side = "BUY" if direction == "LONG" else "SELL"
            order = self.client.market_order(symbol, side, qty if self.cfg.market_type == "futures" else pos_usdt)
            if order is None or ("orderId" not in str(order) and "trackingNo" not in str(order)):
                self._log.error(f"下单失败({symbol} {direction} qty={qty:.4f} pos=${pos_usdt:.0f}): {order}")
                for k in signal_keys:
                    self._mark_signal_failed(k, symbol, direction, signal_interval, signal, response=order)
                self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                    "reason": "order_failed",
                    "qty": qty,
                    "pos_usdt": pos_usdt,
                    "order_response": order,
                }))
                return None
            # 捕获带单追踪号
            if isinstance(order, dict):
                tracking_no = order.get("trackingNo", "")
                fills = order.get("fills", [])
                if fills:
                    fill_prices = [float(f["price"]) for f in fills]
                    fill_qty = [float(f["qty"]) for f in fills]
                    fill_price = sum(p * q for p, q in zip(fill_prices, fill_qty)) / sum(fill_qty)
                else:
                    order_fill = self._extract_order_number(
                        order,
                        ("priceAvg", "avgPrice", "fillPrice", "dealAvgPrice", "tradePrice"),
                    )
                    if order_fill:
                        fill_price = order_fill

        # 设置止损单到交易所(纸笔模式走bot内部兜底)
        if self.client is not None:
            sl_side = "SELL" if direction == "LONG" else "BUY"
            try:
                sl_result = self.client.stop_order(symbol, sl_side, round(sl_price, 8), round(qty, 8), tracking_no=tracking_no)
                if sl_result:
                    self._log.info(f"交易所止损单已挂: {symbol} {sl_price:.4f}")
                else:
                    self._log.warning(f"交易所止损单挂单失败, bot内部兜底: {symbol} SL={sl_price:.4f}")
            except Exception as e:
                self._log.error(f"[{symbol}] 止损单挂单异常: {e}, 该单目前无交易所止损保护!")

        try:
            btc_regime_fields = self._btc_regime_fields()
        except Exception:
            btc_regime_fields = {"btc_regime": "btc_unknown", "btc_score": 0}

        pos = Position(
            symbol=symbol,
            direction=direction,
            entry_price=fill_price,
            entry_time=bj_now(),
            quantity=qty,
            sl_price=sl_price,
            initial_sl=sl_price,
            current_sl=sl_price,
            risk_usdt=risk,
            signal_score=signal["score"],
            initial_band_hi=max_band[-1],
            tracking_no=tracking_no,
            source_interval=signal_interval,
            source_strategy=str(signal.get("source_strategy", "structure") or "structure"),
            initial_band_lo=min_band[-1],
            btc_regime_fields=btc_regime_fields,
            signal_key=signal_key,
            target_zone_type=target_zone.get("target_zone_type", "none"),
            target_zone_price=float(target_zone.get("target_zone_price", 0.0) or 0.0),
            target_zone_low=float(target_zone.get("target_zone_low", 0.0) or 0.0),
            target_zone_high=float(target_zone.get("target_zone_high", 0.0) or 0.0),
            target_r=float(target_zone.get("target_r", 0.0) or 0.0),
            target_distance_pct=float(target_zone.get("target_distance_pct", 0.0) or 0.0),
            target_zone_bars_ago=int(target_zone.get("target_zone_bars_ago", 0) or 0),
        )
        pos.time_stop_armed = True
        pos.time_stop_armed_at = pos.entry_time
        self.positions.append(pos)
        for k in signal_keys:
            self._failed_signal_keys.pop(k, None)
            self._mark_signal_used(k, symbol, direction, signal_interval, signal)
        self._save_positions()
        self._append_signal_event("entry_filled", symbol, self._signal_snapshot(signal, {
            "entry": fill_price,
            "sl": sl_price,
            "fractal_sl": fractal_sl,
            "band_sl": band_sl,
            "qty": qty,
            "pos_usdt": pos_usdt,
            "risk": risk,
            "interval": signal_interval,
            "signal_key": signal_key,
            "tracking_no": tracking_no,
            **target_zone,
            **btc_regime_fields,
        }))

        self._log.info(f"开{direction}: {symbol} 价{fill_price:.4f} 量{qty} "
                 f"SL{sl_price:.4f} 风险${risk:.2f} 评分{signal['score']:.0f}")
        self.status_text = f"开仓: {symbol} {direction}"
        self._send_telegram(
            f"<b>开{direction}</b>\n"
            f"币种: {symbol}\n"
            f"入场: {fill_price:.4f}\n"
            f"止损: {sl_price:.4f}\n"
            f"风险: ${risk:.2f}\n"
            f"评分: {signal['score']:.0f}"
        )
        return pos

    def _calc_atr(self, df, period=14):
        h = df["h"].values; l = df["l"].values; c = df["c"].values
        tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
        tr[0] = h[0] - l[0]
        atr_arr = pd.Series(tr).rolling(period).mean().values
        return atr_arr[-1] if not np.isnan(atr_arr[-1]) else 0

    # ========== 分型止损 ==========
    def _find_fractal_sl(self, df, direction, entry_price):
        """严格标准顶底分型检测 (量化三要素: 极值定位→防线不破→收盘实体破位)
        底分型: 下跌后K₀最低 + 后续不创新低 + 收盘突破K₀最高价 → SL=K₀低*0.998
        顶分型: 上涨后K₀最高 + 后续不创新高 + 收盘跌破K₀最低价 → SL=K₀高*1.002
        """
        closes = df["c"].values; highs = df["h"].values; lows = df["l"].values
        n = len(closes)
        search_range = min(50, n - 5)
        if search_range < 10: return None

        # 数据清洗: 剔除包含关系, 得到标准K线序列
        h, l, c, idx_map = self._clean_klines(highs, lows, closes)
        m = len(h)

        for start in range(min(m - 1, 49), 4, -1):
            i = start

            if direction == "LONG":
                # 1. 极值定位: K₀是最近下跌后的最低点
                if i <= 0 or i >= m - 1: continue
                if not (l[i] < l[i-1] and l[i] < l[i+1]): continue
                if l[i] >= entry_price: continue
                # 验证经历一段下跌: K₀之前的K线均价显著高于K₀
                pre_avg = sum(h[2:i]) / max(i-2, 1) if i > 2 else h[2]
                if l[i] > pre_avg * 0.97: continue  # 跌幅不足3%

                # 2. 防线不破(容错0.3%): K₀之后最低价不破K₀低点
                no_break = True
                for j in range(i+1, m):
                    if l[j] < l[i] * 0.997:
                        no_break = False; break
                if not no_break: continue

                # 3. 实体突破: 某根K线收盘价 > K₀最高价
                confirmed = False
                for j in range(i+1, m):
                    if c[j] > h[i]:
                        confirmed = True; break
                if not confirmed: continue

                sl = l[i] * 0.998
                orig_i = idx_map[i] if i < len(idx_map) else i
                self._log.info(f"底分型✅ K{orig_i} 低{l[i]:.4f} 防线不破 实体突破{h[i]:.4f} SL={sl:.4f}")
                return sl

            else:  # SHORT
                if i <= 0 or i >= m - 1: continue
                if not (h[i] > h[i-1] and h[i] > h[i+1]): continue
                if h[i] <= entry_price: continue
                # 验证经历一段上涨
                pre_avg = sum(l[2:i]) / max(i-2, 1) if i > 2 else l[2]
                if h[i] < pre_avg * 1.03: continue  # 涨幅不足3%

                # 2. 防线不破(容错0.3%): K₀之后最高价不破K₀高点
                no_break = True
                for j in range(i+1, m):
                    if h[j] > h[i] * 1.003:
                        no_break = False; break
                if not no_break: continue

                # 3. 实体跌破: 某根K线收盘价 < K₀最低价
                confirmed = False
                for j in range(i+1, m):
                    if c[j] < l[i]:
                        confirmed = True; break
                if not confirmed: continue

                sl = h[i] * 1.002
                orig_i = idx_map[i] if i < len(idx_map) else i
                self._log.info(f"顶分型✅ K{orig_i} 高{h[i]:.4f} 防线不破 实体跌破{l[i]:.4f} SL={sl:.4f}")
                return sl

        return None

    def _clean_klines(self, highs, lows, closes):
        """K线包含关系处理: 合并被包含的K线, 返回标准序列"""
        n = len(highs)
        if n < 3: return highs, lows, closes, list(range(n))
        h, l, c = [], [], []
        idx_map = []  # 映射新索引到原始索引
        i = 0
        while i < n:
            if i == 0:
                h.append(highs[i]); l.append(lows[i]); c.append(closes[i])
                idx_map.append(i); i += 1; continue
            # 判断包含关系: 前一根高低完全包裹当前K线
            if highs[i] <= h[-1] and lows[i] >= l[-1]:
                # 当前被前一根包含, 按趋势方向处理
                if len(h) >= 2 and h[-1] > h[-2]:  # 上涨: 取高高
                    h[-1] = max(h[-1], highs[i])
                    l[-1] = max(l[-1], lows[i])
                elif len(h) >= 2 and h[-1] < h[-2]:  # 下跌: 取低低
                    h[-1] = min(h[-1], highs[i])
                    l[-1] = min(l[-1], lows[i])
                else:  # 趋势不明: 取更大范围
                    h[-1] = max(h[-1], highs[i])
                    l[-1] = min(l[-1], lows[i])
                c[-1] = closes[i]  # 收盘更新到当前
                idx_map[-1] = i
                i += 1; continue
            if highs[i] >= h[-1] and lows[i] <= l[-1]:
                # 当前包裹前一根 (反向包含)
                if len(h) >= 2 and h[-1] > h[-2]:  # 上涨途中
                    h[-1] = highs[i]; l[-1] = max(l[-1], lows[i])
                elif len(h) >= 2 and h[-1] < h[-2]:  # 下跌途中
                    l[-1] = lows[i]; h[-1] = min(h[-1], highs[i])
                else:
                    h[-1] = highs[i]; l[-1] = lows[i]
                c[-1] = closes[i]
                idx_map[-1] = i
                i += 1; continue
            # 无包含关系: 新增独立K线
            h.append(highs[i]); l.append(lows[i]); c.append(closes[i])
            idx_map.append(i); i += 1
        return (np.array(h), np.array(l), np.array(c), idx_map)

    def _find_fractal_sl_from_exchange(self, symbol, direction, entry_price):
        """恢复持仓时补搜最近分型止损(带K线获取)"""
        try:
            df = fetch_klines(symbol, getattr(self.cfg, 'scan_interval', '15m').split(',')[0].strip(), 200, exchange=self.cfg.exchange)
            if df is not None and len(df) >= 50:
                return self._find_fractal_sl(df, direction, entry_price)
        except:
            pass
        return None

    def _check_ht_trend_conflict(self, symbol, direction, interval):
        """大周期结构性反转冲突检查 (严格分型标准)。
        做空: 大周期出现下跌后底分型确认反弹 → 禁止做空。
        做多: 大周期出现上涨后顶分型确认回调 → 禁止做多。
        返回 (blocked:bool, reason:str)
        """
        ht_map = {"1h":"4h","4h":"1d","1d":"1w","15m":"1h"}
        ht_interval = ht_map.get(interval, "4h")
        df = fetch_klines(symbol, ht_interval, 120, exchange=self.cfg.exchange)
        if df is None or len(df) < 50:
            return False, ""

        closes = df["c"].values; highs = df["h"].values; lows = df["l"].values
        # 数据清洗: 剔除包含关系
        ch, cl, cc, im = self._clean_klines(highs, lows, closes)
        m = len(ch)
        if m < 10: return False, ""

        # 用严格三要素找最近的底分型和顶分型
        last_bottom = -1; last_top = -1
        for i in range(m - 1, 3, -1):
            # 底分型: 极值+防线不破+实体突破
            if last_bottom < 0:
                if cl[i] < cl[i-1] and cl[i] < cl[i+1]:  # K₀极值
                    no_break = all(cl[j] >= cl[i] * 0.997 for j in range(i+1, m))
                    confirmed = cc[-1] > ch[i]
                    if no_break and confirmed:
                        last_bottom = im[i] if i < len(im) else i
            # 顶分型: 极值+防线不破+实体跌破
            if last_top < 0:
                if ch[i] > ch[i-1] and ch[i] > ch[i+1]:  # K₀极值
                    no_break = all(ch[j] <= ch[i] * 1.003 for j in range(i+1, m))
                    confirmed = cc[-1] < cl[i]
                    if no_break and confirmed:
                        last_top = im[i] if i < len(im) else i
            if last_bottom > 0 and last_top > 0: break

        if direction == "SHORT":
            if last_bottom > 0 and last_top < 0:
                return True, f"{ht_interval}底分型确认反弹, 禁止做空"
            if last_bottom > last_top:
                return True, f"{ht_interval}底分型({last_bottom})新于顶分型({last_top}), 禁止做空"
        else:
            if last_top > 0 and last_bottom < 0:
                return True, f"{ht_interval}顶分型确认回调, 禁止做多"
            if last_top > last_bottom:
                return True, f"{ht_interval}顶分型({last_top})新于底分型({last_bottom}), 禁止做多"

        return False, ""

    def _can_reenter(self, symbol, direction, df, interval="15m"):
        """最近平仓后的再入场检查: 不做时间冷却, 只清旧状态并交给当前形态复核。"""
        info = self._closed_positions.get(symbol)
        if not info:
            return True
        closed_at = info.get("time") or bj_now()
        try:
            if not isinstance(closed_at, datetime):
                closed_at = datetime.fromisoformat(str(closed_at))
            now = bj_now()
            if closed_at.tzinfo is None and now.tzinfo is not None:
                closed_at = closed_at.replace(tzinfo=now.tzinfo)
            elif closed_at.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=closed_at.tzinfo)
            elapsed = (now - closed_at).total_seconds()
        except Exception:
            elapsed = 0
        # 超过24小时清除
        if elapsed > 86400:
            del self._closed_positions[symbol]
            return True

        reason = str(info.get("reason", "") or "")
        self._log.info(
            f"{symbol} 最近平仓({reason}, {elapsed:.0f}s前), 不做时间冷却, 继续按当前形态全链路复核"
        )
        del self._closed_positions[symbol]
        return True

    @staticmethod
    def _calc_atr(df, period=14):
        """计算当前ATR值 (吊灯追踪用)"""
        try:
            h = df['h'].values; l = df['l'].values; c = df['c'].values
            tr = np.maximum(h - l,
                   np.maximum(abs(h - np.roll(c, 1)),
                              abs(l - np.roll(c, 1))))
            tr[0] = h[0] - l[0]
            atr_arr = pd.Series(tr).rolling(period).mean().values
            return float(atr_arr[-1]) if len(atr_arr) > 0 and not np.isnan(atr_arr[-1]) else 0.0
        except:
            return 0.0

    def _is_rj_only_position(self, pos: Position) -> bool:
        strategy = str(getattr(pos, 'source_strategy', '') or '').strip().lower()
        sig_upper = str(getattr(pos, 'signal_key', '') or '').upper()
        return strategy == 'rj_only' or sig_upper.startswith('RJ') or 'RJSETUP' in sig_upper

    @staticmethod
    def _is_time_stop_exempt_position(pos: Position) -> bool:
        strategy = str(getattr(pos, 'source_strategy', '') or '').strip().lower()
        signal_key = str(getattr(pos, 'signal_key', '') or '').upper()
        return strategy == 'predicta_ewo' or signal_key.startswith('PREDICTA|')

    def _time_stop_exit_decision(self, pos: Position, inv: str, elapsed_bars: int,
                                 current_r: Optional[float], min_progress_r: float,
                                 source_label: str) -> Optional[str]:
        if self._is_time_stop_exempt_position(pos):
            return None
        if not getattr(self.cfg, 'enable_time_stop', True):
            return None
        if pos.breakeven_triggered or pos.partial_tp_triggered:
            return None
        time_stop_bars = self.get_time_stop_bars(inv)
        if time_stop_bars <= 0 or elapsed_bars < time_stop_bars:
            return None

        mfe_r = float(getattr(pos, 'max_favorable_r', 0.0) or 0.0)
        if not self._is_rj_only_position(pos):
            if current_r is not None:
                if current_r < min_progress_r:
                    return (
                        f'未起爆超时退出({inv} {elapsed_bars}/{time_stop_bars}K, '
                        f'当前推进{current_r:.2f}R<{min_progress_r:.2f}R, '
                        f'最大推进{mfe_r:.2f}R, {source_label})'
                    )
                return None
            if mfe_r < min_progress_r:
                return (
                    f'未起爆超时退出({inv} {elapsed_bars}/{time_stop_bars}K, '
                    f'最大推进{mfe_r:.2f}R<{min_progress_r:.2f}R, {source_label})'
                )
            return None

        hard_loss_r = float(getattr(self.cfg, 'rj_time_stop_hard_loss_r', -0.8) or -0.8)
        extend_bars = max(0, int(getattr(self.cfg, 'rj_time_stop_extend_bars', 6) or 0))
        final_min_r = float(getattr(self.cfg, 'rj_time_stop_final_min_r', 0.3) or 0.3)
        final_bars = time_stop_bars + extend_bars

        recovered = (current_r is not None and current_r >= min_progress_r) or (
            current_r is None and mfe_r >= min_progress_r
        )
        if recovered:
            if getattr(pos, 'time_stop_watch', False):
                pos.time_stop_watch = False
                self._append_signal_event("time_stop_watch_recovered", pos.symbol, {
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "interval": inv,
                    "source_strategy": getattr(pos, "source_strategy", ""),
                    "elapsed_bars": elapsed_bars,
                    "base_bars": time_stop_bars,
                    "final_bars": final_bars,
                    "current_r": round(float(current_r), 4) if current_r is not None else None,
                    "mfe_r": round(mfe_r, 4),
                    "min_progress_r": min_progress_r,
                    "signal_key": getattr(pos, "signal_key", ""),
                })
                self._save_positions()
            return None

        if current_r is not None and current_r <= hard_loss_r:
            return (
                f'RJ未起爆硬止损超时退出({inv} {elapsed_bars}/{time_stop_bars}K, '
                f'当前推进{current_r:.2f}R<={hard_loss_r:.2f}R, '
                f'最大推进{mfe_r:.2f}R, {source_label})'
            )

        if elapsed_bars < final_bars:
            if not getattr(pos, 'time_stop_watch', False):
                pos.time_stop_watch = True
                pos.time_stop_watch_started_at = bj_now()
                pos.time_stop_first_seen_bars = int(elapsed_bars)
                pos.time_stop_first_seen_r = float(current_r) if current_r is not None else 0.0
                pos.time_stop_first_seen_mfe = mfe_r
                self._append_signal_event("time_stop_watch", pos.symbol, {
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "interval": inv,
                    "source_strategy": getattr(pos, "source_strategy", ""),
                    "elapsed_bars": elapsed_bars,
                    "base_bars": time_stop_bars,
                    "extend_bars": extend_bars,
                    "final_bars": final_bars,
                    "current_r": round(float(current_r), 4) if current_r is not None else None,
                    "mfe_r": round(mfe_r, 4),
                    "min_progress_r": min_progress_r,
                    "hard_loss_r": hard_loss_r,
                    "final_min_r": final_min_r,
                    "source": source_label,
                    "signal_key": getattr(pos, "signal_key", ""),
                })
                self._log.info(
                    f'RJ未起爆进入观察: {pos.symbol} {inv} {elapsed_bars}/{final_bars}K '
                    f'R={current_r if current_r is not None else "NA"} MFE={mfe_r:.2f}R'
                )
                self._save_positions()
            return None

        # RJ-only is a momentum reversal strategy, not the old MA squeeze breakout.
        # After the extended watch window, keep it as observation-only unless hard loss triggers.
        return None

        if current_r is not None:
            if current_r < final_min_r:
                return (
                    f'RJ未起爆延长观察后退出({inv} {elapsed_bars}/{final_bars}K, '
                    f'当前推进{current_r:.2f}R<{final_min_r:.2f}R, '
                    f'最大推进{mfe_r:.2f}R, {source_label})'
                )
            return None
        if mfe_r < final_min_r:
            return (
                f'RJ未起爆延长观察后退出({inv} {elapsed_bars}/{final_bars}K, '
                f'最大推进{mfe_r:.2f}R<{final_min_r:.2f}R, {source_label})'
            )
        return None

    # ========== 出场检查 ==========
    def check_exit(self, pos: Position) -> Optional[str]:
        """AXIOM三阶风控: 1.2R防守 → 2.0R减仓 → EMA棘轮追踪"""
        symbol = pos.symbol
        inv = getattr(pos, 'source_interval', self.cfg.scan_interval.split(',')[0] if ',' in self.cfg.scan_interval else self.cfg.scan_interval)
        inv = str(inv or '15m').split(',')[0].strip() or '15m'
        # 未起爆超时先做本地快判: 如果历史最大推进都没到阈值, 不等行情接口直接释放僵尸仓。
        # 若历史曾到过阈值, 继续拉当前价, 按"当前R"决定是否因回落失败而退出。
        if (getattr(self.cfg, 'enable_time_stop', True)
                and not pos.breakeven_triggered and not pos.partial_tp_triggered):
            time_stop_bars = self.get_time_stop_bars(inv)
            elapsed_bars = self._elapsed_closed_bars_since(None, pos.entry_time, inv)
            min_progress_r = float(getattr(self.cfg, 'time_stop_min_r', 0.6) or 0.6)
            if time_stop_bars > 0 and elapsed_bars >= time_stop_bars:
                quick_r = None
                try:
                    quick_price = float(getattr(pos, 'current_price', 0.0) or 0.0)
                    quick_risk = abs(pos.entry_price - pos.initial_sl) if pos.initial_sl > 0 else (
                        pos.risk_usdt / pos.quantity if pos.quantity > 0 else 0
                    )
                    if quick_price > 0 and quick_risk > 0:
                        quick_r = ((quick_price - pos.entry_price) / quick_risk if pos.direction == 'LONG'
                                   else (pos.entry_price - quick_price) / quick_risk)
                except:
                    quick_r = None
                if quick_r is not None:
                    reason = self._time_stop_exit_decision(
                        pos, inv, elapsed_bars, quick_r, min_progress_r, "local_price"
                    )
                    if reason:
                        return reason
                protection_on = (
                    bool(getattr(self.cfg, "enable_early_protect", True))
                    or float(getattr(self.cfg, "half_risk_trigger_r", 0.0) or 0.0) > 0
                )
                if quick_r is None and not protection_on:
                    reason = self._time_stop_exit_decision(
                        pos, inv, elapsed_bars, None, min_progress_r, "local_timer"
                    )
                    if reason:
                        return reason
        df = fetch_klines(
            symbol, inv, 100,
            exchange=self.cfg.exchange,
            market_type=self.cfg.market_type,
            testnet=self.cfg.testnet,
            price_type="mark",
        )
        if df is None or len(df) < 30:
            if (getattr(self.cfg, 'enable_time_stop', True)
                    and not pos.breakeven_triggered and not pos.partial_tp_triggered):
                time_stop_bars = self.get_time_stop_bars(inv)
                elapsed_bars = self._elapsed_closed_bars_since(df, pos.entry_time, inv)
                min_progress_r = float(getattr(self.cfg, 'time_stop_min_r', 0.6) or 0.6)
                reason = self._time_stop_exit_decision(
                    pos, inv, elapsed_bars, None, min_progress_r, "kline_fallback"
                )
                if reason:
                    return reason
            return None

        closes = df["c"]; highs = df["h"]; lows = df["l"]
        current_price = closes.iloc[-1]; current_high = highs.iloc[-1]; current_low = lows.iloc[-1]
        ema_ratchet_val = ema(closes, self.cfg.ema_ratchet).iloc[-1]

        post_entry_history = df.iloc[0:0]
        try:
            open_times = pd.to_numeric(df["ot"], errors="coerce")
            valid_open_times = open_times.dropna()
            if len(valid_open_times) > 0:
                entry_ms = _entry_ms_for_market_data(pos.entry_time, int(valid_open_times.max()))
                post_entry_history = df[open_times >= entry_ms]
        except Exception:
            pass

        # 1R锚点
        if pos.initial_sl > 0:
            initial_risk = abs(pos.entry_price - pos.initial_sl)
        else:
            initial_risk = pos.risk_usdt / pos.quantity if pos.quantity > 0 else 0.01

        # 实时浮盈 (用收盘价算r_multiple; 用极值算二阶触发)
        exchange_pnl = None
        if self.client is not None and self.cfg.market_type == "futures":
            try:
                for xp in (self.client.get_positions() or []):
                    if xp["symbol"] == symbol:
                        exchange_pnl = xp.get("unRealizedProfit", 0)
                        pos.pnl = exchange_pnl
                        pos.current_price = xp.get("markPrice", current_price)
                        pos.entry_price = xp.get("entryPrice", pos.entry_price)
                        self._refresh_target_metrics(pos)
                        current_price = xp.get("markPrice", current_price)
                        break
            except: pass
        unrealized_pnl = exchange_pnl if exchange_pnl is not None else (
            (current_price - pos.entry_price) * pos.quantity if pos.direction == "LONG"
            else (pos.entry_price - current_price) * pos.quantity)

        if len(post_entry_history) > 0:
            current_high = post_entry_history["h"].iloc[-1]
            current_low = post_entry_history["l"].iloc[-1]
        else:
            current_high = current_price
            current_low = current_price

        # R-Multiple (统一使用实时当前价 current_price)
        if initial_risk > 0:
            if pos.direction == "LONG":
                r_multiple = (float(current_price) - pos.entry_price) / initial_risk
                favorable_r = (float(current_high) - pos.entry_price) / initial_risk
                adverse_r = max(0.0, (pos.entry_price - float(current_low)) / initial_risk)
            else:
                r_multiple = (pos.entry_price - float(current_price)) / initial_risk
                favorable_r = (pos.entry_price - float(current_low)) / initial_risk
                adverse_r = max(0.0, (float(current_high) - pos.entry_price) / initial_risk)
        else:
            r_multiple = 0.0
            favorable_r = 0.0
            adverse_r = 0.0
        excursion_source = (
            "mark"
            if self.cfg.exchange == "binance" and self.cfg.market_type == "futures"
            else ""
        )
        if (excursion_source
                and getattr(pos, "excursion_price_source", "") != excursion_source
                and initial_risk > 0
                and len(post_entry_history) > 0):
            history = post_entry_history
            if pos.direction == "LONG":
                rebased_favorable = (float(history["h"].max()) - pos.entry_price) / initial_risk
                rebased_adverse = (pos.entry_price - float(history["l"].min())) / initial_risk
            else:
                rebased_favorable = (pos.entry_price - float(history["l"].min())) / initial_risk
                rebased_adverse = (float(history["h"].max()) - pos.entry_price) / initial_risk
            pos.max_favorable_r = max(0.0, float(r_multiple), float(rebased_favorable))
            pos.max_adverse_r = max(0.0, float(rebased_adverse))
            pos.excursion_price_source = excursion_source
            self._log.info(
                f"{symbol} MFE/MAE switched to mark-price basis: "
                f"MFE={pos.max_favorable_r:.2f}R MAE={pos.max_adverse_r:.2f}R"
            )
            self._save_positions()
        old_max_r = getattr(pos, 'max_favorable_r', 0.0)
        if favorable_r > old_max_r:
            pos.max_favorable_r = favorable_r
            min_progress_r = float(getattr(self.cfg, 'time_stop_min_r', 0.6) or 0.6)
            if favorable_r >= min_progress_r or favorable_r - old_max_r >= 0.05:
                self._save_positions()
        old_adverse_r = getattr(pos, 'max_adverse_r', 0.0)
        if adverse_r > old_adverse_r:
            pos.max_adverse_r = adverse_r
            if adverse_r - old_adverse_r >= 0.05:
                self._save_positions()

        half_trigger_r = max(
            0.0,
            float(getattr(self.cfg, "half_risk_trigger_r", 0.0) or 0.0),
        )
        early_trigger_r = max(
            0.1,
            float(getattr(self.cfg, "early_protect_r", 0.8) or 0.8),
        )
        protect_r = max(
            float(r_multiple),
            float(favorable_r),
            float(getattr(pos, "max_favorable_r", 0.0) or 0.0),
        )

        if (
            half_trigger_r > 0
            and not pos.partial_tp_triggered
            and initial_risk > 0
            and pos.quantity > 0
            and protect_r >= half_trigger_r
            and protect_r < early_trigger_r
        ):
            desired_sl = (
                pos.entry_price - initial_risk * 0.5
                if pos.direction == "LONG"
                else pos.entry_price + initial_risk * 0.5
            )
            should_move = (
                (pos.direction == "LONG" and desired_sl > pos.current_sl)
                or (pos.direction == "SHORT" and desired_sl < pos.current_sl)
            )
            already_tighter = (
                (pos.direction == "LONG" and pos.current_sl >= desired_sl)
                or (pos.direction == "SHORT" and pos.current_sl <= desired_sl)
            )
            valid_price_side = (
                (pos.direction == "LONG" and desired_sl < current_price)
                or (pos.direction == "SHORT" and desired_sl > current_price)
            )
            if should_move and not valid_price_side:
                self._log.warning(
                    f"半损保护跳过: {symbol} action=invalid_price_side "
                    f"current={current_price:.4f} target={desired_sl:.4f}"
                )
            elif should_move:
                old_sl = pos.current_sl
                applied = self.client is None
                stop_result = None
                if self.client is not None:
                    self.client.cancel_all_orders(symbol)
                    sl_side = "SELL" if pos.direction == "LONG" else "BUY"
                    stop_result = self.client.stop_order(
                        symbol,
                        sl_side,
                        round(desired_sl, 8),
                        round(pos.quantity, 8),
                        tracking_no=pos.tracking_no,
                    )
                    if bool(getattr(self.cfg, "testnet", False)):
                        applied = True
                    elif str(getattr(self.cfg, "exchange", "")).lower() == "bitget":
                        if pos.tracking_no:
                            applied = stop_result is not None
                        else:
                            applied = (
                                isinstance(stop_result, dict)
                                and (
                                    stop_result.get("code") == "00000"
                                    or bool(stop_result.get("orderId"))
                                )
                            )
                    else:
                        applied = bool(stop_result)
                if applied:
                    pos.current_sl = desired_sl
                    pos.half_risk_protected = True
                    self._log.info(
                        f"半损保护: {symbol} ({inv}) MFE={protect_r:.2f}R>={half_trigger_r:.2f}R "
                        f"SL {old_sl:.4f}->{pos.current_sl:.4f} 最大亏损收窄至0.50R"
                    )
                    self._append_signal_event("position_protect", symbol, {
                        "symbol": symbol,
                        "direction": pos.direction,
                        "interval": inv,
                        "reason": "half_risk_protect",
                        "entry": round(float(pos.entry_price), 8),
                        "initial_sl": round(float(pos.initial_sl), 8),
                        "current_price": round(float(current_price), 8),
                        "old_sl": round(float(old_sl), 8),
                        "new_sl": round(float(pos.current_sl), 8),
                        "r": round(float(r_multiple), 4),
                        "protect_r": round(float(protect_r), 4),
                        "mfe_r": round(float(getattr(pos, "max_favorable_r", 0.0)), 4),
                        "mae_r": round(float(getattr(pos, "max_adverse_r", 0.0)), 4),
                        "trigger_r": half_trigger_r,
                        "lock_r": -0.5,
                        "action": "half_risk_applied",
                        "quantity": round(float(pos.quantity), 8),
                        "signal_key": getattr(pos, "signal_key", ""),
                    })
                    self._save_positions()
                else:
                    self._log.error(
                        f"半损保护待重试: {symbol} action=retry_pending SL保持{old_sl:.4f}, "
                        f"目标{desired_sl:.4f}, stop_result={stop_result}"
                    )
            elif already_tighter and not pos.half_risk_protected:
                pos.half_risk_protected = True
                self._log.info(
                    f"半损保护状态恢复: {symbol} action=already_tighter "
                    f"current_sl={pos.current_sl:.4f} target={desired_sl:.4f}"
                )
                self._save_positions()

        # ==== 提前保护: 还没到1.2R之前, 先把最大亏损收窄到保本附近 ====
        if (getattr(self.cfg, 'enable_early_protect', True)
                and not pos.partial_tp_triggered
                and initial_risk > 0
                and pos.quantity > 0):
            early_lock_r = max(0.0, float(getattr(self.cfg, 'early_protect_lock_r', 0.0) or 0.0))
            if protect_r >= early_trigger_r:
                desired_sl = (
                    pos.entry_price + initial_risk * early_lock_r
                    if pos.direction == 'LONG'
                    else pos.entry_price - initial_risk * early_lock_r
                )
                should_move = (
                    (pos.direction == 'LONG' and desired_sl > pos.current_sl) or
                    (pos.direction == 'SHORT' and desired_sl < pos.current_sl)
                )
                if should_move:
                    old_sl = pos.current_sl
                    pos.current_sl = desired_sl
                    pos.half_risk_protected = True
                    pos.breakeven_triggered = True
                    pos.breakeven_cooldown = 3
                    self._log.info(
                        f'提前保护: {symbol} ({inv}) R={r_multiple:.2f}>={early_trigger_r:.2f} '
                        f'SL {old_sl:.4f}->{pos.current_sl:.4f} 锁{early_lock_r:.2f}R'
                    )
                    if self.client is not None:
                        self.client.cancel_all_orders(symbol)
                        sl_side = 'SELL' if pos.direction == 'LONG' else 'BUY'
                        self.client.stop_order(
                            symbol, sl_side, round(pos.current_sl, 8), round(pos.quantity, 8),
                            tracking_no=pos.tracking_no
                        )
                    self._append_signal_event("position_protect", symbol, {
                        "symbol": symbol,
                        "direction": pos.direction,
                        "interval": inv,
                        "reason": "early_protect",
                        "entry": round(float(pos.entry_price), 8),
                        "current_price": round(float(current_price), 8),
                        "old_sl": round(float(old_sl), 8),
                        "new_sl": round(float(pos.current_sl), 8),
                        "r": round(float(r_multiple), 4),
                        "protect_r": round(float(protect_r), 4),
                        "mfe_r": round(float(getattr(pos, 'max_favorable_r', 0.0)), 4),
                        "mae_r": round(float(getattr(pos, 'max_adverse_r', 0.0)), 4),
                        "trigger_r": early_trigger_r,
                        "lock_r": early_lock_r,
                        "quantity": round(float(pos.quantity), 8),
                        "signal_key": getattr(pos, "signal_key", ""),
                    })
                    self._save_positions()
                elif not pos.breakeven_triggered and (
                    (pos.direction == 'LONG' and pos.current_sl >= desired_sl) or
                    (pos.direction == 'SHORT' and pos.current_sl <= desired_sl)
                ):
                    pos.half_risk_protected = True
                    pos.breakeven_triggered = True
                    pos.breakeven_cooldown = 3
                    self._save_positions()

        # ==== 未起爆超时退出: 入场后迟迟没有进入一阶/三阶保护, 释放僵尸仓位 ====
        if (getattr(self.cfg, 'enable_time_stop', True)
                and not pos.breakeven_triggered and not pos.partial_tp_triggered):
            time_stop_bars = self.get_time_stop_bars(inv)
            elapsed_bars = self._elapsed_closed_bars_since(df, pos.entry_time, inv)
            min_progress_r = float(getattr(self.cfg, 'time_stop_min_r', 0.6) or 0.6)
            reason = self._time_stop_exit_decision(
                pos, inv, elapsed_bars, r_multiple, min_progress_r, "kline_current"
            )
            if reason:
                return reason

        # ==== 第二阶: 2.0R 减仓50% (优先执行) ====
        if r_multiple >= self.cfg.tier2_partial_r and not pos.partial_tp_triggered and pos.quantity > 0:
            close_qty = self._floor_qty(symbol, pos.quantity * 0.5)
            if close_qty <= 0 or close_qty >= pos.quantity:
                self._log.info(f'{symbol} 数量无法减仓50%, 直接启用追踪')
                pos.partial_tp_triggered = True
                pos.breakeven_triggered = True  # 核心：点亮一阶防守，防止被一阶覆盖
                new_sl = pos.entry_price + initial_risk if pos.direction == 'LONG' else pos.entry_price - initial_risk
                pos.current_sl = new_sl
                if self.client is not None:
                    self.client.cancel_all_orders(symbol)
                    sl_side = 'SELL' if pos.direction == 'LONG' else 'BUY'
                    self.client.stop_order(symbol, sl_side, round(pos.current_sl, 8), round(pos.quantity, 8), tracking_no=pos.tracking_no)
                self._save_positions()
            else:
                exit_side = 'SELL' if pos.direction == 'LONG' else 'BUY'
                if self.client is not None:
                    try:
                        order = self.client.market_order(symbol, exit_side, close_qty, reduce_only=True, tracking_no=pos.tracking_no)
                    except Exception as e:
                        self._log.error(f'[{symbol}] 减仓市价单异常: {e}, 跳过本轮')
                        order = None
                    if order is not None and not (isinstance(order, dict) and order.get('code')):
                        pos.quantity -= close_qty
                        pos.partial_tp_triggered = True
                        pos.breakeven_triggered = True  # 核心：点亮一阶防守
                        new_sl = pos.entry_price + initial_risk if pos.direction == 'LONG' else pos.entry_price - initial_risk
                        pos.current_sl = new_sl
                        mode_label = 'ATR吊灯' if getattr(self.cfg, 'use_atr_trail', False) else 'EMA棘轮'
                        self._log.info(f'2.0R减仓: {symbol} 抛{close_qty:.4f} 余{pos.quantity:.4f} SL锁1R->{new_sl:.4f} [三阶={mode_label}]')
                        # 写减仓交易记录到前端
                        partial_exchange_close = {}
                        partial_order_payload = order
                        if hasattr(self.client, "resolve_close_trade"):
                            try:
                                partial_exchange_close = self.client.resolve_close_trade(
                                    symbol,
                                    order_response=order,
                                    direction=pos.direction,
                                    quantity=close_qty,
                                ) or {}
                                if partial_exchange_close:
                                    partial_order_payload = {
                                        "order_response": order,
                                        "exchange_close": partial_exchange_close,
                                    }
                            except Exception as e:
                                self._log.warning(f"{symbol} 减仓明细反查失败: {e}")
                        partial_exit_price = self._extract_order_number(
                            partial_order_payload,
                            ("exit_price", "closeAvgPrice", "priceAvg", "avgPrice", "fillPrice", "dealAvgPrice", "tradePrice"),
                        ) or current_price
                        partial_estimated_pnl = self._calc_trade_pnl(
                            pos.direction, pos.entry_price, partial_exit_price, close_qty
                        )
                        partial_exchange_pnl = self._extract_order_number(
                            partial_order_payload,
                            ("exchange_pnl", "realizedPnl", "realizedPNL", "realisedPnl", "realizedPL", "totalProfits", "profit", "pnl"),
                            allow_negative=True,
                        )
                        partial_pnl = partial_exchange_pnl if partial_exchange_pnl is not None else partial_estimated_pnl
                        partial_source = str(partial_exchange_close.get("pnl_source") or "") or ("exchange_order" if partial_exchange_pnl is not None else (
                            "fill_estimate" if partial_exit_price != current_price else "estimate"
                        ))
                        self._append_trade({
                            'time': bj_now().isoformat(), 'symbol': symbol, 'direction': pos.direction,
                            'interval': getattr(pos, 'source_interval', ''),
                            'entry': round(pos.entry_price, 6), 'exit': round(partial_exit_price, 6),
                            'quantity': round(close_qty, 8),
                            'sl': round(new_sl, 6), 'pnl': round(partial_pnl, 2),
                            'pnl_pct': round((partial_pnl/(close_qty*pos.entry_price))*100,2) if pos.entry_price else 0,
                            'r': round(partial_pnl / pos.risk_usdt, 4) if pos.risk_usdt else 0.0,
                            'mfe_r': round(getattr(pos, 'max_favorable_r', 0.0), 4),
                            'mae_r': round(getattr(pos, 'max_adverse_r', 0.0), 4),
                            'reason': '二阶减仓50%', 'score': pos.signal_score, 'risk': round(pos.risk_usdt, 2),
                            'estimated_pnl': round(partial_estimated_pnl, 2),
                            'exchange_pnl': round(partial_exchange_pnl, 2) if partial_exchange_pnl is not None else None,
                            'exchange_net_profit': round(float(partial_exchange_close.get("exchange_net_profit")), 2) if partial_exchange_close.get("exchange_net_profit") is not None else None,
                            'exchange_fee': round(float(partial_exchange_close.get("exchange_fee")), 4) if partial_exchange_close.get("exchange_fee") is not None else None,
                            'exchange_funding': round(float(partial_exchange_close.get("exchange_funding")), 4) if partial_exchange_close.get("exchange_funding") is not None else None,
                            'pnl_source': partial_source,
                            'exit_price_source': 'order_fill' if partial_exit_price != current_price else 'mark_estimate',
                            'signal_key': getattr(pos, 'signal_key', ''),
                            **self._position_btc_fields(pos),
                        })
                        self.client.cancel_all_orders(symbol)
                        sl_side = 'SELL' if pos.direction == 'LONG' else 'BUY'
                        self.client.stop_order(symbol, sl_side, round(pos.current_sl, 8), round(pos.quantity, 8), tracking_no=pos.tracking_no)
                        self._save_positions()
                    else:
                        self._log.warning(f'2.0R减仓失败: {symbol}')
                else:
                    # Paper trading
                    pos.quantity -= close_qty
                    pos.partial_tp_triggered = True
                    pos.breakeven_triggered = True
                    new_sl = pos.entry_price + initial_risk if pos.direction == 'LONG' else pos.entry_price - initial_risk
                    pos.current_sl = new_sl
                    self._log.info(f'2.0R减仓(纸笔): {symbol} 抛{close_qty:.4f} 余{pos.quantity:.4f} SL锁1R->{new_sl:.4f}')
                    partial_pnl = self._calc_trade_pnl(pos.direction, pos.entry_price, current_price, close_qty)
                    self._append_trade({
                        'time': bj_now().isoformat(), 'symbol': symbol, 'direction': pos.direction,
                        'interval': getattr(pos, 'source_interval', ''),
                        'entry': round(pos.entry_price, 6), 'exit': round(current_price, 6),
                        'quantity': round(close_qty, 8),
                        'sl': round(new_sl, 6), 'pnl': round(partial_pnl, 2),
                        'pnl_pct': round((partial_pnl/(close_qty*pos.entry_price))*100,2) if pos.entry_price else 0,
                        'r': round(partial_pnl / pos.risk_usdt, 4) if pos.risk_usdt else 0.0,
                        'mfe_r': round(getattr(pos, 'max_favorable_r', 0.0), 4),
                        'mae_r': round(getattr(pos, 'max_adverse_r', 0.0), 4),
                        'reason': '二阶减仓50%', 'score': pos.signal_score, 'risk': round(pos.risk_usdt, 2),
                        'estimated_pnl': round(partial_pnl, 2),
                        'exchange_pnl': None,
                        'pnl_source': 'paper_estimate',
                        'exit_price_source': 'mark_estimate',
                        'signal_key': getattr(pos, 'signal_key', ''),
                        **self._position_btc_fields(pos),
                    })

        # ==== 第一阶: 1.2R 绝对防守, 可在提前保本后继续升级锁0.2R ====
        elif r_multiple >= self.cfg.tier1_defense_r:
            buf = initial_risk * 0.2
            desired_sl = pos.entry_price + buf if pos.direction == 'LONG' else pos.entry_price - buf
            should_move = (
                (pos.direction == 'LONG' and desired_sl > pos.current_sl) or
                (pos.direction == 'SHORT' and desired_sl < pos.current_sl)
            )
            if should_move:
                old_sl = pos.current_sl
                pos.breakeven_triggered = True
                pos.breakeven_cooldown = 3
                pos.current_sl = desired_sl
                self._log.info(f'1.2R防守升级: {symbol} SL {old_sl:.4f}->{pos.current_sl:.4f} R={r_multiple:.1f}')
                if self.client is not None:
                    self.client.cancel_all_orders(symbol)
                    sl_side = 'SELL' if pos.direction == 'LONG' else 'BUY'
                    self.client.stop_order(
                        symbol, sl_side, round(pos.current_sl, 8), round(pos.quantity, 8),
                        tracking_no=pos.tracking_no
                    )
                self._append_signal_event("position_protect", symbol, {
                    "symbol": symbol,
                    "direction": pos.direction,
                    "interval": inv,
                    "reason": "tier1_defense_upgrade",
                    "entry": round(float(pos.entry_price), 8),
                    "current_price": round(float(current_price), 8),
                    "old_sl": round(float(old_sl), 8),
                    "new_sl": round(float(pos.current_sl), 8),
                    "r": round(float(r_multiple), 4),
                    "mfe_r": round(float(getattr(pos, 'max_favorable_r', 0.0)), 4),
                    "mae_r": round(float(getattr(pos, 'max_adverse_r', 0.0)), 4),
                    "trigger_r": float(self.cfg.tier1_defense_r),
                    "lock_r": 0.2,
                    "quantity": round(float(pos.quantity), 8),
                    "signal_key": getattr(pos, "signal_key", ""),
                })
                self._save_positions()

        # ==== 第三阶: 终极追踪止盈 (ATR极值吊灯 / EMA20棘轮 双模式切换) ====
        # 提前保护只负责保本; 真正追踪仍等1.2R防守或2R减仓后启动。
        if pos.breakeven_triggered and (pos.partial_tp_triggered or r_multiple >= self.cfg.tier1_defense_r):
            if getattr(self.cfg, 'use_atr_trail', False):
                current_atr = self._calc_atr(df, getattr(self.cfg, 'atr_trail_period', 14))
                atr_mult = getattr(self.cfg, 'atr_trail_mult', 3.5)

                if pos.direction == 'LONG':
                    highest_seen = getattr(pos, 'highest_price', current_price)
                    if current_price > highest_seen:
                        highest_seen = current_price
                        pos.highest_price = highest_seen
                    chandelier_sl = highest_seen - (atr_mult * current_atr)
                    if chandelier_sl > pos.current_sl:
                        self._log.info(f'ATR吊灯 LONG: {symbol} ({inv}) SL {pos.current_sl:.4f}→{chandelier_sl:.4f} (最高{highest_seen:.4f} ATR{current_atr:.4f}x{atr_mult})')
                        pos.current_sl = chandelier_sl
                        if self.client is not None:
                            self.client.cancel_all_orders(symbol)
                            self.client.stop_order(symbol, 'SELL', round(pos.current_sl, 8), round(pos.quantity, 8), tracking_no=pos.tracking_no)
                        self._save_positions()
                else:
                    lowest_seen = getattr(pos, 'lowest_price', current_price)
                    if current_price < lowest_seen:
                        lowest_seen = current_price
                        pos.lowest_price = lowest_seen
                    chandelier_sl = lowest_seen + (atr_mult * current_atr)
                    if chandelier_sl < pos.current_sl:
                        self._log.info(f'ATR吊灯 SHORT: {symbol} ({inv}) SL {pos.current_sl:.4f}→{chandelier_sl:.4f} (最低{lowest_seen:.4f} ATR{current_atr:.4f}x{atr_mult})')
                        pos.current_sl = chandelier_sl
                        if self.client is not None:
                            self.client.cancel_all_orders(symbol)
                            self.client.stop_order(symbol, 'BUY', round(pos.current_sl, 8), round(pos.quantity, 8), tracking_no=pos.tracking_no)
                        self._save_positions()
            else:
                if pos.direction == 'LONG' and ema_ratchet_val > pos.current_sl:
                    self._log.info(f'EMA棘轮 LONG: {symbol} SL {pos.current_sl:.4f}→{ema_ratchet_val:.4f}')
                    pos.current_sl = ema_ratchet_val
                    if self.client is not None:
                        self.client.cancel_all_orders(symbol)
                        self.client.stop_order(symbol, 'SELL', round(pos.current_sl, 8), round(pos.quantity, 8), tracking_no=pos.tracking_no)
                    self._save_positions()
                elif pos.direction == 'SHORT' and ema_ratchet_val < pos.current_sl:
                    self._log.info(f'EMA棘轮 SHORT: {symbol} SL {pos.current_sl:.4f}→{ema_ratchet_val:.4f}')
                    pos.current_sl = ema_ratchet_val
                    if self.client is not None:
                        self.client.cancel_all_orders(symbol)
                        self.client.stop_order(symbol, 'BUY', round(pos.current_sl, 8), round(pos.quantity, 8), tracking_no=pos.tracking_no)
                    self._save_positions()

        # ==== PnL硬止损: 浮亏超过风险金额时强制平仓 (mark price已击穿但fill price未触发时的保底) ====
        if pos.risk_usdt > 0 and unrealized_pnl < 0:
            loss_ratio = abs(unrealized_pnl) / pos.risk_usdt
            if loss_ratio >= 1.0:
                return f'风险超限强制平仓(PnL${unrealized_pnl:+.2f}/风险${pos.risk_usdt:.2f}={loss_ratio:.1f}x)'

        # ==== 终极止损触发 (彻底修复：统一使用 current_price，抛弃K线历史极值) ====
        if pos.direction == "LONG" and current_price <= pos.current_sl:
            return "击穿动态追踪防线"
        elif pos.direction == "SHORT" and current_price >= pos.current_sl:
            return "击穿动态追踪防线"

        return None

    # ========== 平仓 ==========
    def _verify_position_exists(self, symbol):
        """检查持仓是否仍在交易所存在 (用于检测交易所侧清仓/止损触发)"""
        if self.client is None:
            return False
        try:
            raw = self.client.get_positions()
            if raw:
                for p in raw:
                    amt = float(p.get('positionAmt', 0))
                    if amt != 0 and p.get('symbol') == symbol:
                        return True
            return False
        except:
            return True  # 无法确认时保守认为仍存在

    def _looks_like_initial_stop_exit(self, pos: Position) -> bool:
        """交易所仓位消失时, 判断未进入保护的仓位是否更像初始止损触发。"""
        if pos.breakeven_triggered or pos.partial_tp_triggered:
            return False
        try:
            stop_price = float(getattr(pos, 'initial_sl', 0) or getattr(pos, 'sl_price', 0) or getattr(pos, 'current_sl', 0) or 0)
            entry_price = float(getattr(pos, 'entry_price', 0) or 0)
            current_sl = float(getattr(pos, 'current_sl', 0) or 0)
        except:
            return False
        if stop_price <= 0 or entry_price <= 0:
            return False

        risk_unit = abs(entry_price - stop_price)
        if risk_unit <= 0:
            return False

        # 未保护仓位的 current_sl 理论上仍应贴近初始SL; 若已明显移动, 不归为初始止损。
        sl_tolerance = max(stop_price * 0.001, risk_unit * 0.05)
        if current_sl > 0 and abs(current_sl - stop_price) > sl_tolerance:
            return False

        price_tolerance = max(stop_price * 0.002, risk_unit * 0.15)
        last_price = 0.0
        try:
            last_price = float(getattr(pos, 'current_price', 0.0) or 0.0)
        except:
            last_price = 0.0
        if last_price <= 0 and self.client is not None:
            try:
                last_price = float(self.client.get_price(pos.symbol) or 0.0)
            except:
                last_price = 0.0

        if last_price > 0:
            if pos.direction == 'LONG' and last_price <= stop_price + price_tolerance:
                return True
            if pos.direction == 'SHORT' and last_price >= stop_price - price_tolerance:
                return True
            if pos.direction == 'LONG' and last_price >= entry_price:
                return False
            if pos.direction == 'SHORT' and last_price <= entry_price:
                return False

        try:
            if float(getattr(pos, 'max_adverse_r', 0.0) or 0.0) >= 0.9:
                return True
        except:
            pass

        try:
            inv = str(getattr(pos, 'source_interval', self.cfg.scan_interval.split(',')[0]) or '15m').split(',')[0].strip() or '15m'
            df = fetch_klines(pos.symbol, inv, 5, exchange=self.cfg.exchange)
            if df is not None and len(df) > 0:
                if pos.direction == 'LONG' and float(df['l'].min()) <= stop_price + price_tolerance:
                    return True
                if pos.direction == 'SHORT' and float(df['h'].max()) >= stop_price - price_tolerance:
                    return True
        except:
            pass
        return False

    def _infer_exchange_cleared_reason(self, pos: Position) -> str:
        """交易所无持仓后的交易记录归因: 保护后SL、初始SL、或未知来源清仓。"""
        if pos.breakeven_triggered or pos.partial_tp_triggered:
            return 'SL触发(交易所确认)'
        if self._looks_like_initial_stop_exit(pos):
            return '初始止损触发(交易所确认)'
        return '交易所已清仓(未识别来源)'

    @staticmethod
    def _parse_risk_exit_reason(reason: str) -> dict:
        """提取风险强平触发瞬间口径, 最终记录原因会按成交/估算PnL重写。"""
        reason = str(reason or "")
        if not reason.startswith("风险超限强制平仓("):
            return {}
        try:
            inner = reason.split("(", 1)[1].rsplit(")", 1)[0]
            pnl_text, rest = inner.split("/风险$", 1)
            risk_text, ratio_text = rest.split("=", 1)
            return {
                "exit_trigger_pnl": round(float(pnl_text.replace("PnL$", "")), 2),
                "exit_trigger_risk": round(float(risk_text), 2),
                "exit_trigger_loss_ratio": round(float(ratio_text.rstrip("x")), 4),
            }
        except Exception:
            return {}

    def _cleanup_stale_positions(self):
        """清理交易所已不存在的本地持仓 (手动平仓/止损已触发但本地未同步)"""
        if self.client is None:
            return
        try:
            exchange_syms = set()
            if self.cfg.market_type == 'futures':
                raw = self.client.get_positions()
                if raw:
                    for p in raw:
                        amt = float(p.get('positionAmt', 0))
                        if amt != 0:
                            exchange_syms.add(p.get('symbol', ''))
            elif self.cfg.market_type == 'spot':
                balances = self.client.get_spot_balances()
                if balances:
                    for asset, info in balances.items():
                        if asset != 'USDT' and info.get('total', 0) > 0:
                            exchange_syms.add(f'{asset}USDT')

            for pos in list(self.positions):
                if pos.symbol not in exchange_syms:
                    reason = self._infer_exchange_cleared_reason(pos)
                    self._log.info(f'[持仓同步] {pos.symbol} 交易所已无持仓 → {reason}')
                    self._record_exit_trade(pos, reason, pnl_source="exchange_missing_estimate")
                    self.positions.remove(pos)
                    if hasattr(self.client, '_active_stop_ids'):
                        self.client._active_stop_ids.pop(pos.symbol, None)
            self._save_positions()
        except Exception as e:
            self._log.warning(f'持仓同步检查异常: {e}')

    def _record_exit_trade(self, pos: Position, reason: str, order_response=None,
                           exit_price_override: Optional[float] = None,
                           pnl_override: Optional[float] = None,
                           pnl_source: str = "estimate"):
        """记录平仓交易；有交易所成交/盈亏字段则优先使用，否则明确标记为估算。"""
        symbol = pos.symbol
        raw_exit_reason = str(reason or "")
        risk_exit_audit = self._parse_risk_exit_reason(raw_exit_reason)
        pos.exit_reason = reason
        pos.exit_time = bj_now()

        exchange_close = {}
        if self.client is not None and hasattr(self.client, "resolve_close_trade"):
            try:
                exchange_close = self.client.resolve_close_trade(
                    symbol,
                    order_response=order_response,
                    direction=pos.direction,
                    quantity=pos.quantity,
                ) or {}
                if exchange_close:
                    order_response = {
                        "order_response": order_response,
                        "exchange_close": exchange_close,
                    }
                    if exit_price_override is None and exchange_close.get("exit_price"):
                        exit_price_override = float(exchange_close.get("exit_price"))
                    if pnl_override is None and exchange_close.get("exchange_pnl") is not None:
                        pnl_override = float(exchange_close.get("exchange_pnl"))
                    if exchange_close.get("pnl_source"):
                        pnl_source = str(exchange_close.get("pnl_source"))
            except Exception as e:
                self._log.warning(f"{symbol} 交易所平仓明细反查失败: {e}")

        estimated_exit_price = self._estimate_exit_price(pos, reason)
        order_exit_price = self._extract_order_number(
            order_response,
            ("exit_price", "closeAvgPrice", "priceAvg", "avgPrice", "fillPrice", "dealAvgPrice", "tradePrice"),
        )
        if exit_price_override:
            exit_price = float(exit_price_override)
            exit_price_source = "override"
        elif order_exit_price:
            exit_price = float(order_exit_price)
            exit_price_source = "order_fill"
        else:
            exit_price = estimated_exit_price
            exit_price_source = "estimate"

        estimated_pnl = self._calc_trade_pnl(pos.direction, pos.entry_price, estimated_exit_price, pos.quantity)
        fill_estimated_pnl = self._calc_trade_pnl(pos.direction, pos.entry_price, exit_price, pos.quantity)
        order_exchange_pnl = self._extract_order_number(
            order_response,
            ("exchange_pnl", "realizedPnl", "realizedPNL", "realisedPnl", "realizedPL", "totalProfits", "profit", "pnl"),
            allow_negative=True,
        )
        exchange_pnl = pnl_override if pnl_override is not None else order_exchange_pnl
        if exchange_pnl is not None:
            pos.pnl = float(exchange_pnl)
            pnl_source = "exchange_order" if pnl_source == "estimate" else pnl_source
        else:
            pos.pnl = fill_estimated_pnl
            pnl_source = "fill_estimate" if exit_price_source == "order_fill" else pnl_source

        final_loss_ratio = round(abs(pos.pnl) / pos.risk_usdt, 4) if pos.risk_usdt else 0.0
        if raw_exit_reason.startswith("风险超限强制平仓("):
            trigger_ratio = risk_exit_audit.get("exit_trigger_loss_ratio")
            trigger_note = ""
            if trigger_ratio is not None and abs(float(trigger_ratio) - final_loss_ratio) >= 0.05:
                trigger_note = f", 触发{float(trigger_ratio):.1f}x"
            reason = (
                f"风险超限强制平仓(PnL${pos.pnl:+.2f}/风险${pos.risk_usdt:.2f}="
                f"{final_loss_ratio:.1f}x{trigger_note})"
            )
            pos.exit_reason = reason

        self._remember_closed_position(
            symbol=symbol,
            direction=pos.direction,
            exit_price=exit_price,
            reason=reason,
            pnl=pos.pnl,
            interval=getattr(pos, 'source_interval', ''),
            closed_at=pos.exit_time,
            clear_pending=True,
        )

        # 更新统计
        self.daily_loss += max(0, -pos.pnl)
        if pos.pnl <= 0:
            self.consecutive_losses += 1
            self.cooldown_until = bj_now()
            self.status_text = f'止损: {symbol} | 冷却{self.cfg.cooldown_minutes}分钟'
        else:
            self.consecutive_losses = 0
            self.cooldown_until = None
            # 盈利交易: 扣除燃料费
            if self.cfg.fuel_enabled and self.cfg.commission_rate > 0 and pos.pnl > 0:
                fee = pos.pnl * self.cfg.commission_rate / 100.0
                self.cfg.fuel_balance = max(0, self.cfg.fuel_balance - fee)
                self._log.info(f'燃料费扣除: ${fee:.2f} ({self.cfg.commission_rate:.0f}%盈利), 余额${self.cfg.fuel_balance:.2f}')

        # 取消剩余订单
        if self.client is not None:
            try:
                self.client.cancel_all_orders(symbol)
            except:
                pass

        account_balance_at_exit = 0.0
        account_pnl_at_exit = None
        try:
            account_initial_equity = float(getattr(self.cfg, "account_initial_equity", 0.0) or 0.0)
        except Exception:
            account_initial_equity = 0.0
        if self.client is not None:
            try:
                account_balance_at_exit = float(self.client.get_balance() or 0.0)
                if account_initial_equity > 0 and account_balance_at_exit > 0:
                    account_pnl_at_exit = round(account_balance_at_exit - account_initial_equity, 4)
            except Exception:
                account_balance_at_exit = 0.0

        # 写交易记录
        pnl_pct = round((pos.pnl / (pos.quantity * pos.entry_price)) * 100, 2) if pos.quantity and pos.entry_price else 0
        final_r = round(pos.pnl / pos.risk_usdt, 4) if pos.risk_usdt else 0.0
        hold_minutes = round((pos.exit_time - pos.entry_time).total_seconds() / 60, 1) if pos.entry_time and pos.exit_time else 0.0
        record = {
            'time': bj_now().isoformat(),
            'symbol': symbol, 'direction': pos.direction,
            'interval': getattr(pos, 'source_interval', ''),
            'source_strategy': getattr(pos, 'source_strategy', ''),
            'entry': round(pos.entry_price, 6),
            'exit': round(exit_price, 6),
            'sl': round(pos.sl_price, 6),
            'current_sl': round(pos.current_sl, 6),
            'quantity': round(pos.quantity, 8),
            'pnl': round(pos.pnl, 2),
            'estimated_pnl': round(estimated_pnl, 2),
            'fill_estimated_pnl': round(fill_estimated_pnl, 2),
            'exchange_pnl': round(float(exchange_pnl), 2) if exchange_pnl is not None else None,
            'exchange_net_profit': round(float(exchange_close.get("exchange_net_profit")), 2) if exchange_close.get("exchange_net_profit") is not None else None,
            'exchange_fee': round(float(exchange_close.get("exchange_fee")), 4) if exchange_close.get("exchange_fee") is not None else None,
            'exchange_funding': round(float(exchange_close.get("exchange_funding")), 4) if exchange_close.get("exchange_funding") is not None else None,
            'pnl_source': pnl_source,
            'exit_price_source': exit_price_source,
            'account_balance_at_exit': round(account_balance_at_exit, 4) if account_balance_at_exit > 0 else None,
            'account_pnl_at_exit': account_pnl_at_exit,
            'pnl_pct': pnl_pct,
            'r': final_r,
            'mfe_r': round(getattr(pos, 'max_favorable_r', 0.0), 4),
            'mae_r': round(getattr(pos, 'max_adverse_r', 0.0), 4),
            'hold_minutes': hold_minutes,
            'reason': reason,
            'score': pos.signal_score,
            'risk': round(pos.risk_usdt, 2),
            'breakeven': pos.breakeven_triggered,
            'partial': pos.partial_tp_triggered,
            'time_stop_watch': bool(getattr(pos, 'time_stop_watch', False)),
            'time_stop_first_seen_bars': int(getattr(pos, 'time_stop_first_seen_bars', 0) or 0),
            'time_stop_first_seen_r': round(float(getattr(pos, 'time_stop_first_seen_r', 0.0) or 0.0), 4),
            'time_stop_first_seen_mfe': round(float(getattr(pos, 'time_stop_first_seen_mfe', 0.0) or 0.0), 4),
            'signal_key': getattr(pos, 'signal_key', ''),
            'target_zone_type': getattr(pos, 'target_zone_type', ''),
            'target_zone_price': round(float(getattr(pos, 'target_zone_price', 0.0) or 0.0), 6),
            'target_zone_low': round(float(getattr(pos, 'target_zone_low', 0.0) or 0.0), 6),
            'target_zone_high': round(float(getattr(pos, 'target_zone_high', 0.0) or 0.0), 6),
            'target_r': round(float(getattr(pos, 'target_r', 0.0) or 0.0), 4),
            'target_distance_pct': round(float(getattr(pos, 'target_distance_pct', 0.0) or 0.0), 4),
            'target_zone_bars_ago': int(getattr(pos, 'target_zone_bars_ago', 0) or 0),
            'hermes_confirm': self._public_hermes_confirm(getattr(pos, 'hermes_confirm', {})),
            'choppy_filter': self._position_choppy_filter(getattr(pos, 'choppy_filter', {})),
            **self._position_btc_fields(pos),
        }
        if raw_exit_reason != reason:
            record['raw_exit_reason'] = raw_exit_reason
        if risk_exit_audit:
            record.update(risk_exit_audit)
            record['final_loss_ratio'] = final_loss_ratio
        self._append_trade(record)
        self._append_signal_event("exit", symbol, record)
        if account_balance_at_exit > 0 and account_initial_equity > 0:
            realized = round(sum(self._trade_pnl_value(t) for t in self.trade_log), 2)
            remaining_open_pnl = round(
                sum(float(getattr(p, "pnl", 0.0) or 0.0) for p in self.positions if p is not pos),
                2,
            )
            self._append_equity_snapshot(
                account_balance_at_exit,
                account_initial_equity,
                realized + remaining_open_pnl,
                remaining_open_pnl,
                reason="exit",
            )
        self._log.info(
            f'平{pos.direction}: {symbol} 原因{reason} PnL${pos.pnl:+.2f} '
            f'来源={pnl_source} 出场={exit_price:.4f}({exit_price_source})'
        )

    def exit_position(self, pos: Position, reason: str):
        """平掉指定持仓"""
        symbol = pos.symbol
        side = 'SELL' if pos.direction == 'LONG' else 'BUY'
        order = None
        if self.client is None and getattr(self.cfg, "mode", "paper") != "paper":
            self._log.error(f"平仓阻止({symbol}): live模式客户端未就绪, 保留本地持仓; reason={reason}")
            self._append_signal_event("exit_reject", symbol, {
                "direction": pos.direction,
                "entry": round(pos.entry_price, 8),
                "qty": round(pos.quantity, 8),
                "current_sl": round(pos.current_sl, 8),
                "exit_reason": reason,
                "reason": "client_not_ready",
                "source_interval": getattr(pos, "source_interval", ""),
                "signal_key": getattr(pos, "signal_key", ""),
            })
            return
        if self.client is not None:
            order = self.client.market_order(symbol, side, pos.quantity, reduce_only=True, tracking_no=pos.tracking_no)
            if order is None:
                # API返回空: 检查是否因为持仓已在交易所侧被清掉/SL触发
                if not self._verify_position_exists(symbol):
                    self._log.warning(f'{symbol} 已不在交易所持仓中(SL触发或交易所侧清仓), 记录交易后清理')
                    self._record_exit_trade(pos, reason, order_response=order, pnl_source="exchange_missing_estimate")
                    self.positions.remove(pos)
                    self._save_positions()
                    return
                self._log.error(f'平仓失败({symbol}): API返回空, 持仓保留')
                return
            if isinstance(order, dict) and order.get('code'):
                # API返回错误: 同样检查是否已被交易所侧清仓/SL触发
                if not self._verify_position_exists(symbol):
                    self._log.warning(f'{symbol} 已不在交易所持仓中(SL触发或交易所侧清仓), 记录交易后清理')
                    self._record_exit_trade(pos, reason, order_response=order, pnl_source="exchange_missing_estimate")
                    self.positions.remove(pos)
                    self._save_positions()
                    return
                self._log.error(f"平仓失败({symbol}): {order.get('msg','')}, 持仓保留")
                return

        # 正常路径: API成功 → 记录交易
        self._record_exit_trade(pos, reason, order_response=order)

        emoji = "\u2705" if pos.pnl > 0 else "\u274C"
        self._send_telegram(
            f"{emoji} <b>平{pos.direction}</b>\n"
            f"币种: {symbol}\n"
            f"原因: {reason}\n"
            f"盈亏: ${pos.pnl:+.2f}\n"
            f"连损: {self.consecutive_losses}"
        )

        self.positions.remove(pos)
        self._save_positions()

    def _reconcile_exchange_stop_orders(self, force: bool = False):
        """核对交易所计划止损是否真实存在；缺失时自动重挂。"""
        if self.client is None or not self.positions:
            return
        if not hasattr(self.client, "get_pending_plan_orders"):
            return
        now_ts = time.time()
        if not force and self._last_stop_reconcile_ts and now_ts - self._last_stop_reconcile_ts < 60:
            return
        self._last_stop_reconcile_ts = now_ts
        changed = False
        if not hasattr(self.client, "_active_stop_ids"):
            self.client._active_stop_ids = {}
        for pos in list(self.positions):
            try:
                symbol = pos.symbol
                known_oid = str(self.client._active_stop_ids.get(symbol, "") or "")
                pending = self.client.get_pending_plan_orders(symbol)
                live_ids = {str(o.get("orderId", "")) for o in pending if str(o.get("planStatus", "")).lower() in ("live", "executing", "")}
                if known_oid and known_oid in live_ids:
                    continue
                if live_ids and not known_oid:
                    self.client._active_stop_ids[symbol] = sorted(live_ids)[-1]
                    changed = True
                    continue
                side = "SELL" if pos.direction == "LONG" else "BUY"
                self._log.warning(
                    f"{symbol} 交易所计划止损缺失, 自动补挂 "
                    f"old={known_oid or '-'} live={','.join(sorted(live_ids)) or '-'}"
                )
                result = self.client.stop_order(
                    symbol, side, round(float(pos.current_sl), 8), round(float(pos.quantity), 8),
                    tracking_no=getattr(pos, "tracking_no", "")
                )
                if result:
                    changed = True
            except Exception as e:
                self._log.warning(f"{getattr(pos, 'symbol', '')} 止损核对异常: {e}")
        if changed:
            self._save_positions()

    # ========== 主循环 ==========
    def run_once(self):
        """执行一次完整的检查周期"""
        now = bj_now()

        # ---- 0. 清理本地幽灵持仓 (交易所已平但本地未同步) ----
        self._cleanup_stale_positions()
        self._reconcile_exchange_stop_orders()

        # ---- 1. 检查现有持仓 ----
        for pos in list(self.positions):
            reason = self.check_exit(pos)
            if reason:
                self.exit_position(pos, reason)

        # ---- 2. 扫描新信号 ----
        if self._check_risk_limits():
            # 【多周期并发扫描矩阵】
            intervals = [i.strip() for i in self.cfg.scan_interval.split(',')] if ',' in self.cfg.scan_interval else [self.cfg.scan_interval]
            try:
                btc_fields = self._btc_regime_fields()
            except Exception:
                btc_fields = {"btc_regime": "btc_unknown", "btc_score": 0}
            if self._entry_signal_source() == "predicta_ewo":
                self._run_predicta_cycle(now, btc_fields, intervals)
                self.status_text = "Predicta/EWO | 持仓%s | 候选%s | 日亏$%.0f" % (
                    len(self.positions),
                    len(self._predicta_setup_pool),
                    self.daily_loss,
                )
                return
            if self._entry_signal_source() == "rj_only":
                self._run_rj_only_cycle(now, btc_fields, intervals)
                rj_mode_label = "RJ测试网" if self.cfg.mode != "paper" else "RJ纸笔"
                self.status_text = rj_mode_label + " | 持仓%s | 连损%s | 日亏$%.0f" % (
                    len(self.positions),
                    self.consecutive_losses,
                    self.daily_loss,
                )
                return
            all_signals = []

            for inv in intervals:
                self._log.info(f'开始扫描动量周期: {inv}')
                sigs = scan_squeeze_breakout(inv, top_n=15, exchange=self.cfg.exchange)
                for s in sigs:
                    s['source_interval'] = inv  # 烙印真实触发周期
                all_signals.extend(sigs)

            # 跨周期去重: 同币种保留最高分
            unique_sigs = {}
            for s in all_signals:
                sym = s['symbol']
                if sym not in unique_sigs or s['score'] > unique_sigs[sym]['score']:
                    unique_sigs[sym] = s
            signals = list(unique_sigs.values())
            signals.sort(key=lambda x: x['score'], reverse=True)

            self.last_scan_time = now
            self.last_signal_count = len(signals)
            self._append_signal_event("scan_cycle", payload={
                "intervals": ",".join(intervals),
                "raw_signals": len(all_signals),
                "unique_signals": len(signals),
                "min_score": self.cfg.min_score,
                "positions": len(self.positions),
                **btc_fields,
            })
            for s in signals:
                self._append_signal_event("scan_candidate", s.get("symbol", ""), self._signal_snapshot(s))
            self.last_signals_data = [
                {k: v for k, v in s.items() if k in ("symbol","direction","price","score","tight","duration","fresh","retest_score","strength","vol_score","ht_score","ht_frac","min_spread","breakout_pct","bars_since","squeeze_bars","vol_surge","ht_trend","ht_fractal","retest","source_interval")}
                for s in signals[:10]
            ]

            # 【全局铁闸: 踢出明确逆势(压制)信号, 放行无分型(—)和共振信号】
            valid_ht_signals = [s for s in signals if not ('压制' in str(s.get('ht_fractal', '')))]
            filtered_count = len(signals) - len(valid_ht_signals)
            if filtered_count > 0:
                self._log.info(f'大周期共振拦截: 强行剔除了 {filtered_count} 个逆势压制信号')
                for s in signals:
                    if '压制' in str(s.get('ht_fractal', '')):
                        self._append_signal_event("signal_reject", s.get("symbol", ""), self._signal_snapshot(s, {
                            "reason": "ht_fractal_conflict",
                        }))

            qualified = [s for s in valid_ht_signals if s["score"] >= self.cfg.min_score]
            for s in valid_ht_signals:
                if s["score"] < self.cfg.min_score:
                    self._append_signal_event("signal_reject", s.get("symbol", ""), self._signal_snapshot(s, {
                        "reason": "score_below_min",
                        "min_score": self.cfg.min_score,
                    }))

            def _valid_retest(retest_val):
                return retest_val and "分型" in str(retest_val)

            ready_to_enter = [s for s in qualified if _valid_retest(s.get("retest", ""))]
            rejected = [s for s in valid_ht_signals if s["score"] >= self.cfg.min_score and not _valid_retest(s.get("retest", ""))]
            if rejected:
                self._log.info(f"确认过滤拒绝 {len(rejected)} 个无效信号: {[r['symbol']+'('+r.get('retest','')+')' for r in rejected]}")
                for r in rejected:
                    self._append_signal_event("signal_reject", r.get("symbol", ""), self._signal_snapshot(r, {
                        "reason": "no_confirmed_fractal",
                    }))
            if ready_to_enter:
                ready_to_enter.sort(key=lambda s: s["score"], reverse=True)
                self._log.info(f"扫描到 {len(ready_to_enter)} 个合格信号 (共{len(signals)}个), 最高分{ready_to_enter[0]['score']:.0f}")

            # 【跨周期盯防池: 所有入池信号都自带大周期共振护体】
            now_ts = time.time()
            for sig in valid_ht_signals:
                sym = sig["symbol"]; rt = sig.get("retest", "")
                if any(p.symbol == sym for p in self.positions):
                    self._pending_signals.pop(sym, None)
                    continue
                if "回踩" in rt or "反弹" in rt or "等待" in rt:
                    if "分型" in rt:
                        if sym in self._pending_signals:
                            sig["score"] = max(sig["score"], self.cfg.min_score + 1)
                            self._log.info(f"{sym} 跨扫描确认: 待确认→分型形成, 评分{sig['score']:.0f}")
                        self._pending_signals.pop(sym, None)
                    elif sym not in self._pending_signals:
                        self._pending_signals[sym] = {"first_seen": now_ts, "direction": sig["direction"], "interval": sig.get("source_interval", "15m")}
                        self._log.info(f"盯防池+1 {sym} {sig['direction']} 待分型确认 (池内{len(self._pending_signals)}个)")
                        self._append_signal_event("pool_add", sym, self._signal_snapshot(sig, {
                            "reason": "wait_fractal",
                            "pool_size": len(self._pending_signals),
                        }))

            stale = []
            for s, v in self._pending_signals.items():
                timeout = POOL_TIMEOUT.get(v.get("interval", "15m"), 7200)
                if now_ts - v["first_seen"] > timeout:
                    stale.append(s)
            for s in stale:
                info = self._pending_signals[s]
                self._log.info(f"盯防池超时清除 {s} ({info.get('interval','15m')}) — 入池{(now_ts - info['first_seen'])/3600:.1f}h未确认")
                self._append_signal_event("pool_timeout", s, {
                    "symbol": s,
                    "direction": info.get("direction", ""),
                    "interval": info.get("interval", "15m"),
                    "age_hours": round((now_ts - info['first_seen'])/3600, 3),
                    **btc_fields,
                })
                del self._pending_signals[s]

            # 池子重扫: 每个信号用自己专属周期取K线
            self._scan_count = getattr(self, '_scan_count', 0) + 1
            if self._pending_signals and self._scan_count % 2 == 0:
                available_slots = self.cfg.max_positions - len(self.positions)
                batch = list(self._pending_signals.items())[:min(6, max(0, available_slots))]
                found = 0
                for sym, info in batch:
                    try:
                        sig_inv = info.get('interval', '15m')
                        df = fetch_klines(sym, sig_inv, 200, exchange=self.cfg.exchange)
                        if df is None or len(df) < 50: continue
                        if is_tradfi_or_junk(sym):
                            self._log.info(f"池子清除股票: {sym}")
                            del self._pending_signals[sym]; continue
                        signal_details = verify_pool_signal_details(df, info["direction"], interval=sig_inv)
                        if signal_details is None:
                            # 诊断: 定位 verify_pool_signal 失败的三步原因
                            _closes = df["c"]; _n = len(_closes); _lb = min({"15m":10,"1h":6,"4h":4,"1d":3}.get(sig_inv,8), _n - 1)
                            _mx, _mn, _ = calc_ma_band(_closes)
                            reason = "?"
                            _bw = get_breakout_confirm_window(sig_inv)
                            _br = None
                            _retest_j = None
                            for _j in range(max(120, _n - _bw - 1), _n):
                                if np.isnan(_mn[_j]) or np.isnan(_mx[_j]): continue
                                if info["direction"] == "LONG" and _closes.iloc[_j] > _mx[_j]:
                                    _br = _j; break
                                if info["direction"] == "SHORT" and _closes.iloc[_j] < _mn[_j]:
                                    _br = _j; break
                            if _br is None:
                                reason = "无有效突破"
                            else:
                                _defense_broken = False
                                for _j in range(_br + 1, _n):
                                    if np.isnan(_mn[_j]) or np.isnan(_mx[_j]): continue
                                    if info["direction"] == "LONG" and _closes.iloc[_j] < _mn[_j] * 0.995:
                                        reason = "防线破坏"; _defense_broken = True; break
                                    if info["direction"] == "SHORT" and _closes.iloc[_j] > _mx[_j] * 1.005:
                                        reason = "防线破坏"; _defense_broken = True; break
                                if not _defense_broken:
                                    _retest_ok = False
                                    _retest_j = None
                                    for _j in range(_br + 1, _n):
                                        if np.isnan(_mn[_j]) or np.isnan(_mx[_j]): continue
                                        if info["direction"] == "LONG":
                                            if df["l"].iloc[_j] <= _mx[_j] * 1.015 and _closes.iloc[_j] >= _mn[_j] * 0.995:
                                                _retest_ok = True; _retest_j = _j; break
                                        else:
                                            if df["h"].iloc[_j] >= _mn[_j] * 0.985 and _closes.iloc[_j] <= _mx[_j] * 1.005:
                                                _retest_ok = True; _retest_j = _j; break
                                    if not _retest_ok:
                                        reason = "无回归确认"
                            if reason == "?":
                                _b_hi = _mx[-1]; _b_lo = _mn[-1]
                                _entry_lim = get_entry_float_limit(sig_inv)
                                if info["direction"] == "LONG" and _closes.iloc[-1] > _b_hi * (1 + _entry_lim):
                                    reason = "入场悬空"
                                elif info["direction"] == "SHORT" and _closes.iloc[-1] < _b_lo * (1 - _entry_lim):
                                    reason = "入场悬空"
                                else:
                                    _seq_start = max(0, (_br - 1) if _br is not None else _n - _lb)
                                    _min_confirm = max(
                                        ((_br + 1) if _br is not None else _n),
                                        ((_retest_j - 1) if _retest_j is not None else ((_br + 1) if _br is not None else _n)),
                                        _n - _lb
                                    )
                                    _structure_seq = (
                                        _n - _seq_start >= 4 and
                                        _find_fractal_structure_sequence(
                                            df["h"].values[_seq_start:],
                                            df["l"].values[_seq_start:],
                                            _closes.values[_seq_start:],
                                            info["direction"],
                                            offset=_seq_start,
                                            min_start_idx=_br,
                                            min_confirm_idx=_min_confirm,
                                        )
                                    )
                                    if not _structure_seq:
                                        reason = "结构序列不完整"
                                    else:
                                        reason = "悬空拦截"
                            # 附加关键数值
                            _b_hi = _mx[-1]; _b_lo = _mn[-1]
                            self._log.info(f"[POOL-DIAG] {sym} ({sig_inv}) {info['direction']} 失败:{reason} | 当前价={_closes.iloc[-1]:.4f} band=[{_b_lo:.4f},{_b_hi:.4f}]")
                            self._append_signal_event("pool_reject", sym, {
                                "symbol": sym,
                                "direction": info.get("direction", ""),
                                "interval": sig_inv,
                                "reason": reason,
                                "price": float(_closes.iloc[-1]),
                                "band_hi": float(_b_hi),
                                "band_lo": float(_b_lo),
                                **btc_fields,
                            })
                            continue
                        fractal_sl = signal_details.get("fractal_sl")
                        band_sl = signal_details.get("band_sl")
                        if fractal_sl is not None and band_sl is not None:
                            found += 1
                            if any(p.symbol == sym for p in self.positions):
                                del self._pending_signals[sym]
                                continue
                            signal_keys = self._build_signal_keys(sym, info["direction"], sig_inv, signal_details, df=df)
                            signal_key = signal_keys[0] if signal_keys else ""
                            if self._any_signal_key_used(signal_keys):
                                self._log.info(f"{sym} ({sig_inv}) 旧结构已成交过, 清除盯防池 key={signal_key}")
                                self._append_signal_event("pool_reject", sym, {
                                    "symbol": sym,
                                    "direction": info.get("direction", ""),
                                    "interval": sig_inv,
                                    "reason": "same_signal_reuse",
                                    "signal_key": signal_key,
                                    "fractal_sl": fractal_sl,
                                    "band_sl": band_sl,
                                    **btc_fields,
                                })
                                del self._pending_signals[sym]
                                continue
                            failed_key = self._failed_signal_key(signal_keys)
                            if failed_key:
                                meta = self._failed_signal_keys.get(failed_key, {})
                                self._log.info(f"{sym} ({sig_inv}) 同结构下单失败冷却中, 清除盯防池 key={failed_key}")
                                self._append_signal_event("pool_reject", sym, {
                                    "symbol": sym,
                                    "direction": info.get("direction", ""),
                                    "interval": sig_inv,
                                    "reason": "order_failed_cooldown",
                                    "signal_key": signal_key,
                                    "failed_key": failed_key,
                                    "failed_at": meta.get("time", ""),
                                    "fractal_sl": fractal_sl,
                                    "band_sl": band_sl,
                                    **btc_fields,
                                })
                                del self._pending_signals[sym]
                                continue
                            self._log.info(f"{sym} ({sig_inv}) 盯防池触发: 分型确认，发起狙击！")
                            self._append_signal_event("pool_trigger", sym, {
                                "symbol": sym,
                                "direction": info.get("direction", ""),
                                "interval": sig_inv,
                                "fractal_sl": fractal_sl,
                                "band_sl": band_sl,
                                "signal_key": signal_key,
                                **btc_fields,
                            })
                            closes = df["c"]
                            fractal_sl_for_risk = fractal_sl * (0.998 if info["direction"]=="LONG" else 1.002)
                            if info["direction"]=="LONG":
                                sl = min(fractal_sl_for_risk, band_sl)
                            else:
                                sl = max(fractal_sl_for_risk, band_sl)
                            qty, pos_usdt, risk = self.calc_position_size(sym, info["direction"], closes.iloc[-1], sl, source_interval=sig_inv)
                            if qty > 0:
                                self.enter_position({
                                    "symbol": sym, "direction": info["direction"],
                                    "price": closes.iloc[-1], "score": self.cfg.min_score + 1,
                                    "fractal_sl": fractal_sl,
                                    "band_sl": band_sl,
                                    "retest": "回踩+底分型" if info["direction"]=="LONG" else "反弹+顶分型",
                                    "source_interval": sig_inv,
                                    "signal_key": signal_key,
                                    **signal_details,
                                })
                            del self._pending_signals[sym]
                        time.sleep(0.5)
                    except Exception as e:
                        self._log.warning(f"池子重扫{sym}异常: {e}")
                if batch:
                    self._log.info(f"池子重扫: 检查{len(batch)}个, 通过{found}个, 池内余{len(self._pending_signals)}个")

            for sig in ready_to_enter:
                if len(self.positions) >= self.cfg.max_positions:
                    break
                if any(p.symbol == sig["symbol"] for p in self.positions):
                    continue
                self.enter_position(sig)

        # ---- 3. 更新状态 ----
        self.status_text = (
            f"持仓{len(self.positions)} | "
            f"连损{self.consecutive_losses} | "
            f"日亏${self.daily_loss:.0f}"
        )

    def run_loop(self):
        """主循环 (在后台线程运行)"""
        self.running = True
        if self._entry_signal_source() == "predicta_ewo":
            scan_interval_sec = max(300, int(getattr(self.cfg, "predicta_scan_interval_sec", 1800) or 1800))
        elif self._entry_signal_source() == "rj_only":
            scan_interval_sec = max(300, int(getattr(self.cfg, "rj_only_scan_interval_sec", 1800) or 1800))
        else:
            scan_interval_sec = max(30, int(getattr(self.cfg, "engine_scan_interval_sec", 60) or 60))
        check_interval_sec = 3   # 3秒检查持仓(接近实时)

        last_scan = 0
        self._log.info(f"交易引擎启动 [{self.cfg.mode}] — "
                 f"信号源{self._entry_signal_source()} 扫描{self.cfg.scan_interval} 最低评分{self.cfg.min_score} "
                 f"风险{self.cfg.risk_per_trade}/笔 "
                 f"RJ过滤{self._rj_filter_mode()}({getattr(self.cfg, 'rj_cross_lookback_bars', 8)}K)")

        while self.running:
            try:
                now = time.time()
                # 定期扫描
                if now - last_scan >= scan_interval_sec:
                    self.run_once()
                    last_scan = now

                if self._entry_signal_source() == "rj_only":
                    self._check_rj_setup_pool()

                # 持仓检查(每次都查)
                for pos in list(self.positions):
                    reason = self.check_exit(pos)
                    if reason:
                        self.exit_position(pos, reason)

                time.sleep(check_interval_sec)

            except KeyboardInterrupt:
                break
            except Exception as e:
                self._log.error(f"主循环异常: {e}", exc_info=True)
                time.sleep(60)

    def start(self):
        if self.running:
            return
        self.start_time = bj_now()
        self._restore_stop_ids()   # 恢复活动止损单ID → 下面挂单时可取消旧单
        self._sync_positions()
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.status_text = "已停止"

    def close_position(self, symbol):
        """平掉指定币种的持仓 (无API时直接移除本地记录)"""
        for pos in list(self.positions):
            if pos.symbol == symbol:
                if self.client is None:
                    if getattr(self.cfg, "mode", "paper") != "paper":
                        self._log.error(f"手动平仓阻止({symbol}): live模式客户端未就绪, 保留本地持仓")
                        return False
                    # 模拟盘/无API: 直接移除本地持仓
                    self._log.info(f"手动平仓(本地): {symbol} {pos.direction} PnL${pos.pnl:.2f}")
                    self.positions.remove(pos)
                    self._save_positions()
                    return True
                try:
                    self.exit_position(pos, "手动平仓")
                    return True
                except Exception as e:
                    self._log.error(f"平仓{symbol}失败: {e}")
                    return False
        return False

    def close_all_positions(self):
        """一键平仓所有持仓 (无API时直接清空本地记录)"""
        if self.client is None:
            if getattr(self.cfg, "mode", "paper") != "paper":
                self._log.error("一键平仓阻止: live模式客户端未就绪, 保留本地持仓")
                return 0
            count = len(self.positions)
            self.positions.clear()
            self._save_positions()
            self._log.info(f"手动一键平仓: {count} 笔")
            self.status_text = f"已平仓{count}笔"
            return count
        count = 0
        for pos in list(self.positions):
            try:
                self.exit_position(pos, "手动平仓")
                count += 1
            except Exception as e:
                self._log.error(f"平仓{pos.symbol}失败: {e}")
        self.status_text = f"已平仓{count}笔"
        return count

    def _sync_position_market_snapshot(self):
        """刷新前端展示用的实时价格/浮盈快照, 不改变交易决策。"""
        if not self.positions:
            return

        live_by_key = {}
        live_by_symbol = {}
        if self.client is not None and self.cfg.market_type == "futures":
            try:
                for xp in (self.client.get_positions() or []):
                    sym = xp.get("symbol", "")
                    amt = float(xp.get("positionAmt", 0) or 0)
                    if not sym or amt == 0:
                        continue
                    direction = "LONG" if amt > 0 else "SHORT"
                    live_by_key[(sym, direction)] = xp
                    live_by_symbol.setdefault(sym, xp)
            except Exception as e:
                self._log.warning(f"刷新前端持仓盈亏失败: {e}")

        for pos in self.positions:
            xp = live_by_key.get((pos.symbol, pos.direction)) or live_by_symbol.get(pos.symbol)
            if xp:
                try:
                    entry_price = float(xp.get("entryPrice", 0) or 0)
                    mark_price = float(xp.get("markPrice", 0) or 0)
                    unrealized = float(xp.get("unRealizedProfit", 0) or 0)
                    if entry_price > 0:
                        pos.entry_price = entry_price
                        self._refresh_target_metrics(pos)
                    if mark_price > 0:
                        pos.current_price = mark_price
                    pos.pnl = unrealized
                    continue
                except:
                    pass

            try:
                inv = str(getattr(pos, 'source_interval', self.cfg.scan_interval.split(',')[0]) or '15m').split(',')[0].strip() or '15m'
                df = fetch_klines(pos.symbol, inv, 5, exchange=self.cfg.exchange)
                if df is not None and len(df) > 0:
                    current_price = float(df['c'].iloc[-1])
                    pos.current_price = current_price
                    if pos.direction == 'LONG':
                        pos.pnl = (current_price - pos.entry_price) * pos.quantity
                    else:
                        pos.pnl = (pos.entry_price - current_price) * pos.quantity
            except:
                pass

    def _rj_setup_pool_rows(self, limit: int = 40, now_ts: Optional[float] = None) -> List[dict]:
        """Frontend-ready RJ setup pool rows. Read-only; does not touch exchange APIs."""
        now_ts = time.time() if now_ts is None else float(now_ts)
        near_pct = max(0.0, float(getattr(self.cfg, "rj_only_setup_near_pct", 0.15) or 0.15))
        rows: List[dict] = []
        pool = getattr(self, "_rj_setup_pool", {}) or {}
        for key, item in list(pool.items()):
            try:
                symbol = str(item.get("symbol") or "").upper()
                direction = str(item.get("direction") or "").upper()
                if not symbol or direction not in ("LONG", "SHORT"):
                    continue
                trigger = float(item.get("rj_setup_trigger_price") or item.get("rj_only_confirm_level") or 0.0)
                live_price = float(item.get("rj_setup_live_price") or item.get("price") or item.get("rj_only_confirm_close") or 0.0)
                distance_pct = 0.0
                crossed = False
                if trigger > 0 and live_price > 0:
                    if direction == "LONG":
                        crossed = live_price >= trigger
                        distance_pct = max(0.0, (trigger - live_price) / trigger * 100.0)
                    else:
                        crossed = live_price <= trigger
                        distance_pct = max(0.0, (live_price - trigger) / trigger * 100.0)

                started_at = str(item.get("rj_setup_trigger_started_at") or "")
                last_volume_wait_ts = float(item.get("rj_setup_last_volume_wait_ts", 0.0) or 0.0)
                if last_volume_wait_ts and now_ts - last_volume_wait_ts <= 180:
                    stage = "volume"
                    stage_label = "VOLUME"
                    stage_text = "量能等待"
                    rank = 1
                elif started_at:
                    stage = "touch"
                    stage_label = "TOUCH"
                    stage_text = "突破确认"
                    rank = 0
                elif crossed:
                    stage = "crossed"
                    stage_label = "CROSS"
                    stage_text = "已触发"
                    rank = 1
                elif trigger > 0 and live_price > 0 and distance_pct <= near_pct:
                    stage = "near"
                    stage_label = "NEAR"
                    stage_text = "临界触发"
                    rank = 2
                else:
                    stage = "wait"
                    stage_label = "WAIT"
                    stage_text = "等待突破"
                    rank = 3

                first_seen_ts = float(item.get("rj_setup_first_seen_ts", now_ts) or now_ts)
                expires_at_ts = float(item.get("rj_setup_expires_at_ts", 0.0) or 0.0)
                rows.append({
                    "key": str(item.get("rj_setup_key") or key),
                    "symbol": symbol,
                    "direction": direction,
                    "source_interval": str(item.get("source_interval") or getattr(self.cfg, "scan_interval", "30m")).split(",")[0].strip() or "30m",
                    "score": round(float(item.get("score", 0.0) or 0.0), 2),
                    "stage": stage,
                    "stage_label": stage_label,
                    "stage_text": stage_text,
                    "stage_rank": rank,
                    "price": round(live_price, 8),
                    "trigger_price": round(trigger, 8),
                    "distance_pct": round(distance_pct, 4),
                    "stop_price": round(float(item.get("rj_only_stop_price") or item.get("band_sl") or 0.0), 8),
                    "hist_win_rate": round(float(item.get("rj_only_hist_win_rate", 0.0) or 0.0), 2),
                    "hist_samples": int(float(item.get("rj_only_hist_samples", 0) or 0)),
                    "hist_avg_r": round(float(item.get("rj_only_hist_avg_r", 0.0) or 0.0), 3),
                    "volume_pass": bool(item.get("rj_volume_filter_pass", True)),
                    "volume_ratio": round(float(item.get("rj_volume_ratio", 0.0) or 0.0), 2),
                    "sr_pass": bool(item.get("rj_sr_filter_pass", True)),
                    "setup_confirm_mode": str(item.get("rj_setup_confirm_mode") or ""),
                    "close_confirm_sec": int(float(item.get("rj_setup_close_confirm_sec", 0) or 0)),
                    "age_sec": int(max(0.0, now_ts - first_seen_ts)),
                    "expires_in_sec": int(max(0.0, expires_at_ts - now_ts)) if expires_at_ts else 0,
                    "first_seen": str(item.get("rj_setup_first_seen") or ""),
                    "expires_at": str(item.get("rj_setup_expires_at") or ""),
                    "key_high": round(float(item.get("rj_only_key_high", 0.0) or 0.0), 8),
                    "key_low": round(float(item.get("rj_only_key_low", 0.0) or 0.0), 8),
                    "confirm_level": round(float(item.get("rj_only_confirm_level", 0.0) or 0.0), 8),
                })
            except Exception:
                continue
        rows.sort(key=lambda r: (r.get("stage_rank", 9), r.get("distance_pct", 999.0), -r.get("score", 0.0)))
        collapsed = []
        seen_pairs = set()
        for row in rows:
            pair = (row.get("symbol"), row.get("direction"))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            collapsed.append(row)
        return collapsed[:max(0, int(limit or 40))]

    def _predicta_setup_pool_rows(self, limit: int = 40) -> List[dict]:
        rows = []
        for key, item in (getattr(self, "_predicta_setup_pool", {}) or {}).items():
            rows.append({
                "key": key,
                "symbol": str(item.get("symbol", "") or ""),
                "direction": str(item.get("direction", "") or ""),
                "source_interval": str(item.get("source_interval", "") or ""),
                "source_strategy": "predicta_ewo",
                "stage": "wait_ewo_break",
                "stage_label": "WAIT",
                "stage_text": "等待关键K突破 + EWO同向",
                "price": float(item.get("price", 0.0) or 0.0),
                "key_high": float(item.get("predicta_key_high", 0.0) or 0.0),
                "key_low": float(item.get("predicta_key_low", 0.0) or 0.0),
                "signal_ewo": float(item.get("predicta_signal_ewo", 0.0) or 0.0),
                "confirm_bars": int(item.get("predicta_confirm_bars", 6) or 6),
                "choppy_filter_is_choppy": bool(item.get("choppy_filter_is_choppy", False)),
            })
        rows.sort(key=lambda item: (item["symbol"], item["direction"]))
        return rows[:max(0, int(limit or 40))]

    def _recent_signal_event_stats(self, limit: int = 800, ttl_sec: int = 8) -> dict:
        """Small cached event digest for the frontend pipeline HUD."""
        now_ts = time.time()
        path = Path(getattr(self, "_signal_log_path", SIGNAL_LOG_PATH))
        try:
            mtime = path.stat().st_mtime if path.exists() else 0.0
        except Exception:
            mtime = 0.0
        cache = getattr(self, "_signal_event_cache", {}) or {}
        if cache.get("data") and cache.get("mtime") == mtime and now_ts - float(cache.get("ts", 0.0) or 0.0) < ttl_sec:
            return dict(cache.get("data") or {})

        events = deque(maxlen=max(50, int(limit or 800)))
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except Exception:
                            continue
            except Exception:
                events.clear()

        counts = Counter()
        reject_reasons = Counter()
        latest = {}
        for ev in events:
            name = str(ev.get("event") or "")
            if not name:
                continue
            counts[name] += 1
            reason = str(ev.get("reason") or ev.get("rj_only_stats_reason") or ev.get("rj_sr_reason") or "")
            if name in ("entry_reject", "rj_setup_invalidated", "rj_setup_timeout") and reason:
                reject_reasons[reason] += 1
            if name in (
                "rj_only_scan_cycle", "rj_only_candidate", "rj_setup_add", "rj_setup_near_trigger",
                "rj_setup_trigger_touch", "rj_setup_volume_wait", "rj_setup_trigger",
                "rj_choppy_shadow",
                "hermes_confirm_pass", "hermes_confirm_block", "entry_precheck_pass", "entry_filled",
                "entry_reject",
            ):
                latest[name] = {
                    "time": ev.get("time", ""),
                    "symbol": ev.get("symbol", ""),
                    "direction": ev.get("direction", ""),
                    "reason": reason,
                    "score": ev.get("score", 0),
                }
        data = {
            "window": len(events),
            "last_event_time": events[-1].get("time", "") if events else "",
            "counts": dict(counts),
            "reject_reasons": dict(reject_reasons.most_common(5)),
            "latest": latest,
        }
        self._signal_event_cache = {"ts": now_ts, "mtime": mtime, "data": data}
        return data

    def _rj_pipeline_status(self, setup_rows: Optional[List[dict]] = None) -> dict:
        rows = setup_rows if setup_rows is not None else self._rj_setup_pool_rows(limit=40)
        events = self._recent_signal_event_stats()
        counts = events.get("counts", {}) or {}
        near_count = sum(1 for r in rows if r.get("stage") in ("near", "crossed", "touch", "volume"))
        touch_count = sum(1 for r in rows if r.get("stage") in ("touch", "volume"))
        watchlist = getattr(self, "_rj_watchlist", {}) or {}
        return {
            "running": bool(self.running),
            "scan_interval": getattr(self.cfg, "scan_interval", ""),
            "scan_batch_size": int(getattr(self.cfg, "scan_batch_size", 0) or 0),
            "scan_workers": int(getattr(self.cfg, "scan_workers", 0) or 0),
            "watchlist_enabled": bool(getattr(self.cfg, "rj_only_watchlist_enabled", True)),
            "watchlist_size": len(watchlist.get("symbols", []) or []),
            "watchlist_updated_at": watchlist.get("updated_at", ""),
            "setup_pool_size": len(getattr(self, "_rj_setup_pool", {}) or {}),
            "setup_visible_count": len(rows),
            "near_trigger_count": near_count,
            "touch_count": touch_count,
            "positions_count": len(self.positions),
            "hermes_enabled": bool(getattr(self.cfg, "hermes_confirm_enabled", False)),
            "hermes_mode": str(getattr(self.cfg, "hermes_confirm_mode", "off") or "off"),
            "event_window": events.get("window", 0),
            "last_event_time": events.get("last_event_time", ""),
            "counts": counts,
            "reject_reasons": events.get("reject_reasons", {}),
            "latest": events.get("latest", {}),
        }

    def get_fast_summary(self) -> dict:
        """轻量 Web 摘要：不触发交易所 API / K线请求 / 大历史读写，用于前端首屏和高频轮询。"""
        today_key = bj_now().strftime("%Y-%m-%d")
        now_ts = time.time()
        if (
                self.positions
                and self.client is not None
                and self.cfg.market_type == "futures"
                and now_ts - getattr(self, "_fast_position_snapshot_ts", 0.0) >= 3.0):
            self._sync_position_market_snapshot()
            self._fast_position_snapshot_ts = now_ts
        realized_pnl = round(sum(self._trade_pnl_value(t) for t in self.trade_log), 2)
        open_pnl = round(sum(float(getattr(p, "pnl", 0.0) or 0.0) for p in self.positions), 2)
        net_pnl = round(realized_pnl + open_pnl, 2)
        daily_realized_pnl = round(
            sum(self._trade_pnl_value(t) for t in self.trade_log if str(t.get("time", "")).startswith(today_key)),
            2,
        )
        try:
            account_initial_equity = float(getattr(self.cfg, "account_initial_equity", 0.0) or 0.0)
        except Exception:
            account_initial_equity = 0.0
        win_count = sum(1 for t in self.trade_log if self._trade_pnl_value(t) > 0)
        state = self._load_equity_state()
        equity_history = []
        if state.get("account_balance"):
            latest_equity = state
        elif now_ts - self._fast_equity_cache_ts > 2:
            self._fast_equity_cache = self._load_equity_history(2000)
            self._fast_equity_cache_ts = now_ts
            equity_history = self._fast_equity_cache or []
            latest_equity = equity_history[-1] if equity_history else {}
        else:
            equity_history = self._fast_equity_cache or []
            latest_equity = equity_history[-1] if equity_history else {}
        account_balance = float(latest_equity.get("account_balance", 0) or 0.0)
        if account_initial_equity <= 0 and latest_equity:
            account_initial_equity = float(latest_equity.get("account_initial_equity", 0) or 0.0)
        if self.client is not None:
            try:
                refresh_sec = max(5, int(getattr(self.cfg, "fast_equity_refresh_sec", 15) or 15))
                if now_ts - float(getattr(self, "_fast_live_balance_ts", 0.0) or 0.0) >= refresh_sec:
                    live_balance = float(self.client.get_balance() or 0.0)
                    if live_balance > 0:
                        self._fast_live_balance_ts = now_ts
                        self._fast_live_balance = live_balance
                        account_balance = live_balance
                        latest_equity = {
                            **(latest_equity or {}),
                            "time": bj_now().isoformat(),
                            "account_balance": round(account_balance, 4),
                            "account_initial_equity": round(float(account_initial_equity or 0.0), 4),
                            "account_pnl": round(account_balance - account_initial_equity, 4) if account_initial_equity > 0 else 0.0,
                            "reason": "fast_status_live_balance",
                        }
                        if account_initial_equity > 0:
                            self._append_equity_snapshot(account_balance, account_initial_equity, net_pnl, open_pnl, reason="summary")
                            self._fast_equity_cache = self._load_equity_history(2000)
                            self._fast_equity_cache_ts = now_ts
                            equity_history = self._fast_equity_cache or []
                elif float(getattr(self, "_fast_live_balance", 0.0) or 0.0) > 0:
                    account_balance = float(getattr(self, "_fast_live_balance", 0.0) or 0.0)
            except Exception as e:
                if getattr(self, "_log_ready", False):
                    self._log.warning(f"快速权益实时刷新失败: {e}")
        account_equity_pnl = round(account_balance - account_initial_equity, 2) if account_initial_equity > 0 and account_balance > 0 else None
        account_pnl = account_equity_pnl if account_equity_pnl is not None else net_pnl
        pnl_reconcile_diff = round(account_pnl - net_pnl, 2) if account_equity_pnl is not None else 0.0
        daily_account_pnl = None
        if account_equity_pnl is not None and equity_history:
            today_points = [
                x for x in equity_history
                if str(x.get("time", "")).startswith(today_key)
                and (
                    account_initial_equity <= 0
                    or abs(float(x.get("account_initial_equity", 0) or 0.0) - account_initial_equity) < 0.01
                )
            ]
            if today_points:
                try:
                    first_balance = float(today_points[0].get("account_balance", account_balance) or account_balance)
                    daily_account_pnl = round(account_balance - first_balance, 2)
                except Exception:
                    daily_account_pnl = None
        if daily_account_pnl is None and account_equity_pnl is not None and state.get("daily_account_pnl") is not None and abs(float(state.get("account_balance", 0) or 0) - account_balance) < 0.01:
            try:
                daily_account_pnl = round(float(state.get("daily_account_pnl") or 0.0), 2)
            except Exception:
                daily_account_pnl = None
        daily_display_pnl = daily_account_pnl if daily_account_pnl is not None else daily_realized_pnl
        daily_display_basis = "equity" if daily_account_pnl is not None else "trade_log"
        if now_ts - self._fast_drawdown_cache_ts > 30 or not self._fast_drawdown_cache:
            state = self._load_equity_state()
            if equity_history:
                self._fast_drawdown_cache = self._max_drawdown_summary(
                    equity_history,
                    account_balance,
                    account_initial_equity,
                    account_pnl,
                    open_pnl,
                )
            elif state.get("max_drawdown_abs") is not None and state.get("max_drawdown_pct") is not None:
                self._fast_drawdown_cache = {
                    "max_drawdown_abs": round(float(state.get("max_drawdown_abs", 0) or 0), 2),
                    "max_drawdown_pct": round(float(state.get("max_drawdown_pct", 0) or 0), 2),
                    "max_drawdown_basis": state.get("max_drawdown_basis", "equity"),
                }
            else:
                self._fast_drawdown_cache = self._max_drawdown_summary(
                    equity_history,
                    account_balance,
                    account_initial_equity,
                    account_pnl,
                    open_pnl,
                )
            self._fast_drawdown_cache_ts = now_ts
        max_drawdown = dict(self._fast_drawdown_cache or {})
        if account_balance > 0 and account_initial_equity > 0:
            self._save_equity_state({
                **max_drawdown,
                "updated_at": bj_now().isoformat(),
                "account_balance": round(float(account_balance or 0.0), 4),
                "account_initial_equity": round(float(account_initial_equity or 0.0), 4),
                "account_pnl": round(float(account_pnl or 0.0), 4),
                "daily_account_pnl": daily_account_pnl,
                "daily_display_pnl": daily_display_pnl,
                "daily_display_basis": daily_display_basis,
            })
        rj_watchlist = getattr(self, "_rj_watchlist", {}) or {}
        rj_setup_pool_rows = self._rj_setup_pool_rows(limit=40, now_ts=now_ts)
        rj_pipeline = self._rj_pipeline_status(rj_setup_pool_rows)
        return {
            "fast": True,
            "running": self.running,
            "uptime": int((bj_now() - self.start_time).total_seconds()) if self.start_time else 0,
            "start_time": self.start_time.isoformat() if self.start_time else "",
            "testnet": self.cfg.testnet,
            "market_type": self.cfg.market_type,
            "status": self.status_text,
            "positions": [
                {
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "entry": round(p.entry_price, 6),
                    "current_price": round(getattr(p, 'current_price', p.entry_price), 6),
                    "sl": round(p.current_sl, 6),
                    "qty": round(p.quantity, 6),
                    "entry_value": round(p.quantity * p.entry_price, 2),
                    "value": round(p.quantity * float(getattr(p, 'current_price', p.entry_price) or p.entry_price), 2),
                    "risk": round(p.risk_usdt, 2),
                    "score": p.signal_score,
                    "breakeven": p.breakeven_triggered,
                    "max_favorable_r": round(getattr(p, 'max_favorable_r', 0.0), 2),
                    "time_stop_armed": getattr(p, 'time_stop_armed', True),
                    "time_stop_armed_at": p.time_stop_armed_at.isoformat() if getattr(p, 'time_stop_armed_at', None) else "",
                    "pnl": round(p.pnl, 2),
                    "pnl_pct": round((p.pnl / max(p.quantity * p.entry_price, 1e-9)) * 100, 2) if p.quantity and p.entry_price else 0,
                    "entered": p.entry_time.isoformat() if p.entry_time else "",
                    "source_interval": p.source_interval,
                    "source_strategy": getattr(p, "source_strategy", ""),
                    "target_zone_type": getattr(p, "target_zone_type", ""),
                    "target_zone_price": round(float(getattr(p, "target_zone_price", 0.0) or 0.0), 6),
                    "target_r": round(float(getattr(p, "target_r", 0.0) or 0.0), 4),
                    "hermes_confirm": self._public_hermes_confirm(getattr(p, "hermes_confirm", {})),
                    "choppy_filter": self._position_choppy_filter(getattr(p, "choppy_filter", {})),
                }
                for p in self.positions
            ],
            "daily_loss": round(self.daily_loss, 2),
            "consecutive_losses": self.consecutive_losses,
            "fuel_balance": self.cfg.fuel_balance,
            "fuel_enabled": self.cfg.fuel_enabled,
            "account_balance": round(float(account_balance or 0.0), 2),
            "account_initial_equity": account_initial_equity,
            "account_equity_pnl": account_equity_pnl,
            "account_pnl": account_pnl,
            "pnl_reconcile_diff": pnl_reconcile_diff,
            "pnl_basis": "equity" if account_equity_pnl is not None else "trade_log",
            "equity_history": equity_history,
            **max_drawdown,
            "commission_rate": self.cfg.commission_rate,
            "cfg_risk": self.cfg.risk_per_trade,
            "cfg_maxval": self.cfg.max_position_usdt,
            "cfg_leverage": self.cfg.leverage,
            "cfg_atr_mult": self.cfg.atr_trail_mult,
            "cfg_maxpos": self.cfg.max_positions,
            "cfg_interval": self.cfg.scan_interval,
            "entry_signal_source": self._entry_signal_source(),
            "last_scan": self.last_scan_time.isoformat() if self.last_scan_time else "",
            "last_signals": self.last_signal_count,
            "trade_count": len(self.trade_log),
            "signals": self.last_signals_data,
            "rj_setup_pool_size": len(getattr(self, "_rj_setup_pool", {}) or {}),
            "rj_setup_pool": rj_setup_pool_rows,
            "predicta_setup_pool_size": len(getattr(self, "_predicta_setup_pool", {}) or {}),
            "predicta_setup_pool": self._predicta_setup_pool_rows(limit=40),
            "rj_pipeline": rj_pipeline,
            "rj_watchlist_enabled": bool(getattr(self.cfg, "rj_only_watchlist_enabled", True)),
            "rj_watchlist_size": len(rj_watchlist.get("symbols", []) or []),
            "rj_watchlist_updated_at": rj_watchlist.get("updated_at", ""),
            "rj_watchlist": (rj_watchlist.get("rows", []) or [])[:20],
            "recent_trades": self.trade_log,
            "total_trades": len(self.trade_log),
            "realized_pnl": realized_pnl,
            "record_realized_pnl": realized_pnl,
            "open_pnl": open_pnl,
            "net_pnl": net_pnl,
            "record_net_pnl": net_pnl,
            "total_pnl": realized_pnl,
            "win_rate": round(win_count / max(len(self.trade_log), 1) * 100, 1),
            "daily_trade_count": sum(1 for t in self.trade_log if str(t.get("time", "")).startswith(today_key)),
            "daily_realized_pnl": daily_realized_pnl,
            "daily_record_pnl": daily_realized_pnl,
            "daily_account_pnl": daily_account_pnl,
            "daily_equity_pnl": daily_account_pnl,
            "daily_pnl": daily_realized_pnl,
            "daily_pnl_basis": "trade_log",
            "accounting": {
                "basis": "equity" if account_equity_pnl is not None else "trade_log",
                "base_equity": round(account_initial_equity, 2),
                "account_balance": round(float(account_balance or 0.0), 2),
                "account_net_pnl": account_pnl,
                "record_realized_pnl": realized_pnl,
                "open_pnl": open_pnl,
                "record_net_pnl": net_pnl,
                "reconcile_diff": pnl_reconcile_diff,
                "daily_account_pnl": daily_account_pnl,
                "daily_record_pnl": daily_realized_pnl,
                "daily_display_pnl": daily_display_pnl,
                "daily_display_basis": daily_display_basis,
                "trade_count": len(self.trade_log),
                "win_rate": round(win_count / max(len(self.trade_log), 1) * 100, 1),
                "has_equity_basis": account_equity_pnl is not None,
            },
            "daily_return_pct": round(daily_realized_pnl / max(account_initial_equity or account_balance or 1000, 1) * 100, 2),
        }

    def get_summary(self) -> dict:
        """返回给 Web UI 的状态摘要"""
        self._sync_position_market_snapshot()
        today_key = bj_now().strftime("%Y-%m-%d")
        realized_pnl = round(sum(self._trade_pnl_value(t) for t in self.trade_log), 2)
        open_pnl = round(sum(float(getattr(p, "pnl", 0.0) or 0.0) for p in self.positions), 2)
        net_pnl = round(realized_pnl + open_pnl, 2)
        daily_realized_pnl = round(
            sum(self._trade_pnl_value(t) for t in self.trade_log if str(t.get("time", "")).startswith(today_key)),
            2,
        )
        account_balance = self.client.get_balance() if self.client else 0
        try:
            account_initial_equity = float(getattr(self.cfg, "account_initial_equity", 0.0) or 0.0)
        except Exception:
            account_initial_equity = 0.0
        account_equity_pnl = round(account_balance - account_initial_equity, 2) if account_initial_equity > 0 and account_balance > 0 else None
        account_pnl = account_equity_pnl if account_equity_pnl is not None else net_pnl
        pnl_reconcile_diff = round(account_pnl - net_pnl, 2) if account_equity_pnl is not None else 0.0
        self._append_equity_snapshot(account_balance, account_initial_equity, net_pnl, open_pnl, reason="summary")
        equity_history = self._load_equity_history(2000)
        drawdown_history = self._load_equity_history(100000) if equity_history else []
        max_drawdown = self._max_drawdown_summary(
            drawdown_history or equity_history,
            account_balance,
            account_initial_equity,
            account_pnl,
            open_pnl,
        )
        self._fast_drawdown_cache = dict(max_drawdown or {})
        self._fast_drawdown_cache_ts = time.time()
        self._save_equity_state({
            **(max_drawdown or {}),
            "updated_at": bj_now().isoformat(),
            "account_balance": round(float(account_balance or 0.0), 4),
            "account_initial_equity": round(float(account_initial_equity or 0.0), 4),
            "account_pnl": round(float(account_pnl or 0.0), 4),
        })
        daily_account_pnl = None
        if account_equity_pnl is not None and equity_history:
            today_points = [
                x for x in equity_history
                if str(x.get("time", "")).startswith(today_key)
                and (
                    account_initial_equity <= 0
                    or abs(float(x.get("account_initial_equity", 0) or 0.0) - account_initial_equity) < 0.01
                )
            ]
            if today_points:
                try:
                    daily_account_pnl = round(account_balance - float(today_points[0].get("account_balance", account_balance) or account_balance), 2)
                except Exception:
                    daily_account_pnl = None
        account_daily_basis = "equity" if daily_account_pnl is not None else "trade_log"
        daily_pnl_value = daily_realized_pnl
        daily_display_pnl = daily_account_pnl if daily_account_pnl is not None else daily_realized_pnl
        daily_return_pct = round(daily_pnl_value / max(account_initial_equity or account_balance or 1000, 1) * 100, 2)
        self._save_equity_state({
            **(max_drawdown or {}),
            "updated_at": bj_now().isoformat(),
            "account_balance": round(float(account_balance or 0.0), 4),
            "account_initial_equity": round(float(account_initial_equity or 0.0), 4),
            "account_pnl": round(float(account_pnl or 0.0), 4),
            "daily_account_pnl": daily_account_pnl,
            "daily_display_pnl": daily_display_pnl,
            "daily_display_basis": account_daily_basis,
        })
        win_count = sum(1 for t in self.trade_log if self._trade_pnl_value(t) > 0)
        try:
            self._load_rj_watchlist()
        except Exception:
            pass
        rj_watchlist = getattr(self, "_rj_watchlist", {}) or {}
        now_ts = time.time()
        rj_setup_pool_rows = self._rj_setup_pool_rows(limit=40, now_ts=now_ts)
        rj_pipeline = self._rj_pipeline_status(rj_setup_pool_rows)
        return {
            "running": self.running,
            "uptime": int((bj_now() - self.start_time).total_seconds()) if self.start_time else 0,
            "start_time": self.start_time.isoformat() if self.start_time else "",
            "testnet": self.cfg.testnet,
            "market_type": self.cfg.market_type,
            "status": self.status_text,
            "positions": [
                {
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "entry": round(p.entry_price, 6),
                    "current_price": round(getattr(p, 'current_price', p.entry_price), 6),
                    "sl": round(p.current_sl, 6),
                    "qty": round(p.quantity, 6),
                    "entry_value": round(p.quantity * p.entry_price, 2),
                    "value": round(p.quantity * float(getattr(p, 'current_price', p.entry_price) or p.entry_price), 2),
                    "risk": round(p.risk_usdt, 2),
                    "score": p.signal_score,
                    "breakeven": p.breakeven_triggered,
                    "max_favorable_r": round(getattr(p, 'max_favorable_r', 0.0), 2),
                    "time_stop_armed": getattr(p, 'time_stop_armed', True),
                    "time_stop_armed_at": p.time_stop_armed_at.isoformat() if getattr(p, 'time_stop_armed_at', None) else "",
                    "pnl": round(p.pnl, 2),
                    "pnl_pct": round((p.pnl / max(p.quantity * p.entry_price, 1e-9)) * 100, 2) if p.quantity and p.entry_price else 0,
                    "entered": p.entry_time.isoformat() if p.entry_time else "",
                    "source_interval": p.source_interval,
                    "source_strategy": getattr(p, "source_strategy", ""),
                    "time_stop_watch": bool(getattr(p, "time_stop_watch", False)),
                    "time_stop_first_seen_bars": int(getattr(p, "time_stop_first_seen_bars", 0) or 0),
                    "time_stop_first_seen_r": round(float(getattr(p, "time_stop_first_seen_r", 0.0) or 0.0), 4),
                    "time_stop_first_seen_mfe": round(float(getattr(p, "time_stop_first_seen_mfe", 0.0) or 0.0), 4),
                    "target_zone_type": getattr(p, "target_zone_type", ""),
                    "target_zone_price": round(float(getattr(p, "target_zone_price", 0.0) or 0.0), 6),
                    "target_zone_low": round(float(getattr(p, "target_zone_low", 0.0) or 0.0), 6),
                    "target_zone_high": round(float(getattr(p, "target_zone_high", 0.0) or 0.0), 6),
                    "target_r": round(float(getattr(p, "target_r", 0.0) or 0.0), 4),
                    "target_distance_pct": round(float(getattr(p, "target_distance_pct", 0.0) or 0.0), 4),
                    "target_zone_bars_ago": int(getattr(p, "target_zone_bars_ago", 0) or 0),
                    "hermes_confirm": self._public_hermes_confirm(getattr(p, "hermes_confirm", {})),
                    "choppy_filter": self._position_choppy_filter(getattr(p, "choppy_filter", {})),
                }
                for p in self.positions
            ],
            "daily_loss": round(self.daily_loss, 2),
            "consecutive_losses": self.consecutive_losses,
            "fuel_balance": self.cfg.fuel_balance,
            "fuel_enabled": self.cfg.fuel_enabled,
            "account_balance": account_balance,
            "account_initial_equity": account_initial_equity,
            "account_equity_pnl": account_equity_pnl,
            "account_pnl": account_pnl,
            "pnl_reconcile_diff": pnl_reconcile_diff,
            "pnl_basis": "equity" if account_equity_pnl is not None else "trade_log",
            "equity_history": equity_history,
            **max_drawdown,
            "commission_rate": self.cfg.commission_rate,
            "cfg_risk": self.cfg.risk_per_trade,
            "cfg_maxval": self.cfg.max_position_usdt,
            "cfg_leverage": self.cfg.leverage,
            "cfg_atr_mult": self.cfg.atr_trail_mult,
            "cfg_maxpos": self.cfg.max_positions,
            "cfg_interval": self.cfg.scan_interval,
            "entry_signal_source": self._entry_signal_source(),
            "last_scan": self.last_scan_time.isoformat() if self.last_scan_time else "",
            "last_signals": self.last_signal_count,
            "trade_count": len(self.trade_log),
            "signals": self.last_signals_data,
            "rj_setup_pool_size": len(getattr(self, "_rj_setup_pool", {}) or {}),
            "rj_setup_pool": rj_setup_pool_rows,
            "predicta_setup_pool_size": len(getattr(self, "_predicta_setup_pool", {}) or {}),
            "predicta_setup_pool": self._predicta_setup_pool_rows(limit=40),
            "rj_pipeline": rj_pipeline,
            "rj_watchlist_enabled": bool(getattr(self.cfg, "rj_only_watchlist_enabled", True)),
            "rj_watchlist_size": len(rj_watchlist.get("symbols", []) or []),
            "rj_watchlist_updated_at": rj_watchlist.get("updated_at", ""),
            "rj_watchlist": (rj_watchlist.get("rows", []) or [])[:20],
            "recent_trades": self.trade_log,  # 全量，前端分页
            "total_trades": len(self.trade_log),
            "realized_pnl": realized_pnl,
            "record_realized_pnl": realized_pnl,
            "open_pnl": open_pnl,
            "net_pnl": net_pnl,
            "record_net_pnl": net_pnl,
            "total_pnl": realized_pnl,
            "win_rate": round(
                win_count / max(len(self.trade_log), 1) * 100, 1
            ),
            # 今日统计
            "daily_trade_count": sum(1 for t in self.trade_log if str(t.get("time","")).startswith(today_key)),
            "daily_realized_pnl": daily_realized_pnl,
            "daily_record_pnl": daily_realized_pnl,
            "daily_account_pnl": daily_account_pnl,
            "daily_equity_pnl": daily_account_pnl,
            "daily_pnl": daily_pnl_value,
            "daily_pnl_basis": "trade_log",
            "accounting": {
                "basis": "equity" if account_equity_pnl is not None else "trade_log",
                "base_equity": round(account_initial_equity, 2),
                "account_balance": round(float(account_balance or 0.0), 2),
                "account_net_pnl": account_pnl,
                "record_realized_pnl": realized_pnl,
                "open_pnl": open_pnl,
                "record_net_pnl": net_pnl,
                "reconcile_diff": pnl_reconcile_diff,
                "daily_account_pnl": daily_account_pnl,
                "daily_record_pnl": daily_realized_pnl,
                "daily_display_pnl": daily_display_pnl,
                "daily_display_basis": account_daily_basis,
                "trade_count": len(self.trade_log),
                "win_rate": round(win_count / max(len(self.trade_log), 1) * 100, 1),
                "has_equity_basis": account_equity_pnl is not None,
            },
            "daily_return_pct": daily_return_pct,
        }


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    args = sys.argv[1:]
    mode = "paper"
    if "--live" in args:
        mode = "live"
    elif "--paper" in args:
        mode = "paper"

    cfg = TradeConfig.load()
    cfg.mode = mode

    if mode == "live":
        if not cfg.api_key or not cfg.api_secret:
            # 尝试从环境变量读取
            cfg.api_key = os.getenv("BINANCE_API_KEY", "")
            cfg.api_secret = os.getenv("BINANCE_API_SECRET", "")
            if not cfg.api_key:
                log.error("实盘模式需要配置API Key. "
                          "请设置环境变量 BINANCE_API_KEY / BINANCE_API_SECRET "
                          "或在 trade_config.json 中配置")
                sys.exit(1)
        cfg.enabled = True

    bot = SqueezeBreakoutBot(cfg)
    print(f"\n{'='*60}")
    print(f"  均线粘合起爆点 自动交易引擎")
    print(f"  模式: {mode.upper()}")
    print(f"  扫描周期: {cfg.scan_interval}")
    print(f"  最低评分: {cfg.min_score}")
    print(f"  风险/笔: ${cfg.risk_per_trade}")
    print(f"  最大持仓: {cfg.max_positions}")
    print(f"{'='*60}\n")

    bot.run_loop()
