"""
Telegram notifications for the trading bot.

All sends happen in a background thread so the main trading loop is
never delayed by network latency.

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → copy the token.
  2. Start a chat with your new bot, then visit:
       https://api.telegram.org/bot<TOKEN>/getUpdates
     Copy the "id" value inside "chat" — that is your TELEGRAM_CHAT_ID.
  3. In config.py set:
       TELEGRAM_ENABLED   = True
       TELEGRAM_BOT_TOKEN = "123456789:ABCDefgh..."
       TELEGRAM_CHAT_ID   = "123456789"
"""

import logging
import queue
import threading
import traceback as tb
from datetime import datetime, timezone
from typing import Optional

import requests

import config

_logger = logging.getLogger("telegram")

_queue: queue.Queue = queue.Queue()
_thread: Optional[threading.Thread] = None


# ── Background sender ─────────────────────────────────────────────────────────

def _worker():
    while True:
        msg = _queue.get()
        if msg is None:          # sentinel → shut down
            break
        _send_now(msg)
        _queue.task_done()


def _send_now(text: str):
    if not getattr(config, "TELEGRAM_ENABLED", False):
        return
    token   = getattr(config, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            _logger.warning(
                "Telegram send failed (%s): %s", resp.status_code, resp.text[:200]
            )
    except Exception as exc:
        _logger.warning("Telegram error: %s", exc)


def start():
    """Start the background sender thread. Call once on bot startup."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(
        target=_worker, name="telegram-sender", daemon=True
    )
    _thread.start()
    _logger.info(
        "Telegram sender started (enabled=%s)",
        getattr(config, "TELEGRAM_ENABLED", False),
    )


def stop():
    """Flush the queue and shut down the sender thread gracefully."""
    _queue.put(None)
    if _thread:
        _thread.join(timeout=5)


def send(text: str):
    """Enqueue a message for async delivery — never blocks the caller."""
    _queue.put(text)


# ── Timestamp helper ──────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y  %H:%M UTC")


# ── Notification helpers ──────────────────────────────────────────────────────

def notify_bot_started(balance: float, peak: float, dd_left: float, profit_needed: float):
    mode = "TESTNET" if config.TESTNET else ("DEMO" if config.DEMO else "LIVE")
    send(
        f"🟢 <b>BOT STARTED</b>  [{mode}]  —  {_ts()}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Balance:  <b>${balance:,.2f}</b>\n"
        f"Peak:     ${peak:,.2f}\n"
        f"DD left:  ${dd_left:,.2f}\n"
        f"CFT need: <b>+${profit_needed:,.2f}</b> to pass\n"
    )


def notify_bot_stopped(reason: str):
    send(
        f"🔴 <b>BOT STOPPED</b>  —  {_ts()}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Reason: {reason}\n"
    )


def notify_trade_opened(
    symbol: str, side: str, entry: float,
    sl: float, tp: float, qty: float,
    balance: float, trade_count: int,
):
    arrow     = "📈" if side == "Buy" else "📉"
    direction = "LONG" if side == "Buy" else "SHORT"
    sl_dist   = abs(entry - sl)
    tp_dist   = abs(tp - entry)
    risk_usd  = sl_dist * qty
    reward    = tp_dist * qty
    send(
        f"{arrow} <b>{direction} OPENED</b> — {symbol}  —  {_ts()}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Entry:   <b>${entry:,.4f}</b>\n"
        f"Stop:    ${sl:,.4f}  (risk   ${risk_usd:,.2f})\n"
        f"Target:  ${tp:,.4f}  (reward ${reward:,.2f})\n"
        f"RR: 1:{config.RR}  |  Qty: {qty}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Balance: ${balance:,.2f}  |  Trade {trade_count}/{config.MAX_TRADES_PER_DAY}\n"
    )


def notify_trade_closed(
    symbol: str, side: str, entry: float,
    exit_price: float, pnl: float,
    duration_mins: float, daily_pnl: float,
    balance: float, dd_left: float,
    close_reason: str = "SL/TP",
):
    outcome   = "✅" if pnl >= 0 else "❌"
    pnl_flag  = "🟢" if pnl >= 0 else "🔴"
    direction = "LONG" if side == "Buy" else "SHORT"
    rr_achieved = abs(pnl) / config.RISK_PER_TRADE if config.RISK_PER_TRADE else 0
    send(
        f"{outcome} <b>{direction} CLOSED</b> — {symbol}  [{close_reason}]  —  {_ts()}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Entry:    ${entry:,.4f}\n"
        f"Exit:     <b>${exit_price:,.4f}</b>\n"
        f"PnL:      <b>${pnl:+,.2f}</b>  {pnl_flag}\n"
        f"RR:       {rr_achieved:.2f}R  |  Duration: {duration_mins:.0f} min\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Daily PnL: ${daily_pnl:+,.2f}  |  DD left: ${dd_left:,.2f}\n"
        f"Balance:   ${balance:,.2f}\n"
    )


def notify_daily_report(
    date_str: str, trade_count: int, wins: int, losses: int,
    daily_pnl: float, balance: float, peak: float,
    dd_left: float, profit_needed: float,
):
    wr          = (wins / trade_count * 100) if trade_count else 0
    pnl_flag    = "🟢" if daily_pnl >= 0 else "🔴"
    total_target = config.ACCOUNT_SIZE * 0.10      # e.g. $500 for a $5k account
    earned       = max(0, total_target - profit_needed)
    progress     = min(100, earned / total_target * 100) if total_target else 0
    bars         = int(progress / 10)
    bar          = "█" * bars + "░" * (10 - bars)
    floor        = peak * (1 - config.TRAILING_DD_PCT)
    send(
        f"📊 <b>DAILY REPORT</b>  —  {date_str}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Trades:    {trade_count}  ({wins}W / {losses}L)  WR {wr:.0f}%\n"
        f"Daily PnL: <b>${daily_pnl:+,.2f}</b>  {pnl_flag}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Balance:   <b>${balance:,.2f}</b>\n"
        f"Peak:      ${peak:,.2f}\n"
        f"DD Floor:  ${floor:,.2f}  |  DD Left: <b>${dd_left:,.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"CFT:  need <b>+${profit_needed:,.2f}</b> more\n"
        f"      [{bar}]  {progress:.0f}%\n"
    )


def notify_risk_limit(reason: str):
    """One-shot alert for a newly-triggered risk limit (caller manages dedup)."""
    send(
        f"⚠️ <b>RISK LIMIT HIT</b>  —  {_ts()}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"{reason}\n"
    )


def notify_error(title: str, detail: str):
    short = detail[:800] if len(detail) > 800 else detail
    send(
        f"🆘 <b>{title}</b>  —  {_ts()}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"<code>{short}</code>\n"
    )
