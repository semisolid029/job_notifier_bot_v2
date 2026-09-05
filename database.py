import os
import threading
import logging
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

# Fetch the cloud database URL from Render/Local Environment
DB_URL = os.getenv("DATABASE_URL")

# Initialize a thread-safe connection pool for PostgreSQL
_pool = None
_pool_lock = threading.Lock()

def init_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            if not DB_URL:
                raise ValueError("DATABASE_URL environment variable is missing!")
            # Keep between 1 and 10 active connections open to Supabase
            _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=DB_URL)

@contextmanager
def _get_connection():
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)

def init_db():
    if not DB_URL:
        logger.warning("No DATABASE_URL provided. Database initialization skipped.")
        return
    
    with _get_connection() as conn:
        with conn.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    country TEXT DEFAULT 'US',
                    city TEXT DEFAULT NULL,
                    tier TEXT DEFAULT 'all',
                    active INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    url TEXT NOT NULL,
                    source TEXT NOT NULL,
                    country TEXT,
                    city TEXT,
                    tier TEXT,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS sent_jobs (
                    user_id BIGINT NOT NULL,
                    job_id TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, job_id),
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_country_city_tier ON jobs(country, city, tier)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sent_jobs_user ON sent_jobs(user_id)")
        conn.commit()
        logger.info("Supabase PostgreSQL tables initialized successfully.")

def get_user_preferences(user_id):
    with _get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                "SELECT country, city, tier, active FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = c.fetchone()
    
    if row:
        return {"country": row["country"], "city": row["city"], "tier": row["tier"], "active": row["active"]}
    return {"country": "US", "city": None, "tier": "all", "active": 1}

def update_user_preference(user_id, country=None, city=None, tier=None, active=None):
    with _get_connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            fields, values = [], []
            if country is not None:
                fields.append("country = %s")
                values.append(country)
            if city is not None:
                fields.append("city = %s")
                values.append(None if city in ("", "CLEAR") else city)
            if tier is not None:
                fields.append("tier = %s")
                values.append(tier)
            if active is not None:
                fields.append("active = %s")
                values.append(active)
            
            if fields:
                values.append(user_id)
                c.execute(
                    f"UPDATE users SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
                    values,
                )
        conn.commit()

def get_all_active_users():
    with _get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                "SELECT user_id, country, city, tier FROM users WHERE active = 1"
            )
            rows = c.fetchall()
    return [
        {"user_id": r["user_id"], "country": r["country"], "city": r["city"], "tier": r["tier"]}
        for r in rows
    ]

def upsert_jobs(jobs):
    if not jobs:
        return 0
    inserted = 0
    with _get_connection() as conn:
        with conn.cursor() as c:
            for job in jobs:
                job_id = str(job.get("id") or "").strip()
                url = str(job.get("url") or "").strip()
                if not job_id or not url:
                    continue
                
                c.execute(
                    """
                    INSERT INTO jobs (
                        job_id, title, company, location, url, source,
                        country, city, tier, first_seen_at, last_seen_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(job_id) DO UPDATE SET
                        title=EXCLUDED.title,
                        company=EXCLUDED.company,
                        location=EXCLUDED.location,
                        url=EXCLUDED.url,
                        source=EXCLUDED.source,
                        last_seen_at=CURRENT_TIMESTAMP
                    """,
                    (
                        job_id,
                        job.get("title") or "Untitled role",
                        job.get("company") or "Unknown company",
                        job.get("location") or "",
                        url,
                        job.get("source") or "Unknown",
                        job.get("country") or "",
                        job.get("city") or "",
                        job.get("tier") or "all",
                    ),
                )
                inserted += 1
        conn.commit()
    return inserted

def get_unsent_jobs_for_user(user_id, limit=5):
    with _get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                """
                SELECT j.job_id, j.title, j.company, j.location, j.url, j.source
                FROM jobs j
                JOIN users u ON u.user_id = %s
                LEFT JOIN sent_jobs s
                  ON s.user_id = u.user_id AND s.job_id = j.job_id
                WHERE u.active = 1
                  AND s.job_id IS NULL
                  AND (
                        u.country IS NULL OR u.country = ''
                        OR j.country = '' OR j.country = u.country
                      )
                  AND (
                        u.city IS NULL OR u.city = ''
                        OR lower(j.city) = lower(u.city)
                        OR lower(j.location) LIKE '%%' || lower(u.city) || '%%'
                        OR lower(j.location) LIKE '%%remote%%'
                      )
                  AND (
                        u.tier = 'all'
                        OR j.tier = u.tier
                      )
                ORDER BY j.first_seen_at ASC
                LIMIT %s
                """,
                (user_id, limit),
            )
            rows = c.fetchall()
    return [
        {
            "id": r["job_id"], "title": r["title"], "company": r["company"],
            "location": r["location"], "url": r["url"], "source": r["source"]
        }
        for r in rows
    ]

def mark_jobs_sent(user_id, job_ids):
    ids = [str(x) for x in job_ids if x]
    if not ids:
        return
    with _get_connection() as conn:
        with conn.cursor() as c:
            for job_id in ids:
                c.execute(
                    "INSERT INTO sent_jobs (user_id, job_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (user_id, job_id),
                )
        conn.commit()

def prune_old_sent_jobs(days=90):
    with _get_connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "DELETE FROM sent_jobs WHERE sent_at < CURRENT_TIMESTAMP - INTERVAL '%s days'",
                (int(days),)
            )
            c.execute(
                "DELETE FROM jobs WHERE last_seen_at < CURRENT_TIMESTAMP - INTERVAL '%s days' "
                "AND job_id NOT IN (SELECT job_id FROM sent_jobs)",
                (int(days),)
            )
        conn.commit()