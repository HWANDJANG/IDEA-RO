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
6. 입력이 [SECTION: xxx] 마커로 섹션별로 잘려서 제공되면, 그 섹션 안의 내용에 집중해서 해당 카테고리를 추출하세요. 마커가 없으면 전체 본문에서 찾으세요.

추출할 7개 카테고리:
1) eligibility — 신청 자격 (대상, 자격 요건)
2) warnings — 유의사항, 주의사항
3) schedule — 모든 날짜·기간 (모집·발표·평가·사업기간 등)
4) support_amount — 지원금/지원규모 (금액·한도)
5) required_docs — 제출/구비 서류 목록
6) evaluation — 평가/심사 기준과 배점
7) obligations — 의무사항/정산/사후관리

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
  * announcement_date: 결과 발표일
  * evaluation_date: 평가·심사·면접·PT 일정
  * business_period: 사업 수행 기간, 협약 기간
  * contract_date: 협약 체결, 계약 체결일
  * other: 위에 해당하지 않는 일정

지원금 추출 (support_amount.items[]):
- `text`: 본문 표현 그대로 (예: "기업당 최대 5천만원", "총 5억원 한도")
- `amount_max_won`: 숫자만 추출해서 원 단위 정수로 (5천만원 → 50000000). "차등 지급" 등 숫자가 없으면 null.

서류 추출 (required_docs.items[]):
- `name`: 서류 이름만 (예: "사업자등록증명")
- `note`: 단서 조건 (예: "최근 3개월 이내 발급", "해당시"). 없으면 null.

평가 기준 (evaluation.items[]):
- `criterion`: 항목 이름 (예: "사업 타당성")
- `weight`: 비중·배점 (예: "30%", "30점"). 없으면 null.

의무사항 (obligations.items[]):
- `text`: 본문 그대로 (예: "분기별 사업화 보고서 제출")
- `frequency`: 빈도 (예: "분기", "연 1회", "월별"). 일회성이거나 모르면 null."""


EXTRACTION_USER_TEMPLATE = """아래 공고문에서 7개 카테고리 정보를 추출해주세요.
입력이 [SECTION: xxx] 마커로 섹션별 분리되어 있으면 각 마커 안의 내용을 그 카테고리에 사용하세요.

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


# ─── 폴더 단위 자유 Q&A (PDF 컨텍스트 기반 대화) ──────────────────────────
# 사용자가 업로드한 공고 PDF(들)을 컨텍스트로 자유 질문을 받고 답한다.

QA_SYSTEM_PROMPT = """당신은 한국 정부지원사업 공고문 분석 전문가입니다.
사용자가 업로드한 PDF 문서들을 기반으로 정확하고 친절하게 답변합니다.

엄격히 지켜야 할 규칙:
1. 반드시 제공된 PDF 본문에 명시적으로 적혀있는 내용만 답하세요. 본문에 없는 내용을 추측·해석·일반화하지 마세요.
2. 답변 끝에 가능하면 "[출처: 파일명 p.N]" 형식으로 페이지 인용을 붙이세요. 파일별 텍스트 시작에 `=== [파일명] ===` 마커가, 각 페이지 시작에 `[Page N]` 마커가 있습니다.
3. 본문에서 답을 찾을 수 없는 질문이면 "제공된 공고 자료에서 해당 내용을 찾을 수 없습니다"라고 솔직히 답하세요. 일반 지식으로 보완하지 마세요.
4. 여러 파일에 같은 내용이 다른 표현으로 있으면 통합해서 답하되, 모든 출처 파일을 [출처]에 함께 표기하세요.
5. 한국어로 답하세요.

답변 스타일:
- 한두 문장의 핵심 결론을 먼저 제시
- 필요한 경우 불릿(•)으로 상세 항목 나열
- 숫자·금액·기간은 본문 그대로 보존
- 마지막 줄에 출처 인용
- 너무 장황하지 않게, 질문에 정확히 답하는 데 필요한 만큼만"""


QA_USER_TEMPLATE = """[공고 문서 시작]
{combined_text}
[공고 문서 끝]
{history_block}
[현재 질문]
{question}

위 공고 문서들을 근거로 답해주세요."""


# ─── 공고 다중 비교 (정밀 비교) ────────────────────────────────────────────
COMPARE_SYSTEM_PROMPT = """당신은 한국 정부지원사업 공고를 객관적으로 비교 분석하는 전문가입니다.
사용자가 2개 이상의 공고에 대해 미리 추출된 **구조화 요약**(자격·지원금·서류·평가·의무·유의·일정 카테고리별 항목)을 제공하면, 사용자가 자기 상황에 맞게 판단할 수 있도록 측면별 비교를 제공합니다.

엄격히 지켜야 할 규칙:
1. 제공된 구조화 요약에 명시적으로 적힌 내용만 사용하세요. 요약에 없는 내용은 추측·일반화하지 마세요.
2. "어느 공고가 베스트입니다", "X를 추천합니다" 같은 단정형 결론을 내리지 마세요. 대신 측면별로 "지원금은 A가 가장 큼", "의무 부담은 B가 가장 가벼움" 처럼 각 측면에서의 우열만 짚어주세요.
3. 사용자의 기업 프로필이 제공된 경우, 그 프로필 기준으로 각 공고의 자격 적격 여부를 먼저 1줄로 정리하세요. 프로필이 없거나 일부만 있으면 그 부분은 생략하세요.
4. 요약에서 확인이 안 되는 항목은 "확인 안 됨" 또는 "정보 없음" 으로 표기하세요. (요약 끝에 "PDF에서 누락된 섹션: ..." 표시가 있을 수 있음)
5. 사용자가 채팅으로 자기 우선순위·상황을 추가로 알려주면, 그 우선순위에 비추어 어느 공고가 사용자에게 더 부합하는 측면이 있는지 짚어주되, "당신에게 X가 최선"이라고 단정 짓지 말고 "당신의 우선순위 기준으로 보면 X는 ~점이 부합하고, Y는 ~점이 부합한다" 식으로 사실 기반 정리만 해주세요.
6. 한국어로 답하고, 가능한 한 출처(공고 라벨 A/B/C, 파일명)를 붙이세요.

초기 비교 출력 형식 (사용자가 처음 비교 요청 시):
- 첫 줄: "N개 공고 비교"
- 자격 적격성 (프로필이 있으면): 각 공고별 ✓/✗ 한 줄
- 측면별 비교 (각 측면별로 1~2줄, 우열을 명확히):
  • 지원금/지원 규모
  • 의무사항 (보고·정산·고용·매출 등)
  • 가점 항목
  • 신청 서류 수와 종류
  • 평가 기준
  • 그 외 PDF 에서 두드러진 차이
- 마지막: "어떤 측면이 가장 중요한지 알려주시면 그 관점에서 다시 정리해드릴 수 있습니다." 한 줄"""


COMPARE_USER_TEMPLATE = """{profile_block}[비교할 공고 목록]
{announcements_block}

[지시]
위 공고들의 자격조건, 지원 내용, 의무사항, 가점, 서류 등을 측면별로 비교해주세요.
어느 게 가장 좋다는 단정 없이, 각 측면에서 어느 공고가 우세한지만 객관적으로 짚어주세요."""


CHAT_COMPARE_USER_TEMPLATE = """{profile_block}[비교할 공고 목록]
{announcements_block}
{history_block}
[현재 질문]
{question}

위 공고 문서들을 근거로, 사용자가 자기 상황에 맞게 판단할 수 있도록 도와주세요.
"무엇이 베스트" 라고 단정 짓지 말고, 사용자의 우선순위에 비추어 어느 공고가 어떤 점에서 부합하는지 사실 기반으로 정리해주세요."""


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
        "support_amount": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},          # 본문 표현 그대로 (예: "기업당 최대 5천만원")
                            "amount_max_won": {"type": ["integer", "null"]},  # 숫자 추출 (원 단위, 모르면 null)
                            "page": {"type": "integer"},
                        },
                        "required": ["text", "amount_max_won", "page"],
                        "additionalProperties": False,
                    },
                },
                "extraction_note": {"type": "string"},
            },
            "required": ["items", "extraction_note"],
            "additionalProperties": False,
        },
        "required_docs": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},          # 서류 이름 (예: "사업자등록증명")
                            "note": {"type": ["string", "null"]},  # 단서 (예: "최근 3개월 이내")
                            "page": {"type": "integer"},
                        },
                        "required": ["name", "note", "page"],
                        "additionalProperties": False,
                    },
                },
                "extraction_note": {"type": "string"},
            },
            "required": ["items", "extraction_note"],
            "additionalProperties": False,
        },
        "evaluation": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion": {"type": "string"},     # 평가 항목 (예: "사업 타당성")
                            "weight": {"type": ["string", "null"]},  # 비중/배점 (예: "30%", "30점")
                            "page": {"type": "integer"},
                        },
                        "required": ["criterion", "weight", "page"],
                        "additionalProperties": False,
                    },
                },
                "extraction_note": {"type": "string"},
            },
            "required": ["items", "extraction_note"],
            "additionalProperties": False,
        },
        "obligations": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},          # 의무 본문 (예: "분기별 사업화 보고서 제출")
                            "frequency": {"type": ["string", "null"]},  # 빈도 (예: "분기", "연 1회", null=일회성)
                            "page": {"type": "integer"},
                        },
                        "required": ["text", "frequency", "page"],
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
                "enum": [
                    "eligibility", "warnings", "schedule",
                    "support_amount", "required_docs", "evaluation", "obligations",
                ],
            },
        },
    },
    "required": [
        "eligibility", "warnings", "schedule",
        "support_amount", "required_docs", "evaluation", "obligations",
        "missing_sections",
    ],
    "additionalProperties": False,
}
