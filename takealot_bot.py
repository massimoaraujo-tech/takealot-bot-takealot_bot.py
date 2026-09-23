"""Shared helpers for the Takealot + Checkers Pokémon bots:
filters, state tracking, Discord alerts, proxy config."""
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pokebot")

WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
PROXY_URL = os.getenv("PROXY_URL", "")          # http://user:pass@host:port
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))
ENABLE_BUTTONS = os.getenv("ENABLE_BUTTONS", "true").lower() == "true"

# ---------- Filters (English Pokémon TCG only, 30th = priority) ----------
POKEMON_RE = re.compile(r"pok[eé]mon", re.I)
TCG_RE = re.compile(
    r"tcg|trading card|booster|elite trainer|\betb\b|\btin\b|blister|"
    r"collection|battle deck|premium|bundle|binder|poster|display|sleeved|"
    r"build (and|&) battle|knock ?out",
    re.I,
)
EXCLUDE_RE = re.compile(
    r"japan|japanese|\bjp\b|chinese|\bcn\b|korean|\bkr\b|simplified|"
    r"yu-?gi-?oh|magic: the gathering|\bmtg\b|one piece|digimon|dragon ball|"
    r"lorcana|plush|nintendo switch|t-shirt|hoodie|costume|lunch ?box",
    re.I,
)
PRIORITY_RE = re.compile(r"30th|\bcelebration\b", re.I)


def is_wanted(title: str) -> bool:
    return bool(POKEMON_RE.search(title) and TCG_RE.search(title)
                and not EXCLUDE_RE.search(title))


def is_priority(title: str) -> bool:
    return bool(PRIORITY_RE.search(title))


def proxies():
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


def sleep_jitter(base: int = POLL_SECONDS):
    time.sleep(base * random.uniform(0.7, 1.3))


# ---------- State ----------
class State:
    """Remembers what we've seen so we only alert on changes.
    Railway's disk resets on redeploy; mount a volume and set
    STATE_DIR=/data to keep state across deploys."""

    def __init__(self, name: str):
        self.path = Path(os.getenv("STATE_DIR", ".")) / f"state_{name}.json"
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}
        self.seeded = bool(self.data)

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data))
        except Exception as e:
            log.warning("Could not save state: %s", e)


def process(store: str, state: State, products: list, button_label: str):
    """products: dicts with id, title, url, price, in_stock, image."""
    first_run = not state.seeded
    wanted = [p for p in products if is_wanted(p["title"])]
    for p in wanted:
        old = state.data.get(p["id"])
        event = None
        if old is None:
            event = "NEW LISTING"
        elif not old.get("in_stock") and p["in_stock"]:
            event = "RESTOCK"
        state.data[p["id"]] = {"title": p["title"], "in_stock": p["in_stock"],
                               "price": p.get("price")}
        if event and not first_run:
            send_alert(store, event, p, [(button_label, p["url"])])

    if first_run:
        in_stock = sum(1 for p in wanted if p["in_stock"])
        pri = sum(1 for p in wanted if is_priority(p["title"]))
        post_webhook({"content": (
            f"✅ **{store} bot online** — tracking {len(wanted)} Pokémon TCG "
            f"products ({in_stock} in stock, {pri} 30th Celebration). "
            f"Alerts start from the next check.")})
    state.seeded = True
    state.save()
    log.info("%s: %d products checked (%d wanted)", store, len(products), len(wanted))


# ---------- Discord ----------
def send_alert(store: str, event: str, p: dict, buttons=None):
    pri = is_priority(p["title"])
    color = 0xE3350D if pri else (0x2ECC71 if p["in_stock"] else 0x95A5A6)
    embed = {
        "title": p["title"][:256],
        "url": p["url"],
        "color": color,
        "fields": [
            {"name": "Price", "value": p.get("price") or "—", "inline": True},
            {"name": "Stock", "value": "✅ In stock" if p["in_stock"] else "❌ Out of stock",
             "inline": True},
            {"name": "Store", "value": store, "inline": True},
        ],
        "footer": {"text": event + (" • 30th Celebration" if pri else "")},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if p.get("image"):
        embed["thumbnail"] = {"url": p["image"]}
    content = (f"@everyone 🔥 **30th Celebration {event}** — {store}" if pri
               else f"**{event}** — {store}")
    post_webhook({"content": content, "embeds": [embed],
                  "allowed_mentions": {"parse": ["everyone"]}}, buttons)


def post_webhook(payload: dict, buttons=None):
    if not WEBHOOK:
        log.error("DISCORD_WEBHOOK not set")
        return
    url = WEBHOOK
    if buttons and ENABLE_BUTTONS:
        payload = dict(payload)
        payload["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": label[:80], "url": link}
            for label, link in buttons]}]
        url = WEBHOOK + ("&" if "?" in WEBHOOK else "?") + "with_components=true"
    for _ in range(3):
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        if r.status_code >= 400 and "components" in payload:
            # Buttons rejected — fall back to plain embed (title is still a link)
            payload.pop("components")
            url = WEBHOOK
            continue
        if r.status_code >= 400:
            log.error("Discord error %s: %s", r.status_code, r.text[:200])
        return


# ================= BOT =================
"""Takealot Pokémon TCG tracker -> Discord.
Uses the JSON search endpoint Takealot's own site calls.
If it stops working, search on takealot.com with DevTools > Network open,
copy the new 'searches/products' URL and set TAKEALOT_API."""
import os

import requests


API = os.getenv("TAKEALOT_API",
                "https://api.takealot.com/rest/v-1-12-0/searches/products")
QUERIES = [q.strip() for q in os.getenv(
    "TAKEALOT_QUERIES",
    "pokemon 30th celebration|pokemon tcg|pokemon elite trainer box|pokemon booster",
).split("|") if q.strip()]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en-ZA,en;q=0.9",
    "Origin": "https://www.takealot.com",
    "Referer": "https://www.takealot.com/",
}


def parse(item: dict):
    pv = item.get("product_views", item)
    core = pv.get("core") or {}
    pid, title = core.get("id"), core.get("title")
    if not pid or not title:
        return None
    slug = core.get("slug") or "product"
    bb = pv.get("buybox_summary") or {}
    prices = bb.get("prices") or []
    price = bb.get("pretty_price") or (f"R {prices[0]:,.0f}" if prices else None)
    status = ((pv.get("stock_availability_summary") or {}).get("status") or "").lower()
    if "is_add_to_cart_available" in bb:
        in_stock = bool(bb["is_add_to_cart_available"])
    else:
        in_stock = "in stock" in status or "pre-order" in status
    image = None
    imgs = (pv.get("gallery") or {}).get("images") or []
    if imgs:
        image = imgs[0].replace("{size}", "zoom")
    return {"id": f"PLID{pid}", "title": title,
            "url": f"https://www.takealot.com/{slug}/PLID{pid}",
            "price": price, "in_stock": in_stock, "image": image}


def fetch(q: str):
    r = requests.get(API, params={"qsearch": q}, headers=HEADERS,
                     proxies=proxies(), timeout=30)
    if r.status_code in (403, 429):
        raise RuntimeError(f"Blocked ({r.status_code}) — check PROXY_URL")
    r.raise_for_status()
    results = ((r.json().get("sections") or {}).get("products") or {}).get("results") or []
    if results:
        log.debug("Sample raw item keys: %s", list(results[0].keys()))
    return [p for p in (parse(i) for i in results) if p]


def main():
    state = State("takealot")
    if os.getenv("TEST_ALERT") == "1":
        send_alert("Takealot", "TEST ALERT", {
            "title": "Pokémon TCG: 30th Celebration Elite Trainer Box (test)",
            "url": "https://www.takealot.com", "price": "R 1,299",
            "in_stock": True, "image": None}, [("Open on Takealot", "https://www.takealot.com")])
    while True:
        products = {}
        for q in QUERIES:
            try:
                for p in fetch(q):
                    products[p["id"]] = p
            except Exception as e:
                log.warning("Takealot '%s' failed: %s", q, e)
            sleep_jitter(5)
        if products:
            process("Takealot", state, list(products.values()), "Open on Takealot")
        else:
            log.warning("Takealot: no products returned this round")
        sleep_jitter()


if __name__ == "__main__":
    main()
