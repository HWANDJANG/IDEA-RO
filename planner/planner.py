"""사용자 맞춤 추천 + 액션 플랜 생성 (엔드 투 엔드 1단계).

기존 매칭/자격/유형 분류를 한 흐름으로 묶어 Top N 공고와
각 공고별 부족 서류 + 마감 D-day 를 정리해 반환한다.

LLM 호출 없음 (Step 4 에서 추가 예정).
"""

from __future__ import annotations

import json
import re
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


# ─── 지원금 / 노력 추정 (단순 휴리스틱) ─────────────────────────────────

_AMOUNT_PATTERNS = [
    # (정규식, 곱셈 단위)
    (re.compile(r"(?:최대\s*|최대[\s,]*)?(\d+(?:[,\.]\d+)?)\s*억\s*(?:원|만원)?"), 100_000_000),
    (re.compile(r"(?:최대\s*)?(\d+(?:[,\.]\d+)?)\s*천만\s*원"),                       10_000_000),
    (re.compile(r"(?:최대\s*)?(\d+(?:[,\.]\d+)?)\s*백만\s*원"),                        1_000_000),
    (re.compile(r"(?:최대\s*)?(\d{1,5}(?:,\d{3})*)\s*만\s*원"),                          10_000),
]


def _extract_amount_won(content_text: Optional[str], raw_meta: Optional[dict]) -> tuple[int, Optional[str]]:
    """공고에서 최대 지원금 추출. (won, display_text)."""
    if raw_meta:
        for key in ("max_support_amount", "support_amount_won"):
            v = raw_meta.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return (int(v), _format_won(int(v)))
    if not content_text:
        return (0, None)
    text = content_text[:6000]  # 처음 6천자만 — 보통 첫 부분에 지원금 명시
    candidates: list[int] = []
    for pattern, mult in _AMOUNT_PATTERNS:
        for m in pattern.finditer(text):
            raw = m.group(1).replace(",", "")
            try:
                num = float(raw)
            except ValueError:
                continue
            won = int(num * mult)
            # 현실적 범위 (10만원 ~ 1000억원). 헛수치 제거
            if 100_000 <= won <= 100_000_000_000:
                candidates.append(won)
    if not candidates:
        return (0, None)
    best = max(candidates)
    return (best, _format_won(best))


def _format_won(won: int) -> str:
    if won >= 100_000_000:
        v = won / 100_000_000
        return f"최대 {v:.1f}억원" if v < 10 else f"최대 {int(v)}억원"
    if won >= 10_000_000:
        return f"최대 {won // 10_000_000:,}천만원"
    if won >= 1_000_000:
        return f"최대 {won // 1_000_000}백만원"
    return f"최대 {won // 10_000}만원"


def _amount_to_score(won: int) -> int:
    """지원금 → 0~30 점."""
    if won >= 500_000_000: return 30      # 5억+
    if won >= 100_000_000: return 26      # 1억~5억
    if won >= 50_000_000:  return 20      # 5천만~1억
    if won >= 10_000_000:  return 13      # 1천만~5천만
    if won > 0:            return 7       # ~1천만
    return 10  # 모름 — 중간값으로 보정


# 유형별 base effort (0~30, R&D·자금 무거움, 행사·교육 가벼움)
_EFFORT_BY_TYPE = {
    "funding": 22, "rnd": 25, "space": 14, "global": 18,
    "edu": 8, "event": 5, "other": 12,
}


def _estimate_effort(req_doc_count: int, type_code: str) -> int:
    """0~30 점."""
    base = _EFFORT_BY_TYPE.get(type_code, 12)
    if req_doc_count >= 6:   base += 5
    elif req_doc_count >= 4: base += 3
    elif req_doc_count <= 1: base -= 2
    return max(0, min(30, base))


def _effort_label(score: int) -> str:
    if score >= 20: return "높음"
    if score >= 12: return "중간"
    return "낮음"


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


def compose_action_plan(
    user_id: int,
    top_n: int = 5,
    weights: Optional[dict] = None,
) -> dict:
    """사용자별 Top N 추천 공고 + 각 공고의 부족 서류 + 발급 태스크 반환.

    weights (0~1, default 0.5 / 0.3 / 0.5):
      - amount:  지원금 규모 중요도 (1=고액 위주, 0=무시)
      - effort:  노력 회피 정도 (1=쉬운 거 위주, 0=노력 무관)
      - urgency: 마감 임박 우선 (1=급한 거 위주, 0=무관)

    점수 공식:
      combined = profile_fit
               + urgency_score * w_urgency
               + amount_score  * w_amount
               - effort_score  * w_effort
    """
    weights = weights or {}
    w_amount  = max(0.0, min(1.0, float(weights.get("amount",  0.5))))
    w_effort  = max(0.0, min(1.0, float(weights.get("effort",  0.3))))
    w_urgency = max(0.0, min(1.0, float(weights.get("urgency", 0.5))))

    today = date.today()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        profile = _load_profile(conn, user_id)
        my_docs = _load_my_docs(conn, user_id)
        anns = _load_active_announcements(conn)
    finally:
        conn.close()

    raw_bt = (profile or {}).get("business_type") or "individual"
    bt: BusinessType = cast(BusinessType, raw_bt if raw_bt in ("individual", "corporate") else "individual")

    # ── 1단계: 모든 자격 통과 공고에 대해 light score ──
    scored: list[tuple] = []
    for a in anns:
        raw_meta = _parse_raw_meta(a.get("raw_meta"))
        fit = compute_profile_fit(profile, raw_meta, today)
        if not fit.get("eligible"):
            continue
        days_left = _days_until(a.get("end_date"), today)
        if days_left is None or days_left < 0:
            continue

        # urgency: D-day 단계
        if days_left <= 7:    urgency_score = 30
        elif days_left <= 14: urgency_score = 20
        elif days_left <= 30: urgency_score = 10
        else:                 urgency_score = 0

        type_info = classify_announcement_type(raw_meta, a.get("source_code"), a.get("title"))

        won, amt_display = _extract_amount_won(a.get("content_text"), raw_meta)
        amount_score = _amount_to_score(won)

        # light effort: 서류 수 모르므로 type 기반만. Top N detail 단계에서 보정.
        effort_score_light = _estimate_effort(0, type_info["code"])

        combined_light = (
            int(fit["score"])
            + int(urgency_score * w_urgency)
            + int(amount_score  * w_amount)
            - int(effort_score_light * w_effort)
        )
        signals_light = {
            "profile":        int(fit["score"]),
            "urgency":        urgency_score,
            "amount":         amount_score,
            "effort":         effort_score_light,
            "amount_won":     won,
            "amount_display": amt_display,
        }
        scored.append((combined_light, days_left, a, fit, raw_meta, type_info, signals_light))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:top_n]

    # ── 2단계: Top N 서류 매칭 detail + effort 보정 ──
    recommendations: list[dict] = []
    for combined_light, days_left, a, fit, raw_meta, type_info, signals in top:
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
        except Exception:  # noqa: BLE001
            match_result = None

        missing_docs: list[str] = []
        expiring_docs: list[str] = []
        expired_docs: list[str] = []
        fulfilled_docs: list[str] = []
        issue_tasks: list[dict] = []
        summary = {"fulfilled": 0, "expiring": 0, "expired": 0, "missing": 0}
        req_doc_count = 0

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
            req_doc_count = len(match_result.get("required_docs", []))

        # effort 정확도 보정 (서류 수 반영)
        effort_score = _estimate_effort(req_doc_count, type_info["code"])
        signals["effort"] = effort_score
        signals["effort_label"] = _effort_label(effort_score)
        signals["req_doc_count"] = req_doc_count

        # combined 재계산 (effort 보정 반영)
        combined = (
            int(fit["score"])
            + int(signals["urgency"] * w_urgency)
            + int(signals["amount"]  * w_amount)
            - int(effort_score       * w_effort)
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
            "signals": signals,
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
        "weights": {"amount": w_amount, "effort": w_effort, "urgency": w_urgency},
        "recommendations": recommendations,
    }
