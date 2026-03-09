# crypto_bot

Automated futures trading bot for Bybit (Demo / Live). Designed for CFT prop-firm evaluation on a $5,000 account.

## Strategy

- **Signal:** Williams %R exhaustion hook (consecutive candles in overbought/oversold zone, then reversal)
- **Trend filter:** EMA 200 on 5m and 1H — only trade in the direction of both timeframes
- **Momentum filter:** ADX ≥ 18 (configurable)
- **Volatility filter:** ATR-based stop placement from nearest swing high/low
- **Time filter:** UTC 08:00–20:00 only (liquid hours)
- **RR:** 1.5 (configurable)

## Risk management

| Parameter | Value |
|---|---|
| Risk per trade | $25 (0.5% of $5k) |
| Max daily loss | $180 (stops before CFT's $200 / 4% limit) |
| Max trades/day | 7 |
| Trailing drawdown buffer | Stop $50 before the 6% trailing DD wall |
| Trade cooldown | 10 min after any close |
| Min trade duration | 60 s (prop-firm compliance) |

## Project structure

```
bot.py               — Main loop: NTP sync, candle timing, daily report
config.py            — All settings (gitignored — copy from config.example.py)
config.example.py    — Safe template to commit; fill in secrets locally
exchange.py          — Bybit API wrapper (pybit V5): orders, positions, PnL
strategy.py          — Signal generation (WR, EMA, ADX, ATR)
scanner.py           — Symbol discovery and trade orchestration
risk.py              — Daily loss tracking, trailing DD, trade gate
position_manager.py  — Open position monitoring, SL/TP, ADX fade exit
telegram_bot.py      — Background Telegram notification sender
trade.py             — Trade dataclass
logger.py            — Logging setup
backtest.py          — Offline backtest against Bybit OHLCV data
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

```bash
python backtest.py BTCUSDT 90     # 90 days of BTCUSDT
python backtest.py ETHUSDT 180    # 180 days of ETHUSDT
```

Only add a symbol to `APPROVED_COINS` in `config.py` if its backtest profit factor is ≥ 1.1.

## Telegram notifications

The bot sends alerts for:
- Bot started / stopped
- Trade opened (entry, SL, TP, risk $, RR)
- Trade closed (exit price, PnL, RR achieved, duration)
- Daily report (UTC midnight)
- Unhandled errors (with truncated traceback)

## Security notes

- `config.py` is gitignored — never commit it
- Use a dedicated API key with **Unified Trading (read+write) only** — no withdrawals, no sub-account access
- On CFT accounts there are typically two keys: one for the dashboard (do not touch) and one for the bot
