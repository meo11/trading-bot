from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime
import os
import csv

app = Flask(__name__)
CORS(app)

# === CONFIG ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # Base directory
starting_equity = 10000
trade_log_file = os.path.join(BASE_DIR, "simulated_trades.csv")
equity_file = os.path.join(BASE_DIR, "equity_curve.csv")
current_equity = starting_equity

# === Initialize files if not exist ===
if not os.path.exists(trade_log_file):
    with open(trade_log_file, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Symbol", "Action", "Price", "Order ID", "Time"])

if not os.path.exists(equity_file):
    with open(equity_file, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Time", "Equity"])
        writer.writerow([datetime.now().isoformat(), starting_equity])

# === ROUTES ===
@app.route('/')
def home():
    return "✅ Trade Execution Simulation Server is running"

@app.route('/download/trades')
def download_trades():
    if not os.path.exists(trade_log_file):
        return jsonify({'status': 'error', 'message': 'Trades file not found'}), 404
    return send_file(trade_log_file, as_attachment=True)

@app.route('/download/equity')
def download_equity():
    if not os.path.exists(equity_file):
        return jsonify({'status': 'error', 'message': 'Equity file not found'}), 404
    return send_file(equity_file, as_attachment=True)

@app.route('/debug/files')
def debug_files():
    return jsonify({
        "files": os.listdir(BASE_DIR),
        "equity_file_exists": os.path.exists(equity_file),
        "trades_file_exists": os.path.exists(trade_log_file)
    })

# === Save Trade ===
def save_trade_to_csv(symbol, action, price, order_id):
    with open(trade_log_file, mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([symbol, action.upper(), price, order_id, datetime.now().isoformat()])

# === Save Equity ===
def save_equity_to_csv(equity):
    with open(equity_file, mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([datetime.now().isoformat(), equity])

# === Simulate Equity Change ===
def simulate_equity(price, action):
    global current_equity
    last_trade = None

    if os.path.exists(trade_log_file):
        with open(trade_log_file, "r") as file:
            rows = list(csv.reader(file))
            if len(rows) > 1:
                last_trade = rows[-1]

    if last_trade and last_trade[1] in ["BUY", "SELL"]:
        entry_price = float(last_trade[2])
        direction = 1 if last_trade[1] == "BUY" else -1
        pnl = (float(price) - entry_price) * direction
        current_equity += pnl
        save_equity_to_csv(current_equity)

# === WEBHOOK ===
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    try:
        symbol = data['symbol']
        action = data['action']
        price = float(data['price'])
        order_id = data['order_id']

        save_trade_to_csv(symbol, action, price, order_id)
        simulate_equity(price, action)

        return jsonify({'status': 'ok', 'message': 'Trade recorded and equity updated'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# === START SERVER ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
