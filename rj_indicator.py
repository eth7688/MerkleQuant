"""
RJ / BBKD indicator replica from the provided lesson.

Video rules captured here:
- RJ fast line = KDJ J line.
- RJ slow line defaults to the calibrated K line that best matched KD V8.
- Triangle setup: J crosses above R after an oversold move.
- Purple candle setup: J crosses below R after an overbought move.
- Confirm entries/exits by boxing the setup candle and waiting for a
  close beyond its high/low.
- Bollinger color provides trend context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class RJParams:
    kdj_len: int = 9
    k_smooth: int = 3
    d_smooth: int = 3
    rsi_len: int = 9
    rsi_smooth: int = 1
    kd_ma_type: str = "sma"
    slow_line_mode: str = "k"
    slow_line_scale: float = 0.88
    slow_line_offset: float = 0.0
    slow_line_clamp: bool = True
    bb_len: int = 20
    bb_mult: float = 2.0
    oversold: float = 25.0
    overbought: float = 75.0
    confirm_bars: int = 12
    divergence_lookback: int = 35


def _rma(series: pd.Series, period: int) -> pd.Series:
    p = max(1, int(period) if period else 1)
    s = pd.to_numeric(series, errors="coerce").reset_index(drop=True)
    if p <= 1:
        return s.copy()
    out = pd.Series(np.nan, index=s.index, dtype=float)
    seed_vals: list[float] = []
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


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _rma(gain, period)
    avg_loss = _rma(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def _ma(series: pd.Series, period: int, ma_type: str = "sma") -> pd.Series:
    p = max(1, int(period) if period else 1)
    s = pd.to_numeric(series, errors="coerce").reset_index(drop=True)
    kind = str(ma_type or "sma").strip().lower()
    if kind in ("rma", "wilder", "wilder_rma"):
        return _rma(s, p)
    if p <= 1:
        return s.copy()
    return s.rolling(p).mean()


def _slow_mode(raw: str) -> str:
    key = str(raw or "k").strip().lower().replace("-", "_").replace(" ", "_")
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
        "随机rsi": "stoch_rsi",
        "jrsi": "j_rsi",
        "j线rsi": "j_rsi",
    }
    return aliases.get(key, key if key in ("k", "rsi", "stoch_rsi", "j_rsi") else "k")


def _pivot_divergence(values: pd.Series, price: pd.Series, idx: int, lookback: int, bullish: bool) -> bool:
    start = max(2, idx - lookback)
    pivots: list[int] = []
    for i in range(start, idx - 1):
        if bullish:
            if price.iloc[i] <= price.iloc[i - 1] and price.iloc[i] <= price.iloc[i + 1]:
                pivots.append(i)
        else:
            if price.iloc[i] >= price.iloc[i - 1] and price.iloc[i] >= price.iloc[i + 1]:
                pivots.append(i)
    if len(pivots) < 2:
        return False
    a, b = pivots[-2], pivots[-1]
    if bullish:
        return price.iloc[b] < price.iloc[a] and values.iloc[b] > values.iloc[a]
    return price.iloc[b] > price.iloc[a] and values.iloc[b] < values.iloc[a]


def compute_rj_bbkd(df: pd.DataFrame, params: RJParams | None = None) -> dict[str, Any]:
    p = params or RJParams()
    out = df.copy().reset_index(drop=True)
    for col in ("o", "h", "l", "c", "v"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    low_n = out["l"].rolling(p.kdj_len).min()
    high_n = out["h"].rolling(p.kdj_len).max()
    rsv = ((out["c"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100).fillna(50)
    k = _ma(rsv, p.k_smooth, p.kd_ma_type)
    d = _ma(k, p.d_smooth, p.kd_ma_type)
    j = 3 * k - 2 * d

    rsi = _rsi(out["c"], p.rsi_len)
    rsi_line = (rsi if p.rsi_smooth <= 1 else rsi.ewm(span=p.rsi_smooth, adjust=False).mean()).fillna(50)
    rsi_low = rsi_line.rolling(p.rsi_len).min()
    rsi_high = rsi_line.rolling(p.rsi_len).max()
    stoch_rsi = ((rsi_line - rsi_low) / (rsi_high - rsi_low).replace(0, np.nan) * 100).fillna(50)
    j_rsi_raw = _rsi(j.fillna(50), p.rsi_len)
    j_rsi = (j_rsi_raw if p.rsi_smooth <= 1 else j_rsi_raw.ewm(span=p.rsi_smooth, adjust=False).mean()).fillna(50)
    mode = _slow_mode(p.slow_line_mode)
    if mode == "rsi":
        r_base = rsi_line
    elif mode == "stoch_rsi":
        r_base = stoch_rsi
    elif mode == "j_rsi":
        r_base = j_rsi
    else:
        r_base = k
    r = 50 + (r_base - 50) * float(p.slow_line_scale) + float(p.slow_line_offset)
    if p.slow_line_clamp:
        r = r.clip(lower=0, upper=100)

    basis = out["c"].rolling(p.bb_len).mean()
    dev = out["c"].rolling(p.bb_len).std(ddof=0)
    upper = basis + p.bb_mult * dev
    lower = basis - p.bb_mult * dev
    basis_slope = basis.diff()
    bb_color = np.where((out["c"] >= basis) & (basis_slope >= 0), "blue",
                 np.where((out["c"] <= basis) & (basis_slope <= 0), "purple", "neutral"))

    cross_up = (j.shift(1) <= r.shift(1)) & (j > r)
    cross_down = (j.shift(1) >= r.shift(1)) & (j < r)

    rows: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    pending_buy: dict[str, Any] | None = None
    pending_sell: dict[str, Any] | None = None
    in_long = False
    entry = None
    stop = None

    for i, row in out.iterrows():
        triangle = bool(cross_up.iloc[i] and min(j.iloc[i], r.iloc[i]) <= p.oversold + 15)
        purple_k = bool(cross_down.iloc[i] and max(j.iloc[i], r.iloc[i]) >= p.overbought - 15)
        bull_div = _pivot_divergence(j, out["l"], i, p.divergence_lookback, True) if i > 5 else False
        bear_div = _pivot_divergence(j, out["h"], i, p.divergence_lookback, False) if i > 5 else False

        signal = ""
        note = ""
        if triangle:
            pending_buy = {"idx": i, "high": float(row["h"]), "low": float(row["l"])}
            events.append({"type": "triangle", "idx": i, "price": float(row["l"]), "text": "RJ三角"})
            signal = "triangle"
            note = "RJ金叉关键K"

        if pending_buy and not in_long and i > pending_buy["idx"]:
            age = i - pending_buy["idx"]
            if float(row["c"]) < pending_buy["low"]:
                events.append({"type": "buy_cancel", "idx": i, "price": float(row["c"]), "text": "跌破箱底"})
                pending_buy = None
            elif float(row["c"]) > pending_buy["high"]:
                entry = float(row["c"])
                stop = pending_buy["low"]
                in_long = True
                events.append({"type": "buy_confirm", "idx": i, "price": entry, "text": "确认买入"})
                signal = "buy_confirm"
                note = "收盘突破关键K高点"
                pending_buy = None
            elif age > p.confirm_bars:
                pending_buy = None

        if purple_k:
            pending_sell = {"idx": i, "high": float(row["h"]), "low": float(row["l"])}
            events.append({"type": "purple", "idx": i, "price": float(row["h"]), "text": "紫K"})
            if not signal:
                signal = "purple"
                note = "RJ死叉关键K"

        if pending_sell and in_long and i > pending_sell["idx"]:
            age = i - pending_sell["idx"]
            if float(row["c"]) < pending_sell["low"]:
                events.append({"type": "sell_confirm", "idx": i, "price": float(row["c"]), "text": "确认卖出"})
                signal = "sell_confirm"
                note = "收盘跌破止涨K低点"
                in_long = False
                pending_sell = None
            elif float(row["c"]) > pending_sell["high"]:
                events.append({"type": "sell_fail", "idx": i, "price": float(row["c"]), "text": "止涨失败"})
                pending_sell = None
            elif age > p.confirm_bars:
                pending_sell = None

        if in_long and entry and stop and float(row["c"]) >= entry + (entry - stop):
            note = note or "已超过1R，视频SOP要求至少保护本金"

        rows.append({
            "t": int(row["ot"]),
            "o": float(row["o"]),
            "h": float(row["h"]),
            "l": float(row["l"]),
            "c": float(row["c"]),
            "v": float(row["v"]),
            "j": round(float(j.iloc[i]), 3) if not np.isnan(j.iloc[i]) else None,
            "r": round(float(r.iloc[i]), 3) if not np.isnan(r.iloc[i]) else None,
            "r_base": round(float(r_base.iloc[i]), 3) if not np.isnan(r_base.iloc[i]) else None,
            "bb_mid": round(float(basis.iloc[i]), 8) if not np.isnan(basis.iloc[i]) else None,
            "bb_up": round(float(upper.iloc[i]), 8) if not np.isnan(upper.iloc[i]) else None,
            "bb_dn": round(float(lower.iloc[i]), 8) if not np.isnan(lower.iloc[i]) else None,
            "bb_color": str(bb_color[i]),
            "triangle": triangle,
            "purple": purple_k,
            "bull_div": bool(bull_div),
            "bear_div": bool(bear_div),
            "signal": signal,
            "note": note,
        })

    last = rows[-1] if rows else {}
    return {
        "ok": True,
        "params": p.__dict__,
        "rows": rows,
        "events": events[-80:],
        "summary": {
            "last_price": last.get("c"),
            "bb_color": last.get("bb_color", "neutral"),
            "j": last.get("j"),
            "r": last.get("r"),
            "triangle_count": sum(1 for x in rows if x["triangle"]),
            "purple_count": sum(1 for x in rows if x["purple"]),
            "buy_count": sum(1 for x in events if x["type"] == "buy_confirm"),
            "sell_count": sum(1 for x in events if x["type"] == "sell_confirm"),
        },
    }
