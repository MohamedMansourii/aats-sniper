# Competitions Map — kooora.com

Research date: 2026-06-12. Scope: information architecture of the COMPETITIONS section only (structure, navigation, URL patterns). No article text, images, or design assets were copied. 10 polite sequential fetches.

Pages fetched:
1. `/كل-البطولات` (competitions index — fetched twice with different extraction prompts; second hit served from cache)
2. `/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/2kwbbcootiqqgmrzs6o5inle5` (Premier League hub)
3. `/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/جدول/2kwbbcootiqqgmrzs6o5inle5` (PL standings)
4. `/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/مباريات/2kwbbcootiqqgmrzs6o5inle5` (PL matches)
5. `/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/أفضل-اللاعبين/2kwbbcootiqqgmrzs6o5inle5` (PL top players)
6. `/كرة-القدم/مسابقة/كاس-العالم/أخبار` — **404** (ID-less tab URL does not resolve; see §5)
7. `/كرة-القدم/مسابقة/كأس-العالم/70excpe1synn9kadnbppahdn7` (World Cup hub)
8. `/كرة-القدم/مسابقة/كأس-العالم/جدول/70excpe1synn9kadnbppahdn7` (WC group standings)
9. `/كرة-القدم/مسابقة/كأس-العالم/مباريات/70excpe1synn9kadnbppahdn7` (WC matches)

---

## 1. Competition index page (`/كل-البطولات`): organization scheme + structure

URL: `https://www.kooora.com/كل-البطولات` ("all tournaments").

**Primary organization scheme: by country, alphabetically (Arabic alphabet), with a separate international block.**

- Page heading: **جميع الدوريات والمسابقات للرجال** ("All leagues and men's competitions").
- **International / continental tournaments** sit in their own block — **مسابقات عالمية** ("world competitions") — above/separate from the country groupings (World Cup, UEFA Champions League, AFC Champions League Elite, CAF Champions League, UEFA Europa League...).
- **Domestic competitions** are nested under country headings sorted alphabetically in Arabic, e.g. آسيا (Asia, as a grouping), آيسلندا (Iceland), أذربيجان (Azerbaijan), أرمينيا (Armenia), أستراليا (Australia), ألبانيا (Albania), ألمانيا (Germany)... Each country heading expands/collapses to list that country's league divisions **and** domestic cups together (cups are filed under the same country heading as leagues, not in a separate "cups" section).
- **Filter/tab bar** at top of index: **الكل** (All) | **شائع** (Popular) | **جميع الدول** (All countries) | **دوري** (Leagues) | **رجال** (Men). So filtering axes are: popularity, country, competition type (league), and gender.
- No search box or sidebar observed in the main content area of this page.

### ~15 example competition URLs captured (slug pattern confirmation)

All relative to `https://www.kooora.com`:

| Competition (English) | URL |
|---|---|
| AFC Challenge League | `/كرة-القدم/مسابقة/دوري-التحدي-الأسيوي/c0KouXTug9v4KEYPMIIGm` |
| Icelandic League | `/كرة-القدم/مسابقة/الدوري-الأيسلندي/zilopfej2h0n3vpan5tcynpo` |
| Azerbaijani League | `/كرة-القدم/مسابقة/الدوري-الأذربيجاني/3428tckxcirwwh3o3jgc1m8ji` |
| Armenian League | `/كرة-القدم/مسابقة/الدوري-الأرميني/dn9kmfdbz74lzt3m8e9g0vwjq` |
| Bundesliga (German League) | `/كرة-القدم/مسابقة/الدوري-الألماني/6by3h89i2eykc341oz7lv1ddd` |
| La Liga (Spanish League) | `/كرة-القدم/مسابقة/الدوري-الإسباني/34pl8szyvrbwcmfkuocjm3r6t` |
| Serie A (Italian League) | `/كرة-القدم/مسابقة/الدوري-الإيطالي/1r097lpxe0xn03ihb7wi98kao` |
| English Premier League | `/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/2kwbbcootiqqgmrzs6o5inle5` |
| Ligue 1 (French League) | `/كرة-القدم/مسابقة/الدوري-الفرنسي/dm5ka0os1e3dxcp3vh05kmp33` |
| Saudi Roshn League | `/كرة-القدم/مسابقة/دوري-روشن-السعودي/ea0h6cf3bhl698hkxhpulh2zz` |
| Egyptian Premier League | `/كرة-القدم/مسابقة/الدوري-المصري-الممتاز/8k1xcsyvxapl4jlsluh3eomre` |
| Moroccan Botola Pro | `/كرة-القدم/مسابقة/الدوري-المغربي-الممتاز/1eruend45vd20g9hbrpiggs5u` |
| Qatar Stars League | `/كرة-القدم/مسابقة/دوري-نجوم-قطر/xaouuwuk8qyhv1libkeexwjh` |
| UAE Pro League (Arabian Gulf) | `/كرة-القدم/مسابقة/دوري-الخليج-العربي/f39uq10c8xhg5e6rwwcf6lhgc` |
| Jordanian League | `/كرة-القدم/مسابقة/الدوري-الأردني/145hkd59i6foieuwr4mwi6wlq` |
| FIFA World Cup 2026 | `/كرة-القدم/مسابقة/كأس-العالم/70excpe1synn9kadnbppahdn7` |
| UEFA Champions League | `/كرة-القدم/مسابقة/دوري-أبطال-أوروبا/4oogyu6o156iphvdvphwpck10` |
| UEFA Europa League | `/كرة-القدم/مسابقة/الدوري-الأوروبي/4c1nfi2j1m731hcay25fcgndq` |
| AFC Champions League Elite | `/كرة-القدم/مسابقة/دوري-أبطال-أسيا-النخبة/1fedahp0rws09tj451onten8r` |
| CAF Champions League | `/كرة-القدم/مسابقة/دوري-أبطال-أفريقيا/cse5oqqt2pzfcy8uz6yz3tkbj` |

**Key finding:** every competition URL ends in an **opaque alphanumeric entity ID** (mostly 24–25 chars lowercase, occasionally mixed-case/shorter, e.g. `c0KouXTug9v4KEYPMIIGm`). The Arabic slug is human-readable decoration; the ID is the canonical key. The slug-only pattern given in the brief (`/مسابقة/<slug>/أخبار` without ID) returns **404**.

---

## 2. Competition page template: layout blocks + full tab tree

Reference: Premier League hub (`/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/2kwbbcootiqqgmrzs6o5inle5`).

### Layout blocks (top → bottom)

1. **Global site header** — kooora logo + global nav (competitions, teams, players).
2. **Competition identity block** — competition badge/crest image + Arabic competition name (الدوري الإنجليزي الممتاز). No follow button, no season selector observed in the header.
3. **Tab/sub-navigation bar** (see tab tree below).
4. **Main column modules**, in order:
   - **News feed** — cards (image + headline + timestamp), the default content of the hub.
   - **Matches module (المباريات)** — recent results / upcoming fixtures strip with scores.
   - **Mini league table (ترتيب الدوري)** — top-5 standings widget linking to the full standings tab.
5. **Sidebar / secondary widgets:**
   - **Team crests grid** — all 20 club logos, each clickable to a team page.
   - **Ad slots** (إعلان) interspersed.
6. **Footer** — contact/privacy/terms links, social icons, app download buttons.

### Full tab tree (5 tabs; each tab keeps the trailing competition ID)

| # | Tab label (Arabic) | English | URL segment | URL | Content |
|---|---|---|---|---|---|
| 1 | معلومات | Information / Overview | *(none — base URL)* | `/كرة-القدم/مسابقة/<slug>/<id>` | Default hub: news feed + matches strip + mini table + team crests |
| 2 | الأخبار | News | `أخبار` | `/كرة-القدم/مسابقة/<slug>/أخبار/<id>` | Full competition news listing |
| 3 | المباريات | Matches (fixtures & results) | `مباريات` | `/كرة-القدم/مسابقة/<slug>/مباريات/<id>` | Round-by-round fixtures/results (§3.2) |
| 4 | ترتيب | Standings | `جدول` ("table") | `/كرة-القدم/مسابقة/<slug>/جدول/<id>` | Full standings table (§3.1). Note: tab *label* is ترتيب but URL *segment* is جدول |
| 5 | أفضل اللاعبين | Top players | `أفضل-اللاعبين` | `/كرة-القدم/مسابقة/<slug>/أفضل-اللاعبين/<id>` | Stat leaderboards incl. top scorers (§3.3) |

**Not present** as competition tabs (contrary to the brief's hypothesis): no dedicated الفرق (teams), انتقالات (transfers), or فيديو (videos) tabs on the new kooora competition template. Teams are reached via the crest grid widget; scorers live inside "أفضل اللاعبين" rather than a standalone الهدافين tab.

---

## 3. Tab sub-templates: structural components

### 3.1 Standings tab (ترتيب / URL segment `جدول`)

- **Single full-league table** (all 20 teams for the PL).
- **Columns, right-to-left order:**

| Arabic header | Meaning |
|---|---|
| مركز | Position |
| فريق | Team (crest image + linked name) |
| ل | Played (لعب) |
| ف | Wins (فوز) |
| ت | Draws (تعادل) |
| خ | Losses (خسارة) |
| ل | Goals for (له) — note: same letter glyph as "Played", disambiguated by position |
| ع | Goals against (عليه) |
| +/- | Goal difference |
| نقاط | Points |
| نتائج | Recent results / form strip |

- **Qualification color legend:** zones for Champions League (top, blue), Europa League, Conference League, Relegation (bottom, red).
- **Filter:** a **المستوى** dropdown with three options: **الكل** (All/overall) | **الذهاب** (first half of season / "going" rounds) | **الإياب** (second half / return rounds) — i.e. split tables by season half, not home/away.
- Same 5-tab nav + footer persist around the table.

### 3.2 Matches tab (المباريات / URL segment `مباريات`)

- **Primary grouping: by matchweek/round** — a round selector spanning **الجولة 1 → الجولة 38** (Round 1–38 for a 20-team league).
- **Secondary grouping: by date inside the round** — date headers (e.g. weekday + day + Arabic month name) above the matches played that day.
- **Match row/card structure:** home & away **crests**, **team names** (full + abbreviated), **score** (or kickoff time for future matches) centered between teams, **status badge** (e.g. انتهت = finished), whole row clickable.
- **Match detail link pattern:** `/كرة-القدم/مباراة/<match-slug>/<match-id>` (مباراة = match) — i.e. match pages live outside the competition path, under their own entity type.
- No fixtures-vs-results toggle; the round selector is the only navigation. **No pagination** — one round per view.

### 3.3 Top players tab (أفضل اللاعبين / URL segment `أفضل-اللاعبين`)

- **Stacked vertical leaderboard modules** (not sub-tabs), ~top-10 rows each:
  - **أفضل الهدافين** — Top scorers
  - **مساهمات** — Assists/goal contributions
  - **بطاقات حمراء** — Red cards
  - **بطاقات صفراء** — Yellow cards
  - **تسديدات على الهدف** — Shots on target
  - **أخطاء مرتكبة** — Fouls committed
  - **أخطاء ضده** — Fouls suffered
  - **عرقلات** — Tackles
  - **مرات التسلل** — Offsides
- **Row structure (e.g. top scorers):** rank number | team crest thumbnail | player name (link to player page, pattern `/كرة-القدم/.../<player-slug>/<player-id>`) | stat value.
- No season or stat-type filter dropdowns observed.

### 3.4 News tab (الأخبار)

Not fetched separately (budget); on the hub the news module is a standard card list (thumbnail + headline + timestamp), and the tab is the full-length version of that feed.

---

## 4. League vs tournament template differences (World Cup case)

World Cup hub: `/كرة-القدم/مسابقة/كأس-العالم/70excpe1synn9kadnbppahdn7` (currently the 2026 edition; note slug uses **كأس** with hamza — the ID-less **كاس**-العالم/أخبار URL from the brief 404s).

**Verdict: same 5-tab template, with tournament behavior expressed *inside* the standings and matches tabs rather than via extra tabs.**

| Aspect | League (PL) | Tournament (World Cup 2026) |
|---|---|---|
| Tab set | معلومات, الأخبار, المباريات, ترتيب, أفضل اللاعبين | **Identical** — no extra "groups" or "bracket" tab |
| Standings (`جدول`) | One 20-team table | **12 separate group tables** (المجموعة 1 … المجموعة 12, 2026's 48-team format), same column set as the league table; a "الجولة القادمة" (next round) strip under each group |
| Bracket / knockout view | n/a | **None** — no bracket visualization or group/knockout toggle on the standings tab |
| Qualification legend | CL/EL/Conference/relegation color zones | **No color legend** observed on group tables |
| Matches round selector | الجولة 1–38 | Group-stage rounds **الجولة 1/2/3**, then knockout entries: **1/16** (round of 32), **1/8** (round of 16), **ربع النهائي** (quarter-final), **نصف النهائي** (semi-final), **برونز** (third-place/bronze), **النهائي** (final) |
| Match cards | crests, names, time/score, status | Same, **plus prominent broadcast/TV-channel info** per fixture |
| Hub main column | news + matches + mini table | News carousel + upcoming-matches widget + **FAQ/SEO module** (tickets, records); no group-tables widget on the hub itself |
| Identity block | badge + name | Badge + name with edition branding (2026) |

---

## 5. Season handling + URL pattern summary

### Season handling

- **No season selector** was found anywhere: not on the league hub, standings, matches, or top-players tabs, nor on the World Cup pages.
- The World Cup matches tab carries an **أحدث موسم** ("latest season") label — pages are pinned to the current edition/season; no season-specific URL variant was discovered.
- Historical-season access, if it exists, is not exposed in the competition IA captured here (gap noted in RISKS).

### URL pattern summary table

All paths relative to `https://www.kooora.com`. `<id>` = opaque alphanumeric entity ID (required — slug-only URLs 404). Raw-Arabic URLs work; percent-encoded equivalents also valid.

| Page | Pattern | Example |
|---|---|---|
| Competitions index | `/كل-البطولات` | — |
| Competition hub (= معلومات tab) | `/كرة-القدم/مسابقة/<slug>/<id>` | `/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/2kwbbcootiqqgmrzs6o5inle5` |
| News tab | `/كرة-القدم/مسابقة/<slug>/أخبار/<id>` | `.../الدوري-الإنجليزي-الممتاز/أخبار/2kwbbcootiqqgmrzs6o5inle5` |
| Matches tab | `/كرة-القدم/مسابقة/<slug>/مباريات/<id>` | `.../كأس-العالم/مباريات/70excpe1synn9kadnbppahdn7` |
| Standings tab | `/كرة-القدم/مسابقة/<slug>/جدول/<id>` | `.../كأس-العالم/جدول/70excpe1synn9kadnbppahdn7` |
| Top players tab | `/كرة-القدم/مسابقة/<slug>/أفضل-اللاعبين/<id>` | `.../الدوري-الإنجليزي-الممتاز/أفضل-اللاعبين/2kwbbcootiqqgmrzs6o5inle5` |
| Match detail (linked from matches tab) | `/كرة-القدم/مباراة/<match-slug>/<match-id>` | — |
| Player detail (linked from leaderboards) | `/كرة-القدم/<...>/<player-slug>/<player-id>` | — |

Pattern grammar: `/{sport}/{entity-type}/{arabic-slug}/[{tab-segment}/]{entity-id}` where sport = `كرة-القدم` (football), entity-type ∈ { `مسابقة` competition, `مباراة` match, ... }, and the tab segment (when present) is inserted **between slug and ID**.
