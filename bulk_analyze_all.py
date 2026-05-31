"""모든 진행중 공고에 대해 첨부 자동 fetch + 7카테고리 분석을 일괄 실행.

이미 분석된 공고 (announcement_auto_attachments 에 row 있음) 는 skip — idempotent.
새로 추가된 공고 + 아직 시도 안 한 공고만 처리.

사용:
    python bulk_analyze_all.py            # 진행중 전체
    python bulk_analyze_all.py --force    # 캐시 무시 재시도
"""

from __future__ import annotations

import sqlite3
import sys
import time

from planner.analyzer.dotenv import load_dotenv

load_dotenv()

from planner.auto_fetcher import fetch_and_analyze_announcement
from planner.paths import DB_PATH


def main():
    force = "--force" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT id, title FROM announcements "
        "WHERE end_date IS NOT NULL AND end_date != '' "
        "  AND substr(end_date,1,10) >= date('now') "
        "ORDER BY end_date"
    ))
    if not force:
        # 이미 시도된 공고 (성공/실패 무관) 는 skip
        tried = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT announcement_id FROM announcement_auto_attachments"
            )
        }
        original_n = len(rows)
        rows = [r for r in rows if r["id"] not in tried]
        print(f"전체 진행중 {original_n}건 중 이미 시도된 {original_n - len(rows)}건 skip → {len(rows)}건 처리 대상")
    conn.close()

    if not rows:
        print("처리할 공고가 없습니다.")
        return

    start = time.time()
    stats = {"done": 0, "no_attach": 0, "partial": 0, "all_failed": 0, "error": 0}

    for i, row in enumerate(rows, 1):
        ann_id = row["id"]
        title = (row["title"] or "")[:50]
        c = sqlite3.connect(DB_PATH)
        try:
            result = fetch_and_analyze_announcement(c, ann_id, max_files=10, force=force)
            attachments = result.get("attachments") or []
            if not attachments:
                stats["no_attach"] += 1
                status = "(첨부 없음)"
            else:
                done = sum(1 for a in attachments if a.get("status") == "done")
                failed = len(attachments) - done
                if done == len(attachments):
                    stats["done"] += 1
                    status = f"✅ {done}건 분석"
                elif done > 0:
                    stats["partial"] += 1
                    status = f"⚠️ {done}/{len(attachments)} 분석"
                else:
                    stats["all_failed"] += 1
                    status = f"❌ 모두 실패 ({failed}건)"
            print(f"[{i:4d}/{len(rows)}] ann={ann_id:6d} {status:20s} {title}")
        except Exception as e:  # noqa: BLE001
            stats["error"] += 1
            print(f"[{i:4d}/{len(rows)}] ann={ann_id:6d} ERR: {str(e)[:80]} {title}")
        finally:
            c.close()

        # 주기적 진행률 + ETA
        if i % 25 == 0:
            elapsed = time.time() - start
            rate = i / elapsed * 60
            remain = len(rows) - i
            eta_min = remain / max(rate, 0.1)
            print(f"  ▸ 진행 {i}/{len(rows)} ({elapsed:.0f}s, {rate:.1f}건/분, ETA {eta_min:.0f}분)")
            print(f"    누적: ✅ done={stats['done']} ⚠️ partial={stats['partial']} ❌ all_failed={stats['all_failed']} 📭 no_attach={stats['no_attach']} ⚠ err={stats['error']}")

    elapsed = time.time() - start
    print()
    print("=" * 60)
    print(f"완료: {elapsed:.0f}초 ({elapsed/60:.1f}분)")
    print(f"  ✅ 전체 성공:  {stats['done']}건")
    print(f"  ⚠️ 부분 성공:  {stats['partial']}건")
    print(f"  ❌ 전부 실패:  {stats['all_failed']}건")
    print(f"  📭 첨부 없음:  {stats['no_attach']}건")
    print(f"  ⚠ 처리 오류:   {stats['error']}건")
    print(f"  합계:          {sum(stats.values())}건")


if __name__ == "__main__":
    main()
