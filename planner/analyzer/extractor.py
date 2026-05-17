"""PDF → 텍스트 추출.

PyMuPDF(`pymupdf`) 사용. 같은 파일의 재분석을 피하기 위해 SHA256 접두어 16자를
해시로 사용한다 — 동일 바이트 = 동일 해시 = 동일 분석 결과 캐시.
"""

from __future__ import annotations

import hashlib
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
