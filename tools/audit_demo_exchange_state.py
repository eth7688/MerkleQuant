import json
import sys

from trader import BinanceClient, TradeConfig


def has_secret(cfg):
    return {
        "api_key": bool(getattr(cfg, "api_key", "")),
        "api_secret": bool(getattr(cfg, "api_secret", "")),
        "testnet_api_key": bool(getattr(cfg, "testnet_api_key", "")),
        "testnet_api_secret": bool(getattr(cfg, "testnet_api_secret", "")),
    }


def load_cfg(path):
    return TradeConfig.load(path)


def client_for(cfg, market_type):
    if getattr(cfg, "testnet", False):
        key = cfg.testnet_api_key or cfg.api_key
        secret = cfg.testnet_api_secret or cfg.api_secret
    else:
        key = cfg.api_key
        secret = cfg.api_secret
    if not key or not secret:
        return None
    return BinanceClient(key, secret, testnet=cfg.testnet, market_type=market_type)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "demo_bot_config.json"
    cfg = load_cfg(path)
    out = {
        "mode": cfg.mode,
        "testnet": cfg.testnet,
        "configured_market_type": cfg.market_type,
        "has_secret": has_secret(cfg),
        "futures_positions": [],
        "spot_balances": [],
        "errors": [],
    }

    fut = client_for(cfg, "futures")
    if fut is not None:
        try:
            out["futures_balance_usdt"] = fut.get_balance()
            positions = fut.get_positions() or []
            for p in positions:
                out["futures_positions"].append({
                    "symbol": p.get("symbol"),
                    "direction": p.get("direction"),
                    "quantity": p.get("quantity"),
                    "entryPrice": p.get("entryPrice"),
                    "markPrice": p.get("markPrice"),
                    "unRealizedProfit": p.get("unRealizedProfit"),
                    "openTime": p.get("openTime", ""),
                })
        except Exception as exc:
            out["errors"].append(f"futures:{type(exc).__name__}:{exc}")

    spot = client_for(cfg, "spot")
    if spot is not None:
        try:
            out["spot_balance_usdt"] = spot.get_balance()
            balances = spot.get_spot_balances() or {}
            for asset, info in balances.items():
                if asset == "USDT":
                    continue
                total = float(info.get("total") or 0)
                if total <= 0:
                    continue
                symbol = f"{asset}USDT"
                price = 0.0
                try:
                    price = float(spot.get_price(symbol) or 0.0)
                except Exception:
                    price = 0.0
                out["spot_balances"].append({
                    "asset": asset,
                    "symbol": symbol,
                    "free": info.get("free"),
                    "locked": info.get("locked"),
                    "total": total,
                    "price": price,
                    "usdt_value": round(total * price, 4) if price else None,
                })
        except Exception as exc:
            out["errors"].append(f"spot:{type(exc).__name__}:{exc}")

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
