"""Crawler for IRIS (https://www.iris.go.kr) — 국가R&D 통합공고 시스템.

IRIS 는 공식 OpenAPI 가 없고, 내부적으로 jQuery + jsrender 기반 SPA 페이지가
JSON AJAX endpoint 를 호출하는 구조다. 다행히 그 endpoint 는 인증 없이
세션 쿠키만 있으면 호출 가능하므로 직접 활용한다.

  LIST   : POST /contents/retrieveBsnsAncmBtinSituList.do
            → JSON {paginationInfo, listBsnsAncmBtinSitu: [...]}
  DETAIL : GET  /contents/retrieveBsnsAncmView.do?ancmId=...&ancmPrg=list
            → HTML (div.tstyle_view 안에 사람이 읽는 본문)

`rcveStt` 가 '진행중' / '예정' 인 공고만 채택해 마감 이력은 건너뛴다.

첨부파일은 IRIS 가 별도 2-step AJAX (atchDocId 받기 → form submit) 로
다운로드하는 구조라 단순 GET URL 이 없다. 본 크롤러는 첨부 메타만 수집하지
않고 (목록·본문만), 사용자는 detail_url 로 IRIS 상세 페이지를 직접 열어
다운로드한다.
"""

from __future__ import annotations

import html as html_lib
import re
import time
from typing import Iterator

import requests

from .base import AnnouncementRecord, BaseCrawler, CategorySpec, SourceSpec

BASE_URL = "https://www.iris.go.kr"
LIST_VIEW_PATH = "/contents/retrieveBsnsAncmBtinSituListView.do"
LIST_API_PATH = "/contents/retrieveBsnsAncmBtinSituList.do"
DETAIL_PATH = "/contents/retrieveBsnsAncmView.do"

LIST_VIEW_URL = BASE_URL + LIST_VIEW_PATH
LIST_API_URL = BASE_URL + LIST_API_PATH
DETAIL_URL = BASE_URL + DETAIL_PATH

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str | None) -> str | None:
    if s is None:
        return None
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html_lib.unescape(s))).strip() or None


def _ymd(s: str | None) -> str | None:
    """'2026.05.22' / '2026-05-22' → '2026-05-22'."""
    if not s:
        return None
    m = re.match(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s.strip() or None


def _d_day(end_date: str | None) -> str | None:
    if not end_date or not re.match(r"\d{4}-\d{2}-\d{2}", end_date):
        return None
    from datetime import date
    try:
        y, m, d = int(end_date[:4]), int(end_date[5:7]), int(end_date[8:10])
        diff = (date(y, m, d) - date.today()).days
    except ValueError:
        return None
    if diff == 0:
        return "D-day"
    return f"D-{diff}" if diff > 0 else f"D+{-diff}"


# 상세 페이지에서 본문 영역 추출. tstyle_view 안에 표 형태로 모든 메타가 들어있음.
_DETAIL_BODY_RE = re.compile(
    r'<div[^>]+class="[^"]*tstyle_view[^"]*"[^>]*>([\s\S]+?)</div>\s*</div>',
    re.IGNORECASE,
)


class IrisCrawler(BaseCrawler):
    """IRIS 사업공고 크롤러 — 접수중/예정 공고를 수집한다."""

    source = SourceSpec(
        code="iris",
        name="IRIS 국가R&D통합공고",
        base_url=BASE_URL,
        categories=[
            CategorySpec(
                code="rnd_active",
                name="접수중·예정 공고",
                list_url=LIST_VIEW_URL,
            ),
        ],
    )

    # rcveStt 값별 채택 여부
    _ACTIVE_STATUSES = {"진행중", "예정"}

    def __init__(
        self,
        request_delay: float = 0.3,
        max_pages: int = 500,
        fetch_detail: bool = True,
        timeout: int = 30,
    ):
        """
        Parameters
        ----------
        request_delay
            연속 호출 사이 대기. IRIS 서버 부담 줄이려 0.3s 권장.
        max_pages
            안전 가드. IRIS 는 한 페이지 10건 고정이라 최대 250페이지 정도면
            접수중·예정 공고를 다 커버한다.
        fetch_detail
            True 면 각 공고의 본문 HTML 까지 추가로 GET 해서 content_text 채움.
        """
        self.request_delay = request_delay
        self.max_pages = max_pages
        self.fetch_detail = fetch_detail
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # ─── public ──────────────────────────────────────────────────────────
    def crawl(self) -> Iterator[AnnouncementRecord]:
        # 1) 세션 쿠키 받기 위한 LIST 뷰 GET
        try:
            self.session.get(LIST_VIEW_URL, timeout=self.timeout)
        except requests.RequestException as e:
            print(f"  [iris] 초기 세션 요청 실패: {e}")
            return

        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LIST_VIEW_URL,
            "Origin": BASE_URL,
        }

        total_yielded = skipped_closed = 0
        consecutive_closed_pages = 0
        total_pages_known: int | None = None

        for page in range(1, self.max_pages + 1):
            payload = {
                "pageIndex": str(page),
                "prgmId": "",
                "ancmPrg": "list",
                "bsnsAncmTap": "tap1",
                "bsnsTl": "",
                "ancmSttArr": "",
                "pbofrTpArr": "",
                "blngGovdSeArr": "",
                "sorgnIdArr": "",
                "qualCndtArr": "",
                "techFildArr": "",
            }
            try:
                r = self.session.post(
                    LIST_API_URL, data=payload, headers=ajax_headers,
                    timeout=self.timeout,
                )
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                print(f"  [iris] page {page} 실패: {e}")
                return

            items = data.get("listBsnsAncmBtinSitu") or []
            pagination = data.get("paginationInfo") or {}

            if total_pages_known is None:
                total = pagination.get("totalRecordCount")
                total_pages_known = pagination.get("totalPageCount") or 0
                print(f"[IRIS] 전체 {total}건 / {total_pages_known}페이지 — 활성(접수중·예정)만 수집")

            if not items:
                break

            page_yielded = 0
            for it in items:
                rec = self._build_record(it)
                if rec is None:
                    skipped_closed += 1
                    continue
                page_yielded += 1
                total_yielded += 1
                yield rec
                if self.fetch_detail:
                    time.sleep(self.request_delay)

            print(f"  [iris] page {page}/{total_pages_known}: "
                  f"{page_yielded}/{len(items)} 활성")

            # 최신순 정렬 가정 — 연속 5페이지 모두 마감뿐이면 활성 영역 종료
            if page_yielded == 0:
                consecutive_closed_pages += 1
                if consecutive_closed_pages >= 5:
                    print(f"  [iris] 활성 영역 종료 — page {page} 이후 마감만 보임")
                    break
            else:
                consecutive_closed_pages = 0

            if page >= (total_pages_known or self.max_pages):
                break
            time.sleep(self.request_delay)

        print(f"  → IRIS 총 {total_yielded}건 수집 (마감 제외 {skipped_closed}건)")

    # ─── record build ────────────────────────────────────────────────────
    def _build_record(self, it: dict) -> AnnouncementRecord | None:
        rcve = (it.get("rcveStt") or "").strip()
        if rcve and rcve not in self._ACTIVE_STATUSES:
            return None

        ancm_id = str(it.get("ancmId") or "").strip()
        if not ancm_id:
            return None

        title = (it.get("ancmTl") or "").strip() or "(제목 없음)"
        start_date = _ymd(it.get("rcveStrDe"))
        end_date = _ymd(it.get("rcveEndDe"))
        d_day_raw = it.get("dDay")
        d_day = (f"D-{d_day_raw}" if isinstance(d_day_raw, int) and d_day_raw > 0
                 else _d_day(end_date))

        sorgn = (it.get("sorgnNm") or "").strip()
        govd = (it.get("blngGovdSeNm") or "").strip()
        department = " / ".join(p for p in [govd, sorgn] if p) or None

        detail_url = f"{DETAIL_URL}?ancmId={ancm_id}&ancmPrg=list"

        content_text: str | None = None
        if self.fetch_detail:
            try:
                content_text = self._fetch_content_text(ancm_id)
            except requests.RequestException as e:
                print(f"  [iris] 상세 실패 {ancm_id}: {e}")

        status = "모집중" if rcve == "진행중" else ("예정" if rcve == "예정" else rcve)

        raw_meta = {k: v for k, v in it.items() if v not in (None, "")}

        return AnnouncementRecord(
            category_code="rnd_active",
            external_id=ancm_id,
            title=title,
            detail_url=detail_url,
            status=status,
            d_day=d_day,
            reg_date=_ymd(it.get("ancmDe")),
            start_date=start_date,
            end_date=end_date,
            department=department,
            contact=(it.get("ancmNo") or "").strip() or None,
            content_text=content_text,
            raw_meta=raw_meta,
            attachments=[],  # IRIS 첨부 다운로드는 2-step AJAX 라 단순 URL 미제공
        )

    def _fetch_content_text(self, ancm_id: str) -> str | None:
        r = self.session.get(
            DETAIL_URL,
            params={"ancmId": ancm_id, "ancmPrg": "list"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        m = _DETAIL_BODY_RE.search(r.text)
        if not m:
            return None
        text = _strip_html(m.group(1))
        if not text:
            return None
        # 너무 길면 끊기 (10000 자 한도 — 공고문은 대개 5K 이하)
        return text[:10000]
