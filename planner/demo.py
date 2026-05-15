"""만료 경고 + 발급 일정 시나리오 시연.

실행:
    python -m planner.demo

세 가지 시나리오를 차례대로 출력한다.
"""

from __future__ import annotations

from datetime import date

from planner.checker import (
    PreparationTask,
    RequiredDoc,
    UserDocument,
    build_preparation_schedule,
    check_document_validity,
)


# ─── 출력 헬퍼 ────────────────────────────────────────────────────────────────

def _hr(char: str = "─", width: int = 72) -> str:
    return char * width


def _print_header(idx: int, title: str, deadline: date) -> None:
    print()
    print(_hr("═"))
    print(f"  시나리오 {idx}. {title}")
    print(f"  공고 마감일: {deadline}")
    print(_hr("═"))


def _print_required(required: list[RequiredDoc]) -> None:
    print("  · 요구 서류:")
    for r in required:
        within = r.get("required_within_days")
        within_txt = f"{within}일 이내" if within else "유효기간 없음"
        print(f"      - {r['name']} ({within_txt})")


def _print_check(
    deadline: date,
    user_docs: list[UserDocument],
    required: list[RequiredDoc],
) -> None:
    if not user_docs:
        print("  · 보유 서류 검증: 보유한 서류 없음")
        return
    print("  · 보유 서류 검증:")
    req_map = {r["name"]: r for r in required}
    status_label = {
        "valid": "유효",
        "expiring_soon": "임박",
        "expired": "만료",
        "no_expiry": "유효",
    }
    for ud in user_docs:
        req = req_map.get(ud["name"], {})
        result = check_document_validity(
            ud["name"],
            ud["issued_date"],
            deadline,
            req.get("required_within_days"),
        )
        label = status_label.get(result["status"], result["status"])
        print(f"      [{label}] {result['message']}")


def _print_tasks(tasks: list[PreparationTask]) -> None:
    if not tasks:
        print("  · 발급 태스크: 없음 (모든 서류가 유효합니다)")
        return
    print(f"  · 발급 태스크 ({len(tasks)}건, 마감일·우선순위 순):")
    for i, t in enumerate(tasks, 1):
        print(f"      {i}. [{t['priority'].upper()}] {t['task']}")
        print(
            f"         발급 권장 기간: {t['earliest_date']} ~ {t['due_date']}"
        )
        print(f"         사유: {t['reason']}")


# ─── 시나리오 ────────────────────────────────────────────────────────────────

def scenario_1() -> None:
    deadline = date(2026, 6, 30)
    required: list[RequiredDoc] = [
        {"name": "납세증명서", "required_within_days": 30},
    ]
    user_docs: list[UserDocument] = [
        {"name": "납세증명서", "issued_date": date(2026, 5, 20)},
    ]

    _print_header(1, "보유한 납세증명서가 만료 직전", deadline)
    _print_required(required)
    _print_check(deadline, user_docs, required)
    tasks = build_preparation_schedule(deadline, required, user_docs)
    _print_tasks(tasks)


def scenario_2() -> None:
    deadline = date(2026, 7, 15)
    required: list[RequiredDoc] = [
        {"name": "사업자등록증명", "required_within_days": 90},
        {"name": "납세증명서", "required_within_days": 30},
        {"name": "법인등기부등본", "required_within_days": 90},
        {"name": "4대보험 완납증명서", "required_within_days": 30},
    ]
    user_docs: list[UserDocument] = []

    _print_header(2, "법인 창업자가 모든 서류를 새로 받아야 함", deadline)
    _print_required(required)
    _print_check(deadline, user_docs, required)
    tasks = build_preparation_schedule(deadline, required, user_docs)
    _print_tasks(tasks)


def scenario_3() -> None:
    deadline = date(2026, 8, 1)
    required: list[RequiredDoc] = [
        {"name": "사업자등록증명", "required_within_days": 90},
        {"name": "납세증명서", "required_within_days": 30},
    ]
    user_docs: list[UserDocument] = [
        {"name": "사업자등록증명", "issued_date": date(2026, 7, 25)},
        {"name": "납세증명서", "issued_date": date(2026, 6, 1)},
    ]

    _print_header(3, "일부는 유효, 일부는 만료", deadline)
    _print_required(required)
    _print_check(deadline, user_docs, required)
    tasks = build_preparation_schedule(deadline, required, user_docs)
    _print_tasks(tasks)


def main() -> None:
    scenario_1()
    scenario_2()
    scenario_3()
    print()


if __name__ == "__main__":
    main()
