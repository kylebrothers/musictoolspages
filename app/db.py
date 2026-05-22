"""
db.py — SQLite persistence for playlistrec.

Schema:
  profiles          — named listener profiles
  feedback          — per-song yes/maybe/no ratings, per profile
  maybe_notes       — free text notes on maybe-rated songs
  spotify_tokens    — Spotify OAuth tokens (single-user, user_id=1)

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
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT UNIQUE NOT NULL,
                centroid            TEXT,           -- JSON: {energy, danceability, ...} (legacy)
                seed_artists        TEXT,           -- JSON: [artist, ...]
                disliked_artists    TEXT,           -- JSON: [artist, ...]
                spotify_playlist_id TEXT,           -- Spotify playlist ID linked to profile
                sonic_profile       TEXT,           -- Claude-generated sonic description
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now'))
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

            CREATE TABLE IF NOT EXISTS spotify_tokens (
                user_id       INTEGER PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    REAL NOT NULL,   -- Unix timestamp
                updated_at    TEXT DEFAULT (datetime('now'))
            );
        """)

    # Migrate existing databases: add new columns if absent
    _migrate(conn_factory=get_db)
    logger.info(f"Database initialised at {DB_PATH}")


def _migrate(conn_factory):
    """Add new columns to existing tables without dropping data."""
    migrations = [
        ("profiles", "spotify_playlist_id", "TEXT"),
        ("profiles", "sonic_profile",       "TEXT"),
    ]
    with conn_factory() as conn:
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(profiles)").fetchall()
        }
        for table, col, col_type in migrations:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                logger.info(f"Migration: added {table}.{col}")


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
    """Update any combination of profile fields."""
    allowed = {
        "name", "centroid", "seed_artists", "disliked_artists",
        "spotify_playlist_id", "sonic_profile",
    }
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
    # sonic_profile and spotify_playlist_id are plain strings — no JSON parsing
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


# ── Spotify tokens ────────────────────────────────────────────────────────────

def get_spotify_token(user_id):
    """Return token row dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM spotify_tokens WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def save_spotify_token(user_id, access_token, refresh_token, expires_at):
    """Upsert Spotify tokens for a user."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO spotify_tokens (user_id, access_token, refresh_token, expires_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 access_token  = excluded.access_token,
                 refresh_token = excluded.refresh_token,
                 expires_at    = excluded.expires_at,
                 updated_at    = datetime('now')""",
            (user_id, access_token, refresh_token, expires_at)
        )


def delete_spotify_token(user_id):
    """Remove Spotify tokens for a user (disconnect)."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM spotify_tokens WHERE user_id = ?", (user_id,)
        )
