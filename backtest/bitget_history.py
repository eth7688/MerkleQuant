from __future__ import annotations

import time

import pandas as pd
import requests


GRANULARITY = {"1m": "1m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}


class BitgetHistorySource:
    """Credential-free Bitget USDT futures history adapter."""

    endpoint = "https://api.bitget.com/api/v2/mix/market/history-candles"

    def __init__(self, session=None, request_pause: float = 0.06):
        self.session = session or requests.Session()
        self.request_pause = max(0.0, float(request_pause))

    def fetch_candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        granularity = GRANULARITY.get(interval)
        if not granularity:
            raise ValueError(f"unsupported_interval:{interval}")
        if start_ms >= end_ms:
            raise ValueError("invalid_history_range")
        cursor = int(end_ms)
        rows: dict[int, list[float]] = {}
        while cursor > start_ms:
            response = self.session.get(self.endpoint, params={
                "symbol": symbol.upper(),
                "productType": "USDT-FUTURES",
                "granularity": granularity,
                "endTime": cursor,
                "limit": 200,
            }, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("code")) != "00000":
                raise RuntimeError(f"bitget_history_error:{payload.get('code')}:{payload.get('msg')}")
            batch = payload.get("data") or []
            if not batch:
                break
            oldest = cursor
            for item in batch:
                timestamp = int(item[0])
                oldest = min(oldest, timestamp)
                if start_ms <= timestamp < end_ms:
                    rows[timestamp] = [
                        timestamp, float(item[1]), float(item[2]), float(item[3]),
                        float(item[4]), float(item[5]),
                    ]
            if oldest >= cursor:
                break
            # Bitget rounds endTime to candle boundaries. Reusing the oldest
            # timestamp keeps the immediately preceding candle; dedup handles
            # any boundary row returned twice.
            cursor = oldest
            time.sleep(self.request_pause)
        return pd.DataFrame(
            [rows[key] for key in sorted(rows)], columns=["ot", "o", "h", "l", "c", "v"]
        )
