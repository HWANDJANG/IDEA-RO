"""공고 유형 LLM 재분류.

배경:
  K-Startup 의 `supt_biz_clsfc` 메타 (사업화/시설·공간·보육/멘토링·컨설팅·교육 등) 가
  너무 광범위해서 단순 키워드 매칭으로는 정확도가 떨어진다 (특히 '사업화' 카테고리
  안에 액셀러레이팅·오픈이노베이션·바우처·컨설팅이 다 섞임).

  → 제목 + content_text + supt_biz_clsfc 을 함께 LLM 에 보내 정확한 6 카테고리로
     재분류. 결과를 announcements.auto_type 에 캐시 → 일회성 비용.

용법:
  from planner.analyzer.type_classifier import classify_type_via_llm
  code = classify_type_via_llm(title, content_text, supt_biz_clsfc)
  # → "funding" | "space" | "edu" | "rnd" | "global" | "event" | "other"
"""

from __future__ import annotations

from typing import Optional

from .dotenv import load_dotenv
from .llm.base import LLMError, LLMProvider
from .llm.registry import get_llm_provider


load_dotenv()


_TYPE_CODES = ("funding", "space", "edu", "rnd", "global", "event", "other")


_SYSTEM_PROMPT = """당신은 한국 정부지원사업 공고를 분류하는 전문가입니다.
주어진 공고를 아래 7개 카테고리 중 가장 적합한 하나로 분류하세요.

[카테고리 정의]
- funding (자금 지원): 직접 자금 지원이 핵심. 사업화자금, 바우처, 융자, 보조금, 정책자금, 투자 매칭, R&D 자금 외 일반 사업화 비용 지원
- space (입주·공간): 보육센터/창업공간/사무실 입주, 임대료 지원, 시설 사용권
- edu (교육·멘토링): 교육·아카데미·강의·세미나·컨설팅·멘토링이 핵심 (자금이 부차적)
- rnd (R&D·기술): 기술개발 과제, 연구비, R&D 협력, 기술 실증, 시제품 제작 (NRF/IRIS/NTIS 대부분 여기)
- global (판로·글로벌): 수출 지원, 해외진출, 글로벌 마케팅, 판로개척, 바이어 매칭
- event (행사·네트워크): 액셀러레이팅 프로그램, 오픈이노베이션, 배치/스튜디오, PoC 매칭, 경진대회, 공모전, 네트워킹 행사, 챌린지
- other (기타): 위 어디에도 해당 안 되거나 인재/제도/추천 등 모호한 케이스

[중요 분류 원칙]
1. K-Startup 의 supt_biz_clsfc=='사업화' 는 매우 광범위함. 제목/본문 보고 정확히 분류:
   - 액셀러레이팅/배치/오픈이노베이션/PoC/스튜디오/챌린지 → event
   - 바우처/지원금/사업화자금/투자/보조금 → funding
   - 보육/공간 → space
   - 컨설팅/멘토링/교육 → edu
   - 수출/글로벌/해외 → global
2. '경진대회'/'공모전'은 상금이 있어도 → event (행사 성격이 본질)
3. R&D 과제는 자금이 있어도 → rnd (기술개발이 본질)
4. 명확히 자금만 지원하는 경우만 funding (인재 추천/제도/지원금 외 행정 절차는 other)

[출력]
반드시 JSON 으로: {"code": "<카테고리 코드>", "reason": "<한 줄 근거 (15자 이내)>"}
"""


_USER_TMPL = """[공고 제목]
{title}

[K-Startup 분류 메타]
{clsfc}

[본문 발췌]
{content}

위 공고를 7개 카테고리 중 가장 적합한 하나로 분류하고 한 줄 근거를 답하세요."""


_SCHEMA = {
    "type": "object",
    "properties": {
        "code":   {"type": "string", "enum": list(_TYPE_CODES)},
        "reason": {"type": "string"},
    },
    "required": ["code", "reason"],
}


# 본문은 LLM 비용 절감을 위해 앞부분만 전송 (대부분 핵심 정보가 앞에 있음)
_MAX_CONTENT_CHARS = 1500


def classify_type_via_llm(
    title: str,
    content_text: Optional[str] = None,
    supt_biz_clsfc: Optional[str] = None,
    *,
    provider: Optional[LLMProvider] = None,
) -> dict:
    """공고 1건의 유형을 LLM 으로 분류.

    Returns:
        {"code": str, "reason": str}
    Raises:
        LLMError — 호출 실패 또는 잘못된 응답
    """
    if not (title or "").strip():
        raise LLMError("title 이 비어있음")

    if provider is None:
        provider = get_llm_provider()

    content = (content_text or "").strip()
    if len(content) > _MAX_CONTENT_CHARS:
        content = content[:_MAX_CONTENT_CHARS] + "...(이하 생략)"
    if not content:
        content = "(본문 없음 — 제목과 분류 메타로만 판단)"

    clsfc = (supt_biz_clsfc or "").strip() or "(없음 — 비-K-Startup 출처)"

    user_prompt = _USER_TMPL.format(
        title=title.strip(),
        clsfc=clsfc,
        content=content,
    )

    result = provider.complete(
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        response_schema=_SCHEMA,
        max_tokens=200,
    )
    if not isinstance(result, dict):
        raise LLMError(f"기대치 dict, 받은 타입 {type(result).__name__}")
    code = result.get("code")
    if code not in _TYPE_CODES:
        raise LLMError(f"잘못된 분류 코드: {code!r}")
    return {"code": code, "reason": result.get("reason") or ""}
