"""ICS(아이캘린더) export — 공고 마감일과 발급 태스크를 캘린더로 변환.

표준 라이브러리만 사용해 RFC 5545 형식의 텍스트를 만든다.
import 호환 캘린더 앱: 구글 캘린더, Outlook, Apple 캘린더, Thunderbird 등.

기본 동작:
- 종일(all-day) 이벤트로 등록
- VALARM 로 마감 N일 전 알림 자동 첨부 (기본 7일/1일 전)
- 같은 데이터로 재생성하면 UID 가 같아 캘린더에서 자동 갱신됨

CLI 사용:
    python -m planner.ics_export                  # announcements.ics 생성
    python -m planner.ics_export --output x.ics --source nrf
    python -m planner.ics_export --all            # 이미 마감된 것까지 포함
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, TypedDict

# 기본 알림 시점 (마감 N일 전). 캘린더 앱이 자체 팝업/푸시를 띄움.
DEFAULT_ALARMS_DAYS = [7, 1]


class CalendarEvent(TypedDict, total=False):
    uid: str
    title: str
    description: str
    url: str
    date: date
    alarms_days_before: list[int]


# ─── 포맷팅 헬퍼 ──────────────────────────────────────────────────────────

def _fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _fmt_dtstamp_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(s: str) -> str:
    """RFC 5545 TEXT 이스케이프."""
    if not s:
        return ""
    return (
        s.replace("\\", "\\\\")
         .replace(";", "\\;")
         .replace(",", "\\,")
         .replace("\n", "\\n")
         .replace("\r", "")
    )


def _fold(line: str, byte_limit: int = 73) -> str:
    """RFC 5545 line folding — 75 바이트 초과 시 다음 줄 앞에 공백 한 칸을 두고 이어쓴다.
    UTF-8 한글이 깨지지 않게 문자 단위로 끊는다.
    """
    if len(line.encode("utf-8")) <= byte_limit:
        return line
    chunks: list[str] = []
    buf = ""
    buf_len = 0
    for ch in line:
        e = len(ch.encode("utf-8"))
        if buf_len + e > byte_limit:
            chunks.append(buf)
            buf = ch
            buf_len = e
        else:
            buf += ch
            buf_len += e
    if buf:
        chunks.append(buf)
    return "\r\n ".join(chunks)


def _hash_uid(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{h}@startup-consulting.local"


# ─── ICS 빌더 ─────────────────────────────────────────────────────────────

def build_calendar(
    events: Iterable[CalendarEvent],
    cal_name: str = "정부지원사업 일정",
) -> str:
    """CalendarEvent 들의 iterable 을 ICS 문자열로 직렬화."""
    now = _fmt_dtstamp_utc(datetime.now(timezone.utc))
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//startup-consulting//planner//KR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold(f"X-WR-CALNAME:{_escape(cal_name)}"),
    ]
    for ev in events:
        d = ev.get("date")
        if d is None:
            continue
        title = ev.get("title") or "(제목 없음)"
        uid = ev.get("uid") or _hash_uid(title, d.isoformat())
        alarms = ev.get("alarms_days_before") or DEFAULT_ALARMS_DAYS

        dtend = date.fromordinal(d.toordinal() + 1)  # 종일 이벤트의 exclusive end
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{_fmt_date(d)}",
            f"DTEND;VALUE=DATE:{_fmt_date(dtend)}",
            _fold(f"SUMMARY:{_escape(title)}"),
        ])
        if ev.get("description"):
            lines.append(_fold(f"DESCRIPTION:{_escape(ev['description'])}"))
        if ev.get("url"):
            lines.append(_fold(f"URL:{ev['url']}"))
        for days in alarms:
            lines.extend([
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                _fold(f"DESCRIPTION:{_escape(title)} ({days}일 전 알림)"),
                f"TRIGGER:-P{days}D" if days > 0 else "TRIGGER:PT0M",
                "END:VALARM",
            ])
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    # ICS 규약: CRLF 줄바꿈
    return "\r\n".join(lines) + "\r\n"


# ─── 도메인 객체 → CalendarEvent 변환 ─────────────────────────────────────

def announcement_to_event(row: dict) -> Optional[CalendarEvent]:
    """`announcements` 한 행을 이벤트로 변환.
    end_date 가 'YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM' 형식이라고 가정.
    파싱 실패하면 None.
    """
    end_text = (row.get("end_date") or "").strip()
    if not end_text:
        return None
    try:
        d = date.fromisoformat(end_text[:10])
    except ValueError:
        return None

    title = f"[마감] {row.get('title') or row.get('name') or '제목 없음'}"
    desc_parts = []
    if row.get("source_name") or row.get("source_code"):
        desc_parts.append(f"출처: {row.get('source_name') or row['source_code']}")
    if row.get("department"):
        desc_parts.append(f"주관: {row['department']}")
    if row.get("status"):
        desc_parts.append(f"상태: {row['status']}")
    if row.get("d_day"):
        desc_parts.append(f"D-day: {row['d_day']}")
    if row.get("detail_url"):
        desc_parts.append(f"\n원문: {row['detail_url']}")

    return {
        "uid": _hash_uid("announcement", str(row.get("id") or row.get("external_id")), end_text),
        "title": title,
        "description": "\n".join(desc_parts),
        "url": row.get("detail_url") or "",
        "date": d,
    }


def task_to_event(task: dict) -> CalendarEvent:
    """플래너의 PreparationTask 를 이벤트로 변환.
    due_date / earliest_date 는 date 또는 ISO 문자열 둘 다 허용.
    """
    due = task["due_date"]
    if isinstance(due, str):
        due = date.fromisoformat(due)
    earliest = task.get("earliest_date")
    if isinstance(earliest, str):
        earliest_str = earliest
    else:
        earliest_str = earliest.isoformat() if earliest else ""

    title = f"[{task['priority'].upper()}] {task['task']}"
    desc_parts = []
    if task.get("reason"):
        desc_parts.append(task["reason"])
    if earliest_str:
        desc_parts.append(f"발급 권장 시작일: {earliest_str}")

    # urgent 는 더 촘촘하게 알림
    alarms = [1, 0] if task["priority"] == "urgent" else DEFAULT_ALARMS_DAYS

    return {
        "uid": _hash_uid("task", task["task"], due.isoformat()),
        "title": title,
        "description": "\n".join(desc_parts),
        "date": due,
        "alarms_days_before": alarms,
    }


# ─── DB 직접 조회 (CLI 용) ────────────────────────────────────────────────

def fetch_announcement_events(
    db_path: Path,
    only_upcoming: bool = True,
    source: Optional[str] = None,
) -> list[CalendarEvent]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT a.*, s.code AS source_code, s.name AS source_name "
            "FROM announcements a JOIN sources s ON s.id = a.source_id "
            "WHERE a.end_date IS NOT NULL AND a.end_date != ''"
        )
        params: list = []
        if source:
            query += " AND s.code = ?"
            params.append(source)
        if only_upcoming:
            query += " AND substr(a.end_date,1,10) >= date('now')"
        query += " ORDER BY a.end_date ASC"
        rows = [dict(r) for r in conn.execute(query, params)]
    finally:
        conn.close()

    events: list[CalendarEvent] = []
    for r in rows:
        ev = announcement_to_event(r)
        if ev:
            events.append(ev)
    return events


# ─── CLI ──────────────────────────────────────────────────────────────────

def _run_cli(output_path: str, only_upcoming: bool, source: Optional[str]) -> int:
    db_path = Path(__file__).resolve().parent.parent / "announcements.db"
    if not db_path.exists():
        print(f"DB 파일을 찾을 수 없습니다: {db_path}")
        return 1

    events = fetch_announcement_events(db_path, only_upcoming=only_upcoming, source=source)
    if not events:
        print("조건에 맞는 공고가 없습니다. (--all 로 마감 지난 공고까지 포함 가능)")
        return 0

    cal_name = f"정부지원사업 마감일 ({len(events)}건)"
    ics = build_calendar(events, cal_name=cal_name)
    out = Path(output_path)
    out.write_text(ics, encoding="utf-8", newline="")
    print(f"{len(events)}건의 이벤트를 {out.resolve()} 에 저장했습니다.")
    print("구글 캘린더에서 '설정 → 가져오기' 또는 Outlook '캘린더 추가 → 파일에서'로 import 가능합니다.")
    return 0


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="공고 마감일을 ICS 캘린더로 export")
    p.add_argument("--output", default="announcements.ics", help="출력 파일 경로")
    p.add_argument("--all", action="store_true", help="이미 마감된 공고까지 포함")
    p.add_argument("--source", help="특정 source code 만 (예: nrf, bojo)")
    args = p.parse_args()
    raise SystemExit(_run_cli(args.output, only_upcoming=not args.all, source=args.source))


if __name__ == "__main__":
    main()
