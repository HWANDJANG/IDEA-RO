"""사용자 맞춤 추천 + 액션 플랜 생성 (엔드 투 엔드 1단계).

기존 매칭/자격/유형 분류를 한 흐름으로 묶어 Top N 공고와
각 공고별 부족 서류 + 마감 D-day 를 정리해 반환한다.

LLM 호출 없음 (Step 4 에서 추가 예정).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime
from typing import Optional, cast

from .matcher import (
    BusinessType,
    INDUSTRY_TAGS,
    classify_announcement_type,
    compute_industry_fit,
    compute_profile_fit,
    match_announcement,
    normalize_interest_tags,
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


# ─── LLM Narrative — "왜 이 공고가 당신에게 맞는지" 한 줄 (Step 4) ──────

_NARRATIVE_SYSTEM = """당신은 한국 정부 창업지원사업 추천 컨설턴트입니다.
사용자 프로필과 추천 공고 후보들을 받으면, 각 공고가 왜 이 사용자에게 맞는지를 한 문장으로 요약합니다.

엄격히 지켜야 할 규칙:
1. 단정 금지: "베스트", "반드시 신청", "최고" 같은 표현 금지. 측면별 매칭 포인트만 짚으세요.
2. 사용자 프로필의 구체적 요소를 인용하세요 (예: "예비창업자 트랙이 있어 0.2년 업력과 매칭").
3. 가중치(amount/effort/urgency)를 고려해 왜 이 순위인지 자연스럽게 설명.
   - amount 높으면: 지원금 규모 강조 (예: "지원금 최대 X원")
   - effort 높으면: 노력이 낮다는 점 강조 (예: "필요 서류 N건으로 가벼움")
   - urgency 높으면: 마감 임박 강조 (예: "D-X 안에 마감")
4. 한 문장만. 한국어로 50~80자.
5. 응답은 반드시 지정된 JSON 스키마에 맞아야 합니다."""

_NARRATIVE_USER_TMPL = """## 사용자 프로필
{profile_json}

## 정렬 가중치 (0~1, 높을수록 중요)
- 지원금 규모: {w_amount}
- 노력 회피: {w_effort}
- 마감 임박: {w_urgency}
- 분야 적합도: {w_industry}

## 사용자 관심 분야 (선택)
{interest_tags_str}

## 추천 후보 ({n}건, 위에서부터 1순위)
{cards}

위 각 후보에 대해 "왜 이 사용자에게 맞는지" 한 문장 요약을 만들어 주세요.
"분야매칭=…" 신호가 있으면 그 분야의 매칭 키워드를 활용해 자연스럽게 설명하세요.
"""

_NARRATIVE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "narratives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "announcement_id": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["announcement_id", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["narratives"],
    "additionalProperties": False,
}


def _narrative_cache_key(user_id: int, ann_ids: list[int], weights: dict) -> str:
    """user + 추천 조합 + 가중치 기반 캐시 키. 슬라이더 같은 조합 → 같은 키."""
    parts = [str(user_id), ",".join(str(x) for x in sorted(ann_ids))]
    parts.append(
        f"a{weights.get('amount', 0.5):.2f}"
        f"e{weights.get('effort', 0.3):.2f}"
        f"u{weights.get('urgency', 0.5):.2f}"
        f"i{weights.get('industry', 0.7):.2f}"
    )
    raw = "|".join(parts)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"plan_narrative_{user_id}_{h}"


def _profile_for_narrative(profile: Optional[dict]) -> dict:
    """LLM 컨텍스트에 안전하게 보낼 프로필 (PII 최소화)."""
    if not profile: return {}
    keep = ("business_type", "establishment_date", "region", "industry", "founding_type", "company_name")
    return {k: profile[k] for k in keep if k in profile and profile[k] not in (None, "")}


def _card_for_narrative(r: dict) -> str:
    """추천 1개를 LLM 입력용 한 줄로 압축."""
    s = r.get("signals") or {}
    parts = [
        f"id={r['announcement_id']}",
        f"제목={r.get('title', '')[:80]}",
        f"유형={r.get('type', {}).get('label', '')}",
        f"마감=D-{r.get('days_left')}",
        f"지원금={s.get('amount_display') or '미상'}",
        f"노력={s.get('effort_label')} (서류 {s.get('req_doc_count', 0)}건)",
        f"자격={r.get('profile_fit', {}).get('score', 0)}점",
    ]
    # Step A: 사용자 관심 분야 매칭 — 매칭됐을 때만 추가 (없으면 시각 노이즈)
    matched = s.get("industry_tags") or []
    hits = s.get("industry_hits") or []
    if matched:
        labels = [INDUSTRY_TAGS.get(t, {}).get("label", t) for t in matched]
        kw_str = f" ({', '.join(hits[:3])})" if hits else ""
        parts.append(f"분야매칭={'/'.join(labels)}{kw_str}")
    return "- " + " | ".join(parts)


def generate_recommendation_narratives(
    user_id: int,
    plan: dict,
    *,
    use_cache: bool = True,
) -> dict:
    """추천 공고들에 대해 LLM 한 줄 narrative 생성.

    Returns: {"narratives": {ann_id: "...", ...}, "cached": bool, "count": int}
    """
    from .analyzer.llm.base import LLMError
    from .analyzer.llm.registry import get_llm_provider
    from .analyzer.storage import load_derived, save_derived

    recs = plan.get("recommendations") or []
    if not recs:
        return {"narratives": {}, "cached": False, "count": 0}

    weights = plan.get("weights") or {"amount": 0.5, "effort": 0.3, "urgency": 0.5}
    ann_ids = [r["announcement_id"] for r in recs]
    cache_key = _narrative_cache_key(user_id, ann_ids, weights)

    if use_cache:
        cached = load_derived(cache_key)
        if cached and isinstance(cached, dict) and "narratives" in cached:
            return {"narratives": cached["narratives"], "cached": True, "count": len(cached["narratives"])}

    # LLM 호출용 프로필 — DB 에서 다시 로드
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        profile = _load_profile(conn, user_id)
    finally:
        conn.close()

    profile_safe = _profile_for_narrative(profile)
    interest_tags = plan.get("interest_tags") or []
    if interest_tags:
        interest_str = ", ".join(
            INDUSTRY_TAGS.get(t, {}).get("label", t) for t in interest_tags
        )
    else:
        interest_str = "(미설정 — 분야 매칭 점수는 중립 적용)"
    user_msg = _NARRATIVE_USER_TMPL.format(
        profile_json=json.dumps(profile_safe, ensure_ascii=False),
        w_amount=weights.get("amount", 0.5),
        w_effort=weights.get("effort", 0.3),
        w_urgency=weights.get("urgency", 0.5),
        w_industry=weights.get("industry", 0.7),
        interest_tags_str=interest_str,
        n=len(recs),
        cards="\n".join(_card_for_narrative(r) for r in recs),
    )

    provider = get_llm_provider()
    try:
        result = provider.complete(
            system=_NARRATIVE_SYSTEM,
            user=user_msg,
            response_schema=_NARRATIVE_SCHEMA,
            max_tokens=1200,
        )
    except LLMError:
        raise
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"narrative 생성 실패: {e}") from e

    if not isinstance(result, dict):
        raise LLMError(f"narrative 응답 형식 오류: {type(result).__name__}")

    items = result.get("narratives") or []
    narratives: dict[int, str] = {}
    for it in items:
        try:
            aid = int(it["announcement_id"])
            why = str(it["why"]).strip()
            if why:
                narratives[aid] = why
        except (KeyError, TypeError, ValueError):
            continue

    # str key 로도 만들어서 캐시 (JSON 직렬화 시 int key 가 str 로 바뀌므로 통일)
    save_derived(cache_key, {"narratives": {str(k): v for k, v in narratives.items()}, "ann_ids": ann_ids, "weights": weights})
    # 응답은 str key (JSON 호환)
    return {"narratives": {str(k): v for k, v in narratives.items()}, "cached": False, "count": len(narratives)}


# ─── Step 5: 담은 공고 기반 "이번 주 / 다음 주 할 일" 종합 가이드 ──────

_GUIDE_SYSTEM = """당신은 한국 정부 창업지원사업 신청 컨설턴트입니다.
사용자가 '내 플랜에 담은' 공고들의 마감일·필요 서류·발급 일정·보유 서류 상태·자격 매칭·공고 특성을 보고,
시간 구간별로 "지금 무엇을 해야 하는지" 액션 가이드를 만듭니다.

엄격히 지켜야 할 규칙:
1. 행동 지향: "~하세요" / "~을 발급 신청" 같은 동사 명령형으로 작성.
2. 시간 구간을 명확히 (이번 주 / 다음 주 / 2주 후 ~ 마감 등).
3. 같은 서류가 여러 공고에 필요하면 한 번만 묶어서 언급.
4. 가장 임박한 마감을 최우선으로.

5. ★ 사용자의 보유 서류 상태를 정확히 반영하세요:
   - '보유' 목록의 서류는 다시 발급하지 마세요 — "이미 보유 중" 으로 인정.
   - '만료' 목록은 반드시 "재발급" 으로 표현 (신규 X).
   - '임박' 목록은 "갱신" 으로 표현하되 마감 전 충분한 여유 두기.
   - '미보유' 목록은 "신규 발급" 으로 표현.

6. ★ 가능한 구체적인 발급처를 1개 인용:
   - 주민등록표등본/초본: 정부24
   - 사업자등록증명·납세증명서: 홈택스
   - 지방세납세증명: 위택스
   - 4대보험 가입증명: 4대사회보험정보연계센터
   - 잘 모르면 "정부24" 로 기본 안내.

7. ★ 자격 매칭 결과(reasons)와 공고 특성을 적극 활용:
   - reasons 에 매칭 결과(업력/지역/신청대상)가 있으면 그 detail 을 인용하세요.
     예: "0.2년 예비창업자 트랙 매칭, 자격 100점"
   - 자격 점수가 낮은 항목은 "지원서에 매칭 포인트 강조" 같은 액션으로 연결.

8. ★ 공고 유형별 준비 강도를 다르게 안내:
   - 💰 자금 지원·🔬 R&D: 사업계획서/IR 자료/사업비 산출 비중 큼 → 다음 주 작업 시간 확보 권장.
   - 🏢 입주·공간: 입주신청서/시설 사용 계획서 + 면접 가능성.
   - 🎓 교육·멘토링: 자기소개·동기·학습 목표 1페이지 ~ 3페이지 수준.
   - 🌏 판로·글로벌: 영문 IR 또는 수출 실적 확인.
   - 🎯 행사·네트워크: 1페이지 기업 소개·참가 신청서로 가벼움.

9. ★ 지원금 규모와 노력 등급을 작업 우선순위에 반영:
   - 지원금 ≥1억원 + 노력 '높음' → 사업계획서 작성에 가장 큰 시간 배정 권장.
   - 지원금 작거나 노력 '낮음' → 발급 + 신청서로 충분.

10. ★ 공고 PDF 분석(📄) 이 있는 공고는 그 안의 평가 기준 / 의무사항 / 지원금 상세 / 유의사항을 적극 활용:
   - 평가 기준이 있으면 그 가중치(예: '기술성 40%, 사업성 30%')를 다음 주 가이드에 인용.
   - 의무사항이 있으면 key_warning 또는 마감 직전 가이드에서 "신청 전 부담 확인" 으로 짚어주기.
   - 지원금 상세(현물/연구비/매출액 대비 비율 등)가 있으면 사업계획서 작업 강도 결정에 반영.
   - 유의사항(중복 신청 금지, 자격 제한 등)이 있으면 반드시 명시.
   - PDF 분석이 없는 공고는 일반 가이드로 충분.

11. 사용자가 캘린더에 일정을 등록하는 단계는 이미 별도 버튼이 있으니 가이드에서 다시 언급하지 마세요.
12. 추상 표현 ("지원서류 준비") 대신 구체적 단계 ("사업자등록증명 발급 + 사업계획서 초안 작성") 로.
13. 항목당 한 줄, 한국어 40~120자.
14. 응답은 반드시 지정된 JSON 스키마에 맞아야 합니다."""

_GUIDE_USER_TMPL = """## 오늘 날짜
{today}

## 사용자 프로필
{profile_json}

## 담은 공고 ({n}건)
{picked_cards}

## 통합 발급 태스크 (담은 공고들 합쳐 서류명으로 dedup, 가장 빠른 due 우선)
{task_lines}

위 정보로 이번 주 / 다음 주 / 그 이후 무엇을 해야 하는지 시간 구간별 액션 가이드를 만들어 주세요.

가이드 작성 시 반드시 반영할 점:
- "이미 보유" 서류는 다시 발급하지 말 것.
- "만료" 서류는 "재발급", "미보유" 는 "신규 발급" 으로 표현 구분.
- 발급처(정부24 / 홈택스 / 위택스 등) 1개 인용.
- 자격 매칭 결과(업력/지역/신청대상)의 detail 을 그대로 인용.
- 공고 유형(자금/입주/교육/R&D/판로/행사)에 맞춘 준비 강도 안내.
- 지원금 규모가 큰 사업(≥1억원) 또는 노력 '높음' 사업은 사업계획서·IR 자료 작업 시간을 다음 주 가이드에 명시.
- 노력 '낮음' 사업은 발급 + 신청서로 충분함을 명시.

가장 중요한 한 가지 주의사항이 있으면 key_warning 에 한 줄로 적어 주세요."""

_GUIDE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "week_label": {"type": "string"},   # "이번 주", "다음 주", "2주 후 ~ 마감일" 등
                    "priority":   {"type": "string"},   # "high" | "medium" | "low"
                    "items":      {"type": "array", "items": {"type": "string"}},
                },
                "required": ["week_label", "priority", "items"],
                "additionalProperties": False,
            },
        },
        "key_warning": {"type": "string"},
    },
    "required": ["sections", "key_warning"],
    "additionalProperties": False,
}


def _guide_cache_key(user_id: int, picked_ids: list[int], today: date, pdf_hashes: Optional[list[str]] = None) -> str:
    """캐시 키 — 시스템 프롬프트 해시 + (Step C) 결합된 PDF 분석 hash 포함.
    프롬프트 수정 시 / PDF 추가·삭제 시 모두 자동 invalidate.
    """
    sys_hash = hashlib.sha256(_GUIDE_SYSTEM.encode("utf-8")).hexdigest()[:6]
    pdf_part = ",".join(sorted(pdf_hashes or []))
    parts = [sys_hash, str(user_id), today.isoformat(), ",".join(str(x) for x in sorted(picked_ids)), pdf_part]
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"plan_guide_{user_id}_{h}"


# ─── Step C / Step 5: 담은 공고의 PDF 분석 결과 로드 + 압축 ──────────────
# Step 5 부터 사용자 업로드 + 자동 fetch(announcement_auto_attachments) 모두 합쳐서 반환.
# file_hash 로 dedup (사용자가 업로드한 PDF 가 자동 fetch 와 일치하면 한 번만).
def _load_analyses_for_announcement(
    conn: sqlite3.Connection,
    ann_id: int,
    user_id: int,
) -> list[tuple[str, str, dict]]:
    """공고 id 에 연결된 PDF 분석 결과들 (사용자 직접 첨부 + 자동 fetch 합본).

    Returns: [(file_hash, original_filename, analysis_dict), ...] — dedup by file_hash
    """
    from .analyzer.storage import load_analysis

    # source 별 (hash, filename) 수집 → 마지막에 hash 로 dedup
    candidates: list[tuple[str, str, str]] = []  # (file_hash, filename, source: 'user'|'auto')

    # 1) 사용자가 직접 첨부한 PDF (기존 동작 유지)
    folders = conn.execute(
        "SELECT id FROM attachment_folders WHERE announcement_id=? AND user_id=?",
        (ann_id, user_id),
    ).fetchall()
    if folders:
        folder_ids = tuple(f["id"] for f in folders)
        placeholders = ",".join("?" * len(folder_ids))
        for r in conn.execute(
            f"SELECT file_hash, original_filename FROM uploaded_attachments "
            f"WHERE folder_id IN ({placeholders}) AND user_id=?",
            (*folder_ids, user_id),
        ).fetchall():
            if r["file_hash"]:
                candidates.append((r["file_hash"], r["original_filename"] or "", "user"))

    # 2) 자동 fetch (Step 4) — done 상태만, 시스템 공유
    for r in conn.execute(
        "SELECT file_hash, original_filename FROM announcement_auto_attachments "
        "WHERE announcement_id=? AND status='done' AND file_hash IS NOT NULL",
        (ann_id,),
    ).fetchall():
        if r["file_hash"]:
            candidates.append((r["file_hash"], r["original_filename"] or "", "auto"))

    # 3) hash 로 dedup — 사용자 업로드 우선 (먼저 등록된 게 보존됨)
    seen: set[str] = set()
    out: list[tuple[str, str, dict]] = []
    for fh, fname, _src in candidates:
        if fh in seen:
            continue
        seen.add(fh)
        a = load_analysis(fh)
        if a:
            out.append((fh, fname, a))
    return out


# ─── Step 6: 분석된 PDF 의 일정 후보 추출 + 캘린더 통합용 ──────────────
def _load_extracted_events_for_announcement(
    conn: sqlite3.Connection,
    ann_id: int,
    user_id: int,
) -> list[dict]:
    """공고의 모든 분석(사용자 첨부 + 자동 fetch) 에서 일정 항목을 모아 dedup.

    Returns: [{title, type, date_start, date_end, time, source_files}]
    """
    analyses = _load_analyses_for_announcement(conn, ann_id, user_id)
    if not analyses:
        return []
    from .analyzer.analyzer import merge_schedule_items
    per_file = [
        (fname, (a.get("schedule") or {}).get("items") or [])
        for _h, fname, a in analyses
    ]
    merged = merge_schedule_items(per_file)
    import re as _re
    _ISO = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
    out: list[dict] = []
    for ev in merged.get("items") or []:
        # 날짜가 ISO 형식이 아니면 캘린더에 등록할 수 없으므로 제외
        # (LLM 이 "2026-MM-DD" 같은 placeholder 반환하는 케이스 차단)
        ds = (ev.get("date_start") or "")[:10]
        if not _ISO.match(ds):
            continue
        de_raw = ev.get("date_end")
        de = (de_raw or "")[:10] if de_raw else None
        if de and not _ISO.match(de):
            de = None  # end 만 깨졌으면 단일 시점으로
        # 프론트 키만 노출 (page 등 디버그 필드 제거)
        out.append({
            "title":        ev.get("title") or "",
            "type":         ev.get("type") or "other",
            "date_start":   ds,
            "date_end":     de,
            "time":         ev.get("time"),
            "source_files": ev.get("source_files") or [],
        })
    # 날짜순 정렬 (이미 merge_schedule_items 가 정렬하지만 방어적으로)
    out.sort(key=lambda x: (x.get("date_start") or "", x.get("type") or ""))
    return out


def _summarize_analyses(analyses: list[tuple[str, str, dict]]) -> str:
    """공고의 모든 PDF 분석 결과를 합쳐 LLM 컨텍스트용 짧은 요약.
    가이드 생성에 핵심인 것만: 평가 기준, 의무, 지원금 상세, 유의사항.
    schedule / required_docs 는 다른 데이터로 이미 들어가서 생략.
    """
    if not analyses:
        return ""
    from .analyzer.analyzer import format_analysis_summary
    blocks = []
    for _h, fname, a in analyses[:3]:  # 한 공고당 최대 3 PDF
        text = format_analysis_summary(a, source_name=fname)
        if text:
            blocks.append(text)
    return "\n".join(blocks)


def _dedup_picked_tasks(picked: list[dict]) -> list[dict]:
    """담은 공고들의 발급 태스크를 서류명 기준으로 dedup. 가장 빠른 due_date 우선."""
    m: dict[str, dict] = {}
    for r in picked:
        for t in r.get("issue_tasks") or []:
            name = t.get("task")
            due = (t.get("due_date") or "")[:10]
            if not name or not due:
                continue
            cur = m.get(name)
            if not cur or due < cur["due"]:
                m[name] = {
                    "task": name,
                    "due": due,
                    "priority": t.get("priority") or "normal",
                    "related_titles": [r.get("title")],
                }
            else:
                if r.get("title") not in cur["related_titles"]:
                    cur["related_titles"].append(r.get("title"))
    return sorted(m.values(), key=lambda x: x["due"])


def generate_action_guide(
    user_id: int,
    plan: dict,
    picked_ids: list[int],
    *,
    use_cache: bool = True,
) -> dict:
    """담은 공고 기반 시간 구간별 액션 가이드.

    Returns: {"sections": [...], "key_warning": "...", "cached": bool, "picked_count": int}
    """
    from .analyzer.llm.base import LLMError
    from .analyzer.llm.registry import get_llm_provider
    from .analyzer.storage import load_derived, save_derived

    recs = plan.get("recommendations") or []
    picked_id_set = set(int(x) for x in picked_ids)
    picked = [r for r in recs if int(r.get("announcement_id", 0)) in picked_id_set]
    if not picked:
        return {"sections": [], "key_warning": "", "cached": False, "picked_count": 0}

    today = date.today()
    # PDF 분석 결과 미리 로드 → 캐시 키에 hash 포함 (PDF 추가/삭제 시 자동 invalidate)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        profile = _load_profile(conn, user_id)
        # ann_id → [(file_hash, fname, analysis_dict), ...]
        analyses_by_ann: dict[int, list[tuple[str, str, dict]]] = {}
        all_hashes: list[str] = []
        for r in picked:
            ann_id = int(r["announcement_id"])
            anal = _load_analyses_for_announcement(conn, ann_id, user_id)
            if anal:
                analyses_by_ann[ann_id] = anal
                all_hashes.extend(a[0] for a in anal)
    finally:
        conn.close()

    cache_key = _guide_cache_key(user_id, [r["announcement_id"] for r in picked], today, all_hashes)
    if use_cache:
        cached = load_derived(cache_key)
        if cached and isinstance(cached, dict) and "sections" in cached:
            return {
                "sections":     cached.get("sections") or [],
                "key_warning":  cached.get("key_warning") or "",
                "cached":       True,
                "picked_count": len(picked),
                "pdf_analyzed_count": cached.get("pdf_analyzed_count", 0),
            }

    dedup_tasks = _dedup_picked_tasks(picked)

    def _docs_line(label: str, docs: list) -> str:
        return f"    {label}: {', '.join(docs) if docs else '(없음)'}"

    def _picked_card_block(r: dict) -> str:
        # 자격 매칭 reasons → "업력 ✓ (예비~3년 / 내: 0.2년)" 형태
        reasons = (r.get("profile_fit") or {}).get("reasons") or []
        fit_lines = [
            f"      · {fr.get('label', '')} {'✓' if fr.get('ok') else '✗'} ({fr.get('detail', '')})"
            for fr in reasons
        ]
        fit_block = "\n".join(fit_lines) if fit_lines else "      · (정형 자격 메타 없음)"

        # 유형/지원금/노력
        type_info = r.get("type") or {}
        type_label = f"{type_info.get('emoji', '')} {type_info.get('label', '기타')}"
        sig = r.get("signals") or {}
        amount = sig.get("amount_display") or "지원금 모름"
        effort = sig.get("effort_label") or "?"
        req_n = sig.get("req_doc_count") or 0

        # Step C: PDF 분석 결과 (있을 때만)
        ann_id = int(r["announcement_id"])
        pdf_block = ""
        if ann_id in analyses_by_ann:
            summary = _summarize_analyses(analyses_by_ann[ann_id])
            if summary:
                pdf_block = (
                    f"\n    📄 공고 PDF 분석 ({len(analyses_by_ann[ann_id])} 건):\n"
                    + "\n".join(f"      {line}" for line in summary.split("\n") if line.strip())
                )

        return (
            f"- [{r['announcement_id']}] {r.get('title', '')[:90]}\n"
            f"    마감: {(r.get('end_date') or '')[:10]} (D-{r.get('days_left')})\n"
            f"    유형: {type_label}  |  지원금: {amount}  |  노력 등급: {effort} (필요 서류 {req_n}건)\n"
            f"    자격 매칭 (점수 {(r.get('profile_fit') or {}).get('score', 0)}):\n{fit_block}\n"
            + _docs_line("✅ 보유 (재발급 불필요)", r.get("fulfilled_docs") or []) + "\n"
            + _docs_line("⚠️ 만료 (재발급 필요)",   r.get("expired_docs")   or []) + "\n"
            + _docs_line("⏰ 임박 (갱신 권장)",      r.get("expiring_docs")  or []) + "\n"
            + _docs_line("❌ 미보유 (신규 발급)",    r.get("missing_docs")   or [])
            + pdf_block
        )

    picked_cards = "\n\n".join(_picked_card_block(r) for r in picked)
    task_lines = "\n".join(
        f"- '{t['task']}' (~{t['due']}, {t['priority']}, {len(t['related_titles'])}개 공고 공통)"
        for t in dedup_tasks
    )

    user_msg = _GUIDE_USER_TMPL.format(
        today=today.isoformat(),
        profile_json=json.dumps(_profile_for_narrative(profile), ensure_ascii=False),
        n=len(picked),
        picked_cards=picked_cards,
        task_lines=task_lines or "(공통 발급 태스크 없음 — 보유 서류 충분)",
    )

    provider = get_llm_provider()
    try:
        result = provider.complete(
            system=_GUIDE_SYSTEM,
            user=user_msg,
            response_schema=_GUIDE_SCHEMA,
            max_tokens=1500,
        )
    except LLMError:
        raise
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"가이드 생성 실패: {e}") from e

    if not isinstance(result, dict):
        raise LLMError(f"가이드 응답 형식 오류: {type(result).__name__}")

    sections = result.get("sections") or []
    key_warning = result.get("key_warning") or ""

    pdf_analyzed_count = len(analyses_by_ann)
    save_derived(cache_key, {
        "sections": sections,
        "key_warning": key_warning,
        "picked_ids": sorted(picked_id_set),
        "today": today.isoformat(),
        "pdf_analyzed_count": pdf_analyzed_count,
    })
    return {
        "sections":          sections,
        "key_warning":       key_warning,
        "cached":            False,
        "picked_count":      len(picked),
        "pdf_analyzed_count": pdf_analyzed_count,
    }


# compute_profile_fit / match_announcement 가 보는 핵심 필드만.
_PLAN_PROFILE_FIELDS = (
    "business_type", "establishment_date", "region", "industry",
    "company_name", "founding_type", "interest_tags", "industry_detail",
)


# Step A: 업종 chip(industry_detail) → 분야 매칭 코드 (matcher.INDUSTRY_TAGS 키).
# 프론트의 PF_AREA_TO_TAGS 와 1:1. 기존 사용자가 profile 을 다시 저장하지 않아도
# industry_detail 에서 자동 추론되어 분야 매칭이 즉시 동작.
_AREA_TO_TAGS: dict[str, list[str]] = {
    "IT·소프트웨어":   ["it_ai"],
    "제조·하드웨어":   ["manufacturing"],
    "의료·바이오":     ["bio_medical"],
    "서비스업":        ["commerce_service"],
    "유통·이커머스":   ["commerce_service"],
    "식음료·외식":     ["agri_food"],
    "콘텐츠·미디어":   ["content_culture"],
    "교육":            ["education"],
    "금융·핀테크":     ["fintech"],
    "친환경·에너지":   ["env_energy"],
    "관광·여행":       ["content_culture"],
    "기타":            [],
}


def _norm_area_key(s: str) -> str:
    """업종 칩 텍스트 정규화 — `·`, `/`, 공백, 대소문자 제거."""
    return re.sub(r"[·/\s]+", "", s or "").lower()


# 정규화 키로도 조회 가능하게 미리 인덱스 구축
_AREA_TO_TAGS_NORM: dict[str, list[str]] = {
    _norm_area_key(k): v for k, v in _AREA_TO_TAGS.items()
}


def _infer_interest_tags_from_profile(profile: Optional[dict]) -> list[str]:
    """interest_tags 가 비어 있으면 industry_detail (콤마 구분 업종 칩) 에서 추론.

    이전 버전에선 interest_tags 가 별도 chip 이었지만, UI 통합 후엔 업종 chip 만
    유지하고 매칭 코드는 자동 매핑. 기존 사용자(이미 업종 선택해둔)는 다시 저장하지
    않아도 이 fallback 으로 분야 매칭이 작동한다.

    industry_detail 의 표기가 `IT·소프트웨어` 든 `IT/소프트웨어` 든 정규화하여 매칭.
    """
    if not profile:
        return []
    tags = normalize_interest_tags(profile.get("interest_tags"))
    if tags:
        return tags
    detail = profile.get("industry_detail") or ""
    if not isinstance(detail, str):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for area in [s.strip() for s in detail.split(",") if s.strip()]:
        for tag in _AREA_TO_TAGS_NORM.get(_norm_area_key(area), []):
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out


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

    weights (0~1, default 0.5 / 0.3 / 0.5 / 0.7):
      - amount:   지원금 규모 중요도 (1=고액 위주, 0=무시)
      - effort:   노력 회피 정도 (1=쉬운 거 위주, 0=노력 무관)
      - urgency:  마감 임박 우선 (1=급한 거 위주, 0=무관)
      - industry: 관심 분야 매칭 (1=내 분야 강력 우선, 0=분야 무시) — Step A

    점수 공식:
      combined = profile_fit
               + urgency_score  * w_urgency
               + amount_score   * w_amount
               - effort_score   * w_effort
               + industry_score * w_industry
    """
    weights = weights or {}
    w_amount   = max(0.0, min(1.0, float(weights.get("amount",   0.5))))
    w_effort   = max(0.0, min(1.0, float(weights.get("effort",   0.3))))
    w_urgency  = max(0.0, min(1.0, float(weights.get("urgency",  0.5))))
    w_industry = max(0.0, min(1.0, float(weights.get("industry", 0.7))))

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

    # Step A: 사용자의 관심 분야 — 별도 interest_tags 컬럼이 비어 있으면
    # 업종 chip (industry_detail) 에서 자동 추론. 기존 사용자도 즉시 매칭 작동.
    interest_tags = _infer_interest_tags_from_profile(profile)

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

        # Step A: 분야 매칭 (키워드 기반, LLM 없이)
        industry_fit = compute_industry_fit(
            interest_tags,
            title=a.get("title"),
            content_text=a.get("content_text"),
            raw_meta=raw_meta,
        )
        industry_score = industry_fit["score"]

        # light effort: 서류 수 모르므로 type 기반만. Top N detail 단계에서 보정.
        effort_score_light = _estimate_effort(0, type_info["code"])

        combined_light = (
            int(fit["score"])
            + int(urgency_score * w_urgency)
            + int(amount_score  * w_amount)
            - int(effort_score_light * w_effort)
            + int(industry_score * w_industry)
        )
        signals_light = {
            "profile":         int(fit["score"]),
            "urgency":         urgency_score,
            "amount":          amount_score,
            "effort":          effort_score_light,
            "industry":        industry_score,
            "industry_tags":   industry_fit["matched_tags"],
            "industry_hits":   industry_fit["hit_keywords"],
            "amount_won":      won,
            "amount_display":  amt_display,
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
            + int(signals["urgency"]  * w_urgency)
            + int(signals["amount"]   * w_amount)
            - int(effort_score        * w_effort)
            + int(signals["industry"] * w_industry)
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

    # Step 6: 분석된 PDF 의 일정 항목을 각 recommendation 에 첨부.
    # 이미 분석 완료된 공고만 채워지고, 미완료/없음은 빈 배열.
    # 프론트가 _buildPlanTimeline 에 이 데이터를 흘려 캘린더 후보로 활용.
    conn2 = sqlite3.connect(DB_PATH)
    conn2.row_factory = sqlite3.Row
    try:
        for r in recommendations:
            r["extracted_events"] = _load_extracted_events_for_announcement(
                conn2, int(r["announcement_id"]), user_id,
            )
    finally:
        conn2.close()

    # Step 5: Top N 공고에 대해 백그라운드 auto-fetch 시작 (이미 시도된 공고는 skip).
    # 사용자가 "+ 담기" 누를 즈음엔 분석 완료되어 가이드에 즉시 반영되도록.
    _maybe_trigger_auto_fetch_async([int(r["announcement_id"]) for r in recommendations])

    return {
        "generated_at": datetime.now().isoformat(),
        "user_id": user_id,
        "profile_completed": bool(profile),
        "my_docs_count": len(my_docs),
        "candidate_pool": len(anns),
        "eligible_pool": len(scored),
        "top_n": top_n,
        "business_type": bt,
        "weights": {
            "amount":   w_amount,
            "effort":   w_effort,
            "urgency":  w_urgency,
            "industry": w_industry,
        },
        "interest_tags": interest_tags,
        "recommendations": recommendations,
    }


# ─── Step 5: Top N 자동 fetch 백그라운드 트리거 ──────────────────────────
def _maybe_trigger_auto_fetch_async(ann_ids: list[int]) -> None:
    """Top N 공고 각각에 대해 background thread 로 auto-fetch 시작.

    - 이미 한 번이라도 fetch 시도(announcement_auto_attachments 에 row 존재) 되었으면 skip
    - 모든 예외는 무시 (백그라운드)
    - daemon=True 라 서버 종료 시 함께 종료
    - sqlite 는 connection 분리 시 thread-safe 이므로 각 worker 마다 새 connection
    """
    import threading

    def _worker(aid: int) -> None:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                # 시도 이력 있으면 skip — 재시도는 사용자가 명시적으로 /auto-fetch POST 해야
                row = conn.execute(
                    "SELECT 1 FROM announcement_auto_attachments WHERE announcement_id=? LIMIT 1",
                    (aid,),
                ).fetchone()
                if row:
                    return
                from .auto_fetcher import fetch_and_analyze_announcement
                fetch_and_analyze_announcement(conn, aid, max_files=5)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — 백그라운드, 모든 에러 무시
            pass

    for aid in ann_ids:
        threading.Thread(target=_worker, args=(aid,), daemon=True).start()
