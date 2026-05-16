"""POST /api/match — 보유 서류 ↔ 공고 매칭.

요청 body (둘 중 하나):
    A. DB 공고 ID 로 매칭
       {
         "announcement_id": 1,
         "business_type": "corporate",
         "user_documents": [...],
         "overrides": [...]   (선택)
       }

    B. ad-hoc 입력 (DB 없이)
       {
         "title": "...",
         "deadline": "YYYY-MM-DD",
         "content_text": "..."   (선택, 본문 보조 추출용),
         "business_type": "individual",
         "user_documents": [...]
       }
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner.matcher import match_announcement  # noqa: E402
from planner.paths import DB_PATH  # noqa: E402


def _to_iso(d):
    return d.isoformat() if isinstance(d, date) else d


def _serialize_task(t: dict) -> dict:
    return {
        **t,
        "due_date": _to_iso(t["due_date"]),
        "earliest_date": _to_iso(t["earliest_date"]),
    }


def _load_announcement(ann_id: int) -> dict | None:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, title, end_date, content_text FROM announcements WHERE id=?",
            (ann_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            return self._error(400, f"invalid JSON: {e}")

        business_type = data.get("business_type", "individual")
        if business_type not in ("individual", "corporate"):
            return self._error(400, "business_type must be 'individual' or 'corporate'")

        user_documents = []
        for u in data.get("user_documents") or []:
            try:
                user_documents.append({
                    "name": u["name"],
                    "issued_date": date.fromisoformat(u["issued_date"]),
                })
            except (KeyError, ValueError, TypeError) as e:
                return self._error(400, f"bad user_documents: {e}")

        overrides = data.get("overrides") or None

        # Path A: DB 공고 ID
        if data.get("announcement_id") is not None:
            try:
                ann_id = int(data["announcement_id"])
            except (TypeError, ValueError):
                return self._error(400, "announcement_id must be int")
            row = _load_announcement(ann_id)
            if not row:
                return self._error(404, f"announcement {ann_id} not found")
            try:
                deadline = date.fromisoformat((row["end_date"] or "")[:10])
            except ValueError:
                return self._error(400, f"announcement end_date 형식 오류: {row['end_date']!r}")
            title = row["title"]
            content_text = row["content_text"]
        # Path B: ad-hoc
        else:
            try:
                deadline = date.fromisoformat(data["deadline"])
            except (KeyError, ValueError) as e:
                return self._error(400, f"deadline 필요 또는 형식 오류: {e}")
            ann_id = None
            title = data.get("title")
            content_text = data.get("content_text")

        result = match_announcement(
            announcement_id=ann_id,
            title=title,
            deadline=deadline,
            content_text=content_text,
            business_type=business_type,
            user_documents=user_documents,
            overrides=overrides,
        )
        # tasks 안의 date 직렬화
        result["tasks"] = [_serialize_task(t) for t in result["tasks"]]
        self._send_json(result)

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, msg: str):
        self._send_json({"error": msg}, status=status)

    def log_message(self, format: str, *args):
        return
