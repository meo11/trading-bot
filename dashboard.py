import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import numpy as np
import os
import requests
from io import StringIO
from streamlit_autorefresh import st_autorefresh  # <=== NEW IMPORT

# === Page Config ===
st.set_page_config(
    page_title="US30 Trading Bot Dashboard", 
    layout="wide", 
    page_icon="📈",
    initial_sidebar_state="expanded"
)

# === Custom Styling ===
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
    .stTabs [data-baseweb="tab-list"] {
        gap: 10px;
    }
    .stTabs [data-baseweb="tab"] {
        padding: 8px 16px;
        border-radius: 8px 8px 0 0;
    }
    .stTabs [aria-selected="true"] {
        background-color: #1e293b;
        color: white;
    }
    @media (max-width: 768px) {
        .metric-card { padding: 10px; }
    }
</style>
""", unsafe_allow_html=True)

# === Remote File URLs ===
BASE_URL = "https://trading-bot-1-e2rp.onrender.com"
TRADES_URL = f"{BASE_URL}/download/trades"
EQUITY_URL = f"{BASE_URL}/download/equity"

# === Data Loading Function ===
@st.cache_data(ttl=300)  # Cache for 5 minutes
def load_data(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        return pd.read_csv(StringIO(response.text))
    except Exception as e:
        st.error(f"Failed to load data from {url}: {e}")
        return None

# === Main Title ===
st.title("📈 US30 Trading Bot Dashboard")
st.caption(f"Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")  # NEW: Timestamp under title

# === Sidebar Filters ===
with st.sidebar:
    st.subheader("🔍 Filters")
    # Load minimal first to avoid crash if file missing
    df_preview = load_data(TRADES_URL)
    
    if df_preview is not None and not df_preview.empty:
        min_date = pd.to_datetime(df_preview["Time"]).min().date()
        max_date = pd.to_datetime(df_preview["Time"]).max().date()
    else:
        min_date = max_date = datetime.now().date()

    date_range = st.date_input(
        "Select Date Range",
        value=[min_date, max_date],
        min_value=min_date,
        max_value=max_date
    )

    selected_actions = st.multiselect(
        "Select Trade Type",
        options=["BUY", "SELL"],
        default=["BUY", "SELL"]
    )

    # === Sidebar Refresh Control ===
    st.subheader("🔄 Refresh Mode")
    refresh_mode = st.radio(
        "Choose refresh mode:",
        options=["Auto", "Manual"],
        index=0,
        horizontal=True
    )

    if refresh_mode == "Auto":
        st_autorefresh(interval=10000, key="auto_refresh")  # every 10 sec
    elif refresh_mode == "Manual":
        if st.button("🔁 Refresh Now"):
            st.cache_data.clear()
            st.rerun()

# === Load main data ===
df = load_data(TRADES_URL)
equity_df = load_data(EQUITY_URL)

if df is not None and equity_df is not None:
    # Data Processing
    df["Time"] = pd.to_datetime(df["Time"])
    df["Price"] = df["Price"].astype(float)
    equity_df["Time"] = pd.to_datetime(equity_df["Time"])
    
    current_price = df["Price"].iloc[-1]
    df["PnL"] = df.apply(lambda row:
        (current_price - row["Price"]) if row["Action"] == "BUY" 
        else (row["Price"] - current_price),
        axis=1
    )
    df["PnL_Percentage"] = df["PnL"] / df["Price"] * 100
    df["Status"] = df["PnL"].apply(lambda x: "PROFIT" if x >= 0 else "LOSS")
    
    total_pnl = df["PnL"].sum()
    win_rate = len(df[df["PnL"] > 0]) / len(df[df["PnL"] != 0]) * 100 if len(df[df["PnL"] != 0]) > 0 else 0
    avg_pnl = total_pnl / len(df[df["PnL"] != 0]) if len(df[df["PnL"] != 0]) > 0 else 0
    
    returns = df[df["PnL"] != 0]["PnL"] / df[df["PnL"] != 0]["Price"]
    sharpe_ratio = (returns.mean() / returns.std()) * np.sqrt(252) if len(returns) > 1 else 0
    max_drawdown = (equity_df["Equity"].max() - equity_df["Equity"].min()) / equity_df["Equity"].max() * 100
    
    # Filter
    filtered_df = df[
        (df["Time"].dt.date >= date_range[0]) &
        (df["Time"].dt.date <= date_range[1]) &
        (df["Action"].isin(selected_actions))
    ]

    # === Summary Metrics ===
    st.subheader("📊 Performance Overview")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Trades", len(filtered_df))
    col2.metric("Total PnL", f"${total_pnl:.2f}")
    col3.metric("Win Rate", f"{win_rate:.1f}%")
    col4.metric("Avg PnL/Trade", f"${avg_pnl:.2f}")

    # === Tabs ===
    tab1, tab2, tab3 = st.tabs(["📈 Overview", "📜 Trade Log", "📊 Advanced Metrics"])

    with tab1:
        st.subheader("💹 Equity Curve")
        fig = px.line(equity_df, x="Time", y="Equity", title="Account Balance Over Time")
        fig.update_layout(hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        st.subheader("🧾 Trade Log")
        st.dataframe(filtered_df.sort_values("Time", ascending=False), use_container_width=True)

    with tab3:
        st.subheader("📊 Risk Metrics")
        col1, col2, col3 = st.columns(3)
        col1.metric("Sharpe Ratio", f"{sharpe_ratio:.2f}")
        col2.metric("Max Drawdown", f"{max_drawdown:.2f}%")
        col3.metric("Profit Factor", 
            f"{filtered_df[filtered_df['PnL'] > 0]['PnL'].sum() / abs(filtered_df[filtered_df['PnL'] < 0]['PnL'].sum()):.2f}" 
            if filtered_df[filtered_df['PnL'] < 0]['PnL'].sum() != 0 else "∞")

else:
    st.warning("⚠️ No trading data available.")
    st.info("Make sure your trading bot is running and sending trades!")

# === Footer ===
st.markdown("---")
st.caption(f"Dashboard last updated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")