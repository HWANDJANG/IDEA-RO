# Claude 개발자 가이드

> 이 파일은 Claude 가 자동 로드합니다. 사용자용 문서는 [README.md](README.md), 이건 **Claude 전용 기술 가이드** — 아키텍처 결정, 함정, 컨벤션, 안티패턴.

---

## 한 줄 오리엔테이션

정부지원사업 4개 사이트 통합 매칭 + PDF LLM 분석 + 카카오/Google/Naver 3-provider 로그인 + Google/Naver 캘린더 직접 등록. 단일 Python `http.server` + SQLite + 단일 SPA (`dashboard.html`).

---

## 핵심 아키텍처 결정 (절대 깨지 마세요)

### 캐싱 4층 — 변경 시 비용/지연 영향 큼

| Layer | 위치 | 키 | 무효화 |
|---|---|---|---|
| 1. 파일 hash | `planner/storage/analyses/{hash}.json` | PDF SHA256 16자 | `SCHEMA_VERSION` 증가 시 자동 |
| 2. 텍스트 추출 | `planner/storage/extracts/{hash}.txt` | PDF hash | 수동 |
| 3. derived (폴더 단위) | `planner/storage/derived/{key}.json` | 폴더ID + PDF hash 정렬 | 컨텐츠 해시 (자동) |
| 4. Gemini 명시 캐시 | Gemini 서버측 | 비교 세션 (announcement_ids + profile) | TTL 10분 자동 |

→ **새 LLM 호출 추가 시 항상 캐싱 가능 여부 검토**. 같은 입력 두 번 → 캐시 미스면 비용/지연 두 배.

### 분석 스키마 버전 (`SCHEMA_VERSION`)

[analyzer.py](planner/analyzer/analyzer.py) 의 `SCHEMA_VERSION` 상수. `analyze_pdf` 가 저장하는 JSON 구조 바뀌면 **반드시 +1**. 그러면 기존 캐시가 자동 무효화되어 재분석됨.

현재 v2 — 7카테고리 (eligibility, warnings, schedule, support_amount, required_docs, evaluation, obligations).

### 사용자 데이터 격리 (Phase 2 + 3 + 6)

**모든 사용자별 데이터는 DB + `user_id` FK**. `localStorage` 는 비로그인 fallback 외엔 사용 금지.
- `attachment_folders.user_id`
- `uploaded_attachments.user_id`
- `user_profiles.user_id`
- `google_tokens.user_id` / `naver_tokens.user_id`
- `user_documents.user_id` (Phase 6, 이전 localStorage 에서 이관)

**새 테이블 만들 때 user_id FK 잊지 말 것**. 안 하면 사용자 간 데이터 새는 버그 생김.

### OAuth 3-provider 패턴

각 프로바이더가 `auth.py` 안에 동일 패턴으로 미러링:
- `{provider}_authorize_url()` — 동의 화면 URL 생성
- `exchange_{provider}_code_for_token()` — code → token
- `fetch_{provider}_user_info()` — token → user info
- `upsert_user_from_{provider}()` — DB upsert

새 프로바이더 추가 시 위 4개 함수 + `web.py` 라우트 2개 (`/login`, `/callback`) + 프론트 버튼 3곳 (index.html 모달, onboarding.html, dashboard.html 상단/프로필).

### LLM 비교/채팅의 "사실만, 판단 없이" 원칙

[prompts.py:COMPARE_SYSTEM_PROMPT](planner/analyzer/prompts.py) 가 "어느 게 베스트" 같은 단정 금지. 측면별 우열만 짚도록 강제. **이 톤을 깨지 말 것** — 사용자 명시 원칙임.

채팅 단계에서 사용자가 자기 우선순위 입력하면 그제서야 추천 톤 가능. 초기 출력은 항상 중립.

---

## 흔한 함정 (이번 프로젝트 한정)

### 127.0.0.1 vs localhost

**Naver OAuth 가 `localhost` 거부**. 모든 redirect URI 를 `127.0.0.1` 로 통일. 일부만 localhost 면 OAuth 콜백 후 세션 쿠키 다른 origin 으로 발급돼서 로그인 풀림. `.env` 의 `*_REDIRECT_URI` 3개 모두 + 각 개발자센터 + 사용자 접속 URL 다 동일해야 함.

### Google Cloud Console — Test users 필수

OAuth 동의 화면이 "테스트 중" 상태면 등록된 test users 만 로그인 가능. 안 등록된 Gmail 로는 403 차단. Cloud Console → OAuth consent screen → Audience → Test users 에 추가.

### Naver — 캘린더 API 별도 추가 + 토큰 재발급

네이버 개발자센터에서 "네이버 로그인" 외에 **"캘린더" API 별도 신청** 안 하면 캘린더 호출 401. 신청 후엔 **기존 토큰은 권한 자동 추가 안 됨** — 사용자가 로그아웃 + `auth_type=reauthenticate` 로 강제 재로그인해야 새 토큰에 캘린더 권한 포함.

코드에 자동 처리: `_handle_naver_calendar_insert` 가 401 + errorCode 024 감지 시 DB 토큰 삭제 + `NAVER_AUTH_EXPIRED` 응답 + 프론트가 `?force=1` 으로 재로그인 유도.

### 서버 재시작 시 좀비 listener

`python -m planner.web` 중복 실행 시 같은 포트에 여러 instance 가 lock 안 잡고 살아있을 수 있음. 새 코드가 적용 안 되는 것처럼 보이면:
```powershell
netstat -ano | Select-String ":8765\s+0.0.0.0:0\s+LISTENING"
# PID 확인 후 Stop-Process -Id <pid> -Force
```

### 비교 매트릭스 — DB 일정과 보유 서류 만료의 ID 충돌

캘린더 이벤트에서 DB 일정은 양수 ID, 보유 서류 만료 (localStorage 계산) 는 음수 synthetic ID. 외부 캘린더 등록 시 `event_ids` 로 보내지 말고 `events: [{...}]` 전체 데이터 배열로 보낼 것 — 백엔드가 음수 ID 는 DB 조회 못 함.

---

## 컨벤션

### 새 API 엔드포인트 추가

1. `web.py` 의 `do_GET` 또는 `do_POST` 에 라우트 elif 추가
2. `_handle_{name}()` 메서드 작성
3. 첫 줄에 `user_id = self._require_user_id()` + `if user_id is None: return` (인증 필요한 경우)
4. body 파싱 → 검증 → DB/외부 호출 → `self._send_json({...})`

### 새 크롤러 추가

1. `crawlers/{name}.py` 에 `BaseCrawler` 상속 클래스 작성
2. `run_crawler.py` 의 `CRAWLERS` dict 에 등록
3. K-Startup 외에는 자격 메타가 없을 가능성 높음 → `backfill_eligibility.py` 가 LLM 보강 처리

### 새 LLM 프로바이더 추가

1. `planner/analyzer/llm/{name}.py` 에 `LLMProvider` 상속
2. `complete()` 시그니처: `(system, user, response_schema=None, max_tokens=2000, *, cached_content=None)` — `cached_content` 필수 (캐싱 미지원이면 무시)
3. `create_cache()`, `supports_caching()` 오버라이드 (선택)
4. `registry.py` 에 등록

### 새 카테고리/필드 추출 (PDF 분석)

1. `prompts.py` 의 `EXTRACTION_SCHEMA` 에 필드 추가 (Gemini 가 받는 JSON Schema 형식)
2. `EXTRACTION_SYSTEM_PROMPT` 에 추출 기준 명시
3. `analyzer.py` 의 `SCHEMA_VERSION` +1
4. `_validate_analysis` 의 `page_sections` 튜플에 새 카테고리 추가 (page 번호 검증용)
5. `format_analysis_summary` 에 직렬화 로직 추가 (비교/채팅용)
6. `dashboard.html` 의 `renderAnalysis` + `renderMergedSummary` 에 표시 추가

### 새 첨부 스크래퍼 추가 (다른 정부 사이트)

1. `attach_fetcher.py` 에 `_scrape_{source}(url) -> list[dict]` 추가 (`{"name": str, "url": str, "is_pdf": bool}`)
2. `detect_source(url)` 에 host 매칭 분기 추가
3. `scan_attachments_from_url(url)` 의 dispatch elif 에 등록
4. 기본은 BeautifulSoup 으로 `a[href]` 순회 + `.pdf`/`.hwp` 등 화이트리스트. JS-차단(IRIS 형) 사이트는 `warning` 으로 fallback.

### 새 캘린더 이벤트 유형 추가 (자동 분류)

1. `dashboard.html` 의 `CAL_EVENT_CATEGORIES` 에 `{label, icon}` 추가 (색은 X — 색=폴더, 아이콘=의미)
2. `classifyCalEvent(ev)` 의 `TYPE_MAP` (정형 type 컬럼) 또는 제목 키워드 분기에 추가
3. **합성어 주의** — "서류평가" 는 evaluation 으로 가야 함. `평가/심사` 가 `서류` 보다 먼저 매칭되게 순서 유지

### 공고 페이지 URL 첨부 자동 수집 흐름

사용자가 매칭 카드의 📎 PDF → URL 탭에서 K-Startup/NRF/NTIS 공고 URL 붙여넣기:
1. `POST /api/attachments/scan-url` → `scan_attachments_from_url(url)` → `{source, files:[...], warning?}`
2. 프론트가 PDF 만 선택 가능하게 체크박스 표시 (HWP 등은 disabled)
3. 사용자 선택 → `POST /api/attachments/import-url` 반복 호출 (1건씩 download_to_bytes → analyze_pdf → DB)
4. **Synap 뷰어 URL 직접 처리 X** — K-Startup `a.btn_down[href]` 의 다운로드 URL 사용 (`/afile/fileDownload/{key}`)

---

## 안티패턴 (하지 말 것)

| ❌ | 대신 |
|---|---|
| 사용자 데이터를 localStorage 에 저장 | DB + user_id FK (Phase 6 패턴 따라하기) |
| PDF 전문을 매 LLM 호출마다 보내기 | `format_analysis_summary` 로 잔축 (90%+ 토큰 절감) |
| 캘린더에 일정 자동 push (사용자 동의 없이) | 수동 선택 → 클릭 (현재 패턴 유지) |
| LLM 응답에 "X 가 베스트" 같은 단정 | 측면별 우열만 짚기 (사용자 명시 원칙) |
| 새 OAuth provider 추가 시 기존 helper 와 다른 패턴 | `auth.py` 미러링 패턴 따라하기 |
| `localhost` 와 `127.0.0.1` 섞어 쓰기 | `127.0.0.1` 통일 (Naver 강제) |
| 한 LLM 호출 결과를 캐싱 없이 매번 재계산 | derived 캐시 패턴 (`storage.load_derived` / `save_derived`) |
| **캘린더 기간 이벤트를 매일 칸에 칩으로 복제** | **주 단위 timeline bar (`grid-column: span N`)** — 데이터 도배 방지 |
| **캘린더에 카테고리 색을 폴더 색보다 우선 적용** | **bar 색 = 폴더(공고), 카테고리 = 아이콘 prefix, D-day = 작은 빨강 배지** — 공고 정체성이 1순위 |
| **LLM 응답에 `escapeHtml` 만 적용해서 raw 마크다운 노출** | `_renderCompareMarkdown` 사용 — escape 후 패턴 치환 (XSS 안전) |
| **K-Startup 첨부에 Synap 뷰어 URL 직접 사용** | `a.btn_down[href]` 의 다운로드 URL 사용 (`/afile/fileDownload/{key}`) |
| **연락처 (전화+URL) 를 escape 만 한 plain text 로** | `_formatContact` — 전화 포맷팅 + URL 한글 디코딩 + 호스트 위주 표시 + `tel:`/`http:` 링크 |

---

## 사용자 선호 (이 프로젝트)

- **언어**: 한국어 우선 (코드 주석/문서/UI 모두)
- **톤**: 비교 결과는 사실만 시각화 (1차 매트릭스). LLM 추천은 사용자가 우선순위 입력 후만.
- **자동화**: 캘린더 양방향 동기화 미요청 — 수동 선택으로 충분
- **비용**: LLM 호출 최소화 우선 (캐싱, 요약, RAG 도입 가능 영역)
- **답변 길이**: 짧고 명확, 표 활용, 예시 코드는 최소
- **명령**: 영어 약어/jargon 안 쓰고 한국어로 풀어 설명
- **확인 단계**: 큰 변경 (300줄 이상 또는 여러 파일) 전에는 짧게 계획 보여주고 ok 받기

---

## 빠른 파일 맵

| 무엇 | 어디 |
|---|---|
| HTTP 라우트 등록 | [web.py](planner/web.py) 의 `do_GET` / `do_POST` |
| DB 스키마/마이그레이션 | [db.py](db.py) `SCHEMA` + `_MIGRATIONS` |
| LLM 프롬프트 + 스키마 | [prompts.py](planner/analyzer/prompts.py) |
| 분석 메인 (PDF→7카테고리) | [analyzer.py](planner/analyzer/analyzer.py) `analyze_pdf` |
| 비교/채팅 LLM | [analyzer.py](planner/analyzer/analyzer.py) `compare_announcements`, `chat_compare` |
| OAuth 3종 | [auth.py](planner/auth.py) |
| 카카오/Google/Naver 캘린더 호출 | [web.py](planner/web.py) `_handle_{provider}_calendar_insert` |
| 사용자별 보유 서류 | [web.py](planner/web.py) `_handle_my_docs_list`/`_save` |
| 매칭 로직 | [matcher.py](planner/matcher.py) `compute_profile_fit`, `match_announcement` |
| **공고 유형 분류 (6묶음)** | [matcher.py](planner/matcher.py) `classify_announcement_type` + `ANNOUNCEMENT_TYPE_INFO` |
| **공고 URL → 첨부 PDF 스크랩** | [attach_fetcher.py](planner/attach_fetcher.py) `scan_attachments_from_url`, `download_to_bytes` |
| 서류 마스터 (30개) | [document_master.py](planner/document_master.py) |
| 프론트 (6탭 SPA) | [public/dashboard.html](public/dashboard.html) |
| **캘린더 렌더 (주 단위 bar)** | [public/dashboard.html](public/dashboard.html) `renderCalendar` + `CAL_EVENT_CATEGORIES` |
| **비교 결과 마크다운 렌더** | [public/dashboard.html](public/dashboard.html) `_renderCompareMarkdown`, `_renderInlineMd` |
| 로그인 모달 | [public/index.html](public/index.html) |

---

## 운영 빠른 명령

```bash
# 서버 시작
python -m planner.web

# 크롤러 (전체)
python run_crawler.py

# 자격 메타 LLM 보강 (비-K-Startup 공고)
python backfill_eligibility.py

# Windows 좀비 listener 정리 (PowerShell)
$pids = (netstat -ano | Select-String ":8765\s+0.0.0.0:0\s+LISTENING") | ForEach-Object { ($_ -split "\s+")[-1] }
$pids | ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {} }
```

서버 주소는 **반드시 `http://127.0.0.1:8765`** (localhost X).
