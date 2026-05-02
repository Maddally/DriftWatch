# DriftWatch — Backend Setup

## What's in here

```
driftwatch/
  db/
    schema.sql        ← Run this first to set up your database
  scraper/
    scraper.py        ← Fetches from RentCast, detects price changes
  api/
    api.py            ← FastAPI server the frontend calls
  .env.example        ← Copy to .env and fill in your keys
```

---

## Step 1 — Install dependencies

```bash
pip install fastapi uvicorn psycopg2-binary python-dotenv requests schedule
```

---

## Step 2 — Set up PostgreSQL

```bash
# Create the database
createdb driftwatch

# Run the schema
psql -d driftwatch -f db/schema.sql
```

---

## Step 3 — Add your API keys

```bash
cp .env.example .env
# Edit .env with your RentCast key and DB password
```

Get a RentCast API key at: https://app.rentcast.io/
Pricing is ~$50/mo for 1,000 requests. One city scrape = ~2-5 requests.

---

## Step 4 — Run your first scrape

```bash
cd scraper
python scraper.py --run-now
```

This will:
1. Fetch all active rental listings for your configured cities
2. Store them in the `listings` table
3. Record this week's prices in `price_snapshots`
4. Compare to last week and write diffs to `price_changes`

---

## Step 5 — Start the API

```bash
cd api
uvicorn api:app --reload --port 8000
```

Test it:
- http://localhost:8000/stats
- http://localhost:8000/changes
- http://localhost:8000/changes?change_type=drop&city=Austin
- http://localhost:8000/listings/1

---

## Step 6 — Schedule weekly scrapes

```bash
python scraper.py --schedule
# Runs every Monday at 6:00 AM automatically
```

For production, use a cron job or deploy to Railway/Render with a cron trigger.

---

## API Reference

| Endpoint | Description |
|---|---|
| `GET /stats` | Drop/rise counts for the week |
| `GET /changes` | All price changes (filterable) |
| `GET /changes?change_type=drop` | Only drops |
| `GET /changes?city=Austin&min_delta_pct=5` | Drops >5% in Austin |
| `GET /listings` | All active listings |
| `GET /listings/{id}` | Single listing + full history |
| `GET /weeks` | All weeks with change counts |

---

## Adding more cities

In `scraper/scraper.py`, edit the `MARKETS` list:

```python
MARKETS = [
    {"city": "Austin",   "state": "TX", "limit": 500},
    {"city": "New York", "state": "NY", "limit": 500},
    {"city": "Seattle",  "state": "WA", "limit": 500},
]
```
