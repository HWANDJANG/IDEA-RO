"""Vercel serverless function — POST /api/check

요청 body:
    {
      "deadline": "YYYY-MM-DD",
      "required_docs": [{"name": "...", "required_within_days": 30}, ...],
      "user_documents": [{"name": "...", "issued_date": "YYYY-MM-DD"}, ...]
    }
"""

from __future__ import annotations

import json
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner.checker import (  # noqa: E402
    build_preparation_schedule,
    check_document_validity,
)


def _to_iso(d):
    return d.isoformat() if isinstance(d, date) else d


def _serialize_check(c: dict) -> dict:
    out = dict(c)
    rw = out.get("reissue_window")
    if rw:
        out["reissue_window"] = [_to_iso(rw[0]), _to_iso(rw[1])]
    return out


def _serialize_task(t: dict) -> dict:
    return {
        **t,
        "due_date": _to_iso(t["due_date"]),
        "earliest_date": _to_iso(t["earliest_date"]),
    }


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            return self._error(400, f"invalid JSON: {e}")

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
            return self._error(400, f"bad input: {e}")

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
