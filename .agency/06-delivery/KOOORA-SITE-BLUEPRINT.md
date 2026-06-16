# Kooora.com — Site Architecture Blueprint

| | |
|---|---|
| **Date** | 2026-06-12 |
| **Purpose** | A complete, structure-only information-architecture blueprint of https://www.kooora.com, compiled from six research reports. It is intended as reference input for designing the client's **own original** sports site — not for copying Kooora. |
| **Method** | robots.txt analysis + full sitemap-index crawl (1 index + all 13 child sitemaps, ≈3,532 URLs inventoried) + ~60 polite sequential page fetches covering every distinct page template (homepage, match center, broadcast schedule, competitions, teams, players, news, transfers, videos, shop, other-sport verticals, static pages), including raw-HTML pulls for navigation and deliberate 404 probes to verify routing rules. |
| **Ethics note** | This is a structure-only study. No article text, images, or proprietary design assets were copied at any stage. URLs, slugs, navigation labels, and layout-block descriptions are recorded generically for IA analysis. The client's site must be an original design informed by — not an imitation of — these patterns. |

---

## 1. Executive Summary

**What the site is.** Kooora.com is the largest Arabic-language sports website: a fully right-to-left (RTL), Arabic-only property (`<html lang="ar" dir="rtl">`) running as the Arabic edition (`"edition":"ar"`) of the **Footballco/GOAL publishing platform** — the same Next.js component system (`fco-*` class prefix) that powers GOAL.com. There is no language switcher and no hreflang alternates; internationalization is handled by sibling sites, not locales.

**Two backends, visible in the URLs.** The site is a thin presentation layer over two distinct systems, each with its own ID scheme:

| Backend | What it serves | ID format | Example |
|---|---|---|---|
| Headless CMS (Contentstack-style) | Articles, listicles, video articles, sport-section hubs, most author pages | `blt` + 16 lowercase hex chars | `blt9153ffc689ab3cad` |
| Sports-data feed (Opta/Stats-Perform-style; assets via sportfeeds.io CDN) | Matches, teams, players, competitions, standings, stats | Opaque ~21–25-char base-36 token (lowercase for teams/players/competitions; mixed-case, occasionally hyphenated, for matches) | `ak48fkypnql8y4n69cvcq5ghc` (team), `fE4Kl8TDt5GkVQ9xvEl14` (match) |

**Two football namespaces.** Football content is split across two near-identical path prefixes distinguished only by the Arabic definite article "ال":

- `/كرة-القدم/…` (*kurat al-qadam*, "the football") — **live data pages**: matches, teams, players, competitions, country hubs.
- `/كرة-قدم/…` (*kurat qadam*, "football") — **editorial pages**: news articles and listicles from the CMS.

**Headline IA insight.** Kooora is **one deep football data product plus news verticals for everything else**. Football gets the full entity model (match / team / player / competition / country, each with tabbed sub-pages, standings, and statistics). The six other sports (tennis, Formula 1, basketball, handball, volleyball, futsal) are **news-only CMS feeds** with no scores, standings, or fixtures. Transfers and Shop are likewise editorial feeds dressed as sections, not data products. Coaches, referees, and stadiums appear as plain text — they are not navigable entities.

Other defining traits: mandatory trailing entity IDs on all data and editorial detail URLs (slug-only URLs 404); tab sub-pages expressed as path segments **between** slug and ID on competition/team/player pages but as **client-side JS tabs** on match pages and the matches hub; path-segment pagination (`/أخبار/2`) with zero query parameters anywhere; and a compact single-bar footer.

---

## 2. Complete Hierarchical Site Tree

Legend: `[URL]` = real server route (verified) · `[JS]` = client-side tab/state on the parent URL (no route of its own; probed variants 404) · `[modal]` = client-rendered overlay, no URL · `‹id›` = sports-data entity ID · `‹blt›` = CMS `blt`+16-hex ID. Every Arabic slug is paired with its English translation.

```
www.kooora.com/
|
+-- /                                          Homepage [URL]
|
+-- كرة-القدم/  ("the football" - SPORTS-DATA NAMESPACE)
|   |
|   +-- مباريات-اليوم                          Today's matches / live hub [URL]
|   |   +-- date strip + month calendar        [JS] (no date URLs; ?date= ignored, path dates 404)
|   |   +-- filter: الكل (All)                 [JS]
|   |   +-- filter: مباشر (Live, count badge)  [JS]
|   |   +-- filter: المفضلة (Favorites)        [JS]
|   |
|   +-- مباراة/‹home›-v-‹away›/‹id›            Match detail [URL]  ("match"; joiner -v- or -ضد- "vs")
|   |   +-- نظرة عامة / المُلخص                [JS] Overview/Summary (default; label varies by match state)
|   |   +-- الأحداث الرئيسية                   [JS] Key events timeline
|   |   +-- التشكيلة                           [JS] Lineups (sitemap links use #التشكيلة fragment)
|   |   +-- تعليق                              [JS] Minute-by-minute commentary
|   |   +-- ترتيب                              [JS] Standings
|   |   +-- مناقشة                             [JS] Fan discussion
|   |
|   +-- مسابقة/‹slug›/‹id›                     Competition hub - معلومات Info (default) [URL]
|   |   +-- أخبار/‹id›                         [URL] News tab ("akhbar")
|   |   +-- مباريات/‹id›                       [URL] Matches tab - round selector ("mubarayat")
|   |   +-- جدول/‹id›                          [URL] Standings tab (label: ترتيب; slug: "jadwal" = table)
|   |   +-- أفضل-اللاعبين/‹id›                 [URL] Top players tab (stat leaderboards)
|   |   *League and tournament variants share this 5-tab template; see 3.6*
|   |
|   +-- فريق/‹slug›/‹id›                       Team page - معلومات Info (default) [URL]  ("fariq" = team)
|   |   +-- أخبار/‹id›                         [URL] News tab
|   |   +-- فيديوهات/‹id›                      [URL] Videos tab ("vidyuhat")
|   |   +-- مباريات/‹id›                       [URL] Matches tab (competition + season filters)
|   |   +-- قائمة-اللاعبين/‹id›                [URL] Squad list ("qa'imat al-la'ibin")
|   |   +-- جدول/‹id›                          [URL] Standings tab (label: ترتيب)
|   |   +-- أفضل-اللاعبين/‹id›                 [URL] Top players tab - CLUBS ONLY
|   |   *National-team variant: same pattern, 6 tabs (no Top players); see 3.8*
|   |
|   +-- لاعب/‹slug›/‹id›                       Player page - إحصائيات Stats (default) [URL]  ("la'ib" = player)
|   |   +-- مسيرة-اللاعب/‹id›                  [URL] Career path / transfer history ("masirat al-la'ib")
|   |   +-- أخبار/‹id›                         [URL] News tab (the form most internal links use)
|   |
|   +-- دولة/‹country-name›/‹iso3›             Country hub [URL]  ("dawla" = country; 20 in sitemap)
|                                              (multi-word names keep literal spaces, e.g. المملكة العربية السعودية/ksa)
|
+-- كرة-قدم/  ("football", no article - EDITORIAL/CMS NAMESPACE)
|   +-- أخبار/‹headline-slug›/‹blt›            Article page [URL] - shared by 4 editorial sub-types:
|   |                                          news, video articles ("فيديو:" headline prefix),
|   |                                          where-to-watch guides, featured long-form
|   +-- القوائم/‹slug›/‹blt›                   Listicle / slideshow [URL]  ("al-qawa'im" = lists)
|
+-- أخبار                                      News hub [URL]  ("akhbar" = news)
|   +-- أخبار/2, /3 ...                        [URL] pagination ("أقدم" Older link)
|
+-- فيديوهات                                   Videos hub [URL]  ("vidyuhat" = videos)
|   +-- فيديوهات/2 ...                         [URL] pagination
|
+-- الإنتقالات/‹id›                            Transfers hub [URL]  ("al-intiqalat" = transfers; NFD-encoded slug)
|   +-- الإنتقالات/2/‹id› ...                  [URL] pagination (page number BETWEEN slug and ID)
|
+-- أحداث-رياضية/كرة-القدم                     Broadcast/TV schedule - football [URL]
|   |                                          ("ahdath riyadiya" = sports events; nav label جدول البث)
|   +-- date tabs: اليوم Today + 6 days        [JS] (no date URLs)
|   +-- sport switcher                         -> links to other-sport CMS hubs (below)
|
+-- كل-البطولات                                All-competitions index [URL]  ("kul al-butulat" = all tournaments)
|   +-- filter bar: الكل All | شائع Popular | جميع الدول All countries | دوري League | رجال Men   [JS]
|
+-- التسوق-الرياضي/‹id›                        Shop feed [URL]  ("al-tasawwuq al-riyadi" = sports shopping)
|   +-- التسوق-الرياضي/2/‹id› ...              [URL] pagination
|
+-- OTHER-SPORT VERTICALS (news-only CMS hubs, pattern /‹sport-slug›/‹blt›)
|   +-- تنس/‹blt›                              Tennis [URL]   (+ articles /تنس/أخبار/‹slug›/‹blt›; page /تنس/2/‹blt›)
|   +-- الفورملا-1/‹blt›                       Formula 1 [URL]
|   +-- كرة-سلة/‹blt›                          Basketball [URL]
|   +-- كرة-يد/‹blt›                           Handball [URL]
|   +-- كرة-طائرة/‹blt›                        Volleyball [URL]
|   +-- قدم-صالات/‹blt›                        Futsal [URL]
|
+-- مؤلف/‹author-slug›/‹id›                    Author pages [URL]  ("mu'allif" = author;
|                                              ID = legacy numeric OR ‹blt›; 100 in sitemap)
|
+-- UTILITY / LEGAL
|   +-- سياسة-الخصوصية                         Privacy policy [URL]
|   +-- سياسة-الاستخدام                        Terms (slug literally "usage policy") [URL]
|   +-- اتصل-بنا                               Contact us [URL]
|   +-- cookie-consent settings                [modal] (footer button, no href)
|   +-- login / create account                 [modal] (header button; Google Identity Services;
|   |                                          favorites/follow-team, newsletters - no account URL found)
|   +-- search                                 [JS] client-side only; no SSR search box, no search URL found
|
+-- sitemap                                    XML sitemap index [URL]
    +-- sitemap/‹name›.xml                     13 children: main, matches, teams, competitions,
                                               countries, categories, authors, editorial-news,
                                               editorial-videos, editorial-slides,
                                               editorial-where-to-watch, editorial-featured, google-news
```

Notes on the tree:

- **Header "مباشر (Live)" and "مباريات اليوم (Today's matches)" resolve to the same URL** — there is no dedicated live route; "Live" is a filter state.
- **Match tabs are the only JS-tab entity tabs**; competition, team, and player tabs are all real, crawlable URLs with the tab segment inserted between slug and ID.
- **Players have no sitemap** — none of the 13 children indexes player pages; they are discoverable only through links (squads, leaderboards, nav, article chips).
- The header's third item, **كأس العالم 2026 (World Cup 2026)**, is an event-scoped editorial menu (the homepage also carries World Cup theming); researchers flag it as a time-bound state likely to rotate with the sporting calendar — long-term behavior not verified.

---

## 3. Page-Type Catalog

Consistent mini-format per template: **Purpose · URL pattern · Layout blocks (top→bottom) · Key entities · Links emitted · Behaviors**.

A shared shell wraps every template: global header (logo + 8-item nav + country dropdown + login button) → affiliate-disclaimer strip → main column → compact single-bar footer. Entity pages are **single-column, mobile-first, no sidebar**, with labelled ad slots (إعلان "Ad") interleaved between modules.

### 3.1 Homepage

- **Purpose:** Aggregation front door — live scores, featured match, curated and regional news, videos, transfers.
- **URL:** `/`
- **Layout blocks:** the 11-module sequence detailed in §5.3 (live ticker → date-grid schedule → featured-match hero → broadcast block → curated news carousel → World Cup feed → video rail → competition/region news tabs → transfers block → regional news sections → personalised follow-your-team rail).
- **Key entities:** matches, teams, competitions, players, articles, videos, broadcast channels.
- **Links emitted:** match detail, matches hub, team pages, competition pages, player pages (news-tab form), articles, news hub, transfers hub, external broadcaster sites, external YouTube (video rail).
- **Behaviors:** live scores and the personalised rail hydrate client-side; competition/region news tabs are JS toggles; heavy ad-slot interleaving.

### 3.2 Matches hub (live scores / today)

- **Purpose:** Single page for today's fixtures, live scores, and any other date.
- **URL:** `/كرة-القدم/مباريات-اليوم` (today's matches) — one URL for "Live" and "Today".
- **Layout blocks:** (1) date strip (7 days centered on today) + monthly calendar grid; (2) filter tabs الكل All / مباشر Live (with live-count badge) / المفضلة Favorites; (3) match list grouped under competition section headers (flag + competition name + country + round label, header links to competition page); (4) match rows.
- **Match-row anatomy:** home crest+name | score or kickoff time | away crest+name | status label (live minute `8'`, مباشر live, مؤجلة postponed, finished) | broadcaster block "شاهد مباشرة على" (watch live on) with channel logos and **external** watch links.
- **Key entities:** matches, teams, competitions, broadcasters.
- **Links emitted:** exactly five types per row — match detail, home team, away team, competition (group header), external broadcaster.
- **Behaviors:** date switching, live filter, and favorites are all client-side XHR on the same URL (`?date=` is ignored; path-date probe returned 404). Favorites tie into the login/follow system.

### 3.3 Match detail (+ 6 JS tabs)

- **Purpose:** Full match center for one fixture.
- **URL:** `/كرة-القدم/مباراة/‹home›-v-‹away›/‹matchId›` — joiner `-v-` or `-ضد-` ("vs"), both resolve to the same ID. Match IDs are ~21-char **mixed-case** alphanumeric, occasionally hyphenated (a different format from team/player IDs).
- **Layout blocks (header zone):** competition badge + round (links to competition) → date/time → home crest+name | score + live-minute badge (or kickoff time) | away crest+name → venue (plain text, unlinked) → broadcast channels with external watch links.
- **Tabs (all client-side JS, single URL):** Overview/Summary (default; label نظرة عامة on a live match, المُلخص on the post-match sample) · الأحداث الرئيسية Key events · التشكيلة Lineups · تعليق Commentary · ترتيب Standings · مناقشة Discussion.
- **Overview modules in order:** live broadcast widget → توقع الفائز 3-way prediction poll with live percentages → standings snippet (mini table, team rows linked) → match statistics (possession, xG, shots, corners, fouls) → detailed stats groups (attacking/defensive/disciplinary) → match-details footer block (competition, date, venue, broadcaster recap).
- **Key entities:** match, teams, players (lineups), competition, standings, broadcasters.
- **Links emitted:** both team pages, competition page, external broadcaster links. No coach/referee/stadium links.
- **Behaviors:** tab content (lineups, commentary…) is hydrated via XHR and absent from server HTML; probed tab-suffix URL returned 404; ~50% of sitemap match entries carry a `#التشكيلة` (lineup) fragment confirming client-side tab routing. Tab set may vary by match state (only live + post-match states sampled).

### 3.4 Broadcast schedule

- **Purpose:** TV/streaming guide — where and when to watch.
- **URL:** `/أحداث-رياضية/كرة-القدم` (sports-events / football); header nav label جدول البث (broadcast schedule).
- **Layout blocks:** title block → date tabs (اليوم Today + next 6 days; JS, no URLs) → competition-grouped listing → cards: competition logo+name | team crests+names | time or مباشر live badge | broadcaster logo+name | external watch link.
- **Key entities:** matches, competitions, broadcast channels.
- **Links emitted:** match detail, competition page, external broadcaster sites; a sport switcher linking to the other-sport CMS hubs (`/تنس/‹blt›` etc. — those siblings are news hubs, not broadcast grids).
- **Behaviors:** date tabs client-side; full-width cards, no sidebar.

### 3.5 Competitions index

- **Purpose:** Directory of every league and cup.
- **URL:** `/كل-البطولات` (all tournaments). Heading: "جميع الدوريات والمسابقات للرجال" (all leagues and men's competitions).
- **Layout blocks:** filter bar (الكل All | شائع Popular | جميع الدول All countries | دوري League type | رجال Men) → مسابقات عالمية international/continental block → country groups sorted alphabetically in Arabic, each expanding to that country's leagues **and** domestic cups together.
- **Key entities:** competitions, countries.
- **Links emitted:** competition hubs (`/كرة-القدم/مسابقة/‹slug›/‹id›`).
- **Behaviors:** one long filterable list; no pagination, no in-page search.

### 3.6 Competition hub (معلومات Info, default tab)

- **Purpose:** Landing page per competition.
- **URL:** `/كرة-القدم/مسابقة/‹slug›/‹id›` — clubs competitions and tournaments share one template.
- **Layout blocks:** identity block (badge + Arabic name; **no follow button, no season selector**) → 5-tab bar → news feed (cards: image+headline+timestamp) → matches strip (recent/upcoming) → mini standings widget (top 5, links to standings tab) → team-crests grid (all clubs, linked) → footer.
- **Key entities:** competition, articles, matches, teams, standings.
- **Links emitted:** tab URLs, articles, match details, team pages.
- **Behaviors:** tabs are real URLs (segment between slug and ID). **League vs tournament:** identical tab set; differences live inside tabs — World Cup standings tab renders 12 group tables (no qualification color legend, no bracket view); its matches tab adds knockout round entries (1/16, 1/8, ربع النهائي quarter-final, نصف النهائي semi-final, برونز bronze, النهائي final) and prominent per-fixture broadcast info; its hub adds a FAQ/SEO module. Pages are pinned to the current season ("أحدث موسم" latest season) — no historical-season access found.

#### 3.6.1 Standings tab (label ترتيب, slug جدول)

- **URL:** `/كرة-القدم/مسابقة/‹slug›/جدول/‹id›`
- **Blocks:** full table — columns (RTL order): مركز position | فريق team (crest + linked name) | played | wins | draws | losses | goals for | goals against | +/- | نقاط points | نتائج form strip → qualification color legend (CL/EL/Conference/relegation; leagues only) → المستوى filter: الكل All | الذهاب first half | الإياب second half of season.
- **Links emitted:** team pages. **Behaviors:** tournament variant = one table per group with a "الجولة القادمة" (next round) strip per group.

#### 3.6.2 Matches tab (المباريات, slug مباريات)

- **URL:** `/كرة-القدم/مسابقة/‹slug›/مباريات/‹id›`
- **Blocks:** round selector (الجولة 1→38 for a 20-team league; group rounds + knockout stages for tournaments) → date headers inside the round → match rows (crests, full+abbreviated names, score or kickoff time, status badge e.g. انتهت finished).
- **Links emitted:** match detail pages. **Behaviors:** one round per view, no pagination, no fixtures/results toggle.

#### 3.6.3 Top players tab (أفضل اللاعبين, slug أفضل-اللاعبين)

- **URL:** `/كرة-القدم/مسابقة/‹slug›/أفضل-اللاعبين/‹id›`
- **Blocks:** stacked ~top-10 leaderboards (not sub-tabs): top scorers, assists/contributions, red cards, yellow cards, shots on target, fouls committed, fouls suffered, tackles, offsides. Row = rank | team crest | linked player name | value.
- **Links emitted:** player pages. **Behaviors:** no season/stat filters observed.

*(News tab أخبار = full-length version of the hub's news card list; not fetched separately.)*

### 3.7 Club team page (معلومات Info, default tab)

- **Purpose:** Club landing page.
- **URL:** `/كرة-القدم/فريق/‹slug›/‹id›` — same pattern serves clubs and national teams.
- **Layout blocks:** identity header (crest + Arabic name; SEO title formula "‹team› النتائج والإحصائيات وأبرز اللقطات" — results, stats, highlights) → 7-tab bar → team news feed (~5 featured + more-news link) → recent/next matches module → league-table snippet (top 5, links to standings tab) → latest completed match card → footer.
- **Key entities:** team, articles, matches, standings.
- **Links emitted:** tab URLs, articles, match details, opponent teams; player links only via news items on the Info tab.
- **Behaviors:** tabs are real URLs; no transfers tab at team level.

#### 3.7.1 Squad tab (قائمة اللاعبين, slug قائمة-اللاعبين)

- **URL:** `/كرة-القدم/فريق/‹slug›/قائمة-اللاعبين/‹id›`
- **Blocks:** four position groups in order — حراس المرمى goalkeepers, المدافعون defenders, خط الوسط midfielders, المهاجمون forwards → player cards (headshot, jersey number, linked name, season stat figures) → coach at bottom (name + headshot, **not linked** — no coach entity).
- **Links emitted:** player pages.

#### 3.7.2 Team matches tab (المباريات, slug مباريات)

- **URL:** `/كرة-القدم/فريق/‹slug›/مباريات/‹id›`
- **Blocks:** competition dropdown (جميع المسابقات all competitions) + **season selector** (multi-season archive — notably present here but absent on competition pages) → month-header chronological list → rows: crests+names, score/kickoff, status, competition label, date; knockout ties show aggregate ("مجموع 3 - 1").
- **Links emitted:** match detail pages.

*(Team standings tab جدول and top-players tab أفضل-اللاعبين not fetched — assumed standard table/leaderboard layouts; flagged as a gap.)*

### 3.8 National team variant

- **Purpose / URL:** same `فريق` pattern and template as clubs, with deltas:
- **Differences:** 6 tabs (**no أفضل اللاعبين Top players tab**) · standings tab shows tournament/group standings instead of a league table · no standings snippet on the Info tab · matches mix المباريات الودية friendlies with tournament fixtures, with more prominent broadcast badges · identity header is crest/flag only (no confederation badge, no stadium info).

### 3.9 Player page

- **Purpose:** Player profile, stats, career, news.
- **URL:** `/كرة-القدم/لاعب/‹slug›/‹id›` (default = إحصائيات Statistics). Most internal links point at the news-tab form `/لاعب/‹slug›/أخبار/‹id›`.
- **Layout blocks (stats default):** bio header (large photo + name overlay; current club crest + **linked club name** — the only entity link in the bio; fields: الجنسية nationality, رقم القميص shirt number, تاريخ الميلاد birthdate, العمر age, المركز position; no height/market value; national team not linked) → filters (competition dropdown + season selector back to 2015/16) → stats table with **three columns: club / national team / total** and Arabic metric rows (appearances, starts, minutes, goals, assists, penalty goals, missed penalties, shots on/off target, yellow/red cards, offsides).
- **Tabs (3, real URLs):** إحصائيات Statistics (default, no segment) · مسيرة-اللاعب Career path / transfer history · أخبار News (reduced header; chronological feed with thumbnail, headline, topic tag, timestamp; "أقدم" Older pagination).
- **Links emitted:** club page, articles. **Behaviors:** no videos tab at player level (unlike teams).

### 3.10 News hub

- **Purpose:** Site-wide latest-news archive.
- **URL:** `/أخبار`; page N at `/أخبار/‹N›`. Title pattern: "أخبار كرة القدم صفحة 1" (Football News Page 1).
- **Layout blocks:** hero/featured card (large image + headline) → reverse-chronological card grid — card anatomy: thumbnail (~300×200), headline ≤2 lines, optional subtitle, timestamp (HH:MM + Arabic date), contextual entity label (team or match pairing); no author/comment/view counts on cards → "أقدم" (Older) link.
- **Key entities:** articles (+ their tagged entities).
- **Links emitted:** article pages.
- **Behaviors:** **no own category tabs or filters** — scoped news lives on entity pages instead; server-side path pagination, no infinite scroll; no sidebar.

### 3.11 News article

- **Purpose:** Single editorial story (also the base for video/where-to-watch/featured sub-types — distinguished by sitemap membership and slug keywords, not URL segment).
- **URL:** `/كرة-قدم/أخبار/‹headline-slug›/‹blt›` (listicles: `/كرة-قدم/القوائم/‹slug›/‹blt›`). No date in URLs. `--` double hyphen marks stripped punctuation.
- **Layout blocks:** shared header (no breadcrumb) → **topic chips** — linked entity tags (team, competition, country, player, sport section) → H1 headline → byline (author name **linked to author page**) + publish date/time → hero media slot (one licensed agency photo with credit) → plain-paragraph body (no subheads/inline embeds in sample) → "اقرأ أيضًا" (Read also) related block (~4 cards) → footer.
- **Key entities:** article, author, tagged entities.
- **Links emitted:** entity hubs via chips, author page, related articles.
- **Behaviors:** no comments, no share buttons, no sidebar; single-column with ad slots.

### 3.12 Videos hub

- **Purpose:** Internal video-article archive.
- **URL:** `/فيديوهات`; page N at `/فيديوهات/‹N›`.
- **Layout blocks:** featured grid (~6 cards) → further card grids → "أقدم" pagination. Card: thumbnail (JPG+WebP), "فيديو:" -prefixed headline, contextual subtitle, match/team label, date, short time value. No view counts or channel attribution.
- **Links emitted:** internal video-article pages (not YouTube). **Behaviors:** no category tabs; single mixed feed. (The homepage "YallaGoal" rail is separate — it deep-links to YouTube.)

### 3.13 Video detail

- **Purpose:** Video story. **It is the standard article template** (same `/كرة-قدم/أخبار/‹slug›/‹blt›` pattern) with deltas:
- **Deltas:** custom/self-hosted **video player replaces the hero image** (not a bare YouTube iframe) · "فيديو:" headline prefix retained · short contextual paragraphs after the player · related block headed "قد يعجبك أيضاً" (You may also like) mixing articles and match coverage · topic chips may include a **match-page chip** linking to the match entity.

### 3.14 Transfers feed

- **Purpose:** Transfer-news vertical.
- **URL:** `/الإنتقالات/‹id25›` (al-intiqalat = transfers); page N at `/الإنتقالات/‹N›/‹id25›` — page number **between slug and ID**. Slug is served NFD-encoded (see §4 gotchas). Modeled as a category/section entity (competition-style ID), not a special app.
- **Layout blocks:** hub title → chronological feed of 20+ transfer-article cards (image, destination-club tag, headline, subtitle, timestamp — the standard news card).
- **Behaviors:** **pure news feed, no data product** — no confirmed-deals table, no window (summer/winter) organization, no league/club/rumor-status filters. Items link to ordinary article URLs.

### 3.15 Shop feed

- **Purpose:** Commerce-editorial section ("تسوق مع كووورة" Shop with Kooora) — **not a storefront**.
- **URL:** `/التسوق-الرياضي/‹id25›` (sports shopping); page N at `/التسوق-الرياضي/‹N›/‹id25›`.
- **Layout blocks:** standard article-card feed of buying guides (ticket guides at sample time), newest first.
- **Behaviors:** no product grid, prices, cart, or visible affiliate markers in the listing; no sub-categories or filters. Items link to normal article URLs.

### 3.16 Other-sport vertical hub

- **Purpose:** News-only section per secondary sport (tennis, Formula 1, basketball, handball, volleyball, futsal).
- **URL:** `/‹sport-slug›/‹blt›` (e.g. `/تنس/blta843d4a7d2cc7ebc`); page N at `/‹sport-slug›/‹N›/‹blt›`. Articles namespaced under the sport: `/‹sport-slug›/أخبار/‹slug›/‹blt›`.
- **Layout blocks:** shared header → hero background → featured card + secondary card + standard card grid → "أقدم" pagination → footer. No sidebar.
- **Behaviors:** verified on tennis: **no scores, standings, or tournament sub-navigation** — none of the football infrastructure. Same template engine and URL shape strongly implies the other five verticals are identical (only tennis depth-probed).

### 3.17 Country hub

- **Purpose:** Per-country football hub (20 countries in the sitemap; also reached via article topic chips).
- **URL:** `/كرة-القدم/دولة/‹country-name›/‹iso3›` — ID is a human-readable lowercase 3-letter football code (egy, ksa, mar, eng…), the only non-opaque ID class on the site. Multi-word country names keep **literal spaces** (`%20`), uniquely among all entities.
- **Layout / entities / links:** **internals not verified** — no researcher fetched a country hub page; existence and URL pattern are sitemap- and chip-verified only.

### 3.18 Author & category archives

- **Author page** — `/مؤلف/‹slug›/‹id›`; ID is legacy numeric (11, 140, 270…) **or** CMS `blt` ID; slugs may be Arabic or Latin; 100 authors in the sitemap. Linked from article bylines. **Page internals not verified** (not fetched).
- **Category hubs** — the categories sitemap lists exactly 7 section hubs: the six other-sport verticals (3.16) + sports shopping (3.15); those templates are documented above.

### 3.19 Legal / static pages

- **Purpose:** Utility content. **URLs:** `/سياسة-الخصوصية` privacy policy · `/سياسة-الاستخدام` terms (slug literally "usage policy") · `/اتصل-بنا` contact us. Arabic slugs, **no trailing IDs** — the only ID-less detail pages on the site. Listed in `main.xml` alongside home and `/كل-البطولات`. (No about-us, careers, advertise, or editorial-policy pages exposed in the shared footer.)

---

## 4. URL Pattern Map

All paths relative to `https://www.kooora.com`. Arabic shown percent-decoded. `‹id25›` = ~21–25-char lowercase base-36 sports-data ID · `‹idM›` = ~21-char mixed-case match ID (may contain hyphens) · `‹blt›` = `blt`+16 lowercase hex CMS UID.

| Pattern template | Entity / view | ID system | Example (decoded) | Notes |
|---|---|---|---|---|
| `/` | Homepage | — | `/` | |
| `/كرة-القدم/مباريات-اليوم` | Matches hub (today + live + all dates) | — | — | Single URL; dates/filters are client state; `?date=` ignored; path dates 404 |
| `/كرة-القدم/مباراة/‹home›-v-‹away›/‹idM›` | Match detail (all 6 tabs) | mixed-case match ID | `/كرة-القدم/مباراة/كوريا-الجنوبية-v-تشيكيا/fE4Kl8TDt5GkVQ9xvEl14` | Joiner `-v-` or `-ضد-`, same ID; tabs JS-only; `#التشكيلة` fragment deep-links lineups |
| `/كرة-القدم/مسابقة/‹slug›/‹id25›` | Competition hub (Info tab) | base-36 | `/كرة-القدم/مسابقة/كأس-العالم/70excpe1synn9kadnbppahdn7` | |
| `/كرة-القدم/مسابقة/‹slug›/‹tab›/‹id25›` | Competition tab | base-36 | `/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/جدول/2kwbbcootiqqgmrzs6o5inle5` | `‹tab›` ∈ أخبار news · مباريات matches · جدول standings · أفضل-اللاعبين top players |
| `/كرة-القدم/فريق/‹slug›/‹id25›` | Team page (Info tab) | base-36 | `/كرة-القدم/فريق/ريال-مدريد/3kq9cckrnlogidldtdie2fkbl` | Clubs + national teams, one namespace |
| `/كرة-القدم/فريق/‹slug›/‹tab›/‹id25›` | Team tab | base-36 | `/كرة-القدم/فريق/ريال-مدريد/قائمة-اللاعبين/3kq9cckrnlogidldtdie2fkbl` | `‹tab›` ∈ أخبار · فيديوهات videos · مباريات · قائمة-اللاعبين squad · جدول · أفضل-اللاعبين (clubs only) |
| `/كرة-القدم/لاعب/‹slug›/‹id25›` | Player page (Stats tab) | base-36 | `/كرة-القدم/لاعب/كيليان-مبابي/5e9ilgrz3tzg9kd1gk3yvrahh` | |
| `/كرة-القدم/لاعب/‹slug›/‹tab›/‹id25›` | Player tab | base-36 | `/كرة-القدم/لاعب/محمد-صلاح/أخبار/5ilkkfbsss0bxd6ttdlqg0uz9` | `‹tab›` ∈ أخبار news · مسيرة-اللاعب career; nav links use the news form |
| `/كرة-القدم/دولة/‹name›/‹iso3›` | Country hub | 3-letter code | `/كرة-القدم/دولة/مصر/egy` | Multi-word names keep literal spaces: `المملكة العربية السعودية/ksa` |
| `/كرة-قدم/أخبار/‹slug›/‹blt›` | Editorial article (news / video / where-to-watch / featured) | blt | `/كرة-قدم/أخبار/…/blt9153ffc689ab3cad` | 4 sub-types share one path; distinguished by sitemap + slug keywords |
| `/كرة-قدم/القوائم/‹slug›/‹blt›` | Listicle / slideshow | blt | `/كرة-قدم/القوائم/…/bltee7e56aa6da72486` | Only editorial sub-type with its own segment |
| `/أخبار` → `/أخبار/‹N›` | News hub + pages | — | `/أخبار/2` | "أقدم" (Older) link; H1 shows "صفحة N" |
| `/فيديوهات` → `/فيديوهات/‹N›` | Videos hub + pages | — | `/فيديوهات/2` | Internal video articles, not YouTube |
| `/الإنتقالات/‹id25›` → `/الإنتقالات/‹N›/‹id25›` | Transfers hub + pages | base-36 | `/الإنتقالات/k94w8e1yy9ch14mllpf4srnks` | Page number between slug and ID; NFD encoding mandatory |
| `/التسوق-الرياضي/‹id25›` → `/التسوق-الرياضي/‹N›/‹id25›` | Shop feed + pages | base-36 | `/التسوق-الرياضي/jay1lq18ea7j1bks90cjvzor9` | |
| `/أحداث-رياضية/كرة-القدم` | Broadcast schedule (football) | — | — | Date tabs JS-only |
| `/كل-البطولات` | Competitions index | — | — | In `main.xml` |
| `/‹sport-slug›/‹blt›` → `/‹sport-slug›/‹N›/‹blt›` | Other-sport hub + pages | blt | `/تنس/blta843d4a7d2cc7ebc` | 6 sports; CMS section entities |
| `/‹sport-slug›/أخبار/‹slug›/‹blt›` | Other-sport article | blt | `/تنس/أخبار/…/blt60ca310c191e8b12` | |
| `/مؤلف/‹slug›/‹id›` | Author page | legacy numeric **or** blt | `/مؤلف/عبدالاعلى-سكير/11` | Slugs Arabic or Latin; mixed ID scheme = platform migration artifact |
| `/‹static-slug›` | Legal/static | none | `/سياسة-الخصوصية` | Only ID-less detail pages |
| `/sitemap`, `/sitemap/‹name›.xml` | Sitemap system | — | `/sitemap/matches.xml` | XML index + 13 children, ≈3,532 URLs |

Generalized grammar for data/editorial entities:

```
/{namespace}/{entity-type}/{arabic-slug}/[{tab-or-page-segment}/]{entity-id}
```

### 4.1 Implementation gotchas (verified by probing)

1. **Trailing entity IDs are mandatory.** The slug is decoration; the ID is the canonical key. ID-less URLs 404 (probed: `/كرة-القدم/مسابقة/كاس-العالم/أخبار` → 404; `/انتقالات` bare → 404).
2. **Unicode normalization is inconsistent and load-bearing.** Canonical data-page and section URLs encode hamza in **NFD-decomposed** form (alef U+0627 + combining hamza U+0654/0655), e.g. الإنتقالات served as `%D8%A7%D9%84%D8%A7%D9%95%D9%86%D8%AA%D9%82%D8%A7%D9%84%D8%A7%D8%AA`; requests with **NFC-composed** hamza (`%D8%A5`…) returned **404**. Editorial segments meanwhile use precomposed forms. Crawlers must preserve encodings verbatim; dedupers must normalize before comparing.
3. **Literal `%20` spaces in country slugs** — country hubs are the only entity keeping spaces instead of hyphens (`المملكة العربية السعودية/ksa`).
4. **Fragments inside sitemap entries** — ~50% of `matches.xml` URLs end in `#التشكيلة` (lineups), which is non-standard in sitemaps and signals client-side tab routing.
5. **Zero query parameters site-wide** — no `?page=`/`?date=` anywhere in ~3.5k sitemap URLs or navigation; pagination is always a **path segment** (`/أخبار/2`), inserted **before the trailing ID** when the hub has one (`/الإنتقالات/2/‹id›`).
6. **Tab segments sit between slug and ID** (`/مسابقة/‹slug›/جدول/‹id›`) — an unusual ordering any router/crawler must accommodate.
7. **Tab label ≠ tab slug** — standings tabs are labelled ترتيب (ranking) but routed as جدول (table) on both competitions and teams.
8. **Two joiners for the same match** — `-v-` and `-ضد-` slugs both resolve to the identical match ID.
9. **The two football namespaces differ by one character cluster** (definite article ال): `كرة-القدم` = data platform, `كرة-قدم` = CMS. Easy to conflate; routing-critical.
10. **Raw Arabic and percent-encoded URL forms both resolve** identically (subject to gotcha 2).

---

## 5. Navigation Map

### 5.1 Header

Structure: logo (→ `/`) + primary nav bar of **8 items** (most with mega-menu dropdowns; 103 anchors total in the header) + country-dropdown button + login button. No SSR search box.

```
كووورة (Kooora logo) ............................... /
1. مباشر (Live) .................................... /كرة-القدم/مباريات-اليوم
   +- مباريات اليوم (Today's matches) .............. /كرة-القدم/مباريات-اليوم
   +- جدول البث (Broadcast schedule) ............... /أحداث-رياضية/كرة-القدم
2. أخبار (News) — direct link, no dropdown ......... /أخبار
3. كأس العالم 2026 (World Cup 2026) — mega-menu, 3 groups
   +- معلومات (Information)
   |  +- أخبار (News) .............................. /كرة-القدم/مسابقة/كأس-العالم/أخبار/70excpe1synn9kadnbppahdn7
   |  +- جدول المباريات (Match schedule) ........... /كرة-القدم/مسابقة/كأس-العالم/مباريات/70excpe1synn9kadnbppahdn7
   |  +- جدول الترتيب (Standings) .................. /كرة-القدم/مسابقة/كأس-العالم/جدول/70excpe1synn9kadnbppahdn7
   +- أبرز المنتخبات العالمية (Top international teams) - 6 links
   |  Argentina, Brazil, France, Portugal, Spain, Germany ... /كرة-القدم/فريق/‹slug›/‹id›
   +- المنتخبات العربية (Arab national teams) - 8 links
      Saudi Arabia, Morocco, Egypt, Qatar, Iraq, Jordan, Algeria, Tunisia
4. مسابقات (Competitions) — mega-menu, 3 groups; /كرة-القدم/مسابقة/‹slug›/‹id›
   +- مسابقات عالمية (International) - كل المسابقات (All) -> /كل-البطولات,
   |  World Cup 2026, UEFA CL, AFC CL Elite, CAF CL, UEFA Europa League
   +- الدول الأوروبية (European) - La Liga, Premier League, Serie A, Bundesliga, Ligue 1
   +- الدول العربية (Arab) - Saudi Pro League, Egyptian PL, Botola Pro (Morocco),
      Algerian Ligue 1, Jordanian Pro League, Qatar Stars League, UAE Pro League, Tunisian Ligue 1
5. فرق (Teams) — mega-menu, 5 country columns x 5 clubs; /كرة-القدم/فريق/‹slug›/‹id›
   +- إسبانيا Spain: Athletic Bilbao, Atletico Madrid, Barcelona, Real Madrid, Valencia
   +- إنجلترا England: Arsenal, Chelsea, Liverpool, Man City, Man United
   +- إيطاليا Italy: Inter, Roma, AC Milan, Napoli, Juventus
   +- ألمانيا Germany: Leverkusen, Bayern, Dortmund, Schalke 04, Wolfsburg
   +- فرنسا France: PSG, Lille, Lyon, Marseille, Monaco
6. رياضات (Sports) — dropdown of 6 non-football verticals; /‹sport-slug›/‹blt›
   +- التنس Tennis, الفورملا1 F1, كرة السلة Basketball, كرة اليد Handball,
      الكرة الطائرة Volleyball, قدم الصالات Futsal
7. اللاعبون (Players) — mega-menu, 6 league columns x 5 players; /كرة-القدم/لاعب/‹slug›/أخبار/‹id›
   +- Spain: Mbappe, Vinicius Jr, Lamine Yamal, Raphinha, Bellingham
   +- England: Salah, Haaland, Wirtz, Saka, Palmer
   +- Saudi Arabia: Ronaldo, Benzema, Mahrez, Al-Dawsari, Nunez
   +- Germany: Kane, Musiala, Guirassy, Olise, Davies
   +- France: Hakimi, Dembele, Vitinha, Kvaratskhelia, Doue
   +- Italy: Lautaro, Dybala, Leao, McTominay, Modric
8. تسوق مع كووورة (Shop with Kooora) ............... /التسوق-الرياضي/jay1lq18ea7j1bks90cjvzor9

Utility: country dropdown (client-rendered; sets country for broadcast localization - NOT language)
         login button (client-side auth modal; Google sign-in; favorites/follow teams, newsletters)
```

A few player links deviate from the standard pattern (no `/أخبار` segment, or malformed extra segment) — header data-quality artifacts worth avoiding in a rebuild.

### 5.2 Footer

A **single compact bar** (not multi-column link lists):

| Zone | Contents |
|---|---|
| Logo + copyright | Kooora logo → `/`; text "كل الحقوق محفوظة كووورة© 2026" (All rights reserved) |
| App links (2) | Google Play (`com.sikooora`) · Apple App Store (id `859950269`) |
| Social (5, icon-only) | YouTube · Facebook · Instagram · X · TikTok |
| Legal nav (4) | اتصل بنا Contact (`/اتصل-بنا`) · ملفات الارتباط Cookies (consent **button**, no href) · سياسة الخصوصية Privacy (`/سياسة-الخصوصية`) · الشروط والاحكام Terms (`/سياسة-الاستخدام`) |

Not present: language/region switcher, sitemap link, about-us, advertising page. An RSS feed is declared in `<head>` (`feeds.footballco.com/kooora/feed/…`) but is not surfaced in the footer.

### 5.3 Homepage module sequence (11 ordered modules)

An affiliate-links disclaimer strip sits directly under the header; labelled ad slots (إعلان) appear between modules throughout.

| # | Module | Description | Entities |
|---|---|---|---|
| 1 | Live scoreboard / ticker | Horizontal strip "المباريات" with live scores, minute, favorites/settings button; cards link to match detail/preview | matches, teams, competitions |
| 2 | Match schedule grid | Date scroller (~1-month window) + competition filters | matches, competitions |
| 3 | Featured match hero | One spotlighted match: crests, lineups/formations, possession + momentum visuals, player ratings, standings snippet, "اختر الفائز" pick-the-winner widget | match, teams, players, standings |
| 4 | Broadcast/streaming block | Channel logos with outbound viewing links; ties to جدول البث | matches, broadcasters |
| 5 | Hero news carousel | Editorially curated headline slider (prev/next + indicators) | articles |
| 6 | World Cup 2026 news feed | Competition-scoped chronological list (event theming active at capture) | articles, competition |
| 7 | Video rail ("YallaGoal") | Branded video series cards — deep-links **directly to YouTube** (unlike `/فيديوهات`) | videos |
| 8 | Competition/region news tabs | JS-tabbed article lists (World Cup, Premier League, La Liga, Saudi League, Morocco, Egypt) | articles, competitions |
| 9 | Transfers block | Transfer-market article cluster → transfers hub | articles, players, teams |
| 10 | Regional news sections | Country blocks (Saudi Arabia, England, Spain, Morocco, Egypt) + "المزيد من الأخبار" more-news → `/أخبار` | articles |
| 11 | Personalised rail | "اختر فريقك المفضل" (pick your favorite team) follow-onboarding; client-hydrated | teams, user prefs |

### 5.4 Internal-linking graph

```
                          HOMEPAGE
       (links to every hub + entities via 11 modules)
            |        |        |        |        |
            v        v        v        v        v
        Matches    News    Compet.   Teams   Players
          hub      hub      hubs     pages    pages
           |        |         ^ ^      ^ ^      ^
           |        v         | |      | |      |
           |     ARTICLE -----+ |      | |      |
           |     topic chips ---+------+ |      |
           |      |     \________________|______+--> Country hub
           |      v                      |      |
           |    Author page              |      |
           v                             |      |
        MATCH detail <---- Competition --+      |
         | | | |            matches tab  |      |
         | | | +--> external broadcaster |      |
         | | +----> competition page     |      |
         | +------> home/away team ------+      |
         |              |                       |
         |              +--> squad tab ---------+--> Player page
         |              +--> standings rows --> other teams
         |                                      |
         +--< video-article match chip          +--> current club (bio header)
   Competition top-players rows ----------------+
```

Bullet rules (each verified on raw pages):

- **Match row emits exactly five link types:** match detail, home team, away team, competition (via group header), external broadcaster.
- **Article → entities:** topic chips link to team, competition, country, player, and sport hubs; the byline links to the author page; related-articles blocks link to other articles; video articles may additionally chip-link to a **match page**.
- **Competition → teams/players/matches:** crest grid and standings rows → team pages; top-players rows → player pages; matches tab rows → match details; news tab → articles.
- **Team → players/matches:** squad rows → player pages (coach NOT linked); matches tab → match details; standings tab → other teams; news/videos tabs → articles.
- **Player → club:** the bio header links only the current club (national team unlinked); news tab → articles.
- **Dead ends (display-only, no pages):** coach, referee, stadium/venue, broadcaster (external links only).
- **Hub cross-links:** matches hub and broadcast schedule cross-link the same match/competition entities; transfers/shop/other-sport feeds link only to articles.

---

## 6. Language, RTL & Content Organization

- **Document setup:** `<html lang="ar" dir="rtl" class="fco-kooora">` — Arabic-only, fully right-to-left. All slugs, labels, and content are Arabic (rare Latin author slugs aside). Standings tables read right-to-left (position column rightmost).
- **No internationalization surface:** `<head>` carries only a canonical link and one RSS alternate — **no hreflang, no English alternate** anywhere. Embedded data sets `"edition":"ar"`: Kooora is one edition of the Footballco platform; other languages are sibling sites (e.g., GOAL), not locales of this one.
- **Country ≠ language:** the header country dropdown (options client-rendered) localizes **broadcast/TV-channel data** per the user's country; it is not a language switcher. Per-article `geoTargeting` arrays (empty in samples) imply optional geo-scoped content.
- **Arabic slug conventions:** hyphen-joined words derived from names/headlines; `--` double hyphen where punctuation was stripped; ضد ("vs") or `-v-` as match joiners; slugs are decorative — the trailing ID is canonical; raw and percent-encoded forms both resolve; hamza encoding is mixed NFD/NFC across namespaces (gotcha 4.1.2); one CMS leak observed (`copy-of-…` slug).
- **Categories taxonomy (7):** the categories sitemap defines exactly seven section hubs — tennis, Formula 1, basketball, handball, volleyball, futsal, and sports shopping. Football is *not* a category: it is the site's substrate, expressed through the two football namespaces instead.
- **Editorial sub-types are sitemap-level, not URL-level:** news, video articles, where-to-watch guides, and featured long-form all share `/كرة-قدم/أخبار/…`; only listicles get their own segment (`/القوائم/`). Five editorial sitemaps + a 48-hour Google News sitemap (publication "Kooora.com", language `ar`) carry full ISO-8601 lastmod; data sitemaps carry none.
- **Regional news organization:** geography is a first-class editorial axis — homepage regional blocks (Saudi Arabia, England, Spain, Morocco, Egypt), Arab-league and Arab-team groupings in the header mega-menus, and 20 country hubs (`/كرة-القدم/دولة/…`) fed by article country chips. The news hub itself is deliberately unfiltered; scoping happens on entity and country pages.
- **Editorial author system:** 100 authors in the sitemap with mixed ID schemes — legacy numeric IDs (migrated from the old kooora platform) alongside CMS `blt` IDs; author slugs in Arabic or Latin; bylines on every article link to author pages.
- **Sitemap philosophy:** a freshness feed, not an archive — top-20 competitions, 20 countries, ~1k recent/upcoming matches, 1,086 teams, rolling editorial window; players entirely absent.

---

## 7. Blueprint Notes for Building an Original Site

Original-design guidance distilled from the study — adopt the *lessons*, not the surface.

### 7.1 Recommended entity model

Make six things first-class entities, each with its own template, stable ID, and hub: **competition, team, player, match, article, video**. Kooora proves this set carries the entire experience. Two extensions Kooora lacks that cost it little to add and would differentiate the client's site:

- **Coach, stadium, referee as linkable entities** (Kooora renders them as dead text).
- **Transfers as a structured data product** (player / from / to / fee / status / window), not just an article category — Kooora's transfers hub is a plain news feed.

Model "country" as a lightweight taxonomy hub and "author" as a person entity from day one (Kooora's mixed numeric/CMS author IDs are a visible migration scar).

### 7.2 Routing scheme lessons

- **Readable slug + stable ID works** — keep it, but put the variable parts in saner places: prefer `/{entity}/{slug}-{id}` or `/{entity}/{id}/{slug}` over Kooora's tab-and-page-segments-between-slug-and-ID ordering, which complicates routers and crawlers.
- **Make every tab a real URL.** Kooora does this for competition/team/player tabs (good — crawlable, deep-linkable) but uses JS tabs + sitemap fragments for match lineups/commentary/stats (bad — invisible to SEO and unlinkable). Give match sub-views routes.
- **Give dates URLs.** Kooora's matches hub has no date parameter at all (`?date=` ignored, path dates 404) — a real shareability/SEO gap. Implement `/matches/2026-06-12`-style routes.
- **Normalize Unicode at the router.** Accept NFC and NFD, redirect to one canonical form. Kooora's decomposed-hamza-only URLs (NFC → 404) are a defect class to design out. Likewise: no literal spaces in slugs, one slug joiner (not two "vs" variants), align tab labels with tab slugs.
- **Keep path-segment pagination** (`/news/2`) — clean and crawlable — and keep query strings out of canonical URLs.
- **Avoid namespace pairs distinguishable only by a definite article.** If data and editorial systems need separate namespaces, make them visibly different (e.g., `/football/` vs `/news/`).

### 7.3 Section priorities

Kooora's effort allocation is a proven market read for an Arabic sports audience — order of investment:

1. **Football match center** (live hub + match detail) — the core product; broadcast/"where to watch" info is a first-class feature on every match surface, reflecting real MENA user intent.
2. **Competition hubs** with standings/fixtures/top-player stats.
3. **Team and player profiles** with stats and scoped news.
4. **News engine** (hub + entity-scoped feeds + regional blocks + transfers + videos).
5. **Secondary sports as light news verticals** — ship feeds first; add data products per sport only when justified.

### 7.4 Weaknesses to improve on (verified gaps in Kooora)

| Kooora gap | Opportunity for the original site |
|---|---|
| No season selector on competition pages (pinned to current season; team matches tab has one, inconsistently) | Season-aware competition URLs and archives everywhere |
| No bracket/knockout visualization for tournaments (World Cup = 12 flat group tables) | Proper bracket view + group/knockout toggle |
| No team-level transfers tab; transfers hub is articles-only | Structured transfer center + per-team transfer tabs |
| Match tabs JS-only; lineups deep-linked via sitemap `#fragments` | Routed match sub-pages |
| Secondary sports are news-only | At minimum, scores/schedule modules per sport vertical |
| No header search (client-side afterthought) | Prominent universal entity search |
| Coach/referee/stadium unlinked | Entity pages or at least structured data |
| News hub has no filters; no date in article URLs | Faceted news archive; consider dated URLs for clarity |
| Footer minimal — no about/editorial-policy pages | Trust pages (about, editorial standards, advertising) |
| Inconsistent Unicode encoding; duplicate match-slug joiners | Single normalized canonical form, enforced |

---

## Appendix: Research provenance

| # | Report file | Scope (one line) | Fetches |
|---|---|---|---|
| 1 | `01-sitemap-url-inventory.md` | robots.txt + sitemap index + all 13 child sitemaps; full URL-pattern grammar; ≈3,532 URLs inventoried | 14 |
| 2 | `02-navigation-homepage.md` | Header (8 items, mega-menus, 103 anchors) + footer via raw HTML; 11 homepage modules; news + competitions hub sub-navigation; RTL/i18n/account findings | 6 |
| 3 | `03-matches-live-match-detail.md` | Matches hub (date nav, filters, row anatomy), live match detail + 6 JS tabs (404-probed), broadcast schedule | 8 |
| 4 | `04-competitions.md` | Competitions index + Premier League and World Cup hubs with standings/matches/top-players tabs; league-vs-tournament comparison; season handling | 10 |
| 5 | `05-teams-players.md` | Club (Real Madrid) + national team (Morocco) templates and tabs, squad/matches sub-templates, player (Mbappé) template, coach/referee/stadium entity probe | 8 |
| 6 | `06-news-transfers-videos-other.md` | News hub, article + video templates, transfers/shop/videos hubs, 6 other-sport verticals (tennis depth-probed), static pages, NFD-encoding gotcha | 14 |

**Total: ~60 fetches** (sequential, polite, structure-only), including deliberate 404 probes that established the routing rules in §4.1.

**Residual gaps flagged by researchers (carried forward, not papered over):**

- **JS-hydrated match-tab internals** — lineup formation graphic, per-player links inside lineups, commentary internals: absent from server HTML, not inspected (no browser/network tooling used).
- **Match tab set by state** — only live and post-match samples; pre-match tab set (preview/H2H?) and the Overview-vs-Summary default-label variation not fully mapped.
- **XHR/API endpoints** feeding date switches and match tabs — out of scope for polite static fetching.
- **Logged-in state** — account area, favorites management, and personalised rail rendered only behind the client-side auth modal; not exercised.
- **Rotating editorial surfaces** — the World Cup 2026 header menu and homepage theming are event-scoped snapshots; their off-season behavior is unverified.
- **Not fetched:** country hub internals, author page internals, team standings + team top-players tabs, competition news tab (assumed standard variants); five of six other-sport verticals (template inferred from tennis); historical-season access (none found, may not exist).
