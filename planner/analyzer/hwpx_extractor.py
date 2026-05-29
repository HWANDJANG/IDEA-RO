"""HWPX (한컴 신형 XML) → 평문 텍스트 추출.

HWPX 는 ZIP 컨테이너 + OWPML XML. 외부 의존성 없이 zipfile + xml.etree 로 처리.

구조 (표준):
  mimetype                       (압축 없음, 첫 entry)
  version.xml
  META-INF/manifest.xml
  Contents/header.xml
  Contents/section0.xml          ← 본문
  Contents/section1.xml          ← 본문 (여러 섹션)
  ...

본문 텍스트는 `<hp:p>` 단락 안의 `<hp:t>` 들에 있다. 네임스페이스 prefix
(hp/hs/hh/...) 는 한컴 버전마다 살짝 다를 수 있어 **local-name 매칭** 으로
파싱한다 (`tag.endswith("}t")` / `tag == "t"`).

추출 단위는 단락 (`<hp:p>`) — 같은 단락의 모든 `<hp:t>` 를 이어 붙이고,
단락 사이는 `\n` 으로 구분.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import Iterable
from xml.etree import ElementTree as ET


def _localname(tag: str) -> str:
    """`{ns}name` → `name`. 네임스페이스 없는 경우 그대로."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _iter_paragraph_texts(elem: ET.Element) -> Iterable[str]:
    """루트 element 하위의 모든 `<...p>` 단락에서 텍스트 추출."""
    for p in elem.iter():
        if _localname(p.tag) != "p":
            continue
        parts: list[str] = []
        for t in p.iter():
            if _localname(t.tag) == "t" and t.text:
                parts.append(t.text)
        joined = "".join(parts).strip()
        if joined:
            yield joined


def extract_hwpx_text(hwpx_bytes: bytes) -> str:
    """HWPX 바이트 → 평문. 섹션 순서대로 단락을 `\n` 으로 결합.

    Raises:
      ValueError: ZIP 이 아니거나 section XML 이 하나도 없는 경우
    """
    paragraphs: list[str] = []
    try:
        with zipfile.ZipFile(BytesIO(hwpx_bytes)) as z:
            # 섹션 후보 — 표준은 Contents/section{N}.xml, 가끔 BodyText/Section{N}.xml 도 있음.
            candidates = [
                n for n in z.namelist()
                if (
                    (n.startswith("Contents/section") or n.startswith("BodyText/Section"))
                    and n.lower().endswith(".xml")
                )
            ]
            # 숫자 기준 정렬 (section10 < section2 방지)
            def _section_num(name: str) -> int:
                m = re.search(r"(\d+)\.xml$", name, re.IGNORECASE)
                return int(m.group(1)) if m else 999
            candidates.sort(key=_section_num)

            if not candidates:
                raise ValueError("HWPX 본문 섹션 (Contents/section*.xml) 을 찾지 못했습니다")

            for name in candidates:
                xml_bytes = z.read(name)
                try:
                    root = ET.fromstring(xml_bytes)
                except ET.ParseError as e:
                    # 한 섹션 깨져도 다른 섹션은 살릴 수 있음
                    paragraphs.append(f"[섹션 {name} 파싱 실패: {e}]")
                    continue
                for para in _iter_paragraph_texts(root):
                    paragraphs.append(para)
    except zipfile.BadZipFile as e:
        raise ValueError(f"HWPX 가 올바른 ZIP 컨테이너가 아닙니다: {e}") from e

    return "\n".join(paragraphs)


def is_hwpx_bytes(data: bytes) -> bool:
    """매직 바이트로 HWPX 가능성 확인 (ZIP 시그니처 `PK` 로 시작)."""
    return data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06")
