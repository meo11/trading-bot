# main.py
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime, timezone, date
import os, csv, math, uuid, requests, json, pytz
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

# ========== Execution Mode & Global Guards ==========
LOCAL_TEST        = os.getenv("LOCAL_TEST", "true").lower() == "true"           # dry-switch inside server (also overridable per route)
TRADING_ENABLED   = os.getenv("TRADING_ENABLED", "true").lower() == "true"      # global kill switch

# Daily loss halt: percent of start-of-day NAV (e.g., 1.5 = 1.5%)
DAILY_LOSS_STOP_PCT = float(os.getenv("DAILY_LOSS_STOP_PCT", "0"))              # 0 disables
TRADING_TZ          = os.getenv("TRADING_TZ", "America/Halifax")                # timezone for daily starts & windows
TRADING_WINDOW      = os.getenv("TRADING_WINDOW", "").strip()                   # e.g., "09:30-16:00" (local to TRADING_TZ); empty = disabled

# Discord notifications
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_WEBHOOK_SET = os.getenv("DISCORD_WEBHOOK_SET", "false").lower() == "true" or bool(DISCORD_WEBHOOK_URL)

# ========== OANDA Config ==========
OANDA_TOKEN      = os.getenv("OANDA_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
FORWARD_TO_OANDA = os.getenv("FORWARD_TO_OANDA", "true").lower() == "true"

# ========== Duplikium Config ==========
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
    return {"Content-Type": "application/json"}  # for "basic", handled via auth()

def dup_auth():
    return HTTPBasicAuth(DUP_USER, DUP_TOKEN) if DUP_AUTH == "basic" else None

# ========== Risk Config ==========
MAX_RISK_PCT          = float(os.getenv("MAX_RISK_PCT", "0.50"))     # hard cap, e.g. 0.50% per trade
MAX_UNITS             = int(os.getenv("MAX_UNITS", "300000"))        # absolute ceiling
MASTER_START_BAL      = float(os.getenv("MASTER_START_BAL", "1000000"))

# Allowlist (uppercased TV symbols)
SYMBOL_ALLOW = [s.strip().upper() for s in os.getenv("SYMBOL_ALLOWLIST", "US30,NAS100,XAUUSD,EURUSD").split(",") if s.strip()]

# Per-symbol unit caps, JSON or comma form:
# Example JSON: {"EURUSD": 100000, "XAUUSD": 5000, "US30": 5}
def _parse_symbol_caps(raw: str):
    if not raw:
        return {}
    raw = raw.strip()
    try:
        # Try JSON first
        obj = json.loads(raw)
        return {k.upper(): int(v) for k, v in obj.items()}
    except Exception:
        pass
    # Fallback: "EURUSD:100000, XAUUSD:5000"
    caps = {}
    for part in raw.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            k = k.strip().upper()
            try:
                caps[k] = int(v.strip())
            except Exception:
                pass
    return caps

SYMBOL_RISK_CAPS = _parse_symbol_caps(os.getenv("SYMBOL_RISK_CAPS", ""))

# ========== Symbol metadata & aliases (TradingView -> OANDA) ==========
SYMBOL_META = {
    "EUR_USD":   {"aliases": ["EURUSD", "FX:EURUSD", "OANDA:EURUSD"],             "kind": "fx",    "pip": 0.0001},
    "GBP_USD":   {"aliases": ["GBPUSD", "FX:GBPUSD", "OANDA:GBPUSD"],             "kind": "fx",    "pip": 0.0001},
    "USD_JPY":   {"aliases": ["USDJPY", "FX:USDJPY", "OANDA:USDJPY"],             "kind": "fx",    "pip": 0.01  },
    "XAU_USD":   {"aliases": ["XAUUSD","GOLD","OANDA:XAUUSD","FOREXCOM:XAUUSD"],  "kind": "metal", "point": 0.1},
    "US30_USD":  {"aliases": ["US30","US30USD","OANDA:US30USD","DJI","US30.CASH"],"kind": "index", "point": 1.0},
    "NAS100_USD":{"aliases": ["NAS100","US100","NAS100USD","OANDA:NAS100USD"],    "kind": "index", "point": 1.0},
}

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

# ========== Live OANDA Balance Cache ==========
_balance_cache = {"val": None, "ts": 0}

def get_oanda_balance():
    """Fetch live OANDA NAV (cached ~15s). Falls back to MASTER_START_BAL if any read fails."""
    now = time()
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
        _balance_cache["ts"]  = now
        return bal
    except Exception:
        return MASTER_START_BAL

# ========== Local Sim/Logs ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
starting_equity = 10000
trade_log_file = os.path.join(BASE_DIR, "simulated_trades.csv")
equity_file = os.path.join(BASE_DIR, "equity_curve.csv")
daily_nav_file = os.path.join(BASE_DIR, "daily_nav.json")
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

# ========== Trading Window & Daily-Loss Guard ==========
def trading_window_ok(now_utc: datetime) -> bool:
    """If TRADING_WINDOW set (e.g. '09:30-16:00'), enforce local time window in TRADING_TZ."""
    if not TRADING_WINDOW:
        return True
    try:
        tz = pytz.timezone(TRADING_TZ)
        local_now = now_utc.astimezone(tz)
        start_s, end_s = TRADING_WINDOW.split("-", 1)
        start_h, start_m = [int(x) for x in start_s.split(":")]
        end_h, end_m     = [int(x) for x in end_s.split(":")]
        start_dt = local_now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end_dt   = local_now.replace(hour=end_h,   minute=end_m,   second=0, microsecond=0)
        return start_dt <= local_now <= end_dt
    except Exception:
        return True  # fail open if window parsing fails

def _read_daily_nav():
    try:
        with open(daily_nav_file, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _write_daily_nav(obj):
    try:
        with open(daily_nav_file, "w") as f:
            json.dump(obj, f)
    except Exception:
        pass

def daily_loss_guard(now_utc: datetime) -> (bool, dict):
    """
    Freeze trading if current NAV has dropped below (1 - DAILY_LOSS_STOP_PCT%) of start-of-day NAV.
    Returns (ok, info_dict).
    """
    if DAILY_LOSS_STOP_PCT <= 0:
        return True, {"enabled": False}
    tz = pytz.timezone(TRADING_TZ)
    local_today = now_utc.astimezone(tz).date().isoformat()

    data = _read_daily_nav()
    sod = data.get("start_nav", None)
    sod_date = data.get("date", None)

    nav_now = get_oanda_balance()

    # reset SOD on new local date or missing record
    if (sod is None) or (sod_date != local_today):
        data = {"date": local_today, "start_nav": nav_now}
        _write_daily_nav(data)
        return True, {"enabled": True, "start_nav": nav_now, "nav_now": nav_now, "drawdown_pct": 0.0, "limit_pct": DAILY_LOSS_STOP_PCT}

    # check drawdown
    start_nav = float(data["start_nav"])
    if start_nav <= 0:
        return True, {"enabled": True, "start_nav": start_nav, "nav_now": nav_now, "drawdown_pct": 0.0, "limit_pct": DAILY_LOSS_STOP_PCT}
    dd_pct = max(0.0, (start_nav - nav_now) / start_nav * 100.0)
    ok = dd_pct < DAILY_LOSS_STOP_PCT
    return ok, {"enabled": True, "start_nav": start_nav, "nav_now": nav_now, "drawdown_pct": round(dd_pct, 4), "limit_pct": DAILY_LOSS_STOP_PCT}

# ========== Sizing ==========
def clamp_units(units: int) -> int:
    return max(1, min(int(units), MAX_UNITS))

def apply_symbol_cap(units: int, tv_symbol: str) -> int:
    cap = SYMBOL_RISK_CAPS.get(tv_symbol.upper())
    if cap is None:
        return units
    return max(1, min(int(units), int(cap)))

def size_for_risk(tv_symbol, balance, risk_pct, entry, sl_price):
    """
    Very simple risk model using price distance to SL:
    risk_dollars ≈ units * abs(entry - SL)  =>  units ≈ risk_dollars / price_delta
    - clamps risk_pct to MAX_RISK_PCT
    - clamps units to MAX_UNITS
    - clamps units to SYMBOL_RISK_CAPS[tv_symbol] if provided
    """
    requested = float(risk_pct or 0.0)
    risk_used = min(requested, MAX_RISK_PCT)
    if not sl_price or sl_price == entry:
        base = clamp_units(1)
        return apply_symbol_cap(base, tv_symbol)

    price_delta = abs(entry - sl_price)
    if price_delta <= 0:
        base = clamp_units(1)
        return apply_symbol_cap(base, tv_symbol)

    risk_dollars = float(balance) * (risk_used / 100.0)
    units = math.floor(risk_dollars / price_delta)
    units = clamp_units(units)
    units = apply_symbol_cap(units, tv_symbol)
    return units

# ========== OANDA Execution ==========
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

# ========== Duplikium Forward ==========
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
        "comment": f"TV->{master_source} {datetime.now(timezone.utc).isoformat()}"
    }

    url = f"{DUP_BASE}{DUP_PATH}"
    try:
        resp = requests.post(url, json=payload, headers=dup_headers(), auth=dup_auth(), timeout=12)
        return resp.ok, resp.status_code, resp.text
    except Exception as e:
        return False, 500, str(e)

# ========== Discord Notify ==========
def notify_discord(title: str, fields: dict, color: int = 0x2ecc71):
    """Post a clean embed to Discord (no-op if not configured)."""
    if not DISCORD_WEBHOOK_SET or not DISCORD_WEBHOOK_URL:
        return False, "discord disabled"
    try:
        embed_fields = [{"name": k, "value": str(v), "inline": True} for k, v in fields.items()]
        payload = {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": embed_fields,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=8)
        return r.ok, r.text
    except Exception as e:
        return False, str(e)

# ========== Idempotency (avoid duplicate fills) ==========
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

# ========== Routes ==========
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
        "DAILY_LOSS_STOP_PCT": DAILY_LOSS_STOP_PCT,
        "TRADING_TZ": TRADING_TZ,
        "TRADING_WINDOW": TRADING_WINDOW,
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
        "SYMBOL_ALLOWLIST": SYMBOL_ALLOW,
        "SYMBOL_RISK_CAPS": SYMBOL_RISK_CAPS,
        "DISCORD_WEBHOOK_SET": DISCORD_WEBHOOK_SET
    })

@app.route('/risk-status')
def risk_status():
    bal = get_oanda_balance()
    now_utc = datetime.now(timezone.utc)
    tw_ok = trading_window_ok(now_utc)
    dl_ok, dl_info = daily_loss_guard(now_utc)
    return jsonify({
        "oanda_balance": bal,
        "MAX_RISK_PCT": MAX_RISK_PCT,
        "MAX_UNITS": MAX_UNITS,
        "SYMBOL_ALLOWLIST": SYMBOL_ALLOW,
        "SYMBOL_RISK_CAPS": SYMBOL_RISK_CAPS,
        "trading_window_ok": tw_ok,
        "daily_loss_ok": dl_ok,
        "daily_loss_info": dl_info
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
        "trades_file_exists": os.path.exists(trade_log_file),
        "daily_nav_file_exists": os.path.exists(daily_nav_file)
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

    # guards snapshot
    now_utc = datetime.now(timezone.utc)
    tw_ok = trading_window_ok(now_utc)
    dl_ok, dl_info = daily_loss_guard(now_utc)

    return jsonify({
        "side": side,
        "tv_symbol": tv_symbol,
        "instrument": instrument,
        "entry": price,
        "slPrice": sl_p, "tpPrice": tp_p,
        "risk_pct_requested": risk_pct,
        "risk_pct_applied": min(risk_pct, MAX_RISK_PCT),
        "units": units,
        "LOCAL_TEST": LOCAL_TEST,
        "trading_window_ok": tw_ok,
        "daily_loss_ok": dl_ok,
        "daily_loss_info": dl_info,
        "note": "dry run only; nothing sent",
        "open_positions_for_instrument": 0,
        "open_positions_total": 0
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

        # Idempotency
        if seen(order_id):
            return jsonify({'status': 'ignored', 'reason': 'duplicate order_id'}), 200

        # Trading window & daily drawdown guard
        now_utc = datetime.now(timezone.utc)
        if not trading_window_ok(now_utc):
            save_trade_to_csv(tv_symbol, side, price, order_id)  # still log
            notify_discord("⛔ Blocked by Trading Window", {
                "symbol": tv_symbol, "side": side, "price": price, "order_id": order_id
            }, color=0xe67e22)
            return jsonify({'status': 'skipped', 'reason': 'outside trading window'}), 200

        dl_ok, dl_info = daily_loss_guard(now_utc)
        if not dl_ok:
            save_trade_to_csv(tv_symbol, side, price, order_id)
            notify_discord("⛔ Blocked by Daily Loss Stop", {
                "symbol": tv_symbol, "side": side, "price": price, **dl_info
            }, color=0xe74c3c)
            return jsonify({'status': 'skipped', 'reason': 'daily loss stop hit', 'daily_loss_info': dl_info}), 200

        if not TRADING_ENABLED:
            save_trade_to_csv(tv_symbol, side, price, order_id)
            notify_discord("⚠️ Trading Disabled", {
                "symbol": tv_symbol, "side": side, "price": price, "order_id": order_id
            }, color=0xf1c40f)
            return jsonify({'status': 'skipped', 'reason': 'TRADING_ENABLED=false'}), 200

        # optional SL/TP & risk from payload
        sl_type = (data.get('sl_type') or "points")
        tp_type = (data.get('tp_type') or "points")
        sl_q  = float(data['sl']) if 'sl' in data and data['sl'] is not None else None
        tp_q  = float(data['tp']) if 'tp' in data and data['tp'] is not None else None
        risk_pct_req = float(data.get('risk_pct', 0.05))
        risk_pct_applied = min(risk_pct_req, MAX_RISK_PCT)

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
        units = size_for_risk(tv_symbol, balance, risk_pct_applied, price, sl_p)

        # log locally
        save_trade_to_csv(tv_symbol, side, price, order_id)
        simulate_equity(price, side)

        # execute
        oanda_ok, oanda_msg = place_oanda_order(instrument, side, units=units)
        dup_ok, dup_status, dup_msg = forward_to_duplikium(MASTER_SRC, instrument, side, price, sl_p, tp_p, units)

        status = 'ok' if (oanda_ok and dup_ok) else 'partial' if (oanda_ok or dup_ok) else 'error'

        # Discord notify
        notify_discord(
            "📈 New Signal" if status != 'error' else "❌ Execution Error",
            {
                "status": status,
                "symbol": tv_symbol,
                "instrument": instrument,
                "side": side,
                "price": price,
                "units": units,
                "risk_pct": risk_pct_applied,
                "sl": sl_p, "tp": tp_p,
                "oanda": oanda_msg,
                "duplikium_status": dup_status,
            },
            color=(0x2ecc71 if status == 'ok' else 0xf39c12 if status == 'partial' else 0xe74c3c)
        )

        return jsonify({
            'status': status,
            'LOCAL_TEST': LOCAL_TEST,
            'TRADING_ENABLED': TRADING_ENABLED,
            'risk_pct_applied': risk_pct_applied,
            'oanda': oanda_msg,
            'duplikium_status': dup_status,
            'duplikium_msg': dup_msg,
            'sent_units': units,
            'tv_symbol': tv_symbol,
            'instrument': instrument,
            'slPrice': sl_p, 'tpPrice': tp_p
        }), 200 if status != 'error' else 500

    except Exception as e:
        notify_discord("❌ Webhook Exception", {"error": str(e)}, color=0xe74c3c)
        return jsonify({'status': 'error', 'message': str(e)}), 500

# === Run (local dev) ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
