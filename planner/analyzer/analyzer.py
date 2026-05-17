"""PDF → 추출 → LLM 분석의 메인 오케스트레이션.

흐름:
  1. compute_file_hash (입력 PDF)
  2. load_analysis(hash) → 캐시 히트면 즉시 반환
  3. extract_pdf_text (PyMuPDF)
  4. LLM 호출 (자격조건 + 유의사항 동시 추출)
  5. 결과 검증 (필수 필드, page 번호 범위)
  6. save_analysis (캐시)
  7. 반환
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Union

from .dotenv import load_dotenv
from .extractor import compute_file_hash, extract_pdf_text
from .llm.base import LLMError, LLMProvider
from .llm.registry import get_llm_provider
from .prompts import (
    EXTRACTION_SCHEMA,
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
    SCHEDULE_DEDUP_SCHEMA,
    SCHEDULE_DEDUP_SYSTEM_PROMPT,
    SCHEDULE_DEDUP_USER_TEMPLATE,
)
from .storage import load_analysis, save_analysis, save_extract, save_pdf


# .env 는 모듈 로드 시 1회 적용
load_dotenv()


_ISO_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIME_RE = __import__("re").compile(r"^\d{2}:\d{2}$")


def _validate_analysis(result: dict, page_count: int) -> list[str]:
    """모델 응답이 스키마 외에도 page 범위·필수 키를 지키는지 점검. 문제 사유 리스트 반환."""
    issues: list[str] = []
    for section in ("eligibility", "warnings", "schedule"):
        items = result.get(section, {}).get("items") or []
        for i, item in enumerate(items):
            p = item.get("page")
            if not isinstance(p, int) or p < 1 or p > page_count:
                issues.append(f"{section}.items[{i}].page={p} 가 1..{page_count} 범위 밖")
    # schedule 항목의 날짜 형식 검증
    for i, ev in enumerate(result.get("schedule", {}).get("items") or []):
        ds = ev.get("date_start")
        de = ev.get("date_end")
        tm = ev.get("time")
        if not isinstance(ds, str) or not _ISO_DATE_RE.match(ds):
            issues.append(f"schedule.items[{i}].date_start={ds!r} 가 YYYY-MM-DD 형식 아님")
        if de is not None and (not isinstance(de, str) or not _ISO_DATE_RE.match(de)):
            issues.append(f"schedule.items[{i}].date_end={de!r} 가 YYYY-MM-DD 형식 아님")
        if tm is not None and (not isinstance(tm, str) or not _ISO_TIME_RE.match(tm)):
            issues.append(f"schedule.items[{i}].time={tm!r} 가 HH:MM 형식 아님")
    if "missing_sections" in result and not isinstance(result["missing_sections"], list):
        issues.append("missing_sections 가 리스트가 아님")
    return issues


def analyze_pdf(
    pdf_source: Union[Path, bytes],
    original_filename: Optional[str] = None,
    *,
    provider: Optional[LLMProvider] = None,
    use_cache: bool = True,
) -> dict:
    """PDF 1건을 분석한다.

    pdf_source 는 파일 경로 또는 바이트.
    이미 같은 해시의 분석 결과가 있으면 LLM 호출 없이 캐시 반환.
    """
    started = time.time()

    if isinstance(pdf_source, (bytes, bytearray)):
        pdf_bytes = bytes(pdf_source)
    else:
        pdf_bytes = Path(pdf_source).read_bytes()

    file_hash = compute_file_hash(pdf_bytes)

    if use_cache:
        cached = load_analysis(file_hash)
        # 구버전 캐시(schedule 필드 부재)는 무효 처리 → 자동 재분석
        if cached is not None and "schedule" in cached:
            cached["cache_hit"] = True
            return cached

    # 원본 PDF 도 저장 (재분석/검토 용)
    save_pdf(pdf_bytes, original_filename or f"{file_hash}.pdf")

    extracted = extract_pdf_text(pdf_bytes)
    save_extract(file_hash, extracted)

    if provider is None:
        provider = get_llm_provider()

    user_prompt = EXTRACTION_USER_TEMPLATE.format(full_text=extracted.full_text)

    try:
        llm_result = provider.complete(
            system=EXTRACTION_SYSTEM_PROMPT,
            user=user_prompt,
            response_schema=EXTRACTION_SCHEMA,
            max_tokens=4000,
        )
    except LLMError as e:
        raise

    if not isinstance(llm_result, dict):
        raise LLMError(f"Expected dict from LLM (schema was set), got {type(llm_result).__name__}")

    issues = _validate_analysis(llm_result, extracted.page_count)

    analysis = {
        "source_file": original_filename or f"{file_hash}.pdf",
        "file_hash": file_hash,
        "page_count": extracted.page_count,
        "eligibility": llm_result.get("eligibility", {"items": [], "extraction_note": ""}),
        "warnings": llm_result.get("warnings", {"items": [], "extraction_note": ""}),
        "schedule": llm_result.get("schedule", {"items": [], "extraction_note": ""}),
        "missing_sections": llm_result.get("missing_sections", []),
        "validation_issues": issues,
        "elapsed_seconds": round(time.time() - started, 2),
        "cache_hit": False,
    }

    save_analysis(file_hash, analysis)
    return analysis


def extract_schedule_consolidated(
    files: list[tuple[str, str]],  # [(filename, full_text), ...]
    *,
    provider: Optional[LLMProvider] = None,
) -> dict:
    """여러 문서의 텍스트를 하나의 LLM 호출로 합쳐서 일정만 추출(중복 제거).

    Returns: {"items": [...], "extraction_note": "...", "elapsed_seconds": float}
    """
    started = time.time()
    if not files:
        return {"items": [], "extraction_note": "no files", "elapsed_seconds": 0.0}

    parts = []
    for filename, text in files:
        parts.append(f"=== [{filename}] ===\n{text.strip()}")
    combined = "\n\n".join(parts)

    if provider is None:
        provider = get_llm_provider()

    user_prompt = SCHEDULE_DEDUP_USER_TEMPLATE.format(
        file_count=len(files),
        combined_text=combined,
    )

    llm_result = provider.complete(
        system=SCHEDULE_DEDUP_SYSTEM_PROMPT,
        user=user_prompt,
        response_schema=SCHEDULE_DEDUP_SCHEMA,
        max_tokens=4000,
    )

    if not isinstance(llm_result, dict):
        raise LLMError(f"Expected dict from LLM, got {type(llm_result).__name__}")

    items = llm_result.get("items") or []
    return {
        "items": items,
        "extraction_note": llm_result.get("extraction_note", ""),
        "elapsed_seconds": round(time.time() - started, 2),
    }
