# Claude 개발자 가이드

> 이 파일은 Claude 가 자동 로드합니다. 사용자용 문서는 [README.md](README.md), 이건 **Claude 전용 기술 가이드** — 아키텍처 결정, 함정, 컨벤션, 안티패턴.

---

## 한 줄 오리엔테이션

정부지원사업 4개 사이트 통합 매칭 + PDF LLM 분석 + 카카오/Google/Naver 3-provider 로그인 + Google/Naver 캘린더 직접 등록 + **엔드 투 엔드 맞춤 플랜** (추천→가중치→narrative→담기→가이드→캘린더). 단일 Python `http.server` + SQLite + 단일 SPA (`dashboard.html`).

---

## ★ 개발 워크플로 (사용자 명시 — 이 패턴 그대로 유지)

**작업 → 커밋 → 푸쉬를 한 흐름으로 진행한다.** 사용자에게 "커밋해도 되나요?" 묻지 말 것.

```
[코드 수정] → [Bash: git add 파일들] → [Bash: git commit -m "..."]
              → [Bash: git push origin main] → [한 줄 안내: AWS deploy 실행 권장]
```

**Why**: 사용자가 도메인을 AWS 서버에 연결해놨고 `idea-ro` systemd 서비스로 상시 가동 중. push 안 하면 사용자가 변화를 볼 수 없음. 그래서 사용자가 묻기 전에 push 까지 한 흐름이 기본.

**예외**:
- 사용자가 "커밋만 하고 push 는 하지 마" 명시 시 그대로 따름
- 큰 변경 (300줄 이상 또는 여러 파일) 은 사전 계획 보여주고 ok 받은 후 작업 → 작업 시작했으면 push 까지 자동 진행
- `.env` / 시크릿 / DB 파일 같은 gitignored 파일은 git add 금지

**커밋 메시지 컨벤션**:
- `feat:` 신규 기능 / `fix:` 버그 / `style:` UI·CSS / `docs:` 문서 / `refactor:`
- 한국어 본문 + 핵심 한 줄 + 이유 + 영향
- 마지막에 `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`
- HEREDOC `$(cat <<'EOF'...EOF)` 패턴 사용 (개행 보존)

**Push 후 안내** (한 줄):
```
AWS 에서 `deploy` (또는 `cd ~/IDEA-RO && git pull && sudo systemctl restart idea-ro`) 실행해주세요.
```
- HTML/CSS 만 바뀐 경우는 "강력 새로고침(Ctrl+F5)만으로도 반영" 으로 보강
- `web.py` 라우트 / `planner.py` Python 코드 바뀌면 **반드시 systemctl restart**
- 사이트: https://idea-ro.site

**Step-by-step 시연 흐름**:
- 사용자는 큰 기능을 작은 step 으로 쪼개서 한 번에 1단계씩 진행하길 선호함 (예: 맞춤 플랜 Step 1 → 2 → 2.5 → 3 → 4 → 5 → A → B → C)
- 각 step 끝에 짧은 동작 검증 + commit + push + 다음 step 시작 여부 확인
- "한 번에 다 짜줘" 보다 단계적 누적이 디버깅·rollback 쉬움

---

## 핵심 아키텍처 결정 (절대 깨지 마세요)

### 캐싱 5층 — 변경 시 비용/지연 영향 큼

| Layer | 위치 | 키 | 무효화 |
|---|---|---|---|
| 1. 파일 hash | `planner/storage/analyses/{hash}.json` | PDF SHA256 16자 | `SCHEMA_VERSION` 증가 시 자동 |
| 2. 텍스트 추출 | `planner/storage/extracts/{hash}.txt` | PDF hash | 수동 |
| 3. derived (폴더 단위) | `planner/storage/derived/{key}.json` | 폴더ID + PDF hash 정렬 | 컨텐츠 해시 (자동) |
| 4. Gemini 명시 캐시 | Gemini 서버측 | 비교 세션 (announcement_ids + profile) | TTL 10분 자동 |
| 5. **맞춤 플랜 derived** | `planner/storage/derived/plan_{narrative\|guide}_{user_id}_{hash}.json` | narrative: user + ann_ids + weights / guide: **sys_prompt sha + user + picked + today + PDF hashes** | guide 는 **시스템 프롬프트 수정 시 자동 invalidate** (sha 포함) + PDF 추가/삭제 시 자동 invalidate |

→ **새 LLM 호출 추가 시 항상 캐싱 가능 여부 검토**. 같은 입력 두 번 → 캐시 미스면 비용/지연 두 배.
→ **시스템 프롬프트 변경 후 캐시 invalidate 가 필요한 모든 함수는 키에 `hashlib.sha256(_SYSTEM_PROMPT).hexdigest()[:6]` 포함**. guide 가 모델 패턴.

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

### 맞춤 플랜에 새 신호 (signal) / 가이드 컨텍스트 추가

새 정보를 narrative 또는 guide LLM 입력에 추가하려면 다음 순서:

1. **`planner/planner.py` 의 `compose_action_plan`**:
   - 1단계 (light score) 에서 계산 가능하면 `signals[...]` 에 추가
   - 2단계 (Top N detail) 에서 계산 가능하면 거기에 추가
   - 추천 응답 dict 의 적절한 키에 노출
2. **`_NARRATIVE_USER_TMPL` / `_GUIDE_USER_TMPL`** 의 카드 직렬화 함수 (`_card_for_narrative`, `_picked_card_block`) 에 새 정보 줄 추가
3. **`_NARRATIVE_SYSTEM` / `_GUIDE_SYSTEM` 프롬프트** 에 사용 가이드 추가 (예: "지원금 ≥1억원 + 노력 '높음' → 다음 주 가이드에 사업계획서 시간 명시")
4. **응답 검증** — `generate_xxx(user_id, plan, use_cache=False)` 로 호출해 새 정보가 실제 반영되는지 확인
5. **캐시 키 자동 invalidate** — 시스템 프롬프트 변경됐으므로 `_guide_cache_key` 의 `sys_hash` 가 자동 갱신 (별도 작업 X)

→ Step A/B/C 가 이 패턴의 살아있는 예시. 매번 새 신호 추가 시 같은 순서 따라가면 됨.

### 공고 페이지 URL 첨부 자동 수집 흐름

사용자가 매칭 카드의 📎 PDF → URL 탭에서 K-Startup/NRF/NTIS 공고 URL 붙여넣기:
1. `POST /api/attachments/scan-url` → `scan_attachments_from_url(url)` → `{source, files:[...], warning?}`
2. 프론트가 PDF 만 선택 가능하게 체크박스 표시 (HWP 등은 disabled)
3. 사용자 선택 → `POST /api/attachments/import-url` 반복 호출 (1건씩 download_to_bytes → analyze_pdf → DB)
4. **Synap 뷰어 URL 직접 처리 X** — K-Startup `a.btn_down[href]` 의 다운로드 URL 사용 (`/afile/fileDownload/{key}`)

### 맞춤 플랜 (엔드 투 엔드) — Pipeline 구조와 누적 단계

**위치**: [planner/planner.py](planner/planner.py) + [web.py](planner/web.py) `_handle_plan`/`_handle_plan_narrative`/`_handle_plan_guide` + [dashboard.html](public/dashboard.html) `loadPlan`/`renderPlan`/`_fetchPlanGuideAsync`.

**전체 흐름**:
```
[GET /api/plan?top_n=...&w_amount=...&w_effort=...&w_urgency=...]
    ↓ compose_action_plan(user_id, top_n, weights)
    ↓ Pipeline:
    ↓  1) DB 로드 (profile + my_docs + 진행중 공고)
    ↓  2) 모든 자격 통과 공고에 light score
    ↓     (profile_fit + urgency*w_urgency + amount*w_amount - effort_light*w_effort)
    ↓  3) Top N detail match (match_announcement) → effort 보정 → 최종 정렬
    ↓ → Top N 카드 (signals 포함)
즉시 표시
    ↓
[POST /api/plan/narrative {plan}]  ← 카드 즉시 표시 후 백그라운드
    ↓ generate_recommendation_narratives → 카드별 한 줄
    ↓
[사용자 "+ 내 플랜에 담기" → 700ms debounce]
    ↓
[POST /api/plan/guide {plan, picked_ids}]
    ↓ generate_action_guide (Step A→B→C 누적)
    ↓  · A: missing/fulfilled/expired/expiring docs 명단 + 발급처 매핑
    ↓  · B: profile_fit.reasons + 유형/지원금/노력
    ↓  · C: 담은 공고의 PDF 분석 (analyses/{hash}.json) → format_analysis_summary
```

**가중치 점수 공식** (compose_action_plan):
```
combined = int(profile_fit.score)
         + int(urgency_score * w_urgency)
         + int(amount_score  * w_amount)
         - int(effort_score  * w_effort)
```
- 슬라이더 0~100 → 백엔드 0~1 변환
- 각 점수의 base 범위: profile 0~100, urgency 0~30, amount 0~30, effort 0~30

**LLM 호출 캐시 키 패턴** (모방용 템플릿):
```python
# 1) 시스템 프롬프트 해시를 키에 포함 → 프롬프트 수정 시 자동 invalidate
sys_hash = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:6]
# 2) 사용자 + 입력의 핵심 식별자 + 날짜(필요 시)
parts = [sys_hash, str(user_id), today.isoformat(), ",".join(sorted(input_ids))]
# 3) 추가 입력 (예: PDF hash) 도 키에 포함 → 컨텐츠 바뀌면 자동 invalidate
parts.append(",".join(sorted(pdf_hashes)))
key = f"plan_xxx_{user_id}_{hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]}"
```

**Step C — 공고 ↔ PDF 분석 매핑**:
```
announcement_id → attachment_folders.announcement_id == ann_id AND user_id == user_id
              → folder_ids 의 uploaded_attachments.file_hash 들 (status='ok')
              → planner/storage/analyses/{file_hash}.json
              → format_analysis_summary 로 잔축 (한 PDF 당 ~1~2K 토큰)
```
- 분석 없는 공고는 graceful — Step B 동작과 동일, 사용자 체감 무리 없음
- 프론트는 `pdf_analyzed_count > 0` 일 때만 `📄 PDF N건 활용` 배지 노출

**컨설팅 톤 원칙 (사용자 명시)**:
- narrative: "X가 베스트" 단정 금지. 측면별 매칭 포인트만. ([prompts COMPARE_SYSTEM_PROMPT 원칙과 동일](planner/analyzer/prompts.py))
- guide: 동사 명령형 ("~하세요"), 추상 표현 금지 ("지원서류 준비" X → "사업자등록증명 발급" O)
- 발급처 매핑: 정부24 / 홈택스 / 위택스 / 4대보험 (잘 모르면 정부24)

### 디자인 v2 — 사이드바 / 카드 / 프로필 입히기 원칙

**대원칙**: "기능 유지한 채 디자인만 입히기". 구조 변경이 기능을 해치는 경우 디자인 포기하고 보고. 사용자 명시.

1. **사이드바 v2 (`.app-sidebar[data-workspace=...]`)**:
   - 상단 워크스페이스 스위처 (`#sb-switcher`) — `서류 체커` / `아이디어 검증` 토글. `setWorkspace()` 가 `data-workspace` 속성 변경 → CSS 가 `.sb-nav-docs` / `.sb-nav-ideas` 토글.
   - **profile 탭 없음** — 사이드바 하단 계정 카드 (`#sb-acc-profile`) 클릭으로만 진입. `renderAuthArea()` 에서 onclick 핸들러 부착 (탭 버튼이 아니므로 일반 `.tab-btn` 클릭 핸들러 안 탐).
   - 비로그인 시 `#sb-account` 에 3-provider 로그인 버튼 (카카오/G/N 세로 스택).
2. **매칭 카드 v2 (`#announcements-list` 스코프 한정 — 다른 탭의 카드 안 건드림)**:
   - 외부: `.match-main` (flex:1) + `.match-cmp-zone` (width:78px) 의 flex row. 우측 zone 은 점선 border-left + 상하 절취 도트 (`::before`/`::after`) — 쿠폰 절단선 시각화.
   - 비교 체크박스는 시각적으로 숨기고 (`.match-cmp-zone input { opacity:0 }`) `.mcz-box`(26px) + `.mcz-check`(✓) 로 대체 — **기능은 그대로 (`toggleCompareSelect(idx)`)**.
   - 펼침 토글: `.match-toggle` 의 `.mt-text::after` content 가 expanded 클래스 유무로 "상세보기"/"접기" 자동 전환 (JS 텍스트 변경 X).
3. **3단계 doc-status-pill**:
   - `sum.missing > 0` 또는 `fulfillment === "none"` → `ds-short` (빨강 "서류 부족" ✗)
   - `sum.expired > 0` 또는 `sum.expiring > 0` → `ds-risk` (주황 "만료 위험" ⚠)
   - 그 외 → `ds-complete` (초록 "서류 완비" ✓)
4. **홈 화면 컴팩트 카드 (`.home-sched-card`)**:
   - 좌측 62px 색 블록 D-day: `urgent` (≤3일/오늘/진행중 빨강) / `imm` (≤7일 주황) / `calm` (그 외 파랑).
   - 일정 데이터 자체는 동일 (`renderHomeEvents()` 가 결과만 새 마크업으로 출력) — 렌더링만 변경.
5. **프로필 페이지 3섹션 분리** (`renderProfilePanel()`):
   - `.profile-section-h` 3개로 grid 를 분리 — ① 기본 정보 / ② 소재지·연락처 / ③ 업종 카드.
   - 지역(`pf-region-display`) 은 사용자가 직접 입력 못 함 (readonly + bg-subtle). Daum postcode 콜백이 hidden `pf-region` 과 함께 동시 set.
   - 모든 `pf-*` ID 가 `saveProfileForm()` 의 querySelector 와 매칭 — **ID 바꾸면 저장 깨짐**.

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
| **매칭 카드 디자인 변경을 모든 `.announcement-card` 에 적용** | `#announcements-list .announcement-card` 스코프 한정 — 둘러보기 등 다른 탭의 카드 안 건드림 |
| **`pf-*` 입력 ID 를 자유롭게 리네임** | `saveProfileForm()` 의 querySelector 와 1:1 매칭 — 바꾸면 프로필 저장 깨짐 |
| **사이드바에 `data-tab="profile"` 탭 버튼 추가** | profile 은 사이드바에 없음 — `#sb-acc-profile` (계정 카드) 클릭 핸들러로만 진입 |
| **워크스페이스 nav 토글을 JS 로 매번 show/hide** | `[data-workspace=...]` 속성 + CSS 셀렉터로 토글 (`setWorkspace()` 가 속성만 변경) |
| **홈 다가오는 일정에 D-day 색을 단일 색으로** | urgent (≤3) / imm (≤7) / calm 3단계 그라데이션으로 우선순위 시각화 |
| **맞춤 플랜 LLM 캐시 키에 시스템 프롬프트 미반영** | 프롬프트 수정해도 옛 가이드 응답 → 키에 `hashlib.sha256(_SYSTEM_PROMPT)[:6]` 포함 필수 |
| **맞춤 플랜 가이드를 추천 즉시 호출** | 카드 즉시 표시 후 백그라운드로 narrative, "+ 담기" 후 700ms debounce 로 guide — non-blocking |
| **맞춤 플랜 슬라이더 변경마다 즉시 LLM 호출** | 슬라이더는 debounce 300ms 후에만 `/api/plan` 재호출. narrative/guide 는 결과 도착 후 별도 트리거 |
| **맞춤 플랜 narrative 에 사용자 PII 전송** | `_profile_for_narrative` 의 safe 필드만 (company_name·business_type·establishment_date·region·industry·founding_type). 사업자번호/전화/이메일 X |
| **맞춤 플랜 가이드에 "X가 베스트" 단정** | 동사 명령형 "~하세요" + 측면별 매칭 인용 (`COMPARE_SYSTEM_PROMPT` 원칙과 동일) |
| **맞춤 플랜 가이드의 발급처를 LLM 이 추정** | 시스템 프롬프트로 명시 매핑 강제: 주민등록=정부24 / 사업자등록·납세=홈택스 / 지방세=위택스 / 4대보험=4대사회보험정보연계센터 |
| **PDF 분석 결과를 RAW JSON 으로 LLM 에 송신** | `format_analysis_summary` 로 잔축 (한 PDF 1~2K 토큰). 5개 공고 결합도 5~10K 로 통제 |
| **맞춤 플랜 캘린더 등록을 새 API 로 구현** | 기존 `/api/calendar/{provider}/insert` + `_pushEventsToProvider(events, provider)` 헬퍼 그대로 재사용 — events 페이로드만 통합 타임라인에서 생성 |

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
| **🧭 맞춤 플랜 (엔드 투 엔드)** | [planner.py](planner/planner.py) `compose_action_plan`, `generate_recommendation_narratives`, `generate_action_guide` |
| **맞춤 플랜 — 지원금 추출 / 노력 추정** | [planner.py](planner/planner.py) `_extract_amount_won` (regex 4단계), `_estimate_effort` (유형 base + 서류 수) |
| **맞춤 플랜 — PDF 분석 결합 (Step C)** | [planner.py](planner/planner.py) `_load_analyses_for_announcement` (DB join) + `_summarize_analyses` (format_analysis_summary 재사용) |
| **맞춤 플랜 — 프론트** | [dashboard.html](public/dashboard.html) `loadPlan`, `_renderPlanCard`, `_buildPlanTimeline`, `_fetchPlanNarrativesAsync`, `_fetchPlanGuideAsync`, `togglePlanPick` |
| **공고 URL → 첨부 PDF 스크랩** | [attach_fetcher.py](planner/attach_fetcher.py) `scan_attachments_from_url`, `download_to_bytes` |
| 서류 마스터 (30개) | [document_master.py](planner/document_master.py) |
| 프론트 (6탭 SPA) | [public/dashboard.html](public/dashboard.html) |
| **캘린더 렌더 (주 단위 bar)** | [public/dashboard.html](public/dashboard.html) `renderCalendar` + `CAL_EVENT_CATEGORIES` |
| **비교 결과 마크다운 렌더** | [public/dashboard.html](public/dashboard.html) `_renderCompareMarkdown`, `_renderInlineMd` |
| **사이드바 워크스페이스 스위처** | [public/dashboard.html](public/dashboard.html) `setupWorkspaceSwitcher`, `setWorkspace`, `WORKSPACE_NAMES` |
| **사이드바 하단 계정 카드 (프로필 진입점)** | [public/dashboard.html](public/dashboard.html) `renderAuthArea` 의 `#sb-acc-profile` onclick |
| **프로필 3섹션 분리 렌더** | [public/dashboard.html](public/dashboard.html) `renderProfilePanel` |
| 로그인 모달 | [public/index.html](public/index.html) |

---

## 운영 빠른 명령

### 로컬
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

### AWS (배포)
```bash
# 한 줄 (사용자가 alias 등록한 상태)
deploy

# 풀 명령
cd ~/IDEA-RO && git pull && sudo systemctl restart idea-ro

# 서비스 상태 확인
sudo systemctl status idea-ro --no-pager

# 로그
sudo journalctl -u idea-ro -n 50 --no-pager
```

### 주소
- 로컬: **반드시 `http://127.0.0.1:8765`** (localhost X — Naver OAuth 강제)
- 운영: https://idea-ro.site (AWS Lightsail + Cloudflare Named Tunnel — 도메인 가비아)

### Step-by-step LLM 검증 (script)
```bash
# .env 강제 로드 + 새 LLM 함수 동작 검증 (캐시 우회)
python -c "
from planner.analyzer.dotenv import load_dotenv
load_dotenv()
from planner.planner import compose_action_plan, generate_action_guide
plan = compose_action_plan(USER_ID, top_n=5)
picked = [plan['recommendations'][0]['announcement_id']]
out = generate_action_guide(USER_ID, plan, picked, use_cache=False)
print(out['key_warning'])
for s in out['sections']:
    for it in s['items']: print(it)
"
```
