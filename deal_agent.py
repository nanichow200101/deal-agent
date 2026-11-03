"""
Deal Agent v2 - reads public Telegram deal channels, keeps only deals from
official store links in your categories, and forwards them to YOUR Telegram bot.

Run:
    pip install -r requirements.txt
    python deal_agent.py            # runs forever, checks every 15 minutes
    python deal_agent.py --once     # one check only (used by GitHub Actions)

Needs environment variables (GitHub: Settings > Secrets > Actions):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
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
# Public channel usernames only (the part after t.me/).
CHANNELS = ["freekart_deals", "iamprasadtech"]

MIN_DISCOUNT = 50              # alert if discount >= this percent
INCLUDE_UNKNOWN_DISCOUNT = True  # also alert on "loot / lowest price" posts with no % shown
MAX_ALERTS_PER_RUN = 10        # avoids a flood on the first run
CHECK_EVERY_MINUTES = 15

# Only links from these official stores/short links count.
OFFICIAL_DOMAINS = [
    "amzn.to", "amzn.in", "amazon.in",
    "fkrt.cc", "fkrt.it", "fkrt.site", "flipkart.com", "dl.flipkart.com",
    "myntr.it", "myntra.com",
    "ajiio.in", "ajio.com",
    "zeptonow.com", "zepto.com",
    "blinkit.com",
    "swiggy.com", "swiggy.onelink.me",
]

CATEGORIES = {
    "Electronics": ["phone", "iphone", "laptop", "tv", "television", "earbuds", "headphone",
                    "headphones", "router", "smartwatch", "watch", "charger", "camera",
                    "speaker", "soundbar", "monitor", "power bank", "tablet", "keyboard",
                    "mouse", "ssd", "pendrive", "bluetooth", "trimmer", "ups"],
    "Clothing": ["shirt", "t-shirt", "tshirt", "jeans", "kurta", "dress", "jacket",
                 "hoodie", "saree", "trouser", "top", "lehenga", "shorts", "clothing"],
    "Kitchen & utensils": ["kitchen", "cookware", "pan", "kadai", "utensil", "mixer",
                           "grinder", "bottle", "container", "cooker", "lunch box",
                           "tawa", "dinner set", "casserole", "knife"],
    "Home appliances": ["refrigerator", "fridge", "washing machine", "air conditioner",
                        "split ac", "ac", "microwave", "geyser", "fan", "cooler",
                        "vacuum", "air fryer", "iron", "heater", "water purifier",
                        "chimney", "induction"],
    "Toys": ["toy", "toys", "lego", "doll", "puzzle", "board game", "rc car", "kids game"],
    "Sportswear": ["sportswear", "track pant", "tracksuit", "jersey", "gym wear", "sports bra"],
    "Sports & games kits": ["cricket", "bat", "football", "badminton", "yoga mat", "dumbbell",
                            "gym", "treadmill", "cycle", "racket", "shuttle", "skipping",
                            "carrom", "chess", "sports kit"],
    "Tools": ["tool", "tools", "drill", "screwdriver", "toolkit", "wrench", "spanner",
              "hammer", "plier"],
    "Footwear": ["shoe", "shoes", "sneaker", "sneakers", "sandal", "sandals", "slipper",
                 "slippers", "flip flop", "boots", "crocs", "footwear", "sliders"],
}

LOOT_WORDS = ["loot", "lowest", "price drop", "steal", "bank offer", "all time low"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}

SEEN_FILE = "seen_deals.json"
LOG_FILE = "deals_log.csv"
# -------------------------------------------------------

PERCENT_RE = re.compile(r"(\d{1,2})\s?%\s?(?:off|discount)", re.I)
NUM = r"([\d,]+(?:\.\d+)?)\s?(k|K)?"
PRICE_RE = re.compile(r"(?:@|at|₹|rs\.?|price\s*:?)\s*₹?\s*" + NUM, re.I)
MRP_RE = re.compile(r"(?:mrp|reg(?:ular)?\.?\s*price(?:\s*is)?|original(?:\s*price)?|was)\s*:?\s*₹?\s*" + NUM,
                    re.I)


def to_number(num, k):
    try:
        value = float(num.replace(",", ""))
    except ValueError:
        return None
    return value * 1000 if k else value


def find_discount(text):
    """Return discount percent if the post shows it or lets us calculate it, else None."""
    percents = [int(p) for p in PERCENT_RE.findall(text) if 0 < int(p) < 100]
    if percents:
        return max(percents)
    mrp_m = MRP_RE.search(text)
    price_m = PRICE_RE.search(text)
    if mrp_m and price_m:
        mrp = to_number(*mrp_m.groups())
        price = to_number(*price_m.groups())
        if mrp and price and 0 < price < mrp:
            return round((1 - price / mrp) * 100)
    return None


def find_category(text):
    low = text.lower()
    for category, words in CATEGORIES.items():
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"\b", low):
                return category
    return None


def official_links(links):
    out = []
    for link in links:
        host = re.sub(r"^https?://(www\.)?", "", link.lower()).split("/")[0]
        if any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS):
            out.append(link)
    return out


def fetch_posts(channel):
    """Read the latest posts of a public channel from its web view."""
    url = f"https://t.me/s/{channel}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[{channel}] could not fetch: {e}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message"):
        body = msg.select_one(".tgme_widget_message_text")
        if not body:
            continue
        text = body.get_text("\n", strip=True)
        links = [a.get("href") for a in body.select("a") if a.get("href")]
        posts.append({
            "channel": channel,
            "id": msg.get("data-post", ""),
            "text": text,
            "links": links,
        })
    if not posts:
        print(f"[{channel}] no posts found (channel may be private or have no web view)")
    return posts


def evaluate(post):
    """Return a deal dict if the post matches our rules, else None."""
    links = official_links(post["links"])
    if not links:
        return None
    category = find_category(post["text"])
    if not category:
        return None
    discount = find_discount(post["text"])
    if discount is not None:
        if discount < MIN_DISCOUNT:
            return None
    else:
        low = post["text"].lower()
        if not (INCLUDE_UNKNOWN_DISCOUNT and any(w in low for w in LOOT_WORDS)):
            return None
    return {"category": category, "discount": discount, "link": links[0], **post}


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message[:4000]},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"Telegram error: {e}")


def log_deal(deal):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "channel", "category", "discount_percent", "text", "link"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), deal["channel"],
                    deal["category"], deal["discount"] if deal["discount"] is not None else "",
                    deal["text"].replace("\n", " "), deal["link"]])


def run_check():
    print(f"\n=== Check at {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    seen = load_seen()
    sent = 0

    for channel in CHANNELS:
        for post in fetch_posts(channel):
            key = hashlib.md5(f"{post['id']}|{post['text']}".encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            deal = evaluate(post)
            if not deal or sent >= MAX_ALERTS_PER_RUN:
                continue
            sent += 1
            log_deal(deal)
            pct = f"{deal['discount']}% OFF" if deal["discount"] is not None else "LOOT (check price)"
            msg = (f"{pct} | {deal['category']}\n\n{deal['text']}\n\n"
                   f"Link: {deal['link']}\nSource: t.me/{deal['id']}\n"
                   "(Verify price on the official site before buying)")
            print(msg, "\n")
            send_telegram(msg)
        time.sleep(2)

    save_seen(seen)
    print(f"Deals sent: {sent}")


if __name__ == "__main__":
    run_check()
    if "--once" in sys.argv:
        sys.exit(0)
    schedule.every(CHECK_EVERY_MINUTES).minutes.do(run_check)
    print(f"Scheduled every {CHECK_EVERY_MINUTES} minutes. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(1)
