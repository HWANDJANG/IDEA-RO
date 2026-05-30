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

from planner.auth import (
    COOKIE_NAME,
    GOOGLE_CALENDAR_SCOPE,
    delete_google_tokens,
    delete_naver_tokens,
    exchange_code_for_token,
    exchange_google_code_for_token,
    exchange_naver_code_for_token,
    fetch_google_user_info,
    fetch_kakao_user_info,
    fetch_naver_user_info,
    get_valid_google_access_token,
    get_valid_naver_access_token,
    google_authorize_url,
    has_calendar_scope,
    has_naver_calendar,
    kakao_authorize_url,
    load_user,
    make_clear_cookie_header,
    make_set_cookie_header,
    naver_authorize_url,
    parse_cookies,
    save_google_calendar_tokens,
    save_naver_tokens,
    upsert_user_from_google,
    upsert_user_from_kakao,
    upsert_user_from_naver,
    verify_session_cookie,
)
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
from planner.matcher import match_announcement, compute_profile_fit, classify_announcement_type
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
    # ─── 인증 컨텍스트 ────────────────────────────────────────────────
    def _current_user_id(self) -> int | None:
        """쿠키에서 user_id 추출. 안 로그인 시 None."""
        cookies = parse_cookies(self.headers.get("Cookie"))
        return verify_session_cookie(cookies.get(COOKIE_NAME))

    def _current_user(self) -> dict | None:
        uid = self._current_user_id()
        if uid is None:
            return None
        return load_user(uid)

    def _require_user_id(self) -> int | None:
        """로그인 필수 엔드포인트용. 미인증 시 401 응답 후 None 반환.

        사용 패턴:
            user_id = self._require_user_id()
            if user_id is None:
                return  # 401 already sent
        """
        uid = self._current_user_id()
        if uid is None:
            self._send_json({"error": "로그인이 필요합니다", "code": "AUTH_REQUIRED"}, status=401)
            return None
        return uid

    def _user_owns_folder(self, conn: sqlite3.Connection, folder_id: int, user_id: int) -> bool:
        row = conn.execute(
            "SELECT 1 FROM attachment_folders WHERE id=? AND user_id=?",
            (folder_id, user_id),
        ).fetchone()
        return row is not None

    def _send_redirect(self, location: str, set_cookie: str | None = None, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, payload, status: int = 200, set_cookie: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
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
        elif path == "/dashboard":
            self._send_file(STATIC_DIR / "dashboard.html", "text/html; charset=utf-8")
        elif path == "/onboarding":
            self._send_file(STATIC_DIR / "onboarding.html", "text/html; charset=utf-8")
        elif path == "/ideas":
            self._send_file(STATIC_DIR / "ideas.html", "text/html; charset=utf-8")
        elif path == "/privacy":
            self._send_file(STATIC_DIR / "privacy.html", "text/html; charset=utf-8")
        elif path == "/terms":
            self._send_file(STATIC_DIR / "terms.html", "text/html; charset=utf-8")
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
        elif path == "/api/plan":
            self._handle_plan(parsed.query)
        elif path.startswith("/api/announcements/") and path.endswith("/auto-attachments"):
            ann_id_str = path[len("/api/announcements/"):-len("/auto-attachments")]
            self._handle_auto_attachments_get(ann_id_str)
        elif path == "/api/auth/me":
            self._handle_auth_me()
        elif path == "/api/profile":
            self._handle_profile_get()
        elif path == "/api/my-docs":
            self._handle_my_docs_list()
        elif path == "/api/auth/kakao/login":
            self._handle_kakao_login_redirect()
        elif path == "/api/auth/kakao/callback":
            self._handle_kakao_callback(parsed.query)
        elif path == "/api/auth/google/login":
            self._handle_google_login_redirect()
        elif path == "/api/auth/google/callback":
            self._handle_google_callback(parsed.query)
        elif path == "/api/auth/google/calendar/connect":
            self._handle_google_calendar_connect()
        elif path == "/api/calendar/google/status":
            self._handle_google_calendar_status()
        elif path == "/api/auth/naver/login":
            self._handle_naver_login_redirect()
        elif path == "/api/auth/naver/callback":
            self._handle_naver_callback(parsed.query)
        elif path == "/api/calendar/naver/status":
            self._handle_naver_calendar_status()
        elif not path.startswith("/api/"):
            self._try_serve_static(path)
        else:
            self._send_json({"error": "not found"}, status=404)

    _STATIC_CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css":  "text/css; charset=utf-8",
        ".js":   "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg":  "image/svg+xml",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif":  "image/gif",
        ".ico":  "image/x-icon",
        ".webp": "image/webp",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }

    def _try_serve_static(self, url_path: str) -> None:
        """public/ 아래 파일을 안전하게 서빙. 디렉터리 트래버설 차단."""
        relative = url_path.lstrip("/")
        if not relative:
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            target = (STATIC_DIR / relative).resolve()
            static_root = STATIC_DIR.resolve()
        except (OSError, ValueError):
            self._send_json({"error": "not found"}, status=404)
            return
        if static_root not in target.parents and target != static_root:
            self._send_json({"error": "not found"}, status=404)
            return
        if not target.is_file():
            self._send_json({"error": "not found"}, status=404)
            return
        ctype = self._STATIC_CONTENT_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        self._send_file(target, ctype)

    def _handle_announcements_list(self, query: str) -> None:
        qs = parse_qs(query)
        source = (qs.get("source") or [None])[0]
        include_all = (qs.get("all") or ["0"])[0] in ("1", "true", "yes")
        try:
            limit = max(1, min(int((qs.get("limit") or ["50"])[0]), 500))
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
                "       a.detail_url, a.content_text, a.raw_meta, "
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
            raw_meta_json = r.get("raw_meta")
            raw_meta_obj = None
            if raw_meta_json:
                try:
                    raw_meta_obj = json.loads(raw_meta_json)
                except (json.JSONDecodeError, TypeError):
                    raw_meta_obj = None
            type_info = classify_announcement_type(
                raw_meta_obj, r.get("source_code"), r.get("title")
            )
            compact.append({
                **{k: v for k, v in r.items() if k not in ("content_text", "raw_meta")},
                "content_preview": content[:300],
                "content_length": len(content),
                "type": type_info,
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
        if path == "/api/plan/narrative":
            self._handle_plan_narrative()
            return
        if path == "/api/plan/guide":
            self._handle_plan_guide()
            return
        if path.startswith("/api/announcements/") and path.endswith("/auto-fetch"):
            ann_id_str = path[len("/api/announcements/"):-len("/auto-fetch")]
            self._handle_auto_fetch_post(ann_id_str)
            return
        if path == "/api/upload":
            self._handle_upload()
            return
        if path == "/api/attachments/scan-url":
            self._handle_scan_url()
            return
        if path == "/api/attachments/import-url":
            self._handle_import_url()
            return
        if path == "/api/folders":
            self._handle_folder_create()
            return
        if path == "/api/auth/logout":
            self._handle_logout()
            return
        if path == "/api/profile":
            self._handle_profile_save()
            return
        if path == "/api/my-docs":
            self._handle_my_docs_save()
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
        if path.startswith("/api/folders/") and path.endswith("/ask"):
            folder_id_str = path[len("/api/folders/"):-len("/ask")]
            self._handle_folder_ask(folder_id_str)
            return
        if path == "/api/docs/scan":
            self._handle_docs_scan()
            return
        if path == "/api/compare/initial":
            self._handle_compare_initial()
            return
        if path == "/api/compare/chat":
            self._handle_compare_chat()
            return
        if path == "/api/calendar/google/insert":
            self._handle_google_calendar_insert()
            return
        if path == "/api/calendar/google/disconnect":
            self._handle_google_calendar_disconnect()
            return
        if path == "/api/calendar/naver/insert":
            self._handle_naver_calendar_insert()
            return
        if path == "/api/calendar/naver/disconnect":
            self._handle_naver_calendar_disconnect()
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

        raw_meta_json: str | None = None
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
                    "SELECT id, title, end_date, content_text, raw_meta FROM announcements WHERE id=?",
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
            raw_meta_json = row["raw_meta"]
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

        # 프로필 자격 매칭 — 로그인된 사용자의 프로필 + 공고 raw_meta 비교
        profile = self._load_profile_for_user()
        raw_meta = None
        if raw_meta_json:
            try:
                raw_meta = json.loads(raw_meta_json)
            except (json.JSONDecodeError, TypeError):
                raw_meta = None
        result["profile_fit"] = compute_profile_fit(profile, raw_meta)
        self._send_json(result)

    def _handle_plan(self, query: str) -> None:
        """사용자 맞춤 추천 + 액션 플랜 (Top N 공고 + 부족 서류 + 발급 태스크).

        Query params:
          top_n     : 1~20, default 5
          w_amount  : 0~1, default 0.5 (지원금 규모 중요도)
          w_effort  : 0~1, default 0.3 (노력 회피)
          w_urgency : 0~1, default 0.5 (마감 임박 우선)
        """
        from planner.planner import compose_action_plan
        user_id = self._require_user_id()
        if user_id is None:
            return
        qs = parse_qs(query)

        def _f(key: str, default: float) -> float:
            try:
                return max(0.0, min(1.0, float((qs.get(key) or [str(default)])[0])))
            except ValueError:
                return default

        try:
            top_n = max(1, min(int((qs.get("top_n") or ["5"])[0]), 20))
        except ValueError:
            top_n = 5
        weights = {
            "amount":  _f("w_amount",  0.5),
            "effort":  _f("w_effort",  0.3),
            "urgency": _f("w_urgency", 0.5),
        }
        try:
            plan = compose_action_plan(user_id, top_n=top_n, weights=weights)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"plan 생성 실패: {e}"}, status=500)
            return
        self._send_json(plan)

    def _handle_plan_narrative(self) -> None:
        """추천 카드들에 대해 LLM 한 줄 narrative 생성. 캐시 사용.

        Body: { "plan": {... compose_action_plan 응답 그대로 ...} }
        Response: { "narratives": {ann_id_str: "...", ...}, "cached": bool, "count": int }
        """
        from planner.planner import generate_recommendation_narratives
        from planner.analyzer.llm.base import LLMError

        user_id = self._require_user_id()
        if user_id is None:
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        plan = data.get("plan") or {}
        if not plan.get("recommendations"):
            self._send_json({"narratives": {}, "cached": False, "count": 0})
            return
        try:
            result = generate_recommendation_narratives(user_id, plan)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"narrative 생성 실패: {e}"}, status=500)
            return
        self._send_json(result)

    def _handle_plan_guide(self) -> None:
        """담은 공고들 기반 시간 구간별 액션 가이드.

        Body: { "plan": {...}, "picked_ids": [1, 2, ...] }
        Response: { "sections": [...], "key_warning": "...", "cached": bool, "picked_count": int }
        """
        from planner.planner import generate_action_guide
        from planner.analyzer.llm.base import LLMError

        user_id = self._require_user_id()
        if user_id is None:
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        plan = data.get("plan") or {}
        picked_ids = data.get("picked_ids") or []
        if not isinstance(picked_ids, list):
            self._send_json({"error": "picked_ids must be array"}, status=400)
            return
        try:
            picked_ids_int = [int(x) for x in picked_ids]
        except (TypeError, ValueError):
            self._send_json({"error": "picked_ids must be integers"}, status=400)
            return
        try:
            result = generate_action_guide(user_id, plan, picked_ids_int)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"가이드 생성 실패: {e}"}, status=500)
            return
        self._send_json(result)

    # ─── Step 4: 공고 페이지 자동 fetch + 분석 ────────────────────────
    def _handle_auto_attachments_get(self, ann_id_str: str) -> None:
        """공고에 대해 이미 자동 수집/분석된 첨부 목록 조회 (트리거 X, 캐시만).

        GET /api/announcements/{ann_id}/auto-attachments
        Response: { "announcement_id": int, "attachments": [...], "count": int }
        """
        from planner.auto_fetcher import list_auto_attachments

        # 로그인 안 해도 조회는 허용 (공고는 public)
        try:
            ann_id = int(ann_id_str)
        except ValueError:
            self._send_json({"error": "ann_id must be integer"}, status=400)
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            attachments = list_auto_attachments(conn, ann_id, include_analysis=True)
        finally:
            conn.close()
        self._send_json({
            "announcement_id": ann_id,
            "attachments":     attachments,
            "count":           len(attachments),
        })

    def _handle_auto_fetch_post(self, ann_id_str: str) -> None:
        """공고 페이지에서 첨부 자동 fetch + 분석 트리거 (동기).

        POST /api/announcements/{ann_id}/auto-fetch
        Body (선택): { "force": bool, "max_files": int }
        Response: fetch_and_analyze_announcement 의 dict 그대로
        """
        from planner.auto_fetcher import fetch_and_analyze_announcement

        user_id = self._require_user_id()
        if user_id is None:
            return
        try:
            ann_id = int(ann_id_str)
        except ValueError:
            self._send_json({"error": "ann_id must be integer"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {}
        force = bool(data.get("force"))
        try:
            max_files = int(data.get("max_files") or 10)
        except (TypeError, ValueError):
            max_files = 10
        max_files = max(1, min(max_files, 20))

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            try:
                result = fetch_and_analyze_announcement(
                    conn, ann_id, max_files=max_files, force=force,
                )
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"auto-fetch 실패: {e}"}, status=500)
                return
        finally:
            conn.close()
        self._send_json(result)

    def _load_profile_for_user(self) -> dict | None:
        """현재 세션 사용자의 프로필을 dict 로 반환. 비로그인 시 None."""
        user_id = self._current_user_id()
        if user_id is None:
            return None
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f"SELECT {', '.join(self._PROFILE_FIELDS)} FROM user_profiles WHERE user_id=?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

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
        from planner.analyzer.analyzer import analyze_attachment
        from planner.analyzer.llm.base import LLMError

        user_id = self._require_user_id()
        if user_id is None: return

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
        name_lower = filename.lower()
        allowed_exts = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".hwpx", ".hwp")
        if not name_lower.endswith(allowed_exts):
            self._send_json({
                "error": "PDF / JPG / PNG / WEBP / HWPX / HWP 파일만 지원합니다",
            }, status=415)
            return
        # 클라이언트가 보내준 MIME (multipart 의 Content-Type) — 없으면 None 으로 dispatcher 에 위임
        file_mime = getattr(file_field, "content_type", None)

        announcement_id = (fields.get("announcement_id").data.decode("utf-8")
                           if "announcement_id" in fields else None) or None
        folder_id_raw = (fields.get("folder_id").data.decode("utf-8")
                         if "folder_id" in fields else None)
        try:
            folder_id = int(folder_id_raw) if folder_id_raw else None
        except ValueError:
            self._send_json({"error": "folder_id must be int"}, status=400)
            return

        # folder_id 가 비어있으면 user 의 '기본' 폴더 생성/조회
        conn = sqlite3.connect(DB_PATH)
        try:
            if folder_id is None:
                import db as _db
                folder_id = _db.ensure_default_folder_for_user(conn, user_id)
                conn.commit()
            else:
                # 지정한 폴더가 본인 소유인지 검증
                if not self._user_owns_folder(conn, folder_id, user_id):
                    self._send_json({"error": "folder not yours"}, status=403)
                    return
        finally:
            conn.close()

        # 분석 실행 (캐시 히트 시 즉시 반환됨). 확장자/MIME 에 따라 PDF/이미지로 자동 dispatch.
        try:
            analysis = analyze_attachment(
                file_field.data,
                original_filename=filename,
                mime_type=file_mime,
            )
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001 — 사용자에게 메시지를 보여주기 위함
            self._send_json({"error": f"분석 실패: {e}"}, status=500)
            return

        # DB 에 기록. (file_hash, user_id) 복합 UNIQUE 이므로 다른 사용자는 별도 row.
        # 같은 사용자가 동일 PDF 재업로드 시에는 ON CONFLICT 로 폴더/이름만 갱신.
        file_hash = analysis["file_hash"]
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute(
                    "INSERT INTO uploaded_attachments(file_hash, original_filename, announcement_id, folder_id, user_id, status, analyzed_at) "
                    "VALUES (?,?,?,?,?, 'done', CURRENT_TIMESTAMP) "
                    "ON CONFLICT(file_hash, user_id) DO UPDATE SET "
                    "  original_filename=excluded.original_filename, "
                    "  announcement_id=COALESCE(excluded.announcement_id, uploaded_attachments.announcement_id), "
                    "  folder_id=COALESCE(excluded.folder_id, uploaded_attachments.folder_id), "
                    "  status='done', analyzed_at=CURRENT_TIMESTAMP",
                    (file_hash, filename, announcement_id, folder_id, user_id),
                )
                conn.commit()
            finally:
                conn.close()

        self._send_json({"file_hash": file_hash, "analysis": analysis, "folder_id": folder_id})

    def _handle_scan_url(self) -> None:
        """공고 페이지 URL → 첨부 파일 목록 (다운로드 안 함, 미리보기만)."""
        user_id = self._require_user_id()
        if user_id is None: return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            self._send_json({"error": "유효한 URL 이 필요합니다 (http:// 또는 https://)"}, status=400)
            return
        from planner.attach_fetcher import scan_attachments_from_url
        result = scan_attachments_from_url(url)
        self._send_json(result)

    def _handle_import_url(self) -> None:
        """첨부 URL 1건을 서버가 다운로드 → analyze_pdf → 폴더에 저장."""
        from planner.analyzer.analyzer import analyze_pdf
        from planner.analyzer.llm.base import LLMError
        from planner.attach_fetcher import download_to_bytes

        user_id = self._require_user_id()
        if user_id is None: return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        url = (data.get("url") or "").strip()
        filename = (data.get("filename") or "downloaded.pdf").strip()
        try:
            folder_id = int(data.get("folder_id"))
        except (TypeError, ValueError):
            self._send_json({"error": "folder_id (int) 가 필요합니다"}, status=400)
            return
        announcement_id = data.get("announcement_id")
        if not url.startswith(("http://", "https://")):
            self._send_json({"error": "유효한 URL 이 필요합니다"}, status=400)
            return
        if not filename.lower().endswith(".pdf"):
            self._send_json({"error": "PDF 파일만 분석 가능합니다 (.pdf)"}, status=415)
            return

        # 폴더 소유권 검증
        conn = sqlite3.connect(DB_PATH)
        try:
            if not self._user_owns_folder(conn, folder_id, user_id):
                self._send_json({"error": "folder not yours"}, status=403)
                return
        finally:
            conn.close()

        # 원격 다운로드
        try:
            pdf_bytes, content_type = download_to_bytes(url)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"다운로드 실패: {e}"}, status=502)
            return
        if not pdf_bytes:
            self._send_json({"error": "빈 파일"}, status=502)
            return
        # 간단 검증: PDF 매직 바이트
        if not pdf_bytes.startswith(b"%PDF"):
            self._send_json({
                "error": f"PDF 가 아닌 응답 (Content-Type: {content_type}). URL 이 PDF 직접 링크인지 확인해주세요."
            }, status=415)
            return

        # 분석 (캐시 히트 시 즉시 반환)
        try:
            analysis = analyze_pdf(pdf_bytes, original_filename=filename)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"분석 실패: {e}"}, status=500)
            return

        # DB 기록 (업로드와 동일 패턴)
        file_hash = analysis["file_hash"]
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute(
                    "INSERT INTO uploaded_attachments(file_hash, original_filename, announcement_id, folder_id, user_id, status, analyzed_at) "
                    "VALUES (?,?,?,?,?, 'done', CURRENT_TIMESTAMP) "
                    "ON CONFLICT(file_hash, user_id) DO UPDATE SET "
                    "  original_filename=excluded.original_filename, "
                    "  announcement_id=COALESCE(excluded.announcement_id, uploaded_attachments.announcement_id), "
                    "  folder_id=COALESCE(excluded.folder_id, uploaded_attachments.folder_id), "
                    "  status='done', analyzed_at=CURRENT_TIMESTAMP",
                    (file_hash, filename, announcement_id, folder_id, user_id),
                )
                conn.commit()
            finally:
                conn.close()

        self._send_json({
            "file_hash": file_hash,
            "filename": filename,
            "folder_id": folder_id,
            "size": len(pdf_bytes),
        })

    def _handle_attachments_list(self, query: str = "") -> None:
        user_id = self._require_user_id()
        if user_id is None: return
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
                if not self._user_owns_folder(conn, folder_id, user_id):
                    self._send_json({"attachments": []})
                    return
                cursor = conn.execute(
                    "SELECT id, file_hash, original_filename, announcement_id, folder_id, "
                    "       uploaded_at, analyzed_at, status "
                    "FROM uploaded_attachments WHERE folder_id=? AND user_id=? "
                    "ORDER BY uploaded_at DESC",
                    (folder_id, user_id),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, file_hash, original_filename, announcement_id, folder_id, "
                    "       uploaded_at, analyzed_at, status "
                    "FROM uploaded_attachments WHERE user_id=? ORDER BY uploaded_at DESC",
                    (user_id,),
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
        user_id = self._require_user_id()
        if user_id is None: return
        if not DB_PATH.exists():
            self._send_json({"folders": []})
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            # 처음 진입한 사용자에게 '기본' 폴더 자동 생성 (DB 헬퍼 사용)
            import db as _db
            _db.ensure_default_folder_for_user(conn, user_id)
            conn.commit()
            rows = [dict(r) for r in conn.execute(
                "SELECT f.id, f.name, f.created_at, f.announcement_id, "
                "  (SELECT COUNT(*) FROM uploaded_attachments u "
                "   WHERE u.folder_id = f.id AND u.user_id = ?) AS count "
                "FROM attachment_folders f WHERE f.user_id = ? ORDER BY f.id",
                (user_id, user_id),
            )]
        finally:
            conn.close()
        self._send_json({"folders": rows})

    def _handle_folder_create(self) -> None:
        user_id = self._require_user_id()
        if user_id is None: return
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
        ann_id_raw = data.get("announcement_id")
        try:
            ann_id = int(ann_id_raw) if ann_id_raw not in (None, "") else None
        except (TypeError, ValueError):
            self._send_json({"error": "announcement_id must be int"}, status=400)
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            # 같은 announcement 에 이미 폴더가 있으면 재사용 (사용자별)
            if ann_id is not None:
                existing = conn.execute(
                    "SELECT id, name, created_at, announcement_id "
                    "FROM attachment_folders WHERE user_id=? AND announcement_id=? LIMIT 1",
                    (user_id, ann_id),
                ).fetchone()
                if existing:
                    self._send_json({"folder": dict(existing), "reused": True})
                    return
            cur = conn.execute(
                "INSERT INTO attachment_folders(name, user_id, announcement_id) VALUES (?, ?, ?)",
                (name, user_id, ann_id),
            )
            conn.commit()
            new_id = cur.lastrowid
            row = conn.execute(
                "SELECT id, name, created_at, announcement_id FROM attachment_folders WHERE id=?",
                (new_id,),
            ).fetchone()
        finally:
            conn.close()
        self._send_json({"folder": dict(row) if row else {"id": new_id, "name": name}})

    def _handle_folder_rename(self, folder_id_str: str) -> None:
        user_id = self._require_user_id()
        if user_id is None: return
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
                "UPDATE attachment_folders SET name=? WHERE id=? AND user_id=?",
                (name, folder_id, user_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                self._send_json({"error": "folder not found or not yours"}, status=404)
                return
        finally:
            conn.close()
        self._send_json({"folder": {"id": folder_id, "name": name}})

    def _handle_folder_delete(self, folder_id_str: str) -> None:
        from planner.analyzer.storage import delete_attachment as _delete_storage
        user_id = self._require_user_id()
        if user_id is None: return
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
            if not self._user_owns_folder(conn, folder_id, user_id):
                self._send_json({"error": "folder not found or not yours"}, status=404)
                return
            hashes = [r[0] for r in conn.execute(
                "SELECT file_hash FROM uploaded_attachments WHERE folder_id=? AND user_id=?",
                (folder_id, user_id),
            )]
            conn.execute(
                "DELETE FROM announcement_schedule_events WHERE folder_id=? AND user_id=?",
                (folder_id, user_id),
            )
            conn.execute(
                "DELETE FROM uploaded_attachments WHERE folder_id=? AND user_id=?",
                (folder_id, user_id),
            )
            conn.execute(
                "DELETE FROM attachment_folders WHERE id=? AND user_id=?",
                (folder_id, user_id),
            )
            conn.commit()
        finally:
            conn.close()
        # 스토리지 파일도 정리
        for h in hashes:
            _delete_storage(h)
        self._send_json({"deleted_folder": folder_id, "deleted_files": len(hashes)})

    # ─── 폴더별 일정 ──────────────────────────────────────────────────────
    def _handle_schedule_list(self, folder_id_str: str) -> None:
        user_id = self._require_user_id()
        if user_id is None: return
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
            if not self._user_owns_folder(conn, folder_id, user_id):
                self._send_json({"events": []})
                return
            rows = [dict(r) for r in conn.execute(
                "SELECT id, folder_id, file_hash, title, type, date_start, date_end, "
                "       time, note, source_page, added_at "
                "FROM announcement_schedule_events WHERE folder_id=? AND user_id=? "
                "ORDER BY date_start, COALESCE(time,''), id",
                (folder_id, user_id),
            )]
        finally:
            conn.close()
        self._send_json({"events": rows})

    def _handle_schedule_add(self) -> None:
        user_id = self._require_user_id()
        if user_id is None: return
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
            if not self._user_owns_folder(conn, folder_id, user_id):
                self._send_json({"error": "folder not yours"}, status=403)
                return
            if replace_folder:
                cur = conn.execute(
                    "DELETE FROM announcement_schedule_events WHERE folder_id=? AND user_id=?",
                    (folder_id, user_id),
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
                    "(folder_id, user_id, file_hash, title, type, date_start, date_end, time, note, source_page) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        folder_id,
                        user_id,
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
        user_id = self._require_user_id()
        if user_id is None: return
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
                "SELECT name FROM attachment_folders WHERE id=? AND user_id=?",
                (folder_id, user_id),
            ).fetchone()
            if folder_row is None:
                self._send_json({"error": "folder not found or not yours"}, status=404)
                return
            rows = [dict(r) for r in conn.execute(
                "SELECT id, title, type, date_start, date_end, time, note "
                "FROM announcement_schedule_events WHERE folder_id=? AND user_id=? "
                "ORDER BY date_start, COALESCE(time,''), id",
                (folder_id, user_id),
            )]
        finally:
            conn.close()

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
        """캘린더 탭용: 모든 폴더의 일정을 folder 메타와 함께 반환. (사용자 본인 것만)"""
        user_id = self._require_user_id()
        if user_id is None: return
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
                "WHERE e.user_id = ? "
                "ORDER BY e.date_start, COALESCE(e.time,''), e.id",
                (user_id,),
            )]
            folders = [dict(r) for r in conn.execute(
                "SELECT id, name FROM attachment_folders WHERE user_id=? ORDER BY id",
                (user_id,),
            )]
        finally:
            conn.close()
        self._send_json({"events": events, "folders": folders})

    def _handle_schedule_ics_selection(self) -> None:
        """선택한 event_id 들만 ICS 로 묶어서 다운로드. (본인 일정만)"""
        user_id = self._require_user_id()
        if user_id is None: return
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
                f"WHERE e.id IN ({placeholders}) AND e.user_id = ? "
                f"ORDER BY e.date_start, COALESCE(e.time,''), e.id",
                event_ids + [user_id],
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
        user_id = self._require_user_id()
        if user_id is None: return
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
            cur = conn.execute(
                "DELETE FROM announcement_schedule_events WHERE id=? AND user_id=?",
                (event_id, user_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                self._send_json({"error": "not found or not yours"}, status=404)
                return
        finally:
            conn.close()
        self._send_json({"deleted_event": event_id})

    def _handle_attachment_get(self, file_hash: str) -> None:
        from planner.analyzer.storage import load_analysis
        user_id = self._require_user_id()
        if user_id is None: return
        if not file_hash:
            self._send_json({"error": "missing file_hash"}, status=400)
            return
        # 소유권 검증
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT 1 FROM uploaded_attachments WHERE file_hash=? AND user_id=?",
                (file_hash, user_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            self._send_json({"error": "not found"}, status=404)
            return
        analysis = load_analysis(file_hash)
        if analysis is None:
            self._send_json({"error": "not found"}, status=404)
            return
        self._send_json({"file_hash": file_hash, "analysis": analysis})

    def _handle_docs_scan(self) -> None:
        """발급내역 이미지 → Gemini Vision → 서류 목록."""
        from planner.analyzer.doc_scanner import scan_doc_image
        from planner.analyzer.llm.base import LLMError

        ct = self.headers.get("Content-Type", "")
        if not ct.lower().startswith("multipart/form-data"):
            self._send_json({"error": "Content-Type must be multipart/form-data"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._send_json({"error": "empty body"}, status=400)
            return
        if length > 20 * 1024 * 1024:  # 20MB 상한 (이미지)
            self._send_json({"error": "image too large (20MB limit)"}, status=413)
            return
        raw = self.rfile.read(length)
        try:
            fields = parse_multipart(ct, raw)
        except MultipartError as e:
            self._send_json({"error": f"bad multipart: {e}"}, status=400)
            return

        file_field = fields.get("image") or fields.get("file")
        if file_field is None or not file_field.data:
            self._send_json({"error": "missing 'image' field"}, status=400)
            return

        mime = (file_field.content_type or "image/jpeg").lower()
        if not mime.startswith("image/"):
            self._send_json({"error": "이미지 파일만 업로드 가능합니다"}, status=415)
            return
        # Gemini 가 명확히 지원하는 형식으로 정규화
        if mime not in ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"):
            # 임의의 image/* 는 image/jpeg 로 시도 (대부분 자동 인식)
            mime = "image/jpeg"

        try:
            result = scan_doc_image(file_field.data, mime_type=mime)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"이미지 분석 실패: {e}"}, status=500)
            return

        self._send_json(result)

    def _handle_folder_ask(self, folder_id_str: str) -> None:
        """폴더 내 모든 PDF 텍스트를 컨텍스트로 자유 Q&A."""
        from planner.analyzer.analyzer import ask_folder_question
        from planner.analyzer.llm.base import LLMError
        from planner.analyzer.storage import load_extract, get_pdf_path
        from planner.analyzer.extractor import extract_pdf_text
        from planner.analyzer.storage import save_extract

        user_id = self._require_user_id()
        if user_id is None: return
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

        question = (data.get("question") or "").strip()
        if not question:
            self._send_json({"error": "question required"}, status=400)
            return
        if len(question) > 1000:
            self._send_json({"error": "question too long (max 1000 chars)"}, status=400)
            return
        history = data.get("history") or []
        if not isinstance(history, list):
            history = []

        if not DB_PATH.exists():
            self._send_json({"error": "db not found"}, status=500)
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            if not self._user_owns_folder(conn, folder_id, user_id):
                self._send_json({"error": "folder not yours"}, status=403)
                return
            rows = [dict(r) for r in conn.execute(
                "SELECT file_hash, original_filename FROM uploaded_attachments "
                "WHERE folder_id=? AND user_id=? ORDER BY uploaded_at",
                (folder_id, user_id),
            )]
        finally:
            conn.close()

        if not rows:
            self._send_json({"error": "이 폴더에 파일이 없습니다"}, status=400)
            return

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
            result = ask_folder_question(files, question, history)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"질문 처리 실패: {e}"}, status=500)
            return

        self._send_json({
            "folder_id": folder_id,
            "question": question,
            "answer": result["answer"],
            "elapsed_seconds": result["elapsed_seconds"],
            "file_count": len(files),
        })

    # ─── 공고 다중 비교 (정밀 비교 + 후속 채팅) ──────────────────────────────
    # LLM 에 안전하게 보낼 프로필 필드 (개인정보 제외)
    _LLM_SAFE_PROFILE_FIELDS = (
        "company_name", "business_type", "establishment_date",
        "region", "industry", "industry_detail",
        "employee_count", "founding_type",
    )

    def _llm_safe_profile(self) -> dict:
        """현재 사용자의 프로필 중 LLM 컨텍스트에 넣어도 안전한 필드만 추출.
        비로그인이거나 프로필이 없으면 빈 dict 반환."""
        full = self._load_profile_for_user()
        if not full:
            return {}
        return {k: full[k] for k in self._LLM_SAFE_PROFILE_FIELDS
                if k in full and full[k] not in (None, "")}

    def _collect_compare_items(
        self,
        announcement_ids: list[int],
        user_id: int,
    ) -> list[dict]:
        """선택된 공고들의 메타 + 사용자가 첨부한 PDF의 **구조화 요약**을 모아서
        analyzer.compare_announcements 가 받을 형식으로 변환.

        PDF 전문 대신 analyze_pdf 결과(JSON)를 잔축 텍스트로 직렬화해 비용 10~30× 절감.
        analysis 가 없거나 구버전이면 즉시 analyze_pdf 로 재분석 (1회).
        """
        from planner.analyzer.analyzer import (
            analyze_pdf, format_analysis_summary, SCHEMA_VERSION,
        )
        from planner.analyzer.llm.base import LLMError
        from planner.analyzer.storage import load_analysis, get_pdf_path

        if not announcement_ids:
            return []
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            placeholders = ",".join(["?"] * len(announcement_ids))
            ann_rows = {r["id"]: dict(r) for r in conn.execute(
                f"SELECT a.id, a.title, a.department, a.end_date, a.raw_meta, "
                f"       s.code AS source_code, s.name AS source_name "
                f"FROM announcements a JOIN sources s ON s.id = a.source_id "
                f"WHERE a.id IN ({placeholders})",
                announcement_ids,
            )}
            # 공고별 첨부 PDF 조회 (해당 사용자의 폴더에서)
            attach_by_ann: dict[int, list[dict]] = {aid: [] for aid in announcement_ids}
            rows = conn.execute(
                f"SELECT u.file_hash, u.original_filename, f.announcement_id "
                f"FROM uploaded_attachments u "
                f"JOIN attachment_folders f ON f.id = u.folder_id "
                f"WHERE u.user_id=? AND f.announcement_id IN ({placeholders}) "
                f"ORDER BY u.uploaded_at",
                [user_id, *announcement_ids],
            )
            for r in rows:
                attach_by_ann.setdefault(r["announcement_id"], []).append(dict(r))
        finally:
            conn.close()

        items: list[dict] = []
        for i, aid in enumerate(announcement_ids):
            ann = ann_rows.get(aid)
            if not ann:
                continue
            files: list[tuple[str, str]] = []   # (filename, structured summary text)
            for at in attach_by_ann.get(aid, []):
                h = at["file_hash"]
                filename = at["original_filename"] or h
                analysis = load_analysis(h)
                # 구버전(스키마 미스매치) 또는 부재 → 재분석
                if analysis is None or analysis.get("schema_version") != SCHEMA_VERSION:
                    pdf_path = get_pdf_path(h)
                    if not pdf_path.exists():
                        continue
                    try:
                        analysis = analyze_pdf(pdf_path.read_bytes(), original_filename=filename)
                    except LLMError:
                        continue
                    except Exception:  # noqa: BLE001
                        continue
                summary = format_analysis_summary(analysis, source_name=filename)
                if summary:
                    files.append((filename, summary))
            # 자격 메타 요약 (있으면)
            elig_meta = ""
            raw_meta = ann.get("raw_meta")
            if raw_meta:
                try:
                    rm = json.loads(raw_meta)
                except (TypeError, ValueError, json.JSONDecodeError):
                    rm = {}
                llm_e = rm.get("llm_eligibility") or {}
                bits = []
                for key, label in (("biz_enyy", "업력"), ("supt_regin", "지역"), ("aply_trgt", "대상")):
                    v = rm.get(key) or llm_e.get(key)
                    if v:
                        bits.append(f"{label}={v}")
                if bits:
                    elig_meta = ", ".join(bits)
            items.append({
                "ann_id": aid,
                "label": chr(ord("A") + i),
                "title": ann.get("title") or "",
                "source": ann.get("source_name") or ann.get("source_code") or "",
                "department": ann.get("department") or "",
                "end_date": (ann.get("end_date") or "")[:10],
                "eligibility_meta": elig_meta,
                "files": files,
            })
        return items

    def _handle_compare_initial(self) -> None:
        """선택된 공고들 + 첨부 PDF + 프로필을 LLM 에 보내 측면별 비교 출력."""
        from planner.analyzer.analyzer import compare_announcements
        from planner.analyzer.llm.base import LLMError

        user_id = self._require_user_id()
        if user_id is None: return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return

        ann_ids_raw = data.get("announcement_ids") or []
        if not isinstance(ann_ids_raw, list) or len(ann_ids_raw) < 2:
            self._send_json({"error": "announcement_ids: 2개 이상 필요"}, status=400)
            return
        try:
            ann_ids = [int(x) for x in ann_ids_raw]
        except (TypeError, ValueError):
            self._send_json({"error": "announcement_ids 는 정수 리스트"}, status=400)
            return
        if len(ann_ids) > 4:
            self._send_json({"error": "최대 4개까지 비교 가능"}, status=400)
            return

        items = self._collect_compare_items(ann_ids, user_id)
        if len(items) < 2:
            self._send_json({"error": "비교 가능한 공고를 찾지 못했습니다"}, status=400)
            return

        with_files = [it for it in items if it["files"]]
        if not with_files:
            self._send_json({
                "error": "첨부 PDF 가 있는 공고가 없습니다. 매칭 카드에서 PDF를 먼저 첨부해주세요."
            }, status=400)
            return

        profile = self._llm_safe_profile()
        try:
            result = compare_announcements(items, profile=profile)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"비교 생성 실패: {e}"}, status=500)
            return

        self._send_json({
            "answer": result["answer"],
            "elapsed_seconds": result["elapsed_seconds"],
            "cache_hit": result.get("cache_hit", False),
            "profile_used": bool(profile),
            "items": [
                {
                    "ann_id": it["ann_id"],
                    "label": it["label"],
                    "title": it["title"],
                    "pdf_count": len(it["files"]),
                }
                for it in items
            ],
        })

    def _handle_compare_chat(self) -> None:
        """비교 모달 채팅. 같은 공고/PDF 컨텍스트에 사용자 후속 질문을 더해 답."""
        from planner.analyzer.analyzer import chat_compare
        from planner.analyzer.llm.base import LLMError

        user_id = self._require_user_id()
        if user_id is None: return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return

        ann_ids_raw = data.get("announcement_ids") or []
        if not isinstance(ann_ids_raw, list) or len(ann_ids_raw) < 2:
            self._send_json({"error": "announcement_ids: 2개 이상 필요"}, status=400)
            return
        try:
            ann_ids = [int(x) for x in ann_ids_raw]
        except (TypeError, ValueError):
            self._send_json({"error": "announcement_ids 는 정수 리스트"}, status=400)
            return

        question = (data.get("question") or "").strip()
        if not question:
            self._send_json({"error": "question 필수"}, status=400)
            return
        if len(question) > 2000:
            self._send_json({"error": "question 너무 김 (max 2000)"}, status=400)
            return
        history = data.get("history") or []
        if not isinstance(history, list):
            history = []

        items = self._collect_compare_items(ann_ids, user_id)
        if len(items) < 2:
            self._send_json({"error": "비교 가능한 공고를 찾지 못했습니다"}, status=400)
            return
        if not any(it["files"] for it in items):
            self._send_json({"error": "첨부 PDF 가 없습니다"}, status=400)
            return

        profile = self._llm_safe_profile()
        try:
            result = chat_compare(items, question, history, profile=profile)
        except LLMError as e:
            self._send_json({"error": f"LLM 호출 실패: {e}"}, status=502)
            return
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"채팅 실패: {e}"}, status=500)
            return

        self._send_json({
            "answer": result["answer"],
            "elapsed_seconds": result["elapsed_seconds"],
            "cache_hit": result.get("cache_hit", False),
        })

    def _handle_folder_extract_schedule(self, folder_id_str: str) -> None:
        """폴더 내 모든 PDF 의 #1 분석 결과(JSON)에서 일정 항목들을 Python merge + dedup.

        LLM 호출 없음 (이전: PDF 합본을 LLM 에 보냈음). 분석이 없는 PDF 만 fallback 으로 LLM 1회.
        같은 폴더+같은 PDF 조합이면 derived 캐시 재사용.
        """
        from planner.analyzer.analyzer import analyze_pdf, merge_schedule_items
        from planner.analyzer.llm.base import LLMError
        from planner.analyzer.storage import (
            get_pdf_path, load_analysis, load_derived, save_derived,
        )
        import hashlib

        user_id = self._require_user_id()
        if user_id is None: return
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
            if not self._user_owns_folder(conn, folder_id, user_id):
                self._send_json({"error": "folder not yours"}, status=403)
                return
            rows = [dict(r) for r in conn.execute(
                "SELECT file_hash, original_filename FROM uploaded_attachments "
                "WHERE folder_id=? AND user_id=? ORDER BY uploaded_at",
                (folder_id, user_id),
            )]
        finally:
            conn.close()

        if not rows:
            self._send_json({"error": "이 폴더에 파일이 없습니다"}, status=400)
            return

        # derived 캐시 키: 폴더 ID + 정렬된 PDF hash 들
        sorted_hashes = sorted(r["file_hash"] for r in rows)
        cache_key = "schedule_" + hashlib.sha256(
            (str(folder_id) + ":" + ",".join(sorted_hashes)).encode("utf-8")
        ).hexdigest()[:24]
        cached = load_derived(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            self._send_json(cached)
            return

        # 각 PDF 의 분석 JSON 에서 schedule items 수집. 없는 경우만 fallback LLM 호출.
        per_file: list[tuple[str, list[dict]]] = []
        llm_fallback_calls = 0
        for r in rows:
            h = r["file_hash"]
            filename = r["original_filename"] or h
            analysis = load_analysis(h)
            if analysis is None or "schedule" not in analysis:
                # fallback — PDF 가 있으면 즉시 분석
                pdf_path = get_pdf_path(h)
                if not pdf_path.exists():
                    continue
                try:
                    analysis = analyze_pdf(pdf_path.read_bytes(), original_filename=filename)
                    llm_fallback_calls += 1
                except LLMError:
                    continue
                except Exception:  # noqa: BLE001
                    continue
            items = (analysis.get("schedule") or {}).get("items") or []
            per_file.append((filename, items))

        if not per_file:
            self._send_json({"error": "분석 가능한 파일이 없습니다"}, status=400)
            return

        result = merge_schedule_items(per_file)
        payload = {
            "folder_id": folder_id,
            "file_count": len(per_file),
            "items": result["items"],
            "extraction_note": result["extraction_note"],
            "elapsed_seconds": result["elapsed_seconds"],
            "llm_calls": llm_fallback_calls,
            "cache_hit": False,
        }
        save_derived(cache_key, payload)
        self._send_json(payload)

    def _handle_attachment_reanalyze(self, file_hash: str) -> None:
        """기존에 저장된 PDF 를 캐시 무시하고 다시 LLM 분석."""
        from planner.analyzer.analyzer import analyze_pdf
        from planner.analyzer.llm.base import LLMError
        from planner.analyzer.storage import get_pdf_path

        user_id = self._require_user_id()
        if user_id is None: return
        if not file_hash:
            self._send_json({"error": "missing file_hash"}, status=400)
            return
        # 소유권 검증
        original_filename = None
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                row = conn.execute(
                    "SELECT original_filename FROM uploaded_attachments "
                    "WHERE file_hash=? AND user_id=?",
                    (file_hash, user_id),
                ).fetchone()
                if row is None:
                    self._send_json({"error": "not found or not yours"}, status=404)
                    return
                original_filename = row[0]
            finally:
                conn.close()

        pdf_path = get_pdf_path(file_hash)
        if not pdf_path.exists():
            self._send_json({"error": "원본 PDF 가 storage 에 없음"}, status=404)
            return

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
                    "WHERE file_hash=? AND user_id=?",
                    (file_hash, user_id),
                )
                conn.commit()
            finally:
                conn.close()

        self._send_json({"file_hash": file_hash, "analysis": analysis})

    def _handle_attachment_delete(self, file_hash: str) -> None:
        from planner.analyzer.storage import delete_attachment
        user_id = self._require_user_id()
        if user_id is None: return
        if not file_hash:
            self._send_json({"error": "missing file_hash"}, status=400)
            return
        # 소유권 검증
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                row = conn.execute(
                    "SELECT 1 FROM uploaded_attachments WHERE file_hash=? AND user_id=?",
                    (file_hash, user_id),
                ).fetchone()
                if row is None:
                    self._send_json({"error": "not found or not yours"}, status=404)
                    return
            finally:
                conn.close()
        # 다른 사용자 row 가 같은 hash 로 없는지 확인. 없으면 스토리지 파일도 삭제.
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute(
                    "DELETE FROM uploaded_attachments WHERE file_hash=? AND user_id=?",
                    (file_hash, user_id),
                )
                conn.commit()
                # 다른 사용자가 같은 PDF 를 갖고있지 않은 경우만 storage 삭제
                still_used = conn.execute(
                    "SELECT 1 FROM uploaded_attachments WHERE file_hash=?",
                    (file_hash,),
                ).fetchone()
            finally:
                conn.close()
            if still_used is None:
                delete_attachment(file_hash)
        self._send_json({"deleted": file_hash})

    # ─── 인증 라우트 ──────────────────────────────────────────────────
    def _handle_auth_me(self) -> None:
        user = self._current_user()
        if user is None:
            self._send_json({"user": None})
            return
        # provider_uid 같은 민감 정보는 클라이언트에 노출하지 않음
        self._send_json({"user": {
            "id": user["id"],
            "nickname": user.get("nickname"),
            "email": user.get("email"),
            "profile_img": user.get("profile_img"),
        }})

    _SAFE_NEXT_PATHS = {"/", "/dashboard", "/onboarding"}

    def _safe_next(self, value: str | None) -> str:
        """state/next 로 들어온 경로가 우리 사이트 내부의 알려진 경로인지 검증."""
        if value and value in self._SAFE_NEXT_PATHS:
            return value
        return "/"

    def _handle_kakao_login_redirect(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        next_path = self._safe_next((qs.get("next") or [None])[0])
        try:
            url = kakao_authorize_url(state=next_path)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_redirect(url)

    def _handle_kakao_callback(self, query: str) -> None:
        qs = parse_qs(query)
        code = (qs.get("code") or [None])[0]
        err = (qs.get("error") or [None])[0]
        next_path = self._safe_next((qs.get("state") or [None])[0])
        if err:
            self._send_redirect(f"{next_path}?auth_error={err}")
            return
        if not code:
            self._send_redirect(f"{next_path}?auth_error=missing_code")
            return
        try:
            token = exchange_code_for_token(code)
            access = token.get("access_token")
            if not access:
                raise RuntimeError(f"카카오 토큰 응답에 access_token 없음: {token}")
            info = fetch_kakao_user_info(access)
            user_id = upsert_user_from_kakao(info)
        except Exception as e:  # noqa: BLE001
            print(f"[auth] kakao callback 실패: {e}")
            self._send_redirect(f"{next_path}?auth_error=kakao_failed")
            return

        cookie = make_set_cookie_header(user_id)
        self._send_redirect(next_path, set_cookie=cookie)

    def _handle_google_login_redirect(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        next_path = self._safe_next((qs.get("next") or [None])[0])
        try:
            url = google_authorize_url(state=next_path)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_redirect(url)

    def _handle_google_callback(self, query: str) -> None:
        qs = parse_qs(query)
        code = (qs.get("code") or [None])[0]
        err = (qs.get("error") or [None])[0]
        next_path = self._safe_next((qs.get("state") or [None])[0])
        if err:
            self._send_redirect(f"{next_path}?auth_error={err}")
            return
        if not code:
            self._send_redirect(f"{next_path}?auth_error=missing_code")
            return
        try:
            token = exchange_google_code_for_token(code)
            access = token.get("access_token")
            if not access:
                raise RuntimeError(f"Google 토큰 응답에 access_token 없음: {token}")
            info = fetch_google_user_info(access)
            user_id = upsert_user_from_google(info)
            # Calendar 스코프까지 동의된 경우 토큰 저장 (refresh_token 도 같이 있어야 영구 사용 가능)
            scopes_str = token.get("scope") or ""
            if GOOGLE_CALENDAR_SCOPE in scopes_str:
                save_google_calendar_tokens(
                    user_id,
                    refresh_token=token.get("refresh_token"),
                    access_token=access,
                    expires_in=int(token.get("expires_in") or 3600),
                    scopes=scopes_str,
                )
        except Exception as e:  # noqa: BLE001
            print(f"[auth] google callback 실패: {e}")
            self._send_redirect(f"{next_path}?auth_error=google_failed")
            return

        cookie = make_set_cookie_header(user_id)
        self._send_redirect(next_path, set_cookie=cookie)

    # ─── Google Calendar 연동 ─────────────────────────────────────────────
    def _handle_google_calendar_connect(self) -> None:
        """캘린더 권한 동의 화면으로 302. 로그인 안 됐어도 시작은 가능 (콜백에서 user upsert)."""
        qs = parse_qs(urlparse(self.path).query)
        next_path = self._safe_next((qs.get("next") or [None])[0])
        try:
            url = google_authorize_url(state=next_path, include_calendar=True)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_redirect(url)

    def _handle_google_calendar_status(self) -> None:
        """현재 사용자의 캘린더 연동 상태 조회."""
        user_id = self._current_user_id()
        if user_id is None:
            self._send_json({"connected": False, "auth_required": True})
            return
        connected = has_calendar_scope(user_id)
        self._send_json({"connected": connected, "auth_required": False})

    def _handle_google_calendar_disconnect(self) -> None:
        user_id = self._require_user_id()
        if user_id is None: return
        delete_google_tokens(user_id)
        self._send_json({"ok": True})

    def _handle_google_calendar_insert(self) -> None:
        """선택된 일정들을 사용자 Google Calendar 의 primary 캘린더에 일괄 추가.

        Body: {"events": [{summary, description?, date_start, date_end?, time?}, ...], "calendar_id?": "primary"}
        Returns: {"inserted": N, "failed": [...], "results": [{ann_event_id?, google_event_id, html_link}, ...]}
        """
        import urllib.error, urllib.request
        user_id = self._require_user_id()
        if user_id is None: return

        access = get_valid_google_access_token(user_id)
        if not access:
            self._send_json({
                "error": "Google Calendar 가 연동되지 않았거나 동의가 만료됐습니다",
                "code": "CALENDAR_NOT_CONNECTED",
            }, status=400)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return

        events_in = data.get("events") or []
        event_ids = data.get("event_ids") or []

        # event_ids 가 주어지면 DB 에서 조회해서 events_in 으로 변환 (캘린더 탭 호환)
        if event_ids and not events_in:
            if not isinstance(event_ids, list):
                self._send_json({"error": "event_ids 배열 필수"}, status=400)
                return
            try:
                ids = [int(x) for x in event_ids]
            except (TypeError, ValueError):
                self._send_json({"error": "event_ids 는 정수 리스트"}, status=400)
                return
            if not ids:
                self._send_json({"error": "event_ids 가 비어있음"}, status=400)
                return
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                placeholders = ",".join(["?"] * len(ids))
                rows = conn.execute(
                    f"SELECT e.id, e.title, e.type, e.date_start, e.date_end, e.time, "
                    f"       e.note, f.name AS folder_name "
                    f"FROM announcement_schedule_events e "
                    f"JOIN attachment_folders f ON f.id = e.folder_id "
                    f"WHERE e.id IN ({placeholders}) AND e.user_id=?",
                    [*ids, user_id],
                ).fetchall()
            finally:
                conn.close()
            events_in = []
            for r in rows:
                desc_bits = []
                if r["folder_name"]: desc_bits.append(f"공고: {r['folder_name']}")
                if r["type"]:        desc_bits.append(f"유형: {r['type']}")
                if r["note"]:        desc_bits.append(r["note"])
                events_in.append({
                    "id": r["id"],
                    "summary": r["title"],
                    "description": "\n".join(desc_bits),
                    "date_start": r["date_start"],
                    "date_end": r["date_end"],
                    "time": r["time"],
                })

        if not isinstance(events_in, list) or not events_in:
            self._send_json({"error": "events 또는 event_ids 필수"}, status=400)
            return
        if len(events_in) > 50:
            self._send_json({"error": "한 번에 최대 50개"}, status=400)
            return

        cal_id = (data.get("calendar_id") or "primary").strip() or "primary"
        # Google Calendar 가 한국 일정 처리 위해 timezone 명시 (없으면 사용자 캘린더 기본값)
        default_tz = "Asia/Seoul"

        results: list[dict] = []
        failed: list[dict] = []
        for ev in events_in:
            summary = (ev.get("summary") or "").strip()
            if not summary:
                failed.append({"event": ev, "error": "summary 필수"})
                continue
            ds = (ev.get("date_start") or "").strip()
            de = (ev.get("date_end") or "").strip() or None
            tm = (ev.get("time") or "").strip() or None
            if not ds:
                failed.append({"event": ev, "error": "date_start 필수"})
                continue

            body: dict = {
                "summary": summary,
                "description": ev.get("description") or "",
            }
            if tm:
                # 시간 있음 → dateTime 형식 (YYYY-MM-DDTHH:MM:00)
                body["start"] = {"dateTime": f"{ds}T{tm}:00", "timeZone": default_tz}
                end_date = de or ds
                end_time = tm  # 길이 미지정이면 같은 시각 (Google 이 알아서 처리)
                body["end"]   = {"dateTime": f"{end_date}T{end_time}:00", "timeZone": default_tz}
            else:
                # 시간 없음 → 종일 (date 형식). 종일 이벤트는 end.date 가 exclusive 이므로 +1 일.
                from datetime import datetime, timedelta
                try:
                    end_inclusive = de or ds
                    end_d = datetime.strptime(end_inclusive, "%Y-%m-%d") + timedelta(days=1)
                except ValueError:
                    failed.append({"event": ev, "error": "date_start/end 형식 오류 (YYYY-MM-DD)"})
                    continue
                body["start"] = {"date": ds}
                body["end"]   = {"date": end_d.strftime("%Y-%m-%d")}

            url = f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal_id, safe='')}/events"
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                results.append({
                    "input_id": ev.get("id"),
                    "google_event_id": out.get("id"),
                    "html_link": out.get("htmlLink"),
                    "summary": summary,
                })
            except urllib.error.HTTPError as e:
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                failed.append({"event": ev, "error": f"Google API {e.code}: {body_text[:200]}"})
            except Exception as e:  # noqa: BLE001
                failed.append({"event": ev, "error": str(e)})

        self._send_json({
            "inserted": len(results),
            "failed_count": len(failed),
            "failed": failed,
            "results": results,
        })

    # ─── Naver 로그인 + Calendar ──────────────────────────────────────────
    def _handle_naver_login_redirect(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        next_path = self._safe_next((qs.get("next") or [None])[0])
        # ?force=1 이면 동의 화면 강제 (캘린더 권한 새로 받을 때 사용)
        force = (qs.get("force") or [None])[0] in ("1", "true", "yes")
        try:
            url = naver_authorize_url(state=next_path, force_reconsent=force)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_redirect(url)

    def _handle_naver_callback(self, query: str) -> None:
        qs = parse_qs(query)
        code = (qs.get("code") or [None])[0]
        err = (qs.get("error") or [None])[0]
        state = (qs.get("state") or [None])[0] or "/"
        next_path = self._safe_next(state)
        if err:
            self._send_redirect(f"{next_path}?auth_error={err}")
            return
        if not code:
            self._send_redirect(f"{next_path}?auth_error=missing_code")
            return
        try:
            token = exchange_naver_code_for_token(code, state)
            access = token.get("access_token")
            if not access:
                raise RuntimeError(f"Naver 토큰 응답에 access_token 없음: {token}")
            info = fetch_naver_user_info(access)
            user_id = upsert_user_from_naver(info)
            # 네이버는 로그인 = 캘린더 권한이므로 무조건 토큰 저장
            save_naver_tokens(
                user_id,
                refresh_token=token.get("refresh_token"),
                access_token=access,
                expires_in=int(token.get("expires_in") or 3600),
            )
        except Exception as e:  # noqa: BLE001
            print(f"[auth] naver callback 실패: {e}")
            self._send_redirect(f"{next_path}?auth_error=naver_failed")
            return

        cookie = make_set_cookie_header(user_id)
        self._send_redirect(next_path, set_cookie=cookie)

    def _handle_naver_calendar_status(self) -> None:
        user_id = self._current_user_id()
        if user_id is None:
            self._send_json({"connected": False, "auth_required": True})
            return
        self._send_json({
            "connected": has_naver_calendar(user_id),
            "auth_required": False,
        })

    def _handle_naver_calendar_disconnect(self) -> None:
        user_id = self._require_user_id()
        if user_id is None: return
        delete_naver_tokens(user_id)
        self._send_json({"ok": True})

    def _handle_naver_calendar_insert(self) -> None:
        """선택된 일정들을 사용자 네이버 캘린더에 일괄 추가 (iCal 형식).

        Body: {"event_ids": [...]} (또는 events: [{...}, ...])
        네이버 API: POST https://openapi.naver.com/calendar/createSchedule.json
          - Header: Authorization: Bearer {access_token}
          - Content-Type: application/x-www-form-urlencoded; charset=UTF-8
          - Body: calendarId=defaultCalendarId&scheduleIcalString=<URL-encoded VCALENDAR>
        """
        import urllib.error, urllib.request
        user_id = self._require_user_id()
        if user_id is None: return

        access = get_valid_naver_access_token(user_id)
        # ────── 토큰 점검 로그 ──────
        if not access or not isinstance(access, str) or not access.strip():
            print(f"[naver/insert] user={user_id} access_token invalid: {access!r}")
            self._send_json({
                "error": "네이버 캘린더 권한이 없습니다. 네이버로 다시 로그인해주세요",
                "code": "NAVER_CALENDAR_NOT_CONNECTED",
            }, status=400)
            return
        access = access.strip()
        # 헤더 앞 20자만 안전하게 로그 (전체 노출 X)
        print(f"[naver/insert] user={user_id} token_len={len(access)} "
              f"header_prefix='Bearer {access[:8]}...'")

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return

        events_in = data.get("events") or []
        event_ids = data.get("event_ids") or []
        if event_ids and not events_in:
            try:
                ids = [int(x) for x in event_ids]
            except (TypeError, ValueError):
                self._send_json({"error": "event_ids 는 정수 리스트"}, status=400)
                return
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                placeholders = ",".join(["?"] * len(ids))
                rows = conn.execute(
                    f"SELECT e.id, e.title, e.type, e.date_start, e.date_end, e.time, "
                    f"       e.note, f.name AS folder_name "
                    f"FROM announcement_schedule_events e "
                    f"JOIN attachment_folders f ON f.id = e.folder_id "
                    f"WHERE e.id IN ({placeholders}) AND e.user_id=?",
                    [*ids, user_id],
                ).fetchall()
            finally:
                conn.close()
            events_in = []
            for r in rows:
                desc_bits = []
                if r["folder_name"]: desc_bits.append(f"공고: {r['folder_name']}")
                if r["type"]:        desc_bits.append(f"유형: {r['type']}")
                if r["note"]:        desc_bits.append(r["note"])
                events_in.append({
                    "id": r["id"],
                    "summary": r["title"],
                    "description": "\n".join(desc_bits),
                    "date_start": r["date_start"],
                    "date_end": r["date_end"],
                    "time": r["time"],
                })

        if not isinstance(events_in, list) or not events_in:
            self._send_json({"error": "events 또는 event_ids 필수"}, status=400)
            return
        if len(events_in) > 50:
            self._send_json({"error": "한 번에 최대 50개"}, status=400)
            return

        NAVER_URL = "https://openapi.naver.com/calendar/createSchedule.json"
        NAVER_CT  = "application/x-www-form-urlencoded; charset=UTF-8"

        results: list[dict] = []
        failed: list[dict] = []
        auth_expired = False    # 401 + errorCode 024 감지되면 True → 프론트에서 강제 재로그인 안내
        for ev in events_in:
            try:
                ical = self._build_single_event_vcalendar(ev)
            except ValueError as e:
                failed.append({"event": ev, "error": str(e)})
                continue
            params = urllib.parse.urlencode({
                "calendarId": "defaultCalendarId",
                "scheduleIcalString": ical,
            }).encode("utf-8")
            req = urllib.request.Request(
                NAVER_URL,
                data=params, method="POST",
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": NAVER_CT,
                },
            )
            # ────── 요청 직전 상세 로그 ──────
            print(f"[naver/insert] → POST {NAVER_URL}")
            print(f"[naver/insert]   Content-Type={NAVER_CT}")
            print(f"[naver/insert]   Authorization=Bearer {access[:8]}... (len={len(access)})")
            print(f"[naver/insert]   body_len={len(params)} (calendarId + iCal {len(ical)} chars)")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8")
                print(f"[naver/insert] ← {resp.status} body={body[:300]}")
                try:
                    out = json.loads(body)
                except Exception:
                    out = {"raw": body[:200]}
                results.append({
                    "input_id": ev.get("id"),
                    "summary": ev.get("summary"),
                    "naver_response": out,
                })
            except urllib.error.HTTPError as e:
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                print(f"[naver/insert] ← HTTPError {e.code} body={body_text[:300]}")
                # 401 + Naver errorCode 024 = 토큰이 캘린더 권한 없음 → 강제 재로그인 필요
                if e.code == 401 and ('"errorCode":"024"' in body_text or "024" in body_text):
                    auth_expired = True
                failed.append({"event": ev, "error": f"Naver API {e.code}: {body_text[:200]}"})
            except Exception as e:  # noqa: BLE001
                print(f"[naver/insert] ← Exception: {e}")
                failed.append({"event": ev, "error": str(e)})

        # 모든 호출이 401/024 로 실패 → 토큰 자체가 문제 → 저장된 토큰 삭제 후 재로그인 유도
        if auth_expired and not results:
            from planner.auth import delete_naver_tokens
            delete_naver_tokens(user_id)
            print(f"[naver/insert] user={user_id} 토큰 삭제 (캘린더 권한 없음 - 재로그인 필요)")
            self._send_json({
                "error": "네이버 캘린더 권한이 없습니다. 동의 화면을 다시 띄워 캘린더 권한을 받아주세요.",
                "code": "NAVER_AUTH_EXPIRED",
                "reconnect_url": "/api/auth/naver/login?force=1&next=/dashboard",
                "failed": failed,
            }, status=401)
            return

        self._send_json({
            "inserted": len(results),
            "failed_count": len(failed),
            "failed": failed,
            "results": results,
        })

    def _build_single_event_vcalendar(self, ev: dict) -> str:
        """일정 1건을 RFC 5545 VCALENDAR 문자열로 직렬화 (Naver Calendar API 용).

        ev: {summary, description?, date_start (YYYY-MM-DD), date_end?, time? (HH:MM), location?}

        네이버 공식 샘플(developers.naver.com/docs/login/calendar-api)에 맞춰
        VTIMEZONE + SEQUENCE/CLASS/TRANSP/CREATED/LAST-MODIFIED 까지 포함.
        """
        import re as _re
        import uuid as _uuid
        from datetime import datetime, timedelta

        summary = (ev.get("summary") or "").strip()
        if not summary:
            raise ValueError("summary 필수")
        ds = (ev.get("date_start") or "").strip()
        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
            raise ValueError("date_start 형식 오류 (YYYY-MM-DD)")
        de = (ev.get("date_end") or "").strip() or ds
        if de and not _re.match(r"^\d{4}-\d{2}-\d{2}$", de):
            raise ValueError("date_end 형식 오류 (YYYY-MM-DD)")
        tm = (ev.get("time") or "").strip() or None
        if tm and not _re.match(r"^\d{2}:\d{2}$", tm):
            raise ValueError("time 형식 오류 (HH:MM)")
        description = (ev.get("description") or "")
        location = (ev.get("location") or "").strip()

        def esc(s: str) -> str:
            # RFC 5545 텍스트 이스케이프
            return (s.replace("\\", "\\\\")
                     .replace(";", "\\;")
                     .replace(",", "\\,")
                     .replace("\n", "\\n")
                     .replace("\r", ""))

        # UID — 특수문자 % 금지 (네이버 주의사항). uuid4 hex 는 [0-9a-f] 만이라 안전.
        uid = f"{_uuid.uuid4().hex}@founderly"
        now_z = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

        if tm:
            ds_dt = ds.replace("-", "") + "T" + tm.replace(":", "") + "00"
            de_dt = de.replace("-", "") + "T" + tm.replace(":", "") + "00"
            dtstart = f"DTSTART;TZID=Asia/Seoul:{ds_dt}"
            dtend   = f"DTEND;TZID=Asia/Seoul:{de_dt}"
        else:
            # 종일: VALUE=DATE, DTEND 는 exclusive → +1 일
            de_inclusive = datetime.strptime(de, "%Y-%m-%d") + timedelta(days=1)
            dtstart = f"DTSTART;VALUE=DATE:{ds.replace('-', '')}"
            dtend   = f"DTEND;VALUE=DATE:{de_inclusive.strftime('%Y%m%d')}"

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Founderly//Schedule//KO",
            "CALSCALE:GREGORIAN",
            # VTIMEZONE — 네이버 공식 샘플에 포함됨. TZID 참조용.
            "BEGIN:VTIMEZONE",
            "TZID:Asia/Seoul",
            "BEGIN:STANDARD",
            "DTSTART:19700101T000000",
            "TZNAME:GMT+09:00",
            "TZOFFSETFROM:+0900",
            "TZOFFSETTO:+0900",
            "END:STANDARD",
            "END:VTIMEZONE",
            "BEGIN:VEVENT",
            "SEQUENCE:0",
            "CLASS:PUBLIC",
            "TRANSP:OPAQUE",
            f"UID:{uid}",
            dtstart,
            dtend,
            f"SUMMARY:{esc(summary)}",
        ]
        if description:
            lines.append(f"DESCRIPTION:{esc(description)}")
        if location:
            lines.append(f"LOCATION:{esc(location)}")
        lines.extend([
            f"CREATED:{now_z}",
            f"LAST-MODIFIED:{now_z}",
            f"DTSTAMP:{now_z}",
            "END:VEVENT",
            "END:VCALENDAR",
        ])
        return "\r\n".join(lines)

    def _handle_logout(self) -> None:
        self._send_json(
            {"ok": True},
            set_cookie=make_clear_cookie_header(),
        )

    # ─── 사용자 프로필 (온보딩) ─────────────────────────────────────────────
    _PROFILE_FIELDS = (
        # 기존 (시즌 1)
        "company_name", "business_type", "establishment_date", "region", "industry",
        "business_number", "representative_name", "representative_email", "industry_detail",
        # 확장 (시즌 2) — 사업계획서 자동 생성 대비
        "english_name", "corporation_number", "representative_birth", "employee_count",
        "founding_type", "business_address", "address_zonecode", "phone", "fax",
        "website", "industry_code", "industry_type",
    )
    _BUSINESS_TYPES = {"individual", "corporate", "prelaunch"}
    _INT_FIELDS = {"employee_count"}

    def _handle_profile_get(self) -> None:
        user_id = self._require_user_id()
        if user_id is None: return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f"SELECT {', '.join(self._PROFILE_FIELDS)}, created_at, updated_at "
                "FROM user_profiles WHERE user_id=?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            self._send_json({"profile": None, "completed": False})
            return
        profile = dict(row)
        # 필수 필드 모두 채워졌는지 (예비창업자는 establishment_date 면제)
        bt = profile.get("business_type")
        required = ["company_name", "business_type", "region", "industry"]
        if bt != "prelaunch":
            required.append("establishment_date")
        completed = all(profile.get(k) for k in required)
        self._send_json({"profile": profile, "completed": completed})

    def _handle_profile_save(self) -> None:
        user_id = self._require_user_id()
        if user_id is None: return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return

        # 받은 필드만 추출 (알 수 없는 키 무시)
        clean: dict = {}
        for k in self._PROFILE_FIELDS:
            v = data.get(k)
            if isinstance(v, str):
                v = v.strip() or None
            if k in self._INT_FIELDS and v is not None and v != "":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    self._send_json({"error": f"{k} 는 정수여야 합니다"}, status=400)
                    return
            elif k in self._INT_FIELDS and (v == "" or v is None):
                v = None
            clean[k] = v

        bt = clean.get("business_type")
        if bt is not None and bt not in self._BUSINESS_TYPES:
            self._send_json(
                {"error": f"business_type must be one of {sorted(self._BUSINESS_TYPES)}"},
                status=400,
            )
            return

        # 날짜 형식 검증 (예비창업자는 비워도 OK)
        import re
        for date_field in ("establishment_date", "representative_birth"):
            v = clean.get(date_field)
            if v and not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                self._send_json(
                    {"error": f"{date_field} 는 YYYY-MM-DD 형식이어야 합니다"},
                    status=400,
                )
                return

        cols = list(self._PROFILE_FIELDS)
        placeholders = ", ".join(["?"] * len(cols))
        col_csv = ", ".join(cols)
        update_set = ", ".join(f"{c}=excluded.{c}" for c in cols)
        values = [clean[c] for c in cols]

        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                f"INSERT INTO user_profiles(user_id, {col_csv}, updated_at) "
                f"VALUES (?, {placeholders}, CURRENT_TIMESTAMP) "
                f"ON CONFLICT(user_id) DO UPDATE SET "
                f"  {update_set}, updated_at=CURRENT_TIMESTAMP",
                [user_id, *values],
            )
            conn.commit()
        finally:
            conn.close()
        self._send_json({"ok": True, "profile": clean})

    # ─── 보유 서류 (Phase 6: localStorage → DB) ────────────────────────────
    def _handle_my_docs_list(self) -> None:
        """현재 사용자의 보유 서류 목록 조회."""
        user_id = self._require_user_id()
        if user_id is None: return
        if not DB_PATH.exists():
            self._send_json({"documents": []})
            return
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, name, issued_date, note, created_at "
                "FROM user_documents WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            )]
        finally:
            conn.close()
        self._send_json({"documents": rows})

    def _handle_my_docs_save(self) -> None:
        """보유 서류 전체 교체 저장 (replace mode).

        Body: {"documents": [{"name": "...", "issued_date": "YYYY-MM-DD", "note?": "..."}]}
        """
        user_id = self._require_user_id()
        if user_id is None: return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON: {e}"}, status=400)
            return
        docs = data.get("documents") or []
        if not isinstance(docs, list):
            self._send_json({"error": "documents 는 배열이어야 합니다"}, status=400)
            return
        if len(docs) > 100:
            self._send_json({"error": "최대 100건"}, status=400)
            return

        # 형식 검증
        import re as _re
        clean = []
        for d in docs:
            if not isinstance(d, dict): continue
            name = (d.get("name") or "").strip()
            issued = (d.get("issued_date") or "").strip()
            note = (d.get("note") or "").strip() or None
            if not name or not issued: continue
            if not _re.match(r"^\d{4}-\d{2}-\d{2}$", issued):
                self._send_json(
                    {"error": f"issued_date 형식 오류: {issued!r} (YYYY-MM-DD)"},
                    status=400,
                )
                return
            if len(name) > 100:
                self._send_json({"error": f"name 너무 김 (max 100): {name[:30]}..."}, status=400)
                return
            clean.append((name, issued, note))

        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("DELETE FROM user_documents WHERE user_id=?", (user_id,))
            for name, issued, note in clean:
                conn.execute(
                    "INSERT INTO user_documents(user_id, name, issued_date, note) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, name, issued, note),
                )
            conn.commit()
        finally:
            conn.close()
        self._send_json({"ok": True, "count": len(clean)})

    def log_message(self, format: str, *args) -> None:  # 콘솔 조용히
        return


def main(host: str = "127.0.0.1", port: int = 8765) -> None:
    # .env 를 서버 시작 시점에 미리 로드 (auth, LLM 등 전체에서 환경변수 사용)
    from planner.analyzer.dotenv import load_dotenv
    load_dotenv()
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
