"""
db.py — SQLite persistence for playlistrec.

Schema:
  profiles          — named listener profiles
  feedback          — per-song yes/maybe/no ratings, per profile
  maybe_notes       — free text notes on maybe-rated songs

The database file lives on the NAS volume at /app/database/playlistrec.db
so it survives container rebuilds.
"""

import sqlite3
import json
import logging
import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/app/database/playlistrec.db")


# ── Connection ────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT UNIQUE NOT NULL,
                centroid        TEXT,           -- JSON: {energy, danceability, ...}
                seed_artists    TEXT,           -- JSON: [artist, ...]
                disliked_artists TEXT,          -- JSON: [artist, ...]
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                track_key   TEXT NOT NULL,      -- "Song Title — Artist"
                rating      TEXT NOT NULL CHECK(rating IN ('yes','maybe','no')),
                created_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(profile_id, track_key)
            );

            CREATE TABLE IF NOT EXISTS maybe_notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                track_key   TEXT NOT NULL,
                note        TEXT NOT NULL DEFAULT '',
                updated_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(profile_id, track_key)
            );
        """)
    logger.info(f"Database initialised at {DB_PATH}")


# ── Profiles ──────────────────────────────────────────────────────────────────

def get_all_profiles():
    """Return list of all profiles with parsed JSON fields."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM profiles ORDER BY name"
        ).fetchall()
    return [_parse_profile(r) for r in rows]


def get_profile(profile_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    return _parse_profile(row) if row else None


def get_profile_by_name(name):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE name = ?", (name,)
        ).fetchone()
    return _parse_profile(row) if row else None


def create_profile(name, centroid=None, seed_artists=None, disliked_artists=None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO profiles (name, centroid, seed_artists, disliked_artists)
               VALUES (?, ?, ?, ?)""",
            (
                name,
                json.dumps(centroid) if centroid else None,
                json.dumps(seed_artists or []),
                json.dumps(disliked_artists or []),
            )
        )
        row = conn.execute(
            "SELECT * FROM profiles WHERE name = ?", (name,)
        ).fetchone()
    return _parse_profile(row)


def update_profile(profile_id, **kwargs):
    """Update any combination of centroid, seed_artists, disliked_artists, name."""
    allowed = {"name", "centroid", "seed_artists", "disliked_artists"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return

    # JSON-encode list/dict fields
    for field in ("centroid", "seed_artists", "disliked_artists"):
        if field in updates and updates[field] is not None:
            updates[field] = json.dumps(updates[field])

    updates["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k} = datetime('now')" if v == "datetime('now')" else f"{k} = ?"
        for k, v in updates.items()
    )
    values = [v for v in updates.values() if v != "datetime('now')"]
    values.append(profile_id)

    with get_db() as conn:
        conn.execute(
            f"UPDATE profiles SET {set_clause} WHERE id = ?", values
        )


def delete_profile(profile_id):
    with get_db() as conn:
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))


def _parse_profile(row):
    if row is None:
        return None
    p = dict(row)
    for field in ("centroid", "seed_artists", "disliked_artists"):
        if p.get(field):
            try:
                p[field] = json.loads(p[field])
            except (json.JSONDecodeError, TypeError):
                p[field] = None if field == "centroid" else []
        else:
            p[field] = None if field == "centroid" else []
    return p


# ── Feedback ──────────────────────────────────────────────────────────────────

def get_feedback(profile_id):
    """Return {track_key: rating} dict for a profile."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT track_key, rating FROM feedback WHERE profile_id = ?",
            (profile_id,)
        ).fetchall()
    return {r["track_key"]: r["rating"] for r in rows}


def set_feedback(profile_id, track_key, rating):
    """Upsert a rating. rating must be 'yes', 'maybe', or 'no'."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO feedback (profile_id, track_key, rating)
               VALUES (?, ?, ?)
               ON CONFLICT(profile_id, track_key) DO UPDATE SET rating = excluded.rating""",
            (profile_id, track_key, rating)
        )


def delete_feedback(profile_id, track_key):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM feedback WHERE profile_id = ? AND track_key = ?",
            (profile_id, track_key)
        )


def clear_feedback(profile_id):
    """Remove all feedback for a profile."""
    with get_db() as conn:
        conn.execute("DELETE FROM feedback WHERE profile_id = ?", (profile_id,))
    with get_db() as conn:
        conn.execute("DELETE FROM maybe_notes WHERE profile_id = ?", (profile_id,))


# ── Maybe notes ───────────────────────────────────────────────────────────────

def get_maybe_notes(profile_id):
    """Return {track_key: note} dict for a profile."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT track_key, note FROM maybe_notes WHERE profile_id = ?",
            (profile_id,)
        ).fetchall()
    return {r["track_key"]: r["note"] for r in rows}


def set_maybe_note(profile_id, track_key, note):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO maybe_notes (profile_id, track_key, note)
               VALUES (?, ?, ?)
               ON CONFLICT(profile_id, track_key) DO UPDATE SET note = excluded.note,
               updated_at = datetime('now')""",
            (profile_id, track_key, note)
        )
