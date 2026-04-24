"""
Facebook Ecom Page Tracker Bot
Monitors Facebook pages for new products, price changes, best sellers
Sends Telegram notifications
"""

import os
import json
import hashlib
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BOT_TOKEN = "8453822307:AAENJzNeWcrdzoBNP9sR3Rkcm2yCp0Z6Ox4"
CHAT_ID   = "7975203420"
DB_FILE   = Path("data/snapshots.json")
CHECK_INTERVAL_MINUTES = 30  # Check every 30 minutes

PAGES = [
    "lumza",
    "Akazashop",
    "hexa.ma",
    "vayara",
    "werlma",
    "jemadour",
    "Narami.shop",
    "vidah.ma",
    "evashop",
    "Page Ahlam",
    "zenova.beauty",
    "chridaba.ma",
    "Ahlashop Maroc",
    "Romastic",
    "EvaStore",
    "Lalla Moulati",
]

# Build Facebook URLs from page names
def page_to_url(name):
    slug = name.lower().replace(" ", "").replace(".", "")
    return f"https://www.facebook.com/{slug}"

PAGE_URLS = {p: page_to_url(p) for p in PAGES}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,ar;q=0.8,en;q=0.7",
}

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def load_db():
    DB_FILE.parent.mkdir(exist_ok=True)
    if DB_FILE.exists():
        with open(DB_FILE) as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

# ─── SCRAPING ─────────────────────────────────────────────────────────────────

def scrape_page(page_name: str, url: str) -> dict:
    """Scrape a Facebook page and extract post/product info."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"[{page_name}] HTTP {resp.status_code}")
            return {}

        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract all text content from the page
        posts = []

        # Try to find post-like elements (FB renders differently per session)
        # We look for divs with substantial text content
        text_blocks = soup.find_all(string=True)
        content_pieces = []
        for t in text_blocks:
            cleaned = t.strip()
            if len(cleaned) > 30:  # Only meaningful text
                content_pieces.append(cleaned)

        # Create a hash fingerprint of the page content
        page_text = " ".join(content_pieces[:50])  # First 50 meaningful texts
        content_hash = hashlib.md5(page_text.encode()).hexdigest()

        # Try to extract product-like info (prices, product names)
        products = extract_products(page_text)

        return {
            "hash": content_hash,
            "products": products,
            "raw_text": page_text[:2000],  # Store first 2000 chars
            "scraped_at": datetime.now().isoformat(),
            "url": url,
        }

    except Exception as e:
        logger.error(f"[{page_name}] Scrape error: {e}")
        return {}


def extract_products(text: str) -> list:
    """Extract product-like mentions and prices from text."""
    products = []

    # Price patterns (MAD, DH, درهم)
    price_pattern = re.compile(
        r"(\d[\d\s,.]*)\s*(MAD|DH|درهم|dh|mad|Dh)",
        re.IGNORECASE
    )

    prices = price_pattern.findall(text)
    for price, currency in prices[:10]:  # Max 10 prices
        price_clean = price.strip().replace(" ", "")
        products.append({
            "price": price_clean,
            "currency": currency.upper(),
        })

    return products


# ─── DIFF & NOTIFICATIONS ─────────────────────────────────────────────────────

def detect_changes(page_name: str, old_data: dict, new_data: dict) -> list:
    """Compare old and new snapshots, return list of detected changes."""
    changes = []

    if not old_data:
        # First time seeing this page
        changes.append({
            "type": "first_check",
            "message": f"✅ Première vérification effectuée pour *{page_name}*"
        })
        return changes

    # Check if content changed
    if old_data.get("hash") != new_data.get("hash"):
        old_prices = {p["price"] for p in old_data.get("products", [])}
        new_prices = {p["price"] for p in new_data.get("products", [])}

        # New prices appeared
        added_prices = new_prices - old_prices
        removed_prices = old_prices - new_prices

        if added_prices:
            for price in added_prices:
                changes.append({
                    "type": "new_product",
                    "price": price,
                    "message": (
                        f"🆕 *Nouveau produit/prix détecté!*\n"
                        f"🏪 Page: *{page_name}*\n"
                        f"💰 Prix: *{price} MAD*"
                    )
                })

        if removed_prices and not added_prices:
            changes.append({
                "type": "content_update",
                "message": (
                    f"🔄 *Mise à jour détectée!*\n"
                    f"🏪 Page: *{page_name}*\n"
                    f"📝 Le contenu de la page a changé"
                )
            })

        if not added_prices and not removed_prices:
            # General content change
            changes.append({
                "type": "content_update",
                "message": (
                    f"📢 *Nouveau post/contenu!*\n"
                    f"🏪 Page: *{page_name}*\n"
                    f"🔗 {new_data.get('url', '')}"
                )
            })

    return changes


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

async def send_notification(bot: Bot, message: str, url: str = ""):
    """Send a Telegram message."""
    try:
        full_msg = message
        if url:
            full_msg += f"\n🔗 [Voir la page]({url})"
        full_msg += f"\n⏰ _{datetime.now().strftime('%H:%M - %d/%m/%Y')}_"

        await bot.send_message(
            chat_id=CHAT_ID,
            text=full_msg,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )
        logger.info("Notification sent!")
    except Exception as e:
        logger.error(f"Telegram error: {e}")


# ─── MAIN CHECK LOOP ──────────────────────────────────────────────────────────

async def check_all_pages(bot: Bot):
    """Check all pages for changes."""
    logger.info(f"🔍 Checking {len(PAGE_URLS)} pages...")
    db = load_db()
    changes_found = 0

    for page_name, url in PAGE_URLS.items():
        logger.info(f"  Checking: {page_name}")
        new_data = scrape_page(page_name, url)

        if not new_data:
            continue

        old_data = db.get(page_name, {})
        changes = detect_changes(page_name, old_data, new_data)

        for change in changes:
            if change["type"] != "first_check":  # Don't spam on first run
                await send_notification(bot, change["message"], url)
                changes_found += 1
                await asyncio.sleep(1)  # Avoid rate limits

        # Save new snapshot
        db[page_name] = new_data
        await asyncio.sleep(2)  # Be nice to Facebook's servers

    save_db(db)
    logger.info(f"✅ Check complete. {changes_found} changes found.")

    if changes_found == 0:
        logger.info("No changes detected this round.")


async def send_startup_message(bot: Bot):
    """Send a message when bot starts."""
    pages_list = "\n".join([f"  • {p}" for p in PAGES if p.strip()])
    msg = (
        f"🚀 *EcomTracker démarré!*\n\n"
        f"📋 *Pages surveillées ({len([p for p in PAGES if p.strip()])}) :*\n"
        f"{pages_list}\n\n"
        f"⏱ Vérification toutes les *{CHECK_INTERVAL_MINUTES} minutes*\n"
        f"📬 Tu recevras une notification pour:\n"
        f"  🆕 Nouveau produit/post\n"
        f"  💰 Changement de prix\n"
        f"  📢 Mise à jour de contenu"
    )
    await bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)

    # Send startup notification
    await send_startup_message(bot)

    # Run first check immediately
    await check_all_pages(bot)

    # Schedule recurring checks
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_all_pages,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="page_checker",
    )
    scheduler.start()
    logger.info(f"Scheduler started. Next check in {CHECK_INTERVAL_MINUTES} min.")

    # Keep running
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
