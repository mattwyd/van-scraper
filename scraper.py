#!/usr/bin/env python3
"""
Scrapes Kenny U-Pull (kennyautos.com) and Kijiji for cargo vans / Rangers
under $5,000 and 200,000 km near: Ajax, Pickering, Barrie, London, Newmarket, Peterborough.

Set TEST_MODE=true to disable filters and return the first 3 listings from each source.
"""
import json
import os
import re
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen.json")


def load_seen() -> set[str]:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set[str]) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
MAX_PRICE = 99_999 if TEST_MODE else 5_000
MAX_KM    = 999_999 if TEST_MODE else 190_000

ALLOWED_LOCATIONS = {"ajax", "pickering", "barrie", "london", "newmarket", "peterborough"}

# Matched against uppercase title
WANTED_VEHICLES = {
    "TRANSIT CONNECT",
    "NV CARGO", "NV200",
}

# Kijiji search keywords → one search page each
KIJIJI_SEARCHES = [
    "transit-connect",
    "nv-cargo", "nv200",
]
KIJIJI_BASE = "https://www.kijiji.ca"
# Search within 150km of Collingwood, ON (44.5001, -80.2167)
KIJIJI_URL  = (
    KIJIJI_BASE
    + "/b-cars-trucks/ontario/{kw}/k0c174l9004"
    + "?radius=150.0&address=Collingwood%2C+Ontario&ll=44.5001%2C-80.2167"
    + "&price=__5000&sortingExpression=dateDesc"
)

KENNY_URL  = "https://kennyautos.com/iframe-index.asp?lg=EN"
KENNY_BASE = "https://kennyautos.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ── helpers ────────────────────────────────────────────────────────────────

def extract_number(text: str) -> int | None:
    cleaned = re.sub(r"[,$\s]", "", text)
    m = re.search(r"\d+", cleaned)
    return int(m.group()) if m else None

def is_wanted(title: str) -> bool:
    t = title.upper()
    return any(v in t for v in WANTED_VEHICLES)

def location_ok(location: str) -> bool:
    l = location.lower()
    return any(loc in l for loc in ALLOWED_LOCATIONS)

def price_ok(price: int | None) -> bool:
    return price is None or price <= MAX_PRICE

def km_ok(km: int | None) -> bool:
    return km is None or km <= MAX_KM


# ── Kenny U-Pull ────────────────────────────────────────────────────────────

def scrape_kenny() -> list[dict]:
    print(f"\n[Kenny] Fetching {KENNY_URL} ...")
    r = httpx.get(KENNY_URL, headers={**HEADERS, "Referer": "https://kennyupull.com/"}, timeout=30, follow_redirects=True)
    r.raise_for_status()

    soup  = BeautifulSoup(r.text, "html.parser")
    items = soup.find_all("li")
    print(f"[Kenny] {len(items)} <li> elements found")

    results = []
    for li in items:
        h5 = li.find("h5")
        if not h5:
            continue
        title = h5.get_text(strip=True)

        if not TEST_MODE and not is_wanted(title):
            continue

        price_h5 = li.find("h5", class_="recent_item_price")
        price = extract_number(price_h5.find("b").get_text()) if price_h5 and price_h5.find("b") else None

        km_h5 = li.find("h5", class_="item_wear")
        km = extract_number(km_h5.get_text(strip=True).replace("km", "")) if km_h5 else None

        seller = li.find(class_="itemRecent_seller_name")
        city   = li.find(class_="itemRecent_seller_city")
        location = f"{seller.get_text(strip=True)}, {city.get_text(strip=True)}" if seller and city else ""

        a    = li.find("a", href=True)
        link = (KENNY_BASE + a["href"]) if a and a["href"].startswith("/") else (a["href"] if a else "")

        if not TEST_MODE:
            if not location_ok(location):
                continue
            if not price_ok(price):
                continue
            if not km_ok(km):
                continue

        print(f"  [Kenny] Match: {title} | ${price} | {km} km | {location}")
        results.append({
            "source":   "Kenny U-Pull",
            "title":    title,
            "price":    f"${price:,}" if price is not None else "N/A",
            "km":       f"{km:,} km" if km is not None else "N/A",
            "location": location,
            "link":     link,
        })
        if TEST_MODE and len(results) >= 3:
            break

    return results


# ── Kijiji ──────────────────────────────────────────────────────────────────

def scrape_kijiji() -> list[dict]:
    seen  = set()
    results = []

    for kw in KIJIJI_SEARCHES:
        url = KIJIJI_URL.format(kw=kw)
        print(f"\n[Kijiji] Fetching {url} ...")
        try:
            r = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            print(f"  [Kijiji] Error fetching {kw}: {e}")
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # Parse JSON-LD ItemList
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
                # item may be the vehicle directly or wrapped in {"item": {...}}
                vehicle = item.get("item", item)

                link  = vehicle.get("url", "")
                if not link or link in seen:
                    continue
                seen.add(link)

                title = vehicle.get("name", "")
                if not TEST_MODE and not is_wanted(title):
                    continue
                if "lease" in title.lower():
                    continue

                # Location: embedded in URL path, e.g. /v-cars-trucks/barrie/...
                # Location filtering handled by Kijiji's radius search

                price_raw = None
                offers = vehicle.get("offers", {})
                if isinstance(offers, dict):
                    price_raw = offers.get("price")
                price = int(float(price_raw)) if price_raw is not None else None

                km_raw = vehicle.get("mileageFromOdometer", {})
                km = int(float(km_raw.get("value", 0))) if isinstance(km_raw, dict) and km_raw.get("value") else None

                if not TEST_MODE:
                    if not price_ok(price):
                        continue
                    if not km_ok(km):
                        continue

                # Extract city from URL path
                location = ""
                m = re.search(r"/v-cars-trucks/([^/]+)/", link)
                if m:
                    location = m.group(1).replace("-", " ").title()

                print(f"  [Kijiji] Match: {title} | ${price} | {km} km | {location}")
                results.append({
                    "source":   "Kijiji",
                    "title":    title,
                    "price":    f"${price:,}" if price is not None else "N/A",
                    "km":       f"{km:,} km" if km is not None else "N/A",
                    "location": location,
                    "link":     link,
                })
                if TEST_MODE and len(results) >= 3:
                    return results

    return results


# ── Discord ─────────────────────────────────────────────────────────────────

COLORS = {"Kenny U-Pull": 0xDA291C, "Kijiji": 0x373373}

def send_discord(listings: list[dict]) -> None:
    if not listings:
        payload = {
            "username": "Van Bot",
            "content":  "No matching listings found right now. Will check again later.",
        }
    else:
        embeds = []
        for car in listings[:10]:
            embeds.append({
                "title":  f"[{car['source']}] {car['title']}",
                "url":    car["link"] or None,
                "color":  COLORS.get(car["source"], 0x00AA00),
                "fields": [
                    {"name": "Price",    "value": car["price"],    "inline": True},
                    {"name": "KM",       "value": car["km"],       "inline": True},
                    {"name": "Location", "value": car["location"] or "N/A", "inline": True},
                ],
            })
        label = (
            f"TEST — first listings from Kenny + Kijiji ({len(listings)} total):"
            if TEST_MODE
            else f"Found **{len(listings)}** matching vehicle(s) under $5,000 and 200,000 km:"
        )
        payload = {
            "username": "Van Bot",
            "content":  label,
            "embeds":   embeds,
        }
        if len(listings) > 10:
            payload["content"] += f"\n_(showing first 10 of {len(listings)})_"

    r = httpx.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    r.raise_for_status()
    print("\nDiscord notification sent.")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"TEST_MODE={TEST_MODE}, MAX_PRICE={MAX_PRICE}, MAX_KM={MAX_KM}")

    seen = set() if TEST_MODE else load_seen()

    kenny   = scrape_kenny()
    kijiji  = scrape_kijiji()
    all_listings = kenny + kijiji

    new_listings = [l for l in all_listings if l["link"] not in seen]
    print(f"\nKenny: {len(kenny)} | Kijiji: {len(kijiji)} | Total: {len(all_listings)} | New: {len(new_listings)}")

    send_discord(new_listings)

    if not TEST_MODE:
        seen.update(l["link"] for l in all_listings)
        save_seen(seen)


if __name__ == "__main__":
    main()
