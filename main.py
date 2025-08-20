from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime
import os, csv, math, uuid, requests
import oandapyV20
import oandapyV20.endpoints.orders as orders
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

# === Initialize ===
app = Flask(__name__)
CORS(app)
load_dotenv()

# === OANDA Config ===
OANDA_TOKEN = os.getenv("OANDA_TOKEN")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
FORWARD_TO_OANDA = os.getenv("FORWARD_TO_OANDA", "true").lower() == "true"

# === Duplikium Config ===
DUP_BASE   = (os.getenv("DUPLIKIUM_BASE") or "").rstrip("/")
DUP_USER   = os.getenv("DUPLIKIUM_USER") or ""
DUP_TOKEN  = os.getenv("DUPLIKIUM_TOKEN") or ""
DUP_AUTH   = os.getenv("DUPLIKIUM_AUTH_STYLE", "headers").lower()  # headers | bearer | basic
DUP_PATH   = os.getenv("DUPLIKIUM_ORDERS_PATH", "/orders")
MASTER_SRC = os.getenv("MASTER_SOURCE", "OANDA_MASTER")
FORWARD_TO_DUP = os.getenv("FORWARD_TO_DUPLIKIUM", "true").lower() == "true"

def dup_headers():
    if DUP_AUTH == "bearer":
        return {"Authorization": f"Bearer {DUP_TOKEN}", "Content-Type": "application/json"}
    if DUP_AUTH == "headers":
        return {"X-Auth-Username": DUP_USER, "X-Auth-Token": DUP_TOKEN, "Content-Type": "application/json"}
    return {"Content-Type": "application/json"}  # for basic

def dup_auth():
    return HTTPBasicAuth(DUP_USER, DUP_TOKEN) if DUP_AUTH == "basic" else None

# === Symbol mapping & risk config (tweak to your broker naming) ===
SYMBOL_MAP = {
    "US30": "US30_USD",
    "NAS100": "NAS100_USD",
    "XAUUSD": "XAU_USD",
}
# $ per point for master risk sizing (adjust to your broker’s economics)
POINT_VALUE = {"US30": 1.0, "NAS100": 1.0, "XAUUSD": 1.0}
MASTER_START_BAL = float(os.getenv("MASTER_START_BAL", "1000000"))  # fallback for sizing

# === Local Sim/Logs ===
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

# === Helpers ===
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

def place_oanda_order(symbol, action, units=1):
    if not FORWARD_TO_OANDA:
        return True, "OANDA forwarding disabled"
    try:
        client = oandapyV20.API(access_token=OANDA_TOKEN)
        data = {
            "order": {
                "instrument": symbol,
                "units": str(units if action.upper() == "BUY" else -units),
                "type": "MARKET",
                "positionFill": "DEFAULT"
            }
        }
        r = orders.OrderCreate(accountID=OANDA_ACCOUNT_ID, data=data)
        client.request(r)
        return True, "✅ OANDA order placed"
    except Exception as e:
        return False, f"❌ OANDA order failed: {str(e)}"

def compute_sl_tp(entry, side, sl_type, sl, tp_type, tp):
    sl_p = None; tp_p = None
    if sl is not None:
        if str(sl_type).lower() == "price":
            sl_p = float(sl)
        else:
            sl_p = entry - sl if side == "BUY" else entry + sl
    if tp is not None:
        if str(tp_type).lower() == "price":
            tp_p = float(tp)
        else:
            tp_p = entry + tp if side == "BUY" else entry - tp
    return sl_p, tp_p

def size_for_risk(tv_symbol, balance, risk_pct, entry, sl_price):
    # very simple per-point risk; refine as needed
    if not sl_price or sl_price == entry:
        return 1
    risk_dollars = balance * (risk_pct / 100.0)
    pts = abs(entry - sl_price)
    dollars_per_pt = POINT_VALUE.get(tv_symbol.upper(), 1.0)
    units = risk_dollars / (pts * dollars_per_pt)
    return max(1, int(math.floor(units)))

def forward_to_duplikium(master_source, instrument, side, entry, sl_p, tp_p, units, tag="tv_v1"):
    if not FORWARD_TO_DUP:
        return True, 200, "Duplikium forwarding disabled"

    if not DUP_BASE:
        return False, 400, "DUPLIKIUM_BASE not set"

    payload = {
        "source": master_source,
        "symbol": instrument,
        "side": side,                 # BUY / SELL
        "orderType": "MARKET",
        "units": units,               # size on master; slaves scale via Duplikium rules
        "entryPrice": entry,
        "slPrice": sl_p,
        "tpPrice": tp_p,
        "clientOrderId": f"{tag}-{uuid.uuid4().hex[:8]}",
        "comment": f"TV->{master_source} {datetime.utcnow().isoformat()}"
    }

    url = f"{DUP_BASE}{DUP_PATH}"
    try:
        resp = requests.post(url, json=payload, headers=dup_headers(), auth=dup_auth(), timeout=10)
        return resp.ok, resp.status_code, resp.text
    except Exception as e:
        return False, 500, str(e)

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

# === Webhook Route ===
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    try:
        # accept either {action:"BUY"...} or {signal:"BUY_SIGNAL"...}
        action = (data.get('action') or data.get('signal') or "").upper()
        side = "BUY" if "BUY" in action else "SELL" if "SELL" in action else None
        symbol_tv = (data.get('symbol') or "").replace(".","").upper()
        price = float(data.get('price'))
        order_id = data.get('order_id') or f"tv-{uuid.uuid4().hex[:8]}"

        if not side or not symbol_tv:
            return jsonify({'status': 'error', 'message': 'Missing side/symbol'}), 400

        # optional SL/TP & risk from payload
        sl_type = data.get('sl_type')         # "price" | "points"
        sl_val  = float(data['sl']) if 'sl' in data and data['sl'] is not None else None
        tp_type = data.get('tp_type')
        tp_val  = float(data['tp']) if 'tp' in data and data['tp'] is not None else None
        risk_pct= float(data.get('risk_pct', 0.5))

        # map to broker instrument for master
        instrument = SYMBOL_MAP.get(symbol_tv, symbol_tv)

        # compute SL/TP prices & simple master sizing
        sl_p, tp_p = compute_sl_tp(price, side, sl_type, sl_val, tp_type, tp_val)
        units = size_for_risk(symbol_tv, MASTER_START_BAL, risk_pct, price, sl_p)

        # log locally (for dashboard)
        save_trade_to_csv(symbol_tv, side, price, order_id)
        simulate_equity(price, side)

        # place on OANDA master (optional)
        oanda_ok, oanda_msg = True, "OANDA skipped"
        if FORWARD_TO_OANDA:
            oanda_ok, oanda_msg = place_oanda_order(instrument, side, units=units)

        # forward to Duplikium (recommended)
        dup_ok, dup_status, dup_msg = forward_to_duplikium(MASTER_SRC, instrument, side, price, sl_p, tp_p, units)

        status = 'ok' if (oanda_ok and dup_ok) else 'partial' if (oanda_ok or dup_ok) else 'error'
        return jsonify({
            'status': status,
            'oanda': oanda_msg,
            'duplikium_status': dup_status,
            'duplikium_msg': dup_msg,
            'sent_units': units,
            'instrument': instrument,
            'slPrice': sl_p, 'tpPrice': tp_p
        }), 200 if status != 'error' else 500

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# === Run ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)