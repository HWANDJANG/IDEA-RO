"""Crawler for NTIS (https://www.ntis.go.kr) — 국가과학기술지식정보서비스.

R&D 통합공고 페이지에서 각 부처·전문기관이 등록한 공고를 일괄 수집한다.
공식 OpenAPI 권한이 떨어지기 전까지의 임시 수집 경로.

  LIST   : GET /rndgate/eg/un/ra/mng.do?pageIndex=N
            → SSR HTML, 5번째 tbody 가 결과 행 (10건/페이지)
  DETAIL : GET /rndgate/eg/un/ra/view.do?roRndUid=...&flag=rndList
            → SSR HTML, 본문 + 첨부 링크

'접수중' / '접수예정' 만 채택해 마감 이력은 건너뛴다. 페이지가 연속 마감만
나오면 활성 영역 종료로 판단해 조기 중단.
"""

from __future__ import annotations

import html as html_lib
import re
import time
from typing import Iterator

import requests

from .base import AnnouncementRecord, BaseCrawler, CategorySpec, SourceSpec

BASE_URL = "https://www.ntis.go.kr"
LIST_PATH = "/rndgate/eg/un/ra/mng.do"
DETAIL_PATH = "/rndgate/eg/un/ra/view.do"

LIST_URL = BASE_URL + LIST_PATH
DETAIL_URL = BASE_URL + DETAIL_PATH

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip(s: str | None) -> str | None:
    if s is None:
        return None
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html_lib.unescape(s))).strip() or None


def _ymd(s: str | None) -> str | None:
    if not s:
        return None
    m = re.match(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s.strip() or None


def _d_day_text(end_date: str | None) -> str | None:
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


_ROW_RE = re.compile(
    r'<tr[^>]*>'
    r'\s*<td[^>]*>\s*<input[^>]+value="(?P<uid>\d+)"[^>]*/?>\s*</td>'  # 체크박스 = roRndUid
    r'\s*<td[^>]*data-title="순번"[^>]*>(?P<seq>[\s\S]+?)</td>'
    r'\s*<td[^>]*data-title="현황"[^>]*>(?P<status>[\s\S]+?)</td>'
    r'\s*<td[^>]*data-title="공고명"[^>]*>[\s\S]+?title="(?P<title>[^"]+)"[\s\S]+?</td>'
    r'\s*<td[^>]*data-title="부처명"[^>]*>(?P<dept>[\s\S]+?)</td>'
    r'[\s\S]+?<td[^>]*data-title="접수일"[^>]*>(?P<start>[\s\S]+?)</td>'
    r'\s*<td[^>]*data-title="마감일"[^>]*>(?P<end>[\s\S]+?)</td>'
    r'[\s\S]+?<td[^>]*data-title="D-day"[^>]*>(?P<dday>[\s\S]+?)</td>',
    re.IGNORECASE,
)

# 상세 페이지 본문: <div class="notice_view"> 안의 메타+본문 표
_DETAIL_BODY_RE = re.compile(
    r'<div[^>]+class="notice_view"[^>]*>([\s\S]+?)</div>\s*</div>',
    re.IGNORECASE,
)

# 첨부파일 — NTIS 는 fn_fileDownload('파일ID','토큰') 호출이라 단순 GET URL 이 없음.
# 파일명만 수집해서 사용자가 detail_url 에서 직접 받도록 안내.
_ATTACH_RE = re.compile(
    r"<a[^>]+onclick=\"javascript:fn_fileDownload\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)[^>]*>([^<]+)</a>"
)


class NtisCrawler(BaseCrawler):
    """NTIS R&D 통합공고 크롤러 — 접수중·접수예정만 수집."""

    source = SourceSpec(
        code="ntis",
        name="NTIS 국가R&D통합공고",
        base_url=BASE_URL,
        categories=[
            CategorySpec(
                code="rnd_active",
                name="접수중·접수예정",
                list_url=LIST_URL,
            ),
        ],
    )

    _ACTIVE_STATUSES = {"접수중", "접수예정"}

    def __init__(
        self,
        request_delay: float = 0.3,
        max_pages: int = 200,
        fetch_detail: bool = True,
        timeout: int = 30,
        inactive_stop_threshold: int = 30,
    ):
        """
        Parameters
        ----------
        inactive_stop_threshold
            연속 N 페이지가 전부 마감(활성 0건)이면 조기 종료. NTIS 는 활성/마감
            을 페이지 전반에 드문드문 섞어 보내므로 5 같은 작은 값으로 끊으면
            깊은 페이지의 활성 공고를 놓친다. 30 이면 약 300건 마감을 통과하며
            전체 활성 공고를 잡을 확률이 매우 높다.
        """
        self.request_delay = request_delay
        self.max_pages = max_pages
        self.fetch_detail = fetch_detail
        self.timeout = timeout
        self.inactive_stop_threshold = inactive_stop_threshold

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # ─── public ──────────────────────────────────────────────────────────
    def crawl(self) -> Iterator[AnnouncementRecord]:
        # 초기 세션 (JSESSIONID/WMONID 받기)
        try:
            self.session.get(LIST_URL, timeout=self.timeout)
        except requests.RequestException as e:
            print(f"  [ntis] 초기 세션 실패: {e}")
            return

        total_yielded = skipped_closed = 0
        consecutive_inactive_pages = 0

        for page in range(1, self.max_pages + 1):
            try:
                r = self.session.get(
                    LIST_URL, params={"pageIndex": page},
                    timeout=self.timeout,
                )
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"  [ntis] page {page} 실패: {e}")
                return

            items = list(_ROW_RE.finditer(r.text))
            if not items:
                print(f"  [ntis] page {page}: 행 없음 — 종료")
                break

            page_yielded = 0
            for m in items:
                status_text = _strip(m.group("status")) or ""
                if status_text not in self._ACTIVE_STATUSES:
                    skipped_closed += 1
                    continue
                rec = self._build_record(m)
                if rec is None:
                    continue
                page_yielded += 1
                total_yielded += 1
                yield rec
                if self.fetch_detail:
                    time.sleep(self.request_delay)

            print(f"  [ntis] page {page}: {page_yielded}/{len(items)} 활성")

            if page_yielded == 0:
                consecutive_inactive_pages += 1
                if consecutive_inactive_pages >= self.inactive_stop_threshold:
                    print(f"  [ntis] 활성 영역 종료 — page {page} 이후 "
                          f"{self.inactive_stop_threshold}페이지 연속 마감")
                    break
            else:
                consecutive_inactive_pages = 0

            time.sleep(self.request_delay)

        print(f"  → NTIS 총 {total_yielded}건 수집 (마감 제외 {skipped_closed}건)")

    # ─── record build ────────────────────────────────────────────────────
    def _build_record(self, m: re.Match) -> AnnouncementRecord | None:
        uid = m.group("uid").strip()
        title = html_lib.unescape(m.group("title").strip())
        if not uid or not title:
            return None

        status_text = _strip(m.group("status")) or ""
        department = _strip(m.group("dept"))
        start_date = _ymd(_strip(m.group("start")))
        end_date = _ymd(_strip(m.group("end")))
        d_day = _strip(m.group("dday")) or _d_day_text(end_date)
        # NTIS 의 D-day 셀은 " D - 9 " 같이 공백이 끼어있으므로 정리
        if d_day:
            d_day = re.sub(r"\s*-\s*", "-", d_day)

        detail_url = f"{DETAIL_URL}?roRndUid={uid}&flag=rndList"

        content_text = None
        attachments: list[dict] = []
        if self.fetch_detail:
            try:
                content_text, attachments = self._fetch_detail(uid)
            except requests.RequestException as e:
                print(f"  [ntis] 상세 실패 {uid}: {e}")

        status = "모집중" if status_text == "접수중" else (
            "예정" if status_text == "접수예정" else status_text or None
        )

        raw_meta = {
            "roRndUid": uid,
            "ntis_seq": _strip(m.group("seq")),
            "raw_status": status_text,
        }

        return AnnouncementRecord(
            category_code="rnd_active",
            external_id=uid,
            title=title,
            detail_url=detail_url,
            status=status,
            d_day=d_day,
            reg_date=None,
            start_date=start_date,
            end_date=end_date,
            department=department,
            contact=None,
            content_text=content_text,
            raw_meta=raw_meta,
            attachments=attachments,
        )

    def _fetch_detail(self, uid: str) -> tuple[str | None, list[dict]]:
        r = self.session.get(
            DETAIL_URL,
            params={"roRndUid": uid, "flag": "rndList"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        text = r.text

        # 본문 — notice_view 표 안에 메타+공고문이 들어있음
        body = None
        m = _DETAIL_BODY_RE.search(text)
        if m:
            body = _strip(m.group(1))
        if body and len(body) > 10000:
            body = body[:10000]

        # 첨부파일 — fn_fileDownload('파일ID', '토큰') 호출이라 직접 GET URL 없음.
        # 파일명만 수집, download_url 은 NTIS 상세 페이지로 fallback.
        attachments: list[dict] = []
        seen_ids: set[str] = set()
        for am in _ATTACH_RE.finditer(text):
            file_id = am.group(1)
            if file_id in seen_ids:
                continue
            seen_ids.add(file_id)
            name = html_lib.unescape(am.group(3)).strip()
            if not name:
                continue
            attachments.append({
                "file_name": name,
                "download_url": f"{DETAIL_URL}?roRndUid={uid}&flag=rndList",
            })

        return body, attachments
