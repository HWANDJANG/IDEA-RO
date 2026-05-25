# 정부지원사업 매칭·비교 서비스 

> 4개 정부 사이트 공고 통합 매칭 + 보유 서류 만료 관리 + PDF 정밀 비교/채팅을 한 화면에서.
> Founderly 본 서비스의 "마이페이지 > 지원서·캘린더·서류 관리" 영역에 들어갈 모듈입니다.

---

## 🎯 무엇을 하는 서비스인가

| 페인포인트 | 우리 해결 방식 |
|---|---|
| 정부 사이트가 4곳 흩어져있어 진행중 공고 찾기 번거로움 | K-Startup / NRF / IRIS / NTIS 통합 크롤링 + 필터 |
| 보유 서류가 언제 만료되는지 기억 안 남 | 발급일 입력 → 자동 만료일 계산, D-30 임박 알림 |
| 매번 공고 보고 자격 일일이 확인 | 기업 프로필 + 보유 서류 기반 자동 매칭 + 점수화 |
| 공고 PDF 본문 다 못 읽음 | PDF 업로드 → LLM 이 7개 카테고리 구조화 추출 |
| 비슷한 공고 여러 개 중 뭘 골라야 할지 | 2~4개 공고를 정형 매트릭스 + LLM 정밀 비교로 나란히 |
| 비교 결과만 봐선 내 상황에 뭐가 맞는지 모름 | 같은 PDF 컨텍스트에 자기 우선순위 입력 → LLM 채팅 |
| 폴더 PDF 들의 일정 중복 정리 | analyze 시 추출된 일정을 Python 으로 dedup (LLM 호출 0회) |
| 본인이 쓰는 캘린더 앱에 일정을 어떻게 옮길지 | 일정 선택 → Google / Naver 캘린더에 원클릭 자동 등록 |
| 카테고리별 일괄 추가 가능 | 캘린더 탭 칩 옆 **[G][N]** 버튼 — "📋 내 서류 만료" 카테고리 전체 한 클릭으로 Google/Naver 캘린더에 등록 |
| 로그인 옵션 부족 | 카카오 / Google / Naver 3가지 OAuth, 마지막 사용 제공자 자동 표시 |
| 보유 서류가 브라우저별 따로 — 같은 PC 에서 다른 계정으로 들어가도 같이 보임 | DB 격리 (Phase 6) — 사용자별로 분리, 첫 로그인 시 기존 localStorage 데이터 자동 이관 |

---

## 🛠 스택

- **백엔드**: Python 3.13 표준 라이브러리(`http.server` + `sqlite3` + `json` + `urllib`)
- **외부 의존**: `requests`, `beautifulsoup4` (크롤러), `pymupdf` (PDF 텍스트), `google-genai` (Gemini Vision/Chat), `anthropic` (선택)
- **프론트엔드**: 단일 `public/dashboard.html` SPA (Vanilla JS, 약 5000줄)
- **DB**: SQLite 단일 파일 (`announcements.db`)
- **파일 저장**: 로컬 디스크 (`planner/storage/`)
- **LLM**: Gemini 2.5 Flash Lite (기본). `LLM_PROVIDER` 환경변수로 Claude/OpenAI 도 가능
- **인증**: 카카오 / Google / Naver OAuth 3종 + HMAC 서명 세션 쿠키 (서버측 세션 테이블 없음)
- **캘린더 연동**: Google Calendar API (`events.insert`) + Naver Calendar API (`createSchedule` iCal)
- **토큰 관리**: refresh_token 영구 DB 저장 → access_token 자동 갱신 (`google_tokens`, `naver_tokens` 테이블)

---

## 📁 디렉터리 구조

```
데이터 베이스/
├── README.md                     ← 지금 이 문서
├── announcements.db              ← SQLite DB
├── db.py                         ← 스키마 + 마이그레이션 (Phase 6 까지)
├── run_crawler.py                ← 크롤러 실행 엔트리
├── backfill_eligibility.py       ← 비-K-Startup 공고에 LLM 자격 메타 보강 (배치)
├── requirements.txt
├── .env                          ← LLM + 카카오/Google/Naver OAuth + 세션 시크릿 (gitignore 됨)
│
├── crawlers/
│   ├── base.py                   ← Crawler 추상 클래스 + 데이터 모델
│   ├── kstartup.py               ← K-Startup OpenAPI (구조화 메타 포함)
│   ├── nrf.py                    ← 한국연구재단 (HTML)
│   ├── iris.py                   ← IRIS 국가R&D (AJAX/JSON)
│   └── ntis.py                   ← NTIS R&D 서비스 (HTML)
│
├── planner/                      ← 메인 백엔드
│   ├── web.py                    ← HTTP 서버 + 모든 API 엔드포인트 (~2300줄)
│   ├── auth.py                   ← 카카오/Google/Naver OAuth + HMAC 쿠키 + Calendar 토큰 관리
│   ├── checker.py                ← 서류 만료 계산 + 발급 태스크
│   ├── matcher.py                ← 공고 ↔ 보유 서류 + 프로필 매칭
│   ├── document_master.py        ← 30개 정부 서류 마스터 데이터
│   ├── eligibility_extractor.py  ← 비-K-Startup 공고 자격 메타 LLM 추출
│   ├── ics_export.py             ← 캘린더 ICS 빌더 (RFC 5545)
│   ├── multipart.py              ← multipart/form-data 파서
│   ├── paths.py                  ← 경로 상수
│   │
│   ├── analyzer/                 ← PDF + LLM 분석 파이프라인
│   │   ├── analyzer.py           ← 메인: hash→캐시→섹션분리→LLM→저장 + 비교/채팅
│   │   ├── extractor.py          ← PyMuPDF 텍스트 + 정규식 섹션 분리(find_sections)
│   │   ├── prompts.py            ← 시스템 프롬프트 + 7카테고리 JSON 스키마
│   │   ├── storage.py            ← PDF/extract/analysis/derived 파일 캐시
│   │   ├── doc_scanner.py        ← 발급내역 이미지 OCR (Gemini Vision)
│   │   ├── dotenv.py             ← .env 로더 (표준 라이브러리)
│   │   └── llm/
│   │       ├── base.py           ← LLMProvider ABC + create_cache 인터페이스
│   │       ├── gemini.py         ← Gemini (text + vision + 명시 캐싱)
│   │       ├── claude.py         ← Anthropic (ephemeral system caching)
│   │       ├── openai.py         ← placeholder
│   │       └── registry.py       ← 환경변수로 프로바이더 선택
│   │
│   └── storage/                  ← 사용자 업로드 + derived 캐시 (gitignore)
│       ├── pdfs/{hash}.pdf
│       ├── extracts/{hash}.txt
│       ├── analyses/{hash}.json  ← 7카테고리 구조화 추출 (schema_version=2)
│       └── derived/{key}.json    ← 폴더-단위 derived (일정 dedup 등)
│
└── public/
    ├── index.html                ← 랜딩 (3-provider 로그인 모달 + Last used 표시)
    ├── onboarding.html           ← 3-provider 로그인 + 기업 프로필 입력
    └── dashboard.html            ← 메인 SPA (6탭, ~5500줄)
```

---

## 🚀 로컬 실행

### 사전 준비

```bash
# Python 3.13
pip install -r requirements.txt
```

### .env 파일 만들기

```env
# LLM
LLM_PROVIDER=gemini
LLM_API_KEY=AIza...본인_키
# LLM_MODEL=gemini-2.5-flash-lite  # 기본값

# 세션 (HMAC 서명 키 — 긴 랜덤 문자열)
SESSION_SECRET=...

# 카카오 OAuth
KAKAO_REST_API_KEY=...
KAKAO_CLIENT_SECRET=...
KAKAO_REDIRECT_URI=http://127.0.0.1:8765/api/auth/kakao/callback

# Google OAuth + Calendar API
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://127.0.0.1:8765/api/auth/google/callback

# Naver OAuth + Calendar API
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
NAVER_REDIRECT_URI=http://127.0.0.1:8765/api/auth/naver/callback

# K-Startup 공공데이터 (선택, 크롤링용)
K_startup_decoding_key=...
```

> ⚠️ **`127.0.0.1` 통일 필수**: Naver 가 `localhost` 를 허용하지 않아서 `127.0.0.1` 로 통일. 한 origin 으로 통일해야 세션 쿠키가 OAuth 콜백 전후로 유지됩니다.

LLM 키 발급:
- Gemini: <https://aistudio.google.com/apikey> (무료, 한국어 좋고 캐시 75% 할인 지원)
- 카카오: <https://developers.kakao.com>

### OAuth + Calendar API 사전 설정

#### 카카오

1. [내 애플리케이션](https://developers.kakao.com/console/app) → 앱 생성
2. **카카오 로그인** 활성화 → Redirect URI 등록: `http://127.0.0.1:8765/api/auth/kakao/callback`
3. **REST API 키** + (필요시) **Client Secret** 발급해서 `.env` 에 입력

#### Google

1. [Cloud Console](https://console.cloud.google.com) → 새 프로젝트
2. **OAuth 동의 화면** 설정 (앱 이름 + 지원 이메일만 필수, 외부 출시 전엔 거의 비워도 됨)
3. **사용자 인증 정보** → "+ 사용자 인증 정보 만들기" → **OAuth 클라이언트 ID** → 웹 애플리케이션
   - 승인된 리디렉션 URI: `http://127.0.0.1:8765/api/auth/google/callback`
4. **Calendar API 활성화** → "APIs & Services > Library" 에서 "Google Calendar API" 사용 설정
5. ⚠️ **테스트 사용자 등록 필수** (앱이 "테스트 중" 상태일 때): OAuth 동의 화면 → **대상 / Test users** → 본인 Gmail 추가. 안 하면 동의 화면에서 403 으로 차단됨

#### 네이버

1. [네이버 개발자센터](https://developers.naver.com/apps) → 애플리케이션 등록
2. **사용 API**: "네이버 로그인" + **"캘린더"** 둘 다 체크 (캘린더 따로 신청 안 하면 일정 등록 401)
3. **PC 웹 환경 추가** →
   - 서비스 URL: `http://127.0.0.1:8765`
   - Callback URL: `http://127.0.0.1:8765/api/auth/naver/callback`
4. **저장** 누른 뒤 새로고침해서 값 유지 확인
5. 검수 전 ("개발 중" 상태) 에는 **멤버관리 → 관리자 ID 등록** 에 본인 네이버 ID (`@naver.com` 앞부분만) 추가 → 본인 외엔 로그인 차단

### 서버 실행

```bash
python -m planner.web
```

→ **<http://127.0.0.1:8765>** 접속 (localhost X — Naver redirect URI 와 통일). 로그인은 카카오 / Google / Naver 중 택일.

### 공고 데이터 채우기 (최초 1회 + 주기적 갱신)

```bash
python run_crawler.py            # 모든 크롤러
python run_crawler.py kstartup   # K-Startup 만
python run_crawler.py iris       # IRIS 만
python run_crawler.py ntis       # NTIS 만
python run_crawler.py nrf        # NRF 만

# 비-K-Startup 공고에 LLM 자격 메타 보강 (선택, 매칭 품질 향상)
python backfill_eligibility.py
```

K-Startup 250건 기준 약 2~3분.

---

## 🧭 화면 구성 (6개 탭)

1. **🏠 홈** — 만료된 서류 / D-30 임박 서류 / 다가오는 공고 마감 / 다가오는 일정 한 화면.
2. **📁 내 서류** — 보유 서류 등록 (휠 피커 + 자동완성 + 📷 사진 OCR), 상태별 색 구분. **DB 저장 (사용자별 격리)** — 다른 OAuth 계정으로 들어가면 각자의 목록만 보임. 비로그인 시엔 localStorage fallback.
3. **🔍 공고 둘러보기** — 4개 출처 전체 공고 카탈로그 + 검색/필터.
4. **🎯 공고 매칭** — 기업 프로필 + 보유 서류 기반 적합도 정렬. **체크박스 2개 이상 → 비교 모달** (아래 [공고 비교](#-공고-비교-기능) 참고).
5. **📊 공고 분석** — PDF 폴더 단위 관리.
   - PDF 업로드 → 자동으로 **7카테고리 추출** (자격·지원금·서류·평가·의무·유의·일정)
   - **📋 통합 요약**: 폴더 안 모든 PDF의 7카테고리를 한꺼번에 그룹별 표시
   - **📅 일정 추출**: 폴더 내 PDF 들의 일정을 LLM 없이 Python merge + dedup
   - **💬 폴더 Q&A**: PDF 본문 기반 자유 채팅
6. **📅 캘린더** — 월간 그리드, 공고 일정 + 보유 서류 만료일 통합 표시. 칩마다 체크박스 ☑ — 일반 공고 일정 / 보유 서류 만료 둘 다 선택 가능.
   - **세 가지 등록 방식**:
     - **카테고리 1-클릭** (가장 빠름): 상단 범례 칩 옆 **[G] / [N]** 작은 버튼 → 그 카테고리 전체 (예: 📋 내 서류 만료 3건) 한 번에 등록
     - **선택 → 일괄**: 일정 N개 체크 → 툴바 "📅 Google 캘린더에 추가" / "📅 네이버 캘린더에 추가"
     - **월간 전체 선택**: 한 달치 모두 체크 → 등록
   - 연동 상태 표시 + 미연동 시 해당 OAuth 동의 화면으로 자동 안내. 네이버 401 (errorCode 024) 감지 시 `auth_type=reauthenticate` 로 강제 재동의 안내.
   - **수동 등록 방식** (자동 양방향 동기화 아님 — 새 일정 생기면 매번 재선택 필요).
7. **🏢 기업 프로필** — 사업자 유형/설립일/지역/업종/종업원 등 — 매칭 점수 + LLM 컨텍스트에 사용.

---

## 🆚 공고 비교 기능

매칭 탭에서 공고 2~4개 체크 → 하단 플로팅 바 → "비교 보기"

### 1차: 정형 매트릭스 (LLM 없음)

| 항목 | 시각화 |
|---|---|
| 접수기간 | 같은 시간축 가로 막대 + "오늘" 세로선 |
| D-day | 색 배지 (≤7일 빨강 / ≤30일 주황 / 그 외 초록) |
| 자격 (업력/지역/대상) | 칩 + 내 프로필과 매칭 ✓/✗ |
| 필요 서류 | 보유 ●/임박 ●/만료 ●/미보유 ○ |
| 매칭 점수 | 충족 N/총 M |
| 출처/부서/연락처 | 텍스트 |

→ **다른 셀이 있는 행은 옅은 노란 배경** 자동 강조 (어디가 다른지 한눈에).

### 2차: 정밀 비교 (LLM + PDF + 채팅)

각 공고에 **📎 PDF** 버튼 → 첨부 → "정밀 비교 시작" 클릭:

- LLM 이 **측면별 우열**만 객관 정리 ("지원금은 A 가 큼 / 의무는 B 가 가벼움" 식, "베스트 X" 같은 단정 없음)
- 사용자 기업 프로필 자동 첨부 (PII 제외)
- 결과 아래 채팅창 → 사용자 우선순위 입력 → LLM 이 같은 컨텍스트로 답변

**비용 최적화 4단**:
1. **PDF 전문 → 구조화 JSON 요약 전송** (토큰 ~95%↓)
2. **Gemini 명시 캐시** (입력 75% 할인, TTL 10분, 채팅 턴마다 재사용)
3. 같은 (공고 조합 + 프로필) → 같은 캐시 키
4. **세션 1회 (PDF 3개 + 채팅 5회) 비용: ₩6~12** (이전 ₩250 → 현재)

---

## 🔌 API 엔드포인트 요약

### 공고 매칭

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/announcements?source=...&limit=...` | 진행중 공고 리스트 |
| POST | `/api/check` | 보유 서류 만료 + 마감일 검증 |
| POST | `/api/match` | 공고 자격 + 서류 매칭 + 프로필 점수 |
| GET | `/api/ics/announcements?source=...` | 공고 마감일 ICS |
| POST | `/api/ics/tasks` | 발급 태스크 ICS |

### 인증 + 프로필 (3-provider OAuth)

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/auth/kakao/login` | 카카오 로그인 시작 |
| GET | `/api/auth/kakao/callback` | 카카오 콜백 (쿠키 발급) |
| GET | `/api/auth/google/login` | Google 로그인 시작 (openid/email/profile scope) |
| GET | `/api/auth/google/callback` | Google 콜백 — Calendar scope 도 동의됐으면 refresh_token 저장 |
| GET | `/api/auth/google/calendar/connect` | 기존 사용자가 추가로 Calendar scope 만 동의 (incremental) |
| GET | `/api/auth/naver/login?force=1` | Naver 로그인 시작 (앱 등록 시 캘린더 API 신청 → 자동으로 캘린더 권한 포함). `force=1` 이면 `auth_type=reauthenticate` 추가 → 동의 화면 강제 표시 |
| GET | `/api/auth/naver/callback` | Naver 콜백 (refresh_token 저장) |
| GET | `/api/auth/me` | 현재 사용자 조회 |
| POST | `/api/auth/logout` | 로그아웃 |
| GET | `/api/profile` | 기업 프로필 조회 |
| POST | `/api/profile` | 기업 프로필 저장 |
| GET | `/api/my-docs` | **(Phase 6)** 현재 사용자 보유 서류 목록 조회 (user_id 별 격리) |
| POST | `/api/my-docs` | **(Phase 6)** 보유 서류 전체 교체 저장. Body: `{"documents":[{"name","issued_date":"YYYY-MM-DD","note?"}]}`. 형식 검증 후 replace mode |

### 서류 마스터

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/documents` | 30개 서류 마스터 (issuing_url, validity_days 등) |
| POST | `/api/docs/scan` | 발급내역 이미지 OCR (Gemini Vision) |

### 폴더 + 첨부파일

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/folders` | 폴더 목록 + 파일 카운트 |
| POST | `/api/folders` | 폴더 생성 `{name, announcement_id?}` (announcement_id 주면 같은 공고 폴더 재사용) |
| PATCH | `/api/folders/{id}` | 이름 수정 |
| DELETE | `/api/folders/{id}` | 폴더 + 안의 파일 모두 삭제 |
| POST | `/api/upload` | PDF 업로드 (multipart, `file_id`, `folder_id`) → analyze_pdf 자동 호출 |
| GET | `/api/attachments?folder_id=...&include=analysis` | 폴더 첨부 + 분석 결과 |
| POST | `/api/attachments/{hash}/reanalyze` | 캐시 무시 재분석 |
| DELETE | `/api/attachments/{hash}` | 첨부 삭제 |

### 일정 (LLM-free)

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/folders/{id}/extract-schedule` | 폴더 내 PDF 들의 일정을 Python merge + dedup (LLM 호출 0회, derived 캐시) |
| POST | `/api/schedule` | 추출된 일정을 DB 에 저장 (`{folder_id, events, replace_folder}`) |
| GET | `/api/folders/{id}/schedule` | 폴더 일정 조회 |
| GET | `/api/schedule/all` | 캘린더용 전체 일정 |
| POST | `/api/schedule/ics` | 선택 이벤트 ICS |
| DELETE | `/api/schedule/{event_id}` | 일정 1건 삭제 |
| GET | `/api/folders/{id}/schedule.ics` | 폴더 전체 일정 ICS |

### 외부 캘린더 연동

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/calendar/google/status` | Google 캘린더 연동 상태 조회 |
| POST | `/api/calendar/google/insert` | 선택 일정들을 Google Calendar 의 primary 캘린더에 일괄 등록. Body 는 `events: [{summary, description, date_start, date_end?, time?}]` (frontend 가 보유 서류 만료 포함 전체 데이터 전송). 만료된 access_token 자동 refresh |
| POST | `/api/calendar/google/disconnect` | Google 캘린더 연동 해제 (`google_tokens` 행 삭제) |
| GET | `/api/calendar/naver/status` | Naver 캘린더 연동 상태 조회 |
| POST | `/api/calendar/naver/insert` | 선택 일정들을 Naver 캘린더에 iCal(RFC 5545) 포맷으로 등록. Body 는 위와 동일 |
| POST | `/api/calendar/naver/disconnect` | Naver 캘린더 연동 해제 |

### Q&A + 비교 (LLM)

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/folders/{id}/ask` | 폴더 PDF 본문 기반 자유 Q&A |
| POST | `/api/compare/initial` | 선택된 공고들 PDF 요약 + 프로필 → LLM 측면별 비교 (Gemini 캐시 생성) |
| POST | `/api/compare/chat` | 같은 캐시 컨텍스트에 채팅 후속 질문 |

---

## 🧠 LLM 사용처 (7종)

| # | 함수 | 트리거 | 입력 | 토큰 (전형) | 비용/회 | 캐시 |
|---|---|---|---|---|---|---|
| 1 | `analyze_pdf` | `/api/upload` | PDF 섹션 분리본 (~70%) | 5~50K | ₩2~10 | 파일 hash (영구) |
| 2 | `merge_schedule_items` | `/api/folders/{id}/extract-schedule` | — | 0 (Python) | **₩0** | derived 캐시 |
| 3 | `ask_folder_question` | `/api/folders/{id}/ask` | 폴더 PDF 합본 + 질문 | 15~150K | ₩5~30 | 없음 (RAG 도입 예정) |
| 4 | `compare_announcements` | `/api/compare/initial` | PDF 구조화 요약 N개 + 프로필 + 지시 | 5~30K | ₩1~5 | Gemini 명시 캐시 |
| 5 | `chat_compare` | `/api/compare/chat` | (캐시) + 히스토리 + 질문 | 변동분 2~5K | ₩2~3 | #4 캐시 재사용 |
| 6 | `scan_doc_image` | `/api/docs/scan` | 이미지 1장 | 1K | ₩1~2 | 없음 |
| 7 | `extract_llm_eligibility` | `backfill_eligibility.py` | 공고 본문 | 1~10K | ₩0.5~3 | 없음 (1회성) |

### 비용 시나리오

| 사용자 행동 | 비용 |
|---|---|
| 같은 PDF 두 번 업로드 | ₩0 (hash 캐시) |
| 새 PDF 1개 업로드 | ₩2~10 (#1) |
| 폴더 일정 추출 | ₩0 (Python) |
| 정밀 비교 시작 (PDF 3개) | ₩1~5 (#4, 캐시 + 요약) |
| 비교 채팅 10회 (10분 내) | ₩20~30 (#5 × 10, 캐시 적중) |
| 발급내역 이미지 OCR | ₩1~2 (#6) |

---

## 🧱 캐싱 4층 구조

```
┌─────────────────────────────────────────────────┐
│ Layer 1: 파일 hash 캐시 (analyses/*.json)        │
│   - 같은 PDF 재업로드 → analyze_pdf 건너뜀       │
│   - SCHEMA_VERSION 바뀌면 자동 무효화             │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Layer 2: extracts 캐시 (extracts/*.txt)         │
│   - PyMuPDF 추출 결과 보관                       │
│   - 모든 후속 처리(merge, ask 등) 가 재사용       │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Layer 3: derived 캐시 (derived/*.json)          │
│   - 폴더-단위 계산 결과 (예: schedule dedup)     │
│   - 키 = 폴더ID + PDF hash 들 정렬 (자동 무효화) │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Layer 4: Gemini 명시 캐시 (비교 세션 단위)        │
│   - PDF 요약 + 프로필 prefix 를 Gemini 측 저장   │
│   - TTL 10분, 입력 토큰 75% 할인                  │
│   - 같은 (공고 조합 + 프로필) → 같은 캐시 키      │
└─────────────────────────────────────────────────┘
```

---

## 🗄 DB 스키마 핵심

```sql
-- 크롤러 출처
sources(id, code, name, base_url)
categories(id, source_id, code, name, list_url)

-- 공고
announcements(id, source_id, category_id, business_id, external_id,
              title, start_date, end_date, department, contact,
              content_text, detail_url, raw_meta, ...)
            -- raw_meta 에는 K-Startup 메타 + (비-K-Startup) llm_eligibility 보관
assistance_businesses(id, source_id, external_id, name, department, ...)
business_attributes(id, business_id, name)
attachments(id, announcement_id, name, url)
crawl_runs(id, source_id, category_id, started_at, finished_at, items_*, status)

-- 사용자 (Phase 2+: 카카오/Google/Naver OAuth, provider_uid 로 식별)
users(id, auth_provider, provider_uid, nickname, email, profile_img, ...)
            -- auth_provider ∈ {'kakao','google','naver'}, UNIQUE(auth_provider, provider_uid)
user_profiles(user_id PRIMARY KEY,
              -- 기본 (시즌1): company_name, business_type, establishment_date, region, industry
              -- 확장 (시즌2): english_name, corporation_number, representative_*,
              --              employee_count, founding_type, business_address, phone, fax,
              --              website, industry_code, industry_type, ...)

-- 사용자 업로드 PDF (Phase 3+)
attachment_folders(id, name, created_at, user_id,
                   announcement_id  -- Phase 4: 매칭 카드 PDF 첨부 시 공고와 연결
)
uploaded_attachments(id, file_hash, original_filename, folder_id, user_id,
                     uploaded_at, analyzed_at, status,
                     UNIQUE(file_hash, user_id))
announcement_schedule_events(id, folder_id, file_hash, title, type,
                             date_start, date_end, time, note, source_page, user_id, ...)

-- Phase 5: 외부 캘린더 OAuth 토큰 (Google Calendar API 호출용)
google_tokens(user_id PK, refresh_token, access_token, expires_at, scopes, ...)

-- Phase 5.1: Naver 캘린더는 별도 scope 없이 로그인 = 캘린더 권한 자동 부여
naver_tokens(user_id PK, refresh_token, access_token, expires_at, ...)

-- Phase 6: 보유 서류 (이전 localStorage → DB 이관, 사용자별 격리)
user_documents(id, user_id, name, issued_date, note, created_at)
            -- 첫 로그인 시 기존 localStorage 데이터 자동 이관 후 localStorage 정리
```

---

## 🌐 외부 공유 (데모용 빠른 배포)

### Ngrok / Cloudflare Tunnel 등

```powershell
# 첫 회만:
winget install --id Ngrok.Ngrok
ngrok config add-authtoken <token>

# 매번:
ngrok http 8765
```

출력의 `https://xxxx.ngrok-free.dev` 가 외부 URL.

**외부 공유 시 redirect URI 3개 다 동일하게 설정 필수**:
- `.env` 의 `KAKAO_REDIRECT_URI`, `GOOGLE_REDIRECT_URI`, `NAVER_REDIRECT_URI`
- 카카오 개발자센터의 Redirect URI
- Google Cloud Console 의 승인된 리디렉션 URI
- Naver 개발자센터의 서비스 URL + Callback URL

세 콘솔 모두 동일하게 갱신 → 서버 재시작. https 도메인이라 `Secure` 쿠키 플래그도 필요할 수 있음 (`.env` 의 `FORCE_SECURE_COOKIE=1`).

---

## 🌍 크롤러 별 작동 방식

| 출처 | 방식 | 특징 |
|---|---|---|
| **K-Startup** | OpenAPI (공공데이터포털 키 필요) | 구조화 메타(biz_enyy/supt_regin/aply_trgt) 직접 제공 → 자격 매칭에 최적 |
| **NRF** | 페이지 HTML 파싱 (BeautifulSoup) | 한국연구재단 전용. 자격 메타 없음 → LLM 보강 필요 |
| **IRIS** | AJAX/JSON 엔드포인트 | 국가R&D 공고, 첨부 다운로드 JS 차단됨 |
| **NTIS** | SSR HTML + 정규식 | R&D 서비스 DB, 마찬가지로 첨부 다운로드 차단 |

비-K-Startup 3개는 `backfill_eligibility.py` 로 LLM 자격 메타 보강 (Gemini 무료 한도 15 RPM 준수).

---

## 🚧 다음 단계 (시나리오 갭)

| 영역 | 상태 |
|---|---|
| 폴더 Q&A 의 RAG 도입 (#3 호출 비용 ↓) | ❌ (계획됨, C4) |
| 모델 라우팅 (싼/비싼 분리) | ❌ (계획됨, C5) |
| Apple Calendar / Outlook 직접 등록 (ICS 구독 URL 방식) | ❌ |
| Google/Naver 캘린더 양방향 자동 동기화 (변경 hook, event_id 추적) | ❌ (현재 수동 선택 → 클릭 등록만) |
| OAuth 앱 외부 출시 (Google Verification, Naver 검수) | ❌ (현재 본인/테스터 ID 만 로그인 가능) |
| 관심 공고(북마크) | ❌ |
| 키워드/태그 검색 | ❌ |
| 푸시/이메일 알림 | ❌ |
| 사용자별 데이터 격리 (실서비스 수준) | ✅ Phase 2 (folders/uploads) + Phase 3 (profiles) + Phase 6 (보유 서류). 검증: 3개 OAuth 계정 교차 확인 완료 |
| 우대(가점) 사항 별도 시각화 | △ (analyze 의 obligations/evaluation 에 일부 들어감) |

---

## 🤝 기여 가이드

- **새 크롤러 추가**: `crawlers/` 에 `BaseCrawler` 상속 클래스 → `run_crawler.py` 의 `CRAWLERS` dict 에 등록
- **새 LLM 프로바이더**: `planner/analyzer/llm/` 에 `LLMProvider` 상속 + `registry.py` 등록
- **DB 스키마 변경**: `db.py` 의 `SCHEMA` 수정 + `_MIGRATIONS` 에 `(table, column, ddl)` 추가
- **분석 스키마 변경**: `prompts.py` 의 `EXTRACTION_SCHEMA` 수정 + `analyzer.py` 의 `SCHEMA_VERSION` 증가 → 기존 캐시 자동 무효화
- **UI 변경**: `public/dashboard.html` 단일 파일. 탭 추가 시 `<nav.tabs>` 와 `<section.pane>` 둘 다

---

## 📞 핵심 연락 / 외부 시스템

- Gemini API 키: <https://aistudio.google.com/apikey>
- 카카오 OAuth: <https://developers.kakao.com>
- Google Cloud Console: <https://console.cloud.google.com> (OAuth + Calendar API)
- Naver 개발자센터: <https://developers.naver.com/apps> (로그인 + 캘린더 API)
- Naver Calendar API 문서: <https://developers.naver.com/docs/login/calendar-api/calendar-api.md>
- 공공데이터포털 (K-Startup): <https://www.data.go.kr>
- 정부24: 주민등록표·가족관계증명서 등 발급
- 홈택스: 납세증명서·사업자등록증명
- 4대보험 정보연계센터: 보험 가입 증명
- K-Startup 원본: <https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do>
- NRF 원본: <https://www.nrf.re.kr/page/362?menuNo=362&bizNotGubn=guide>
- IRIS 원본: <https://www.iris.go.kr>
- NTIS 원본: <https://www.ntis.go.kr>
