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
from planner.multipart import MultipartError, parse_multipart
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
            self._send_json({
                "documents": [{"key": k, **v} for k, v in DOCUMENT_MASTER.items()],
            })
        elif path == "/api/announcements":
            self._handle_announcements_list(parsed.query)
        elif path == "/api/ics/announcements":
            self._handle_ics_announcements(parsed.query)
        elif path == "/api/attachments":
            self._handle_attachments_list(parsed.query)
        elif path.startswith("/api/attachments/"):
            file_hash = path[len("/api/attachments/"):].strip("/")
            self._handle_attachment_get(file_hash)
        elif path == "/api/folders":
            self._handle_folders_list()
        elif path.startswith("/api/folders/") and path.endswith("/schedule"):
            folder_id_str = path[len("/api/folders/"):-len("/schedule")]
            self._handle_schedule_list(folder_id_str)
        elif path.startswith("/api/folders/") and path.endswith("/schedule.ics"):
            folder_id_str = path[len("/api/folders/"):-len("/schedule.ics")]
            self._handle_schedule_ics(folder_id_str)
        elif path == "/api/schedule/all":
            self._handle_schedule_all()
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
        if path == "/api/upload":
            self._handle_upload()
            return
        if path == "/api/folders":
            self._handle_folder_create()
            return
        if path == "/api/schedule":
            self._handle_schedule_add()
            return
        if path == "/api/schedule/ics":
            self._handle_schedule_ics_selection()
            return
        if path.startswith("/api/attachments/") and path.endswith("/reanalyze"):
            file_hash = path[len("/api/attachments/"):-len("/reanalyze")]
            self._handle_attachment_reanalyze(file_hash)
            return
        if path.startswith("/api/folders/") and path.endswith("/extract-schedule"):
            folder_id_str = path[len("/api/folders/"):-len("/extract-schedule")]
            self._handle_folder_extract_schedule(folder_id_str)
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

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/attachments/"):
            file_hash = path[len("/api/attachments/"):].strip("/")
            self._handle_attachment_delete(file_hash)
            return
        if path.startswith("/api/folders/"):
            folder_id_str = path[len("/api/folders/"):].strip("/")
            self._handle_folder_delete(folder_id_str)
            return
        if path.startswith("/api/schedule/"):
            event_id_str = path[len("/api/schedule/"):].strip("/")
            self._handle_schedule_delete(event_id_str)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/folders/"):
            folder_id_str = path[len("/api/folders/"):].strip("/")
            self._handle_folder_rename(folder_id_str)
            return
        self._send_json({"error": "not found"}, status=404)

    # ─── 첨부파일 분석 라우트 ─────────────────────────────────────────────
    def _handle_upload(self) -> None:
        from planner.analyzer.analyzer import analyze_pdf
        from planner.analyzer.llm.base import LLMError

        ct = self.headers.get("Content-Type", "")
        if not ct.lower().startswith("multipart/form-data"):
            self._send_json({"error": "Content-Type must be multipart/form-data"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._send_json({"error": "empty body"}, status=400)
            return
        # 50 MB 안전 상한
        if length > 50 * 1024 * 1024:
            self._send_json({"error": "file too large (50MB limit)"}, status=413)
            return
        raw = self.rfile.read(length)
        try:
            fields = parse_multipart(ct, raw)
        except MultipartError as e:
            self._send_json({"error": f"bad multipart: {e}"}, status=400)
            return

        file_field = fields.get("file") or fields.get("pdf")
        if file_field is None or not file_field.data:
            self._send_json({"error": "missing 'file' field"}, status=400)
            return
        filename = (file_field.filename or "uploaded.pdf").strip()
        if not filename.lower().endswith(".pdf"):
            self._send_json({"error": "only .pdf files are supported"}, status=415)
            return

        announcement_id = (fields.get("announcement_id").data.decode("utf-8")
                           if "announcement_id" in fields else None) or None
        folder_id_raw = (fields.get("folder_id").data.decode("utf-8")
                         if "folder_id" in fields else None)
        try:
            folder_id = int(folder_id_raw) if folder_id_raw else None
        except ValueError:
            self._send_json({"error": "folder_id must be int"}, status=400)
            return

        # folder_id 가 비어있으면 가장 오래된 폴더(보통 '기본')로 떨어뜨림
        if folder_id is None and DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                row = conn.execute(
                    "SELECT id FROM attachment_folders ORDER BY id LIMIT 1"
                ).fetchone()
                if row:
                    folder_id = row[0]
            finally:
                conn.close()

        # 분석 실행 (캐시 히트 시 즉시 반환됨)
        try:
            analysis = analyze_pdf(file_field.data, original_filename=filename)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001 — 사용자에게 메시지를 보여주기 위함
            self._send_json({"error": f"분석 실패: {e}"}, status=500)
            return

        # DB 에 기록 (upsert). 같은 PDF 를 다른 폴더로 다시 올리면 폴더가 갱신됨.
        file_hash = analysis["file_hash"]
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute(
                    "INSERT INTO uploaded_attachments(file_hash, original_filename, announcement_id, folder_id, status, analyzed_at) "
                    "VALUES (?,?,?,?, 'done', CURRENT_TIMESTAMP) "
                    "ON CONFLICT(file_hash) DO UPDATE SET "
                    "  original_filename=excluded.original_filename, "
                    "  announcement_id=COALESCE(excluded.announcement_id, uploaded_attachments.announcement_id), "
                    "  folder_id=COALESCE(excluded.folder_id, uploaded_attachments.folder_id), "
                    "  status='done', analyzed_at=CURRENT_TIMESTAMP",
                    (file_hash, filename, announcement_id, folder_id),
                )
                conn.commit()
            finally:
                conn.close()

        self._send_json({"file_hash": file_hash, "analysis": analysis, "folder_id": folder_id})

    def _handle_attachments_list(self, query: str = "") -> None:
        if not DB_PATH.exists():
            self._send_json({"attachments": []})
            return
        qs = parse_qs(query)
        folder_filter = qs.get("folder_id", [None])[0]
        try:
            folder_id = int(folder_filter) if folder_filter else None
        except ValueError:
            self._send_json({"error": "folder_id must be int"}, status=400)
            return

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            if folder_id is not None:
                cursor = conn.execute(
                    "SELECT id, file_hash, original_filename, announcement_id, folder_id, "
                    "       uploaded_at, analyzed_at, status "
                    "FROM uploaded_attachments WHERE folder_id=? "
                    "ORDER BY uploaded_at DESC",
                    (folder_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, file_hash, original_filename, announcement_id, folder_id, "
                    "       uploaded_at, analyzed_at, status "
                    "FROM uploaded_attachments ORDER BY uploaded_at DESC"
                )
            rows = [dict(r) for r in cursor]
        finally:
            conn.close()

        if qs.get("include", [""])[0] == "analysis":
            from planner.analyzer.storage import load_analysis
            for r in rows:
                r["analysis"] = load_analysis(r["file_hash"])

        self._send_json({"attachments": rows})

    # ─── 폴더 CRUD ─────────────────────────────────────────────────────────
    def _handle_folders_list(self) -> None:
        if not DB_PATH.exists():
            self._send_json({"folders": []})
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT f.id, f.name, f.created_at, "
                "  (SELECT COUNT(*) FROM uploaded_attachments u WHERE u.folder_id = f.id) AS count "
                "FROM attachment_folders f ORDER BY f.id"
            )]
        finally:
            conn.close()
        self._send_json({"folders": rows})

    def _handle_folder_create(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        name = (data.get("name") or "").strip()
        if not name:
            self._send_json({"error": "folder name required"}, status=400)
            return
        if len(name) > 100:
            self._send_json({"error": "folder name too long (max 100)"}, status=400)
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute("INSERT INTO attachment_folders(name) VALUES (?)", (name,))
            conn.commit()
            new_id = cur.lastrowid
            row = conn.execute(
                "SELECT id, name, created_at FROM attachment_folders WHERE id=?",
                (new_id,),
            ).fetchone()
        finally:
            conn.close()
        self._send_json({"folder": dict(row) if row else {"id": new_id, "name": name}})

    def _handle_folder_rename(self, folder_id_str: str) -> None:
        try:
            folder_id = int(folder_id_str)
        except ValueError:
            self._send_json({"error": "invalid folder id"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        name = (data.get("name") or "").strip()
        if not name:
            self._send_json({"error": "folder name required"}, status=400)
            return
        if len(name) > 100:
            self._send_json({"error": "folder name too long (max 100)"}, status=400)
            return
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                "UPDATE attachment_folders SET name=? WHERE id=?", (name, folder_id)
            )
            conn.commit()
            if cur.rowcount == 0:
                self._send_json({"error": "folder not found"}, status=404)
                return
        finally:
            conn.close()
        self._send_json({"folder": {"id": folder_id, "name": name}})

    def _handle_folder_delete(self, folder_id_str: str) -> None:
        from planner.analyzer.storage import delete_attachment as _delete_storage
        try:
            folder_id = int(folder_id_str)
        except ValueError:
            self._send_json({"error": "invalid folder id"}, status=400)
            return
        if not DB_PATH.exists():
            self._send_json({"error": "db not found"}, status=500)
            return
        conn = sqlite3.connect(DB_PATH)
        try:
            hashes = [r[0] for r in conn.execute(
                "SELECT file_hash FROM uploaded_attachments WHERE folder_id=?",
                (folder_id,),
            )]
            conn.execute("DELETE FROM announcement_schedule_events WHERE folder_id=?", (folder_id,))
            conn.execute("DELETE FROM uploaded_attachments WHERE folder_id=?", (folder_id,))
            conn.execute("DELETE FROM attachment_folders WHERE id=?", (folder_id,))
            conn.commit()
        finally:
            conn.close()
        # 스토리지 파일도 정리
        for h in hashes:
            _delete_storage(h)
        self._send_json({"deleted_folder": folder_id, "deleted_files": len(hashes)})

    # ─── 폴더별 일정 ──────────────────────────────────────────────────────
    def _handle_schedule_list(self, folder_id_str: str) -> None:
        try:
            folder_id = int(folder_id_str)
        except ValueError:
            self._send_json({"error": "invalid folder id"}, status=400)
            return
        if not DB_PATH.exists():
            self._send_json({"events": []})
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, folder_id, file_hash, title, type, date_start, date_end, "
                "       time, note, source_page, added_at "
                "FROM announcement_schedule_events WHERE folder_id=? "
                "ORDER BY date_start, COALESCE(time,''), id",
                (folder_id,),
            )]
        finally:
            conn.close()
        self._send_json({"events": rows})

    def _handle_schedule_add(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        try:
            folder_id = int(data.get("folder_id"))
        except (TypeError, ValueError):
            self._send_json({"error": "folder_id required"}, status=400)
            return
        events = data.get("events") or []
        if not isinstance(events, list) or not events:
            self._send_json({"error": "events array required"}, status=400)
            return
        file_hash = data.get("file_hash")  # optional
        replace_folder = bool(data.get("replace_folder"))
        inserted_ids = []
        deleted_count = 0
        conn = sqlite3.connect(DB_PATH)
        try:
            if replace_folder:
                cur = conn.execute(
                    "DELETE FROM announcement_schedule_events WHERE folder_id=?",
                    (folder_id,),
                )
                deleted_count = cur.rowcount or 0
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                title = (ev.get("title") or "").strip()
                ds = (ev.get("date_start") or "").strip()
                if not title or not ds:
                    continue
                cur = conn.execute(
                    "INSERT INTO announcement_schedule_events"
                    "(folder_id, file_hash, title, type, date_start, date_end, time, note, source_page) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        folder_id,
                        file_hash,
                        title,
                        ev.get("type") or "other",
                        ds,
                        ev.get("date_end"),
                        ev.get("time"),
                        ev.get("note"),
                        ev.get("page"),
                    ),
                )
                inserted_ids.append(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        self._send_json({
            "inserted_ids": inserted_ids,
            "count": len(inserted_ids),
            "deleted_count": deleted_count,
        })

    _SCHEDULE_TYPE_LABEL = {
        "recruitment_period": "신청·접수",
        "announcement_date": "발표",
        "evaluation_date": "심사·평가",
        "business_period": "사업 수행",
        "contract_date": "협약 체결",
        "other": "일정",
    }

    def _handle_schedule_ics(self, folder_id_str: str) -> None:
        try:
            folder_id = int(folder_id_str)
        except ValueError:
            self._send_json({"error": "invalid folder id"}, status=400)
            return
        if not DB_PATH.exists():
            self._send_json({"error": "db not found"}, status=500)
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            folder_row = conn.execute(
                "SELECT name FROM attachment_folders WHERE id=?", (folder_id,)
            ).fetchone()
            rows = [dict(r) for r in conn.execute(
                "SELECT id, title, type, date_start, date_end, time, note "
                "FROM announcement_schedule_events WHERE folder_id=? "
                "ORDER BY date_start, COALESCE(time,''), id",
                (folder_id,),
            )]
        finally:
            conn.close()

        if folder_row is None:
            self._send_json({"error": "folder not found"}, status=404)
            return

        events = []
        for r in rows:
            try:
                d_start = date.fromisoformat(r["date_start"])
            except (TypeError, ValueError):
                continue
            d_end = None
            if r.get("date_end"):
                try:
                    d_end = date.fromisoformat(r["date_end"])
                except ValueError:
                    d_end = None
            type_label = self._SCHEDULE_TYPE_LABEL.get(r["type"], "일정")
            time_s = (r.get("time") or "").strip()
            desc_parts = [f"분류: {type_label}"]
            if time_s:
                desc_parts.append(f"시간: {time_s}")
            if r.get("note"):
                desc_parts.append(str(r["note"]))
            desc_parts.append(f"출처 폴더: {folder_row['name']}")
            events.append({
                "uid": f"sched-{r['id']}-{r['date_start']}@startup-consulting.local",
                "title": f"[{type_label}] {r['title']}",
                "description": "\n".join(desc_parts),
                "date": d_start,
                "date_end": d_end,
            })

        cal_name = f"{folder_row['name']} 일정 ({len(events)}건)"
        ics = build_calendar(events, cal_name=cal_name)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in folder_row["name"])
        self._send_ics(ics, filename=f"{safe or 'folder'}-schedule.ics")

    def _handle_schedule_all(self) -> None:
        """캘린더 탭용: 모든 폴더의 일정을 folder 메타와 함께 반환."""
        if not DB_PATH.exists():
            self._send_json({"events": [], "folders": []})
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            events = [dict(r) for r in conn.execute(
                "SELECT e.id, e.folder_id, e.file_hash, e.title, e.type, "
                "       e.date_start, e.date_end, e.time, e.note, e.source_page, "
                "       f.name AS folder_name "
                "FROM announcement_schedule_events e "
                "JOIN attachment_folders f ON f.id = e.folder_id "
                "ORDER BY e.date_start, COALESCE(e.time,''), e.id"
            )]
            folders = [dict(r) for r in conn.execute(
                "SELECT id, name FROM attachment_folders ORDER BY id"
            )]
        finally:
            conn.close()
        self._send_json({"events": events, "folders": folders})

    def _handle_schedule_ics_selection(self) -> None:
        """선택한 event_id 들만 ICS 로 묶어서 다운로드."""
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        event_ids = data.get("event_ids") or []
        if not isinstance(event_ids, list) or not event_ids:
            self._send_json({"error": "event_ids required"}, status=400)
            return
        try:
            event_ids = [int(x) for x in event_ids]
        except (TypeError, ValueError):
            self._send_json({"error": "event_ids must be integers"}, status=400)
            return

        placeholders = ",".join("?" * len(event_ids))
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(
                f"SELECT e.id, e.title, e.type, e.date_start, e.date_end, e.time, e.note, "
                f"       f.name AS folder_name "
                f"FROM announcement_schedule_events e "
                f"JOIN attachment_folders f ON f.id = e.folder_id "
                f"WHERE e.id IN ({placeholders}) "
                f"ORDER BY e.date_start, COALESCE(e.time,''), e.id",
                event_ids,
            )]
        finally:
            conn.close()

        if not rows:
            self._send_json({"error": "선택한 일정을 찾을 수 없음"}, status=404)
            return

        events = []
        for r in rows:
            try:
                d_start = date.fromisoformat(r["date_start"])
            except (TypeError, ValueError):
                continue
            d_end = None
            if r.get("date_end"):
                try:
                    d_end = date.fromisoformat(r["date_end"])
                except ValueError:
                    d_end = None
            type_label = self._SCHEDULE_TYPE_LABEL.get(r["type"], "일정")
            time_s = (r.get("time") or "").strip()
            desc_parts = [f"분류: {type_label}", f"공고: {r['folder_name']}"]
            if time_s:
                desc_parts.append(f"시간: {time_s}")
            if r.get("note"):
                desc_parts.append(str(r["note"]))
            events.append({
                "uid": f"sched-{r['id']}-{r['date_start']}@startup-consulting.local",
                "title": f"[{type_label}] {r['title']}",
                "description": "\n".join(desc_parts),
                "date": d_start,
                "date_end": d_end,
            })

        cal_name = f"공고 일정 선택분 ({len(events)}건)"
        ics = build_calendar(events, cal_name=cal_name)
        self._send_ics(ics, filename="selected-schedule.ics")

    def _handle_schedule_delete(self, event_id_str: str) -> None:
        try:
            event_id = int(event_id_str)
        except ValueError:
            self._send_json({"error": "invalid event id"}, status=400)
            return
        if not DB_PATH.exists():
            self._send_json({"error": "db not found"}, status=500)
            return
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("DELETE FROM announcement_schedule_events WHERE id=?", (event_id,))
            conn.commit()
        finally:
            conn.close()
        self._send_json({"deleted_event": event_id})

    def _handle_attachment_get(self, file_hash: str) -> None:
        from planner.analyzer.storage import load_analysis
        if not file_hash:
            self._send_json({"error": "missing file_hash"}, status=400)
            return
        analysis = load_analysis(file_hash)
        if analysis is None:
            self._send_json({"error": "not found"}, status=404)
            return
        self._send_json({"file_hash": file_hash, "analysis": analysis})

    def _handle_folder_extract_schedule(self, folder_id_str: str) -> None:
        """폴더 내 모든 PDF 텍스트를 한 번에 LLM 에 보내 일정 추출(중복 제거)."""
        from planner.analyzer.analyzer import extract_schedule_consolidated
        from planner.analyzer.llm.base import LLMError
        from planner.analyzer.storage import load_extract, get_pdf_path
        from planner.analyzer.extractor import extract_pdf_text
        from planner.analyzer.storage import save_extract

        try:
            folder_id = int(folder_id_str)
        except ValueError:
            self._send_json({"error": "invalid folder id"}, status=400)
            return

        if not DB_PATH.exists():
            self._send_json({"error": "db not found"}, status=500)
            return

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT file_hash, original_filename FROM uploaded_attachments "
                "WHERE folder_id=? ORDER BY uploaded_at",
                (folder_id,),
            )]
        finally:
            conn.close()

        if not rows:
            self._send_json({"error": "이 폴더에 파일이 없습니다"}, status=400)
            return

        # 각 파일의 추출 텍스트 수집. 없으면 PDF 에서 다시 뽑음.
        files: list[tuple[str, str]] = []
        for r in rows:
            h = r["file_hash"]
            text = load_extract(h)
            if text is None:
                pdf_path = get_pdf_path(h)
                if pdf_path.exists():
                    try:
                        doc = extract_pdf_text(pdf_path.read_bytes())
                        save_extract(h, doc)
                        text = doc.full_text
                    except Exception:  # noqa: BLE001
                        continue
            if text:
                files.append((r["original_filename"] or h, text))

        if not files:
            self._send_json({"error": "추출 가능한 텍스트가 없습니다"}, status=400)
            return

        try:
            result = extract_schedule_consolidated(files)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"일정 추출 실패: {e}"}, status=500)
            return

        self._send_json({
            "folder_id": folder_id,
            "file_count": len(files),
            "items": result["items"],
            "extraction_note": result["extraction_note"],
            "elapsed_seconds": result["elapsed_seconds"],
        })

    def _handle_attachment_reanalyze(self, file_hash: str) -> None:
        """기존에 저장된 PDF 를 캐시 무시하고 다시 LLM 분석."""
        from planner.analyzer.analyzer import analyze_pdf
        from planner.analyzer.llm.base import LLMError
        from planner.analyzer.storage import get_pdf_path

        if not file_hash:
            self._send_json({"error": "missing file_hash"}, status=400)
            return
        pdf_path = get_pdf_path(file_hash)
        if not pdf_path.exists():
            self._send_json({"error": "원본 PDF 가 storage 에 없음"}, status=404)
            return

        # DB 에서 원본 파일명 찾기 (없으면 hash 사용)
        original_filename = None
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                row = conn.execute(
                    "SELECT original_filename FROM uploaded_attachments WHERE file_hash=?",
                    (file_hash,),
                ).fetchone()
                if row:
                    original_filename = row[0]
            finally:
                conn.close()

        try:
            analysis = analyze_pdf(
                pdf_path.read_bytes(),
                original_filename=original_filename,
                use_cache=False,
            )
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"재분석 실패: {e}"}, status=500)
            return

        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute(
                    "UPDATE uploaded_attachments SET status='done', analyzed_at=CURRENT_TIMESTAMP "
                    "WHERE file_hash=?",
                    (file_hash,),
                )
                conn.commit()
            finally:
                conn.close()

        self._send_json({"file_hash": file_hash, "analysis": analysis})

    def _handle_attachment_delete(self, file_hash: str) -> None:
        from planner.analyzer.storage import delete_attachment
        if not file_hash:
            self._send_json({"error": "missing file_hash"}, status=400)
            return
        delete_attachment(file_hash)
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute("DELETE FROM uploaded_attachments WHERE file_hash=?", (file_hash,))
                conn.commit()
            finally:
                conn.close()
        self._send_json({"deleted": file_hash})

    def log_message(self, format: str, *args) -> None:  # 콘솔 조용히
        return


def main(host: str = "127.0.0.1", port: int = 8765) -> None:
    import db as _db
    _db.init_db()
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
