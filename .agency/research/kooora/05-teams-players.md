# Teams & Players Map — kooora.com

Research date: 2026-06-12. Method: 8 polite sequential fetches (homepage, club team page + 2 tabs, national team page, player page + stats tab, 1 match detail page). Structure only — no article text, images, or design assets copied. All paths are relative to `https://www.kooora.com`. Site is RTL Arabic; sport segment `كرة-القدم` = "football".

---

## 1. Team URL pattern + club team page template (blocks + tab tree)

### URL pattern

```
/كرة-القدم/فريق/<team-slug>/<opta-style-id>            ← default "Info" tab
/كرة-القدم/فريق/<team-slug>/<tab-slug>/<opta-style-id>  ← any other tab
```

- `فريق` = "team". `<team-slug>` is the Arabic team name, hyphen-separated (e.g., `ريال-مدريد` = Real Madrid, `مانشستر-سيتي` = Manchester City).
- `<id>` is a stable lowercase alphanumeric entity ID (~25 chars, e.g., `3kq9cckrnlogidldtdie2fkbl` for Real Madrid). The ID, not the slug, is canonical.
- The tab slug is inserted **between** the team slug and the ID.
- Same pattern serves clubs and national teams (no separate namespace).
- Raw Arabic URLs and percent-encoded equivalents both resolve.

Sample club URLs verified on homepage (footer/nav):

| Club | Path |
|---|---|
| ريال مدريد (Real Madrid) | `/كرة-القدم/فريق/ريال-مدريد/3kq9cckrnlogidldtdie2fkbl` |
| برشلونة (Barcelona) | `/كرة-القدم/فريق/برشلونة/agh9ifb2mw3ivjusgedj7c3fe` |
| ليفربول (Liverpool) | `/كرة-القدم/فريق/ليفربول/c8h9bw1l82s06h77xxrelzhur` |
| بايرن ميونخ (Bayern Munich) | `/كرة-القدم/فريق/بايرن-ميونخ/apoawtpvac4zqlancmvw4nk4o` |
| باريس سان جيرمان (PSG) | `/كرة-القدم/فريق/باريس-سان-جيرمان/2b3mar72yy8d6uvat1ka6tn3r` |

The homepage footer/nav exposes ~25 club links (La Liga, Premier League, Serie A, Bundesliga, Ligue 1 top clubs) and ~14 national teams. Note: no Saudi/Egyptian club links (Al-Hilal, Al-Ahly) were present on the homepage itself — only national teams from MENA.

### Club team page template (verified: Real Madrid)

**Identity header block**
- Club crest (logo image, CDN: sportfeeds.io)
- Team name (Arabic)
- SEO title formula: "<team> النتائج والإحصائيات وأبرز اللقطات" (results, stats, highlights)
- Horizontal tab bar directly below

**Tab tree** (tab slug → meaning):

| Arabic tab label | Tab URL slug | English | Path example (Real Madrid) |
|---|---|---|---|
| معلومات | *(none — default)* | Info / Overview | `/كرة-القدم/فريق/ريال-مدريد/3kq9cckrnlogidldtdie2fkbl` |
| الأخبار | `أخبار` | News | `/كرة-القدم/فريق/ريال-مدريد/أخبار/<id>` |
| فيديوهات | `فيديوهات` | Videos | `/كرة-القدم/فريق/ريال-مدريد/فيديوهات/<id>` |
| المباريات | `مباريات` | Matches / Fixtures | `/كرة-القدم/فريق/ريال-مدريد/مباريات/<id>` |
| قائمة اللاعبين | `قائمة-اللاعبين` | Squad list | `/كرة-القدم/فريق/ريال-مدريد/قائمة-اللاعبين/<id>` |
| ترتيب | `جدول` | Standings (label "ترتيب", slug `جدول` = table) | `/كرة-القدم/فريق/ريال-مدريد/جدول/<id>` |
| أفضل اللاعبين | `أفضل-اللاعبين` | Top players (club stats leaders) | `/كرة-القدم/فريق/ريال-مدريد/أفضل-اللاعبين/<id>` |

Note: there is **no dedicated transfers (انتقالات) tab** at team level; transfer content surfaces through the news feed and through player career pages.

**Main content modules on default (Info) tab, in order**
1. Team news feed — "<team> الأخبار", ~5 featured articles + "more news" link to the news tab
2. Recent/next matches module ("المباريات") — compact match cards
3. League table snippet — "الدوري الإسباني" top-5 rows, links to full standings tab
4. Latest completed match card (score line)

**Sidebar**: none detected — single-column, mobile-first layout. Ad slots interleaved between modules. Footer: social links, app-store links (iOS/Android), legal pages.

**Entity cross-links from club page**: player links inside news items only; squad access via the squad tab; no coach or stadium links in the header.

---

## 2. National team template differences

Verified: المغرب (Morocco), `/كرة-القدم/فريق/المغرب/avggs3u2b5cu8i1dnzknhth52`.

Same `فريق` URL pattern and largely the same template. Differences observed:

| Aspect | Club page | National team page |
|---|---|---|
| Tab bar | 7 tabs incl. أفضل اللاعبين (Top players) | 6 tabs — **أفضل اللاعبين tab absent** |
| Standings tab context | Domestic league table | Tournament/group standings (World Cup 2026 group) |
| Matches widget labels | League/cup competition labels | المباريات الودية (friendlies) mixed with كأس العالم (World Cup); broadcast badge (beIN Sports) more prominent |
| Standings snippet on Info tab | League table snippet present | No standings table on Info tab |
| Identity header | Crest + league context | Crest/flag badge, no confederation badge, no home stadium info |

Tabs verified on Morocco: معلومات (default), أخبار, فيديوهات, مباريات, قائمة-اللاعبين, جدول — identical slug scheme `/كرة-القدم/فريق/المغرب/<tab>/<id>`.

National team URLs seen on homepage: الأرجنتين, البرازيل, فرنسا, البرتغال, إسبانيا, ألمانيا, السعودية, المغرب, مصر, قطر, العراق, الأردن, الجزائر, تونس (each `/كرة-القدم/فريق/<slug>/<id>`).

---

## 3. Team tab sub-templates (squad, matches…)

### 3a. Squad list — `قائمة-اللاعبين` (verified: Real Madrid)

- **Grouping**: four position sections with Arabic headers, in order:
  1. حراس المرمى (Goalkeepers)
  2. المدافعون (Defenders)
  3. خط الوسط (Midfielders)
  4. المهاجمون (Forwards)
- **Player card fields**: headshot photo, jersey number, linked player name, numeric season stats (appearances/goals-type metrics).
- **Player links** from squad rows go to the player entity: `/كرة-القدم/لاعب/<player-slug>/<id>` (e.g., Bellingham → `…/e83ula4wockmc2xid7185kcq2`, Vinícius → `…/b4witgokm49tidm1a83qcvbop`).
- **Coach/manager**: listed at the bottom of the squad (name + headshot, e.g., جوزيه مورينيو) but **not hyperlinked** — no coach entity page reachable from here.
- Page keeps the full team tab bar; footer + ad slots as elsewhere.

### 3b. Matches — `مباريات` (verified: Real Madrid)

- **Grouping**: chronological by month (Arabic month headers, e.g., يونيو 2025, أغسطس 2025); month sections act as headings.
- **Match row fields**: both team crests + names, final score or kickoff time, status label (انتهت = finished), competition label (الدوري الإسباني, دوري أبطال أوروبا, كأس العالم للأندية), date; knockout ties show aggregate ("مجموع 3 - 1").
- **Filters**: competition dropdown "جميع المسابقات" (all competitions) + season selector (e.g., 2024/2025) — multi-season archive supported.
- **Match detail links**: rows link to a separate MATCH entity:

```
/كرة-القدم/مباراة/<teamA-slug>-v-<teamB-slug>/<match-id>
```

  - `مباراة` = "match" (singular). Match IDs are mixed-case alphanumeric (e.g., `h3bnelQyY-tXL8sXesTGp`), a different ID format from team/player IDs.
  - Example: `/كرة-القدم/مباراة/ريال-مدريد-v-برشلونة/h3bnelQyY-tXL8sXesTGp`
- Match detail page own tabs (observed): المُلخص (summary), الأحداث الرئيسية (key events), التشكيلة (lineups), تعليق (commentary), ترتيب (standings), مناقشة (discussion).

(Not fetched: جدول standings tab and أفضل-اللاعبين tab — assumed standard table/leaderboard layouts; flagged as gap.)

---

## 4. Player URL pattern + player page template (blocks + tab tree)

### URL pattern

```
/كرة-القدم/لاعب/<player-slug>/<id>              ← default tab = إحصائيات (Statistics)
/كرة-القدم/لاعب/<player-slug>/<tab-slug>/<id>   ← other tabs
```

- `لاعب` = "player". Same slug+ID scheme as teams; tab slug inserted before the ID.
- Homepage player links point at the **news tab** form: `/كرة-القدم/لاعب/كيليان-مبابي/أخبار/5e9ilgrz3tzg9kd1gk3yvrahh`.

Sample player URLs from homepage: كيليان مبابي (Mbappé) `…/5e9ilgrz3tzg9kd1gk3yvrahh`, فينيسيوس جونيور `…/b4witgokm49tidm1a83qcvbop`, لامين يامال `…/abr79wsl0folgkyvl821ggs2c`, محمد صلاح `…/5ilkkfbsss0bxd6ttdlqg0uz9`, كريستيانو رونالدو `…/h17s3qts1dz1zqjw19jazzkl`, سالم الدوسري `…/2ghv5nqgyn0wv9ekdx1ow0445` (~20 star players linked in homepage footer/nav).

### Player page template (verified: Mbappé)

**Identity/bio header** (full version on the stats/default tab):
- Large player photo with name overlay; current club crest + linked club name (→ `/كرة-القدم/فريق/ريال-مدريد/<id>`)
- Bio fields with Arabic labels: الجنسية (nationality), رقم القميص (shirt number), تاريخ الميلاد (birthdate), العمر (age), المركز (position, e.g., مهاجم = forward)
- Not shown: height, market value. National team is not linked from the bio header (only club is).
- On the news tab the header renders reduced (photo, name, club link only).

**Tab tree** (3 tabs):

| Arabic tab label | Tab URL slug | English | Path example (Mbappé) |
|---|---|---|---|
| إحصائيات | *(none — default)* | Statistics | `/كرة-القدم/لاعب/كيليان-مبابي/5e9ilgrz3tzg9kd1gk3yvrahh` |
| مسيرة الاحتراف | `مسيرة-اللاعب` | Career path / transfer history | `/كرة-القدم/لاعب/كيليان-مبابي/مسيرة-اللاعب/<id>` |
| أخبار | `أخبار` | News | `/كرة-القدم/لاعب/كيليان-مبابي/أخبار/<id>` |

No dedicated videos tab at player level (unlike teams).

**Statistics tab modules**
1. Bio header (above)
2. Filters: competition dropdown (جميع المسابقات / per-competition: league, cups, UCL, internationals) + season selector (2024/25 back to 2015/16)
3. Main stats table — **three columns: club / national team / total** — Arabic metric rows: عدد المشاركات (appearances), في التشكيلة الأساسية (starts), الدقائق التي لعبها (minutes), أهداف (goals), تمريرات حاسمة (assists), أهداف ركلات الجزاء (penalty goals), ركلات جزاء ضائعة (missed penalties), تسديدات على المرمى (shots on target), تسديدات خارج المرمى (shots off target), بطاقات صفراء (yellow cards), بطاقات حمراء (red cards), تسللات (offsides)

**News tab modules**
1. Heading "أخبار <player>" + subheading "آخر الأخبار والشائعات…" (latest news and rumors)
2. Chronological article feed: thumbnail, linked headline, related-topic tag, timestamp
3. Pagination via "أقدم" (older) link

**Sidebar**: none — single-column with interleaved ad slots, same shell as team pages.

---

## 5. Other entity page types discovered (coach/stadium/referee) — existence + patterns

| Entity | Exists as linked page type? | Evidence |
|---|---|---|
| Coach / manager (مدرب) | **No linked page found** | Squad list shows coach name + headshot with no hyperlink; match page has no coach links; no `/كرة-القدم/مدرب/…` URL seen anywhere |
| Referee (حكم) | **No linked page found** | Match detail page has no referee links |
| Stadium / venue (ملعب) | **No linked page found** | Stadium shown as plain text on match page ("سانتياجو بيرنابيو", unlinked) |
| Match (مباراة) | **Yes** — full entity page | `/كرة-القدم/مباراة/<teamA>-v-<teamB>/<matchId>` with 6 tabs (summary, key events, lineups, commentary, standings, discussion) |
| Competition (مسابقات/فرق hubs) | Indicated | Breadcrumbs on player page reference كرة-القدم → مسابقات (competitions) and فرق (teams) hub pages; not fetched (out of scope/budget) |

Conclusion: kooora.com's entity model exposes **team, player, match, and competition** pages; coaches, referees, and stadiums are display-only data, not navigable entities.

---

## 6. URL pattern summary table

| Entity / view | Pattern | Example |
|---|---|---|
| Team (default Info tab) | `/كرة-القدم/فريق/<slug>/<id>` | `/كرة-القدم/فريق/ريال-مدريد/3kq9cckrnlogidldtdie2fkbl` |
| Team news | `/كرة-القدم/فريق/<slug>/أخبار/<id>` | `/كرة-القدم/فريق/المغرب/أخبار/avggs3u2b5cu8i1dnzknhth52` |
| Team videos | `/كرة-القدم/فريق/<slug>/فيديوهات/<id>` | — |
| Team matches | `/كرة-القدم/فريق/<slug>/مباريات/<id>` | `/كرة-القدم/فريق/ريال-مدريد/مباريات/3kq9cckrnlogidldtdie2fkbl` |
| Team squad | `/كرة-القدم/فريق/<slug>/قائمة-اللاعبين/<id>` | `/كرة-القدم/فريق/ريال-مدريد/قائمة-اللاعبين/3kq9cckrnlogidldtdie2fkbl` |
| Team standings | `/كرة-القدم/فريق/<slug>/جدول/<id>` | — |
| Team top players (clubs only) | `/كرة-القدم/فريق/<slug>/أفضل-اللاعبين/<id>` | — |
| Player (default Stats tab) | `/كرة-القدم/لاعب/<slug>/<id>` | `/كرة-القدم/لاعب/كيليان-مبابي/5e9ilgrz3tzg9kd1gk3yvrahh` |
| Player news | `/كرة-القدم/لاعب/<slug>/أخبار/<id>` | `/كرة-القدم/لاعب/محمد-صلاح/أخبار/5ilkkfbsss0bxd6ttdlqg0uz9` |
| Player career/transfers | `/كرة-القدم/لاعب/<slug>/مسيرة-اللاعب/<id>` | `/كرة-القدم/لاعب/كيليان-مبابي/مسيرة-اللاعب/5e9ilgrz3tzg9kd1gk3yvrahh` |
| Match detail | `/كرة-القدم/مباراة/<teamA>-v-<teamB>/<matchId>` | `/كرة-القدم/مباراة/ريال-مدريد-v-برشلونة/h3bnelQyY-tXL8sXesTGp` |
| Coach / referee / stadium | *(no entity pages — plain text only)* | — |

**ID conventions**: team/player IDs are ~25-char lowercase alphanumeric (sportfeeds/Opta-style); match IDs are shorter, mixed-case, may contain hyphens. Slugs are Arabic, hyphen-separated, interchangeable with percent-encoded form; the trailing ID is the canonical key.

**Template invariants**: every entity page = identity header → horizontal tab bar → single-column module stack (no sidebar) → footer (social, app stores, legal). Data/images served from sportfeeds.io CDN.

---

### Fetch log (politeness audit — 8/12 budget)
1. `https://www.kooora.com/` (homepage link harvest)
2. Real Madrid team page (default tab)
3. Morocco national team page (default tab)
4. Real Madrid squad tab (قائمة-اللاعبين)
5. Real Madrid matches tab (مباريات)
6. Mbappé player news tab (أخبار)
7. Mbappé player stats tab (default)
8. Real Madrid v Barcelona match page (entity-link probe)

(8 successful page fetches + 1 tool-schema load; no retries needed — raw Arabic URLs resolved directly.)
