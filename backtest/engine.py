from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from backtest.execution import SimBroker
from backtest.metrics import summarize_positions
from strategy_core import ExitRules, PositionState, StrategySnapshot, advance_position, evaluate_rj_entry


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
    ) -> ReplayResult:
        frame30 = candles_30m.sort_values("ot").reset_index(drop=True)
        frame1 = candles_1m.sort_values("ot").reset_index(drop=True)
        if len(frame30) < self.warmup_bars or frame1.empty:
            return ReplayResult([], summarize_positions([]), self.initial_equity, set())

        broker = SimBroker(self.initial_equity, self.fee_rate, self.slippage_bps)
        events: list[dict] = []
        used_keys: set[str] = set()
        decision_rows = [
            (int(frame30["ot"].iloc[index]) + 1_800_000, index)
            for index in range(self.warmup_bars - 1, len(frame30))
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
                )
                decision = evaluate_rj_entry(self.bot, snapshot)
                if not decision.allowed:
                    events.append({
                        "type": "signal_reject", "symbol": symbol, "time": decision_time,
                        "reason": decision.reason, "signal_key": decision.signal_key,
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
