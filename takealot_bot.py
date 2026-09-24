"""Spidey 🕷️ — Takealot Pokémon TCG tracker -> Discord."""
import html
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("takealot")

BOT_NAME, BOT_EMOJI = "Spidey", "🕷️"

# ---------------- Settings (all optional Railway variables) ----------------
WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
WEBHOOK_30TH = os.getenv("WEBHOOK_30TH", "")        # #30th-alerts channel (Level 1 copies)
ROLE_30TH = os.getenv("ROLE_30TH_ID", "")           # @30th role id; blank = @everyone
PROXY_URL = os.getenv("PROXY_URL", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))
WORKERS = int(os.getenv("WORKERS", "8"))            # stores checked at the same time
REPORT_HOURS = float(os.getenv("REPORT_HOURS", "6"))
OOS_CONFIRM = int(os.getenv("OOS_CONFIRM", "3"))    # checks in a row before "sold out" counts
COOLDOWN_HOURS = float(os.getenv("RESTOCK_COOLDOWN_HOURS", "2"))
FAIL_LIMIT = int(os.getenv("FAIL_LIMIT", "5"))      # failed checks in a row before quarantine
RETRY_HOURS = float(os.getenv("RETRY_HOURS", "24")) # how often a quarantined store is retried
ONLY_30TH = os.getenv("ONLY_30TH", "false").lower() == "true"
ENABLE_BUTTONS = os.getenv("ENABLE_BUTTONS", "true").lower() == "true"
STATE_FILE = Path(os.getenv("STATE_DIR", ".")) / "state_takealot.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# ---------------- Filters: English sealed Pokémon TCG, 30th = Level 1 ----------------
POKEMON_RE = re.compile(r"pok[eé]mon", re.I)
TCG_RE = re.compile(
    r"tcg|trading card|booster|elite trainer|\betb\b|\btin\b|blister|collection|"
    r"battle deck|premium|bundle|binder|poster|display|sleeved|build (and|&) battle|"
    r"card game|scarlet|violet|mega evolution", re.I)
EXCLUDE_RE = re.compile(
    r"japan|japanese|\bjp\b|chinese|\bcn\b|korean|\bkr\b|simplified|yu-?gi-?oh|"
    r"magic: the gathering|\bmtg\b|one piece|digimon|dragon ball|lorcana|plush|"
    r"nintendo switch|t-shirt|hoodie|costume|lunch ?box", re.I)
PRIORITY_RE = re.compile(r"30th|\bcelebration\b", re.I)
SINGLES_TITLE_RE = re.compile(
    r"\b\d{1,3}/\d{2,3}\b|reverse holo|full art|illustration rare|secret rare|"
    r"ultra rare|holo rare|hyper rare|\bpsa ?\d|\bcgc ?\d|\bbgs ?\d|graded|slab|"
    r"single card", re.I)
SINGLES_META_RE = re.compile(r"single|graded|slab|\bpsa\b|\bcgc\b|\bbgs\b", re.I)


def is_priority(title):
    return bool(PRIORITY_RE.search(title))


def is_wanted(item):
    title, meta = item["title"], item.get("meta") or ""
    if SINGLES_TITLE_RE.search(title) or (meta and SINGLES_META_RE.search(meta)):
        return False
    everything = f"{title} {meta}"
    return bool(POKEMON_RE.search(everything) and TCG_RE.search(everything)
                and not EXCLUDE_RE.search(title))


# ---------------- RRP check for 30th items ----------------
def _load_rrp():
    raw = os.getenv("RRP_30TH", "elite trainer=1299|booster bundle=699|sticker=499|mini tin=299")
    out = []
    for part in raw.split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out.append((k.strip().lower(), float(v)))
            except ValueError:
                pass
    return out


RRP_30TH = _load_rrp()


def money(text):
    """'R 1,299.00' -> 1299.0"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = re.sub(r"[^\d.,]", "", str(text))
    if re.search(r",\d{2}$", t):
        t = t.replace(".", "").replace(",", ".")
    t = t.replace(",", "")
    try:
        return float(t) if t else None
    except ValueError:
        return None


def rrp_note(item):
    if not is_priority(item["title"]) or not item.get("price_value"):
        return None
    low = item["title"].lower()
    for key, rrp in RRP_30TH:
        if key in low:
            diff = item["price_value"] - rrp
            if diff <= rrp * 0.05:
                return f"✅ At RRP (R{rrp:,.0f})"
            return f"⚠️ R{diff:,.0f} above RRP (R{rrp:,.0f})"
    return None


def fmt_price(v):
    return f"R {v:,.2f}" if v else "—"


# ---------------- Discord ----------------
def post_webhook(payload, buttons=None, flags=0, url=None):
    url = url or WEBHOOK
    if not url:
        log.error("DISCORD_WEBHOOK not set")
        return
    payload = dict(payload)
    target = url
    if flags:
        payload["flags"] = flags
    if buttons and ENABLE_BUTTONS:
        payload["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": l[:80], "url": u} for l, u in buttons[:5]]}]
        target = url + ("&" if "?" in url else "?") + "with_components=true"
    for _ in range(4):
        try:
            r = requests.post(target, json=payload, timeout=15)
        except Exception as e:
            log.warning("Discord post failed: %s", e)
            return
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        if r.status_code >= 400 and ("components" in payload or "flags" in payload):
            payload.pop("components", None)     # fall back to a plain message
            payload.pop("flags", None)
            target = url
            continue
        if r.status_code >= 400:
            log.error("Discord error %s: %s", r.status_code, r.text[:200])
        return


def post_long(text, url=None):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 1900:
            post_webhook({"content": chunk}, url=url)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        post_webhook({"content": chunk}, url=url)


def mention():
    return f"<@&{ROLE_30TH}>" if ROLE_30TH else "@everyone"


BADGE = {1: "🔴 LEVEL 1 · 30TH CELEBRATION", 2: "🟢 LEVEL 2 · IN STOCK",
         3: "⚪ LEVEL 3 · LISTED (not in stock yet)"}
COLOR = {1: 0xE3350D, 2: 0x2ECC71, 3: 0x95A5A6}
EVENT_ICON = {"NEW LISTING": "🆕", "RESTOCK": "🔁", "PRICE DROP": "📉", "TEST ALERT": "🧪"}


def level_of(item):
    if is_priority(item["title"]):
        return 1
    return 2 if item["in_stock"] else 3


def send_alert(store, event, item):
    lvl = level_of(item)
    fields = [
        {"name": "Price", "value": fmt_price(item.get("price_value")) if item.get("price_value")
         else (item.get("price") or "—"), "inline": True},
        {"name": "Stock", "value": "✅ In stock" if item["in_stock"] else "❌ Out of stock",
         "inline": True},
        {"name": "Store", "value": store, "inline": True},
    ]
    if item.get("was"):
        fields.append({"name": "Was", "value": fmt_price(item["was"]), "inline": True})
    note = rrp_note(item)
    if note:
        fields.append({"name": "RRP check", "value": note, "inline": True})
    embed = {
        "title": item["title"][:256], "url": item["url"], "color": COLOR[lvl],
        "description": f"**{BADGE[lvl]}**\n{EVENT_ICON.get(event, '')} {event}",
        "fields": fields,
        "footer": {"text": f"{BOT_EMOJI} {BOT_NAME} • {store}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if item.get("image"):
        embed["image" if lvl == 1 else "thumbnail"] = {"url": item["image"]}
    if lvl == 1:
        content = f"{mention()} 🔥 **30th Celebration {event}** — {store}"
    else:
        content = f"{EVENT_ICON.get(event, '')} **{event}** — {store}"
    allowed = {"parse": ["everyone"]}
    if ROLE_30TH:
        allowed["roles"] = [ROLE_30TH]
    buttons = [(f"Open on {store}"[:80], item["url"])]
    if item.get("checkout_url") and item["in_stock"]:
        buttons.insert(0, (item.get("checkout_label") or "Add to cart", item["checkout_url"]))
    payload = {"content": content, "embeds": [embed], "allowed_mentions": allowed}
    post_webhook(payload, buttons, flags=4096 if lvl == 3 else 0)   # Level 3 = silent
    if lvl == 1 and WEBHOOK_30TH:
        post_webhook(payload, buttons, url=WEBHOOK_30TH)


def short_error(e):
    t = str(e)
    if "Failed to resolve" in t or "NameResolution" in t or "ERR_NAME_NOT_RESOLVED" in t:
        return "link doesn't exist"
    if "timed out" in t or "Timeout" in t:
        return "site didn't respond (timed out)"
    if "SSL" in t or "certificate" in t or "ERR_CERT" in t:
        return "site's security certificate is broken"
    if "Expecting value" in t:
        return "didn't return product data"
    return t.split("\n")[0][:120]


# ---------------- State ----------------
def load_state():
    try:
        s = json.loads(STATE_FILE.read_text())
    except Exception:
        s = {}
    s.setdefault("products", {})
    s.setdefault("stores", {})
    s.setdefault("seeded", [])
    return s


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        log.warning("Could not save state: %s", e)


def handle_item(state, store, item, seeded, now):
    old = state["products"].get(item["id"])
    pv = item.get("price_value")
    event = None
    if old is None:
        event = "NEW LISTING"
        rec = {"in_stock": item["in_stock"], "oos": 0, "last_alert": 0, "price": pv}
    else:
        rec = dict(old)
        rec.setdefault("oos", 0)
        rec.setdefault("last_alert", 0)
        if item["in_stock"]:
            if not old.get("in_stock"):
                event = "RESTOCK"
            rec["in_stock"], rec["oos"] = True, 0
            prev = old.get("price")
            if event is None and pv and prev and pv <= prev * 0.95 and prev - pv >= 20:
                event, item["was"] = "PRICE DROP", prev
        else:
            # sellers swapping / pages flickering: only count "sold out" once it sticks
            rec["oos"] += 1
            if rec["oos"] >= OOS_CONFIRM:
                rec["in_stock"] = False
        if pv:
            rec["price"] = pv
    if event in ("RESTOCK", "PRICE DROP") and now - rec["last_alert"] < COOLDOWN_HOURS * 3600:
        event = None
    if event and seeded and (not ONLY_30TH or is_priority(item["title"])):
        send_alert(store, event, item)
        rec["last_alert"] = now
    state["products"][item["id"]] = rec


# ---------------- Main loop ----------------
def run(get_stores, fetch, single=False, parallel=True):
    """get_stores() -> [(name, key, opts)];  fetch(key, opts) -> [items]"""
    state = load_state()
    if os.getenv("TEST_ALERT") == "1":
        send_alert("Test Store", "TEST ALERT", {
            "title": "Pokémon TCG: 30th Celebration Elite Trainer Box (test)",
            "url": "https://store.nintendo.co.za", "price_value": 1299.0,
            "in_stock": True, "image": None, "checkout_url": "https://store.nintendo.co.za",
            "checkout_label": "Checkout now"})
    startup, last_report = True, 0.0
    while True:
        now = time.time()
        stores = get_stores()
        active, parked = [], []
        for name, key, opts in stores:
            rec = state["stores"].setdefault(key, {"fails": 0, "quarantined": False, "since": 0})
            if (not single and rec["quarantined"] and not startup
                    and now - rec["since"] < RETRY_HOURS * 3600):
                parked.append(name)
            else:
                active.append((name, key, opts))

        results = {}
        if parallel and len(active) > 1:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futs = {k: pool.submit(fetch, k, o) for _, k, o in active}
                for k, f in futs.items():
                    try:
                        results[k] = f.result()
                    except Exception as e:
                        results[k] = e
        else:
            for _, k, o in active:
                try:
                    results[k] = fetch(k, o)
                except Exception as e:
                    results[k] = e

        ok, failed, tracked, in_stock, pri, hot = 0, [], 0, 0, 0, []
        for name, key, opts in active:
            rec, res = state["stores"][key], results.get(key)
            if isinstance(res, Exception):
                reason = short_error(res)
                rec["fails"] += 1
                log.warning("%s failed (%d in a row): %s", name, rec["fails"], res)
                failed.append(f"{name} — {reason}")
                if rec["quarantined"]:
                    rec["since"] = now                       # retry failed, park again
                elif rec["fails"] >= FAIL_LIMIT:
                    if single:
                        if rec["fails"] == FAIL_LIMIT:
                            post_webhook({"content": f"⚠️ **{name} has failed {FAIL_LIMIT} checks in a row** "
                                                     f"({reason}). Still trying — check Railway logs."})
                    else:
                        rec.update(quarantined=True, since=now)
                        post_webhook({"content": f"🚫 **{name} quarantined** — failed {rec['fails']} checks "
                                                 f"in a row ({reason}). Skipping it so the other stores "
                                                 f"keep running; retrying every {RETRY_HOURS:g}h. "
                                                 f"Fix its link in stores.txt when you have time."})
                continue
            if rec["quarantined"] or (single and rec["fails"] >= FAIL_LIMIT):
                post_webhook({"content": f"✅ **{name} is working again** — back on the watchlist."})
            rec.update(fails=0, quarantined=False)
            ok += 1
            seeded = key in state["seeded"]
            for item in res:
                if not is_wanted(item):
                    continue
                tracked += 1
                in_stock += item["in_stock"]
                if is_priority(item["title"]):
                    pri += 1
                    if item["in_stock"]:
                        hot.append((name, item))
                handle_item(state, name, item, seeded, now)
            if not seeded:
                state["seeded"].append(key)
        save_state(state)
        log.info("Round done: %d/%d OK, %d tracked", ok, len(active), tracked)

        if startup:
            mode = "30th Celebration only" if ONLY_30TH else "all Pokémon TCG, 30th = Level 1"
            label = "stores" if not single else "check"
            msg = (f"{BOT_EMOJI} **{BOT_NAME} online** ({mode}) — {ok}/{len(active)} {label} working, "
                   f"tracking {tracked} Pokémon TCG products ({in_stock} in stock, {pri} 30th Celebration).")
            if failed:
                msg += "\n\n**Not working (auto-quarantined after " + str(FAIL_LIMIT) + \
                       " failed checks):**\n" + "\n".join(f"• {f}" for f in failed)
            post_long(msg)
            startup = False

        if now - last_report >= REPORT_HOURS * 3600:
            post_report(hot, [n for n, k, _ in stores if state["stores"].get(k, {}).get("quarantined")])
            last_report = now
        time.sleep(POLL_SECONDS * random.uniform(0.8, 1.2))


def post_report(hot, quarantined):
    if hot:
        by_store = {}
        for store, item in hot:
            by_store.setdefault(store, []).append(item)
        lines = [f"🔥 **{BOT_EMOJI} {BOT_NAME} — 30th Celebration IN STOCK right now** "
                 f"({len(hot)} products at {len(by_store)} stores)"]
        for store in sorted(by_store):
            lines.append(f"\n**{store}**")
            for it in sorted(by_store[store], key=lambda x: x["title"]):
                price = fmt_price(it.get("price_value")) if it.get("price_value") else (it.get("price") or "—")
                note = rrp_note(it)
                lines.append(f"• [{it['title'][:90]}]({it['url']}) — {price}"
                             + (f" · {note}" if note else ""))
    else:
        lines = [f"{BOT_EMOJI} **{BOT_NAME}** — 30th Celebration stock check: nothing in stock right now."]
    if quarantined:
        lines.append("\n🚫 **Quarantined (fix when you have time):** " + ", ".join(quarantined))
    post_long("\n".join(lines))


# ---------------- Takealot: the JSON search endpoint takealot.com itself uses ----------------
# If it stops working: search on takealot.com with DevTools > Network open,
# copy the new 'searches/products' URL and set TAKEALOT_API on Railway.
API = os.getenv("TAKEALOT_API", "https://api.takealot.com/rest/v-1-12-0/searches/products")
QUERIES = [q.strip() for q in os.getenv(
    "TAKEALOT_QUERIES",
    "pokemon 30th celebration|pokemon tcg|pokemon elite trainer box|pokemon booster",
).split("|") if q.strip()]
HEADERS = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-ZA,en;q=0.9",
           "Origin": "https://www.takealot.com", "Referer": "https://www.takealot.com/"}


def parse(item):
    pv = item.get("product_views", item)
    core = pv.get("core") or {}
    pid, title = core.get("id"), core.get("title")
    if not pid or not title:
        return None
    bb = pv.get("buybox_summary") or {}
    prices = bb.get("prices") or []
    price_value = float(prices[0]) if prices else money(bb.get("pretty_price"))
    status = ((pv.get("stock_availability_summary") or {}).get("status") or "").lower()
    if "is_add_to_cart_available" in bb:
        in_stock = bool(bb["is_add_to_cart_available"])
    else:
        in_stock = "in stock" in status or "pre-order" in status
    imgs = (pv.get("gallery") or {}).get("images") or []
    return {"id": f"PLID{pid}", "title": title,
            "url": f"https://www.takealot.com/{core.get('slug') or 'product'}/PLID{pid}",
            "price_value": price_value, "in_stock": in_stock,
            "image": imgs[0].replace("{size}", "zoom") if imgs else None, "meta": ""}


def fetch(key, opts):
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    found, errors = {}, []
    for q in QUERIES:
        try:
            r = requests.get(API, params={"qsearch": q}, headers=HEADERS,
                             proxies=proxies, timeout=30)
            if r.status_code in (403, 429):
                raise RuntimeError(f"{r.status_code} — blocked")
            r.raise_for_status()
            results = ((r.json().get("sections") or {}).get("products") or {}).get("results") or []
            for p in (parse(i) for i in results):
                if p:
                    found[p["id"]] = p
        except Exception as e:
            errors.append(e)
        time.sleep(random.uniform(3, 6))
    if errors and len(errors) == len(QUERIES):
        raise errors[-1]
    return list(found.values())


if __name__ == "__main__":
    run(lambda: [("Takealot", "takealot", {})], fetch, single=True, parallel=False)
