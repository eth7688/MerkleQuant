#!/usr/bin/env python3
"""Show how RJ-only history samples are filtered stage by stage."""
from __future__ import annotations

import json
import sys
from collections import Counter

import numpy as np
import pandas as pd

from trader import SqueezeBreakoutBot, TradeConfig, fetch_klines


def audit(symbol: str, direction: str, config_path: str = "demo_bot_config.json"):
    cfg = TradeConfig.load(config_path)
    bot = SqueezeBreakoutBot(cfg)
    lookback = max(80, int(getattr(cfg, "rj_only_stats_lookback_bars", 220) or 220))
    horizon = max(1, int(getattr(cfg, "rj_only_stats_horizon_bars", 12) or 12))
    target_r = max(0.2, float(getattr(cfg, "rj_only_stats_target_r", 1.0) or 1.0))
    confirm_bars = max(1, int(getattr(cfg, "rj_only_confirm_bars", 6) or 6))
    atr_period = max(2, int(getattr(cfg, "atr_trail_period", 14) or 14))
    atr_mult = max(0.0, float(getattr(cfg, "rj_only_atr_sl_mult", 0.5) or 0.0))
    confirm_atr_buffer = max(0.0, float(getattr(cfg, "rj_only_confirm_atr_buffer", 0.08) or 0.0))
    invalidate_on_opposite = bool(getattr(cfg, "rj_only_invalidate_on_opposite_break", True))
    min_stop_pct = max(0.0, float(getattr(cfg, "rj_only_min_stop_pct", 0.003) or 0.0))
    max_stop_pct = max(min_stop_pct, float(getattr(cfg, "rj_only_max_stop_pct", 0.08) or 0.08))
    min_spread = max(0.0, float(getattr(cfg, "rj_min_jr_spread", 0.0) or 0.0))
    volume_filter_enabled = bool(getattr(cfg, "rj_only_volume_filter", True))
    volume_len = max(1, int(getattr(cfg, "rj_only_volume_len", 20) or 20))
    volume_mult = max(0.0, float(getattr(cfg, "rj_only_volume_mult", 1.1) or 0.0))

    df = fetch_klines(symbol, str(getattr(cfg, "scan_interval", "30m") or "30m").split(",")[0].strip(), lookback, exchange=cfg.exchange)
    if df is None or len(df) < 80:
        raise RuntimeError(f"kline unavailable: {symbol}")
    dfx = df.tail(lookback).reset_index(drop=True)
    lines = bot._compute_rj_lines(dfx)
    if not lines:
        raise RuntimeError("RJ line computation failed")
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
    volume_arr = pd.to_numeric(dfx["v"], errors="coerce").reset_index(drop=True).to_numpy(dtype=float) if "v" in dfx.columns else np.array([])
    volume_ma_arr = (
        pd.Series(volume_arr).shift(1).rolling(volume_len).mean().to_numpy(dtype=float)
        if len(volume_arr) == len(dfx) else np.array([])
    )
    level_up, level_down = bot._rj_original_level_triggers(j_line)
    if direction == "LONG":
        cross_series = ((j_line.shift(1) <= r_line.shift(1)) & (j_line > r_line)).fillna(False)
        level_series = level_up.fillna(False)
    else:
        cross_series = ((j_line.shift(1) >= r_line.shift(1)) & (j_line < r_line)).fillna(False)
        level_series = level_down.fillna(False)
    trigger = (cross_series | level_series).fillna(False).to_numpy()
    level_trigger = level_series.to_numpy()

    counts = Counter()
    samples = []
    examples = []
    n = len(dfx)
    last_confirm = -999999
    for cross_idx in range(1, n - horizon - 1):
        if not bool(trigger[cross_idx]):
            continue
        counts["raw_trigger"] += 1
        if cross_idx <= last_confirm:
            counts["overlap_after_prior_confirm"] += 1
            continue
        sr_state = bot._rj_only_sr_divergence_state(dfx, j_line, direction, cross_idx)
        if not sr_state.get("rj_sr_filter_pass", True):
            counts[f"sr_reject:{sr_state.get('rj_sr_reason', 'unknown')}"] += 1
            continue
        counts["sr_pass"] += 1
        key_high = float(h[cross_idx])
        key_low = float(l[cross_idx])
        if key_high <= 0 or key_low <= 0:
            counts["bad_key_bar"] += 1
            continue
        confirm_idx = None
        confirm_reject = "no_confirm"
        max_confirm = min(n - horizon - 1, cross_idx + confirm_bars)
        for j in range(cross_idx + 1, max_confirm + 1):
            spread = float(j_line.iloc[j] - r_line.iloc[j])
            if direction == "LONG":
                if invalidate_on_opposite and float(c[j]) < key_low:
                    confirm_idx = -1
                    confirm_reject = "opposite_break"
                    break
                if not bool(level_trigger[cross_idx]) and spread < min_spread:
                    confirm_reject = "spread_low"
                    continue
                confirm_level = key_high + float(atr_arr[j]) * confirm_atr_buffer
                if float(c[j]) > confirm_level:
                    confirm_idx = j
                    break
            else:
                if invalidate_on_opposite and float(c[j]) > key_high:
                    confirm_idx = -1
                    confirm_reject = "opposite_break"
                    break
                if not bool(level_trigger[cross_idx]) and -spread < min_spread:
                    confirm_reject = "spread_low"
                    continue
                confirm_level = key_low - float(atr_arr[j]) * confirm_atr_buffer
                if float(c[j]) < confirm_level:
                    confirm_idx = j
                    break
        if confirm_idx is None or confirm_idx < 0:
            counts[f"confirm_reject:{confirm_reject}"] += 1
            continue
        counts["confirm_pass"] += 1
        if volume_filter_enabled:
            if len(volume_arr) != n or len(volume_ma_arr) != n:
                counts["volume_reject:missing"] += 1
                continue
            cur_vol = float(volume_arr[confirm_idx])
            avg_vol = float(volume_ma_arr[confirm_idx])
            if not np.isfinite(cur_vol) or not np.isfinite(avg_vol) or avg_vol <= 0:
                counts["volume_reject:invalid"] += 1
                continue
            if cur_vol / avg_vol < volume_mult:
                counts["volume_reject:ratio_low"] += 1
                continue
        counts["volume_pass"] += 1
        entry = float(c[confirm_idx])
        if entry <= 0:
            counts["entry_invalid"] += 1
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
        if risk <= 0:
            counts["risk_invalid"] += 1
            continue
        if stop_pct > max_stop_pct:
            counts["stop_reject:max_stop_pct"] += 1
            continue
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
        counts["sample"] += 1
        last_confirm = confirm_idx
        if len(examples) < 8:
            examples.append({
                "cross_idx": cross_idx,
                "confirm_idx": int(confirm_idx),
                "kind": "level" if bool(level_trigger[cross_idx]) else "cross",
                "entry": round(entry, 8),
                "stop_pct": round(stop_pct * 100, 4),
                "r": round(float(result_r), 4),
            })
    wins = sum(1 for x in samples if x > 0)
    out = {
        "symbol": symbol,
        "direction": direction,
        "bars": len(dfx),
        "settings": {
            "lookback": lookback,
            "confirm_bars": confirm_bars,
            "horizon": horizon,
            "volume_filter": volume_filter_enabled,
            "volume_mult": volume_mult,
            "sr_filter": bool(getattr(cfg, "rj_only_sr_filter", True)),
            "require_divergence": bool(getattr(cfg, "rj_only_require_divergence", True)),
        },
        "counts": dict(counts),
        "official_stats": bot._rj_only_history_stats(dfx, direction),
        "calc_samples": len(samples),
        "wins": wins,
        "losses": len(samples) - wins,
        "win_rate": round(wins / len(samples) * 100, 2) if samples else 0,
        "avg_r": round(sum(samples) / len(samples), 4) if samples else 0,
        "examples": examples,
    }
    return out


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "WLDUSDT"
    directions = [sys.argv[2]] if len(sys.argv) > 2 else ["LONG", "SHORT"]
    for direction in directions:
        print(json.dumps(audit(symbol, direction.upper()), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
