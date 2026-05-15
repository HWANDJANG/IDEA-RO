"""표준 라이브러리만으로 동작하는 가벼운 웹 데모.

실행:
    python -m planner.web
    → http://127.0.0.1:8765 접속

라우트:
    GET  /                  index.html (단일 페이지)
    GET  /api/documents     마스터 서류 리스트
    POST /api/check         검증 결과 + 발급 태스크
"""

from __future__ import annotations

import json
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from planner.checker import build_preparation_schedule, check_document_validity
from planner.document_master import DOCUMENT_MASTER
from planner.ics_export import (
    build_calendar,
    fetch_announcement_events,
    task_to_event,
)
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
        elif path == "/api/ics/announcements":
            self._handle_ics_announcements(parsed.query)
        else:
            self._send_json({"error": "not found"}, status=404)

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

        req_map = {r["name"]: r for r in required}
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

        tasks = build_preparation_schedule(deadline, required, user_docs or None)
        self._send_json({
            "deadline": deadline.isoformat(),
            "required_docs": required,
            "checks": checks,
            "tasks": [_serialize_task(t) for t in tasks],
        })

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
