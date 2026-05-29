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
from .extractor import compute_file_hash, extract_pdf_text, find_sections
from .llm.base import LLMError, LLMProvider
from .llm.registry import get_llm_provider
from .prompts import (
    COMPARE_SYSTEM_PROMPT,
    EXTRACTION_SCHEMA,
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
    QA_SYSTEM_PROMPT,
    QA_USER_TEMPLATE,
)
from .storage import load_analysis, save_analysis, save_extract, save_image, save_pdf, save_source


# .env 는 모듈 로드 시 1회 적용
load_dotenv()


_ISO_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIME_RE = __import__("re").compile(r"^\d{2}:\d{2}$")

# 분석 결과 스키마 버전. 키 추가/필드 변경 시 +1 → 기존 캐시 자동 무효화.
SCHEMA_VERSION = 2


def _validate_analysis(result: dict, page_count: int) -> list[str]:
    """모델 응답이 스키마 외에도 page 범위·필수 키를 지키는지 점검. 문제 사유 리스트 반환."""
    issues: list[str] = []
    page_sections = (
        "eligibility", "warnings", "schedule",
        "support_amount", "required_docs", "evaluation", "obligations",
    )
    for section in page_sections:
        items = (result.get(section) or {}).get("items") or []
        for i, item in enumerate(items):
            p = item.get("page")
            if not isinstance(p, int) or p < 1 or p > page_count:
                issues.append(f"{section}.items[{i}].page={p} 가 1..{page_count} 범위 밖")
    # schedule 항목의 날짜 형식 검증
    for i, ev in enumerate((result.get("schedule") or {}).get("items") or []):
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


# 섹션 카테고리 → 한글 라벨 (LLM 입력 SECTION 마커용)
_SECTION_LABELS_KO = {
    "eligibility":   "지원대상",
    "support":       "지원내용",
    "required_docs": "제출서류",
    "evaluation":    "평가기준",
    "schedule":      "일정",
    "obligations":   "의무사항",
    "warnings":      "유의사항",
    "contact":       "문의",
}

# 섹션 기반 입력 채택 임계치 — N개 이상 카테고리 + 전체 텍스트의 30% 이상 커버시 채택.
_SECTION_MIN_CATEGORIES = 3
_SECTION_MIN_COVERAGE = 0.30


def _build_section_input(full_text: str) -> tuple[str, dict]:
    """섹션 분리 시도. 충분히 잡히면 [SECTION: xxx] 마커 텍스트 반환, 부족하면 원문.

    Returns: (input_text, meta) — meta = {used_sections: bool, categories: [...], coverage: float}
    """
    if not full_text:
        return full_text, {"used_sections": False, "categories": [], "coverage": 0.0}
    sections = find_sections(full_text)
    if not sections or len(sections) < _SECTION_MIN_CATEGORIES:
        return full_text, {"used_sections": False, "categories": list(sections.keys()), "coverage": 0.0}
    coverage = sum(len(v) for v in sections.values()) / max(1, len(full_text))
    if coverage < _SECTION_MIN_COVERAGE:
        return full_text, {"used_sections": False, "categories": list(sections.keys()), "coverage": round(coverage, 3)}
    parts = []
    for cat, txt in sections.items():
        label = _SECTION_LABELS_KO.get(cat, cat)
        parts.append(f"[SECTION: {label}]\n{txt}")
    return "\n\n".join(parts), {
        "used_sections": True,
        "categories": list(sections.keys()),
        "coverage": round(coverage, 3),
    }


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
        # SCHEMA_VERSION 이 맞아야 캐시 채택. 구버전(=확장 필드 부재)은 재분석.
        if cached is not None and cached.get("schema_version") == SCHEMA_VERSION:
            cached["cache_hit"] = True
            return cached

    # 원본 PDF 도 저장 (재분석/검토 용)
    save_pdf(pdf_bytes, original_filename or f"{file_hash}.pdf")

    extracted = extract_pdf_text(pdf_bytes)
    save_extract(file_hash, extracted)

    if provider is None:
        provider = get_llm_provider()

    # 섹션 분리 시도 — 잘 잡히면 SECTION 마커 텍스트로, 아니면 원문 그대로 LLM 에 전송
    input_text, section_meta = _build_section_input(extracted.full_text)
    user_prompt = EXTRACTION_USER_TEMPLATE.format(full_text=input_text)

    try:
        llm_result = provider.complete(
            system=EXTRACTION_SYSTEM_PROMPT,
            user=user_prompt,
            response_schema=EXTRACTION_SCHEMA,
            max_tokens=5000,
        )
    except LLMError as e:
        raise

    if not isinstance(llm_result, dict):
        raise LLMError(f"Expected dict from LLM (schema was set), got {type(llm_result).__name__}")

    issues = _validate_analysis(llm_result, extracted.page_count)

    empty_section = {"items": [], "extraction_note": ""}
    analysis = {
        "source_file": original_filename or f"{file_hash}.pdf",
        "file_hash": file_hash,
        "page_count": extracted.page_count,
        "schema_version": SCHEMA_VERSION,
        "eligibility":    llm_result.get("eligibility",    dict(empty_section)),
        "warnings":       llm_result.get("warnings",       dict(empty_section)),
        "schedule":       llm_result.get("schedule",       dict(empty_section)),
        "support_amount": llm_result.get("support_amount", dict(empty_section)),
        "required_docs":  llm_result.get("required_docs",  dict(empty_section)),
        "evaluation":     llm_result.get("evaluation",     dict(empty_section)),
        "obligations":    llm_result.get("obligations",    dict(empty_section)),
        "missing_sections": llm_result.get("missing_sections", []),
        "validation_issues": issues,
        "section_extraction": section_meta,
        "elapsed_seconds": round(time.time() - started, 2),
        "cache_hit": False,
    }

    save_analysis(file_hash, analysis)
    return analysis


# ─── 이미지 분석 ─────────────────────────────────────────────────────
# Gemini Vision (multimodal) 으로 JPG/PNG 공고 캡처 1장을 직접 분석한다.
# PDF 와 동일한 7카테고리 스키마로 결과를 돌려주므로, 하류 코드 (format_analysis_summary,
# 비교 매트릭스, 맞춤 플랜 가이드) 는 별도 분기 없이 그대로 사용 가능.

_EXTRACTION_USER_TEMPLATE_IMAGE = """이 이미지는 한국 정부지원사업 공고문 페이지(또는 캡처)입니다.
이미지에 보이는 텍스트와 표를 읽어 7개 카테고리 정보를 추출해주세요.

이미지는 단일 페이지로 취급합니다. 모든 항목의 `page` 필드는 1 로 설정하세요."""


_EXTRACTION_USER_TEMPLATE_TEXT = """아래는 한국 정부지원사업 공고문에서 추출한 평문 텍스트입니다 (HWPX 등 비-PDF 소스).
[SECTION: xxx] 마커가 있으면 해당 섹션의 카테고리에 집중해서 7개 카테고리 정보를 추출해주세요.

페이지 정보는 알 수 없으므로 모든 항목의 `page` 필드는 1 로 설정하세요.

[공고문 시작]
{full_text}
[공고문 끝]"""


_IMAGE_MIME_BY_EXT = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
}


_PAGED_SECTIONS = (
    "eligibility", "warnings", "schedule",
    "support_amount", "required_docs", "evaluation", "obligations",
)


def _force_single_page(llm_result: dict) -> None:
    """단일 페이지 소스(이미지/HWPX)는 모든 items.page=1 로 정규화. in-place."""
    for section in _PAGED_SECTIONS:
        s = llm_result.get(section)
        if not isinstance(s, dict):
            continue
        for item in s.get("items") or []:
            if isinstance(item, dict):
                item["page"] = 1


def analyze_image(
    image_source: Union[Path, bytes],
    original_filename: Optional[str] = None,
    mime_type: Optional[str] = None,
    *,
    provider: Optional[LLMProvider] = None,
    use_cache: bool = True,
) -> dict:
    """이미지 1장을 분석. PDF 와 동일한 7카테고리 스키마로 반환.

    - mime_type 이 None 이면 파일명 확장자로 추정 (jpg/png/webp)
    - 같은 hash 의 분석 결과가 캐시에 있으면 LLM 호출 없이 즉시 반환
    - Gemini Vision 만 지원 (provider 미지원 시 LLMError)
    """
    started = time.time()

    if isinstance(image_source, (bytes, bytearray)):
        image_bytes = bytes(image_source)
    else:
        image_bytes = Path(image_source).read_bytes()

    file_hash = compute_file_hash(image_bytes)

    # MIME 추정 (인자 → 확장자 → 기본 jpeg)
    if not mime_type:
        ext = (Path(original_filename or "").suffix or "").lower()
        mime_type = _IMAGE_MIME_BY_EXT.get(ext, "image/jpeg")

    if use_cache:
        cached = load_analysis(file_hash)
        if cached is not None and cached.get("schema_version") == SCHEMA_VERSION:
            cached["cache_hit"] = True
            return cached

    save_image(image_bytes, mime_type)

    if provider is None:
        provider = get_llm_provider()

    try:
        llm_result = provider.complete_vision(
            system=EXTRACTION_SYSTEM_PROMPT,
            user=_EXTRACTION_USER_TEMPLATE_IMAGE,
            image_bytes=image_bytes,
            mime_type=mime_type,
            response_schema=EXTRACTION_SCHEMA,
            max_tokens=5000,
        )
    except LLMError:
        raise

    if not isinstance(llm_result, dict):
        raise LLMError(f"Expected dict from LLM (schema was set), got {type(llm_result).__name__}")

    # 이미지는 단일 페이지로 취급 — LLM 이 절 번호를 page 로 잘못 잡는 경우 정규화
    _force_single_page(llm_result)
    issues = _validate_analysis(llm_result, page_count=1)

    empty_section = {"items": [], "extraction_note": ""}
    analysis = {
        "source_file":   original_filename or f"{file_hash}{Path(original_filename or '.jpg').suffix or '.jpg'}",
        "source_type":   "image",
        "source_mime":   mime_type,
        "file_hash":     file_hash,
        "page_count":    1,
        "schema_version": SCHEMA_VERSION,
        "eligibility":    llm_result.get("eligibility",    dict(empty_section)),
        "warnings":       llm_result.get("warnings",       dict(empty_section)),
        "schedule":       llm_result.get("schedule",       dict(empty_section)),
        "support_amount": llm_result.get("support_amount", dict(empty_section)),
        "required_docs":  llm_result.get("required_docs",  dict(empty_section)),
        "evaluation":     llm_result.get("evaluation",     dict(empty_section)),
        "obligations":    llm_result.get("obligations",    dict(empty_section)),
        "missing_sections":  llm_result.get("missing_sections", []),
        "validation_issues": issues,
        "section_extraction": {"used_sections": False, "categories": [], "coverage": 0.0},
        "elapsed_seconds":    round(time.time() - started, 2),
        "cache_hit":          False,
    }

    save_analysis(file_hash, analysis)
    return analysis


# ─── 텍스트 분석 (HWPX, 일반 텍스트) ─────────────────────────────────
# HWPX 처럼 원본은 바이너리 ZIP 이지만 본문 텍스트만 추출해 분석하는 경우.
# - 캐시 키: 원본 바이트 hash (extracted_text 가 아니라!) → 같은 파일 재업로드 시 캐시 hit
# - LLM 호출: PDF 와 동일한 EXTRACTION_USER_TEMPLATE 사용
# - 페이지 개념 없음 → page_count=1, 모든 items.page=1 로 검증

def analyze_text(
    raw_bytes: bytes,
    extracted_text: str,
    original_filename: Optional[str] = None,
    source_type: str = "text",
    source_ext: str = "",
    *,
    provider: Optional[LLMProvider] = None,
    use_cache: bool = True,
) -> dict:
    """이미 추출된 평문 텍스트를 7카테고리로 분석.

    Args:
      raw_bytes:       원본 파일 바이트 (캐시 hash 계산용)
      extracted_text:  raw_bytes 에서 미리 추출한 평문
      source_type:     "hwpx" | "hwp" | "text" — 분석 결과의 source_type 필드에 기록
      source_ext:      원본 저장용 확장자 (예: ".hwpx"). 비우면 storage 에 저장 안 함.
    """
    started = time.time()

    if not extracted_text or not extracted_text.strip():
        raise LLMError("추출된 텍스트가 비어있습니다 (원본 파일에 본문이 없거나 파싱 실패)")

    file_hash = compute_file_hash(raw_bytes)

    if use_cache:
        cached = load_analysis(file_hash)
        if cached is not None and cached.get("schema_version") == SCHEMA_VERSION:
            cached["cache_hit"] = True
            return cached

    if source_ext:
        save_source(raw_bytes, source_ext)
    # 추출 텍스트도 캐시 (재분석/검토용)
    from .extractor import ExtractedDocument
    save_extract(file_hash, ExtractedDocument(full_text=extracted_text, page_count=1, pages=[extracted_text]))

    if provider is None:
        provider = get_llm_provider()

    # 섹션 자동 분리는 PDF 와 같은 흐름 — HWPX 도 헤더로 절 구분돼 있는 경우가 많음
    input_text, section_meta = _build_section_input(extracted_text)
    user_prompt = _EXTRACTION_USER_TEMPLATE_TEXT.format(full_text=input_text)

    try:
        llm_result = provider.complete(
            system=EXTRACTION_SYSTEM_PROMPT,
            user=user_prompt,
            response_schema=EXTRACTION_SCHEMA,
            max_tokens=5000,
        )
    except LLMError:
        raise

    if not isinstance(llm_result, dict):
        raise LLMError(f"Expected dict from LLM (schema was set), got {type(llm_result).__name__}")

    # 텍스트 기반은 단일 페이지로 강제 + 검증
    _force_single_page(llm_result)
    issues = _validate_analysis(llm_result, page_count=1)

    empty_section = {"items": [], "extraction_note": ""}
    analysis = {
        "source_file":   original_filename or f"{file_hash}{source_ext or '.txt'}",
        "source_type":   source_type,
        "file_hash":     file_hash,
        "page_count":    1,
        "schema_version": SCHEMA_VERSION,
        "eligibility":    llm_result.get("eligibility",    dict(empty_section)),
        "warnings":       llm_result.get("warnings",       dict(empty_section)),
        "schedule":       llm_result.get("schedule",       dict(empty_section)),
        "support_amount": llm_result.get("support_amount", dict(empty_section)),
        "required_docs":  llm_result.get("required_docs",  dict(empty_section)),
        "evaluation":     llm_result.get("evaluation",     dict(empty_section)),
        "obligations":    llm_result.get("obligations",    dict(empty_section)),
        "missing_sections":  llm_result.get("missing_sections", []),
        "validation_issues": issues,
        "section_extraction": section_meta,
        "elapsed_seconds":    round(time.time() - started, 2),
        "cache_hit":          False,
    }

    save_analysis(file_hash, analysis)
    return analysis


def analyze_attachment(
    file_bytes: bytes,
    original_filename: str,
    mime_type: Optional[str] = None,
    *,
    provider: Optional[LLMProvider] = None,
    use_cache: bool = True,
) -> dict:
    """파일 형식 자동 dispatch — PDF / 이미지 / HWPX 를 한 진입점에서.

    분기 우선순위:
      1. mime_type 인자 (application/pdf, image/*, application/hwp+xml 등)
      2. 파일명 확장자 (.pdf / .jpg .jpeg .png .webp / .hwpx)

    지원하지 않는 형식은 LLMError.
    """
    name = (original_filename or "").lower()
    mt = (mime_type or "").lower()

    is_pdf  = (mt == "application/pdf") or name.endswith(".pdf")
    is_image = mt.startswith("image/") or any(name.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp"))
    is_hwpx = name.endswith(".hwpx") or "hwp+xml" in mt or "hwpx" in mt

    if is_pdf:
        return analyze_pdf(
            file_bytes,
            original_filename=original_filename,
            provider=provider,
            use_cache=use_cache,
        )
    if is_image:
        if not mt:
            ext = Path(name).suffix
            mt = _IMAGE_MIME_BY_EXT.get(ext, "image/jpeg")
        return analyze_image(
            file_bytes,
            original_filename=original_filename,
            mime_type=mt,
            provider=provider,
            use_cache=use_cache,
        )
    if is_hwpx:
        from .hwpx_extractor import extract_hwpx_text
        try:
            text = extract_hwpx_text(file_bytes)
        except ValueError as e:
            raise LLMError(f"HWPX 파싱 실패: {e}") from e
        return analyze_text(
            file_bytes,
            text,
            original_filename=original_filename,
            source_type="hwpx",
            source_ext=".hwpx",
            provider=provider,
            use_cache=use_cache,
        )

    raise LLMError(f"지원하지 않는 파일 형식: {original_filename!r} (mime={mime_type!r})")


def _normalize_title(title: str) -> str:
    """제목을 dedup 비교용으로 정규화 — 공백/구두점/괄호 정리."""
    import re as _re
    s = title or ""
    s = _re.sub(r"[\s·•\-:：\.\(\)\[\]【】]+", "", s).lower()
    return s


def merge_schedule_items(
    per_file_schedules: list[tuple[str, list[dict]]],
) -> dict:
    """이미 analyze_pdf 로 추출된 일정들을 합쳐 중복 제거.

    Args:
        per_file_schedules: [(filename, schedule_items_list), ...]
          각 schedule_item 은 analyze_pdf 가 만든 dict (title, type, date_start, date_end, time, page, note)

    Returns: {"items": [...], "extraction_note": "...", "elapsed_seconds": float, "llm_calls": 0}

    Dedup 키: (type, date_start, date_end). 같은 키 내에서는 가장 긴 제목을 대표로 쓰고
    note/source_files 는 합친다. 시간(time)도 다른 게 있으면 첫 번째 비-null 채택.
    """
    started = time.time()
    if not per_file_schedules:
        return {"items": [], "extraction_note": "no files", "elapsed_seconds": 0.0, "llm_calls": 0}

    # 그룹화: (type, date_start, date_end) → 같은 일정 = 표기만 달라도 한 건으로 dedup.
    # 단, type == "other" 는 카테고리 모호하므로 제목 정규화까지 키에 포함해 보수적으로.
    buckets: dict[tuple, list[dict]] = {}
    for filename, items in per_file_schedules:
        for it in items or []:
            typ = it.get("type") or "other"
            ds = it.get("date_start") or ""
            de = it.get("date_end") or ""
            if typ == "other":
                key = (typ, ds, de, _normalize_title(it.get("title", "")))
            else:
                key = (typ, ds, de, "")
            buckets.setdefault(key, []).append({**it, "_source_file": filename})

    merged: list[dict] = []
    for key, group in buckets.items():
        typ, ds, de = key[0], key[1], key[2]
        # 가장 긴 제목 (정보량 많음) 채택
        rep = max(group, key=lambda x: len(x.get("title") or ""))
        times = [g.get("time") for g in group if g.get("time")]
        notes = [g.get("note") for g in group if g.get("note")]
        sources = sorted({g.get("_source_file") for g in group if g.get("_source_file")})
        merged.append({
            "title": rep.get("title", ""),
            "type": typ,
            "date_start": ds,
            "date_end": de or None,
            "time": times[0] if times else None,
            "page": rep.get("page"),
            "note": "; ".join(sorted(set(notes))) if notes else None,
            "source_files": sources,
        })

    # 시작일 기준 정렬
    merged.sort(key=lambda x: (x.get("date_start") or "", x.get("type") or ""))

    return {
        "items": merged,
        "extraction_note": f"{len(per_file_schedules)}개 파일에서 {sum(len(s) for _,s in per_file_schedules)}건 → dedup 후 {len(merged)}건",
        "elapsed_seconds": round(time.time() - started, 2),
        "llm_calls": 0,
    }


def ask_folder_question(
    files: list[tuple[str, str]],
    question: str,
    history: Optional[list[dict]] = None,
    *,
    provider: Optional[LLMProvider] = None,
    max_history_turns: int = 4,
) -> dict:
    """폴더(공고) 안의 모든 PDF 텍스트를 컨텍스트로 LLM 에 자유 질문.

    Args:
        files: [(filename, full_text), ...]
        question: 사용자 질문
        history: 이전 대화. [{"role": "user"|"assistant", "text": "..."}]
        max_history_turns: 너무 길어지지 않게 최근 N턴만 포함

    Returns: {"answer": str, "elapsed_seconds": float}
    """
    started = time.time()
    if not files:
        return {
            "answer": "이 폴더에 분석된 PDF가 없습니다. 먼저 PDF 를 업로드하세요.",
            "elapsed_seconds": 0.0,
        }
    if not question or not question.strip():
        raise LLMError("질문이 비어있습니다")

    parts = [f"=== [{name}] ===\n{text.strip()}" for name, text in files]
    combined = "\n\n".join(parts)

    history_block = ""
    if history:
        recent = history[-max_history_turns * 2:]  # user+assistant 쌍 N번
        lines = ["", "[이전 대화 (참고용)]"]
        for turn in recent:
            role = turn.get("role")
            txt = (turn.get("text") or "").strip()
            if not txt:
                continue
            label = "질문" if role == "user" else "답변"
            lines.append(f"{label}: {txt}")
        history_block = "\n".join(lines) + "\n"

    user_prompt = QA_USER_TEMPLATE.format(
        combined_text=combined,
        history_block=history_block,
        question=question.strip(),
    )

    if provider is None:
        provider = get_llm_provider()

    result = provider.complete(
        system=QA_SYSTEM_PROMPT,
        user=user_prompt,
        response_schema=None,  # 자유 텍스트
        max_tokens=2000,
    )
    if isinstance(result, dict):
        # 혹시 dict 가 오면 텍스트로 평탄화
        answer = str(result)
    else:
        answer = (result or "").strip() or "(빈 응답)"

    return {
        "answer": answer,
        "elapsed_seconds": round(time.time() - started, 2),
    }


# ─── analysis JSON → 잔축 텍스트 (비교용) ────────────────────────────────
def format_analysis_summary(analysis: dict, *, source_name: str = "") -> str:
    """analyze_pdf 결과(JSON)를 LLM 비교용 잔축 텍스트로 직렬화.

    PDF 전문을 보내는 대신 이 요약만 보내면 토큰이 보통 10~30× 줄어든다.
    스키마 v2 의 7개 카테고리를 카테고리별 불릿으로 정리.
    """
    if not isinstance(analysis, dict):
        return ""

    lines: list[str] = []
    if source_name:
        lines.append(f"--- 파일: {source_name} ---")

    def section_lines(title: str, items: list[dict], fmt) -> None:
        if not items:
            return
        lines.append(f"◆ {title}:")
        for it in items[:15]:  # 안전 상한
            s = fmt(it)
            if s:
                lines.append(f"  - {s}")

    elig = (analysis.get("eligibility") or {}).get("items") or []
    section_lines("지원대상/자격", elig, lambda x: (x.get("text") or "").strip())

    sup = (analysis.get("support_amount") or {}).get("items") or []
    def _fmt_sup(it: dict) -> str:
        t = (it.get("text") or "").strip()
        am = it.get("amount_max_won")
        if am and isinstance(am, int):
            return f"{t}  (≈ {am:,}원)"
        return t
    section_lines("지원금/규모", sup, _fmt_sup)

    docs = (analysis.get("required_docs") or {}).get("items") or []
    def _fmt_doc(it: dict) -> str:
        name = (it.get("name") or "").strip()
        note = (it.get("note") or "").strip()
        return f"{name} ({note})" if note else name
    section_lines("제출 서류", docs, _fmt_doc)

    ev = (analysis.get("evaluation") or {}).get("items") or []
    def _fmt_ev(it: dict) -> str:
        c = (it.get("criterion") or "").strip()
        w = (it.get("weight") or "").strip()
        return f"{c} — {w}" if w else c
    section_lines("평가 기준", ev, _fmt_ev)

    ob = (analysis.get("obligations") or {}).get("items") or []
    def _fmt_ob(it: dict) -> str:
        t = (it.get("text") or "").strip()
        f = (it.get("frequency") or "").strip()
        return f"{t} [빈도: {f}]" if f else t
    section_lines("의무사항", ob, _fmt_ob)

    warn = (analysis.get("warnings") or {}).get("items") or []
    section_lines("유의사항", warn, lambda x: (x.get("text") or "").strip())

    sch = (analysis.get("schedule") or {}).get("items") or []
    def _fmt_sch(it: dict) -> str:
        title = (it.get("title") or "").strip()
        ds = it.get("date_start") or ""
        de = it.get("date_end") or ""
        period = f"{ds} ~ {de}" if de else ds
        tm = (it.get("time") or "").strip()
        period = f"{period} {tm}" if tm else period
        return f"{title}: {period}" if title else period
    section_lines("일정", sch, _fmt_sch)

    missing = analysis.get("missing_sections") or []
    if missing:
        lines.append(f"※ PDF에서 누락된 섹션: {', '.join(missing)}")

    return "\n".join(lines)


# ─── 공고 다중 비교 (정밀 비교) ────────────────────────────────────────────
_PROFILE_LABELS_KO = {
    "business_type":      "사업자 유형",
    "establishment_date": "설립일",
    "region":             "지역",
    "industry":           "업종",
    "industry_detail":    "업종 상세",
    "employee_count":     "종업원 수",
    "founding_type":      "창업 유형",
    "company_name":       "회사명",
}

_BUSINESS_TYPE_LABELS = {
    "individual": "개인사업자",
    "corporate":  "법인",
    "prelaunch":  "예비창업자",
}


def _format_profile_block(profile: Optional[dict]) -> str:
    """LLM 에 보낼 프로필 블록을 만듦. profile 이 비어있거나 None 이면 빈 문자열 반환."""
    if not profile:
        return ""
    lines: list[str] = []
    for key, label in _PROFILE_LABELS_KO.items():
        v = profile.get(key)
        if v in (None, "", 0):
            continue
        if key == "business_type":
            v = _BUSINESS_TYPE_LABELS.get(v, v)
        if key == "employee_count":
            v = f"{v}명"
        lines.append(f"  • {label}: {v}")
    if not lines:
        return ""
    return "[사용자 기업 프로필]\n" + "\n".join(lines) + "\n\n"


def _format_announcements_block(items: list[dict]) -> str:
    """각 공고의 메타 + PDF 본문을 LLM 컨텍스트용 텍스트로 직렬화.

    items: [{
        "label": "A",
        "title": str,
        "source": str,
        "department": str,
        "end_date": str,
        "eligibility_meta": str,   # 자격 메타 요약 (선택)
        "files": [(filename, text), ...],
    }, ...]
    """
    blocks: list[str] = []
    for it in items:
        label = it.get("label") or "?"
        title = it.get("title") or "(제목 없음)"
        head = [f"=== [공고 {label}] {title}"]
        meta_bits = []
        if it.get("source"): meta_bits.append(f"출처: {it['source']}")
        if it.get("department"): meta_bits.append(f"부서: {it['department']}")
        if it.get("end_date"): meta_bits.append(f"마감: {it['end_date']}")
        if meta_bits:
            head.append("  " + " · ".join(meta_bits))
        if it.get("eligibility_meta"):
            head.append(f"  자격 메타: {it['eligibility_meta']}")
        files = it.get("files") or []
        if files:
            head.append("  첨부 PDF:")
            for name, text in files:
                head.append(f"  --- 파일: {name} ---")
                head.append((text or "").strip())
        else:
            head.append("  (첨부 PDF 없음)")
        head.append("=== [공고 " + label + "] 끝 ===")
        blocks.append("\n".join(head))
    return "\n\n".join(blocks)


# ─── 비교 세션 캐시 레지스트리 ────────────────────────────────────────────
# 사용자가 같은 공고 조합으로 채팅을 이어가면 PDF 컨텍스트는 동일하므로
# Gemini 명시 캐시(75% 입력 할인)로 재사용한다.
# 키: (정렬된 announcement_id 튜플, 프로필 시그니처). 값: (cache_name, expires_at).
# TTL 만료 시 Gemini 가 자동 삭제하므로 명시적 cleanup 불필요.
_COMPARE_CACHE_REGISTRY: dict[str, tuple[str, float]] = {}
_COMPARE_CACHE_TTL_SECONDS = 600  # 10분


def _compare_cache_key(items: list[dict], profile: Optional[dict]) -> str:
    """안정적인 캐시 키 — ann_id + 첨부 file count + 프로필 내용으로 SHA256."""
    import hashlib
    payload_parts = []
    for it in items:
        ann_id = it.get("ann_id")
        files = it.get("files") or []
        # 파일 이름 리스트만 (해시 안전)
        names = ",".join(sorted(f[0] for f in files))
        payload_parts.append(f"{ann_id}:{names}:{len(files)}")
    profile_sig = ""
    if profile:
        profile_sig = "|".join(f"{k}={profile[k]}" for k in sorted(profile.keys()))
    payload = "||".join(payload_parts) + "##" + profile_sig
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _get_or_create_compare_cache(
    provider: LLMProvider,
    items: list[dict],
    profile: Optional[dict],
    cached_text: str,
) -> Optional[str]:
    """캐시가 살아있으면 재사용, 아니면 새로 생성. 미지원/실패 시 None."""
    if not provider.supports_caching():
        return None
    key = _compare_cache_key(items, profile)
    now = time.time()
    hit = _COMPARE_CACHE_REGISTRY.get(key)
    if hit:
        name, expires = hit
        # 안전 마진 30초 — 만료 직전이면 새로 생성
        if expires > now + 30:
            return name
    try:
        name = provider.create_cache(
            COMPARE_SYSTEM_PROMPT,
            cached_text,
            ttl_seconds=_COMPARE_CACHE_TTL_SECONDS,
        )
    except LLMError:
        return None
    if not name:
        return None
    _COMPARE_CACHE_REGISTRY[key] = (name, now + _COMPARE_CACHE_TTL_SECONDS)
    return name


def _build_cacheable_prefix(items: list[dict], profile: Optional[dict]) -> str:
    """캐시에 저장할 정적 컨텍스트 (프로필 + 공고들)."""
    return _format_profile_block(profile) + "[비교할 공고 목록]\n" + _format_announcements_block(items)


def compare_announcements(
    items: list[dict],
    profile: Optional[dict] = None,
    *,
    provider: Optional[LLMProvider] = None,
) -> dict:
    """선택된 공고들의 첨부 PDF + 프로필을 한꺼번에 LLM 에 넣고 측면별 비교 출력.

    Args:
        items: _format_announcements_block 참고 형식
        profile: 사용자 프로필 (PII 필터링된 dict). 없거나 비어있으면 일반화 비교.

    Returns: {"answer": str, "elapsed_seconds": float, "cache_hit": bool}
    """
    started = time.time()
    if len(items) < 2:
        raise LLMError("비교는 공고 2개 이상이 필요합니다")

    if provider is None:
        provider = get_llm_provider()

    cacheable_prefix = _build_cacheable_prefix(items, profile)
    cache_name = _get_or_create_compare_cache(provider, items, profile, cacheable_prefix)

    # 캐시 사용 시: 변동분(지시문)만 user 로 보냄. 없으면 prefix + 지시 합쳐서 일반 호출.
    instruction = (
        "\n[지시]\n위 공고들의 자격조건, 지원 내용, 의무사항, 가점, 서류 등을 측면별로 비교해주세요. "
        "어느 게 가장 좋다는 단정 없이, 각 측면에서 어느 공고가 우세한지만 객관적으로 짚어주세요."
    )
    if cache_name:
        result = provider.complete(
            system="",  # 캐시에 포함됨
            user=instruction,
            response_schema=None,
            max_tokens=3000,
            cached_content=cache_name,
        )
    else:
        result = provider.complete(
            system=COMPARE_SYSTEM_PROMPT,
            user=cacheable_prefix + instruction,
            response_schema=None,
            max_tokens=3000,
        )
    answer = (result if isinstance(result, str) else str(result)).strip() or "(빈 응답)"
    return {
        "answer": answer,
        "elapsed_seconds": round(time.time() - started, 2),
        "cache_hit": cache_name is not None,
    }


def chat_compare(
    items: list[dict],
    question: str,
    history: Optional[list[dict]] = None,
    profile: Optional[dict] = None,
    *,
    provider: Optional[LLMProvider] = None,
    max_history_turns: int = 4,
) -> dict:
    """비교 모달의 채팅 — 같은 컨텍스트(공고들 + 프로필)에 사용자 후속 질문을 더해 답."""
    started = time.time()
    if not question or not question.strip():
        raise LLMError("질문이 비어있습니다")
    if len(items) < 2:
        raise LLMError("비교는 공고 2개 이상이 필요합니다")

    if provider is None:
        provider = get_llm_provider()

    history_block = ""
    if history:
        recent = history[-max_history_turns * 2:]
        lines = ["", "[이전 대화 (참고용)]"]
        for turn in recent:
            role = turn.get("role")
            txt = (turn.get("text") or "").strip()
            if not txt:
                continue
            label = "질문" if role == "user" else "답변"
            lines.append(f"{label}: {txt}")
        history_block = "\n".join(lines) + "\n"

    cacheable_prefix = _build_cacheable_prefix(items, profile)
    cache_name = _get_or_create_compare_cache(provider, items, profile, cacheable_prefix)

    # 변동분 (history + 새 질문)
    variable_suffix = (
        f"{history_block}\n[현재 질문]\n{question.strip()}\n\n"
        "위 공고 문서들을 근거로, 사용자가 자기 상황에 맞게 판단할 수 있도록 도와주세요. "
        "\"무엇이 베스트\" 라고 단정 짓지 말고, 사용자의 우선순위에 비추어 어느 공고가 어떤 점에서 부합하는지 사실 기반으로 정리해주세요."
    )

    if cache_name:
        result = provider.complete(
            system="",
            user=variable_suffix,
            response_schema=None,
            max_tokens=2000,
            cached_content=cache_name,
        )
    else:
        result = provider.complete(
            system=COMPARE_SYSTEM_PROMPT,
            user=cacheable_prefix + variable_suffix,
            response_schema=None,
            max_tokens=2000,
        )
    answer = (result if isinstance(result, str) else str(result)).strip() or "(빈 응답)"
    return {
        "answer": answer,
        "elapsed_seconds": round(time.time() - started, 2),
        "cache_hit": cache_name is not None,
    }
