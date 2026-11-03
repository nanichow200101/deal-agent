"""
Deal Agent - checks coupon pages every 15 minutes and alerts on big discounts.

Run:
    pip install -r requirements.txt
    python deal_agent.py            # runs forever, checks every 15 minutes
    python deal_agent.py --once     # one check only (use with cron / GitHub Actions)

Optional Telegram alerts (set these environment variables):
    TELEGRAM_BOT_TOKEN   (create a bot via @BotFather)
    TELEGRAM_CHAT_ID     (your chat id, get it from @userinfobot)
"""

import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

import requests
import schedule
from bs4 import BeautifulSoup

# ---------------- SETTINGS (edit these) ----------------
MIN_DISCOUNT = 50          # alert only if discount >= this percent
CHECK_EVERY_MINUTES = 15
HYDERABAD_PINCODE = "500001"

# Coupon aggregator pages (the apps themselves have no public coupon pages).
# If a page returns 404, open the site in a browser, find the right URL and replace it.
SOURCES = {
    "Amazon":    "https://www.grabon.in/amazon-coupons/",
    "Flipkart":  "https://www.grabon.in/flipkart-coupons/",
    "Myntra":    "https://www.grabon.in/myntra-coupons/",
    "Ajio":      "https://www.grabon.in/ajio-coupons/",
    "Zepto":     "https://www.grabon.in/zepto-coupons/",
    "Blinkit":   "https://www.grabon.in/blinkit-coupons/",
    "Instamart": "https://www.grabon.in/swiggy-instamart-coupons/",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}

SEEN_FILE = "seen_deals.json"
LOG_FILE = "deals_log.csv"
# -------------------------------------------------------

PERCENT_RE = re.compile(r"(\d{1,3})\s?%\s?(?:off|discount|cashback)?", re.I)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


def fetch_deals(store, url):
    """Download a page and pull out text snippets that mention a % discount."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[{store}] could not fetch: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    deals = []
    # Generic approach: look at list items, headings, paragraphs and links.
    for tag in soup.find_all(["li", "h2", "h3", "h4", "p", "a", "div"]):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if not (15 < len(text) < 200):
            continue
        percents = [int(p) for p in PERCENT_RE.findall(text) if 0 < int(p) <= 100]
        if not percents:
            continue
        deals.append({
            "store": store,
            "discount": max(percents),
            "text": text,
            "is_gift_card": "gift card" in text.lower(),
            "url": url,
        })
    return deals


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"Telegram error: {e}")


def log_deal(deal):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "store", "discount_percent", "gift_card", "text", "url"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), deal["store"],
                    deal["discount"], deal["is_gift_card"], deal["text"], deal["url"]])


def run_check():
    print(f"\n=== Check at {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    seen = load_seen()
    found = 0

    for store, url in SOURCES.items():
        for deal in fetch_deals(store, url):
            if deal["discount"] < MIN_DISCOUNT:
                continue
            key = hashlib.md5(f"{store}|{deal['text']}".encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            found += 1
            log_deal(deal)
            tag = "GIFT CARD " if deal["is_gift_card"] else ""
            msg = (f"{tag}{deal['discount']}% OFF - {store}\n{deal['text']}\n"
                   f"{deal['url']}\n(Verify on the official site; pincode {HYDERABAD_PINCODE})")
            print(msg, "\n")
            send_telegram(msg)
        time.sleep(3)  # be polite, don't hammer the sites

    save_seen(seen)
    print(f"New deals >= {MIN_DISCOUNT}%: {found}")


if __name__ == "__main__":
    run_check()
    if "--once" in sys.argv:
        sys.exit(0)
    schedule.every(CHECK_EVERY_MINUTES).minutes.do(run_check)
    print(f"Scheduled every {CHECK_EVERY_MINUTES} minutes. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(1)
