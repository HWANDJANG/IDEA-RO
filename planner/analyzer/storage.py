"""첨부파일 원본 + 추출 텍스트 + 분석 결과의 파일시스템 저장.

레이아웃:
    planner/storage/
      ├── pdfs/{hash}.pdf
      ├── images/{hash}.{jpg|png|webp}
      ├── extracts/{hash}.txt
      └── analyses/{hash}.json

`{hash}` 는 입력 파일 바이트의 SHA256 앞 16자 (포맷 무관).
같은 파일 재업로드 시 `load_analysis(hash)` 가 캐시 히트되어 LLM 재호출이 없다.
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


def _images_dir() -> Path:
    p = _STORAGE_ROOT / "images"
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


_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg":  ".jpg",
    "image/pjpeg": ".jpg",
    "image/png":  ".png",
    "image/webp": ".webp",
}


def save_image(image_bytes: bytes, mime_type: str) -> str:
    """이미지 바이트를 hash 파일명 + MIME 추정 확장자로 저장. file_hash 반환."""
    file_hash = compute_file_hash(image_bytes)
    ext = _MIME_TO_EXT.get((mime_type or "").lower(), ".bin")
    path = _images_dir() / f"{file_hash}{ext}"
    if not path.exists():
        path.write_bytes(image_bytes)
    return file_hash


def get_image_path(file_hash: str) -> Optional[Path]:
    for p in _images_dir().glob(f"{file_hash}.*"):
        return p
    return None


def _sources_dir() -> Path:
    p = _STORAGE_ROOT / "sources"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_source(file_bytes: bytes, ext: str) -> str:
    """PDF/이미지가 아닌 원본 파일(HWPX, HWP 등) 을 hash 파일명으로 저장. file_hash 반환.

    ext 는 점 포함 (예: '.hwpx').
    """
    file_hash = compute_file_hash(file_bytes)
    ext = ext if ext.startswith(".") else f".{ext}"
    path = _sources_dir() / f"{file_hash}{ext}"
    if not path.exists():
        path.write_bytes(file_bytes)
    return file_hash


def get_source_path(file_hash: str) -> Optional[Path]:
    for p in _sources_dir().glob(f"{file_hash}.*"):
        return p
    return None


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
    for d in (_pdfs_dir(), _images_dir(), _sources_dir(), _extracts_dir(), _analyses_dir()):
        # 확장자가 디렉터리마다 다르니 일괄 처리하지 말고 glob
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
