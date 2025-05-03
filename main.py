from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime
import os
import csv
import oandapyV20
import oandapyV20.endpoints.orders as orders
from dotenv import load_dotenv

# === Initialize ===
app = Flask(__name__)
CORS(app)
load_dotenv()

# === OANDA Config ===
OANDA_TOKEN = os.getenv("OANDA_TOKEN")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")

# === Local Sim Config ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
starting_equity = 10000
trade_log_file = os.path.join(BASE_DIR, "simulated_trades.csv")
equity_file = os.path.join(BASE_DIR, "equity_curve.csv")
current_equity = starting_equity

# === File Setup ===
if not os.path.exists(trade_log_file):
    with open(trade_log_file, "w", newline="") as file:
        csv.writer(file).writerow(["Symbol", "Action", "Price", "Order ID", "Time"])

if not os.path.exists(equity_file):
    with open(equity_file, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Time", "Equity"])
        writer.writerow([datetime.now().isoformat(), starting_equity])

# === Routes ===
@app.route('/')
def home():
    return "✅ Trade Execution Server is running"

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

# === Core Functions ===
def save_trade_to_csv(symbol, action, price, order_id):
    with open(trade_log_file, "a", newline="") as file:
        csv.writer(file).writerow([symbol, action.upper(), price, order_id, datetime.now().isoformat()])

def save_equity_to_csv(equity):
    with open(equity_file, "a", newline="") as file:
        csv.writer(file).writerow([datetime.now().isoformat(), equity])

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

def place_oanda_order(symbol, action):
    try:
        client = oandapyV20.API(access_token=OANDA_TOKEN)
        data = {
            "order": {
                "instrument": symbol,
                "units": "1" if action.upper() == "BUY" else "-1",
                "type": "MARKET",
                "positionFill": "DEFAULT"
            }
        }
        r = orders.OrderCreate(accountID=OANDA_ACCOUNT_ID, data=data)
        client.request(r)
        return True, "✅ OANDA order placed successfully"
    except Exception as e:
        return False, f"❌ OANDA order failed: {str(e)}"

# === Webhook Route ===
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

        success, msg = place_oanda_order(symbol, action)
        return jsonify({'status': 'ok' if success else 'error', 'message': msg}), 200 if success else 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# === Run ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)