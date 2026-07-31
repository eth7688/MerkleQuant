"""
均线粘合选币器 v2
用法:
  python screener.py                        (默认: 4H 粘合扫选)
  python screener.py --1h                   (1H 粘合扫选)
  python screener.py --short                (山寨币暴涨后做空扫选)
  python screener.py --short --days 7 --min-chg 50  (自定义做空参数)
"""
import math, sys, time, requests, pandas as pd, numpy as np
from datetime import datetime, timezone

# Windows终端兼容emoji (模块级, import时即生效)
try: sys.stdout.reconfigure(encoding='utf-8')
except: pass

# ==========================================
# Config
# ==========================================
EMA_LENS = [20, 60, 120]
MA_LENS  = [20, 60, 120]

# 数据源
_use_testnet = False
_rest_base = "https://api.binance.com"
_exchange = "binance"  # 调用方在每次扫描前设置

def set_testnet_mode(on: bool):
    global _use_testnet, _rest_base
    _use_testnet = on
    _rest_base = "https://testnet.binance.vision" if on else "https://api.binance.com"

def set_scan_exchange(ex: str):
    global _exchange
    _exchange = ex
MIN_VOLUME = 3_000_000    # 最低24h成交量(USDT)
MIN_PRICE  = 0.001
MAX_PAIRS  = 1000

ENTRY_FLOAT_LIMIT = {
    "15m": 0.016,
    "1h": 0.025,
    "4h": 0.045,
    "1d": 0.065,
    "1w": 0.065,
}

FRACTAL_FLOAT_LIMIT = {
    "15m": 0.025,
    "1h": 0.040,
    "4h": 0.065,
    "1d": 0.095,
    "1w": 0.095,
}

BREAKOUT_CONFIRM_WINDOW = {
    "15m": 6,
    "1h": 4,
    "4h": 6,
    "1d": 6,
    "1w": 6,
}

def get_entry_float_limit(interval: str) -> float:
    return ENTRY_FLOAT_LIMIT.get(interval, 0.020)

def get_fractal_float_limit(interval: str) -> float:
    return FRACTAL_FLOAT_LIMIT.get(interval, 0.030)

def get_breakout_confirm_window(interval: str) -> int:
    return BREAKOUT_CONFIRM_WINDOW.get(interval, 5)

def get_squeeze_max(interval: str) -> float:
    return {"15m": 2.0, "1h": 3.9, "4h": 6.5, "1d": 10.4, "1w": 9.1}.get(interval, 5.2)

def get_squeeze_lookback(interval: str) -> int:
    return {"1h": 20, "4h": 40, "1d": 30, "1w": 20}.get(interval, 25)

def _find_squeeze_zone_before_breakout(spread_arr, breakout_bar: int, interval: str):
    """找到本次突破前最近一段合格密集区。"""
    squeeze_max = get_squeeze_max(interval)
    look_start = max(120, breakout_bar - get_squeeze_lookback(interval))
    zone_end = None
    for j in range(breakout_bar, look_start - 1, -1):
        sp = spread_arr[j]
        if not np.isnan(sp) and sp <= squeeze_max:
            zone_end = j
            break
    if zone_end is None:
        return None

    zone_start = zone_end
    for j in range(zone_end - 1, look_start - 1, -1):
        sp = spread_arr[j]
        if not np.isnan(sp) and sp <= squeeze_max:
            zone_start = j
        else:
            break

    if zone_end - zone_start + 1 < 5:
        return None
    return zone_start, zone_end

def _calc_squeeze_band_stop(min_band, max_band, spread_arr, squeeze_start: int, squeeze_end: int, direction: str, reference_idx: int = None):
    """用确认/入场K线的六线真实上下轨作为结构边缘止损。"""
    valid_idx = [
        j for j in range(squeeze_start, squeeze_end + 1)
        if not np.isnan(spread_arr[j]) and not np.isnan(min_band[j]) and not np.isnan(max_band[j])
    ]
    if not valid_idx:
        return None
    tight_idx = min(valid_idx, key=lambda j: spread_arr[j])
    edge_idx = reference_idx if reference_idx is not None else tight_idx
    if edge_idx < 0 or edge_idx >= len(min_band) or np.isnan(min_band[edge_idx]) or np.isnan(max_band[edge_idx]):
        edge_idx = tight_idx
    if direction == "LONG":
        edge = float(min_band[edge_idx])
        return edge * 0.995, edge_idx, edge
    edge = float(max_band[edge_idx])
    return edge * 1.005, edge_idx, edge

# 大市值币种(做空模式排除)
BLUE_CHIPS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
              "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
              "MATICUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", "BCHUSDT"}
STABLECOINS = set()
JUNK_LIST = set()  # 垃圾标的集合 (稳定币+法币+股票+商品)

# 稳定币前缀
for pfx in {"USDC","FDUSD","USD1","RLUSD","TUSD","DAI","USDP","USDD","PYUSD","USDY","crvUSD","sUSD","eUSD","GHO","LUSD","MIM","FRAX","USTC","USDE","USR","EURS","EURC","XSGD","XAUT","PAXG","USDJ","USDX","USDB","USDZ","AEUR","USDF","STUSD","USDQ","XUSD","USDS"}:
    JUNK_LIST.add(pfx+"USDT")

# 法币交易对
for fx in {"EUR","GBP","AUD","JPY","CAD","CHF","NZD","TRY","BRL","ZAR","RUB","UAH","PLN","RON","ARS"}:
    JUNK_LIST.add(fx+"USDT")

# 传统金融合成资产 (美股/ETF/商品/黄金)
TRADFI_SYMBOLS = {
    "UPUSDT","DOWNUSDT","BULLUSDT","BEARUSDT","BETHUSDT",
    "SPYUSDT","QQQUSDT","TQQQUSDT","SOXLUSDT",
    "AAPLUSDT","TSLAUSDT","GMEUSDT","AMCUSDT","NVDAUSDT","MSTRUSDT","METAUSDT","COINUSDT",
    "INTCUSDT","DELLUSDT","AMDUSDT","MUUSDT","QCOMUSDT","MRVLUSDT","SNDKUSDT",
    "AMZNUSDT","GLWUSDT","APLDUSDT","COHRUSDT","BEUSDT",
    "MSFTUSDT","GOOGLUSDT","GOOGUSDT","NFLXUSDT",
    "PYPLUSDT","DISUSDT","BABAUSDT","NIOUSDT","RIVNUSDT","PLTRUSDT",
    "XAUUSDT","XAGUSDT","WTIUSDT","BRENTUSDT","PAXGUSDT",
    "UUSDT","USDEUSDT",
    # 股票代币化 (Binance Stock Tokens)
    "LITEUSDT","TSLAUSDT","AAPLUSDT","MSTRUSDT","COINUSDT","METAUSDT",
    "NVDAUSDT","AMDUSDT","INTCUSDT","MUUSDT","QCOMUSDT","MRVLUSDT",
    "GMEUSDT","AMCUSDT","DELLUSDT","SNDKUSDT",
}
for x in TRADFI_SYMBOLS:
    JUNK_LIST.add(x)

def is_tradfi_or_junk(symbol: str) -> bool:
    if symbol in JUNK_LIST:
        return True
    # 动态拦截股票、看多/看空杠杆代币、商品
    forbidden_keywords = ["STOCK", "BULL", "BEAR", "UP", "DOWN", "XAU", "XAG"]
    for kw in forbidden_keywords:
        if kw in symbol:
            return True
    return False

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
}
INTERVAL_OPEN_PHASE_MS = {
    "1w": 4 * 24 * 60 * 60_000,  # Monday 00:00 UTC relative to Unix epoch
}

def _closed_kline_frame(df, interval, limit=None):
    """统一只返回已收盘K线，避免未收盘K污染突破/分型/出场判断。"""
    if df is None or len(df) == 0 or "ot" not in df:
        return df
    try:
        step_ms = INTERVAL_MS.get(str(interval).strip())
        if not step_ms:
            return df.tail(limit).reset_index(drop=True) if limit else df.reset_index(drop=True)
        out = df.copy()
        out["ot"] = out["ot"].astype("int64")
        out = out.sort_values("ot").reset_index(drop=True)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        out = out[out["ot"] + step_ms <= now_ms].reset_index(drop=True)
        if limit:
            out = out.tail(limit).reset_index(drop=True)
        out.attrs["closed_only"] = True
        out.attrs["interval"] = interval
        return out
    except Exception:
        return df.tail(limit).reset_index(drop=True) if limit else df

def fetch_klines(symbol, interval, limit=200, exchange=None, closed_only=True,
                 market_type=None, testnet=None, price_type=None):
    ex = exchange if exchange is not None else _exchange
    if ex == "bitget":
        return _fetch_klines_bitget(symbol, interval, limit, closed_only=closed_only)
    if str(market_type or "spot").lower() == "futures":
        base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        endpoint = "markPriceKlines" if str(price_type or "").lower() == "mark" else "klines"
        url = f"{base}/fapi/v1/{endpoint}"
    else:
        if testnet is True:
            base = "https://testnet.binance.vision"
        elif testnet is False or exchange:
            base = "https://api.binance.com"
        else:
            base = _rest_base
        url = f"{base}/api/v3/klines"
    try:
        req_limit = min(int(limit) + 1, 1000) if closed_only else int(limit)
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": req_limit}, timeout=5)
        r.raise_for_status()
        data = r.json()
        if not data: return None
        df = pd.DataFrame(data, columns=["ot","o","h","l","c","v","ct","qv","n","tbv","tbqv","ig"])
        for col in ["o","h","l","c","v"]: df[col] = df[col].astype(float)
        return _closed_kline_frame(df, interval, limit) if closed_only else df
    except: return None


def fetch_klines_range(symbol, interval, start_ms, end_ms=None, exchange=None,
                       market_type=None, testnet=None, price_type=None,
                       pause_seconds=0.06, max_pages=1000):
    """Fetch a complete closed-candle range, failing closed on partial pagination."""
    step_ms = INTERVAL_MS.get(str(interval).strip())
    if not step_ms:
        return None
    try:
        start_ms = int(start_ms)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        phase_ms = INTERVAL_OPEN_PHASE_MS.get(str(interval).strip(), 0)
        current_open_ms = (
            ((now_ms - phase_ms) // step_ms) * step_ms + phase_ms
        )
        last_closed_open_ms = current_open_ms - step_ms
        end_ms = (
            last_closed_open_ms
            if end_ms is None
            else min(int(end_ms), last_closed_open_ms)
        )
        max_pages = max(1, int(max_pages))
        pause_seconds = max(0.0, float(pause_seconds))
    except (TypeError, ValueError):
        return None

    columns = ["ot", "o", "h", "l", "c", "v"]
    if start_ms > end_ms:
        empty = pd.DataFrame(columns=columns)
        empty.attrs.update({
            "range_complete": True,
            "range_start_ms": start_ms,
            "range_end_ms": end_ms,
            "interval": interval,
        })
        return empty

    first_expected_ms = (
        ((start_ms - phase_ms + step_ms - 1) // step_ms) * step_ms
        + phase_ms
    )
    last_expected_ms = (
        ((end_ms - phase_ms) // step_ms) * step_ms
        + phase_ms
    )
    if first_expected_ms > last_expected_ms:
        empty = pd.DataFrame(columns=columns)
        empty.attrs.update({
            "range_complete": True,
            "range_start_ms": start_ms,
            "range_end_ms": end_ms,
            "interval": interval,
        })
        return empty

    rows = {}
    complete = False
    ex = exchange if exchange is not None else _exchange
    try:
        if ex == "bitget":
            granularity = {
                "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W",
            }.get(interval)
            if not granularity:
                return None
            # Bitget history-candles treats endTime as exclusive. Request the
            # next candle boundary so the requested last closed candle is
            # included in the first page.
            cursor = end_ms + step_ms
            page_limit = 200
            for page_index in range(max_pages):
                batch = None
                for attempt in range(3):
                    try:
                        response = requests.get(
                            "https://api.bitget.com/api/v2/mix/market/history-candles",
                            params={
                                "symbol": symbol,
                                "granularity": granularity,
                                "productType": "USDT-FUTURES",
                                "endTime": str(cursor),
                                "limit": str(page_limit),
                            },
                            timeout=5,
                        )
                        payload = response.json()
                        if (
                            response.status_code == 200
                            and isinstance(payload, dict)
                            and payload.get("code") == "00000"
                        ):
                            batch = payload.get("data") or None
                    except (requests.RequestException, ValueError):
                        batch = None
                    if batch:
                        break
                    if attempt < 2:
                        time.sleep(max(pause_seconds, 0.2 * (2 ** attempt)))
                if not batch:
                    return None
                valid = [item for item in batch if len(item) >= 6]
                if not valid or len(valid) != len(batch):
                    return None
                open_times = [int(item[0]) for item in valid]
                ordered_times = sorted(open_times)
                if len(set(ordered_times)) != len(ordered_times):
                    return None
                if any(
                    (open_time - phase_ms) % step_ms != 0
                    for open_time in ordered_times
                ):
                    return None
                if any(
                    right - left != step_ms
                    for left, right in zip(ordered_times, ordered_times[1:])
                ):
                    return None
                newest = ordered_times[-1]
                boundary_gap = cursor - newest
                if boundary_gap < 0 or boundary_gap > step_ms:
                    return None
                for item, open_time in zip(valid, open_times):
                    if start_ms <= open_time <= end_ms:
                        values = [
                            open_time,
                            float(item[1]),
                            float(item[2]),
                            float(item[3]),
                            float(item[4]),
                            float(item[5]),
                        ]
                        if not all(math.isfinite(value) for value in values[1:]):
                            return None
                        rows[open_time] = values
                oldest = ordered_times[0]
                if oldest < start_ms + step_ms:
                    complete = True
                    break
                if oldest >= cursor:
                    return None
                cursor = oldest
                if pause_seconds and page_index + 1 < max_pages:
                    time.sleep(pause_seconds)
        else:
            if str(market_type or "spot").lower() == "futures":
                base = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
                endpoint = "markPriceKlines" if str(price_type or "").lower() == "mark" else "klines"
                url = f"{base}/fapi/v1/{endpoint}"
            else:
                if testnet is True:
                    base = "https://testnet.binance.vision"
                elif testnet is False or exchange:
                    base = "https://api.binance.com"
                else:
                    base = _rest_base
                url = f"{base}/api/v3/klines"
            cursor = start_ms
            page_limit = 1000
            for page_index in range(max_pages):
                response = requests.get(
                    url,
                    params={
                        "symbol": symbol,
                        "interval": interval,
                        "startTime": cursor,
                        "endTime": end_ms,
                        "limit": page_limit,
                    },
                    timeout=5,
                )
                response.raise_for_status()
                batch = response.json() or []
                if not batch:
                    return None
                valid = [item for item in batch if len(item) >= 6]
                if not valid or len(valid) != len(batch):
                    return None
                open_times = [int(item[0]) for item in valid]
                ordered_times = sorted(open_times)
                if len(set(ordered_times)) != len(ordered_times):
                    return None
                if any(
                    (open_time - phase_ms) % step_ms != 0
                    for open_time in ordered_times
                ):
                    return None
                if any(
                    right - left != step_ms
                    for left, right in zip(ordered_times, ordered_times[1:])
                ):
                    return None
                first_open = ordered_times[0]
                if first_open < cursor or first_open - cursor >= step_ms:
                    return None
                for item, open_time in zip(valid, open_times):
                    if start_ms <= open_time <= end_ms:
                        values = [
                            open_time,
                            float(item[1]),
                            float(item[2]),
                            float(item[3]),
                            float(item[4]),
                            float(item[5]),
                        ]
                        if not all(math.isfinite(value) for value in values[1:]):
                            return None
                        rows[open_time] = values
                latest = ordered_times[-1]
                if latest > end_ms:
                    return None
                if end_ms - latest < step_ms:
                    complete = True
                    break
                next_cursor = latest + step_ms
                if next_cursor <= cursor:
                    return None
                cursor = next_cursor
                if pause_seconds and page_index + 1 < max_pages:
                    time.sleep(pause_seconds)
    except Exception:
        return None

    if not complete:
        return None
    ordered_keys = sorted(rows)
    if not ordered_keys:
        return None
    if (
        ordered_keys[0] != first_expected_ms
        or ordered_keys[-1] != last_expected_ms
        or any(
            right - left != step_ms
            for left, right in zip(ordered_keys, ordered_keys[1:])
        )
    ):
        return None
    frame = pd.DataFrame([rows[key] for key in ordered_keys], columns=columns)
    frame.attrs.update({
        "range_complete": True,
        "range_start_ms": start_ms,
        "range_end_ms": end_ms,
        "interval": interval,
    })
    return frame


def _fetch_klines_bitget(symbol, interval, limit=200, closed_only=True):
    granularity = {"1m":"1m","5m":"5m","15m":"15m","30m":"30m",
                   "1h":"1H","4h":"4H","1d":"1D","1w":"1W"}.get(interval,"1H")
    try:
        req_limit = min(int(limit) + 1, 1000) if closed_only else int(limit)
        params = {"symbol": symbol, "granularity": granularity, "limit": str(req_limit),
                  "productType": "USDT-FUTURES"}
        r = requests.get("https://api.bitget.com/api/v2/mix/market/candles",
                        params=params, timeout=5)
        if r.status_code != 200: return None
        data = r.json()
        if data.get("code") != "00000": return None
        rows = data.get("data", [])
        if not rows: return None
        df = pd.DataFrame([{"ot":int(r[0]),"o":float(r[1]),"h":float(r[2]),
                            "l":float(r[3]),"c":float(r[4]),"v":float(r[5])} for r in rows if len(r)>=6])
        df["t"] = pd.to_datetime(df["ot"], unit="ms")
        return _closed_kline_frame(df, interval, limit) if closed_only else df
    except: return None

def _fetch_pairs_bitget():
    """Bitget 合约交易对 (USDT永续) + 24h 成交量"""
    try:
        r = requests.get("https://api.bitget.com/api/v2/mix/market/contracts",
                        params={"productType": "USDT-FUTURES"}, timeout=10)
        if r.status_code != 200: return [], {}
        data = r.json()
        if data.get("code") != "00000": return [], {}
        symbols = []
        for item in data.get("data", []):
            sym = item.get("symbol", "")
            if sym.endswith("USDT") and item.get("symbolType") == "perpetual" and item.get("isRwa") != "YES":
                symbols.append(sym)

        # 获取 24h 成交量
        vols = {}
        try:
            r2 = requests.get("https://api.bitget.com/api/v2/mix/market/tickers",
                            params={"productType": "USDT-FUTURES"}, timeout=10)
            if r2.status_code == 200:
                d2 = r2.json()
                if d2.get("code") == "00000":
                    for t in d2.get("data", []):
                        sym = t.get("symbol", "")
                        qv = t.get("usdtVolume") or t.get("quoteVolume") or ""
                        if sym and qv:
                            vols[sym] = float(qv)
        except:
            pass

        for sym in symbols:
            if sym not in vols:
                vols[sym] = MIN_VOLUME
        return symbols, vols
    except: return [], {}

def fetch_pairs(exchange=None):
    ex = exchange if exchange is not None else _exchange
    if ex == "bitget":
        return _fetch_pairs_bitget()
    base = "https://api.binance.com" if exchange else _rest_base
    try:
        # 用现货API拉交易对(更稳定), K线也用现货
        r = requests.get(f"{base}/api/v3/exchangeInfo", timeout=10)
        r.raise_for_status()
        info = r.json()
        symbols = [s["symbol"] for s in info["symbols"]
                   if s["symbol"].endswith("USDT") and s["status"] == "TRADING"
                   and "USDC" not in s["symbol"]]  # 排除USDC交易对
        r2 = requests.get(f"{base}/api/v3/ticker/24hr", timeout=10)
        r2.raise_for_status()
        tickers = {t["symbol"]: float(t["quoteVolume"]) for t in r2.json()}
        return symbols, tickers
    except Exception as e:
        print(f"Error: {e}"); return [], {}

def fetch_midcap_symbols():
    """从 CoinGecko 拉取市值 100M-1B 的代币列表, 返回 Binance USDT 交易对集合"""
    midcap = set()
    try:
        for page in range(1, 4):
            url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false"
            r = requests.get(url, timeout=15)
            if r.status_code != 200: break
            for coin in r.json():
                mcap = coin.get("market_cap", 0)
                if mcap and 50_000_000 <= mcap <= 5_000_000_000:
                    sym = coin.get("symbol", "").upper()
                    if sym:
                        midcap.add(sym + "USDT")
        print(f"  CoinGecko: {len(midcap)} 个中市值币 (50M-5B)")
    except Exception as e:
        print(f"  CoinGecko 获取失败: {e}")
    return midcap

def calc_spread(closes):
    n = len(closes)
    if n < 150: return 999
    ema_vals = [ema(closes, p).iloc[-1] for p in EMA_LENS]
    ma_vals  = [closes.rolling(p).mean().iloc[-1] for p in MA_LENS]
    all_mas = ema_vals + ma_vals
    return (max(all_mas) - min(all_mas)) / closes.iloc[-1] * 100

def calc_trend(closes):
    price = closes.iloc[-1]
    ema_vals = [ema(closes, p).iloc[-1] for p in EMA_LENS]
    ma_vals  = [closes.rolling(p).mean().iloc[-1] for p in MA_LENS]
    all_mas = ema_vals + ma_vals
    max_ma, min_ma = max(all_mas), min(all_mas)
    inside = min_ma <= price <= max_ma
    ema20 = ema_vals[0]

    if price > max_ma: return "突破上轨", False, max_ma, min_ma, ema20
    elif price < min_ma: return "跌破下轨", False, max_ma, min_ma, ema20
    elif price > ema20: return "粘合偏多", True, max_ma, min_ma, ema20
    else: return "粘合偏空", True, max_ma, min_ma, ema20

def pct_change(closes, days, interval):
    bars = days * (24 if interval == "1h" else 6)
    bars = min(bars, len(closes) - 1)
    p0 = closes.iloc[-bars-1] if bars < len(closes) else closes.iloc[0]
    return (closes.iloc[-1] - p0) / p0 * 100

# ==========================================
# Mode 1: 粘合扫选 (默认)
# ==========================================
def scan_squeeze(interval, top_n, change_days, max_chg, min_chg, use_chg_filter):
    mode_desc = f"{change_days}日涨跌{min_chg:+.0f}%~{max_chg:+.0f}%" if use_chg_filter else "无涨跌过滤"
    print(f"\n{'='*80}")
    print(f"  均线粘合扫选 [{interval}] — 寻找蓄力待发的币")
    print(f"  {mode_desc}")
    print(f"{'='*80}")

    symbols, vols = fetch_pairs()
    ranked = sorted(symbols, key=lambda s: vols.get(s, 0), reverse=True)
    ranked = [s for s in ranked if vols.get(s, 0) > MIN_VOLUME and s != "USDCUSDT" and not is_tradfi_or_junk(s)]
    print(f"候选: {len(ranked)} 个 (量>{MIN_VOLUME/1e6:.0f}M, 排除USDC)")
    print(f"扫描中...\n")

    results = []; errors = 0
    for i, sym in enumerate(ranked[:MAX_PAIRS]):
        df = fetch_klines(sym, interval, 200)
        if df is None or len(df) < 150: errors += 1; continue
        closes = df["c"]; price = closes.iloc[-1]
        if price < MIN_PRICE: continue

        if use_chg_filter:
            chg = pct_change(closes, change_days, interval)
            if chg < min_chg or chg > max_chg: continue
        else:
            chg = pct_change(closes, 3, interval)

        spread = calc_spread(closes)
        trend, inside, max_ma, min_ma, ema20 = calc_trend(closes)
        if not inside: continue

        results.append({
            "symbol": sym, "spread": round(spread,2), "trend": trend,
            "price": price, "vol24m": vols.get(sym,0)/1e6, "chg": round(chg,1),
        })
        if (i+1) % 30 == 0: sys.stdout.write(f"\r  已扫: {i+1}/{min(len(ranked),MAX_PAIRS)} ..."); sys.stdout.flush()

    print(f"\r  完成: {len(results)} 个未突破粘合币")

    results.sort(key=lambda r: r["spread"])

    print(f"\n{'='*85}")
    print(f"  Top {top_n} 粘合待突破 ({interval})")
    print(f"{'='*85}")
    print(f"  {'#':3s} {'交易对':12s} {'离散%':>6s} {'状态':10s} {'价格':>10s} {'3d涨跌':>7s} {'24h量M':>7s}")
    print(f"  {'-'*70}")

    for i, r in enumerate(results[:top_n]):
        star = "***" if r["spread"] < 2 else "** " if r["spread"] < 4 else "*  " if r["spread"] < 6 else "   "
        sgn = "+" if r["chg"] >= 0 else ""
        print(f"  {i+1:<3d} {star} {r['symbol']:12s} {r['spread']:>5.2f}% {r['trend']:10s} {r['price']:>10.4f} {sgn}{r['chg']:>+6.1f}% {r['vol24m']:>6.1f}M")

    top5 = [r["symbol"] for r in results[:5]]
    print(f"\n  {'='*85}")
    print(f"  *** <2%  ** <4%  *  <6%")
    if top5: print(f"  重点盯盘: {', '.join(top5)}")
    print(f"{'='*85}\n")
    return results


# ==========================================
# Mode 2: 山寨币做空扫选
# ==========================================
def scan_short(interval, top_n, change_days, min_chg_pct):
    print(f"\n{'='*85}")
    print(f"  山寨币暴涨做空扫选 [{interval}] — {change_days}日涨幅 >{min_chg_pct}%")
    print(f"  排除BTC/ETH/BNB/SOL等大市值, 专找情绪驱动的小市值山寨")
    print(f"{'='*85}")

    symbols, vols = fetch_pairs()
    ranked = [s for s in symbols if vols.get(s, 0) > MIN_VOLUME/2 and s not in BLUE_CHIPS and not is_tradfi_or_junk(s)]
    print(f"候选山寨: {len(ranked)} 个 (排除蓝筹+低量)")
    print(f"扫描中...\n")

    results = []; errors = 0
    for i, sym in enumerate(ranked[:MAX_PAIRS]):
        df = fetch_klines(sym, interval, 200)
        if df is None or len(df) < 150: errors += 1; continue
        closes = df["c"]; price = closes.iloc[-1]
        if price < MIN_PRICE: continue

        chg = pct_change(closes, change_days, interval)
        if chg < min_chg_pct: continue  # 只选暴涨的

        spread = calc_spread(closes)
        trend, inside, max_ma, min_ma, ema20 = calc_trend(closes)

        # 做空信号评估
        # 1. 价格还在均线上方(拉盘后还未崩溃) = 做空机会
        # 2. 价格已经开始跌破EMA20 = 转弱信号
        # 3. 均线离散度开始收窄 = 动能衰减
        above_all = price > max_ma
        below_ema20 = price < ema20
        rolling_over = above_all and below_ema20  # 最强做空信号: 还在高位但开始转弱

        short_score = 0
        if price > max_ma: short_score += 30      # 仍在高位, 做空空间大
        if price < ema20: short_score += 25        # 已跌破EMA20, 动能转弱
        if spread < 8: short_score += 20           # 均线开始收敛, 上涨衰竭
        if chg > 80: short_score += 15             # 极端涨幅, 回归压力大
        if rolling_over: short_score += 10         # 完美做空形态

        results.append({
            "symbol": sym, "chg": round(chg,1), "price": price,
            "spread": round(spread,2), "trend": trend,
            "vol24m": vols.get(sym,0)/1e6,
            "score": short_score, "roll": rolling_over,
            "above_all": above_all, "below_ema20": below_ema20,
        })
        if (i+1) % 30 == 0: sys.stdout.write(f"\r  已扫: {i+1}/{min(len(ranked),MAX_PAIRS)} ..."); sys.stdout.flush()

    print(f"\r  完成: {len(results)} 个暴涨山寨币")

    # 按做空评分排序
    results.sort(key=lambda r: r["score"], reverse=True)

    print(f"\n{'='*90}")
    print(f"  Top {top_n} 做空候选")
    print(f"{'='*90}")
    print(f"  {'#':3s} {'交易对':12s} {'涨幅':>7s} {'离散%':>6s} {'状态':10s} {'价格':>10s} {'评分':>4s} {'信号':10s}")
    print(f"  {'-'*75}")

    for i, r in enumerate(results[:top_n]):
        star = "!!!" if r["score"] >= 70 else "!! " if r["score"] >= 50 else "!  " if r["score"] >= 30 else "   "
        sig = "做空!" if r["roll"] else "高位" if r["above_all"] else "转弱" if r["below_ema20"] else "观望"
        print(f"  {i+1:<3d} {star} {r['symbol']:12s} {r['chg']:>+6.1f}% {r['spread']:>5.2f}% {r['trend']:10s} {r['price']:>10.4f} {r['score']:>4d}  {sig:10s}")

    top5 = [(r["symbol"], r["chg"]) for r in results[:5]]
    print(f"\n  {'='*90}")
    print(f"  !!! = 高概率做空(>75分)  !! = 中等(>50)  ! = 关注(>30)")
    print(f"  做空信号: 价在均线上方但跌破EMA20 = 暴涨后首次转弱, 最佳做空时机")
    if top5: print(f"  重点盯盘: {', '.join(f'{s}({c:+.0f}%)' for s,c in top5)}")
    print(f"{'='*90}\n")
    return results


# ==========================================
# Helper: K线包含关系处理
# ==========================================
def _clean_kline_window(highs, lows, closes):
    """窗口内K线包含关系处理, 返回标准序列"""
    n = len(highs)
    if n < 3: return highs, lows, closes, list(range(n))
    h, l, c = [], [], []
    idx_map = []
    for i in range(n):
        if i == 0:
            h.append(highs[i]); l.append(lows[i]); c.append(closes[i])
            idx_map.append(i); continue
        if highs[i] <= h[-1] and lows[i] >= l[-1]:
            if len(h) >= 2 and h[-1] > h[-2]:
                h[-1] = max(h[-1], highs[i]); l[-1] = max(l[-1], lows[i])
            elif len(h) >= 2 and h[-1] < h[-2]:
                h[-1] = min(h[-1], highs[i]); l[-1] = min(l[-1], lows[i])
            else:
                h[-1] = max(h[-1], highs[i]); l[-1] = min(l[-1], lows[i])
            c[-1] = closes[i]; idx_map[-1] = i; continue
        if highs[i] >= h[-1] and lows[i] <= l[-1]:
            if len(h) >= 2 and h[-1] > h[-2]:
                h[-1] = highs[i]; l[-1] = max(l[-1], lows[i])
            elif len(h) >= 2 and h[-1] < h[-2]:
                l[-1] = lows[i]; h[-1] = min(h[-1], highs[i])
            else:
                h[-1] = highs[i]; l[-1] = lows[i]
            c[-1] = closes[i]; idx_map[-1] = i; continue
        h.append(highs[i]); l.append(lows[i]); c.append(closes[i])
        idx_map.append(i)
    return (np.array(h), np.array(l), np.array(c), idx_map)

def _find_fractal_structure_sequence(highs, lows, closes, direction, offset=0,
                                     min_start_idx=None, min_confirm_idx=None):
    """
    轻量结构序列确认:
    LONG 需要突破后先出现顶分型, 再由最新底分型确认回踩结束。
    SHORT 需要跌破后先出现底分型, 再由最新顶分型确认反弹结束。
    """
    try:
        ch, cl, cc, idx_map = _clean_kline_window(highs, lows, closes)
        m = len(ch)
        if m < 4:
            return None

        def gidx(i):
            return offset + (idx_map[i] if i < len(idx_map) else i)

        latest_candidates = [m - 2, m - 3]
        if direction == "LONG":
            for confirm_i in latest_candidates:
                if confirm_i <= 0 or confirm_i >= m - 1:
                    continue
                confirm_idx = gidx(confirm_i)
                if min_confirm_idx is not None and confirm_idx < min_confirm_idx:
                    continue
                if not (cl[confirm_i] < cl[confirm_i - 1] and cl[confirm_i] < cl[confirm_i + 1]):
                    continue
                for start_i in range(confirm_i - 1, 0, -1):
                    start_idx = gidx(start_i)
                    if min_start_idx is not None and start_idx < min_start_idx:
                        continue
                    if ch[start_i] > ch[start_i - 1] and ch[start_i] > ch[start_i + 1]:
                        return {
                            "first_fractal": "top",
                            "confirm_fractal": "bottom",
                            "first_fractal_bar": start_idx,
                            "confirm_fractal_bar": confirm_idx,
                            "fractal_sl": float(cl[confirm_i]),
                        }
            return None

        for confirm_i in latest_candidates:
            if confirm_i <= 0 or confirm_i >= m - 1:
                continue
            confirm_idx = gidx(confirm_i)
            if min_confirm_idx is not None and confirm_idx < min_confirm_idx:
                continue
            if not (ch[confirm_i] > ch[confirm_i - 1] and ch[confirm_i] > ch[confirm_i + 1]):
                continue
            for start_i in range(confirm_i - 1, 0, -1):
                start_idx = gidx(start_i)
                if min_start_idx is not None and start_idx < min_start_idx:
                    continue
                if cl[start_i] < cl[start_i - 1] and cl[start_i] < cl[start_i + 1]:
                    return {
                        "first_fractal": "bottom",
                        "confirm_fractal": "top",
                        "first_fractal_bar": start_idx,
                        "confirm_fractal_bar": confirm_idx,
                        "fractal_sl": float(ch[confirm_i]),
                    }
        return None
    except:
        return None

# ==========================================
# Helper: 计算滚动均线带 (每条K线的六线上下轨)
# ==========================================
def calc_ma_band(closes):
    """返回每条K线的 (上轨, 下轨, 离散%) 数组, 长度同closes, 前120根为NaN"""
    n = len(closes)
    max_band = np.full(n, np.nan)
    min_band = np.full(n, np.nan)
    spread_arr = np.full(n, np.nan)
    if n < 130: return max_band, min_band, spread_arr

    # 用rolling一次性算MA, EMA逐根算
    ma20  = closes.rolling(20).mean().values
    ma60  = closes.rolling(60).mean().values
    ma120 = closes.rolling(120).mean().values
    ema20  = ema(closes, 20).values
    ema60  = ema(closes, 60).values
    ema120 = ema(closes, 120).values

    for i in range(120, n):
        all_mas = [ema20[i], ema60[i], ema120[i], ma20[i], ma60[i], ma120[i]]
        max_band[i] = max(all_mas)
        min_band[i] = min(all_mas)
        spread_arr[i] = (max_band[i] - min_band[i]) / closes.iloc[i] * 100
    return max_band, min_band, spread_arr


# ==========================================
# Mode 3: 均线粘合起爆点扫选 (六线极度收敛→刚突破的瞬间)
# ==========================================
def scan_squeeze_breakout(interval, top_n, squeeze_max=None, lookback=None,
                          breakout_max_bars=None, max_breakout_pct=None, exchange=None):
    # 根据周期自动调参: 大周期=更长回溯+更宽粘合阈值+更宽突破窗口
    if squeeze_max is None:
        # 阈值放宽30%: 原本15m是1.5, 现在2.0。放宽粘合极限，允许更多的币通过
        squeeze_max = get_squeeze_max(interval)
    if lookback is None:
        lookback = get_squeeze_lookback(interval)
    if breakout_max_bars is None:
        breakout_max_bars = get_breakout_confirm_window(interval)
    if max_breakout_pct is None:
        # 放宽30%: 允许更暴力的突破幅度 (原15m为 3.5 -> 4.5)
        max_breakout_pct = {"15m": 4.5, "1h": 6.5, "4h": 10.4, "1d": 13.0, "1w": 13.0}.get(interval, 7.8)

    print(f"\n{'='*85}")
    print(f"  均线粘合起爆点扫选 [{interval}] — 六线极度收敛后的突破瞬间")
    print(f"  粘合阈值: <{squeeze_max}% | 突破窗口: ≤{breakout_max_bars}根 | 最大涨幅限制: ≤{max_breakout_pct}% | 回溯: {lookback}根")
    print(f"{'='*85}")

    symbols, vols = fetch_pairs(exchange=exchange)
    ranked = sorted(symbols, key=lambda s: vols.get(s, 0), reverse=True)
    ranked = [s for s in ranked if vols.get(s, 0) > MIN_VOLUME and s != "USDCUSDT" and not is_tradfi_or_junk(s)]
    print(f"候选: {len(ranked)} 个 (量>{MIN_VOLUME/1e6:.0f}M, 排除稳定币)")
    print(f"扫描中...\n")

    results = []; errors = 0
    for i, sym in enumerate(ranked[:MAX_PAIRS]):
        df = fetch_klines(sym, interval, 200, exchange=exchange)
        if df is None or len(df) < 150: errors += 1; continue
        closes = df["c"]; price = closes.iloc[-1]
        if price < MIN_PRICE: continue

        max_band, min_band, spread_arr = calc_ma_band(closes)
        n = len(closes)
        look_start = max(120, n - lookback)

        # ===== 1. 找最近一段连续粘合期的结束点 =====
        # 从当前K线往回扫，找到最近一次 spread<squeeze_max 的连续区间
        squeeze_end = None   # 粘合期最后一根K线索引
        squeeze_start = None # 粘合期第一根K线索引
        in_squeeze = False
        for j in range(n - 1, look_start - 1, -1):
            sp = spread_arr[j]
            is_tight = not np.isnan(sp) and sp <= squeeze_max
            if is_tight and not in_squeeze:
                squeeze_end = j     # 从后往前，第一次遇到=粘合结束点
                in_squeeze = True
            elif not is_tight and in_squeeze:
                squeeze_start = j + 1  # 粘合开始点(不粘合那根的下一根)
                break
        if in_squeeze and squeeze_start is None:
            squeeze_start = look_start  # 粘合一直延伸到回溯起点

        if squeeze_end is None or squeeze_start is None:
            continue  # 回溯窗口内没有粘合

        squeeze_duration = squeeze_end - squeeze_start + 1
        if squeeze_duration < 5:
            continue  # 粘合时间太短

        # ===== 2. 粘合期内必须在均线带内(价格不能已经突破) =====
        # 取粘合期末端的均线带和价格验证
        inside_count = 0
        for j in range(squeeze_start, squeeze_end + 1):
            if not np.isnan(min_band[j]) and not np.isnan(max_band[j]):
                if min_band[j] <= closes.iloc[j] <= max_band[j]:
                    inside_count += 1
        # 放宽30%: 降低带内驻留比例要求，允许前期稍微上蹿下跳 (原15m 0.70 -> 0.50)
        min_inside_ratio = {"15m": 0.50, "1h": 0.42, "4h": 0.35}.get(interval, 0.28)
        if inside_count < squeeze_duration * min_inside_ratio:
            continue  # 粘合期间价格大部分时间在带外, 属于单边趋势的次级折返，直接过滤

        # ===== 3. 粘合期内的最紧离散度 =====
        squeeze_spreads = spread_arr[squeeze_start:squeeze_end+1]
        valid_sp = squeeze_spreads[~np.isnan(squeeze_spreads)]
        if len(valid_sp) == 0: continue
        min_spread = float(np.nanmin(valid_sp))

        # ===== 4. 找精确突破K线(必须在粘合结束后) =====
        # 完整链路: 先突破密集区, 再等待回踩/反弹, 不要求当前价重新突破。
        breakout_bar = None
        direction = None
        breakout_pct = 0.0
        for j in range(squeeze_end, n):
            if np.isnan(min_band[j]) or np.isnan(max_band[j]):
                continue
            if closes.iloc[j] > max_band[j]:
                direction = "LONG"
                breakout_bar = j
                breakout_pct = (closes.iloc[j] - max_band[j]) / max_band[j] * 100
                break
            elif closes.iloc[j] < min_band[j]:
                direction = "SHORT"
                breakout_bar = j
                breakout_pct = (min_band[j] - closes.iloc[j]) / min_band[j] * 100
                break

        if breakout_bar is None:
            continue  # 没找到有效突破

        band_stop_info = _calc_squeeze_band_stop(
            min_band, max_band, spread_arr, squeeze_start, squeeze_end, direction, n - 1
        )
        if band_stop_info is None:
            continue
        band_sl_price, squeeze_tight_idx, squeeze_edge = band_stop_info

        # ===== 5. 突破幅度过滤 =====
        if breakout_pct > max_breakout_pct:
            continue

        bars_since_breakout = n - 1 - breakout_bar
        if bars_since_breakout > breakout_max_bars:
            continue

        # ===== 5.5 入场价不能悬空太远 =====
        # 分型贴边还不够, 最终成交价也必须靠近均线边缘；防止瀑布后在远离密集区的位置追空/追多。
        current_band_hi = max_band[-1]
        current_band_lo = min_band[-1]
        current_close = float(closes.iloc[-1])
        if np.isnan(current_band_hi) or np.isnan(current_band_lo):
            continue
        entry_float_limit = get_entry_float_limit(interval)
        if direction == "LONG" and current_close > current_band_hi * (1 + entry_float_limit):
            continue
        if direction == "SHORT" and current_close < current_band_lo * (1 - entry_float_limit):
            continue

        # ===== 6. 回踩/反弹期间防线不能被破坏 =====
        defense_broken = False
        for j in range(breakout_bar + 1, n):
            if np.isnan(min_band[j]) or np.isnan(max_band[j]):
                continue
            if direction == "LONG" and closes.iloc[j] < min_band[j] * 0.995:
                defense_broken = True
                break
            if direction == "SHORT" and closes.iloc[j] > max_band[j] * 1.005:
                defense_broken = True
                break
        if defense_broken:
            continue

        # ===== 6.1 核心修复 3：拒绝成熟趋势的底部震荡/顶部盘整 =====
        # 真正的起爆点，突破前短期均线(EMA20)与长期均线(EMA120)应该是刚刚交叉或缠绕，绝不是早已形成单边发散。
        trend_mature_bars = 0
        ema20_arr = ema(closes, 20).values
        ema120_arr = ema(closes, 120).values

        if direction == "LONG":
            # 如果是做多，要求突破前不能是已经涨了很久的形态 (EMA20 > EMA120 持续很久)
            for j in range(breakout_bar - 1, max(0, breakout_bar - 30), -1):
                if ema20_arr[j] > ema120_arr[j]:
                    trend_mature_bars += 1
                else:
                    break
        else:
            # 如果是做空，要求突破前不能是已经跌了很久的形态 (EMA20 < EMA120 持续很久)
            for j in range(breakout_bar - 1, max(0, breakout_bar - 30), -1):
                if ema20_arr[j] < ema120_arr[j]:
                    trend_mature_bars += 1
                else:
                    break

        # 放宽30%: 允许趋势稍微成熟一点再入场，捕捉趋势中继 (原 15根 -> 20根)
        if trend_mature_bars >= 20:
            continue

        # ===== 6.5 回踩/反弹确认 + 分型检测 =====
        retest_confirmed = False
        retest_type = ""
        structure_seq = None
        retest_j = -1
        fractal_bonus = 0  # 分型额外加分
        fractal_sl_price = None  # 分型止损价, 传递给交易引擎
        if direction == "LONG":
            # 核心修复 1：回踩循环必须从 breakout_bar + 1 开始，杜绝把突破K线当回踩
            for j in range(breakout_bar + 1, n):
                band_hi_j = max_band[j]; band_lo_j = min_band[j]
                if np.isnan(band_hi_j) or np.isnan(band_lo_j): continue
                if df["l"].iloc[j] <= band_hi_j * 1.015 and closes.iloc[j] >= band_lo_j * 0.995:
                    retest_confirmed = True
                    retest_j = j
            if retest_confirmed:
                retest_type = "回踩确认"

            # 回踩处底分型
            if n - breakout_bar >= 3:
                seq_from = max(0, breakout_bar - 1)
                min_confirm_idx = max(
                    breakout_bar + 1,
                    (retest_j - 1) if retest_j > 0 else breakout_bar + 1
                )
                structure_seq = _find_fractal_structure_sequence(
                    df["h"].values[seq_from:n],
                    df["l"].values[seq_from:n],
                    df["c"].values[seq_from:n],
                    direction,
                    offset=seq_from,
                    min_start_idx=breakout_bar,
                    min_confirm_idx=min_confirm_idx,
                )

                if structure_seq is not None and retest_confirmed:
                    retest_type = "回踩+底分型"
                    fractal_bonus = 10
                    fractal_sl_price = structure_seq["fractal_sl"]

            if not retest_confirmed:
                all_above = all(closes.iloc[j] > max_band[j] for j in range(breakout_bar, n) if not np.isnan(max_band[j]))
                if all_above and bars_since_breakout >= 1:
                    retest_confirmed = True
                    retest_type = "突破横盘"
                else:
                    # 核心修复：刚刚突破还没回踩，打上特殊标记进入候选池盯防
                    retest_type = "等待回踩"

        else:
            # 核心修复 1：回踩循环必须从 breakout_bar + 1 开始
            for j in range(breakout_bar + 1, n):
                band_hi_j = max_band[j]; band_lo_j = min_band[j]
                if np.isnan(band_hi_j) or np.isnan(band_lo_j): continue
                if df["h"].iloc[j] >= band_lo_j * 0.985 and closes.iloc[j] <= band_hi_j * 1.005:
                    retest_confirmed = True
                    retest_j = j
            if retest_confirmed:
                retest_type = "反弹确认"

            if n - breakout_bar >= 3:
                seq_from = max(0, breakout_bar - 1)
                min_confirm_idx = max(
                    breakout_bar + 1,
                    (retest_j - 1) if retest_j > 0 else breakout_bar + 1
                )
                structure_seq = _find_fractal_structure_sequence(
                    df["h"].values[seq_from:n],
                    df["l"].values[seq_from:n],
                    df["c"].values[seq_from:n],
                    direction,
                    offset=seq_from,
                    min_start_idx=breakout_bar,
                    min_confirm_idx=min_confirm_idx,
                )

                if structure_seq is not None and retest_confirmed:
                    retest_type = "反弹+顶分型"
                    fractal_bonus = 10
                    fractal_sl_price = structure_seq["fractal_sl"]

            if not retest_confirmed:
                all_below = all(closes.iloc[j] < min_band[j] for j in range(breakout_bar, n) if not np.isnan(min_band[j]))
                if all_below and bars_since_breakout >= 1:
                    retest_confirmed = True
                    retest_type = "跌破横盘"
                else:
                    # 核心修复：刚刚突破还没反弹，打上特殊标记进入候选池盯防
                    retest_type = "等待反弹"

                # ===== 7. 量能 =====
        vol_recent = df["v"].iloc[-3:].mean()
        vol_avg = df["v"].iloc[max(0,n-30):max(0,n-5)].mean()
        if vol_avg <= 0: vol_avg = vol_recent
        vol_surge = vol_recent / vol_avg

        # ===== 8. 量能硬过滤: 缩量严重=假突破 =====
        # 放宽30%: 允许量能相对较弱的突破通过 (原 0.6 -> 0.42)
        if vol_surge < 0.42:
            continue  # 量能不足均量42%, 排除

        # ===== 9. 更高时间框架趋势确认 =====
        ht_interval = {"1h": "4h", "4h": "1d", "1d": "1w", "15m": "1h"}.get(interval, "4h")
        ht_df = fetch_klines(sym, ht_interval, 80, exchange=exchange)
        ht_aligned = None  # None=无法判断, True=顺势, False=逆势
        ht_trend_desc = "--"
        if ht_df is not None and len(ht_df) >= 30:
            ht_closes = ht_df["c"]
            ht_ema20 = ema(ht_closes, 20).iloc[-1]
            ht_price = ht_closes.iloc[-1]
            if direction == "LONG":
                ht_aligned = ht_price > ht_ema20
                ht_trend_desc = f"{ht_interval}多头" if ht_aligned else f"{ht_interval}空头"
            else:
                ht_aligned = ht_price < ht_ema20
                ht_trend_desc = f"{ht_interval}空头" if ht_aligned else f"{ht_interval}多头"

        # ===== 9.5 大周期分型结构确认（标准3-K线共振判断方向） =====
        ht_fractal_score = 0
        ht_fractal_desc = "--"
        if ht_df is not None and len(ht_df) >= 30:
            ht_closes_arr = ht_df["c"].values
            ht_highs = ht_df["h"].values
            ht_lows = ht_df["l"].values
            ch, cl, cc, _ = _clean_kline_window(ht_highs, ht_lows, ht_closes_arr)
            cm = len(ch)

            # 根据当前触发的扫描周期，动态自适应大周期的分型回溯根数
            ht_lookback_map = {
                "15m": 16,  # 15m看1h图：回溯16根 (约大半天的日内大势)
                "1h": 12,   # 1h看4h图：回溯12根 (过去2天的中短期大势)
                "4h": 8,    # 4h看日线图：回溯8根 (过去一周多的波段大势)
                "1d": 5     # 日线看周线图：回溯5根 (过去一个多月的宏观大势)
            }
            target_lookback = ht_lookback_map.get(interval, 10)

            # 逆向回溯大周期K线，寻找最新形成的标准分型形态
            lookback_ht = min(target_lookback, cm - 2)
            found_ht_frac = None

            for i in range(cm - 2, cm - lookback_ht - 1, -1):
                # 检查标准底分型：中间K线的最低点比左右都低
                if cl[i] < cl[i-1] and cl[i] < cl[i+1]:
                    found_ht_frac = "BOTTOM"
                    break
                # 检查标准顶分型：中间K线的最高点比左右都高
                if ch[i] > ch[i-1] and ch[i] > ch[i+1]:
                    found_ht_frac = "TOP"
                    break

            # 根据大周期最新的分型形态判定是否产生共振
            if direction == "LONG" and found_ht_frac == "BOTTOM":
                ht_fractal_score = 20
                ht_fractal_desc = f"{ht_interval}底分型共振"
            elif direction == "SHORT" and found_ht_frac == "TOP":
                ht_fractal_score = 20
                ht_fractal_desc = f"{ht_interval}顶分型共振"
            elif direction == "LONG" and found_ht_frac == "TOP":
                ht_fractal_score = -30
                ht_fractal_desc = f"{ht_interval}顶分型压制"
            elif direction == "SHORT" and found_ht_frac == "BOTTOM":
                ht_fractal_score = -30
                ht_fractal_desc = f"{ht_interval}底分型压制"

        # ===== 10. K线实体强度 =====
        last_open = df["o"].iloc[-1]
        last_close = df["c"].iloc[-1]
        body_pct = abs(last_close - last_open) / last_open * 100
        is_strong_body = body_pct > 0.3

        # ===== 综合评分 =====
        # 1. 粘合紧度 (0-30分): 越紧=弹簧压得越狠
        tight_score = max(0, (squeeze_max - min_spread) / squeeze_max * 30)

        # 2. 粘合持续时间 (0-12分): 横盘越久=蓄力越足
        duration_score = min(squeeze_duration / 4, 12)

        # 3. 起爆新鲜度 (0-22分): 0-1根最加分, 越旧越少
        fresh_score = {0: 22, 1: 18, 2: 10, 3: 4}.get(bars_since_breakout, 0)

        # 4. 回踩确认 (0-35分): 分型=35, 回踩确认=25, 等待回踩=15, 横盘=12
        if "分型" in retest_type:
            retest_score = 35  # 回踩+分型: 最强信号
        elif "确认" in retest_type:  # "回踩确认" / "反弹确认"
            retest_score = 25
        elif "等待" in retest_type:  # 核心修复：给"等待回踩/反弹"基础分，过及格线进池子
            retest_score = 15
        elif "横盘" in retest_type:
            retest_score = 12
        else:
            retest_score = 0

        # 5. 突破幅度黄金区间 (0-10分): 1-5%最好
        if 1.0 <= breakout_pct <= 5.0:
            strength_score = 10
        elif 0.3 <= breakout_pct < 1.0:
            strength_score = 7
        elif breakout_pct < 0.3:
            strength_score = 2
        else:
            strength_score = max(0, 10 - (breakout_pct - 5) * 2)

        # 5. 量能确认 (0-8分): 放量>1x加分, 缩量扣分
        if vol_surge >= 2.0:
            vol_score = 8       # 放量2倍以上
        elif vol_surge >= 1.5:
            vol_score = 6       # 显著放量
        elif vol_surge >= 1.0:
            vol_score = 3       # 微放量
        else:
            vol_score = 0       # 量平平, 不扣分但也不加分

        # 6. 大周期趋势共振 (0-12分): 顺势满分, 逆势扣分
        if ht_aligned is True:
            ht_score = 12       # 顺势: 最强加分
        elif ht_aligned is False:
            ht_score = -8       # 逆势: 扣分但不排除
        else:
            ht_score = 0        # 无法判断

        total_score = tight_score + duration_score + fresh_score + retest_score + strength_score + vol_score + ht_score + ht_fractal_score

        # 起爆K线必须有力(实体太小=假突破)
        if not is_strong_body and bars_since_breakout <= 1:
            total_score *= 0.5

        # 粘合无缝突破额外加分
        gap = breakout_bar - squeeze_end
        if gap <= 0:
            total_score += 5

        results.append({
            "symbol": sym,
            "direction": direction,
            "price": price,
            "min_spread": round(min_spread, 2),
            "current_spread": round(spread_arr[-1], 2),
            "breakout_pct": round(breakout_pct, 2),
            "bars_since": bars_since_breakout,
            "squeeze_bars": squeeze_duration,
            "retest": retest_type,
            "fractal_sl": fractal_sl_price,  # 分型止损价, 传给交易引擎直接使用
            "band_sl": band_sl_price,        # 确认/入场K线六线边缘止损
            "breakout_bar": breakout_bar,
            "retest_bar": retest_j,
            "squeeze_start": squeeze_start,
            "squeeze_end": squeeze_end,
            "squeeze_tight_idx": squeeze_tight_idx,
            "squeeze_edge": round(squeeze_edge, 8),
            "first_fractal": structure_seq.get("first_fractal") if structure_seq else "",
            "confirm_fractal": structure_seq.get("confirm_fractal") if structure_seq else "",
            "first_fractal_bar": structure_seq.get("first_fractal_bar") if structure_seq else None,
            "confirm_fractal_bar": structure_seq.get("confirm_fractal_bar") if structure_seq else None,
            "vol24m": vols.get(sym, 0) / 1e6,
            "vol_surge": round(vol_surge, 1),
            "body_pct": round(body_pct, 2),
            "ht_trend": ht_trend_desc,
            "ht_fractal": ht_fractal_desc,
            "score": round(total_score, 1),
            "tight": round(tight_score, 1),
            "duration": round(duration_score, 1),
            "fresh": fresh_score,
            "retest_score": retest_score,
            "strength": round(strength_score, 1),
            "vol_score": round(vol_score, 1),
            "ht_score": ht_score,
            "ht_frac": ht_fractal_score,
            "ht_fractal": ht_fractal_desc,
        })

        if (i + 1) % 30 == 0:
            sys.stdout.write(f"\r  已扫: {i+1}/{min(len(ranked),MAX_PAIRS)} ..."); sys.stdout.flush()

    print(f"\r  完成: {len(results)} 个起爆点候选")

    # 分开做多/做空, 各排各的
    longs = sorted([r for r in results if r["direction"] == "LONG"], key=lambda r: r["score"], reverse=True)
    shorts = sorted([r for r in results if r["direction"] == "SHORT"], key=lambda r: r["score"], reverse=True)

    def print_table(rows, label, emoji):
        print(f"\n  {'─'*85}")
        try: print(f"  {emoji} {label} (Top {top_n})")
        except UnicodeEncodeError: print(f"  {label} (Top {top_n})")
        print(f"  {'─'*85}")
        print(f"  {'#':3s} {'交易对':12s} {'评分':>5s} {'最紧粘合':>7s} {'突破%':>7s} {'K线':>4s} {'横盘':>4s} {'放量':>4s} {'大周期':>6s} {'实体%':>5s} {'价格':>10s}")
        print(f"  {'-'*88}")
        for i, r in enumerate(rows[:top_n]):
            star = "🔥" if r["score"] >= 70 else "⚡" if r["score"] >= 50 else "💡" if r["score"] >= 35 else "  "
            try: sfx = star
            except: sfx = "***" if r["score"] >= 65 else "** " if r["score"] >= 45 else "*  "
            print(f"  {i+1:<3d} {sfx} {r['symbol']:12s} {r['score']:>4.1f} {r['min_spread']:>6.2f}% {r['breakout_pct']:>+6.2f}% {r['bars_since']:>3d}根 {r['squeeze_bars']:>3d}根 {r['vol_surge']:>3.1f}x {r.get('ht_trend','--'):>6s} {r['body_pct']:>4.2f}% {r['price']:>10.4f}")
        top5 = [r["symbol"] for r in rows[:5]]
        if top5: print(f"\n    起爆盯盘: {', '.join(top5)}")

    print_table(longs, "做多起爆点 (粘合后刚突破向上)", "📈")
    print_table(shorts, "做空起爆点 (粘合后刚突破向下)", "📉")

    print(f"\n  {'='*85}")
    print(f"  评分: 紧度(0-30) + 横盘(0-12) + 新鲜(0-22) + 回踩(0-25) + 突破(0-10) + 量能(0-8) + 趋势(±12)")
    print(f"  🔥 >75分  ⚡ >55分  💡 >38分")
    print(f"  硬过滤: 量<均量60%排除 | 大周期趋势: 顺势+12 逆势-8")
    print(f"  起爆点定义: 六线粘合<{squeeze_max}%≥5根→突破≤{breakout_max_bars}根→涨幅<{max_breakout_pct}%")
    print(f"  总候选: {len(results)} (做多{len(longs)} + 做空{len(shorts)})")
    print(f"{'='*85}\n")
    return results


# ==========================================
# Mode 4: 低市值高换手扫选
# ==========================================
def scan_volume_anomaly(interval, top_n, max_price):
    print(f"\n{'='*85}")
    print(f"  低市值高换手扫选 [{interval}] — 价格<{max_price}U 但成交量媲美主流")
    print(f"  排除BTC/ETH/BNB/SOL等蓝筹, 专找交易活跃的小市值代币")
    print(f"{'='*85}")

    symbols, vols = fetch_pairs()
    ranked = sorted(symbols, key=lambda s: vols.get(s, 0), reverse=True)
    candidates = [s for s in ranked if vols.get(s, 0) > MIN_VOLUME*5 and s not in BLUE_CHIPS and not is_tradfi_or_junk(s)]
    print(f"候选: {len(candidates)} 个 (量>{MIN_VOLUME*5/1e6:.0f}M, 排除蓝筹)")
    print(f"扫描中...\n")

    results = []
    for i, sym in enumerate(candidates[:80]):
        df = fetch_klines(sym, interval, 100)
        if df is None or len(df) < 50: continue
        price = df["c"].iloc[-1]
        if price > max_price: continue

        vol_usdt = vols.get(sym, 0)
        # 换手强度: 24h成交量 / 价格 = 每单位价格的交易活跃度
        turnover = vol_usdt / price

        # 均线状态(用简化算法, 因只取了100根K线)
        closes = df["c"]
        n_ok = len(closes)
        if n_ok >= 80:
            ema_arr = [ema(closes, p).iloc[-1] for p in [20,60,120]]
            ma_arr  = [closes.rolling(p).mean().iloc[-1] for p in [20,60,120]]
            all_mas = ema_arr + ma_arr
            spread = (max(all_mas) - min(all_mas)) / closes.iloc[-1] * 100
            max_m = max(all_mas)
            min_m = min(all_mas)
            inside = min_m <= price <= max_m
            trend = "粘合偏多" if inside and price > ema_arr[0] else ("粘合偏空" if inside else ("突破上轨" if price > max_m else "跌破下轨"))
        else:
            spread = 99
            inside = False
            trend = "--"

        results.append({
            "symbol": sym,
            "price": price,
            "vol24m": round(vol_usdt / 1e6, 1),
            "turnover": round(turnover / 1e6, 2),  # 百万单位
            "spread": round(spread, 2),
            "trend": trend,
            "inside": inside,
        })

    results.sort(key=lambda r: r["turnover"], reverse=True)

    print(f"\r  完成: {len(results)} 个低价高换手币")

    print(f"\n{'='*90}")
    print(f"  Top {top_n} 低市值高换手 ({interval}, 价<{max_price}U)")
    print(f"{'='*90}")
    print(f"  {'#':3s} {'交易对':12s} {'换手强度':>8s} {'价格':>8s} {'24h量M':>8s} {'离散%':>6s} {'趋势':10s}")
    print(f"  {'-'*75}")

    for i, r in enumerate(results[:top_n]):
        star = "***" if r["turnover"] > 50 else "** " if r["turnover"] > 20 else "*  " if r["turnover"] > 10 else "   "
        print(f"  {i+1:<3d} {star} {r['symbol']:12s} {r['turnover']:>7.1f}M {r['price']:>8.4f} {r['vol24m']:>7.1f}M {r['spread']:>5.2f}% {r['trend']:10s}")

    top5 = [(r["symbol"], r["price"]) for r in results[:5]]
    print(f"\n  {'='*90}")
    print(f"  *** >50M  ** >20M  *  >10M (换手强度 = 24h量/价格)")
    if top5: print(f"  重点盯盘: {', '.join(f'{s}({p})' for s,p in top5)}")
    print(f"{'='*90}\n")
    return results


# ==========================================
# CLI
# ==========================================
def verify_pool_signal_details(df, direction, interval="15m"):
    """池子重扫/下单前复核：返回突破后分型止损与确认/入场K线六线边缘止损。"""
    try:
        closes = df["c"]
        highs = df["h"].values
        lows = df["l"].values
        max_band, min_band, spreads = calc_ma_band(closes)
        n = len(closes)
        if n < 130: return None

        defense_lookback = {"15m": 10, "1h": 6, "4h": 4, "1d": 3, "1w": 3}.get(interval, 8)
        lookback = min(defense_lookback, n - 1)

        # 1. 必须先有过有效突破；之后才允许等待回踩/反弹分型。
        breakout_window = get_breakout_confirm_window(interval)
        breakout_start = max(120, n - breakout_window - 1)
        breakout_bar = None
        for j in range(breakout_start, n):
            if np.isnan(min_band[j]) or np.isnan(max_band[j]):
                continue
            if direction == "LONG" and closes.iloc[j] > max_band[j]:
                breakout_bar = j
                break
            if direction == "SHORT" and closes.iloc[j] < min_band[j]:
                breakout_bar = j
                break
        if breakout_bar is None:
            return None

        squeeze_zone = _find_squeeze_zone_before_breakout(spreads, breakout_bar, interval)
        if squeeze_zone is None:
            return None
        squeeze_start, squeeze_end = squeeze_zone
        band_stop_info = _calc_squeeze_band_stop(
            min_band, max_band, spreads, squeeze_start, squeeze_end, direction, n - 1
        )
        if band_stop_info is None:
            return None
        band_sl, squeeze_tight_idx, squeeze_edge = band_stop_info

        # 2. 回踩/反弹期间防线不能被破坏。LONG不破下轨, SHORT不破上轨。
        for j in range(breakout_bar + 1, n):
            if np.isnan(min_band[j]) or np.isnan(max_band[j]):
                continue
            if direction == "LONG" and closes.iloc[j] < min_band[j] * 0.995:
                return None
            if direction == "SHORT" and closes.iloc[j] > max_band[j] * 1.005:
                return None

        # 3. 必须是突破之后才出现回踩/反弹确认。
        #    旧逻辑会在“刚穿越带边”时直接拿突破前的旧分型开仓，
        #    导致未完成完整链路就进场。
        retest_confirmed = False
        retest_j = None
        for j in range(breakout_bar + 1, n):
            if np.isnan(min_band[j]) or np.isnan(max_band[j]):
                continue
            if direction == "LONG":
                if lows[j] <= max_band[j] * 1.015 and closes.iloc[j] >= min_band[j] * 0.995:
                    retest_confirmed = True
                    retest_j = j
                    break
            else:
                if highs[j] >= min_band[j] * 0.985 and closes.iloc[j] <= max_band[j] * 1.005:
                    retest_confirmed = True
                    retest_j = j
                    break
        if not retest_confirmed:
            return None

        # 4. 结构必须完整: LONG=突破后顶分型→底分型; SHORT=跌破后底分型→顶分型。
        #    最后的确认分型仍必须是最新分型, 不能借旧分型。
        seq_start = max(0, breakout_bar - 1)
        min_confirm_idx = max(
            breakout_bar + 1,
            (retest_j - 1) if retest_j is not None else breakout_bar + 1,
            n - lookback
        )
        if n - seq_start < 4:
            return None
        structure_seq = _find_fractal_structure_sequence(
            highs[seq_start:],
            lows[seq_start:],
            closes.values[seq_start:],
            direction,
            offset=seq_start,
            min_start_idx=breakout_bar,
            min_confirm_idx=min_confirm_idx,
        )
        if structure_seq is None:
            return None
        fractal_sl = structure_seq["fractal_sl"]

        # 5. 分型不能破防线, 且不能离当前均线边缘太远。
        current_band_hi = max_band[-1]
        current_band_lo = min_band[-1]
        if np.isnan(current_band_hi) or np.isnan(current_band_lo):
            return None

        current_close = float(closes.iloc[-1])
        entry_float_limit = get_entry_float_limit(interval)
        if direction == "LONG" and current_close > current_band_hi * (1 + entry_float_limit):
            return None
        if direction == "SHORT" and current_close < current_band_lo * (1 - entry_float_limit):
            return None

        susp_limit = get_fractal_float_limit(interval)

        if direction == "LONG":
            if fractal_sl < current_band_lo * 0.995:
                return None
            if fractal_sl > current_band_hi * (1 + susp_limit):
                return None
        else:
            if fractal_sl > current_band_hi * 1.005:
                return None
            if fractal_sl < current_band_lo * (1 - susp_limit):
                return None

        return {
            "fractal_sl": fractal_sl,
            "band_sl": band_sl,
            "breakout_bar": breakout_bar,
            "retest_bar": retest_j,
            "squeeze_start": squeeze_start,
            "squeeze_end": squeeze_end,
            "squeeze_tight_idx": squeeze_tight_idx,
            "squeeze_edge": squeeze_edge,
            "first_fractal": structure_seq.get("first_fractal"),
            "confirm_fractal": structure_seq.get("confirm_fractal"),
            "first_fractal_bar": structure_seq.get("first_fractal_bar"),
            "confirm_fractal_bar": structure_seq.get("confirm_fractal_bar"),
        }
    except:
        return None

def verify_pool_signal(df, direction, interval="15m"):
    """兼容旧调用：只返回突破后确认分型止损。"""
    details = verify_pool_signal_details(df, direction, interval=interval)
    return details.get("fractal_sl") if details else None


def _find_fractal_sl_in_window(highs, lows, closes, direction):
    """只找最新形成的标准形态分型，杜绝历史陈旧分型导致提前开仓"""
    try:
        ch, cl, cc, _ = _clean_kline_window(highs, lows, closes)
        m = len(ch)
        if m < 3: return None

        if direction == "LONG":
            # 核心修复：分型的右肩必须是倒数第1根或第2根K线，保证是刚刚形成的
            for i in [m-2, m-3]:
                if i > 0 and cl[i] < cl[i-1] and cl[i] < cl[i+1]:
                    return float(cl[i])
            return None
        else:
            for i in [m-2, m-3]:
                if i > 0 and ch[i] > ch[i-1] and ch[i] > ch[i+1]:
                    return float(ch[i])
            return None
    except:
        return None


if __name__ == "__main__":
    # Windows 终端兼容 emoji
    try: sys.stdout.reconfigure(encoding='utf-8')
    except: pass
    args = sys.argv[1:]
    interval = "4h"
    top_n = 15
    mode = "squeeze"      # squeeze / short / volume / breakout
    change_days = 3
    max_chg = 999.0
    min_chg = -99.0
    max_price = 0
    squeeze_max = None    # None=自动按周期调参
    use_chg = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--short": mode = "short"
        elif a == "--breakout": mode = "breakout"
        elif a == "--volume": mode = "volume"
        elif a == "--1h": interval = "1h"
        elif a == "--4h": interval = "4h"
        elif a == "--top" and i+1 < len(args): i += 1; top_n = int(args[i])
        elif a == "--max-price" and i+1 < len(args): i += 1; max_price = float(args[i])
        elif a == "--squeeze-max" and i+1 < len(args): i += 1; squeeze_max = float(args[i])
        elif a == "--days" and i+1 < len(args): i += 1; change_days = int(args[i])
        elif a == "--max-chg" and i+1 < len(args): i += 1; max_chg = float(args[i]); use_chg = True
        elif a == "--min-chg" and i+1 < len(args): i += 1; min_chg = float(args[i]); use_chg = True
        i += 1

    if mode == "short":
        if min_chg == -99.0: min_chg = 50.0
        if change_days == 3: change_days = 7
        scan_short(interval, top_n, change_days, min_chg)
    elif mode == "volume":
        if max_price == 0: max_price = 5.0   # 默认5U以下
        scan_volume_anomaly(interval, top_n, max_price)
    elif mode == "breakout":
        scan_squeeze_breakout(interval, top_n, squeeze_max)
    else:
        scan_squeeze(interval, top_n, change_days, max_chg, min_chg, use_chg)
