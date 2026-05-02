-- DriftWatch Database Schema
-- Run this in PostgreSQL to set up your database

-- ============================================================
-- LISTINGS: One row per unique apartment unit
-- ============================================================
CREATE TABLE listings (
  id            SERIAL PRIMARY KEY,
  source        TEXT NOT NULL,           -- 'rentcast', 'craigslist', 'manual'
  source_id     TEXT NOT NULL,           -- ID from the source (e.g. RentCast listing ID)
  address       TEXT NOT NULL,
  unit          TEXT,                    -- e.g. "Apt 3B"
  city          TEXT NOT NULL,
  state         TEXT NOT NULL,
  zip           TEXT,
  neighborhood  TEXT,
  lat           NUMERIC(9,6),
  lng           NUMERIC(9,6),
  bedrooms      NUMERIC(3,1),
  bathrooms     NUMERIC(3,1),
  sqft          INT,
  property_type TEXT,                   -- 'apartment', 'condo', 'house', 'studio'
  amenities     TEXT[],                 -- ['pets', 'laundry', 'parking']
  first_seen_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at  TIMESTAMPTZ DEFAULT NOW(),
  active        BOOLEAN DEFAULT TRUE,
  UNIQUE (source, source_id)            -- prevent duplicate listings
);

-- ============================================================
-- PRICE SNAPSHOTS: Every time we scrape, store the price
-- ============================================================
CREATE TABLE price_snapshots (
  id          SERIAL PRIMARY KEY,
  listing_id  INT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  price       INT NOT NULL,             -- monthly rent in dollars
  scraped_at  TIMESTAMPTZ DEFAULT NOW(),
  week_start  DATE NOT NULL,            -- Monday of the week this snapshot belongs to
  source_url  TEXT                      -- direct link to the listing
);

-- Index for fast lookups by listing + week
CREATE INDEX idx_snapshots_listing_week ON price_snapshots(listing_id, week_start);
CREATE INDEX idx_snapshots_week ON price_snapshots(week_start);

-- ============================================================
-- PRICE CHANGES: Computed diff between consecutive weeks
-- ============================================================
CREATE TABLE price_changes (
  id              SERIAL PRIMARY KEY,
  listing_id      INT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  from_week       DATE NOT NULL,
  to_week         DATE NOT NULL,
  price_before    INT NOT NULL,
  price_after     INT NOT NULL,
  delta           INT NOT NULL,           -- price_after - price_before (negative = drop)
  delta_pct       NUMERIC(5,2) NOT NULL,  -- percentage change
  detected_at     TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (listing_id, from_week, to_week)
);

CREATE INDEX idx_changes_to_week ON price_changes(to_week);
CREATE INDEX idx_changes_delta ON price_changes(delta);

-- ============================================================
-- SCRAPE RUNS: Log every time we run the scraper
-- ============================================================
CREATE TABLE scrape_runs (
  id            SERIAL PRIMARY KEY,
  source        TEXT NOT NULL,
  started_at    TIMESTAMPTZ DEFAULT NOW(),
  finished_at   TIMESTAMPTZ,
  listings_found  INT DEFAULT 0,
  listings_new    INT DEFAULT 0,
  prices_changed  INT DEFAULT 0,
  status        TEXT DEFAULT 'running',  -- 'running', 'success', 'error'
  error_message TEXT
);

-- ============================================================
-- USERS & ALERTS (for email notifications)
-- ============================================================
CREATE TABLE users (
  id         SERIAL PRIMARY KEY,
  email      TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE alert_subscriptions (
  id           SERIAL PRIMARY KEY,
  user_id      INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  listing_id   INT REFERENCES listings(id) ON DELETE CASCADE,   -- watch a specific unit
  neighborhood TEXT,                                             -- OR watch a whole area
  city         TEXT,
  min_drop_pct NUMERIC(5,2) DEFAULT 5.0,  -- only alert if drop is >= this %
  active       BOOLEAN DEFAULT TRUE,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- HELPER VIEW: Latest price per listing
-- ============================================================
CREATE VIEW latest_prices AS
SELECT DISTINCT ON (listing_id)
  listing_id,
  price,
  week_start,
  scraped_at
FROM price_snapshots
ORDER BY listing_id, week_start DESC;

-- ============================================================
-- HELPER VIEW: This week's changes with listing details
-- ============================================================
CREATE VIEW weekly_changes AS
SELECT
  c.id,
  c.listing_id,
  l.address,
  l.unit,
  l.neighborhood,
  l.city,
  l.bedrooms,
  l.bathrooms,
  l.sqft,
  l.amenities,
  l.lat,
  l.lng,
  c.from_week,
  c.to_week,
  c.price_before,
  c.price_after,
  c.delta,
  c.delta_pct,
  CASE
    WHEN c.delta < 0 THEN 'drop'
    WHEN c.delta > 0 THEN 'rise'
    ELSE 'unchanged'
  END AS change_type
FROM price_changes c
JOIN listings l ON l.id = c.listing_id;
