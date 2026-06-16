# Sitemap & URL Inventory — kooora.com

Research task R1 — structure-only inventory of https://www.kooora.com (Arabic, RTL).
Crawl date: 2026-06-12. 14 polite sequential fetches total (1 sitemap index + all 13 child sitemaps).
No article text, images, or design assets were copied — URL structure only.

---

## 1. robots.txt summary (already known: permissive, sitemap at /sitemap)

- `User-agent: *` with **no Disallow rules** — entire site crawlable.
- Single sitemap declaration: `Sitemap: https://www.kooora.com/sitemap`.
- No crawl-delay directive observed; politeness self-imposed (sequential fetches).

## 2. Sitemap architecture (index → children)

`https://www.kooora.com/sitemap` is an **XML sitemap index** (despite the extensionless URL) containing exactly **13 child sitemaps**, all under the `/sitemap/` path prefix and all plain XML `<urlset>` files (none of the children is a nested index). No HTML sitemap was needed; fallbacks `/sitemap.xml` and `/sitemap_index.xml` were not required since the index resolved fully.

| # | Child sitemap | Kind / entity | Entry count | lastmod | Notes |
|---|---|---|---|---|---|
| 1 | `/sitemap/main.xml` | Static/core pages | 5 | no | Home + legal/contact + all-competitions hub |
| 2 | `/sitemap/matches.xml` | Match detail pages | 1,007 | no | ~50% of entries carry a `#التشكيلة` (lineup) fragment — unusual in a sitemap |
| 3 | `/sitemap/teams.xml` | Team profile pages | 1,086 | no | One URL per team, no sub-pages listed |
| 4 | `/sitemap/competitions.xml` | Competition hub pages | 20 | no | Only top competitions; one URL each, no /أخبار or /ترتيب sub-pages listed |
| 5 | `/sitemap/countries.xml` | Country hub pages | 20 | no | Ends in 3-letter country code (mar, esp, eng, ksa, ita, egy, ger, alg, jor, fra, irq, tun…) |
| 6 | `/sitemap/categories.xml` | Sport-section hubs | 7 | no | Tennis, Formula 1, basketball, handball, volleyball, futsal, sports shopping |
| 7 | `/sitemap/authors.xml` | Author/byline pages | 100 | — | Mixed ID schemes (CMS `blt…` + legacy numeric) |
| 8 | `/sitemap/editorial-news.xml` | News articles | 312 | yes (ISO 8601) | `/كرة-قدم/أخبار/<slug>/blt<id>` |
| 9 | `/sitemap/editorial-videos.xml` | Video articles | ~383 | yes | Same `/أخبار/` path as news; "فيديو" appears only in slug. Uses `<image:image>` thumbnails, **no** `<video:video>` tags |
| 10 | `/sitemap/editorial-slides.xml` | Listicles/slideshows | 260 | yes | Distinct section segment `/القوائم/` (al-qawa'im, "lists") |
| 11 | `/sitemap/editorial-where-to-watch.xml` | Broadcast/"where to watch" guides | 209 | yes | Same `/أخبار/` path; distinguished only editorially (slugs like ما-القنوات-الناقلة / جدول-مباريات-اليوم) |
| 12 | `/sitemap/editorial-featured.xml` | Featured long-form | 23 | yes | Same `/أخبار/` path; no dedicated segment |
| 13 | `/sitemap/google-news.xml` | Google News (last 48h window) | 100 | yes | Uses `<news:news>`; publication "Kooora.com", language `ar`. Mixes `/أخبار/` and `/القوائم/` URLs |

Total indexed URLs across all children: ≈ 3,532.

## 3. URL pattern grammar

All entity-detail pages follow the shape `/<namespace>/<entity-type>/<arabic-slug>/<id>`. Two distinct namespaces exist for football: **كرة-القدم** (kurat al-qadam, with definite article — live sports-data pages) and **كرة-قدم** (kurat qadam, without article — editorial/CMS pages).

| Pattern template | Entity type | Example (decoded) | Notes |
|---|---|---|---|
| `/` | Home | `https://www.kooora.com/` | |
| `/كرة-القدم/مباراة/<teamA>-ضد-<teamB>/<sportsId>` | Match detail | `/كرة-القدم/مباراة/المكسيك-ضد-جنوب-أفريقيا/kmC15xNAExEGFTumq0d9r` | "ضد" (didd) = "vs". Optional fragment tabs, e.g. `#التشكيلة` (lineup) |
| `/كرة-القدم/فريق/<team-slug>/<sportsId>` | Team profile | `/كرة-القدم/فريق/الأرجنتين/ak48fkypnql8y4n69cvcq5ghc` | فريق (fariq) = team; clubs and national teams alike |
| `/كرة-القدم/مسابقة/<competition-slug>/<sportsId>` | Competition hub | `/كرة-القدم/مسابقة/كأس-العالم/70excpe1synn9kadnbppahdn7` | مسابقة (musabaqa) = competition |
| `/كرة-القدم/دولة/<country-name>/<iso3>` | Country hub | `/كرة-القدم/دولة/مصر/egy` | دولة (dawla) = country. ID is a lowercase 3-letter code, not an opaque ID. Multi-word names keep literal spaces (`%20`), e.g. `المملكة العربية السعودية/ksa` |
| `/كرة-قدم/أخبار/<article-slug>/blt<hex16>` | Editorial article (news, video, where-to-watch, featured) | `/كرة-قدم/أخبار/عصر-جديد-في-كرة-القدم--فيفا-يحدث-ثورة-شاملة-بنظام-الانتقالات/blt9153ffc689ab3cad` | أخبار (akhbar) = news. One shared path for 4 editorial sub-types; type is distinguished by sitemap membership/slug, not URL segment |
| `/كرة-قدم/القوائم/<slug>/blt<hex16>` | Listicle / slideshow | `/كرة-قدم/القوائم/ما-هي-صفقات-يوفنتوس-في-سوق-الانتقالات-الصيفية-2026/bltee7e56aa6da72486` | القوائم (al-qawa'im) = lists |
| `/مؤلف/<author-slug>/<id>` | Author page | `/مؤلف/عبدالاعلى-سكير/11` and `/مؤلف/gill-clark/blt539641b53a119a3d` | مؤلف (mu'allif) = author. ID is legacy numeric (11, 140, 270, 1514, 2881) **or** CMS `blt<hex>`. Slugs may be Arabic or Latin |
| `/<sport-section-slug>/<id>` | Non-football sport hub | `/تنس/blta843d4a7d2cc7ebc` (tennis), `/كرة-سلة/blt1cf15af39086dfdf` (basketball) | Sections: تنس, الفورملا-1, كرة-سلة, كرة-يد, كرة-طائرة, قدم-صالات, التسوق-الرياضي |
| `/<static-page-slug>` | Static/legal page | `/سياسة-الخصوصية` (privacy), `/اتصل-بنا` (contact), `/سياسة-الاستخدام` (terms) | No trailing ID |
| `/كل-البطولات` | All-competitions directory | `/كل-البطولات` | kul al-butulat = "all tournaments"; listed in main.xml |
| `/sitemap`, `/sitemap/<name>.xml` | Sitemap system | `/sitemap/matches.xml` | Index + 13 XML children |

Generalized grammar:

```
URL        := "https://www.kooora.com/" path
path       := ""                                      # home
            | static-slug                             # legal/contact/كل-البطولات
            | "كرة-القدم/" data-entity                 # sports-data namespace
            | "كرة-قدم/" editorial-entity              # editorial namespace
            | "مؤلف/" slug "/" (numeric-id | blt-id)   # authors
            | sport-slug "/" (blt-id | sports-id)      # other-sport section hubs
            | "sitemap" [ "/" name ".xml" ]
data-entity      := ("مباراة" | "فريق" | "مسابقة") "/" slug "/" sports-id
                  | "دولة" "/" country-name "/" iso3
editorial-entity := ("أخبار" | "القوائم") "/" slug "/" blt-id
sports-id  := [a-z0-9]{21,25}      # opaque lowercase base-36 (sports-data provider IDs)
blt-id     := "blt" [0-9a-f]{16}   # CMS entry UID (Contentstack-style)
slug       := arabic-or-latin words joined by "-" ("--" where punctuation was stripped)
```

## 4. Top-level path segments discovered (complete list)

1. `/` — home
2. `/كرة-القدم/…` (kurat al-qadam — football, with article) — data namespace: `مباراة` match, `فريق` team, `مسابقة` competition, `دولة` country
3. `/كرة-قدم/…` (kurat qadam — football, no article) — editorial namespace: `أخبار` news, `القوائم` lists
4. `/مؤلف/…` (mu'allif — author)
5. `/تنس` (tinis — tennis)
6. `/الفورملا-1` (al-formula-1)
7. `/كرة-سلة` (kurat salla — basketball)
8. `/كرة-يد` (kurat yad — handball)
9. `/كرة-طائرة` (kura ta'ira — volleyball)
10. `/قدم-صالات` (qadam salat — futsal)
11. `/التسوق-الرياضي` (al-tasawwuq al-riyadi — sports shopping)
12. `/كل-البطولات` (kul al-butulat — all competitions)
13. `/سياسة-الخصوصية` (privacy policy)
14. `/سياسة-الاستخدام` (terms of use)
15. `/اتصل-بنا` (contact us)
16. `/sitemap` (+ `/sitemap/*.xml`)

Not present in any sitemap but implied by site navigation conventions (gap to verify in a page-crawl task): hub/listing sub-pages such as `/كرة-القدم/مباريات-اليوم` (today's matches) and per-competition tabs (`/أخبار`, `/ترتيب` standings, `/هدافين` scorers) — the sitemaps index only one canonical URL per entity, with tabs apparently handled via fragments (e.g., `#التشكيلة`) or unindexed routes.

## 5. Observations

- **Locale/RTL**: No locale prefix (`/ar/`) and no country subdomain — the site is monolingual Arabic at the root. Arabic appears directly in paths, percent-encoded (UTF-8) in sitemap `<loc>` values.
- **Dual football namespaces**: `كرة-القدم` (data pages: matches/teams/competitions/countries) vs `كرة-قدم` (editorial: news/lists). The presence/absence of the definite article "ال" is the routing discriminator between the live-data platform and the CMS.
- **Two ID systems, hinting two backends**:
  - `blt` + 16 lowercase hex chars on all editorial content, category hubs, and most authors — the `blt…` UID format is characteristic of the Contentstack headless CMS.
  - Opaque 21–25 char lowercase base-36 IDs on matches/teams/competitions (e.g., `ak48fkypnql8y4n69cvcq5ghc`) — characteristic of a sports-data provider (Opta/Stats Perform-style entity IDs).
  - Legacy small numeric IDs survive on some author pages (`/مؤلف/…/11`) — likely migrated accounts from the old kooora platform.
- **Slugs are descriptive but non-canonical-looking**: trailing ID is the true key; the Arabic slug is hyphen-joined, with `--` (double hyphen) where punctuation (colons, commas) was stripped from headlines. One slug leaked a CMS artifact: `copy-of-…` in editorial-slides.
- **Unicode normalization is inconsistent**: data pages encode hamza letters in decomposed form (e.g., `ا` + combining hamza U+0654: `%D8%A7%D9%94` for أ in `أفريقيا`), while editorial section segments use precomposed `أ` (`%D8%A3` in `أخبار`). Any crawler/deduper must NFC/NFD-normalize before comparing URLs.
- **Spaces vs hyphens**: country pages uniquely use literal spaces (`%20`) inside multi-word slugs (`المملكة العربية السعودية/ksa`); every other entity uses hyphens.
- **Fragments in sitemap entries**: ~half the match URLs include `#التشكيلة` (lineup tab) — fragments are non-standard in sitemaps and signal client-side tab routing on match pages.
- **Country code suffix**: country hubs end with lowercase 3-letter football codes (eng, ksa, alg, mar…), not opaque IDs — the only human-readable ID class on the site.
- **No pagination or query params anywhere**: zero `?page=`, `?p=`, or other query strings in any of the ~3.5k sitemap URLs. Listing pagination, if any, is unindexed or fragment/JS-driven.
- **lastmod discipline split**: editorial sitemaps carry full ISO-8601 `lastmod` (millisecond precision, e.g. `2026-06-10T20:43:18.385Z`); data sitemaps (matches/teams/competitions/countries/main) carry none. No `changefreq`/`priority` anywhere.
- **Editorial sub-types are sitemap-level, not URL-level**: videos, where-to-watch guides, and featured pieces all share `/كرة-قدم/أخبار/…` — only their sitemap membership (and slug keywords like فيديو, القنوات-الناقلة) distinguishes them. Lists (`/القوائم/`) are the sole editorial sub-type with its own path segment.
- **Sitemap coverage is shallow by design**: only 20 competitions and 20 countries indexed (top entities), 1,086 teams, ~1,000 recent/upcoming matches, and a rolling editorial window — the sitemap system is a freshness feed, not an exhaustive archive.
