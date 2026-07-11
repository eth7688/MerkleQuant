from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    price: float
    quantity: float
    fee: float
    time: int
    reason: str
    position_id: str
    gross_pnl: float = 0.0
    net_pnl: float = 0.0


class SimBroker:
    def __init__(self, initial_equity: float, fee_rate: float, slippage_bps: float):
        self.initial_equity = float(initial_equity)
        self.equity = float(initial_equity)
        self.fee_rate = float(fee_rate)
        self.slippage = float(slippage_bps) / 10_000.0
        self.positions: dict[str, dict] = {}
        self.fills: list[Fill] = []

    def _fill_price(self, reference: float, buy: bool) -> float:
        return float(reference) * (1.0 + self.slippage if buy else 1.0 - self.slippage)

    def open_market(self, symbol: str, direction: str, reference: float, quantity: float, time: int) -> Fill:
        if symbol in self.positions and self.positions[symbol]["quantity"] > 0:
            raise ValueError("position_already_open")
        buy = direction == "LONG"
        price = self._fill_price(reference, buy)
        fee = abs(price * quantity) * self.fee_rate
        position_id = uuid4().hex
        self.positions[symbol] = {
            "position_id": position_id, "direction": direction, "entry": price,
            "quantity": float(quantity),
        }
        self.equity -= fee
        fill = Fill(symbol, "BUY" if buy else "SELL", price, quantity, fee, time, "entry", position_id)
        self.fills.append(fill)
        return fill

    def close_market(self, symbol: str, quantity: float, reference: float, time: int, reason: str) -> Fill:
        position = self.positions.get(symbol)
        if not position or position["quantity"] <= 0:
            raise ValueError("position_not_open")
        quantity = min(float(quantity), float(position["quantity"]))
        direction = position["direction"]
        buy = direction == "SHORT"
        price = self._fill_price(reference, buy)
        gross = (
            (price - position["entry"]) * quantity
            if direction == "LONG"
            else (position["entry"] - price) * quantity
        )
        fee = abs(price * quantity) * self.fee_rate
        net = gross - fee
        self.equity += net
        position["quantity"] -= quantity
        fill = Fill(
            symbol, "BUY" if buy else "SELL", price, quantity, fee, time, reason,
            position["position_id"], gross, net,
        )
        self.fills.append(fill)
        return fill

    def position_quantity(self, symbol: str) -> float:
        return float(self.positions.get(symbol, {}).get("quantity", 0.0))
