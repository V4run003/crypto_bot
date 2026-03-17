# crypto_bot

Automated futures trading bot for Bybit (Demo / Live). Designed for CFT prop-firm evaluation on a **$10,000 2-phase account**.

## Strategy

The bot runs a **hybrid strategy** — each coin gets the best signal source for its character:

### WR Exhaustion (all approved coins)

1. **Exhaustion zone** — `WR_EXHAUSTION_LOOKBACK` (= 1) consecutive candle(s) must sit inside the Williams %R extreme zone (overbought > −20 for shorts, oversold < −80 for longs)
2. **Hook candle** — the next closed candle exits the zone (the reversal "hook")
3. **SL/TP** — SL at swing high/low of last 10 candles ± 0.1% buffer; TP = `SL distance × 1.5R`
4. **5m EMA 200 trend** — longs only above EMA 200, shorts only below (never disabled)
5. **1H EMA 200 HTF filter** — same alignment requirement on the 1-hour chart
6. **4H EMA 200 filter** — applied to 6 coins; excluded for SUIUSDT and PIPPINUSDT (over-filters their edge)
7. **ADX ≥ 18** — directional momentum required; 18 confirmed optimal in sweep [14–26]
8. **ATR > ATR_MA(20)** — skips flat/compressed markets

### RSI Pullback layer (BTCUSDT, ZECUSDT, SUIUSDT)

Checked **first** on these three coins; WR is used as fallback if RSI is silent.

1. **RSI zone** — 14-period RSI in 28–45 (longs) or 55–72 (shorts). Wider preset confirmed best across all three coins.
2. **RSI slope** — RSI must be turning in the trade direction (rising for longs, falling for shorts)
3. All EMA / HTF / ADX / ATR guards apply identically

### PAXG long-only override

Shorts are disabled on PAXGUSDT (`WR_LONG_ONLY_COINS`). Gold-backed asset with structural upward bias — short WR 27.8%, PF 0.58 in bear regime.

## Risk management (10k 2-phase account)

| Parameter | Value | Notes |
|---|---|---|
| Risk per trade | $50 (0.5%) | Scales with $10k account |
| Max daily loss (bot) | $150 | $350 inside the prop-firm's $500/day limit |
| Max overall loss | $1,000 (10%) | Fixed floor at $9,000 — **not trailing** |
| DD buffer | $100 | Bot pauses $100 before the $9,000 floor |
| Unrealized loss cap | $100 (2× risk) | Force-close on SL gap/slippage |
| Phase 1 profit target | $800 (8%) | Reach $10,800 to pass Phase 1 |
| Max trades/day | 14 | Caps only 2.8% of trading days |
| Trade cooldown | 10 min | Post-close cooldown |
| Min trade duration | 60 s | Prop-firm compliance |
| RR | 1.5 | Swept 1.0–3.5; 1.5 optimal |

## Project structure

```
bot.py               — Main loop: NTP sync, candle timing, window alerts, daily report
config.py            — All settings (gitignored — copy from config.example.py)
config.example.py    — Safe template to commit; fill in secrets locally
exchange.py          — Bybit V5 API: orders, positions, PnL, candles, balances
strategy.py          — WR Exhaustion signal (EMA, ADX, ATR, 4H filter, HTF)
strategy_rsi.py      — RSI Pullback signal (same shared filters)
scanner.py           — Symbol loop, hybrid signal orchestration
risk.py              — Daily loss, fixed DD floor, trade gate
position_manager.py  — Trade lifecycle, trade log (trade_log.csv)
trade.py             — Order placement (limit with market fallback)
telegram_bot.py      — Background Telegram notification sender
logger.py            — Logging setup
data_cache.py        — Parquet candle cache (avoid API rate limits on backtests)
cache_manager.py     — CLI cache management tool
show_trades.py       — View persistent trade history from trade_log.csv
backtest.py          — WR-only single-coin backtest
backtest_rsi.py      — RSI-only single-coin backtest
backtest_hybrid.py   — Primary backtest: mirrors scanner.py exactly
backtest_sma.py      — SMA pullback backtest (validated, not yet in live scanner)
backtest_portfolio.py — Multi-coin portfolio sim with shared slot + daily limits
```

## Setup

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/V4run003/crypto_bot.git
cd crypto_bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install pybit pandas ta requests ntplib pyarrow
```

### 2. Configure

```bash
cp config.example.py config.py
```

Edit `config.py` and fill in:

| Key | Where to get it |
|---|---|
| `API_KEY` / `API_SECRET` | Bybit → Account → API Management → Create key (Unified Trading, read+write) |
| `DEMO` | `True` for Bybit Demo / CFT eval, `False` for live funded account |
| `TELEGRAM_BOT_TOKEN` | `@BotFather` → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` |

### 3. Pre-warm the candle cache

```bash
python cache_manager.py --refresh
```

This fetches 180 days × 8 coins × 4 timeframes (32 parquet files) once. All backtests then run from disk with no API calls.

### 4. Run locally

```bash
python bot.py
```

## Backtesting

Always use `backtest_hybrid.py` — it mirrors `scanner.py` exactly (RSI-first on approved coins, WR fallback, 4H filter).

```bash
# Hybrid (use for all validation)
python backtest_hybrid.py BTCUSDT 180
python backtest_hybrid.py ZECUSDT 90 --quiet

# Portfolio (8 coins, shared slot)
python backtest_portfolio.py 180
python backtest_portfolio.py 180 --no-sma

# Single-strategy
python backtest.py SOLUSDT 180
python backtest_rsi.py BTCUSDT 180
python backtest_sma.py ZECUSDT 180
```

## Cache management

```bash
python cache_manager.py --refresh             # refresh all 8 coins × 4 TFs
python cache_manager.py --refresh ZECUSDT    # single coin
python cache_manager.py --status             # show all cached files + age
python cache_manager.py --clear              # delete all cache files
python cache_manager.py --clear BTCUSDT      # delete one coin's cache
```

Cache lives in `cache/` (e.g. `ZECUSDT_5m_180d.parquet`). Files older than `CACHE_MAX_AGE_DAYS` (7) are re-fetched automatically.

## Trade history

Every closed trade is appended to `trade_log.csv` on the VPS. The file survives bot restarts.

```bash
python show_trades.py        # last 20 trades
python show_trades.py 50     # last 50 trades
python show_trades.py all    # full history
```

Columns: `datetime, symbol, side, strategy, entry_type, entry, exit, pnl, duration_mins, close_reason, version`

## Deploying to a server (DigitalOcean / any Linux VPS)

```bash
git clone https://github.com/V4run003/crypto_bot.git
cd crypto_bot
python3 -m venv venv
source venv/bin/activate
pip install pybit pandas ta requests ntplib pyarrow
nano config.py   # paste your filled-in config here
```

### systemd service (auto-restart on crash / reboot)

Create `/etc/systemd/system/cryptobot.service`:

```ini
[Unit]
Description=Crypto Trading Bot
After=network.target

[Service]
WorkingDirectory=/root/crypto_bot
ExecStart=/root/crypto_bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable cryptobot
systemctl start cryptobot
systemctl status cryptobot
```

### Useful server commands

```bash
journalctl -u cryptobot -f                        # live log stream
journalctl -u cryptobot --since "1 hour ago"      # recent logs
journalctl -u cryptobot -n 200                    # last 200 lines
systemctl restart cryptobot                        # apply config changes
systemctl stop cryptobot                           # clean shutdown
python show_trades.py 50                           # last 50 trades
```

## Telegram notifications

- Bot started / stopped
- Trading window opened / closed
- **Trade opened** — strategy (WR/RSI), entry type (limit/market), wait time if market fallback, entry, SL, TP, risk $, balance
- **Trade closed** — exit, PnL, RR achieved, duration, close reason
- Daily report — trades, W/L, daily PnL, balance, DD left, CFT progress bar
- Risk limit hit (daily loss or overall DD wall)
- Signal drought alert (no signal for 6h inside active window)
- Unhandled errors (truncated traceback)

## Security notes

- `config.py` is gitignored — never commit it
- Use a dedicated API key with **Unified Trading (read+write) only** — no withdrawals
- On CFT accounts there are typically two keys: one for the dashboard and one for the bot


## Strategy

The bot runs a **hybrid strategy** — each coin gets the best signal source for its character:

### WR Exhaustion (all approved coins)

1. **Exhaustion zone** — on the 5m chart, `WR_EXHAUSTION_LOOKBACK` (= 1) consecutive candles must sit inside the Williams %R extreme zone (overbought > −20 for shorts, oversold < −80 for longs)
2. **Hook candle** — the next candle must close back outside the zone (the reversal "hook")
3. **ATR stop** — SL is placed at the swing high/low of the last 5 candles plus one ATR buffer; TP is `SL distance × RR (1.5)`
4. **5m EMA 200 trend** — longs only when price is above EMA 200, shorts only below (dead-cat bounce filter — never disabled)
5. **1H EMA 200 HTF filter** — same rule on the 1-hour chart (`HTF_FILTER = True`)
6. **ADX ≥ 18** — trade only when the market has directional momentum; tested vs [14–26], 18 confirmed optimal (highest net PnL + DD within prop-firm limit)
7. **Time window** — 08:00–22:00 UTC only (liquid session); bot notifies on window open and close

### RSI Pullback layer (BTCUSDT, ZECUSDT, SUIUSDT)

For these three coins the RSI Pullback signal is checked **first**; the WR signal is used as fallback only if RSI is silent on that candle.

1. **RSI zone** — 14-period RSI must be inside the entry zone: longs 28–45, shorts 55–72 (wider preset confirmed best across all three coins vs six alternatives)
2. **RSI slope** — RSI must be turning in the trade direction (rising for longs, falling for shorts) to confirm the pullback is ending
3. **Same EMA 200 / HTF / ADX / ATR / time guards** as WR apply identically

### All filters tested and rejected (documented in `config.py`)

Candle confirmation, volume filter (1.05×–2.0×), resistance proximity, ADX rising (1- and 2-candle), RSI-2/3 extreme zones, partial TP (1R+1.5R and 1R+2.0R), routing by ADX value. All degraded net PnL or killed trade count without offsetting gains.

## Risk management

| Parameter | Value |
|---|---|
| Risk per trade | $25 (0.5 % of $5k) |
| Max daily loss | $180 (stops before CFT's $200 / 4 % limit) |
| Max trades / day | 8 |
| Trailing drawdown buffer | Stop $50 before the 6 % trailing DD wall ($300) |
| Trade cooldown | 10 min after any close |
| Min trade duration | 60 s (prop-firm compliance) |
| RR | 1.5 (swept 1.0–3.0; 1.5 optimal — higher RR breaches $300 DD limit) |

## Project structure
```
bot.py               — Main loop: NTP sync, candle timing, window notifications, daily report
config.py            — All settings (gitignored — copy from config.example.py)
config.example.py    — Safe template to commit; fill in secrets locally
exchange.py          — Bybit API wrapper (pybit V5): orders, positions, PnL
strategy.py          — WR Exhaustion signal generation (EMA, ADX, ATR)
strategy_rsi.py      — RSI Pullback signal generation
scanner.py           — Symbol loop and hybrid signal orchestration
risk.py              — Daily loss tracking, trailing DD, trade gate
position_manager.py  — Open position monitoring, SL/TP, ADX fade exit
telegram_bot.py      — Background Telegram notification sender
trade.py             — Trade dataclass
logger.py            — Logging setup
backtest.py          — Offline backtest: WR Exhaustion only
backtest_rsi.py      — Offline backtest: RSI Pullback only
backtest_hybrid.py   — Offline backtest: Hybrid (mirrors scanner.py exactly — use this)
```

## Setup

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/V4run003/crypto_bot.git
cd crypto_bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install pybit pandas ta requests ntplib
```

### 2. Configure

```bash
cp config.example.py config.py
```

Edit `config.py` and fill in:

| Key | Where to get it |
|---|---|
| `API_KEY` / `API_SECRET` | Bybit → Account → API Management → Create key (Unified Trading, read+write) |
| `DEMO` | `True` for Bybit Demo / CFT eval, `False` for live funded account |
| `TELEGRAM_BOT_TOKEN` | `@BotFather` → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` |

### 3. Run locally

```bash
python bot.py
```

## Deploying to a server (DigitalOcean / any Linux VPS)

```bash
# On the server
git clone https://github.com/V4run003/crypto_bot.git
cd crypto_bot
python3 -m venv venv
source venv/bin/activate
pip install pybit pandas ta requests ntplib

# Create config (never commit this)
nano config.py   # paste your filled-in config here
```

### systemd service (auto-restart on crash / reboot)

Create `/etc/systemd/system/cryptobot.service`:

```ini
[Unit]
Description=Crypto Trading Bot
After=network.target

[Service]
WorkingDirectory=/root/crypto_bot
ExecStart=/root/crypto_bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable cryptobot
systemctl start cryptobot
systemctl status cryptobot
```

### Useful commands

```bash
journalctl -u cryptobot -f                        # live log stream
journalctl -u cryptobot --since "1 hour ago"      # recent logs
systemctl restart cryptobot                        # apply config changes
systemctl stop cryptobot                           # clean shutdown (sends Telegram alert)
```

## Backtesting

Always use `backtest_hybrid.py` — it mirrors `scanner.py` exactly (RSI-first on approved coins, WR fallback). The other two scripts are for isolated strategy debugging.

```bash
# Hybrid (use this for real validation)
python backtest_hybrid.py BTCUSDT 180          # BTC, 180 days
python backtest_hybrid.py ZECUSDT 90           # ZEC, 90 days
python backtest_hybrid.py ETHUSDT 180 --quiet  # suppress per-trade output

# WR Exhaustion only
python backtest.py SOLUSDT 180
python backtest.py BTCUSDT 90 --quiet

# RSI Pullback only
python backtest_rsi.py BTCUSDT 180
python backtest_rsi.py ZECUSDT 90 --quiet
```

Defaults when omitted: symbol = `BTCUSDT`, days = `90`.

Only add a symbol to `APPROVED_COINS` in `config.py` if hybrid backtest profit factor ≥ 1.2 and MaxDD ≤ $300.

## Telegram notifications

The bot sends alerts for:
- Bot started / stopped
- Trading window opened (08:00 UTC) / closed (22:00 UTC)
- Trade opened (entry, SL, TP, risk $, RR)
- Trade closed (exit price, PnL, RR achieved, duration)
- Daily report (sent at window close — 22:00 UTC — when all trades for the day are done)
- Risk limit hit (daily loss or trailing DD wall)
- Unhandled errors (with truncated traceback)

## Security notes

- `config.py` is gitignored — never commit it
- Use a dedicated API key with **Unified Trading (read+write) only** — no withdrawals, no sub-account access
- On CFT accounts there are typically two keys: one for the dashboard (do not touch) and one for the bot
