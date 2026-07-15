"""Shared deterministic contracts for live RJ decisions and historical replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from btc_stage import evaluate_btc_gate


RULE_VERSION = "rj_strategy_core_v1"


def _interval_ms(interval: str) -> int:
    value = str(interval).strip().lower()
    unit = value[-1:]
    amount = int(value[:-1])
    scale = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(unit)
    if scale is None:
        raise ValueError(f"unsupported_interval:{interval}")
    return amount * scale


@dataclass(frozen=True)
class StrategySnapshot:
    symbol: str
    interval: str
    decision_time: int
    candles_30m: pd.DataFrame
    btc_stage: dict[str, Any]


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: str
    direction: str = ""
    reference_entry: float = 0.0
    stop: float = 0.0
    signal_key: str = ""
    key_time: int | None = None
    confirm_time: int | None = None
    trigger_source: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    rule_version: str = RULE_VERSION


@dataclass(frozen=True)
class ExitDecision:
    action: str
    price: float
    quantity: float
    reason: str
    time: int


@dataclass(frozen=True)
class ExitRules:
    early_protect_r: float = 0.8
    early_lock_r: float = 0.0
    tier1_r: float = 1.2
    tier1_lock_r: float = 0.5
    tier2_r: float = 1.7
    tier2_fraction: float = 0.5


@dataclass
class PositionState:
    symbol: str
    direction: str
    entry: float
    initial_stop: float
    quantity: float
    current_stop: float | None = None
    remaining_qty: float | None = None
    mfe_r: float = 0.0
    mae_r: float = 0.0
    early_protected: bool = False
    tier1_done: bool = False
    tier2_done: bool = False

    def __post_init__(self):
        if self.current_stop is None:
            self.current_stop = float(self.initial_stop)
        if self.remaining_qty is None:
            self.remaining_qty = float(self.quantity)


def _signal_coin_reversal_pass(signal: dict[str, Any], direction: str) -> bool:
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


def evaluate_rj_entry(bot: Any, snapshot: StrategySnapshot) -> EntryDecision:
    frame = snapshot.candles_30m
    if frame is None or frame.empty:
        return EntryDecision(False, "no_candles")
    last_open = int(float(frame["ot"].iloc[-1]))
    if last_open + _interval_ms(snapshot.interval) > int(snapshot.decision_time):
        raise ValueError("strategy_snapshot_lookahead")

    signal = bot._rj_only_signal_from_df(snapshot.symbol, snapshot.interval, frame.copy())
    if not signal:
        return EntryDecision(False, "no_rj_signal")
    direction = str(signal.get("direction", "")).upper()
    cfg = getattr(bot, "cfg", None)
    if cfg is not None and bool(getattr(cfg, "rj_only_stats_enabled", True)):
        if not bool(signal.get("rj_only_stats_pass", False)):
            return EntryDecision(
                False,
                str(signal.get("rj_only_stats_reason", "rj_only_stats_failed") or "rj_only_stats_failed"),
                direction=direction,
                signal_key=str(signal.get("signal_key", "") or ""),
                evidence=dict(signal),
            )
    if cfg is not None and float(signal.get("score", 0) or 0) < float(getattr(cfg, "min_score", 0) or 0):
        return EntryDecision(
            False,
            "score_below_min",
            direction=direction,
            signal_key=str(signal.get("signal_key", "") or ""),
            evidence=dict(signal),
        )
    reversal = _signal_coin_reversal_pass(signal, direction)
    allowed, reason = evaluate_btc_gate(direction, snapshot.btc_stage or {}, reversal)
    evidence = dict(signal)
    evidence["btc_coin_reversal_pass"] = reversal
    evidence["btc_gate_reason"] = reason
    return EntryDecision(
        allowed=bool(allowed),
        reason="pass" if allowed else reason,
        direction=direction,
        reference_entry=float(signal.get("price", 0) or 0),
        stop=float(signal.get("rj_only_stop_price", 0) or 0),
        signal_key=str(signal.get("signal_key", "") or ""),
        key_time=signal.get("rj_only_key_time"),
        confirm_time=signal.get("rj_only_confirm_time"),
        trigger_source=str(signal.get("rj_trigger_source", "") or ""),
        evidence=evidence,
    )


def evaluate_predicta_entry(bot: Any, snapshot: StrategySnapshot) -> EntryDecision:
    frame = snapshot.candles_30m
    if frame is None or frame.empty:
        return EntryDecision(False, "no_candles")
    last_open = int(float(frame["ot"].iloc[-1]))
    if last_open + _interval_ms(snapshot.interval) > int(snapshot.decision_time):
        raise ValueError("strategy_snapshot_lookahead")
    decision_frame = frame.tail(160).copy().reset_index(drop=True)
    signal = bot._predicta_signal_from_df(snapshot.symbol, snapshot.interval, decision_frame)
    if not signal:
        return EntryDecision(False, "no_predicta_signal")
    direction = str(signal.get("direction", "")).upper()
    allowed, reason = evaluate_btc_gate(direction, snapshot.btc_stage or {}, False)
    evidence = dict(signal)
    evidence["btc_gate_reason"] = reason
    return EntryDecision(
        allowed=bool(allowed),
        reason="pass" if allowed else reason,
        direction=direction,
        reference_entry=float(signal.get("price", 0.0) or 0.0),
        stop=float(signal.get("predicta_stop_price", 0.0) or 0.0),
        signal_key=str(signal.get("signal_key", "") or ""),
        key_time=signal.get("predicta_key_time"),
        confirm_time=signal.get("predicta_confirm_time") or signal.get("predicta_key_time"),
        trigger_source=str(signal.get("predicta_entry_path", "") or ""),
        evidence=evidence,
        rule_version="predicta_ewo_v1",
    )


def evaluate_entry(bot: Any, snapshot: StrategySnapshot) -> EntryDecision:
    source = (
        bot._entry_signal_source()
        if callable(getattr(bot, "_entry_signal_source", None))
        else str(getattr(getattr(bot, "cfg", None), "entry_signal_source", "rj_only"))
    )
    if source == "predicta_ewo":
        return evaluate_predicta_entry(bot, snapshot)
    return evaluate_rj_entry(bot, snapshot)


def advance_position(
    position: PositionState,
    bar: dict[str, Any],
    rules: ExitRules,
) -> tuple[PositionState, list[ExitDecision]]:
    """Advance one intrabar step. Existing stop is evaluated adverse-first."""
    if not position.remaining_qty:
        return position, []
    high = float(bar["h"])
    low = float(bar["l"])
    timestamp = int(bar["ot"])
    stop = float(position.current_stop)
    stopped = low <= stop if position.direction == "LONG" else high >= stop
    if stopped:
        qty = float(position.remaining_qty)
        position.remaining_qty = 0.0
        return position, [ExitDecision("full_exit", stop, qty, "stop", timestamp)]

    risk = abs(float(position.entry) - float(position.initial_stop))
    if risk <= 0:
        raise ValueError("position_invalid_risk")
    favorable = (
        (high - position.entry) / risk
        if position.direction == "LONG"
        else (position.entry - low) / risk
    )
    adverse = (
        (position.entry - low) / risk
        if position.direction == "LONG"
        else (high - position.entry) / risk
    )
    position.mfe_r = max(position.mfe_r, favorable)
    position.mae_r = max(position.mae_r, adverse)
    sign = 1.0 if position.direction == "LONG" else -1.0
    events: list[ExitDecision] = []

    if not position.early_protected and position.mfe_r >= rules.early_protect_r:
        candidate = position.entry + sign * rules.early_lock_r * risk
        position.current_stop = max(position.current_stop, candidate) if sign > 0 else min(position.current_stop, candidate)
        position.early_protected = True
        events.append(ExitDecision("move_stop", position.current_stop, 0.0, "early_protect", timestamp))

    if not position.tier1_done and position.mfe_r >= rules.tier1_r:
        candidate = position.entry + sign * rules.tier1_lock_r * risk
        position.current_stop = max(position.current_stop, candidate) if sign > 0 else min(position.current_stop, candidate)
        position.tier1_done = True
        events.append(ExitDecision("move_stop", position.current_stop, 0.0, "tier1_defense", timestamp))

    if not position.tier2_done and position.mfe_r >= rules.tier2_r:
        quantity = min(position.remaining_qty, position.quantity * rules.tier2_fraction)
        position.remaining_qty -= quantity
        position.tier2_done = True
        fill = position.entry + sign * rules.tier2_r * risk
        events.append(ExitDecision("partial_exit", fill, quantity, "tier2_partial", timestamp))
    return position, events
