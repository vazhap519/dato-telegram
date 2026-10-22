"""Read-only diagnostic of Tribute Shop API endpoints.

No orders, charges, or changes to Tribute are made.
The key is entered hidden and is never printed or saved.
"""
import getpass
import json
import urllib.error
import urllib.request

BASE = "https://tribute.tg/api/v1/"

def inspect(payload):
    if isinstance(payload, list):
        print("API returned a list of shops; count:", len(payload))
        for shop in payload[:3]:
            if isinstance(shop, dict):
                report_shop(shop)
        return
    if isinstance(payload, dict):
        if "status" in payload or "recurrent" in payload or "onlyStars" in payload:
            report_shop(payload)
            return
        for field in ("shops", "data", "items"):
            if isinstance(payload.get(field), list):
                inspect(payload[field])
                return
    print("API response received, but shop status fields were not recognized.")

def report_shop(shop):
    status = shop.get("status")
    print("Shop status (1=active, 0=inactive):", status if status in (0, 1) else "not returned")
    print("Recurring enabled:", shop.get("recurrent") if isinstance(shop.get("recurrent"), bool) else "not returned")
    print("Stars-only:", shop.get("onlyStars") if isinstance(shop.get("onlyStars"), bool) else "not returned")
    callback = shop.get("callbackUrl")
    print("Webhook configured:", bool(isinstance(callback, str) and callback.startswith("https://")))
    print("Webhook targets our server:", bool(isinstance(callback, str) and callback.startswith("https://bot.arutyun.me/")))

def main():
    key = getpass.getpass("Tribute API key (hidden): ").strip()
    if not key:
        print("Key not provided.")
        return
    for suffix in ("shop", "shops"):
        req = urllib.request.Request(
            BASE + suffix,
            headers={"Api-Key": key, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read(131072).decode("utf-8"))
                print(f"GET /api/v1/{suffix}: HTTP {response.status}")
                inspect(payload)
        except urllib.error.HTTPError as error:
            print(f"GET /api/v1/{suffix}: HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            print(f"GET /api/v1/{suffix}: {type(error).__name__}")
    print("Read-only check completed; no orders or payments created.")

if __name__ == "__main__":
    main()
