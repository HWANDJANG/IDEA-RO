"""LLM 기반 공고 자격 조건 추출.

NTIS / IRIS / NRF 처럼 자격 메타가 구조화되어 있지 않은 공고에 대해
content_text 본문을 LLM 에 보내 K-Startup 의 raw_meta 와 호환되는 자격
필드들을 추출한다. 결과는 raw_meta['llm_eligibility'] 에 저장되어 기존
matcher.compute_profile_fit() 의 입력으로 직접 활용된다.

추출 필드:
    biz_enyy        — '예비창업자', '1년미만,3년미만' 등 K-Startup 표기 그대로
    supt_regin      — '전국' 또는 '서울특별시' 등 시·도 (콤마 구분 다중 OK)
    aply_trgt       — '예비창업자,일반기업' 등
    eligibility_summary — 사람이 읽을 한 줄 요약

확실하지 않은 필드는 빈 문자열로 둔다. 매처는 빈 필드를 자동 통과시킨다.
"""

from __future__ import annotations

import json
from typing import Optional

from planner.analyzer.llm.base import LLMError
from planner.analyzer.llm.registry import get_llm_provider


SYSTEM_PROMPT = """당신은 한국 정부 R&D·창업 지원사업 공고를 분석해 신청 자격 조건을 구조화하는 전문가입니다.

주어진 공고 본문에서 신청 자격에 대한 단서를 찾아 JSON 으로 추출하세요.
확실하지 않거나 본문에 명시되지 않은 항목은 빈 문자열 "" 로 두세요. 추측하지 마세요.

표기 규칙:
- biz_enyy: 창업 기간을 K-Startup 표기로. 가능한 토큰: '예비창업자', '1년미만', '2년미만', '3년미만', '5년미만', '7년미만', '10년미만', 'N년이상'. 여러 개면 콤마로 join.
- supt_regin: 사업장 지역 제한. 전국 가능하면 '전국'. 특정 시·도면 정식 명칭 (예: '서울특별시', '경기도'). 콤마 구분 다중 OK.
- aply_trgt: 신청 자격 주체. 가능한 토큰: '예비창업자', '개인사업자', '일반기업', '1인 창조기업', '대학', '연구기관', '비영리법인'. 콤마 join.
- eligibility_summary: 자격 조건을 사람이 읽기 좋게 한 줄로 (50자 이내).

본문에 자격 관련 언급이 전혀 없으면 모든 필드를 빈 문자열로."""

ELIGIBILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "biz_enyy":              {"type": "string"},
        "supt_regin":            {"type": "string"},
        "aply_trgt":             {"type": "string"},
        "eligibility_summary":   {"type": "string"},
    },
    "required": ["biz_enyy", "supt_regin", "aply_trgt", "eligibility_summary"],
}


def extract_eligibility(
    content_text: str,
    title: Optional[str] = None,
    provider=None,
) -> dict:
    """공고 본문에서 자격 조건을 LLM 으로 추출.

    Returns
    -------
    dict with keys: biz_enyy, supt_regin, aply_trgt, eligibility_summary
    Empty strings for fields the model couldn't determine.

    Raises
    ------
    LLMError on provider failure (caller decides to swallow vs propagate).
    """
    if not content_text or not content_text.strip():
        return {
            "biz_enyy": "",
            "supt_regin": "",
            "aply_trgt": "",
            "eligibility_summary": "",
        }

    user_msg = ""
    if title:
        user_msg += f"[공고명] {title.strip()}\n\n"
    # 본문 너무 길면 자르기 (LLM 비용 절약)
    body = content_text.strip()
    if len(body) > 6000:
        body = body[:6000] + "\n…(생략)"
    user_msg += f"[본문]\n{body}"

    provider = provider or get_llm_provider()
    result = provider.complete(
        system=SYSTEM_PROMPT,
        user=user_msg,
        response_schema=ELIGIBILITY_SCHEMA,
        max_tokens=600,
    )

    if isinstance(result, str):
        # 일부 프로바이더가 JSON Schema 미지원 시 자유 텍스트 반환 — 파싱 시도
        try:
            result = json.loads(result)
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM 응답이 JSON 이 아님: {e}: {result[:200]}")

    # 누락 키 보정
    for k in ("biz_enyy", "supt_regin", "aply_trgt", "eligibility_summary"):
        if k not in result or not isinstance(result.get(k), str):
            result[k] = ""

    return result
