import os
import sqlite3
import threading
from contextlib import contextmanager

DB_NAME = os.getenv("DB_PATH", "bot_users.db")
_DB_LOCK = threading.RLock()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _DB_LOCK, _connect() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
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
                user_id INTEGER NOT NULL,
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


def get_user_preferences(user_id):
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT country, city, tier, active FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row:
        return {"country": row[0], "city": row[1], "tier": row[2], "active": row[3]}
    return {"country": "US", "city": None, "tier": "all", "active": 1}


def update_user_preference(user_id, country=None, city=None, tier=None, active=None):
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )
        fields, values = [], []
        if country is not None:
            fields.append("country = ?")
            values.append(country)
        if city is not None:
            fields.append("city = ?")
            values.append(None if city in ("", "CLEAR") else city)
        if tier is not None:
            fields.append("tier = ?")
            values.append(tier)
        if active is not None:
            fields.append("active = ?")
            values.append(active)
        if fields:
            values.append(user_id)
            conn.execute(
                f"UPDATE users SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                values,
            )


def get_all_active_users():
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, country, city, tier FROM users WHERE active = 1"
        ).fetchall()
    return [
        {"user_id": r[0], "country": r[1], "city": r[2], "tier": r[3]}
        for r in rows
    ]


def upsert_jobs(jobs):
    if not jobs:
        return 0
    inserted = 0
    with _DB_LOCK, _connect() as conn:
        for job in jobs:
            job_id = str(job.get("id") or "").strip()
            url = str(job.get("url") or "").strip()
            if not job_id or not url:
                continue
            before = conn.total_changes
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, title, company, location, url, source,
                    country, city, tier, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(job_id) DO UPDATE SET
                    title=excluded.title,
                    company=excluded.company,
                    location=excluded.location,
                    url=excluded.url,
                    source=excluded.source,
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
            if conn.total_changes > before:
                inserted += 1
    return inserted


def get_unsent_jobs_for_user(user_id, limit=5):
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT j.job_id, j.title, j.company, j.location, j.url, j.source
            FROM jobs j
            JOIN users u ON u.user_id = ?
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
                    OR lower(j.location) LIKE '%' || lower(u.city) || '%'
                    OR lower(j.location) LIKE '%remote%'
                  )
              AND (
                    u.tier = 'all'
                    OR j.tier = u.tier
              )
            ORDER BY j.first_seen_at ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [
        {
            "id": r[0], "title": r[1], "company": r[2],
            "location": r[3], "url": r[4], "source": r[5]
        }
        for r in rows
    ]


def mark_jobs_sent(user_id, job_ids):
    ids = [str(x) for x in job_ids if x]
    if not ids:
        return
    with _DB_LOCK, _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO sent_jobs (user_id, job_id) VALUES (?, ?)",
            [(user_id, job_id) for job_id in ids],
        )


def prune_old_sent_jobs(days=90):
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "DELETE FROM sent_jobs WHERE sent_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        conn.execute(
            "DELETE FROM jobs WHERE last_seen_at < datetime('now', ?) "
            "AND job_id NOT IN (SELECT job_id FROM sent_jobs)",
            (f"-{int(days)} days",),
        )
