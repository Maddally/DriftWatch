-- ============================================================
-- DriftWatch — Submission System Schema
-- Run this AFTER schema.sql
-- psql -d driftwatch -f db/schema_submissions.sql
-- ============================================================

-- Pending submissions from landlords (not yet approved)
CREATE TABLE submissions (
  id              SERIAL PRIMARY KEY,
  -- Contact
  landlord_name   TEXT NOT NULL,
  landlord_email  TEXT NOT NULL,
  landlord_phone  TEXT NOT NULL,
  -- Property
  address         TEXT NOT NULL,
  unit            TEXT,
  city            TEXT NOT NULL,
  state           TEXT NOT NULL,
  zip             TEXT NOT NULL,
  neighborhood    TEXT,
  lat             NUMERIC(9,6),
  lng             NUMERIC(9,6),
  -- Listing details
  bedrooms        NUMERIC(3,1) NOT NULL,
  bathrooms       NUMERIC(3,1) NOT NULL,
  sqft            INT,
  price           INT NOT NULL,            -- monthly rent
  available_date  DATE,
  amenities       TEXT[],
  description     TEXT,
  -- Status tracking
  status          TEXT DEFAULT 'pending',  -- pending, email_verified, approved, rejected, spam
  rejection_reason TEXT,
  -- Verification
  verify_token    TEXT UNIQUE,             -- emailed to landlord
  verified_at     TIMESTAMPTZ,
  -- Rate limiting
  submitter_ip    TEXT,
  -- Timestamps
  submitted_at    TIMESTAMPTZ DEFAULT NOW(),
  reviewed_at     TIMESTAMPTZ,
  reviewed_by     TEXT,
  -- Flags
  price_flagged   BOOLEAN DEFAULT FALSE,   -- auto-flagged for unusual price
  address_valid   BOOLEAN,                 -- geocoding check result
  -- Link to listing once approved
  listing_id      INT REFERENCES listings(id)
);

CREATE INDEX idx_submissions_status ON submissions(status);
CREATE INDEX idx_submissions_email  ON submissions(landlord_email);
CREATE INDEX idx_submissions_token  ON submissions(verify_token);
CREATE INDEX idx_submissions_ip     ON submissions(submitter_ip);

-- Rate limit log (track submissions per email/IP per day)
CREATE TABLE submission_rate_log (
  id          SERIAL PRIMARY KEY,
  identifier  TEXT NOT NULL,   -- email or IP
  kind        TEXT NOT NULL,   -- 'email' or 'ip'
  submitted_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_rate_log ON submission_rate_log(identifier, submitted_at);

-- Admin users (for the review queue)
CREATE TABLE admin_users (
  id           SERIAL PRIMARY KEY,
  email        TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- View: pending submissions needing review
CREATE VIEW pending_review AS
SELECT
  s.*,
  CASE
    WHEN s.price < 500  THEN 'Price unusually low'
    WHEN s.price > 15000 THEN 'Price unusually high'
    WHEN NOT s.address_valid THEN 'Address could not be verified'
    ELSE NULL
  END AS auto_flag_reason
FROM submissions s
WHERE s.status = 'email_verified'
ORDER BY s.submitted_at ASC;
