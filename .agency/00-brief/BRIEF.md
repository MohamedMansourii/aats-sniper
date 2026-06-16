# BRIEF — Kooora.com Structural Reverse-Engineering (Research Engagement)

**Date:** 2026-06-12
**From:** CEO (direct instruction, with explicit methodology)
**Type:** Research / information-architecture study — no production code. Standard G0–G6 build gates do not apply; CEO supplied the work plan directly.

## CEO Brief (restated)

Produce a complete structural blueprint of https://www.kooora.com/ — every section, subsection,
sub-subsection, and every distinct page type/template. Focus strictly on structure, information
architecture, URL patterns, and navigation — NOT on copying article text, images, or proprietary
design assets. The output is an original-site design aid.

### Required method (CEO-specified)
1. Inventory robots.txt + sitemap(s) → full URL structure.
2. Map homepage navigation (header, footer, content sections).
3. Drill into every top-level section → subsections → sub-subsections (tree).
4. Catalog every distinct page type/template with generic structural components.
5. Document URL patterns per section/page type.
6. Note language/RTL structure and content-category organization.
Respect robots.txt; reasonable request rates.

### Deliverable
One architecture document (site tree, page-type catalog, URL pattern map, navigation map),
compiled to PDF.

## Facts established at intake (verified 2026-06-12)
- Homepage title: "الموقع العربي الرياضي الأول | كووورة" — Arabic, RTL.
- robots.txt: `User-agent: *` with NO Disallow rules; `Sitemap: https://www.kooora.com/sitemap`.
- Key entry points: `/كرة-القدم/مباريات-اليوم` (today's matches/live), `/أخبار` (news),
  `/كل-البطولات` (all competitions), `/التسوق-الرياضي` (shop),
  `/كرة-القدم/مسابقة/<competition>/...` (competition pages), `/أحداث-رياضية/كرة-القدم` (broadcast schedule).

## Research artifacts
Written to `.agency/research/kooora/01..06-*.md`; final blueprint at
`.agency/06-delivery/KOOORA-SITE-BLUEPRINT.md` + PDF at project root.
