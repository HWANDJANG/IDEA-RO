"""Vercel serverless function — POST /api/ics/tasks

요청 body:
    {
      "tasks": [PreparationTask, ...],
      "calendar_name": "..."  (선택)
    }
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from planner.ics_export import build_calendar, task_to_event  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            return self._error(400, f"invalid JSON: {e}")

        tasks = data.get("tasks") or []
        if not tasks:
            return self._error(400, "tasks 가 비어있습니다")
        try:
            events = [task_to_event(t) for t in tasks]
        except (KeyError, ValueError, TypeError) as e:
            return self._error(400, f"bad task payload: {e}")

        cal_name = data.get("calendar_name") or f"서류 발급 일정 ({len(events)}건)"
        ics = build_calendar(events, cal_name=cal_name).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="document-tasks.ics"')
        self.send_header("Content-Length", str(len(ics)))
        self.end_headers()
        self.wfile.write(ics)

    def _error(self, status: int, msg: str):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args):
        return
