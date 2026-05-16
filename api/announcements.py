"""GET /api/announcements — DB의 공고 리스트(진행중 우선).

쿼리:
    ?source=nrf          특정 source 만
    ?limit=20            최대 개수 (기본 50)
    ?all=1               마감 지난 공고까지 포함
"""

from __future__ import annotations

import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner.paths import DB_PATH  # noqa: E402


def fetch_announcements(
    only_upcoming: bool = True,
    source: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        q = (
            "SELECT a.id, a.external_id, a.title, a.status, a.d_day, "
            "       a.start_date, a.end_date, a.department, a.contact, "
            "       a.detail_url, a.content_text, "
            "       s.code AS source_code, s.name AS source_name, "
            "       c.name AS category_name "
            "FROM announcements a "
            "JOIN sources s ON s.id = a.source_id "
            "LEFT JOIN categories c ON c.id = a.category_id "
            "WHERE a.end_date IS NOT NULL AND a.end_date != ''"
        )
        params: list = []
        if source:
            q += " AND s.code = ?"
            params.append(source)
        if only_upcoming:
            q += " AND substr(a.end_date, 1, 10) >= date('now')"
        q += " ORDER BY a.end_date ASC LIMIT ?"
        params.append(int(limit))
        rows = [dict(r) for r in conn.execute(q, params)]
    finally:
        conn.close()
    return rows


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        source = (qs.get("source") or [None])[0]
        include_all = (qs.get("all") or ["0"])[0] in ("1", "true", "yes")
        try:
            limit = int((qs.get("limit") or ["50"])[0])
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 200))

        rows = fetch_announcements(only_upcoming=not include_all, source=source, limit=limit)
        # content_text 는 응답에서 길이가 커서 요약만 보냄 (full text 는 /api/match 안에서만 사용)
        compact = []
        for r in rows:
            content = r.get("content_text") or ""
            compact.append({
                **{k: v for k, v in r.items() if k != "content_text"},
                "content_preview": content[:300],
                "content_length": len(content),
            })
        body = json.dumps(
            {"announcements": compact, "total": len(compact)},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args):
        return
