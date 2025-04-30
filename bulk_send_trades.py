import requests
import random
import time
from datetime import datetime

# === CONFIG ===
WEBHOOK_URL = "https://trading-bot-1-e2rp.onrender.com/webhook"  # Your Render server
NUM_TRADES = 10  # How many trades to send
START_PRICE = 34500  # Starting price
MAX_MOVE = 50  # Maximum price move per trade
DELAY = 10  # Seconds between sending each trade (adjust as needed)

# === SEND TRADE FUNCTION ===
def send_trade(action, price, order_id):
    payload = {
        "symbol": "US30",
        "action": action,
        "price": price,
        "order_id": order_id
    }
    response = requests.post(WEBHOOK_URL, json=payload)
    if response.status_code == 200:
        print(f"✅ Sent {action} at ${price}")
    else:
        print(f"❌ Failed to send trade: {response.text}")

# === SIMULATE MULTIPLE TRADES ===
def simulate_trades():
    current_price = START_PRICE
    for i in range(NUM_TRADES):
        action = random.choice(["BUY", "SELL"])
        move = random.uniform(-MAX_MOVE, MAX_MOVE)
        current_price += move
        current_price = max(current_price, 1)  # Prevent negative prices
        order_id = f"BOT_{datetime.now().strftime('%Y%m%d%H%M%S')}_{i}"
        send_trade(action, round(current_price, 2), order_id)
        time.sleep(DELAY)

# === MAIN ===
if __name__ == "__main__":
    simulate_trades()