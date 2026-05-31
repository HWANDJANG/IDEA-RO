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

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    auth_provider TEXT NOT NULL,           -- 'kakao' | 'google' | ...
    provider_uid  TEXT NOT NULL,           -- 외부 시스템의 사용자 ID (카카오 id 등)
    nickname      TEXT,
    email         TEXT,
    profile_img   TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    UNIQUE(auth_provider, provider_uid)
);

CREATE INDEX IF NOT EXISTS idx_users_provider ON users(auth_provider, provider_uid);

-- Phase 3: 사용자 프로필 (온보딩에서 수집한 기업 기본 정보)
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id              INTEGER PRIMARY KEY REFERENCES users(id),
    -- 필수 (시즌 1)
    company_name         TEXT,
    business_type        TEXT,   -- 'individual' | 'corporate' | 'prelaunch'
    establishment_date   TEXT,   -- ISO YYYY-MM-DD (예비창업자는 NULL)
    region               TEXT,   -- 시·도 (예: '서울특별시')
    industry             TEXT,   -- 업태 대분류 (예: 'IT/소프트웨어')
    -- 부가 (시즌 2)
    business_number      TEXT,
    representative_name  TEXT,
    representative_email TEXT,
    industry_detail      TEXT,
    -- 메타
    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    file_hash         TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    announcement_id   TEXT,
    uploaded_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    analyzed_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_hash ON uploaded_attachments(file_hash);
CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_announcement ON uploaded_attachments(announcement_id);

-- Phase 5: Google Calendar 연동 토큰. 1 user = 1 row.
-- refresh_token 은 사용자가 연동 해제할 때까지 영구 보관. access_token 은 expires_at 까지 캐싱.
CREATE TABLE IF NOT EXISTS google_tokens (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    refresh_token TEXT NOT NULL,
    access_token  TEXT,
    expires_at    TEXT,           -- ISO 시각 (UTC), access_token 만료 시점
    scopes        TEXT,           -- 공백 구분 scope 리스트
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Phase 5.1: Naver Calendar 연동 토큰. (네이버 로그인 = 캘린더 권한 동시 부여)
CREATE TABLE IF NOT EXISTS naver_tokens (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    refresh_token TEXT NOT NULL,
    access_token  TEXT,
    expires_at    TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Phase 6: 보유 서류를 localStorage 에서 DB 로 이관 (사용자별 격리)
CREATE TABLE IF NOT EXISTS user_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                 -- 서류명 (예: '주민등록등본')
    issued_date TEXT NOT NULL,                 -- ISO YYYY-MM-DD
    note        TEXT,                          -- 사용자 메모 (선택)
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_documents_user ON user_documents(user_id);

-- Phase 7 (Step 4): 공고 페이지에서 자동 수집한 첨부파일. 시스템 소유 (user_id 없음) —
-- 같은 공고를 여러 사용자가 봐도 한 번만 다운로드/분석. (announcement_id, source_url) dedup.
-- file_hash 가 채워지면 analyses/{file_hash}.json 으로 7카테고리 조회.
CREATE TABLE IF NOT EXISTS announcement_auto_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    source_url      TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    file_hash       TEXT,                       -- 분석 완료 후 채워짐 (analyses/{hash}.json 키)
    file_format     TEXT,                       -- 'pdf' | 'image' | 'hwpx' | null(unsupported)
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | done | skipped | failed
    error_message   TEXT,
    skipped_reason  TEXT,                       -- 'unsupported_format' | 'too_large' | 'download_failed'
    fetched_at      TEXT,
    analyzed_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (announcement_id, source_url)
);
CREATE INDEX IF NOT EXISTS idx_auto_attachments_ann ON announcement_auto_attachments(announcement_id);
CREATE INDEX IF NOT EXISTS idx_auto_attachments_hash ON announcement_auto_attachments(file_hash);

-- Phase 8 (라이브러리): 사용자가 "라이브러리에 담은" 공고. 새로고침 후에도 유지.
-- source: 'recommendation' (AI 추천에서 담음) | 'browse' (둘러보기) | 'compare' | 'panel'
CREATE TABLE IF NOT EXISTS user_picked_announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    source          TEXT NOT NULL DEFAULT 'recommendation',
    note            TEXT,
    picked_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, announcement_id)
);
CREATE INDEX IF NOT EXISTS idx_picked_user ON user_picked_announcements(user_id);
CREATE INDEX IF NOT EXISTS idx_picked_ann  ON user_picked_announcements(announcement_id);

-- Phase 8 (라이브러리): 사이드 패널 채팅 메시지 영구 저장. 공고당 누적.
-- role: 'user' | 'assistant'. content: 메시지 본문 (LLM 응답 또는 사용자 질문).
CREATE TABLE IF NOT EXISTS announcement_chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,                  -- 'user' | 'assistant'
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_user_ann ON announcement_chat_messages(user_id, announcement_id, id);
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
    # Phase 2: 사용자별 자원 격리. 기존 row 는 NULL (legacy = 누구에게도 안 보임)
    ("attachment_folders", "user_id", "INTEGER REFERENCES users(id)"),
    ("uploaded_attachments", "user_id", "INTEGER REFERENCES users(id)"),
    ("announcement_schedule_events", "user_id", "INTEGER REFERENCES users(id)"),
    # Phase 3.1: 확장된 기업 프로필 (사업계획서 자동 생성 대비)
    ("user_profiles", "english_name",        "TEXT"),
    ("user_profiles", "corporation_number",  "TEXT"),
    ("user_profiles", "representative_birth","TEXT"),
    ("user_profiles", "employee_count",      "INTEGER"),
    ("user_profiles", "founding_type",       "TEXT"),
    ("user_profiles", "business_address",    "TEXT"),
    ("user_profiles", "address_zonecode",    "TEXT"),
    ("user_profiles", "phone",               "TEXT"),
    ("user_profiles", "fax",                 "TEXT"),
    ("user_profiles", "website",             "TEXT"),
    ("user_profiles", "industry_code",       "TEXT"),
    ("user_profiles", "industry_type",       "TEXT"),
    # Phase 4: 폴더 ↔ 공고 연결 (정밀 비교: 매칭 카드에서 PDF 첨부 시 자동 생성)
    ("attachment_folders", "announcement_id", "INTEGER REFERENCES announcements(id)"),
    # Phase 8 (Step A 분야 매칭): 사용자가 자기 분야를 멀티 선택. JSON 배열 (예: '["it_ai","fintech"]').
    # NULL/빈 배열이면 분야 점수 = 중립 (15점), 적용 안 한 사용자도 기존처럼 동작.
    ("user_profiles", "interest_tags",       "TEXT"),
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
    "CREATE INDEX IF NOT EXISTS idx_attachment_folders_user ON attachment_folders(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_user ON uploaded_attachments(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_schedule_events_user ON announcement_schedule_events(user_id)",
    # 같은 PDF 를 여러 사용자가 올릴 수 있도록 복합 UNIQUE (NULL 은 distinct)
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_uploaded_hash_user ON uploaded_attachments(file_hash, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_attachment_folders_announcement ON attachment_folders(announcement_id)",
]


def _rebuild_uploaded_attachments_if_needed(conn: sqlite3.Connection) -> bool:
    """기존 DB 의 uploaded_attachments.file_hash UNIQUE 컬럼 제약을 제거하기 위해
    테이블 재구축. (SQLite 는 ALTER TABLE DROP CONSTRAINT 미지원)
    이미 마이그레이션됐으면 no-op. 작업 수행 시 True 반환."""
    indexes = list(conn.execute("PRAGMA index_list(uploaded_attachments)"))
    has_old_unique = False
    for idx in indexes:
        # row: (seq, name, unique, origin, partial)
        name = idx[1]
        unique = idx[2]
        if unique and name.startswith("sqlite_autoindex_uploaded_attachments"):
            cols = list(conn.execute(f"PRAGMA index_info({name})"))
            # cols row: (seqno, cid, name)
            if len(cols) == 1 and cols[0][2] == "file_hash":
                has_old_unique = True
                break
    if not has_old_unique:
        return False

    # 새 테이블에 기존 SCHEMA 와 정확히 동일하게 (단 UNIQUE 없이) 생성 후 복사
    conn.executescript("""
        CREATE TABLE uploaded_attachments_new (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash         TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            announcement_id   TEXT,
            folder_id         INTEGER REFERENCES attachment_folders(id),
            user_id           INTEGER REFERENCES users(id),
            uploaded_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            analyzed_at       TEXT,
            status            TEXT NOT NULL DEFAULT 'pending'
        );
        INSERT INTO uploaded_attachments_new
            (id, file_hash, original_filename, announcement_id, folder_id, user_id, uploaded_at, analyzed_at, status)
        SELECT id, file_hash, original_filename, announcement_id, folder_id, user_id, uploaded_at, analyzed_at, status
        FROM uploaded_attachments;
        DROP TABLE uploaded_attachments;
        ALTER TABLE uploaded_attachments_new RENAME TO uploaded_attachments;
    """)
    return True


def _cleanup_legacy_resources(conn: sqlite3.Connection) -> list[str]:
    """user_id IS NULL 인 legacy 데이터를 정리. 삭제된 PDF file_hash 들을 반환
    (호출자가 스토리지 파일 정리)."""
    # 사용 중인 file_hash 들 (다른 사용자가 들고있는 것) 파악
    used_hashes = {
        r[0] for r in conn.execute(
            "SELECT file_hash FROM uploaded_attachments WHERE user_id IS NOT NULL"
        )
    }
    legacy_hashes = [
        r[0] for r in conn.execute(
            "SELECT file_hash FROM uploaded_attachments WHERE user_id IS NULL"
        )
    ]
    # 다른 사용자가 갖고있지 않은 hash 만 storage 에서 삭제 대상
    to_delete_storage = [h for h in legacy_hashes if h not in used_hashes]

    conn.execute("DELETE FROM announcement_schedule_events WHERE user_id IS NULL")
    conn.execute("DELETE FROM uploaded_attachments WHERE user_id IS NULL")
    conn.execute("DELETE FROM attachment_folders WHERE user_id IS NULL")
    return to_delete_storage


def ensure_default_folder_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    """사용자에게 폴더가 하나도 없으면 '기본' 폴더 생성. 폴더가 없는 user 의
    기존 첨부(있다면)는 거기로 편입. 사용자의 첫 폴더 ID 를 반환."""
    row = conn.execute(
        "SELECT id FROM attachment_folders WHERE user_id=? ORDER BY id LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is not None:
        return row[0] if isinstance(row, tuple) else row["id"]
    cur = conn.execute(
        "INSERT INTO attachment_folders(name, user_id) VALUES (?, ?)",
        ("기본", user_id),
    )
    default_id = cur.lastrowid
    conn.execute(
        "UPDATE uploaded_attachments SET folder_id=? "
        "WHERE folder_id IS NULL AND user_id=?",
        (default_id, user_id),
    )
    return default_id


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        # 기존 DB 의 file_hash UNIQUE 컬럼 제약 제거 (Phase 2.1)
        rebuilt = _rebuild_uploaded_attachments_if_needed(conn)
        if rebuilt:
            print("[db] uploaded_attachments 테이블 재구축 완료 (file_hash UNIQUE 제거)")
        for ddl in _POST_MIGRATION_INDEXES:
            conn.execute(ddl)
        # Legacy NULL user_id 자원 자동 정리 (Phase 2.1)
        legacy_hashes = _cleanup_legacy_resources(conn)
        if legacy_hashes:
            print(f"[db] legacy 자원 {len(legacy_hashes)}개 storage 정리 중...")
            try:
                from planner.analyzer.storage import delete_attachment as _ds
                for h in legacy_hashes:
                    _ds(h)
            except Exception as e:  # noqa: BLE001
                print(f"[db] storage 정리 부분 실패: {e}")
        # 사용자별 기본 폴더는 첫 로그인 이후 lazy 생성 (web.py 의 _require_user_id 에서)


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
