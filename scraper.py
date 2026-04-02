#!/usr/bin/env python3
"""
Kenny U-Pull cargo van scraper.
Finds cargo vans under $7,500 and under 200,000 km and posts to Discord.
"""
import asyncio
import os
import re
import sys
import httpx
from playwright.async_api import async_playwright

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
MAX_PRICE = 7_500
MAX_KM = 200_000
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

        # Try common card selectors used by WP-based inventory sites
        card_selectors = [
            ".car-card",
            ".vehicle-card",
            ".listing-card",
            "[class*='car-item']",
            "[class*='vehicle-item']",
            "[class*='listing-item']",
            "article.type-car",
            "article.type-post",
            ".cars-list article",
            ".post-type-car",
            ".wp-block-post",
            "article",
        ]

        cards = []
        used_selector = ""
        for selector in card_selectors:
            found = await page.query_selector_all(selector)
            if found:
                print(f"Found {len(found)} elements with selector '{selector}'")
                cards = found
                used_selector = selector
                break

        if not cards:
            html = await page.content()
            with open("page_dump.html", "w") as f:
                f.write(html)
            print("No listing cards found. HTML saved to page_dump.html for debugging.")
            await browser.close()
            return []

        print(f"Processing {len(cards)} cards from '{used_selector}' ...")

        for card in cards:
            text = await card.inner_text()
            text_lower = text.lower()

            # Only keep cargo van listings
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

            # --- Apply filters ---
            if price is not None and price > MAX_PRICE:
                print(f"  Skip (price ${price:,} > ${MAX_PRICE:,}): {title}")
                continue
            if km is not None and km > MAX_KM:
                print(f"  Skip (km {km:,} > {MAX_KM:,}): {title}")
                continue

            print(f"  Match: {title} | ${price:,} | {km:,} km | {link}")
            results.append(
                {
                    "title": title,
                    "price": f"${price:,}" if price is not None else "N/A",
                    "km": f"{km:,} km" if km is not None else "N/A",
                    "link": link,
                }
            )

        await browser.close()
    return results


def send_discord(listings: list[dict]) -> None:
    if not listings:
        print("No matching listings — sending 'nothing found' message to Discord.")
        payload = {
            "username": "Kenny U-Pull Bot",
            "content": "No cargo vans found under $7,500 and 200,000 km right now. Will check again later.",
        }
    else:
        embeds = []
        for car in listings[:10]:  # Discord allows max 10 embeds per message
            embeds.append(
                {
                    "title": car["title"],
                    "url": car["link"] or None,
                    "color": 0x00AA00,
                    "fields": [
                        {"name": "Price", "value": car["price"], "inline": True},
                        {"name": "KM", "value": car["km"], "inline": True},
                    ],
                }
            )
        payload = {
            "username": "Kenny U-Pull Bot",
            "content": f"Found **{len(listings)}** cargo van(s) under $7,500 and 200,000 km at Kenny U-Pull:",
            "embeds": embeds,
        }
        if len(listings) > 10:
            payload["content"] += f"\n_(showing first 10 of {len(listings)})_"

    r = httpx.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    r.raise_for_status()
    print("Discord notification sent.")


async def main() -> None:
    if os.environ.get("TEST_MODE"):
        listings = [
            {
                "title": "2012 Ford Transit Connect Cargo Van",
                "price": "$5,995",
                "km": "187,432 km",
                "link": "https://kennyupull.com/cars-for-sale/",
            }
        ]
    else:
        listings = await scrape()
    print(f"\nTotal matching listings: {len(listings)}")
    send_discord(listings)


if __name__ == "__main__":
    asyncio.run(main())
