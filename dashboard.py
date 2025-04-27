import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import numpy as np
import os
import requests
from io import StringIO
from streamlit_autorefresh import st_autorefresh  # New import

# === Page Config ===
st.set_page_config(
    page_title="US30 Trading Bot Dashboard", 
    layout="wide", 
    page_icon="📈",
    initial_sidebar_state="expanded"
)

# === Styling ===
st.markdown("""
<style>
    .metric-card {
        background-color: #1e293b;
        border-radius: 10px;
        padding: 15px;
        margin-bottom: 10px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        border-left: 4px solid #3b82f6;
    }
    .positive { color: #10b981; }
    .negative { color: #ef4444; }
    .stDataFrame { font-size: 14px; }
    .stTabs [data-baseweb="tab-list"] { gap: 10px; }
    .stTabs [data-baseweb="tab"] { padding: 8px 16px; border-radius: 8px 8px 0 0; }
    .stTabs [aria-selected="true"] { background-color: #1e293b; color: white; }
    @media (max-width: 768px) { .metric-card { padding: 10px; } }
</style>
""", unsafe_allow_html=True)

# === Remote URLs ===
BASE_URL = "https://trading-bot-1-e2rp.onrender.com"
TRADES_URL = f"{BASE_URL}/download/trades"
EQUITY_URL = f"{BASE_URL}/download/equity"

# === Data Loader ===
@st.cache_data(ttl=300)
def load_data(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        return pd.read_csv(StringIO(response.text))
    except Exception as e:
        st.error(f"Failed to load data from {url}: {e}")
        return None

# === Sidebar: Filters and Refresh Mode ===
with st.sidebar:
    st.subheader("🔍 Filters")

    refresh_mode = st.radio(
        "Refresh Mode",
        ["Auto", "Manual"],
        horizontal=True
    )

    date_range = st.date_input(
        "Select Date Range",
        value=[datetime(2025, 4, 21), datetime(2025, 4, 21)],
    )

    selected_actions = st.multiselect(
        "Select Trade Type",
        options=["BUY", "SELL"],
        default=["BUY", "SELL"]
    )

# === Auto-Refresh Every 10 Seconds ===
if refresh_mode == "Auto":
    st_autorefresh(interval=10 * 1000, key="auto_refresh")

# === Title ===
st.title("📈 US30 Trading Bot Dashboard")

# === Load data ===
df = load_data(TRADES_URL)
equity_df = load_data(EQUITY_URL)

if df is not None and equity_df is not None:
    # Process data
    df["Time"] = pd.to_datetime(df["Time"])
    df["Price"] = df["Price"].astype(float)
    equity_df["Time"] = pd.to_datetime(equity_df["Time"])

    current_price = df["Price"].iloc[-1]
    
    df["PnL"] = df.apply(lambda row:
                         (current_price - row["Price"]) if row["Action"] == "BUY"
                         else (row["Price"] - current_price), axis=1)
    df["PnL_Percentage"] = df["PnL"] / df["Price"] * 100
    df["Status"] = df["PnL"].apply(lambda x: "PROFIT" if x >= 0 else "LOSS")
    
    total_pnl = df["PnL"].sum()
    win_rate = len(df[df["PnL"] > 0]) / len(df[df["PnL"] != 0]) * 100 if len(df[df["PnL"] != 0]) > 0 else 0
    avg_pnl = total_pnl / len(df[df["PnL"] != 0]) if len(df[df["PnL"] != 0]) > 0 else 0

    returns = df[df["PnL"] != 0]["PnL"] / df[df["PnL"] != 0]["Price"]
    sharpe_ratio = (returns.mean() / returns.std()) * np.sqrt(252) if len(returns) > 1 else 0
    max_drawdown = (equity_df["Equity"].max() - equity_df["Equity"].min()) / equity_df["Equity"].max() * 100

    filtered_df = df[
        (df["Time"].dt.date >= date_range[0]) &
        (df["Time"].dt.date <= date_range[1]) &
        (df["Action"].isin(selected_actions))
    ]

    # === Tabs ===
    tab1, tab2, tab3, tab4 = st.tabs(["📈 Overview", "📜 Trade Log", "📊 Advanced Metrics", "🧮 Open Positions"])

    with tab1:
        st.subheader("📊 Performance Overview")
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.metric("Total Trades", len(filtered_df))
        with col2:
            st.metric("Total PnL", f"${total_pnl:,.2f}")
        with col3:
            st.metric("Win Rate", f"{win_rate:.1f}%")
        with col4:
            st.metric("Avg PnL/Trade", f"${avg_pnl:,.2f}")

        st.subheader("💹 Equity Curve")
        fig = px.line(equity_df, x="Time", y="Equity", title="Account Balance Over Time")
        fig.update_layout(hovermode="x unified", yaxis_title="Account Balance ($)", xaxis_title="Date")
        fig.add_hline(y=equity_df["Equity"].iloc[0], line_dash="dash", annotation_text="Starting Balance", line_color="gray")
        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        st.subheader("🧾 Trade Log")
        st.dataframe(
            filtered_df.sort_values("Time", ascending=False),
            use_container_width=True,
            height=600
        )

    with tab3:
        st.subheader("📊 Risk Metrics")
        col1, col2, col3 = st.columns(3)
        col1.metric("Sharpe Ratio", f"{sharpe_ratio:.2f}")
        col2.metric("Max Drawdown", f"{max_drawdown:.2f}%")
        col3.metric("Profit Factor", 
                    f"{filtered_df[filtered_df['PnL'] > 0]['PnL'].sum() / abs(filtered_df[filtered_df['PnL'] < 0]['PnL'].sum()):.2f}" 
                    if filtered_df[filtered_df['PnL'] < 0]['PnL'].sum() != 0 else "∞")
        
        st.subheader("📉 PnL Distribution")
        fig_hist = px.histogram(filtered_df, x="PnL", color="Status", nbins=20,
                                color_discrete_map={"PROFIT": "#10b981", "LOSS": "#ef4444"},
                                marginal="box")
        st.plotly_chart(fig_hist, use_container_width=True)

        st.subheader("📅 Daily Performance")
        daily_pnl = filtered_df.groupby(filtered_df["Time"].dt.date)["PnL"].sum()
        fig_bar = px.bar(daily_pnl, y="PnL", labels={'PnL': 'PnL ($)'}, title="Daily PnL")
        st.plotly_chart(fig_bar, use_container_width=True)

    with tab4:
        st.subheader("🧮 Open Positions Overview")
        open_trades = df.copy()
        open_trades["Real-Time PnL"] = open_trades.apply(
            lambda row: (current_price - row["Price"]) if row["Action"] == "BUY" 
                        else (row["Price"] - current_price), axis=1)

        st.dataframe(
            open_trades[["Time", "Symbol", "Action", "Price", "Real-Time PnL"]].sort_values("Time", ascending=False),
            use_container_width=True
        )
        total_open_pnl = open_trades["Real-Time PnL"].sum()
        pnl_color = "🟢" if total_open_pnl >= 0 else "🔴"
        st.metric("Total Real-Time PnL", f"{pnl_color} ${total_open_pnl:.2f}")

else:
    st.warning("⚠️ No trading data available")
    st.image("https://via.placeholder.com/800x400?text=No+trades+yet", use_container_width=True)
    st.info("Please ensure the trading bot is running and data is being collected.")

# === Footer ===
st.markdown("---")
st.markdown(f"<small>Dashboard last updated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small>", unsafe_allow_html=True)
