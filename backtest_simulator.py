import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime
import csv
import os

# === CONFIGURATION ===
DATA_PATH = "historical_us30.csv"
OUTPUT_PATH = "backtest_trades.csv"
EQUITY_CURVE_PATH = "equity_curve.csv"
PERFORMANCE_REPORT_PATH = "performance_report.txt"
COMMISSION = 0.5   # $ per trade
SLIPPAGE = 0.5      # points
CONTRACT_SIZE = 1
QUARTER_GAP = 250
BREAK_CONFIRMATION = 50
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5
TP_MULTIPLIER = 3.0
INITIAL_BALANCE = 10000

# === STRATEGY ===
class QuarterPointStrategy:
    def __init__(self):
        self.position = None
        self.entry_price = None
        self.stop_loss = None
        self.take_profit = None
        self.trade_id = 0
        self.trade_log = []

    def generate_signals(self, df):
        df['ema'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['atr'] = df['High'].rolling(ATR_PERIOD).max() - df['Low'].rolling(ATR_PERIOD).min()

        for i in range(ATR_PERIOD, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            base = np.floor(row['Close'] / QUARTER_GAP) * QUARTER_GAP
            quarter_up = base + QUARTER_GAP
            quarter_down = base

            price = row['Close']
            atr = row['atr']
            ema = row['ema']

            long_condition = (
                price > quarter_up + BREAK_CONFIRMATION and
                prev['Close'] > quarter_up + BREAK_CONFIRMATION and
                price > ema and
                self.position != 'LONG'
            )

            short_condition = (
                price < quarter_down - BREAK_CONFIRMATION and
                prev['Close'] < quarter_down - BREAK_CONFIRMATION and
                price < ema and
                self.position != 'SHORT'
            )

            if long_condition:
                if self.position == 'SHORT':
                    self._exit_trade(row, 'EXIT_SHORT')
                self._enter_trade(row, 'BUY', atr)
                self.position = 'LONG'

            elif short_condition:
                if self.position == 'LONG':
                    self._exit_trade(row, 'EXIT_LONG')
                self._enter_trade(row, 'SELL', atr)
                self.position = 'SHORT'

        return self.trade_log

    def _enter_trade(self, row, action, atr):
        self.trade_id += 1
        entry_price = row['Close'] + (SLIPPAGE if action == 'BUY' else -SLIPPAGE)
        stop = entry_price - ATR_MULTIPLIER * atr if action == 'BUY' else entry_price + ATR_MULTIPLIER * atr
        target = entry_price + TP_MULTIPLIER * atr if action == 'BUY' else entry_price - TP_MULTIPLIER * atr

        self.entry_price = entry_price
        self.stop_loss = stop
        self.take_profit = target

        self.trade_log.append({
            "Time": row['Time'],
            "Symbol": "US30",
            "Action": action,
            "Price": entry_price,
            "Quantity": CONTRACT_SIZE,
            "Order ID": f"US30_{self.trade_id}",
            "Commission": COMMISSION,
            "StopLoss": stop,
            "TakeProfit": target
        })

    def _exit_trade(self, row, action):
        exit_price = row['Close'] + (SLIPPAGE if action == 'EXIT_SHORT' else -SLIPPAGE)

        self.trade_log.append({
            "Time": row['Time'],
            "Symbol": "US30",
            "Action": action,
            "Price": exit_price,
            "Quantity": CONTRACT_SIZE,
            "Order ID": f"US30_{self.trade_id}_exit",
            "Commission": COMMISSION
        })

        self.position = None
        self.entry_price = None
        self.stop_loss = None
        self.take_profit = None

# === ANALYSIS ===
def analyze_performance(trades):
    df = pd.DataFrame(trades)
    df['PnL'] = 0.0
    equity_curve = []
    balance = INITIAL_BALANCE
    peak = INITIAL_BALANCE
    max_drawdown = 0.0

    position = None
    entry_price = None

    for i in range(len(df)):
        row = df.iloc[i]
        if row['Action'] in ['BUY', 'SELL']:
            position = row['Action']
            entry_price = row['Price']
        elif row['Action'] in ['EXIT_LONG', 'EXIT_SHORT'] and entry_price is not None:
            exit_price = row['Price']
            pnl = (exit_price - entry_price if position == 'BUY' else entry_price - exit_price) * CONTRACT_SIZE - 2 * COMMISSION
            df.at[i, 'PnL'] = pnl
            balance += pnl
            equity_curve.append((row['Time'], balance))

            peak = max(peak, balance)
            drawdown = (peak - balance) / peak * 100
            max_drawdown = max(max_drawdown, drawdown)

            position = None
            entry_price = None

    df['Cumulative PnL'] = df['PnL'].cumsum()
    pd.DataFrame(equity_curve, columns=["Time", "Equity"]).to_csv(EQUITY_CURVE_PATH, index=False)
    df.to_csv(OUTPUT_PATH, index=False)

    # Save performance report
    total_pnl = df['PnL'].sum()
    win_rate = len(df[df['PnL'] > 0]) / len(df[df['PnL'] != 0]) * 100 if len(df[df['PnL'] != 0]) else 0
    avg_pnl = total_pnl / len(df[df['PnL'] != 0]) if len(df[df['PnL'] != 0]) else 0
    report = f"""
=== PERFORMANCE REPORT ===
Initial Capital: ${INITIAL_BALANCE:,.2f}
Final Balance: ${balance:,.2f}
Net Profit: ${total_pnl:,.2f} ({(total_pnl / INITIAL_BALANCE * 100):.2f}%)
Total Trades: {len(df)}
Winning Trades: {len(df[df['PnL'] > 0])}
Losing Trades: {len(df[df['PnL'] < 0])}
Win Rate: {win_rate:.2f}%
Average PnL per Trade: ${avg_pnl:.2f}
Max Drawdown: {max_drawdown:.2f}%
"""
    print(report)
    with open(PERFORMANCE_REPORT_PATH, 'w') as f:
        f.write(report)

    # Plot
    equity_df = pd.DataFrame(equity_curve, columns=["Time", "Equity"])
    equity_df['Time'] = pd.to_datetime(equity_df['Time'])
    plt.plot(equity_df['Time'], equity_df['Equity'])
    plt.title("Equity Curve")
    plt.grid(True)
    plt.tight_layout()
    plt.show()

# === MAIN ===
if __name__ == '__main__':
    if not os.path.exists(DATA_PATH):
        print("Error: historical_us30.csv not found")
        exit()

    df = pd.read_csv(DATA_PATH)
    df['Time'] = pd.to_datetime(df['Time'])

    strategy = QuarterPointStrategy()
    trades = strategy.generate_signals(df)

    if trades:
        print(f"✅ {len(trades)} trades generated. Analyzing...")
        analyze_performance(trades)
    else:
        print("⚠️ No trades generated during backtest period")
