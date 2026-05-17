"""SQLite database layer with a unified schema for multi-source crawlers.

Each crawler (NRF, KOSME, ...) writes into the same tables so downstream
consumers can query across sources uniformly. New sites are added by
registering a new row in `sources` (and optional `categories`) and
implementing a crawler module in `crawlers/`.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

DB_PATH = Path(__file__).resolve().parent / "announcements.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    list_url    TEXT,
    UNIQUE (source_id, code)
);

CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    category_id     INTEGER REFERENCES categories(id),
    business_id     INTEGER REFERENCES assistance_businesses(id),
    external_id     TEXT NOT NULL,
    external_biz_id TEXT,
    title           TEXT NOT NULL,
    status          TEXT,
    d_day           TEXT,
    reg_date        TEXT,
    start_date      TEXT,
    end_date        TEXT,
    department      TEXT,
    contact         TEXT,
    content_html    TEXT,
    content_text    TEXT,
    detail_url      TEXT NOT NULL,
    raw_meta        TEXT,
    crawled_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_announcements_category ON announcements(category_id);
CREATE INDEX IF NOT EXISTS idx_announcements_end_date ON announcements(end_date);

CREATE TABLE IF NOT EXISTS assistance_businesses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    category_id     INTEGER REFERENCES categories(id),
    external_id     TEXT NOT NULL,
    bsns_year       TEXT,
    name            TEXT NOT NULL,
    parent_name     TEXT,
    purpose         TEXT,
    overview        TEXT,
    legal_basis     TEXT,
    support_target  TEXT,
    start_date      TEXT,
    end_date        TEXT,
    department      TEXT,
    sub_dept        TEXT,
    charger_name    TEXT,
    charger_tel     TEXT,
    charger_email   TEXT,
    sub_count       INTEGER,
    classification  TEXT,
    business_type   TEXT,
    delivery_type   TEXT,
    settlement_type TEXT,
    portal_url      TEXT,
    detail_url      TEXT,
    raw_meta        TEXT,
    crawled_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_id, external_id, bsns_year)
);

CREATE INDEX IF NOT EXISTS idx_businesses_dept ON assistance_businesses(department);
CREATE INDEX IF NOT EXISTS idx_businesses_year ON assistance_businesses(bsns_year);

CREATE TABLE IF NOT EXISTS business_attributes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     INTEGER NOT NULL REFERENCES assistance_businesses(id) ON DELETE CASCADE,
    attribute_name  TEXT NOT NULL,
    attribute_code  TEXT,
    attribute_type  TEXT,
    UNIQUE (business_id, attribute_name)
);

CREATE INDEX IF NOT EXISTS idx_attributes_business ON business_attributes(business_id);
CREATE INDEX IF NOT EXISTS idx_attributes_name ON business_attributes(attribute_name);

CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    attach_no       TEXT,
    file_name       TEXT NOT NULL,
    download_url    TEXT NOT NULL,
    file_size       INTEGER,
    UNIQUE (announcement_id, attach_no, file_name)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    category_id     INTEGER REFERENCES categories(id),
    started_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     TEXT,
    items_found     INTEGER DEFAULT 0,
    items_new       INTEGER DEFAULT 0,
    items_updated   INTEGER DEFAULT 0,
    status          TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS attachment_folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS announcement_schedule_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id   INTEGER NOT NULL REFERENCES attachment_folders(id),
    file_hash   TEXT,
    title       TEXT NOT NULL,
    type        TEXT NOT NULL,
    date_start  TEXT NOT NULL,
    date_end    TEXT,
    time        TEXT,
    note        TEXT,
    source_page INTEGER,
    added_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_schedule_events_folder ON announcement_schedule_events(folder_id);

CREATE TABLE IF NOT EXISTS uploaded_attachments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash         TEXT UNIQUE NOT NULL,
    original_filename TEXT NOT NULL,
    announcement_id   TEXT,
    uploaded_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    analyzed_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_hash ON uploaded_attachments(file_hash);
CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_announcement ON uploaded_attachments(announcement_id);
"""


@contextmanager
def connect(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, ddl-fragment)
    ("announcements", "business_id", "INTEGER REFERENCES assistance_businesses(id)"),
    ("uploaded_attachments", "folder_id", "INTEGER REFERENCES attachment_folders(id)"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add columns that older DBs predate. SQLite ALTER TABLE only supports
    adding columns, so each migration entry is a (table, column, ddl) tuple."""
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# Indexes that depend on migrated columns; created after _apply_migrations.
_POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_announcements_business ON announcements(business_id)",
    "CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_folder ON uploaded_attachments(folder_id)",
]


def _ensure_default_folder(conn: sqlite3.Connection) -> None:
    """폴더가 하나도 없으면 '기본' 폴더 생성. 폴더가 없는 기존 첨부는 거기로 편입."""
    row = conn.execute("SELECT id FROM attachment_folders ORDER BY id LIMIT 1").fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO attachment_folders(name) VALUES (?)", ("기본",))
        default_id = cur.lastrowid
    else:
        default_id = row["id"]
    conn.execute(
        "UPDATE uploaded_attachments SET folder_id=? WHERE folder_id IS NULL",
        (default_id,),
    )


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        for ddl in _POST_MIGRATION_INDEXES:
            conn.execute(ddl)
        _ensure_default_folder(conn)


def upsert_source(conn: sqlite3.Connection, code: str, name: str, base_url: str) -> int:
    conn.execute(
        "INSERT INTO sources(code, name, base_url) VALUES (?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, base_url=excluded.base_url",
        (code, name, base_url),
    )
    row = conn.execute("SELECT id FROM sources WHERE code=?", (code,)).fetchone()
    return row["id"]


def upsert_category(
    conn: sqlite3.Connection, source_id: int, code: str, name: str, list_url: Optional[str] = None
) -> int:
    conn.execute(
        "INSERT INTO categories(source_id, code, name, list_url) VALUES (?,?,?,?) "
        "ON CONFLICT(source_id, code) DO UPDATE SET name=excluded.name, list_url=excluded.list_url",
        (source_id, code, name, list_url),
    )
    row = conn.execute(
        "SELECT id FROM categories WHERE source_id=? AND code=?", (source_id, code)
    ).fetchone()
    return row["id"]


def upsert_announcement(conn: sqlite3.Connection, data: dict[str, Any]) -> tuple[int, bool]:
    """Insert or update an announcement. Returns (id, was_new)."""
    raw_meta = data.get("raw_meta")
    if isinstance(raw_meta, (dict, list)):
        raw_meta = json.dumps(raw_meta, ensure_ascii=False)

    now = datetime.now().isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT id FROM announcements WHERE source_id=? AND external_id=?",
        (data["source_id"], data["external_id"]),
    ).fetchone()

    fields = dict(
        source_id=data["source_id"],
        category_id=data.get("category_id"),
        external_id=data["external_id"],
        external_biz_id=data.get("external_biz_id"),
        title=data["title"],
        status=data.get("status"),
        d_day=data.get("d_day"),
        reg_date=data.get("reg_date"),
        start_date=data.get("start_date"),
        end_date=data.get("end_date"),
        department=data.get("department"),
        contact=data.get("contact"),
        content_html=data.get("content_html"),
        content_text=data.get("content_text"),
        detail_url=data["detail_url"],
        raw_meta=raw_meta,
        updated_at=now,
    )

    if existing:
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        conn.execute(f"UPDATE announcements SET {cols} WHERE id=:id", {**fields, "id": existing["id"]})
        return existing["id"], False

    cols = ", ".join(fields.keys())
    placeholders = ", ".join(f":{k}" for k in fields.keys())
    cur = conn.execute(f"INSERT INTO announcements({cols}) VALUES ({placeholders})", fields)
    return cur.lastrowid, True


def replace_attachments(
    conn: sqlite3.Connection, announcement_id: int, attachments: Iterable[dict[str, Any]]
) -> int:
    conn.execute("DELETE FROM attachments WHERE announcement_id=?", (announcement_id,))
    count = 0
    for a in attachments:
        conn.execute(
            "INSERT INTO attachments(announcement_id, attach_no, file_name, download_url, file_size) "
            "VALUES (?,?,?,?,?)",
            (
                announcement_id,
                a.get("attach_no"),
                a["file_name"],
                a["download_url"],
                a.get("file_size"),
            ),
        )
        count += 1
    return count


def upsert_assistance_business(conn: sqlite3.Connection, data: dict[str, Any]) -> tuple[int, bool]:
    """Insert or update an assistance business (보조사업 마스터). Returns (id, was_new)."""
    raw_meta = data.get("raw_meta")
    if isinstance(raw_meta, (dict, list)):
        raw_meta = json.dumps(raw_meta, ensure_ascii=False)

    now = datetime.now().isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT id FROM assistance_businesses WHERE source_id=? AND external_id=? AND IFNULL(bsns_year,'')=IFNULL(?,'')",
        (data["source_id"], data["external_id"], data.get("bsns_year")),
    ).fetchone()

    fields = dict(
        source_id=data["source_id"],
        category_id=data.get("category_id"),
        external_id=data["external_id"],
        bsns_year=data.get("bsns_year"),
        name=data["name"],
        parent_name=data.get("parent_name"),
        purpose=data.get("purpose"),
        overview=data.get("overview"),
        legal_basis=data.get("legal_basis"),
        support_target=data.get("support_target"),
        start_date=data.get("start_date"),
        end_date=data.get("end_date"),
        department=data.get("department"),
        sub_dept=data.get("sub_dept"),
        charger_name=data.get("charger_name"),
        charger_tel=data.get("charger_tel"),
        charger_email=data.get("charger_email"),
        sub_count=data.get("sub_count"),
        classification=data.get("classification"),
        business_type=data.get("business_type"),
        delivery_type=data.get("delivery_type"),
        settlement_type=data.get("settlement_type"),
        portal_url=data.get("portal_url"),
        detail_url=data.get("detail_url"),
        raw_meta=raw_meta,
        updated_at=now,
    )

    if existing:
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        conn.execute(f"UPDATE assistance_businesses SET {cols} WHERE id=:id", {**fields, "id": existing["id"]})
        return existing["id"], False

    cols = ", ".join(fields.keys())
    placeholders = ", ".join(f":{k}" for k in fields.keys())
    cur = conn.execute(f"INSERT INTO assistance_businesses({cols}) VALUES ({placeholders})", fields)
    return cur.lastrowid, True


def replace_business_attributes(
    conn: sqlite3.Connection, business_id: int, attributes: Iterable[dict[str, Any]]
) -> int:
    """Replace all attributes for a business. attributes: [{name, code?, type?}, ...]"""
    conn.execute("DELETE FROM business_attributes WHERE business_id=?", (business_id,))
    count = 0
    seen = set()
    for a in attributes:
        name = a.get("attribute_name") or a.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        conn.execute(
            "INSERT INTO business_attributes(business_id, attribute_name, attribute_code, attribute_type) "
            "VALUES (?,?,?,?)",
            (business_id, name, a.get("attribute_code") or a.get("code"), a.get("attribute_type") or a.get("type")),
        )
        count += 1
    return count


def start_crawl_run(conn: sqlite3.Connection, source_id: int, category_id: Optional[int]) -> int:
    cur = conn.execute(
        "INSERT INTO crawl_runs(source_id, category_id, status) VALUES (?,?, 'running')",
        (source_id, category_id),
    )
    return cur.lastrowid


def finish_crawl_run(
    conn: sqlite3.Connection,
    run_id: int,
    items_found: int,
    items_new: int,
    items_updated: int,
    status: str = "ok",
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE crawl_runs SET finished_at=CURRENT_TIMESTAMP, items_found=?, items_new=?, "
        "items_updated=?, status=?, error=? WHERE id=?",
        (items_found, items_new, items_updated, status, error, run_id),
    )
