"""프로젝트 전체에서 공유하는 경로 상수.

`api/` (Vercel) 와 `planner/web.py` (로컬) 양쪽에서 동일한 경로를 사용하도록
한 곳에 모은다.
"""

from __future__ import annotations

from pathlib import Path

# planner/paths.py 의 부모의 부모 = 프로젝트 루트
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "announcements.db"
STATIC_DIR = PROJECT_ROOT / "public"
INDEX_HTML = STATIC_DIR / "index.html"
