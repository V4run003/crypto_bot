# ---------------------------------------------------------------------------
# Bot configuration — update API_KEY / API_SECRET with your Bybit credentials
# ---------------------------------------------------------------------------

API_KEY    = "Vi0bmWXcZHM0HGXhkd"
API_SECRET = "xaD8mBKUpVrst7ZaivYkcSuG9gv626mB4UC4"
TESTNET    = False  # True = Bybit Testnet (testnet.bybit.com keys)
DEMO       = True   # True = Bybit Demo Trading (bybit.com demo keys)

TIMEFRAME          = 5      # Primary candle interval in minutes
RISK_PER_TRADE     = 35     # USD risked per trade
MAX_DAILY_LOSS     = 100    # Bot stops after this cumulative daily loss (USD)
MAX_TRADES_PER_DAY = 6      # Hard cap on daily trade count
RR                 = 1.5    # Take-profit risk:reward ratio

ATR_PERIOD    = 14   # ATR indicator period
ATR_MA_PERIOD = 20   # Rolling-average period for volatility filter

SWING_LOOKBACK = 10     # Candles back to locate swing high/low for SL
SL_BUFFER_PCT  = 0.001  # 0.1 % buffer beyond the swing point

ADX_THRESHOLD = 18   # Minimum ADX to confirm a trending market (lower = more trades)

# ── Experimental filters ──────────────────────────────────────────────────────
# TIME_FILTER: +$963 vs +$945 baseline, PF 1.30, lower MaxDD — KEEP ON
# ADX_RISING_FILTER: PF 1.36 but 58% fewer trades, less total PnL — OFF
# EMA50_PULLBACK_FILTER: PF collapses to 1.01, MaxDD spikes — OFF
ADX_RISING_FILTER        = False # ADX must be rising for momentum confirmation
TIME_FILTER              = True  # Only trade during liquid hours
TIME_FILTER_START        = 8     # UTC hour, inclusive (08:00)
TIME_FILTER_END          = 20    # UTC hour, exclusive  (20:00)
EMA50_PULLBACK_FILTER    = False # Price within N × ATR of EMA50 (kills edge, leave off)
EMA50_PULLBACK_ATR_MULT  = 1.0   # Distance threshold multiplier

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
BACKTEST         = True              # True → run backtest on startup
BACKTEST_SYMBOLS = ["BTCUSDT"]        # Symbols to test (tested in sequence)
BACKTEST_DAYS    = 90                  # Calendar days of history to fetch
BACKTEST_QUIET   = False               # True → suppress per-trade output lines

# Coins approved for live trading — only symbols with backtest profit factor > 1.1
# Run `python backtest.py SYMBOL 90` before adding any new symbol here.
APPROVED_COINS = [
    "BTCUSDT",   # PF 1.27  ✓
    "BNBUSDT",   # PF 1.14  ✓
    "ETHUSDT",   # PF 1.07  (borderline — monitor)
]

# Fallback if dynamic volume fetch fails — same approved list
FALLBACK_COINS = APPROVED_COINS