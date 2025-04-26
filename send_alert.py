import requests
import json
from datetime import datetime

# Replace with your actual Ngrok URL
WEBHOOK_URL = "https://fb08-142-67-224-32.ngrok-free.app/webhook"

# Build the alert message
payload = {
    "symbol": "US30",
    "action": "BUY",  # or "SELL"
    "price": 39125.0,
    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
}

# Send POST request
response = requests.post(WEBHOOK_URL, headers={"Content-Type": "application/json"}, data=json.dumps(payload))

# Show the result
print(f"Status Code: {response.status_code}")
print("Response:", response.text)