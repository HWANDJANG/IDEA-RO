"""공고 페이지 URL → 첨부 PDF 목록 + 다운로드.

K-Startup / NRF / NTIS 의 detail 페이지 URL 을 받아 첨부 파일 목록을 스크랩한다.
사용자가 매칭 카드의 detail_url 을 직접 붙여넣으면 첨부 PDF 를 자동으로
가져올 수 있게 한다 (Synap 뷰어 우회).

스크랩 로직은 기존 크롤러(crawlers/*) 와 동일한 패턴을 짧게 재구현.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


SourceCode = Literal["kstartup", "nrf", "ntis", "iris", "unknown"]

# PDF 만 우선 지원 (다른 포맷은 LLM 분석 불가). 확장자 화이트리스트.
PDF_EXTS = (".pdf",)
OTHER_DOC_EXTS = (".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".zip")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 15


def detect_source(url: str) -> SourceCode:
    host = (urlparse(url).hostname or "").lower()
    if "k-startup.go.kr" in host:
        return "kstartup"
    if "nrf.re.kr" in host:
        return "nrf"
    if "ntis.go.kr" in host:
        return "ntis"
    if "iris.go.kr" in host:
        return "iris"
    return "unknown"


def scan_attachments_from_url(url: str) -> dict:
    """공고 페이지 URL → 첨부 파일 목록.

    반환:
      {
        "source": "kstartup" | "nrf" | "ntis" | "iris" | "unknown",
        "files": [{"name": str, "url": str, "is_pdf": bool}],
        "warning": str | None,
      }
    """
    src = detect_source(url)
    if src == "iris":
        return {
            "source": src, "files": [],
            "warning": "IRIS 는 JS 차단으로 첨부 자동 수집 불가. 다운로드 후 직접 업로드해주세요.",
        }

    try:
        if src == "kstartup":
            files = _scrape_kstartup(url)
        elif src == "nrf":
            files = _scrape_nrf(url)
        elif src == "ntis":
            files = _scrape_ntis(url)
        else:
            files = _scrape_generic(url)
    except requests.RequestException as e:
        return {"source": src, "files": [], "warning": f"페이지 가져오기 실패: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"source": src, "files": [], "warning": f"스크랩 실패: {e}"}

    if not files:
        return {
            "source": src, "files": [],
            "warning": "첨부 파일을 찾지 못했습니다. URL 이 정확한 공고 상세 페이지인지 확인해주세요.",
        }
    return {"source": src, "files": files, "warning": None}


def download_to_bytes(
    url: str,
    *,
    max_bytes: int = 50 * 1024 * 1024,
    referer: str | None = None,
) -> tuple[bytes, str | None]:
    """첨부 URL → (bytes, content_type). max_bytes 초과 시 HTTPError.

    referer 를 지정하면 Referer 헤더 추가 — NTIS 같은 사이트는 Referer 없으면
    HTML 에러 페이지를 첨부 대신 응답한다.
    """
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, stream=True)
    r.raise_for_status()
    content_length = int(r.headers.get("Content-Length") or 0)
    if content_length and content_length > max_bytes:
        raise ValueError(f"파일이 너무 큽니다 ({content_length // 1024 // 1024}MB > {max_bytes // 1024 // 1024}MB)")
    chunks: list[bytes] = []
    total = 0
    for chunk in r.iter_content(chunk_size=64 * 1024):
        if not chunk: continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"파일이 너무 큽니다 (> {max_bytes // 1024 // 1024}MB)")
        chunks.append(chunk)
    return b"".join(chunks), r.headers.get("Content-Type")


# ─── 출처별 스크래퍼 ────────────────────────────────────────────────────────

KSTARTUP_PORTAL = "https://www.k-startup.go.kr"
KSTARTUP_DETAIL = f"{KSTARTUP_PORTAL}/web/contents/bizpbanc-ongoing.do"


def _scrape_kstartup(url: str) -> list[dict]:
    """K-Startup 상세 페이지 → board_file > ul > li 구조 파싱.

    구조:
      <div class="board_file">
        <ul>
          <li>
            <a class="file_bg" title="..." >파일명</a>
            <div class="btn_wrap">
              <ul>
                <li><a onclick="fnPdfView(...)">바로보기</a></li>
                <li><a href="/afile/fileDownload/XXX" class="btn_down">다운로드</a></li>
              </ul>
            </div>
          </li>
          ...
        </ul>
      </div>
    """
    qs = parse_qs(urlparse(url).query)
    pbanc_sn = (qs.get("pbancSn") or qs.get("pbanc_sn") or qs.get("aanc_sn") or [None])[0]
    if pbanc_sn:
        r = requests.get(
            KSTARTUP_DETAIL,
            params={"schM": "view", "pbancSn": pbanc_sn},
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
        )
    else:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    files: list[dict] = []
    seen_urls: set[str] = set()
    for board in soup.select("div.board_file"):
        for li in board.select("ul > li"):
            # 파일명 — file_bg 우선, 없으면 title 속성
            name_el = li.select_one("a.file_bg")
            if name_el is None:
                continue
            name = (name_el.get_text(strip=True) or
                    (name_el.get("title") or "").replace("[첨부파일]", "").strip())
            if not name:
                continue
            # 다운로드 URL — btn_down 우선
            dl = li.select_one("a.btn_down")
            href = dl.get("href") if dl else None
            if not href or href.startswith("javascript"):
                continue
            if href.startswith("/"):
                href = KSTARTUP_PORTAL + href
            if href in seen_urls:
                continue
            seen_urls.add(href)
            files.append({
                "name": name,
                "url": href,
                "is_pdf": name.lower().endswith(PDF_EXTS),
            })
    return files


def _scrape_nrf(url: str) -> list[dict]:
    """NRF — bs4 로 첨부 ul/li 영역 파싱."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    files: list[dict] = []
    # NRF 첨부 영역: 보통 .file_list 또는 .attach 클래스
    for a in soup.select("a"):
        href = (a.get("href") or "").strip()
        name = a.get_text(strip=True)
        if not href or not name: continue
        if "/download/" not in href.lower() and "fileDown" not in href.lower():
            continue
        if href.startswith("/"):
            href = urljoin(url, href)
        files.append({
            "name": name,
            "url": href,
            "is_pdf": name.lower().endswith(PDF_EXTS),
        })
    return files


def _scrape_ntis(url: str) -> list[dict]:
    """NTIS — 첨부가 JS 로 차단되어 있지만 일부 케이스는 a[href] 노출."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    files: list[dict] = []
    for a in soup.select("a"):
        href = (a.get("href") or "").strip()
        name = a.get_text(strip=True)
        if not href or not name: continue
        # NTIS 첨부 패턴
        if "fileDownload" not in href and "download" not in href.lower():
            continue
        if href.startswith("/"):
            href = urljoin(url, href)
        files.append({
            "name": name,
            "url": href,
            "is_pdf": name.lower().endswith(PDF_EXTS),
        })
    return files


def _scrape_generic(url: str) -> list[dict]:
    """기타 — 페이지의 모든 PDF/HWP 링크 수집."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    files: list[dict] = []
    for a in soup.select("a"):
        href = (a.get("href") or "").strip()
        name = a.get_text(strip=True) or href.rsplit("/", 1)[-1]
        if not href: continue
        lower = href.lower()
        if not (lower.endswith(PDF_EXTS) or lower.endswith(OTHER_DOC_EXTS)):
            continue
        if href.startswith("/") or href.startswith("./") or not href.startswith("http"):
            href = urljoin(url, href)
        files.append({
            "name": name,
            "url": href,
            "is_pdf": lower.endswith(PDF_EXTS),
        })
    return files
