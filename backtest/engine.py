from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from backtest.execution import SimBroker
from backtest.metrics import summarize_positions
from strategy_core import ExitRules, PositionState, StrategySnapshot, advance_position, evaluate_entry


@dataclass
class ReplayResult:
    events: list[dict]
    metrics: dict
    final_equity: float
    used_signal_keys: set[str]


class ReplayEngine:
    def __init__(
        self,
        bot,
        initial_equity: float,
        risk_usdt: float,
        fee_rate: float,
        slippage_bps: float,
        exit_rules: ExitRules,
        warmup_bars: int = 60,
    ):
        self.bot = bot
        self.initial_equity = float(initial_equity)
        self.risk_usdt = float(risk_usdt)
        self.fee_rate = float(fee_rate)
        self.slippage_bps = float(slippage_bps)
        self.exit_rules = exit_rules
        self.warmup_bars = max(1, int(warmup_bars))

    def run_symbol(
        self,
        symbol: str,
        candles_30m: pd.DataFrame,
        candles_1m: pd.DataFrame,
        btc_stage_provider: Callable[[int], dict],
        entry_start_ms: int | None = None,
        candles_1d: pd.DataFrame | None = None,
    ) -> ReplayResult:
        frame30 = candles_30m.sort_values("ot").reset_index(drop=True)
        frame1 = candles_1m.sort_values("ot").reset_index(drop=True)
        frame_daily = (
            candles_1d.sort_values("ot").reset_index(drop=True)
            if isinstance(candles_1d, pd.DataFrame) else pd.DataFrame()
        )
        if len(frame30) < self.warmup_bars or frame1.empty:
            return ReplayResult([], summarize_positions([]), self.initial_equity, set())

        broker = SimBroker(self.initial_equity, self.fee_rate, self.slippage_bps)
        events: list[dict] = []
        used_keys: set[str] = set()
        decision_rows = [
            (int(frame30["ot"].iloc[index]) + 1_800_000, index)
            for index in range(self.warmup_bars - 1, len(frame30))
            if entry_start_ms is None or int(frame30["ot"].iloc[index]) + 1_800_000 >= int(entry_start_ms)
        ]
        decision_cursor = 0
        state: PositionState | None = None
        position_id = ""

        def apply_exit(bar: dict) -> None:
            nonlocal state, position_id
            if state is None:
                return
            state, intents = advance_position(state, bar, self.exit_rules)
            for intent in intents:
                if intent.action == "move_stop":
                    events.append({
                        "type": "stop_move", "position_id": position_id,
                        "time": intent.time, "price": intent.price, "reason": intent.reason,
                    })
                    continue
                fill = broker.close_market(symbol, intent.quantity, intent.price, intent.time, intent.reason)
                events.append({
                    "type": "exit_fill", "position_id": position_id, "symbol": symbol,
                    "time": fill.time, "price": fill.price, "quantity": fill.quantity,
                    "gross_pnl": fill.gross_pnl, "net_pnl": fill.net_pnl,
                    "fee": fill.fee, "reason": intent.reason,
                    "mfe_r": state.mfe_r, "mae_r": state.mae_r,
                    "equity": broker.equity,
                })
                if intent.action == "full_exit" or broker.position_quantity(symbol) <= 0:
                    state = None
                    position_id = ""

        for _, minute in frame1.iterrows():
            bar = {key: minute[key] for key in ("ot", "o", "h", "l", "c", "v")}
            apply_exit(bar)
            minute_time = int(minute["ot"])
            while decision_cursor < len(decision_rows) and decision_rows[decision_cursor][0] <= minute_time:
                decision_time, index = decision_rows[decision_cursor]
                decision_cursor += 1
                window = frame30.iloc[: index + 1].copy().reset_index(drop=True)
                snapshot = StrategySnapshot(
                    symbol=symbol,
                    interval="30m",
                    decision_time=decision_time,
                    candles_30m=window,
                    btc_stage=btc_stage_provider(decision_time),
                    candles_1d=frame_daily,
                )
                decision = evaluate_entry(self.bot, snapshot)
                if not decision.allowed:
                    events.append({
                        "type": "signal_reject", "symbol": symbol, "time": decision_time,
                        "reason": decision.reason, "signal_key": decision.signal_key,
                        "daily_pattern": decision.evidence.get("daily_pattern", {}),
                    })
                    continue
                if not decision.signal_key or decision.signal_key in used_keys or state is not None:
                    continue
                distance = abs(decision.reference_entry - decision.stop)
                if distance <= 0:
                    events.append({"type": "signal_reject", "symbol": symbol, "time": decision_time, "reason": "invalid_risk"})
                    continue
                quantity = self.risk_usdt / distance
                fill = broker.open_market(symbol, decision.direction, float(minute["o"]), quantity, minute_time)
                position_id = fill.position_id
                state = PositionState(symbol, decision.direction, fill.price, decision.stop, quantity)
                used_keys.add(decision.signal_key)
                events.append({
                    "type": "entry_fill", "position_id": position_id, "symbol": symbol,
                    "direction": decision.direction, "time": fill.time, "price": fill.price,
                    "quantity": fill.quantity, "fee": fill.fee, "risk_usdt": self.risk_usdt,
                    "signal_key": decision.signal_key, "trigger_source": decision.trigger_source,
                    "initial_stop": decision.stop,
                    "daily_pattern": decision.evidence.get("daily_pattern", {}),
                    "equity": broker.equity,
                })
                apply_exit(bar)

        if state is not None and broker.position_quantity(symbol) > 0:
            last = frame1.iloc[-1]
            quantity = broker.position_quantity(symbol)
            fill = broker.close_market(symbol, quantity, float(last["c"]), int(last["ot"]), "end_of_data")
            events.append({
                "type": "exit_fill", "position_id": position_id, "symbol": symbol,
                "time": fill.time, "price": fill.price, "quantity": fill.quantity,
                "gross_pnl": fill.gross_pnl, "net_pnl": fill.net_pnl, "fee": fill.fee,
                "reason": "end_of_data", "mfe_r": state.mfe_r, "mae_r": state.mae_r,
                "equity": broker.equity,
            })
        return ReplayResult(events, summarize_positions(events), broker.equity, used_keys)


class PortfolioReplayEngine:
    """Chronological multi-symbol replay with shared account position slots."""

    def __init__(
        self,
        bot,
        initial_equity: float,
        risk_usdt: float,
        fee_rate: float,
        slippage_bps: float,
        exit_rules: ExitRules,
        max_positions: int,
        warmup_bars: int = 60,
    ):
        self.bot = bot
        self.initial_equity = float(initial_equity)
        self.risk_usdt = float(risk_usdt)
        self.fee_rate = float(fee_rate)
        self.slippage_bps = float(slippage_bps)
        self.exit_rules = exit_rules
        self.max_positions = max(1, int(max_positions))
        self.warmup_bars = max(1, int(warmup_bars))

    def run(
        self,
        candles_30m: dict[str, pd.DataFrame],
        candles_1m: dict[str, pd.DataFrame],
        btc_stage_provider: Callable[[int], dict],
        entry_start_ms: int,
        precomputed: dict[str, dict[int, object]] | None = None,
        candles_1d: dict[str, pd.DataFrame] | None = None,
    ) -> ReplayResult:
        frames30 = {key: value.sort_values("ot").reset_index(drop=True) for key, value in candles_30m.items()}
        frames1 = {key: value.sort_values("ot").reset_index(drop=True) for key, value in candles_1m.items()}
        frames_daily = {
            key: value.sort_values("ot").reset_index(drop=True)
            for key, value in (candles_1d or {}).items()
        }
        decision_times = sorted({
            int(row) + 1_800_000
            for frame in frames30.values()
            for row in frame["ot"].tolist()
            if int(row) + 1_800_000 >= int(entry_start_ms)
        })
        broker = SimBroker(self.initial_equity, self.fee_rate, self.slippage_bps)
        states: dict[str, PositionState] = {}
        position_ids: dict[str, str] = {}
        events: list[dict] = []
        used_keys: set[str] = set()
        minute_cursors = {
            symbol: int(frame["ot"].searchsorted(entry_start_ms, side="left"))
            for symbol, frame in frames1.items()
        }
        close_times = {
            symbol: frame["ot"].to_numpy() + 1_800_000
            for symbol, frame in frames30.items()
        }
        eligible_counts: dict[str, set[int]] = {}
        if precomputed is None and hasattr(self.bot, "_compute_rj_lines") and hasattr(self.bot, "_rj_original_level_triggers"):
            confirm_bars = max(1, int(getattr(self.bot.cfg, "rj_only_confirm_bars", 6) or 6))
            for symbol, frame in frames30.items():
                lines = self.bot._compute_rj_lines(frame)
                if not lines:
                    continue
                j_line = lines["j"]
                r_line = lines["r"]
                cross = ((j_line.shift(1) <= r_line.shift(1)) & (j_line > r_line)) | (
                    (j_line.shift(1) >= r_line.shift(1)) & (j_line < r_line)
                )
                level_up, level_down = self.bot._rj_original_level_triggers(j_line)
                trigger = (cross | level_up | level_down).fillna(False).to_numpy()
                eligible_counts[symbol] = {
                    confirm_index + 1
                    for confirm_index in range(1, len(frame))
                    if trigger[max(1, confirm_index - confirm_bars):confirm_index].any()
                }

        def apply_bar(symbol: str, row) -> None:
            if symbol not in states:
                return
            state, intents = advance_position(states[symbol], row, self.exit_rules)
            states[symbol] = state
            position_id = position_ids[symbol]
            for intent in intents:
                if intent.action == "move_stop":
                    events.append({
                        "type": "stop_move", "position_id": position_id, "symbol": symbol,
                        "time": intent.time, "price": intent.price, "reason": intent.reason,
                    })
                    continue
                fill = broker.close_market(symbol, intent.quantity, intent.price, intent.time, intent.reason)
                events.append({
                    "type": "exit_fill", "position_id": position_id, "symbol": symbol,
                    "time": fill.time, "price": fill.price, "quantity": fill.quantity,
                    "gross_pnl": fill.gross_pnl, "net_pnl": fill.net_pnl, "fee": fill.fee,
                    "reason": intent.reason, "mfe_r": state.mfe_r, "mae_r": state.mae_r,
                    "equity": broker.equity,
                })
                if intent.action == "full_exit" or broker.position_quantity(symbol) <= 0:
                    states.pop(symbol, None)
                    position_ids.pop(symbol, None)

        processed_until = int(entry_start_ms)
        for decision_time in decision_times:
            for symbol, frame in frames1.items():
                start_index = minute_cursors[symbol]
                end_index = int(frame["ot"].searchsorted(decision_time, side="left"))
                for _, row in frame.iloc[start_index:end_index].iterrows():
                    apply_bar(symbol, row)
                minute_cursors[symbol] = end_index
            processed_until = decision_time

            stage = btc_stage_provider(decision_time)
            candidates = []
            for symbol, frame in frames30.items():
                closed_count = int(close_times[symbol].searchsorted(decision_time, side="right"))
                closed = frame.iloc[:closed_count]
                if len(closed) < self.warmup_bars or symbol in states:
                    continue
                if symbol in eligible_counts and closed_count not in eligible_counts[symbol]:
                    continue
                if precomputed is not None:
                    decision = precomputed.get(symbol, {}).get(decision_time)
                    if decision is None:
                        continue
                else:
                    decision = evaluate_entry(
                        self.bot,
                        StrategySnapshot(
                            symbol, "30m", decision_time, closed, stage,
                            candles_1d=frames_daily.get(symbol),
                        ),
                    )
                if not decision.allowed:
                    if decision.reason not in ("no_rj_signal", "no_predicta_signal"):
                        events.append({
                            "type": "signal_reject", "symbol": symbol, "time": decision_time,
                            "reason": decision.reason, "signal_key": decision.signal_key,
                            "daily_pattern": decision.evidence.get("daily_pattern", {}),
                        })
                    continue
                if decision.signal_key and decision.signal_key not in used_keys:
                    candidates.append(decision)
            candidates.sort(key=lambda item: float(item.evidence.get("score", 0) or 0), reverse=True)

            for decision in candidates:
                symbol = str(decision.evidence.get("symbol", "") or "")
                if not symbol:
                    signal_key = decision.signal_key.split("|")
                    symbol = signal_key[1] if len(signal_key) > 1 else ""
                if not symbol or symbol in states:
                    continue
                if len(states) >= self.max_positions:
                    used_keys.add(decision.signal_key)
                    events.append({
                        "type": "signal_reject", "symbol": symbol, "time": decision_time,
                        "reason": "capacity_full", "signal_key": decision.signal_key,
                    })
                    continue
                minute_frame = frames1.get(symbol)
                if minute_frame is None:
                    continue
                fill_rows = minute_frame[minute_frame["ot"] >= decision_time]
                if fill_rows.empty:
                    continue
                minute = fill_rows.iloc[0]
                distance = abs(decision.reference_entry - decision.stop)
                if distance <= 0:
                    continue
                quantity = self.risk_usdt / distance
                fill = broker.open_market(symbol, decision.direction, float(minute["o"]), quantity, int(minute["ot"]))
                states[symbol] = PositionState(symbol, decision.direction, fill.price, decision.stop, quantity)
                position_ids[symbol] = fill.position_id
                used_keys.add(decision.signal_key)
                events.append({
                    "type": "entry_fill", "position_id": fill.position_id, "symbol": symbol,
                    "direction": decision.direction, "time": fill.time, "price": fill.price,
                    "quantity": fill.quantity, "fee": fill.fee, "risk_usdt": self.risk_usdt,
                    "signal_key": decision.signal_key, "trigger_source": decision.trigger_source,
                    "initial_stop": decision.stop,
                    "daily_pattern": decision.evidence.get("daily_pattern", {}),
                    "equity": broker.equity,
                })

        final_time = max((int(frame["ot"].iloc[-1]) for frame in frames1.values() if not frame.empty), default=processed_until)
        for symbol, frame in frames1.items():
            for _, row in frame.iloc[minute_cursors[symbol]:].iterrows():
                apply_bar(symbol, row)
        for symbol in list(states):
            frame = frames1[symbol]
            if frame.empty:
                continue
            state = states[symbol]
            fill = broker.close_market(symbol, broker.position_quantity(symbol), float(frame["c"].iloc[-1]), final_time, "end_of_data")
            events.append({
                "type": "exit_fill", "position_id": position_ids[symbol], "symbol": symbol,
                "time": fill.time, "price": fill.price, "quantity": fill.quantity,
                "gross_pnl": fill.gross_pnl, "net_pnl": fill.net_pnl, "fee": fill.fee,
                "reason": "end_of_data", "mfe_r": state.mfe_r, "mae_r": state.mae_r,
                "equity": broker.equity,
            })
        return ReplayResult(events, summarize_positions(events), broker.equity, used_keys)
