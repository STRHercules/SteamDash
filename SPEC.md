# Steam Dashboard — Product Spec

> Real-time Steam sales monitoring dashboard for indie game developers.
> Single Python file, zero dependencies, web-based setup.

## Vision

`python3 dashboard.py` → 브라우저 열기 → 설정 마법사 → 대시보드 시작.
config.json 편집 필요 없음. 개발자가 아니어도 쓸 수 있을 정도로 간단하게.

---

## Phases

### Phase 1 — Core Dashboard ✅ DONE

단일 게임, 다크 테마, 한/영 전환, 기본 데이터 수집.

**파일:** `dashboard.py` (2059줄, 문법 통과)

**포함 기능:**
- [x] config.json 로딩 + CLI 대화형 세팅 (fallback)
- [x] SQLite 데이터 저장 (판매/동접/리뷰/위시리스트)
- [x] Steam API: 동접, 리뷰, 앱 정보
- [x] Financial API: 일별 판매, 국가별 판매, 위시리스트
- [x] 텔레그램 알림 (판매/리뷰/동접 급증/위시리스트)
- [x] 대시보드 HTML: 메트릭 카드 8개, 차트 3개, 국가별, 리뷰
- [x] i18n (한/영 토글)
- [x] 다크 테마 + 5종 액센트 (wine/ocean/forest/amber/slate)
- [x] 모바일 반응형 (4 breakpoint)
- [x] SO_REUSEADDR (재시작 안정성)
- [x] 비싼 API 호출 1시간 주기
- [x] fetch 에러 백오프
- [x] DataCollector 클래스

**검증 필요:**
- [ ] 실제 실행 테스트 (API 키 넣고 돌려보기)
- [ ] 모바일 스크린샷 확인
- [ ] 차트 포인트 사이즈 모바일 대응
- [ ] Python 트리플 쿼트 안 JS 정규식 문제 없는지

---

### Phase 2 — Web Setup Wizard ✅ DONE

웹 마법사로 설정. SQLite에 저장. config.json 불필요.

**새 파일/변경:**
- `dashboard.py` 수정

**DB 변경:**
```sql
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

주요 설정 키:
- `steam_api_key`, `steam_financial_key`
- `games` (JSON array: [{app_id, name, launch_date}, ...])
- `telegram_enabled`, `telegram_bot_token`, `telegram_chat_ids`
- `theme` (dark/light), `accent` (wine/ocean/forest/amber/slate)
- `language` (ko/en)
- `port` (default 8081)

**Setup Wizard UI (SETUP_HTML):**

```
┌─────────────────────────────────────────────┐
│         Steam Dashboard Setup               │
│  ① API Keys  ② Games  ③ Alerts  ④ Theme    │
├─────────────────────────────────────────────┤
│                                             │
│  Steam API Key                              │
│  ┌─────────────────────────────────────┐    │
│  │                                     │    │
│  └─────────────────────────────────────┘    │
│  ℹ Get your key at steamcommunity.com/dev   │
│                                             │
│  Steam Financial API Key                    │
│  ┌─────────────────────────────────────┐    │
│  │                                     │    │
│  └─────────────────────────────────────┘    │
│  ℹ From Steamworks Partner dashboard        │
│                                             │
│                          [ Next → ]         │
└─────────────────────────────────────────────┘
```

**스텝 구성:**
1. **API Keys** — Steam API Key + Financial API Key + 설명 링크
2. **Games** — App ID 입력 + "Test Connection" 버튼 + Launch Date + "Add Another Game" 버튼
3. **Alerts** — 텔레그램 On/Off 토글, bot token + chat IDs (optional)
4. **Theme** — Dark/Light 토글 미리보기 + 액센트 컬러 스와치 5종 + 언어 KR/EN

**HTTP 라우팅 추가:**
- `GET /` → 설정 없으면 SETUP_HTML, 있으면 DASHBOARD_HTML
- `POST /api/setup` → 설정 저장 → redirect /
- `GET /api/test-connection?key=...&app_id=...` → 키 검증 (200/400)
- `GET /settings` → 설정 페이지 (마법사와 같은 폼, 값 pre-fill)
- `POST /api/settings` → 설정 업데이트

**Settings 접근:** 대시보드 헤더에 ⚙ 톱니바퀴 아이콘 → /settings

---

### Phase 3 — Multi-game Support ✅ DONE

여러 게임을 동시에 모니터링. 게임별 데이터 테이블.

**DB 변경:**
- 모든 데이터 테이블에 `app_id` 컬럼 추가 (또는 테이블명에 prefix)
- 기존 단일 게임 데이터 마이그레이션

**데이터 수집:**
```python
class DataCollector:
    def __init__(self, games, ...):
        self.games = games  # [{app_id, name, launch_date}, ...]

    def collect_data(self):
        for game in self.games:
            self._collect_for_game(game)
```

**Dashboard UI:**
- 게임 1개 → 변화 없음
- 게임 2개+ → 헤더에 게임 셀렉터 드롭다운
```
┌──────────────────────────────────────────┐
│ [Game Image] Grand Cru ▾  │ LIVE  KR/EN │
│             ├─ Grand Cru ──┤             │
│             ├─ My Other Game┤            │
│             └──────────────┘             │
└──────────────────────────────────────────┘
```

**API:**
- `GET /api/data` → 기본 첫 번째 게임
- `GET /api/data?app_id=4451370` → 특정 게임
- `GET /api/games` → 등록된 게임 목록

**텔레그램:** 알림 메시지에 게임 이름 포함
```
💰 [Grand Cru] 새 판매 +2건!
```

---

### Phase 4 — Polish & Release

**README.md:**
- 영어 메인, 한국어 섹션 포함
- 스크린샷: 설정 마법사, 다크 대시보드, 라이트 대시보드, 모바일
- Quick Start: 3줄이면 끝
- Features 리스트
- Configuration 설명
- FAQ

**추가 기능:**
- [ ] 라이트 테마 완성도 높이기
- [ ] 스크린샷 자동 캡처 (Playwright)
- [ ] GitHub Actions CI (문법 체크)
- [ ] LICENSE (MIT)
- [ ] .gitignore (dashboard.db, config.json, __pycache__)

---

## Tech Constraints

| 항목 | 규칙 |
|------|------|
| 파일 | 단일 `dashboard.py` (HTML 인라인) |
| 의존성 | Python 3.8+ 표준 라이브러리 only |
| DB | SQLite (파일 1개, 같은 디렉토리) |
| 프론트엔드 | Chart.js CDN, Google Fonts CDN |
| Python 문자열 | `'''` 안에서 `\n` regex 금지, `<>` regex 금지, DOM 기반 이스케이프 사용 |
| 보안 | API 키는 config/DB에만 저장, 대시보드 HTML에 노출 금지 |

---

## Design System

### Fonts
- Display: Crimson Pro (serif)
- Body: DM Sans (sans-serif)
- Data: JetBrains Mono (monospace)

### Dark Theme
| Token | Value |
|-------|-------|
| bg-base | #080509 |
| bg-surface | #1a0f14 |
| bg-elevated | #241520 |
| border | #3a2030 |
| text-primary | #e8ddd0 |
| text-secondary | #9a8878 |
| text-tertiary | #6a5a4e |

### Light Theme
| Token | Value |
|-------|-------|
| bg-base | #f8f6f3 |
| bg-surface | #ffffff |
| bg-elevated | #f0ece8 |
| border | #e0d8d0 |
| text-primary | #2a2420 |
| text-secondary | #6a5a50 |
| text-tertiary | #9a8a80 |

### Accent Colors
| Name | Main | Dim |
|------|------|-----|
| wine | #a84a56 | #722f37 |
| ocean | #4a8aaa | #2f5a72 |
| forest | #5a9a5e | #2f6a37 |
| amber | #c9a84c | #8a7434 |
| slate | #7a8a9a | #4a5a6a |

---

## Execution Order

1. **지금** — Phase 1 검증 (실행 테스트, 버그 수정)
2. **Phase 2** — 설정 마법사 (스크린샷 찍을 수 있는 핵심)
3. **Phase 3** — 멀티게임
4. **Phase 4** — README + 스크린샷 + GitHub 공개
