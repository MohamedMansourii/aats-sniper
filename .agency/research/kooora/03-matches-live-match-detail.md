# Match Center Map — kooora.com

Research date: 2026-06-12. Scope: structure only (no content copied). 8 polite sequential fetches.
Site is RTL Arabic; all canonical URLs use Arabic slugs (served percent-encoded in `href` attributes — both forms resolve identically).

---

## 1. Matches hub (live scores) page structure & URL pattern (incl. date navigation)

**URL:** `https://www.kooora.com/كرة-القدم/مباريات-اليوم`
(`كرة-القدم` = football, `مباريات-اليوم` = today's matches)

**Important finding — single URL for "Live" and "Today":** the site header has two nav items, `مباشر` (Live) and `مباريات اليوم` (Today's Matches), but both hrefs decode to the **same URL** `/كرة-القدم/مباريات-اليوم`. There is **no separate live-scores URL**; "Live" is a client-side filter state of this one hub page. The homepage live-scores carousel's "جميع النتائج" (All results) link also points here.

### 1.1 Date navigation
- **Horizontal date strip:** 7 days centered on today (e.g., Tue 9 June → Mon 15 June), each day a tappable pill.
- **Monthly calendar grid:** full month (June 2026) below/behind the strip, with weekday header row (`ن ث ر خ ج س ح`).
- **Both are JavaScript-driven, NOT links.** Verified:
  - Date-strip items carry no `href` attributes (plain text/button nodes).
  - `?date=2026-06-13` query param: page still rendered June 12 fixtures (param ignored).
  - Path probe `/كرة-القدم/مباريات-اليوم/2026-06-13`: **HTTP 404**.
- **Conclusion:** there is no server-side date-parameterized URL. Other dates are loaded via client-side XHR into the same hub URL. Anyone re-implementing this needs a date-state on one route (or must add their own `?date=` pattern — kooora does not expose one).

### 1.2 Filter tabs
Top of the match list (client-side toggles, same URL):
- `الكل` (All) — default
- `مباشر` (Live) — with live-match count badge, e.g. "مباشر (1)"
- `المفضلة` (Favorites) — user-starred matches, e.g. "(0)"

No separate finished/upcoming tabs; status is conveyed per-row. No competition dropdown filter observed — competition filtering is done by the grouping itself.

### 1.3 Match grouping
Matches are grouped under **competition section headers**, each header showing: country flag icon + competition name + country name + round label, and linking to the competition page. Examples seen:
- `كأس العالم` (World Cup) — "الجولة 1" (Round 1)
- `دوري بيلاروسيا الممتاز` (Belarusian Premier League) — "الجولة 11"
- `الدوري المغربي الممتاز` (Moroccan Premier League) — "الجولة 24"
- `الدوري الأيرلندي الممتاز` (Irish Premier League) — "الجولة 20"

Competition header link example (verbatim, decoded):
`/كرة-القدم/مسابقة/كأس-العالم/70excpe1synn9kadnbppahdn7` (`مسابقة` = competition)

### 1.4 Match row anatomy
Each row contains, structurally:
| Element | Notes |
|---|---|
| Home team | crest image + name (Arabic), links to team page |
| Away team | crest image + name, links to team page |
| Center cell | score `0 – 0` for live/finished, or kickoff time `21:00` for upcoming |
| Status label | live minute `8'`, `مباشر` (live), kickoff time (not started), `مؤجلة` (postponed), finished state |
| Round | `الجولة X` at group-header level |
| Broadcaster block | "شاهد مباشرة على" (watch live on) + channel logos/names, e.g. beIN Sports MENA Max 1/2, Arryadia HD — links go to **external** broadcaster sites (e.g. `https://connect.bein.com/live/75`, `https://snrtlive.ma/`) |
| Row click target | the match detail page |

**Links offered per row:** (1) match detail, (2) home team page, (3) away team page, (4) competition page (via group header), (5) external broadcaster link(s).

### 1.5 Match detail URL pattern (captured from rows)
`/كرة-القدم/مباراة/<home-slug>-v-<away-slug>/<opaque-alphanumeric-id>`
(`مباراة` = match; teams joined by `-v-`; sometimes rendered `-ضد-` ("vs") in links on other pages — both observed)

Verbatim examples (decoded):
- `/كرة-القدم/مباراة/كوريا-الجنوبية-v-تشيكيا/fE4Kl8TDt5GkVQ9xvEl14` (South Korea v Czechia)
- `/كرة-القدم/مباراة/كندا-v-البوسنة-والهرسك/JJwT9wAsUuHF3aoeMKYNC` (Canada v Bosnia)
- `/كرة-القدم/مباراة/الفتح-الرباطي-v-المغرب-الفاسي/yWHSffoe9dQQNyFVRatwv` (Fath Rabat v Maghreb Fès)

The trailing ID is an opaque ~21-char mixed-case alphanumeric token (Opta/Stats-Perform-style), not numeric.

---

## 2. Match detail template: layout blocks + tab tree

**Page fetched:** `/كرة-القدم/مباراة/كوريا-الجنوبية-v-تشيكيا/fE4Kl8TDt5GkVQ9xvEl14` (live at 9' during fetch).

### 2.1 Header block (top of page)
1. Competition breadcrumb/badge: `كأس العالم` + round `كأس العالم - الجولة 1`, links to competition page
2. Match date/time: `12 يونيو 2026, 04:00`
3. Home team: crest + name (links to team page)
4. Score center: `0 – 0` + live minute badge `9'` (kickoff time shown pre-match)
5. Away team: crest + name (links to team page)
6. Venue: stadium name (e.g., `أكرون`)
7. Broadcast channels with external watch links (beIN Sports MENA Max 1 / Max 2)

### 2.2 Tab tree
Tabs observed (Arabic verbatim → English):

| Tab (verbatim) | English | URL? |
|---|---|---|
| `نظرة عامة` | Overview (default) | same URL |
| `الأحداث الرئيسية` | Key events (timeline: goals/cards/subs) | same URL |
| `التشكيلة` | Lineups | same URL |
| `تعليق` | Commentary (minute-by-minute) | same URL |
| `ترتيب` | Standings | same URL |
| `مناقشة` | Discussion (fan comments) | same URL |

**All tabs are client-side JS tabs — NOT separate URLs.** Verified three ways:
- No `<a href>` on the tab labels (rendered as text/buttons in the HTML).
- No hrefs anywhere on the page contain the match ID `fE4Kl8TDt5GkVQ9xvEl14` with a path suffix or `?tab=` param.
- Direct probe of `/…/fE4Kl8TDt5GkVQ9xvEl14/التشكيلة` → **HTTP 404**.

Tab content (lineups, commentary, etc.) is hydrated client-side after load; the lineups markup is not present in the initial server HTML.

Note: this tab set may vary by match state (e.g., a pre-match page may show preview/H2H instead of الأحداث الرئيسية/تعليق) — only the live state was sampled within the fetch budget.

### 2.3 Main content blocks on the default (Overview) view, in order
1. **Live broadcast widget** — channel logos + external watch links
2. **Prediction poll** — `توقع الفائز` (predict the winner): 3-way vote bar (home/draw/away) with live percentages
3. **Standings snippet** — group/league mini-table of the match's competition (here: World Cup group with Mexico, Czechia, South Korea, South Africa), team rows link to team pages
4. **Match statistics** — possession bar (e.g., 35% / 65%), expected goals (xG), shots, corners, fouls
5. **Detailed stats sub-sections** — attacking / defensive / disciplinary groupings
6. **Match details footer block** — competition badge, date, venue, broadcaster recap

### 2.4 Sidebar
No persistent sidebar on the match page; broadcast options and ad slots (`إعلان`) are inline in the main column.

### 2.5 Entity links on the page (verbatim, decoded)
- Team: `/كرة-القدم/فريق/كوريا-الجنوبية/1yghbv1c71b37eenutbwnvvq` (`فريق` = team)
- Team: `/كرة-القدم/فريق/تشيكيا/70tnqyqn871jwlk26gtjw7knm`
- Competition: `/كرة-القدم/مسابقة/كأس-العالم/70excpe1synn9kadnbppahdn7`
- Player (seen in site-wide modules): `/كرة-القدم/لاعب/<player-slug>/أخبار/<id>` (`لاعب` = player, `أخبار` = news — player URLs embed a section segment)

---

## 3. Match sub-page templates (lineups/stats/etc.): components + URL patterns

**There are no match sub-page URLs.** All match-detail facets (lineups, commentary, key events, standings, discussion) live behind JS tabs on the single match URL:

`/كرة-القدم/مباراة/<home>-v-<away>/<matchId>`

Evidence:
- `/…/<matchId>/التشكيلة` → 404 (probed)
- No tab `<a href>` elements and no matchId-suffixed hrefs in the page HTML
- Lineups/commentary content absent from server HTML → hydrated via XHR

**Implication for rebuild/scraping:** tab data comes from background API calls, not crawlable URLs. A clone should either replicate the SPA-tab pattern or deliberately improve on kooora by giving each tab its own route (better for SEO/deep-linking).

Structural components per tab (from labels + overview rendering):
- **التشكيلة (Lineups):** starting XI per team, substitutes, (formation graphic rendered client-side — not in server HTML, could not be confirmed structurally within budget)
- **الأحداث الرئيسية (Key events):** chronological timeline of goals/cards/substitutions
- **تعليق (Commentary):** minute-by-minute text feed
- **ترتيب (Standings):** same standings table component as the Overview snippet, full version
- **مناقشة (Discussion):** comment thread widget

---

## 4. Broadcast schedule page structure

**URL:** `https://www.kooora.com/أحداث-رياضية/كرة-القدم`
(`أحداث-رياضية` = sports events; header nav label: `جدول البث` = broadcast schedule)

- **Purpose/title:** "شاهد البث المباشر لكرة القدم على التلفزيون اليوم" — TV broadcast guide for football matches (where/when to watch).
- **Date navigation:** horizontal tabs — `اليوم` (Today) + next 6 weekdays with dates (e.g., السبت 13 يونيو … الخميس 18 يونيو). No date hrefs observed → JS tabs, same URL.
- **Grouping:** by competition sections (same pattern as the hub): e.g., `كأس العالم`, `الدوري المغربي الممتاز`.
- **Listing card structure:** competition logo + name | team crests + names | time or `مباشر` (live) badge | broadcaster logo + name (e.g., beIN Sports MENA Max 1) | external watch link (`https://connect.bein.com/live/75`).
- **Card links:** match detail page (here the slug joiner renders as `-ضد-` "vs": `/كرة-القدم/مباراة/كوريا-الجنوبية-ضد-تشيكيا/fE4Kl8TDt5GkVQ9xvEl14` — same match ID as the `-v-` form, so both joiners resolve to the same match) + competition page link.
- **Other sports variants** (from its sport switcher; note the different ID style — CMS `blt…` IDs, suggesting these schedule hubs are CMS pages, unlike the Opta-ID match pages):
  - `التنس` (tennis): `/تنس/blta843d4a7d2cc7ebc`
  - `الفورمولا 1`: `/الفورملا-1/bltd557251dfd094243`
  - `كرة السلة` (basketball): `/كرة-سلة/blt1cf15af39086dfdf`
- **Sidebar:** none; full-width cards with inline ad slots (`إعلان`).

---

## 5. URL pattern summary table for everything in the match center

| Entity / view | URL pattern (Arabic slugs verbatim) | Translation / notes | ID style |
|---|---|---|---|
| Matches hub (today + live + any date) | `/كرة-القدم/مباريات-اليوم` | football / today's-matches — single URL; date & live filters are client-side; `?date=` ignored; path dates 404 | — |
| "Live" nav item | same as above | `مباشر` header link decodes to the identical URL — no dedicated live route | — |
| Match detail (all tabs) | `/كرة-القدم/مباراة/<home>-v-<away>/<matchId>` | football / match / team-pair; joiner is `-v-` (also seen `-ضد-`, same ID) | opaque ~21-char alphanumeric (Opta-style), e.g. `fE4Kl8TDt5GkVQ9xvEl14` |
| Match sub-tabs (lineups/stats/commentary/standings/discussion) | none — JS tabs on the match URL | probed `/<matchId>/التشكيلة` → 404 | — |
| Competition | `/كرة-القدم/مسابقة/<competition-slug>/<id>` | football / competition | opaque alphanumeric, e.g. `70excpe1synn9kadnbppahdn7` |
| Team | `/كرة-القدم/فريق/<team-slug>/<id>` | football / team | opaque alphanumeric, e.g. `1yghbv1c71b37eenutbwnvvq` |
| Player | `/كرة-القدم/لاعب/<player-slug>/أخبار/<id>` | football / player / news — player URLs carry a section segment | opaque alphanumeric |
| Broadcast schedule (football) | `/أحداث-رياضية/كرة-القدم` | sports-events / football; nav label `جدول البث` | — |
| Broadcast schedule (other sports) | `/تنس/blt…`, `/الفورملا-1/blt…`, `/كرة-سلة/blt…` | per-sport hubs | CMS `blt…` IDs (Contentstack-style) |
| News index | `/أخبار` | news | — |
| External watch links | `https://connect.bein.com/live/<n>`, `https://snrtlive.ma/` | broadcaster sites, open off-site | — |

### Cross-cutting observations
- Two ID families coexist: **Opta-style opaque tokens** for football data entities (match/team/competition/player) and **`blt…` CMS IDs** for editorial/section pages — the match center is a data-feed layer skinned over a CMS site.
- Heavy client-side hydration: date switching, status filters, and every match tab are XHR-driven; only the default Overview view is server-rendered. Deep links exist only at the match level, not the tab level.
- Match rows consistently expose exactly five link types: match, home team, away team, competition, external broadcaster.

### Gaps / not verified (fetch budget)
- Lineups tab internals (formation graphic, per-player links) — content is JS-hydrated, absent from server HTML.
- Tab set on a pre-match or finished match (only a live match was sampled).
- The XHR/API endpoints feeding date switches and tabs (would need browser network inspection, out of scope for polite static fetching).
