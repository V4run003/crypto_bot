# ---------------------------------------------------------------------------
# COPY THIS FILE TO config.py AND FILL IN YOUR VALUES
# config.py is gitignored and will never be committed.
# ---------------------------------------------------------------------------

API_KEY    = "YOUR_BYBIT_API_KEY"
API_SECRET = "YOUR_BYBIT_API_SECRET"
TESTNET    = False  # True = Bybit Testnet
DEMO       = True   # True = Bybit Demo Trading (CFT evaluation uses demo=True)

TIMEFRAME          = 5
RISK_PER_TRADE     = 25
MAX_DAILY_LOSS     = 180
MAX_TRADES_PER_DAY = 7
RR                 = 1.5

ACCOUNT_SIZE          = 5000
PROFIT_TARGET         = 500
KNOWN_PEAK_BALANCE    = 5000
MAX_DAILY_LOSS_PCT    = 0.04
TRAILING_DD_PCT       = 0.06
TRAILING_DD_BUFFER    = 50

ATR_PERIOD    = 14
ATR_MA_PERIOD = 20
SWING_LOOKBACK = 10
SL_BUFFER_PCT  = 0.001
ADX_THRESHOLD = 18

HTF_FILTER               = True
ADX_RISING_FILTER        = False
ADX_RISING2_FILTER       = False
CANDLE_CONFIRM_FILTER    = False
VOLUME_CONFIRM_FILTER    = False
VOLUME_CONFIRM_MULT      = 1.4
RESISTANCE_FILTER        = False
RESISTANCE_ATR_MULT      = 0.5
TIME_FILTER              = False
TIME_FILTER_START        = 8
TIME_FILTER_END          = 22
EMA50_PULLBACK_FILTER    = False
EMA50_PULLBACK_ATR_MULT  = 1.0

WR_EXHAUSTION_LOOKBACK = 1

MAX_LEVERAGE   = 5
MAX_MARGIN_PCT = 0.20

TRADE_COOLDOWN_SECS     = 600
MIN_TRADE_DURATION_SECS = 60
ADX_FADE_ENABLED        = False
BE_ENABLED              = False

PARTIAL_TP_ENABLED = False
PARTIAL_TP1_R      = 1.0
PARTIAL_TP2_R      = 2.0

# ── 4H EMA200 higher-timeframe filter (V3) ───────────────────────────────────
# Apply to all coins except those in the excluded list.
# Test per-coin with htf_4h_compare.py before adding/removing from excluded list.
HTF_4H_FILTER          = True
HTF_4H_FILTER_EXCLUDED = []   # coins where 4H filter hurts edge — add per coin

# ── Regime filter (daily EMA) — off by default, 1H EMA200 already covers this ─
REGIME_FILTER          = False
REGIME_EMA_PERIOD      = 20
REGIME_NEUTRAL_PCT     = 0.005
REGIME_FILTER_EXCLUDED = []

# ── Signal drought Telegram alert ────────────────────────────────────────────
# Fires once if no trade opens for this many hours during the active window.
# Resets automatically when the next trade fires.  Set 0 to disable.
DROUGHT_ALERT_HOURS    = 6

# ── BTC 4H regime Telegram alert ─────────────────────────────────────────────
# Fires when BTC 4H price crosses the EMA200 (bull ↔ bear flip).
# Alert only — zero impact on trading behaviour.
REGIME_ALERT_ENABLED   = True

USE_LIMIT_ENTRY          = True
LIMIT_ORDER_TIMEOUT_SECS = 30

CANDLE_BUFFER_SECS = 3

TELEGRAM_ENABLED   = False
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_TELEGRAM_CHAT_ID"

SCAN_INTERVAL      = 300
TOP_SYMBOLS_COUNT  = 30
VOLUME_FILTER_USD  = 5_000_000
SYMBOL_REFRESH_SECS = 1800

BACKTEST         = False
BACKTEST_SYMBOLS = ["BTCUSDT"]
BACKTEST_DAYS    = 90
BACKTEST_QUIET   = False

APPROVED_COINS = [
    "PAXGUSDT",
    "ZECUSDT",
    "SUIUSDT",
    "PIPPINUSDT",
    "RIVERUSDT",
    "BCHUSDT",
    "BTCUSDT",
    "ATOMUSDT",
]
FALLBACK_COINS = APPROVED_COINS
RSI_STRATEGY = True
RSI_APPROVED_COINS = [
    "ZECUSDT",
    "SUIUSDT",
    "BTCUSDT",
]

RSI_PERIOD          = 14
RSI_LONG_ZONE_LOW   = 28
RSI_LONG_ZONE_HIGH  = 45
RSI_SHORT_ZONE_LOW  = 55
RSI_SHORT_ZONE_HIGH = 72

# ── Candle data cache ─────────────────────────────────────────────────────────
# Backtest scripts read from disk on warm runs — no API calls, no rate-limit waits.
# Cache key: symbol + interval + days (e.g. ZECUSDT_5m_180d.parquet)
# Freshness: file mtime — files older than CACHE_MAX_AGE_DAYS are re-fetched.
# Run `python cache_manager.py --refresh` to pre-warm before a batch of backtests.
USE_CACHE          = True   # False = always fetch live (bypasses cache entirely)
CACHE_DIR          = "cache"  # Relative to working directory
CACHE_MAX_AGE_DAYS = 7      # Files older than this are considered stale and re-fetched
