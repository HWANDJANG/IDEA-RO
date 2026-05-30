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
| 입주·자금·R&D 등 공고 종류가 섞여있어 분류 어려움 | 6개 묶음(💰자금·🏢입주·🎓교육·🔬R&D·🌏판로·🎯행사) 자동 분류 + 상단 필터 칩 + 카드 컬러 배지 |
| K-Startup PDF 가 Synap 뷰어로 열려 다운로드 어려움 | 공고 페이지 URL 붙여넣기 → 서버가 첨부 PDF 목록 자동 수집 + 선택 가져오기 (`/api/attachments/scan-url`) |
| 캘린더에 같은 "모집기간"이 매일 칩으로 도배되어 산만함 | 주 단위 timeline bar (Google Calendar 종일 일정 스타일) + 일정 클릭 시 우측 사이드 패널 (상세·진행률·준비서류·액션) |
| 사이드바·탭·카드 디자인이 단조롭고 정보 우선순위 모호 | **디자인 v2** — 사이드바 워크스페이스 스위처(서류 체커/아이디어 검증) + 그룹 라벨 + 하단 계정 카드, 매칭 카드 쿠폰 절취선 (좌측 본문 / 우측 비교 zone), 3단계 서류 상태 pill (완비·위험·부족), 홈 화면 2열 + 상단 일정 그리드 카드 |
| 매칭은 되는데 "그래서 뭘 해야 하지" 가 모호 | **🧭 맞춤 플랜 (엔드 투 엔드)** — 가중치 슬라이더로 Top N 추천 정렬 (지원금/노력/마감) → ✨ "왜 이게 맞는지" narrative → "+ 담기" → AI 액션 가이드 (이번주/다음주/그 이후 + 발급처 인용 + 분석된 PDF의 평가/의무까지 인용) → 통합 타임라인 + 캘린더 일괄 등록 |

---

## 🛠 스택

- **백엔드**: Python 3.13 표준 라이브러리(`http.server` + `sqlite3` + `json` + `urllib`)
- **외부 의존**: `requests`, `beautifulsoup4` (크롤러), `pymupdf` (PDF 텍스트), `google-genai` (Gemini Vision/Chat), `anthropic` (선택)
- **프론트엔드**: 단일 `public/dashboard.html` SPA (Vanilla JS, 약 7700줄). 디자인 v2 — Noto Serif KR 타이틀 + Noto Sans KR 본문, 워크스페이스 스위처
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
│   ├── web.py                    ← HTTP 서버 + 모든 API 엔드포인트 (~2500줄)
│   ├── auth.py                   ← 카카오/Google/Naver OAuth + HMAC 쿠키 + Calendar 토큰 관리
│   ├── checker.py                ← 서류 만료 계산 + 발급 태스크
│   ├── matcher.py                ← 공고 ↔ 보유 서류 + 프로필 매칭 + 공고 유형 분류 (6개 묶음)
│   ├── planner.py                ← 🎯 내 액션 플랜 (Top N + 가중치 + LLM narrative + 액션 가이드 A/B/C + extracted_events)
│   ├── auto_fetcher.py           ← 공고 페이지 자동 fetch + 분석 백엔드 (Step 4) — DB row 시스템 공유, magic 검증, idempotent
│   ├── attach_fetcher.py         ← 공고 페이지 URL → 첨부 자동 수집 스크래퍼 (K-Startup/NRF/NTIS)
│   ├── document_master.py        ← 30개 정부 서류 마스터 데이터
│   ├── eligibility_extractor.py  ← 비-K-Startup 공고 자격 메타 LLM 추출
│   ├── ics_export.py             ← 캘린더 ICS 빌더 (RFC 5545)
│   ├── multipart.py              ← multipart/form-data 파서
│   ├── paths.py                  ← 경로 상수
│   │
│   ├── analyzer/                 ← 파일 → 7카테고리 LLM 분석 파이프라인
│   │   ├── analyzer.py           ← 메인 dispatcher: analyze_pdf / analyze_image / analyze_text + analyze_attachment + 비교/채팅
│   │   ├── extractor.py          ← PyMuPDF 텍스트 + 정규식 섹션 분리(find_sections)
│   │   ├── hwpx_extractor.py     ← HWPX (신형) ZIP+XML 파싱 (stdlib, 외부 의존성 0)
│   │   ├── hwp_extractor.py      ← HWP (구형, HWP5/OLE2) pyhwp hwp5txt CLI 호출 + venv 환경 보강
│   │   ├── prompts.py            ← 시스템 프롬프트 + 7카테고리 JSON 스키마
│   │   ├── storage.py            ← pdfs/images/sources/extracts/analyses/derived 파일 캐시
│   │   ├── doc_scanner.py        ← 발급내역 이미지 OCR (Gemini Vision)
│   │   ├── dotenv.py             ← .env 로더 (표준 라이브러리)
│   │   └── llm/
│   │       ├── base.py           ← LLMProvider ABC + create_cache + complete_vision 인터페이스
│   │       ├── gemini.py         ← Gemini (text + vision + 명시 캐싱)
│   │       ├── claude.py         ← Anthropic (ephemeral system caching)
│   │       ├── openai.py         ← placeholder
│   │       └── registry.py       ← 환경변수로 프로바이더 선택
│   │
│   └── storage/                  ← 사용자 업로드 + 자동 fetch 캐시 (gitignore)
│       ├── pdfs/{hash}.pdf
│       ├── images/{hash}.{jpg|png|webp}   ← Step 1
│       ├── sources/{hash}.{hwpx|hwp}      ← Step 2 + A
│       ├── extracts/{hash}.txt
│       ├── analyses/{hash}.json           ← 7카테고리 구조화 추출 (schema_version=2)
│       └── derived/{key}.json             ← 폴더 / 맞춤 플랜 derived
│
└── public/
    ├── index.html                ← 랜딩 (3-provider 로그인 모달 + Last used 표시)
    ├── onboarding.html           ← 3-provider 로그인 + 기업 프로필 입력
    └── dashboard.html            ← 메인 SPA (2탭 통합 후 ~7000줄)
```

---

## 🚀 로컬 실행

### 사전 준비

```bash
# Python 3.10+ (3.13 권장)
pip install -r requirements.txt
```

> **HWP 분석 (구형 hwp5)**: `pyhwp>=0.1b15` + `six>=1.16` 가 같이 설치됨. `hwp5txt` CLI 가 venv/bin 또는 시스템 PATH 에 자동 등록되어야 함. 검증:
> ```bash
> hwp5txt --version  # 또는: python -m hwp5.hwp5txt --version
> ```
>
> **AWS Ubuntu 24.04 + venv 환경 주의**:
> - 시스템 pip 가 PEP 668 로 차단되므로 venv 사용 권장
> - venv 의 pip 로 설치: `~/IDEA-RO/venv/bin/pip install -r requirements.txt`
> - systemd ExecStart 가 venv/bin/python 가리켜야 함
> - 코드의 `_find_hwp5txt()` 가 `sys.executable` 디렉터리 기준으로 hwp5txt 탐색 → venv 자동 인식

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

## 🧭 화면 구성 (사이드바 2탭 통합 — 옵션 B)

> 🎨 **디자인 v2 공통**: 좌측 고정 사이드바 + 워크스페이스 스위처 (서류 체커 / 아이디어 검증) + 그룹 라벨 (탐색·마이페이지) + 하단 계정 카드 (아바타·기업명·로그아웃). 상단 topbar 제거. 각 페인 타이틀은 28px 세리프 (`--serif-ko`).
>
> **🔀 4탭 → 2탭 통합 (2026-05-30)**: 기존 「공고 둘러보기 · 공고 매칭 · 공고 분석 · 맞춤 플랜」 4개 → **「🔍 공고 둘러보기」 + 「🎯 내 액션 플랜」 2개**. 자동 fetch 도입으로 공고 분석은 카드 펼침에 인라인 흡수, 매칭은 둘러보기 안 토글 모드로 합쳐짐.

1. **🏠 홈** — 만료된 서류 / D-30 임박 서류 / 다가오는 공고 마감 / 다가오는 일정.
   - **상단 그리드 카드**: 다가오는 일정을 232px+ 컴팩트 카드 (`.home-sched-grid`) 로 한 줄에 여러 개 표시. 좌측 색 블록 D-day (urgent ≤3 빨강 / imm ≤7 주황 / calm 그 외 파랑).
   - **하단 2열 레이아웃** (`.home-cols`): 좌=서류 만료(만료 + 임박), 우=공고 마감.
2. **📁 내 서류** — 보유 서류 등록 (휠 피커 + 자동완성 + 📷 사진 OCR), 상태별 색 구분. **DB 저장 (사용자별 격리)** — 다른 OAuth 계정으로 들어가면 각자의 목록만 보임. 비로그인 시엔 localStorage fallback.
3. **🔍 공고 둘러보기** — 두 모드 토글 + 카드 펼침에 자동 분석 인라인.
   - **📋 전체 목록 모드 (기본)**: 4개 출처 진행 중 공고 카탈로그 + 검색/정렬/즐겨찾기 + 유형 필터 칩. 자격 매칭 없이 모든 공고 표시.
   - **🎯 자격 맞춤 모드**: 기업 프로필 + 보유 서류 기반 추천순 카드. 한 토글로 둘 사이 즉시 전환.
   - **카드 리뉴얼**: 좌측 본문 (`.match-main`) + 우측 비교 zone (`.match-cmp-zone`) 쿠폰 절취선 스타일. 비교 선택 시 카드 전체에 primary 색 외곽 글로우.
   - **3단계 서류 상태 pill**: ✓ 완비 (초록) / ⚠ 만료 위험 (주황) / ✗ 부족 (빨강).
   - **유형 필터 칩 (6묶음)**: 💰 자금 지원 / 🏢 입주·공간 / 🎓 교육·멘토링 / 🔬 R&D·기술 / 🌏 판로·글로벌 / 🎯 행사·네트워크.
   - **📊 카드 펼침 시 자동 분석 결과 인라인 표시** (옛 "공고 분석" 탭 흡수):
     - 공고 페이지 첨부를 **PDF / 이미지 / HWPX / HWP 모두 자동 다운/분석** (Step 1~4 + A)
     - 포맷별 아이콘: 📄 PDF · 🖼️ 이미지 · 📋 HWPX · 📝 HWP · 📦 미지원
     - 카테고리별 추출 카운트 (자격N · 일정N · 지원금N · 서류N · 평가N · 의무N · 유의N)
     - 📅 추출된 일정 리스트 + 🗓️ Google · N 네이버 캘린더 일괄 등록 버튼
     - 분석 안 된 공고는 펼치는 순간 자동 트리거 (lazy, 로그인 사용자만)
     - "+ 내 자료 추가" 보조 버튼 — 자동 안 된 케이스 fallback
   - **공고 비교**: 카드 다중 선택 → 비교 보기 → 자동 fetch 분석을 LLM 비교에 즉시 활용 (사용자가 PDF 한 번도 직접 첨부 안 해도 정밀 비교 가능, 옵션 B-1)
4. **🎯 내 액션 플랜 (엔드 투 엔드)** — 추천부터 일정/캘린더 등록까지 한 흐름. (이전 "맞춤 플랜")
   - **3 슬라이더로 가중치 조정**: 💰 지원금 규모 / ⚡ 노력 회피 / ⏰ 마감 임박 (0~100). 프리셋 4종 (균형 / 고액 / 쉬운 / 마감 우선). 변경 시 debounce 300ms 후 자동 재호출 + 재정렬.
   - **Top N 추천 카드** (3/5/10 선택): 자격 매칭 + 보유 서류 + 지원금 규모 + 노력 등급 + D-day 종합 점수. 각 카드에 💰 amount + ⚙️ effort 배지, 점수 hover tooltip 으로 raw score 분해(profile + urgency + amount − effort).
   - **✨ AI narrative**: 각 카드 헤더 아래 "왜 이 공고가 당신에게 맞는지" 한 줄 (백그라운드 Gemini Flash Lite, 캐시 적중 시 ₩0).
   - **"+ 내 플랜에 담기"** 토글 → 담은 공고들만 아래에 모임. 카드 외곽 accent 강조.
   - **AI 액션 가이드** (700ms debounce 후 자동 호출): 담은 공고 종합해 ⚠️ key_warning + **이번 주 (긴급) / 다음 주 (중요) / 2주 후 (여유)** 시간 구간별 액션 카드. Step A → B → C 누적:
     - **A**: 보유/만료/임박/미보유 서류 명단 + 발급처 인용 (정부24/홈택스/위택스/4대보험)
     - **B**: 자격 매칭 reasons detail + 공고 유형/지원금/노력 등급 활용한 작업 강도 안내 ("R&D 15억원 → 다음 주는 사업계획서에 시간 확보")
     - **C**: 담은 공고에 분석된 PDF 있으면 평가 기준·의무사항·지원금 상세·유의사항까지 인용 ("기술성 40%, 사업성 30%" / "협약 후 2년 정규직 의무"). 가이드 헤더에 `📄 PDF N건 활용` 배지로 시그널.
   - **통합 액션 타임라인**: 담은 공고들의 발급 태스크 + 신청 마감 시간순. 같은 서류는 가장 빠른 due 로 dedup ("N개 공고 공통" 라벨).
   - **🗓️ Google / N 네이버 캘린더 일괄 등록**: 선택 체크박스 + 전체/발급만/마감만 필터 + 기존 `/api/calendar/*/insert` 재사용.
5. **📅 캘린더** — 월간 그리드, 공고 일정 + 보유 서류 만료일 통합 표시. **모던 리뉴얼 (v2)**.
   - **주 단위 Timeline Bar**: 기간 이벤트(접수기간 등)는 매일 칩으로 도배되지 않고 **연속 막대**로 표시 (Google Calendar 종일 일정 스타일). 주 경계 잘림은 `‹` `›` 마커.
   - **색 시스템**: bar 색 = 폴더(공고)별 고유 색 → 같은 공고 한눈에 묶임. 좌측 액센트 strip + 카테고리 아이콘 prefix(📝 접수·🏆 발표·📊 평가·📋 서류·🤝 협약). 마감 D-7 이내는 **빨강 펄스 D-day 배지**.
   - **사이드 패널** (일정 클릭 시): 제목·공고·날짜·진행률 막대·메모·**준비 서류 요약**(매칭된 공고면 보유/임박/만료/미보유)·**추천 액션**(이 일정만 G/N 캘린더 추가, 원본 공고 열기, 일괄 선택 토글).
   - **+N 팝오버**: 한 주에 일정 많을 때 컬럼별 `+N` → 클릭하면 그날 전체 일정 카드 팝오버 → 클릭 시 사이드 패널 열림.
   - **세 가지 일괄 등록 방식**: 카테고리 1-클릭 [G][N] / 선택 N개 → 툴바 / 월간 전체 선택.
   - 네이버 401 (errorCode 024) 감지 시 `auth_type=reauthenticate` 로 강제 재동의 안내.
   - **수동 등록 방식** (자동 양방향 동기화 아님 — 새 일정 생기면 매번 재선택 필요).
6. **🏢 기업 프로필** — 사이드바에 별도 탭 없음. **하단 계정 카드 클릭** 으로 진입.
   - **3 섹션 분리**: ① 기본 정보 (기업명·사업자등록번호·대표·설립일·법인등록번호·업종코드) / ② 소재지·연락처 (지역·주소·전화·이메일·팩스·웹사이트) / ③ 업종 (chip 다중 선택 + 업태 자유 입력 카드).
   - 지역(`pf-region-display`) 은 Daum 우편번호 검색 시 자동 동기화 readonly 입력.
   - 모든 `pf-*` ID 는 `saveProfileForm()` 과 호환 — 기능 100% 보존.

---

## 🆚 공고 비교 기능

**둘러보기 > 자격 맞춤 모드** 에서 공고 2~4개 체크 → 하단 플로팅 바 → "비교 보기"

### 1차: 정형 매트릭스 (LLM 없음)

| 항목 | 시각화 |
|---|---|
| 접수기간 타임라인 | 트랙 안에 **D-day 색 진행률 fill** (시작→오늘 elapsed) + ▼ 오늘 마커 + 시작·마감일 라벨 |
| D-day | 큰 알약 배지 (≤7일 빨강 / ≤30일 주황 / 그 외 초록 + 배경 채움) |
| 자격 (업력/지역/대상) | 칩 + 내 프로필과 매칭 ✓/✗ |
| 필요 서류 | 보유 ●/임박 ●/만료 ●/미보유 ○ |
| 매칭 점수 | 충족 N/총 M |
| 연락처 | 전화번호 자동 포맷팅 (`041-589-7118`, `tel:` 링크) + URL 한글 디코딩 + 호스트 위주 짧은 표시 + 클릭 가능 |

→ **다른 셀이 있는 행은 좌측 4px 주황 사이드바 + 옅은 주황 배경** 자동 강조 (행 자체가 아니라 라벨에 띠를 띄워 더 잘 띔). 같은 값 행은 muted.

### 2차: 정밀 비교 (LLM + 자동 분석 + 채팅) — 옵션 B-1 갱신

이제 **PDF 직접 첨부 없이도 정밀 비교 가능** — 자동 fetch 된 분석 결과 (PDF/이미지/HWPX/HWP) 가 자동으로 비교 컨텍스트에 포함됨.

비교 모달 진입 시 자동 처리:
1. 각 공고에 대해 `_collect_compare_items` 가 **사용자 직접 첨부 + 자동 fetch (status=done)** 합본 조회 (`file_hash` 로 dedup, 사용자 첨부 우선)
2. 화면 표시: "분석 자료 N건 (자동 X · 직접 Y)" — 진짜 보유한 자료량 파악
3. 자료 0건인 공고는 비교 모달 열 때 백그라운드 자동 fetch trigger (옵션 A 와 동일 정책)
4. **"+ 내 자료"** 보조 버튼만 남음 (점선 outline) — 자동이 메인, 수동이 fallback

"🚀 정밀 비교 시작" 클릭:
- LLM 이 **측면별 우열**만 객관 정리 ("지원금은 A 가 큼 / 의무는 B 가 가벼움" 식, "베스트 X" 같은 단정 없음)
- 사용자 기업 프로필 자동 첨부 (PII 제외)
- 결과 아래 채팅창 → 사용자 우선순위 입력 → LLM 이 같은 컨텍스트로 답변
- **마크다운 자동 렌더링**: `**bold**` → 강조, `* **섹션:**` → 좌측 파랑 띠 카드, bullet → 깔끔한 리스트, "공고 A/B/C/D" → **컬러 칩**(파/초/주/핑크).

**비용 최적화 4단**:
1. **PDF 전문 → 구조화 JSON 요약 전송** (`format_analysis_summary`, 토큰 ~95%↓)
2. **Gemini 명시 캐시** (입력 75% 할인, TTL 10분, 채팅 턴마다 재사용)
3. 같은 (공고 조합 + 프로필) → 같은 캐시 키
4. **세션 1회 (자료 3개 + 채팅 5회) 비용: ₩6~12**
5. **자동 fetch 분석은 시스템 공유 캐시** — 같은 공고를 여러 사용자가 봐도 분석 1회 (`announcement_auto_attachments` 의 file_hash 기반)

---

## 🔌 API 엔드포인트 요약

### 공고 매칭

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/announcements?source=...&limit=...` | 진행중 공고 리스트 (각 항목에 `type: {code, emoji, label}` 분류 메타 포함 — 6개 묶음 자동 분류) |
| POST | `/api/check` | 보유 서류 만료 + 마감일 검증 |
| POST | `/api/match` | 공고 자격 + 서류 매칭 + 프로필 점수 |
| GET | `/api/ics/announcements?source=...` | 공고 마감일 ICS |
| POST | `/api/ics/tasks` | 발급 태스크 ICS |
| GET | `/api/plan?top_n=5&w_amount=0.5&w_effort=0.3&w_urgency=0.5` | **맞춤 플랜 (LLM 없음)** — 자격 매칭 + 가중치 기반 Top N + 부족 서류 + 발급 태스크. 슬라이더 변경 시 호출. signals(amount_display, effort_label, req_doc_count) + profile_fit reasons 포함 |
| POST | `/api/plan/narrative` | **각 카드 한 줄 narrative** (Gemini Flash Lite). Body: `{plan: {...}}` → `{narratives: {ann_id_str: "..."}, cached, count}`. 캐시 키 = user + sorted(ann_ids) + weights. 같은 슬라이더 조합 두 번 → ₩0 |
| POST | `/api/plan/guide` | **AI 액션 가이드** (Gemini Flash Lite). Body: `{plan, picked_ids}` → `{sections, key_warning, cached, picked_count, pdf_analyzed_count}`. Step A/B/C 누적: 서류 명단 + 발급처 / 자격 reasons + 유형/지원금/노력 / 분석된 PDF 의 평가·의무·지원금 상세. 캐시 키에 시스템 프롬프트 sha + 담은 공고의 PDF hash 포함 → 프롬프트 수정·PDF 추가삭제 시 자동 invalidate |
| GET | `/api/announcements/{id}/auto-attachments` | **자동 수집 첨부 + 추출 일정 조회** (Step 4+6). 비로그인 허용. Response: `{attachments: [{status, file_format, file_hash, analysis, ...}], extracted_events: [...]}`. 카드 펼침 시 자동 호출. attachments 0건이고 로그인 시 옵션 A lazy trigger 동작. |
| POST | `/api/announcements/{id}/auto-fetch` | **공고 페이지에서 첨부 자동 다운/분석 트리거** (Step 4). 로그인 필요. Body: `{force?: bool, max_files?: int (1~20)}`. 동기 처리 (10~60s blocking). detail_url 스크랩 → PDF/JPG/PNG/HWPX/HWP 자동 다운 → 7카테고리 분석 → DB 저장 + 응답에 extracted_events 포함. idempotent: 이미 done 인 첨부는 skip (force=true 시 재시도). |

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
| POST | `/api/attachments/scan-url` | 공고 페이지 URL 붙여넣기 → 첨부 PDF 목록 자동 수집 (`{url}` → `{source, files:[{name,url,is_pdf}], warning?}`). K-Startup/NRF/NTIS 별 스크래퍼 + generic fallback |
| POST | `/api/attachments/import-url` | 수집된 PDF URL 1건을 서버가 다운로드 + analyze_pdf + 폴더 저장 (`{folder_id, url, filename, announcement_id?}`) — Synap 뷰어가 아닌 직접 다운로드 URL 사용 |
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

## 🧠 LLM 사용처 (12종)

| # | 함수 | 트리거 | 입력 | 토큰 (전형) | 비용/회 | 캐시 |
|---|---|---|---|---|---|---|
| 1 | `analyze_pdf` | `/api/upload` 또는 자동 fetch | PDF 섹션 분리본 (~70%) | 5~50K | ₩2~10 | 파일 hash (영구) |
| 2 | **`analyze_image`** | `/api/upload` (JPG/PNG/WEBP) 또는 자동 fetch | 이미지 1장 (Gemini Vision) | 1~5K | ₩2~3 | 파일 hash (영구) |
| 3 | **`analyze_text`** | HWPX/HWP 자동 dispatch | 추출된 평문 | 5~50K | ₩2~10 | 파일 hash (영구) |
| 4 | `merge_schedule_items` | `/api/folders/{id}/extract-schedule` 또는 `_load_extracted_events_for_announcement` | — | 0 (Python) | **₩0** | derived 캐시 |
| 5 | `ask_folder_question` | `/api/folders/{id}/ask` | 폴더 PDF 합본 + 질문 | 15~150K | ₩5~30 | 없음 (RAG 도입 예정) |
| 6 | `compare_announcements` | `/api/compare/initial` | **자동 fetch + 직접 첨부 합본** 구조화 요약 + 프로필 | 5~30K | ₩1~5 | Gemini 명시 캐시 |
| 7 | `chat_compare` | `/api/compare/chat` | (캐시) + 히스토리 + 질문 | 변동분 2~5K | ₩2~3 | #6 캐시 재사용 |
| 8 | `scan_doc_image` | `/api/docs/scan` | 발급내역 이미지 1장 | 1K | ₩1~2 | 없음 |
| 9 | `extract_llm_eligibility` | `backfill_eligibility.py` | 공고 본문 | 1~10K | ₩0.5~3 | 없음 (1회성) |
| 10 | `generate_recommendation_narratives` | `/api/plan/narrative` | profile(safe) + 추천 카드 N개 라이트 라인 + 가중치 | 2~5K | ₩1~3 | derived (user + ann_ids + weights) |
| 11 | `generate_action_guide` | `/api/plan/guide` | profile + 담은 공고 풀 컨텍스트 + 통합 발급 태스크 + (있으면) **자동/직접 합본 PDF 분석 요약** | 3~15K | ₩2~8 | derived (sys_hash + user + picked + today + PDF hashes) |
| 12 | **자동 fetch trigger** | `compose_action_plan` (Top N 백그라운드) / 카드 펼침 (옵션 A lazy) / 비교 모달 (Fix 4) | `auto_fetcher.fetch_and_analyze_announcement` → 스크랩 + 다운 + #1/#2/#3 호출 | 변동 (per file) | ₩3~5/file | DB row idempotent (status='done' 이면 skip) + 파일 hash |

### 비용 시나리오

| 사용자 행동 | 비용 |
|---|---|
| 같은 PDF/HWP/이미지 두 번 업로드 | ₩0 (hash 캐시) |
| 새 PDF 1개 업로드 | ₩2~10 (#1) |
| 새 JPG/PNG 1장 업로드 | ₩2~3 (#2) |
| 새 HWPX/HWP 1개 업로드 | ₩2~10 (#3) |
| 폴더 일정 추출 | ₩0 (Python) |
| 정밀 비교 시작 (자료 3개) | ₩1~5 (#6, 캐시 + 요약) |
| 비교 채팅 10회 (10분 내) | ₩20~30 (#7 × 10, 캐시 적중) |
| 발급내역 이미지 OCR | ₩1~2 (#8) |
| 맞춤 플랜 첫 호출 (Top 5 narrative + 가이드) | ₩3~10 (#10 + #11) |
| 슬라이더 조정 (같은 조합 재호출) | ₩0 (캐시 적중) |
| 같은 picked + 같은 날짜 가이드 재호출 | ₩0 (캐시 적중) |
| 카드 펼침 시 자동 fetch (옵션 A, 새 공고) | ₩3~15 (PDF/이미지/HWP 평균 2건 × ₩3~5) |
| 같은 공고 N명이 봐도 | ₩3~15 (시스템 공유 캐시, 첫 1명만) |
| 진행중 343 공고 일괄 사전 분석 (1회) | ₩1,500~2,000 (일부 HWP 만 있는 공고는 분석 가능, NTIS 는 form-POST 미구현으로 fail) |

---

## 🧱 캐싱 6층 구조

```
┌─────────────────────────────────────────────────┐
│ Layer 1: 파일 hash 캐시 (analyses/*.json)        │
│   - PDF / 이미지 / HWPX / HWP 분석 결과 보관     │
│   - 같은 파일 재업로드 → LLM 호출 0              │
│   - SCHEMA_VERSION 바뀌면 자동 무효화             │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Layer 2: extracts 캐시 (extracts/*.txt)         │
│   - PyMuPDF / hwpx / hwp5txt 추출 결과 보관      │
│   - 모든 후속 처리(merge, ask 등) 가 재사용       │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Layer 3: derived 캐시 (derived/*.json)          │
│   - 폴더-단위 계산 결과 (schedule dedup, 가이드) │
│   - 키 = 폴더ID + PDF hash 들 정렬 (자동 무효화) │
│   - 맞춤 플랜 narrative / guide 캐시도 여기      │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Layer 4: Gemini 명시 캐시 (비교 세션 단위)        │
│   - PDF 요약 + 프로필 prefix 를 Gemini 측 저장   │
│   - TTL 10분, 입력 토큰 75% 할인                  │
│   - 같은 (공고 조합 + 프로필) → 같은 캐시 키      │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Layer 5: 자동 fetch 시도 결과 (DB, 시스템 공유)   │
│   - announcement_auto_attachments 테이블          │
│   - (announcement_id, source_url) UNIQUE          │
│   - status: pending|done|skipped|failed           │
│   - file_hash → Layer 1 의 analyses/*.json 연결  │
│   - 같은 공고를 여러 사용자가 봐도 fetch 1회만   │
│   - idempotent: done 인 건 force=true 만 재시도   │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Layer 6: 맞춤 플랜 가이드 캐시 (Step C 통합)     │
│   - derived/plan_guide_{user_id}_{hash}.json     │
│   - 키 = sha(_GUIDE_SYSTEM) + user + picked +    │
│         today + 분석된 PDF hashes 합본            │
│   - 시스템 프롬프트 수정 / 사용자 첨부 / 자동    │
│     fetch 결과 추가 모두 자동 invalidate          │
└─────────────────────────────────────────────────┘
```

**Layer 1 + 5 의 시너지**: 자동 fetch 가 새 공고에서 PDF 다운/분석 → file_hash 가 다른 공고의 동일 PDF (가끔 있음, 같은 양식 재사용) 와 일치하면 Layer 1 hit → LLM 0회. 한 번 분석된 파일은 시스템 전체에 1회만 분석.

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
| 공고 유형 분류 (입주/자금/교육/R&D/...) | ✅ K-Startup `supt_biz_clsfc` + 제목 키워드 fallback → 6개 묶음. 매칭 탭 필터 칩 + 카드 배지 |
| K-Startup 첨부 자동 수집 (Synap 뷰어 우회) | ✅ `attach_fetcher.py` — 공고 페이지 URL → PDF 목록 → 선택 가져오기 |
| 캘린더 timeline bar + 사이드 패널 + +N 팝오버 | ✅ 주 단위 span bar, 클릭 시 우측 상세 패널 (진행률·준비서류·액션) |
| 비교 채팅 결과 마크다운 렌더링 + 공고 A/B 칩 | ✅ `_renderCompareMarkdown` — XSS 안전 패턴 치환 |
| 사이드바 v2 (워크스페이스 스위처·그룹 라벨·하단 계정 카드) | ✅ `setupWorkspaceSwitcher` — 서류 체커 / 아이디어 검증 모드 토글 (`data-workspace` 속성으로 nav 토글) |
| 매칭 카드 쿠폰 절취선 + 3단계 서류 상태 pill | ✅ `.match-main` + `.match-cmp-zone` (`#announcements-list` 스코프 한정), `ds-complete/ds-risk/ds-short` |
| 홈 화면 컴팩트 일정 카드 + 2열 레이아웃 | ✅ `.home-sched-grid` (색 D-day 블록 urgent/imm/calm) + `.home-cols` |
| 둘러보기 유형 필터 + 매칭과 독립 상태 | ✅ `_browseActiveTypes` Set, `#browse-type-filter` 칩 |
| 프로필 페이지 3섹션 분리 + 사이드바 하단 진입 | ✅ 기본 정보·소재지/연락처·업종, 사이드바에 profile 탭 없음 — 계정 카드 클릭으로만 진입 |
| 🧭 맞춤 플랜 (엔드 투 엔드: 추천 → 가중치 → narrative → 담기 → 가이드 → 타임라인 → 캘린더) | ✅ `planner.py` `compose_action_plan` + `generate_recommendation_narratives` + `generate_action_guide` (Step A/B/C 누적). PDF 분석 결과까지 결합 |
| 📄 JPG/PNG/WEBP 자동 분석 (Gemini Vision) — 정부 공고 캡처 첨부도 7카테고리 추출 | ✅ Step 1 `analyze_image` — multimodal 직접, page=1 정규화 |
| 📋 HWPX (신형 한컴) 자동 분석 — stdlib ZIP+XML 추출 | ✅ Step 2 `hwpx_extractor` + `analyze_text` — 외부 의존성 0 |
| 🤖 공고 페이지 자동 fetch + 분석 백엔드 | ✅ Step 4 `auto_fetcher.fetch_and_analyze_announcement` — DB row 시스템 공유, magic 사전검증, idempotent, `/api/announcements/{id}/auto-fetch` |
| 🧭 맞춤 플랜이 자동 fetch 분석까지 활용 + 백그라운드 트리거 | ✅ Step 5 `_maybe_trigger_auto_fetch_async` (Top N 백그라운드 thread) + `_load_analyses_for_announcement` 가 사용자 첨부 + auto fetch 합본 |
| 📅 PDF 추출 일정 → 맞춤 플랜 타임라인 + 캘린더 통합 | ✅ Step 6 `_load_extracted_events_for_announcement` + 6 type 아이콘 매핑 + range/time 캘린더 push |
| 🔀 사이드바 4탭 → 2탭 통합 (옵션 B) | ✅ Phase 1+2+3 — 「공고 둘러보기 (전체/자격 맞춤 토글) + 내 액션 플랜」. 공고 분석은 카드 펼침 인라인으로 흡수, 비교도 자동 fetch 합본 사용 |
| 📝 HWP (구형) 자동 분석 — pyhwp의 hwp5txt CLI | ✅ A `hwp_extractor.py` — venv 환경 보강 (`_find_hwp5txt`), six 의존성 명시 |
| 🎯 카드 펼침 시 lazy auto-fetch + 비교 모달 자동 trigger | ✅ 옵션 A + Fix 4 — 사용자 명시 행동 시점에 분석 시작 |
| NTIS form-POST 첨부 다운로드 (스크래퍼 개선) | ❌ 현재는 단일 `download.do` URL 만 잡혀 HTML 응답 → "server returned HTML" 명확 에러로 차단됨 |
| 진행중 공고 전체 일괄 사전 분석 (cron 배치) | ❌ 사용자 펼침 / 맞춤 플랜 Top N 트리거에만 의존. 1회 ₩2K 예상 비용으로 전체 커버리지 ↑ 가능 |

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
