# ---------------------------------------------------------------------------
# Bot configuration — update API_KEY / API_SECRET with your Bybit credentials
# ---------------------------------------------------------------------------

API_KEY    = "Vi0bmWXcZHM0HGXhkd"
API_SECRET = "xaD8mBKUpVrst7ZaivYkcSuG9gv626mB4UC4"
TESTNET    = False  # True = Bybit Testnet (testnet.bybit.com keys)
DEMO       = True   # True = Bybit Demo Trading (bybit.com demo keys)

TIMEFRAME          = 5      # Primary candle interval in minutes
RISK_PER_TRADE     = 25     # USD risked per trade (0.5% of $5k — safe vs 6% trailing DD)
MAX_DAILY_LOSS     = 180    # Bot stops at $180 daily loss (CFT limit is $200 = 4%)
MAX_TRADES_PER_DAY = 15     # Hard cap on daily trade count (13 coins active)
RR                 = 1.5    # Take-profit risk:reward ratio

# ── CFT Prop-firm challenge rules ($5,000 account) ───────────────────────────
ACCOUNT_SIZE          = 5000   # Starting balance
PROFIT_TARGET         = 571    # Remaining to reach $5,500 target ($4,929 current → $5,500)
KNOWN_PEAK_BALANCE    = 5000   # Highest balance ever seen — seeds trailing DD floor on restart
MAX_DAILY_LOSS_PCT    = 0.04   # 4%  → $200 hard daily loss limit
TRAILING_DD_PCT       = 0.06   # 6%  → $300 trailing drawdown from peak
TRAILING_DD_BUFFER    = 50     # Stop trading $50 before hitting the trailing DD wall

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

# ── Order execution ───────────────────────────────────────────────────────────
# Post-only limit orders pay maker fee (0.02%) vs taker (0.06%) — 3× cheaper.
# If the limit is not filled within LIMIT_ORDER_TIMEOUT_SECS, it is cancelled
# and a market order is sent as fallback.
USE_LIMIT_ENTRY          = True  # Try post-only limit before falling back to market
LIMIT_ORDER_TIMEOUT_SECS = 30    # Seconds to wait for limit fill

CANDLE_BUFFER_SECS = 3      # Extra seconds to wait after candle close
SCAN_INTERVAL      = 300    # Fallback scan interval (seconds) if timing fails

# Dynamic market discovery
TOP_SYMBOLS_COUNT  = 30            # Top N symbols by 24h volume
VOLUME_FILTER_USD  = 50_000_000   # Minimum 24h USD turnover
SYMBOL_REFRESH_SECS = 1800        # Refresh symbol list every 30 minutes

# ── Backtest mode ──────────────────────────────────────────────────────────
# Set BACKTEST = True then run `python bot.py` (or `python backtest.py`).
# Live trading is completely disabled while BACKTEST is True.
BACKTEST         = False             # True → run backtest on startup
BACKTEST_SYMBOLS = ["BTCUSDT"]        # Symbols to test (tested in sequence)
BACKTEST_DAYS    = 90                  # Calendar days of history to fetch
BACKTEST_QUIET   = False               # True → suppress per-trade output lines

# Coins approved for live trading — only symbols with backtest profit factor > 1.1
# Run `python backtest.py SYMBOL 90` before adding any new symbol here.
APPROVED_COINS = [
    # Tier 1 — PF >= 1.40 (highest confidence)
    "GRTUSDT",   # PF 1.97  ✓  WR 56.8%
    "PAXGUSDT",  # PF 1.94  ✓  WR 56.3%  MaxDD $140
    "TIAUSDT",   # PF 1.61  ✓  WR 51.8%
    "ONDOUSDT",  # PF 1.55  ✓  WR 50.8%
    "FILUSDT",   # PF 1.44  ✓  MaxDD $140
    "BTCUSDT",   # PF 1.41  ✓
    "SUIUSDT",   # PF 1.41  ✓  MaxDD $140
    "ARBUSDT",   # PF 1.41  ✓
    "TONUSDT",   # PF 1.40  ✓
    # Tier 2 — PF 1.25–1.39
    "CRVUSDT",   # PF 1.32  ✓  MaxDD $140
    "BNBUSDT",   # PF 1.27  ✓
    "LDOUSDT",   # PF 1.28  ✓
    "ETHUSDT",   # PF 1.20  ✓
]

# Fallback if dynamic volume fetch fails — same approved list
FALLBACK_COINS = APPROVED_COINS