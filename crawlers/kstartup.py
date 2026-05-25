"""Crawler for K-Startup 창업지원포털 via the public OpenAPI.

공공데이터포털에서 제공하는 창업진흥원 OPEN API (`kisedKstartupService01`)를
호출해 (1) 지원사업 공고 (`getAnnouncementInformation01`) 와
(2) 통합공고 지원사업 마스터 (`getBusinessInformation01`) 를 수집한다.

`pbanc_sn` 은 기존 HTML 크롤러가 쓰던 식별자와 동일하므로 source_id +
external_id UNIQUE 제약에 의해 자연스럽게 upsert 된다.

`fetch_attachments=True` 면 공고 상세 HTML 페이지를 추가로 1회 긁어
첨부파일 목록을 보강한다 (API 응답에는 첨부 정보가 없음).
"""

from __future__ import annotations

import html as html_lib
import os
import re
import time
from typing import Any, Iterator

import requests

from planner.analyzer.dotenv import load_dotenv

from .base import (
    AnnouncementRecord,
    AssistanceBusinessRecord,
    BaseCrawler,
    CategorySpec,
    SourceSpec,
)

load_dotenv()

API_BASE = "https://apis.data.go.kr/B552735/kisedKstartupService01"
ANNOUNCEMENT_URL = f"{API_BASE}/getAnnouncementInformation01"
BUSINESS_URL = f"{API_BASE}/getBusinessInformation01"

KSTARTUP_PORTAL = "https://www.k-startup.go.kr"
DETAIL_PAGE = f"{KSTARTUP_PORTAL}/web/contents/bizpbanc-ongoing.do"

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


def _ymd(s: Any) -> str | None:
    """'20260521' 또는 '2026-05-21 09:00:00' → '2026-05-21'."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = re.match(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s


def _d_day(end_date: str | None) -> str | None:
    """end_date (YYYY-MM-DD) → 'D-N' / 'D-day' / 'D+N' / None."""
    if not end_date or not re.match(r"\d{4}-\d{2}-\d{2}", end_date):
        return None
    from datetime import date
    y, m, d = int(end_date[0:4]), int(end_date[5:7]), int(end_date[8:10])
    try:
        diff = (date(y, m, d) - date.today()).days
    except ValueError:
        return None
    if diff == 0:
        return "D-day"
    if diff > 0:
        return f"D-{diff}"
    return f"D+{-diff}"


def _join_nonempty(parts: list[str | None], sep: str = " | ") -> str | None:
    cleaned = [p.strip() for p in parts if p and str(p).strip()]
    return sep.join(cleaned) if cleaned else None


class KstartupApiError(RuntimeError):
    pass


class KstartupCrawler(BaseCrawler):
    """K-Startup OpenAPI 기반 크롤러. 공고 + 사업 마스터 모두 수집."""

    source = SourceSpec(
        code="kstartup",
        name="K-Startup 창업지원포털",
        base_url=KSTARTUP_PORTAL,
        categories=[
            CategorySpec(
                code="ongoing",
                name="모집중 공고",
                list_url=f"{DETAIL_PAGE}",
            ),
            CategorySpec(
                code="business",
                name="통합공고 지원사업",
                list_url=f"{KSTARTUP_PORTAL}/web/contents/bizpbanc-deadline.do",
            ),
        ],
    )

    def __init__(
        self,
        service_key: str | None = None,
        per_page: int = 100,
        request_delay: float = 0.1,
        max_pages: int = 1000,
        fetch_attachments: bool = False,
        only_active: bool = True,
        timeout: int = 30,
    ):
        """
        Parameters
        ----------
        service_key
            None 이면 환경변수 K_startup_decoding_key (또는 KSTARTUP_API_KEY) 사용.
        per_page
            API 한 페이지 결과 수. 최대 100 권장 (API 명세 기준).
        request_delay
            연속 API 호출 사이 대기 (30 TPS 한도 안전 마진).
        max_pages
            안전 가드. 28k 공고 / 100 = 약 290 페이지면 충분.
        fetch_attachments
            True 면 각 공고 상세 HTML 을 추가로 받아 첨부파일을 보강한다.
            느려지므로 (28k * 0.3s ≈ 2시간) 기본 False. NEW 공고만 보강하고
            싶으면 호출자 측에서 별도 처리.
        only_active
            True 면 rcrt_prgs_yn != 'Y' (마감) 인 공고는 건너뛴다.
        """
        key = service_key or os.environ.get("K_startup_decoding_key") \
            or os.environ.get("KSTARTUP_API_KEY")
        if not key:
            raise KstartupApiError(
                "K-Startup API key not found. "
                "Set K_startup_decoding_key in .env"
            )
        self.service_key = key
        self.per_page = per_page
        self.request_delay = request_delay
        self.max_pages = max_pages
        self.fetch_attachments = fetch_attachments
        self.only_active = only_active
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # ─── public ──────────────────────────────────────────────────────────
    def crawl(self) -> Iterator[AnnouncementRecord | AssistanceBusinessRecord]:
        yield from self._crawl_businesses()
        yield from self._crawl_announcements()

    # ─── 공고 (announcements) ────────────────────────────────────────────
    def _crawl_announcements(self) -> Iterator[AnnouncementRecord]:
        total_yielded = skipped = 0
        for page_no, items in self._iter_pages(ANNOUNCEMENT_URL, "공고"):
            for item in items:
                if self.only_active and item.get("rcrt_prgs_yn") != "Y":
                    skipped += 1
                    continue
                rec = self._build_announcement(item)
                if rec is None:
                    continue
                total_yielded += 1
                yield rec
        print(f"  → K-Startup 공고 총 {total_yielded}건 수집 (마감 제외 {skipped}건)")

    def _build_announcement(self, item: dict) -> AnnouncementRecord | None:
        pbanc_sn = item.get("pbanc_sn")
        if pbanc_sn is None:
            return None
        external_id = str(pbanc_sn)

        title = item.get("biz_pbanc_nm") or "(제목 없음)"
        start_date = _ymd(item.get("pbanc_rcpt_bgng_dt"))
        end_date = _ymd(item.get("pbanc_rcpt_end_dt"))

        # sprv_inst 는 분류 ("공공기관" 등) — 실제 기관명은 pbanc_ntrp_nm
        department = _join_nonempty([
            item.get("pbanc_ntrp_nm"),
            item.get("biz_prch_dprt_nm"),
        ], sep=" / ")

        contact = _join_nonempty([
            item.get("prch_cnpl_no"),
            item.get("biz_gdnc_url"),
        ])

        detail_url = item.get("detl_pg_url") or \
            f"{DETAIL_PAGE}?schM=view&pbancSn={external_id}"
        # 일부 URL 이 스킴 없이 'www.k-startup...' 로 옴
        if detail_url and not detail_url.startswith("http"):
            detail_url = "https://" + detail_url.lstrip("/")

        content_text = self._compose_content_text(item)

        status = "모집중" if item.get("rcrt_prgs_yn") == "Y" else "마감"

        attachments: list[dict] = []
        if self.fetch_attachments:
            try:
                attachments = self._scrape_attachments(external_id)
            except Exception as e:  # noqa: BLE001
                print(f"  [kstartup] 첨부 스크랩 실패 {external_id}: {e}")

        # 정규 필드로 흡수하지 않은 모든 API 원본을 raw_meta 에 보관
        raw_meta = {k: v for k, v in item.items() if v not in (None, "")}

        return AnnouncementRecord(
            category_code="ongoing",
            external_id=external_id,
            title=title,
            detail_url=detail_url,
            status=status,
            d_day=_d_day(end_date),
            reg_date=None,  # API 에 등록일 필드 없음
            start_date=start_date,
            end_date=end_date,
            department=department,
            contact=contact,
            content_text=content_text,
            raw_meta=raw_meta,
            attachments=attachments,
        )

    def _compose_content_text(self, item: dict) -> str | None:
        """API 의 여러 보조 필드를 사람이 읽기 쉽게 합쳐 content_text 로."""
        lines: list[str] = []
        mapping = [
            ("사업명", item.get("intg_pbanc_biz_nm")),
            ("지원분야", item.get("supt_biz_clsfc")),
            ("지원지역", item.get("supt_regin")),
            ("신청대상", item.get("aply_trgt")),
            ("창업업력", item.get("biz_enyy")),
            ("대상연령", item.get("biz_trgt_age")),
            ("우대사항", item.get("prfn_matr")),
            ("신청제외", item.get("aply_excl_trgt_ctnt")),
            ("주관기관", item.get("sprv_inst")),
        ]
        for label, val in mapping:
            if val and str(val).strip():
                lines.append(f"[{label}] {val}")

        content = item.get("pbanc_ctnt")
        if content and str(content).strip():
            lines.append("")
            lines.append(_strip_html(content) or "")

        aply_mthd = [
            ("방문", item.get("aply_mthd_vst_rcpt_istc")),
            ("우편", item.get("aply_mthd_pssr_rcpt_istc")),
            ("팩스", item.get("aply_mthd_fax_rcpt_istc")),
            ("이메일", item.get("aply_mthd_eml_rcpt_istc")),
            ("온라인", item.get("aply_mthd_onli_rcpt_istc")),
            ("기타", item.get("aply_mthd_etc_istc")),
        ]
        method_lines = [f"  • {k}: {v}" for k, v in aply_mthd if v and str(v).strip()]
        if method_lines:
            lines.append("")
            lines.append("[신청방법]")
            lines.extend(method_lines)

        return "\n".join(lines) if lines else None

    # ─── 사업 마스터 (businesses) ────────────────────────────────────────
    def _crawl_businesses(self) -> Iterator[AssistanceBusinessRecord]:
        total = 0
        for page_no, items in self._iter_pages(BUSINESS_URL, "사업"):
            for item in items:
                rec = self._build_business(item)
                if rec is None:
                    continue
                total += 1
                yield rec
        print(f"  → K-Startup 사업 마스터 총 {total}건 수집")

    def _build_business(self, item: dict) -> AssistanceBusinessRecord | None:
        name = item.get("supt_biz_titl_nm")
        if not name:
            return None

        biz_yr = item.get("biz_yr")
        bsns_year = str(biz_yr) if biz_yr is not None else None

        # detl_pg_url 의 id= 또는 pbancSn= 파라미터에서 외부 식별자 추출
        # 없으면 (사업명+연도) 해시로 폴백
        detl_url = item.get("detl_pg_url") or ""
        external_id: str | None = None
        m = re.search(r"[?&](?:id|pbancSn)=(\d+)", detl_url)
        if m:
            external_id = m.group(1)
        else:
            import hashlib
            h = hashlib.sha1(f"{bsns_year or ''}::{name}".encode("utf-8"))
            external_id = h.hexdigest()[:16]

        if detl_url and not detl_url.startswith("http"):
            detl_url = "https://" + detl_url.lstrip("/")

        overview_parts = [
            item.get("supt_biz_intrd_info"),
            item.get("biz_supt_bdgt_info"),
            item.get("biz_supt_ctnt"),
        ]
        overview = "\n\n".join(p for p in overview_parts if p and str(p).strip()) or None

        attrs: list[dict] = []
        if item.get("biz_category_cd"):
            attrs.append({"name": "사업구분", "code": item["biz_category_cd"]})

        return AssistanceBusinessRecord(
            category_code="business",
            external_id=external_id,
            name=name,
            bsns_year=bsns_year,
            purpose=item.get("supt_biz_chrct"),
            overview=overview,
            support_target=item.get("biz_supt_trgt_info"),
            classification=item.get("biz_category_cd"),
            detail_url=detl_url or None,
            portal_url=detl_url or None,
            raw_meta={k: v for k, v in item.items() if v not in (None, "")},
            attributes=attrs,
        )

    # ─── 첨부파일 하이브리드 스크랩 ─────────────────────────────────────
    def _scrape_attachments(self, pbanc_sn: str) -> list[dict]:
        r = self.session.get(
            DETAIL_PAGE,
            params={"schM": "view", "pbancSn": pbanc_sn},
            timeout=self.timeout,
        )
        r.raise_for_status()
        html = r.text
        attachments: list[dict] = []

        # board_file 영역 추출 (기존 크롤러 로직과 동일)
        file_block = re.search(
            r'<div class="board_file">(.+?)</div>\s*</div>\s*<!--',
            html, re.DOTALL,
        )
        if not file_block:
            file_block = re.search(
                r'<div class="board_file">(.+?)(?=<div\s+class="guide_wrap"|</section>)',
                html, re.DOTALL,
            )
        if not file_block:
            return attachments

        for am in re.finditer(
            r'<a[^>]+href="([^"]+)"[^>]*>(.+?)</a>',
            file_block.group(1),
            re.DOTALL,
        ):
            url = am.group(1).strip()
            name = _strip_html(am.group(2))
            if not url or not name or "fileDown" not in url.lower():
                continue
            if url.startswith("/"):
                url = KSTARTUP_PORTAL + url
            attachments.append({"file_name": name, "download_url": url})

        time.sleep(self.request_delay)
        return attachments

    # ─── 페이지네이션 ────────────────────────────────────────────────────
    def _iter_pages(
        self, url: str, label: str
    ) -> Iterator[tuple[int, list[dict]]]:
        page = 1
        total = None
        while page <= self.max_pages:
            try:
                resp = self.session.get(
                    url,
                    params={
                        "serviceKey": self.service_key,
                        "page": page,
                        "perPage": self.per_page,
                        "returnType": "json",
                    },
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as e:
                print(f"  [kstartup/{label}] page {page} 실패: {e}")
                return

            if total is None:
                total = payload.get("totalCount")
                if total is not None:
                    print(f"[K-Startup/{label}] 전체 {total}건 수집 시작 "
                          f"(perPage={self.per_page})")

            items = payload.get("data") or []
            if not items:
                return

            print(f"  [kstartup/{label}] page {page}: {len(items)}건")
            yield page, items

            if len(items) < self.per_page:
                return
            page += 1
            time.sleep(self.request_delay)
