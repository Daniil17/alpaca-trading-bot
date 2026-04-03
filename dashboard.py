"""
TRADING BOT DASHBOARD
======================
Streamlit web dashboard for the Alpaca Trading Bot.
Connects live to Alpaca API for real-time data and also
reads bot_state.json for historical context.

Deploy for free: https://streamlit.io/cloud
  1. Push this repo to GitHub (already done)
  2. Go to share.streamlit.io → "New app"
  3. Pick your repo, branch: main, file: dashboard.py
  4. Add secrets in Streamlit Cloud dashboard:
       ALPACA_API_KEY = "your-key"
       ALPACA_SECRET_KEY = "your-secret"
       ALPACA_PAPER = "true"
  5. Copy the app URL → paste into DASHBOARD_URL in config.py

Run locally:
  streamlit run dashboard.py
"""

import os
import json
import time
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Trading Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────
# CREDENTIALS — environment vars or Streamlit secrets
# ─────────────────────────────────────────────────────────

def get_credentials():
    """Pull API credentials from environment or Streamlit secrets."""
    try:
        api_key = st.secrets.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY", "")
        secret_key = st.secrets.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY", "")
        paper = (st.secrets.get("ALPACA_PAPER") or os.environ.get("ALPACA_PAPER", "true")).lower() == "true"
        return api_key, secret_key, paper
    except Exception:
        return os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", ""), True


# ─────────────────────────────────────────────────────────
# ALPACA CLIENT (cached — reconnects at most once per 60s)
# ─────────────────────────────────────────────────────────

@st.cache_resource(ttl=60)
def get_alpaca_clients():
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
    api_key, secret_key, paper = get_credentials()
    if not api_key or api_key == "ALPACA_API_KEY":
        return None, None, None, paper
    trading = TradingClient(api_key, secret_key, paper=paper)
    stock_data = StockHistoricalDataClient(api_key, secret_key)
    crypto_data = CryptoHistoricalDataClient(api_key, secret_key)
    return trading, stock_data, crypto_data, paper


# ─────────────────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def fetch_account():
    trading, _, _, paper = get_alpaca_clients()
    if not trading:
        return None
    try:
        acc = trading.get_account()
        return {
            "equity": float(acc.equity),
            "cash": float(acc.cash),
            "portfolio_value": float(acc.portfolio_value),
            "buying_power": float(acc.buying_power),
            "paper": paper,
        }
    except Exception as e:
        st.error(f"Account fetch failed: {e}")
        return None


@st.cache_data(ttl=30)
def fetch_positions():
    trading, _, _, _ = get_alpaca_clients()
    if not trading:
        return []
    try:
        positions = trading.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "entry": float(p.avg_entry_price),
                "current": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc) * 100,
                "is_crypto": "/" in p.symbol,
            }
            for p in positions
        ]
    except Exception as e:
        st.error(f"Positions fetch failed: {e}")
        return []


@st.cache_data(ttl=60)
def fetch_recent_orders(limit=30):
    trading, _, _, _ = get_alpaca_clients()
    if not trading:
        return []
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit)
        orders = trading.get_orders(req)
        result = []
        for o in orders:
            try:
                result.append({
                    "symbol": o.symbol,
                    "side": str(o.side).replace("OrderSide.", ""),
                    "qty": str(o.qty or o.notional),
                    "filled_price": float(o.filled_avg_price) if o.filled_avg_price else 0,
                    "status": str(o.status),
                    "submitted_at": o.submitted_at.strftime("%Y-%m-%d %H:%M") if o.submitted_at else "",
                    "is_crypto": "/" in o.symbol,
                })
            except Exception:
                continue
        return result
    except Exception as e:
        st.error(f"Orders fetch failed: {e}")
        return []


@st.cache_data(ttl=300)
def fetch_portfolio_history():
    """Fetch 30-day portfolio history for the equity curve."""
    trading, _, _, _ = get_alpaca_clients()
    if not trading:
        return None
    try:
        history = trading.get_portfolio_history(period="1M", timeframe="1D")
        if history and history.equity:
            df = pd.DataFrame({
                "timestamp": pd.to_datetime(history.timestamp, unit="s"),
                "equity": history.equity,
                "profit_loss": history.profit_loss,
            })
            return df
        return None
    except Exception:
        return None


def load_bot_state():
    """Load bot_state.json if present (for run count, peak value, etc.)."""
    try:
        if os.path.exists("bot_state.json"):
            with open("bot_state.json") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


# ─────────────────────────────────────────────────────────
# STYLING
# ─────────────────────────────────────────────────────────

def metric_card(label, value, delta=None, delta_color="normal"):
    """Render a styled metric."""
    st.metric(label=label, value=value, delta=delta, delta_color=delta_color)


def pl_color(value):
    return "🟢" if value >= 0 else "🔴"


# ─────────────────────────────────────────────────────────
# MAIN DASHBOARD
# ─────────────────────────────────────────────────────────

def main():
    # Header
    col_title, col_refresh = st.columns([4, 1])
    with col_title:
        st.title("📈 Trading Bot Dashboard")
        st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    with col_refresh:
        st.write("")
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    # Check credentials
    api_key, _, paper = get_credentials()
    if not api_key or api_key == "ALPACA_API_KEY":
        st.warning(
            "⚠️ Alpaca credentials not configured. "
            "Set ALPACA_API_KEY and ALPACA_SECRET_KEY as environment variables "
            "or Streamlit secrets."
        )
        return

    # Mode badge
    mode_badge = "🧪 PAPER TRADING" if paper else "💵 LIVE TRADING"
    st.info(mode_badge)

    # Fetch data
    with st.spinner("Fetching live data..."):
        account = fetch_account()
        positions = fetch_positions()
        orders = fetch_recent_orders()
        state = load_bot_state()
        history_df = fetch_portfolio_history()

    if not account:
        st.error("Could not connect to Alpaca. Check your API credentials.")
        return

    # ── SECTION 1: Portfolio Overview ──────────────────────────────
    st.subheader("💼 Portfolio Overview")

    total_pl = sum(p["unrealized_pl"] for p in positions)
    total_invested = sum(p["market_value"] for p in positions)
    cash_pct = account["cash"] / account["portfolio_value"] * 100 if account["portfolio_value"] else 0
    peak = state.get("peak_portfolio_value", account["portfolio_value"])
    drawdown = (peak - account["portfolio_value"]) / peak * 100 if peak > 0 else 0

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        metric_card("Portfolio Value", f"${account['portfolio_value']:,.2f}")
    with col2:
        metric_card("Cash", f"${account['cash']:,.2f}", f"{cash_pct:.0f}% of portfolio")
    with col3:
        pl_sign = "+" if total_pl >= 0 else ""
        metric_card(
            "Unrealized P&L",
            f"{pl_color(total_pl)} ${pl_sign}{total_pl:,.2f}",
            delta_color="normal",
        )
    with col4:
        metric_card("Open Positions", str(len(positions)))
    with col5:
        dd_color = "inverse" if drawdown > 5 else "normal"
        metric_card("Drawdown from Peak", f"{drawdown:.1f}%", delta_color=dd_color)

    st.divider()

    # ── SECTION 2: Portfolio Equity Curve ──────────────────────────
    if history_df is not None and not history_df.empty:
        st.subheader("📊 Portfolio Equity Curve (30 Days)")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=history_df["timestamp"],
            y=history_df["equity"],
            mode="lines",
            name="Portfolio Value",
            line=dict(color="#00C851", width=2),
            fill="tozeroy",
            fillcolor="rgba(0,200,81,0.08)",
        ))
        fig.update_layout(
            height=280,
            margin=dict(l=0, r=0, t=10, b=0),
            xaxis_title=None,
            yaxis_title="Value ($)",
            hovermode="x unified",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True, key="equity_curve")
        st.divider()

    # ── SECTION 3: Open Positions ───────────────────────────────────
    st.subheader("📈 Open Positions")

    if not positions:
        st.info("No open positions right now.")
    else:
        stock_positions = [p for p in positions if not p["is_crypto"]]
        crypto_positions = [p for p in positions if p["is_crypto"]]

        tabs = st.tabs([
            f"All ({len(positions)})",
            f"📈 Stocks ({len(stock_positions)})",
            f"🪙 Crypto ({len(crypto_positions)})",
        ])

        for tab_idx, (tab, pos_list) in enumerate(zip(tabs, [positions, stock_positions, crypto_positions])):
            with tab:
                if not pos_list:
                    st.info("No positions in this category.")
                    continue

                df = pd.DataFrame(pos_list)
                df = df.sort_values("unrealized_pl", ascending=False)

                # Colour the P&L column
                def colour_pl(val):
                    color = "#00C851" if val >= 0 else "#FF4444"
                    return f"color: {color}; font-weight: bold"

                display_df = df[["symbol", "qty", "entry", "current", "market_value",
                                  "unrealized_pl", "unrealized_plpc"]].copy()
                display_df.columns = ["Symbol", "Qty", "Entry $", "Current $",
                                       "Market Value $", "Unrealized P&L $", "P&L %"]
                display_df["Entry $"] = display_df["Entry $"].map("${:,.4f}".format)
                display_df["Current $"] = display_df["Current $"].map("${:,.4f}".format)
                display_df["Market Value $"] = display_df["Market Value $"].map("${:,.2f}".format)
                display_df["Unrealized P&L $"] = display_df["Unrealized P&L $"].map("${:+,.2f}".format)
                display_df["P&L %"] = display_df["P&L %"].map("{:+.2f}%".format)

                st.dataframe(display_df, use_container_width=True, hide_index=True)

                # Donut chart of allocation
                if len(pos_list) > 1:
                    fig_pie = px.pie(
                        df,
                        values="market_value",
                        names="symbol",
                        hole=0.5,
                        title="Position Allocation",
                    )
                    fig_pie.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
                    st.plotly_chart(fig_pie, use_container_width=True, key=f"pie_chart_{tab_idx}")

    st.divider()

    # ── SECTION 4: P&L Bar Chart ────────────────────────────────────
    if positions:
        st.subheader("💰 Unrealized P&L by Position")
        df_pl = pd.DataFrame(positions).sort_values("unrealized_pl")
        colors = ["#00C851" if v >= 0 else "#FF4444" for v in df_pl["unrealized_pl"]]

        fig_bar = go.Figure(go.Bar(
            x=df_pl["symbol"],
            y=df_pl["unrealized_pl"],
            marker_color=colors,
            text=df_pl["unrealized_pl"].map("${:+,.2f}".format),
            textposition="outside",
        ))
        fig_bar.update_layout(
            height=300,
            margin=dict(l=0, r=0, t=10, b=0),
            xaxis_title=None,
            yaxis_title="P&L ($)",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_bar, use_container_width=True, key="pnl_bar")
        st.divider()

    # ── SECTION 5: Recent Trades ────────────────────────────────────
    st.subheader("🔁 Recent Trades")

    if not orders:
        st.info("No recent closed orders found.")
    else:
        orders_df = pd.DataFrame(orders)
        orders_df["type"] = orders_df["is_crypto"].map({True: "🪙 Crypto", False: "📈 Stock"})
        orders_df["side"] = orders_df["side"].str.upper()

        display_orders = orders_df[["submitted_at", "type", "symbol", "side",
                                     "qty", "filled_price", "status"]].copy()
        display_orders.columns = ["Time", "Type", "Symbol", "Side", "Qty/Notional",
                                   "Fill Price $", "Status"]
        display_orders["Fill Price $"] = display_orders["Fill Price $"].map(
            lambda x: f"${x:,.4f}" if x > 0 else "—"
        )

        def style_side(val):
            if val == "BUY":
                return "color: #00C851; font-weight: bold"
            elif val == "SELL":
                return "color: #FF4444; font-weight: bold"
            return ""

        st.dataframe(display_orders, use_container_width=True, hide_index=True)
        st.divider()

    # ── SECTION 6: Bot Status ───────────────────────────────────────
    st.subheader("🤖 Bot Status")

    col_a, col_b, col_c, col_d = st.columns(4)
    with col_a:
        run_count = state.get("run_count", "—")
        metric_card("Total Cycles Run", str(run_count))
    with col_b:
        peak_val = state.get("peak_portfolio_value", 0)
        metric_card("Peak Portfolio Value", f"${peak_val:,.2f}" if peak_val else "—")
    with col_c:
        manual = state.get("manual_symbols", [])
        metric_card("Manual Positions (ignored)", str(len(manual)) if manual else "0")
    with col_d:
        last_summary = state.get("last_summary_date", "—")
        metric_card("Last Daily Summary", str(last_summary))

    st.divider()

    # ── FOOTER ──────────────────────────────────────────────────────
    st.caption(
        "Dashboard reads live data from Alpaca API. "
        "Bot state (cycles, peak value) is loaded from bot_state.json in the repo. "
        "Auto-refreshes every 60 seconds — or press Refresh above."
    )

    # Auto-refresh every 60 seconds
    time.sleep(0.1)
    st_autorefresh = st.empty()
    st_autorefresh.markdown(
        '<meta http-equiv="refresh" content="60">',
        unsafe_allow_html=True,
    )


if __name__ == "__main__" or True:
    main()
