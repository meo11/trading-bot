import requests
import time

# === CONFIG ===
WEBHOOK_URL = "https://trading-bot-1-e2rp.onrender.com/webhook"  # <-- Your live Flask server

# === Define Trade Details ===
trade_data = {
    "symbol": "US30",             # Instrument you are trading
    "action": "BUY",              # "BUY" or "SELL"
    "price": 34600.0,             # Example: current price
    "order_id": f"BOT_{int(time.time())}"  # Unique ID based on timestamp
}

# === Send Trade ===
def send_trade(data):
    try:
        response = requests.post(WEBHOOK_URL, json=data)
        if response.status_code == 200:
            print("✅ Trade sent successfully!")
            print(response.json())
        else:
            print(f"⚠️ Error: {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"❌ Failed to send trade: {e}")

# === MAIN ===
if __name__ == "__main__":
    send_trade(trade_data)