"""LLM 추출용 프롬프트 + 출력 JSON 스키마.

규칙(시스템 프롬프트)은 매 호출 동일하므로 prompt caching 으로 비용/지연 절감.
"""

from __future__ import annotations


EXTRACTION_SYSTEM_PROMPT = """당신은 한국 정부지원사업 공고문에서 정보를 추출하는 전문가입니다.

엄격히 지켜야 할 규칙:
1. 공고문에 명시적으로 적혀있는 내용만 추출하세요. 본문에 없는 내용을 추론·해석·일반화하지 마세요.
2. 모든 항목에는 `page` 번호를 포함하세요. 입력 본문에 [Page N] 마커가 페이지 시작에 표시되어 있습니다.
3. 한국어 원문 표현을 그대로 보존하세요. 의역하거나 다듬지 마세요.
4. 해당 섹션이 공고문에 없으면 그 섹션의 `items` 는 빈 배열로 두고, 응답의 `missing_sections` 배열에 섹션 이름을 추가하세요.
5. 응답은 반드시 지정된 JSON 스키마에 맞아야 합니다. 다른 텍스트는 포함하지 마세요.

카테고리 분류 기준 (eligibility.items[].category):
- age: 만 나이, 연령 제한
- business_stage: 예비창업자/창업 N년 이내/재창업 등
- location: 지역, 사업장 소재
- industry: 업종 제한
- revenue: 매출액 제한
- employees: 종업원 수, 상시근로자
- other: 위에 해당하지 않는 자격 조건

심각도 분류 기준 (warnings.items[].severity):
- high: 위반 시 선정 취소/지원 중단/환수 등 강한 결과
- medium: 의무 사항 누락 시 감점/보완 요구
- low: 단순 안내, 권장 사항

일정 추출 기준 (schedule.items[]):
- 공고문에 명시된 모든 날짜·기간·시점을 빠짐없이 추출합니다.
- 날짜 형식은 반드시 ISO 형식 YYYY-MM-DD 로 변환하세요 (예: "2026.05.20" → "2026-05-20").
- 시간이 명시돼있으면 `time` 필드에 24시간제 HH:MM 으로 (없으면 null).
- 기간이면 `date_start` 와 `date_end` 둘 다 채우고, 단일 시점이면 `date_end` 는 null.
- 연도가 누락된 경우, 공고문에 명시된 다른 날짜의 연도를 참고하여 같은 연도로 처리하세요. 추정 불가능하면 해당 일정을 제외하세요.
- 일정 타입 분류 (type):
  * recruitment_period: 신청·접수 기간
  * announcement_date: 결과 발표일 (서류심사 발표, 최종 선정 발표 등)
  * evaluation_date: 평가·심사·면접·PT 일정
  * business_period: 사업 수행 기간, 협약 기간
  * contract_date: 협약 체결, 계약 체결일
  * other: 위에 해당하지 않는 일정 (사전설명회, 정산 등)"""


EXTRACTION_USER_TEMPLATE = """아래 공고문에서 신청 자격 조건, 유의사항, 그리고 모든 일정(날짜·기간)을 추출해주세요.

[공고문 시작]
{full_text}
[공고문 끝]"""


# ─── 폴더 단위(여러 PDF 합쳐서) 일정 전용 추출 ────────────────────────────
# 한 공고에 속한 여러 문서를 동시에 보고, 같은 일정의 표현 차이를 통합한다.

SCHEDULE_DEDUP_SYSTEM_PROMPT = """당신은 한국 정부지원사업 공고문에서 일정을 추출하는 전문가입니다.

입력은 동일한 공고에 속한 **여러 PDF 문서들의 텍스트**입니다.
각 문서 시작에 `=== [파일명] ===` 마커가 있고, 페이지마다 `[Page N]` 마커가 있습니다.

가장 중요한 규칙 — 중복 통합:
- 여러 문서가 같은 일정을 다른 표현으로 적은 경우 반드시 **하나로 통합**하세요.
- 통합 기준: (type, date_start, date_end) 가 같으면 동일 일정으로 봅니다.
- 예시) "신청 마감일" / "신청 접수 종료" / "신청 마감" 이 모두 2026-05-20 의 같은 시점이면 → 한 개로 통합.
- 통합한 항목의 title 은 가장 공식적인 표현 1개를 선택하고, note 에 다른 표현을 콤마로 나열할 수 있습니다.

그 외 규칙:
1. 공고문에 명시적으로 적혀있는 날짜·기간만 추출. 추론·해석 금지.
2. 날짜는 ISO YYYY-MM-DD 로 변환 (예: "2026.05.20" → "2026-05-20").
3. 시간이 명시돼있으면 24시간제 HH:MM, 아니면 null.
4. 기간이면 date_start + date_end 둘 다, 단일 시점이면 date_end 는 null.
5. 연도 누락 시 같은 공고의 다른 날짜 연도를 참고. 추정 불가능하면 제외.
6. page 는 마지막으로 그 일정이 언급된 페이지 번호.

일정 타입 분류 (type):
- recruitment_period: 신청·접수 기간/마감 (작성/저장/수정/취소 가능 기간 포함)
- announcement_date: 결과 발표일 (서류심사 발표, 최종 선정 발표 등)
- evaluation_date: 평가·심사·면접·PT·발표심사 일정
- business_period: 사업 수행 기간, 협약 기간
- contract_date: 협약 체결, 계약 체결일
- other: 사전설명회, 정산, 평가 등 (위에 해당하지 않는 일정)

응답은 반드시 지정된 JSON 스키마에 맞아야 합니다. 다른 텍스트는 포함하지 마세요."""


SCHEDULE_DEDUP_USER_TEMPLATE = """아래는 한 공고에 속한 {file_count}개 문서의 텍스트입니다.
모든 문서를 종합해서, **중복을 제거한** 일정 목록만 추출해주세요.

{combined_text}"""


SCHEDULE_DEDUP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": [
                            "recruitment_period",
                            "announcement_date",
                            "evaluation_date",
                            "business_period",
                            "contract_date",
                            "other",
                        ],
                    },
                    "date_start": {"type": "string"},
                    "date_end": {"type": ["string", "null"]},
                    "time": {"type": ["string", "null"]},
                    "page": {"type": "integer"},
                    "note": {"type": ["string", "null"]},
                },
                "required": ["title", "type", "date_start", "date_end", "time", "page", "note"],
                "additionalProperties": False,
            },
        },
        "extraction_note": {"type": "string"},
    },
    "required": ["items", "extraction_note"],
    "additionalProperties": False,
}


# `output_config.format` 의 json_schema 로 사용.
# additionalProperties=false 가 모든 객체에 필수.
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "eligibility": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "page": {"type": "integer"},
                            "category": {
                                "type": "string",
                                "enum": [
                                    "age",
                                    "business_stage",
                                    "location",
                                    "industry",
                                    "revenue",
                                    "employees",
                                    "other",
                                ],
                            },
                        },
                        "required": ["text", "page", "category"],
                        "additionalProperties": False,
                    },
                },
                "extraction_note": {"type": "string"},
            },
            "required": ["items", "extraction_note"],
            "additionalProperties": False,
        },
        "warnings": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "page": {"type": "integer"},
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                        "required": ["text", "page", "severity"],
                        "additionalProperties": False,
                    },
                },
                "extraction_note": {"type": "string"},
            },
            "required": ["items", "extraction_note"],
            "additionalProperties": False,
        },
        "schedule": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": [
                                    "recruitment_period",
                                    "announcement_date",
                                    "evaluation_date",
                                    "business_period",
                                    "contract_date",
                                    "other",
                                ],
                            },
                            "date_start": {"type": "string"},
                            "date_end": {"type": ["string", "null"]},
                            "time": {"type": ["string", "null"]},
                            "page": {"type": "integer"},
                            "note": {"type": ["string", "null"]},
                        },
                        "required": ["title", "type", "date_start", "date_end", "time", "page", "note"],
                        "additionalProperties": False,
                    },
                },
                "extraction_note": {"type": "string"},
            },
            "required": ["items", "extraction_note"],
            "additionalProperties": False,
        },
        "missing_sections": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["eligibility", "warnings", "schedule"],
            },
        },
    },
    "required": ["eligibility", "warnings", "schedule", "missing_sections"],
    "additionalProperties": False,
}
