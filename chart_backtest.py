"""
chart_backtest.py  —  Interactive portfolio backtest dashboard.

Runs backtest_portfolio.run_portfolio(), then opens a Bybit-style
HTML dashboard with:
  • Portfolio equity curve + trade markers (zoom/pan, hover for trade details)
  • Drawdown chart below (x-axis linked to equity curve)
  • Per-coin cumulative equity lines
  • Monthly PnL bar chart
  • Win / Loss / BE outcome donut
  • Per-coin stats table

Usage:
    python chart_backtest.py              # 180d, all APPROVED_COINS
    python chart_backtest.py 90           # 90-day window
    python chart_backtest.py 180 30       # 180d ending 30 days ago
    python chart_backtest.py 180 0 0 --no-sma
"""

import os
import sys
import tempfile
import webbrowser
from collections import defaultdict

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    sys.exit("plotly not installed.  Run:  pip install plotly")

import pandas as pd

import config
import backtest_portfolio as bp

# ── CLI args ──────────────────────────────────────────────────────────────────
_args           = [a for a in sys.argv[1:] if not a.startswith("--")]
_no_sma         = "--no-sma"  in sys.argv
_sma_only       = "--sma-only" in sys.argv
days            = int(_args[0]) if len(_args) > 0 else 180
end_offset_days = int(_args[1]) if len(_args) > 1 else 0
coin_daily_cap  = int(_args[2]) if len(_args) > 2 else 0

if _no_sma:
    config.SMA_STRATEGY = False
    print("  [--no-sma] SMA fallback disabled\n")
if _sma_only:
    config.SMA_STRATEGY = True
    config.SMA_APPROVED_COINS = list(config.APPROVED_COINS)
    print("  [--sma-only] SMA strategy only\n")

# ── Run backtest ──────────────────────────────────────────────────────────────
print(f"Running {days}d portfolio backtest …")
all_trades, trades_per_day, cap_blocked_days = bp.run_portfolio(
    days, end_offset_days, coin_daily_cap)

if not all_trades:
    sys.exit("No completed trades — nothing to chart.")

all_trades.sort(key=lambda t: t["exit_ts"])

# ── Helpers ───────────────────────────────────────────────────────────────────
def ms_to_ts(ms):
    return pd.Timestamp(ms, unit="ms", tz="UTC")

start_dt = ms_to_ts(all_trades[0]["open_ts"])

# ── Portfolio equity + drawdown ───────────────────────────────────────────────
eq_x = [start_dt] + [ms_to_ts(t["exit_ts"]) for t in all_trades]
eq_y = [0.0]
for t in all_trades:
    eq_y.append(eq_y[-1] + t["pnl"])

peak = 0.0
dd_y = []
for e in eq_y:
    if e > peak:
        peak = e
    dd_y.append(e - peak)

net    = eq_y[-1]
max_dd = abs(min(dd_y))
wins   = [t for t in all_trades if t["result"] == "win"]
losses = [t for t in all_trades if t["result"] == "loss"]
bes    = [t for t in all_trades if t["result"] in ("breakeven", "fade")]
gw     = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
gl     = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
pf     = gw / gl if gl > 0 else float("inf")
wr     = len(wins) / len(all_trades) * 100

# ── Trade markers (x coords + y = equity at exit) ────────────────────────────
cum = 0.0
exit_eq = {}
for t in all_trades:
    cum += t["pnl"]
    exit_eq[id(t)] = cum

def _hover(t):
    dt = ms_to_ts(t["exit_ts"]).strftime("%Y-%m-%d %H:%M")
    sign = "+" if t["pnl"] >= 0 else ""
    return (f"<b>{t['symbol']}</b>  {t['sig'].upper()} · {t['strategy']}<br>"
            f"PnL: {sign}${t['pnl']:.2f}<br>"
            f"Entry: {t['entry']:.5g}  SL: {t['sl']:.5g}  TP: {t['tp']:.5g}<br>"
            f"Exit: {dt}")

win_x  = [ms_to_ts(t["exit_ts"]) for t in all_trades if t["result"] == "win"]
win_y  = [exit_eq[id(t)] for t in all_trades if t["result"] == "win"]
win_h  = [_hover(t) for t in all_trades if t["result"] == "win"]

loss_x = [ms_to_ts(t["exit_ts"]) for t in all_trades if t["result"] == "loss"]
loss_y = [exit_eq[id(t)] for t in all_trades if t["result"] == "loss"]
loss_h = [_hover(t) for t in all_trades if t["result"] == "loss"]

be_x   = [ms_to_ts(t["exit_ts"]) for t in all_trades if t["result"] in ("breakeven","fade")]
be_y   = [exit_eq[id(t)] for t in all_trades if t["result"] in ("breakeven","fade")]
be_h   = [_hover(t) for t in all_trades if t["result"] in ("breakeven","fade")]

# ── Per-coin equity curves ────────────────────────────────────────────────────
# ── Monthly PnL ───────────────────────────────────────────────────────────────
monthly: dict = defaultdict(float)
for t in all_trades:
    monthly[ms_to_ts(t["exit_ts"]).strftime("%Y-%m")] += t["pnl"]
months     = sorted(monthly)
month_vals = [monthly[m] for m in months]

# ── Bybit colour palette ─────────────────────────────────────────────────────
BG       = "#0b0e11"   # page background
CARD     = "#161a1e"   # panel/card background
CARD2    = "#1c2127"   # slightly lighter card
BORDER   = "#2b3139"   # border/divider
GRID     = "#1e242c"   # chart gridlines
GREEN    = "#0ecb81"   # Bybit green  (wins, positive)
RED      = "#f6465d"   # Bybit red    (losses, negative)
YELLOW   = "#f0b90b"   # Bybit gold   (equity line, accents)
BLUE     = "#1890ff"   # info blue    (per-coin lines start)
TEXT     = "#eaecef"   # primary text
SUBTEXT  = "#848e9c"   # secondary / axis labels
BE_CLR   = "#d4a72c"   # breakeven dots

COIN_COLORS = [
    "#1890ff", "#0ecb81", "#f0b90b", "#a66cff",
    "#ff6b6b", "#43e0d9", "#ff9c2a", "#88cc44",
    "#e879f9", "#38bdf8", "#fb923c", "#a3e635",
]

# ── Per-coin stats table data ─────────────────────────────────────────────────
month_clr  = [GREEN if v >= 0 else RED for v in month_vals]

tbl_coins, tbl_n, tbl_wr, tbl_pf, tbl_net, tbl_dd, tbl_strat = [], [], [], [], [], [], []
for sym in config.APPROVED_COINS:
    ctrades = [t for t in all_trades if t["symbol"] == sym]
    if not ctrades:
        continue
    s = bp._stats(ctrades)
    rsi_n = sum(1 for t in ctrades if t.get("strategy") == "RSI")
    sma_n = sum(1 for t in ctrades if t.get("strategy") == "SMA")
    wr_n  = sum(1 for t in ctrades if t.get("strategy") == "WR")
    parts = []
    if wr_n:  parts.append(f"WR {wr_n}")
    if rsi_n: parts.append(f"RSI {rsi_n}")
    if sma_n: parts.append(f"SMA {sma_n}")
    tbl_coins.append(sym.replace("USDT", ""))
    tbl_n.append(str(s["n"]))
    tbl_wr.append(f"{s['wr']:.1f}%")
    tbl_pf.append(f"{s['pf']:.2f}")
    tbl_net.append(f"${s['net']:+.0f}")
    tbl_dd.append(f"${s['max_dd']:.0f}")
    tbl_strat.append("  ·  ".join(parts))

net_font_colors  = [GREEN if v.startswith("+") or (v.startswith("$") and not v.startswith("$-")) and float(v.replace("$","").replace("+","")) > 0 else RED if "-" in v else SUBTEXT for v in tbl_net]

# ── Build figure ──────────────────────────────────────────────────────────────
fig = make_subplots(
    rows=4, cols=2,
    row_heights=[0.34, 0.10, 0.30, 0.26],
    column_widths=[0.62, 0.38],
    specs=[
        [{"colspan": 2, "type": "xy"},   None],
        [{"colspan": 2, "type": "xy"},   None],
        [{"type": "xy"},                 {"type": "xy"}],
        [{"type": "domain"},             {"type": "table"}],
    ],
    subplot_titles=[""] * 8,   # we'll add styled annotations manually
    vertical_spacing=0.06,
    horizontal_spacing=0.06,
)

# ── Row 1: Equity curve ───────────────────────────────────────────────────────
# Shaded area below zero (loss zone)
fig.add_trace(go.Scatter(
    x=eq_x, y=[min(v, 0) for v in eq_y],
    mode="lines", showlegend=False, hoverinfo="skip",
    line=dict(width=0),
    fill="tozeroy", fillcolor="rgba(246,70,93,0.07)",
), row=1, col=1)

# Shaded area above zero (profit zone)
fig.add_trace(go.Scatter(
    x=eq_x, y=[max(v, 0) for v in eq_y],
    mode="lines", showlegend=False, hoverinfo="skip",
    line=dict(width=0),
    fill="tozeroy", fillcolor="rgba(14,203,129,0.07)",
), row=1, col=1)

# Equity line
fig.add_trace(go.Scatter(
    x=eq_x, y=eq_y,
    mode="lines", name="Portfolio Equity",
    line=dict(color=YELLOW, width=2.5),
    hovertemplate="<b>Equity</b>  $%{y:,.2f}<br><span style='color:#848e9c'>%{x|%d %b %Y %H:%M}</span><extra></extra>",
), row=1, col=1)

fig.add_trace(go.Scatter(
    x=win_x, y=win_y, mode="markers", name="Win",
    marker=dict(color=GREEN, size=8, symbol="triangle-up",
                line=dict(color="rgba(14,203,129,0.4)", width=1)),
    text=win_h, hovertemplate="%{text}<extra></extra>",
), row=1, col=1)

fig.add_trace(go.Scatter(
    x=loss_x, y=loss_y, mode="markers", name="Loss",
    marker=dict(color=RED, size=8, symbol="triangle-down",
                line=dict(color="rgba(246,70,93,0.4)", width=1)),
    text=loss_h, hovertemplate="%{text}<extra></extra>",
), row=1, col=1)

if be_x:
    fig.add_trace(go.Scatter(
        x=be_x, y=be_y, mode="markers", name="BE / Fade",
        marker=dict(color=BE_CLR, size=6, symbol="circle-open",
                    line=dict(color=BE_CLR, width=1.5)),
        text=be_h, hovertemplate="%{text}<extra></extra>",
    ), row=1, col=1)

fig.add_hline(y=0, line_color=BORDER, line_width=1, row=1, col=1)

# ── Row 2: Drawdown ───────────────────────────────────────────────────────────
fig.add_trace(go.Scatter(
    x=eq_x, y=dd_y,
    mode="lines", name="Drawdown",
    line=dict(color=RED, width=1.5),
    fill="tozeroy", fillcolor="rgba(246,70,93,0.18)",
    showlegend=False,
    hovertemplate="<b>Drawdown</b>  $%{y:,.2f}<br><span style='color:#848e9c'>%{x|%d %b %Y %H:%M}</span><extra></extra>",
), row=2, col=1)

fig.update_layout(**{"xaxis2.matches": "x", "xaxis2.showticklabels": False})

# ── Row 3 left: Per-coin equity ───────────────────────────────────────────────
for idx, sym in enumerate(config.APPROVED_COINS):
    ctrades = sorted([t for t in all_trades if t["symbol"] == sym],
                     key=lambda t: t["exit_ts"])
    if not ctrades:
        continue
    cxs = [start_dt]
    cys = [0.0]
    c = 0.0
    for t in ctrades:
        c += t["pnl"]
        cxs.append(ms_to_ts(t["exit_ts"]))
        cys.append(c)
    label = sym.replace("USDT", "")
    color = COIN_COLORS[idx % len(COIN_COLORS)]
    fig.add_trace(go.Scatter(
        x=cxs, y=cys,
        mode="lines", name=label,
        line=dict(color=color, width=1.8),
        hovertemplate=f"<b>{label}</b>  $%{{y:,.2f}}<br><span style='color:#848e9c'>%{{x|%d %b %Y}}</span><extra></extra>",
    ), row=3, col=1)

fig.add_hline(y=0, line_color=BORDER, line_width=1, row=3, col=1)

# ── Row 3 right: Monthly PnL ──────────────────────────────────────────────────
fig.add_trace(go.Bar(
    x=months, y=month_vals,
    marker=dict(
        color=month_clr,
        line=dict(color=[GREEN if v >= 0 else RED for v in month_vals], width=1),
    ),
    name="Monthly PnL",
    showlegend=False,
    text=[f"${v:+,.0f}" for v in month_vals],
    textposition="outside",
    textfont=dict(size=10, color=[GREEN if v >= 0 else RED for v in month_vals]),
    hovertemplate="<b>%{x}</b><br>PnL: $%{y:+,.2f}<extra></extra>",
), row=3, col=2)

fig.add_hline(y=0, line_color=BORDER, line_width=1, row=3, col=2)

# ── Row 4 left: Outcome donut ─────────────────────────────────────────────────
fig.add_trace(go.Pie(
    labels=[f"Win  {len(wins)}", f"Loss  {len(losses)}", f"BE  {len(bes)}"],
    values=[len(wins), len(losses), len(bes)],
    marker=dict(
        colors=[GREEN, RED, BE_CLR],
        line=dict(color=BG, width=3),
    ),
    hole=0.60,
    textinfo="label+percent",
    textfont=dict(size=12, color=TEXT),
    showlegend=False,
    hovertemplate="<b>%{label}</b><br>%{value} trades · %{percent}<extra></extra>",
), row=4, col=1)

# Centre annotation on the donut
fig.add_annotation(
    text=f"<b style='font-size:15px'>{wr:.1f}%</b><br><span style='color:#848e9c;font-size:11px'>Win Rate</span>",
    x=0.18, y=0.115,
    xref="paper", yref="paper",
    showarrow=False,
    font=dict(color=TEXT, size=13),
)

# ── Row 4 right: Per-coin stats table ─────────────────────────────────────────
ROW_EVEN  = CARD
ROW_ODD   = CARD2
row_fills = [ROW_EVEN if i % 2 == 0 else ROW_ODD for i in range(len(tbl_coins))]

fig.add_trace(go.Table(
    columnwidth=[60, 38, 52, 45, 58, 58, 80],
    header=dict(
        values=["<b>Coin</b>","<b>Trades</b>","<b>Win %</b>",
                "<b>PF</b>","<b>Net PnL</b>","<b>Max DD</b>","<b>Strategy</b>"],
        fill_color="#1e2630",
        font=dict(color=SUBTEXT, size=11, family="Inter, Arial, sans-serif"),
        align=["left", "center", "center", "center", "center", "center", "left"],
        line_color=BORDER,
        height=32,
    ),
    cells=dict(
        values=[tbl_coins, tbl_n, tbl_wr, tbl_pf, tbl_net, tbl_dd, tbl_strat],
        fill_color=[
            row_fills, row_fills, row_fills, row_fills,
            row_fills, row_fills, row_fills,
        ],
        font=dict(
            color=[TEXT, SUBTEXT, TEXT, TEXT, net_font_colors, RED, SUBTEXT],
            size=11,
            family="Inter, Arial, sans-serif",
        ),
        align=["left", "center", "center", "center", "center", "center", "left"],
        line_color=BORDER,
        height=28,
    ),
), row=4, col=2)

# ── Layout ────────────────────────────────────────────────────────────────────
netsign = f"+${net:,.0f}" if net >= 0 else f"-${abs(net):,.0f}"
title_html = (
    f"<span style='font-size:17px;color:{TEXT};font-weight:700;font-family:Inter,Arial'>" 
    f"Portfolio Backtest</span>"
    f"<span style='color:{SUBTEXT};font-size:13px'>  ·  {days}d window  ·  {len(all_trades)} trades  ·  "
    f"{getattr(config,'MAX_OPEN_SLOTS',1)}-slot</span>"
)

fig.update_layout(
    title=dict(text=title_html, x=0.01, y=0.993, xanchor="left",
               font=dict(color=TEXT, size=17)),
    paper_bgcolor=BG,
    plot_bgcolor=CARD,
    font=dict(color=SUBTEXT, family="Inter, Arial, sans-serif", size=11),
    height=1450,
    legend=dict(
        bgcolor="rgba(22,26,30,0.92)",
        bordercolor=BORDER, borderwidth=1,
        font=dict(size=11, color=TEXT),
        x=0.01, y=0.992,
        orientation="h",
        tracegroupgap=4,
    ),
    margin=dict(l=64, r=32, t=56, b=48),
    hovermode="x unified",
    hoverlabel=dict(
        bgcolor="#1e2630",
        bordercolor=BORDER,
        font=dict(color=TEXT, size=11, family="Inter, Arial, sans-serif"),
    ),
)

# ── Axis styling ──────────────────────────────────────────────────────────────
axis_style = dict(
    gridcolor=GRID,
    gridwidth=1,
    zerolinecolor=BORDER,
    zerolinewidth=1,
    tickfont=dict(color=SUBTEXT, size=10),
    linecolor=BORDER,
    showline=True,
    tickcolor=BORDER,
    mirror=False,
)
for row in range(1, 5):
    for col in range(1, 3):
        fig.update_xaxes(axis_style, row=row, col=col)
        fig.update_yaxes(axis_style, tickprefix="$", row=row, col=col)

# Monthly bars: no dollar prefix on x-axis
fig.update_xaxes(tickangle=-35, tickfont=dict(size=10), row=3, col=2)
fig.update_yaxes(tickprefix="$", row=3, col=2)

# Drawdown: no y prefix override needed but keep consistent
fig.update_yaxes(tickprefix="$", row=2, col=1)

# ── Section header annotations ────────────────────────────────────────────────
def _header(text, x, y):
    return dict(
        text=f"<b style='color:{TEXT};font-size:12px'>{text}</b>",
        xref="paper", yref="paper",
        x=x, y=y, xanchor="left",
        showarrow=False,
        font=dict(color=TEXT, size=12),
        bgcolor="rgba(0,0,0,0)",
    )

# ── KPI badges injected as HTML via the page title bar ───────────────────────
kpi_color  = GREEN if net >= 0 else RED
kpis = [
    ("NET P&L",    f"<span style='color:{kpi_color}'>${net:+,.0f}</span>"),
    ("PROFIT FACTOR", f"<span style='color:{YELLOW}'>{pf:.2f}</span>"),
    ("WIN RATE",    f"<span style='color:{TEXT}'>{wr:.1f}%</span>"),
    ("MAX DRAWDOWN", f"<span style='color:{RED}'>-${max_dd:,.0f}</span>"),
    ("TRADES",      f"<span style='color:{TEXT}'>{len(all_trades)}</span>"),
    ("RISK / TRADE", f"<span style='color:{TEXT}'>${config.RISK_PER_TRADE}</span>"),
]

# ── Inject a KPI top-bar via full HTML output ─────────────────────────────────
kpi_html = "".join(
    f"""<div style='display:inline-flex;flex-direction:column;align-items:center;
        background:#161a1e;border:1px solid #2b3139;border-radius:6px;
        padding:8px 20px;margin:0 6px;min-width:110px'>
        <span style='color:#848e9c;font-size:10px;letter-spacing:.08em;
        font-family:Inter,Arial,sans-serif;text-transform:uppercase'>{label}</span>
        <span style='font-size:18px;font-weight:700;font-family:Inter,Arial,sans-serif;
        margin-top:3px'>{val}</span></div>"""
    for label, val in kpis
)

kpi_bar = f"""
<div style='background:#0b0e11;padding:14px 24px 10px;border-bottom:1px solid #2b3139;
font-family:Inter,Arial,sans-serif;display:flex;align-items:center;flex-wrap:wrap;gap:4px'>
  <div style='color:#eaecef;font-size:16px;font-weight:700;margin-right:16px'>
    📈 Portfolio Backtest &nbsp;
    <span style='color:#848e9c;font-size:12px;font-weight:400'>{days}d · {len(all_trades)} trades</span>
  </div>
  {kpi_html}
</div>
"""

# ── Save and open ─────────────────────────────────────────────────────────────
out = os.path.join(tempfile.gettempdir(), "backtest_dashboard.html")
raw_html = fig.to_html(
    include_plotlyjs="cdn",
    full_html=True,
    config={"scrollZoom": True, "displayModeBar": True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "toImageButtonOptions": {"format": "png", "scale": 2}},
)

# Inject KPI bar + Google Font + global CSS right after <body>
body_tag = "<body>"
head_inject = (
    "<link rel='preconnect' href='https://fonts.googleapis.com'>\n"
    "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap' rel='stylesheet'>\n"
    "<style>\n"
    "  body { margin:0; padding:0; background:#0b0e11; font-family:Inter,Arial,sans-serif; }\n"
    "  .main-svg { border-radius:8px; }\n"
    "  ::-webkit-scrollbar { width:6px; background:#0b0e11; }\n"
    "  ::-webkit-scrollbar-thumb { background:#2b3139; border-radius:3px; }\n"
    "</style>\n"
)
raw_html = raw_html.replace("<head>", "<head>\n" + head_inject)
raw_html = raw_html.replace(body_tag, body_tag + "\n" + kpi_bar, 1)

with open(out, "w", encoding="utf-8") as fh:
    fh.write(raw_html)

print(f"\n✓ Dashboard → {out}")
print("  Opening in browser …")
webbrowser.open(f"file:///{out.replace(os.sep, '/')}")
