"""
MACD 背离策略 — 配置文件
"""
# === 交易对 & 周期 ===
SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"

# === 策略参数 (15m 最优) ===
MACD_FAST = 13
MACD_SLOW = 34
MACD_SIG = 9
MIN_TROUGHS = 2         # 2=高频 3=高胜率
MIN_PEAK_RATIO = 0.12   # 波峰最小差异
FRACTAL_LOOKBACK = 15   # 分型K0回溯
ARMED_TIMEOUT = 60      # 背离有效期(K线数)

# === 风控 (趋势跟踪止盈) ===
ATR_LEN = 13            # ATR周期
SL_ATR_MULT = 1.0       # 初始SL: 信号K线±ATR倍数
TRAIL_MULT = 1.5        # 跟踪止盈ATR间距
BREAKEVEN_R = 2.5       # 保本触发(×R)

# === DD 阶梯仓位 ===
POS_FULL = 200   # DD<20% 满仓(等值)
POS_HALF = 100   # DD20-30% 半仓
POS_MIN = 50     # DD>30% 底仓

# === 风控限制 ===
MAX_POSITION_USDT = 5000    # 单笔最大仓位(USDT)
MIN_POSITION_USDT = 50      # 单笔最小仓位
MAX_DAILY_TRADES = 20       # 每日最大交易数
MAX_CONSECUTIVE_LOSS = 5    # 连续止损后暂停

# === 通知 ===
TELEGRAM_TOKEN = ""         # Bot Token (留空=只打印)
TELEGRAM_CHAT_ID = ""       # Chat ID

# === Binance API (留空=只发信号不交易) ===
BINANCE_API_KEY = ""
BINANCE_API_SECRET = ""
TRADE_MODE = "signal"       # "signal"=只发信号 "paper"=模拟 "live"=实盘

INITIAL_CAPITAL = 1000  # 初始资金(模拟)

# === 数据缓存 ===
MAX_BARS = 5000             # 本地缓存K线数
