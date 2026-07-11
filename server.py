"""
MACD 背离策略 — 自动化交易服务
用法: python server.py          (只发信号, 不交易)
      python server.py --paper  (模拟交易)
      python server.py --live   (实盘交易, 需配置API)
"""

import sys, time, json, logging
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd
import numpy as np

import config as cfg
from strategy import MACDStrategy


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("macd_bot.log")]
)
log = logging.getLogger(__name__)


class DataFeed:
    """Binance 数据源"""

    def __init__(self, symbol, interval, max_bars=5000):
        self.symbol = symbol
        self.interval = interval
        self.max_bars = max_bars
        self.base_url = "https://api.binance.com/api/v3/klines"

    def fetch(self, limit=200):
        """获取最新K线"""
        params = {"symbol": self.symbol, "interval": self.interval, "limit": limit}
        try:
            resp = requests.get(self.base_url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Binance API error: {e}")
            return None

        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_buy_vol",
            "taker_buy_quote_vol", "ignore"
        ])
        df = df[["open_time", "open", "high", "low", "close", "volume"]]
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = df["open_time"].astype(int)
        return df

    def update_cache(self, filepath="cache_15m.parquet"):
        """更新本地缓存"""
        old = None
        if Path(filepath).exists():
            try:
                old = pd.read_parquet(filepath)
            except Exception:
                pass

        new_data = self.fetch(limit=500)
        if new_data is None:
            return old

        if old is not None:
            combined = pd.concat([old, new_data], ignore_index=True)
            combined = combined.drop_duplicates("open_time").sort_values("open_time")
            combined = combined.tail(self.max_bars).reset_index(drop=True)
        else:
            combined = new_data

        combined.to_parquet(filepath, index=False)
        return combined


class TradeManager:
    """交易管理: 仓位 + 风控 + 通知"""

    def __init__(self, mode="signal"):
        self.mode = mode  # signal / paper / live
        self.position = 0.0
        self.avg_price = 0.0
        self.entry_time = None
        self.sl_price = 0.0
        self.trail_high = 0.0
        self.trail_low = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self.last_trade_day = None
        self.peak_equity = cfg.INITIAL_CAPITAL if hasattr(cfg, 'INITIAL_CAPITAL') else 1000.0

        # Paper trading
        self.paper_capital = 1000.0
        self.paper_equity = []

        self.trade_log = []

    def _dd_pct(self):
        """当前回撤%"""
        if self.mode == "paper":
            eq = self.paper_capital
            if self.position > 0:
                # Simple equity estimate
                pass
        return 0.0  # simplified

    def position_pct(self):
        """DD阶梯仓位%"""
        dd = self._dd_pct()
        if dd < 20:
            return cfg.POS_FULL
        elif dd < 30:
            return cfg.POS_HALF
        else:
            return cfg.POS_MIN

    def enter(self, direction, price, signal_low, signal_high, df):
        """开仓"""
        if self.position != 0:
            return False

        # 日交易限制
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_trade_day:
            self.trades_today = 0; self.last_trade_day = today
        if self.trades_today >= cfg.MAX_DAILY_TRADES:
            log.warning(f"已达日交易上限 {cfg.MAX_DAILY_TRADES}")
            return False

        # 连续止损暂停
        if self.consecutive_losses >= cfg.MAX_CONSECUTIVE_LOSS:
            log.warning(f"连续止损{cfg.MAX_CONSECUTIVE_LOSS}次, 暂停交易")
            return False

        atr_val = strategy_module.atr(
            df["high"].values, df["low"].values, df["close"].values, cfg.ATR_LEN
        )[-1] if len(df) > cfg.ATR_LEN else price * 0.005

        if direction == "long":
            sl = signal_low - atr_val * cfg.SL_ATR_MULT
        else:
            sl = signal_high + atr_val * cfg.SL_ATR_MULT

        # 仓位大小
        pos_pct = self.position_pct()
        usdt_size = self.paper_capital * pos_pct / 100 if self.mode == "paper" else cfg.MAX_POSITION_USDT
        qty = usdt_size / price

        self.position = qty if direction == "long" else -qty
        self.avg_price = price
        self.sl_price = sl
        self.entry_time = datetime.now(timezone.utc)
        self.trail_high = price; self.trail_low = price

        log.info(f"开{'多' if direction=='long' else '空'}: 价{price:.1f} SL{sl:.1f} 仓{pos_pct}% 量{qty:.4f}")
        return True

    def check_exit(self, high, low, close, cur_atr, df):
        """检查出场"""
        if self.position == 0:
            return None

        pnl = 0.0; result = None
        entry_r = abs(self.avg_price - self.sl_price)

        if self.position > 0:
            self.trail_high = max(self.trail_high, high)
            cur_sl = self.sl_price
            # 保本触发
            if high >= self.avg_price + entry_r * cfg.BREAKEVEN_R:
                trail_sl = self.trail_high - cur_atr * cfg.TRAIL_MULT
                cur_sl = max(cur_sl, max(trail_sl, self.avg_price))
            if low <= cur_sl:
                pnl = self.position * (cur_sl - self.avg_price)
                result = "trail_tp" if cur_sl > self.avg_price else "sl"

        else:
            self.trail_low = min(self.trail_low, low)
            cur_sl = self.sl_price
            if low <= self.avg_price - entry_r * cfg.BREAKEVEN_R:
                trail_sl = self.trail_low + cur_atr * cfg.TRAIL_MULT
                cur_sl = min(cur_sl, min(trail_sl, self.avg_price))
            if high >= cur_sl:
                pnl = -self.position * (self.avg_price - cur_sl)
                result = "trail_tp" if cur_sl < self.avg_price else "sl"

        if result:
            self.position = 0.0; self.trades_today += 1
            if pnl > 0: self.consecutive_losses = 0
            else: self.consecutive_losses += 1

            if self.mode == "paper":
                self.paper_capital += pnl

            self.trade_log.append({
                "time": datetime.now(timezone.utc).isoformat(),
                "result": result, "pnl": round(pnl, 2),
                "capital": round(self.paper_capital, 2)
            })
            log.info(f"出场: {result} PnL:\${pnl:+.2f} 资本:\${self.paper_capital:.0f}")

        return result


# 全局实例
strategy_module = None  # hack for import


class SignalBot:
    """主循环"""

    def __init__(self, mode="signal"):
        self.mode = mode
        self.data = DataFeed(cfg.SYMBOL, cfg.TIMEFRAME, cfg.MAX_BARS)
        self.strategy = MACDStrategy(cfg)
        self.trader = TradeManager(mode)

    def run_once(self):
        """执行一次检查"""
        df = self.data.update_cache()
        if df is None or len(df) < 200:
            log.warning("数据不足, 跳过")
            return None

        i = len(df) - 1
        close = df["close"].iloc[-1]; high = df["high"].iloc[-1]; low = df["low"].iloc[-1]
        cur_atr = self._get_atr(df)

        # === 先检查出场 ===
        exit_result = self.trader.check_exit(high, low, close, cur_atr, df)

        # === 再检查入场 ===
        long_sig, short_sig, info = self.strategy.check(df, i)

        if long_sig and self.trader.position == 0:
            self.trader.enter("long", close, info["signal_low"], info["signal_high"], df)
            self._notify(f"📈 做多信号\n价格:{close:.1f} ATR:{cur_atr:.1f}\nSL:{info['signal_low']-cur_atr:.1f}")

        if short_sig and self.trader.position == 0:
            self.trader.enter("short", close, info["signal_low"], info["signal_high"], df)
            self._notify(f"📉 做空信号\n价格:{close:.1f} ATR:{cur_atr:.1f}\nSL:{info['signal_high']+cur_atr:.1f}")

        # === 状态摘要 ===
        status = {
            "time": datetime.fromtimestamp(df["open_time"].iloc[-1]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "price": round(close, 1),
            "position": "LONG" if self.trader.position > 0 else "SHORT" if self.trader.position < 0 else "FLAT",
            "daily_trend": "↑" if info["daily_trend"] >= 1 else "↓" if info["daily_trend"] <= -1 else "↔",
            "state": ("激活中" if info["bull_activated"] or info["bear_activated"]
                     else "装弹中" if info["bull_armed"] or info["bear_armed"]
                     else "空"),
            "signal": "LONG!" if long_sig else "SHORT!" if short_sig else "-",
            "exit": exit_result or "-",
            "capital": round(self.trader.paper_capital, 0) if self.mode == "paper" else "-",
        }

        return status

    def _get_atr(self, df):
        """计算当前ATR"""
        from strategy import atr
        a = atr(df["high"].values, df["low"].values, df["close"].values, cfg.ATR_LEN)
        return a[-1] if not np.isnan(a[-1]) else df["close"].iloc[-1] * 0.005

    def _notify(self, msg):
        """发送通知"""
        log.info(msg)
        if cfg.TELEGRAM_TOKEN and cfg.TELEGRAM_CHAT_ID:
            try:
                url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
                requests.post(url, json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
            except Exception:
                pass

    def run_loop(self, interval_sec=60):
        """持续运行, 每N秒检查一次"""
        log.info(f"MACD Bot 启动 [{self.mode}] — {cfg.SYMBOL} {cfg.TIMEFRAME}")
        log.info(f"参数: MACD({cfg.MACD_FAST}/{cfg.MACD_SLOW}/{cfg.MACD_SIG}) "
                 f"T{cfg.MIN_TROUGHS} R{cfg.MIN_PEAK_RATIO} L{cfg.FRACTAL_LOOKBACK} "
                 f"BE{cfg.BREAKEVEN_R} Trail{cfg.TRAIL_MULT}")

        status_count = 0
        while True:
            try:
                status = self.run_once()
                if status:
                    status_count += 1
                    if status_count % 10 == 0:  # 每10次打印表头
                        log.info(f"{'Time':<16s} {'Price':>8s} {'Pos':6s} {'DT':3s} {'State':6s} {'Sig':8s} {'Exit':8s} {'Cap':>8s}")
                    log.info(f"{status['time']:<16s} {status['price']:>8.1f} {status['position']:6s} "
                             f"{status['daily_trend']:3s} {status['state']:6s} {status['signal']:8s} "
                             f"{status['exit']:8s} {status['capital']:>8s}")

                time.sleep(interval_sec)

            except KeyboardInterrupt:
                log.info("手动停止")
                break
            except Exception as e:
                log.error(f"异常: {e}", exc_info=True)
                time.sleep(interval_sec * 5)


if __name__ == "__main__":
    mode = "signal"
    if "--paper" in sys.argv:
        mode = "paper"
    elif "--live" in sys.argv:
        mode = "live"

    bot = SignalBot(mode)
    bot.run_loop(interval_sec=30)  # 每30秒检查一次
