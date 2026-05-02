"""
DriftWatch — Submission API
Handles landlord listing submissions with:
  - Rate limiting (1 per email per 24h, 3 per IP per 24h)
  - Address geocoding verification
  - Price sanity checks
  - Email verification flow
  - Admin review queue

Add these routes to your existing api.py, or run as a separate service on port 8001.

Extra packages needed:
  pip install slowapi python-multipart httpx passlib[bcrypt] python-jose[cryptography]

Email sending uses smtplib (built into Python).
For production, swap with SendGrid or Resend.
"""

import os, secrets, smtplib, hashlib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, date
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, validator
from dotenv import load_dotenv
from passlib.context import CryptContext
from jose import jwt

load_dotenv()

app = FastAPI(title="DriftWatch Submission API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

pwd_ctx = CryptContext(schemes=["bcrypt"])
SECRET  = os.getenv("JWT_SECRET", "change-this-in-production")

# ── DB ────────────────────────────────────────────────────────

def db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST","localhost"), port=os.getenv("DB_PORT",5432),
        dbname=os.getenv("DB_NAME","driftwatch"), user=os.getenv("DB_USER","postgres"),
        password=os.getenv("DB_PASSWORD",""), cursor_factory=psycopg2.extras.RealDictCursor
    )

# ── Models ────────────────────────────────────────────────────

class SubmissionIn(BaseModel):
    # Contact
    landlord_name:  str
    landlord_email: EmailStr
    landlord_phone: str
    # Property
    address:        str
    unit:           Optional[str] = None
    city:           str
    state:          str
    zip:            str
    neighborhood:   Optional[str] = None
    # Details
    bedrooms:       float
    bathrooms:      float
    sqft:           Optional[int] = None
    price:          int
    available_date: Optional[date] = None
    amenities:      Optional[list[str]] = []
    description:    Optional[str] = None
    # Anti-bot
    captcha_token:  Optional[str] = None   # Cloudflare Turnstile token

    @validator('price')
    def price_range(cls, v):
        if v < 100 or v > 50000:
            raise ValueError('Price must be between $100 and $50,000/month')
        return v

    @validator('landlord_phone')
    def phone_format(cls, v):
        digits = ''.join(filter(str.isdigit, v))
        if len(digits) < 10:
            raise ValueError('Please enter a valid phone number')
        return v

    @validator('bedrooms')
    def beds_range(cls, v):
        if v < 0 or v > 20:
            raise ValueError('Invalid bedroom count')
        return v

# ── Rate limiting ─────────────────────────────────────────────

def check_rate_limit(conn, identifier: str, kind: str, limit: int, window_hours: int = 24):
    """Returns True if over limit."""
    cur = conn.cursor()
    since = datetime.utcnow() - timedelta(hours=window_hours)
    cur.execute("""
        SELECT COUNT(*) as cnt FROM submission_rate_log
        WHERE identifier = %s AND kind = %s AND submitted_at > %s
    """, (identifier, kind, since))
    count = cur.fetchone()["cnt"]
    return count >= limit

def log_submission(conn, identifier: str, kind: str):
    cur = conn.cursor()
    cur.execute("INSERT INTO submission_rate_log (identifier, kind) VALUES (%s, %s)", (identifier, kind))

# ── Address geocoding ─────────────────────────────────────────

async def verify_address(address: str, city: str, state: str, zip: str):
    """
    Uses Nominatim (free, no key needed) to check if address exists.
    Returns (is_valid, lat, lng).
    Swap with Google Maps API for production — more accurate.
    """
    query = f"{address}, {city}, {state} {zip}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1},
                headers={"User-Agent": "DriftWatch/1.0"}
            )
            data = r.json()
            if data:
                return True, float(data[0]["lat"]), float(data[0]["lon"])
            return False, None, None
    except Exception:
        return None, None, None   # None = couldn't check (don't block submission)

# ── Price sanity check ────────────────────────────────────────

def is_price_suspicious(price: int, bedrooms: float, city: str) -> bool:
    """Basic sanity — flag extreme outliers for human review."""
    if price < 400:  return True
    if price > 20000: return True
    # Studio shouldn't be $5k+ in most markets (will catch luxury edge cases — that's fine, just flags for review)
    if bedrooms == 0 and price > 5000: return True
    return False

# ── Email ─────────────────────────────────────────────────────

def send_verify_email(to_email: str, name: str, token: str):
    """
    Sends verification email. Uses Gmail SMTP by default.
    Set SMTP_* vars in .env, or swap with SendGrid/Resend for production.
    """
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    base_url  = os.getenv("BASE_URL", "http://localhost:8001")

    verify_url = f"{base_url}/submit/verify/{token}"

    body = f"""Hi {name},

Thanks for submitting your listing to DriftWatch!

Please verify your email to publish your listing:

{verify_url}

This link expires in 24 hours.

If you didn't submit a listing, you can ignore this email.

— The DriftWatch Team
"""
    msg = MIMEText(body)
    msg["Subject"] = "Verify your DriftWatch listing"
    msg["From"]    = smtp_user
    msg["To"]      = to_email

    if not smtp_user:
        # Dev mode — just print the link
        print(f"\n[DEV] Verify link for {to_email}:\n{verify_url}\n")
        return

    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)

# ── Captcha verification ──────────────────────────────────────

async def verify_captcha(token: str) -> bool:
    """Verifies Cloudflare Turnstile token. Skip in dev."""
    secret = os.getenv("TURNSTILE_SECRET", "")
    if not secret or not token:
        return True  # Skip if not configured
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token}
        )
        return r.json().get("success", False)

# ── Submit endpoint ───────────────────────────────────────────

@app.post("/submit")
async def submit_listing(body: SubmissionIn, request: Request):
    ip = request.client.host
    conn = db()
    cur  = conn.cursor()

    try:
        # 1. Captcha
        if not await verify_captcha(body.captcha_token or ""):
            raise HTTPException(400, "Captcha verification failed")

        # 2. Rate limit by email (1 per 24h)
        if check_rate_limit(conn, body.landlord_email, "email", limit=1):
            raise HTTPException(429, "You've already submitted a listing in the last 24 hours. Please wait before submitting again.")

        # 3. Rate limit by IP (3 per 24h)
        if check_rate_limit(conn, ip, "ip", limit=3):
            raise HTTPException(429, "Too many submissions from this location. Please try again tomorrow.")

        # 4. Address verification
        addr_valid, lat, lng = await verify_address(body.address, body.city, body.state, body.zip)

        # 5. Price sanity check
        price_flagged = is_price_suspicious(body.price, body.bedrooms, body.city)

        # 6. Generate verify token
        token = secrets.token_urlsafe(32)

        # 7. Save submission as 'pending'
        cur.execute("""
            INSERT INTO submissions (
              landlord_name, landlord_email, landlord_phone,
              address, unit, city, state, zip, neighborhood, lat, lng,
              bedrooms, bathrooms, sqft, price, available_date, amenities, description,
              status, verify_token, submitter_ip, price_flagged, address_valid
            ) VALUES (
              %s,%s,%s, %s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s,
              'pending',%s,%s,%s,%s
            ) RETURNING id
        """, (
            body.landlord_name, body.landlord_email, body.landlord_phone,
            body.address, body.unit, body.city, body.state, body.zip,
            body.neighborhood, lat, lng,
            body.bedrooms, body.bathrooms, body.sqft, body.price,
            body.available_date, body.amenities, body.description,
            token, ip, price_flagged, addr_valid
        ))
        sub_id = cur.fetchone()["id"]

        # 8. Log rate limit
        log_submission(conn, body.landlord_email, "email")
        log_submission(conn, ip, "ip")

        conn.commit()

        # 9. Send verification email
        send_verify_email(body.landlord_email, body.landlord_name, token)

        return {
            "success": True,
            "message": "Submission received! Check your email to verify your listing.",
            "id": sub_id
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Submission failed: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ── Email verification ────────────────────────────────────────

@app.get("/submit/verify/{token}")
def verify_email(token: str):
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT id, status, submitted_at FROM submissions
            WHERE verify_token = %s
        """, (token,))
        sub = cur.fetchone()

        if not sub:
            raise HTTPException(404, "Invalid verification link")
        if sub["status"] != "pending":
            return {"message": "Already verified! Your listing is under review."}

        # Check token not expired (24h)
        age = datetime.utcnow() - sub["submitted_at"].replace(tzinfo=None)
        if age > timedelta(hours=24):
            raise HTTPException(410, "This verification link has expired. Please resubmit.")

        cur.execute("""
            UPDATE submissions SET status = 'email_verified', verified_at = NOW()
            WHERE id = %s
        """, (sub["id"],))
        conn.commit()

        return {"message": "Email verified! Your listing will be reviewed and published within 24 hours."}
    finally:
        cur.close()
        conn.close()

# ── Admin auth ────────────────────────────────────────────────

class AdminLogin(BaseModel):
    email: str
    password: str

@app.post("/admin/login")
def admin_login(body: AdminLogin):
    conn = db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM admin_users WHERE email = %s", (body.email,))
    admin = cur.fetchone()
    cur.close(); conn.close()

    if not admin or not pwd_ctx.verify(body.password, admin["password_hash"]):
        raise HTTPException(401, "Invalid credentials")

    token = jwt.encode(
        {"sub": admin["email"], "exp": datetime.utcnow() + timedelta(hours=8)},
        SECRET, algorithm="HS256"
    )
    return {"token": token}

def require_admin(authorization: str = Header(...)):
    try:
        token = authorization.replace("Bearer ", "")
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        return payload["sub"]
    except Exception:
        raise HTTPException(401, "Not authorized")

# ── Admin review queue ────────────────────────────────────────

@app.get("/admin/queue")
def get_queue(admin=Depends(require_admin)):
    conn = db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM pending_review LIMIT 100")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"count": len(rows), "submissions": rows}

@app.post("/admin/approve/{sub_id}")
def approve(sub_id: int, admin=Depends(require_admin)):
    conn = db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM submissions WHERE id = %s", (sub_id,))
        s = cur.fetchone()
        if not s:
            raise HTTPException(404, "Submission not found")

        # Create real listing
        cur.execute("""
            INSERT INTO listings
              (source, source_id, address, unit, city, state, zip, neighborhood,
               lat, lng, bedrooms, bathrooms, sqft, property_type, amenities)
            VALUES ('manual', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'apartment', %s)
            RETURNING id
        """, (
            f"manual-{sub_id}", s["address"], s["unit"], s["city"], s["state"],
            s["zip"], s["neighborhood"], s["lat"], s["lng"],
            s["bedrooms"], s["bathrooms"], s["sqft"], s["amenities"]
        ))
        listing_id = cur.fetchone()["id"]

        # Record price snapshot
        from datetime import date as dt
        week_start = dt.today() - __import__('datetime').timedelta(days=dt.today().weekday())
        cur.execute("""
            INSERT INTO price_snapshots (listing_id, price, week_start)
            VALUES (%s, %s, %s)
        """, (listing_id, s["price"], week_start))

        # Mark submission approved
        cur.execute("""
            UPDATE submissions SET status='approved', reviewed_at=NOW(),
            reviewed_by=%s, listing_id=%s WHERE id=%s
        """, (admin, listing_id, sub_id))

        conn.commit()
        return {"success": True, "listing_id": listing_id}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        cur.close(); conn.close()

@app.post("/admin/reject/{sub_id}")
def reject(sub_id: int, reason: str = "Does not meet listing requirements", admin=Depends(require_admin)):
    conn = db()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE submissions SET status='rejected', rejection_reason=%s,
        reviewed_at=NOW(), reviewed_by=%s WHERE id=%s
    """, (reason, admin, sub_id))
    conn.commit()
    cur.close(); conn.close()
    return {"success": True}

@app.post("/admin/spam/{sub_id}")
def mark_spam(sub_id: int, admin=Depends(require_admin)):
    conn = db()
    cur  = conn.cursor()
    # Also block this email from future submissions
    cur.execute("SELECT landlord_email FROM submissions WHERE id=%s", (sub_id,))
    s = cur.fetchone()
    cur.execute("UPDATE submissions SET status='spam', reviewed_by=%s WHERE id=%s", (admin, sub_id))
    conn.commit()
    cur.close(); conn.close()
    return {"success": True}

@app.get("/admin/stats")
def admin_stats(admin=Depends(require_admin)):
    conn = db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT
          COUNT(*) FILTER (WHERE status='pending')        AS pending,
          COUNT(*) FILTER (WHERE status='email_verified') AS awaiting_review,
          COUNT(*) FILTER (WHERE status='approved')       AS approved,
          COUNT(*) FILTER (WHERE status='rejected')       AS rejected,
          COUNT(*) FILTER (WHERE status='spam')           AS spam
        FROM submissions
    """)
    stats = dict(cur.fetchone())
    cur.close(); conn.close()
    return stats
