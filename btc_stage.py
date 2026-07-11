"""Deterministic BTC market-stage classification from closed candles."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


RULE_VERSION = "btc_stage_v1"
MIN_BARS = 80


def _series(df: pd.DataFrame, *names: str) -> pd.Series:
    for name in names:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce").astype(float)
    raise ValueError(f"btc_stage_missing_column:{names[0]}")


def _rma(values: pd.Series, period: int) -> pd.Series:
    return values.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _atr_adx(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series]:
    high = _series(df, "h", "high")
    low = _series(df, "l", "low")
    close = _series(df, "c", "close")
    previous = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - previous).abs(), (low - previous).abs()],
        axis=1,
    ).max(axis=1)
    atr = _rma(tr, period)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    plus_di = 100.0 * _rma(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100.0 * _rma(minus_dm, period) / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return atr, _rma(dx, period).fillna(0.0)


def _last_breakout_age(close: pd.Series, direction: str, lookback: int = 20) -> int | None:
    previous_high = close.shift(1).rolling(lookback).max()
    previous_low = close.shift(1).rolling(lookback).min()
    hits = close > previous_high if direction == "bull" else close < previous_low
    locations = np.flatnonzero(hits.fillna(False).to_numpy())
    if not len(locations):
        return None
    return int(len(close) - 1 - locations[-1])


def build_interval_snapshot(df: pd.DataFrame) -> dict[str, Any]:
    """Build a JSON-safe indicator snapshot from already closed OHLCV rows."""
    if df is None or len(df) < MIN_BARS:
        raise ValueError("btc_stage_insufficient_bars")
    frame = df.reset_index(drop=True).copy()
    close = _series(frame, "c", "close")
    volume = _series(frame, "v", "volume")
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema60 = close.ewm(span=60, adjust=False).mean()
    atr, adx = _atr_adx(frame)
    atr_now = float(atr.iloc[-1])
    if not np.isfinite(atr_now) or atr_now <= 0:
        raise ValueError("btc_stage_invalid_atr")

    ema20_slope = float((ema20.iloc[-1] - ema20.iloc[-6]) / atr_now / 5.0)
    ema60_slope = float((ema60.iloc[-1] - ema60.iloc[-6]) / atr_now / 5.0)
    spread_now = float((ema20.iloc[-1] - ema60.iloc[-1]) / atr_now)
    spread_then = float((ema20.iloc[-6] - ema60.iloc[-6]) / atr_now)
    direction = "range"
    if spread_now > 0.15 and ema20_slope > 0:
        direction = "bull"
    elif spread_now < -0.15 and ema20_slope < 0:
        direction = "bear"

    distance = float((close.iloc[-1] - ema20.iloc[-1]) / atr_now)
    volume_base = float(volume.iloc[-21:-1].mean())
    volume_ratio = float(volume.iloc[-1] / volume_base) if volume_base > 0 else 0.0
    momentum_now = float((close.iloc[-1] - close.iloc[-6]) / atr_now)
    momentum_before = float((close.iloc[-6] - close.iloc[-11]) / atr_now)
    momentum_change = momentum_now - momentum_before
    adx_now = float(adx.iloc[-1])
    adx_change = float(adx.iloc[-1] - adx.iloc[-6])
    breakout_age = _last_breakout_age(close, direction) if direction in {"bull", "bear"} else None

    exhaustion: list[str] = []
    if abs(distance) >= 2.5:
        exhaustion.append("overextended")
    aligned_momentum = momentum_now > 0 if direction == "bull" else momentum_now < 0
    weakening_momentum = momentum_change < 0 if direction == "bull" else momentum_change > 0
    if direction in {"bull", "bear"} and aligned_momentum and weakening_momentum:
        exhaustion.append("momentum_divergence")
    if direction in {"bull", "bear"} and aligned_momentum and volume_ratio < 0.75:
        exhaustion.append("volume_contraction")
    if adx_now >= 25 and adx_change < -4:
        exhaustion.append("trend_strength_fading")

    closed_col = "ct" if "ct" in frame.columns else "ot"
    closed_at = int(float(frame[closed_col].iloc[-1]))
    return {
        "direction": direction,
        "ema20": round(float(ema20.iloc[-1]), 8),
        "ema60": round(float(ema60.iloc[-1]), 8),
        "ema20_slope_atr": round(ema20_slope, 6),
        "ema60_slope_atr": round(ema60_slope, 6),
        "ema_spread_atr": round(spread_now, 6),
        "ema_spread_change": round(abs(spread_now) - abs(spread_then), 6),
        "adx": round(adx_now, 4),
        "adx_change": round(adx_change, 4),
        "distance_ema20_atr": round(distance, 6),
        "atr": round(atr_now, 8),
        "volume_ratio": round(volume_ratio, 6),
        "breakout_age": breakout_age,
        "momentum_change": round(momentum_change, 6),
        "exhaustion_flags": exhaustion,
        "closed_at": closed_at,
    }


def classify_stage_snapshots(one_hour: dict[str, Any], four_hour: dict[str, Any]) -> dict[str, Any]:
    """Classify stage from two precomputed closed-candle snapshots."""
    if not one_hour or not four_hour:
        return _stage_result("unknown", "unknown", False, one_hour, four_hour, ["missing_snapshot"])
    d1 = str(one_hour.get("direction", "unknown"))
    d4 = str(four_hour.get("direction", "unknown"))
    if d1 not in {"bull", "bear", "range"} or d4 not in {"bull", "bear", "range"}:
        return _stage_result("unknown", "unknown", False, one_hour, four_hour, ["invalid_direction"])
    if d1 != d4 or d1 == "range":
        return _stage_result("range", "range", False, one_hour, four_hour, ["timeframes_not_aligned"])

    direction = d1
    prefix = "bull" if direction == "bull" else "bear"
    snapshots = (one_hour, four_hour)
    exhaustion = sorted({flag for row in snapshots for flag in row.get("exhaustion_flags", [])})
    max_distance = max(abs(float(row.get("distance_ema20_atr", 0))) for row in snapshots)
    weakening = all(
        float(row.get("adx_change", 0)) < 0 and float(row.get("ema_spread_change", 0)) <= 0
        for row in snapshots
    )
    if exhaustion or max_distance >= 2.5:
        return _stage_result(f"late_{prefix}", direction, False, one_hour, four_hour, exhaustion or ["overextended"])
    if weakening:
        return _stage_result(f"{prefix}_decay", direction, False, one_hour, four_hour, ["trend_weakening"])

    momentum_aligned = all(
        float(row.get("momentum_change", 0)) > 0
        if direction == "bull"
        else float(row.get("momentum_change", 0)) < 0
        for row in snapshots
    )
    expanding = all(
        float(row.get("adx", 0)) >= 25
        and float(row.get("adx_change", 0)) >= 0
        and float(row.get("ema_spread_change", 0)) > 0
        and float(row.get("volume_ratio", 0)) >= 0.9
        for row in snapshots
    )
    ages = [row.get("breakout_age") for row in snapshots]
    valid_ages = [int(age) for age in ages if age is not None]
    early = len(valid_ages) == 2 and max(valid_ages) <= 4
    stage = f"early_{prefix}" if early else f"mid_{prefix}"
    extreme = bool(expanding and momentum_aligned and max_distance <= 2.2)
    evidence = ["aligned_timeframes"]
    if expanding:
        evidence.append("trend_expanding")
    if momentum_aligned:
        evidence.append("momentum_expanding")
    return _stage_result(stage, direction, extreme, one_hour, four_hour, evidence)


def _stage_result(
    stage: str,
    direction: str,
    extreme: bool,
    one_hour: dict[str, Any],
    four_hour: dict[str, Any],
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "stage": stage,
        "direction": direction,
        "extreme_veto": bool(extreme),
        "evidence": evidence,
        "exhaustion_flags": sorted({
            flag
            for row in (one_hour or {}, four_hour or {})
            for flag in row.get("exhaustion_flags", [])
        }),
        "closed_1h_at": (one_hour or {}).get("closed_at"),
        "closed_4h_at": (four_hour or {}).get("closed_at"),
        "one_hour": one_hour or {},
        "four_hour": four_hour or {},
        "rule_version": RULE_VERSION,
    }


def classify_btc_stage(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> dict[str, Any]:
    try:
        return classify_stage_snapshots(
            build_interval_snapshot(df_1h),
            build_interval_snapshot(df_4h),
        )
    except (ValueError, KeyError, TypeError) as exc:
        return _stage_result("unknown", "unknown", False, {}, {}, [str(exc)])


def evaluate_btc_gate(
    direction: str,
    stage: dict[str, Any],
    coin_reversal_pass: bool = False,
) -> tuple[bool, str]:
    """Apply extreme veto and ordinary early/mid opposite-signal confirmation."""
    name = str((stage or {}).get("stage", "unknown"))
    market_direction = str((stage or {}).get("direction", "unknown"))
    extreme = bool((stage or {}).get("extreme_veto", False))
    if name == "unknown" or market_direction == "unknown":
        return True, "btc_unknown_pass"
    if extreme and market_direction == "bull" and direction == "SHORT":
        return False, "btc_extreme_bull_blocks_short"
    if extreme and market_direction == "bear" and direction == "LONG":
        return False, "btc_extreme_bear_blocks_long"
    ordinary_opposite = (
        name in {"early_bull", "mid_bull"} and market_direction == "bull" and direction == "SHORT"
    ) or (
        name in {"early_bear", "mid_bear"} and market_direction == "bear" and direction == "LONG"
    )
    if ordinary_opposite and not coin_reversal_pass:
        side = "bull" if market_direction == "bull" else "bear"
        return False, f"btc_{side}_opposite_requires_coin_reversal"
    if ordinary_opposite:
        return True, "btc_opposite_coin_reversal_pass"
    return True, "pass"
