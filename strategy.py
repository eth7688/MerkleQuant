"""
MACD 背离策略引擎 — 从回测移植
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def macd_hist(closes, fast=13, slow=34, sig=9):
    ml = ema(pd.Series(closes), fast) - ema(pd.Series(closes), slow)
    sl = ema(ml, sig)
    return (ml - sl).values


def atr(highs, lows, closes, period=13):
    h = np.array(highs); l = np.array(lows); c = np.array(closes)
    tr = np.maximum(h - l,
           np.maximum(abs(h - np.roll(c, 1)),
                      abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    return pd.Series(tr).rolling(period).mean().values


class MACDStrategy:
    def __init__(self, config):
        self.cfg = config
        self.reset()

    def reset(self):
        self.bull_armed = False
        self.bull_arm_bar = -9999
        self.bull_activated = False
        self.bull_act_bar = -9999
        self.bear_armed = False
        self.bear_arm_bar = -9999
        self.bear_activated = False
        self.bear_act_bar = -9999
        self.signal_low = 0.0
        self.signal_high = 0.0

        # 波峰/波谷追踪
        self.t_vals = []   # [(val, price), ...]
        self.p_vals = []

        # 分型追踪: 历史K线
        self.history = None  # DataFrame

        # 日线趋势
        self.daily_trend = 0
        self.daily_history = None

    def _detect_divergence(self, hist, lows, highs, i):
        """在bar i检测MACD背离"""
        cfg = self.cfg

        # 波谷检测
        if i >= 3 and hist[i-1] < 0 and hist[i-2] > hist[i-1] and hist[i] > hist[i-1]:
            self.t_vals.append((hist[i-1], lows[i-1]))
            if len(self.t_vals) > 20:
                self.t_vals.pop(0)

        # 波峰检测
        if i >= 3 and hist[i-1] > 0 and hist[i-2] < hist[i-1] and hist[i] < hist[i-1]:
            self.p_vals.append((hist[i-1], highs[i-1]))
            if len(self.p_vals) > 20:
                self.p_vals.pop(0)

        bull_div = False; bear_div = False

        if cfg.MIN_TROUGHS == 3 and len(self.t_vals) >= 3:
            t3, t2, t1 = self.t_vals[-3], self.t_vals[-2], self.t_vals[-1]
            if t3[0] < 0 and t2[0] < 0 and t1[0] < 0:
                if t1[0] > t2[0] and t2[0] > t3[0] and t1[1] < t2[1] and t2[1] < t3[1]:
                    d21 = (t2[0]-t3[0]) / abs(t3[0]) if abs(t3[0]) > 0 else 0
                    d10 = (t1[0]-t2[0]) / abs(t2[0]) if abs(t2[0]) > 0 else 0
                    if d21 > cfg.MIN_PEAK_RATIO and d10 > cfg.MIN_PEAK_RATIO:
                        bull_div = True; self.t_vals = []

        if cfg.MIN_TROUGHS == 3 and len(self.p_vals) >= 3:
            p3, p2, p1 = self.p_vals[-3], self.p_vals[-2], self.p_vals[-1]
            if p3[0] > 0 and p2[0] > 0 and p1[0] > 0:
                if p1[0] < p2[0] and p2[0] < p3[0] and p1[1] > p2[1] and p2[1] > p3[1]:
                    d21 = (p3[0]-p2[0]) / p3[0] if p3[0] > 0 else 0
                    d10 = (p2[0]-p1[0]) / p2[0] if p2[0] > 0 else 0
                    if d21 > cfg.MIN_PEAK_RATIO and d10 > cfg.MIN_PEAK_RATIO:
                        bear_div = True; self.p_vals = []

        if cfg.MIN_TROUGHS == 2 and len(self.t_vals) >= 2:
            t2, t1 = self.t_vals[-2], self.t_vals[-1]
            if t2[0] < 0 and t1[0] < 0 and t1[0] > t2[0] and t1[1] < t2[1]:
                d = (t1[0]-t2[0]) / abs(t2[0]) if abs(t2[0]) > 0 else 0
                if d > cfg.MIN_PEAK_RATIO:
                    bull_div = True; self.t_vals = []

        if cfg.MIN_TROUGHS == 2 and len(self.p_vals) >= 2:
            p2, p1 = self.p_vals[-2], self.p_vals[-1]
            if p2[0] > 0 and p1[0] > 0 and p1[0] < p2[0] and p1[1] > p2[1]:
                d = (p2[0]-p1[0]) / p2[0] if p2[0] > 0 else 0
                if d > cfg.MIN_PEAK_RATIO:
                    bear_div = True; self.p_vals = []

        return bull_div, bear_div

    def _histogram_flip(self, hist, i):
        """MACD柱实心→虚心翻转"""
        if i < 2:
            return False, False
        # Long: solid fall -> hollow rise
        solid_fall = hist[i-1] < 0 and hist[i-1] < hist[i-2]
        hollow_rise = hist[i] < 0 and hist[i] > hist[i-1]
        flip_long = hollow_rise and solid_fall
        # Short: solid rise -> hollow fall
        solid_rise = hist[i-1] > 0 and hist[i-1] > hist[i-2]
        hollow_fall = hist[i] > 0 and hist[i] < hist[i-1]
        flip_short = hollow_fall and solid_rise
        return flip_long, flip_short

    def _check_fractal(self, highs, lows, closes, i):
        """15m分型完全确认"""
        cfg = self.cfg
        lb = cfg.FRACTAL_LOOKBACK
        if i < lb + 1:
            return False, False

        # 底分型
        window_low = lows[i-lb:i+1]
        k0_low = window_low.min()
        k0_idx = np.argmin(window_low)
        bot_frac = False
        if k0_idx < i:
            k0_high = highs[i-lb + k0_idx]
            after_lows = lows[i-lb+k0_idx+1 : i+1]
            if len(after_lows) > 0 and after_lows.min() >= k0_low:
                if closes[i] > k0_high:
                    bot_frac = True

        # 顶分型
        window_high = highs[i-lb:i+1]
        k0_high = window_high.max()
        k0_idx = np.argmax(window_high)
        top_frac = False
        if k0_idx < i:
            k0_low = lows[i-lb + k0_idx]
            after_highs = highs[i-lb+k0_idx+1 : i+1]
            if len(after_highs) > 0 and after_highs.max() <= k0_high:
                if closes[i] < k0_low:
                    top_frac = True

        return bot_frac, top_frac

    def _update_daily(self, df_15m):
        """从15m数据推算日线分型趋势"""
        if df_15m is None or len(df_15m) < 24 * 4:
            return 0

        # 简单: 用最近几根日线高点低点
        # 取每日OHLC
        closes = df_15m["close"].values
        highs = df_15m["high"].values
        lows = df_15m["low"].values

        # 粗略: 最近几个"日" = 每96根15m算一天
        n = len(closes)
        daily_bars = []
        for start in range(0, n, 96):
            end = min(start + 96, n)
            if end - start < 10:  # skip incomplete days
                continue
            daily_bars.append({
                'high': highs[start:end].max(),
                'low': lows[start:end].min(),
            })

        if len(daily_bars) < 3:
            return 0

        # 找最近底/顶分型
        trend = 0
        for j in range(2, len(daily_bars)):
            # 顶分型
            if daily_bars[j-1]['high'] > daily_bars[j-2]['high'] and daily_bars[j-1]['high'] > daily_bars[j]['high']:
                trend = -1
            # 底分型
            if daily_bars[j-1]['low'] < daily_bars[j-2]['low'] and daily_bars[j-1]['low'] < daily_bars[j]['low']:
                trend = 1

        return trend

    def check(self, df_15m, i):
        """在bar i处检查信号, 返回 (long_signal, short_signal, info_dict)"""
        cfg = self.cfg

        closes = df_15m["close"].values
        highs = df_15m["high"].values
        lows = df_15m["low"].values
        n = len(closes)

        if i < 80 or n < 100:
            return False, False, {}

        # 计算指标
        hist = macd_hist(closes[:i+1], cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIG)
        atr_val = atr(highs[:i+1], lows[:i+1], closes[:i+1], cfg.ATR_LEN)[-1]

        if np.isnan(atr_val) or atr_val <= 0:
            return False, False, {}

        # 背离
        bull_div, bear_div = self._detect_divergence(hist, lows[:i+1], highs[:i+1], i)

        # 柱翻转
        flip_long, flip_short = self._histogram_flip(hist, i)

        # 日线趋势
        self.daily_trend = self._update_daily(df_15m)

        # 状态机
        if bull_div:
            self.bull_armed = True; self.bull_arm_bar = i
            self.bull_activated = False
            self.bear_armed = False; self.bear_activated = False

        if bear_div:
            self.bear_armed = True; self.bear_arm_bar = i
            self.bear_activated = False
            self.bull_armed = False; self.bull_activated = False

        if self.bull_armed and i - self.bull_arm_bar > cfg.ARMED_TIMEOUT:
            self.bull_armed = False; self.bull_activated = False
        if self.bear_armed and i - self.bear_arm_bar > cfg.ARMED_TIMEOUT:
            self.bear_armed = False; self.bear_activated = False

        if self.bull_armed and flip_long and not self.bull_activated:
            self.bull_activated = True; self.bull_act_bar = i
            self.signal_low = lows[i]

        if self.bear_armed and flip_short and not self.bear_activated:
            self.bear_activated = True; self.bear_act_bar = i
            self.signal_high = highs[i]

        if bull_div: self.bear_activated = False
        if bear_div: self.bull_activated = False

        # 分型
        bot_frac, top_frac = self._check_fractal(highs, lows, closes, i)

        daily_long_ok = self.daily_trend >= 0
        daily_short_ok = self.daily_trend <= 0

        # 入场信号
        long_cond = self.bull_activated and bot_frac and daily_long_ok
        short_cond = self.bear_activated and top_frac and daily_short_ok

        # 清状态
        if long_cond:
            self.bull_armed = False; self.bull_activated = False
        if short_cond:
            self.bear_armed = False; self.bear_activated = False

        info = {
            "bull_armed": self.bull_armed, "bull_activated": self.bull_activated,
            "bear_armed": self.bear_armed, "bear_activated": self.bear_activated,
            "daily_trend": self.daily_trend,
            "bot_frac": bot_frac, "top_frac": top_frac,
            "atr": atr_val, "hist": hist[-1],
            "signal_low": self.signal_low if long_cond else self.signal_low,
            "signal_high": self.signal_high if short_cond else self.signal_high,
        }

        return long_cond, short_cond, info
