"""Crawler for the Korean National Research Foundation (한국연구재단, NRF).

Targets two list pages on https://www.nrf.re.kr :
  - menu_no=362  →  신규사업공모 (bizNotGubn=guide)
  - menu_no=364  →  사업일반공지 (bizNotGubn=notice)

The list endpoint `/page/362` (or `/page/364`) defaults to filtering by the
current month, so we force `bizSearchRegDttmAllYn=Y` to retrieve every item
regardless of registration date. Detail pages live at
`/biz/notice/view?postNo=...&menuNo=...&bizNo=...`.
"""

from __future__ import annotations

import re
import time
from typing import Iterator
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from .base import AnnouncementRecord, BaseCrawler, CategorySpec, SourceSpec

BASE_URL = "https://www.nrf.re.kr"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class NrfCrawler(BaseCrawler):
    source = SourceSpec(
        code="nrf",
        name="한국연구재단",
        base_url=BASE_URL,
        categories=[
            CategorySpec(
                code="guide",
                name="신규사업공모",
                list_url=f"{BASE_URL}/page/362?menuNo=362&bizNotGubn=guide",
            ),
            CategorySpec(
                code="notice",
                name="사업일반공지",
                list_url=f"{BASE_URL}/page/364?menuNo=364&bizNotGubn=notice",
            ),
        ],
    )

    # Map category.code -> (menu_no, page path)
    _MENU_MAP = {
        "guide": ("362", "/page/362"),
        "notice": ("364", "/page/364"),
    }

    def __init__(self, request_delay: float = 0.4):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.request_delay = request_delay

    def _get(self, url: str, params: dict | None = None) -> str:
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        time.sleep(self.request_delay)
        return resp.text

    def crawl(self) -> Iterator[AnnouncementRecord]:
        for cat in self.source.categories:
            yield from self._crawl_category(cat)

    # ---- list page ----------------------------------------------------------

    def _crawl_category(self, cat: CategorySpec) -> Iterator[AnnouncementRecord]:
        menu_no, path = self._MENU_MAP[cat.code]
        params = {
            "menuNo": menu_no,
            "bizNotGubn": cat.code,
            "bizSearchRegDttmAllYn": "Y",
            "bizSearchRegDttmAllYnInput": "Y",
            "pageNum": "1",
            "pageSize": "100",  # large enough to fit all results on one page
        }
        html = self._get(BASE_URL + path, params=params)
        soup = BeautifulSoup(html, "html.parser")

        # Total count for logging/verification
        total = None
        m = re.search(r"전체\s*<b>(\d+)</b>", html)
        if m:
            total = int(m.group(1))
        print(f"[NRF/{cat.code}] 전체 {total}건 감지")

        blocks = soup.select("div.public-notice-block")
        if total is not None and len(blocks) != total:
            print(f"  WARN: 페이지 블록 수({len(blocks)}) ≠ 전체({total})")

        for block in blocks:
            rec = self._parse_list_block(block, cat.code, menu_no)
            if rec is None:
                continue
            self._fill_detail(rec, menu_no)
            yield rec

    def _parse_list_block(self, block, category_code: str, menu_no: str) -> AnnouncementRecord | None:
        a = block.select_one("a.title-name.view_btn") or block.select_one("a.title-name")
        if not a:
            return None

        post_no = a.get("data-post_no")
        biz_no = a.get("data-biz_no")
        post_close_yn = a.get("data-post_close_yn")
        title = a.get_text(strip=True)
        if not post_no or not title:
            return None

        # Status (접수중/접수마감/접수대기) and D-day are both `.block-text`
        state_block = block.select_one(".pnb-state")
        status = None
        d_day = None
        if state_block:
            spans = state_block.select(".block-text")
            if spans:
                status = spans[0].get_text(strip=True) or None
            d_el = state_block.select_one(".cfy--date .block-text")
            if d_el:
                d_day = d_el.get_text(strip=True) or None

        # 접수일자: e.g. "2026-05-11 14:00 ~ 2026-06-18 17:00"
        start_date = end_date = None
        info = block.select_one("ul.pnb-info")
        if info:
            for li in info.select("li"):
                text = li.get_text(" ", strip=True)
                m = re.search(r"접수일자\s*:\s*(\S+(?:\s+\S+)?)\s*~\s*(\S+(?:\s+\S+)?)", text)
                if m:
                    start_date, end_date = m.group(1), m.group(2)
                    break

        bread = block.select_one(".bread-crumb-text")
        bread_text = bread.get_text(strip=True) if bread else None

        detail_params = {
            "ac": "view",
            "postNo": post_no,
            "menuNo": menu_no,
            "bizNo": biz_no or "0",
            "bizNotGubn": category_code,
        }
        detail_url = f"{BASE_URL}/biz/notice/view?{urlencode(detail_params)}"

        return AnnouncementRecord(
            category_code=category_code,
            external_id=str(post_no),
            external_biz_id=str(biz_no) if biz_no else None,
            title=title,
            detail_url=detail_url,
            status=status,
            d_day=d_day,
            start_date=start_date,
            end_date=end_date,
            raw_meta={
                "post_close_yn": post_close_yn,
                "bread_crumb": bread_text,
            },
        )

    # ---- detail page --------------------------------------------------------

    def _fill_detail(self, rec: AnnouncementRecord, menu_no: str) -> None:
        params = {
            "ac": "view",
            "postNo": rec.external_id,
            "menuNo": menu_no,
            "bizNo": rec.external_biz_id or "0",
            "bizNotGubn": rec.category_code,
        }
        try:
            html = self._get(BASE_URL + "/biz/notice/view", params=params)
        except requests.RequestException as e:
            print(f"  detail fetch failed for postNo={rec.external_id}: {e}")
            return

        soup = BeautifulSoup(html, "html.parser")

        # Pull every (label, body) view-row
        rows: dict[str, str] = {}
        rows_html: dict[str, str] = {}
        for row in soup.select("div.view-row"):
            label_el = row.select_one(".omks--stnc-text")
            body_el = row.select_one(".row-body .view-contents") or row.select_one(".row-body")
            if not body_el:
                continue
            label = label_el.get_text(strip=True).lstrip("·").strip() if label_el else ""
            body_text = body_el.get_text("\n", strip=True)
            rows.setdefault(label, body_text)
            rows_html.setdefault(label, str(body_el))

        # Map well-known labels into structured fields. Anything unmapped is
        # retained in raw_meta so future consumers can still see it.
        period_text = rows.get("신청 기간 및 사업 금액") or ""
        if period_text:
            m = re.search(r"신청\s*기간\s*[:：]\s*(\S+(?:\s+\S+)?)\s*~\s*(\S+(?:\s+\S+)?)", period_text)
            if m:
                rec.start_date = rec.start_date or m.group(1)
                rec.end_date = rec.end_date or m.group(2)

        content_html_parts = []
        content_text_parts = []
        for label in ("공고내용", ""):
            if label in rows:
                content_text_parts.append(rows[label])
                content_html_parts.append(rows_html[label])
        # Some detail rows render their main body without a label
        if not content_text_parts:
            body_el = soup.select_one(".public-notice-detail, .view-contents")
            if body_el:
                content_text_parts.append(body_el.get_text("\n", strip=True))
                content_html_parts.append(str(body_el))
        rec.content_text = "\n\n".join(p for p in content_text_parts if p) or None
        rec.content_html = "\n\n".join(p for p in content_html_parts if p) or None

        rec.contact = rows.get("문의처")

        # Attachments
        for a in soup.select("a.omks--file-name"):
            attach_no = a.get("data-attach_no")
            file_name = a.get_text(strip=True)
            onclick = a.get("onclick", "")
            m = re.search(r"\"((?:\\\/|/)download[^\"]+)\"", onclick)
            url_path = m.group(1).replace("\\/", "/") if m else (f"/download/post/loading/{attach_no}" if attach_no else None)
            if not url_path or not file_name:
                continue
            rec.attachments.append({
                "attach_no": attach_no,
                "file_name": file_name,
                "download_url": BASE_URL + url_path,
            })

        # Preserve extra labelled rows we didn't otherwise consume
        extra = {k: v for k, v in rows.items() if k not in {"공고내용", "문의처", "첨부파일", "신청 기간 및 사업 금액"}}
        if extra:
            rec.raw_meta = {**(rec.raw_meta or {}), "extra_rows": extra}
