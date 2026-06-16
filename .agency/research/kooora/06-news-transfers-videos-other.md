# News, Videos & Other Sections Map — kooora.com

Research task R6. Structure-only mapping (no article text, images, or design assets copied).
Fetched 2026-06-12. Site is RTL Arabic, built on the GOAL.com publishing platform
(Contentstack CMS — `blt`-prefixed entry IDs identical to goal.com's URL scheme).

**Encoding caveat (important for implementation):** canonical kooora URLs use *decomposed*
Arabic hamza (NFD: alef U+0627 + combining hamza U+0654/U+0655) in slugs, e.g.
`الإنتقالات` is served as `%D8%A7%D9%84%D8%A7%D9%95%D9%86%D8%AA%D9%82%D8%A7%D9%84%D8%A7%D8%AA`.
Requests with NFC-composed hamza (`%D8%A5` etc.) returned **404** in testing. Any crawler/
router must preserve the site's percent-encoding verbatim, not re-normalize it.

---

## 1. News hub structure (categories, feed organization, pagination)

**URL:** `/أخبار` ("news") — page `<title>`: "أخبار كرة القدم صفحة 1 | كووورة" (Football News Page 1 | Kooora).

**Category tabs/filters:** the news hub itself has **no own tab bar or filters** — no
competition/region/topic facets on the feed. Filtering is delegated to the *site-wide header
nav* (shared across all pages):

| Arabic label | English | URL/slug |
|---|---|---|
| مباريات اليوم | Today's Matches | `/كرة-القدم/مباريات-اليوم` |
| جدول البث | Broadcast Schedule | `/أحداث-رياضية/كرة-القدم` |
| أخبار | News | `/أخبار` |
| كأس العالم 2026 | World Cup 2026 | dropdown with sub-links |
| مسابقات | Competitions | dropdown → `/كرة-القدم/مسابقة/<slug>/<id>` (e.g. دوري روشن السعودي `/كرة-القدم/مسابقة/دوري-روشن-السعودي/ea0h6cf3bhl698hkxhpulh2zz`) |
| فرق | Teams | dropdown |
| رياضات | Sports | dropdown (other-sport verticals, see §6) |
| اللاعبون | Players | dropdown |

Competition-scoped news lives on competition pages, not as filters on `/أخبار`.

**Feed organization:**
- One **featured/hero card** (large image + headline) at top.
- Below it, a **reverse-chronological card feed** (grid of article cards).
- Card anatomy: thumbnail (~300×200), headline (≤2 lines), short subtitle/description on
  some cards, timestamp (`HH:MM` + `DD شهر YYYY`, e.g. "00:12 12 يونيو 2026"), contextual
  label (team name or match pairing, e.g. "الولايات المتحدة الأمريكية ضد باراجواي").
- **No comment counts, no author, no view counts** on cards.
- Ad slots ("إعلان") interspersed in the feed.

**Pagination:** single link at the bottom labeled **"أقدم"** (Older) → `/أخبار/2`
(path-segment page number; no load-more, no infinite scroll). H1 confirms server-side
paging ("صفحة 1" = page 1).

**Sidebar:** none. Full-width single-column feed; no most-read/polls/matches widgets on
this page.

## 2. Article page template (generic blocks) + URL pattern

Sampled one article under `/كرة-قدم/أخبار/<slug>/blta2d14a3ca66d81c5`. Template, top to bottom:

1. **Shared site header** (nav as above). No breadcrumb trail detected.
2. **Topic chips (tags)** near/above the headline — linked entity chips, e.g.:
   - team: `/كرة-القدم/فريق/<slug>/<id>` (فريق = team)
   - competition: `/كرة-القدم/مسابقة/<slug>/<id>` (مسابقة = competition)
   - country: `/كرة-القدم/دولة/<slug>/<id>` (دولة = country)
   - player: player-profile URL (اللاعب)
   - sport: `/كرة-قدم/blt…` (كرة قدم = football)
3. **H1 headline** (single heading; "فيديو:" prefix convention for video articles).
4. **Byline + timestamp:** author name linked to an author-profile page, plus publish
   date and time on its own line ("12 يونيو 2026" + "00:07"). No "updated" stamp observed.
5. **Hero media slot:** one large licensed photo with agency credit line (Getty Images);
   responsive image with width/quality URL params. (On video articles the player occupies
   this slot — see §4.)
6. **Body:** plain paragraphs only — no subheadings, no inline images, no embedded
   tweets/videos, no in-body "read also" interruptions in the sampled article.
7. **"اقرأ أيضًا" (Read Also) related-articles block** after the body: ~4 cards in a
   horizontal grid (thumbnail + headline + timestamp). Video articles use the variant
   heading "قد يعجبك أيضاً" (You may also like), mixing articles and match coverage.
8. **Comments: none.** No comment section, count, or login prompt on the template.
9. **Share controls:** not present in the rendered template (only footer-level social
   profile icons: YouTube, Facebook, Instagram, X, TikTok).
10. **Sidebar: none** — single-column centered layout with full-width ad slots.

**Article URL pattern (precise):**

```
/<sport-section>/<category>/<arabic-headline-slug>/blt<16-hex-id>
```

- `<sport-section>`: `كرة-قدم` (football) for football news; other sports use their own
  namespace (e.g. `/تنس/أخبار/…`, see §6).
- `<category>` observed: `أخبار` (news), `القوائم` (lists/listicles).
- Slug: Arabic words hyphen-joined, derived from the headline (double hyphen `--` where
  the headline contains punctuation).
- ID: Contentstack entry UID, `blt` + 16 hex chars (e.g. `blta2d14a3ca66d81c5`). The ID is
  the stable key; the slug is decorative.
- No date component in article URLs.

## 3. Transfers section (existence, structure, URL pattern)

**Exists** as a dedicated hub, linked from a homepage "أخبار الانتقالات" (transfer news)
rail with a "المزيد من الأخبار" (more news) link.

**Hub URL:** `/الإنتقالات/k94w8e1yy9ch14mllpf4srnks`
(canonical encoded form: `/%D8%A7%D9%84%D8%A7%D9%95%D9%86%D8%AA%D9%82%D8%A7%D9%84%D8%A7%D8%AA/k94w8e1yy9ch14mllpf4srnks`).
Page title: "أخبار الإنتقالات، صفحة 1 | كووورة" (Transfer News, Page 1). The ID suffix is the
same 25-char base-36 style ID used for competition pages — transfers is modeled as a
*category/section entity*, not a special app.

**Structure — pure news feed, no data product:**
- Hub title block, then a vertical chronological feed of 20+ transfer-article cards.
- **No confirmed-deals table** (no player/from/to/fee columns anywhere).
- **No per-window organization** (no summer/winter tabs).
- **No filters** (no league, club, confirmed-vs-rumor facets).
- Card = image + destination-club tag + headline + 1–2 line subtitle + timestamp —
  same card as the news hub.
- Items link to ordinary article URLs under `/كرة-قدم/أخبار/<slug>/blt<id>`.
- No sidebar.

**Pagination:** "أقدم" (Older) → `/الإنتقالات/2/k94w8e1yy9ch14mllpf4srnks` — note the page
number sits **between slug and ID**: `/<hub-slug>/<page>/<id>`.

**Gotcha log:** `/انتقالات` (bare) → 404; `/كرة-قدم/الإنتقالات/<id>` → 404 (no sport prefix
on this hub); composed-hamza encoding → 404. Only the exact decomposed-encoded href works.

## 4. Videos section (hub + detail templates, URL patterns)

**Hub URL:** `/فيديوهات` ("videos", encoded `/%D9%81%D9%8A%D8%AF%D9%8A%D9%88%D9%87%D8%A7%D8%AA`).
Page title: "أخبار الفيديو، الصفحة 1 | كووورة" (Video News, Page 1).

**Hub structure:**
- Primary grid of ~6 featured video cards, then further card grids below; ad slots between.
- Card anatomy: thumbnail (JPG + WebP), headline prefixed "فيديو:" (Video:), contextual
  subtitle, match/team label (e.g. "المكسيك ضد جنوب أفريقيا" = Mexico vs South Africa),
  publish date, short time value (duration or publish time, e.g. "00:04"). No view counts,
  no channel attribution.
- **No video category tabs/filters** — single mixed feed.
- Pagination: "أقدم" (Older) → `/فيديوهات/2`.
- No sidebar.
- Cards link **internally** (kooora.com), not to YouTube.

**Video detail template:** video pages are the *standard article template* (same URL
pattern `/كرة-قدم/أخبار/<slug>/blt<id>`) with these deltas:
- A **video player replaces the hero image slot** (custom/self-hosted player UI; GOAL
  placeholder branding visible on the poster frame — not a bare YouTube iframe).
- Headline keeps the "فيديو:" prefix; byline (linked author) + date as in §2.
- Short contextual paragraphs follow the player.
- Topic chips as in §2, including a **match-page chip**:
  `/كرة-القدم/مباراة/<match-slug>/<id>` (مباراة = match; ID here is a mixed-case short ID,
  e.g. `kmC15xNAExEGFTumq0d9r`).
- Related block headed "قد يعجبك أيضاً" (4 mixed items). No comments; no share buttons.

**Homepage YallaGoal rail:** distinct from `/فيديوهات` — its items deep-link **directly to
YouTube** (`youtu.be/...`, `youtube.com/watch?v=...`), i.e. an embedded external channel
rail rather than internal video pages.

## 5. Shop section (internal/external, top-level structure)

**URL:** `/التسوق-الرياضي/jay1lq18ea7j1bks90cjvzor9` ("sports shopping"; the bare
`/التسوق-الرياضي` path the brief cited resolves through this ID-suffixed canonical).
Breadcrumb/category label: "تسوق مع كووورة" (Shop with Kooora).

**Verdict: internal editorial section, NOT a storefront.**
- It is a feed of **commerce-editorial articles** (buying guides — currently World Cup 2026
  ticket guides and domestic-league match-ticket guides), newest first.
- Cards are the standard article card (image, headline, subtitle, timestamp); items link to
  normal article URLs under `/كرة-قدم/أخبار/<slug>`.
- **No product grid, no prices, no cart, no external retailer domains or visible affiliate
  markers** in the listing. (Affiliate links may exist inside article bodies — not inspected,
  body text out of scope.)
- No sub-categories or filters; pagination "أقدم" → `/التسوق-الرياضي/2/jay1lq18ea7j1bks90cjvzor9`
  (same `/<slug>/<page>/<id>` pattern as transfers).

## 6. Other-sports verticals (list + depth of one example)

Full set found in the header "رياضات" (Sports) dropdown (mirrored in footer per brief):

| Arabic label | English | Hub URL |
|---|---|---|
| التنس / تنس | Tennis | `/تنس/blta843d4a7d2cc7ebc` |
| الفورملا 1 | Formula 1 | `/الفورملا-1/bltd557251dfd094243` |
| كرة السلة / كرة-سلة | Basketball | `/كرة-سلة/blt1cf15af39086dfdf` |
| كرة اليد / كرة-يد | Handball | `/كرة-يد/blt8ebc9b6d97010e55` |
| الكرة الطائرة / كرة-طائرة | Volleyball | `/كرة-طائرة/bltb20b8f1b4da6ca04` |
| قدم الصالات / قدم-صالات | Futsal | `/قدم-صالات/blta013beb1cff92434` |

Hub URL pattern: `/<sport-slug>/blt<id>` — each sport is a CMS section entity.

**Depth probe — Tennis (`/تنس/blta843d4a7d2cc7ebc`):**
- Page title: "أخبار تنس، صفحة 1 | كووورة" (Tennis News, Page 1).
- **NEWS-ONLY vertical.** It does **not** mirror the football structure: no matches/scores
  section, no standings, no tournament sub-navigation (no ATP/WTA/Grand Slam links).
- Layout: shared header → hero background → featured card + secondary card + standard card
  grid → "أقدم" pagination → footer. No sidebar.
- Articles are namespaced under the sport: `/تنس/أخبار/<slug>/blt<id>`
  (e.g. `/تنس/أخبار/زفيريف-بطلا-لرولان-جاروس/blt60ca310c191e8b12`).
- Pagination: `/تنس/2/blta843d4a7d2cc7ebc` (`/<sport-slug>/<page>/<id>`).
- Strong presumption (same template engine + same hub URL shape): the other five sport
  verticals are likewise news-only feeds. Only football gets matches/competitions/teams/
  players infrastructure.

## 7. Utility/static pages inventory

From the global footer (verified on `/فيديوهات`; identical shared footer site-wide):

| Arabic label | English | URL |
|---|---|---|
| اتصل بنا | Contact us | `/اتصل-بنا` (`/%D8%A7%D8%AA%D8%B5%D9%84-%D8%A8%D9%86%D8%A7`) |
| سياسة الخصوصية | Privacy policy | `/سياسة-الخصوصية` (`/%D8%B3%D9%8A%D8%A7%D8%B3%D8%A9-%D8%A7%D9%84%D8%AE%D8%B5%D9%88%D8%B5%D9%8A%D8%A9`) |
| الشروط والاحكام | Terms & conditions | `/سياسة-الاستخدام` (slug literally "usage policy": `/%D8%B3%D9%8A%D8%A7%D8%B3%D8%A9-%D8%A7%D9%84%D8%A7%D8%B3%D8%AA%D8%AE%D8%AF%D8%A7%D9%85`) |
| ملفات الارتباط | Cookies | no href (cookie-consent settings trigger, not a page) |
| — Android app | Google Play | `https://play.google.com/store/apps/details?id=com.sikooora&hl=ar` |
| — iOS app | App Store | `https://apps.apple.com/app/kooora/id859950269` |

Social profiles (footer icons): YouTube `youtube.com/channel/UCcHR8e5S-3uWN_yL64emgzg`,
Facebook `facebook.com/kooora`, Instagram `instagram.com/kooora/`, X `x.com/kooora`,
TikTok `tiktok.com/@kooora`.

**Not found in footer:** about-us (من نحن), careers, advertise, editorial policy, sitemap —
either absent or not exposed in the shared footer. (Footer was truncated on the long
homepage fetch; verified on a shorter page.)

## 8. URL pattern summary table

| Content type | Pattern | Example/notes |
|---|---|---|
| News hub | `/أخبار` | page N: `/أخبار/2` |
| Videos hub | `/فيديوهات` | page N: `/فيديوهات/2` |
| Transfers hub | `/الإنتقالات/<id25>` | page N: `/الإنتقالات/2/<id25>`; decomposed-hamza encoding required |
| Shop hub | `/التسوق-الرياضي/<id25>` | page N: `/التسوق-الرياضي/2/<id25>` |
| Other-sport hub | `/<sport-slug>/blt<hex16>` | `/تنس/blta843d4a7d2cc7ebc`; page N: `/<sport-slug>/2/blt<hex16>` |
| Article (football) | `/كرة-قدم/<category>/<arabic-slug>/blt<hex16>` | categories: `أخبار`, `القوائم`; video articles same pattern with "فيديو:" headline prefix |
| Article (other sport) | `/<sport-slug>/أخبار/<arabic-slug>/blt<hex16>` | `/تنس/أخبار/…/blt60ca310c191e8b12` |
| Competition | `/كرة-القدم/مسابقة/<slug>/<id25>` | `/كرة-القدم/مسابقة/الدوري-الإسباني/34pl8szyvrbwcmfkuocjm3r6t` |
| Team | `/كرة-القدم/فريق/<slug>/<id>` | from article tag chips |
| Country | `/كرة-القدم/دولة/<slug>/<id>` | from article tag chips |
| Match | `/كرة-القدم/مباراة/<slug>/<idMixed>` | `…/المكسيك-ضد-جنوب-أفريقيا/kmC15xNAExEGFTumq0d9r` |
| Today's matches | `/كرة-القدم/مباريات-اليوم` | header nav |
| Broadcast schedule | `/أحداث-رياضية/كرة-القدم` | "sports events / football" |
| All competitions | `/كل-البطولات` | homepage section link |
| Static pages | `/<arabic-slug>` | `/اتصل-بنا`, `/سياسة-الخصوصية`, `/سياسة-الاستخدام` |

ID types observed: `blt<16-hex>` = Contentstack entry UID (articles, sport sections);
25-char lowercase base-36 = football data entities (competitions, transfers/shop hubs);
mixed-case short ID = match pages. Page numbers are always **path segments**, inserted
before the trailing ID when one exists; the hub heading reflects it ("صفحة N").

---

### Fetch log (14 total, sequential)
1. `/أخبار` — news hub. 2. article `blta2d14a3ca66d81c5` — article template.
3. `/` homepage — nav/section discovery. 4–6. transfer-URL variants (two 404s:
`/كرة-قدم/الإنتقالات/<id>`, `/انتقالات`) + cached homepage re-query for exact href.
7. `/الإنتقالات/k94w8e1yy9ch14mllpf4srnks` — transfers hub. 8. `/فيديوهات` — videos hub.
9. video article `bltfef3d14e8c873360` — video detail. 10. `/التسوق-الرياضي/<id>` — shop.
11. `/تنس/blta843d4a7d2cc7ebc` — tennis hub. 12–13. cached re-queries (homepage footer —
truncated; `/فيديوهات` footer — successful).
