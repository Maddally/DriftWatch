"""
DriftWatch — RentCast Scraper
Fetches rental listings from RentCast API, stores snapshots,
and detects price changes week over week.

Setup:
  pip install requests psycopg2-binary python-dotenv schedule

Run once:
  python scraper.py --run-now

Run on schedule (every Monday at 6am):
  python scraper.py --schedule
"""

import os
import requests
import psycopg2
import psycopg2.extras
import schedule
import time
import logging
import argparse
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

RENTCAST_API_KEY = os.getenv("RENTCAST_API_KEY")  # https://app.rentcast.io/
RENTCAST_BASE    = "https://api.rentcast.io/v1"

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     os.getenv("DB_PORT", 5432),
    "dbname":   os.getenv("DB_NAME", "driftwatch"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Cities/zips to track — add as many as you want
MARKETS = [
    {"city": "Austin",  "state": "TX", "limit": 500},
    {"city": "Chicago", "state": "IL", "limit": 500},
    {"city": "Denver",  "state": "CO", "limit": 500},
]

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(**DB_CONFIG)

def get_week_start(d: date = None) -> date:
    """Return the Monday of the current (or given) week."""
    d = d or date.today()
    return d - timedelta(days=d.weekday())

# ── RentCast API ──────────────────────────────────────────────────────────────

def fetch_listings(city: str, state: str, limit: int = 500) -> list[dict]:
    """
    Fetch rental listings from RentCast for a given city.
    Docs: https://developers.rentcast.io/reference/rental-listings
    """
    headers = {
        "accept": "application/json",
        "X-Api-Key": RENTCAST_API_KEY,
    }
    params = {
        "city": city,
        "state": state,
        "status": "Active",
        "limit": min(limit, 500),  # RentCast max per request
        "propertyType": "Apartment,Condo",
    }

    all_listings = []
    offset = 0

    while True:
        params["offset"] = offset
        resp = requests.get(f"{RENTCAST_BASE}/listings/rental/long-term", headers=headers, params=params)

        if resp.status_code == 429:
            log.warning("Rate limited. Waiting 60s...")
            time.sleep(60)
            continue

        resp.raise_for_status()
        data = resp.json()

        if not data:
            break

        all_listings.extend(data)
        log.info(f"  Fetched {len(all_listings)} listings from {city}, {state}...")

        if len(data) < 500:
            break  # last page
        offset += 500

    return all_listings

# ── Upsert Logic ──────────────────────────────────────────────────────────────

def upsert_listing(cur, raw: dict) -> tuple[int, bool]:
    """
    Insert listing if new, update last_seen if existing.
    Returns (listing_id, is_new).
    """
    cur.execute("""
        INSERT INTO listings
          (source, source_id, address, unit, city, state, zip, neighborhood,
           lat, lng, bedrooms, bathrooms, sqft, property_type, amenities, last_seen_at)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (source, source_id) DO UPDATE SET
          last_seen_at = NOW(),
          active = TRUE,
          address     = EXCLUDED.address,
          neighborhood = EXCLUDED.neighborhood,
          amenities   = EXCLUDED.amenities
        RETURNING id, (xmax = 0) AS is_new
    """, (
        "rentcast",
        raw.get("id"),
        raw.get("formattedAddress"),
        raw.get("unit"),
        raw.get("city"),
        raw.get("state"),
        raw.get("zipCode"),
        raw.get("county"),          # RentCast doesn't give neighborhood, county is close
        raw.get("latitude"),
        raw.get("longitude"),
        raw.get("bedrooms"),
        raw.get("bathrooms"),
        raw.get("squareFootage"),
        raw.get("propertyType", "").lower(),
        raw.get("features", {}).get("amenities", []),
    ))
    row = cur.fetchone()
    return row["id"], row["is_new"]

def record_snapshot(cur, listing_id: int, price: int, week_start: date, source_url: str):
    """Store a price snapshot for this week (skip if already recorded)."""
    cur.execute("""
        INSERT INTO price_snapshots (listing_id, price, week_start, source_url)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (listing_id, price, week_start, source_url))

def detect_change(cur, listing_id: int, week_start: date):
    """
    Compare this week's price to last week's.
    If different, write a row to price_changes.
    Returns the change delta (or None if no previous data).
    """
    prev_week = week_start - timedelta(weeks=1)

    cur.execute("""
        SELECT price FROM price_snapshots
        WHERE listing_id = %s AND week_start = %s
        ORDER BY scraped_at DESC LIMIT 1
    """, (listing_id, prev_week))
    prev = cur.fetchone()

    cur.execute("""
        SELECT price FROM price_snapshots
        WHERE listing_id = %s AND week_start = %s
        ORDER BY scraped_at DESC LIMIT 1
    """, (listing_id, week_start))
    curr = cur.fetchone()

    if not prev or not curr:
        return None  # not enough history yet

    delta = curr["price"] - prev["price"]
    if delta == 0:
        return 0  # no change, nothing to write

    delta_pct = round((delta / prev["price"]) * 100, 2)

    cur.execute("""
        INSERT INTO price_changes
          (listing_id, from_week, to_week, price_before, price_after, delta, delta_pct)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (listing_id, from_week, to_week) DO UPDATE SET
          price_before = EXCLUDED.price_before,
          price_after  = EXCLUDED.price_after,
          delta        = EXCLUDED.delta,
          delta_pct    = EXCLUDED.delta_pct
    """, (listing_id, prev_week, week_start, prev["price"], curr["price"], delta, delta_pct))

    return delta

# ── Main Scrape Run ───────────────────────────────────────────────────────────

def run_scrape():
    log.info("=" * 60)
    log.info("Starting DriftWatch scrape run")
    week_start = get_week_start()
    log.info(f"Week: {week_start}")

    db = get_db()
    db.autocommit = False
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Log the run
    cur.execute("""
        INSERT INTO scrape_runs (source, status) VALUES ('rentcast', 'running') RETURNING id
    """)
    run_id = cur.fetchone()["id"]
    db.commit()

    total_found = 0
    total_new   = 0
    total_changed = 0

    try:
        for market in MARKETS:
            log.info(f"\nFetching {market['city']}, {market['state']}...")
            listings = fetch_listings(market["city"], market["state"], market["limit"])
            log.info(f"  Got {len(listings)} listings")

            for raw in listings:
                price = raw.get("price")
                if not price:
                    continue  # skip if no rent listed

                listing_id, is_new = upsert_listing(cur, raw)
                total_found += 1
                if is_new:
                    total_new += 1

                source_url = f"https://app.rentcast.io/listings/{raw.get('id')}"
                record_snapshot(cur, listing_id, int(price), week_start, source_url)

                delta = detect_change(cur, listing_id, week_start)
                if delta and delta != 0:
                    total_changed += 1
                    direction = "↓" if delta < 0 else "↑"
                    log.info(f"  {direction} Price change: {raw.get('formattedAddress')} — ${delta:+,}/mo")

            db.commit()

        # Mark run as success
        cur.execute("""
            UPDATE scrape_runs SET
              finished_at     = NOW(),
              status          = 'success',
              listings_found  = %s,
              listings_new    = %s,
              prices_changed  = %s
            WHERE id = %s
        """, (total_found, total_new, total_changed, run_id))
        db.commit()

        log.info(f"\n✓ Done. Found: {total_found} | New: {total_new} | Changed: {total_changed}")

    except Exception as e:
        db.rollback()
        cur.execute("""
            UPDATE scrape_runs SET status = 'error', error_message = %s, finished_at = NOW()
            WHERE id = %s
        """, (str(e), run_id))
        db.commit()
        log.error(f"Scrape failed: {e}")
        raise

    finally:
        cur.close()
        db.close()

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-now", action="store_true", help="Run immediately")
    parser.add_argument("--schedule", action="store_true", help="Run on weekly schedule")
    args = parser.parse_args()

    if args.run_now:
        run_scrape()

    elif args.schedule:
        log.info("Scheduling scraper for every Monday at 6:00 AM...")
        schedule.every().monday.at("06:00").do(run_scrape)
        while True:
            schedule.run_pending()
            time.sleep(60)

    else:
        print("Usage: python scraper.py --run-now | --schedule")
