"""
Create an admin user for the DriftWatch review queue.

Usage:
  python create_admin.py your@email.com yourpassword
"""
import sys, os
import psycopg2
from passlib.context import CryptContext
from dotenv import load_dotenv

load_dotenv()
pwd_ctx = CryptContext(schemes=["bcrypt"])

if len(sys.argv) != 3:
    print("Usage: python create_admin.py email password")
    sys.exit(1)

email    = sys.argv[1]
password = sys.argv[2]
hashed   = pwd_ctx.hash(password)

conn = psycopg2.connect(
    host=os.getenv("DB_HOST","localhost"), port=os.getenv("DB_PORT",5432),
    dbname=os.getenv("DB_NAME","driftwatch"), user=os.getenv("DB_USER","postgres"),
    password=os.getenv("DB_PASSWORD","")
)
cur = conn.cursor()
cur.execute("""
    INSERT INTO admin_users (email, password_hash) VALUES (%s, %s)
    ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash
""", (email, hashed))
conn.commit()
cur.close(); conn.close()
print(f"✓ Admin user created: {email}")
