from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime, timezone
import os, csv, math, uuid, requests
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.accounts as accounts
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth
from time import time

# === Initialize ===
app = Flask(__name__)
CORS(app)
load_dotenv()

# === Execution Mode & Guards ===
LOCAL_TEST = os.getenv("LOCAL_TEST", "true").lower() == "true"          # local testing toggle
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() == "true"  # global kill switch

# === OANDA Config ===
OANDA_TOKEN = os.getenv("OANDA_TOKEN")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
FORWARD_TO_OANDA = os.getenv("FORWARD_TO_OANDA", "true").lower() == "true"

# === Duplikium Config ===
DUP_BASE   = (os.getenv("DUPLIKIUM_BASE") or "").rstrip("/")
DUP_USER   = os.getenv("DUPLIKIUM_USER") or ""
DUP_TOKEN  = os.getenv("DUPLIKIUM_TOKEN") or ""
DUP_AUTH   = os.getenv("DUPLIKIUM_AUTH_STYLE", "headers").lower()  # headers | bearer | basic | token
DUP_PATH   = os.getenv("DUPLIKIUM_ORDERS_PATH", "/orders")
MASTER_SRC = os.getenv("MASTER_SOURCE", "OANDA_MASTER")
FORWARD_TO_DUP = os.getenv("FORWARD_TO_DUPLIKIUM", "true").lower() == "true"

def dup_headers():
    if DUP_AUTH in ("bearer",):
        return {"Authorization": f"Bearer {DUP_TOKEN}", "Content-Type": "application/json"}
    if DUP_AUTH in ("headers", "token"):
        return {"X-Auth-Username": DUP_USER, "X-Auth-Token": DUP_TOKEN, "Content-Type": "application/json"}
    return {"Content-Type": "application/json"}  # for basic

def dup_auth():
    return HTTPBasicAuth(DUP_USER, DUP_TOKEN) if DUP_AUTH == "basic" else None

# === Risk Controls (env) ===
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "0.50"))     # hard cap, e.g. 0.50 = 0.50%
MAX_UNITS    = int(os.getenv("MAX_UNITS", "300000"))        # cap absolute units (e.g., ~3 lots FX)
MASTER_START_BAL = float(os.getenv("MASTER_START_BAL", "1000000"))  # fallback if balance fetch fails
SYMBOL_ALLOW = [s.strip().upper() for s in os.getenv("SYMBOL_ALLOWLIST", "US30,NAS100,XAUUSD,EURUSD").split(",") if s.strip()]

# === Symbol metadata & aliases (TradingView -> OANDA) ===
SYMBOL_META = {
    "EUR_USD":   {"aliases": ["EURUSD", "FX:EURUSD", "OANDA:EURUSD"],             "kind": "fx",    "pip": 0.0001},
    "GBP_USD":   {"aliases": ["GBPUSD", "FX:GBPUSD", "OANDA:GBPUSD"],             "kind": "fx",    "pip": 0.0001},
    "USD_JPY":   {"aliases": ["USDJPY", "FX:USDJPY", "OANDA:USDJPY"],             "kind": "fx",    "pip": 0.01  },
    "XAU_USD":   {"aliases": ["XAUUSD","GOLD","OANDA:XAUUSD","FOREXCOM:XAUUSD"],  "kind": "metal", "point": 0.1},
    "US30_USD":  {"aliases": ["US30","US30USD","OANDA:US30USD","DJI","US30.CASH"],"kind": "index", "point": 1.0},
    "NAS100_USD":{"aliases": ["NAS100","US100","NAS100USD","OANDA:NAS100USD"],    "kind": "index", "point": 1.0},
}
# optional per-point dollar value if you want to use that sizing style (we default to price-delta sizing)
POINT_VALUE = {"US30": 1.0, "NAS100": 1.0, "XAUUSD": 1.0}

ALIAS_TO_OANDA = {}
for o_sym, meta in SYMBOL_META.items():
    ALIAS_TO_OANDA[o_sym] = o_sym
    for a in meta.get("aliases", []):
        ALIAS_TO_OANDA[a.upper().replace(":", "").replace(".", "")] = o_sym

def map_symbol(tv_symbol: str) -> str:
    raw = (tv_symbol or "").upper().replace(":", "").replace(".", "")
    return ALIAS_TO_OANDA.get(raw, raw)

def to_price_delta(oanda_sym: str, qty: float, unit_type: str) -> float:
    """
    Convert a qty in 'pips' or 'points' to a price delta for the instrument.
    If unit_type == 'price', returns qty as-is.
    """
    unit_type = (unit_type or "").lower()
    if qty is None:
        return 0.0
    if unit_type == "price":
        return float(qty)

    meta = SYMBOL_META.get(oanda_sym, {})
    kind = meta.get("kind")

    if unit_type == "pips":  # FX
        pip = meta.get("pip", 0.0001)
        return float(qty) * pip

    # default treat as "points"
    if kind in ("index", "metal"):
        point = meta.get("point", 1.0)
        return float(qty) * point

    # fallback for fx if "points" sent
    pip = meta.get("pip", 0.0001)
    return float(qty) * pip

# === Live OANDA Balance Cache ===
_balance_cache = {"val": None, "ts": 0}

def get_oanda_balance():
    """Fetch live OANDA balance (cached ~15s). Falls back to MASTER_START_BAL if any read fails."""
    import time as _t
    now = _t.time()
    if _balance_cache["val"] is not None and now - _balance_cache["ts"] < 15:
        return _balance_cache["val"]
    try:
        if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
            raise RuntimeError("Missing OANDA credentials")
        client = oandapyV20.API(access_token=OANDA_TOKEN, environment="practice")
        req = accounts.AccountSummary(accountID=OANDA_ACCOUNT_ID)
        resp = client.request(req)
        acct = resp.get("account", {})
        bal = float(acct.get("NAV", acct.get("balance")))
        if not bal or bal <= 0:
            raise RuntimeError("Invalid balance from OANDA")
        _balance_cache["val"] = bal
        _balance_cache["ts"] = now
        return bal
    except Exception:
        return MASTER_START_BAL

# === Local Sim/Logs ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
starting_equity = 10000
trade_log_file = os.path.join(BASE_DIR, "simulated_trades.csv")
equity_file = os.path.join(BASE_DIR, "equity_curve.csv")
current_equity = starting_equity

if not os.path.exists(trade_log_file):
    with open(trade_log_file, "w", newline="") as f:
        csv.writer(f).writerow(["Symbol", "Action", "Price", "Order ID", "Time"])

if not os.path.exists(equity_file):
    with open(equity_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Time", "Equity"])
        w.writerow([datetime.now().isoformat(), starting_equity])

def save_trade_to_csv(symbol, action, price, order_id):
    with open(trade_log_file, "a", newline="") as f:
        csv.writer(f).writerow([symbol, action.upper(), price, order_id, datetime.now().isoformat()])

def save_equity_to_csv(equity):
    with open(equity_file, "a", newline="") as f:
        csv.writer(f).writerow([datetime.now().isoformat(), equity])

def simulate_equity(price, action):
    global current_equity
    last_trade = None
    if os.path.exists(trade_log_file):
        with open(trade_log_file, "r") as f:
            rows = list(csv.reader(f))
            if len(rows) > 1:
                last_trade = rows[-1]
    if last_trade and last_trade[1] in ["BUY", "SELL"]:
        entry_price = float(last_trade[2])
        direction = 1 if last_trade[1] == "BUY" else -1
        pnl = (float(price) - entry_price) * direction
        current_equity += pnl
        save_equity_to_csv(current_equity)

# === Sizing ===
def clamp_units(units: int) -> int:
    return max(1, min(int(units), MAX_UNITS))

def size_for_risk(tv_symbol, balance, risk_pct, entry, sl_price):
    """
    Very simple risk model using price distance to SL:
    risk_dollars ≈ units * abs(entry - SL)  =>  units ≈ risk_dollars / price_delta
    - clamps risk_pct to MAX_RISK_PCT
    - clamps units to MAX_UNITS
    """
    requested = float(risk_pct or 0.0)
    risk_used = min(requested, MAX_RISK_PCT)
    if not sl_price or sl_price == entry:
        return clamp_units(1)
    price_delta = abs(entry - sl_price)
    if price_delta <= 0:
        return clamp_units(1)
    risk_dollars = float(balance) * (risk_used / 100.0)
    units = risk_dollars / price_delta
    return clamp_units(math.floor(units))

# === OANDA Execution ===
def place_oanda_order(symbol, action, units=1):
    if LOCAL_TEST or not FORWARD_TO_OANDA or not TRADING_ENABLED:
        return True, "OANDA not called (LOCAL_TEST / forwarding disabled / trading disabled)"
    try:
        client = oandapyV20.API(access_token=OANDA_TOKEN, environment="practice")
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

# === Duplikium Forward ===
def forward_to_duplikium(master_source, instrument, side, entry, sl_p, tp_p, units, tag="tv_v1"):
    if LOCAL_TEST or not FORWARD_TO_DUP or not TRADING_ENABLED:
        return True, 200, "Duplikium not called (LOCAL_TEST / forwarding disabled / trading disabled)"
    if not DUP_BASE:
        return False, 400, "DUPLIKIUM_BASE not set"

    payload = {
        "source": master_source,
        "symbol": instrument,
        "side": side,                 # BUY / SELL
        "orderType": "MARKET",
        "units": units,               # master size; slaves scale in Duplikium
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

# === Idempotency (avoid duplicate fills) ===
LAST_SEEN = {}
ID_TTL = 90  # seconds

def seen(order_id: str) -> bool:
    now = time()
    # purge old
    for k, t in list(LAST_SEEN.items()):
        if now - t > ID_TTL:
            LAST_SEEN.pop(k, None)
    if not order_id:
        return False
    if order_id in LAST_SEEN:
        return True
    LAST_SEEN[order_id] = now
    return False

# === Routes ===
@app.route('/')
def home():
    return "✅ Trade Execution Server is running"

@app.route('/env-check')
def env_check():
    def mask(v):
        if not v: return None
        return v[:4] + "..." + v[-4:] if len(v) > 8 else "***"
    return jsonify({
        "LOCAL_TEST": LOCAL_TEST,
        "TRADING_ENABLED": TRADING_ENABLED,
        "MAX_RISK_PCT": MAX_RISK_PCT,
        "MAX_UNITS": MAX_UNITS,
        "MASTER_START_BAL": MASTER_START_BAL,
        "FORWARD_TO_OANDA": FORWARD_TO_OANDA,
        "FORWARD_TO_DUPLIKIUM": FORWARD_TO_DUP,
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID,
        "OANDA_TOKEN": mask(OANDA_TOKEN),
        "DUPLIKIUM_BASE": DUP_BASE,
        "DUPLIKIUM_USER": DUP_USER,
        "DUPLIKIUM_TOKEN": mask(DUP_TOKEN),
        "DUPLIKIUM_AUTH_STYLE": DUP_AUTH,
        "DUPLIKIUM_ORDERS_PATH": DUP_PATH,
        "SYMBOL_ALLOWLIST": SYMBOL_ALLOW
    })

@app.route('/risk-status')
def risk_status():
    bal = get_oanda_balance()
    return jsonify({
        "oanda_balance": bal,
        "MAX_RISK_PCT": MAX_RISK_PCT,
        "MAX_UNITS": MAX_UNITS,
        "SYMBOL_ALLOWLIST": SYMBOL_ALLOW
    })

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

@app.route('/dryrun', methods=['POST'])
def dryrun():
    """Compute mapping, sizing, SL/TP — no orders sent; logs locally."""
    data = request.get_json(silent=True) or {}
    action = (data.get('action') or data.get('signal') or "BUY_SIGNAL").upper()
    side = "BUY" if "BUY" in action else "SELL"
    tv_symbol = (data.get('symbol') or "US30").upper()
    if tv_symbol not in SYMBOL_ALLOW:
        return jsonify({'status': 'error', 'message': f'Symbol {tv_symbol} not allowed'}), 400

    price   = float(data.get('price') or 39250)
    sl_type = data.get('sl_type', 'points')
    sl_q    = float(data.get('sl', 150))
    tp_type = data.get('tp_type', 'points')
    tp_q    = float(data.get('tp', 300))
    risk_pct= float(data.get('risk_pct', 0.05))

    instrument = map_symbol(tv_symbol)
    sl_delta = to_price_delta(instrument, sl_q, sl_type)
    tp_delta = to_price_delta(instrument, tp_q, tp_type)
    sl_p = price - sl_delta if side == "BUY" else price + sl_delta
    tp_p = price + tp_delta if side == "BUY" else price - tp_delta

    balance = get_oanda_balance()
    units = size_for_risk(tv_symbol, balance, risk_pct, price, sl_p)

    return jsonify({
        "side": side,
        "tv_symbol": tv_symbol,
        "instrument": instrument,
        "entry": price,
        "slPrice": sl_p, "tpPrice": tp_p,
        "risk_pct": min(risk_pct, MAX_RISK_PCT),
        "units": units,
        "LOCAL_TEST": LOCAL_TEST,
        "note": "dry run only; nothing sent"
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    try:
        # accept either {action:"BUY"...} or {signal:"BUY_SIGNAL"...}
        action = (data.get('action') or data.get('signal') or "").upper()
        side = "BUY" if "BUY" in action else "SELL" if "SELL" in action else None
        tv_symbol = (data.get('symbol') or "").upper()
        if not side or not tv_symbol:
            return jsonify({'status': 'error', 'message': 'Missing side/symbol'}), 400
        if tv_symbol not in SYMBOL_ALLOW:
            return jsonify({'status': 'error', 'message': f'Symbol {tv_symbol} not allowed'}), 400

        price = float(data.get('price'))
        order_id = data.get('order_id') or f"tv-{uuid.uuid4().hex[:8]}"
        if seen(order_id):
            return jsonify({'status': 'ignored', 'reason': 'duplicate order_id'}), 200
        if not TRADING_ENABLED:
            save_trade_to_csv(tv_symbol, side, price, order_id)  # still log
            return jsonify({'status': 'skipped', 'reason': 'TRADING_ENABLED=false'}), 200

        # optional SL/TP & risk from payload
        sl_type = (data.get('sl_type') or "points")
        tp_type = (data.get('tp_type') or "points")
        sl_q  = float(data['sl']) if 'sl' in data and data['sl'] is not None else None
        tp_q  = float(data['tp']) if 'tp' in data and data['tp'] is not None else None
        risk_pct = float(data.get('risk_pct', 0.05))

        # symbol mapping
        instrument = map_symbol(tv_symbol)

        # compute SL/TP prices via deltas
        sl_p = None; tp_p = None
        if sl_q is not None:
            d = to_price_delta(instrument, sl_q, sl_type)
            sl_p = price - d if side == "BUY" else price + d
        if tp_q is not None:
            d = to_price_delta(instrument, tp_q, tp_type)
            tp_p = price + d if side == "BUY" else price - d

        # risk sizing (live balance)
        balance = get_oanda_balance()
        units = size_for_risk(tv_symbol, balance, risk_pct, price, sl_p)

        # log locally
        save_trade_to_csv(tv_symbol, side, price, order_id)
        simulate_equity(price, side)

        # execute
        oanda_ok, oanda_msg = place_oanda_order(instrument, side, units=units)
        dup_ok, dup_status, dup_msg = forward_to_duplikium(MASTER_SRC, instrument, side, price, sl_p, tp_p, units)

        status = 'ok' if (oanda_ok and dup_ok) else 'partial' if (oanda_ok or dup_ok) else 'error'
        return jsonify({
            'status': status,
            'LOCAL_TEST': LOCAL_TEST,
            'TRADING_ENABLED': TRADING_ENABLED,
            'oanda': oanda_msg,
            'duplikium_status': dup_status,
            'duplikium_msg': dup_msg,
            'sent_units': units,
            'tv_symbol': tv_symbol,
            'instrument': instrument,
            'slPrice': sl_p, 'tpPrice': tp_p
        }), 200 if status != 'error' else 500

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# === Run (local dev) ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
