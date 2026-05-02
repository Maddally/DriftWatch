"""
DriftWatch — FastAPI Backend
Serves listing data and price changes to the frontend.

Setup:
  pip install fastapi uvicorn psycopg2-binary python-dotenv

Run:
  uvicorn api:app --reload --port 8000

Endpoints:
  GET /changes          — this week's price changes
  GET /listings         — all active listings with latest price
  GET /listings/{id}    — single listing with full price history
  GET /stats            — summary stats for the week
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
import psycopg2.extras
import os
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="DriftWatch API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this in production
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME", "driftwatch"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )

def get_week_start(d: date = None) -> date:
    d = d or date.today()
    return d - timedelta(days=d.weekday())

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/stats")
def get_stats(week: date = None):
    """
    Summary stats for a given week.
    Returns counts of drops, rises, new listings, unchanged.
    """
    week_start = week or get_week_start()
    db = get_db()
    cur = db.cursor()

    cur.execute("""
        SELECT
          COUNT(*) FILTER (WHERE delta < 0)  AS drops,
          COUNT(*) FILTER (WHERE delta > 0)  AS rises,
          COUNT(*)                           AS total_changes,
          AVG(delta) FILTER (WHERE delta < 0) AS avg_drop,
          MIN(delta)                          AS biggest_drop,
          MAX(delta)                          AS biggest_rise
        FROM price_changes
        WHERE to_week = %s
    """, (week_start,))

    stats = dict(cur.fetchone())

    # Count new listings this week
    cur.execute("""
        SELECT COUNT(*) AS new_listings
        FROM listings
        WHERE DATE_TRUNC('week', first_seen_at) = %s
    """, (week_start,))
    stats["new_listings"] = cur.fetchone()["new_listings"]
    stats["week_start"] = week_start.isoformat()

    cur.close()
    db.close()
    return stats


@app.get("/changes")
def get_changes(
    week:          date  = None,
    city:          str   = None,
    neighborhood:  str   = None,
    change_type:   str   = Query(None, pattern="^(drop|rise|all)$"),
    min_delta_pct: float = None,
    bedrooms:      float = None,
    limit:         int   = Query(100, le=500),
    offset:        int   = 0,
):
    """
    Returns all price changes for a given week.
    Filterable by city, neighborhood, change direction, and % threshold.
    """
    week_start = week or get_week_start()
    db = get_db()
    cur = db.cursor()

    filters = ["c.to_week = %s"]
    params  = [week_start]

    if city:
        filters.append("l.city ILIKE %s")
        params.append(f"%{city}%")
    if neighborhood:
        filters.append("l.neighborhood ILIKE %s")
        params.append(f"%{neighborhood}%")
    if change_type == "drop":
        filters.append("c.delta < 0")
    elif change_type == "rise":
        filters.append("c.delta > 0")
    if min_delta_pct:
        filters.append("ABS(c.delta_pct) >= %s")
        params.append(min_delta_pct)
    if bedrooms:
        filters.append("l.bedrooms = %s")
        params.append(bedrooms)

    where = " AND ".join(filters)

    cur.execute(f"""
        SELECT
          c.id, c.listing_id,
          l.address, l.unit, l.neighborhood, l.city, l.state,
          l.bedrooms, l.bathrooms, l.sqft, l.amenities,
          l.lat, l.lng,
          c.from_week, c.to_week,
          c.price_before, c.price_after, c.delta, c.delta_pct,
          CASE WHEN c.delta < 0 THEN 'drop' WHEN c.delta > 0 THEN 'rise' ELSE 'unchanged' END AS change_type,
          s.source_url
        FROM price_changes c
        JOIN listings l ON l.id = c.listing_id
        LEFT JOIN price_snapshots s ON s.listing_id = c.listing_id AND s.week_start = c.to_week
        WHERE {where}
        ORDER BY ABS(c.delta) DESC
        LIMIT %s OFFSET %s
    """, params + [limit, offset])

    results = [dict(row) for row in cur.fetchall()]
    cur.close()
    db.close()
    return {"week": week_start.isoformat(), "count": len(results), "changes": results}


@app.get("/listings")
def get_listings(
    city:        str   = None,
    neighborhood: str  = None,
    bedrooms:    float = None,
    max_price:   int   = None,
    min_price:   int   = None,
    limit:       int   = Query(100, le=500),
    offset:      int   = 0,
):
    """All active listings with their current price."""
    db = get_db()
    cur = db.cursor()

    filters = ["l.active = TRUE"]
    params  = []

    if city:
        filters.append("l.city ILIKE %s"); params.append(f"%{city}%")
    if neighborhood:
        filters.append("l.neighborhood ILIKE %s"); params.append(f"%{neighborhood}%")
    if bedrooms:
        filters.append("l.bedrooms = %s"); params.append(bedrooms)
    if max_price:
        filters.append("p.price <= %s"); params.append(max_price)
    if min_price:
        filters.append("p.price >= %s"); params.append(min_price)

    where = " AND ".join(filters)

    cur.execute(f"""
        SELECT
          l.id, l.address, l.unit, l.neighborhood, l.city, l.state,
          l.bedrooms, l.bathrooms, l.sqft, l.amenities,
          l.lat, l.lng, l.first_seen_at,
          p.price AS current_price,
          p.week_start AS price_as_of
        FROM listings l
        JOIN latest_prices p ON p.listing_id = l.id
        WHERE {where}
        ORDER BY l.id
        LIMIT %s OFFSET %s
    """, params + [limit, offset])

    results = [dict(row) for row in cur.fetchall()]
    cur.close()
    db.close()
    return {"count": len(results), "listings": results}


@app.get("/listings/{listing_id}")
def get_listing(listing_id: int):
    """Single listing with full price history."""
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT * FROM listings WHERE id = %s", (listing_id,))
    listing = cur.fetchone()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    # Full price history
    cur.execute("""
        SELECT week_start, price, scraped_at, source_url
        FROM price_snapshots
        WHERE listing_id = %s
        ORDER BY week_start ASC
    """, (listing_id,))
    history = [dict(row) for row in cur.fetchall()]

    # All changes
    cur.execute("""
        SELECT from_week, to_week, price_before, price_after, delta, delta_pct
        FROM price_changes
        WHERE listing_id = %s
        ORDER BY to_week ASC
    """, (listing_id,))
    changes = [dict(row) for row in cur.fetchall()]

    cur.close()
    db.close()
    return {**dict(listing), "price_history": history, "price_changes": changes}


@app.get("/weeks")
def get_weeks(limit: int = 12):
    """Returns list of weeks we have data for, with change counts."""
    db = get_db()
    cur = db.cursor()

    cur.execute("""
        SELECT
          to_week,
          COUNT(*) AS total_changes,
          COUNT(*) FILTER (WHERE delta < 0) AS drops,
          COUNT(*) FILTER (WHERE delta > 0) AS rises
        FROM price_changes
        GROUP BY to_week
        ORDER BY to_week DESC
        LIMIT %s
    """, (limit,))

    rows = [dict(row) for row in cur.fetchall()]
    cur.close()
    db.close()
    return rows


@app.get("/health")
def health():
    return {"status": "ok"}
