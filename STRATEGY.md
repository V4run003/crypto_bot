# Strategy & Backtest Reference

*Last updated: March 2026 — CFT 1-phase $5,000 challenge, Bybit Demo*

---

## 1. What the bot is doing

The bot trades **Bybit USDT perpetual futures** on 8 approved coins using a **hybrid exhaustion strategy** on 5-minute candles.

It looks for **overextended moves** — price that has been pushed too far in one direction by emotional participants — and fades the extreme when momentum shows the first sign of reversal. Two variants of that reversal signal are used:

- **Williams %R Exhaustion** — used on all 8 coins
- **RSI Pullback** — used first on ZEC, SUI, BTC; WR used as fallback if RSI is silent

Both signal types share the same filters, SL/TP logic and risk rules. The only difference is the entry trigger condition.

---

## 2. Exact entry conditions

### Shared pre-conditions (must ALL pass)

| Check | Rule | Notes |
|---|---|---|
| 5m EMA 200 | Long: price > EMA200, Short: price < EMA200 | Dead-cat bounce filter — never disabled |
| 1H EMA 200 (HTF) | Same rule on the 1-hour chart | `HTF_FILTER = True` |
| ADX ≥ 18 | Market must have directional momentum | Swept [14–26]; 18 is the sweet spot |
| ATR > ATR_MA(20) | Market must not be flat/compressed | Cuts signals in ranging chop |
| Time window | 08:00–22:00 UTC only | `TIME_FILTER = True` — liquid session filter |

### WR Exhaustion signal

1. `WR_EXHAUSTION_LOOKBACK = 1` consecutive candle(s) sit deep in the extreme zone
   - **Long**: WR < −80 (selling exhaustion zone)
   - **Short**: WR > −20 (buying exhaustion zone)
2. The next closed candle **exits the zone** (the "hook"):
   - **Long**: WR crosses back above −80
   - **Short**: WR crosses back below −20
3. All shared filters above must pass

### RSI Pullback signal (ZEC, SUI, BTC only — tried first)

1. 5m RSI is inside the pullback zone:
   - **Long**: 28 ≤ RSI ≤ 45 (pullback into demand after uptrend)
   - **Short**: 55 ≤ RSI ≤ 72 (bounce into supply after downtrend)
2. RSI is **turning** in the trade direction (rising for longs, falling for shorts) — confirms pullback ending, not continuing
3. All shared filters above must pass

### SL / TP calculation

- **SL**: swing high/low of the last 10 candles ± 0.1% buffer
- **TP**: `entry + (SL distance × 1.5)` — fixed 1.5R take-profit
- Position size: $25 risked per trade at the calculated SL distance

---

## 3. What has been tested and rejected

All testing was done on 180 days of data unless noted. Results are on BTC unless stated.

### Filters that hurt

| Filter | Result | Decision |
|---|---|---|
| ADX rising (1 candle) | −63% trades, PF flat | ❌ Rejected |
| ADX rising (2 candles) | −69% trades, PF −0.03 | ❌ Rejected |
| EMA50 pullback gate | PF 1.28 → 1.01, DD spikes | ❌ Rejected |
| Candle-break confirm | PF 1.28 → 1.07, DD $287 → $625 | ❌ Rejected |
| Volume confirm (1.05×–2.0×) | Best: PF −0.11 at 1.2×; every threshold hurts | ❌ Rejected |
| Resistance proximity filter | Best: PF +0.02 (noise) | ❌ Rejected |
| StochRSI(14,3,3) gate | BTC −$250; cuts 20–37% trades with no gain | ❌ Rejected |
| Dual WR-14+WR-112 confluence | Cuts 45–50% trades; works on daily TF, not 5m | ❌ Rejected |
| Partial TP 1R+1.5R | WR +8pp but net PnL −$725 | ❌ Rejected |
| Breakeven (BE) stop | 331 BEs cut winners; PF 1.31 → 1.11, net −$4,312 | ❌ Rejected |
| ADX fade early exit | Cuts 32% of trades; PF 1.31 → 1.18, net −$2,726 | ❌ Rejected |
| Daily regime filter (EMA20) | PF flat, MaxDD −15% but net −$575; HTF already handles it | ❌ Rejected |

### Parameters confirmed optimal

| Parameter | Value | Notes |
|---|---|---|
| ADX threshold | 18 | Optimal in [14,16,18,20,22,24,26] sweep |
| RR | 1.5 | Optimal in 1.0–3.5 sweep (higher RR → DD breach) |
| WR lookback | 1 candle | More signals, same PF vs 2-candle |
| RSI zones | 28–45 / 55–72 | Best in 7-preset sweep across BTC/ZEC/SUI |
| ATR period | 14 | — |
| Swing lookback | 10 candles | — |

---

## 4. Approved coin portfolio

8 coins, validated on 180 days of history. Sorted by portfolio PF (best first). The portfolio sim is more conservative than standalone — coins compete for the single open slot and shared daily cap.

| Coin | Mode | Portfolio PF | Portfolio Net | Portfolio MaxDD | Standalone PF | Notes |
|---|---|---|---|---|---|---|
| PAXGUSDT | WR only | 1.35 | $925 | $300 | 1.48 | Gold-pegged; low corr. with crypto |
| ZECUSDT | Hybrid | 1.38 | $1,350 | $350 | 1.45 | Best RSI coin |
| RIVERUSDT | WR only | 1.26 | $488 | $288 | 1.40 | New coin; only ~120 days of daily history |
| SUIUSDT | Hybrid | 1.27 | $688 | $300 | 1.25 | — |
| PIPPINUSDT | WR only | 1.35 | $762 | $175 | 1.19 | Low DD |
| BCHUSDT | WR only | 1.27 | $500 | $450 | 1.16 | Higher DD; keep monitoring |
| BTCUSDT | Hybrid | 1.21 | $438 | $312 | 1.13 | Core market |
| ATOMUSDT | WR only | 1.19 | $275 | $225 | — | ⚠ PF 0.74 in Dec'24–Jun'25 window (period-sensitive) |

**Portfolio combined (Sep 2025 → Mar 2026, 180d):**
- Net PnL: **$5,437**
- Profit factor: **1.30**
- Max drawdown: **$500**
- Win rate: **46.4%**
- Total trades: **1,355**
- Max consecutive losses: **7**
- Average trades/day: **7.5**
- Days bot would stop (daily loss > $100): **4 / 180 (2.2%)**

### Coins rejected from the portfolio

Rejected because they became **slot-stealers** — they took trade slots away from better coins and dragged portfolio PF below 1.25:

ETHUSDT (PF 0.85), WIFUSDT (PF 0.88), 1000PEPEUSDT (PF 0.95), XAUTUSDT (PF 0.99), SAHARAUSDT (PF 0.95), TIAUSDT (PF 0.78), POWERUSDT (live slippage −$37 on SL hit), ARBUSDT, GRTUSDT, INJUSDT, BNBUSDT, DOTUSDT, CRVUSDT, SOLUSDT, LINKUSDT, AVAXUSDT, TRUMPUSDT (DD too high), SEIUSDT, XLMUSDT, TAOUSDT, FARTCOINUSDT, PENGUUSDT, FLOWUSDT, ENAUSDT, ARCUSDT, UNIUSDT, ASTERUSDT, NAORISUSDT, HYPEUSDT, LTCUSDT, XAGUSDT/XAUUSDT (no signals).

---

## 5. Risk management

All rules serve the CFT 1-phase challenge: grow $5,000 → $5,500, with a 4% max daily loss and a 6% trailing drawdown from the account peak.

| Rule | Setting | Why |
|---|---|---|
| Risk per trade | $25 | 10-loss streak = $250 — stays inside the $300 trailing DD budget |
| Max daily loss | $100 | 3 bad days = $300 = CFT trailing DD limit |
| Max trades/day | 14 | Cap=14 blocked only 4/180 days (2.2%) — negligible restriction |
| Trailing DD buffer | $50 | Stop trading $50 before the 6% wall — avoids last-candle breach |
| Trade cooldown | 10 min | After any trade close — prevents revenge entries |
| Min trade duration | 60 s | Prop-firm compliance |
| Max leverage | 5× | Set per-symbol via API before every order |

### Daily limits logic

1. `reset_if_new_day()` runs at every scan — resets daily PnL counter at UTC midnight
2. If `daily_loss ≤ −$100` → `can_trade()` returns False → no new entries
3. Peak balance tracked in `risk.py`; trailing DD floor = peak − $300 − $50 buffer
4. If current balance ≤ DD floor → bot stops and sends Telegram alert

---

## 6. Live trading status (as of March 12, 2026)

- **Account**: Bybit Demo Trading, `API_KEY = F4ZD88D7...`
- **Balance**: ~$4,880 (peak $5,049)
- **Trailing DD floor**: $5,049 − $300 = $4,749 (bot stops at $4,799 with buffer)
- **Runway**: ~$81 before auto-stop
- **Profit target remaining**: ~$620 to reach $5,500
- **VPS**: DigitalOcean `138.68.81.86`, running as `cryptobot.service` (systemd)
- **Branch**: `V2-branch` on GitHub

### Live trading analysis (Mar 10–12)

13 trades recorded in the V2 period. 2 TP wins, 5 SL losses, ~4 manual closes near break-even, 2 open context.

**Root causes of underperformance (not strategy failure):**
1. Stale VPS config still had POWERUSDT in APPROVED_COINS after it was removed locally — resulted in 3 POWER trades with slippage losses
2. Bear market regime across most coins — WR signals primarily fire longs (price above EMA200), which fail when the whole market is trending down
3. RIVER over-firing: fired 5 of 13 trades (38%) when other coins weren't signalling in the bear regime — bear market periods with few signals cause coin concentration

**Statistical context**: probability of ≤1 win in 5 trades at 46% WR = 24% — within normal variance, not a strategy problem.

---

## 7. Project file map

### Core bot (live trading)

| File | Role |
|---|---|
| `bot.py` | Main loop: NTP sync, candle timing, window notifications, daily report |
| `scanner.py` | Symbol loop, hybrid signal orchestration, regime filter gate |
| `strategy.py` | WR exhaustion signal generation + `get_regime()` |
| `strategy_rsi.py` | RSI pullback signal generation |
| `exchange.py` | Bybit API wrapper (pybit V5): orders, positions, balances, candles |
| `risk.py` | Daily loss tracking, trailing DD, `can_trade()` gate |
| `position_manager.py` | Open position monitoring, SL/TP management |
| `market_scanner.py` | Dynamic top-N symbol discovery by 24h USD volume |
| `trade.py` | Trade dataclass |
| `telegram_bot.py` | Background Telegram notification sender |
| `logger.py` | Logging setup |
| `config.py` | All settings — gitignored; copy from `config.example.py` |
| `config.example.py` | Safe public template (no secrets) |

### Backtest scripts

| File | Role |
|---|---|
| `backtest_hybrid.py` | **Primary** — mirrors scanner.py exactly; use this for all validation |
| `backtest_portfolio.py` | Multi-coin portfolio sim — one shared open slot, shared daily limits |
| `backtest.py` | WR-only single-coin backtest (legacy) |
| `backtest_rsi.py` | RSI-only single-coin backtest (legacy) |

### Research & comparison scripts

| File | What it tested |
|---|---|
| `adx_sweep.py` | ADX threshold sweep [14–26] |
| `adx_rising_compare.py` | ADX rising 1/2-candle filter |
| `rr_sweep.py` | RR ratio sweep 1.0–3.5 |
| `rsi_compare.py` | RSI vs WR head-to-head |
| `rsi_zone_sweep.py` | RSI zone preset sweep (7 presets) |
| `rsi2_compare.py` | RSI-2/3 extreme zones test |
| `volume_compare.py` | Volume confirmation filter sweep |
| `resistance_compare.py` | Resistance proximity filter sweep |
| `dual_wr_compare.py` | Dual WR-14+WR-112 confluence test |
| `stochrsi_compare.py` | StochRSI(14,3,3) gate test |
| `partial_tp_compare.py` | Partial TP 1R+1.5R and 1R+2.0R test |
| `batch_backtest.py` | Batch coin validation runner |
| `validate_coins.py` | Candidate coin screener |
| `compare_caps.py` | Per-coin daily cap comparison (result: cap hurts, don't use) |
| `regime_impact.py` | Regime filter per-coin impact analysis |

### Scratch / output files

| File | What it is |
|---|---|
| `_screen.py` / `_screen_candidates.py` | One-off coin screening scripts |
| `_screen_out.txt` / `_screen_candidates_out.txt` | Screening run output |
| `_monthly_report.py` | Monthly PnL summary generator |
| `_report_out.txt` | Report output |

---

## 8. Key decisions log

| Date | Decision | Reason |
|---|---|---|
| Jan 2026 | ADX fade disabled | Backtest: PF 1.31 → 1.18, net −$2,726 |
| Jan 2026 | Breakeven stop disabled | Backtest: PF 1.31 → 1.11, net −$4,312 |
| Feb 2026 | Moved to V2 hybrid strategy (RSI + WR) | RSI PF consistently > WR on ZEC/SUI/BTC |
| Mar 2026 | POWERUSDT removed | Live slippage −$37 on SL; only 64 trades (low sample) |
| Mar 2026 | 8-coin portfolio confirmed | Best risk-adjusted portfolio from 30+ coins tested |
| Mar 2026 | Per-coin daily cap rejected | Cap=1/2/3 all hurt PF and net PnL — frequent RIVER days are profitable |
| Mar 2026 | Daily regime filter (EMA20) rejected | PF unchanged, net −$575, consec losses worse; 1H EMA200 already handles trend alignment |

---
## 9. How to add a new coin

1. Run standalone backtest: `python backtest_hybrid.py NEWUSDT 180`
   - Minimum bar: **PF ≥ 1.25, MaxDD ≤ $500**
2. Run cross-period validation: `python backtest_hybrid.py NEWUSDT 180` with `end_offset_days=180` — checks the *previous* 180-day window
   - ATOMUSDT failed this: PF 0.74 in Dec'24→Jun'25 — still in portfolio but flagged ⚠
3. Run portfolio test with the new coin added to `APPROVED_COINS`: `python backtest_portfolio.py 180`
   - Accept only if **portfolio PF ≥ 1.28** (within 2% of current baseline) and **MaxDD ≤ $500**
   - Many coins that pass standalone fail the portfolio test (slot-stealer effect)
4. Update `config.py` locally and push to VPS via git

---

## 10. VPS deployment

```bash
# SSH via DigitalOcean browser console (local SSH blocked by firewall)
# then:

cd /root/crypto_bot
git pull origin V2-branch
# Edit config.py directly on VPS (gitignored — never committed):
nano config.py

# Manage the service:
sudo systemctl restart cryptobot.service
sudo systemctl status cryptobot.service
sudo journalctl -u cryptobot.service -f        # live log
sudo journalctl -u cryptobot.service -n 200    # last 200 lines
```

The VPS `config.py` must always have the **live Bybit Demo keys** (`F4ZD88D7...`) and `DEMO = True`. Local `config.py` uses the test keys.


Note:

SMA pullback on 15m confirmed clean edge: ZEC 1.88, BTC 1.46, PAXG 1.56 (long-only). Slot conflict makes it incompatible with shared single-slot WR/RSI portfolio. Viable as separate parallel process on dedicated coins. Revisit after CFT challenge completion.