"""
AXIOM QUANT - Institutional Crypto Quant Terminal
Version: 1.0.0 (Titanium Build)
启动: python web_ui.py  -> 浏览器 http://127.0.0.1:5000
"""
from flask import Flask, render_template_string, jsonify, request, session, send_from_directory
import re
import threading, time, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from functools import wraps
import account_manager as accounts
from screener import fetch_pairs, fetch_klines, ema, calc_ma_band, MIN_VOLUME, BLUE_CHIPS, STABLECOINS
from rj_indicator import compute_rj_bbkd, RJParams
import numpy as np
from trader import TradeConfig, SqueezeBreakoutBot
import os as _os
from pathlib import Path
from momentum_reflow import scan_momentum_reflow
from momentum_reflow_dashboard import (
    load_reflow_dashboard,
    load_reflow_settings,
    merge_reflow_signals,
    next_reflow_scan_at,
)
from momentum_reflow_alerts import (
    WEBHOOK_PREFIX,
    deliver_due_wechat,
    observe_reflow_alerts,
    read_public_alerts,
)

# 交易引擎实例 (全局单例)
_BASE_DIR = _os.path.dirname(_os.path.abspath(__file__))
MOMENTUM_REFLOW_LEDGER = Path(__file__).with_name("momentum_reflow_state.json")
MOMENTUM_REFLOW_SETTINGS = Path(_BASE_DIR) / "momentum_reflow_settings.json"
MOMENTUM_REFLOW_HISTORY = Path(_BASE_DIR) / "momentum_reflow_daily_signals.json"
MOMENTUM_REFLOW_ALERT_SETTINGS = Path(_BASE_DIR) / "momentum_reflow_alert_settings.json"
MOMENTUM_REFLOW_ALERT_LEDGER = Path(_BASE_DIR) / "momentum_reflow_alerts.json"

# 数据回测展示配置 (管理员后台设置, 用户只读)
_demo_cfg_path = _os.path.join(_BASE_DIR, 'demo_config.json')
import json as _json
if _os.path.exists(_demo_cfg_path):
    with open(_demo_cfg_path) as _f: demo_display_cfg = _json.load(_f)
else:
    demo_display_cfg = {"cfg_maxval":5000,"cfg_risk":200,"cfg_atr_mult":3,"cfg_maxpos":10,"cfg_interval":"30m"}
    with open(_demo_cfg_path,'w') as _f: _json.dump(demo_display_cfg, _f)

# ═══════════════════════════════════════════
# 多用户引擎管理器
# ═══════════════════════════════════════════
class UserBotManager:
    def __init__(self):
        self._bots = {}       # user_id -> SqueezeBreakoutBot
        self._configs = {}    # user_id -> TradeConfig
        self._demo_bot = None # 共享演示引擎 (id=0)

    def _normalize_demo_config(self, cfg):
        """Demo engine is allowed to place real orders only on exchange testnet."""
        cfg.testnet = True
        cfg.fuel_enabled = False
        cfg.mode = "live"
        cfg.rj_daily_pattern_filter_mode = "log_only"
        return cfg

    def _user_path(self, uid, filename):
        return _os.path.join(_BASE_DIR, f'{filename}_{uid}.json')

    def get_bot(self, uid=None):
        """获取用户专属引擎, uid=None返回演示引擎"""
        if uid is None or uid == 0:
            if self._demo_bot is None:
                cfg = TradeConfig()
                cfg_path = _os.path.join(_BASE_DIR, 'demo_bot_config.json')
                if _os.path.exists(cfg_path): cfg = TradeConfig.load(cfg_path)
                cfg = self._normalize_demo_config(cfg)
                self._configs[0] = cfg
                self._demo_bot = SqueezeBreakoutBot(cfg)
                self._demo_bot._positions_path = _os.path.join(_BASE_DIR, 'positions_<uid>.json')
                self._demo_bot._trade_log_path = _os.path.join(_BASE_DIR, 'trades_<uid>.jsonl')
                self._demo_bot._signal_log_path = _os.path.join(_BASE_DIR, 'signal_events_0.jsonl')
                self._demo_bot._rj_watchlist_path = _os.path.join(_BASE_DIR, 'rj_watchlist_0.json')
                # 重新从独立文件加载 (覆盖__init__中误读的默认文件)
                self._demo_bot.trade_log = []
                self._demo_bot._load_trade_history()
                self._demo_bot.positions = self._demo_bot._load_positions()
                self._demo_bot.setup_instance_log(0)
                self._demo_bot.start()
            return self._demo_bot
        uid = int(uid)
        if uid not in self._bots:
            # 从数据库加载用户配置, 或使用默认
            cfg = TradeConfig()
            user_prefs = accounts.load_user_config(uid)
            for k, v in user_prefs.items():
                if hasattr(cfg, k): setattr(cfg, k, v)
            cfg.mode = "live" if (cfg.api_key or cfg.testnet_api_key or cfg.bitget_api_key) else "paper"
            self._configs[uid] = cfg
            bot = SqueezeBreakoutBot(cfg)
            # 设定用户独立文件路径
            bot._positions_path = _os.path.join(_BASE_DIR, f'positions_{uid}.json')
            bot._trade_log_path = _os.path.join(_BASE_DIR, f'trades_{uid}.jsonl')
            bot._signal_log_path = _os.path.join(_BASE_DIR, f'signal_events_{uid}.jsonl')
            bot._rj_watchlist_path = _os.path.join(_BASE_DIR, f'rj_watchlist_{uid}.json')
            # 重新从用户独立文件加载 (覆盖__init__中误读的默认trades.jsonl)
            bot.trade_log = []
            bot._load_trade_history()
            bot.positions = bot._load_positions()
            bot._user_id = uid
            bot.setup_instance_log(uid)
            self._bots[uid] = bot
        return self._bots[uid]

    def _save_runtime_enabled(self, uid, enabled: bool):
        """持久化用户自动交易开关, 让服务重启后能恢复实盘引擎。"""
        if uid is None or int(uid) == 0:
            return
        uid = int(uid)
        cfg = self.get_user_config(uid)
        cfg.enabled = bool(enabled)
        self._configs[uid] = cfg
        accounts.save_user_config(uid, {k: v for k, v in cfg.__dict__.items() if not k.startswith('_')})

    def _has_local_positions(self, uid) -> bool:
        path = self._user_path(int(uid), 'positions')
        try:
            if not _os.path.exists(path) or _os.path.getsize(path) <= 2:
                return False
            with open(path) as f:
                data = _json.load(f)
            return bool(data)
        except Exception:
            return False

    def start_user_bot(self, uid):
        if uid is not None:
            uid = int(uid)
        bot = self.get_bot(uid)
        if uid is not None and int(uid) != 0:
            self._save_runtime_enabled(uid, True)
            bot.cfg.enabled = True
        if not bot.running: bot.start()
        return bot

    def stop_user_bot(self, uid):
        if uid is not None:
            uid = int(uid)
        if uid == 0 and self._demo_bot:
            self._demo_bot.stop()
        elif uid in self._bots:
            self._bots[uid].stop()
        if uid is not None and int(uid) != 0:
            self._save_runtime_enabled(uid, False)

    def auto_start_enabled_user_bots(self):
        """服务启动后恢复所有已开启自动交易的用户引擎。"""
        started = 0
        for u in accounts.admin_get_all_users():
            try:
                uid = int(u.get('id', 0))
                if uid <= 0 or not u.get('active'):
                    continue
                cfg_data = accounts.load_user_config(uid)
                should_resume = bool(cfg_data.get('enabled', False)) or self._has_local_positions(uid)
                if not should_resume:
                    continue
                cfg = TradeConfig()
                for k, v in cfg_data.items():
                    if hasattr(cfg, k): setattr(cfg, k, v)
                has_api = bool(cfg.api_key or cfg.testnet_api_key or cfg.bitget_api_key)
                valid, _, _ = accounts.check_license_valid(uid)
                if not has_api or not valid:
                    continue
                self.start_user_bot(uid)
                started += 1
            except Exception as e:
                print(f"[自动恢复] 用户{u.get('id')}引擎启动失败: {e}")
        if started:
            print(f"[自动恢复] 已启动 {started} 个用户自动交易引擎")

    def save_user_config(self, uid, data: dict):
        cfg = self._configs.get(uid)
        if cfg is None:
            # 加载已有配置, 保护API密钥不丢失
            if uid == 0:
                cfg_path = _os.path.join(_BASE_DIR, 'demo_bot_config.json')
                cfg = TradeConfig.load(cfg_path) if _os.path.exists(cfg_path) else TradeConfig()
            else:
                cfg = TradeConfig()
                saved = accounts.load_user_config(uid)
                for k, v in saved.items():
                    if hasattr(cfg, k): setattr(cfg, k, v)
            self._configs[uid] = cfg
        # 保护密钥: 如果前端传来空值且已有密钥, 保留旧值
        secret_fields = ['api_key','api_secret','testnet_api_key','testnet_api_secret',
                        'bitget_api_key','bitget_api_secret','bitget_api_pass']
        for k, v in data.items():
            if hasattr(cfg, k):
                if k in secret_fields and ((not v) or str(v).startswith("****")) and getattr(cfg, k, ''):
                    continue  # 跳过空值, 保留已有密钥
                setattr(cfg, k, v)
        if uid != 0:
            cfg.mode = "live" if (cfg.api_key or cfg.testnet_api_key or cfg.bitget_api_key) else "paper"
        if uid == 0:
            cfg = self._normalize_demo_config(cfg)
        # 持久化: 演示引擎存文件, 用户存数据库
        if uid == 0:
            cfg.save(_os.path.join(_BASE_DIR, 'demo_bot_config.json'))
        else:
            accounts.save_user_config(uid, {k: v for k, v in cfg.__dict__.items() if not k.startswith('_')})
        # 更新运行中的bot: 演示引擎用_demo_bot, 用户用_bots[uid]
        bot = self._demo_bot if uid == 0 else self._bots.get(uid)
        if bot:
            bot.cfg = cfg
            bot._init_client()
            if bot.client is not None:
                bot._restore_stop_ids()
            if uid == 0 and bot.client is not None:
                try:
                    bot._sync_positions()
                except Exception as e:
                    try:
                        import logging
                        logging.getLogger("trader").warning(f"演示引擎热更新后同步交易所持仓失败: {e}")
                    except:
                        pass
            try:
                import logging
                logging.getLogger("trader").info(f"用户{uid}配置已更新, 客户端已刷新")
            except: pass

    def get_user_config(self, uid):
        cfg = self._configs.get(uid)
        if cfg is None:
            if uid == 0:
                cfg_path = _os.path.join(_BASE_DIR, 'demo_bot_config.json')
                cfg = TradeConfig.load(cfg_path) if _os.path.exists(cfg_path) else TradeConfig()
            else:
                user_prefs = accounts.load_user_config(uid)
                cfg = TradeConfig()
                for k, v in user_prefs.items():
                    if hasattr(cfg, k): setattr(cfg, k, v)
            self._configs[uid] = cfg
        return cfg

bot_manager = UserBotManager()

def _current_bot():
    """获取当前登录用户的引擎"""
    uid = session.get('user_id')
    return bot_manager.get_bot(uid)

# 扫描数据源跟随
import screener
screener.set_testnet_mode(False)  # 扫描始终主网


# 启动演示引擎(数据回测面板用)
bot_manager.get_bot(0)  # 初始化演示引擎
print("[自动启动] 演示引擎已就绪")
bot_manager.auto_start_enabled_user_bots()

def bj_now():
    return datetime.now(timezone.utc) + timedelta(hours=8)

app = Flask(__name__)
app.secret_key = _os.environ.get('FLASK_SECRET', 'axiom-quant-titanium-2026-secure-key')
EMA_LENS = [20, 60, 120]; MA_LENS = [20, 60, 120]; MIN_PRICE = 0.001

# Junk filter — 稳定币+法币+股票+商品合成资产
JUNK = set()
for pfx in {"USDC","FDUSD","USD1","RLUSD","TUSD","DAI","USDP","USDD","PYUSD","USDY","crvUSD","sUSD","eUSD","GHO","LUSD","MIM","FRAX","USTC","USDE","USR","EURS","EURC","XSGD","XAUT","PAXG","USDJ","USDX","USDB","USDZ","AEUR","USDF","STUSD","USDQ","XUSD","USDS"}:
    JUNK.add(pfx+"USDT")
for fx in {"EUR","GBP","AUD","JPY","CAD","CHF","NZD","TRY","BRL","ZAR","RUB","UAH","PLN","RON","ARS"}:
    JUNK.add(fx+"USDT")
TRADFI_SYMBOLS = {
    "UPUSDT","DOWNUSDT","BULLUSDT","BEARUSDT","BETHUSDT",
    "SPYUSDT","QQQUSDT","TQQQUSDT","SOXLUSDT",
    "AAPLUSDT","TSLAUSDT","GMEUSDT","AMCUSDT","NVDAUSDT","MSTRUSDT","METAUSDT","COINUSDT",
    "INTCUSDT","DELLUSDT","AMDUSDT","MUUSDT","QCOMUSDT","MRVLUSDT","SNDKUSDT",
    "XAUUSDT","XAGUSDT","WTIUSDT","BRENTUSDT","PAXGUSDT",
    "UUSDT","USDEUSDT",
}
for x in TRADFI_SYMBOLS:
    JUNK.add(x)

def is_tradfi_or_junk(symbol):
    if symbol in JUNK:
        return True
    for kw in ["STOCK","BULL","BEAR","UP","DOWN","XAU","XAG"]:
        if kw in symbol:
            return True
    return False

def scan_divergence(interval, top_n=40):
    """趋势扩张扫描: 找刚开始发散的代币"""
    global state
    symbols, vols = fetch_pairs(exchange="binance")
    if not symbols: return []
    ranked = sorted(symbols, key=lambda s: vols.get(s, 0), reverse=True)
    ranked = [s for s in ranked if not is_tradfi_or_junk(s) and vols.get(s,0)>1_000_000]
    total = len(ranked); results = []; counter = [0]
    def process(sym):
        try:
            df = fetch_klines(sym, interval, 120, exchange="binance")
            if df is None or len(df)<100: return None
            c = df["c"]; price=c.iloc[-1]; n=len(c)
            emas_now = [ema(c, p).iloc[-1] for p in EMA_LENS]
            mas_now  = [c.rolling(p).mean().iloc[-1] for p in MA_LENS]
            am = emas_now + mas_now; mx, mn = max(am), min(am)
            spread_now = (mx-mn)/price*100
            inside = mn<=price<=mx
            # 找10根K线前的离散度
            lookback = min(20, n//4)
            if lookback<5: return None
            c_past = c.iloc[:n-lookback]
            price_past = c.iloc[n-lookback-1]
            emas_past = [ema(c_past, p).iloc[-1] for p in EMA_LENS]
            mas_past  = [c_past.rolling(p).mean().iloc[-1] for p in MA_LENS]
            am_past = emas_past + mas_past
            spread_past = (max(am_past)-min(am_past))/price_past*100
            # 发散加速 = 现在离散度 - 过去离散度
            acceleration = spread_now - spread_past
            # 必须: 发散加速>1%, 不在区间内(已突破), 当前离散>3%
            if acceleration<1.0 or inside or spread_now<3: return None
            trend = "向上发散" if price>mx else "向下发散"
            return {"symbol":str(sym),"spread":round(float(spread_now),2),"trend":str(trend),
                    "inside":bool(inside),"price":float(round(price,6 if price<1 else 2)),
                    "vol":float(round(vols.get(sym,0)/1e6,1)),"chg":float(round(acceleration,2)),"score":float(round(acceleration,2))}
        except: return None
        finally:
            counter[0]+=1
            if counter[0]%30==0: state["progress"]=f"{counter[0]}/{total}"
    with ThreadPoolExecutor(max_workers=20) as pool:
        for f in as_completed({pool.submit(process, s): s for s in ranked}):
            r = f.result()
            if r: results.append(r)
    state["progress"]=f"{len(results)}结果"
    results.sort(key=lambda r:r["spread"])  # 离散越低=刚开始发散, 排在前面
    return results[:top_n]

def scan_breakout(interval, top_n=40, squeeze_max=None):
    """动量突破扫描 - 显示全部信号, 交易时再做拐点过滤"""
    global state
    from screener import scan_squeeze_breakout
    state["progress"] = f"扫描{interval}中..."
    results = scan_squeeze_breakout(interval, top_n, exchange="binance")
    state["progress"] = f"{len(results)}结果"
    if len(results) == 0:
        state["progress"] = "0结果(行情无动量突破)"
    return results


def scan(interval, mode, max_price=5):
    global state
    symbols, vols = fetch_pairs(exchange="binance")
    if not symbols: return []
    ranked = sorted(symbols, key=lambda s: vols.get(s, 0), reverse=True)
    ranked = [s for s in ranked if not is_tradfi_or_junk(s) and vols.get(s,0)>1_000_000]  # 过滤24h量<1M的垃圾币
    if mode in ("short","volume"): ranked = [s for s in ranked if s not in BLUE_CHIPS]
    if mode=="volume": ranked = [s for s in ranked if vols.get(s,0)>MIN_VOLUME*3]
    total = len(ranked); results = []; counter = [0]
    bars_map = {"1d":7, "1w":4, "4h":42, "1h":168, "15m":672}
    def process(sym):
        try:
            df = fetch_klines(sym, interval, 200, exchange="binance")
            if df is None or len(df)<50: return None
            c = df["c"]; price=c.iloc[-1]
            if price<MIN_PRICE: return None
            emas = [ema(c, p).iloc[-1] for p in EMA_LENS]
            mas  = [c.rolling(p).mean().iloc[-1] for p in MA_LENS]
            all_mas = emas + mas; mx, mn = max(all_mas), min(all_mas)
            spread = (mx-mn)/price*100
            inside = mn<=price<=mx
            trend = "突破上轨" if price>mx else ("跌破下轨" if price<mn else ("收敛偏多" if price>emas[0] else "收敛偏空"))
            if mode=="squeeze" and not inside: return None
            if mode=="short" and sym in BLUE_CHIPS: return None
            if mode=="volume" and price>max_price: return None
            bars = bars_map.get(interval, 7*24); bars=min(bars, len(c)-2)
            chg=(price-c.iloc[-bars-1])/c.iloc[-bars-1]*100 if bars<len(c) else 0
            if mode=="short" and chg<30: return None
            score=0
            if mode=="short":
                # 涨幅越大做空空间越大 (0-30分)
                score += min(30, chg * 0.3)
                # 均线越发散越容易反转 (0-25分)
                if spread > 20: score += 25
                elif spread > 15: score += 20
                elif spread > 10: score += 12
                elif spread > 6: score += 6
                # 价格高于均线带 = 偏离度大 (0-20分)
                if price > mx: score += min(20, (price - mx) / price * 100 * 2)
                # EMA20 下方 = 短期趋势转弱 (0-15分)
                if price < emas[0]: score += 15
                # 成交量放大 = 关注度高 (0-10分)
                vol_score = min(10, vols.get(sym, 0) / 5e6)
                score += vol_score
                # 价格在带上方但EMA下方 = 临界做空点 (0-5分)
                if price > mx and price < emas[0]: score += 5
            elif mode=="volume": score=round(vols.get(sym,0)/price/1e6,1)
            return {"symbol":str(sym),"spread":round(float(spread),2),"trend":str(trend),"inside":bool(inside),
                    "price":float(round(price,6 if price<1 else 2)),"vol":float(round(vols.get(sym,0)/1e6,1)),
                    "chg":float(round(chg,1)),"score":round(float(score))}
        except: return None
        finally:
            counter[0]+=1
            if counter[0]%30==0: state["progress"]=f"{counter[0]}/{total}"
    with ThreadPoolExecutor(max_workers=20) as pool:
        for f in as_completed({pool.submit(process, s): s for s in ranked}):
            r = f.result()
            if r: results.append(r)
    state["progress"]=f"{len(results)}结果"
    if mode=="short": results.sort(key=lambda r:r["score"],reverse=True)
    elif mode=="volume": results.sort(key=lambda r:r["score"],reverse=True)
    else: results.sort(key=lambda r:r["spread"])
    return results[:40]

def scan_funding(top_n=100):
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10)
        data = r.json()
    except: return {"negative":[], "positive":[], "nextTime":0}
    results = []
    next_time = 0
    for item in data:
        sym = item.get("symbol","")
        if not sym.endswith("USDT") or is_tradfi_or_junk(sym) or sym in BLUE_CHIPS: continue
        rate = float(item.get("lastFundingRate",0))*100
        price = float(item.get("markPrice",0))
        nt = int(item.get("nextFundingTime",0))
        if nt > next_time: next_time = nt
        results.append({"symbol":sym,"spread":round(rate,4),"trend":"负费(空付多)" if rate<0 else "正费(多付空)","inside":rate<0,
                        "price":round(price,6 if price<1 else 2),"vol":0,"chg":round(rate,4),"score":round(rate,4)})
    negative = sorted([r for r in results if r["score"]<0], key=lambda r:r["score"])[:top_n//2]
    positive = sorted([r for r in results if r["score"]>0], key=lambda r:r["score"], reverse=True)[:top_n//2]
    return {"negative":negative, "positive":positive, "nextTime":next_time}

cache = {"squeeze_4h":[],"squeeze_1h":[],"squeeze_15m":[],"squeeze_1d":[],"squeeze_1w":[],
         "diverge_4h":[],"diverge_1h":[],"diverge_15m":[],"diverge_1d":[],"diverge_1w":[],
         "breakout_4h":[],"breakout_1h":[],"breakout_15m":[],"breakout_1d":[],
         "short_4h":[],"short_1h":[],"volume_4h":[],"volume_1h":[],"trader":[],
         "funding":{"negative":[],"positive":[],"nextTime":0},
         "reflow_1h":None}
state = {"time":"--","text":"就绪","scanning":False,"progress":""}
_scan_lock = threading.Lock()
_scan_worker = None
_scan_generation = 0
_scan_worker_token = None
_reflow_automation_lock = threading.Lock()
_reflow_automation = {
    "running": False,
    "last_auto_scan_at": 0,
    "last_auto_error": "",
    "last_skip_at": 0,
    "next_scan_at": 0,
}
_reflow_scheduler_lock = threading.Lock()
_reflow_scheduler_thread = None
_reflow_scheduler_stop = threading.Event()
_reflow_alert_lock = threading.Lock()
_reflow_alert_thread = None
_reflow_alert_stop = threading.Event()
_reflow_alert_wakeup = threading.Event()
_reflow_alert_status = {
    "running": False,
    "last_error": "",
    "last_delivery_at": 0,
}

def _sanitize_reflow_alert_error(error):
    return re.sub(
        rf"({re.escape(WEBHOOK_PREFIX)})\S*",
        r"\1****",
        str(error or ""),
    )[:80]

TABS = [
    ("squeeze_4h","收敛 4H","1"), ("squeeze_1h","收敛 1H","2"), ("squeeze_1d","收敛 日线","3"), ("squeeze_1w","收敛 周线","4"),
    ("breakout_4h","动量突破 4H","5"), ("breakout_1h","动量突破 1H","6"), ("breakout_15m","动量突破 15m","7"), ("breakout_1d","动量突破 日","8"),
    ("short_4h","做空 4H","8"), ("short_1h","做空 1H","9"),
    ("trader","自动交易","T"),
    ("volume_4h","高换手 4H","-"), ("volume_1h","高换手 1H","-"),
    ("funding","资金费率","-"),
]

HTML = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AXIOM Quant | 机构级量化终端</title>
<style>
/* ═══════════════════════════════════════════════
   SPRING THUNDER v3 — Titanium Linear Design
   Matte Titanium · 1px Glow · Spring Motion
   ═══════════════════════════════════════════════ */

:root{
  /* Titanium gray palette */
  --bg: #151718;
  --bg2: #1A1C1E;
  --card: #1D1F21;
  --card2: #222527;
  --border: rgba(255,255,255,0.07);
  --border2: rgba(255,255,255,0.11);
  --text: #D4D6D9;
  --text2: #8B8D91;
  --muted: #5F6165;

  /* Single cold accent — silver-blue to teal */
  --brand: #2DD4BF;
  --brand2: #14B8A6;

  /* Semantic — desaturated metallic */
  --s-green: #34D399;
  --s-red: #F87171;
  --s-blue: #60A5FA;
  --s-yellow: #FBBF24;
  --s-orange: #FB923C;
  --s-purple: #A78BFA;
  --s-cyan: #22D3EE;
  --s-gold: #F59E0B;

  /* Linear shadows — ambient only, no hard edges */
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.2), 0 0 0 1px rgba(255,255,255,0.03);
  --shadow-md: 0 2px 8px rgba(0,0,0,0.25), 0 0 0 1px rgba(255,255,255,0.04);
  --shadow-lg: 0 4px 16px rgba(0,0,0,0.3), 0 0 0 1px rgba(255,255,255,0.05);

  /* 8px grid */
  --u1: 8px; --u2: 16px; --u3: 24px; --u4: 32px;
  --u6: 48px; --u8: 64px; --u12: 96px; --u16: 128px;

  /* Typography — Inter / Geist priority */
  --font: 'Inter', 'Geist', 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'SF Mono', 'Cascadia Code', 'Consolas', monospace;

  /* Spring motion */
  --spring: cubic-bezier(0.25, 1, 0.5, 1);
  --spring-fast: 200ms;
  --spring-normal: 250ms;
}

/* ═══ RESET & BASE ═══ */
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);color:var(--text);
  font-family:var(--font);font-size:14px;font-weight:400;
  line-height:1.5;min-height:100vh;color-scheme:dark;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  letter-spacing:-0.01em;
}
b,strong{font-weight:600}
small{font-size:12px;color:var(--text2)}

/* ═══ PROGRESS BAR ═══ */
.progress-bar{width:100%;height:1.5px;background:rgba(255,255,255,0.03);position:fixed;top:0;left:0;z-index:200}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--brand2),var(--brand),rgba(45,212,191,0.5));transition:width 0.4s var(--spring);width:0}

/* ═══ HEADER ═══ */
header{
  background:rgba(21,23,24,0.94);border-bottom:1px solid var(--border);
  padding:var(--u1) var(--u3);display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:var(--u2);position:sticky;top:0;z-index:100;
  backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
}
header h1{font-size:16px;font-weight:650;letter-spacing:-0.02em;color:var(--text);white-space:nowrap}
.header-right{display:flex;align-items:center;gap:var(--u2);font-size:12px;color:var(--text2)}
.status-dot{width:5px;height:5px;border-radius:50%;background:var(--brand);display:inline-block;transition:all 0.3s var(--spring);box-shadow:0 0 6px rgba(45,212,191,0.3)}
.status-dot.scanning{background:var(--s-orange);box-shadow:0 0 8px rgba(251,146,60,0.4);animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.35;transform:scale(2)}}

/* ═══ LAYOUT ═══ */
main{display:flex;gap:0;min-height:calc(100vh - 110px);max-width:1440px;margin:0 auto}
.sidebar{
  width:220px;background:var(--bg2);border-right:1px solid var(--border);
  padding:var(--u2) 0;flex-shrink:0;
  background:linear-gradient(180deg,rgba(26,28,30,0.98) 0%,rgba(21,23,24,0.95) 100%);
}
.content{flex:1;padding:var(--u4);overflow-x:auto;max-width:1200px}

/* ═══ SIDEBAR ═══ */
.nav-item{
  display:flex;align-items:center;gap:var(--u1);padding:var(--u1) var(--u3);
  cursor:pointer;font-size:12.5px;color:var(--text2);
  border-left:1.5px solid transparent;transition:all var(--spring-fast) var(--spring);
  margin:1px 0;font-weight:480;font-variant-numeric:tabular-nums;
}
.nav-item:hover{color:var(--text);background:rgba(255,255,255,0.025)}
.nav-item.active{color:var(--text);background:rgba(45,212,191,0.04);border-left-color:var(--brand);font-weight:550}
.nav-item .hotkey{
  font-size:9.5px;color:var(--muted);background:rgba(255,255,255,0.03);
  padding:1px 5px;border-radius:3px;margin-left:auto;border:1px solid rgba(255,255,255,0.05);
  font-variant-numeric:tabular-nums;
}
.nav-item.active .hotkey{color:var(--brand);border-color:rgba(45,212,191,0.2);background:rgba(45,212,191,0.06)}

/* Collapsible menu */
.menu-group{margin:2px 0}
.menu-title{
  display:flex;align-items:center;gap:var(--u1);padding:var(--u1) var(--u3);
  cursor:pointer;font-size:11.5px;font-weight:600;color:var(--text2);
  transition:all var(--spring-fast) var(--spring);user-select:none;letter-spacing:0.02em;
}
.menu-title:hover{color:var(--text)}
.menu-title .arrow{font-size:7px;transition:transform var(--spring-fast) var(--spring);color:var(--muted);margin-left:auto}
.menu-title.open .arrow{transform:rotate(90deg)}
.menu-title.open{color:var(--brand)}
.menu-items{overflow:hidden;max-height:0;transition:max-height var(--spring-normal) var(--spring)}
.menu-items.open{max-height:200px}
.menu-items .nav-item{padding-left:36px;font-size:12px}

/* Glowing menu — cold metallic tint */
.menu-glow-btc .menu-title{color:var(--s-gold)!important}
.menu-glow-btc .nav-item.active{border-left-color:var(--s-gold);background:rgba(245,158,11,0.05)}
.menu-glow-blast .menu-title{color:var(--s-yellow)!important}
.menu-glow-blast .nav-item.active{border-left-color:var(--s-yellow);background:rgba(251,191,36,0.05)}
.menu-glow-trader .menu-title{color:var(--s-cyan)!important}
.menu-glow-trader .nav-item.active{border-left-color:var(--s-cyan);background:rgba(34,211,238,0.05)}
.menu-glow-calc .menu-title{color:var(--s-blue)!important}
.menu-glow-calc .nav-item.active{border-left-color:var(--s-blue);background:rgba(96,165,250,0.05)}
.menu-glow-cyber .menu-title{color:var(--s-green, #34d399)!important}
.menu-glow-cyber .nav-item.active{border-left-color:var(--s-green, #34d399);background:rgba(52,211,153,0.05)}

/* ═══ BUTTONS ═══ */
.toolbar{display:flex;align-items:center;gap:var(--u1);margin-bottom:var(--u4);flex-wrap:wrap}
.btn{
  background:var(--card);color:var(--text);border:1px solid var(--border);
  padding:9px var(--u3);border-radius:6px;cursor:pointer;font-size:13px;font-weight:520;
  letter-spacing:-0.01em;transition:all var(--spring-fast) var(--spring);
  font-family:var(--font);font-variant-numeric:tabular-nums;
  position:relative;overflow:hidden;
}
.btn::after{content:'';position:absolute;inset:0;opacity:0;transition:opacity 0.3s;background:radial-gradient(circle at var(--mx,50%) var(--my,50%),rgba(255,255,255,0.04) 0%,transparent 60%)}
.btn:hover{background:var(--card2);border-color:var(--border2)}
.btn:hover::after{opacity:1}
.btn:disabled{opacity:0.25;cursor:not-allowed;filter:grayscale(0.3)}
.btn-stop{background:rgba(248,113,113,0.06);color:var(--s-red);border-color:rgba(248,113,113,0.12);display:none}
.btn-stop:hover{background:rgba(248,113,113,0.1);border-color:rgba(248,113,113,0.2)}

/* ═══ STAT CARDS & BAR ═══ */
.stats{display:flex;gap:var(--u2);margin-bottom:var(--u4);flex-wrap:wrap}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:var(--u2) var(--u3);min-width:110px;transition:all var(--spring-fast) var(--spring);box-shadow:var(--shadow-sm)}
.stat-card .label{font-size:10.5px;color:var(--muted);margin-bottom:3px;text-transform:uppercase;letter-spacing:0.05em;font-weight:500}
.stat-card .value{font-size:22px;font-weight:680;letter-spacing:-0.02em;font-family:var(--font-mono);font-variant-numeric:tabular-nums;color:#B8BCC1}
.stat-bar{display:flex;align-items:center;gap:0;padding:11px 18px;background:var(--card);border:1px solid var(--border);border-radius:8px;margin-bottom:var(--u3);box-shadow:var(--shadow-sm)}
.stat-bar-item{display:flex;align-items:center;gap:6px;padding:0 var(--u3)}
.stat-bar-item:first-child{padding-left:0}
.stat-bar-label{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;font-weight:500}
.stat-bar-item b{font-size:17px;font-weight:680;font-family:var(--font-mono);letter-spacing:-0.02em;font-variant-numeric:tabular-nums;color:#C8CBD0}
.stat-bar-sep{width:1px;height:20px;background:var(--border);flex-shrink:0}

/* ═══ TABLE — financial grade ═══ */
table{
  width:100%;border-collapse:collapse;font-size:13px;
  background:var(--card);border-radius:8px;overflow:hidden;
  border:1px solid var(--border);box-shadow:var(--shadow-sm);
}
th{
  text-align:left;padding:10px var(--u2);border-bottom:1px solid var(--border);
  color:var(--text2);font-weight:550;font-size:10.5px;text-transform:uppercase;
  letter-spacing:0.06em;background:rgba(255,255,255,0.015);
  font-variant-numeric:tabular-nums;
}
td{
  padding:9px var(--u2);border-bottom:1px solid var(--border);position:relative;
  font-variant-numeric:tabular-nums;font-family:var(--font-mono);font-size:12.5px;
  line-height:1.5;color:#B8BCC1;transition:background var(--spring-fast) var(--spring),color 0.3s;
}
tr{transition:background 0.1s}
tr:hover td{background:rgba(255,255,255,0.02)}
tr:last-child td{border-bottom:none}

/* Value flash — 300ms glow on update */
@keyframes valFlash{0%{color:var(--brand);text-shadow:0 0 8px rgba(45,212,191,0.3)}100%{color:inherit;text-shadow:none}}
.val-flash{animation:valFlash .35s var(--spring)}

/* ═══ COLORS & BADGES ═══ */
.g{color:var(--s-green)}.r{color:var(--s-red)}.y{color:var(--s-yellow)}.c{color:var(--s-blue)}.p{color:var(--s-purple)}.o{color:var(--s-orange)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10.5px;font-weight:550;letter-spacing:0.02em}
.badge-in{background:rgba(52,211,153,0.07);color:var(--s-green);border:1px solid rgba(52,211,153,0.12)}
.badge-out{background:rgba(248,113,113,0.07);color:var(--s-red);border:1px solid rgba(248,113,113,0.12)}
.star{font-weight:700;font-size:12px;display:inline-block;width:20px;text-align:center}

/* ═══ FEATURE DESCRIPTION ═══ */
#featureDesc{color:#D4C5A0;font-size:12px;line-height:1.65;padding:0 0 var(--u2);letter-spacing:0.02em;font-weight:420;transition:opacity var(--spring-fast)}

/* ═══ EMPTY STATE ═══ */
.empty-state{text-align:center;padding:var(--u12) var(--u3);color:var(--muted)}
.empty-state h3{color:var(--text2);font-size:15px;font-weight:550;margin-bottom:var(--u1)}
.empty-state p{font-size:13px;opacity:0.45;line-height:1.6}

/* ===== MOMENTUM REFLOW PERSISTENT DASHBOARD ===== */
.reflow-status-grid{display:grid;grid-template-columns:repeat(8,minmax(92px,1fr));gap:var(--u1);margin-bottom:var(--u3)}
.reflow-status-card{min-width:0;padding:9px 10px;background:var(--card);border:1px solid var(--border);border-radius:7px;box-shadow:var(--shadow-sm)}
.reflow-status-card span{display:block;color:var(--muted);font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;margin-bottom:3px}
.reflow-status-card b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);font-family:var(--font-mono);font-size:12px;font-weight:620;font-variant-numeric:tabular-nums}
.reflow-status-card .g{color:var(--s-green)}.reflow-status-card .r{color:var(--s-red)}.reflow-status-card .c{color:var(--s-blue)}
.reflow-filter-bar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:var(--u3)}
.reflow-filter-group{display:flex;align-items:center;gap:3px;padding:3px;background:rgba(255,255,255,.018);border:1px solid var(--border);border-radius:6px}
.reflow-filter-label{padding:0 4px;color:var(--muted);font-size:9.5px;letter-spacing:.04em}
.reflow-filter-btn{border:0;border-radius:4px;padding:4px 6px;background:transparent;color:var(--text2);cursor:pointer;font:520 10px var(--font)}
.reflow-filter-btn:hover,.reflow-filter-btn.active{color:var(--text);background:rgba(255,255,255,.07)}
.reflow-sound-controls{display:flex;align-items:center;gap:5px;margin-left:auto;color:var(--muted);font-size:10px}
.reflow-sound-controls button{border:1px solid var(--border);border-radius:4px;padding:4px 6px;background:rgba(255,255,255,.018);color:var(--text2);cursor:pointer;font:520 10px var(--font)}
.reflow-sound-controls button:hover{color:var(--text);background:rgba(255,255,255,.07)}
.reflow-quality,.reflow-status{display:inline-block;padding:2px 6px;border:1px solid transparent;border-radius:4px;font-size:10px;font-family:var(--font);font-weight:600;white-space:nowrap}
.reflow-quality-high{color:var(--s-green);background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.15)}
.reflow-quality-standard{color:var(--s-blue);background:rgba(96,165,250,.08);border-color:rgba(96,165,250,.15)}
.reflow-quality-watch{color:var(--s-yellow);background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.15)}
.reflow-status-active{color:var(--s-green);background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.15)}
.reflow-status-window-complete,.reflow-status-invalid{color:var(--muted);background:rgba(148,163,184,.07);border-color:rgba(148,163,184,.12)}
.reflow-mobile-details{display:none}

/* ═══ FOOTER ═══ */
.footer{text-align:center;padding:var(--u3);color:var(--muted);font-size:11px;border-top:1px solid var(--border);background:var(--bg2);letter-spacing:0.02em;max-width:1440px;margin:0 auto}
a{color:var(--brand);text-decoration:none;transition:color var(--spring-fast)}
a:hover{color:var(--brand2)}

/* ═══ TRADER PANEL ═══ */
.t-status-bar{
  display:flex;align-items:center;flex-wrap:wrap;gap:var(--u1) var(--u2);margin-bottom:var(--u3);
  padding:var(--u2) var(--u3);background:var(--card);
  border:1px solid var(--border);border-radius:8px;box-shadow:var(--shadow-sm);
}
.t-layout{display:grid;grid-template-columns:280px 1fr;gap:16px;margin-bottom:24px;min-width:0}
.t-layout>*{min-width:0;overflow:hidden}
.trader-panel select,.trader-panel input{
  width:100%;padding:9px 10px;background:var(--bg);color:var(--text);
  border:1px solid var(--border);border-radius:5px;font-size:13px;
  font-family:var(--font);transition:all var(--spring-fast) var(--spring);
  font-variant-numeric:tabular-nums;
}
.trader-panel select:focus,.trader-panel input:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 2px rgba(45,212,191,0.08)}
.t-field{margin-bottom:10px}
.t-field label{display:block;font-size:11px;color:var(--text2);margin-bottom:3px;font-weight:480;letter-spacing:0.01em}
.trader-panel h4{letter-spacing:0.01em;font-weight:580;font-size:12.5px}

/* Collapsible sections */
.t-section{border:1px solid var(--border);border-radius:6px;margin-bottom:var(--u1);overflow:hidden;background:linear-gradient(180deg,rgba(29,31,33,0.9) 0%,rgba(26,28,30,0.95) 100%);transition:all var(--spring-fast) var(--spring);box-shadow:var(--shadow-sm)}
.t-section.api-section{border-color:rgba(251,146,60,0.18);background:linear-gradient(180deg,rgba(251,146,60,0.03) 0%,rgba(29,31,33,0.95) 100%)}
.t-section-header{display:flex;align-items:center;gap:var(--u1);padding:9px 12px;cursor:pointer;user-select:none;transition:background var(--spring-fast);font-size:12.5px;font-weight:520;color:var(--text2);letter-spacing:-0.01em}
.t-section-header:hover{background:rgba(255,255,255,0.015)}
.t-section.api-section .t-section-header{color:var(--s-orange)}
.t-section-header .t-arrow{font-size:7px;transition:transform var(--spring-fast) var(--spring);color:var(--muted);margin-left:auto;transform:rotate(0deg)}
.t-section.open .t-section-header .t-arrow{transform:rotate(90deg)}
.t-section-body{max-height:0;overflow:hidden;transition:max-height var(--spring-normal) var(--spring);padding:0 12px}
.t-section.open .t-section-body{max-height:1100px;padding:0 12px 10px}
.t-section-indicator{width:5px;height:5px;border-radius:50%;flex-shrink:0}
.t-section.api-section .t-section-indicator{background:var(--s-orange);box-shadow:0 0 5px rgba(251,146,60,0.3)}

/* ═══ HOVER RADIAL SPOTLIGHT ═══ */
.spotlight{position:relative;overflow:hidden}
.spotlight::before{content:'';position:absolute;inset:0;opacity:0;transition:opacity 0.4s;pointer-events:none;z-index:1;border-radius:inherit}
.spotlight:hover::before{opacity:1}

/* ═══ TOOLTIP ═══ */
.tip{position:relative;display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border-radius:50%;background:rgba(167,139,250,0.1);color:var(--s-purple);font-size:9.5px;font-weight:650;cursor:help;margin-left:4px}
.tip .tip-text{visibility:hidden;opacity:0;position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);background:#1a1d21;color:#e0e2e6;font-size:11px;font-weight:400;padding:8px 12px;border-radius:6px;border:1px solid rgba(255,255,255,0.12);white-space:normal;z-index:9999;transition:opacity 0.15s;pointer-events:none;line-height:1.5;box-shadow:0 8px 24px rgba(0,0,0,0.5);min-width:180px;text-align:center}
.tip:hover .tip-text{visibility:visible;opacity:1;transition:opacity 0.15s}

/* ═══ ICON SYSTEM ═══ */
.ic-dot{display:inline-block;width:7px;height:7px;border-radius:50%;vertical-align:middle;flex-shrink:0;margin-right:4px}
.ic-dot-sm{width:5px;height:5px;margin-right:3px}
.ic-strong{background:var(--s-green);box-shadow:0 0 5px rgba(52,211,153,0.25)}
.ic-mid{background:var(--s-yellow);box-shadow:0 0 5px rgba(251,191,36,0.2)}
.ic-weak{background:var(--muted)}
.ic-bull{background:var(--s-green)}
.ic-bear{background:var(--s-red)}
.ic-cold{background:var(--s-blue)}
.ic-empty{width:36px;height:36px;border:1.5px solid var(--border);border-radius:8px;margin:0 auto var(--u4);opacity:0.2;position:relative}
.ic-empty::after{content:'';position:absolute;bottom:8px;left:9px;right:9px;height:1.5px;background:var(--border);border-radius:1px;box-shadow:0 -6px 0 var(--border),0 -12px 0 var(--border)}
.ic-lock{width:32px;height:26px;border:1.5px solid var(--border);border-radius:4px;margin:0 auto var(--u3);opacity:0.25;position:relative}
.ic-lock::before{content:'';position:absolute;top:-12px;left:50%;transform:translateX(-50%);width:14px;height:12px;border:1.5px solid var(--border);border-bottom:none;border-radius:7px 7px 0 0}
.ic-lock::after{content:'';position:absolute;top:7px;left:50%;transform:translateX(-50%);width:3px;height:3px;border-radius:50%;background:var(--border)}
.ic-warn{width:32px;height:28px;border:1.5px solid var(--s-yellow);border-radius:3px;margin:0 auto var(--u3);opacity:0.3;position:relative}
.ic-warn::after{content:'!';position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:650;color:var(--s-yellow);font-family:var(--font)}
.ic-bolt{display:inline-block;width:3.5px;height:13px;background:var(--brand);border-radius:1.5px;vertical-align:middle;margin-right:5px;position:relative;top:-1px;clip-path:polygon(50% 0%,100% 55%,60% 55%,80% 100%,0% 45%,40% 45%)}
.ic-close-btn{display:inline-block;width:10px;height:10px;border:1.5px solid var(--s-red);border-radius:2px;vertical-align:middle;margin-right:3px;position:relative;top:-1px}

/* ═══ COPY ═══ */
.copy-sym{cursor:pointer;opacity:0.25;margin-right:5px;flex-shrink:0;transition:all var(--spring-fast);display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;position:relative;vertical-align:middle}
.copy-sym::before{content:'';position:absolute;width:7px;height:8px;border:1.3px solid var(--text2);border-radius:1.2px;top:2px;left:2px;transition:all var(--spring-fast)}
.copy-sym::after{content:'';position:absolute;width:7px;height:8px;border:1.3px solid var(--text2);border-radius:1.2px;bottom:2px;right:2px;background:var(--bg);transition:all var(--spring-fast)}
.copy-sym:hover{opacity:0.7}
.copy-sym:hover::before,.copy-sym:hover::after{border-color:var(--text)}
.copy-sym.copied::before,.copy-sym.copied::after{border-color:var(--brand)}

/* ═══ BTC DASHBOARD ═══ */
.btc-dashboard{max-width:960px;margin:0 auto}
.btc-header{display:flex;align-items:center;justify-content:space-between;padding:0 0 var(--u3);border-bottom:1px solid var(--border);margin-bottom:var(--u3)}
.btc-header-left{display:flex;align-items:center;gap:10px}
.btc-icon{font-size:28px;font-weight:700;color:var(--s-gold);line-height:1}
.btc-title{font-size:18px;font-weight:650;color:var(--text);letter-spacing:-0.02em}
.btc-header-right{display:flex;align-items:center;gap:var(--u1)}
.btc-pulse{width:6px;height:6px;border-radius:50%;background:var(--brand);box-shadow:0 0 6px rgba(45,212,191,0.3);animation:pulse 2.5s infinite}
.btc-signal-bar{display:grid;grid-template-columns:repeat(4,1fr);gap:var(--u1);margin-bottom:var(--u3)}
.btc-signal-item{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:var(--u2);text-align:center;transition:all var(--spring-fast) var(--spring);box-shadow:var(--shadow-sm);position:relative;overflow:hidden}
.btc-signal-item::after{content:'';position:absolute;inset:0;opacity:0;transition:opacity 0.4s;background:radial-gradient(circle at var(--mx,50%) var(--my,50%),rgba(255,255,255,0.03) 0%,transparent 60%);pointer-events:none}
.btc-signal-item:hover::after{opacity:1}
.btc-signal-item:hover{border-color:var(--border2)}
.btc-signal-item span{display:block;font-size:9.5px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:3px}
.btc-signal-item b{font-size:15px;font-weight:620;letter-spacing:-0.02em;color:#B8BCC1}
.btc-signal-item.hot{border-color:rgba(251,191,36,0.2);background:linear-gradient(180deg,rgba(251,191,36,0.04) 0%,var(--card) 100%)}
.btc-signal-item.hot b{color:var(--s-yellow)}
.btc-signal-item.strong b{color:var(--s-green)}
.btc-signal-item.weak b{color:var(--s-blue)}
.btc-signal-item.cold b{color:var(--s-red)}
.btc-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:var(--u2)}
.btc-card{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:all var(--spring-fast) var(--spring);box-shadow:var(--shadow-sm);position:relative}
.btc-card::after{content:'';position:absolute;inset:0;opacity:0;transition:opacity 0.4s;background:radial-gradient(circle at var(--mx,50%) var(--my,50%),rgba(255,255,255,0.03) 0%,transparent 60%);pointer-events:none;z-index:1;border-radius:inherit}
.btc-card:hover::after{opacity:1}
.btc-card:hover{border-color:var(--border2);transform:translateY(-1px);box-shadow:var(--shadow-md)}
.btc-card-head{display:flex;align-items:center;gap:var(--u1);padding:12px var(--u2) 8px}
.btc-card-label{font-size:10.5px;font-weight:520;color:var(--text2);text-transform:uppercase;letter-spacing:0.05em;flex:1}
.btc-card-price{font-size:14px;font-weight:620;color:var(--text);font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.btc-row{display:flex;align-items:center;padding:6px var(--u2);gap:var(--u1)}
.btc-row-label{font-size:9.5px;color:var(--muted);text-transform:uppercase;letter-spacing:0.04em;min-width:52px}
.btc-row-val{font-size:11.5px;font-weight:520;margin-left:auto;text-align:right}
.btc-row.sub{font-size:9.5px;color:var(--muted);padding:3px var(--u2) 3px 68px;gap:4px;font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.btc-overall{text-align:center;padding:8px var(--u2) 12px;font-size:14px;font-weight:650;letter-spacing:-0.02em}
.btc-overall.hot{color:var(--s-yellow)}.btc-overall.strong{color:var(--s-green)}.btc-overall.weak{color:var(--s-blue)}.btc-overall.cold{color:var(--s-red)}.btc-overall.neutral{color:var(--muted)}
.btc-footer{text-align:center;color:var(--muted);font-size:10.5px;padding:var(--u4) 0;opacity:0.35;max-width:768px;margin:0 auto;line-height:1.6}

/* ═══ RJ / BBKD CHART ═══ */
.rj-board{max-width:1080px;margin:0 auto}
.rj-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:14px;border-bottom:1px solid var(--border);padding-bottom:14px}
.rj-title{font-size:18px;font-weight:650;color:var(--text);letter-spacing:0}
.rj-sub{font-size:11px;color:var(--muted);margin-top:3px}
.rj-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.rj-controls input,.rj-controls select{height:34px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:0 9px;font-size:12px;font-family:var(--font-mono)}
.rj-controls input{width:112px}
.rj-controls select{width:76px}
.rj-metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:12px}
.rj-metric{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px;min-width:0}
.rj-metric span{display:block;font-size:9.5px;color:var(--muted);letter-spacing:0.05em;text-transform:uppercase;margin-bottom:3px}
.rj-metric b{font-family:var(--font-mono);font-size:15px;font-weight:680;color:var(--text)}
.rj-chart{background:linear-gradient(180deg,rgba(20,22,24,0.95) 0%,rgba(14,15,17,0.98) 100%);border:1px solid var(--border);border-radius:8px;overflow:hidden;box-shadow:var(--shadow-sm)}
.rj-chart svg{width:100%;height:560px;display:block}
.rj-panel-title{font-size:10px;fill:rgba(184,188,193,0.72);font-family:monospace;letter-spacing:0}
.rj-note{font-size:11px;color:var(--muted);line-height:1.7;margin-top:10px}
.rj-events{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-top:12px}
.rj-event{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px;font-size:11px;color:var(--text2);font-family:var(--font-mono)}
@media(max-width:768px){.rj-head{align-items:flex-start;flex-direction:column}.rj-metrics{grid-template-columns:repeat(2,1fr)}.rj-chart svg{height:480px}.rj-controls input{width:96px}}
.ali-bull{color:var(--s-green)!important}.ali-bear{color:var(--s-red)!important}.ali-neu{color:var(--muted)!important}
.sq-tight{color:var(--s-gold)}.sq-dense{color:var(--s-orange)}.sq-spread{color:var(--s-blue)}.sq-trend{color:var(--s-purple)}
.pos-above{color:var(--s-green)}.pos-below{color:var(--s-red)}.pos-inside{color:var(--s-yellow)}
.brk-up{color:var(--s-green)}.brk-down{color:var(--s-red)}.brk-none{color:var(--muted)}
.frac-up{color:var(--s-green)}.frac-down{color:var(--s-red)}

/* ═══ CALCULATOR ═══ */
.calc-wrap{max-width:420px;margin:0 auto;font-size:13px}
.calc-wrap .c-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;font-weight:480;margin-bottom:3px;display:block}
.calc-wrap input[type=number]{width:100%;padding:8px 10px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:5px;font-size:13px;font-family:var(--font-mono);transition:all var(--spring-fast) var(--spring);font-variant-numeric:tabular-nums}
.calc-wrap input[type=number]:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 2px rgba(45,212,191,0.08)}
.calc-wrap input[type=range]{width:100%;height:3px;background:var(--border);border-radius:2px;appearance:none;accent-color:var(--brand);cursor:pointer}
.calc-card{background:linear-gradient(180deg,rgba(29,31,33,0.9) 0%,rgba(26,28,30,0.95) 100%);border:1px solid var(--border);border-radius:8px;padding:var(--u3);box-shadow:var(--shadow-sm);margin-bottom:var(--u2);position:relative;overflow:hidden}
.calc-card::after{content:'';position:absolute;inset:0;opacity:0;transition:opacity 0.4s;background:radial-gradient(circle at var(--mx,50%) var(--my,50%),rgba(255,255,255,0.03) 0%,transparent 60%);pointer-events:none;border-radius:inherit}
.calc-card:hover::after{opacity:1}
.calc-result{background:linear-gradient(180deg,rgba(21,23,24,0.95) 0%,rgba(18,20,21,0.98) 100%);border:1px solid var(--border);border-radius:8px;padding:var(--u3);color:#B8BCC1}
.calc-hint{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:0.04em}
.calc-badge{padding:3px 8px;border-radius:4px;font-size:10.5px;font-weight:580}
.calc-badge.long{background:rgba(52,211,153,0.08);color:var(--s-green);border:1px solid rgba(52,211,153,0.12)}
.calc-badge.short{background:rgba(248,113,113,0.08);color:var(--s-red);border:1px solid rgba(248,113,113,0.12)}

/* ═══ ROLLER ═══ */
.roller-wrap{max-width:900px;margin:0 auto}
.roller-table{overflow-x:auto;border-radius:6px;border:1px solid var(--border)}
.roller-table table{width:100%;border-collapse:collapse;font-size:12px;background:inherit}
.roller-table th{background:rgba(255,255,255,0.015);color:var(--muted);font-weight:500;padding:10px 12px;border-bottom:1px solid var(--border);font-size:10px;text-transform:none;letter-spacing:0.04em}
.roller-table td{padding:9px 12px;border-bottom:1px solid var(--border);color:#B0B3B8;font-family:var(--font-mono);font-size:11.5px;font-variant-numeric:tabular-nums}
.roller-highlight td{background:rgba(251,191,36,0.04);color:var(--s-yellow);font-weight:580}
.roller-badge-l{display:inline-block;padding:2px 6px;border-radius:3px;font-size:10.5px;font-weight:580;background:rgba(52,211,153,0.08);color:var(--s-green);border:1px solid rgba(52,211,153,0.12)}
.roller-badge-s{display:inline-block;padding:2px 6px;border-radius:3px;font-size:10.5px;font-weight:580;background:rgba(248,113,113,0.08);color:var(--s-red);border:1px solid rgba(248,113,113,0.12)}
.text-right{text-align:right}.text-center{text-align:center}
.roller-info{font-size:10.5px;color:var(--muted);line-height:1.7;margin-top:var(--u3);max-width:768px;margin-left:auto;margin-right:auto;text-align:center}

/* ═══ INTRO WHITEPAPER ═══ */
.intro-wrap { max-width: 960px; margin: 0 auto; padding: 0 16px 40px; animation: valFlash 0.8s var(--spring); }
.intro-hero { text-align: center; padding: 48px 24px; margin-bottom: 32px; background: rgba(45,212,191,0.03); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(45,212,191,0.12); border-radius: 16px; position: relative; overflow: hidden; box-shadow: 0 0 60px -20px rgba(45,212,191,0.08), inset 0 1px 0 rgba(255,255,255,0.02); }
.intro-hero::before { content: ''; position: absolute; top: -50%; left: -50%; width: 200%; height: 200%; background: radial-gradient(ellipse at 50% 0%, rgba(45,212,191,0.04) 0%, transparent 60%); pointer-events: none; }
.intro-hero::after { content: ''; position: absolute; top: 0; left: -100%; width: 50%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.02), transparent); animation: laserSweep 4s infinite; pointer-events: none; }
.intro-card { background: rgba(20,21,23,0.6); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); border: 1px solid rgba(255,255,255,0.06); border-radius: 14px; padding: 28px 24px; margin-bottom: 18px; box-shadow: 0 4px 24px -8px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.02); position: relative; transition: all 0.35s var(--spring); }
.intro-card::before { content: ''; position: absolute; inset: 0; border-radius: 14px; background: radial-gradient(ellipse at 0% 0%, rgba(45,212,191,0.04) 0%, transparent 50%); pointer-events: none; }
.intro-card:hover { border-color: rgba(45,212,191,0.25); transform: translateY(-3px); box-shadow: 0 12px 36px -12px rgba(45,212,191,0.12), 0 0 0 1px rgba(45,212,191,0.08); background: rgba(23,24,27,0.7); }
.intro-num { display: inline-flex; width: 34px; height: 34px; border-radius: 10px; background: rgba(45,212,191,0.08); backdrop-filter: blur(4px); border: 1px solid rgba(45,212,191,0.18); color: var(--brand); font-weight: 800; font-size: 14px; align-items: center; justify-content: center; margin-right: 14px; box-shadow: 0 0 14px rgba(45,212,191,0.08); }
.intro-title { font-size: 18px; font-weight: 700; color: #fff; display: flex; align-items: center; margin-bottom: 8px; letter-spacing: -0.01em; }
.intro-sub { color: var(--s-cyan); font-size: 12px; font-weight: 600; margin-bottom: 16px; margin-left: 48px; letter-spacing: 0.05em; opacity: 0.85; }
.intro-text { font-size: 13px; color: var(--text2); line-height: 1.75; margin-bottom: 12px; text-align: justify; }
.intro-list { list-style: none; padding: 0; margin: 0; }
.intro-list li { position: relative; padding-left: 18px; font-size: 13px; color: #C8CBD0; margin-bottom: 10px; line-height: 1.65; }
.intro-list li::before { content: '▪'; position: absolute; left: 0; color: var(--brand); font-weight: bold; opacity: 0.7; }
.intro-list b { color: #fff; font-weight: 600; }
.intro-tier-card { background: rgba(255,255,255,0.015); backdrop-filter: blur(6px); padding: 18px 16px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.04); transition: all 0.3s; }
.intro-tier-card:hover { background: rgba(255,255,255,0.03); border-color: rgba(255,255,255,0.08); }
@keyframes laserSweep { 0%{left:-100%} 100%{left:200%} }
@media(max-width:768px){
  .intro-hero{padding:32px 16px}
  .intro-hero h2{font-size:22px!important}
  .intro-hero h3{font-size:13px!important;letter-spacing:1px!important}
  .intro-card{padding:20px 16px}
  .intro-title{font-size:15px}
  .intro-sub{margin-left:0;font-size:11px}
  .intro-text{font-size:12px;text-align:left}
  .intro-list li{font-size:11px}
}

/* ═══ MOBILE ═══ */
@media(max-width:768px){
  body{font-size:13px;overflow-x:hidden}
  main{overflow-x:hidden}
  .content{overflow-x:hidden;max-width:100vw}
  header{padding:var(--u1) var(--u2)}
  header h1{font-size:15px}
  .header-right{font-size:11px;gap:var(--u1)}
  main{flex-direction:column;max-width:100%}
  .sidebar{position:fixed;top:0;left:0;width:260px;height:100vh;z-index:300;transform:translateX(-100%);transition:transform var(--spring-normal) var(--spring);overflow-y:auto;padding-top:52px}
  .sidebar.open{transform:translateX(0);box-shadow:0 0 0 9999px rgba(0,0,0,0.45)}
  .sidebar-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:299;display:none}
  .sidebar-overlay.show{display:block}
  .hamburger{display:inline-flex!important;background:none;border:none;color:var(--text);font-size:18px;cursor:pointer;padding:4px 8px}
  .content{padding:var(--u2)}
  .toolbar .btn{padding:7px 12px;font-size:12px;flex:1;text-align:center}
  .stat-bar{flex-wrap:wrap;gap:6px;padding:8px 12px}
  .stat-bar-item{padding:0 var(--u2)}
  .stat-bar-item b{font-size:14px}
  table{font-size:11px;display:block;overflow-x:auto;white-space:nowrap;-webkit-overflow-scrolling:touch}
  .reflow-status-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.reflow-status-card{padding:8px}.reflow-status-card b{font-size:10.5px}.reflow-filter-bar{gap:5px}.reflow-filter-group{width:100%;justify-content:flex-start}.reflow-filter-btn{padding:4px 5px}.reflow-dashboard table th:nth-child(n+8),.reflow-dashboard table td:nth-child(n+8){display:none}.reflow-mobile-freshness{display:block;margin-top:3px;color:var(--text2);font:10px var(--font-mono)}.reflow-mobile-details{display:block;white-space:normal;margin-top:5px;color:var(--text2);font:10px/1.6 var(--font)}.reflow-mobile-details summary{cursor:pointer;color:var(--muted);font-size:10px}.reflow-mobile-details div{padding-top:4px}
  th,td{padding:6px 8px}
  tr.row-blast td:first-child::before,tr.row-strong td:first-child::before{display:none}
  tr.row-blast{background:rgba(251,191,36,0.1)!important}
  tr.row-strong{background:rgba(52,211,153,0.1)!important}
  .t-layout{grid-template-columns:1fr!important;gap:var(--u2)!important;overflow:visible}
  .t-layout>*{overflow:visible}
  .t-status-bar{padding:6px 10px!important;gap:8px!important}
  .t-status-bar #tRunLabel{font-size:12px}
  .axiom-hb-bar{height:auto!important;flex-wrap:wrap;padding:6px 10px!important;margin-left:0!important}
  .axiom-hb-bar .ah-timer{min-width:0;flex:0 0 auto!important;padding:0 8px!important}
  .axiom-hb-bar .ah-main-sec{font-size:14px}
  .axiom-hb-bar .ah-ms-sec{font-size:10px}
  .axiom-hb-bar .ah-label{font-size:9px}
  .ah-scan-text{font-size:10px!important;padding:0 8px!important}
  .trader-panel select,.trader-panel input{font-size:16px;padding:9px 10px}
  #posPanel table,#logPanel table{display:block;overflow-x:auto}
  .btc-signal-bar{grid-template-columns:repeat(2,1fr)}
  .btc-grid{grid-template-columns:1fr}
  .btc-title{font-size:16px}
  .calc-wrap{max-width:100%}
  .roller-wrap{max-width:100%}.roller-table td,.roller-table th{padding:7px 8px;font-size:10px}
  .footer{font-size:10px;padding:var(--u2)}
}
@media(min-width:769px){.hamburger{display:none!important}.sidebar-overlay{display:none!important}}

/* ═══════════════════════════════════════════════
   SPRING THUNDER v3 — 全局 Linear 级流光特效包
   ═══════════════════════════════════════════════ */

/* 全局隐形幽灵滚动条 */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.08);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:rgba(45,212,191,0.35);box-shadow:0 0 6px rgba(45,212,191,0.2)}
::-webkit-scrollbar-corner{background:transparent}

/* 顶部标题：金属拉丝钛金反光 (仅AXIOM部分) */
header h1 .shimmer{
  background:linear-gradient(110deg,var(--text) 20%,#fff 40%,#fff 50%,var(--text) 80%);
  background-size:200% 100%;-webkit-background-clip:text;-webkit-text-fill-color:transparent;
  animation:metallicShimmer 1.8s linear infinite;
}
@keyframes metallicShimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* 侧边栏：激活项能量柱与悬停扫掠光 */
.nav-item{position:relative;overflow:hidden;z-index:1}
.nav-item::after{
  content:'';position:absolute;top:0;left:-100%;width:50%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.03),transparent);
  transform:skewX(-20deg);z-index:-1;
}
.nav-item:hover::after{left:200%;transition:left .6s cubic-bezier(.4,0,.2,1)}
.nav-item.active{background:linear-gradient(90deg,rgba(45,212,191,0.06) 0%,transparent 100%);border-left:none}
.nav-item.active::before{
  content:'';position:absolute;left:0;top:0;bottom:0;width:2.5px;
  background:linear-gradient(180deg,transparent 0%,var(--brand) 50%,transparent 100%);
  background-size:100% 200%;animation:sideBarFlow 1.5s linear infinite;
  box-shadow:0 0 10px var(--brand);
}
@keyframes sideBarFlow{0%{background-position:0 -100%}100%{background-position:0 100%}}

/* 扫描状态灯：隐形雷达静默探测波纹 */
.status-dot.scanning{
  background:var(--s-orange);box-shadow:0 0 0 0 rgba(251,146,60,0.4);
  animation:radarRipple 1.5s cubic-bezier(.1,.7,1,.1) infinite;
}
@keyframes radarRipple{
  0%{box-shadow:0 0 0 0 rgba(251,146,60,0.4),0 0 0 0 rgba(251,146,60,0.4)}
  50%{box-shadow:0 0 0 4px rgba(251,146,60,0.2),0 0 0 10px rgba(251,146,60,0.1)}
  100%{box-shadow:0 0 0 8px rgba(251,146,60,0),0 0 0 20px rgba(251,146,60,0)}
}

/* 顶部进度条：极速激光穿梭 */
.progress-fill{
  position:relative;overflow:hidden;
  background:linear-gradient(90deg,transparent,var(--brand),#fff);
  box-shadow:0 0 8px var(--brand),0 0 2px var(--brand2);
}
.progress-fill::after{
  content:'';position:absolute;top:0;left:-100%;width:50%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.8),transparent);
  animation:laserSweep 1s linear infinite;
}
@keyframes laserSweep{0%{left:-100%}100%{left:200%}}

/* 数据表格：行悬停时左侧激光游标切入 */
tbody tr{position:relative}
tbody tr td:first-child::before{
  content:'';position:absolute;left:0;top:4px;bottom:4px;width:2.5px;
  background:var(--brand);border-radius:0 2px 2px 0;
  transform:scaleY(0);transition:transform .25s var(--spring);
  box-shadow:0 0 8px var(--brand);z-index:10;
}
tbody tr:hover td:first-child::before{transform:scaleY(1)}

/* 动量突破表格：极强/强势信号行 — 左侧光柱常亮 + 整行微光环 */
@keyframes rowGlowPulse{0%,100%{box-shadow:0 0 6px var(--row-glow),0 0 12px var(--row-glow)}50%{box-shadow:0 0 12px var(--row-glow),0 0 24px var(--row-glow)}}
tr.row-blast{--row-glow:var(--s-yellow)}
tr.row-strong{--row-glow:var(--s-green)}
tr.row-blast td:first-child::before{
  background:var(--s-yellow)!important;animation:rowGlowPulse 1.5s ease-in-out infinite!important;
  transform:scaleY(1)!important;
}
tr.row-strong td:first-child::before{
  background:var(--s-green)!important;animation:rowGlowPulse 1.5s ease-in-out infinite!important;
  transform:scaleY(1)!important;
}
tr.row-blast{background:linear-gradient(90deg,rgba(251,191,36,0.05) 0%,transparent 100%)!important}
tr.row-strong{background:linear-gradient(90deg,rgba(52,211,153,0.05) 0%,transparent 100%)!important}

/* 全局交互按钮：赛博光刃扫过动效 */
.btn{position:relative;overflow:hidden}
.btn::before{
  content:'';position:absolute;top:0;left:-100%;width:40%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.12),transparent);
  transform:skewX(-20deg);transition:none;z-index:1;pointer-events:none;
}
.btn:hover::before{left:200%;transition:left .5s ease-in-out}

/* 核心模块卡片：底部 1px 极细游走光纤 */
.stat-card,.btc-card,.calc-card,.t-section{position:relative;overflow:hidden}
.stat-card::after,.btc-card::after,.calc-card::after,.t-section::after{
  content:'';position:absolute;left:0;bottom:0;height:1px;width:100%;
  background:linear-gradient(90deg,transparent,rgba(45,212,191,0.5),transparent);
  background-size:200% 100%;opacity:0;transition:opacity .3s;
  animation:cardEdgeFlow 2.5s linear infinite;pointer-events:none;
}
.stat-card:hover::after,.btc-card:hover::after,.calc-card:hover::after,.t-section:hover::after{opacity:1}
@keyframes cardEdgeFlow{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* 强烈信号专属：无限循环跑马灯边缘流光 */
.btc-signal-item.hot::before,.btc-signal-item.strong::before{
  content:'';position:absolute;left:0;bottom:0;height:1.5px;width:100%;
  background:linear-gradient(90deg,transparent,var(--brand),transparent);
  background-size:200% 100%;animation:dataFlow 1.5s linear infinite;z-index:2;
}
.btc-signal-item.hot::before{background:linear-gradient(90deg,transparent,var(--s-yellow),transparent)}
@keyframes dataFlow{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* ═══════════════════════════════════════════════
   Magic Border — 全边框环绕跑马灯流光
   conic-gradient + mask 仅边框游走，内部透明
   ═══════════════════════════════════════════════ */
@property --border-angle{syntax:'<angle>';initial-value:0deg;inherits:false}
@keyframes magicBorderSpin{100%{--border-angle:360deg}}

/* 清除旧底部单线条 */
.btc-signal-item.hot::before,.btc-signal-item.strong::before,
.btc-signal-item.weak::before,.btc-signal-item.cold::before{display:none!important}

/* 压暗边框凸显光刃 */
.btc-signal-item.hot,.btc-signal-item.strong,
.btc-signal-item.weak,.btc-signal-item.cold,
.btc-card.hot,.btc-card.strong,
.btc-card.weak,.btc-card.cold{border-color:rgba(255,255,255,0.02)!important}

/* 核心魔法：conic-gradient + mask 全边框流光 */
.btc-signal-item.hot::after,.btc-signal-item.strong::after,
.btc-signal-item.weak::after,.btc-signal-item.cold::after,
.btc-card.hot::after,.btc-card.strong::after,
.btc-card.weak::after,.btc-card.cold::after{
  content:'';position:absolute;inset:-1px;border-radius:inherit;
  padding:1.5px;
  background:conic-gradient(from var(--border-angle),transparent 65%,var(--glow-color) 100%);
  -webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);
  -webkit-mask-composite:xor;mask-composite:exclude;
  pointer-events:none;animation:magicBorderSpin 2s linear infinite;z-index:5;opacity:0.9;
}

/* 信号状态专属冷冽荧光色 */
.btc-signal-item.hot,.btc-card.hot{--glow-color:var(--s-yellow)}
.btc-signal-item.strong,.btc-card.strong{--glow-color:var(--s-green)}
.btc-signal-item.weak,.btc-card.weak{--glow-color:var(--s-blue)}
.btc-signal-item.cold,.btc-card.cold{--glow-color:var(--s-red)}

/* ====== AXIOM QUANT 引擎心跳 ====== */
.axiom-heartbeat{display:inline-flex;align-items:center;background:linear-gradient(90deg,rgba(15,17,18,0.8) 0%,rgba(20,22,24,0.9) 100%);border:1px solid rgba(255,255,255,0.06);border-radius:6px;padding:0;height:36px;box-shadow:inset 0 1px 0 rgba(255,255,255,0.02),0 2px 8px rgba(0,0,0,0.2);position:relative;overflow:hidden;margin-left:auto}
.axiom-hb-bar{display:flex;margin-left:0;border-top:none;border-radius:0 0 10px 10px;height:34px}
.axiom-hb-bar .ah-timer{flex:0 0 auto}
.axiom-hb-bar #startBtn,.axiom-hb-bar #traderStopBtn,.axiom-hb-bar #btnCloseAll{flex-shrink:0;position:relative;z-index:2}
.ah-status{display:flex;align-items:center;padding:0 12px;height:100%;background:rgba(255,255,255,0.02);border-right:1px solid rgba(255,255,255,0.04)}
.ah-dot{width:6px;height:6px;border-radius:50%;background:var(--muted);margin-right:8px;transition:all 0.3s}
.ah-label{font-size:10px;font-weight:650;color:var(--text2);letter-spacing:0.06em;text-transform:uppercase}
.ah-timer{display:flex;align-items:baseline;padding:0 12px;min-width:85px;justify-content:center}
.ah-main-sec{font-family:var(--font-mono);font-size:16px;font-weight:700;color:var(--text);font-variant-numeric:tabular-nums;letter-spacing:-0.02em}
.ah-ms-sec{font-family:var(--font-mono);font-size:11px;font-weight:600;color:var(--brand);font-variant-numeric:tabular-nums}
.ah-track{position:absolute;bottom:0;left:0;right:0;height:1.5px;background:rgba(255,255,255,0.05)}
.ah-fill{height:100%;width:100%;background:var(--brand);box-shadow:0 0 6px var(--brand);transform-origin:left center;will-change:transform}
.axiom-heartbeat.warning .ah-ms-sec{color:var(--s-red)}
.axiom-heartbeat.warning .ah-fill{background:var(--s-red);box-shadow:0 0 8px var(--s-red)}
.axiom-heartbeat.warning .ah-dot{background:var(--s-red);box-shadow:0 0 8px rgba(248,113,113,0.4)}
.axiom-heartbeat.is-scanning .ah-timer{display:none}
.axiom-heartbeat.is-scanning .ah-status{border-right:none;background:transparent}
.axiom-heartbeat.is-scanning .ah-dot{background:var(--brand);box-shadow:0 0 8px rgba(45,212,191,0.5);animation:pulseOpacity 1s infinite}
.axiom-heartbeat.is-scanning .ah-label{color:var(--brand)}
.ah-scan-text{display:none;font-family:var(--font-mono);font-size:12px;font-weight:700;color:var(--brand);padding:0 12px;letter-spacing:0.08em;text-shadow:0 0 8px rgba(45,212,191,0.3)}
.axiom-heartbeat.is-scanning .ah-scan-text{display:block}
.axiom-heartbeat.is-scanning .ah-fill{width:30%;background:linear-gradient(90deg,transparent,var(--brand),transparent);box-shadow:0 0 8px var(--brand);transform:none!important;animation:scanningSweep 0.8s cubic-bezier(.4,0,.2,1) infinite alternate}
@keyframes scanningSweep{0%{left:-10%}100%{left:80%}}
@keyframes pulseOpacity{0%,100%{opacity:1}50%{opacity:0.3}}

/* ====== AXIOM 自动交易面板动态视效包 ====== */
.rj-pipeline-panel{background:linear-gradient(180deg,rgba(16,18,23,0.96),rgba(11,13,17,0.98));border:1px solid rgba(45,212,191,0.16);border-radius:10px;padding:14px;margin-bottom:14px;box-shadow:inset 0 1px 0 rgba(255,255,255,0.03),0 12px 28px rgba(0,0,0,0.28);position:relative;overflow:hidden}
.rj-pipeline-panel::before{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(45,212,191,0.045),transparent);transform:translateX(-100%);animation:rjPanelSweep 4.8s linear infinite;pointer-events:none}
.rj-pipeline-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;position:relative;z-index:1}
.rj-title{display:flex;align-items:center;gap:8px;color:var(--s-cyan);font-size:13px;font-weight:800;letter-spacing:0.02em}
.rj-title-dot{width:6px;height:6px;border-radius:50%;background:var(--brand);box-shadow:0 0 8px rgba(45,212,191,0.8);animation:rjDotPulse 1.4s ease-in-out infinite}
.rj-meta{font-family:var(--font-mono);font-size:10px;color:var(--muted);white-space:nowrap}
.rj-rail{display:grid;grid-template-columns:repeat(7,minmax(88px,1fr));gap:6px;margin-bottom:12px;position:relative;z-index:1}
.rj-node{background:rgba(255,255,255,0.025);border:1px solid rgba(255,255,255,0.055);border-radius:7px;padding:8px 9px;min-width:0;position:relative;overflow:hidden}
.rj-node::after{content:'';position:absolute;left:-30%;bottom:0;width:60%;height:1px;background:linear-gradient(90deg,transparent,var(--brand),transparent);animation:rjNodeFlow 2.6s linear infinite;opacity:0.55}
.rj-node.active{border-color:rgba(45,212,191,0.24);background:rgba(45,212,191,0.045)}
.rj-node.warn{border-color:rgba(251,191,36,0.22);background:rgba(251,191,36,0.045)}
.rj-node.hot{border-color:rgba(52,211,153,0.25);background:rgba(52,211,153,0.045)}
.rj-node.off{opacity:0.55}
.rj-node-k{font-size:9px;color:var(--muted);font-weight:700;letter-spacing:0.06em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rj-node-v{font-family:var(--font-mono);font-size:15px;font-weight:850;color:#fff;margin-top:2px;font-variant-numeric:tabular-nums}
.rj-node.active .rj-node-v{color:var(--brand);text-shadow:0 0 12px rgba(45,212,191,0.32)}
.rj-node.warn .rj-node-v{color:var(--s-yellow)}
.rj-node.hot .rj-node-v{color:var(--s-green)}
.rj-table-wrap{position:relative;z-index:1;overflow-x:auto;border-radius:8px;border:1px solid rgba(255,255,255,0.045);background:rgba(5,7,10,0.28)}
.rj-table{width:100%;border-collapse:collapse;min-width:980px}
.rj-table th{font-size:10px;color:var(--muted);font-weight:750;text-align:left;padding:9px 10px;background:rgba(255,255,255,0.035);white-space:nowrap}
.rj-table td{font-size:11px;padding:9px 10px;border-top:1px solid rgba(255,255,255,0.045);font-family:var(--font-mono);white-space:nowrap}
.rj-row-near,.rj-row-touch,.rj-row-volume{box-shadow:inset 2px 0 0 rgba(45,212,191,0.85);background:linear-gradient(90deg,rgba(45,212,191,0.07),transparent 55%)}
.rj-row-volume{box-shadow:inset 2px 0 0 rgba(251,191,36,0.9);background:linear-gradient(90deg,rgba(251,191,36,0.065),transparent 55%)}
.rj-stage{display:inline-flex;align-items:center;justify-content:center;min-width:58px;height:20px;border-radius:5px;font-size:9px;font-weight:850;letter-spacing:0.06em;border:1px solid rgba(255,255,255,0.08)}
.rj-stage.wait{color:var(--muted);background:rgba(255,255,255,0.035)}
.rj-stage.near,.rj-stage.crossed{color:var(--brand);background:rgba(45,212,191,0.11);border-color:rgba(45,212,191,0.25);box-shadow:0 0 12px rgba(45,212,191,0.13)}
.rj-stage.touch{color:var(--s-green);background:rgba(52,211,153,0.11);border-color:rgba(52,211,153,0.25);animation:rjStagePulse 1.1s ease-in-out infinite alternate}
.rj-stage.volume{color:var(--s-yellow);background:rgba(251,191,36,0.11);border-color:rgba(251,191,36,0.25)}
.rj-distance{display:flex;align-items:center;gap:7px;min-width:120px}
.rj-dist-track{width:58px;height:4px;border-radius:999px;background:rgba(255,255,255,0.08);overflow:hidden}
.rj-dist-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--brand),var(--s-green));transform-origin:left center}
.rj-empty{position:relative;z-index:1;text-align:center;color:var(--muted);font-size:12px;padding:22px;border:1px dashed rgba(255,255,255,0.06);border-radius:8px;background:rgba(255,255,255,0.018)}
@keyframes rjPanelSweep{0%{transform:translateX(-120%)}58%,100%{transform:translateX(120%)}}
@keyframes rjDotPulse{0%,100%{opacity:.55;transform:scale(1)}50%{opacity:1;transform:scale(1.55)}}
@keyframes rjNodeFlow{0%{transform:translateX(-30%)}100%{transform:translateX(230%)}}
@keyframes rjStagePulse{0%{box-shadow:0 0 5px rgba(52,211,153,0.15)}100%{box-shadow:0 0 16px rgba(52,211,153,0.42)}}
@media(max-width:900px){.rj-rail{grid-template-columns:repeat(2,1fr)}.rj-pipeline-head{align-items:flex-start;flex-direction:column}.rj-meta{white-space:normal}}

.trader-panel-bg{position:relative;z-index:1}
.trader-panel-bg::before{content:'';position:absolute;inset:-50%;z-index:-1;background-image:linear-gradient(rgba(255,255,255,0.012) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,0.012) 1px,transparent 1px);background-size:40px 40px;background-position:center center;animation:gridMove 25s linear infinite;pointer-events:none;mask-image:radial-gradient(ellipse at top,black 20%,transparent 70%);-webkit-mask-image:radial-gradient(ellipse at top,black 20%,transparent 70%)}
@keyframes gridMove{0%{transform:translateY(0) translateX(0)}100%{transform:translateY(-40px) translateX(-40px)}}

.hud-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:20px}
.hud-trader{grid-template-columns:repeat(6,1fr);gap:8px}
.hud-demo{grid-template-columns:repeat(4,1fr);gap:8px}
.hud-trader .hud-box{padding:10px 6px}
.hud-trader .hud-val{font-size:18px}
.hud-trader .hud-title{font-size:9px}
.hud-daily-pnl{font-size:17px!important}
.hud-daily-ret{font-size:15px!important}
.t-status-uptime{font-size:13px;color:var(--brand);font-family:var(--font-mono);margin-left:6px;font-weight:600}

@media(max-width:768px){.hud-grid{grid-template-columns:repeat(3,1fr);gap:6px}.hud-trader{grid-template-columns:repeat(3,1fr)}.hud-val{font-size:14px}.hud-box{padding:8px 6px}.hud-title{font-size:8px}.hud-daily-pnl{font-size:14px!important}.hud-daily-ret{font-size:12px!important}.t-status-uptime{font-size:10px}.pos-grid{grid-template-columns:1fr}.tl-item{gap:6px;padding:6px 8px;align-items:flex-start}.tl-time{font-size:9px;min-width:36px}.tl-sym{font-size:11px}.tl-dir{font-size:8px}.tl-reason{display:none}.tl-item.expanded .tl-reason{display:inline}.tl-pnl-val{font-size:11px}.tl-pnl-pct{font-size:9px}.tl-pnl{min-width:55px}.tl-data{display:none;flex-wrap:wrap;gap:4px}.tl-item.expanded .tl-data{display:flex}.tl-item{cursor:pointer}}
@media(max-width:480px){.hud-grid{grid-template-columns:repeat(2,1fr)}.hud-val{font-size:13px}.hud-box{padding:6px 4px}.hud-title{font-size:7px}.hud-daily-pnl{font-size:13px!important}.hud-daily-ret{font-size:11px!important}.t-status-uptime{font-size:9px}}
.hud-box{background:linear-gradient(180deg,rgba(26,28,30,0.95) 0%,rgba(20,22,24,0.98) 100%);border:1px solid rgba(255,255,255,0.05);border-radius:10px;padding:16px;position:relative;overflow:hidden;box-shadow:inset 0 1px 0 rgba(255,255,255,0.02),0 4px 12px rgba(0,0,0,0.3)}
.hud-box::after{content:'';position:absolute;top:0;left:-100%;width:50%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.03),transparent);animation:laserSweep 3s infinite;pointer-events:none}
.hud-title{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em;font-weight:600;margin-bottom:4px}
.hud-val{font-size:26px;font-family:var(--font-mono);font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-0.03em}
.hud-val.g{color:var(--s-green);text-shadow:0 0 16px rgba(52,211,153,0.4);animation:breathG 2s infinite alternate}
.hud-val.r{color:var(--s-red);text-shadow:0 0 16px rgba(248,113,113,0.4);animation:breathR 2s infinite alternate}
.hud-val.neu{color:#fff;text-shadow:0 0 12px rgba(255,255,255,0.2)}
.equity-panel{background:linear-gradient(180deg,rgba(23,25,35,0.96) 0%,rgba(17,19,28,0.98) 100%);border:1px solid rgba(139,92,246,0.22);border-radius:10px;padding:14px 16px 12px;margin-bottom:18px;box-shadow:inset 0 1px 0 rgba(255,255,255,0.03),0 8px 24px rgba(0,0,0,0.32);overflow:hidden}
.eq-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.eq-title{display:flex;align-items:center;gap:8px;color:var(--s-purple);font-size:13px;font-weight:750;letter-spacing:0.02em}
.eq-sub{font-size:10px;color:var(--muted);font-family:var(--font-mono);margin-left:8px;font-weight:500}
.eq-range{display:flex;gap:2px;background:rgba(0,0,0,0.25);border:1px solid rgba(255,255,255,0.04);border-radius:7px;padding:2px}
.eq-range button{border:0;background:transparent;color:var(--text2);font-size:10px;font-weight:700;padding:4px 8px;border-radius:5px;cursor:pointer}
.eq-range button.active{background:linear-gradient(135deg,var(--s-purple),#7c3aed);color:#fff;box-shadow:0 0 12px rgba(124,58,237,0.35)}
.eq-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px}
.eq-card{background:rgba(5,7,12,0.72);border:1px solid rgba(255,255,255,0.04);border-radius:8px;padding:9px 10px;min-width:0}
.eq-label{font-size:9px;color:var(--text2);font-weight:700;margin-bottom:4px;white-space:nowrap}
.eq-value{font-family:var(--font-mono);font-size:16px;font-weight:850;font-variant-numeric:tabular-nums}
.eq-value.g{color:var(--s-green)}.eq-value.r{color:var(--s-red)}.eq-value.neu{color:#fff}
.eq-chart-wrap{position:relative;height:240px;border-top:1px solid rgba(255,255,255,0.03)}
.eq-empty{display:flex;height:220px;align-items:center;justify-content:center;color:var(--muted);font-size:12px;border:1px dashed rgba(255,255,255,0.05);border-radius:8px}
.eq-chart{width:100%;height:100%;display:block}
.eq-legend{display:flex;justify-content:center;gap:18px;border-top:1px solid rgba(255,255,255,0.04);padding-top:9px;font-size:10px;color:var(--muted);font-family:var(--font-mono)}
.eq-dot{display:inline-block;width:14px;height:2px;background:var(--s-green);vertical-align:middle;margin-right:6px}
.eq-dot.base{background:rgba(148,163,184,0.65);border-top:1px dashed rgba(148,163,184,0.9)}
@media(max-width:768px){.eq-head{align-items:flex-start;flex-direction:column}.eq-metrics{grid-template-columns:repeat(2,1fr)}.eq-chart-wrap{height:210px}.eq-range button{padding:4px 7px}}
@media(max-width:480px){.eq-metrics{grid-template-columns:1fr}.eq-value{font-size:15px}.eq-chart-wrap{height:190px}}
.r-panel{background:linear-gradient(180deg,rgba(18,22,27,.97),rgba(11,14,18,.99));border:1px solid rgba(52,211,153,.18);border-radius:10px;padding:14px 16px;margin:-8px 0 18px;box-shadow:inset 0 1px 0 rgba(255,255,255,.025),0 8px 24px rgba(0,0,0,.28)}
.r-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.r-title{font-size:12px;font-weight:800;letter-spacing:.08em;color:var(--s-green)}
.r-meta{font-size:9px;color:var(--muted);font-family:var(--font-mono)}
.r-metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:7px}
.r-card{min-width:0;padding:9px;background:rgba(4,7,10,.72);border:1px solid rgba(255,255,255,.045);border-radius:8px}
.r-label{font-size:8px;color:var(--text2);font-weight:700;white-space:nowrap;margin-bottom:4px}
.r-value{font:800 15px var(--font-mono);font-variant-numeric:tabular-nums}
.r-value.g{color:var(--s-green)}.r-value.r{color:var(--s-red)}.r-value.neu{color:#fff}.r-value.p{color:var(--s-purple)}
.r-chart-wrap{height:150px;margin-top:10px;border-top:1px solid rgba(255,255,255,.04)}
.r-chart{width:100%;height:100%;display:block}
.r-details{margin-top:8px;border-top:1px solid rgba(255,255,255,.04);padding-top:7px}
.r-details summary{cursor:pointer;color:var(--text2);font-size:10px}
.r-detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}
.r-detail-box{font-size:9px;color:var(--muted);background:rgba(4,7,10,.55);padding:8px;border-radius:7px}
@media(max-width:900px){.r-metrics{grid-template-columns:repeat(3,1fr)}}
@media(max-width:600px){.r-metrics{grid-template-columns:repeat(2,1fr)}.r-detail-grid{grid-template-columns:1fr}.r-head{align-items:flex-start;flex-direction:column}}
@keyframes breathG{0%{text-shadow:0 0 10px rgba(52,211,153,0.2)}100%{text-shadow:0 0 24px rgba(52,211,153,0.6)}}
@keyframes breathR{0%{text-shadow:0 0 10px rgba(248,113,113,0.2)}100%{text-shadow:0 0 24px rgba(248,113,113,0.6)}}

.pos-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.pos-card{background:rgba(15,17,18,0.9);border:1px solid var(--border);border-radius:12px;padding:16px;position:relative;overflow:hidden;transition:all 0.3s cubic-bezier(.4,0,.2,1)}
.pos-card.profit{border-color:rgba(52,211,153,0.3);box-shadow:0 0 20px rgba(52,211,153,0.05),inset 0 0 15px rgba(52,211,153,0.02)}
.pos-card.loss{border-color:rgba(248,113,113,0.3);box-shadow:0 0 20px rgba(248,113,113,0.05),inset 0 0 15px rgba(248,113,113,0.02)}
.pos-card:hover{transform:translateY(-3px)}
.pos-card.profit:hover{box-shadow:0 8px 30px rgba(52,211,153,0.15),inset 0 0 20px rgba(52,211,153,0.05)}
.pos-card.loss:hover{box-shadow:0 8px 30px rgba(248,113,113,0.15),inset 0 0 20px rgba(248,113,113,0.05)}
.pos-card::before{content:'';position:absolute;left:0;width:100%;height:2px;background:rgba(255,255,255,0.1);opacity:0.5;animation:cardScan 3s linear infinite;pointer-events:none}
.pos-card.profit::before{background:linear-gradient(90deg,transparent,rgba(52,211,153,0.8),transparent)}
.pos-card.loss::before{background:linear-gradient(90deg,transparent,rgba(248,113,113,0.8),transparent)}
@keyframes cardScan{0%{top:-10%;opacity:0}10%{opacity:1}90%{opacity:1}100%{top:110%;opacity:0}}

.pc-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.pc-sym{font-size:16px;font-weight:800;color:#fff;letter-spacing:-0.02em}
.pc-dir{font-size:11px;padding:3px 8px;border-radius:4px;font-weight:700}
.pc-dir.long{background:rgba(52,211,153,0.1);color:var(--s-green)}
.pc-dir.short{background:rgba(248,113,113,0.1);color:var(--s-red)}
.pc-data{display:flex;justify-content:space-between;font-size:11px;margin-bottom:5px;font-family:var(--font-mono)}
.pc-label{color:var(--muted)}
.pc-val{color:var(--text);font-weight:500}
.pc-pnl-box{display:flex;justify-content:space-between;align-items:flex-end;margin-top:14px;padding-top:14px;border-top:1px dashed rgba(255,255,255,0.05)}
.pc-pnl-val{font-size:22px;font-weight:800;font-family:var(--font-mono)}
.pos-card.profit .pc-pnl-val{color:var(--s-green);text-shadow:0 0 12px rgba(52,211,153,0.4);animation:breathG 2s infinite alternate}
.pos-card.loss .pc-pnl-val{color:var(--s-red);text-shadow:0 0 12px rgba(248,113,113,0.4);animation:breathR 2s infinite alternate}
.pc-btn{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);color:#fff;padding:5px 14px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;transition:0.2s}
.pc-btn:hover{background:var(--s-red);border-color:var(--s-red);box-shadow:0 0 15px rgba(248,113,113,0.4)}
.pc-trail-shimmer{text-align:center;margin-top:10px;padding-top:10px;border-top:1px dashed rgba(255,255,255,0.04);font-size:10px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;
  background:linear-gradient(110deg,var(--s-green) 30%,#fff 50%,var(--s-green) 70%);background-size:200% 100%;
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  animation:metallicShimmer 2s linear infinite;}

.pos-monitor-head{display:flex;align-items:center;gap:10px;margin-bottom:12px;padding:10px 14px;background:rgba(15,17,18,0.6);border:1px solid rgba(45,212,191,0.1);border-radius:8px;position:relative;overflow:hidden}
.pos-monitor-head::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(45,212,191,0.03),transparent);animation:monitorPulse 3s ease-in-out infinite}
.pos-monitor-dot{width:6px;height:6px;border-radius:50%;background:var(--brand);box-shadow:0 0 6px var(--brand),0 0 12px rgba(45,212,191,0.3);flex-shrink:0;animation:monitorDotPulse 1s ease-in-out infinite}
.pos-monitor-wave{display:flex;align-items:center;gap:2px;margin-left:auto;opacity:0.6}
.pos-monitor-wave::before,.pos-monitor-wave::after{content:'';width:2px;border-radius:1px;background:var(--brand);animation:waveBar 0.8s ease-in-out infinite alternate}
.pos-monitor-wave::before{height:8px;animation-delay:0s}
.pos-monitor-wave::after{height:14px;animation-delay:0.2s}
@keyframes monitorPulse{0%,100%{opacity:0}50%{opacity:1}}
@keyframes monitorDotPulse{0%,100%{opacity:0.6;transform:scale(1)}50%{opacity:1;transform:scale(1.5)}}
@keyframes waveBar{0%{opacity:0.3;transform:scaleY(0.5)}100%{opacity:1;transform:scaleY(1)}}

.tl-list{display:flex;flex-direction:column;gap:2px}
.tl-item{display:flex;align-items:center;gap:12px;padding:8px 12px;background:rgba(15,17,18,0.6);border-left:2px solid var(--border);border-radius:0 6px 6px 0;transition:all 0.2s}
.tl-item:hover{background:rgba(20,22,24,0.8);border-left-color:var(--border2)}
.tl-item.tl-win{border-left-color:rgba(52,211,153,0.3)}
.tl-item.tl-loss{border-left-color:rgba(248,113,113,0.3)}
.tl-time{font-family:var(--font-mono);font-size:10px;color:var(--muted);min-width:42px;text-align:center}
.tl-body{flex:1;min-width:0}
.tl-head{display:flex;align-items:center;gap:8px;margin-bottom:2px}
.tl-sym{font-size:13px;font-weight:700;color:#fff;letter-spacing:-0.01em}
.tl-dir{font-size:10px;font-weight:600;padding:1px 5px;border-radius:3px}
.tl-dir.g{background:rgba(52,211,153,0.1);color:var(--s-green)}
.tl-dir.r{background:rgba(248,113,113,0.1);color:var(--s-red)}
.tl-reason{font-size:9.5px;color:var(--muted);margin-left:auto}
.tl-data{display:flex;gap:12px;font-family:var(--font-mono);font-size:10px;color:var(--muted)}
.tl-data span{white-space:nowrap}
.tl-src{color:var(--s-orange)!important}
.tl-src.ok{color:var(--s-green)!important}
.tl-pnl{text-align:right;min-width:70px}
.tl-pnl-val{font-family:var(--font-mono);font-size:13px;font-weight:700;display:block}
.tl-pnl-pct{font-family:var(--font-mono);font-size:10px;font-weight:600}



/* ====== AXIOM 终极网关页 ====== */
.space-grid{position:absolute;inset:-50%;z-index:0;background-image:linear-gradient(rgba(255,255,255,0.1) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,0.1) 1px,transparent 1px);background-size:50px 50px;background-position:center center;transform:perspective(600px) rotateX(60deg) translateY(-100px) translateZ(-200px);animation:gridFly 15s linear infinite;mask-image:radial-gradient(circle at center,black 40%,transparent 90%);-webkit-mask-image:radial-gradient(circle at center,black 40%,transparent 90%);pointer-events:none}
@keyframes gridFly{0%{transform:perspective(600px) rotateX(60deg) translateY(0) translateZ(-200px)}100%{transform:perspective(600px) rotateX(60deg) translateY(50px) translateZ(-200px)}}
.cursor-glow{position:absolute;width:800px;height:800px;background:radial-gradient(circle,rgba(45,212,191,0.25) 0%,rgba(45,212,191,0.08) 40%,transparent 70%);border-radius:50%;pointer-events:none;z-index:1;transform:translate(-50%,-50%);transition:width 0.2s,height 0.2s}
@property --angle{syntax:'<angle>';initial-value:0deg;inherits:false}
@keyframes spinBorder{to{--angle:360deg}}
.auth-wrapper{position:relative;z-index:10;width:400px;animation:floatCard 6s ease-in-out infinite}
@keyframes floatCard{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
.auth-card-gw{background:linear-gradient(180deg,rgba(21,23,24,0.85) 0%,rgba(15,17,18,0.95) 100%);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-radius:16px;padding:48px 40px;position:relative;text-align:center;box-shadow:0 30px 60px rgba(0,0,0,0.6),inset 0 1px 0 rgba(255,255,255,0.05);z-index:2}
.auth-wrapper::before{content:'';position:absolute;inset:-1.5px;border-radius:18px;background:conic-gradient(from var(--angle),transparent 60%,var(--brand) 100%);animation:spinBorder 3s linear infinite;z-index:1;opacity:0.8}
.auth-wrapper::after{content:'';position:absolute;inset:0;border-radius:16px;background:var(--bg);z-index:1}
.gw-logo{display:inline-flex;width:48px;height:48px;background:rgba(45,212,191,0.05);border:1px solid rgba(45,212,191,0.2);border-radius:12px;align-items:center;justify-content:center;margin-bottom:16px;box-shadow:0 0 20px rgba(45,212,191,0.1)}
.gw-logo svg{width:24px;height:24px;fill:var(--brand)}
.gw-logo-text{font-size:24px;font-weight:800;letter-spacing:2px;background:linear-gradient(110deg,#fff 30%,var(--brand) 80%);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.gw-sub{font-size:11px;color:var(--muted);letter-spacing:0.2em;text-transform:uppercase;margin-top:6px;font-weight:600}
.gw-input{position:relative;margin-bottom:24px;text-align:left}
.gw-input label{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.1em;margin-bottom:8px;font-weight:600}
.gw-input input{width:100%;background:rgba(0,0,0,0.3);border:1px solid var(--border);padding:14px 16px;color:#fff;font-size:14px;font-family:var(--font-mono);border-radius:8px;outline:none;transition:all 0.3s;box-shadow:inset 0 2px 4px rgba(0,0,0,0.2)}
.gw-input input:focus{border-color:var(--brand);box-shadow:0 0 15px rgba(45,212,191,0.15),inset 0 2px 4px rgba(0,0,0,0.2)}
.gw-input::after{content:'';position:absolute;bottom:0;left:50%;transform:translateX(-50%);width:0;height:1px;background:var(--brand);transition:width 0.3s ease;box-shadow:0 0 8px var(--brand)}
.gw-input:focus-within::after{width:80%}
.gw-btn{width:100%;background:var(--brand);color:#000;font-size:14px;font-weight:800;letter-spacing:0.1em;padding:16px;border:none;border-radius:8px;cursor:pointer;position:relative;overflow:hidden;transition:all 0.3s;box-shadow:0 0 20px rgba(45,212,191,0.2);margin-top:8px}
.gw-btn::before{content:'';position:absolute;top:0;left:-100%;width:40%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.4),transparent);transform:skewX(-20deg);transition:none;pointer-events:none}
.gw-btn:hover{box-shadow:0 0 30px rgba(45,212,191,0.4);transform:translateY(-2px)}
.gw-btn:hover::before{left:200%;transition:left 0.6s ease-in-out}
.gw-btn:active{transform:translateY(1px)}
.gw-term{max-height:0;overflow:hidden;margin-top:0;background:rgba(0,0,0,0.5);border-radius:8px;font-family:var(--font-mono);font-size:11px;text-align:left;color:var(--brand);transition:all 0.4s cubic-bezier(.4,0,.2,1)}
.gw-term.active{max-height:130px;margin-top:24px;padding:12px;border:1px solid rgba(45,212,191,0.2)}
.gw-log-line{margin-bottom:4px;opacity:0;animation:gateTypeLine 0.1s forwards;line-height:1.4}
.gw-log-line.error{color:var(--s-red)}
.gw-log-cursor{display:inline-block;width:6px;height:12px;background:var(--brand);animation:blink 0.8s infinite;vertical-align:middle}
@keyframes gateTypeLine{to{opacity:1}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
.gw-links{margin-top:24px;font-size:11px;color:var(--muted);font-weight:500}
.gw-links a{color:var(--brand);text-decoration:none;margin:0 8px;transition:0.2s;opacity:0.7}
.gw-links a:hover{opacity:1;text-shadow:0 0 8px var(--brand)}
#authOverlay{overflow:hidden}
@media(max-width:768px){div#authOverlay .auth-wrapper{width:340px;max-width:92vw}div#authOverlay .auth-card-gw{padding:32px 24px}div#authOverlay .gw-logo-text{font-size:20px}div#authOverlay .gw-input input{padding:12px 14px;font-size:13px}div#authOverlay .gw-btn{padding:14px;font-size:13px}body.auth-locked{overflow:hidden;position:fixed;width:100%}}
</style></head><body>
<div id="authOverlay" style="position:fixed;inset:0;z-index:9999;background:var(--bg);display:flex;align-items:center;justify-content:center">
<div class="space-grid"></div><div class="cursor-glow" id="cursorGlow"></div>
<div class="auth-wrapper"><div class="auth-card-gw">
<div style="margin-bottom:32px"><div class="gw-logo"><svg viewBox="0 0 24 24"><path d="M13 10V3L4 14h7v7l9-11h-7z"/></svg></div><div class="gw-logo-text">AXIOM QUANT</div><div class="gw-sub">多因子量化信号探测与自动交易引擎 v1.0</div></div>
<div id="gwLoginForm">
<div class="gw-input"><label>系统授权标识 (Username)</label><input type="text" id="authUser" placeholder="输入用户名" autocomplete="off"></div>
<div class="gw-input"><label>多维加密密钥 (Password)</label><input type="password" id="authPwd" placeholder="••••••" autocomplete="off" onkeydown="if(event.key==='Enter')doAuthLogin()"></div>
<button class="gw-btn" onclick="doAuthLogin()">建立安全连接</button>
</div>
<div id="gwRegForm" style="display:none">
<div class="gw-input"><label>用户名 (3位以上)</label><input type="text" id="regUser" placeholder="设置用户名" autocomplete="off"></div>
<div class="gw-input"><label>邮箱 (用于找回密码)</label><input type="text" id="regEmail" placeholder="your@email.com" autocomplete="off"></div>
<div class="gw-input"><label>密码 (6位以上)</label><input type="password" id="regPwd" placeholder="设置密码"></div>
<div class="gw-input"><label>确认密码</label><input type="password" id="regPwd2" placeholder="再次输入密码"></div>
<button class="gw-btn" onclick="doAuthReg()">注 册</button>
</div>
<div id="gwForgotForm" style="display:none">
<div class="gw-input"><label>注册邮箱</label><input type="text" id="forgotEmail" placeholder="输入注册时的邮箱" autocomplete="off"></div>
<button class="gw-btn" onclick="doForgotPwd()">获取验证码</button>
<div id="gwResetFields" style="display:none;margin-top:16px">
<div class="gw-input"><label>验证码</label><input type="text" id="forgotCode" placeholder="输入验证码"></div>
<div class="gw-input"><label>新密码 (6位以上)</label><input type="password" id="forgotPwd" placeholder="设置新密码"></div>
<button class="gw-btn" onclick="doForgotPwd()">重置密码</button>
</div>
</div>
<div class="gw-term" id="gwTerm"></div>
<p id="authMsg" style="color:var(--s-red);font-size:12px;margin-top:10px;text-align:center;display:none"></p>
<div class="gw-links">
<a href="#" onclick="switchGwForm('login');return false">登录</a> | <a href="#" onclick="switchGwForm('reg');return false">注册</a> | <a href="#" onclick="switchGwForm('forgot');return false">忘记密码</a>
</div>
</div></div></div>
    </div>
  </div>
</div>


<div class="progress-bar"><div class="progress-fill" id="progressBar"></div></div>
<div class="sidebar-overlay" id="sidebarOverlay" onclick="closeSidebar()"></div>
<header>
  <button class="hamburger" onclick="toggleSidebar()" title="菜单">☰</button>
  <div style="display:flex;flex-direction:column;gap:2px">
    <h1><span class="ic-bolt"></span><span class="shimmer" style="letter-spacing:1px;font-weight:800">AXIOM</span> <span style="font-weight:400;color:var(--text2)">Quant</span></h1>
    <span style="font-size:10px;color:var(--muted);letter-spacing:0.04em;font-weight:400">多因子量化探测与合约自动交易系统</span>
  </div>
  <div class="header-right">
    <span class="status-dot" id="dot"></span>
    <span id="statusText" style="font-weight:600;color:var(--text)">就绪</span>
    <span style="opacity:0.2">│</span>
    <span id="statusTime" style="color:var(--s-cyan)">--</span>
    <span style="opacity:0.2">│</span>
    <span id="statusCount" style="color:var(--s-gold);font-weight:700">0 结果</span>
    <span style="opacity:0.2">│</span>
    <span id="headerUser" style="font-size:11px;color:var(--text2)"></span>
    <a href="#" onclick="doLogout()" style="font-size:10px;color:var(--muted)">退出</a>
  </div>
</header>
<main>
<div class="sidebar" id="sidebar"></div>
<div class="content">
  <div class="toolbar">
    <button class="btn" id="scanBtn" onclick="doScan()">▶ 开始扫描</button>
    <button class="btn btn-stop" id="stopBtn" onclick="stopScan()">■ 停止</button>
    <span style="font-size:12px;color:var(--s-cyan);font-weight:600" id="scanLabel">BTC 大盘行情监控</span>
  </div>
  <div id="featureDesc"></div>
  <div class="stats" id="stats"></div>
  <div id="main"><div class="empty-state"><div class="ic-empty"></div><h3>选择左侧模式，点击开始扫描</h3><p style="margin-top:6px;font-size:13px">快捷键 1-9 切换 · 20线程并行 · 实时数据</p></div></div>
</div>
</main>
<div class="footer"><span class="ic-dot ic-strong"></span> 系统运行中 &nbsp;|&nbsp; <span class="ic-dot ic-mid"></span> 延迟: 12ms &nbsp;|&nbsp; <span class="ic-dot ic-weak"></span> 数据源: BINANCE U本位合约 &nbsp;|&nbsp; AXIOM QUANTITATIVE ENGINE V1.0.0</div>
<script>
// ===== INIT =====
let D={{ data|tojson }};
let DEMO_CFG={{ demo_cfg_json|safe }};
let cur='squeeze_4h';
let pollTimer=null;
var _reflowPolling=false, _pollingScan=false;

// ===== SIDEBAR =====
let groups=[
  ['glow','AXIOM 系统介绍',[['intro','AXIOM交易系统全景解析','W']]],
  ['btc','BTC 大盘行情监控',[['btc_monitor','BTC行情','B']]],
  ['glow','RJ指标策略',[['rj_indicator','RJ/BBKD','J']]],
  ['glow','演示引擎',[['demo','净值看板','D']]],
  ['breakout','动量突破',[['breakout_4h','4H','6'],['breakout_1h','1H','7'],['breakout_15m','15m','8'],['breakout_1d','日线','9']]],
  ['reflow','动能回流',[['reflow_1h','1H首次回流','M']]],
  ['trader','自动交易',[['trader','交易面板','T']]],
  ['squeeze','收敛扫选',[['squeeze_4h','4H','1'],['squeeze_1h','1H','2'],['squeeze_15m','15m','3'],['squeeze_1d','日线','4'],['squeeze_1w','周线','5']]],
  ['short','做空扫选',[['short_4h','4H','9'],['short_1h','1H','0']]],
  ['volume','高换手',[['volume_4h','4H','-'],['volume_1h','1H','-']]],
  ['funding','资金费率',[['funding','费率','-']]],
  ['glow','仓位计算器',[['calculator','计算器','C']]],
  ['glow','浮云滚仓',[['roller','滚仓策略','R']]],
  ['cyber','CryptoRank 雷达',[
    ['cryptorank_home','首页工作台','H'],
    ['cryptorank_funding','融资雷达','F'],
    ['cryptorank_opportunities','机会页','O']
  ]],
];

// ═══ 功能描述词典 ═══
var _desc={
  intro:'AXIOM 机构级量化交易系统全景解析：从动量突破识别、结构回归确认，到短周期狙击与失速退出，完整理解系统如何筛选趋势、清理弱单、留下真正有希望的行情。',
  btc_monitor:'BTC 多周期综合监控，覆盖四个时间维度，追踪价格结构与波动状态，辅助判断市场整体方向。仅监控，不执行交易。',
  rj_indicator:'复刻视频里的加强版RJ/BBKD策略：绿色J线作为快线，紫线默认用贴近原版的K线校准口径，结合布林趋势色、三角关键K和收盘突破确认。仅图表分析，不执行交易。',
  breakout_4h:'全市场方向性信号扫描（4H周期）。波动压缩后的突破候选，经多层结构验证，评分≥60为高可信度。',
  breakout_1h:'方向性信号扫描（1H周期），灵敏度较高，适合捕捉短线机会。',
  breakout_15m:'方向性信号扫描（15m周期），灵敏度最高。建议结合大周期结构辅助判断。',
  breakout_1d:'方向性信号扫描（日线周期），结构稳定性最高，适合中长线参考。',
  reflow_1h:'1H 首次回流看板：持续保留当日信号，展示自动扫描状态、最新回流窗口与质量分层，辅助复核而不执行交易。',
  trader:'自动交易引擎。配置API后自动执行：信号扫描→结构止损→动态追踪。内置多级风控与入场验证。',
  squeeze_4h:'波动压缩扫描（4H周期）。监测价格波动收敛状态，压缩越紧=蓄力越充分。',
  squeeze_1h:'波动压缩扫描（1H周期），全市场波动收敛程度排序。',
  squeeze_15m:'波动压缩扫描（15m周期），高频监测。',
  squeeze_1d:'波动压缩扫描（日线周期），大级别收敛信号。',
  squeeze_1w:'波动压缩扫描（周线周期），长周期结构参考。',
  short_4h:'做空专项监测（4H周期），筛选向下突破的结构验证信号。',
  short_1h:'做空专项监测（1H周期），高频做空信号筛选。',
  volume_4h:'成交量异常监测（4H周期），筛选换手率显著放大的标的。',
  volume_1h:'成交量异常监测（1H周期），更高灵敏度的放量检测。',
  funding:'永续合约资金费率排名。极端值提示市场情绪可能反转。',
  calculator:'固定风险仓位计算器。输入本金与止损距离，自动计算开仓数量。',
  roller:'仓位路径推演。输入初始成本与本金，自动演算递进杠杆的复利路径。',
  cryptorank_home:'中文 CryptoRank 机会雷达首页。把市场总览、头部币种、涨跌榜、Upcoming、融资和今日摘要压缩成一个适合中文用户的工作台。',
  cryptorank_funding:'中文融资雷达。把近期 Funding Rounds 压成适合中文创作者和研究者使用的信号面板，标注重点基金和融资阶段。',
  cryptorank_opportunities:'中文机会页。把 Upcoming IDO/ICO 和空投活动放在同一页面，用中文告诉你今天优先跟踪哪几个项目。',
  default:'Axiom Quant v1.0 — 多因子量化探测与自动化合约交易执行系统。'
};
function setDesc(tab){
  document.getElementById('featureDesc').textContent=_desc[tab]||_desc['default'];
}

(function buildSidebar(){
  var sb=document.getElementById('sidebar');
  var h='';
  var glowMap={btc:'menu-glow-btc', breakout:'menu-glow-blast', trader:'menu-glow-trader', glow:'menu-glow-calc', cyber:'menu-glow-cyber'};
  for(var gi=0; gi<groups.length; gi++){
    var g=groups[gi];
    var gtype=g[0], gname=g[1], items=g[2];
    var glowClass=glowMap[gtype]||'';
    if(items.length===1){
      var item=items[0];
      h+='<div class="menu-group '+glowClass+'"><div class="menu-title" style="cursor:pointer" onclick="selectTab(\''+item[0]+'\')">'+gname+'</div>';
      h+='<div class="nav-item" id="nav_'+item[0]+'" onclick="selectTab(\''+item[0]+'\')">'+item[1]+'<span class="hotkey">'+item[2]+'</span></div></div>';
    }else{
      var mid='mg_'+gi;
      h+='<div class="menu-group '+glowClass+'"><div class="menu-title" onclick="toggleMenu(\''+mid+'\')">'+gname+'<span class="arrow">▶</span></div>';
      h+='<div class="menu-items" id="'+mid+'">';
      for(var j=0; j<items.length; j++){
        var id=items[j][0], label=items[j][1], key=items[j][2];
        h+='<div class="nav-item" id="nav_'+id+'" onclick="selectTab(\''+id+'\');event.stopPropagation()">'+label+'<span class="hotkey">'+key+'</span></div>';
      }
      h+='</div></div>';
    }
  }
  sb.innerHTML=h;
})();

function selectTab(tid){
  var el=document.getElementById('nav_'+tid);
  show(tid, el);
  closeSidebar();
}

function copySymbol(sym, el){
  // 兼容 HTTP 环境的复制方案
  var ta=document.createElement('textarea');
  ta.value=sym; ta.style.position='fixed'; ta.style.left='-9999px';
  document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0,99999);
  document.execCommand('copy'); document.body.removeChild(ta);
  if(el){el.classList.add('copied'); setTimeout(function(){el.classList.remove('copied');},800);}
}
function toggleSidebar(){
  var sb=document.getElementById('sidebar');
  var ov=document.getElementById('sidebarOverlay');
  if(sb.classList.contains('open')){sb.classList.remove('open');ov.classList.remove('show');}
  else{sb.classList.add('open');ov.classList.add('show');}
}
function closeSidebar(){
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebarOverlay').classList.remove('show');
}

function toggleMenu(gid){
  var items=document.getElementById(gid);
  if(!items) return;
  var title=items.previousElementSibling;
  if(items.classList.contains('open')){
    items.classList.remove('open');
    title.classList.remove('open');
  }else{
    items.classList.add('open');
    title.classList.add('open');
  }
}

// ===== TAB SWITCHING =====
function show(tab, btn){
  var prev=cur;
  cur=tab;
  if(prev!==tab){_lastTraderData=null;}
  if(tab!=='trader' && traderPoll){clearInterval(traderPoll); traderPoll=null; _traderInitDone=false; stopEngineHeartbeat();}
  if(tab!=='btc_monitor' && btcPoll){clearInterval(btcPoll); btcPoll=null;}
  if(tab!=='demo' && demoPoll){clearInterval(demoPoll); demoPoll=null;}
  if(prev==='reflow_1h' && tab!=='reflow_1h'){
    _reflowPolling=false;
    if(pollTimer && !_pollingScan){clearInterval(pollTimer); pollTimer=null;}
  }
  var all=document.querySelectorAll('.nav-item');
  for(var i=0; i<all.length; i++){all[i].classList.remove('active');}
  if(btn) btn.classList.add('active');
  var rows=D[tab]||[];
  var resultCount=tab==='reflow_1h'&&rows&&Array.isArray(rows.rows)?rows.rows.length:(Array.isArray(rows)?rows.length:0);
  setDesc(tab);
  document.getElementById('statusCount').textContent=resultCount+' 结果';
  if(btn){
    var label=btn.textContent.replace(/\d/g,'').trim();
    document.getElementById('scanLabel').textContent='当前: '+label;
  }
  render(tab, rows);
  if(tab==='reflow_1h'){
    _reflowPolling=true;
    if(!pollTimer){pollTimer=setInterval(pollResults,1500); pollResults();}
  }
}

function render(tab, rows){
  var isV=tab.indexOf('volume')===0, isS=tab.indexOf('short')===0, isF=tab==='funding', isB=tab.indexOf('breakout')===0, isTrader=tab==='trader', isBtc=tab==='btc_monitor', isCalc=tab==='calculator', isRoller=tab==='roller', isDemo=tab==='demo', isIntro=tab==='intro', isRj=tab==='rj_indicator', isCR=tab.indexOf('cryptorank_')===0;
  var isNonScan=isIntro||isBtc||isCalc||isRoller||isDemo||isRj||isCR;
  document.getElementById('scanBtn').style.display=isNonScan?'none':'';
  document.getElementById('stopBtn').style.display=isNonScan?'none':'';
  if(isIntro){renderIntro(); return;}
  if(isTrader){renderTrader(); return;}
  if(isBtc){renderBtcMonitor(); return;}
  if(isRj){renderRjIndicator(); return;}
  if(isCalc){renderCalculator(); return;}
  if(isRoller){renderRoller(); return;}
  if(isDemo){renderDemo(); return;}
  if(isCR){renderCryptorank(tab.replace('cryptorank_','')); return;}
  if(tab==='reflow_1h'){renderMomentumReflow(rows); return;}
  if(isF){renderFunding(rows); return;}
  if(isB && rows && rows.length>0){renderBreakout(rows); return;}

  // Generic table rendering
  if(!rows || rows.length===0){
    document.getElementById('stats').innerHTML='';
    document.getElementById('main').innerHTML='<div class="empty-state"><div class="ic-empty"></div><h3>暂无数据</h3><p>点击 开始扫描</p></div>';
    return;
  }
  var avgSpread=0;
  for(var i=0; i<rows.length; i++){avgSpread+=rows[i].spread;}
  avgSpread/=rows.length;
  var insideCount=0;
  for(var i=0; i<rows.length; i++){if(rows[i].inside) insideCount++;}
  document.getElementById('stats').innerHTML=
    '<div class="stat-card"><div class="label">结果数</div><div class="value c">'+rows.length+'</div></div>'+
    '<div class="stat-card"><div class="label">平均离散</div><div class="value '+(avgSpread<4?'g':avgSpread<8?'y':'')+'">'+avgSpread.toFixed(2)+'%</div></div>'+
    (!isS?'<div class="stat-card"><div class="label">区间内</div><div class="value g">'+insideCount+'</div></div>':'');

  var h='<table><thead><tr><th>#</th><th>交易对</th><th>离散</th>';
  if(!isS && !isV) h+='<th>状态</th>';
  h+='<th>价格</th><th>涨跌</th>';
  if(isS) h+='<th>评分</th>';
  if(isV) h+='<th>换手</th>';
  h+='<th>24h量M</th></tr></thead><tbody>';
  for(var i=0; i<rows.length; i++){
    var r=rows[i];
    var star=r.spread<2?'***':r.spread<4?'** ':r.spread<6?'*  ':'   ';
    var rowCls=r.spread<2?'row-blast':r.spread<4?'row-strong':'';
    if(isS){star=r.score>=70?'★★★':r.score>=50?'★★ ':r.score>=30?'★  ':'   '; rowCls=r.score>=70?'row-blast':r.score>=50?'row-strong':'';}
    h+='<tr class="'+rowCls+'"><td>'+ (i+1) +'</td><td><span class="copy-sym" onclick="event.stopPropagation();copySymbol(\''+r.symbol+'\',this)" title="复制"></span> <span class="star g">'+star+'</span> <b>'+r.symbol.replace('USDT','')+'</b></td>';
    h+='<td class="'+(r.spread<4?'g':r.spread<8?'y':'')+'">'+r.spread.toFixed(2)+'%</td>';
    if(!isS && !isV) h+='<td><span class="badge '+(r.inside?'badge-in':'badge-out')+'">'+(r.inside?'内':'外')+'</span> '+r.trend+'</td>';
    h+='<td>'+r.price+'</td>';
    h+='<td class="'+(r.chg>0?'g':r.chg<0?'r':'')+'">'+(r.chg>0?'+':'')+r.chg+'%</td>';
    if(isS) h+='<td><b class="r">'+r.score+'</b></td>';
    if(isV) h+='<td><b class="c">'+r.score.toFixed(1)+'M</b></td>';
    h+='<td>'+r.vol+'M</td></tr>';
  }
  h+='</tbody></table>';
  document.getElementById('main').innerHTML=h;
}

// ===== FUNDING RATE =====
function renderFunding(data){
  var neg=data.negative||[], pos=data.positive||[];
  document.getElementById('stats').innerHTML=
    '<div class="stat-card"><div class="label">负费率(做多拥挤)</div><div class="value r">'+neg.length+' 个</div></div>'+
    '<div class="stat-card"><div class="label">正费率(做空拥挤)</div><div class="value g">'+pos.length+' 个</div></div>';
  var h='<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">';
  h+='<div><h3 style="color:var(--s-red);margin-bottom:8px;font-size:14px">负费率(空付多)</h3><table><thead><tr><th>#</th><th>交易对</th><th>费率</th><th>标记价</th></tr></thead><tbody>';
  for(var i=0; i<neg.length; i++){h+='<tr><td>'+(i+1)+'</td><td><span class="copy-sym" onclick="event.stopPropagation();copySymbol(\''+neg[i].symbol+'\',this)" title="复制"></span> <b>'+neg[i].symbol.replace('USDT','')+'</b></td><td class="r"><b>'+neg[i].score.toFixed(4)+'%</b></td><td>'+neg[i].price+'</td></tr>';}
  h+='</tbody></table></div>';
  h+='<div><h3 style="color:var(--brand);margin-bottom:8px;font-size:14px">正费率(多付空)</h3><table><thead><tr><th>#</th><th>交易对</th><th>费率</th><th>标记价</th></tr></thead><tbody>';
  for(var i=0; i<pos.length; i++){h+='<tr><td>'+(i+1)+'</td><td><span class="copy-sym" onclick="event.stopPropagation();copySymbol(\''+pos[i].symbol+'\',this)" title="复制"></span> <b>'+pos[i].symbol.replace('USDT','')+'</b></td><td class="g"><b>+'+pos[i].score.toFixed(4)+'%</b></td><td>'+pos[i].price+'</td></tr>';}
  h+='</tbody></table></div></div>';
  document.getElementById('main').innerHTML=h;
}

// ===== BREAKOUT RENDERING =====
function renderBreakout(rows){
  // 按评分降序排列
  rows.sort(function(a,b){return b.score-a.score;});
  var top=rows.slice(0,10);
  var longs=0, shorts=0, avgScore=0;
  for(var i=0; i<rows.length; i++){if(rows[i].direction==='LONG')longs++;else shorts++; avgScore+=rows[i].score;}
  avgScore/=rows.length||1;
  document.getElementById('stats').innerHTML=
    '<div class="stat-bar"><span class="stat-bar-item"><span class="stat-bar-label">总信号</span> <b>'+rows.length+'</b></span><span class="stat-bar-sep"></span><span class="stat-bar-item"><span class="stat-bar-label">做多</span> <b class="g">'+longs+'</b></span><span class="stat-bar-sep"></span><span class="stat-bar-item"><span class="stat-bar-label">做空</span> <b class="r">'+shorts+'</b></span><span class="stat-bar-sep"></span><span class="stat-bar-item"><span class="stat-bar-label">均分</span> <b class="c">'+avgScore.toFixed(1)+'</b></span></div>';
  var h='<table><thead><tr><th>#</th><th>方向</th><th>交易对</th><th>评分</th><th>收敛度</th><th>确认</th><th>HT拐点</th><th>突破%</th><th>K线</th><th>盘整</th><th>放量</th><th>大周期</th><th>价格</th></tr></thead><tbody>';
  for(var i=0; i<top.length; i++){
    var r=top[i];
    var dir=r.direction==='LONG'?'<span class="ic-dot ic-bull"></span>多':'<span class="ic-dot ic-bear"></span>空';
    var cls=r.direction==='LONG'?'g':'r';
    var starCls=r.score>=70?'ic-mid':r.score>=50?'ic-strong':r.score>=35?'ic-weak':'';
    var star=starCls?'<span class="ic-dot '+starCls+'"></span>':'';
    var rowCls=r.score>=70?'row-blast':r.score>=60?'row-strong':'';
    h+='<tr class="'+rowCls+'"><td>'+(i+1)+'</td><td class="'+cls+'">'+dir+'</td><td><span class="copy-sym" onclick="event.stopPropagation();copySymbol(\''+r.symbol+'\',this)" title="复制"></span> '+star+'<b>'+r.symbol.replace('USDT','')+'</b></td>';
    h+='<td class="'+cls+'" style="font-weight:800">'+r.score.toFixed(1)+'</td>';
    h+='<td class="'+(r.min_spread<2?cls:r.min_spread<3?'y':'')+'">'+r.min_spread.toFixed(2)+'%</td>';
    var rt=r.retest||'--'; var rtCls=rt.indexOf('回踩')>=0||rt.indexOf('反弹')>=0?'g':'y';
    rt=rt.replace('回踩','支撑').replace('反弹','阻力').replace('确认','验证').replace('分型','拐点').replace('横盘','盘整');
    h+='<td style="font-size:10px;font-weight:700" class="'+rtCls+'">'+rt+'</td>';
    var htf=r.ht_fractal||'--', htfCls=htf.indexOf('共振')>=0?'g':htf.indexOf('压制')>=0?'r':'';
    htf=htf.replace('底分型','底部拐点').replace('顶分型','顶部拐点').replace('分型','拐点');
    h+='<td style="font-size:10px" class="'+htfCls+'">'+htf+'</td>';
    h+='<td class="'+cls+'">'+(r.breakout_pct>=0?'+':'')+r.breakout_pct.toFixed(2)+'%</td>';
    h+='<td>'+(r.bars_since===0?'<b class="'+cls+'">当前</b>':r.bars_since+'根')+'</td>';
    h+='<td>'+(r.squeeze_bars||'?')+'根</td>';
    h+='<td class="'+(r.vol_surge>=1.5?'g':'')+'">'+(r.vol_surge||0).toFixed(1)+'x</td>';
    var ht=r.ht_trend||'--';
    h+='<td class="'+(r.direction==='LONG'&&ht.indexOf('多')>=0||r.direction==='SHORT'&&ht.indexOf('空')>=0?'g':'r')+'">'+ht+'</td>';
    h+='<td>'+r.price+'</td></tr>';
  }
  if(top.length===0) h+='<tr><td colspan="12" style="text-align:center;color:var(--muted);padding:20px">暂无动量突破信号</td></tr>';
  h+='</tbody></table>';
  document.getElementById('main').innerHTML=h;
}

var _reflowFilters={quality:'ALL',direction:'ALL',type:'ALL',status:'ALL'};
var _reflowPayload={rows:[]};

var REFLOW_ALERT_SOUND_KEY='axiom_reflow_alert_sound_v1';
var REFLOW_ALERT_CURSOR_KEY='axiom_reflow_alert_cursor_v1';
var _reflowAlertTimer=null,_reflowAudioContext=null,_reflowSoundNeedsGesture=false;
var _reflowAlertPollPromise=null,_reflowAlertBaselinePromise=null,_reflowAlertGeneration=0;

function updateReflowSoundControls(){
  var enabled=localStorage.getItem(REFLOW_ALERT_SOUND_KEY)==='1';
  var state=document.getElementById('reflowSoundState');
  var toggle=document.getElementById('reflowSoundToggle');
  if(state) state.textContent=_reflowSoundNeedsGesture?'需要点击恢复声音':(enabled?'已开启':'已关闭');
  if(toggle){
    toggle.textContent=enabled?'关闭声音提醒':'开启声音提醒';
    toggle.onclick=function(){return setReflowSoundEnabled(!enabled);};
  }
}
function activateReflowAudio(){
  var AudioCtor=window.AudioContext||window.webkitAudioContext;
  if(!AudioCtor) return Promise.resolve(false);
  _reflowAudioContext=_reflowAudioContext||new AudioCtor();
  return Promise.resolve(_reflowAudioContext.resume()).then(function(){
    _reflowSoundNeedsGesture=false;updateReflowSoundControls();return true;
  }).catch(function(){_reflowSoundNeedsGesture=true;updateReflowSoundControls();return false;});
}
function playReflowCoinSound(playGuard){
  var AudioCtor=window.AudioContext||window.webkitAudioContext;
  if(!AudioCtor) return Promise.resolve(false);
  _reflowAudioContext=_reflowAudioContext||new AudioCtor();
  return Promise.resolve(_reflowAudioContext.resume()).then(function(){
    if(playGuard&&!playGuard()) return false;
    var now=_reflowAudioContext.currentTime;
    [880,1175,1568].forEach(function(frequency,index){
      var start=now+index*0.18,osc=_reflowAudioContext.createOscillator(),gain=_reflowAudioContext.createGain();
      osc.type='triangle';osc.frequency.setValueAtTime(frequency,start);
      gain.gain.setValueAtTime(0.0001,start);gain.gain.exponentialRampToValueAtTime(0.22,start+0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001,start+0.32);
      osc.connect(gain);gain.connect(_reflowAudioContext.destination);osc.start(start);osc.stop(start+0.34);
    });
    _reflowSoundNeedsGesture=false;updateReflowSoundControls();return true;
  }).catch(function(){_reflowSoundNeedsGesture=true;updateReflowSoundControls();return false;});
}
function fetchReflowAlertBaseline(){
  return fetch('/api/reflow/alerts?after=0').then(function(response){
    if(!response.ok) throw new Error('alert baseline failed');
    return response.json();
  });
}
function storeReflowAlertCursor(latestAlertId){
  var current=parseInt(localStorage.getItem(REFLOW_ALERT_CURSOR_KEY),10);
  var latest=parseInt(latestAlertId||0,10);
  if(!isFinite(latest)||latest<0) latest=0;
  if(isFinite(current)) latest=Math.max(current,latest);
  localStorage.setItem(REFLOW_ALERT_CURSOR_KEY,String(latest));
}
function setReflowSoundEnabled(enabled){
  var generation=++_reflowAlertGeneration;
  localStorage.setItem(REFLOW_ALERT_SOUND_KEY,'0');
  if(!enabled){updateReflowSoundControls();return Promise.resolve();}
  return activateReflowAudio().then(function(active){
    if(!active||generation!==_reflowAlertGeneration) return null;
    return fetchReflowAlertBaseline();
  }).then(function(data){
    if(!data||generation!==_reflowAlertGeneration) return;
    storeReflowAlertCursor(data.latest_alert_id);
    _reflowAlertBaselinePromise=Promise.resolve(true);
    localStorage.setItem(REFLOW_ALERT_SOUND_KEY,'1');
    updateReflowSoundControls();
  }).catch(function(){
    if(generation===_reflowAlertGeneration){localStorage.setItem(REFLOW_ALERT_SOUND_KEY,'0');updateReflowSoundControls();}
  });
}
function testReflowCoinSound(){return playReflowCoinSound();}
function pollReflowAlerts(){
  if(localStorage.getItem(REFLOW_ALERT_SOUND_KEY)!=='1') return Promise.resolve();
  if(_reflowAlertPollPromise) return _reflowAlertPollPromise;
  var generation=_reflowAlertGeneration;
  var ready=_reflowAlertBaselinePromise||Promise.resolve(localStorage.getItem(REFLOW_ALERT_CURSOR_KEY)!==null);
  var work=Promise.resolve(ready).then(function(baselineReady){
    if(!baselineReady||generation!==_reflowAlertGeneration||localStorage.getItem(REFLOW_ALERT_SOUND_KEY)!=='1') return null;
    var cursor=parseInt(localStorage.getItem(REFLOW_ALERT_CURSOR_KEY),10);
    if(!isFinite(cursor)) return null;
    return fetch('/api/reflow/alerts?after='+cursor).then(function(response){
      if(!response.ok) throw new Error('alert poll failed');return response.json();
    }).then(function(data){return {cursor:cursor,data:data};});
  }).then(function(result){
    if(!result||generation!==_reflowAlertGeneration||localStorage.getItem(REFLOW_ALERT_SOUND_KEY)!=='1') return;
    var data=result.data;
    if(!Array.isArray(data.events)||!data.events.length) return;
    var canPlay=function(){return generation===_reflowAlertGeneration&&localStorage.getItem(REFLOW_ALERT_SOUND_KEY)==='1';};
    return playReflowCoinSound(canPlay).then(function(played){
      if(played&&generation===_reflowAlertGeneration&&localStorage.getItem(REFLOW_ALERT_SOUND_KEY)==='1'){
        storeReflowAlertCursor(data.latest_alert_id||result.cursor);
      }
    });
  }).catch(function(){});
  _reflowAlertPollPromise=work.then(function(value){_reflowAlertPollPromise=null;return value;});
  return _reflowAlertPollPromise;
}
function initReflowAlertSound(){
  if(localStorage.getItem(REFLOW_ALERT_CURSOR_KEY)===null){
    _reflowAlertBaselinePromise=fetchReflowAlertBaseline().then(function(data){
      storeReflowAlertCursor(data.latest_alert_id);return true;
    }).catch(function(){return false;});
  }else if(!_reflowAlertBaselinePromise) _reflowAlertBaselinePromise=Promise.resolve(true);
  if(!_reflowAlertTimer) _reflowAlertTimer=setInterval(pollReflowAlerts,5000);
  updateReflowSoundControls();
}
function reflowSoundControls(){
  var enabled=localStorage.getItem(REFLOW_ALERT_SOUND_KEY)==='1';
  return '<div class="reflow-sound-controls">'
    +'<button id="reflowSoundToggle" type="button">'
    +(enabled?'关闭声音提醒':'开启声音提醒')+'</button>'
    +'<button type="button" onclick="testReflowCoinSound()">测试声音</button>'
    +'<span id="reflowSoundState"></span></div>';
}

function reflowFreshness(returnCloseTime){
  var time=finiteRNumber(returnCloseTime);
  if(time===null||time<=0) return '--';
  var minutes=Math.max(0,Math.floor((Date.now()-time)/60000));
  if(minutes<1) return '\u521a\u521a';
  if(minutes<60) return minutes+'\u5206\u949f\u524d';
  return Math.floor(minutes/60)+'\u5c0f\u65f6\u524d';
}

function setReflowFilter(name,value){
  if(!Object.prototype.hasOwnProperty.call(_reflowFilters,name)) return;
  _reflowFilters[name]=String(value||'ALL');
  renderMomentumReflow(_reflowPayload);
}

function renderMomentumReflow(payload){
  payload=payload&&typeof payload==='object'?payload:{};
  _reflowPayload=payload;
  var sourceRows=Array.isArray(payload.rows)?payload.rows:[];
  var rows=sourceRows.slice();
  var automation=payload.automation&&typeof payload.automation==='object'?payload.automation:{};
  var dailyLabel={strong_momentum:'\u5f3a\u52a8\u80fd\u65e5K',bullish_engulfing:'\u770b\u6da8\u541e\u6ca1',bearish_engulfing:'\u770b\u8dcc\u541e\u6ca1',hammer:'\u9524\u5b50\u7ebf',shooting_star:'\u6d41\u661f\u7ebf',morning_star:'\u65e9\u6668\u4e4b\u661f',evening_star:'\u9ec4\u660f\u4e4b\u661f',bottom_fractal:'\u5e95\u5206\u578b',top_fractal:'\u9876\u5206\u578b'};
  var qualityLabel={HIGH:'\u9ad8\u8d28\u91cf',STANDARD:'\u6807\u51c6',WATCH:'\u89c2\u5bdf'};
  var typeLabel={CRYPTO:'\u52a0\u5bc6',COMMODITY:'\u5546\u54c1',FX:'\u5916\u6c47'};
  var statusLabel={ACTIVE:'\u56de\u6d41\u6709\u6548',WINDOW_COMPLETE:'\u7a97\u53e3\u7ed3\u675f',INVALID:'\u4e8b\u4ef6\u5931\u6548'};
  function count(value){var n=finiteRNumber(value);return n===null?'--':String(Math.max(0,Math.trunc(n)));}
  function atr(value){var n=finiteRNumber(value);return n===null?'--':Math.abs(n).toFixed(2)+' ATR';}
  function price(value){var n=finiteRNumber(value);return n===null?'--':n.toFixed(6).replace(/\.?(0+)$/,'');}
  function volume(value){var n=finiteRNumber(value);return n===null?'--':n.toFixed(1)+'x';}
  function bjTime(value){var n=finiteRNumber(value);if(n===null||n<=0) return '--';var date=new Date(n);return isNaN(date.getTime())?'--':date.toLocaleString('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}
  function qualityKey(row){var value=String(row.quality_label||row.quality||'WATCH');return qualityLabel[value]?value:'WATCH';}
  function typeKey(row){var value=String(row.instrument_type||'CRYPTO');return typeLabel[value]?value:'CRYPTO';}
  function statusKey(row){var value=String(row.status||'ACTIVE');return statusLabel[value]?value:'ACTIVE';}
  function card(label,value,tone){return '<div class="reflow-status-card"><span>'+label+'</span><b'+(tone?' class="'+tone+'"':'')+'>'+escapeRHtml(value)+'</b></div>';}
  function filters(){
    var defs=[['quality','\u8d28\u91cf',[['ALL','\u5168\u90e8'],['HIGH','\u9ad8\u8d28\u91cf'],['STANDARD','\u6807\u51c6'],['WATCH','\u89c2\u5bdf']]],['direction','\u65b9\u5411',[['ALL','\u5168\u90e8'],['LONG','LONG'],['SHORT','SHORT']]],['type','\u7c7b\u578b',[['ALL','\u5168\u90e8'],['CRYPTO','\u52a0\u5bc6'],['COMMODITY','\u5546\u54c1'],['FX','\u5916\u6c47']]],['status','\u72b6\u6001',[['ALL','\u5168\u90e8'],['ACTIVE','\u56de\u6d41\u6709\u6548'],['WINDOW_COMPLETE','\u7a97\u53e3\u7ed3\u675f'],['INVALID','\u4e8b\u4ef6\u5931\u6548']]]],h='<div class="reflow-filter-bar">';
    for(var i=0;i<defs.length;i++){var group=defs[i];h+='<div class="reflow-filter-group"><span class="reflow-filter-label">'+group[1]+'</span>';for(var j=0;j<group[2].length;j++){var item=group[2][j];h+='<button type="button" class="reflow-filter-btn '+(_reflowFilters[group[0]]===item[0]?'active':'')+'" onclick="setReflowFilter(\''+group[0]+'\',\''+item[0]+'\')">'+item[1]+'</button>';}h+='</div>';}
    return h+reflowSoundControls()+'</div>';
  }
  rows.sort(function(a,b){a=a&&typeof a==='object'?a:{};b=b&&typeof b==='object'?b:{};return finiteRNumber(b.return_open_time)-finiteRNumber(a.return_open_time)||finiteRNumber(b.quality_score)-finiteRNumber(a.quality_score)||finiteRNumber(b.breakout_volume_ratio)-finiteRNumber(a.breakout_volume_ratio)||String(a.symbol).localeCompare(String(b.symbol));});
  rows=rows.filter(function(row){row=row&&typeof row==='object'?row:{};return (_reflowFilters.quality==='ALL'||qualityKey(row)===_reflowFilters.quality)&&(_reflowFilters.direction==='ALL'||row.direction===_reflowFilters.direction)&&(_reflowFilters.type==='ALL'||typeKey(row)===_reflowFilters.type)&&(_reflowFilters.status==='ALL'||statusKey(row)===_reflowFilters.status);});
  var hasProgress=automation.progress!==undefined&&automation.progress!==null&&automation.progress!=='';
  var progress=automation.scanning?'\u626b\u63cf\u4e2d'+(hasProgress?' '+String(automation.progress):''):(hasProgress?String(automation.progress):'--');
  document.getElementById('stats').innerHTML='<div class="reflow-status-grid">'+card('\u81ea\u52a8\u626b\u63cf',automation.auto_scan_enabled?'\u5df2\u5f00\u542f':'\u5df2\u5173\u95ed',automation.auto_scan_enabled?'g':'r')+card('\u4e0a\u6b21\u626b\u63cf',bjTime(automation.last_auto_scan_at))+card('\u4e0b\u6b21\u626b\u63cf',bjTime(automation.next_scan_at),'c')+card('\u5f53\u524d\u626b\u63cf',progress,automation.scanning?'c':'')+card('\u5df2\u626b\u63cf',count(payload.scanned))+card('\u9519\u8bef',count(payload.errors),'r')+card('\u4eca\u65e5\u603b\u6570',count(payload.today_total),'c')+card('\u9ad8\u8d28\u91cf',count(payload.high_quality_count),'g')+'</div>';
  if(!rows.length){document.getElementById('main').innerHTML=filters()+'<div class="empty-state"><div class="ic-empty"></div><h3>\u6682\u65e0\u9996\u6b21\u56de\u6d41\u5019\u9009</h3><p>\u7b49\u5f85 EMA50 \u5f3a\u52bf\u7a81\u7834\u3001\u6269\u5f20\u4e0e\u65e5\u7ebf\u786e\u8ba4\u540e\u9996\u6b21\u56de\u8e29\u3002</p></div>';updateReflowSoundControls();return;}
  var h=filters()+'<div class="reflow-dashboard"><table><thead><tr><th>#</th><th>\u4ea4\u6613\u5bf9</th><th>\u65b9\u5411</th><th>\u8d28\u91cf</th><th>\u72b6\u6001</th><th>\u7c7b\u578b</th><th>\u4ef7\u683c</th><th>EMA50</th><th>\u65b0\u9c9c\u5ea6</th><th>\u7a97\u53e3</th><th>\u7a81\u7834\u65f6\u95f4</th><th>\u65e5\u7ebf\u786e\u8ba4</th><th>\u91cf\u6bd4</th></tr></thead><tbody>';
  for(var i=0;i<rows.length;i++){var row=rows[i]&&typeof rows[i]==='object'?rows[i]:{},symbol=String(row.symbol===null||row.symbol===undefined?'':row.symbol),direction=row.direction==='SHORT'?'SHORT':'LONG',tone=direction==='LONG'?'g':'r',q=qualityKey(row),type=typeKey(row),status=statusKey(row),windowIndex=finiteRNumber(row.window_index),window=windowIndex===null?'--/5':Math.max(0,Math.trunc(windowIndex))+'/5',daily=escapeRHtml(dailyLabel[row.daily_kind]||row.daily_kind||'--');h+='<tr><td>'+(i+1)+'</td><td><span class="copy-sym" data-symbol="'+escapeRHtml(symbol)+'" onclick="event.stopPropagation();copySymbol(this.getAttribute(\'data-symbol\'),this)" title="\u590d\u5236"></span> <b>'+(escapeRHtml(symbol.replace('USDT',''))||'--')+'</b></td><td class="'+tone+' reflow-'+direction.toLowerCase()+'">'+direction+'</td><td><span class="reflow-quality reflow-quality-'+q.toLowerCase()+'">'+qualityLabel[q]+'</span></td><td class="reflow-mobile-detail-cell"><span class="reflow-status reflow-status-'+status.toLowerCase().replace('_','-')+'">'+statusLabel[status]+'</span><span class="reflow-mobile-freshness">'+reflowFreshness(row.return_close_time)+'</span><details class="reflow-mobile-details"><summary>\u66f4\u591a\u8be6\u60c5</summary><div>\u7c7b\u578b '+typeLabel[type]+' \u00b7 \u4ef7\u683c '+price(row.price)+' \u00b7 EMA50 '+price(row.ema50)+' \u00b7 \u7a97\u53e3 '+window+' \u00b7 \u7a81\u7834 '+bjTime(row.breakout_time)+' \u00b7 \u65e5\u7ebf '+daily+' \u00b7 \u91cf\u6bd4 '+volume(row.breakout_volume_ratio)+' \u00b7 \u6536\u76d8\u8ddd\u79bb '+atr(row.close_distance_atr)+' \u00b7 \u6700\u5927\u6269\u5f20 '+atr(row.max_expansion_atr)+'</div></details></td><td>'+typeLabel[type]+'</td><td>'+price(row.price)+'</td><td>'+price(row.ema50)+'</td><td>'+reflowFreshness(row.return_close_time)+'</td><td>'+window+'</td><td>'+bjTime(row.breakout_time)+'</td><td>'+daily+'</td><td>'+volume(row.breakout_volume_ratio)+'</td></tr>';}
  document.getElementById('main').innerHTML=h+'</tbody></table></div>';
  updateReflowSoundControls();
}

// ===== SCANNING =====
function setScanning(s){
  document.getElementById('scanBtn').disabled=s;
  document.getElementById('scanBtn').textContent=s?'扫描中...':'▶ 开始扫描';
  document.getElementById('stopBtn').style.display=s?'inline-block':'none';
  document.getElementById('dot').className='status-dot'+(s?' scanning':'');
  document.getElementById('progressBar').style.width=s?'30%':'0';
}
function stopScan(){
  if(pollTimer){clearInterval(pollTimer); pollTimer=null;}
  _pollingScan=false;
  setScanning(false);
  document.getElementById('progressBar').style.width='0';
  document.getElementById('statusText').textContent='已停止';
}
function doScan(){
  if(cur==='trader'){selectTab('breakout_1h'); return;}
  _pollingScan=true;
  setScanning(true);
  var url=cur==='funding'?'/scan/funding':'/scan/'+cur.split('_')[0]+'/'+cur.split('_')[1];
  document.getElementById('progressBar').style.width='10%';
  fetch(url).then(function(r){return r.json();}).then(function(d){
    document.getElementById('statusText').textContent=d.status||'';
    if(!pollTimer) pollTimer=setInterval(pollResults, 1500);
  }).catch(function(e){setScanning(false);});
}
function pollResults(){
  fetch('/data').then(function(r){return r.json();}).then(function(d){
    var wasScanning=_pollingScan;
    _pollingScan=!!d.scanning;
    D=d.data;
    document.getElementById('statusTime').textContent=d.time;
    var txt=d.status; if(d.progress) txt+=' ['+d.progress+']';
    document.getElementById('statusText').textContent=txt;
    if(d.progress!==undefined && d.progress!==null && d.progress!==''){
      var parts=String(d.progress).split('/');
      if(parts.length===2) document.getElementById('progressBar').style.width=(parseInt(parts[0])/parseInt(parts[1])*90+10)+'%';
    }
    if(cur==='reflow_1h') show(cur, null);
    if(!d.scanning){
      if(wasScanning){
        setScanning(false);
        document.getElementById('progressBar').style.width='100%';
        setTimeout(function(){document.getElementById('progressBar').style.width='0'},500);
        if(cur!=='trader' && cur!=='reflow_1h') show(cur, null);
      }
      if(!_reflowPolling){clearInterval(pollTimer); pollTimer=null;}
    }
  }).catch(function(e){
    clearInterval(pollTimer); pollTimer=null;
    _pollingScan=false;
    setScanning(false);
  });
}

// ===== TRADER PANEL =====
var traderPoll=null;
var _traderInitDone=false;
var _traderLastSigCount=-1;
var engineCountdownReq=null;

function startEngineHeartbeat(durationSeconds){
  durationSeconds=durationSeconds||60;
  var timerBox=document.getElementById('axiomTimerBox');
  var mainSecEl=document.getElementById('acMainSec');
  var msSecEl=document.getElementById('acMsSec');
  var fillBar=document.getElementById('acFillBar');
  var statusLabel=document.getElementById('acStatusLabel');
  var statusDot=document.getElementById('acStatusDot');
  if(!timerBox||!mainSecEl||!msSecEl||!fillBar) return;
  if(engineCountdownReq) cancelAnimationFrame(engineCountdownReq);
  timerBox.style.display='inline-flex';
  timerBox.classList.remove('warning','is-scanning');
  statusLabel.textContent='距下轮扫描';
  statusDot.className='ah-dot';
  fillBar.style.animation='none';
  var durationMs=durationSeconds*1000;
  var endTime=performance.now()+durationMs;
  function update(t){
    var rem=Math.max(0,endTime-t);
    var s=rem/1000;
    mainSecEl.textContent=Math.floor(s).toString().padStart(2,'0');
    msSecEl.textContent=(s%1).toFixed(2).substring(1);
    fillBar.style.transform='scaleX('+(rem/durationMs)+')';
    if(rem<=5000&&rem>0&&!timerBox.classList.contains('warning')) timerBox.classList.add('warning');
    if(rem>0){engineCountdownReq=requestAnimationFrame(update);}
    else{triggerEngineScan();}
  }
  engineCountdownReq=requestAnimationFrame(update);
}

function triggerEngineScan(){
  var timerBox=document.getElementById('axiomTimerBox');
  if(!timerBox) return;
  timerBox.classList.remove('warning');
  timerBox.classList.add('is-scanning');
  document.getElementById('acStatusLabel').textContent='信号矩阵演算中';
  if(typeof refreshTraderData==='function') refreshTraderData();
  // 2秒后自动恢复倒计时
  setTimeout(function(){resetEngineHeartbeat();},3000);
}

function stopEngineHeartbeat(){
  if(engineCountdownReq){cancelAnimationFrame(engineCountdownReq); engineCountdownReq=null;}
  var timerBox=document.getElementById('axiomTimerBox'); if(timerBox) timerBox.style.display='none';
}
function resetEngineHeartbeat(){
  if(engineCountdownReq){cancelAnimationFrame(engineCountdownReq); engineCountdownReq=null;}
  startEngineHeartbeat();
}

function renderTrader(){
  setDesc('trader');
  if(!sessionStorage.getItem('trader_authed')){
    _traderInitDone=false;
    document.getElementById('stats').innerHTML='';
    document.getElementById('main').innerHTML=
      '<div style="max-width:400px;margin:60px auto;text-align:center">'+
      '<div class="ic-lock" style="margin-bottom:var(--u3)"></div>'+
      '<h3 style="color:var(--s-blue);margin-bottom:8px">交易面板已锁定</h3>'+
      '<p style="color:var(--muted);font-size:13px;margin-bottom:20px">输入面板密码解锁</p>'+
      '<input type="password" id="traderPwd" placeholder="密码" style="width:100%;padding:10px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;font-size:14px;text-align:center;margin-bottom:12px" onkeydown="if(event.key===\'Enter\')doAuth()">'+
      '<button class="btn" style="width:100%" onclick="doAuth()">解锁</button>'+
      '<p id="authErr" style="color:var(--s-red);font-size:12px;margin-top:8px;display:none">密码错误</p>'+
      '</div>';
    document.getElementById('scanLabel').textContent='交易面板已锁定';
    return;
  }
  if(!_traderInitDone){
    initTraderPanel();
    return;
  }
  refreshTraderData();
}

var _rPerformanceDetailsOpen=false;
function rememberRPerformanceDetailsState(details){
  _rPerformanceDetailsOpen=!!(details&&details.open);
}

function rPerformancePanelHtml(){
  return '<section class="r-panel" id="rPerformancePanel">'
    +'<div class="r-head"><div class="r-title">R PERFORMANCE</div><div class="r-meta" id="rPerformanceMeta">--</div></div>'
    +'<div class="r-metrics">'
    +'<div class="r-card"><div class="r-label">累计净 R</div><div class="r-value" id="rNetValue">--</div></div>'
    +'<div class="r-card"><div class="r-label">每笔期望</div><div class="r-value" id="rExpectancyValue">--</div></div>'
    +'<div class="r-card" title="平均盈利R ÷ 平均亏损R绝对值"><div class="r-label">平均盈亏比</div><div class="r-value p" id="rPayoffValue">--</div></div>'
    +'<div class="r-card" title="全部盈利R ÷ 全部亏损R绝对值"><div class="r-label">Profit Factor</div><div class="r-value p" id="rProfitFactorValue">--</div></div>'
    +'<div class="r-card"><div class="r-label">平均盈利 / 亏损</div><div class="r-value" id="rAverageValue">--</div></div>'
    +'<div class="r-card"><div class="r-label">最大回撤 R</div><div class="r-value r" id="rDrawdownValue">--</div></div>'
    +'</div>'
    +'<div class="r-chart-wrap" id="rPerformanceChart"><div class="eq-empty">等待有效 R 交易记录</div></div>'
    +'<details class="r-details" id="rPerformanceDetails"'
    +(_rPerformanceDetailsOpen?' open':'')
    +' ontoggle="rememberRPerformanceDetailsState(this)"><summary>更多复盘</summary><div class="r-detail-grid" id="rPerformanceDetailGrid"></div></details>'
    +'</section>';
}

function initTraderPanel(){
  document.getElementById('main').innerHTML='<div style="text-align:center;padding:60px;color:var(--muted)">加载配置中...</div>';
  document.getElementById('stats').innerHTML='';
  fetch('/trader/config').then(function(r){return r.json();}).then(function(cfg){
    if(cur!=='trader') return;
    var testnet=cfg.testnet||false;
    var h='<div class="trader-panel trader-panel-bg">';
    // Status bar — 上排信息+按钮
    h+='<div class="t-status-bar" style="display:flex;align-items:center;gap:10px;padding:6px 16px">';
    h+='<span class="status-dot" id="tDot" style="width:8px;height:8px;border-radius:50%;display:inline-block"></span>';
    h+='<b id="tRunLabel" style="font-size:14px">○ 已停止</b>';
    h+='<span id="tEnvLabel" style="font-size:11px;font-weight:700"></span>';
    h+='<span class="t-status-uptime" id="tUptime"></span>';
    h+='<span id="tStatusText" style="display:none"></span>';
    h+='<span style="flex:1"></span>';
    h+='<button class="btn" id="startBtn" onclick="startTrader()" style="background:var(--brand);color:#000;font-weight:700;padding:5px 12px;font-size:12px">▶ 启动</button>';
    h+='<button class="btn btn-stop" id="traderStopBtn" onclick="stopTrader()" style="display:none;padding:5px 12px;font-size:12px">■ 停止</button>';
    h+='<button class="btn btn-stop" id="btnCloseAll" style="display:none;padding:5px 12px;font-size:12px" onclick="closeAllPositions()"><span class="ic-close-btn"></span>平仓</button>';
    h+='</div>';
    // Equity review panel
    h+='<div class="equity-panel" id="equityPanel">';
    h+='<div class="eq-head"><div><div class="eq-title">账户净值曲线 <span class="eq-sub" id="eqSummary">--</span></div></div><div class="eq-range" id="eqRange"><button onclick="setEquityRange(7,this)">1W</button><button onclick="setEquityRange(30,this)">1M</button><button onclick="setEquityRange(90,this)">3M</button><button onclick="setEquityRange(180,this)">6M</button><button onclick="setEquityRange(365,this)">1Y</button><button onclick="setEquityRange(0,this)">ALL</button></div></div>';
    h+='<div class="eq-metrics"><div class="eq-card"><div class="eq-label">当前权益</div><div class="eq-value neu" id="eqBalance">--</div></div><div class="eq-card"><div class="eq-label">权益净盈</div><div class="eq-value" id="eqAccountPnl">--</div></div><div class="eq-card"><div class="eq-label">记录净盈</div><div class="eq-value" id="eqRecordPnl">--</div></div><div class="eq-card"><div class="eq-label">最大回撤</div><div class="eq-value" id="eqDrawdown">--</div></div></div>';
    h+='<div class="eq-chart-wrap" id="equityChart"><div class="eq-empty">等待交易记录生成净值曲线</div></div>';
    h+='<div class="eq-legend"><span><i class="eq-dot"></i>账户净值</span><span><i class="eq-dot base"></i>初始权益</span></div>';
    h+='</div>';
    h+=rPerformancePanelHtml();
    // Heartbeat bar — 下排全宽底栏 (纯展示, 无按钮)
    h+='<div class="axiom-heartbeat axiom-hb-bar" id="axiomTimerBox" style="display:none;margin-bottom:18px;border-radius:0 0 10px 10px;width:100%">';
    h+='<div class="ah-status"><span class="ah-dot" id="acStatusDot"></span><span class="ah-label" id="acStatusLabel">距下轮扫描</span></div>';
    h+='<div class="ah-timer" id="acTimeWrap" style="flex:1;justify-content:center"><span class="ah-main-sec" id="acMainSec">60</span><span class="ah-ms-sec" id="acMsSec">.00</span></div>';
    h+='<div class="ah-scan-text" id="acScanText">信号矩阵演算中...</div>';
    h+='<div class="ah-track"><div class="ah-fill" id="acFillBar"></div></div></div>';

    // Signal area
    h+='<div id="sigPanel" style="margin-bottom:14px"></div>';

    // 2-column layout: Config + Positions
    h+='<div class="t-layout">';

    // Left: Config (Linear-style collapsible sections)
    h+='<div>';

    // -- API Config section (vibrant orange accent) --
    h+='<div class="t-section api-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)">';
    h+='<span class="t-section-indicator"></span>';
    h+='<span>交易所配置</span>';
    h+='<span style="font-size:10px;color:var(--s-orange);margin-left:4px;font-weight:400">'+(testnet?'模拟盘':'实盘')+'</span>';
    h+='<span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="t-field"><label>交易所</label><select id="cfg_exchange" onchange="toggleExchange()"><option value="binance"'+(cfg.exchange!='bitget'?' selected':'')+'>币安 Binance</option><option value="bitget"'+(cfg.exchange=='bitget'?' selected':'')+'>Bitget</option></select></div>';
    h+='<div class="t-field"><label>市场</label><select id="cfg_market"><option value="spot"'+(cfg.market_type=="spot"?" selected":"")+'>现货</option><option value="futures"'+(cfg.market_type=="futures"?" selected":"")+'>合约</option></select></div>';
    h+='<div id="binanceFields" style="display:'+(cfg.exchange=='bitget'?'none':'block')+'">';
    h+='<div class="t-field"><label>交易模式</label><select id="cfg_testnet" onchange="toggleTestnet()"><option value="0"'+(testnet?"":" selected")+'>实盘交易</option><option value="1"'+(testnet?" selected":"")+'>模拟盘 (Testnet)</option></select></div>';
    h+='<div id="testnetFields" style="display:'+(testnet?'block':'none')+'">';
    h+='<div class="t-field"><label>API Key</label><input type="password" id="cfg_testkey" placeholder="模拟盘 Key"></div>';
    h+='<div class="t-field"><label>API Secret</label><input type="password" id="cfg_testsecret" placeholder="模拟盘 Secret"></div>';
    h+='</div>';
    h+='<div id="liveFields" style="display:'+(testnet?'none':'block')+'">';
    h+='<div class="t-field"><label>API Key</label><input type="password" id="cfg_apikey" placeholder="实盘 Key"></div>';
    h+='<div class="t-field"><label>API Secret</label><input type="password" id="cfg_secret" placeholder="实盘 Secret"></div>';
    h+='</div>';
    h+='</div>';
    h+='<div id="bitgetFields" style="display:'+(cfg.exchange=='bitget'?'block':'none')+'">';
    h+='<div class="t-field"><label>API Key</label><input type="password" id="cfg_bgkey" placeholder="Bitget API Key"></div>';
    h+='<div class="t-field"><label>API Secret</label><input type="password" id="cfg_bgsecret" placeholder="Bitget API Secret"></div>';
    h+='<div class="t-field"><label>API Passphrase</label><input type="password" id="cfg_bgpass" placeholder="Bitget Passphrase"></div>';
    h+='</div>';
    h+='</div></div>';

    // -- Strategy section --
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-blue);box-shadow:0 0 6px rgba(59,130,246,0.15)"></span><span>策略参数</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="t-field"><label>多周期并发<span class="tip">!<span class="tip-text">逗号分隔多周期同步扫描<br>例: 30m 或 30m,1h</span></span></label><input type="text" id="cfg_interval" value="'+(cfg.scan_interval||'30m')+'" placeholder="30m" style="font-family:monospace;font-size:11px"></div>';
    h+='<div class="t-field"><label>最低评分</label><input type="number" id="cfg_score" value="'+(cfg.min_score||70)+'" min="30" max="90" step="5"></div>';
    h+='<div class="t-field"><label>最大持仓</label><input type="number" id="cfg_maxpos" value="'+(cfg.max_positions||3)+'" min="1" max="10"></div>';
    h+='<div class="t-field" style="display:none"><label>信号源</label><select id="cfg_signal_source"><option value="predicta_ewo"'+((cfg.entry_signal_source||'rj_only')==='predicta_ewo'?' selected':'')+'>Predicta + EWO</option><option value="rj_only"'+((cfg.entry_signal_source||'rj_only')==='rj_only'?' selected':'')+'>RJ独立策略</option><option value="structure"'+((cfg.entry_signal_source||'rj_only')==='structure'?' selected':'')+'>结构突破 + RJ过滤</option></select></div>';
    h+='</div></div>';

    // -- RJ entry filter section --
    h+='<div class="t-section" style="display:none">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-green);box-shadow:0 0 6px rgba(34,197,94,0.15)"></span><span>RJ动能过滤</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="t-field"><label>过滤模式<span class="tip">!<span class="tip-text">RJ独立策略默认关闭二次过滤<br>结构突破旧策略可使用hard/soft过滤</span></span></label><select id="cfg_rj_filter"><option value="off"'+((cfg.rj_entry_filter||'off')==='off'?' selected':'')+'>关闭</option><option value="hard"'+((cfg.rj_entry_filter||'off')==='hard'?' selected':'')+'>硬过滤</option><option value="soft"'+((cfg.rj_entry_filter||'off')==='soft'?' selected':'')+'>软过滤</option><option value="log_only"'+((cfg.rj_entry_filter||'off')==='log_only'?' selected':'')+'>仅记录</option></select></div>';
    h+='<div class="t-field"><label>交叉有效K数<span class="tip">!<span class="tip-text">LONG要求最近N根内RJ金叉且当前J>R<br>SHORT要求最近N根内RJ死叉且当前J<R</span></span></label><input type="number" id="cfg_rj_cross_bars" value="'+(cfg.rj_cross_lookback_bars||8)+'" min="1" max="50" step="1"></div>';
    h+='<div class="t-field"><label>J/R最小差值</label><input type="number" id="cfg_rj_spread" value="'+(cfg.rj_min_jr_spread||0)+'" min="0" max="20" step="0.5"></div>';
    h+='<div class="t-field"><label>RJ K/D均线</label><select id="cfg_rj_kd_ma"><option value="sma"'+((cfg.rj_kd_ma_type||'sma')==='sma'?' selected':'')+'>SMA 原版KD V8</option><option value="rma"'+((cfg.rj_kd_ma_type||'sma')==='rma'?' selected':'')+'>RMA 旧后端</option></select></div>';
    h+='<div class="t-field"><label>紫线口径</label><select id="cfg_rj_slow_mode"><option value="k"'+((cfg.rj_slow_line_mode||'k')==='k'?' selected':'')+'>K线(贴近原版)</option><option value="rsi"'+((cfg.rj_slow_line_mode||'k')==='rsi'?' selected':'')+'>RSI</option><option value="stoch_rsi"'+((cfg.rj_slow_line_mode||'k')==='stoch_rsi'?' selected':'')+'>随机RSI</option><option value="j_rsi"'+((cfg.rj_slow_line_mode||'k')==='j_rsi'?' selected':'')+'>J线RSI</option></select></div>';
    h+='<div class="t-field"><label>紫线倍率</label><input type="number" id="cfg_rj_slow_scale" value="'+(cfg.rj_slow_line_scale||0.88)+'" min="0.1" max="2" step="0.01"></div>';
    h+='<div class="t-field"><label>紫线偏移</label><input type="number" id="cfg_rj_slow_offset" value="'+((cfg.rj_slow_line_offset===0||cfg.rj_slow_line_offset)?cfg.rj_slow_line_offset:0)+'" min="-50" max="50" step="0.1"></div>';
    h+='<div class="t-field"><label>紫线限制0-100</label><select id="cfg_rj_slow_clamp"><option value="1"'+(cfg.rj_slow_line_clamp!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_slow_line_clamp===false?' selected':'')+'>关闭</option></select></div>';
    h+='</div></div>';

    // -- RJ-only live strategy section --
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-green);box-shadow:0 0 6px rgba(45,212,191,0.15)"></span><span>RJ胜率筛选</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="t-field"><label>确认K数</label><input type="number" id="cfg_rjo_confirm" value="'+(cfg.rj_only_confirm_bars||6)+'" min="1" max="20" step="1"></div>';
    h+='<div class="t-field" style="display:none"><label>扫描数量</label><input type="number" id="cfg_rjo_max_symbols" value="'+(cfg.rj_only_max_symbols||80)+'" min="10" max="600" step="10"></div>';
    h+='<div class="t-field"><label>最小成交额</label><input type="number" id="cfg_rjo_min_vol" value="'+(cfg.rj_only_min_volume_usdt||3000000)+'" min="0" step="1000000"></div>';
    h+='<div class="t-field" style="display:none"><label>量能过滤</label><select id="cfg_rjo_vol_filter"><option value="1"'+(cfg.rj_only_volume_filter!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_volume_filter===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="t-field" style="display:none"><label>量能周期</label><input type="number" id="cfg_rjo_vol_len" value="'+(cfg.rj_only_volume_len||20)+'" min="5" max="120" step="1"></div>';
    h+='<div class="t-field"><label>量能倍数</label><input type="number" id="cfg_rjo_vol_mult" value="'+(cfg.rj_only_volume_mult||1.1)+'" min="0" max="5" step="0.05"></div>';
    h+='<div class="t-field" style="display:none"><label>历史胜率筛选</label><select id="cfg_rjo_stats_on"><option value="1"'+(cfg.rj_only_stats_enabled!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_stats_enabled===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="t-field" style="display:none"><label>回看K数</label><input type="number" id="cfg_rjo_lookback" value="'+(cfg.rj_only_stats_lookback_bars||1000)+'" min="80" max="1000" step="20"></div>';
    h+='<div class="t-field"><label>入场最小样本</label><input type="number" id="cfg_rjo_samples" value="'+(cfg.rj_only_stats_min_samples||8)+'" min="3" max="80" step="1"></div>';
    h+='<div class="t-field"><label>入场最低胜率%</label><input type="number" id="cfg_rjo_win" value="'+(cfg.rj_only_stats_min_win_rate||52)+'" min="30" max="90" step="1"></div>';
    h+='<div class="t-field" style="display:none"><label>最低平均R</label><input type="number" id="cfg_rjo_avg_r" value="'+(cfg.rj_only_stats_min_avg_r||0)+'" min="-1" max="2" step="0.05"></div>';
    h+='<div class="t-field" style="display:none"><label>统计持有K数</label><input type="number" id="cfg_rjo_horizon" value="'+(cfg.rj_only_stats_horizon_bars||12)+'" min="3" max="80" step="1"></div>';
    h+='<div class="t-field" style="display:none"><label>统计目标R</label><input type="number" id="cfg_rjo_target_r" value="'+(cfg.rj_only_stats_target_r||1)+'" min="0.3" max="5" step="0.1"></div>';
    h+='<div class="t-field" style="display:none"><label>优选池</label><select id="cfg_rjo_watch_on"><option value="1"'+(cfg.rj_only_watchlist_enabled!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_watchlist_enabled===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="t-field" style="display:none"><label>优选刷新(分)</label><input type="number" id="cfg_rjo_watch_refresh" value="'+(cfg.rj_only_watchlist_refresh_minutes||60)+'" min="5" max="720" step="5"></div>';
    h+='<div class="t-field" style="display:none"><label>优选评估数</label><input type="number" id="cfg_rjo_watch_eval" value="'+(cfg.rj_only_watchlist_eval_symbols||80)+'" min="20" max="600" step="10"></div>';
    h+='<div class="t-field" style="display:none"><label>优选池数量</label><input type="number" id="cfg_rjo_watch_size" value="'+(cfg.rj_only_watchlist_size||80)+'" min="5" max="300" step="5"></div>';
    h+='<div class="t-field"><label>优选最小样本</label><input type="number" id="cfg_rjo_watch_samples" value="'+(cfg.rj_only_watchlist_min_samples||8)+'" min="3" max="80" step="1"></div>';
    h+='<div class="t-field"><label>优选最低胜率%</label><input type="number" id="cfg_rjo_watch_win" value="'+(cfg.rj_only_watchlist_min_win_rate||52)+'" min="30" max="90" step="1"></div>';
    h+='<div class="t-field" style="display:none"><label>优选最低平均R</label><input type="number" id="cfg_rjo_watch_avg_r" value="'+(cfg.rj_only_watchlist_min_avg_r||0)+'" min="-1" max="2" step="0.05"></div>';
    h+='<div class="t-field" style="display:none"><label>新币发现数</label><input type="number" id="cfg_rjo_discovery" value="'+(cfg.rj_only_watchlist_discovery_top_n||30)+'" min="0" max="200" step="5"></div>';
    h+='<div class="t-field" style="display:none"><label>候选池</label><select id="cfg_rjo_pool_on"><option value="1"'+(cfg.rj_only_setup_pool_enabled!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_setup_pool_enabled===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="t-field" style="display:none"><label>确认方式</label><select id="cfg_rjo_setup_mode"><option value="near_close"'+((cfg.rj_only_setup_confirm_mode||'near_close')==='near_close'?' selected':'')+'>临近收盘</option><option value="hold"'+((cfg.rj_only_setup_confirm_mode||'near_close')==='hold'?' selected':'')+'>突破维持</option><option value="touch"'+((cfg.rj_only_setup_confirm_mode||'near_close')==='touch'?' selected':'')+'>触碰即进</option></select></div>';
    h+='<div class="t-field" style="display:none"><label>收盘前确认秒</label><input type="number" id="cfg_rjo_close_sec" value="'+(cfg.rj_only_setup_close_confirm_sec||120)+'" min="5" max="300" step="5"></div>';
    h+='<div class="t-field" style="display:none"><label>候选检查秒</label><input type="number" id="cfg_rjo_check_sec" value="'+(cfg.rj_only_setup_check_interval_sec||10)+'" min="3" max="60" step="1"></div>';
    h+='<div class="t-field" style="display:none"><label>候选池上限</label><input type="number" id="cfg_rjo_max_pool" value="'+(cfg.rj_only_setup_max_pool||40)+'" min="5" max="200" step="5"></div>';
    h+='<div class="t-field" style="display:none"><label>支撑压力过滤</label><select id="cfg_rjo_sr_filter"><option value="1"'+(cfg.rj_only_sr_filter!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_sr_filter===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="t-field" style="display:none"><label>要求J背离</label><select id="cfg_rjo_div_req"><option value="1"'+(cfg.rj_only_require_divergence!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_require_divergence===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="t-field" style="display:none"><label>止损ATR缓冲</label><input type="number" id="cfg_rjo_atr_sl" value="'+(cfg.rj_only_atr_sl_mult||0.5)+'" min="0" max="3" step="0.1"></div>';
    h+='<div class="t-field" style="display:none"><label>确认ATR缓冲</label><input type="number" id="cfg_rjo_confirm_buf" value="'+(cfg.rj_only_confirm_atr_buffer||0.08)+'" min="0" max="1" step="0.01"></div>';
    h+='<div class="t-field" style="display:none"><label>最小止损%</label><input type="number" id="cfg_rjo_min_stop" value="'+(cfg.rj_only_min_stop_pct||0.003)+'" min="0.001" max="0.03" step="0.001"></div>';
    h+='<div class="t-field" style="display:none"><label>最大止损%</label><input type="number" id="cfg_rjo_max_stop" value="'+(cfg.rj_only_max_stop_pct||0.08)+'" min="0.01" max="0.3" step="0.01"></div>';
    h+='</div></div>';

    // -- Risk section --
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-red);box-shadow:0 0 6px rgba(239,68,68,0.15)"></span><span>风控参数</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="t-field"><label>风险/笔 (USDT)<span class="tip">!<span class="tip-text">纯数字=统一风险<br>多周期: 30m:10,1h:15</span></span></label><input type="text" id="cfg_risk" value="'+(cfg.risk_per_trade||10)+'" placeholder="10 或 30m:10,1h:15" style="font-family:monospace;font-size:11px"></div>';
    h+='<div class="t-field"><label>最大单笔名义价值</label><input type="number" id="cfg_maxval" value="'+(cfg.max_position_usdt||1000)+'" min="100" max="50000" step="100"></div>';
    h+='<div class="t-field"><label>杠杆倍数 (仅合约)</label><input type="number" id="cfg_leverage" value="'+(cfg.leverage||3)+'" min="1" max="50" step="1"></div>';
    h+='<div class="t-field"><label>单日最大亏损</label><input type="number" id="cfg_maxdl" value="'+(cfg.max_daily_loss||30)+'" min="10" max="2000" step="10"></div>';
    h+='<div class="t-field"><label>初始账户权益<span class="tip">!<span class="tip-text">用于前端账本校准<br>填入最初本金后, 账户净盈=当前交易所权益-初始权益<br>留空或0则使用交易记录估算</span></span></label><input type="number" id="cfg_initial_equity" value="'+(cfg.account_initial_equity||0)+'" min="0" max="1000000" step="0.01"></div>';
    h+='<div class="t-field"><label>连续止损暂停</label><input type="number" id="cfg_maxcl" value="'+(cfg.max_consecutive_loss||3)+'" min="2" max="10"></div>';
    h+='<div class="t-field"><label>止损后冷却(分)</label><input type="number" id="cfg_cooldown" value="'+(cfg.cooldown_minutes||90)+'" min="10" max="480" step="10"></div>';
    h+='</div></div>';

    // -- Trail section --
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-purple);box-shadow:0 0 6px rgba(168,85,247,0.15)"></span><span>三阶止盈</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<p style="font-size:10px;color:var(--muted);margin-bottom:10px;line-height:1.5">1阶防守 → 2阶减仓 → 3阶追踪 (EMA棘轮 / ATR吊灯)</p>';
    h+='<div class="t-field"><label>0.5R半损保护<span class="tip">!<span class="tip-text">达到设置的R后，把最大亏损从1R降到0.5R<br>0=关闭，不锁浮盈</span></span></label><input type="number" id="cfg_half_risk_r" value="'+(cfg.half_risk_trigger_r??0)+'" min="0" max="0.79" step="0.1"></div>';
    h+='<div class="t-field"><label>提前保护<span class="tip">!<span class="tip-text">未到1.2R前先保本<br>防止0.8R附近回落成亏损</span></span></label><select id="cfg_early_protect"><option value="1"'+(cfg.enable_early_protect!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.enable_early_protect===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="t-field"><label>提前保护R</label><input type="number" id="cfg_early_r" value="'+(cfg.early_protect_r||0.8)+'" min="0.3" max="1.2" step="0.1"></div>';
    h+='<div class="t-field"><label>提前锁定R<span class="tip">!<span class="tip-text">0=止损推到入场价<br>0.05=锁0.05R小利润</span></span></label><input type="number" id="cfg_early_lock" value="'+(cfg.early_protect_lock_r||0)+'" min="0" max="0.5" step="0.05"></div>';
    h+='<div class="t-field"><label>一阶防守 (R倍数)<span class="tip">!<span class="tip-text">浮盈达此R值触发绝对防守<br>SL移至入场价+0.2R缓冲</span></span></label><input type="number" id="cfg_tier1" value="'+(cfg.tier1_defense_r||1.2)+'" min="0.5" max="3.0" step="0.1"></div>';
    h+='<div class="t-field"><label>二阶减仓 (R倍数)<span class="tip">!<span class="tip-text">浮盈达此R值减仓50%<br>剩余仓位SL锁1R利润</span></span></label><input type="number" id="cfg_tier2" value="'+(cfg.tier2_partial_r||2.0)+'" min="1.0" max="5.0" step="0.1"></div>';
    h+='<div class="t-field"><label>未起爆超时<span class="tip">!<span class="tip-text">超过指定K数仍未进入一阶/三阶保护<br>且最大顺势推进低于阈值则自动平仓</span></span></label><select id="cfg_time_stop"><option value="1"'+(cfg.enable_time_stop!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.enable_time_stop===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="t-field"><label>超时K数<span class="tip">!<span class="tip-text">纯数字=统一K数<br>多周期: 15m:6,1h:5,4h:4,1d:3</span></span></label><input type="text" id="cfg_time_bars" value="'+(cfg.time_stop_bars||'15m:6,1h:5,4h:4,1d:3')+'" placeholder="15m:6,1h:5,4h:4,1d:3" style="font-family:monospace;font-size:11px"></div>';
    h+='<div class="t-field"><label>最小推进R<span class="tip">!<span class="tip-text">入场后最大浮盈未达到该R值<br>超时才认定为未起爆失败</span></span></label><input type="number" id="cfg_time_minr" value="'+(cfg.time_stop_min_r||0.6)+'" min="0.1" max="1.5" step="0.1"></div>';
    h+='<div class="t-field"><label>三阶追踪模式<span class="tip">!<span class="tip-text">EMA棘轮=均线只上不下<br>ATR吊灯=极值-ATR×倍数 防插针</span></span></label><select id="cfg_atr_trail" onchange="toggleAtrMode()"><option value="0"'+(cfg.use_atr_trail?'':' selected')+'>EMA棘轮</option><option value="1"'+(cfg.use_atr_trail?' selected':'')+'>ATR吊灯</option></select></div>';
    h+='<div id="trail_ema_fields" style="display:'+(cfg.use_atr_trail?'none':'block')+'"><div class="t-field"><label>EMA周期<span class="tip">!<span class="tip-text">棘轮追踪均线周期<br>20=短线紧贴 60=中线 120=长线</span></span></label><input type="number" id="cfg_ratchet" value="'+(cfg.ema_ratchet||20)+'" min="10" max="200" step="10"></div></div>';
    h+='<div id="trail_atr_fields" style="display:'+(cfg.use_atr_trail?'block':'none')+'"><div class="t-field"><label>ATR倍数<span class="tip">!<span class="tip-text">吊灯偏移倍数<br>3.5=标准 2.5=紧贴 5.0=宽松</span></span></label><input type="number" id="cfg_atr_mult" value="'+(cfg.atr_trail_mult||3.5)+'" min="1.5" max="8.0" step="0.5"></div><div class="t-field"><label>ATR周期<span class="tip">!<span class="tip-text">ATR回溯周期<br>14=标准 7=灵敏 21=平滑</span></span></label><input type="number" id="cfg_atr_period" value="'+(cfg.atr_trail_period||14)+'" min="5" max="50" step="1"></div></div>';
    h+='</div></div>';

    // -- Web password + Save --
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--muted)"></span><span>其他</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="t-field"><label>Web面板密码</label><input type="password" id="cfg_webpwd" placeholder="留空=不设密码"></div>';
    h+='<div class="t-field"><label>Hermes AI确认</label><select id="cfg_hermes_on"><option value="0"'+(cfg.hermes_confirm_enabled?'':' selected')+'>关闭</option><option value="1"'+(cfg.hermes_confirm_enabled?' selected':'')+'>启用</option></select></div>';
    h+='<div class="t-field"><label>Hermes模式</label><select id="cfg_hermes_mode"><option value="log_only"'+((cfg.hermes_confirm_mode||'log_only')==='log_only'?' selected':'')+'>仅记录</option><option value="hard_filter"'+((cfg.hermes_confirm_mode||'log_only')==='hard_filter'?' selected':'')+'>拦截冲突</option></select></div>';
    h+='<div class="t-field"><label>Hermes最低置信度</label><input type="number" id="cfg_hermes_conf" value="'+(cfg.hermes_confirm_min_confidence||65)+'" min="0" max="100" step="1"></div>';
        h+='<div class="t-field"><label>Hermes超时秒</label><input type="number" id="cfg_hermes_timeout" value="'+(cfg.hermes_confirm_timeout_sec||240)+'" min="3" max="300" step="1"></div>';
        h+='<div class="t-field"><label>Hermes排队秒</label><input type="number" id="cfg_hermes_queue_wait" value="'+(cfg.hermes_confirm_queue_wait_sec||300)+'" min="0" max="300" step="5"></div>';
    h+='<div class="t-field"><label>Hermes异常放行</label><select id="cfg_hermes_fail_open"><option value="0"'+(cfg.hermes_confirm_fail_open?'':' selected')+'>否</option><option value="1"'+(cfg.hermes_confirm_fail_open?' selected':'')+'>是</option></select></div>';
    h+='</div></div>';

    h+='<button class="btn" style="width:100%;margin-top:10px;padding:12px;font-size:14px" onclick="saveConfig()">保存配置</button>';
    h+='</div>';

    // Right: Positions (compact)
    h+='<div style="background:linear-gradient(180deg,rgba(20,20,24,0.9) 0%,rgba(17,17,20,0.95) 100%);border:1px solid var(--border);border-radius:10px;padding:16px;box-shadow:var(--shadow-sm)" id="posPanel">';
    h+='<div class="pos-monitor-head"><span class="pos-monitor-dot"></span><h4 style="color:var(--brand);margin:0;font-size:13px;letter-spacing:0.04em">毫秒级持仓态势感知</h4><span class="pos-monitor-wave"></span></div>';
    h+='<div id="posContent"><div style="text-align:center;color:var(--muted);padding:20px">加载中...</div></div>';
    h+='</div>';
    h+='</div>';

    // Trade log — full width at bottom
    h+='<div style="background:linear-gradient(180deg,rgba(20,20,24,0.9) 0%,rgba(17,17,20,0.95) 100%);border:1px solid var(--border);border-radius:10px;padding:16px;box-shadow:var(--shadow-sm)" id="logPanel">';
    h+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><h4 style="color:var(--s-purple);margin:0;font-size:13px">交易记录</h4><div id="tradeStats" style="display:flex;gap:16px;font-size:11px;color:var(--muted)"></div></div>';
    h+='<div id="logContent"><div style="text-align:center;color:var(--muted);padding:20px">加载中...</div></div>';
    h+='<div id="tradePager" style="display:flex;justify-content:center;align-items:center;gap:6px;margin-top:10px;font-size:11px"></div>';
    h+='</div>';

    h+='</div>';
    document.getElementById('main').innerHTML=h;

    _traderInitDone=true;
    refreshTraderData();
  });
}

var _equityRangeDays=7;
var _lastTraderData=null;
function setEquityRange(days, btn){
  _equityRangeDays=days;
  var bs=document.querySelectorAll('#eqRange button');
  for(var i=0;i<bs.length;i++) bs[i].classList.remove('active');
  if(btn) btn.classList.add('active');
  if(_lastTraderData) renderEquityReview(_lastTraderData);
}
function getRRangeSummary(d){
  var ranges=((d||{}).r_performance||{}).ranges||{};
  var key=Number(_equityRangeDays||0)>0?String(_equityRangeDays):'all';
  return ranges[key]||null;
}
function getRRangeLabel(){
  var labels={7:'1W',30:'1M',90:'3M',180:'6M',365:'1Y',0:'ALL'};
  return labels[Number(_equityRangeDays||0)]||'ALL';
}
function escapeRHtml(value){
  return String(value===null||value===undefined?'':value)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}
function finiteRNumber(value){
  if(value===null||value===undefined||value==='') return null;
  var n=Number(value);
  return isFinite(n)?n:null;
}
function fmtRValue(value,signed){
  var n=finiteRNumber(value);
  if(n===null) return '--';
  var prefix=signed?(n>0?'+':n<0?'−':''):(n<0?'−':'');
  return prefix+Math.abs(n).toFixed(2)+'R';
}
function fmtRatio(value){
  var n=finiteRNumber(value);
  return n===null?'--':n.toFixed(2);
}
function fmtRCount(value){
  var n=finiteRNumber(value);
  return n===null?'--':String(Math.trunc(n));
}
function fmtRPercent(value){
  var n=finiteRNumber(value);
  return n===null?'--':n.toFixed(1)+'%';
}
function setRValue(id,text,value,tone){
  var el=document.getElementById(id);
  if(!el) return;
  el.textContent=text;
  var n=finiteRNumber(value);
  el.className='r-value '+(tone||(n===null?'neu':n>0?'g':n<0?'r':'neu'));
}
function resetRPerformance(message){
  var meta=document.getElementById('rPerformanceMeta');
  if(meta) meta.textContent=getRRangeLabel()+' · '+(message||'无统计数据');
  setRValue('rNetValue','--',null);
  setRValue('rExpectancyValue','--',null);
  setRValue('rPayoffValue','--',null,'p');
  setRValue('rProfitFactorValue','--',null,'p');
  setRValue('rAverageValue','--',null);
  setRValue('rDrawdownValue','--',null,'r');
  var chart=document.getElementById('rPerformanceChart');
  if(chart) chart.innerHTML='<div class="eq-empty">等待有效 R 交易记录</div>';
  var detail=document.getElementById('rPerformanceDetailGrid');
  if(detail) detail.innerHTML='<div class="r-detail-box">暂无复盘数据</div>';
}
function fmtMoney(v, signed){
  var n=Number(v||0);
  var p=signed?(n>=0?'+':'-'):(n<0?'-':'');
  return p+'$'+Math.abs(n).toFixed(2);
}
function tradePnlValue(t){
  if(!t || t.voided) return 0;
  var ex=t.exchange_pnl;
  if(ex!==undefined && ex!==null && ex!=='') return Number(ex)||0;
  return Number(t.pnl||0);
}
function hermesText(h){
  if(!h || !h.active) return 'Hermes 未启用';
  if(!h.analysis_status) {
    var legacyDir=(h.direction||'NEUTRAL').toUpperCase();
    var legacyCn=legacyDir==='LONG'?'看多':(legacyDir==='SHORT'?'看空':'中性');
    return 'Hermes '+legacyCn+' · 旧版记录';
  }
  var status=(h.analysis_status||'').toUpperCase();
  if(status && status!=='OK') return 'Hermes 异常 · '+status;
  var dir=(h.dominant_direction||h.direction||'').toUpperCase();
  var cn=dir==='LONG'?'看多':(dir==='SHORT'?'看空':'无方向');
  var quality=h.tradeable?'可交易':'回避';
  var mode=h.mode==='hard_filter'?'拦截':'记录';
  return 'Hermes '+cn+' · '+quality+' · '+mode;
}
function hermesClass(h, axiomDir){
  if(!h || !h.active) return 'neu';
  if(!h.analysis_status) {
    var legacyDir=(h.direction||'NEUTRAL').toUpperCase();
    return legacyDir==='NEUTRAL'?'neu':(legacyDir===axiomDir?'g':'r');
  }
  if((h.analysis_status||'').toUpperCase()!=='OK' || !h.tradeable) return 'r';
  var dir=(h.dominant_direction||h.direction||'').toUpperCase();
  if(!dir) return 'neu';
  return dir===axiomDir?'g':'r';
}
function hermesLine(h, axiomDir){
  if(!h || !h.active) return '<div class="pc-data"><span class="pc-label">Hermes</span><span class="pc-val">未启用</span></div>';
  var cls=hermesClass(h, axiomDir);
  var reason=(h.reason||'').replace(/[<>]/g,'').slice(0,80);
  var title=reason?(' title="'+reason+'"'):'';
  return '<div class="pc-data"><span class="pc-label">Hermes</span><span class="pc-val '+cls+'"'+title+'>'+hermesText(h)+'</span></div>';
}
function choppyReasonText(a){
  var labels={atr_contraction:'波动衰减',box_squeeze:'箱体收缩',middle_chop:'中轴泥潭',pass:'活跃通过',insufficient_data:'数据不足',not_recorded:'未记录'};
  var reasons=(a&&Array.isArray(a.reasons)&&a.reasons.length?a.reasons:[a&&a.reason]).filter(Boolean);
  return reasons.map(function(x){return labels[x]||'状态未知';}).join(' + ')||'状态未知';
}
function choppyAuditMeta(a){
  if(!a||!a.recorded) return '';
  var parts=[];
  if(a.atr_ratio!==null&&a.atr_ratio!==undefined) parts.push('ATR比率 '+Number(a.atr_ratio).toFixed(2));
  if(a.box_position!==null&&a.box_position!==undefined) parts.push('箱体位置 '+(Number(a.box_position)*100).toFixed(1)+'%');
  if(a.box_amplitude!==null&&a.box_amplitude!==undefined) parts.push('箱体振幅 '+(Number(a.box_amplitude)*100).toFixed(2)+'%');
  return parts.join(' · ');
}
function choppyAuditClass(a){
  if(!a||!a.recorded||!a.available) return 'neu';
  return a.is_choppy?'y':'g';
}
function choppyAuditText(a){
  if(!a||!a.recorded) return '入场未记录';
  if(!a.available) return '数据不足';
  return (a.is_choppy?'震荡 · ':'活跃 · ')+choppyReasonText(a);
}
function choppyLine(a){
  var meta=choppyAuditMeta(a);
  var title=meta?' title="'+meta+'"':'';
  return '<div class="pc-data"><span class="pc-label">震荡过滤</span><span class="pc-val '+choppyAuditClass(a)+'"'+title+'>'+choppyAuditText(a)+'</span></div>';
}
function choppyTradeTag(a){
  var meta=choppyAuditMeta(a);
  var title=meta?' title="'+meta+'"':'';
  return '<span class="'+choppyAuditClass(a)+'"'+title+'>'+choppyAuditText(a)+'</span>';
}
function dailyPatternText(a){
  if(!a||!a.recorded) return '未记录';
  var labels={strong_momentum:'强动能',morning_star:'早晨之星',evening_star:'黄昏之星',bullish_engulfing:'看涨吞没',bearish_engulfing:'看跌吞没',hammer:'锤子线',shooting_star:'射击之星',bottom_fractal:'底分型',top_fractal:'顶分型',mixed:'混合形态',none:'无明确形态'};
  var kind=String(a.kind||'none').replace(/[<>]/g,'').slice(0,40);
  var align=a.alignment==='aligned'?'同向':(a.alignment==='opposed'?'反向':(a.alignment==='mixed'?'混合':'中性'));
  var day='';
  if(a.candle_open_time!==null&&a.candle_open_time!==undefined){
    var dt=new Date(Number(a.candle_open_time));
    if(!isNaN(dt.getTime())) day=dt.toISOString().slice(0,10)+' UTC收盘';
  }
  return (labels[kind]||kind)+' · '+align+(day?' · '+day:'');
}
function dailyPatternClass(a){
  if(!a||!a.recorded||a.alignment==='none') return 'neu';
  return a.alignment==='aligned'?'g':(a.alignment==='opposed'?'r':'y');
}
function dailyPatternLine(a){
  return '<div class="pc-data"><span class="pc-label">昨日收盘</span><span class="pc-val '+dailyPatternClass(a)+'">'+dailyPatternText(a)+'</span></div>';
}
function dailyPatternTradeTag(a){
  return '<span class="'+dailyPatternClass(a)+'">昨日日K '+dailyPatternText(a)+'</span>';
}
function setEqValue(id, value, signed){
  var el=document.getElementById(id);
  if(!el) return;
  var n=Number(value||0);
  el.textContent=fmtMoney(n, !!signed);
  el.className='eq-value '+(n>0?'g':n<0?'r':'neu');
}
function acct(d){
  var a=d.accounting||{};
  return {
    basis: a.basis || d.pnl_basis || 'trade_log',
    base: Number(a.base_equity!==undefined?a.base_equity:(d.account_initial_equity||0)),
    balance: Number(a.account_balance!==undefined?a.account_balance:(d.account_balance||0)),
    accountNet: Number(a.account_net_pnl!==undefined?a.account_net_pnl:(d.account_pnl!==undefined?d.account_pnl:(d.net_pnl||0))),
    realized: Number(a.record_realized_pnl!==undefined?a.record_realized_pnl:(d.realized_pnl||d.total_pnl||0)),
    open: Number(a.open_pnl!==undefined?a.open_pnl:(d.open_pnl||0)),
    recordNet: Number(a.record_net_pnl!==undefined?a.record_net_pnl:(d.net_pnl||0)),
    diff: Number(a.reconcile_diff!==undefined?a.reconcile_diff:(d.pnl_reconcile_diff||0)),
    dailyAccount: a.daily_account_pnl===null||a.daily_account_pnl===undefined?null:Number(a.daily_account_pnl||0),
    dailyRecord: Number(a.daily_record_pnl!==undefined?a.daily_record_pnl:(d.daily_record_pnl||d.daily_pnl||0)),
    dailyDisplay: Number(a.daily_display_pnl!==undefined?a.daily_display_pnl:(d.daily_account_pnl!==null&&d.daily_account_pnl!==undefined?d.daily_account_pnl:(d.daily_record_pnl||d.daily_pnl||0))),
    dailyBasis: a.daily_display_basis || (d.daily_account_pnl!==null&&d.daily_account_pnl!==undefined?'equity':'trade_log')
  };
}
function fmtTargetType(t){
  var m={
    swing_resistance:'历史压力',
    swing_support:'历史支撑',
    squeeze_resistance:'密集压力',
    squeeze_support:'密集支撑',
    none:'--'
  };
  return m[t]||t||'--';
}
function parseTradeTime(t){
  if(!t) return new Date();
  var s=String(t).trim().replace(' ','T');
  // 后端历史时间是北京时间墙上时间；旧记录可能带 +00:00，浏览器会误换算到次日。
  // 净值曲线按本地交易日复盘，先按去时区后的墙上时间解析。
  var local=s.replace(/([+-]\d{2}:\d{2}|Z)$/,'');
  var d=new Date(local);
  if(isNaN(d.getTime())) d=new Date(s);
  return isNaN(d.getTime())?new Date():d;
}
function buildEquitySeries(d){
  var hist=(d.equity_history||[]).slice().filter(function(x){
    return x && Number(x.account_balance||0)>0 && x.time;
  }).sort(function(a,b){return parseTradeTime(a.time)-parseTradeTime(b.time);});
  if(hist.length){
    var hbase=Number(hist[0].account_initial_equity||d.account_initial_equity||0);
    if(!hbase) hbase=Number(hist[0].account_balance||0)-Number(hist[0].account_pnl||0);
    if(!isFinite(hbase)||hbase<0) hbase=0;
    var hpts=[{t:parseTradeTime(hist[0].time),v:hbase}];
    for(var hi=0;hi<hist.length;hi++){
      hpts.push({t:parseTradeTime(hist[hi].time),v:Number(hist[hi].account_balance||0)});
    }
    var liveBal=Number(d.account_balance||0);
    if(liveBal>0 && Math.abs(liveBal-hpts[hpts.length-1].v)>0.001){
      hpts.push({t:new Date(),v:liveBal});
    }
    return {base:hbase,points:hpts,source:'equity'};
  }
  var trades=(d.recent_trades||[]).slice().sort(function(a,b){
    return parseTradeTime(a.time)-parseTradeTime(b.time);
  });
  var base=Number(d.account_initial_equity||0);
  if(!base){
    var ap=Number(d.account_pnl!==undefined?d.account_pnl:(d.net_pnl||0));
    base=Number(d.account_balance||0)-ap;
    if(!isFinite(base)||base<0) base=0;
  }
  var points=[];
  var first=trades.length?parseTradeTime(trades[0].time):new Date();
  points.push({t:first,v:base});
  var equity=base;
  for(var i=0;i<trades.length;i++){
    equity+=tradePnlValue(trades[i]);
    points.push({t:parseTradeTime(trades[i].time),v:equity});
  }
  if(Number(d.open_pnl||0)!==0) points.push({t:new Date(),v:equity+Number(d.open_pnl||0)});
  if(d.pnl_basis==='equity' && Number(d.account_balance||0)>0){
    points.push({t:new Date(),v:Number(d.account_balance||0)});
  }
  return {base:base,points:points,source:'trade_log'};
}
function filterEquityPoints(points){
  if(!points.length) return [];
  var days=Number(_equityRangeDays||0);
  if(days<=0) return points;
  var cutoff=new Date(Date.now()-days*86400000);
  var out=[], anchor=null;
  for(var i=0;i<points.length;i++){
    if(points[i].t<cutoff) anchor=points[i];
    else out.push(points[i]);
  }
  if(anchor) out.unshift(anchor);
  if(out.length<2 && points.length>=2) out=points.slice(Math.max(0,points.length-2));
  return out;
}
function calcDrawdown(points){
  if(!points.length) return {abs:0,pct:0};
  var peak=points[0].v, maxAbs=0, maxPct=0;
  for(var i=0;i<points.length;i++){
    if(points[i].v>peak) peak=points[i].v;
    var dd=peak-points[i].v;
    var pct=peak>0?dd/peak*100:0;
    if(dd>maxAbs){maxAbs=dd;maxPct=pct;}
  }
  return {abs:maxAbs,pct:maxPct};
}
function renderRPerformance(d){
  var s=getRRangeSummary(d), meta=document.getElementById('rPerformanceMeta');
  if(!s){
    resetRPerformance('无统计数据');
    return;
  }
  var chart=document.getElementById('rPerformanceChart');
  if(!chart) return;
  if(meta){
    var excluded=finiteRNumber(s.excluded_records);
    meta.textContent=getRRangeLabel()+' · 完整交易 '+fmtRCount(s.valid_trade_count)
      +' · 胜率 '+fmtRPercent(s.win_rate)
      +' · 连亏峰值 '+fmtRCount(s.max_consecutive_losses)
      +' · 有效记录 '+fmtRCount(s.valid_exit_record_count)+' / '+fmtRCount(s.source_record_count)
      +(excluded!==null&&excluded>0?' · 未计入 '+fmtRCount(excluded)+' 条':'');
  }
  setRValue('rNetValue',fmtRValue(s.net_r,true),s.net_r);
  setRValue('rExpectancyValue',fmtRValue(s.expectancy_r,true),s.expectancy_r);
  setRValue('rPayoffValue',fmtRatio(s.average_payoff_ratio),s.average_payoff_ratio,'p');
  setRValue('rProfitFactorValue',fmtRatio(s.profit_factor),s.profit_factor,'p');
  setRValue('rAverageValue',fmtRValue(s.average_win_r,true)+' / '+fmtRValue(s.average_loss_r,true),null);
  var drawdown=finiteRNumber(s.max_drawdown_r);
  var drawdownValue=drawdown===null?null:-Math.abs(drawdown);
  setRValue('rDrawdownValue',fmtRValue(drawdownValue,true),drawdownValue,'r');

  var rawPoints=s.cumulative_r_points;
  var points=Array.isArray(rawPoints)?rawPoints.slice():[];
  var pointValues=[], invalidPoints=rawPoints!==null&&rawPoints!==undefined&&!Array.isArray(rawPoints);
  for(var pi=0;pi<points.length&&!invalidPoints;pi++){
    var pointValue=finiteRNumber(points[pi]&&points[pi].r);
    if(pointValue===null) invalidPoints=true;
    else pointValues.push(pointValue);
  }
  if(invalidPoints){
    chart.innerHTML='<div class="eq-empty">R 曲线数据无效</div>';
  }else if(!points.length){
    chart.innerHTML='<div class="eq-empty">等待有效 R 交易记录</div>';
  }else{
    var W=860,H=150,L=44,R=12,T=12,B=22, values=pointValues.slice();
    values.push(0);
    var min=Math.min.apply(null,values),max=Math.max.apply(null,values);
    if(max-min<1){max+=.5;min-=.5}
    var span=max-min,pad=span*.12;min-=pad;max+=pad;
    var coords=points.map(function(p,i){
      return [L+(points.length===1?0:i/(points.length-1))*(W-L-R),T+(max-pointValues[i])/(max-min)*(H-T-B)];
    });
    var zero=T+(max-0)/(max-min)*(H-T-B);
    var poly=coords.map(function(c){return c[0].toFixed(1)+','+c[1].toFixed(1)}).join(' ');
    var netR=finiteRNumber(s.net_r);
    chart.innerHTML='<svg class="r-chart" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'
      +'<line x1="'+L+'" y1="'+zero.toFixed(1)+'" x2="'+(W-R)+'" y2="'+zero.toFixed(1)+'" stroke="rgba(148,163,184,.45)" stroke-dasharray="4 5"/>'
      +'<polyline points="'+poly+'" fill="none" stroke="'+(netR!==null&&netR<0?'#f87171':'#34d399')+'" stroke-width="2"/>'
      +'</svg>';
  }

  var directions=s.direction_breakdown||{}, reasons=s.exit_reason_breakdown||{};
  var directionText=Object.keys(directions).map(function(k){return escapeRHtml(k)+' '+fmtRValue((directions[k]||{}).net_r,true)}).join('<br>')||'--';
  var reasonText=Object.keys(reasons).sort(function(a,b){
    var av=finiteRNumber((reasons[a]||{}).net_r), bv=finiteRNumber((reasons[b]||{}).net_r);
    return (bv===null?-1:Math.abs(bv))-(av===null?-1:Math.abs(av));
  }).map(function(k){
    var row=reasons[k]||{};
    return escapeRHtml(k)+' '+fmtRCount(row.trades)+' 笔 · '+fmtRValue(row.net_r,true);
  }).join('<br>')||'--';
  var capture=finiteRNumber(s.mfe_capture_efficiency);
  var detail=document.getElementById('rPerformanceDetailGrid');
  if(detail) detail.innerHTML='<div class="r-detail-box"><b>方向贡献</b><br>'+directionText+'</div>'
    +'<div class="r-detail-box"><b>平仓原因贡献</b><br>'+reasonText+'</div>'
    +'<div class="r-detail-box"><b>MFE 与极值</b><br>平均 MFE '+fmtRValue(s.average_mfe_r,false)
    +'<br>捕获效率 '+(capture===null?'--':(capture*100).toFixed(1)+'%')
    +'<br>最佳 '+fmtRValue(s.largest_win_r,true)+' · 最差 '+fmtRValue(s.largest_loss_r,true)+'</div>';
}
function renderEquityReview(d){
  _lastTraderData=d;
  var a=acct(d);
  var bs=document.querySelectorAll('#eqRange button');
  for(var bi=0;bi<bs.length;bi++){
    var txt=bs[bi].textContent;
    var active=(_equityRangeDays===7&&txt==='1W')||(_equityRangeDays===30&&txt==='1M')||(_equityRangeDays===90&&txt==='3M')||(_equityRangeDays===180&&txt==='6M')||(_equityRangeDays===365&&txt==='1Y')||(_equityRangeDays===0&&txt==='ALL');
    bs[bi].classList.toggle('active',active);
  }
  setEqValue('eqBalance', a.balance, false);
  setEqValue('eqAccountPnl', a.accountNet, true);
  setEqValue('eqRecordPnl', a.recordNet, true);
  var built=buildEquitySeries(d);
  var pts=filterEquityPoints(built.points);
  var dd=calcDrawdown(built.points);
  if(d.max_drawdown_pct!==undefined && d.max_drawdown_pct!==null){
    dd={abs:Number(d.max_drawdown_abs||0),pct:Number(d.max_drawdown_pct||0)};
  }
  var ddEl=document.getElementById('eqDrawdown');
  if(ddEl){
    ddEl.textContent=dd.abs>0 ? '-$'+Number(dd.abs||0).toFixed(2)+' / -'+dd.pct.toFixed(1)+'%' : '-'+dd.pct.toFixed(1)+'%';
    ddEl.title='历史最大回撤';
    ddEl.className='eq-value '+(dd.pct>0?'r':'neu');
  }
  var sum=document.getElementById('eqSummary');
  if(sum){
    var src=a.dailyBasis==='equity'?'权益':'记录';
    sum.textContent='交易 '+(d.total_trades||0)+' | 胜率 '+(d.win_rate||0).toFixed(1)+'% | 今日'+src+' '+fmtMoney(a.dailyDisplay,true)+' | 已实现 '+fmtMoney(a.realized,true)+' | 持仓 '+fmtMoney(a.open,true)+' | 账差 '+fmtMoney(a.diff,true);
  }
  renderRPerformance(d);
  var box=document.getElementById('equityChart');
  if(!box) return;
  if(!pts.length){box.innerHTML='<div class="eq-empty">等待交易记录生成净值曲线</div>'; return;}
  var W=860,H=240,L=54,R=12,T=18,B=30;
  var minV=Math.min.apply(null,pts.map(function(p){return p.v;})), maxV=Math.max.apply(null,pts.map(function(p){return p.v;}));
  minV=Math.min(minV,built.base); maxV=Math.max(maxV,built.base);
  if(maxV-minV<1){maxV+=1;minV-=1;}
  var pad=(maxV-minV)*0.12; minV-=pad; maxV+=pad;
  var minT=pts[0].t.getTime(), maxT=pts[pts.length-1].t.getTime();
  if(maxT===minT) maxT=minT+1;
  function x(p){return L+(p.t.getTime()-minT)/(maxT-minT)*(W-L-R);}
  function yv(v){return T+(maxV-v)/(maxV-minV)*(H-T-B);}
  var coords=pts.map(function(p){return [x(p),yv(p.v)];});
  var line=coords.map(function(c){return c[0].toFixed(1)+','+c[1].toFixed(1);}).join(' ');
  var baseY=yv(built.base);
  var area='M '+coords[0][0].toFixed(1)+' '+baseY.toFixed(1)+' L '+coords.map(function(c){return c[0].toFixed(1)+' '+c[1].toFixed(1);}).join(' L ')+' L '+coords[coords.length-1][0].toFixed(1)+' '+baseY.toFixed(1)+' Z';
  var svg='<svg class="eq-chart" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">';
  for(var i=0;i<5;i++){
    var gy=T+i*(H-T-B)/4, val=maxV-i*(maxV-minV)/4;
    svg+='<line x1="'+L+'" y1="'+gy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+gy.toFixed(1)+'" stroke="rgba(148,163,184,0.12)" stroke-dasharray="3 5"/>';
    svg+='<text x="8" y="'+(gy+4).toFixed(1)+'" fill="rgba(148,163,184,0.8)" font-size="11" font-family="monospace">$'+val.toFixed(0)+'</text>';
  }
  svg+='<line x1="'+L+'" y1="'+baseY.toFixed(1)+'" x2="'+(W-R)+'" y2="'+baseY.toFixed(1)+'" stroke="rgba(148,163,184,0.35)" stroke-dasharray="4 5"/>';
  var gradId='eqGrad';
  svg+='<defs><linearGradient id="'+gradId+'" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="rgba(34,197,94,0.34)"/><stop offset="100%" stop-color="rgba(34,197,94,0.02)"/></linearGradient></defs>';
  svg+='<path d="'+area+'" fill="url(#'+gradId+')"/>';
  svg+='<polyline points="'+line+'" fill="none" stroke="var(--s-green)" stroke-width="2.4" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>';
  var labels=4;
  for(var j=0;j<=labels;j++){
    var idx=Math.min(pts.length-1,Math.round(j*(pts.length-1)/labels));
    var p=pts[idx], lx=x(p), dt=p.t;
    svg+='<text x="'+lx.toFixed(1)+'" y="'+(H-8)+'" fill="rgba(148,163,184,0.75)" font-size="11" text-anchor="middle" font-family="monospace">'+(dt.getMonth()+1)+'/'+dt.getDate()+'</text>';
  }
  svg+='</svg>';
  box.innerHTML=svg;
}

function fmtRjPrice(v){
  var n=Number(v||0);
  if(!isFinite(n)||n===0) return '--';
  if(Math.abs(n)>=100) return n.toFixed(2);
  if(Math.abs(n)>=1) return n.toFixed(4);
  return n.toFixed(6);
}
function fmtRjTime(sec){
  sec=Number(sec||0);
  if(sec<=0) return '--';
  var h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60);
  if(h>0) return h+'h '+m+'m';
  return m+'m';
}
function rjNode(label,value,cls){
  return '<div class="rj-node '+(cls||'')+'"><div class="rj-node-k">'+label+'</div><div class="rj-node-v">'+value+'</div></div>';
}
function rjDirText(v){
  return String(v||'').toUpperCase()==='LONG'?'做多':'做空';
}
function rjModeText(v){
  v=String(v||'').toLowerCase();
  if(v==='log_only') return '仅记录';
  if(v==='hard_filter') return '硬拦截';
  if(v==='off') return '关闭';
  return v||'关闭';
}
function rjStageText(v,label){
  v=String(v||'wait').toLowerCase();
  if(v==='wait') return '等待';
  if(v==='near') return '临界';
  if(v==='crossed') return '已触发';
  if(v==='touch') return '确认中';
  if(v==='volume') return '等量能';
  label=String(label||'');
  if(label==='SIGNAL') return '扫描信号';
  return label||'等待';
}
function renderRjPipelinePanel(d){
  var pipe=d.rj_pipeline||{};
  var rows=d.rj_setup_pool||[];
  var counts=pipe.counts||{};
  var sigs=d.signals||[];
  var activeSignals=0;
  for(var i=0;i<sigs.length;i++){if(Number(sigs[i].score||0)>=60) activeSignals++;}
  var hermes=pipe.hermes_enabled?rjModeText(pipe.hermes_mode):'关闭';
  var titleCount=rows.length>0?rows.length:sigs.length;
  var meta='扫描 '+(pipe.scan_batch_size||'--')+' / 并发 '+(pipe.scan_workers||'--')+' / 事件 '+(pipe.event_window||0);
  if(pipe.last_event_time){meta+=' / 最近 '+String(pipe.last_event_time).slice(5,16).replace('T',' ');}
  var html='<div class="rj-pipeline-panel">';
  html+='<div class="rj-pipeline-head"><div class="rj-title"><span class="rj-title-dot"></span>RJ候选池 · 扫描开仓链路 <span style="color:var(--muted);font-family:var(--font-mono)">('+titleCount+')</span></div><div class="rj-meta">'+meta+'</div></div>';
  html+='<div class="rj-rail">';
  html+=rjNode('扫描',pipe.running?'运行':'停止',pipe.running?'active':'off');
  html+=rjNode('评分信号',activeSignals,'active');
  html+=rjNode('候选池',pipe.setup_pool_size||rows.length,(pipe.setup_pool_size||rows.length)>0?'hot':'');
  html+=rjNode('临界触发',pipe.near_trigger_count||0,(pipe.near_trigger_count||0)>0?'warn':'');
  html+=rjNode('Hermes',hermes,pipe.hermes_enabled?'active':'off');
  html+=rjNode('预检',counts.entry_precheck_pass||0,(counts.entry_precheck_pass||0)>0?'active':'');
  html+=rjNode('下单',counts.entry_filled||0,(counts.entry_filled||0)>0?'hot':'');
  html+='</div>';
  if(rows.length>0){
    html+='<div class="rj-table-wrap"><table class="rj-table"><thead><tr><th>#</th><th>币种</th><th>方向</th><th>阶段</th><th>评分</th><th>胜率/样本</th><th>当前价</th><th>触发价</th><th>距离</th><th>止损</th><th>量能</th><th>过期</th></tr></thead><tbody>';
    for(var r=0;r<rows.length;r++){
      var x=rows[r], dc=x.direction==='LONG'?'g':'r';
      var stage=String(x.stage||'wait');
      var dist=Number(x.distance_pct||0);
      var fill=Math.max(4,Math.min(100,100-dist*8));
      html+='<tr class="rj-row-'+stage+'"><td>'+(r+1)+'</td>';
      html+='<td><span class="copy-sym" onclick="event.stopPropagation();copySymbol(\''+x.symbol+'\',this)" title="复制"></span> <b>'+String(x.symbol||'').replace('USDT','')+'</b> <span style="color:var(--muted);font-size:10px">'+(x.source_interval||'')+'</span></td>';
      html+='<td class="'+dc+'" style="font-weight:800">'+rjDirText(x.direction)+'</td>';
      html+='<td><span class="rj-stage '+stage+'">'+rjStageText(stage,x.stage_label)+'</span></td>';
      html+='<td class="'+dc+'" style="font-weight:850">'+Number(x.score||0).toFixed(1)+'</td>';
      html+='<td>'+Number(x.hist_win_rate||0).toFixed(1)+'% / '+(x.hist_samples||0)+'</td>';
      html+='<td>'+fmtRjPrice(x.price)+'</td><td>'+fmtRjPrice(x.trigger_price)+'</td>';
      html+='<td><div class="rj-distance"><span>'+dist.toFixed(2)+'%</span><span class="rj-dist-track"><span class="rj-dist-fill" style="width:'+fill.toFixed(0)+'%"></span></span></div></td>';
      html+='<td>'+fmtRjPrice(x.stop_price)+'</td>';
      html+='<td>'+(x.volume_pass?'通过':'等待')+' '+Number(x.volume_ratio||0).toFixed(1)+'x</td>';
      html+='<td>'+fmtRjTime(x.expires_in_sec)+'</td></tr>';
    }
    html+='</tbody></table></div>';
  }else if(sigs.length>0){
    html+='<div class="rj-table-wrap"><table class="rj-table"><thead><tr><th>#</th><th>币种</th><th>方向</th><th>阶段</th><th>评分</th><th>确认</th><th>突破%</th><th>K线</th><th>量</th><th>价格</th></tr></thead><tbody>';
    for(var sidx=0;sidx<sigs.length;sidx++){
      var s=sigs[sidx], sdc=s.direction==='LONG'?'g':'r';
      html+='<tr><td>'+(sidx+1)+'</td><td><span class="copy-sym" onclick="event.stopPropagation();copySymbol(\''+s.symbol+'\',this)" title="复制"></span> <b>'+String(s.symbol||'').replace('USDT','')+'</b></td>';
      html+='<td class="'+sdc+'" style="font-weight:800">'+rjDirText(s.direction)+'</td><td><span class="rj-stage wait">扫描信号</span></td>';
      html+='<td class="'+sdc+'" style="font-weight:850">'+Number(s.score||0).toFixed(1)+'</td><td style="font-size:10px;color:var(--s-yellow);font-weight:750">'+(s.retest||'--')+'</td>';
      html+='<td class="'+sdc+'">'+(Number(s.breakout_pct||0)>=0?'+':'')+Number(s.breakout_pct||0).toFixed(2)+'%</td><td>'+(s.bars_since||0)+'根</td><td>'+Number(s.vol_surge||0).toFixed(1)+'x</td><td>'+s.price+'</td></tr>';
    }
    html+='</tbody></table></div>';
  }else{
    html+='<div class="rj-empty">扫描链路在线，当前没有进入候选池的标的。等待 RJ 关键K回0/100确认与突破触发。</div>';
  }
  html+='</div>';
  return html;
}

function refreshTraderData(){
  fetch('/trader/status?fast=1&_='+Date.now()).then(function(r){return r.json();}).then(function(d){
    if(cur!=='trader') return;
    renderEquityReview(d);
    var running=d.running;
    var dot=document.getElementById('tDot');
    if(dot){dot.className='status-dot'+(running?' scanning':'');}
    var lbl=document.getElementById('tRunLabel');
    if(lbl){lbl.style.color=running?'var(--brand)':'var(--muted)'; lbl.textContent=running?'● 运行中':'○ 已停止';}
    var env=document.getElementById('tEnvLabel');
    if(env){env.textContent=d.testnet?'模拟盘':'实盘'; env.style.color=d.testnet?'var(--s-orange)':'var(--s-red)';}
    var upt=document.getElementById('tUptime');
    if(upt){var us=d.uptime||0,uh=Math.floor(us/3600),um=Math.floor((us%3600)/60); upt.textContent=uh>0?uh+'h '+um+'m':um+'m';}
    var bel=document.getElementById('tBalance'); if(bel){var ab=d.account_balance||0; bel.textContent=ab>0?'$'+ab.toFixed(2):''; bel.style.display=ab>0?'inline':'none';}
    var st=document.getElementById('tStatusText');
    if(st){st.textContent=d.status||'';}
    var btn=document.getElementById('startBtn');
    if(btn){btn.style.display=running?'none':'inline-block';}
    var sbtn=document.getElementById('traderStopBtn');
    if(sbtn){sbtn.style.display=running?'inline-block':'none';}
    var cb=document.getElementById('btnCloseAll');
    if(cb){cb.style.display=(d.positions&&d.positions.length>0)?'inline-block':'none';}
    var tsb=document.getElementById('traderStopBtn');
    if(tsb){tsb.style.display=running?'inline-block':'none';}
    if(running){
      if(!engineCountdownReq) startEngineHeartbeat();
    }else{stopEngineHeartbeat();}
    // 检测新扫描: 信号数变了重置心跳
    var liveSigCount=(d.signals?d.signals.length:0)+(d.rj_setup_pool?d.rj_setup_pool.length:0);
    if(running && liveSigCount!==_traderLastSigCount){
      _traderLastSigCount=liveSigCount;
      resetEngineHeartbeat();
    }
    // Signals
    var sigHTML='';
    if(d.signals && d.signals.length>0){
      var qualCount=0;
      for(var i=0; i<d.signals.length; i++){if(d.signals[i].score>=60) qualCount++;}
      sigHTML='<div style="background:linear-gradient(180deg,rgba(20,20,24,0.9) 0%,rgba(17,17,20,0.95) 100%);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:14px">';
      sigHTML+='<h4 style="color:var(--s-cyan);margin-bottom:8px;font-size:13px">最新动量突破 ('+d.signals.length+'个) - 评分≥60: '+qualCount+'个</h4>';
      sigHTML+='<table><thead><tr><th>#</th><th>币种</th><th>方向</th><th>评分</th><th>收敛</th><th>确认</th><th>HT拐点</th><th>突破%</th><th>K线</th><th>量</th><th>大周期</th><th>价格</th></tr></thead><tbody>';
      for(var i=0; i<d.signals.length; i++){
        var s=d.signals[i];
        var starCls=s.score>=70?'ic-mid':s.score>=60?'ic-strong':'';
        var star=starCls?'<span class="ic-dot '+starCls+'"></span>':'';
        var dc=s.direction==='LONG'?'g':'r';
        var rowCls=s.score>=70?'row-blast':s.score>=60?'row-strong':'';
        sigHTML+='<tr class="'+rowCls+'"><td>'+(i+1)+'</td><td><span class="copy-sym" onclick="event.stopPropagation();copySymbol(\''+s.symbol+'\',this)" title="复制"></span> '+star+'<b>'+s.symbol.replace('USDT','')+'</b></td>';
        sigHTML+='<td class="'+dc+'">'+s.direction+'</td>';
        sigHTML+='<td class="'+dc+'" style="font-weight:800">'+s.score.toFixed(1)+'</td>';
        sigHTML+='<td>'+(s.min_spread||0).toFixed(2)+'%</td>';
        var rt=s.retest||'--'; var rtCls=rt.indexOf('回踩')>=0||rt.indexOf('反弹')>=0?'g':'y';
        rt=rt.replace('回踩','支撑').replace('反弹','阻力').replace('确认','验证').replace('分型','拐点').replace('横盘','盘整');
        sigHTML+='<td style="font-size:10px;font-weight:700" class="'+rtCls+'">'+rt+'</td>';
        var htf=s.ht_fractal||'--', htfCls=htf.indexOf('共振')>=0?'g':htf.indexOf('压制')>=0?'r':'';
        htf=htf.replace('底分型','底部拐点').replace('顶分型','顶部拐点').replace('分型','拐点');
        sigHTML+='<td class="'+htfCls+'" style="font-size:10px;font-weight:600">'+htf+'</td>';
        sigHTML+='<td class="'+dc+'">'+(s.breakout_pct>=0?'+':'')+(s.breakout_pct||0).toFixed(2)+'%</td>';
        sigHTML+='<td>'+(s.bars_since||0)+'根</td>';
        sigHTML+='<td>'+(s.vol_surge||0).toFixed(1)+'x</td>';
        sigHTML+='<td>'+(s.ht_trend||'--')+'</td>';
        sigHTML+='<td>'+s.price+'</td></tr>';
      }
      sigHTML+='</tbody></table></div>';
    }
    var sp=document.getElementById('sigPanel');
    sigHTML=renderRjPipelinePanel(d);
    if(sp){sp.innerHTML=sigHTML;}

    // Positions — 活体呼吸卡片
    var posHTML='';
    if(!d.positions||d.positions.length===0){
      posHTML='<div style="text-align:center;color:var(--muted);padding:40px;border:1px dashed rgba(255,255,255,0.05);border-radius:12px"><span class="pos-monitor-dot" style="display:inline-block;margin:0 auto 12px"></span><div style="font-size:12px;letter-spacing:0.04em">系统持续扫描中 · 等待信号触发</div></div>';
    }else{
      posHTML='<div class="pos-grid">';
      for(var i=0; i<d.positions.length; i++){
        var p=d.positions[i];
        var isLong=p.direction==='LONG';
        var isProfit=p.pnl>=0;
        var stateCls=isProfit?'profit':'loss';
        posHTML+='<div class="pos-card '+stateCls+'">';
        var et2=p.entered||''; if(et2) et2=et2.slice(0,16).replace('T',' ');
        var srcInt=(p.source_interval||''); if(!srcInt){var cfgInt=d.cfg_interval||'15m'; srcInt=cfgInt.split(',')[0];}
        posHTML+='<div class="pc-head"><span class="pc-sym"><span class="copy-sym" onclick="event.stopPropagation();copySymbol(\''+p.symbol+'\',this)" title="复制"></span>'+p.symbol.replace('USDT','')+'</span><span class="pc-dir '+(isLong?'long':'short')+'">'+(isLong?'做多 LONG':'做空 SHORT')+'</span><span style="font-size:10px;color:var(--brand);background:rgba(45,212,191,0.08);padding:2px 6px;border-radius:4px;margin-left:8px">'+srcInt+'</span></div>';
        if(et2) posHTML+='<div class="pc-data"><span class="pc-label">开仓时间</span><span class="pc-val">'+et2+'</span></div>';
        posHTML+='<div class="pc-data"><span class="pc-label">入场价</span><span class="pc-val">'+p.entry+'</span></div>';
        posHTML+='<div class="pc-data"><span class="pc-label">当前价</span><span class="pc-val">'+(p.current_price||p.entry).toFixed(6)+'</span></div>';
        posHTML+='<div class="pc-data"><span class="pc-label">止损价</span><span class="pc-val">'+p.sl+'</span></div>';
        var pTargetR=Number(p.target_r||0);
        if(pTargetR>0){
          posHTML+='<div class="pc-data"><span class="pc-label">目标区</span><span class="pc-val">'+fmtTargetType(p.target_zone_type)+' '+Number(p.target_zone_price||0).toFixed(6)+' / '+pTargetR.toFixed(2)+'R</span></div>';
        }
        posHTML+=choppyLine(p.choppy_filter);
        posHTML+=dailyPatternLine(p.daily_pattern);
        posHTML+=hermesLine(p.hermes_confirm,p.direction);
        posHTML+='<div class="pc-data"><span class="pc-label">持仓价值</span><span class="pc-val">$'+(p.value||0).toFixed(2)+'</span></div>';
        posHTML+='<div class="pc-pnl-box"><div class="pc-pnl-val">'+(isProfit?'+':'')+p.pnl.toFixed(2)+' USDT</div><button class="pc-btn" onclick="closeOne(\''+p.symbol+'\')">平仓</button></div>';
        if(p.breakeven){posHTML+='<div class="pc-trail-shimmer">✦ 追踪止盈已开启</div>';}
        posHTML+='</div>';
      }
      posHTML+='</div>';
    }
    var pc=document.getElementById('posContent');
    if(pc){pc.innerHTML=posHTML;}

    // Trade log — 分页 + 统计
    var trades=d.recent_trades||[];
    var ta=acct(d);
    var total=trades.length;
    var tp=ta.realized;
    var op=ta.open;
    var np=ta.recordNet;
    var ap=ta.accountNet;
    var pdiff=ta.diff;
    var wr=d.win_rate||0;
    var st=document.getElementById('tradeStats');
    if(st){var statHtml='<span>共<strong style=\"color:var(--brand)\">'+total+'</strong>笔</span><span>记录已实现 <strong style=\"color:'+(tp>=0?'var(--s-green)':'var(--s-red)')+'\">'+(tp>=0?'+$':'-$')+Math.abs(tp).toFixed(2)+'</strong></span><span>持仓浮动 <strong style=\"color:'+(op>=0?'var(--s-green)':'var(--s-red)')+'\">'+(op>=0?'+$':'-$')+Math.abs(op).toFixed(2)+'</strong></span><span>记录净盈 <strong style=\"color:'+(np>=0?'var(--s-green)':'var(--s-red)')+'\">'+(np>=0?'+$':'-$')+Math.abs(np).toFixed(2)+'</strong></span>'; if(ta.basis==='equity'){statHtml+='<span>权益净盈 <strong style=\"color:'+(ap>=0?'var(--s-green)':'var(--s-red)')+'\">'+(ap>=0?'+$':'-$')+Math.abs(ap).toFixed(2)+'</strong></span><span>账差 <strong style=\"color:'+(pdiff>=0?'var(--s-green)':'var(--s-red)')+'\">'+(pdiff>=0?'+$':'-$')+Math.abs(pdiff).toFixed(2)+'</strong></span>';} statHtml+='<span>胜率 <strong style=\"color:var(--brand)\">'+wr.toFixed(1)+'%</strong></span>'; st.innerHTML=statHtml;}

    var pageSize=10;
    var curPage=window._tradePage||1;
    var totalPages=Math.max(1,Math.ceil(total/pageSize));
    if(curPage>totalPages) curPage=totalPages;
    window._tradePage=curPage;
    var start=(curPage-1)*pageSize;
    var pageTrades=trades.slice().reverse().slice(start,start+pageSize);

    var logHTML='';
    if(!trades.length){
      logHTML='<div style=\"text-align:center;color:var(--muted);padding:30px;border:1px dashed rgba(255,255,255,0.04);border-radius:10px\">暂无交易记录</div>';
    }else{
      logHTML='<div class=\"tl-list\">';
      for(var i=0;i<pageTrades.length;i++){
        var t=pageTrades[i];
        var dt=t.time?t.time.slice(5,16).replace('T',' '):'';
        var pnlVal=tradePnlValue(t);
        var isWin=pnlVal>=0;
        var psrc=String(t.pnl_source||'estimate');
        var psrcOk=(t.exchange_pnl!==undefined && t.exchange_pnl!==null && t.exchange_pnl!=='') || psrc==='exchange_order'||psrc.indexOf('bitget_')===0;
        var psrcText=psrcOk?'交易所':'估算';
        var psrcCls=psrcOk?'tl-src ok':'tl-src';
        var targetR=Number(t.target_r||0);
        var targetText=targetR>0?('目标 '+targetR.toFixed(2)+'R'):'目标 --';
        logHTML+='<div class=\"tl-item '+(isWin?'tl-win':'tl-loss')+'\" onclick=\"this.classList.toggle(\'expanded\')\">';
        logHTML+='<div class=\"tl-time\">'+dt+'</div>';
        logHTML+='<div class=\"tl-body\"><div class=\"tl-head\"><span class=\"tl-sym\">'+t.symbol.replace('USDT','')+'</span><span class=\"tl-dir '+(t.direction==='LONG'?'g':'r')+'\">'+(t.direction==='LONG'?'多':'空')+'</span><span class=\"tl-reason\">'+t.reason+'</span></div>';
        logHTML+='<div class=\"tl-data\"><span>入场 '+(t.entry||0).toFixed(4)+'</span><span>出场 '+(t.exit||0).toFixed(4)+'</span><span>SL '+(t.sl||0).toFixed(4)+'</span><span>'+targetText+'</span><span class=\"'+psrcCls+'\">'+psrcText+'</span>'+choppyTradeTag(t.choppy_filter)+dailyPatternTradeTag(t.daily_pattern)+'<span class=\"'+hermesClass(t.hermes_confirm,t.direction)+'\">'+hermesText(t.hermes_confirm)+'</span></div></div>';
        logHTML+='<div class=\"tl-pnl\"><span class=\"tl-pnl-val '+(isWin?'g':'r')+'\">'+(isWin?'+':'')+pnlVal.toFixed(2)+'</span><span class=\"tl-pnl-pct '+(isWin?'g':'r')+'\">'+(t.pnl_pct>=0?'+':'')+(t.pnl_pct||0).toFixed(2)+'%</span></div>';
        logHTML+='</div>';
      }
      logHTML+='</div>';
    }
    var lc=document.getElementById('logContent');
    if(lc){lc.innerHTML=logHTML;}

    // 分页器
    var pagerHTML='';
    if(totalPages>1){
      pagerHTML='<button onclick=\"window._tradePage=1;refreshTraderData()\" '+(curPage===1?'disabled':'')+' style=\"background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px\">首页</button>';
      pagerHTML+='<button onclick=\"window._tradePage='+(curPage-1)+';refreshTraderData()\" '+(curPage===1?'disabled':'')+' style=\"background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px\">‹</button>';
      for(var pi=Math.max(1,curPage-2); pi<=Math.min(totalPages,curPage+2); pi++){
        pagerHTML+='<button onclick=\"window._tradePage='+pi+';refreshTraderData()\" style=\"background:'+(pi===curPage?'var(--brand)':'var(--card)')+';color:'+(pi===curPage?'#000':'var(--muted)')+';border:1px solid '+(pi===curPage?'var(--brand)':'var(--border)')+';border-radius:4px;padding:4px 8px;cursor:pointer;font-size:11px;min-width:24px\">'+pi+'</button>';
      }
      pagerHTML+='<button onclick=\"window._tradePage='+(curPage+1)+';refreshTraderData()\" '+(curPage===totalPages?'disabled':'')+' style=\"background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px\">›</button>';
      pagerHTML+='<button onclick=\"window._tradePage='+totalPages+';refreshTraderData()\" '+(curPage===totalPages?'disabled':'')+' style=\"background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px\">末页</button>';
    }
    var pg=document.getElementById('tradePager');
    if(pg){pg.innerHTML=pagerHTML;}
  });

  if(traderPoll) clearInterval(traderPoll);
  traderPoll=setInterval(function(){if(cur==='trader')refreshTraderData();}, 2000);
}

// ===== TRADER ACTIONS =====
function doAuth(){
  var pwd=document.getElementById('traderPwd').value;
  fetch('/trader/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pwd})})
  .then(function(r){return r.json();}).then(function(d){
    if(d.ok){sessionStorage.setItem('trader_authed','1'); document.getElementById('scanLabel').textContent='当前: 自动交易'; _traderInitDone=false; renderTrader();}
    else{document.getElementById('authErr').style.display='block';}
  });
}
function startTrader(){
  fetch('/trader/start').then(function(){refreshTraderData();});
}
function stopTrader(){
  fetch('/trader/stop').then(function(){refreshTraderData();});
}
function toggleSection(header){
  var section=header.parentElement;
  section.classList.toggle('open');
}
function toggleExchange(){
  var isBg=document.getElementById('cfg_exchange').value==='bitget';
  document.getElementById('binanceFields').style.display=isBg?'none':'block';
  document.getElementById('bitgetFields').style.display=isBg?'block':'none';
}
function toggleTestnet(){
  var on=document.getElementById('cfg_testnet').value==='1';
  document.getElementById('testnetFields').style.display=on?'block':'none';
  document.getElementById('liveFields').style.display=on?'none':'block';
}
function toggleAtrMode(){
  var on=document.getElementById('cfg_atr_trail').value==='1';
  document.getElementById('trail_ema_fields').style.display=on?'none':'block';
  document.getElementById('trail_atr_fields').style.display=on?'block':'none';
}
function closeOne(sym){if(!confirm('平掉 '+sym.replace('USDT','')+'？'))return;fetch('/trader/close/'+sym).then(function(r){return r.json()}).then(function(d){alert(d.msg);refreshTraderData();});}
function closeAllPositions(){
  if(!confirm('确定平掉所有持仓？此操作不可撤销！')) return;
  fetch('/trader/close_all').then(function(r){return r.json();}).then(function(d){alert(d.msg||'已提交平仓指令'); refreshTraderData();});
}
function saveConfig(){
  var testnet=document.getElementById('cfg_testnet').value==='1';
  var cfg={
    exchange: document.getElementById('cfg_exchange').value, market_type: document.getElementById('cfg_market').value, testnet: testnet,
    scan_interval: document.getElementById('cfg_interval').value,
    min_score: parseFloat(document.getElementById('cfg_score').value),
    max_positions: parseInt(document.getElementById('cfg_maxpos').value),
    entry_signal_source: document.getElementById('cfg_signal_source').value,
    rj_entry_filter: document.getElementById('cfg_rj_filter').value,
    rj_cross_lookback_bars: parseInt(document.getElementById('cfg_rj_cross_bars').value)||8,
    rj_min_jr_spread: parseFloat(document.getElementById('cfg_rj_spread').value)||0,
    rj_kd_ma_type: document.getElementById('cfg_rj_kd_ma').value,
    rj_slow_line_mode: document.getElementById('cfg_rj_slow_mode').value,
    rj_slow_line_scale: parseFloat(document.getElementById('cfg_rj_slow_scale').value)||0.88,
    rj_slow_line_offset: parseFloat(document.getElementById('cfg_rj_slow_offset').value)||0,
    rj_slow_line_clamp: document.getElementById('cfg_rj_slow_clamp').value==='1',
    rj_only_confirm_bars: parseInt(document.getElementById('cfg_rjo_confirm').value)||6,
    rj_only_max_symbols: parseInt(document.getElementById('cfg_rjo_max_symbols').value)||80,
    rj_only_min_volume_usdt: parseFloat(document.getElementById('cfg_rjo_min_vol').value)||3000000,
    rj_only_volume_filter: document.getElementById('cfg_rjo_vol_filter').value==='1',
    rj_only_volume_len: parseInt(document.getElementById('cfg_rjo_vol_len').value)||20,
    rj_only_volume_mult: parseFloat(document.getElementById('cfg_rjo_vol_mult').value)||1.1,
    rj_only_stats_enabled: document.getElementById('cfg_rjo_stats_on').value==='1',
    rj_only_stats_lookback_bars: parseInt(document.getElementById('cfg_rjo_lookback').value)||1000,
    rj_only_stats_min_samples: parseInt(document.getElementById('cfg_rjo_samples').value)||8,
    rj_only_stats_min_win_rate: parseFloat(document.getElementById('cfg_rjo_win').value)||52,
    rj_only_stats_min_avg_r: parseFloat(document.getElementById('cfg_rjo_avg_r').value)||0,
    rj_only_stats_horizon_bars: parseInt(document.getElementById('cfg_rjo_horizon').value)||12,
    rj_only_stats_target_r: parseFloat(document.getElementById('cfg_rjo_target_r').value)||1.0,
    rj_only_watchlist_enabled: document.getElementById('cfg_rjo_watch_on').value==='1',
    rj_only_watchlist_refresh_minutes: parseInt(document.getElementById('cfg_rjo_watch_refresh').value)||60,
    rj_only_watchlist_eval_symbols: parseInt(document.getElementById('cfg_rjo_watch_eval').value)||80,
    rj_only_watchlist_size: parseInt(document.getElementById('cfg_rjo_watch_size').value)||80,
    rj_only_watchlist_min_samples: parseInt(document.getElementById('cfg_rjo_watch_samples').value)||8,
    rj_only_watchlist_min_win_rate: parseFloat(document.getElementById('cfg_rjo_watch_win').value)||52,
    rj_only_watchlist_min_avg_r: parseFloat(document.getElementById('cfg_rjo_watch_avg_r').value)||0,
    rj_only_watchlist_discovery_top_n: parseInt(document.getElementById('cfg_rjo_discovery').value)||30,
    rj_only_setup_pool_enabled: document.getElementById('cfg_rjo_pool_on').value==='1',
    rj_only_setup_confirm_mode: document.getElementById('cfg_rjo_setup_mode').value,
    rj_only_setup_close_confirm_sec: parseInt(document.getElementById('cfg_rjo_close_sec').value)||120,
    rj_only_setup_check_interval_sec: parseInt(document.getElementById('cfg_rjo_check_sec').value)||10,
    rj_only_setup_max_pool: parseInt(document.getElementById('cfg_rjo_max_pool').value)||40,
    rj_only_sr_filter: document.getElementById('cfg_rjo_sr_filter').value==='1',
    rj_only_require_divergence: document.getElementById('cfg_rjo_div_req').value==='1',
    rj_only_atr_sl_mult: parseFloat(document.getElementById('cfg_rjo_atr_sl').value)||0.5,
    rj_only_confirm_atr_buffer: parseFloat(document.getElementById('cfg_rjo_confirm_buf').value)||0.08,
    rj_only_min_stop_pct: parseFloat(document.getElementById('cfg_rjo_min_stop').value)||0.003,
    rj_only_max_stop_pct: parseFloat(document.getElementById('cfg_rjo_max_stop').value)||0.08,
    risk_per_trade: document.getElementById('cfg_risk').value,
    leverage: parseInt(document.getElementById('cfg_leverage').value),
    max_daily_loss: parseFloat(document.getElementById('cfg_maxdl').value),
    account_initial_equity: parseFloat(document.getElementById('cfg_initial_equity').value)||0,
    max_consecutive_loss: parseInt(document.getElementById('cfg_maxcl').value),
    max_position_usdt: parseFloat(document.getElementById('cfg_maxval').value),
    cooldown_minutes: parseInt(document.getElementById('cfg_cooldown').value),
    half_risk_trigger_r: parseFloat(document.getElementById('cfg_half_risk_r').value)||0,
    enable_early_protect: document.getElementById('cfg_early_protect').value==='1',
    early_protect_r: parseFloat(document.getElementById('cfg_early_r').value)||0.8,
    early_protect_lock_r: parseFloat(document.getElementById('cfg_early_lock').value)||0,
    tier1_defense_r: parseFloat(document.getElementById('cfg_tier1').value)||1.2,
    tier2_partial_r: parseFloat(document.getElementById('cfg_tier2').value)||2.0,
    enable_time_stop: document.getElementById('cfg_time_stop').value==='1',
    time_stop_bars: document.getElementById('cfg_time_bars').value,
    time_stop_min_r: parseFloat(document.getElementById('cfg_time_minr').value)||0.6,
    use_atr_trail: document.getElementById('cfg_atr_trail').value==='1',
    ema_ratchet: parseInt(document.getElementById('cfg_ratchet').value)||20,
    atr_trail_mult: parseFloat(document.getElementById('cfg_atr_mult').value)||3.5,
    atr_trail_period: parseInt(document.getElementById('cfg_atr_period').value)||14,
    hermes_confirm_enabled: document.getElementById('cfg_hermes_on').value==='1',
    hermes_confirm_mode: document.getElementById('cfg_hermes_mode').value,
    hermes_confirm_min_confidence: parseFloat(document.getElementById('cfg_hermes_conf').value)||65,
    hermes_confirm_timeout_sec: parseInt(document.getElementById('cfg_hermes_timeout').value)||240,
    hermes_confirm_queue_wait_sec: parseInt(document.getElementById('cfg_hermes_queue_wait').value)||300,
    hermes_confirm_fail_open: document.getElementById('cfg_hermes_fail_open').value==='1',
  };
  // 始终带上所有API字段, 防止切换交易所时覆盖丢失
  cfg.bitget_api_key=document.getElementById('cfg_bgkey').value;
  cfg.bitget_api_secret=document.getElementById('cfg_bgsecret').value;
  cfg.bitget_api_pass=document.getElementById('cfg_bgpass').value;
  cfg.testnet_api_key=document.getElementById('cfg_testkey').value;
  cfg.testnet_api_secret=document.getElementById('cfg_testsecret').value;
  cfg.api_key=document.getElementById('cfg_apikey').value;
  cfg.api_secret=document.getElementById('cfg_secret').value;
  cfg.mode=(cfg.api_key||cfg.testnet_api_key||cfg.bitget_api_key)?'live':'paper';
  var wp=document.getElementById('cfg_webpwd').value;
  if(wp) cfg.web_password=wp;
  fetch('/trader/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)})
  .then(function(r){return r.json();}).then(function(){
    var btns=document.querySelectorAll('#main button.btn');
    for(var i=0; i<btns.length; i++){if(btns[i].textContent.indexOf('保存')>=0){btns[i].textContent='已保存'; setTimeout(function(){btns[i].textContent='保存配置'}, 1500);}}
  });
}

// ===== RJ / BBKD INDICATOR =====
function _rjFmt(v,d){if(v===null||v===undefined||isNaN(v))return '--';return Number(v).toFixed(d);}
function renderRjIndicator(){
  setDesc('rj_indicator');
  document.getElementById('scanLabel').textContent='当前: RJ指标策略';
  document.getElementById('statusCount').textContent='只读图表';
  document.getElementById('stats').innerHTML='';
  var h='<div class="rj-board">';
  h+='<div class="rj-head"><div><div class="rj-title">RJ / BBKD K线策略</div><div class="rj-sub">绿色J快线 × 紫线原版校准 × 布林趋势色 × 关键K确认</div></div>';
  h+='<div class="rj-controls"><input id="rjSymbol" value="BTCUSDT" spellcheck="false"><select id="rjInterval"><option>15m</option><option selected>1h</option><option>4h</option><option>1d</option></select><button class="btn" onclick="loadRjChart()">刷新</button></div></div>';
  h+='<div class="rj-metrics" id="rjMetrics"></div>';
  h+='<div class="rj-chart" id="rjChart"><div class="empty-state"><div class="ic-empty"></div><h3>加载RJ指标...</h3></div></div>';
  h+='<div class="rj-note">三角形是RJ金叉后的止跌关键K；视频SOP要求等待后续K线以收盘价突破关键K高点才买入，跌破低点则放弃。紫色K同理用于止涨/卖出确认。</div>';
  h+='<div class="rj-events" id="rjEvents"></div></div>';
  document.getElementById('main').innerHTML=h;
  loadRjChart();
}
function loadRjChart(){
  var sym=(document.getElementById('rjSymbol').value||'BTCUSDT').toUpperCase().replace(/[^A-Z0-9]/g,'');
  var itv=document.getElementById('rjInterval').value||'1h';
  document.getElementById('rjChart').innerHTML='<div class="empty-state"><div class="ic-empty"></div><h3>同步K线与RJ信号...</h3></div>';
  fetch('/rj_indicator?symbol='+encodeURIComponent(sym)+'&interval='+encodeURIComponent(itv)).then(function(r){return r.json();}).then(function(d){
    if(!d.ok){document.getElementById('rjChart').innerHTML='<div class="empty-state"><div class="ic-warn"></div><h3>'+((d.error)||'数据获取失败')+'</h3></div>';return;}
    renderRjMetrics(d,sym,itv);
    drawRjSvg(d.rows,d.events);
    renderRjEvents(d.events);
  }).catch(function(e){
    document.getElementById('rjChart').innerHTML='<div class="empty-state"><div class="ic-warn"></div><h3>'+String(e).slice(0,80)+'</h3></div>';
  });
}
function renderRjMetrics(d,sym,itv){
  var s=d.summary||{}, trend=s.bb_color==='blue'?'蓝色上升':(s.bb_color==='purple'?'紫色偏空':'中性');
  var cls=s.bb_color==='blue'?'g':(s.bb_color==='purple'?'p':'c');
  document.getElementById('rjMetrics').innerHTML=
    '<div class="rj-metric"><span>交易对</span><b>'+sym+'</b></div>'+
    '<div class="rj-metric"><span>周期</span><b>'+itv+'</b></div>'+
    '<div class="rj-metric"><span>价格</span><b>$'+_rjFmt(s.last_price,2)+'</b></div>'+
    '<div class="rj-metric"><span>布林趋势</span><b class="'+cls+'">'+trend+'</b></div>'+
    '<div class="rj-metric"><span>J / 紫线</span><b>'+_rjFmt(s.j,1)+' / '+_rjFmt(s.r,1)+'</b></div>'+
    '<div class="rj-metric"><span>确认买/卖</span><b>'+s.buy_count+' / '+s.sell_count+'</b></div>';
}
function drawRjSvg(rows,events){
  if(!rows||rows.length<30){document.getElementById('rjChart').innerHTML='<div class="empty-state"><h3>K线不足</h3></div>';return;}
  var totalRows=rows.length;
  rows=rows.slice(-160);
  var idxOffset=Math.max(0,totalRows-rows.length);
  var W=1080,H=560,L=58,R=18,T=22,priceH=356,oscT=410,oscH=120;
  var highs=rows.map(function(x){return x.h}), lows=rows.map(function(x){return x.l});
  rows.forEach(function(x){if(x.bb_up)highs.push(x.bb_up);if(x.bb_dn)lows.push(x.bb_dn);});
  var maxP=Math.max.apply(null,highs), minP=Math.min.apply(null,lows), pad=(maxP-minP)*0.08||1; maxP+=pad; minP-=pad;
  function xAt(i){return L+i*(W-L-R)/Math.max(1,rows.length-1);}
  function yP(v){return T+(maxP-v)/(maxP-minP)*priceH;}
  function yO(v){return oscT+(100-v)/100*oscH;}
  var cw=Math.max(2,(W-L-R)/rows.length*0.58);
  var svg='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">';
  svg+='<rect x="0" y="0" width="'+W+'" height="'+H+'" fill="transparent"/>';
  for(var g=0;g<5;g++){var gy=T+g*priceH/4;svg+='<line x1="'+L+'" y1="'+gy+'" x2="'+(W-R)+'" y2="'+gy+'" stroke="rgba(148,163,184,0.10)" stroke-dasharray="4 6"/>';svg+='<text x="8" y="'+(gy+4)+'" fill="rgba(148,163,184,0.72)" font-size="11" font-family="monospace">$'+(maxP-g*(maxP-minP)/4).toFixed(2)+'</text>';}
  function linePath(key){var pts=[];for(var i=0;i<rows.length;i++){var v=rows[i][key];if(v!==null&&v!==undefined){pts.push((pts.length?'L':'M')+xAt(i).toFixed(1)+' '+yP(v).toFixed(1));}}return pts.join(' ');}
  svg+='<path d="'+linePath('bb_up')+'" fill="none" stroke="rgba(96,165,250,0.35)" stroke-width="1.2" vector-effect="non-scaling-stroke"/>';
  svg+='<path d="'+linePath('bb_mid')+'" fill="none" stroke="rgba(184,188,193,0.35)" stroke-width="1" stroke-dasharray="5 5" vector-effect="non-scaling-stroke"/>';
  svg+='<path d="'+linePath('bb_dn')+'" fill="none" stroke="rgba(168,85,247,0.35)" stroke-width="1.2" vector-effect="non-scaling-stroke"/>';
  for(var i=0;i<rows.length;i++){
    var r=rows[i], x=xAt(i), up=r.c>=r.o, col=r.purple?'#a855f7':(up?'#34d399':'#f87171');
    if(r.bb_color==='blue'&&!r.purple) col=up?'#5eead4':'#60a5fa';
    svg+='<line x1="'+x.toFixed(1)+'" y1="'+yP(r.h).toFixed(1)+'" x2="'+x.toFixed(1)+'" y2="'+yP(r.l).toFixed(1)+'" stroke="'+col+'" stroke-width="1" vector-effect="non-scaling-stroke"/>';
    var y=Math.min(yP(r.o),yP(r.c)), hh=Math.max(1,Math.abs(yP(r.o)-yP(r.c)));
    svg+='<rect x="'+(x-cw/2).toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+cw.toFixed(1)+'" height="'+hh.toFixed(1)+'" fill="'+col+'" opacity="'+(up?'0.72':'0.62')+'"/>';
    if(r.triangle){svg+='<path d="M '+x.toFixed(1)+' '+(yP(r.l)+13).toFixed(1)+' L '+(x-6).toFixed(1)+' '+(yP(r.l)+24).toFixed(1)+' L '+(x+6).toFixed(1)+' '+(yP(r.l)+24).toFixed(1)+' Z" fill="#fbbf24"/>';}
    if(r.bull_div){svg+='<text x="'+x.toFixed(1)+'" y="'+(yP(r.l)+38).toFixed(1)+'" fill="#34d399" font-size="10" text-anchor="middle">DIV</text>';}
  }
  (events||[]).filter(function(e){return e.idx>=idxOffset;}).forEach(function(e){var i=e.idx-idxOffset;if(i<0||i>=rows.length)return;var x=xAt(i), row=rows[i], y=e.type.indexOf('sell')>=0||e.type==='purple'?yP(row.h)-10:yP(row.l)+42;var c=e.type==='buy_confirm'?'#34d399':(e.type==='sell_confirm'?'#f87171':(e.type==='purple'?'#a855f7':'#fbbf24'));svg+='<circle cx="'+x.toFixed(1)+'" cy="'+y.toFixed(1)+'" r="3.5" fill="'+c+'"/><text x="'+(x+5).toFixed(1)+'" y="'+(y+3).toFixed(1)+'" fill="'+c+'" font-size="10" font-family="monospace">'+e.text+'</text>';});
  svg+='<text class="rj-panel-title" x="'+L+'" y="'+(oscT-12)+'">RJ oscillator: green J / calibrated purple line</text>';
  [25,50,75].forEach(function(v){svg+='<line x1="'+L+'" y1="'+yO(v).toFixed(1)+'" x2="'+(W-R)+'" y2="'+yO(v).toFixed(1)+'" stroke="rgba(148,163,184,0.12)" stroke-dasharray="3 6"/>';});
  var jPts=[], rPts=[];for(var k=0;k<rows.length;k++){if(rows[k].j!==null)jPts.push(xAt(k).toFixed(1)+','+yO(Math.max(0,Math.min(100,rows[k].j))).toFixed(1));if(rows[k].r!==null)rPts.push(xAt(k).toFixed(1)+','+yO(Math.max(0,Math.min(100,rows[k].r))).toFixed(1));}
  svg+='<polyline points="'+jPts.join(' ')+'" fill="none" stroke="#2dd4bf" stroke-width="1.6" vector-effect="non-scaling-stroke"/>';
  svg+='<polyline points="'+rPts.join(' ')+'" fill="none" stroke="#c084fc" stroke-width="1.6" vector-effect="non-scaling-stroke"/>';
  svg+='</svg>';
  document.getElementById('rjChart').innerHTML=svg;
}
function renderRjEvents(events){
  events=(events||[]).slice(-8).reverse();
  if(!events.length){document.getElementById('rjEvents').innerHTML='';return;}
  var h='';events.forEach(function(e){h+='<div class="rj-event">'+e.idx+' · '+e.text+' · $'+_rjFmt(e.price,4)+'</div>';});
  document.getElementById('rjEvents').innerHTML=h;
}

// ===== KEYBOARD =====
document.addEventListener('keydown', function(e){
  var tag=document.activeElement.tagName;
  if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT') return;
  var k=parseInt(e.key);
  if(k>=1&&k<=9){
    var ids=['squeeze_4h','squeeze_1h','squeeze_15m','squeeze_1d','squeeze_1w','breakout_4h','breakout_1h','breakout_1d','short_4h'];
    selectTab(ids[k-1]);
  }
  if(e.key==='0') selectTab('short_1h');
  if(e.key==='T'||e.key==='t') selectTab('trader');
});

// ===== BTC MONITOR =====
var btcPoll=null;
function renderBtcMonitor(){
  setDesc('btc_monitor');
  document.getElementById('stats').innerHTML='';
  document.getElementById('scanLabel').textContent='当前: BTC 大盘监控';
  fetchBtcData();
  if(btcPoll) clearInterval(btcPoll);
  btcPoll=setInterval(fetchBtcData, 30000);
}

function fetchBtcData(){
  fetch('/btc_monitor').then(function(r){return r.json();}).then(function(resp){
    if(!resp.ok){document.getElementById('main').innerHTML='<div class="empty-state"><div class="ic-warn"></div><h3>数据获取失败</h3></div>'; return;}
    var d=resp.data;
    var intervals=['1h','4h','1d','1w'];
    var labels={'1h':'1 小时','4h':'4 小时','1d':'日 线','1w':'周 线'};
    var icons={'1h':'1H','4h':'4H','1d':'日','1w':'周'};
    var h='<div class="btc-dashboard">';

    // Header
    h+='<div class="btc-header">';
    h+='<div class="btc-header-left"><span class="btc-icon">₿</span><span class="btc-title">BTC 大盘监控</span></div>';
    h+='<div class="btc-header-right"><span class="btc-pulse"></span><span style="color:var(--text2);font-size:11px">实时 · '+resp.time+'</span></div>';
    h+='</div>';

    // Signal bar — overall summary
    h+='<div class="btc-signal-bar">';
    for(var i=0; i<intervals.length; i++){
      var itv=intervals[i], di=d[itv];
      if(!di||di.error){h+='<div class="btc-signal-item err"><span>'+labels[itv]+'</span><small>无数据</small></div>'; continue;}
      h+='<div class="btc-signal-item '+di.ov_class+'"><span>'+labels[itv]+'</span><b>'+di.overall+'</b></div>';
    }
    h+='</div>';

    // Detail cards
    h+='<div class="btc-grid">';
    for(var i=0; i<intervals.length; i++){
      var itv=intervals[i], di=d[itv];
      if(!di||di.error){h+='<div class="btc-card err"><div style="text-align:center;color:var(--muted);padding:30px">'+labels[itv]+' 数据异常</div></div>'; continue;}
      var alCls={'多头排列':'ali-bull','空头排列':'ali-bear','交叉震荡':'ali-neu'}[di.alignment]||'';
      h+='<div class="btc-card '+di.ov_class+'">';
      // Card header
      h+='<div class="btc-card-head"><span class="btc-card-label">'+labels[itv]+'</span><span class="btc-card-price">$'+di.price.toFixed(2)+'</span></div>';
      // EMA alignment
      h+='<div class="btc-row"><span class="btc-row-label">趋势结构</span><span class="btc-row-val '+alCls+'">'+di.alignment+'</span></div>';
      h+='<div class="btc-row sub"><span>EMA20</span><span>'+di.ema20+'</span><span>EMA60</span><span>'+di.ema60+'</span><span>EMA120</span><span>'+di.ema120+'</span></div>';
      // Squeeze
      var sqCls={'极限压缩':'sq-tight','波动收敛':'sq-dense','趋势扩张':'sq-spread','趋势扩张':'sq-trend'}[di.squeeze_state]||'';
      h+='<div class="btc-row"><span class="btc-row-label">波动状态</span><span class="btc-row-val '+sqCls+'">'+di.squeeze_state+' <small>('+di.spread_pct+'%)</small></span></div>';
      // Band + Position
      h+='<div class="btc-row sub"><span>带上轨</span><span>'+di.band_hi+'</span><span>带下轨</span><span>'+di.band_lo+'</span></div>';
      h+='<div class="btc-row"><span class="btc-row-label">价格位置</span><span class="btc-row-val pos-'+di.pos_class+'">'+di.position+'</span></div>';
      // Breakout
      var brkCls={'向上突破':'brk-up','向下突破':'brk-down','无突破':'brk-none'}[di.breakout_dir]||'';
      h+='<div class="btc-row"><span class="btc-row-label">突破状态</span><span class="btc-row-val '+brkCls+'">'+di.breakout_dir+'</span></div>';
      // Fractal
      var fracCls={'底部拐点':'frac-up','顶部拐点':'frac-down'}[di.fractal]||'';
      h+='<div class="btc-row"><span class="btc-row-label">拐点信号</span><span class="btc-row-val '+fracCls+'">'+(di.fractal!='--'?'● ':'○ ')+di.fractal+'</span></div>';
      // Overall badge
      h+='<div class="btc-overall '+di.ov_class+'">'+di.overall+'</div>';
      h+='</div>';
    }
    h+='</div>';

    h+='<div class="btc-footer">趋势结构 · 波动压缩度 · 方向信号 · 价格结构 · 综合评估 &nbsp;|&nbsp; 仅监控不交易</div>';
    h+='</div>';
    document.getElementById('main').innerHTML=h;
  });
}

// ===== ROLLER =====
var _rlStages=[[0.00,25],[0.04,20],[0.05,20],[0.05,15],[0.07,10],[0.10,5],[0.20,3],[0.35,2],[0.50,1],[1.00,0.5],[2.00,0.2],[5.00,0.1]];
function _rlFmt(n,d){var s=Math.abs(n).toFixed(d); var p=s.split('.'); p[0]=p[0].replace(/\B(?=(\d{3})+(?!\d))/g,','); return (n<0?'-':'')+p.join('.');}
function _rlFp(n){return n<1?n.toFixed(6):n.toFixed(4);}
function renderRoller(){
  setDesc('roller');
  document.getElementById('stats').innerHTML='';
  document.getElementById('scanLabel').textContent='当前: 浮云滚仓';
  var h='<div class="roller-wrap">';
  h+='<div style="text-align:center;margin-bottom:24px"><h3 style="font-size:18px;font-weight:700;color:var(--text);letter-spacing:-0.02em">浮云滚仓策略计算器</h3><p style="font-size:12px;color:var(--muted);margin-top:4px">币本位复利模型 · 多空双向 · 12阶段杠杆推演</p></div>';
  h+='<div class="calc-card" style="padding:20px;margin-bottom:20px">';
  h+='<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px">';
  h+='<div><label class="c-label">初始成本价格</label><input type="number" id="rlCost" value="0.06" step="0.000001" oninput="rollerCalc()"></div>';
  h+='<div><label class="c-label">币本位本金</label><input type="number" id="rlPrincipal" value="10000" oninput="rollerCalc()"></div>';
  h+='<div><label class="c-label">仓位方向</label><select id="rlDir" onchange="rollerCalc()" style="width:100%;padding:9px 10px;background:#1D1F21;color:#D4D6D9;border:1px solid var(--border);border-radius:5px;font-size:13px;font-family:var(--font)"><option value="long" style="background:#1D1F21;color:#D4D6D9">做多 Long</option><option value="short" style="background:#1D1F21;color:#D4D6D9">做空 Short</option></select></div>';
  h+='</div></div>';
  h+='<div class="roller-table"><table><thead><tr><th class="text-center">阶段</th><th>目标单价</th><th class="text-right">变动</th><th class="text-right">币本位本金</th><th class="text-center">杠杆</th><th class="text-right">维持仓位(币)</th><th class="text-right">币本位收益</th><th class="text-right">资产总额(U)</th></tr></thead><tbody id="rlBody"></tbody></table></div>';
  h+='<div class="roller-info">* 币本位收益 = 上一阶段维持仓位 × 变动幅度；币本位本金 = 上一阶段本金 + 收益；资产总额 = 币本位本金 × 当前单价。<br>* 算法严格遵循币本位复利分配，请结合实际波动严格风控。</div>';
  h+='</div>';
  document.getElementById('main').innerHTML=h;
  rollerCalc();
}
function rollerCalc(){
  var price=parseFloat(document.getElementById('rlCost').value)||0;
  var prin=parseFloat(document.getElementById('rlPrincipal').value)||0;
  var dir=document.getElementById('rlDir').value;
  var dm=dir==='long'?1:-1;
  var pPrice=price, pPrin=prin, pPos=0, h='';
  for(var i=0; i<_rlStages.length; i++){
    var move=_rlStages[i][0], lev=_rlStages[i][1], curP, profit, curPrin, curPos, totalU;
    if(i===0){curP=price; profit=0; curPrin=prin; curPos=curPrin*lev; totalU=curPrin*curP;}
    else{curP=pPrice*(1+dm*move); profit=pPos*move; curPrin=pPrin+profit; curPos=curPrin*lev; totalU=curPrin*curP;}
    var hl=i===7?' class="roller-highlight"':'';
    var sign=dir==='long'?'+':'-';
    var disMove=i===0?'0%':sign+(move*100).toFixed(0)+'%';
    var badge=dir==='long'?'roller-badge-l':'roller-badge-s';
    h+='<tr'+hl+'><td class="text-center">'+i+'</td><td>'+_rlFp(curP)+'</td><td class="text-right"><span class="'+badge+'">'+disMove+'</span></td><td class="text-right">'+_rlFmt(curPrin,0)+'</td><td class="text-center">'+lev+'</td><td class="text-right">'+_rlFmt(curPos,0)+'</td><td class="text-right">'+_rlFmt(profit,0)+'</td><td class="text-right">'+_rlFmt(totalU,2)+'</td></tr>';
    pPrice=curP; pPrin=curPrin; pPos=curPos;
  }
  document.getElementById('rlBody').innerHTML=h;
}

// ===== CALCULATOR =====
function renderCalculator(){
  setDesc('calculator');
  document.getElementById('stats').innerHTML='';
  document.getElementById('scanLabel').textContent='当前: 仓位计算器';
  var h='<div class="calc-wrap">';
  h+='<div style="text-align:center;margin-bottom:24px"><h3 style="font-size:18px;font-weight:700;color:var(--text);letter-spacing:-0.02em">固定风险仓位计算器</h3><p style="font-size:12px;color:var(--muted);margin-top:4px">多维推演 · 杠杆测算</p></div>';

  // Single unified card
  h+='<div class="calc-card" style="padding:20px">';

  // Capital
  h+='<div style="margin-bottom:20px"><label class="c-label">总本金 (USDT)</label><input type="number" id="cBankroll" value="1000" oninput="calcUpdate()"></div>';

  // Risk trio
  h+='<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:20px;padding-bottom:20px;border-bottom:1px solid var(--border)">';
  h+='<div><label class="c-label">风险比例 %</label><input type="number" id="cRiskPct" value="2" step="0.1" onfocus="calcSetDriver(\'pct\')" oninput="calcUpdate()"></div>';
  h+='<div><label class="c-label">固定亏损 $</label><input type="number" id="cRiskAmt" value="20" onfocus="calcSetDriver(\'amt\')" oninput="calcUpdate()" style="border-color:rgba(34,197,94,0.3);background:rgba(34,197,94,0.03)"></div>';
  h+='<div><label class="c-label">开仓数量</label><input type="number" id="cSize" value="0.04" onfocus="calcSetDriver(\'size\')" oninput="calcUpdate()" style="border-color:rgba(59,130,246,0.3);background:rgba(59,130,246,0.03)"></div>';
  h+='</div>';

  // Price inputs
  h+='<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">';
  h+='<div><label class="c-label">开仓价</label><input type="number" id="cEntry" value="64000" oninput="calcUpdate()"></div>';
  h+='<div><label class="c-label">硬止损</label><input type="number" id="cSl" value="63500" oninput="calcUpdate()"></div>';
  h+='</div>';
  h+='<div style="margin-bottom:12px"><label class="c-label">止盈价 <span style="color:var(--muted);font-weight:400">(可选)</span></label><input type="number" id="cTp" placeholder="不填则不计算" oninput="calcUpdate()"></div>';
  h+='<div id="cDirBadge" style="text-align:center;padding:6px;border-radius:4px;font-size:12px;font-weight:600;margin-bottom:16px"></div>';

  // Leverage
  h+='<div style="padding:12px 0 20px;border-top:1px solid var(--border)"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><label class="c-label" style="margin:0">杠杆倍数</label><span style="font-weight:700;font-size:16px;color:var(--s-blue)" id="cLevDisplay">10x</span></div>';
  h+='<input type="range" id="cLeverage" min="1" max="100" value="10" oninput="calcUpdate()"></div>';

  // Result block
  h+='<div style="background:var(--bg);border-radius:8px;padding:16px;margin-top:4px">';
  h+='<div class="calc-hint" style="margin-bottom:4px">标准开仓数量</div>';
  h+='<div style="display:flex;align-items:baseline;gap:6px;margin-bottom:12px"><span style="font-size:32px;font-weight:800;color:var(--brand);font-family:var(--font-mono);letter-spacing:-0.03em" id="cPosSize">0.00</span><span style="font-size:12px;color:var(--text2)">Base</span></div>';
  h+='<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;padding-top:14px;border-top:1px solid var(--border)">';
  h+='<div><div class="calc-hint">名义总价值</div><div style="font-family:var(--font-mono);font-size:15px;color:var(--text)" id="cNotional">$0.00</div></div>';
  h+='<div><div class="calc-hint">所需保证金</div><div style="font-family:var(--font-mono);font-size:15px;color:var(--s-yellow);font-weight:700" id="cMargin">$0.00</div></div>';
  h+='</div>';
  h+='<div id="cRrr" style="display:none;margin-top:14px;padding-top:14px;border-top:1px solid var(--border);display:grid;grid-template-columns:1fr 1fr;gap:14px">';
  h+='<div><div class="calc-hint">预期盈利</div><div style="font-family:var(--font-mono);font-size:15px;color:var(--s-green)" id="cProfit">$0.00</div></div>';
  h+='<div><div class="calc-hint">盈亏比 RRR</div><div style="font-family:var(--font-mono);font-size:15px;font-weight:700" id="cRrrVal">1 : 0.00</div></div>';
  h+='</div></div>';

  h+='</div></div>';
  document.getElementById('main').innerHTML=h;
  calcUpdate();
}

var _calcDriver='pct';
function calcSetDriver(d){_calcDriver=d;calcUpdate();}
function calcUpdate(){
  var br=parseFloat(document.getElementById('cBankroll').value)||0;
  var entry=parseFloat(document.getElementById('cEntry').value)||0;
  var sl=parseFloat(document.getElementById('cSl').value)||0;
  var tp=parseFloat(document.getElementById('cTp').value);
  var lev=parseInt(document.getElementById('cLeverage').value)||1;
  var stopDist=Math.abs(entry-sl);
  document.getElementById('cLevDisplay').textContent=lev+'x';

  if(entry===0||sl===0||entry===sl){
    document.getElementById('cPosSize').textContent='0.00';
    document.getElementById('cDirBadge').style.display='none';
    return;
  }

  var isLong=entry>sl;
  var badge=document.getElementById('cDirBadge');
  badge.style.display='block';
  badge.textContent=isLong?'做多 LONG':'做空 SHORT';
  badge.className=isLong?'calc-badge long':'calc-badge short';

  var pct=parseFloat(document.getElementById('cRiskPct').value)||0;
  var amt=parseFloat(document.getElementById('cRiskAmt').value)||0;
  var size=parseFloat(document.getElementById('cSize').value)||0;

  if(_calcDriver==='pct'){amt=br*(pct/100);document.getElementById('cRiskAmt').value=amt.toFixed(2);size=stopDist>0?amt/stopDist:0;document.getElementById('cSize').value=size<0.001?size.toFixed(5):size.toFixed(3);}
  else if(_calcDriver==='amt'){pct=br>0?(amt/br)*100:0;document.getElementById('cRiskPct').value=pct.toFixed(2);size=stopDist>0?amt/stopDist:0;document.getElementById('cSize').value=size<0.001?size.toFixed(5):size.toFixed(3);}
  else if(_calcDriver==='size'){amt=size*stopDist;document.getElementById('cRiskAmt').value=amt.toFixed(2);pct=br>0?(amt/br)*100:0;document.getElementById('cRiskPct').value=pct.toFixed(2);}

  document.getElementById('cPosSize').textContent=size<0.001?size.toFixed(5):size.toFixed(3);
  var notional=size*entry;
  document.getElementById('cNotional').textContent='$'+notional.toFixed(2);
  document.getElementById('cMargin').textContent='$'+(notional/lev).toFixed(2);

  var rrrEl=document.getElementById('cRrr');
  if(!isNaN(tp)&&tp!==0&&((isLong&&tp>entry)||(!isLong&&tp<entry))){
    rrrEl.style.display='grid';
    var profitDist=Math.abs(tp-entry);
    var expProfit=size*profitDist;
    var riskAmt=size*stopDist;
    document.getElementById('cProfit').textContent='$'+expProfit.toFixed(2);
    document.getElementById('cRrrVal').textContent='1 : '+(riskAmt>0?expProfit/riskAmt:0).toFixed(2);
  }else{rrrEl.style.display='none';}
}

// ===== AUTH =====
var _authUser='', _authRole='';
// Gateway functions
function switchGwForm(f){
  document.getElementById('gwLoginForm').style.display=f==='login'?'block':'none';
  document.getElementById('gwRegForm').style.display=f==='reg'?'block':'none';
  document.getElementById('gwForgotForm').style.display=f==='forgot'?'block':'none';
  document.getElementById('authMsg').style.display='none'; _forgotStep=0;
  if(f==='forgot') document.getElementById('gwResetFields').style.display='none';
}
document.addEventListener('mousemove',function(e){
  var gl=document.getElementById('cursorGlow'); if(gl){gl.style.left=e.clientX+'px';gl.style.top=e.clientY+'px';}
});
function _runTerminal(cb){
  var t=document.getElementById('gwTerm'),btn=document.querySelector('.gw-btn');
  if(!t) return cb();
  t.classList.add('active'); t.innerHTML=''; var logs=[
    '> 正在建立安全握手协议... [OK]',
    '> 正在核实节点身份...',
    '> 正在解密非对称安全密钥...',
    '> 量子加密签名匹配完成。',
    '> 授权通过。正在路由至核心交易网络...'
  ];
  var d=0; logs.forEach(function(l,i){
    setTimeout(function(){
      var p=document.createElement('div'); p.className='gw-log-line';
      p.innerHTML=l+(i===logs.length-1?'<span class=\"gw-log-cursor\"></span>':'');
      t.appendChild(p); t.scrollTop=t.scrollHeight;
      if(i===logs.length-1) setTimeout(function(){t.classList.remove('active'); cb();},800);
    }, d); d+=Math.floor(Math.random()*300)+250;
  });
}
function _switchAuthTab(n){switchAuthTab(n===0?'login':'reg');}
function switchAuthTab(t){
  var isLogin=t==='login';
  document.getElementById('tabLogin').style.color=isLogin?'var(--brand)':'var(--muted)';
  document.getElementById('tabLogin').style.borderBottomColor=isLogin?'var(--brand)':'transparent';
  document.getElementById('tabReg').style.color=isLogin?'var(--muted)':'var(--brand)';
  document.getElementById('tabReg').style.borderBottomColor=isLogin?'transparent':'var(--brand)';
  document.getElementById('loginFields').style.display=isLogin?'block':'none';
  document.getElementById('regFields').style.display=isLogin?'none':'block';
  document.getElementById('authMsg').style.display='none';
}
function doAuthLogin(){
  var u=document.getElementById('authUser').value, p=document.getElementById('authPwd').value;
  if(!u||!p){var m=document.getElementById('authMsg');m.style.display='block';m.textContent='请输入用户名和密码'; return;}
  var m=document.getElementById('authMsg'); m.style.display='none';
  var btn=document.querySelector('.gw-btn'); if(btn){btn.disabled=true;btn.style.opacity='0.5';btn.textContent='验证安全凭证中...';}
  // 终端动画
  var t=document.getElementById('gwTerm'), logs=[
    '> 正在建立安全握手协议... [OK]',
    '> 正在核实节点身份 ['+u.toUpperCase()+']...',
    '> 正在解密非对称安全密钥...',
    '> 量子加密签名匹配完成。',
    '> 授权通过。正在路由至核心交易网络...'
  ];
  if(t){t.classList.add('active'); t.innerHTML=''; var d=0; logs.forEach(function(l,i){setTimeout(function(){var p2=document.createElement('div');p2.className='gw-log-line';p2.innerHTML=l+(i===logs.length-1?'<span class=\"gw-log-cursor\"></span>':'');t.appendChild(p2);t.scrollTop=t.scrollHeight;if(i===logs.length-1){setTimeout(function(){t.classList.remove('active');doActualLogin(u,p);},600);}},d);d+=Math.floor(Math.random()*300)+250;});}
  else{doActualLogin(u,p);}
}
function doActualLogin(u,p){
  var m=document.getElementById('authMsg'), btn=document.querySelector('.gw-btn');
  fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})})
  .then(function(r){return r.json()}).then(function(d){
    if(d.ok){
      _authUser=u; _authRole=d.role;
      document.getElementById('authOverlay').style.display='none'; document.body.classList.remove('auth-locked');
      document.getElementById('scanLabel').textContent='欢迎, '+u; document.getElementById('headerUser').textContent=u;
      checkNewUser();
    }else{m.style.display='block'; m.textContent=d.msg;}
    if(btn){btn.disabled=false;btn.style.opacity='1';btn.textContent='建立安全连接';}
  });
}
function doAuthReg(){
  var u=document.getElementById('regUser').value, p=document.getElementById('regPwd').value, e=document.getElementById('regEmail').value;
  var m=document.getElementById('authMsg');
  if(u.length<3||p.length<6){m.style.display='block'; m.textContent='用户名3位/密码6位以上'; return;}var p2=document.getElementById('regPwd2').value;if(p!==p2){m.style.display='block'; m.textContent='两次密码不一致'; return;}
  if(!e||e.indexOf('@')<0){m.style.display='block'; m.textContent='请填写有效邮箱'; return;}
  fetch('/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p,email:e})})
  .then(function(r){return r.json()}).then(function(d){
    if(d.ok){
      document.getElementById('authUser').value=u; document.getElementById('authPwd').value='';
      switchGwForm('login');
      m.style.display='block'; m.style.color='var(--brand)'; m.style.fontSize='14px'; m.style.fontWeight='700';
      m.style.padding='12px'; m.style.border='1px solid rgba(45,212,191,0.3)'; m.style.borderRadius='8px';
      m.style.background='rgba(45,212,191,0.08)'; m.textContent='✓ 注册成功！请登录';
      var gwTerm=document.getElementById('gwTerm');
      if(gwTerm){gwTerm.classList.add('active'); gwTerm.innerHTML='<div class=\"gw-log-line\" style=\"color:var(--brand)\">> 账户创建完成，7天试用许可已激活</div><div class=\"gw-log-line\" style=\"color:var(--brand)\">> 已自动填入用户名，请输入密码登录</div>';}
    }else{m.style.display='block'; m.textContent=d.msg;}
  });
}
var _forgotStep=0;
function showForgotPwd(){
  _forgotStep=0; switchGwForm('login');
  document.getElementById('gwLoginForm').innerHTML='<div class="t-field"><label>注册邮箱</label><input type="text" id="forgotEmail" placeholder="输入注册时使用的邮箱"></div><button class="btn" style="width:100%;background:var(--brand);color:#000;font-weight:700;padding:12px;margin-top:8px" onclick="doForgotPwd()">获取验证码</button><p style="text-align:center;margin-top:8px"><a href="#" onclick="location.reload()" style="font-size:10px;color:var(--muted)">返回登录</a></p>';
}
function doForgotPwd(){
  var m=document.getElementById('authMsg'); m.style.display='block';
  if(_forgotStep===0){
    var email=document.getElementById('forgotEmail').value;
    if(!email){m.textContent='请输入邮箱'; return;}
    fetch('/auth/forgot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.ok){_forgotStep=1; _forgotEmail=email; m.style.color='var(--brand)'; m.textContent='验证码: '+d.code+' (30分钟有效)'; document.getElementById('gwResetFields').style.display='block';
        document.getElementById('gwLoginForm').innerHTML='<div class="gw-input"><label>验证码</label><input type="text" id="forgotCode" placeholder="输入验证码"></div><div class="gw-input"><label>新密码 (6位以上)</label><input type="password" id="forgotPwd" placeholder="设置新密码"></div><button class="gw-btn" onclick="doForgotPwd()">重置密码</button>';}
      else{m.style.color='var(--s-red)'; m.textContent=d.msg;}
    });
  }else{
    var code=document.getElementById('forgotCode').value, pwd=document.getElementById('forgotPwd').value;
    if(!code||pwd.length<6){m.textContent='请填写验证码和新密码(6位以上)'; return;}
    fetch('/auth/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:_forgotEmail,code:code,password:pwd})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.ok){m.style.color='var(--brand)'; m.textContent='密码重置成功, 请登录'; _switchAuthTab(0); location.reload();}
      else{m.style.color='var(--s-red)'; m.textContent=d.msg;}
    });
  }
}
var _forgotEmail='';
function checkNewUser(){
  fetch('/trader/config').then(function(r){return r.json()}).then(function(cfg){
    // 新用户没有API配置: 默认进入回测看板, 交易面板需自行配置后才能使用
    var hasApi=cfg.api_key||cfg.testnet_api_key;
    var navTrader=document.getElementById('nav_trader');
    if(navTrader){
      if(!hasApi){
        navTrader.style.opacity='0.4';
        navTrader.setAttribute('title','请先配置API密钥');
      }else{
        navTrader.style.opacity='1';
        navTrader.removeAttribute('title');
      }
    }
  });
}
function doLogout(){
  fetch('/auth/logout'); document.getElementById('authOverlay').style.display='flex';
  _authUser=''; document.getElementById('authUser').value=''; document.getElementById('authPwd').value='';
}

// 自动检测登录状态 + 初始化默认页面
fetch('/auth/status').then(function(r){return r.json()}).then(function(s){
  if(s.logged_in){_authUser=s.user; _authRole=s.role; document.getElementById('authOverlay').style.display='none'; document.body.classList.remove('auth-locked'); document.getElementById('scanLabel').textContent='欢迎, '+s.user; document.getElementById('headerUser').textContent=s.user; checkNewUser();}
  else{document.body.classList.add('auth-locked');}
  setDesc('intro'); show('intro', document.getElementById('nav_intro'));
});

// ===== DEMO PANEL =====
var demoPoll=null;
var _demoTradePage=1;
function setDemoTradePage(page){
  _demoTradePage=Math.max(1,parseInt(page)||1);
  fetchDemoData();
}
function renderDemo(){
  setDesc('demo');
  document.getElementById('stats').innerHTML='';
  document.getElementById('scanLabel').textContent='数据回测 · 实时交易数据 · 只读';
  document.getElementById('main').innerHTML='<div style="text-align:center;padding:40px;color:var(--muted)">加载模拟数据...</div>';
  fetchDemoData();
  if(demoPoll) clearInterval(demoPoll);
  demoPoll=setInterval(fetchDemoData,2000);
}
function fetchDemoData(){
  fetch('/demo/status?fast=1&_='+Date.now()).then(function(r){return r.json()}).then(function(d){
    var h='<div class="trader-panel trader-panel-bg">';
    h+='<div style="text-align:center;margin-bottom:20px"><h3 style="font-size:18px;font-weight:700;color:var(--brand)">数据回测</h3><p style="font-size:11px;color:var(--muted)">本金'+Math.round(DEMO_CFG.cfg_maxval||5000)+'U · 风控'+Math.round(DEMO_CFG.cfg_risk||200)+'U · ATR×'+(DEMO_CFG.cfg_atr_mult||3)+'追踪 · 最大'+(DEMO_CFG.cfg_maxpos||10)+'仓 · '+(DEMO_CFG.cfg_interval||'15m')+'周期</p></div>';
    h+='<div class="hud-grid hud-demo"><div class="hud-box"><div class="hud-title">模拟持仓</div><div class="hud-val neu">'+d.positions.length+'</div></div><div class="hud-box"><div class="hud-title">账户余额</div><div class="hud-val g" style="font-size:16px" id="demoBal">--</div></div><div class="hud-box"><div class="hud-title">累计盈亏</div><div class="hud-val '+(d.total_pnl>=0?'g':'r')+'">$'+d.total_pnl.toFixed(2)+'</div></div><div class="hud-box"><div class="hud-title">胜率</div><div class="hud-val neu">'+(d.win_rate||0).toFixed(0)+'%</div></div><div class="hud-box"><div class="hud-title">持续运行</div><div class="hud-val g" id="demoUptime" style="font-size:16px">--</div></div></div>';
    if(d.positions&&d.positions.length>0){
      h+='<div class="pos-grid">';
      for(var i=0;i<d.positions.length;i++){var p=d.positions[i],ip=p.pnl>=0,isL=p.direction==='LONG';
        var etd=(p.entered||''); if(etd) etd=etd.slice(0,16).replace('T',' ');
        h+='<div class="pos-card '+(ip?'profit':'loss')+'">';
        var sInt2=(p.source_interval||DEMO_CFG.cfg_interval||'15m'); h+='<div class="pc-head"><span class="pc-sym">'+p.symbol.replace('USDT','')+'</span><span class="pc-dir '+(isL?'long':'short')+'">'+(isL?'做多 LONG':'做空 SHORT')+'</span><span style="font-size:10px;color:var(--brand);background:rgba(45,212,191,0.08);padding:2px 6px;border-radius:4px;margin-left:8px">'+sInt2+'</span></div>';
        if(etd) h+='<div class="pc-data"><span class="pc-label">开仓时间</span><span class="pc-val">'+etd+'</span></div>';
        h+='<div class="pc-data"><span class="pc-label">入场价</span><span class="pc-val">'+p.entry+'</span></div>';
        h+='<div class="pc-data"><span class="pc-label">当前价</span><span class="pc-val">'+(p.current_price||p.entry).toFixed(6)+'</span></div>';
        h+='<div class="pc-data"><span class="pc-label">止损价</span><span class="pc-val">'+p.sl+'</span></div>';
        h+=choppyLine(p.choppy_filter);
        h+='<div class="pc-data"><span class="pc-label">持仓价值</span><span class="pc-val">$'+(p.value||0).toFixed(2)+'</span></div>';
        h+='<div class="pc-pnl-box"><div class="pc-pnl-val">'+(ip?'+':'')+p.pnl.toFixed(2)+' USDT</div></div>';
        if(p.breakeven) h+='<div class="pc-trail-shimmer">✦ 追踪止盈已开启</div>';
        h+='</div>';}
      h+='</div>';
    }else{h+='<div style="text-align:center;color:var(--muted);padding:30px;border:1px dashed rgba(255,255,255,0.05);border-radius:10px;margin-top:12px">模拟引擎监控中, 暂无持仓</div>';}
    if(d.recent_trades&&d.recent_trades.length>0){
      var tp2=d.total_pnl||0;
      h+='<div style="margin-top:20px"><h4 style="color:var(--s-purple);margin-bottom:6px;font-size:13px">最近交易 <span style="font-weight:400;font-size:10px;color:var(--muted)">共'+d.total_trades+'笔 | 总'+(tp2>=0?'+$':'-$')+Math.abs(tp2).toFixed(2)+' | 胜率'+(d.win_rate||0).toFixed(0)+'%</span></h4><div class="tl-list">';
      var tr=d.recent_trades.slice().reverse().slice(0,8);
      for(var i=0;i<tr.length;i++){var t=tr[i],pnlVal=tradePnlValue(t),tw=pnlVal>=0;h+='<div class="tl-item '+(tw?'tl-win':'tl-loss')+'"><div class="tl-time">'+t.time.slice(5,16).replace('T',' ')+'</div><div class="tl-body"><div class="tl-head"><span class="tl-sym">'+t.symbol.replace('USDT','')+'</span><span class="tl-dir '+(t.direction==='LONG'?'g':'r')+'">'+(t.direction==='LONG'?'多':'空')+'</span><span class="tl-reason">'+t.reason+'</span></div></div><div class="tl-pnl"><span class="tl-pnl-val '+(tw?'g':'r')+'">'+(tw?'+':'')+pnlVal.toFixed(2)+'</span></div></div>';}
      h+='</div></div>';
    }
    h+='</div>';document.getElementById('main').innerHTML=h;
    var upt=d.uptime||0, uh=Math.floor(upt/3600), um=Math.floor((upt%3600)/60);
    var uptEl=document.getElementById('demoUptime'); if(uptEl) uptEl.textContent=uh+'h '+um+'m';var balEl=document.getElementById('demoBal'); if(balEl){var ab=d.account_balance||0; balEl.textContent='$'+Math.round(ab).toLocaleString();}
  });
}

// ===== DEMO EQUITY DASHBOARD OVERRIDE =====
function renderDemo(){
  setDesc('demo');
  document.getElementById('stats').innerHTML='';
  document.getElementById('scanLabel').textContent='演示引擎 · 自动交易净值曲线 · 只读';
  document.getElementById('main').innerHTML='<div style="text-align:center;padding:40px;color:var(--muted)">加载演示引擎状态...</div>';
  fetchDemoData();
  if(demoPoll) clearInterval(demoPoll);
  demoPoll=setInterval(fetchDemoData,2000);
}
function renderDemoPositionCards(d){
  if(!d.positions||d.positions.length===0){
    return '<div style="text-align:center;color:var(--muted);padding:30px;border:1px dashed rgba(255,255,255,0.05);border-radius:10px;margin-top:12px">演示引擎监控中 · 暂无持仓</div>';
  }
  var h='<div class="pos-grid">';
  for(var i=0;i<d.positions.length;i++){
    var p=d.positions[i],ip=Number(p.pnl||0)>=0,isL=p.direction==='LONG';
    var etd=(p.entered||''); if(etd) etd=etd.slice(0,16).replace('T',' ');
    var sInt=(p.source_interval||d.cfg_interval||'15m');
    h+='<div class="pos-card '+(ip?'profit':'loss')+'">';
    h+='<div class="pc-head"><span class="pc-sym">'+p.symbol.replace('USDT','')+'</span><span class="pc-dir '+(isL?'long':'short')+'">'+(isL?'做多 LONG':'做空 SHORT')+'</span><span style="font-size:10px;color:var(--brand);background:rgba(45,212,191,0.08);padding:2px 6px;border-radius:4px;margin-left:8px">'+sInt+'</span></div>';
    if(etd) h+='<div class="pc-data"><span class="pc-label">开仓时间</span><span class="pc-val">'+etd+'</span></div>';
    h+='<div class="pc-data"><span class="pc-label">入场价</span><span class="pc-val">'+p.entry+'</span></div>';
    h+='<div class="pc-data"><span class="pc-label">当前价</span><span class="pc-val">'+Number(p.current_price||p.entry||0).toFixed(6)+'</span></div>';
    h+='<div class="pc-data"><span class="pc-label">止损价</span><span class="pc-val">'+p.sl+'</span></div>';
    h+='<div class="pc-data"><span class="pc-label">MFE</span><span class="pc-val">'+Number(p.max_favorable_r||0).toFixed(2)+'R</span></div>';
    h+=choppyLine(p.choppy_filter);
    h+=dailyPatternLine(p.daily_pattern);
    h+='<div class="pc-data"><span class="pc-label">持仓价值</span><span class="pc-val">$'+Number(p.value||0).toFixed(2)+'</span></div>';
    h+='<div class="pc-pnl-box"><div class="pc-pnl-val">'+(ip?'+':'')+Number(p.pnl||0).toFixed(2)+' USDT</div></div>';
    if(p.breakeven) h+='<div class="pc-trail-shimmer">保护止损已启动</div>';
    h+='</div>';
  }
  return h+'</div>';
}
function renderDemoTradeList(d){
  var trades=(d.recent_trades||[]).slice();
  var a=acct(d);
  var realized=a.realized;
  var open=a.open;
  var net=a.recordNet;
  var total=trades.length;
  var pageSize=10;
  var totalPages=Math.max(1,Math.ceil(total/pageSize));
  if(_demoTradePage>totalPages) _demoTradePage=totalPages;
  var curPage=Math.max(1,_demoTradePage||1);
  var start=(curPage-1)*pageSize;
  var pageTrades=trades.reverse().slice(start,start+pageSize);
  var h='<div style="margin-top:20px;background:linear-gradient(180deg,rgba(20,20,24,0.9) 0%,rgba(17,17,20,0.95) 100%);border:1px solid var(--border);border-radius:10px;padding:16px;box-shadow:var(--shadow-sm)">';
  h+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:10px;flex-wrap:wrap"><h4 style="color:var(--s-purple);margin:0;font-size:13px">最近交易</h4><div style="display:flex;gap:16px;font-size:11px;color:var(--muted);flex-wrap:wrap"><span>共<strong style="color:var(--brand)">'+(d.total_trades||total)+'</strong>笔</span><span>已实现 <strong style="color:'+(realized>=0?'var(--s-green)':'var(--s-red)')+'">'+fmtMoney(realized,true)+'</strong></span><span>持仓浮动 <strong style="color:'+(open>=0?'var(--s-green)':'var(--s-red)')+'">'+fmtMoney(open,true)+'</strong></span><span>记录净盈 <strong style="color:'+(net>=0?'var(--s-green)':'var(--s-red)')+'">'+fmtMoney(net,true)+'</strong></span><span>胜率 <strong style="color:var(--brand)">'+Number(d.win_rate||0).toFixed(1)+'%</strong></span><span>第 '+curPage+' / '+totalPages+' 页</span></div></div>';
  if(!total){
    h+='<div style="text-align:center;color:var(--muted);padding:30px;border:1px dashed rgba(255,255,255,0.04);border-radius:10px">暂无交易记录</div>';
  }else{
    h+='<div class="tl-list">';
    for(var i=0;i<pageTrades.length;i++){
      var t=pageTrades[i],pnlVal=tradePnlValue(t),tw=pnlVal>=0;
      var entry=Number(t.entry||0), exit=Number(t.exit||0), sl=Number(t.sl||0), pnlPct=Number(t.pnl_pct||0);
      var psrc=String(t.pnl_source||'estimate');
      var psrcOk=(t.exchange_pnl!==undefined && t.exchange_pnl!==null && t.exchange_pnl!=='') || psrc==='exchange_order'||psrc.indexOf('bitget_')===0;
      var psrcText=psrcOk?'交易所':'估算';
      var psrcCls=psrcOk?'tl-src ok':'tl-src';
      var targetR=Number(t.target_r||0);
      var targetText=targetR>0?('目标 '+targetR.toFixed(2)+'R'):'目标 --';
      h+='<div class="tl-item '+(tw?'tl-win':'tl-loss')+'"><div class="tl-time">'+String(t.time||'').slice(5,16).replace('T',' ')+'</div><div class="tl-body"><div class="tl-head"><span class="tl-sym">'+String(t.symbol||'').replace('USDT','')+'</span><span class="tl-dir '+(t.direction==='LONG'?'g':'r')+'">'+(t.direction==='LONG'?'多':'空')+'</span><span class="tl-reason">'+(t.reason||'')+'</span></div>';
      h+='<div class="tl-data"><span>入场 '+entry.toFixed(4)+'</span><span>出场 '+exit.toFixed(4)+'</span><span>SL '+sl.toFixed(4)+'</span><span>'+targetText+'</span><span class="'+psrcCls+'">'+psrcText+'</span>'+choppyTradeTag(t.choppy_filter)+dailyPatternTradeTag(t.daily_pattern)+'</div></div><div class="tl-pnl"><span class="tl-pnl-val '+(tw?'g':'r')+'">'+(tw?'+':'')+pnlVal.toFixed(2)+'</span><span class="tl-pnl-pct '+(tw?'g':'r')+'">'+(pnlPct>=0?'+':'')+pnlPct.toFixed(2)+'%</span></div></div>';
    }
    h+='</div>';
  }
  if(totalPages>1){
    h+='<div style="display:flex;justify-content:center;align-items:center;gap:6px;margin-top:10px;font-size:11px">';
    h+='<button onclick="setDemoTradePage(1)" '+(curPage===1?'disabled':'')+' style="background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px">首页</button>';
    h+='<button onclick="setDemoTradePage('+(curPage-1)+')" '+(curPage===1?'disabled':'')+' style="background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px">‹</button>';
    for(var pi=Math.max(1,curPage-2); pi<=Math.min(totalPages,curPage+2); pi++){
      h+='<button onclick="setDemoTradePage('+pi+')" style="background:'+(pi===curPage?'var(--brand)':'var(--card)')+';color:'+(pi===curPage?'#000':'var(--muted)')+';border:1px solid '+(pi===curPage?'var(--brand)':'var(--border)')+';border-radius:4px;padding:4px 8px;cursor:pointer;font-size:11px;min-width:24px">'+pi+'</button>';
    }
    h+='<button onclick="setDemoTradePage('+(curPage+1)+')" '+(curPage===totalPages?'disabled':'')+' style="background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px">›</button>';
    h+='<button onclick="setDemoTradePage('+totalPages+')" '+(curPage===totalPages?'disabled':'')+' style="background:var(--card);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px">末页</button>';
    h+='</div>';
  }
  return h+'</div>';
}
function fetchDemoData(){
  fetch('/demo/status?fast=1&_='+Date.now()).then(function(r){return r.json()}).then(function(d){
    if(cur!=='demo') return;
    var originalBalance=Number(d.account_balance||0);
    var uiBase=Number(d.account_initial_equity||0);
    if(!uiBase && originalBalance>0){
      uiBase=originalBalance-Number(d.account_pnl||0);
    }
    if(!uiBase) uiBase=Number(d.cfg_maxval||DEMO_CFG.cfg_maxval||0);
    var balanceEstimated=false;
    if(uiBase>0 && originalBalance<=0){
      balanceEstimated=true;
      d.account_initial_equity=uiBase;
      d.account_balance=uiBase+Number(d.net_pnl||0);
      d.account_pnl=Number(d.net_pnl||0);
      d.pnl_basis='trade_log';
    }
    if(uiBase>0) d.account_initial_equity=uiBase;
    d.demo_equity_base=uiBase;
    d.demo_equity_source=balanceEstimated?'记录估算':(d.account_equity_pnl!==null&&d.account_equity_pnl!==undefined?'交易所权益':'交易记录');
    var a=acct(d);
    var running=!!d.running;
    var posCount=(d.positions||[]).length;
    var accountPnl=a.accountNet;
    var dailyPnl=a.dailyDisplay;
    var bal=a.balance;
    var dayLabel=a.dailyBasis==='equity'?'今日权益':'今日记录';
    var uptime=Number(d.uptime||0), uh=Math.floor(uptime/3600), um=Math.floor((uptime%3600)/60);
    var statusMeta=(d.testnet?'测试网':'主网')+' · '+(d.market_type||'')+' · '+(d.entry_signal_source||'')+' · '+(d.cfg_interval||'');
    var h='<div class="trader-panel trader-panel-bg">';
    h+='<div class="t-status-bar" style="display:flex;align-items:center;gap:10px;padding:6px 16px;margin-bottom:14px">';
    h+='<span class="status-dot'+(running?' scanning':'')+'" style="width:8px;height:8px;border-radius:50%;display:inline-block"></span>';
    h+='<strong style="color:'+(running?'var(--brand)':'var(--muted)')+'">'+(running?'运行中':'已停止')+'</strong>';
    h+='<span class="t-status-uptime">'+(uh>0?uh+'h '+um+'m':um+'m')+'</span>';
    h+='<span style="color:var(--muted);font-size:12px;margin-left:auto">'+(d.status||'')+' · '+statusMeta+'</span>';
    h+='</div>';
    h+='<div class="equity-panel" id="equityPanel">';
    h+='<div class="eq-head"><div><div class="eq-title">演示引擎净值曲线 <span class="eq-sub" id="eqSummary">--</span></div></div><div class="eq-range" id="eqRange"><button onclick="setEquityRange(7,this)">1W</button><button onclick="setEquityRange(30,this)">1M</button><button onclick="setEquityRange(90,this)">3M</button><button onclick="setEquityRange(180,this)">6M</button><button onclick="setEquityRange(365,this)">1Y</button><button onclick="setEquityRange(0,this)">ALL</button></div></div>';
    h+='<div class="eq-metrics"><div class="eq-card"><div class="eq-label">账户权益</div><div class="eq-value neu" id="eqBalance">--</div></div><div class="eq-card"><div class="eq-label">权益净盈</div><div class="eq-value" id="eqAccountPnl">--</div></div><div class="eq-card"><div class="eq-label">记录净盈</div><div class="eq-value" id="eqRecordPnl">--</div></div><div class="eq-card"><div class="eq-label">最大回撤</div><div class="eq-value" id="eqDrawdown">--</div></div></div>';
    h+='<div class="eq-chart-wrap" id="equityChart"><div class="eq-empty">等待演示交易生成净值曲线</div></div>';
    h+='<div class="eq-legend"><span><i class="eq-dot"></i>账户净值</span><span><i class="eq-dot base"></i>初始权益</span></div>';
    h+='</div>';
    h+=rPerformancePanelHtml();
    h+='<div class="hud-grid hud-demo"><div class="hud-box"><div class="hud-title">配置本金</div><div class="hud-val neu" style="font-size:16px">'+(uiBase>0?'$'+uiBase.toFixed(2):'--')+'</div></div><div class="hud-box"><div class="hud-title">已实现盈亏</div><div class="hud-val '+(a.realized>=0?'g':'r')+'">'+fmtMoney(a.realized,true)+'</div></div><div class="hud-box"><div class="hud-title">持仓浮动</div><div class="hud-val '+(a.open>=0?'g':'r')+'">'+fmtMoney(a.open,true)+'</div></div><div class="hud-box"><div class="hud-title">记录净盈</div><div class="hud-val '+(a.recordNet>=0?'g':'r')+'">'+fmtMoney(a.recordNet,true)+'</div></div><div class="hud-box"><div class="hud-title">'+dayLabel+'</div><div class="hud-val '+(dailyPnl>=0?'g':'r')+'">'+fmtMoney(dailyPnl,true)+'</div></div><div class="hud-box"><div class="hud-title">权益净盈</div><div class="hud-val '+(accountPnl>=0?'g':'r')+'">'+fmtMoney(accountPnl,true)+'</div></div><div class="hud-box"><div class="hud-title">账差</div><div class="hud-val '+(a.diff>=0?'g':'r')+'">'+fmtMoney(a.diff,true)+'</div></div><div class="hud-box"><div class="hud-title">持仓 / 胜率</div><div class="hud-val neu">'+posCount+' / '+Number(d.win_rate||0).toFixed(1)+'%</div></div></div>';
    h+=renderDemoPositionCards(d);
    h+=renderDemoTradeList(d);
    h+='</div>';
    document.getElementById('main').innerHTML=h;
    renderEquityReview(d);
  });
}

// ===== INTRO WHITEPAPER =====
function renderIntro(){
  setDesc('intro');
  document.getElementById('stats').innerHTML='';
  document.getElementById('scanLabel').textContent='当前: AXIOM交易系统全景解析';

  var h = `
  <div class="intro-wrap">
    <div class="intro-hero">
      <h2 style="font-size:28px;font-weight:800;color:#fff;letter-spacing:-1px;margin-bottom:8px">
        AXIOM <span style="color:var(--brand)">QUANT</span>
      </h2>
      <h3 style="font-size:16px;color:var(--text);font-weight:600;margin-bottom:16px;letter-spacing:2px">机构级量化交易系统全景解析</h3>
      <p style="max-width:760px;margin:0 auto;font-size:13px;color:var(--muted);line-height:1.8;text-align:justify">
        在剧烈波动的加密货币永续市场中，真正决定长期结果的从来不是单次方向判断，而是能否在混沌行情里持续识别高质量动量窗口、压缩错误成本，并把有限仓位留给最有希望延展成趋势的标的。<br><br>
        <strong style="color:var(--text2)">AXIOM 交易系统</strong> 的设计初衷，是用机构级结构验证、固定风险仓位与自动化出场链路，替代人类交易中最脆弱的三件事：追在情绪末端、扛住失效仓位、以及在利润回吐时犹豫。它不是预测未来的魔法球，而是一台围绕概率优势、执行纪律与仓位周转构建的趋势狙击终端。
      </p>
    </div>

    <div class="intro-card">
      <div class="intro-title"><span class="intro-num">01</span>进场模型</div>
      <div class="intro-sub">动量窗口突破后的结构确认狙击</div>
      <div class="intro-text">市面上常见的量化脚本往往采用无脑指标交叉策略，导致在震荡市中被反复来回洗盘。AXIOM 的入场不是看到价格异动就追，而是要求市场完成一整套结构自证：压缩、释放、回归、确认、触发，缺一不可。</div>
      <ul class="intro-list">
        <li><b>动量压缩窗口：</b>系统只寻找价格、波动与趋势节奏高度收敛的区域。这里代表市场力量被压缩到临界状态，方向尚未完全释放。</li>
        <li><b>有效释放确认：</b>必须由价格完成明确的结构释放，而不是单根影线或短暂噪音。突破只代表方向线索，不代表立即追单，系统会进入二次验证阶段。</li>
        <li><b>结构回归不失效：</b>释放后的价格回归必须维持关键结构完整，不能重新跌回无效区间。市场必须证明方向仍然有效，趋势才具备延续价值。</li>
        <li><b>二次确认作为句号：</b>回归测试结束后，系统等待新的结构确认信号。它不是装饰指标，而是确认测试结束、趋势继续的执行签名。</li>
      </ul>
    </div>

    <div class="intro-card">
      <div class="intro-title"><span class="intro-num">02</span>空间过滤</div>
      <div class="intro-sub">拒绝悬空追单与瀑布后迟到入场</div>
      <div class="intro-text">真正的高质量机会不是越远越好，而是必须在可控空间内完成验证。AXIOM v1.9 引入空间偏离拦截，防止信号看似成立、实际成交价却已经远离安全区域的追涨杀跌。</div>
      <ul class="intro-list">
        <li><b>入场空间受控：</b>不同周期采用不同的空间容忍度。系统宁愿错过一部分尾部行情，也不在深坑和高台上追单。</li>
        <li><b>确认信号不悬空：</b>结构确认本身也必须靠近安全区域。若信号离原始动量窗口过远，说明行情已经脱离最佳执行区，系统直接拦截。</li>
        <li><b>候选池实时复检：</b>等待二次确认的信号会进入候选池，每轮重新读取市场数据、重新验证方向、空间与结构。旧信号不会被机械复用，只有最新结构合格才允许发起狙击。</li>
      </ul>
    </div>

    <div class="intro-card">
      <div class="intro-title"><span class="intro-num">03</span>资金管理</div>
      <div class="intro-sub">绝对物理隔离与固定风险</div>
      <div class="intro-text">保住本金是交易的第一要义。AXIOM 采用固定风险反推仓位模型，将每一笔交易的最大风险压缩到可计量、可复盘、可持续的范围内。</div>
      <ul class="intro-list">
        <li><b>分周期风险预算：</b>系统支持短、中、长周期独立风险额度。周期越大，验证时间越长，风险预算越独立。</li>
        <li><b>亏损绝对值锁定：</b>系统根据入场价与结构防线距离反推开仓数量，而不是按本金比例盲目下注。防线距离越远，仓位越小；距离过近，则自动按防滑点规则抬高风险缓冲。</li>
        <li><b>交易所精度保护：</b>所有数量向下取整，受 minQty、maxQty 与 stepSize 约束。低于交易所要求直接放弃，避免因为精度错误产生不可控订单。</li>
      </ul>
    </div>

    <div class="intro-card">
      <div class="intro-title"><span class="intro-num">04</span>出场机制</div>
      <div class="intro-sub">失速清理 + 三阶动态护城河</div>
      <div class="intro-text">利润不是算出来的，是守出来的；弱单也不是用信仰扛出来的，是按节奏和推进质量清理出来的。AXIOM 将出场拆成两套机制：先清理没有形成延续的仓位，再让真正跑出来的趋势进入三阶追踪。</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(240px, 1fr));gap:12px;margin-top:16px">
        <div class="intro-tier-card">
          <div style="color:var(--brand);font-weight:700;margin-bottom:6px">短线狙击：推进质量验证</div>
          <div style="font-size:12px;color:var(--text2);line-height:1.6">短周期信号必须在限定节奏内证明自己。若迟迟没有进入保护状态，且当前推进质量不足，即使历史曾短暂冲高，也会被判定为失速并退出。</div>
        </div>
        <div class="intro-tier-card">
          <div style="color:var(--s-yellow);font-weight:700;margin-bottom:6px">阶梯一/二：防守与抽血</div>
          <div style="font-size:12px;color:var(--text2);line-height:1.6">趋势进入优势区后，系统会自动抬高防线，并在关键阶段释放部分风险。它不依赖交易员临场情绪，而是把浮动利润逐步转化为防守结构。</div>
        </div>
        <div class="intro-tier-card">
          <div style="color:var(--s-purple);font-weight:700;margin-bottom:6px">阶梯三：终极追踪</div>
          <div style="font-size:12px;color:var(--text2);line-height:1.6">当行情进入强趋势阶段，系统会按原始触发周期匹配波动节奏，持续推进动态防线。短周期用短周期节奏，大周期用大周期节奏，防线只紧不松。</div>
        </div>
      </div>
    </div>

    <div class="intro-card">
      <div class="intro-title"><span class="intro-num">05</span>执行韧性与预期管理</div>
      <div class="intro-sub">重启恢复、止损补挂与接口异常兜底</div>
      <ul class="intro-list">
        <li><b>实盘引擎自动恢复：</b>用户开启自动交易后，运行态会持久化。服务重启时，系统会恢复有效用户的实盘引擎，并继续管理已有持仓。</li>
        <li><b>开仓状态不丢失：</b>恢复交易所持仓时，系统优先保留本地原始开仓时间、触发周期与初始防线，避免重启后错误切换管理节奏，也避免失速计数被重置。</li>
        <li><b>防线订单去重与补挂：</b>每个持仓都会记录防线订单状态，挂新防线前先取消旧单，重启后自动恢复并补挂，防止重复保护单或裸奔持仓。</li>
        <li><b>接受局部试错：</b>系统不是追求每一笔都盈利，而是通过短线弱单清理、趋势单追踪和固定风险结构，在大样本中把资金留给真正的肥尾行情。</li>
      </ul>
    </div>
  </div>`;

  document.getElementById('main').innerHTML = h;
}

function renderCryptorank(page){
  setDesc('cryptorank_'+page);
  document.getElementById('stats').innerHTML='';
  document.getElementById('scanLabel').textContent='当前: CryptoRank 中文雷达';
  var routeMap={home:'', funding:'funding', opportunities:'opportunities'};
  var url='/cryptorank/'+(routeMap[page]||'');
  document.getElementById('main').innerHTML=
    '<iframe src="'+url+'" style="width:100%;height:calc(100vh - 120px);border:1px solid rgba(148,163,184,0.14);border-radius:8px;background:#050911" allow="clipboard-write"></iframe>';
}

// ===== INIT =====
var firstMenu=document.querySelector('.menu-items');
if(firstMenu){firstMenu.classList.add('open'); firstMenu.previousElementSibling.classList.add('open');}
if(Object.values(D).every(function(v){return Array.isArray(v)?v.length===0:(!v||(Array.isArray(v.rows)?v.rows.length===0:(!Array.isArray(v.negative)||v.negative.length===0)&&(!Array.isArray(v.positive)||v.positive.length===0)))})){
  document.getElementById('scanLabel').textContent='首次使用，点击开始扫描';
}
initReflowAlertSound();
</script></body></html>"""

@app.route("/")
def index():
    data = dict(cache)
    data["reflow_1h"] = _reflow_dashboard_payload(cache.get("reflow_1h"))
    return render_template_string(HTML, data=data, demo_cfg_json=_json.dumps(demo_display_cfg))

# ═══ CryptoRank 中文雷达 ═══
_CR_DIST = _os.path.join(_BASE_DIR, 'crypot-rank-bot-main', 'dist')
_CR_API = 'http://localhost:3001'

@app.route('/cryptorank')
@app.route('/cryptorank/')
@app.route('/cryptorank/<path:filename>')
def serve_cryptorank(filename='index.html'):
    """CryptoRank SPA 静态文件 + 客户端路由回退"""
    filepath = _os.path.join(_CR_DIST, filename)
    if _os.path.isfile(filepath):
        return send_from_directory(_CR_DIST, filename)
    return send_from_directory(_CR_DIST, 'index.html')

@app.route('/api/radar')
@app.route('/api/funding')
@app.route('/api/upcoming')
@app.route('/api/airdrops')
@app.route('/api/opportunities')
@app.route('/api/brief')
@app.route('/api/health')
def proxy_cryptorank_api():
    """代理 CryptoRank Express API (端口 3001)"""
    try:
        resp = requests.get(f'{_CR_API}{request.path}', timeout=30)
        return resp.content, resp.status_code, {'Content-Type': 'application/json'}
    except requests.RequestException as e:
        return jsonify({'updatedAt': '', 'source': [], 'error': f'CryptoRank API 不可用: {str(e)}'}), 502

@app.route("/data")
def get_data():
    data = dict(cache)
    data["reflow_1h"] = _reflow_dashboard_payload(cache.get("reflow_1h"))
    return jsonify({"data":data,"time":state["time"],"status":state["text"],
                    "scanning":state["scanning"],"progress":state["progress"]})

@app.route("/scan/funding")
def do_funding():
    def apply_result(result):
        cache["funding"] = result
        return len(result["negative"]) + len(result["positive"])

    if not _start_scan_worker("扫描资金费率", lambda progress: scan_funding(50), apply_result):
        return jsonify({"scanning": True, "status": "扫描中..."})
    return jsonify({"scanning":True})

def _start_scan_worker(label, work, apply_result, apply_error=None):
    global _scan_worker, _scan_generation, _scan_worker_token
    with _scan_lock:
        if state["scanning"] or (_scan_worker is not None and _scan_worker.is_alive()):
            return False
        _scan_generation += 1
        generation = _scan_generation
        token = object()
        state.update(scanning=True, text=label, progress="", _scan_start=time.time())

        worker = None

        def is_current():
            return (
                generation == _scan_generation
                and token is _scan_worker_token
                and _scan_worker is worker
            )

        def progress(completed, total):
            with _scan_lock:
                if is_current():
                    state["progress"] = f"{completed}/{total}"

        def run():
            try:
                result = work(progress)
                with _scan_lock:
                    if is_current():
                        result_count = apply_result(result)
                        state["time"] = bj_now().strftime("%H:%M:%S")
                        state["text"] = f"完成: {result_count} 结果"
            except Exception as e:
                with _scan_lock:
                    if is_current():
                        if apply_error is not None:
                            apply_error(e)
                        state["text"] = str(e)[:80]
            finally:
                with _scan_lock:
                    if is_current():
                        state["scanning"] = False

        worker = threading.Thread(target=run, daemon=True)
        _scan_worker = worker
        _scan_worker_token = token
        try:
            worker.start()
        except Exception:
            if is_current():
                _scan_worker = None
                state["scanning"] = False
            raise
    return True

def _reflow_dashboard_payload(base=None, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    payload = (
        load_reflow_dashboard(MOMENTUM_REFLOW_HISTORY, now_ms)
        if base is None
        else dict(base)
    )
    with _reflow_automation_lock:
        automation = dict(_reflow_automation)
    with _scan_lock:
        automation.update(
            scanning=bool(state["scanning"]),
            progress=state["progress"],
        )
    try:
        settings = load_reflow_settings(MOMENTUM_REFLOW_SETTINGS)
    except (OSError, ValueError) as error:
        automation["auto_scan_enabled"] = False
        automation["last_auto_error"] = str(error)[:80]
    else:
        automation["auto_scan_enabled"] = settings["auto_scan_enabled"]
    payload["automation"] = automation
    return payload

def _run_reflow_scan(progress):
    result = scan_momentum_reflow(MOMENTUM_REFLOW_LEDGER, progress=progress)
    now_ms = int(time.time() * 1000)
    payload = merge_reflow_signals(
        MOMENTUM_REFLOW_HISTORY,
        MOMENTUM_REFLOW_LEDGER,
        result,
        now_ms,
    )
    _process_reflow_alerts(payload, now_ms)
    return payload

def _process_reflow_alerts(payload, now_ms):
    try:
        created = observe_reflow_alerts(
            MOMENTUM_REFLOW_ALERT_LEDGER,
            payload.get("rows", []),
            now_ms,
        )
    except Exception as error:
        with _reflow_alert_lock:
            _reflow_alert_status["last_error"] = _sanitize_reflow_alert_error(error)
        return []
    if created:
        _reflow_alert_wakeup.set()
    return created

def _update_reflow_automation_success(trigger, payload):
    if trigger != "auto":
        return
    scanned = max(0, int(payload.get("scanned", 0) or 0))
    errors = max(0, int(payload.get("errors", 0) or 0))
    high_failure_rate = scanned > 0 and errors >= 3 and errors * 2 >= scanned
    with _reflow_automation_lock:
        _reflow_automation["last_auto_scan_at"] = int(time.time() * 1000)
        _reflow_automation["last_auto_error"] = (
            f"扫描任务失败 {errors}/{scanned}" if high_failure_rate else ""
        )

def _update_reflow_automation_error(trigger, error):
    if trigger != "auto":
        return
    with _reflow_automation_lock:
        _reflow_automation["last_auto_error"] = str(error)[:80]

def _start_reflow_scan(trigger):
    label = "动能回流自动扫描" if trigger == "auto" else "reflow 1h 扫描中"

    def apply_result(payload):
        cache["reflow_1h"] = payload
        _update_reflow_automation_success(trigger, payload)
        return len(payload["rows"])

    return _start_scan_worker(
        label,
        _run_reflow_scan,
        apply_result,
        lambda error: _update_reflow_automation_error(trigger, error),
    )

def _new_reflow_scheduler_state(**overrides):
    scheduler_state = {
        "previous_enabled": None,
        "last_attempt_slot": 0,
        "last_skip_at": 0,
        "next_scan_at": 0,
    }
    scheduler_state.update(overrides)
    return scheduler_state

def _reflow_scheduler_step(now, settings, scheduler_state, start_scan):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    enabled = settings.get("auto_scan_enabled")
    if type(enabled) is not bool:
        raise ValueError("auto_scan_enabled must be a boolean")

    updated = dict(scheduler_state)
    if not enabled:
        updated["previous_enabled"] = False
        updated["next_scan_at"] = 0
        return updated

    now_ms = int(now.timestamp() * 1000)
    previous_enabled = updated.get("previous_enabled")
    next_scan_at = int(updated.get("next_scan_at") or 0)
    immediate = previous_enabled is not True
    due = next_scan_at > 0 and now_ms >= next_scan_at
    updated["previous_enabled"] = True

    if not immediate and not due:
        if next_scan_at == 0:
            updated["next_scan_at"] = int(
                next_reflow_scan_at(now).timestamp() * 1000
            )
        return updated

    attempt_slot = now_ms if immediate else next_scan_at
    if updated.get("last_attempt_slot") == attempt_slot:
        updated["next_scan_at"] = int(
            next_reflow_scan_at(now).timestamp() * 1000
        )
        return updated
    updated["last_attempt_slot"] = attempt_slot
    started = start_scan()
    if not started:
        updated["last_skip_at"] = attempt_slot
    updated["next_scan_at"] = int(
        next_reflow_scan_at(now).timestamp() * 1000
    )
    return updated

def _reflow_scheduler_loop():
    scheduler_state = _new_reflow_scheduler_state()
    settings_error = ""
    try:
        while not _reflow_scheduler_stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                settings = load_reflow_settings(MOMENTUM_REFLOW_SETTINGS)
            except Exception as error:
                settings_error = str(error)[:80]
                scheduler_state = _reflow_scheduler_step(
                    now,
                    {"auto_scan_enabled": False},
                    scheduler_state,
                    lambda: False,
                )
                with _reflow_automation_lock:
                    _reflow_automation["last_auto_error"] = settings_error
                    _reflow_automation["last_skip_at"] = scheduler_state["last_skip_at"]
                    _reflow_automation["next_scan_at"] = 0
                    _reflow_automation["auto_scan_enabled"] = False
            else:
                with _reflow_automation_lock:
                    if (
                        settings_error
                        and _reflow_automation["last_auto_error"] == settings_error
                    ):
                        _reflow_automation["last_auto_error"] = ""
                settings_error = ""
                scheduler_state = _reflow_scheduler_step(
                    now,
                    settings,
                    scheduler_state,
                    lambda: _start_reflow_scan("auto"),
                )
                with _reflow_automation_lock:
                    _reflow_automation["last_skip_at"] = scheduler_state["last_skip_at"]
                    _reflow_automation["next_scan_at"] = scheduler_state["next_scan_at"]
                    _reflow_automation["auto_scan_enabled"] = settings[
                        "auto_scan_enabled"
                    ]
            _reflow_scheduler_stop.wait(1)
    finally:
        with _reflow_automation_lock:
            _reflow_automation["running"] = False

def _start_reflow_scheduler():
    global _reflow_scheduler_thread
    with _reflow_scheduler_lock:
        if (
            _reflow_scheduler_thread is not None
            and _reflow_scheduler_thread.is_alive()
        ):
            return False
        _reflow_scheduler_stop.clear()
        worker = threading.Thread(target=_reflow_scheduler_loop, daemon=True)
        _reflow_scheduler_thread = worker
        with _reflow_automation_lock:
            _reflow_automation["running"] = True
        try:
            worker.start()
        except Exception:
            _reflow_scheduler_thread = None
            with _reflow_automation_lock:
                _reflow_automation["running"] = False
            raise
    return True

def _stop_reflow_scheduler_for_tests():
    global _reflow_scheduler_thread
    _reflow_scheduler_stop.set()
    with _reflow_scheduler_lock:
        worker = _reflow_scheduler_thread
    if worker is not None and worker.is_alive():
        worker.join(timeout=2)
    with _reflow_scheduler_lock:
        if _reflow_scheduler_thread is not worker:
            return
        still_running = worker is not None and worker.is_alive()
        if not still_running:
            _reflow_scheduler_thread = None
        with _reflow_automation_lock:
            _reflow_automation["running"] = still_running

def _reflow_alert_worker_loop():
    with _reflow_alert_lock:
        _reflow_alert_status["running"] = True
    try:
        while not _reflow_alert_stop.is_set():
            try:
                result = deliver_due_wechat(
                    MOMENTUM_REFLOW_ALERT_SETTINGS,
                    MOMENTUM_REFLOW_ALERT_LEDGER,
                    int(time.time() * 1000),
                )
                with _reflow_alert_lock:
                    _reflow_alert_status["last_error"] = _sanitize_reflow_alert_error(
                        result.get("error", "")
                    )
                    if result.get("status") == "delivered":
                        _reflow_alert_status["last_delivery_at"] = int(
                            time.time() * 1000
                        )
            except Exception as error:
                with _reflow_alert_lock:
                    _reflow_alert_status["last_error"] = _sanitize_reflow_alert_error(error)
            _reflow_alert_wakeup.wait(30)
            _reflow_alert_wakeup.clear()
    finally:
        with _reflow_alert_lock:
            _reflow_alert_status["running"] = False

def _start_reflow_alert_worker():
    global _reflow_alert_thread
    with _reflow_alert_lock:
        if _reflow_alert_thread is not None and _reflow_alert_thread.is_alive():
            return False
        _reflow_alert_stop.clear()
        worker = threading.Thread(target=_reflow_alert_worker_loop, daemon=True)
        _reflow_alert_thread = worker
        try:
            worker.start()
        except Exception:
            _reflow_alert_thread = None
            raise
    return True

def _stop_reflow_alert_worker_for_tests():
    global _reflow_alert_thread
    _reflow_alert_stop.set()
    _reflow_alert_wakeup.set()
    with _reflow_alert_lock:
        worker = _reflow_alert_thread
    if worker is not None and worker.is_alive():
        worker.join(timeout=2)
    with _reflow_alert_lock:
        if _reflow_alert_thread is worker and (
            worker is None or not worker.is_alive()
        ):
            _reflow_alert_thread = None

@app.route("/api/reflow/automation/status")
def reflow_automation_status():
    with _reflow_automation_lock:
        automation = dict(_reflow_automation)
    try:
        settings = load_reflow_settings(MOMENTUM_REFLOW_SETTINGS)
    except (OSError, ValueError) as error:
        return jsonify({
            **automation,
            "auto_scan_enabled": False,
            "error": str(error)[:80],
        }), 503
    return jsonify({
        **automation,
        "auto_scan_enabled": settings["auto_scan_enabled"],
    })

@app.route("/api/reflow/alerts")
def reflow_alert_events():
    if not session.get("user_id"):
        return jsonify({"error": "未登录"}), 401
    raw = request.args.get("after", "0")
    try:
        after_id = int(raw)
        if after_id < 0:
            raise ValueError("negative cursor")
    except (TypeError, ValueError):
        return jsonify({"error": "after must be a nonnegative integer"}), 400
    try:
        payload = read_public_alerts(MOMENTUM_REFLOW_ALERT_LEDGER, after_id)
    except ValueError:
        return jsonify({"error": "警报暂不可用"}), 503
    return jsonify(payload)

@app.route("/scan/<mode>/<interval>")
def do_scan(mode, interval):
    if mode == "reflow" and interval != "1h":
        return jsonify({"error":"reflow only supports 1h"}), 400
    if mode == "reflow":
        if not _start_reflow_scan("manual"):
            return jsonify({"scanning":True,"status":"扫描中..."})
        return jsonify({"scanning":True})

    key = f"{mode}_{interval}"
    def work(progress):
        if mode == "diverge": return scan_divergence(interval)
        if mode == "breakout": return scan_breakout(interval)
        return scan(interval, mode, 5)

    def apply_result(result):
        cache[key] = result
        return len(result)

    if not _start_scan_worker(f"{mode} {interval} 扫描中", work, apply_result):
        return jsonify({"scanning":True,"status":"扫描中..."})
    return jsonify({"scanning":True})

@app.route("/rj_indicator")
def rj_indicator_api():
    """RJ/BBKD 图表指标接口: 只读分析, 不执行交易。"""
    symbol = (request.args.get("symbol") or "BTCUSDT").upper().strip()
    interval = (request.args.get("interval") or "1h").strip()
    if not symbol.endswith("USDT") or not symbol.replace("USDT", "").isalnum():
        return jsonify({"ok": False, "error": "交易对格式无效"})
    if interval not in {"15m", "1h", "4h", "1d"}:
        return jsonify({"ok": False, "error": "周期仅支持 15m/1h/4h/1d"})
    df = fetch_klines(symbol, interval, 260, exchange="binance")
    if df is None or len(df) < 80:
        return jsonify({"ok": False, "error": "K线数据不足或获取失败"})
    try:
        data = compute_rj_bbkd(df, RJParams())
        data["symbol"] = symbol
        data["interval"] = interval
        data["time"] = bj_now().strftime("%H:%M:%S")
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]})

@app.route("/trader/status")
def trader_status():
    bot = _current_bot()
    if request.args.get("fast") == "1":
        return jsonify(bot.get_fast_summary())
    return jsonify(bot.get_summary())

@app.route("/demo/status")
def demo_status():
    bot = bot_manager.get_bot(0)
    if request.args.get("fast") == "1":
        return jsonify(bot.get_fast_summary())
    return jsonify(bot.get_summary())

# 管理后台 API: 演示引擎控制
@app.route("/api/admin/demo/status")
def admin_demo_status():
    bot = bot_manager.get_bot(0)
    cfg = bot_manager.get_user_config(0) if 0 in bot_manager._configs else TradeConfig()
    return jsonify({"running": bot.running, "status": bot.status_text,
        "positions": len(bot.positions), "total_pnl": sum(t.get("pnl",0) for t in bot.trade_log),
        "cfg_api": bool(cfg.api_key or cfg.testnet_api_key),
        "mode": getattr(cfg, "mode", ""),
        "testnet": getattr(cfg, "testnet", False),
        "client_ready": bool(getattr(bot, "client", None)),
        "entry_signal_source": getattr(cfg, "entry_signal_source", "structure"),
        "scan_interval": getattr(cfg, "scan_interval", ""),
        "position_list": [
            {"symbol": p.symbol, "direction": p.direction,
             "entry": round(p.entry_price,6), "current_price": round(getattr(p,'current_price',p.entry_price),6),
             "sl": round(p.current_sl,6), "qty": round(p.quantity,6),
             "value": round(p.quantity * float(getattr(p, 'current_price', p.entry_price) or p.entry_price), 2), "pnl": round(p.pnl, 2),
             "breakeven": p.breakeven_triggered,
             "entered": p.entry_time.isoformat() if p.entry_time else ""}
            for p in bot.positions
        ]})

@app.route("/api/admin/demo/start")
def admin_demo_start():
    bot_manager.start_user_bot(0)
    return jsonify({"ok": True})

@app.route("/api/admin/demo/stop")
def admin_demo_stop():
    bot_manager.stop_user_bot(0)
    return jsonify({"ok": True})

@app.route("/api/admin/demo/config", methods=["GET","POST"])
def admin_demo_config():
    if request.method == "POST":
        data = request.get_json() or {}
        bot_manager.save_user_config(0, data)
        return jsonify({"ok": True})
    cfg = bot_manager.get_user_config(0)
    d = {k:v for k,v in cfg.__dict__.items() if not k.startswith("_")}
    for k in ('api_key','api_secret','testnet_api_key','testnet_api_secret',
              'bitget_api_key','bitget_api_secret','bitget_api_pass'):
        if d.get(k):
            s = str(d.get(k))
            d[k] = "****" + s[-4:] if len(s) > 4 else "****"
    return jsonify(d)

@app.route("/api/admin/demo/close/<symbol>")
def admin_demo_close_one(symbol):
    bot = bot_manager.get_bot(0)
    ok = bot.close_position(symbol)
    return jsonify({"ok": ok, "symbol": symbol, "msg": "已平仓" if ok else ("未找到持仓 "+symbol+" 或API未配置")})

@app.route("/api/admin/demo/close_all")
def admin_demo_close_all():
    bot = bot_manager.get_bot(0)
    n = bot.close_all_positions()
    return jsonify({"ok": True, "count": n})

@app.route("/trader/start")
def trader_start():
    uid = session.get('user_id')
    if uid:
        valid, _, days = accounts.check_license_valid(uid)
        if not valid:
            return jsonify({"running": False, "status": f"许可已过期({days}天), 请联系管理员续期"})
    b = _current_bot()
    if not b.running:
        bot_manager.start_user_bot(session.get('user_id'))
    b = _current_bot()
    return jsonify({"running": b.running, "status": b.status_text})

@app.route("/trader/stop")
def trader_stop():
    bot_manager.stop_user_bot(session.get('user_id'))
    return jsonify({"running": False, "status": "已停止"})

@app.route("/trader/close_all")
def trader_close_all():
    n = _current_bot().close_all_positions()
    return jsonify({"ok": True, "msg": f"已平仓 {n} 笔"})

@app.route("/trader/close/<symbol>")
def trader_close_one(symbol):
    ok = _current_bot().close_position(symbol)
    return jsonify({"ok": ok, "msg": f"{symbol} 已平仓" if ok else f"{symbol} 平仓失败"})

@app.route("/trader/config", methods=["GET", "POST"])
def trader_config():
    uid = session.get('user_id')
    if request.method == "POST":
        data = request.get_json() or {}
        if uid:
            bot_manager.save_user_config(uid, data)
        else:
            # 未登录: 直接写主配置文件
            cfg = TradeConfig()
            for k, v in data.items():
                if hasattr(cfg, k): setattr(cfg, k, v)
            cfg.save(_os.path.join(_BASE_DIR, 'trade_config.json'))
        return jsonify({"ok": True})
    # GET: 返回用户配置
    if uid:
        cfg = bot_manager.get_user_config(uid)
        d = {k: v for k, v in cfg.__dict__.items() if not k.startswith('_')}
    else:
        from trader import TradeConfig
        d = {k: v for k, v in TradeConfig().__dict__.items() if not k.startswith('_')}
    for k in ('api_key','api_secret','testnet_api_key','testnet_api_secret',
              'bitget_api_key','bitget_api_secret','bitget_api_pass'):
        if d.get(k):
            s = str(d.get(k))
            d[k] = "****" + s[-4:] if len(s) > 4 else "****"
    return jsonify(d)

@app.route("/trader/auth", methods=["POST"])
def trader_auth():
    data = request.get_json() or {}
    pwd = data.get("password", "")
    if _current_bot().cfg.web_password and pwd == _current_bot().cfg.web_password:
        return jsonify({"ok": True})
    elif not _current_bot().cfg.web_password:
        return jsonify({"ok": True, "msg": "no password set"})
    return jsonify({"ok": False, "msg": "密码错误"}), 403

# ===== BTC 大盘监控 API =====
BTC_INTERVALS = ["1h", "4h", "1d", "1w"]

@app.route("/btc_monitor")
def btc_monitor():
    """返回 BTC 四个周期的均线/拐点/趋势综合诊断 (始终使用主网数据)"""
    df_all = {}
    try:
        for interval in BTC_INTERVALS:
            df = fetch_klines("BTCUSDT", interval, 200, exchange="binance")
            if df is None or len(df) < 100:
                df_all[interval] = {"error": "数据不足"}
                continue
            closes = df["c"]
            highs = df["h"]
            lows = df["l"]
            n = len(closes)
            price = closes.iloc[-1]

            # EMA + MA
            emas = {p: ema(closes, p).iloc[-1] for p in EMA_LENS}
            mas = {p: closes.rolling(p).mean().iloc[-1] for p in MA_LENS}
            all_mas = list(emas.values()) + list(mas.values())
            band_hi, band_lo = max(all_mas), min(all_mas)
            spread_pct = (band_hi - band_lo) / price * 100

            # 趋势结构
            e20, e60, e120 = emas[20], emas[60], emas[120]
            if e20 > e60 > e120:
                alignment = "多头排列"
                ali_class = "bullish"
            elif e20 < e60 < e120:
                alignment = "空头排列"
                ali_class = "bearish"
            else:
                alignment = "交叉震荡"
                ali_class = "neutral"

            # 收敛/发散
            tight_threshold = {"1h": 6, "4h": 10, "1d": 8, "1w": 12}.get(interval, 8)
            if spread_pct <= tight_threshold * 0.5:
                squeeze_state = "极限压缩"
                sq_class = "tight"
            elif spread_pct <= tight_threshold:
                squeeze_state = "波动收敛"
                sq_class = "tight"
            elif spread_pct <= tight_threshold * 2:
                squeeze_state = "趋势扩张"
                sq_class = "spread"
            else:
                squeeze_state = "趋势扩张"
                sq_class = "trending"

            # 价格位置
            if price > band_hi:
                position = "结构上方"
                pos_class = "above"
            elif price < band_lo:
                position = "结构下方"
                pos_class = "below"
            else:
                inside_ratio = (price - band_lo) / (band_hi - band_lo) * 100 if band_hi > band_lo else 50
                position = "区间内部"
                pos_class = "inside"

            # 突破与方向
            if price > band_hi:
                breakout_dir = "向上突破"
                brk_class = "up"
            elif price < band_lo:
                breakout_dir = "向下突破"
                brk_class = "down"
            else:
                breakout_dir = "无突破"
                brk_class = "none"

            # 拐点检测(最近8根K线)
            fractal = "--"
            frac_class = ""
            for i in range(n - 3, max(n - 9, 3), -1):
                if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                    if i + 2 < n and (closes[i+1] > highs[i] or closes[i+2] > highs[i]):
                        fractal = "底部拐点"
                        frac_class = "frac-bull"
                        break
                if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                    if i + 2 < n and (closes[i+1] < lows[i] or closes[i+2] < lows[i]):
                        fractal = "顶部拐点"
                        frac_class = "frac-bear"
                        break

            # 综合判定
            score = 0
            if alignment == "多头排列": score += 30
            elif alignment == "空头排列": score -= 30
            if "拐点" in fractal:
                score += 25 if "底" in fractal else -25
            if breakout_dir == "向上突破": score += 25
            elif breakout_dir == "向下突破": score -= 25
            if squeeze_state in ("极限压缩", "波动收敛"): score += 15

            if score >= 60:
                overall, ov_class = "<span class='ic-dot ic-strong'></span>动量突破", "hot"
            elif score >= 30:
                overall, ov_class = "<span class='ic-dot ic-mid'></span>强势", "strong"
            elif score <= -60:
                overall, ov_class = "<span class='ic-dot ic-bear'></span>空头爆发", "cold"
            elif score <= -30:
                overall, ov_class = "<span class='ic-dot ic-cold'></span>弱势", "weak"
            else:
                overall, ov_class = "<span class='ic-dot ic-weak'></span>盘整观望", "neutral"

            df_all[interval] = {
                "price": round(price, 2),
                "spread_pct": round(spread_pct, 2),
                "band_hi": round(band_hi, 2),
                "band_lo": round(band_lo, 2),
                "ema20": round(e20, 2),
                "ema60": round(e60, 2),
                "ema120": round(e120, 2),
                "alignment": alignment, "ali_class": ali_class,
                "squeeze_state": squeeze_state, "sq_class": sq_class,
                "position": position, "pos_class": pos_class,
                "breakout_dir": breakout_dir, "brk_class": brk_class,
                "fractal": fractal, "frac_class": frac_class,
                "overall": overall, "ov_class": ov_class,
            }
        return jsonify({"ok": True, "data": df_all, "time": bj_now().strftime("%H:%M:%S")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ═══════════════════════════════════════════
# 多用户认证与账户管理
# ═══════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not __import__('flask').session.get('user_id'):
            return __import__('flask').jsonify({"error":"未登录"}), 401
        return f(*a, **kw)
    return wrap

@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json() or {}
    user = accounts.authenticate(data.get("username",""), data.get("password",""))
    if not user:
        return jsonify({"ok": False, "msg": "用户名或密码错误"})
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    valid, plan, days = accounts.check_license_valid(user['id'])
    return jsonify({"ok": True, "user": user['username'], "role": user['role'],
                    "license_valid": valid, "plan": plan, "days_left": days})

@app.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json() or {}
    ok, msg = accounts.register_user(data.get("username",""), data.get("password",""),
                                      data.get("email",""))
    if ok:
        user = accounts.authenticate(data.get("username",""), data.get("password",""))
        if user: accounts.issue_license(user['id'], 'trial', 7)
        return jsonify({"ok": True, "msg": msg})
    return jsonify({"ok": False, "msg": msg}), 400

@app.route("/auth/forgot", methods=["POST"])
def auth_forgot():
    data = request.get_json() or {}
    success, code, msg = accounts.request_password_reset(data.get("email",""))
    return jsonify({"ok": success, "code": code, "msg": msg})

@app.route("/auth/reset", methods=["POST"])
def auth_reset():
    data = request.get_json() or {}
    ok, msg = accounts.reset_password_with_code(data.get("email",""), data.get("code",""),
                                                  data.get("password",""))
    return jsonify({"ok": ok, "msg": msg})

@app.route("/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/auth/status")
def auth_status():
    uid = session.get('user_id')
    if not uid: return jsonify({"logged_in": False})
    user = accounts.get_user(uid)
    valid, plan, days = accounts.check_license_valid(uid)
    bal = accounts.get_fuel_balance(uid)
    return jsonify({"logged_in": True, "user": user['username'], "role": user['role'],
                    "license_valid": valid, "plan": plan, "days_left": days,
                    "fuel_balance": bal})

# ═══════════════════════════════════════════
# 燃料费路由 (多用户)
# ═══════════════════════════════════════════
@app.route("/fuel/topup", methods=["POST"])
def fuel_topup():
    uid = session.get('user_id')
    if not uid: return jsonify({"ok": False, "msg": "未登录"}), 401
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    if amount <= 0: return jsonify({"ok": False, "msg": "金额无效"}), 400
    bal = accounts.fuel_topup(uid, amount)
    return jsonify({"ok": True, "balance": bal})

@app.route("/fuel/history")
def fuel_history():
    uid = session.get('user_id')
    if not uid: return jsonify([]), 401
    return jsonify(accounts.get_fuel_history(uid, 30))

# ═══════════════════════════════════════════
# 管理后台路由 (admin only)
# ═══════════════════════════════════════════
@app.route("/admin/stats")
def admin_stats():
    if session.get('role') != 'admin': return jsonify({"error": "无权限"}), 403
    return jsonify(accounts.admin_get_stats())

@app.route("/admin/users")
def admin_users():
    if session.get('role') != 'admin': return jsonify([]), 403
    users = accounts.admin_get_all_users()
    return jsonify([{"id": u['id'], "username": u['username'], "role": u['role'],
                     "active": bool(u['active']), "created_at": u['created_at']} for u in users])

@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
def admin_toggle_user(uid):
    if session.get('role') != 'admin': return jsonify({"error": "无权限"}), 403
    data = request.get_json() or {}
    active = data.get("active", True)
    accounts.admin_set_user_active(uid, active)
    return jsonify({"ok": True})

@app.route("/admin/license", methods=["POST"])
def admin_issue_license():
    if session.get('role') != 'admin': return jsonify({"error": "无权限"}), 403
    data = request.get_json() or {}
    key = accounts.issue_license(int(data.get("user_id",0)), data.get("plan","standard"), int(data.get("days",30)))
    return jsonify({"ok": True, "key": key})

# ═══════════════════════════════════════════
# 交易数据燃料集成
# ═══════════════════════════════════════════
def _current_fuel_balance():
    uid = session.get('user_id')
    return accounts.get_fuel_balance(uid) if uid else 0

def _deduct_fuel_commission(pnl: float):
    uid = session.get('user_id')
    if uid and pnl > 0 and _current_bot().cfg.commission_rate > 0 and _current_bot().cfg.fuel_enabled:
        fee = pnl * _current_bot().cfg.commission_rate / 100.0
        accounts.fuel_deduct(uid, fee, f"盈利分成 {_current_bot().cfg.commission_rate:.0f}%")
        return fee
    return 0

if __name__=="__main__":
    print("\n  >>> Axiom Quant v1.0 <<<")
    print("  http://127.0.0.1:5000\n")
    _start_reflow_scheduler()
    _start_reflow_alert_worker()
    app.run(host="0.0.0.0",port=5000,debug=False,threaded=True)
