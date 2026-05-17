""".env 파일을 표준 라이브러리만으로 로드.

`KEY=VALUE` 줄 단위. `#` 주석, 빈 줄, 양옆 따옴표 제거.
이미 정의된 환경변수는 덮어쓰지 않는다 (Vercel 등의 시스템 환경변수 우선).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv(path: Optional[Path] = None) -> None:
    if path is None:
        # 프로젝트 루트의 .env
        path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # 양옆 따옴표 한 겹 제거
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)
