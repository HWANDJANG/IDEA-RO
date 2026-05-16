"""표준 라이브러리만으로 동작하는 가벼운 웹 데모.

실행:
    python -m planner.web
    → http://127.0.0.1:8765 접속

라우트:
    GET  /                       index.html (단일 페이지)
    GET  /api/documents          마스터 서류 리스트
    POST /api/check              검증 결과 + 발급 태스크
    GET  /api/announcements      DB 공고 리스트 (진행중 우선)
    POST /api/match              보유 서류 ↔ 공고 매칭
    GET  /api/ics/announcements  공고 마감일 ICS
    POST /api/ics/tasks          발급 태스크 ICS
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from planner.checker import (
    build_preparation_schedule,
    check_document_validity,
    filter_by_business_type,
)
from planner.document_master import DOCUMENT_MASTER
from planner.ics_export import (
    build_calendar,
    fetch_announcement_events,
    task_to_event,
)
from planner.matcher import match_announcement
from planner.paths import DB_PATH, STATIC_DIR  # noqa: F401  (DB_PATH 는 아래 라우트에서 사용)


def _to_iso(d):
    return d.isoformat() if isinstance(d, date) else d


def _serialize_check(c: dict) -> dict:
    out = dict(c)
    rw = out.get("reissue_window")
    if rw:
        out["reissue_window"] = [_to_iso(rw[0]), _to_iso(rw[1])]
    return out


def _serialize_task(t: dict) -> dict:
    return {**t, "due_date": _to_iso(t["due_date"]), "earliest_date": _to_iso(t["earliest_date"])}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._send_json({"error": "not found"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_ics(self, ics_text: str, filename: str = "calendar.ics") -> None:
        body = ics_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/documents":
            self._send_json({"documents": list(DOCUMENT_MASTER.values())})
        elif path == "/api/announcements":
            self._handle_announcements_list(parsed.query)
        elif path == "/api/ics/announcements":
            self._handle_ics_announcements(parsed.query)
        else:
            self._send_json({"error": "not found"}, status=404)

    def _handle_announcements_list(self, query: str) -> None:
        qs = parse_qs(query)
        source = (qs.get("source") or [None])[0]
        include_all = (qs.get("all") or ["0"])[0] in ("1", "true", "yes")
        try:
            limit = max(1, min(int((qs.get("limit") or ["50"])[0]), 200))
        except ValueError:
            limit = 50
        if not DB_PATH.exists():
            self._send_json({"error": f"DB 파일 없음: {DB_PATH}"}, status=500)
            return
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
            if not include_all:
                q += " AND substr(a.end_date,1,10) >= date('now')"
            q += " ORDER BY a.end_date ASC LIMIT ?"
            params.append(limit)
            rows = [dict(r) for r in conn.execute(q, params)]
        finally:
            conn.close()
        compact = []
        for r in rows:
            content = r.get("content_text") or ""
            compact.append({
                **{k: v for k, v in r.items() if k != "content_text"},
                "content_preview": content[:300],
                "content_length": len(content),
            })
        self._send_json({"announcements": compact, "total": len(compact)})

    def _handle_ics_announcements(self, query: str) -> None:
        from urllib.parse import parse_qs
        qs = parse_qs(query)
        source = (qs.get("source") or [None])[0]
        include_all = (qs.get("all") or ["0"])[0] in ("1", "true", "yes")
        if not DB_PATH.exists():
            self._send_json({"error": f"DB 파일을 찾을 수 없습니다: {DB_PATH}"}, status=500)
            return
        events = fetch_announcement_events(
            DB_PATH, only_upcoming=not include_all, source=source
        )
        cal_name = f"정부지원사업 마감일 ({len(events)}건)"
        ics = build_calendar(events, cal_name=cal_name)
        fname = f"announcements{('-' + source) if source else ''}.ics"
        self._send_ics(ics, filename=fname)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/ics/tasks":
            self._handle_ics_tasks()
            return
        if path == "/api/match":
            self._handle_match()
            return
        if path != "/api/check":
            self._send_json({"error": "not found"}, status=404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return

        try:
            deadline = date.fromisoformat(data["deadline"])
            required = [
                {"name": r["name"], "required_within_days": r.get("required_within_days")}
                for r in data.get("required_docs", [])
            ]
            user_docs = [
                {"name": u["name"], "issued_date": date.fromisoformat(u["issued_date"])}
                for u in (data.get("user_documents") or [])
            ]
        except (KeyError, ValueError, TypeError) as e:
            self._send_json({"error": f"bad input: {e}"}, status=400)
            return

        business_type = data.get("business_type")
        if business_type not in (None, "", "individual", "corporate"):
            self._send_json({"error": "business_type must be 'individual', 'corporate', or null"}, status=400)
            return
        if business_type == "":
            business_type = None

        filtered_required, skipped = filter_by_business_type(required, business_type)

        req_map = {r["name"]: r for r in filtered_required}
        checks = []
        for ud in user_docs:
            req = req_map.get(ud["name"], {})
            result = check_document_validity(
                ud["name"], ud["issued_date"], deadline, req.get("required_within_days"),
            )
            checks.append({
                "name": ud["name"],
                "issued_date": ud["issued_date"].isoformat(),
                **_serialize_check(result),
            })

        tasks = build_preparation_schedule(
            deadline, required, user_docs or None, user_business_type=business_type,
        )
        self._send_json({
            "deadline": deadline.isoformat(),
            "business_type": business_type,
            "required_docs": filtered_required,
            "skipped_documents": skipped,
            "checks": checks,
            "tasks": [_serialize_task(t) for t in tasks],
        })

    def _handle_match(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return

        business_type = data.get("business_type", "individual")
        if business_type not in ("individual", "corporate"):
            self._send_json({"error": "business_type must be 'individual' or 'corporate'"}, status=400)
            return

        user_documents = []
        try:
            for u in data.get("user_documents") or []:
                user_documents.append({
                    "name": u["name"],
                    "issued_date": date.fromisoformat(u["issued_date"]),
                })
        except (KeyError, ValueError, TypeError) as e:
            self._send_json({"error": f"bad user_documents: {e}"}, status=400)
            return

        overrides = data.get("overrides") or None
        ann_id = None
        title = None
        content_text = None
        deadline = None

        if data.get("announcement_id") is not None:
            try:
                ann_id = int(data["announcement_id"])
            except (TypeError, ValueError):
                self._send_json({"error": "announcement_id must be int"}, status=400)
                return
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT id, title, end_date, content_text FROM announcements WHERE id=?",
                    (ann_id,),
                ).fetchone()
            finally:
                conn.close()
            if not row:
                self._send_json({"error": f"announcement {ann_id} not found"}, status=404)
                return
            try:
                deadline = date.fromisoformat((row["end_date"] or "")[:10])
            except ValueError:
                self._send_json({"error": f"end_date 형식 오류: {row['end_date']!r}"}, status=400)
                return
            title = row["title"]
            content_text = row["content_text"]
        else:
            try:
                deadline = date.fromisoformat(data["deadline"])
            except (KeyError, ValueError) as e:
                self._send_json({"error": f"deadline 필요 또는 형식 오류: {e}"}, status=400)
                return
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
        result["tasks"] = [_serialize_task(t) for t in result["tasks"]]
        self._send_json(result)

    def _handle_ics_tasks(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        tasks = data.get("tasks") or []
        if not tasks:
            self._send_json({"error": "tasks 가 비어있습니다"}, status=400)
            return
        try:
            events = [task_to_event(t) for t in tasks]
        except (KeyError, ValueError, TypeError) as e:
            self._send_json({"error": f"bad task payload: {e}"}, status=400)
            return
        cal_name = data.get("calendar_name") or f"서류 발급 일정 ({len(events)}건)"
        ics = build_calendar(events, cal_name=cal_name)
        self._send_ics(ics, filename="document-tasks.ics")

    def log_message(self, format: str, *args) -> None:  # 콘솔 조용히
        return


def main(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"서류 만료 체커 데모 → http://{host}:{port}")
    print("종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
