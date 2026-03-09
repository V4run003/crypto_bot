# ---------------------------------------------------------------------------
# Bot configuration — update API_KEY / API_SECRET with your Bybit credentials
# ---------------------------------------------------------------------------

API_KEY    = "Vi0bmWXcZHM0HGXhkd"
API_SECRET = "xaD8mBKUpVrst7ZaivYkcSuG9gv626mB4UC4"
TESTNET    = False  # True = Bybit Testnet (testnet.bybit.com keys)
DEMO       = True   # True = Bybit Demo Trading (bybit.com demo keys)

TIMEFRAME          = 5      # Primary candle interval in minutes
RISK_PER_TRADE     = 25     # USD risked per trade
MAX_DAILY_LOSS     = 150    # Bot stops after this cumulative daily loss (USD)
MAX_TRADES_PER_DAY = 6      # Hard cap on daily trade count
RR                 = 1.5    # Take-profit risk:reward ratio

ATR_PERIOD    = 14   # ATR indicator period
ATR_MA_PERIOD = 20   # Rolling-average period for volatility filter

SWING_LOOKBACK = 10     # Candles back to locate swing high/low for SL
SL_BUFFER_PCT  = 0.001  # 0.1 % buffer beyond the swing point

ADX_THRESHOLD = 18   # Minimum ADX to confirm a trending market (lower = more trades)

# Williams %R Trend Exhaustion
# Consecutive candles that must sit in the extreme zone before the hook fires.
WR_EXHAUSTION_LOOKBACK = 2   # 2 = more signals, 3 = fewer but stricter

# Leverage & margin safety
MAX_LEVERAGE   = 5     # Applied via API before every order
MAX_MARGIN_PCT = 0.20  # Max fraction of wallet committed as margin per trade

# Prop-firm compliance
TRADE_COOLDOWN_SECS     = 600   # 10-minute cooldown after any trade closes
MIN_TRADE_DURATION_SECS = 60    # Minimum seconds a trade must stay open

CANDLE_BUFFER_SECS = 3      # Extra seconds to wait after candle close
SCAN_INTERVAL      = 300    # Fallback scan interval (seconds) if timing fails

# Dynamic market discovery
TOP_SYMBOLS_COUNT  = 30            # Top N symbols by 24h volume
VOLUME_FILTER_USD  = 50_000_000   # Minimum 24h USD turnover
SYMBOL_REFRESH_SECS = 1800        # Refresh symbol list every 30 minutes

# ── Backtest mode ──────────────────────────────────────────────────────────
# Set BACKTEST = True then run `python bot.py` (or `python backtest.py`).
# Live trading is completely disabled while BACKTEST is True.
BACKTEST         = True               # True → run backtest on startup
BACKTEST_SYMBOLS = ["ETHUSDT"]        # Symbols to test (tested in sequence)
BACKTEST_DAYS    = 90                  # Calendar days of history to fetch
BACKTEST_QUIET   = False               # True → suppress per-trade output lines

# Fallback symbol list used if the dynamic fetch fails
FALLBACK_COINS = [
    "BTCUSDT",  "ETHUSDT",  "SOLUSDT",  "BNBUSDT",  "XRPUSDT",
    "LINKUSDT", "AVAXUSDT", "ADAUSDT",  "DOTUSDT",  "ATOMUSDT",
    "NEARUSDT", "APTUSDT",  "DOGEUSDT",
]