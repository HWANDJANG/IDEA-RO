"""모든 진행중 공고 (또는 전체) 의 유형을 LLM 으로 재분류.

기존: matcher.classify_announcement_type 의 키워드 매칭 → 정확도 낮음 (특히
      K-Startup '사업화' 카테고리가 너무 광범위해서 액셀러레이팅·바우처·컨설팅이
      모두 funding 으로 분류됨)
신규: 제목 + content_text + supt_biz_clsfc 를 Gemini 에 보내 정확히 7 카테고리로
      재분류. 결과를 announcements.auto_type 에 캐시 → 일회성 비용.

사용:
    python bulk_classify_types.py             # 진행중 + auto_type IS NULL 만 처리
    python bulk_classify_types.py --all       # 마감 포함 전체
    python bulk_classify_types.py --force     # 캐시 무시 재분류
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime

from planner.analyzer.dotenv import load_dotenv

load_dotenv()

from planner.analyzer.llm.base import LLMError
from planner.analyzer.type_classifier import classify_type_via_llm
from planner.paths import DB_PATH


def main():
    include_closed = "--all" in sys.argv
    force = "--force" in sys.argv

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    q = (
        "SELECT id, title, content_text, raw_meta, auto_type FROM announcements "
        "WHERE 1=1 "
    )
    params: list = []
    if not include_closed:
        q += " AND end_date IS NOT NULL AND substr(end_date,1,10) >= date('now') "
    if not force:
        q += " AND (auto_type IS NULL OR auto_type = '') "
    q += " ORDER BY id"

    rows = list(conn.execute(q, params))
    if not rows:
        print("처리할 공고가 없습니다.")
        conn.close()
        return

    print(f"처리 대상: {len(rows)}건 (force={force}, include_closed={include_closed})")
    print()

    start = time.time()
    stats = Counter()
    fail_n = 0

    for i, row in enumerate(rows, 1):
        ann_id = row["id"]
        title = (row["title"] or "")[:50]

        # supt_biz_clsfc 추출
        clsfc = ""
        if row["raw_meta"]:
            try:
                meta = json.loads(row["raw_meta"])
                clsfc = meta.get("supt_biz_clsfc") or ""
            except (json.JSONDecodeError, TypeError):
                pass

        try:
            result = classify_type_via_llm(
                row["title"] or "",
                row["content_text"] or "",
                clsfc,
            )
            code = result["code"]
            reason = result["reason"][:30]
            stats[code] += 1
            now_iso = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE announcements SET auto_type=?, auto_type_at=? WHERE id=?",
                (code, now_iso, ann_id),
            )
            conn.commit()
            print(f"[{i:4d}/{len(rows)}] ann={ann_id:6d} {code:8s} '{reason}' | {title}")
        except LLMError as e:
            fail_n += 1
            print(f"[{i:4d}/{len(rows)}] ann={ann_id:6d} FAIL  {str(e)[:60]} | {title}")
        except Exception as e:  # noqa: BLE001
            fail_n += 1
            print(f"[{i:4d}/{len(rows)}] ann={ann_id:6d} ERR   {str(e)[:60]} | {title}")

        if i % 25 == 0:
            elapsed = time.time() - start
            rate = i / elapsed * 60
            remain = len(rows) - i
            eta_min = remain / max(rate, 0.1)
            print(f"  ▸ 진행 {i}/{len(rows)} ({elapsed:.0f}s, {rate:.1f}건/분, ETA {eta_min:.0f}분)")

    conn.close()

    elapsed = time.time() - start
    print()
    print("=" * 60)
    print(f"완료: {elapsed:.0f}초 ({elapsed/60:.1f}분), 실패 {fail_n}건")
    print(f"분류 분포 (성공한 {sum(stats.values())}건):")
    for code, n in stats.most_common():
        print(f"  {code:10s} {n:4d}건")


if __name__ == "__main__":
    main()
