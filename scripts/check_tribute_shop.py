"""Read-only Tribute Shop API capability check.

Run on the server using:
  .venv/bin/python -m scripts.check_tribute_shop

The API key is typed into a hidden prompt; it is never printed or persisted.
"""
import getpass
import json
import urllib.error
import urllib.request

ENDPOINT = "https://tribute.tg/api/v1/shop"

def main():
    key = getpass.getpass("Tribute Shop API key (hidden): ").strip()
    if not key:
        print("No key provided. Aborted.")
        return
    req = urllib.request.Request(
        ENDPOINT,
        headers={"Api-Key": key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read(131072).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        labels = {401: "Unauthorized: check the API key", 404: "Shop not found or not enabled"}
        print(f"Tribute Shop API HTTP {exc.code}: {labels.get(exc.code, 'API error')}")
        return
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"Unable to check Tribute Shop API: {type(exc).__name__}")
        return
    finally:
        key = None
    if not isinstance(payload, dict):
        print("Unexpected API response type.")
        return
    status = payload.get("status")
    recurrent = payload.get("recurrent")
    only_stars = payload.get("onlyStars")
    callback = payload.get("callbackUrl")
    print("Tribute Shop API connection: OK")
    print("Shop active (status=1):", status == 1)
    print("Recurring payments enabled:", recurrent is True)
    print("Stars-only mode:", only_stars is True)
    print("Webhook configured:", isinstance(callback, str) and callback.startswith("https://"))
    print("Webhook target is our server:", isinstance(callback, str) and callback.startswith("https://bot.arutyun.me/"))
    print("No charge or order was created.")

if __name__ == "__main__":
    main()
