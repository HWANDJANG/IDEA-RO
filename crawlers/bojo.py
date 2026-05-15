"""Crawler for 보조금통합포털 (e나라도움, https://www.bojo.go.kr).

This site exposes 보조사업 (assistance businesses) — masters that may have
multiple sub-businesses and zero-or-more active 공모 (announcements). The
search UI lives at `/ca/getCA001201View.do` and is backed by JSON AJAX
endpoints discovered by reading the page's JS:

    /ca/retrieveSearchListDtlbzAjax.do   — 내역사업 (top-level business) list
    /ca/retrieveDtlbzDetailAjax.do       — 내역사업 detail + 사업속성 + 공모 list

The catalogue is filtered by 사업속성. To match a specific filter we hand the
filter code(s) over via `selectedMultiType` (comma-separated codes) and
`selectedMultiText` (the label). The 창업자 filter under 경제활동 has code
`000000000071`.
"""

from __future__ import annotations

import time
from typing import Iterator
from urllib.parse import urlencode

import requests

from .base import (
    AnnouncementRecord,
    AssistanceBusinessRecord,
    BaseCrawler,
    CategorySpec,
    SourceSpec,
)

BASE_URL = "https://www.bojo.go.kr"
SEARCH_PAGE = f"{BASE_URL}/ca/getCA001201View.do"
LIST_API = f"{BASE_URL}/ca/retrieveSearchListDtlbzAjax.do"
DETAIL_API = f"{BASE_URL}/ca/retrieveDtlbzDetailAjax.do"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Known 사업속성 filter codes. Add more as needed.
ATTRIBUTE_CODES = {
    "경제활동:창업자": "000000000071",
}


class BojoCrawler(BaseCrawler):
    """Crawl 내역사업 (대표 보조사업) filtered by a 사업속성 (default: 경제활동=창업자)."""

    source = SourceSpec(
        code="bojo",
        name="보조금통합포털 (e나라도움)",
        base_url=BASE_URL,
        categories=[
            CategorySpec(
                code="startup",
                name="창업자 (경제활동)",
                list_url=f"{SEARCH_PAGE}?attr=000000000071",
            ),
        ],
    )

    def __init__(
        self,
        bsns_year: str = "2026",
        attribute_text: str = "창업자",
        attribute_code: str = ATTRIBUTE_CODES["경제활동:창업자"],
        category_code: str = "startup",
        request_delay: float = 0.4,
        page_size: int = 100,
    ):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.bsns_year = bsns_year
        self.attribute_text = attribute_text
        self.attribute_code = attribute_code
        self.category_code = category_code
        self.request_delay = request_delay
        self.page_size = page_size

    def _post_json(self, url: str, data: dict) -> dict:
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SEARCH_PAGE,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        resp = self.session.post(url, data=data, headers=headers, timeout=30)
        resp.raise_for_status()
        time.sleep(self.request_delay)
        return resp.json()

    def crawl(self) -> Iterator[AssistanceBusinessRecord]:
        # Touch the search page once so we pick up any session cookies it sets.
        self.session.get(SEARCH_PAGE, timeout=20)
        time.sleep(self.request_delay)

        items = self._fetch_list()
        total = items[0].get("dtlbzTotCnt") if items else 0
        print(f"[BOJO/{self.category_code}] 전체 {total}건 ({len(items)}건 수신)")
        if total and len(items) != total:
            print(f"  WARN: 수신({len(items)}) ≠ 전체({total})")

        for item in items:
            rec = self._build_record(item)
            self._fill_detail(rec, item)
            yield rec

    # ---- list ---------------------------------------------------------------

    def _fetch_list(self) -> list[dict]:
        payload = {
            "bsnsyear": self.bsns_year,
            "selAsstnSe": "",  # 전체 (1=국고, 2=지방)
            "selSido": "", "selSigungu": "", "selJrsd": "",
            "selectedMultiText": self.attribute_text,
            "selectedMultiType": self.attribute_code,
            "queryDiv": "TOBE",
            "searchDtlbzPssrYn": "",
            "searchFilterYn": "Y",
            "dtlbzCurPage": "1",
            "dtlbzPerPage": str(self.page_size),
            "sortOdr1": "",
        }
        data = self._post_json(LIST_API, payload)
        return data.get("dtlbzList") or []

    def _build_record(self, item: dict) -> AssistanceBusinessRecord:
        dtlbz_id = str(item["dtlbzId"])
        bsns_year = str(item.get("bsnsyear") or self.bsns_year)
        portal_url = (
            f"{SEARCH_PAGE}?dtlbzId={dtlbz_id}&bsnsyear={bsns_year}"
        )
        detail_url = (
            f"{BASE_URL}/ia/getIA001100Popup.do?" + urlencode({
                "dtlbzId": dtlbz_id, "bsnsyear": bsns_year,
            })
        )
        return AssistanceBusinessRecord(
            category_code=self.category_code,
            external_id=dtlbz_id,
            bsns_year=bsns_year,
            name=item.get("dtlbzNm") or "",
            purpose=item.get("bsnsPurpsDc"),
            start_date=item.get("bsnsBeginDe"),
            end_date=item.get("bsnsEndDe"),
            department=item.get("jrsdNm"),
            sub_count=_to_int(item.get("ddtlzCnt")),
            classification="국고",  # 내역사업 endpoint only returns 국고 entries
            portal_url=portal_url,
            detail_url=detail_url,
            raw_meta={"list_item": item},
        )

    # ---- detail -------------------------------------------------------------

    def _fill_detail(self, rec: AssistanceBusinessRecord, list_item: dict) -> None:
        payload = {"bsnsyear": rec.bsns_year, "viewDtlbzId": rec.external_id}
        try:
            data = self._post_json(DETAIL_API, payload)
        except requests.RequestException as e:
            print(f"  detail fetch failed for dtlbzId={rec.external_id}: {e}")
            return

        info = data.get("dtlbzVO") or {}
        rec.parent_name = info.get("dtbzNm") or rec.parent_name
        rec.overview = info.get("bsnsScaleDc") or rec.overview
        rec.legal_basis = info.get("basisLawordCn")
        rec.support_target = info.get("sportCndCn")
        rec.business_type = info.get("asbzBassTyNm")
        rec.delivery_type = info.get("asbzDlypNm")
        rec.settlement_type = info.get("asstnStlDc")
        rec.sub_dept = info.get("dlvplNm") or info.get("dlvplNmPath")
        rec.charger_name = info.get("userNm")
        rec.charger_tel = info.get("telno")
        rec.charger_email = info.get("email")

        # 사업속성 — flat list of names (e.g. 창업자, 청년(19~29세), 고용안정 ...)
        attr_names = data.get("cmmnAtrbNmList") or []
        rec.attributes = [{"name": n} for n in attr_names if n]

        # 진행중 공모
        for ps in data.get("pssrpDetlList") or []:
            rec.announcements.append(self._build_announcement(ps, rec))

        # Preserve full detail payload in raw_meta for future use
        meta = rec.raw_meta or {}
        meta["detail"] = {
            "dtlbzVO": info,
            "cmmnAtrbNmList": attr_names,
            "pssrpDetlList": data.get("pssrpDetlList") or [],
        }
        rec.raw_meta = meta

    def _build_announcement(self, ps: dict, rec: AssistanceBusinessRecord) -> AnnouncementRecord:
        pssrp_no = str(ps.get("pssrpNo") or "")
        title = ps.get("pblancNm") or rec.name
        start = ps.get("rceptBeginDe") or ps.get("pblancBeginDe")
        end = ps.get("rceptEndDe") or ps.get("pblancEndDe")
        contact_parts = [
            ps.get("chargerNm"),
            ps.get("chargerTelno"),
        ]
        contact = " | ".join(p for p in contact_parts if p) or None
        detail_url = (
            f"{BASE_URL}/da/getDA001200View.do?" + urlencode({
                "pssrpNo": pssrp_no, "bsnsyear": rec.bsns_year,
            })
        )
        return AnnouncementRecord(
            category_code=self.category_code,
            external_id=pssrp_no or f"{rec.external_id}-{ps.get('pblancManageNo','')}",
            external_biz_id=rec.external_id,
            title=title,
            detail_url=detail_url,
            start_date=start,
            end_date=end,
            contact=contact,
            raw_meta={"pssrp": ps},
            business_external_id=rec.external_id,
            business_bsns_year=rec.bsns_year,
        )


def _to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None
