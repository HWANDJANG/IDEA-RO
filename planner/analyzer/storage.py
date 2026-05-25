"""첨부파일 원본 + 추출 텍스트 + 분석 결과의 파일시스템 저장.

레이아웃:
    planner/storage/
      ├── pdfs/{hash}.pdf
      ├── extracts/{hash}.txt
      └── analyses/{hash}.json

`{hash}` 는 PyMuPDF 추출 전에 계산되는 SHA256 의 앞 16자.
같은 PDF 재업로드 시 `load_analysis(hash)` 가 캐시 히트되어 LLM 재호출이 없다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from planner.paths import PROJECT_ROOT
from .extractor import ExtractedDocument, compute_file_hash


_STORAGE_ROOT = PROJECT_ROOT / "planner" / "storage"


def _pdfs_dir() -> Path:
    p = _STORAGE_ROOT / "pdfs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _extracts_dir() -> Path:
    p = _STORAGE_ROOT / "extracts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _analyses_dir() -> Path:
    p = _STORAGE_ROOT / "analyses"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_pdf(pdf_bytes: bytes, original_filename: str) -> str:
    """PDF 바이트를 받아 hash 파일명으로 저장. (file_hash, 저장 경로) 중 hash 만 반환."""
    file_hash = compute_file_hash(pdf_bytes)
    path = _pdfs_dir() / f"{file_hash}.pdf"
    if not path.exists():
        path.write_bytes(pdf_bytes)
    return file_hash


def get_pdf_path(file_hash: str) -> Path:
    return _pdfs_dir() / f"{file_hash}.pdf"


def save_extract(file_hash: str, doc: ExtractedDocument) -> None:
    (_extracts_dir() / f"{file_hash}.txt").write_text(doc.full_text, encoding="utf-8")


def load_extract(file_hash: str) -> Optional[str]:
    p = _extracts_dir() / f"{file_hash}.txt"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def save_analysis(file_hash: str, analysis: dict) -> None:
    path = _analyses_dir() / f"{file_hash}.json"
    path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")


def load_analysis(file_hash: str) -> Optional[dict]:
    path = _analyses_dir() / f"{file_hash}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete_attachment(file_hash: str) -> None:
    for d in (_pdfs_dir(), _extracts_dir(), _analyses_dir()):
        # 확장자가 셋 다 다르니 일괄 처리하지 말고 glob
        for p in d.glob(f"{file_hash}.*"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


# ─── 폴더-단위 derived 결과 캐시 (일정 dedup 등) ──────────────────────────
# 키는 컨텐츠 해시(폴더 ID + PDF hash 들 정렬)로 만들어 invalidation 자동.
def _derived_dir() -> Path:
    p = _STORAGE_ROOT / "derived"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_derived(key: str) -> Optional[dict]:
    path = _derived_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_derived(key: str, payload: dict) -> None:
    path = _derived_dir() / f"{key}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
