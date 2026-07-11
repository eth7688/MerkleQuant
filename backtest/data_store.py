from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = ("ot", "o", "h", "l", "c", "v")


class HistoricalStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _candle_path(self, exchange: str, symbol: str, interval: str) -> Path:
        return self.root / "candles" / exchange.lower() / symbol.upper() / f"{interval}.csv"

    def write_candles(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        frame: pd.DataFrame,
        data_version: str,
    ) -> Path:
        missing = set(REQUIRED_COLUMNS) - set(frame.columns)
        if missing:
            raise ValueError(f"missing_candle_columns:{sorted(missing)}")
        clean = frame.loc[:, REQUIRED_COLUMNS].copy()
        clean["ot"] = pd.to_numeric(clean["ot"], errors="raise").astype("int64")
        for column in REQUIRED_COLUMNS[1:]:
            clean[column] = pd.to_numeric(clean[column], errors="raise").astype(float)
        clean = clean.drop_duplicates("ot", keep="last").sort_values("ot").reset_index(drop=True)
        path = self._candle_path(exchange, symbol, interval)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".csv.tmp")
        clean.to_csv(temp, index=False)
        temp.replace(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {
            "exchange": exchange.lower(), "symbol": symbol.upper(), "interval": interval,
            "rows": len(clean), "first_ot": int(clean["ot"].iloc[0]),
            "last_ot": int(clean["ot"].iloc[-1]), "data_version": data_version,
            "sha256": digest,
        }
        path.with_suffix(".manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def read_candles(self, exchange: str, symbol: str, interval: str) -> pd.DataFrame:
        path = self._candle_path(exchange, symbol, interval)
        frame = pd.read_csv(path)
        frame["ot"] = frame["ot"].astype("int64")
        return frame.sort_values("ot").reset_index(drop=True)

    @staticmethod
    def find_gaps(frame: pd.DataFrame, interval_ms: int) -> list[tuple[int, int]]:
        opens = sorted(int(value) for value in frame["ot"].tolist())
        gaps: list[tuple[int, int]] = []
        for previous, current in zip(opens, opens[1:]):
            expected = previous + interval_ms
            if current > expected:
                gaps.append((expected, current - interval_ms))
        return gaps

    def write_contracts(self, contracts: list[dict]) -> None:
        path = self.root / "contracts.json"
        path.write_text(json.dumps(contracts, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def universe_at(self, timestamp_ms: int) -> list[str]:
        path = self.root / "contracts.json"
        contracts = json.loads(path.read_text(encoding="utf-8"))
        available = []
        for row in contracts:
            listed = int(row.get("listed_at", 0) or 0)
            delisted = row.get("delisted_at")
            if listed <= timestamp_ms and (delisted is None or timestamp_ms < int(delisted)):
                available.append(str(row["symbol"]).upper())
        return sorted(available)
