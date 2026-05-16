"""공고 ↔ 보유 서류 매칭.

한국 정부지원사업 공고는 본문에 요구 서류를 잘 적지 않고 첨부 공고문(PDF/HWP)
에 적어두는 경우가 많다. 따라서 본문 키워드 추출만으로는 신뢰할 수 있는 매칭이
어렵다. 본 모듈은 두 가지 전략을 결합한다:

1. **기본 프리셋**: 한국 정부지원사업이 거의 공통적으로 요구하는 서류 세트를
   사업체 유형(개인/법인)에 따라 적용한다.
2. **본문 보조 추출**: 본문에 명시적으로 언급된 서류·유효기간 패턴이 발견되면
   기본 프리셋을 override 한다.

매칭 결과는 항상 "추정"으로 표시되어야 한다 — 실제 요구 서류는 첨부파일에
있으므로 사용자가 최종 확인해야 한다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal, Optional, TypedDict

from planner.checker import (
    PreparationTask,
    RequiredDoc,
    UserDocument,
    build_preparation_schedule,
    check_document_validity,
)
from planner.document_master import DOCUMENT_MASTER


BusinessType = Literal["individual", "corporate"]


class MatchedDocument(TypedDict):
    name: str
    required_within_days: Optional[int]
    holding_status: Literal["valid", "expiring_soon", "expired", "missing", "no_expiry"]
    issued_date: Optional[str]
    message: str
    source: Literal["preset", "extracted"]


class MatchResult(TypedDict):
    announcement_id: Optional[int]
    title: Optional[str]
    deadline: Optional[str]
    business_type: BusinessType
    required_docs: list[MatchedDocument]
    summary: dict[str, int]  # {fulfilled, expiring, missing, expired}
    tasks: list[PreparationTask]
    fulfillment: Literal["complete", "partial", "none"]
    notes: list[str]   # 매칭 과정의 가정/주의사항


# 사업체 유형별 기본 요구 서류 프리셋
DEFAULT_PRESET: dict[BusinessType, list[RequiredDoc]] = {
    "individual": [
        {"name": "사업자등록증명", "required_within_days": 90},
        {"name": "납세증명서", "required_within_days": 30},
        {"name": "지방세납세증명", "required_within_days": 30},
        {"name": "주민등록등본", "required_within_days": 90},
    ],
    "corporate": [
        {"name": "사업자등록증명", "required_within_days": 90},
        {"name": "납세증명서", "required_within_days": 30},
        {"name": "지방세납세증명", "required_within_days": 30},
        {"name": "4대보험 완납증명서", "required_within_days": 30},
        {"name": "법인등기부등본", "required_within_days": 90},
        {"name": "인감증명서", "required_within_days": 90},
    ],
}


# 본문에 명시적으로 등장할 만한 서류명 (alias 포함)
_DOC_ALIASES: dict[str, str] = {
    # canonical alias -> DOCUMENT_MASTER name
    "사업자등록증명": "사업자등록증명",
    "사업자등록증": "사업자등록증명",
    "납세증명서": "납세증명서",
    "납세증명": "납세증명서",
    "국세 납세증명": "국세납세증명",
    "국세납세증명": "국세납세증명",
    "지방세 납세증명": "지방세납세증명",
    "지방세납세증명": "지방세납세증명",
    "4대보험 가입자명부": "4대보험 가입자명부",
    "4대보험 가입증명": "4대보험 가입자명부",
    "4대보험 완납증명": "4대보험 완납증명서",
    "법인 등기부등본": "법인등기부등본",
    "법인등기부등본": "법인등기부등본",
    "인감증명서": "인감증명서",
    "주민등록등본": "주민등록등본",
    "임대차계약서": "사업장 임대차계약서 사본",
}


# 유효기간 패턴: "발급 후 30일 이내", "최근 3개월 이내 발급", "발급일로부터 1개월 이내"
_PERIOD_PATTERNS = [
    re.compile(r"발급\s*(?:일자|일)?\s*(?:이후|로부터|후)?\s*(\d+)\s*(?:일|개월|달)\s*이내"),
    re.compile(r"최근\s*(\d+)\s*(?:일|개월|달)\s*이내\s*(?:에\s*)?발급"),
    re.compile(r"발급일\s*기준\s*(\d+)\s*(?:일|개월|달)\s*이내"),
]


def _period_to_days(num: int, unit_text: str) -> int:
    if "개월" in unit_text or "달" in unit_text:
        return num * 30
    return num


def extract_documents_from_text(content_text: str | None) -> dict[str, int]:
    """공고 본문에서 명시적으로 언급된 서류와 그 유효기간(일)을 추출.

    돌려주는 dict 의 키는 DOCUMENT_MASTER 의 정식 이름, 값은 유효기간(일).
    유효기간 정보를 못 찾으면 -1 을 넣어 "언급은 됐으나 기간은 미상" 으로 표시.
    """
    if not content_text:
        return {}

    found: dict[str, int] = {}
    for alias, canonical in _DOC_ALIASES.items():
        if alias not in content_text:
            continue
        idx = content_text.find(alias)
        window = content_text[idx : idx + 80]  # 같은 줄/문장에서 유효기간 찾기
        days = -1
        for pat in _PERIOD_PATTERNS:
            m = pat.search(window)
            if m:
                num = int(m.group(1))
                # 패턴이 잡은 단위 추정 — group 자체는 숫자만 있으므로 매칭 텍스트로 단위 추정
                matched_text = m.group(0)
                days = _period_to_days(num, matched_text)
                break
        # 이미 본 서류라면 기간 정보 있는 쪽을 우선
        if canonical not in found or found[canonical] == -1:
            found[canonical] = days
    return found


def build_required_docs(
    business_type: BusinessType,
    content_text: str | None = None,
    overrides: Optional[list[RequiredDoc]] = None,
) -> tuple[list[RequiredDoc], list[str]]:
    """기본 프리셋 + 본문 추출 + 사용자 override 를 합쳐 최종 요구 서류 목록을 만든다.

    Returns:
        (required_docs, notes) — notes 는 매칭 과정의 가정을 설명하는 메시지
    """
    notes: list[str] = []
    docs: dict[str, RequiredDoc] = {}

    # 1. 프리셋 적용
    for r in DEFAULT_PRESET[business_type]:
        docs[r["name"]] = dict(r)
    notes.append(
        f"{business_type} 기본 프리셋 {len(docs)}건 적용 — 실제 요구 서류는 첨부 공고문 확인 필요"
    )

    # 2. 본문에서 추가 추출
    extracted = extract_documents_from_text(content_text)
    new_from_text = []
    for name, days in extracted.items():
        if name in docs:
            # 본문에서 유효기간 명시한 경우 override
            if days > 0:
                docs[name]["required_within_days"] = days
        else:
            spec_days = DOCUMENT_MASTER.get(name, {}).get("validity_days")
            docs[name] = {
                "name": name,
                "required_within_days": days if days > 0 else spec_days,
            }
            new_from_text.append(name)
    if new_from_text:
        notes.append(f"본문에서 추가 추출: {', '.join(new_from_text)}")

    # 3. 사용자 override (제거 또는 추가)
    if overrides:
        for r in overrides:
            docs[r["name"]] = dict(r)
        notes.append(f"사용자 지정 override {len(overrides)}건 적용")

    return list(docs.values()), notes


def match_announcement(
    *,
    announcement_id: Optional[int] = None,
    title: Optional[str] = None,
    deadline: date,
    content_text: Optional[str] = None,
    business_type: BusinessType = "individual",
    user_documents: Optional[list[UserDocument]] = None,
    overrides: Optional[list[RequiredDoc]] = None,
) -> MatchResult:
    """공고 1건에 대해 보유 서류 매칭 결과를 만든다."""
    user_documents = user_documents or []
    required, notes = build_required_docs(business_type, content_text, overrides)

    user_map = {u["name"]: u for u in user_documents}
    matched: list[MatchedDocument] = []
    fulfilled = expiring = missing = expired = 0

    for req in required:
        name = req["name"]
        within = req.get("required_within_days")
        held = user_map.get(name)
        if held is None:
            # 미보유
            matched.append({
                "name": name,
                "required_within_days": within,
                "holding_status": "missing",
                "issued_date": None,
                "message": f"{name}: 보유한 서류가 없어 발급이 필요합니다.",
                "source": "preset",
            })
            missing += 1
            continue

        check = check_document_validity(name, held["issued_date"], deadline, within)
        status = check["status"]
        matched.append({
            "name": name,
            "required_within_days": within,
            "holding_status": status,
            "issued_date": held["issued_date"].isoformat(),
            "message": check["message"],
            "source": "preset",
        })
        if status == "valid" or status == "no_expiry":
            fulfilled += 1
        elif status == "expiring_soon":
            expiring += 1
        else:
            expired += 1

    tasks = build_preparation_schedule(deadline, required, user_documents or None)

    if missing + expired + expiring == 0:
        overall: Literal["complete", "partial", "none"] = "complete"
    elif fulfilled == 0:
        overall = "none"
    else:
        overall = "partial"

    return {
        "announcement_id": announcement_id,
        "title": title,
        "deadline": deadline.isoformat(),
        "business_type": business_type,
        "required_docs": matched,
        "summary": {
            "fulfilled": fulfilled,
            "expiring": expiring,
            "missing": missing,
            "expired": expired,
        },
        "tasks": tasks,
        "fulfillment": overall,
        "notes": notes,
    }
