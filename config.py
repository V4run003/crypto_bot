# ---------------------------------------------------------------------------
# Bot configuration — update API_KEY / API_SECRET with your Bybit credentials
# ---------------------------------------------------------------------------

API_KEY    = "Vi0bmWXcZHM0HGXhkd"
API_SECRET = "xaD8mBKUpVrst7ZaivYkcSuG9gv626mB4UC4"
TESTNET    = False  # True = Bybit Testnet (testnet.bybit.com keys)
DEMO       = True   # True = Bybit Demo Trading (bybit.com demo keys)

TIMEFRAME          = 5      # Candle interval in minutes
RISK_PER_TRADE     = 25     # USD risked per trade
MAX_DAILY_LOSS     = 100    # Bot stops trading after this cumulative loss (USD)
MAX_TRADES_PER_DAY = 4      # Hard cap on daily trade count
RR                 = 1.5    # Take-profit risk:reward ratio

ATR_PERIOD    = 14   # ATR indicator period
ATR_MA_PERIOD = 20   # Rolling-average period used for volatility filter

SWING_LOOKBACK = 10     # Candles back to locate swing high/low for stop loss
SL_BUFFER_PCT  = 0.001  # 0.1 % buffer placed beyond the swing point

# Williams %R Trend Exhaustion
# Number of consecutive candles that must sit inside the extreme zone (-80/-20)
# before a hook back out of that zone is treated as a valid exhaustion signal.
# Higher = fewer but higher-quality signals.
WR_EXHAUSTION_LOOKBACK = 3

# Leverage & margin safety
# MAX_LEVERAGE  : leverage applied to every symbol before entry.
#                 5× is conservative and well within typical prop-firm limits.
# MAX_MARGIN_PCT: hard cap on the fraction of wallet used as margin per trade.
#                 20 % means a $10 k account never commits more than $2 k margin.
MAX_LEVERAGE   = 5
MAX_MARGIN_PCT = 0.20

SCAN_INTERVAL     = 60  # Seconds between full market scans
TOP_SYMBOLS_COUNT = 30  # Number of top-volume USDT perps to scan

# Used as a fallback if the dynamic symbol fetch fails
FALLBACK_COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT",  "BNBUSDT",  "XRPUSDT",
    "LINKUSDT", "AVAXUSDT", "ADAUSDT", "DOTUSDT",  "ATOMUSDT",
    "NEARUSDT", "APTUSDT",
]