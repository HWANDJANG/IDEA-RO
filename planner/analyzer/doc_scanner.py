"""정부24/홈택스 등 발급내역 화면 캡처에서 서류명·발급일 자동 추출.

Gemini Vision 으로 한 번 호출. 사용자가 휴대폰으로 발급내역 화면을 찍어 올리면
서류 목록을 추출해서 "내 서류" 일괄 등록 후보로 돌려준다.
"""

from __future__ import annotations

from typing import Optional

from .llm.base import LLMError, LLMProvider
from .llm.registry import get_llm_provider


DOC_SCAN_SYSTEM_PROMPT = """당신은 한국 정부 발급 서비스(정부24, 홈택스, 4대보험, 도로명주소 등)의
발급내역/발급함 화면 캡처에서 서류 목록을 추출하는 전문가입니다.

엄격히 지켜야 할 규칙:
1. 이미지에 명시적으로 보이는 항목만 추출하세요. 추론·해석·일반화 금지.
2. 각 항목은 반드시 서류명(name)과 발급일(issued_date) 두 가지를 모두 가져야 합니다.
   둘 중 하나라도 명확히 보이지 않는 항목은 제외하세요.
3. 발급일은 반드시 ISO 형식 YYYY-MM-DD 로 변환하세요.
   - "2024-10-07 00:52:49" → "2024-10-07"
   - "2024.10.07" → "2024-10-07"
   - "24.10.07" → "2024-10-07" (2자리 연도는 2000년대로 가정)
4. 서류명은 한국어 정식 명칭을 그대로 보존하세요. 의역·요약·줄임 금지.
   예시: "주민등록표등본", "납세증명서", "건강보험자격득실확인서"
5. 화면 상단 메뉴(발급내역 1개월/3개월 등), 보관기간 같은 메타 정보는 추출하지 마세요.
6. 같은 서류가 여러 번 나오면 발급일이 다르면 각각 추출. 완전히 동일한 항목은 한 번만.
7. 응답은 반드시 지정된 JSON 스키마에 맞아야 합니다."""


DOC_SCAN_USER_PROMPT = """이 이미지는 정부 발급 서비스의 발급내역 화면입니다.
이미지에 보이는 서류 목록과 각각의 발급일을 추출해주세요."""


DOC_SCAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "issued_date": {"type": "string"},
                    "note": {"type": ["string", "null"]},
                },
                "required": ["name", "issued_date", "note"],
                "additionalProperties": False,
            },
        },
        "extraction_note": {"type": "string"},
    },
    "required": ["documents", "extraction_note"],
    "additionalProperties": False,
}


def scan_doc_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    *,
    provider: Optional[LLMProvider] = None,
) -> dict:
    """이미지를 LLM 에 보내 서류 목록 추출.

    Returns: {"documents": [{name, issued_date, note}, ...], "extraction_note": "..."}
    """
    if not image_bytes:
        raise LLMError("이미지 데이터가 비어있습니다")
    if provider is None:
        provider = get_llm_provider()

    result = provider.complete_vision(
        system=DOC_SCAN_SYSTEM_PROMPT,
        user=DOC_SCAN_USER_PROMPT,
        image_bytes=image_bytes,
        mime_type=mime_type,
        response_schema=DOC_SCAN_SCHEMA,
        max_tokens=2000,
    )

    if not isinstance(result, dict):
        raise LLMError(f"Expected dict, got {type(result).__name__}")

    return {
        "documents": result.get("documents") or [],
        "extraction_note": result.get("extraction_note", ""),
    }
