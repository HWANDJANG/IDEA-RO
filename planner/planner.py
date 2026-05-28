"""사용자 맞춤 추천 + 액션 플랜 생성 (엔드 투 엔드 1단계).

기존 매칭/자격/유형 분류를 한 흐름으로 묶어 Top N 공고와
각 공고별 부족 서류 + 마감 D-day 를 정리해 반환한다.

LLM 호출 없음 (Step 4 에서 추가 예정).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Optional, cast

from .matcher import (
    BusinessType,
    classify_announcement_type,
    compute_profile_fit,
    match_announcement,
)
from .paths import DB_PATH


# compute_profile_fit / match_announcement 가 보는 핵심 필드만.
_PLAN_PROFILE_FIELDS = (
    "business_type", "establishment_date", "region", "industry",
    "company_name", "founding_type",
)


def _load_profile(conn: sqlite3.Connection, user_id: int) -> Optional[dict]:
    row = conn.execute(
        f"SELECT {', '.join(_PLAN_PROFILE_FIELDS)} FROM user_profiles WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def _load_my_docs(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT name, issued_date FROM user_documents WHERE user_id=?",
        (user_id,),
    ).fetchall()
    result: list[dict] = []
    for r in rows:
        try:
            issued = date.fromisoformat(r["issued_date"])
        except (ValueError, TypeError):
            continue
        result.append({"name": r["name"], "issued_date": issued})
    return result


def _load_active_announcements(conn: sqlite3.Connection, limit: int = 500) -> list[dict]:
    q = (
        "SELECT a.id, a.title, a.start_date, a.end_date, a.d_day, "
        "       a.department, a.contact, a.detail_url, a.content_text, a.raw_meta, "
        "       s.code AS source_code, s.name AS source_name "
        "FROM announcements a JOIN sources s ON s.id = a.source_id "
        "WHERE a.end_date IS NOT NULL AND a.end_date != '' "
        "  AND substr(a.end_date,1,10) >= date('now') "
        "ORDER BY a.end_date ASC LIMIT ?"
    )
    return [dict(r) for r in conn.execute(q, (limit,))]


def _parse_raw_meta(s: Optional[str]) -> Optional[dict]:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _days_until(end_str: Optional[str], today: date) -> Optional[int]:
    if not end_str:
        return None
    try:
        return (date.fromisoformat(end_str[:10]) - today).days
    except (ValueError, TypeError):
        return None


def _to_iso(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def _serialize_task(t: dict) -> dict:
    return {
        **t,
        "due_date": _to_iso(t.get("due_date")),
        "earliest_date": _to_iso(t.get("earliest_date")),
    }


def compose_action_plan(user_id: int, top_n: int = 5) -> dict:
    """사용자별 Top N 추천 공고 + 각 공고의 부족 서류 + 발급 태스크 반환.

    Pipeline:
      1. 프로필 + 보유 서류 로드 (DB)
      2. 모든 진행중 공고에 대해 자격 매칭 (LLM 없음)
      3. 자격 통과 + 마감 임박 종합 점수로 정렬 → Top N
      4. Top N 각각에 대해 서류 매칭 detail 계산 (preset rule)
      5. 결과 정리해서 dict 반환
    """
    today = date.today()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        profile = _load_profile(conn, user_id)
        my_docs = _load_my_docs(conn, user_id)
        anns = _load_active_announcements(conn)
    finally:
        conn.close()

    # match_announcement 는 individual/corporate 만 지원. prelaunch 는 individual 로 fallback.
    raw_bt = (profile or {}).get("business_type") or "individual"
    bt: BusinessType = cast(BusinessType, raw_bt if raw_bt in ("individual", "corporate") else "individual")

    # ── 1단계: 모든 공고 자격 + 마감 점수 계산 (가벼움) ──
    scored: list[tuple] = []
    for a in anns:
        raw_meta = _parse_raw_meta(a.get("raw_meta"))
        fit = compute_profile_fit(profile, raw_meta, today)
        if not fit.get("eligible"):
            continue
        days_left = _days_until(a.get("end_date"), today)
        if days_left is None or days_left < 0:
            continue
        # 마감 임박 가산: 14일 이내 +20, 30일 이내 +10
        urgency = 20 if days_left <= 14 else (10 if days_left <= 30 else 0)
        combined = int(fit["score"]) + urgency
        scored.append((combined, days_left, a, fit, raw_meta))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:top_n]

    # ── 2단계: Top N 서류 매칭 detail ──
    recommendations: list[dict] = []
    for combined, days_left, a, fit, raw_meta in top:
        try:
            deadline = date.fromisoformat(str(a["end_date"])[:10])
        except (ValueError, TypeError):
            continue

        try:
            match_result = match_announcement(
                announcement_id=a["id"],
                title=a["title"],
                deadline=deadline,
                content_text=a.get("content_text"),
                business_type=bt,
                user_documents=my_docs,
            )
        except Exception:  # noqa: BLE001 — 한 공고 실패가 전체 막지 않도록
            match_result = None

        missing_docs: list[str] = []
        expiring_docs: list[str] = []
        expired_docs: list[str] = []
        fulfilled_docs: list[str] = []
        issue_tasks: list[dict] = []
        summary = {"fulfilled": 0, "expiring": 0, "expired": 0, "missing": 0}

        if match_result:
            for d in match_result.get("required_docs", []):
                name = d.get("name") or ""
                st = d.get("holding_status")
                if st == "missing":
                    missing_docs.append(name)
                elif st == "expiring_soon":
                    expiring_docs.append(name)
                elif st == "expired":
                    expired_docs.append(name)
                elif st in ("valid", "no_expiry"):
                    fulfilled_docs.append(name)
            for t in match_result.get("tasks", []):
                if isinstance(t, dict):
                    issue_tasks.append(_serialize_task(t))
            summary = match_result.get("summary", summary)

        type_info = classify_announcement_type(
            raw_meta, a.get("source_code"), a.get("title")
        )

        recommendations.append({
            "announcement_id": a["id"],
            "title": a["title"],
            "source_code": a.get("source_code"),
            "source_name": a.get("source_name"),
            "department": a.get("department"),
            "detail_url": a.get("detail_url"),
            "start_date": a.get("start_date"),
            "end_date": a.get("end_date"),
            "d_day": a.get("d_day"),
            "days_left": days_left,
            "type": type_info,
            "profile_fit": fit,
            "score_combined": combined,
            "fulfillment": match_result.get("fulfillment") if match_result else None,
            "summary": summary,
            "fulfilled_docs": fulfilled_docs,
            "missing_docs": missing_docs,
            "expiring_docs": expiring_docs,
            "expired_docs": expired_docs,
            "issue_tasks": issue_tasks,
        })

    return {
        "generated_at": datetime.now().isoformat(),
        "user_id": user_id,
        "profile_completed": bool(profile),
        "my_docs_count": len(my_docs),
        "candidate_pool": len(anns),
        "eligible_pool": len(scored),
        "top_n": top_n,
        "business_type": bt,
        "recommendations": recommendations,
    }
