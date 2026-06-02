"""공고 페이지 자동 fetch + 분석 백엔드.

사용자가 매칭 카드를 열거나 맞춤 플랜에서 추천받은 공고를 보기만 해도,
서버가 그 공고의 detail_url 에서 첨부 파일을 스크랩 → 다운로드 → 7카테고리
분석을 자동으로 수행한다.

흐름:
  announcements.detail_url
    → scan_attachments_from_url (K-Startup/NRF/NTIS/generic)
    → 지원 포맷만 필터 (PDF / 이미지 / HWPX)
    → 각 파일: download_to_bytes → analyze_attachment → analyses/{hash}.json
    → 결과를 announcement_auto_attachments 에 기록

특징:
- **시스템 소유** — 같은 공고를 여러 사용자가 봐도 한 번만 fetch (캐시 공유)
- **idempotent** — 이미 done 인 첨부는 skip. file_hash 캐시로 같은 PDF 재분석 0회.
- **graceful** — 한 첨부가 실패해도 다른 첨부는 계속. unsupported 포맷은 skip 기록.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from planner.analyzer.analyzer import analyze_attachment
from planner.analyzer.llm.base import LLMError
from planner.analyzer.storage import load_analysis
from planner.attach_fetcher import (
    download_to_bytes,
    scan_attachments_from_url,
    PDF_EXTS,
)


# 자동 분석을 시도할 확장자 (Step 1+2 의 분석 커버리지 + HWP + TXT 추가)
_AUTO_ANALYZE_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".hwpx", ".hwp", ".txt")
# 명시적으로 unsupported 로 기록할 확장자 (사용자에게 "지원 안 됨" 표시)
_KNOWN_UNSUPPORTED_EXTS = (".doc", ".docx", ".xls", ".xlsx", ".zip", ".alz")

_MAX_FILE_BYTES = 25 * 1024 * 1024   # 25MB — Gemini Vision/PDF 한계 고려


def _detect_format(filename: str) -> Optional[str]:
    """파일명 확장자로 분석 가능 포맷 분류. 미지원이면 None."""
    n = (filename or "").lower()
    if n.endswith(".pdf"):
        return "pdf"
    if any(n.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
        return "image"
    if n.endswith(".hwpx"):
        return "hwpx"
    if n.endswith(".hwp"):
        return "hwp"
    if n.endswith(".txt"):
        return "txt"
    return None


def _check_format_magic(fmt: str, data: bytes, content_type: Optional[str]) -> Optional[str]:
    """다운로드 바이트가 선언된 포맷과 실제로 일치하는지 매직 바이트로 확인.
    문제가 있으면 에러 메시지 반환, 없으면 None.

    정부 사이트는 form-POST/세션이 필요한 다운로드에 대해 HTML 에러 페이지를
    돌려주는 경우가 잦은데, LLM 호출까지 가기 전에 이 단계에서 차단.
    """
    ct = (content_type or "").lower()
    # HTML 응답은 모든 포맷에서 에러
    if "text/html" in ct or data[:6].lower().startswith((b"<html", b"<!doct")):
        return f"server returned HTML (content-type={content_type}) — 다운로드 URL 무효 가능"

    if fmt == "pdf":
        if not data.startswith(b"%PDF"):
            return f"not a PDF (magic={data[:8]!r}, content-type={content_type})"
        return None

    if fmt == "hwpx":
        # HWPX 는 ZIP 컨테이너 — PK\x03\x04 또는 PK\x05\x06 시작
        if not (data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06")):
            return f"not a valid HWPX/ZIP (magic={data[:8]!r}, content-type={content_type})"
        return None

    if fmt == "hwp":
        # HWP5 는 OLE2 Compound Document — D0 CF 11 E0 A1 B1 1A E1 매직
        if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return f"not a valid HWP/OLE2 (magic={data[:8]!r}, content-type={content_type})"
        return None

    if fmt == "image":
        # JPEG: FF D8 FF, PNG: 89 50 4E 47, WEBP: RIFF....WEBP, GIF: 47 49 46 38
        if data.startswith(b"\xff\xd8\xff"):           return None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):     return None
        if data.startswith(b"RIFF") and b"WEBP" in data[:16]: return None
        if data.startswith(b"GIF8"):                   return None
        return f"not a valid image (magic={data[:8]!r}, content-type={content_type})"

    if fmt == "txt":
        # TXT 는 별도 magic 없음 — bynary blob 인지만 가볍게 검사.
        # 처음 1KB 에서 NUL 바이트가 많으면 텍스트 아닐 가능성.
        sample = data[:1024]
        if not sample:
            return "empty file"
        nul_ratio = sample.count(b"\x00") / max(1, len(sample))
        if nul_ratio > 0.05:
            return f"not a valid text (binary content, content-type={content_type})"
        return None

    return None


def _upsert_auto_attachment(
    conn: sqlite3.Connection,
    ann_id: int,
    source_url: str,
    filename: str,
) -> int:
    """첨부 1건을 등록(또는 기존 row 의 id 조회). status=pending 으로 시작.

    Race-safe: 두 스레드가 동시에 같은 (ann_id, source_url) 처리해도
    INSERT OR IGNORE 가 충돌을 흡수하고 SELECT 로 동일 id 를 돌려준다.
    """
    conn.execute(
        "INSERT OR IGNORE INTO announcement_auto_attachments "
        "(announcement_id, source_url, original_filename, status) "
        "VALUES (?, ?, ?, 'pending')",
        (ann_id, source_url, filename),
    )
    row = conn.execute(
        "SELECT id FROM announcement_auto_attachments "
        "WHERE announcement_id=? AND source_url=?",
        (ann_id, source_url),
    ).fetchone()
    return int(row["id"])


def _mark_done(
    conn: sqlite3.Connection,
    row_id: int,
    file_hash: str,
    file_format: str,
) -> None:
    conn.execute(
        "UPDATE announcement_auto_attachments SET "
        "file_hash=?, file_format=?, status='done', "
        "fetched_at=CURRENT_TIMESTAMP, analyzed_at=CURRENT_TIMESTAMP, "
        "error_message=NULL, skipped_reason=NULL "
        "WHERE id=?",
        (file_hash, file_format, row_id),
    )


def _mark_skipped(conn: sqlite3.Connection, row_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE announcement_auto_attachments SET "
        "status='skipped', skipped_reason=?, error_message=NULL "
        "WHERE id=?",
        (reason, row_id),
    )


def _mark_failed(conn: sqlite3.Connection, row_id: int, error: str) -> None:
    conn.execute(
        "UPDATE announcement_auto_attachments SET "
        "status='failed', error_message=? WHERE id=?",
        (error[:500], row_id),
    )


def list_auto_attachments(
    conn: sqlite3.Connection,
    ann_id: int,
    *,
    include_analysis: bool = True,
) -> list[dict]:
    """공고에 대해 현재 DB 에 기록된 자동 수집 첨부 목록을 돌려준다.
    fetch 트리거 안 함 (조회 전용).
    """
    rows = conn.execute(
        "SELECT id, source_url, original_filename, file_hash, file_format, "
        "status, error_message, skipped_reason, fetched_at, analyzed_at "
        "FROM announcement_auto_attachments WHERE announcement_id=? "
        "ORDER BY id",
        (ann_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = {
            "id":              int(r["id"]),
            "source_url":      r["source_url"],
            "original_filename": r["original_filename"],
            "file_hash":       r["file_hash"],
            "file_format":     r["file_format"],
            "status":          r["status"],
            "error_message":   r["error_message"],
            "skipped_reason":  r["skipped_reason"],
            "fetched_at":      r["fetched_at"],
            "analyzed_at":     r["analyzed_at"],
        }
        if include_analysis and r["file_hash"] and r["status"] == "done":
            d["analysis"] = load_analysis(r["file_hash"])
        out.append(d)
    return out


def fetch_and_analyze_announcement(
    conn: sqlite3.Connection,
    ann_id: int,
    *,
    max_files: int = 10,
    force: bool = False,
) -> dict:
    """공고 1건의 자동 fetch + 분석을 수행. 동기 호출.

    Args:
      ann_id:      announcements.id
      max_files:   한 공고에서 시도할 최대 파일 수 (정부 공고는 보통 3~7개)
      force:       True 면 done 인 첨부도 재분석 (기본 False — 캐시 우선)

    Returns:
      {
        'announcement_id': int,
        'detail_url':     str,
        'source':         str,                   # kstartup/nrf/ntis/iris/unknown
        'total_found':    int,                   # 페이지에서 발견된 첨부 개수
        'attempted':      int,                   # 이번 호출에서 시도한 개수
        'newly_analyzed': int,                   # 신규 LLM 호출 발생 개수
        'cache_hits':     int,                   # 같은 file_hash 이미 분석돼 캐시 hit
        'skipped':        int,                   # unsupported / too large
        'failed':         int,                   # 다운로드/분석 실패
        'attachments':    [list_auto_attachments() 의 형태와 동일],
        'warning':        str|None,              # 스크랩 단계 경고 (예: IRIS JS 차단)
      }
    """
    # 1) 공고 정보 로드
    row = conn.execute(
        "SELECT detail_url FROM announcements WHERE id=?", (ann_id,)
    ).fetchone()
    if not row:
        return {
            "announcement_id": ann_id, "detail_url": None, "source": "unknown",
            "total_found": 0, "attempted": 0, "newly_analyzed": 0, "cache_hits": 0,
            "skipped": 0, "failed": 0, "attachments": [], "error": "공고를 찾을 수 없음",
        }
    detail_url = row["detail_url"]
    if not detail_url:
        return {
            "announcement_id": ann_id, "detail_url": None, "source": "unknown",
            "total_found": 0, "attempted": 0, "newly_analyzed": 0, "cache_hits": 0,
            "skipped": 0, "failed": 0, "attachments": [], "error": "detail_url 없음",
        }

    # 2) 페이지 스크랩
    scan = scan_attachments_from_url(detail_url)
    source_code = scan.get("source", "unknown")
    files = scan.get("files") or []
    warning = scan.get("warning")

    counters = {"attempted": 0, "newly_analyzed": 0, "cache_hits": 0, "skipped": 0, "failed": 0}

    # 3) 각 첨부 처리 (idempotent)
    for file_info in files[:max_files]:
        name = (file_info.get("name") or "").strip()
        url = (file_info.get("url") or "").strip()
        if not name or not url:
            continue

        row_id = _upsert_auto_attachment(conn, ann_id, url, name)

        # 이미 done 이고 force 아니면 skip
        existing = conn.execute(
            "SELECT status, file_hash FROM announcement_auto_attachments WHERE id=?",
            (row_id,),
        ).fetchone()
        if not force and existing and existing["status"] == "done":
            counters["cache_hits"] += 1
            continue

        # 포맷 확인
        fmt = _detect_format(name)
        if fmt is None:
            # 명시적 unsupported
            if any(name.lower().endswith(e) for e in _KNOWN_UNSUPPORTED_EXTS):
                _mark_skipped(conn, row_id, "unsupported_format")
            else:
                _mark_skipped(conn, row_id, "unknown_format")
            counters["skipped"] += 1
            counters["attempted"] += 1
            continue

        counters["attempted"] += 1

        # 다운로드 — detail_url 을 Referer 로 (NTIS 등 정부 사이트가 요구)
        try:
            file_bytes, content_type = download_to_bytes(
                url, max_bytes=_MAX_FILE_BYTES, referer=detail_url,
            )
        except ValueError as e:
            # 크기 초과
            _mark_skipped(conn, row_id, f"too_large: {e}")
            counters["skipped"] += 1
            continue
        except Exception as e:  # noqa: BLE001 — 네트워크/HTTP/etc
            _mark_failed(conn, row_id, f"download_failed: {e}")
            counters["failed"] += 1
            continue

        if not file_bytes:
            _mark_failed(conn, row_id, "empty file")
            counters["failed"] += 1
            continue

        # 매직 바이트 사전 검증 — 정부 사이트가 가끔 HTML 에러 페이지를 첨부 링크로 응답
        # (예: NTIS 의 form-POST 필요 케이스, 세션 만료 등). LLM 호출 전에 정확히 차단.
        magic_error = _check_format_magic(fmt, file_bytes, content_type)
        if magic_error:
            _mark_failed(conn, row_id, magic_error)
            counters["failed"] += 1
            continue

        # 분석 (캐시 hit 시 즉시 반환)
        try:
            analysis = analyze_attachment(
                file_bytes,
                original_filename=name,
                mime_type=content_type,
            )
        except LLMError as e:
            _mark_failed(conn, row_id, f"analyze_failed: {e}")
            counters["failed"] += 1
            continue
        except Exception as e:  # noqa: BLE001
            _mark_failed(conn, row_id, f"analyze_error: {e}")
            counters["failed"] += 1
            continue

        file_hash = analysis.get("file_hash")
        if analysis.get("cache_hit"):
            counters["cache_hits"] += 1
        else:
            counters["newly_analyzed"] += 1
        _mark_done(conn, row_id, file_hash, fmt)

    conn.commit()

    attachments = list_auto_attachments(conn, ann_id, include_analysis=True)
    return {
        "announcement_id": ann_id,
        "detail_url":      detail_url,
        "source":          source_code,
        "total_found":     len(files),
        **counters,
        "attachments":     attachments,
        "warning":         warning,
    }
