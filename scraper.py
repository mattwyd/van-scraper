#!/usr/bin/env python3
"""
Scrapes Kenny U-Pull and Kijiji for vehicles within 150km of Collingwood, ON.
Sends new listings as a Beeper message.

Set TEST_MODE=true to disable filters and return first 3 listings from each source.
"""
import json
import os
import re
import urllib.parse
import httpx
from typing import Optional, Tuple
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
# Shared cross-machine secrets. Project .env wins on conflicts (loaded first).
load_dotenv(os.path.expanduser("~/.config/secrets.env"))

SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen.json")

# Beeper Desktop API — runs inside the Beeper Desktop app on this machine.
# The app must be running and signed in or the send will fail.
BEEPER_BASE_URL     = os.environ.get("BEEPER_BASE_URL", "http://localhost:23373")
BEEPER_ACCESS_TOKEN = os.environ["BEEPER_ACCESS_TOKEN"]
BEEPER_CHAT_ID      = os.environ["BEEPER_CHAT_ID"]
TEST_MODE           = os.environ.get("TEST_MODE", "false").lower() == "true"

MAX_KM = 999_999 if TEST_MODE else 190_000

VEHICLE_RULES = [
    {"match": "TRANSIT CONNECT", "max_price": 7_500,  "require_manual": False, "max_km": 190_000},
    {"match": "FORD TRANSIT",    "max_price": 15_000, "require_manual": False, "max_km": 150_000},
    {"match": "GRAND CARAVAN",   "max_price": 3_500,  "require_manual": False, "max_km": 190_000},
    {"match": "FORD FOCUS",      "max_price": 7_000,  "require_manual": True,  "max_km": 190_000},
]

MANUAL_KEYWORDS = {"MANUAL", "5-SPEED", "6-SPEED", "STICK", "STANDARD", "5SPD", "6SPD", "STICK-SHIFT", "STICKSHIFT"}
AUTO_KEYWORDS   = {"AUTOMATIC", "AUTO", "CVT", "AT"}

# Skip listings that mention mechanical problems
PROBLEM_KEYWORDS = {
    "engine", "transmission", "blown", "seized", "rebuilt", "rebuild",
    "needs work", "as is", "as-is", "parts only", "parts or repair",
    "not running", "no start", "won't start", "wont start",
    "for parts", "salvage", "accident", "flood", "fire",
}


KIJIJI_SEARCHES = [
    "ford-transit-connect",
    "ford-transit",
    "dodge-grand-caravan",
    "ford-focus",
]
KIJIJI_BASE = "https://www.kijiji.ca"
# Two search centres: Collingwood (catches Barrie, Thornbury, Owen Sound) +
# Scarborough (catches Toronto, Ajax, Pickering, Oshawa) — 80km radius each
KIJIJI_URLS = [
    KIJIJI_BASE + "/b-cars-trucks/ontario/{kw}/k0c174l9004"
    + "?radius=80.0&address=Collingwood%2C+Ontario&ll=44.5001%2C-80.2167"
    + "&price=__15000&sortingExpression=dateDesc",

    KIJIJI_BASE + "/b-cars-trucks/ontario/{kw}/k0c174l9004"
    + "?radius=60.0&address=Scarborough%2C+Ontario&ll=43.7764%2C-79.2318"
    + "&price=__15000&sortingExpression=dateDesc",
]

KENNY_BASE = "https://kennyautos.com"
# Branch-specific URLs: send ALL cars from Ajax (21502) and Peterborough (21497)
KENNY_BRANCH_URLS = [
    KENNY_BASE + "/iframe-index.asp?ItemCategoryID=0&action=chgDlr&dID=21502&lg=EN",  # Ajax
    KENNY_BASE + "/iframe-index.asp?ItemCategoryID=0&action=chgDlr&dID=21497&lg=EN",  # Peterborough
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ── helpers ────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def extract_number(text: str) -> Optional[int]:
    cleaned = re.sub(r"[,$\s]", "", text)
    m = re.search(r"\d+", cleaned)
    return int(m.group()) if m else None


def match_vehicle(title: str) -> Optional[dict]:
    t = title.upper()
    for rule in VEHICLE_RULES:
        if rule["match"] in t:
            return rule
    return None


def transmission_ok(title: str, require_manual: bool) -> Tuple[bool, str]:
    if not require_manual:
        return True, ""
    t = title.upper()
    words = set(re.findall(r"[A-Z0-9\-]+", t))
    if words & AUTO_KEYWORDS:
        return False, ""
    if words & MANUAL_KEYWORDS:
        return True, "(MANUAL)"
    return False, ""


def kenny_listing_is_manual(url: str) -> bool:
    """Fetch a Kenny detail page and return True only if it explicitly states Manual."""
    try:
        r = httpx.get(url, headers={**HEADERS, "Referer": "https://kennyupull.com/"}, timeout=20, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        print(f"  [Kenny] detail fetch failed for {url}: {e}")
        return False
    body = r.text.upper()
    m = re.search(r"TRANSMISSION[^A-Z0-9]{0,20}([A-Z][A-Z\- ]{2,20})", body)
    if not m:
        return False
    tx = m.group(1).strip()
    if "MANUAL" in tx or "STICK" in tx or "STANDARD" in tx:
        return True
    return False


def km_ok(km: Optional[int], max_km: int = MAX_KM) -> bool:
    return km is None or km <= max_km


def price_ok(price: Optional[int], max_price: int) -> bool:
    return price is None or price <= max_price


def has_problems(title: str, description: str = "") -> bool:
    t = (title + " " + description).lower()
    return any(kw in t for kw in PROBLEM_KEYWORDS)



# ── Kenny U-Pull ────────────────────────────────────────────────────────────

def scrape_kenny() -> list:
    results = []
    seen_titles: set = set()

    for url in KENNY_BRANCH_URLS:
        branch_name = "Ajax" if "21502" in url else "Peterborough"
        print(f"\n[Kenny] Fetching {branch_name} ...")
        try:
            r = httpx.get(url, headers={**HEADERS, "Referer": "https://kennyupull.com/"}, timeout=30, follow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            print(f"  [Kenny] Error: {e}")
            continue

        soup  = BeautifulSoup(r.text, "html.parser")
        items = soup.find_all("li")
        print(f"[Kenny] {branch_name}: {len(items)} <li> elements found")

        for li in items:
            h5 = li.find("h5")
            if not h5:
                continue
            title = h5.get_text(strip=True)
            if title in seen_titles:
                continue
            seen_titles.add(title)

            price_h5 = li.find("h5", class_="recent_item_price")
            price = extract_number(price_h5.find("b").get_text()) if price_h5 and price_h5.find("b") else None

            km_h5 = li.find("h5", class_="item_wear")
            km = extract_number(km_h5.get_text(strip=True).replace("km", "")) if km_h5 else None

            seller = li.find(class_="itemRecent_seller_name")
            city   = li.find(class_="itemRecent_seller_city")
            location = f"{seller.get_text(strip=True)}, {city.get_text(strip=True)}" if seller and city else branch_name

            a    = li.find("a", href=True)
            link = (KENNY_BASE + a["href"]) if a and a["href"].startswith("/") else (a["href"] if a else "")

            img_tag = li.find("img")
            image = img_tag["src"] if img_tag and img_tag.get("src") else ""
            if image and image.startswith("/"):
                image = KENNY_BASE + image

            rule = match_vehicle(title)
            if not TEST_MODE:
                if "CHRYSLER" in title.upper():
                    continue
                if km is not None and km > 190_000:
                    continue
                is_transit = "TRANSIT" in title.upper()
                if not is_transit and price is not None and price > 7_000:
                    continue
                if rule and rule["require_manual"]:
                    ok, _ = transmission_ok(title, True)
                    if not ok:
                        # Title alone doesn't confirm manual — check the listing detail page
                        if not link or not kenny_listing_is_manual(link):
                            continue

            print(f"  [Kenny] {branch_name}: {title} | ${price} | {km} km")
            results.append({
                "source":   f"Kenny U-Pull ({branch_name})",
                "title":    title,
                "price":    f"${price:,}" if price is not None else "N/A",
                "km":       f"{km:,} km" if km is not None else "N/A",
                "location": location,
                "link":     link,
                "image":    image,
            })
            if TEST_MODE and len(results) >= 3:
                return results

    return results


# ── Kijiji ──────────────────────────────────────────────────────────────────

def scrape_kijiji() -> list:
    seen    = set()
    results = []

    for kw in KIJIJI_SEARCHES:
        for url_template in KIJIJI_URLS:
            url = url_template.format(kw=kw)
            print(f"\n[Kijiji] Fetching {url} ...")
            try:
                r = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
                r.raise_for_status()
            except Exception as e:
                print(f"  [Kijiji] Error fetching {kw}: {e}")
                continue

            soup = BeautifulSoup(r.text, "html.parser")

            for tag in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(tag.string or "")
                except Exception:
                    continue

                items = []
                if data.get("@type") == "ItemList":
                    items = data.get("itemListElement", [])
                elif isinstance(data.get("itemListElement"), list):
                    items = data["itemListElement"]

                for item in items:
                    vehicle = item.get("item", item)

                    link = vehicle.get("url", "")
                    if not link or link in seen:
                        continue
                    seen.add(link)

                    title = vehicle.get("name", "")
                    description = vehicle.get("description", "")
                    rule = match_vehicle(title)
                    if not TEST_MODE and not rule:
                        continue
                    if "lease" in title.lower():
                        continue

                    price_raw = None
                    offers = vehicle.get("offers", {})
                    if isinstance(offers, dict):
                        price_raw = offers.get("price")
                    price = int(float(price_raw)) if price_raw is not None else None

                    km_raw = vehicle.get("mileageFromOdometer", {})
                    km = int(float(km_raw.get("value", 0))) if isinstance(km_raw, dict) and km_raw.get("value") else None

                    img_field = vehicle.get("image", "")
                    if isinstance(img_field, list):
                        image = img_field[0] if img_field else ""
                    else:
                        image = img_field or ""

                    if not TEST_MODE and rule:
                        if has_problems(title, description):
                            continue
                        if not price_ok(price, rule["max_price"]):
                            continue
                        if not km_ok(km, rule["max_km"]):
                            continue
                        ok, tx_note = transmission_ok(title, rule["require_manual"])
                        if not ok:
                            continue
                    else:
                        tx_note = ""

                    m = re.search(r"/v-cars-trucks/([^/]+)/", link)
                    location = m.group(1).replace("-", " ").title() if m else ""

                    print(f"  [Kijiji] Match: {title} | ${price} | {km} km | {location}")
                    results.append({
                        "source":   "Kijiji",
                        "title":    f"{title} {tx_note}".strip(),
                        "price":    f"${price:,}" if price is not None else "N/A",
                        "km":       f"{km:,} km" if km is not None else "N/A",
                        "location": location,
                        "link":     link,
                        "image":    image,
                    })
                    if TEST_MODE and len(results) >= 3:
                        return results

    return results


# ── Beeper ───────────────────────────────────────────────────────────────────

def _upload_photo(image_url: str) -> Optional[dict]:
    """Fetch a listing photo and hand it to Beeper, returning the upload record.

    Beeper accepts one attachment per message, so each van gets its own message.
    A photo that will not download is not worth losing the listing over, so this
    returns None and the caller sends text only.
    """
    if not image_url:
        return None
    try:
        img = httpx.get(image_url, headers=HEADERS, timeout=20, follow_redirects=True)
        img.raise_for_status()
        name = os.path.basename(urllib.parse.urlparse(image_url).path) or "van.jpg"
        up = httpx.post(
            f"{BEEPER_BASE_URL}/v1/assets/upload",
            headers={"Authorization": f"Bearer {BEEPER_ACCESS_TOKEN}"},
            files={"file": (name, img.content)},
            timeout=30.0,
        )
        up.raise_for_status()
        return up.json()
    except Exception as e:
        print(f"  [photo] skipped ({type(e).__name__}: {str(e)[:60]})")
        return None


def send_beeper(listings: list, seen_count: int = 0) -> None:
    """Post new listings to a Beeper chat, one message per van with its photo.

    Raises on failure so main() skips save_seen() and the same listings get
    retried on the next run, matching the old SMTP behaviour. Note the tradeoff:
    if some vans send and a later one fails, the whole batch is retried and the
    ones that landed will arrive twice. Duplicates beat silently losing a van.
    """
    if not listings:
        print("No new listings - skipping notification.")
        return

    chat_id = urllib.parse.quote(BEEPER_CHAT_ID, safe="")
    url = f"{BEEPER_BASE_URL}/v1/chats/{chat_id}/messages"
    headers = {"Authorization": f"Bearer {BEEPER_ACCESS_TOKEN}"}
    failures = []

    for car in listings[:20]:
        text = "\n".join([
            f"🚐 **[{car['source']}] {car['title']}**",
            f"💰 {car['price']}  |  🛣 {car['km']}  |  📍 {car['location'] or 'N/A'}",
            car.get("link") or "",
        ]).strip()

        payload = {"text": text}
        photo = _upload_photo(car.get("image", ""))
        if photo:
            payload["attachment"] = {
                "uploadID": photo["uploadID"],
                "type": "image",
                "mimeType": photo.get("mimeType"),
                "fileName": photo.get("fileName"),
            }

        resp = httpx.post(url, headers=headers, json=payload, timeout=30.0)
        if resp.status_code >= 400:
            failures.append(f"{car['title'][:40]} [{resp.status_code}] {resp.text[:120]}")
        else:
            print(f"  sent: {car['title'][:60]}{' (with photo)' if photo else ''}")

    if len(listings) > 20:
        httpx.post(url, headers=headers,
                   json={"text": f"_...and {len(listings) - 20} more not shown_"},
                   timeout=30.0)

    if failures:
        raise RuntimeError(
            "Beeper send failed for: " + " ; ".join(failures[:3])
            + ". Is Beeper Desktop running and signed in?"
        )

    print(f"\nBeeper: {min(len(listings), 20)} message(s) sent")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"TEST_MODE={TEST_MODE}, MAX_KM={MAX_KM}")
    for rule in VEHICLE_RULES:
        print(f"  {rule['match']}: max ${rule['max_price']:,}" + (" (manual only)" if rule["require_manual"] else ""))

    seen = set() if TEST_MODE else load_seen()

    kenny  = scrape_kenny()
    kijiji = scrape_kijiji()
    all_listings = kenny + kijiji

    new_listings = [l for l in all_listings if l["link"] not in seen]
    seen_count = len(all_listings) - len(new_listings)
    print(f"\nKenny: {len(kenny)} | Kijiji: {len(kijiji)} | Total: {len(all_listings)} | New: {len(new_listings)} | Already seen: {seen_count}")

    send_beeper(new_listings, seen_count=seen_count)

    if not TEST_MODE:
        seen.update(l["link"] for l in all_listings)
        save_seen(seen)


if __name__ == "__main__":
    main()
