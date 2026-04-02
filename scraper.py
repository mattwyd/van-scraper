#!/usr/bin/env python3
"""
Kenny U-Pull scraper.
Finds cargo vans under $7,500 and under 200,000 km and posts to Discord.
Set TEST_MODE=true to find any car with no filters (for debugging).
"""
import asyncio
import os
import re
import httpx
from playwright.async_api import async_playwright

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
MAX_PRICE = 99_999 if TEST_MODE else 7_500
MAX_KM = 999_999 if TEST_MODE else 200_000
URL = "https://kennyupull.com/cars-for-sale/"


def extract_number(text: str) -> int | None:
    cleaned = re.sub(r"[,$\s]", "", text)
    match = re.search(r"\d+", cleaned)
    return int(match.group()) if match else None


async def scrape() -> list[dict]:
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )

        print(f"Loading {URL} ...")
        await page.goto(URL, wait_until="networkidle", timeout=60_000)

        # Extra wait for lazy-loaded content
        await page.wait_for_timeout(3000)

        # Dump HTML for debugging
        html = await page.content()
        with open("page_dump.html", "w") as f:
            f.write(html)
        print(f"Page HTML dumped ({len(html):,} chars)")

        # Try every plausible selector, widest to narrowest
        card_selectors = [
            ".car-card",
            ".vehicle-card",
            ".listing-card",
            "[class*='car-item']",
            "[class*='vehicle-item']",
            "[class*='listing-item']",
            "[class*='inventory']",
            "[class*='product']",
            "article.type-car",
            ".cars-list article",
            ".post-type-car",
            ".wp-block-post",
            ".entry",
            "article",
        ]

        cards = []
        used_selector = ""
        for selector in card_selectors:
            found = await page.query_selector_all(selector)
            if found:
                print(f"Selector '{selector}' → {len(found)} elements")
                cards = found
                used_selector = selector
                break

        if not cards:
            print("No cards found with any selector — check page_dump.html artifact")
            await browser.close()
            return []

        print(f"Using '{used_selector}', found {len(cards)} cards")

        for card in cards:
            text = await card.inner_text()
            text_lower = text.lower()

            # In test mode find any car; in normal mode only cargo vans
            if not TEST_MODE:
                if "cargo" not in text_lower and "van" not in text_lower:
                    continue

            # --- Title ---
            title = ""
            for sel in ["h2", "h3", "h4", ".title", "[class*='title']", "[class*='name']"]:
                el = await card.query_selector(sel)
                if el:
                    title = (await el.inner_text()).strip()
                    break
            if not title:
                title = text.split("\n")[0].strip()

            # --- Price ---
            price: int | None = None
            price_el = await card.query_selector("[class*='price']")
            price_text = (await price_el.inner_text()) if price_el else ""
            if not price_text:
                m = re.search(r"\$\s*[\d,]+", text)
                price_text = m.group() if m else ""
            if price_text:
                price = extract_number(price_text)

            # --- KM ---
            km: int | None = None
            m = re.search(r"([\d,]+)\s*km", text, re.IGNORECASE)
            if m:
                km = extract_number(m.group(1))

            # --- Link ---
            link = ""
            link_el = await card.query_selector("a[href]")
            if link_el:
                link = await link_el.get_attribute("href") or ""

            # --- Filters ---
            if price is not None and price > MAX_PRICE:
                continue
            if km is not None and km > MAX_KM:
                continue

            print(f"  Match: {title} | price={price} | km={km} | {link}")
            results.append({
                "title": title,
                "price": f"${price:,}" if price is not None else "N/A",
                "km": f"{km:,} km" if km is not None else "N/A",
                "link": link,
            })

            if TEST_MODE and len(results) >= 3:
                break  # just grab first 3 in test mode

        await browser.close()
    return results


def send_discord(listings: list[dict]) -> None:
    if not listings:
        payload = {
            "username": "Kenny U-Pull Bot",
            "content": (
                "No matching listings found right now. "
                + ("(TEST MODE — scraper couldn't find any cards, check the artifact)" if TEST_MODE else "Will check again later.")
            ),
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
        label = "TEST — first 3 listings found:" if TEST_MODE else f"Found **{len(listings)}** cargo van(s) under $7,500 and 200,000 km at Kenny U-Pull:"
        payload = {
            "username": "Kenny U-Pull Bot",
            "content": label,
            "embeds": embeds,
        }

    r = httpx.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    r.raise_for_status()
    print("Discord notification sent.")


async def main() -> None:
    print(f"TEST_MODE={TEST_MODE}, MAX_PRICE={MAX_PRICE}, MAX_KM={MAX_KM}")
    listings = await scrape()
    print(f"\nTotal matching listings: {len(listings)}")
    send_discord(listings)


if __name__ == "__main__":
    asyncio.run(main())
