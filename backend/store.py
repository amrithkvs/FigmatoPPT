"""Tiny SQLite store for decks. One row per Figma-URL -> deck job."""

import json
import os
import sqlite3
import sys
from datetime import datetime


def _db_path():
    # Explicit override for hosted environments.
    forced = os.environ.get("FIGMA_DECK_DB_PATH")
    if forced:
        base = os.path.dirname(forced)
        if base:
            os.makedirs(base, exist_ok=True)
        return forced

    # In a packaged .app, write the DB to a persistent user location (not the
    # read-only bundle). In dev, keep it at the project root.
    if getattr(sys, "frozen", False):
        base = os.path.expanduser("~/Library/Application Support/Figma Deck")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "data.db")

    # In container/hosted mode, co-locate DB with deck output directory when set.
    out_dir = os.environ.get("DECKS_OUTPUT_DIR")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        return os.path.join(out_dir, "data.db")

    return os.path.join(os.path.dirname(__file__), "..", "data.db")


DB_PATH = _db_path()


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS decks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                figma_url TEXT NOT NULL,
                file_key TEXT NOT NULL,
                token TEXT NOT NULL,
                times TEXT NOT NULL DEFAULT '["03:00"]',
                last_run TEXT,
                last_status TEXT,
                build_progress INTEGER,
                slide_count INTEGER,
                pptx_path TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # Migrations for webhook (live-sync) + cloud sync support.
        existing = {row["name"] for row in c.execute("PRAGMA table_info(decks)")}
        for col, decl in (
            ("passcode", "TEXT"),
            ("webhook_id", "TEXT"),
            ("web_url", "TEXT"),
            ("onedrive_item_id", "TEXT"),
            ("build_progress", "INTEGER"),
        ):
            if col not in existing:
                c.execute(f"ALTER TABLE decks ADD COLUMN {col} {decl}")

        # Single connected Microsoft account (OneDrive/SharePoint sync).
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ms_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT, refresh_token TEXT, expires_at REAL, name TEXT
            )
            """
        )

        # App-wide settings (e.g. the one saved Figma token).
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")

        # Migration: seed the global Figma token from an existing deck, so users
        # who already created decks don't have to re-enter it.
        has_token = c.execute("SELECT value FROM settings WHERE key='figma_token'").fetchone()
        if not has_token:
            d = c.execute("SELECT token FROM decks WHERE token IS NOT NULL LIMIT 1").fetchone()
            if d and d["token"]:
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('figma_token', ?)", (d["token"],))


def add_deck(name, figma_url, file_key, token, times):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO decks (name, figma_url, file_key, token, times, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (name, figma_url, file_key, token, json.dumps(times), datetime.now().isoformat()),
        )
        return cur.lastrowid


def list_decks():
    with _conn() as c:
        rows = c.execute("SELECT * FROM decks ORDER BY id DESC").fetchall()
    return [_public(dict(r)) for r in rows]


def get_deck(deck_id, with_secrets=False):
    with _conn() as c:
        row = c.execute("SELECT * FROM decks WHERE id=?", (deck_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    return d if with_secrets else _public(d)


def delete_deck(deck_id):
    with _conn() as c:
        c.execute("DELETE FROM decks WHERE id=?", (deck_id,))


def set_webhook(deck_id, webhook_id, passcode):
    with _conn() as c:
        c.execute("UPDATE decks SET webhook_id=?, passcode=? WHERE id=?", (webhook_id, passcode, deck_id))


def find_decks_for_webhook(file_key, passcode):
    """Decks matching an incoming FILE_UPDATE, authenticated by passcode."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM decks WHERE file_key=? AND passcode IS NOT NULL AND passcode=?",
            (file_key, passcode),
        ).fetchall()
    return [dict(r) for r in rows]


def all_decks_with_secrets():
    with _conn() as c:
        rows = c.execute("SELECT * FROM decks").fetchall()
    return [dict(r) for r in rows]


def mark_building(deck_id):
    with _conn() as c:
        c.execute("UPDATE decks SET last_status='building', build_progress=0 WHERE id=?", (deck_id,))


def update_progress(deck_id, progress):
    p = max(0, min(100, int(progress)))
    with _conn() as c:
        c.execute("UPDATE decks SET last_status='building', build_progress=? WHERE id=?", (p, deck_id))


def record_run(deck_id, status, slide_count=None, pptx_path=None):
    progress = 100 if status == "ok" else None
    with _conn() as c:
        c.execute(
            "UPDATE decks SET last_run=?, last_status=?, build_progress=?, slide_count=COALESCE(?, slide_count), "
            "pptx_path=COALESCE(?, pptx_path) WHERE id=?",
            (datetime.now().isoformat(), status, progress, slide_count, pptx_path, deck_id),
        )


def set_deck_cloud(deck_id, web_url, item_id):
    with _conn() as c:
        c.execute("UPDATE decks SET web_url=?, onedrive_item_id=? WHERE id=?", (web_url, item_id, deck_id))


# ---- connected Microsoft account ----

def set_ms_account(access_token, refresh_token, expires_at, name):
    with _conn() as c:
        c.execute("DELETE FROM ms_account")
        c.execute(
            "INSERT INTO ms_account (id, access_token, refresh_token, expires_at, name) VALUES (1,?,?,?,?)",
            (access_token, refresh_token, expires_at, name),
        )


def get_ms_account():
    with _conn() as c:
        r = c.execute("SELECT * FROM ms_account WHERE id=1").fetchone()
    return dict(r) if r else None


def clear_ms_account():
    with _conn() as c:
        c.execute("DELETE FROM ms_account")


# ---- saved Figma token (entered once, reused for every deck) ----

def set_figma_token(token):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('figma_token', ?)", (token,))
        c.execute("UPDATE decks SET token=?", (token,))  # keep existing decks on the same token


def get_figma_token():
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key='figma_token'").fetchone()
    return r["value"] if r else None


def _public(d: dict) -> dict:
    """Strip secrets before sending a deck to the browser."""
    d = dict(d)
    d.pop("token", None)
    d.pop("passcode", None)
    d["live_sync"] = bool(d.pop("webhook_id", None))
    d["times"] = json.loads(d.get("times") or "[]")
    return d
