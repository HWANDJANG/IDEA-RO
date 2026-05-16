"""Vercel serverless function — GET /api/documents

로컬 `planner/web.py` 의 동일 경로와 같은 응답을 돌려준다.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# Vercel 환경에서도 planner 패키지를 import 가능하게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner.document_master import DOCUMENT_MASTER  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(
            {"documents": [{"key": k, **v} for k, v in DOCUMENT_MASTER.items()]},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args):  # 콘솔 조용히
        return
