#!/usr/bin/env python3
"""
Kenny U-Pull cargo van scraper.
Scrapes kennyautos.com (the iframe behind kennyupull.com/cars-for-sale/)
and posts matching cargo vans to Discord.

Set TEST_MODE=true to disable filters and return the first 3 listings found.
"""
import os
import re
import httpx
from bs4 import BeautifulSoup

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
MAX_PRICE = 99_999 if TEST_MODE else 7_500
MAX_KM = 999_999 if TEST_MODE else 200_000
LISTING_URL = "https://kennyautos.com/iframe-index.asp?lg=EN"
BASE_URL = "https://kennyautos.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kennyupull.com/",
}


def extract_number(text: str) -> int | None:
    cleaned = re.sub(r"[,$\s]", "", text)
    match = re.search(r"\d+", cleaned)
    return int(match.group()) if match else None


def scrape() -> list[dict]:
    print(f"Fetching {LISTING_URL} ...")
    r = httpx.get(LISTING_URL, headers=HEADERS, timeout=30, follow_redirects=True)
    r.raise_for_status()
    print(f"Got {len(r.text):,} chars")

    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.find_all("li")
    print(f"Found {len(items)} <li> elements")

    results = []
    for li in items:
        h5 = li.find("h5")
        if not h5:
            continue

        title = h5.get_text(strip=True)
        title_upper = title.upper()

        # In test mode skip type filter; otherwise only cargo/van
        if not TEST_MODE:
            if "CARGO" not in title_upper and "VAN" not in title_upper:
                continue

        # --- Price: <strong> containing $ ---
        price: int | None = None
        for strong in li.find_all("strong"):
            text = strong.get_text(strip=True)
            if "$" in text:
                price = extract_number(text)
                break

        # --- KM: <strong> containing "km" ---
        km: int | None = None
        for strong in li.find_all("strong"):
            text = strong.get_text(strip=True)
            if "km" in text.lower():
                km = extract_number(text)
                break

        # --- Link ---
        a = li.find("a", href=True)
        link = (BASE_URL + a["href"]) if a and a["href"].startswith("/") else (a["href"] if a else "")

        # --- Filters ---
        if price is not None and price > MAX_PRICE:
            print(f"  Skip (${price:,} > ${MAX_PRICE:,}): {title}")
            continue
        if km is not None and km > MAX_KM:
            print(f"  Skip ({km:,}km > {MAX_KM:,}km): {title}")
            continue

        print(f"  Match: {title} | ${price:,} | {km:,} km | {link}" if price and km else f"  Match: {title} | price={price} | km={km} | {link}")
        results.append({
            "title": title,
            "price": f"${price:,}" if price is not None else "N/A",
            "km": f"{km:,} km" if km is not None else "N/A",
            "link": link,
        })

        if TEST_MODE and len(results) >= 3:
            break

    return results


def send_discord(listings: list[dict]) -> None:
    if not listings:
        payload = {
            "username": "Kenny U-Pull Bot",
            "content": "No cargo vans found under $7,500 and 200,000 km right now. Will check again later.",
        }
    else:
        embeds = []
        for car in listings[:10]:
            embeds.append({
                "title": car["title"],
                "url": car["link"] or None,
                "color": 0x00AA00,
                "fields": [
                    {"name": "Price", "value": car["price"], "inline": True},
                    {"name": "KM", "value": car["km"], "inline": True},
                ],
            })
        label = (
            "TEST — first 3 real listings from Kenny U-Pull:"
            if TEST_MODE
            else f"Found **{len(listings)}** cargo van(s) under $7,500 and 200,000 km at Kenny U-Pull:"
        )
        payload = {
            "username": "Kenny U-Pull Bot",
            "content": label,
            "embeds": embeds,
        }
        if len(listings) > 10:
            payload["content"] += f"\n_(showing first 10 of {len(listings)})_"

    r = httpx.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    r.raise_for_status()
    print("Discord notification sent.")


def main() -> None:
    print(f"TEST_MODE={TEST_MODE}, MAX_PRICE={MAX_PRICE}, MAX_KM={MAX_KM}")
    listings = scrape()
    print(f"\nTotal matching listings: {len(listings)}")
    send_discord(listings)


if __name__ == "__main__":
    main()
