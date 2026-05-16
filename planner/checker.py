"""제출용 서류의 유효성 검증과 발급 일정 계산.

핵심 가설:
  사용자는 "이미 가진 서류"가 공고 마감일 기준에선 이미 만료된 경우가 많다.
  본 모듈은 (1) 보유분의 마감일 기준 유효성 판정과
            (2) 재발급/신규발급 권장 일정 생성을 담당한다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Optional, TypedDict

from planner.document_master import (
    BusinessApplicability,
    DOCUMENT_MASTER,
)

# 발급 후 사용 가능 일수 산정 시 안전 마진.
# (예: 30일짜리 서류를 마감일 직전까지 쓰지 않고 3일 여유를 남겨둔다)
SAFETY_MARGIN_DAYS = 3

# 마감일 며칠 전까지 모든 서류 준비를 끝내야 하는지 (실제 제출 여유).
SUBMISSION_BUFFER_DAYS = 7


DocumentStatus = Literal["valid", "expiring_soon", "expired", "no_expiry"]
TaskPriority = Literal["urgent", "high", "normal"]


class ValidityCheckResult(TypedDict):
    is_valid: bool
    days_remaining: int
    status: DocumentStatus
    message: str
    reissue_window: Optional[tuple[date, date]]


class PreparationTask(TypedDict):
    task: str
    due_date: date
    earliest_date: date
    priority: TaskPriority
    reason: str


class RequiredDoc(TypedDict, total=False):
    name: str
    required_within_days: Optional[int]


class UserDocument(TypedDict):
    name: str
    issued_date: date


class SkippedDocument(TypedDict):
    name: str
    applicable_to: list[BusinessApplicability]
    reason: str


def filter_by_business_type(
    required_docs: list["RequiredDoc"],
    user_business_type: Optional[BusinessApplicability],
) -> tuple[list["RequiredDoc"], list[SkippedDocument]]:
    """사업자 유형에 해당하지 않는 서류를 거르고 (kept, skipped) 를 반환.

    `user_business_type` 이 None 이면 필터링 없이 전부 그대로 통과시킨다.
    마스터에 없는 서류는 일단 통과시킨다(사용자 정의 항목 보호).
    """
    if user_business_type is None:
        return list(required_docs), []

    kept: list[RequiredDoc] = []
    skipped: list[SkippedDocument] = []
    for r in required_docs:
        name = r["name"]
        spec = DOCUMENT_MASTER.get(name)
        if spec is None:
            kept.append(r)
            continue
        applicable = list(spec.get("applicable_to") or [])
        if user_business_type in applicable or "all" in applicable:
            kept.append(r)
        else:
            skipped.append({
                "name": name,
                "applicable_to": applicable,
                "reason": (
                    f"{user_business_type} 사업자에 해당하지 않는 서류입니다 "
                    f"(applicable_to={applicable})."
                ),
            })
    return kept, skipped


def check_document_validity(
    doc_name: str,
    issued_date: date,
    submission_date: date,
    required_within_days: Optional[int] = None,
) -> ValidityCheckResult:
    """제출일(=공고 마감일) 시점에 보유 서류가 유효한지 판정한다.

    공고에서 별도로 유효기간을 지정하면(`required_within_days`) 그 값이 우선이고,
    아니면 `DOCUMENT_MASTER`의 기본 유효기간을 적용한다.
    """
    spec = DOCUMENT_MASTER.get(doc_name)

    effective_validity = required_within_days
    if effective_validity is None and spec is not None:
        effective_validity = spec.get("validity_days")

    if effective_validity is None:
        # 자체 만료 개념이 없는 서류 (사업장 임대차계약서 등)
        return {
            "is_valid": True,
            "days_remaining": 10**6,  # 사실상 무한
            "status": "no_expiry",
            "message": (
                f"{doc_name}: 만료 개념이 없는 서류입니다 "
                f"(공고에서 별도 유효기간을 요구하지 않는 한 사용 가능)."
            ),
            "reissue_window": None,
        }

    expiry_date = issued_date + timedelta(days=effective_validity)
    days_remaining = (expiry_date - submission_date).days

    if days_remaining < 0:
        status: DocumentStatus = "expired"
        is_valid = False
    elif days_remaining <= 7:
        status = "expiring_soon"
        is_valid = True
    else:
        status = "valid"
        is_valid = True

    # 권장 재발급 가능 구간: 마감 전날까지가 latest, 안전 마진을 둔 역산이 earliest.
    latest_reissue = submission_date - timedelta(days=1)
    earliest_reissue = submission_date - timedelta(
        days=max(effective_validity - SAFETY_MARGIN_DAYS, 1)
    )
    if earliest_reissue > latest_reissue:
        # 유효기간이 너무 짧아 마진 적용 시 역전되는 경우 → 마감 직전 발급
        earliest_reissue = latest_reissue

    if status == "expired":
        msg = (
            f"{doc_name}: 발급일 {issued_date} → 유효기간 {effective_validity}일이라 "
            f"{expiry_date}에 만료되었습니다. 제출일({submission_date}) 기준 "
            f"{-days_remaining}일 초과 → 재발급 필요."
        )
    elif status == "expiring_soon":
        msg = (
            f"{doc_name}: {expiry_date}에 만료됩니다. 제출일까지 "
            f"{days_remaining}일밖에 남지 않음 → 재발급 권장."
        )
    else:
        msg = (
            f"{doc_name}: {expiry_date}까지 유효합니다. "
            f"제출일까지 {days_remaining}일 여유."
        )

    return {
        "is_valid": is_valid,
        "days_remaining": days_remaining,
        "status": status,
        "message": msg,
        "reissue_window": (earliest_reissue, latest_reissue),
    }


def build_preparation_schedule(
    announcement_deadline: date,
    required_docs: list[RequiredDoc],
    user_documents: Optional[list[UserDocument]] = None,
    user_business_type: Optional[BusinessApplicability] = None,
) -> list[PreparationTask]:
    """공고 마감일에 맞춰 필요한 서류 발급 일정을 생성한다.

    - `user_business_type` 이 주어지면 마스터의 `applicable_to` 에 맞지 않는 서류는
      태스크 생성에서 제외한다. None 이면 기존처럼 모두 포함.
    - 보유분이 마감일 기준 유효(`valid`/`no_expiry`)하면 태스크를 만들지 않는다.
    - 만료/임박이면 "재발급" 태스크를 만든다.
    - 보유분이 없으면 "신규 발급" 태스크를 만든다.
    - 모든 발급 태스크의 due_date는 마감 7일 전이다 (안전 여유).
    """
    if user_business_type is not None:
        required_docs, _ = filter_by_business_type(required_docs, user_business_type)

    user_documents = user_documents or []
    user_map = {d["name"]: d for d in user_documents}

    target_completion = announcement_deadline - timedelta(days=SUBMISSION_BUFFER_DAYS)
    tasks: list[PreparationTask] = []

    for req in required_docs:
        name = req["name"]
        required_within = req.get("required_within_days")
        spec = DOCUMENT_MASTER.get(name)

        effective_validity = required_within
        if effective_validity is None and spec is not None:
            effective_validity = spec.get("validity_days")

        held = user_map.get(name)
        if held is not None:
            check = check_document_validity(
                name, held["issued_date"], announcement_deadline, required_within
            )
            if check["status"] in ("valid", "no_expiry"):
                # 보유분이 유효 → 새 태스크 불필요
                continue
            priority: TaskPriority = (
                "urgent" if check["status"] == "expired" else "high"
            )
            reason = check["message"]
            task_label = f"{name} 재발급"
        else:
            priority = "normal"
            reason = f"{name}: 보유 서류가 없어 신규 발급이 필요합니다."
            task_label = f"{name} 신규 발급"

        # earliest_date: 마감 기준으로 유효기간을 빼고 안전 마진을 더한 가장 이른 발급 가능일.
        if effective_validity is not None:
            earliest = announcement_deadline - timedelta(
                days=max(effective_validity - SAFETY_MARGIN_DAYS, 1)
            )
            if earliest > target_completion:
                earliest = target_completion
        else:
            # 만료 개념 없는 서류는 언제 발급해도 무방 → 준비기간 2주 전부터 가능 표시.
            earliest = target_completion - timedelta(days=14)

        tasks.append({
            "task": task_label,
            "due_date": target_completion,
            "earliest_date": earliest,
            "priority": priority,
            "reason": reason,
        })

    tasks.sort(key=lambda t: (t["due_date"], _priority_rank(t["priority"])))
    return tasks


_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2}


def _priority_rank(p: TaskPriority) -> int:
    return _PRIORITY_ORDER.get(p, 99)
