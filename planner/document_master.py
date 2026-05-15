"""정부지원사업 신청에 자주 요구되는 서류의 마스터 사전.

발급처/유효기간/온라인 발급 가능 여부 등 정적인 정보만 담는다.
"발급 후 N개월 이내" 같은 사업별 요구는 여기 들어가지 않고,
공고 단위에서 `required_within_days`로 오버라이드한다.

validity_days가 None인 서류는 자체 만료 개념이 없다.
다만 사업에 따라 "최근 N개월 이내" 발급분만 받는 경우가 있으므로,
이때는 호출 측에서 `required_within_days`를 함께 전달해야 한다.
"""

from __future__ import annotations

from typing import Optional, TypedDict


class DocumentSpec(TypedDict):
    name: str
    issuing_authority: str
    validity_days: Optional[int]
    online_issuable: bool
    typical_usage: str


DOCUMENT_MASTER: dict[str, DocumentSpec] = {
    "사업자등록증명": {
        "name": "사업자등록증명",
        "issuing_authority": "홈택스",
        "validity_days": None,
        "online_issuable": True,
        "typical_usage": "사업자 등록 사실 증빙. 일부 사업은 '최근 3개월 이내' 발급분 요구.",
    },
    "납세증명서": {
        "name": "납세증명서",
        "issuing_authority": "홈택스",
        "validity_days": 30,
        "online_issuable": True,
        "typical_usage": "체납 사실이 없음을 증빙. 정부지원사업·입찰 단골 요구.",
    },
    "국세납세증명": {
        "name": "국세납세증명",
        "issuing_authority": "홈택스",
        "validity_days": 30,
        "online_issuable": True,
        "typical_usage": "국세 완납 증빙. 납세증명서와 함께 묶여 요구되는 경우가 많음.",
    },
    "지방세납세증명": {
        "name": "지방세납세증명",
        "issuing_authority": "위택스",
        "validity_days": 30,
        "online_issuable": True,
        "typical_usage": "지방세 완납 증빙. 국세 납세증명과 한 세트로 요구됨.",
    },
    "4대보험 가입자명부": {
        "name": "4대보험 가입자명부",
        "issuing_authority": "4대사회보험정보연계센터",
        "validity_days": 30,
        "online_issuable": True,
        "typical_usage": "고용/인력 현황 증빙. 인력지원·고용창출 사업에서 자주 요구.",
    },
    "4대보험 완납증명서": {
        "name": "4대보험 완납증명서",
        "issuing_authority": "건강보험공단·국민연금공단·근로복지공단",
        "validity_days": 30,
        "online_issuable": True,
        "typical_usage": "보험료 완납 증빙. 4개 공단별 각각 발급해야 하는 경우 있음.",
    },
    "법인등기부등본": {
        "name": "법인등기부등본",
        "issuing_authority": "인터넷등기소",
        "validity_days": 90,
        "online_issuable": True,
        "typical_usage": "법인 등기 사항 증빙. 법인 사업자에 한해 요구.",
    },
    "인감증명서": {
        "name": "인감증명서",
        "issuing_authority": "정부24",
        "validity_days": 90,
        "online_issuable": True,
        "typical_usage": "본인발급분. 협약서·약정서 체결 시 요구.",
    },
    "주민등록등본": {
        "name": "주민등록등본",
        "issuing_authority": "정부24",
        "validity_days": 90,
        "online_issuable": True,
        "typical_usage": "거주지/세대 구성 증빙. 청년/지역 연계 사업에서 요구.",
    },
    "사업장 임대차계약서 사본": {
        "name": "사업장 임대차계약서 사본",
        "issuing_authority": "본인 보관",
        "validity_days": None,
        "online_issuable": False,
        "typical_usage": "사업장 소재지 증빙. 계약 기간 자체가 유효 범위 역할.",
    },
}
