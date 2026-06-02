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


# 공고 유형 — 6개 큰 묶음 (K-Startup 11개 분류 + 비-K-Startup R&D)
ANNOUNCEMENT_TYPE_INFO: dict[str, dict[str, str]] = {
    "funding": {"emoji": "💰", "label": "자금 지원"},
    "space":   {"emoji": "🏢", "label": "입주·공간"},
    "edu":     {"emoji": "🎓", "label": "교육·멘토링"},
    "rnd":     {"emoji": "🔬", "label": "R&D·기술"},
    "global":  {"emoji": "🌏", "label": "판로·글로벌"},
    "event":   {"emoji": "🎯", "label": "행사·네트워크"},
    "other":   {"emoji": "📌", "label": "기타"},
}


def classify_announcement_type(
    raw_meta: dict | None,
    source_code: str | None,
    title: str | None = None,
    auto_type: str | None = None,
) -> dict:
    """공고 유형을 6개 큰 묶음으로 분류한다.

    우선순위:
      1. auto_type (LLM 재분류 결과, announcements.auto_type 컬럼) — 가장 정확
      2. K-Startup 의 supt_biz_clsfc 메타 기준 키워드 매칭
      3. 출처(NRF/IRIS/NTIS=R&D) 또는 제목 키워드로 추정
      4. 끝까지 모르면 '기타'
    """
    # 1) LLM 분류 결과 우선 (Phase 9)
    if auto_type and auto_type in ANNOUNCEMENT_TYPE_INFO:
        info = ANNOUNCEMENT_TYPE_INFO[auto_type]
        return {"code": auto_type, "emoji": info["emoji"], "label": info["label"]}

    code = "other"
    clsfc = ""
    if raw_meta:
        c = raw_meta.get("supt_biz_clsfc")
        if isinstance(c, str):
            clsfc = c
    if clsfc:
        if "사업화" in clsfc or "정책자금" in clsfc or "융자" in clsfc:
            code = "funding"
        elif "시설" in clsfc or "공간" in clsfc or "보육" in clsfc:
            code = "space"
        elif "멘토" in clsfc or "컨설팅" in clsfc or "교육" in clsfc:
            code = "edu"
        elif "기술개발" in clsfc or "R&D" in clsfc or "R&amp;D" in clsfc:
            code = "rnd"
        elif "판로" in clsfc or "해외" in clsfc or "글로벌" in clsfc:
            code = "global"
        elif "행사" in clsfc or "네트워크" in clsfc or "인력" in clsfc:
            code = "event"
    if code == "other" and source_code in ("nrf", "iris", "ntis"):
        code = "rnd"
    if code == "other" and title:
        t = title.lower()
        if "입주" in title or "보육센터" in title or "창업보육" in title or "공간" in title:
            code = "space"
        elif "r&d" in t or "기술개발" in title or "연구개발" in title:
            code = "rnd"
        elif "교육" in title or "멘토" in title or "컨설팅" in title or "아카데미" in title:
            code = "edu"
        elif "행사" in title or "경진대회" in title or "공모전" in title or "네트워크" in title or "포럼" in title:
            code = "event"
        elif "수출" in title or "해외" in title or "글로벌" in title or "판로" in title:
            code = "global"
    info = ANNOUNCEMENT_TYPE_INFO[code]
    return {"code": code, "emoji": info["emoji"], "label": info["label"]}


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


def _years_since(date_str: str | None, today: date) -> float | None:
    """ISO YYYY-MM-DD → 오늘까지의 햇수 (소수점 포함). 없으면 None."""
    if not date_str or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return None
    try:
        y, m, d = int(date_str[0:4]), int(date_str[5:7]), int(date_str[8:10])
        est = date(y, m, d)
    except ValueError:
        return None
    return (today - est).days / 365.25


def _check_enyy(enyy_field: str, years: float | None, is_prelaunch: bool) -> bool:
    """K-Startup biz_enyy 필드(예: '예비창업자,3년미만') 와 사용자 상태 매칭.
    어느 한 토큰이라도 매치되면 True (OR 조건)."""
    if not enyy_field:
        return True
    tokens = [t.strip() for t in enyy_field.split(",") if t.strip()]
    for tok in tokens:
        if tok == "예비창업자" and is_prelaunch:
            return True
        m = re.match(r"(\d+)\s*년\s*미만", tok)
        if m and years is not None and years < int(m.group(1)):
            return True
        m2 = re.match(r"(\d+)\s*년\s*이상", tok)
        if m2 and years is not None and years >= int(m2.group(1)):
            return True
    return False


def _check_region(regin_field: str, user_region: str | None) -> bool:
    """K-Startup supt_regin 필드와 사용자 지역 매칭.
    '전국' 포함 시 무조건 통과. 그 외엔 사용자 시·도 명이 텍스트에 포함되어야."""
    if not regin_field:
        return True
    if "전국" in regin_field:
        return True
    if not user_region:
        return False
    # '서울특별시' vs '서울'·'서울특별시' 양쪽 매칭 위해 핵심 지역명 추출
    short = re.sub(r"(특별시|광역시|특별자치시|특별자치도|도)$", "", user_region)
    return user_region in regin_field or (short and short in regin_field)


_APLY_TRGT_BIZ_TYPE = {
    "prelaunch":  {"예비창업자"},
    "individual": {"일반기업", "1인 창조기업", "1인창조기업", "개인사업자", "기업"},
    "corporate":  {"일반기업", "1인 창조기업", "1인창조기업", "법인", "기업"},
}

# LLM 추출본은 표기가 자유로워서 (예: '기관, 기업', '연구기관') 사업자 관련 토큰을 더 넓게 인식
_BIZ_RELATED_TOKENS = {
    "예비창업자", "일반기업", "1인 창조기업", "1인창조기업",
    "개인사업자", "법인", "기업", "기관",
}


def _check_aply_trgt(aply_field: str, business_type: str | None) -> bool:
    """aply_trgt 필드와 사용자 사업자 유형 매칭.
    K-Startup 표기('예비창업자','일반기업'...) 와 LLM 추출 자유 표기('기업','기관') 둘 다 인식."""
    if not aply_field or not business_type:
        return True
    tokens = {t.strip() for t in re.split(r"[,，·\s]+", aply_field) if t.strip()}
    # 사업자 관련 토큰 추출 — 정확 매칭 또는 부분 매칭
    biz_related_in_field = set()
    for t in tokens:
        if t in _BIZ_RELATED_TOKENS:
            biz_related_in_field.add(t)
            continue
        # 부분 매칭 (예: '연구기관' → 기관, '중소기업' → 기업)
        if "기업" in t:
            biz_related_in_field.add("기업")
        if "기관" in t:
            biz_related_in_field.add("기관")
    if not biz_related_in_field:
        return True
    matchers = _APLY_TRGT_BIZ_TYPE.get(business_type, set())
    # '기관' 단독은 (연구기관·공공기관) 우리 사업자 유형엔 안 맞음 — 통과 안 시킴
    if biz_related_in_field == {"기관"}:
        return False
    return bool(biz_related_in_field & matchers)


class ProfileFitResult(TypedDict):
    eligible: bool
    score: int   # 0~100
    reasons: list[dict]  # [{label, ok, detail}]


# ─── Step A: 분야(산업) 매칭 — 키워드 기반 ────────────────────────────────
#
# 자격(업력/지역/대상) 통과해도 분야가 안 맞으면 사용자는 "이게 왜 추천됐지?" 라고 느낌.
# 사용자가 자기 분야를 multi-select 로 고르고, 공고 텍스트(title+content)에 분야 키워드가
# 잡히면 가산점. LLM 없이 사전(dictionary)만으로 작동 — 즉시·비용 0.
#
# 정확도는 ~70% (동일 분야인데 키워드 표현이 다르면 miss). 정확도 향상은 Step B (LLM 분류).
#
# 키 = 분야 코드 (DB interest_tags JSON 배열에 저장). 값 = 매칭 키워드 set.

INDUSTRY_TAGS: dict[str, dict[str, object]] = {
    "it_ai": {
        "label": "IT/AI",
        "emoji": "💻",
        "keywords": {
            "AI", "인공지능", "머신러닝", "딥러닝", "ML", "LLM", "GPT", "챗봇", "ChatGPT",
            "데이터", "빅데이터", "데이터분석", "분석",
            "SaaS", "플랫폼", "소프트웨어", "앱", "어플", "모바일", "iOS", "Android",
            "웹", "웹사이트", "백엔드", "프론트엔드", "풀스택", "API",
            "클라우드", "AWS", "Azure", "GCP", "DevOps", "오픈소스",
            "블록체인", "NFT", "Web3", "메타버스", "VR", "AR", "XR",
            "사이버보안", "보안", "정보보호", "ICT", "정보통신", "디지털전환", "DX",
            "로봇", "자율주행", "IoT", "사물인터넷", "5G", "6G",
        },
    },
    "bio_medical": {
        "label": "바이오/의료",
        "emoji": "🧬",
        "keywords": {
            "바이오", "BT", "생명공학", "제약", "신약", "의약품", "백신",
            "의료", "헬스케어", "건강", "병원", "진단", "치료",
            "디지털헬스", "원격의료", "헬스테크", "의료기기", "메디컬",
            "유전체", "게놈", "줄기세포", "세포치료", "면역", "단백질",
            "임상", "전임상", "GMP", "식약처", "FDA",
        },
    },
    "manufacturing": {
        "label": "제조/소재",
        "emoji": "🏭",
        "keywords": {
            "제조", "제조업", "생산", "공장", "스마트공장", "스마트팩토리",
            "소재", "부품", "기계", "금속", "화학", "철강",
            "반도체", "디스플레이", "전자", "이차전지", "배터리",
            "3D프린팅", "적층제조", "정밀가공", "공정",
            "자동차", "조선", "항공", "우주", "방산",
        },
    },
    "content_culture": {
        "label": "콘텐츠/문화",
        "emoji": "🎬",
        "keywords": {
            "콘텐츠", "문화", "K-콘텐츠", "한류",
            "영상", "영화", "드라마", "방송", "OTT", "유튜브",
            "음악", "K-POP", "공연", "축제", "전시",
            "게임", "e스포츠", "웹툰", "웹소설", "출판", "만화", "애니메이션",
            "디자인", "UX", "UI", "브랜드", "패션",
            "관광", "여행", "체험",
        },
    },
    "agri_food": {
        "label": "농업/식품",
        "emoji": "🌾",
        "keywords": {
            "농업", "농산물", "농촌", "농가", "스마트팜", "스마트농업",
            "식품", "식자재", "외식", "푸드테크", "F&B",
            "수산", "어업", "양식", "수산물",
            "축산", "낙농", "원예", "임업",
            "친환경농산물", "유기농", "할랄", "전통식품",
        },
    },
    "env_energy": {
        "label": "환경/에너지",
        "emoji": "🌱",
        "keywords": {
            "환경", "친환경", "탄소중립", "탄소", "넷제로", "CO2",
            "에너지", "신재생", "재생에너지", "태양광", "풍력", "수소", "연료전지",
            "ESS", "전기차", "EV", "충전", "그린수소",
            "기후", "기후변화", "GreenTech", "그린", "ESG",
            "폐기물", "재활용", "리사이클", "업사이클", "순환경제",
        },
    },
    "education": {
        "label": "교육/에듀테크",
        "emoji": "📚",
        "keywords": {
            "교육", "에듀", "에듀테크", "EduTech", "이러닝", "e-러닝", "온라인교육",
            "학습", "튜터", "강의", "코딩교육", "디지털교육",
            "유아교육", "평생교육", "직업훈련", "재교육",
            "학교", "대학", "학원", "강사",
        },
    },
    "commerce_service": {
        "label": "유통/서비스",
        "emoji": "🛍️",
        "keywords": {
            "유통", "물류", "택배", "배송", "라스트마일", "풀필먼트",
            "이커머스", "전자상거래", "쇼핑", "리테일", "마켓플레이스",
            "서비스", "O2O", "공유", "공유경제", "구독", "구독경제",
            "프랜차이즈", "소상공인", "자영업",
        },
    },
    "fintech": {
        "label": "핀테크/금융",
        "emoji": "💳",
        "keywords": {
            "핀테크", "FinTech", "금융", "결제", "페이", "송금", "이체",
            "투자", "자산", "보험", "인슈어테크", "InsurTech",
            "대출", "P2P", "크라우드펀딩",
            "회계", "세무", "세금", "정산",
        },
    },
    "global_export": {
        "label": "글로벌/수출",
        "emoji": "🌏",
        "keywords": {
            "글로벌", "해외", "수출", "해외진출", "해외시장",
            "현지화", "다국적", "오버시즈",
            "북미", "유럽", "동남아", "아세안", "중국", "일본", "미국", "베트남", "인도",
            "무역", "통상", "관세", "FTA",
        },
    },
}


_INDUSTRY_CODES: set[str] = set(INDUSTRY_TAGS.keys())


def normalize_interest_tags(raw) -> list[str]:
    """프론트/DB 에서 받은 interest_tags 를 정규화.
    - 입력: list[str] 또는 JSON 문자열 또는 None
    - 출력: 유효한 코드만 (순서 보존, 중복 제거)
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in raw:
        if not isinstance(v, str):
            continue
        if v in _INDUSTRY_CODES and v not in seen:
            seen.add(v)
            out.append(v)
    return out


class IndustryFitResult(TypedDict):
    score: int                  # 0~30
    matched_tags: list[str]     # 매칭된 분야 코드 (그 공고가 어떤 사용자 분야와 매칭됐는지)
    hit_keywords: list[str]     # 매칭된 키워드 일부 (디버깅 + UI 표시용, 최대 5개)


# 미설정 사용자의 분야 점수 (중립). 기존 점수 분포를 깨지 않기 위해 중간값.
INDUSTRY_NEUTRAL_SCORE = 15


def compute_industry_fit(
    interest_tags: list[str] | None,
    *,
    title: str | None = None,
    content_text: str | None = None,
    raw_meta: dict | None = None,
) -> IndustryFitResult:
    """공고가 사용자 관심 분야와 얼마나 맞는지 0~30 점으로 계산.

    점수 규칙:
      - 사용자가 분야 미설정: 중립 15 (가산점 없음, 패널티 없음)
      - 1개 분야 매칭: 25
      - 2개 분야 매칭: 30 (만점)
      - 0개 매칭: 5 (자기 분야 키워드가 하나도 없으면 약한 감점)

    분야당 키워드 hit 가 2회 이상이면 그 분야는 "강한 매칭" 으로 인정.
    1회만 잡히면 약한 매칭 (분야 카운트엔 들지만 hit_keywords 에 추가).
    """
    tags = normalize_interest_tags(interest_tags)
    if not tags:
        return {"score": INDUSTRY_NEUTRAL_SCORE, "matched_tags": [], "hit_keywords": []}

    # 공고 텍스트 합치기 (title 가중 = 2번 등장 효과)
    title_text = (title or "").strip()
    body_text = (content_text or "")[:8000]
    # raw_meta 에서 추가 신호 (분야 분류 표시가 있는 K-Startup 일부)
    meta_text = ""
    if raw_meta:
        for k in ("supt_biz_clsfc", "biz_pbanc_nm", "intg_pbanc_biz_nm"):
            v = raw_meta.get(k)
            if isinstance(v, str):
                meta_text += " " + v
    text = (title_text + " " + title_text + " " + meta_text + " " + body_text)

    matched_tags: list[str] = []
    hit_keywords: list[str] = []
    for tag in tags:
        info = INDUSTRY_TAGS.get(tag)
        if not info:
            continue
        kws = info.get("keywords") or set()
        hits_for_tag: list[str] = []
        for kw in kws:
            # 단어 매칭 — 한글은 word boundary 가 의미 없으므로 substring 으로 검사.
            # 다만 영어 약어(AI, ML, DX, EV...) 는 대소문자 무시 + 양쪽이 영문자가 아닌 경우만 OK
            #   (예: "AI" 가 "MAIN" 에 잘못 매칭되지 않도록).
            if not kw:
                continue
            if kw.isascii() and kw.isalpha():
                # 영문 약어/단어 — 대소문자 무시 + 단어 경계
                pat = re.compile(r"(?<![A-Za-z])" + re.escape(kw) + r"(?![A-Za-z])", re.IGNORECASE)
                if pat.search(text):
                    hits_for_tag.append(kw)
            else:
                # 한글 또는 혼합 — substring
                if kw in text:
                    hits_for_tag.append(kw)
        if hits_for_tag:
            matched_tags.append(tag)
            # hit_keywords 누적 (최대 5개까지)
            for kw in hits_for_tag:
                if kw not in hit_keywords and len(hit_keywords) < 5:
                    hit_keywords.append(kw)

    n = len(matched_tags)
    if n >= 2:
        score = 30
    elif n == 1:
        score = 25
    else:
        score = 5  # 자기 분야 키워드가 하나도 없는 공고

    return {"score": score, "matched_tags": matched_tags, "hit_keywords": hit_keywords}


def compute_profile_fit(
    profile: Optional[dict],
    raw_meta: Optional[dict],
    today: Optional[date] = None,
) -> ProfileFitResult:
    """기업 프로필과 공고의 자격 메타데이터를 비교.

    profile keys: business_type, establishment_date, region, industry
    raw_meta keys (K-Startup): biz_enyy, supt_regin, aply_trgt, ...

    profile / raw_meta 가 없거나, 자격 필드가 모두 비어 있으면 eligible=True 로 통과시켜
    비로그인 / 프로필 미입력 사용자가 모든 공고를 숨김 처리되지 않도록 한다.
    """
    if not raw_meta or not profile:
        return {"eligible": True, "score": 100, "reasons": []}
    today = today or date.today()
    bt = profile.get("business_type")
    is_prelaunch = bt == "prelaunch"
    years = _years_since(profile.get("establishment_date"), today)
    user_region = profile.get("region")

    # K-Startup 메타 + LLM 추출(llm_eligibility) 합치기. K-Startup 우선.
    llm = raw_meta.get("llm_eligibility") or {}
    merged = {
        "biz_enyy":   raw_meta.get("biz_enyy")   or llm.get("biz_enyy")   or "",
        "supt_regin": raw_meta.get("supt_regin") or llm.get("supt_regin") or "",
        "aply_trgt":  raw_meta.get("aply_trgt")  or llm.get("aply_trgt")  or "",
    }

    reasons: list[dict] = []

    enyy = (merged["biz_enyy"] or "").strip()
    if enyy:
        ok = _check_enyy(enyy, years, is_prelaunch)
        if years is not None:
            user_state = f"{years:.1f}년"
        elif is_prelaunch:
            user_state = "예비창업"
        else:
            user_state = "미입력"
        reasons.append({"label": "업력", "ok": ok, "detail": f"{enyy} (내: {user_state})"})

    regin = (merged["supt_regin"] or "").strip()
    if regin:
        ok = _check_region(regin, user_region)
        reasons.append({"label": "지역", "ok": ok, "detail": f"{regin} (내: {user_region or '미입력'})"})

    aply = (merged["aply_trgt"] or "").strip()
    if aply:
        ok = _check_aply_trgt(aply, bt)
        bt_label = {"prelaunch": "예비창업자", "individual": "개인", "corporate": "법인"}.get(bt or "", "미입력")
        reasons.append({"label": "신청대상", "ok": ok, "detail": f"{aply} (내: {bt_label})"})

    if not reasons:
        return {"eligible": True, "score": 100, "reasons": []}

    eligible = all(r["ok"] for r in reasons)
    score = int(sum(r["ok"] for r in reasons) / len(reasons) * 100)
    return {"eligible": eligible, "score": score, "reasons": reasons}


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
