"""PDF → 텍스트 추출.

PyMuPDF(`pymupdf`) 사용. 같은 파일의 재분석을 피하기 위해 SHA256 접두어 16자를
해시로 사용한다 — 동일 바이트 = 동일 해시 = 동일 분석 결과 캐시.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

import pymupdf  # type: ignore[import-untyped]


@dataclass
class ExtractedPage:
    page_num: int
    text: str


@dataclass
class ExtractedDocument:
    page_count: int
    pages: list[ExtractedPage] = field(default_factory=list)
    full_text: str = ""  # [Page N] 마커가 포함된 합본 텍스트

    def to_dict(self) -> dict:
        return {
            "page_count": self.page_count,
            "pages": [{"page_num": p.page_num, "text": p.text} for p in self.pages],
        }


def compute_file_hash(source: Union[Path, bytes]) -> str:
    """SHA256 의 앞 16자 (16진수). 파일 경로 또는 바이트를 받는다."""
    h = hashlib.sha256()
    if isinstance(source, (bytes, bytearray)):
        h.update(source)
    else:
        path = Path(source)
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()[:16]


# ─── 섹션 헤더 기반 분할 (analyze_pdf 의 LLM 입력 축소용) ──────────────────
# 정부 공고문은 통상 ■/◇/▶/숫자/Roman/한글 chapter marker 뒤에 정해진 키워드가 옴.
# 너무 엄격한 regex 는 매칭 실패가 잦아 fallback (=full text) 비율이 높아짐.
# 보수적으로: 줄 시작 + 마커 prefix + 키워드.

_HEADER_PREFIX_RE = (
    r"(?:^|\n)\s*"
    r"(?:"
    r"[■▶◇◆●○□※*◎◈☆◐◑▣▷◁]"      # 글머리 기호
    r"|[①-⑳]"                              # 원숫자
    r"|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]"                  # 로마숫자
    r"|\d+[\.\)）]"                         # 1. / 1)
    r"|\(?\d+\)"                            # (1)
    r"|[가-힣][\.\)\s]"                     # 가. / 가)
    r")"
    r"[\s ]*"
)

# 카테고리 → 키워드 동의어 목록 (공백 변형은 normalize 단계에서 흡수)
_SECTION_KEYWORDS: dict[str, list[str]] = {
    "eligibility":   ["지원대상", "신청자격", "신청대상", "모집대상", "지원자격", "참여자격", "공모대상"],
    "support":       ["지원내용", "지원사항", "지원규모", "지원금", "지원한도", "지원범위", "지원수준"],
    "required_docs": ["제출서류", "구비서류", "필요서류", "신청서류", "제출자료"],
    "evaluation":    ["평가기준", "심사기준", "선정기준", "평가방법", "선정방법", "평가항목"],
    "schedule":      ["모집기간", "신청기간", "접수기간", "추진일정", "사업일정", "주요일정", "공고기간"],
    "obligations":   ["의무사항", "정산", "후속관리", "약정사항", "준수사항", "사후관리"],
    "warnings":      ["유의사항", "주의사항", "참고사항", "기타사항"],
    "contact":       ["문의", "문의처", "연락처", "담당자"],
}


def _norm_for_section_match(s: str) -> str:
    """헤더 매칭용 공백 제거 — '지원 대상' / '지원대상' 같이 인식."""
    return re.sub(r"\s+", "", s)


def find_sections(full_text: str) -> dict[str, str]:
    """공고문 전체 텍스트에서 인식 가능한 섹션을 분리.

    Returns: {category: extracted_text}. 매칭되지 않은 카테고리는 dict 에 없음.
    같은 카테고리가 여러 번 나오면 합쳐서 저장.

    구현: 헤더 후보 줄을 다 찾고, 각 헤더에서 다음 헤더 전까지를 그 카테고리의 본문으로.
    """
    if not full_text:
        return {}

    # 1) 헤더 위치 후보 모두 수집
    matches: list[tuple[int, int, str]] = []  # (start, end_of_header_line, category)
    # 전체 텍스트를 줄 단위로 보되, _HEADER_PREFIX_RE 다음에 키워드(공백 무시) 매칭
    line_re = re.compile(_HEADER_PREFIX_RE + r"([^\n]{1,80})")
    for m in line_re.finditer(full_text):
        head_text = m.group(1) or ""
        norm = _norm_for_section_match(head_text)
        for category, kws in _SECTION_KEYWORDS.items():
            if any(kw in norm for kw in kws):
                # 줄의 시작(=마커 직전)을 start 로, 헤더 줄 끝(\n)을 end 로
                line_end = full_text.find("\n", m.end())
                if line_end == -1:
                    line_end = len(full_text)
                matches.append((m.start(), line_end, category))
                break

    if not matches:
        return {}

    matches.sort(key=lambda x: x[0])

    # 2) 각 헤더에서 다음 헤더 전까지의 본문 추출
    sections: dict[str, list[str]] = {}
    for i, (start, head_end, category) in enumerate(matches):
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(full_text)
        content = full_text[head_end:next_start].strip()
        if content:
            sections.setdefault(category, []).append(content)

    return {k: "\n\n".join(v) for k, v in sections.items()}


def extract_pdf_text(pdf_source: Union[Path, bytes]) -> ExtractedDocument:
    """PDF 의 페이지별 텍스트를 추출하고 [Page N] 마커를 포함한 full_text 를 만든다.

    pdf_source 는 파일 경로 또는 바이트.
    """
    if isinstance(pdf_source, (bytes, bytearray)):
        doc = pymupdf.open(stream=bytes(pdf_source), filetype="pdf")
    else:
        doc = pymupdf.open(Path(pdf_source))

    try:
        pages: list[ExtractedPage] = []
        parts: list[str] = []
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            text = text.strip()
            pages.append(ExtractedPage(page_num=i, text=text))
            parts.append(f"[Page {i}]\n{text}")
        return ExtractedDocument(
            page_count=len(pages),
            pages=pages,
            full_text="\n\n".join(parts),
        )
    finally:
        doc.close()
