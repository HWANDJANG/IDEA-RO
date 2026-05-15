"""Vercel serverless function — GET /api/ics/announcements

쿼리:
    ?source=nrf       특정 source code 만
    ?all=1            마감 지난 공고까지 포함
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# api/ics/announcements.py → 프로젝트 루트는 parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from planner.ics_export import (  # noqa: E402
    build_calendar,
    fetch_announcement_events,
)
from planner.paths import DB_PATH  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        source = (qs.get("source") or [None])[0]
        include_all = (qs.get("all") or ["0"])[0] in ("1", "true", "yes")

        if not DB_PATH.exists():
            body = json.dumps(
                {"error": f"DB 파일을 찾을 수 없습니다: {DB_PATH}"},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        events = fetch_announcement_events(
            DB_PATH, only_upcoming=not include_all, source=source
        )
        ics = build_calendar(
            events,
            cal_name=f"정부지원사업 마감일 ({len(events)}건)",
        ).encode("utf-8")
        fname = f"announcements{('-' + source) if source else ''}.ics"
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(len(ics)))
        # 구글 캘린더 URL 구독은 주기적으로 GET 함 — 적당한 캐시 허용
        self.send_header("Cache-Control", "public, max-age=600")
        self.end_headers()
        self.wfile.write(ics)

    def log_message(self, format: str, *args):
        return
