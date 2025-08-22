from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import os, csv, math, uuid, requests, re
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.positions as positions
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth
from time import time

# === Initialize ===
app = Flask(__name__)
CORS(app)
load_dotenv()

# === Execution Mode & Global Switch ===
LOCAL_TEST = os.getenv("LOCAL_TEST", "true").lower() == "true"
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() == "true"

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

# === Discord Notify ===
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

def discord_notify(msg: str, level: str = "info", extra: dict | None = None):
    """Fire-and-forget Discord webhook; safely no-op if not set."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        color = 0x58a6ff if level == "info" else 0xf39c12 if level == "warn" else 0xe74c3c
        embeds = [{
            "title": f"[{level.upper()}] Trading Bot",
            "description": msg[:1900],
            "color": color,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "fields": [{"name": k, "value": f"`{v}`", "inline": True} for k, v in (extra or {}).items()]
        }]
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": embeds}, timeout=5)
    except Exception:
        pass

# === Risk Controls (env) ===
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "0.50"))     # hard cap (percent of master balance)
MAX_UNITS    = int(os.getenv("MAX_UNITS", "300000"))        # absolute units cap
MASTER_START_BAL = float(os.getenv("MASTER_START_BAL", "1000000"))

TRADING_TZ = os.getenv("TRADING_TZ", "America/Halifax")
TRADING_WINDOW = os.getenv("TRADING_WINDOW", "").strip()    # e.g., "Mon-Fri 06:00-20:00"
DAILY_LOSS_STOP_PCT = float(os.getenv("DAILY_LOSS_STOP_PCT", "1.5"))

CAPS_RAW = os.getenv("SYMBOL_RISK_CAPS", "")
SYMBOL_RISK_CAPS = {}
if CAPS_RAW:
    for kv in CAPS_RAW.split(","):
        if ":" in kv:
            k, v = kv.split(":", 1)
            try:
                SYMBOL_RISK_CAPS[k.strip().upper()] = float(v)
            except:
                pass

SYMBOL_ALLOW = [s.strip().upper() for s in os.getenv("SYMBOL_ALLOWLIST", "US30,NAS100,XAUUSD,EURUSD").split(",") if s.strip()]

# NEW: concurrency & min SL env
MAX_CONCURRENT_GLOBAL = int(os.getenv("MAX_CONCURRENT_GLOBAL", "3"))
MAX_CONCURRENT_PER_SYMBOL = int(os.getenv("MAX_CONCURRENT_PER_SYMBOL", "2"))
MIN_SL_RULES_RAW = os.getenv("MIN_SL_RULES", "").strip()   # e.g., "US30:50pts,XAUUSD:5pts,EURUSD:10pips"

def parse_min_sl_rules(raw: str):
    """
    Parse rules like 'US30:50pts,XAUUSD:5pts,EURUSD:10pips'
    Returns dict: { "US30": {"qty":50, "kind":"points"}, "EURUSD":{"qty":10,"kind":"pips"} }
    """
    out = {}
    if not raw:
        return out
    for item in raw.split(","):
        if ":" not in item:
            continue
        sym, rest = item.split(":", 1)
        sym = sym.strip().upper()
        rest = rest.strip().lower()
        if rest.endswith("pips"):
            try:
                out[sym] = {"qty": float(rest.replace("pips","")), "kind": "pips"}
            except: pass
        elif rest.endswith("pts") or rest.endswith("points"):
            try:
                out[sym] = {"qty": float(re.sub(r"(pts|points)$","", rest)), "kind": "points"}
            except: pass
        elif rest.endswith("price"):
            try:
                out[sym] = {"qty": float(rest.replace("price","")), "kind": "price"}
            except: pass
    return out

MIN_SL_RULES = parse_min_sl_rules(MIN_SL_RULES_RAW)

# === Symbol metadata & aliases (TradingView -> OANDA) ===
SYMBOL_META = {
    "EUR_USD":   {"aliases": ["EURUSD", "FX:EURUSD", "OANDA:EURUSD"],             "kind": "fx",    "pip": 0.0001},
    "GBP_USD":   {"aliases": ["GBPUSD", "FX:GBPUSD", "OANDA:GBPUSD"],             "kind": "fx",    "pip": 0.0001},
    "USD_JPY":   {"aliases": ["USDJPY", "FX:USDJPY", "OANDA:USDJPY"],             "kind": "fx",    "pip": 0.01  },
    "XAU_USD":   {"aliases": ["XAUUSD","GOLD","OANDA:XAUUSD","FOREXCOM:XAUUSD"],  "kind": "metal", "point": 0.1},
    "US30_USD":  {"aliases": ["US30","US30USD","OANDA:US30USD","DJI","US30CASH"], "kind": "index", "point": 1.0},
    "NAS100_USD":{"aliases": ["NAS100","US100","NAS100USD","OANDA:NAS100USD"],    "kind": "index", "point": 1.0},
}
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

# === OANDA Balance Cache ===
_balance_cache = {"val": None, "ts": 0}

def get_oanda_balance():
    """Fetch live OANDA balance (cached ~15s). Fallback to MASTER_START_BAL if fails."""
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
        _balance_cache["ts"] = now
        return bal
    except Exception:
        return MASTER_START_BAL

# === OANDA Open Positions (for concurrency) ===
_pos_cache = {"by_symbol": {}, "ts": 0}

def get_open_positions_counts():
    """
    Returns (global_count, per_symbol_counts) using OANDA OpenPositions.
    Safe fallback to zeros on failure.
    """
    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
        return 0, {}
    now = time()
    try:
        # light cache (5s)
        if _pos_cache["by_symbol"] and now - _pos_cache["ts"] < 5:
            d = _pos_cache["by_symbol"]
            return sum(d.values()), dict(d)

        client = oandapyV20.API(access_token=OANDA_TOKEN, environment="practice")
        req = positions.OpenPositions(accountID=OANDA_ACCOUNT_ID)
        resp = client.request(req)
        per = {}
        for p in resp.get("positions", []):
            inst = p.get("instrument")
            # Count as 1 if long or short units non-zero
            long_units  = float(p.get("long", {}).get("units", "0"))
            short_units = float(p.get("short", {}).get("units", "0"))
            if abs(long_units) > 0 or abs(short_units) > 0:
                per[inst] = per.get(inst, 0) + 1
        _pos_cache["by_symbol"] = per
        _pos_cache["ts"] = now
        return sum(per.values()), per
    except Exception:
        return 0, {}

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

# === Timezone helpers & daily loss / trading window ===
def tznow():
    return datetime.now(ZoneInfo(TRADING_TZ))

def today_key():
    return tznow().strftime("%Y-%m-%d")

def start_of_day_equity():
    """Return first equity row for today (in TRADING_TZ). Fallback to last known, else starting."""
    if not os.path.exists(equity_file):
        return starting_equity
    sod = None
    with open(equity_file, "r") as f:
        rows = list(csv.reader(f))
    for r in rows[1:]:
        try:
            t = datetime.fromisoformat(r[0].replace("Z", ""))
            t_local = t.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(TRADING_TZ))
            if t_local.date() == tznow().date():
                sod = float(r[1]); break
        except:
            continue
    if sod is not None:
        return sod
    try:
        return float(rows[-1][1])
    except:
        return starting_equity

def latest_equity():
    if not os.path.exists(equity_file):
        return starting_equity
    with open(equity_file, "r") as f:
        rows = list(csv.reader(f))
    try:
        return float(rows[-1][1])
    except:
        return starting_equity

def day_loss_pct():
    sod = start_of_day_equity()
    cur = latest_equity()
    if sod <= 0:
        return 0.0
    return max(0.0, (sod - cur) / sod * 100.0)

def in_trading_window() -> bool:
    if not TRADING_WINDOW:
        return True
    # e.g., "Mon-Fri 06:00-20:00"
    m = re.match(r"(?i)^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:-(Mon|Tue|Wed|Thu|Fri|Sat|Sun))?\s+(\d{2}:\d{2})-(\d{2}:\d{2})\s*$", TRADING_WINDOW)
    if not m:
        return True
    start_day, end_day, start_hhmm, end_hhmm = m.groups()
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    s_idx = days.index(start_day[:3].title())
    e_idx = days.index((end_day or start_day)[:3].title())
    today_idx = tznow().weekday()  # Mon=0

    if s_idx <= e_idx:
        allowed = set(range(s_idx, e_idx+1))
    else:
        allowed = set(list(range(s_idx,7)) + list(range(0,e_idx+1)))

    if today_idx not in allowed:
        return False

    hh, mm = map(int, start_hhmm.split(":"))
    st = dtime(hh, mm)
    hh, mm = map(int, end_hhmm.split(":"))
    et = dtime(hh, mm)
    nowt = tznow().time()
    return st <= nowt <= et

# === Sizing & risk clamp ===
def clamp_units(units: int) -> int:
    return max(1, min(int(units), MAX_UNITS))

def clamp_risk(tv_symbol: str, requested_pct: float) -> float:
    pct = min(float(requested_pct or 0.0), MAX_RISK_PCT)
    cap = SYMBOL_RISK_CAPS.get((tv_symbol or "").upper())
    if cap is not None:
        pct = min(pct, cap)
    return max(0.0, pct)

def size_for_risk(tv_symbol, balance, risk_pct, entry, sl_price):
    """
    Very simple risk model using price distance to SL:
    risk_dollars ≈ units * abs(entry - SL)  =>  units ≈ risk_dollars / price_delta
    """
    applied_pct = clamp_risk(tv_symbol, risk_pct)
    if not sl_price or sl_price == entry:
        return clamp_units(1), applied_pct
    price_delta = abs(entry - sl_price)
    if price_delta <= 0:
        return clamp_units(1), applied_pct
    risk_dollars = float(balance) * (applied_pct / 100.0)
    units = risk_dollars / price_delta
    return clamp_units(math.floor(units)), applied_pct

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
        "side": side,
        "orderType": "MARKET",
        "units": units,
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
        "TRADING_TZ": TRADING_TZ,
        "TRADING_WINDOW": TRADING_WINDOW,
        "DAILY_LOSS_STOP_PCT": DAILY_LOSS_STOP_PCT,
        "SYMBOL_RISK_CAPS": SYMBOL_RISK_CAPS,
        "SYMBOL_ALLOWLIST": SYMBOL_ALLOW,
        "MAX_CONCURRENT_GLOBAL": MAX_CONCURRENT_GLOBAL,
        "MAX_CONCURRENT_PER_SYMBOL": MAX_CONCURRENT_PER_SYMBOL,
        "MIN_SL_RULES": MIN_SL_RULES,
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID,
        "OANDA_TOKEN": mask(OANDA_TOKEN),
        "DUPLIKIUM_BASE": DUP_BASE,
        "DUPLIKIUM_USER": DUP_USER,
        "DUPLIKIUM_TOKEN": mask(DUP_TOKEN),
        "DUPLIKIUM_AUTH_STYLE": DUP_AUTH,
        "DUPLIKIUM_ORDERS_PATH": DUP_PATH,
        "DISCORD_WEBHOOK_SET": bool(DISCORD_WEBHOOK_URL),
    })

@app.route('/risk-status')
def risk_status():
    bal = get_oanda_balance()
    g_count, per_counts = get_open_positions_counts()
    return jsonify({
        "oanda_balance": bal,
        "start_of_day_equity": start_of_day_equity(),
        "latest_equity": latest_equity(),
        "day_loss_pct": round(day_loss_pct(), 4),
        "trading_window": TRADING_WINDOW or "(none)",
        "in_trading_window_now": in_trading_window(),
        "open_positions_total": g_count,
        "open_positions_by_instrument": per_counts,
        "TRADING_ENABLED": TRADING_ENABLED,
        "MAX_RISK_PCT": MAX_RISK_PCT,
        "MAX_UNITS": MAX_UNITS,
        "SYMBOL_RISK_CAPS": SYMBOL_RISK_CAPS,
        "SYMBOL_ALLOWLIST": SYMBOL_ALLOW,
        "MAX_CONCURRENT_GLOBAL": MAX_CONCURRENT_GLOBAL,
        "MAX_CONCURRENT_PER_SYMBOL": MAX_CONCURRENT_PER_SYMBOL,
        "MIN_SL_RULES": MIN_SL_RULES,
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
    risk_req= float(data.get('risk_pct', 0.05))

    instrument = map_symbol(tv_symbol)

    # Min SL enforcement (dryrun)
    min_rule = MIN_SL_RULES.get(tv_symbol)
    if min_rule and sl_q is not None:
        req_delta = to_price_delta(instrument, min_rule["qty"], min_rule["kind"])
        got_delta = to_price_delta(instrument, sl_q, sl_type)
        if got_delta < req_delta:
            return jsonify({'status':'error','message':f'Min SL for {tv_symbol} is {min_rule["qty"]}{min_rule["kind"]}, provided is smaller'}), 400

    sl_delta = to_price_delta(instrument, sl_q, sl_type)
    tp_delta = to_price_delta(instrument, tp_q, tp_type)
    sl_p = price - sl_delta if side == "BUY" else price + sl_delta
    tp_p = price + tp_delta if side == "BUY" else price - tp_delta

    balance = get_oanda_balance()
    units, risk_applied = size_for_risk(tv_symbol, balance, risk_req, price, sl_p)

    # Concurrency preview
    g_count, per_counts = get_open_positions_counts()
    per_count = per_counts.get(instrument, 0)

    return jsonify({
        "side": side,
        "tv_symbol": tv_symbol,
        "instrument": instrument,
        "entry": price,
        "slPrice": sl_p, "tpPrice": tp_p,
        "risk_pct_requested": risk_req,
        "risk_pct_applied": risk_applied,
        "units": units,
        "open_positions_total": g_count,
        "open_positions_for_instrument": per_count,
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

        # --- hard guards ---
        if not TRADING_ENABLED:
            save_trade_to_csv(tv_symbol, side, price, order_id)  # still log
            discord_notify("Trade skipped: TRADING_ENABLED=false",
                           "warn", {"symbol": tv_symbol, "price": price, "order_id": order_id})
            return jsonify({'status': 'skipped', 'reason': 'TRADING_ENABLED=false'}), 200

        if not in_trading_window():
            discord_notify("Trade blocked: outside trading window",
                           "warn", {"symbol": tv_symbol, "price": price})
            return jsonify({'status': 'error', 'message': 'Outside TRADING_WINDOW'}), 403

        dlp = day_loss_pct()
        if DAILY_LOSS_STOP_PCT > 0 and dlp >= DAILY_LOSS_STOP_PCT:
            discord_notify("Trade blocked: daily loss stop hit",
                           "error", {"day_loss_pct": dlp, "limit": DAILY_LOSS_STOP_PCT})
            return jsonify({'status':'error','message':f'Daily loss stop hit ({dlp:.2f}% >= {DAILY_LOSS_STOP_PCT:.2f}%)'}), 403

        # Concurrency guard
        instrument = map_symbol(tv_symbol)
        g_count, per_counts = get_open_positions_counts()
        per_count = per_counts.get(instrument, 0)
        if MAX_CONCURRENT_GLOBAL and g_count >= MAX_CONCURRENT_GLOBAL:
            discord_notify("Trade blocked: max concurrent (global) reached",
                           "warn", {"open_total": g_count, "limit": MAX_CONCURRENT_GLOBAL})
            return jsonify({'status':'error','message': f'Max concurrent trades (global) reached: {g_count}/{MAX_CONCURRENT_GLOBAL}'}), 403
        if MAX_CONCURRENT_PER_SYMBOL and per_count >= MAX_CONCURRENT_PER_SYMBOL:
            discord_notify("Trade blocked: max concurrent (per symbol) reached",
                           "warn", {"instrument": instrument, "open_for_symbol": per_count, "limit": MAX_CONCURRENT_PER_SYMBOL})
            return jsonify({'status':'error','message': f'Max concurrent trades for {instrument} reached: {per_count}/{MAX_CONCURRENT_PER_SYMBOL}'}), 403

        # optional SL/TP & risk from payload
        sl_type = (data.get('sl_type') or "points")
        tp_type = (data.get('tp_type') or "points")
        sl_q  = float(data['sl']) if 'sl' in data and data['sl'] is not None else None
        tp_q  = float(data['tp']) if 'tp' in data and data['tp'] is not None else None
        risk_req = float(data.get('risk_pct', 0.05))

        # Min SL enforcement
        min_rule = MIN_SL_RULES.get(tv_symbol)
        if min_rule and sl_q is not None:
            req_delta = to_price_delta(instrument, min_rule["qty"], min_rule["kind"])
            got_delta = to_price_delta(instrument, sl_q, sl_type)
            if got_delta < req_delta:
                discord_notify("Trade blocked: SL below minimum",
                               "warn", {"symbol": tv_symbol, "min": f'{min_rule["qty"]}{min_rule["kind"]}', "got": f'{sl_q}{sl_type}'})
                return jsonify({'status':'error','message':f'Min SL for {tv_symbol} is {min_rule["qty"]}{min_rule["kind"]}, provided is smaller'}), 400

        # compute SL/TP prices via deltas
        sl_p = None; tp_p = None
        if sl_q is not None:
            d = to_price_delta(instrument, sl_q, sl_type)
            sl_p = price - d if side == "BUY" else price + d
        if tp_q is not None:
            d = to_price_delta(instrument, tp_q, tp_type)
            tp_p = price + d if side == "BUY" else price - d

        # risk sizing (live balance) — NOTE: risk clamp happens here
        balance = get_oanda_balance()
        units, risk_applied = size_for_risk(tv_symbol, balance, risk_req, price, sl_p)

        # log locally
        save_trade_to_csv(tv_symbol, side, price, order_id)
        simulate_equity(price, side)

        # execute
        oanda_ok, oanda_msg = place_oanda_order(instrument, side, units=units)
        dup_ok, dup_status, dup_msg = forward_to_duplikium(MASTER_SRC, instrument, side, price, sl_p, tp_p, units)

        status = 'ok' if (oanda_ok and dup_ok) else 'partial' if (oanda_ok or dup_ok) else 'error'

        if status != 'error':
            discord_notify("Trade executed" if status=='ok' else "Trade partially executed",
                           "info", {"symbol": tv_symbol, "side": side, "units": units,
                                    "risk_applied%": risk_applied, "oanda": oanda_ok, "duplikium": dup_ok})

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
            'slPrice': sl_p, 'tpPrice': tp_p,
            'risk_pct_requested': risk_req,
            'risk_pct_applied': risk_applied,
            'day_loss_pct': round(dlp, 3),
            'open_positions_total': g_count,
            'open_positions_for_instrument': per_count
        }), 200 if status != 'error' else 500

    except Exception as e:
        discord_notify(f"Webhook error: {e}", "error")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# === Run (local dev) ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
