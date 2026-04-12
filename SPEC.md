# Steam Dashboard — Product Spec

> Real-time Steam sales monitoring dashboard for indie game developers.
> Single Python file, zero dependencies, web-based setup.

## Vision

`python3 dashboard.py` → Open Browser → Setup Wizard → Dashboard Starts.
No need to edit config.json. Simple enough for non-developers to use.

---

## Phases

### Phase 1 — Core Dashboard ✅ DONE

Single game, dark theme, KR/EN toggle, basic data collection.

**File:** `dashboard.py` (2059 lines, passes syntax check)

**Included Features:**
- [x] config.json loading + CLI interactive setup (fallback)
- [x] SQLite data storage (sales/ccu/reviews/wishlists)
- [x] Steam API: CCU (concurrent users), reviews, app details
- [x] Financial API: Daily sales, regional sales, wishlists
- [x] Telegram alerts (sales/reviews/ccu spikes/wishlists)
- [x] Dashboard HTML: 8 metric cards, 3 charts, regional breakdown, recent reviews
- [x] i18n (KR/EN toggle)
- [x] Dark theme + 5 accent colors (wine/ocean/forest/amber/slate)
- [x] Mobile responsive (4 breakpoints)
- [x] SO_REUSEADDR (restart stability)
- [x] Expensive API calls at 1-hour intervals
- [x] Fetch error backoff
- [x] DataCollector class

**Needs Verification:**
- [ ] Actual execution test (running with actual API keys)
- [ ] Check mobile screenshots
- [ ] Adjust chart point sizes for mobile
- [ ] Ensure no regex issues with JS inside Python triple quotes

---

### Phase 2 — Web Setup Wizard ✅ DONE

Configure via web wizard. Saved to SQLite. No need for config.json.

**New File / Changes:**
- Modify `dashboard.py`

**DB Changes:**
```sql
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

Key configuration keys:
- `steam_api_key`, `steam_financial_key`
- `games` (JSON array: [{app_id, name, launch_date}, ...])
- `telegram_enabled`, `telegram_bot_token`, `telegram_chat_ids`
- `theme` (dark/light), `accent` (wine/ocean/forest/amber/slate)
- `language` (ko/en)
- `port` (default 8081)

**Setup Wizard UI (SETUP_HTML):**

```text
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

**Step Breakdown:**
1. **API Keys** — Steam API Key + Financial API Key + description links
2. **Games** — Input App ID + "Test Connection" button + Launch Date + "Add Another Game" button
3. **Alerts** — Telegram On/Off toggle, bot token + chat IDs (optional)
4. **Theme** — Dark/Light toggle preview + 5 accent color swatches + KR/EN language selection

**HTTP Routing Additions:**
- `GET /` → Return SETUP_HTML if no settings, otherwise DASHBOARD_HTML
- `POST /api/setup` → Save settings → redirect to /
- `GET /api/test-connection?key=...&app_id=...` → Validate key (200/400)
- `GET /settings` → Settings page (same form as wizard, values pre-filled)
- `POST /api/settings` → Update settings

**Settings Access:** ⚙ Cog icon in the dashboard header → /settings

---

### Phase 3 — Multi-game Support ✅ DONE

Monitor multiple games simultaneously. Data tables per game.

**DB Changes:**
- Add `app_id` column to all data tables (or prefix table names)
- Migrate existing single-game data

**Data Collection:**
```python
class DataCollector:
    def __init__(self, games, ...):
        self.games = games  # [{app_id, name, launch_date}, ...]

    def collect_data(self):
        for game in self.games:
            self._collect_for_game(game)
```

**Dashboard UI:**
- 1 game → No changes
- 2+ games → Game selector dropdown in the header
```text
┌──────────────────────────────────────────┐
│ [Game Image] Grand Cru ▾  │ LIVE  KR/EN │
│             ├─ Grand Cru ──┤             │
│             ├─ My Other Game┤            │
│             └──────────────┘             │
└──────────────────────────────────────────┘
```

**API:**
- `GET /api/data` → Defaults to the first game
- `GET /api/data?app_id=4451370` → Specific game
- `GET /api/games` → List of registered games

**Telegram:** Include game name in alert messages
```text
💰 [Grand Cru] New sale +2 units!
```

---

### Phase 4 — Polish & Release

**README.md:**
- Main content in English, include a Korean section
- Screenshots: Setup wizard, dark dashboard, light dashboard, mobile
- Quick Start: Done in 3 lines
- Features list
- Configuration explanation
- FAQ

**Additional Features:**
- [ ] Improve light theme quality
- [ ] Auto-capture screenshots (Playwright)
- [ ] GitHub Actions CI (syntax check)
- [ ] LICENSE (MIT)
- [ ] .gitignore (dashboard.db, config.json, __pycache__)

---

## Tech Constraints

| Item | Rule |
|------|------|
| Files | Single `dashboard.py` (Inline HTML) |
| Dependencies | Python 3.8+ Standard Library only |
| DB | SQLite (1 file, same directory) |
| Frontend | Chart.js CDN, Google Fonts CDN |
| Python Strings | No `\n` regex inside `'''`, no `<>` regex, use DOM-based escaping |
| Security | API keys stored only in config/DB, never exposed in dashboard HTML |

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

1. **Now** — Phase 1 verification (execution tests, bug fixes)
2. **Phase 2** — Setup wizard (core required to take screenshots)
3. **Phase 3** — Multi-game
4. **Phase 4** — README + Screenshots + GitHub Release
