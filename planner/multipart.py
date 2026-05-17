"""multipart/form-data 의 가벼운 표준 라이브러리 파서.

cgi 모듈은 Python 3.13에서 deprecated 되었고, 의존성 추가 없이 PDF 업로드 1개를
처리하면 충분하므로 직접 boundary 분할로 구현한다.

지원하는 것:
- 파일 필드 (Content-Disposition: form-data; name=...; filename=...)
- 일반 필드 (filename 없음)
한계:
- nested multipart 미지원
- 거대한 파일은 메모리에 통째로 적재 (서버 진입 시점에 길이 제한 필요)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MultipartField:
    name: str
    filename: Optional[str]
    content_type: str
    data: bytes


class MultipartError(Exception):
    pass


def parse_multipart(content_type_header: str, raw_body: bytes) -> dict[str, MultipartField]:
    boundary = _find_boundary(content_type_header)
    if not boundary:
        raise MultipartError("missing boundary in Content-Type")

    delim = b"--" + boundary.encode("ascii")
    chunks = raw_body.split(delim)

    fields: dict[str, MultipartField] = {}
    for chunk in chunks:
        # 시작/끝 chunk 는 빈 prologue + epilogue 이므로 무시
        c = chunk.strip(b"\r\n")
        if not c or c == b"--":
            continue
        # 헤더-바디 분리
        sep = c.find(b"\r\n\r\n")
        if sep < 0:
            continue
        headers_raw = c[:sep].decode("utf-8", errors="replace")
        body = c[sep + 4:]
        # 끝 boundary 가 같은 chunk 안에 오면 잘라낸다
        if body.endswith(b"\r\n"):
            body = body[:-2]

        name = None
        filename = None
        content_type = "application/octet-stream"
        for line in headers_raw.splitlines():
            lower = line.lower()
            if lower.startswith("content-disposition:"):
                for piece in line[len("content-disposition:"):].split(";"):
                    piece = piece.strip()
                    if piece.startswith("name="):
                        name = _strip_quotes(piece[5:])
                    elif piece.startswith("filename="):
                        filename = _strip_quotes(piece[9:])
            elif lower.startswith("content-type:"):
                content_type = line[len("content-type:"):].strip() or content_type

        if name is None:
            continue
        fields[name] = MultipartField(
            name=name,
            filename=filename,
            content_type=content_type,
            data=body,
        )
    return fields


def _find_boundary(content_type_header: str) -> Optional[str]:
    for piece in content_type_header.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            return _strip_quotes(piece[9:])
    return None


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s
