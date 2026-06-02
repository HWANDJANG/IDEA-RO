"""Run one or more crawlers and persist their output into SQLite.

Usage:
    python run_crawler.py               # runs every registered crawler
    python run_crawler.py nrf kstartup  # runs only the named crawlers
"""

from __future__ import annotations

import sys
import traceback
from collections import defaultdict

import db
from crawlers.base import (
    AnnouncementRecord,
    AssistanceBusinessRecord,
    BaseCrawler,
)
from crawlers.iris import IrisCrawler
from crawlers.kstartup import KstartupCrawler
from crawlers.nrf import NrfCrawler
from crawlers.ntis import NtisCrawler

CRAWLERS: dict[str, type[BaseCrawler]] = {
    "nrf": NrfCrawler,
    "kstartup": KstartupCrawler,
    "iris": IrisCrawler,
    "ntis": NtisCrawler,
}


def _save_announcement(conn, source_id: int, category_ids: dict, rec: AnnouncementRecord, business_id: int | None = None):
    payload = {
        "source_id": source_id,
        "category_id": category_ids.get(rec.category_code),
        "business_id": business_id,
        "external_id": rec.external_id,
        "external_biz_id": rec.external_biz_id,
        "title": rec.title,
        "status": rec.status,
        "d_day": rec.d_day,
        "reg_date": rec.reg_date,
        "start_date": rec.start_date,
        "end_date": rec.end_date,
        "department": rec.department,
        "contact": rec.contact,
        "content_html": rec.content_html,
        "content_text": rec.content_text,
        "detail_url": rec.detail_url,
        "raw_meta": rec.raw_meta,
    }
    ann_id, was_new = db.upsert_announcement(conn, payload)
    if business_id is not None:
        conn.execute("UPDATE announcements SET business_id=? WHERE id=?", (business_id, ann_id))
    db.replace_attachments(conn, ann_id, rec.attachments)
    return ann_id, was_new


def _save_business(conn, source_id: int, category_ids: dict, rec: AssistanceBusinessRecord):
    payload = {
        "source_id": source_id,
        "category_id": category_ids.get(rec.category_code),
        "external_id": rec.external_id,
        "bsns_year": rec.bsns_year,
        "name": rec.name,
        "parent_name": rec.parent_name,
        "purpose": rec.purpose,
        "overview": rec.overview,
        "legal_basis": rec.legal_basis,
        "support_target": rec.support_target,
        "start_date": rec.start_date,
        "end_date": rec.end_date,
        "department": rec.department,
        "sub_dept": rec.sub_dept,
        "charger_name": rec.charger_name,
        "charger_tel": rec.charger_tel,
        "charger_email": rec.charger_email,
        "sub_count": rec.sub_count,
        "classification": rec.classification,
        "business_type": rec.business_type,
        "delivery_type": rec.delivery_type,
        "settlement_type": rec.settlement_type,
        "portal_url": rec.portal_url,
        "detail_url": rec.detail_url,
        "raw_meta": rec.raw_meta,
    }
    biz_id, was_new = db.upsert_assistance_business(conn, payload)
    db.replace_business_attributes(conn, biz_id, rec.attributes)
    return biz_id, was_new


def run_one(crawler: BaseCrawler) -> None:
    src = crawler.source
    print(f"\n=== {src.name} ({src.code}) ===")

    with db.connect() as conn:
        source_id = db.upsert_source(conn, src.code, src.name, src.base_url)
        category_ids = {
            c.code: db.upsert_category(conn, source_id, c.code, c.name, c.list_url)
            for c in src.categories
        }

    # Per-category stats for announcements vs businesses separately
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {
        "ann_found": 0, "ann_new": 0, "ann_updated": 0,
        "biz_found": 0, "biz_new": 0, "biz_updated": 0,
    })
    run_ids: dict[str, int] = {}
    error_per_cat: dict[str, str | None] = {c.code: None for c in src.categories}

    with db.connect() as conn:
        for c in src.categories:
            run_ids[c.code] = db.start_crawl_run(conn, source_id, category_ids[c.code])

    try:
        with db.connect() as conn:
            for rec in crawler.crawl():
                if isinstance(rec, AssistanceBusinessRecord):
                    biz_id, was_new = _save_business(conn, source_id, category_ids, rec)
                    s = stats[rec.category_code]
                    s["biz_found"] += 1
                    s["biz_new" if was_new else "biz_updated"] += 1
                    print(
                        f"  [{rec.category_code}/biz] {'NEW ' if was_new else 'upd '}"
                        f"{rec.external_id} {rec.name[:55]} "
                        f"(부처={rec.department}, 속성={len(rec.attributes)}, 공모={len(rec.announcements)})"
                    )
                    for ann in rec.announcements:
                        _, ann_new = _save_announcement(conn, source_id, category_ids, ann, business_id=biz_id)
                        s["ann_found"] += 1
                        s["ann_new" if ann_new else "ann_updated"] += 1
                        print(f"      └─ 공모 {'NEW' if ann_new else 'upd'} {ann.external_id} {ann.title[:50]}")

                elif isinstance(rec, AnnouncementRecord):
                    _, ann_new = _save_announcement(conn, source_id, category_ids, rec)
                    s = stats[rec.category_code]
                    s["ann_found"] += 1
                    s["ann_new" if ann_new else "ann_updated"] += 1
                    print(
                        f"  [{rec.category_code}] {'NEW ' if ann_new else 'upd '}"
                        f"{rec.external_id} {rec.title[:60]} ({len(rec.attachments)} attach)"
                    )
                else:
                    print(f"  WARN: unknown record type {type(rec).__name__}")
    except Exception as e:  # noqa: BLE001
        print("".join(traceback.format_exception(e)), file=sys.stderr)
        for code in error_per_cat:
            error_per_cat[code] = str(e)

    with db.connect() as conn:
        for code, run_id in run_ids.items():
            s = stats[code]
            err = error_per_cat[code]
            # crawl_runs.items_* historically tracked announcements; combine totals
            db.finish_crawl_run(
                conn,
                run_id=run_id,
                items_found=s["ann_found"] + s["biz_found"],
                items_new=s["ann_new"] + s["biz_new"],
                items_updated=s["ann_updated"] + s["biz_updated"],
                status="error" if err else "ok",
                error=err,
            )
            print(
                f"  → {code}: biz(found={s['biz_found']}, new={s['biz_new']}, upd={s['biz_updated']})  "
                f"ann(found={s['ann_found']}, new={s['ann_new']}, upd={s['ann_updated']})  "
                f"status={'error' if err else 'ok'}"
            )


def _classify_new_announcements_via_llm() -> None:
    """크롤링 후 auto_type IS NULL 인 신규 진행중 공고만 LLM 분류 (Phase 9).

    이미 분류된 공고는 캐시 hit 으로 skip → 사실상 신규 공고만 비용 발생.
    LLM 호출 실패해도 크롤링 결과는 영향 없음 (best-effort).
    """
    print("\n=== 신규 공고 자동 분류 (LLM) ===")
    try:
        from planner.analyzer.dotenv import load_dotenv
        load_dotenv()
        from planner.analyzer.type_classifier import classify_type_via_llm
        from planner.analyzer.llm.base import LLMError
        import json
        import sqlite3
        from datetime import datetime
        from planner.paths import DB_PATH

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(
            "SELECT id, title, content_text, raw_meta FROM announcements "
            "WHERE end_date IS NOT NULL AND substr(end_date,1,10) >= date('now') "
            "  AND (auto_type IS NULL OR auto_type = '') "
            "ORDER BY id"
        ))
        if not rows:
            print("  신규 분류 대상 없음 (모두 캐시 hit)")
            conn.close()
            return

        print(f"  대상 {len(rows)}건 처리 시작...")
        ok, fail = 0, 0
        for i, row in enumerate(rows, 1):
            clsfc = ""
            if row["raw_meta"]:
                try:
                    clsfc = (json.loads(row["raw_meta"]) or {}).get("supt_biz_clsfc") or ""
                except (json.JSONDecodeError, TypeError):
                    pass
            try:
                r = classify_type_via_llm(row["title"] or "", row["content_text"] or "", clsfc)
                now_iso = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE announcements SET auto_type=?, auto_type_at=? WHERE id=?",
                    (r["code"], now_iso, row["id"]),
                )
                conn.commit()
                ok += 1
            except (LLMError, Exception) as e:  # noqa: BLE001
                fail += 1
                if fail <= 3:
                    print(f"  FAIL ann={row['id']}: {str(e)[:80]}")
        conn.close()
        print(f"  완료: 성공 {ok}, 실패 {fail}")
    except Exception as e:  # noqa: BLE001
        # LLM 환경 (API key 등) 미설정 시에도 크롤링은 성공 처리
        print(f"  자동 분류 SKIP: {e}")


def main() -> int:
    db.init_db()
    requested = sys.argv[1:] or list(CRAWLERS.keys())
    unknown = [r for r in requested if r not in CRAWLERS]
    if unknown:
        print(f"Unknown crawler(s): {unknown}. Available: {list(CRAWLERS)}", file=sys.stderr)
        return 2

    for code in requested:
        run_one(CRAWLERS[code]())

    _classify_new_announcements_via_llm()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
