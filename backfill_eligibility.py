"""기존 활성 공고들에 LLM 자격 추출을 일괄 적용해 raw_meta 보강.

대상: K-Startup 외의 source (IRIS / NTIS / NRF). K-Startup 은 이미 구조화된
자격 메타 (biz_enyy / supt_regin / aply_trgt) 가 있어 LLM 보강 불필요.

매번 같은 row 를 다시 호출하지 않도록 raw_meta['llm_eligibility'] 키가
이미 있으면 skip. --force 옵션으로 재추출 가능.

Usage:
    python backfill_eligibility.py             # 신규만
    python backfill_eligibility.py --force     # 전체 재추출
    python backfill_eligibility.py --source ntis  # 특정 source 만
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import db
from planner.analyzer.dotenv import load_dotenv
from planner.analyzer.llm.base import LLMError
from planner.analyzer.llm.registry import get_llm_provider
from planner.eligibility_extractor import extract_eligibility


# K-Startup 은 이미 구조화 메타 있음 → 보강 불필요
TARGET_SOURCES_DEFAULT = ("iris", "ntis", "nrf")

# Gemini 무료 티어 15 RPM 안전 마진
REQUEST_INTERVAL = 4.2  # seconds between LLM calls


def run(sources: tuple[str, ...], force: bool) -> None:
    provider = get_llm_provider()
    print(f"[backfill] LLM provider ready ({type(provider).__name__})")

    placeholders = ",".join("?" * len(sources))
    with db.connect() as conn:
        rows = list(conn.execute(
            f"SELECT a.id, a.external_id, a.title, a.content_text, a.raw_meta, s.code AS source_code "
            f"FROM announcements a JOIN sources s ON s.id=a.source_id "
            f"WHERE s.code IN ({placeholders}) "
            f"  AND a.end_date >= date('now') "
            f"  AND length(IFNULL(a.content_text,'')) > 50 "
            f"ORDER BY a.end_date",
            sources,
        ))
    print(f"[backfill] 대상 활성 공고: {len(rows)}건")

    skipped = updated = failed = 0
    for i, row in enumerate(rows, 1):
        try:
            raw = json.loads(row["raw_meta"]) if row["raw_meta"] else {}
        except json.JSONDecodeError:
            raw = {}

        if not force and isinstance(raw.get("llm_eligibility"), dict):
            skipped += 1
            print(f"  [{i:3d}/{len(rows)}] skip (이미 추출됨) "
                  f"{row['source_code']}/{row['external_id']}")
            continue

        try:
            elig = extract_eligibility(
                row["content_text"], row["title"], provider=provider
            )
        except LLMError as e:
            failed += 1
            print(f"  [{i:3d}/{len(rows)}] FAIL {row['source_code']}/{row['external_id']}: {e}")
            time.sleep(REQUEST_INTERVAL)
            continue

        raw["llm_eligibility"] = elig
        with db.connect() as conn:
            conn.execute(
                "UPDATE announcements SET raw_meta=? WHERE id=?",
                (json.dumps(raw, ensure_ascii=False), row["id"]),
            )
        updated += 1

        # 요약 한 줄
        summary = elig.get("eligibility_summary", "")[:40]
        regin = elig.get("supt_regin") or "-"
        trgt = elig.get("aply_trgt") or "-"
        print(f"  [{i:3d}/{len(rows)}] OK   {row['source_code']}/{row['external_id']} "
              f"| 지역={regin} | 대상={trgt} | {summary}")

        if i < len(rows):
            time.sleep(REQUEST_INTERVAL)

    print(f"\n=== 백필 완료 ===")
    print(f"  추출 성공: {updated}건")
    print(f"  skip(기존): {skipped}건")
    print(f"  실패: {failed}건")


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true",
                   help="이미 추출된 row 도 재추출")
    p.add_argument("--source", action="append",
                   help="특정 source code 만 (반복 지정 가능). 기본: iris/ntis/nrf")
    args = p.parse_args()

    sources = tuple(args.source) if args.source else TARGET_SOURCES_DEFAULT
    run(sources, args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
