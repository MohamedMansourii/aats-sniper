# Navigation & Homepage Map — kooora.com

Research date: 2026-06-12. Method: 3 cached WebFetch passes of the homepage + 1 raw-HTML pull (server-rendered markup parsed for header/footer hrefs) + 2 hub-page fetches. Structure only; no article text or assets copied. Site is a Next.js app (Footballco platform, edition `ar`); header/footer are server-rendered, the country dropdown and auth modal hydrate client-side.

---

## 1. Header navigation tree (every item + href, grouped, with translations)

Header = logo + one primary nav bar (8 items, most with mega-menu dropdowns) + country-dropdown button + login button. 103 anchor tags total in the `<header>` element. Relative hrefs are relative to `https://www.kooora.com`. Arabic slugs are served unencoded (the server also accepts percent-encoded forms).

### Logo
- كووورة (Kooora) → `/`

### 1.1 مباشر (Live) → `/كرة-القدم/مباريات-اليوم`
| Arabic | English | href |
|---|---|---|
| مباريات اليوم | Today's matches | `/كرة-القدم/مباريات-اليوم` |
| جدول البث | Broadcast (TV) schedule | `/أحداث-رياضية/كرة-القدم` |

### 1.2 أخبار (News) → `/أخبار`
No dropdown — direct link to the news hub.

### 1.3 كأس العالم 2026 (World Cup 2026) — mega-menu, 3 groups

**Group: معلومات (Information)**
| Arabic | English | href |
|---|---|---|
| أخبار | News | `/كرة-القدم/مسابقة/كأس-العالم/أخبار/70excpe1synn9kadnbppahdn7` |
| جدول المباريات | Match schedule | `/كرة-القدم/مسابقة/كأس-العالم/مباريات/70excpe1synn9kadnbppahdn7` |
| جدول الترتيب | Standings | `/كرة-القدم/مسابقة/كأس-العالم/جدول/70excpe1synn9kadnbppahdn7` |

**Group: أبرز المنتخبات العالمية (Top international teams)** — pattern `/كرة-القدم/فريق/<slug>/<id>`
| Arabic | English | href |
|---|---|---|
| الأرجنتين | Argentina | `/كرة-القدم/فريق/الأرجنتين/ak48fkypnql8y4n69cvcq5ghc` |
| البرازيل | Brazil | `/كرة-القدم/فريق/البرازيل/ajab3nmpoltsoeqcuoyi4pwzx` |
| فرنسا | France | `/كرة-القدم/فريق/فرنسا/4pz87gsel7183b7kcadw1dwzv` |
| البرتغال | Portugal | `/كرة-القدم/فريق/البرتغال/8gxg8f7p9299jbrz30u8bsc7g` |
| إسبانيا | Spain | `/كرة-القدم/فريق/إسبانيا/eh7yt2x2wck51oixw8012ux5j` |
| ألمانيا | Germany | `/كرة-القدم/فريق/ألمانيا/3l2t2db0c5ow2f7s7bhr6mij4` |

**Group: المنتخبات العربية (Arab national teams)**
| Arabic | English | href |
|---|---|---|
| السعودية | Saudi Arabia | `/كرة-القدم/فريق/السعودية/9l4imoomrnyceg5u3kdxf5l5r` |
| المغرب | Morocco | `/كرة-القدم/فريق/المغرب/avggs3u2b5cu8i1dnzknhth52` |
| مصر | Egypt | `/كرة-القدم/فريق/مصر/3669sbeijslw88073qlw1cbcp` |
| قطر | Qatar | `/كرة-القدم/فريق/قطر/ea68amhsn28ijs7bzjuzeqrz6` |
| العراق | Iraq | `/كرة-القدم/فريق/العراق/7u6a9femhquay3jnk6ysgiwx9` |
| الأردن | Jordan | `/كرة-القدم/فريق/الأردن/btn8ibf55ovz7y252vdiprbip` |
| الجزائر | Algeria | `/كرة-القدم/فريق/الجزائر/cbx8lz7loz866tsoawwrpxyl9` |
| تونس | Tunisia | `/كرة-القدم/فريق/تونس/ctp7ovvf34m7fzshua9ogbr6i` |

### 1.4 مسابقات (Competitions) — mega-menu, 3 groups; pattern `/كرة-القدم/مسابقة/<slug>/<id>`

**Group: مسابقات عالمية (International competitions)**
| Arabic | English | href |
|---|---|---|
| كل المسابقات | All competitions | `/كل-البطولات` |
| كأس العالم 2026 | World Cup 2026 | `/كرة-القدم/مسابقة/كأس-العالم/70excpe1synn9kadnbppahdn7` |
| دوري أبطال أوروبا | UEFA Champions League | `/كرة-القدم/مسابقة/دوري-أبطال-أوروبا/4oogyu6o156iphvdvphwpck10` |
| دوري أبطال آسيا النخبة | AFC Champions League Elite | `/كرة-القدم/مسابقة/دوري-أبطال-أسيا-النخبة/1fedahp0rws09tj451onten8r` |
| دوري أبطال إفريقيا | CAF Champions League | `/كرة-القدم/مسابقة/دوري-أبطال-أفريقيا/cse5oqqt2pzfcy8uz6yz3tkbj` |
| الدوري الأوروبي | UEFA Europa League | `/كرة-القدم/مسابقة/الدوري-الأوروبي/4c1nfi2j1m731hcay25fcgndq` |

**Group: الدول الأوروبية (European countries)**
| Arabic | English | href |
|---|---|---|
| الدوري الإسباني – لا ليجا | La Liga (Spain) | `/كرة-القدم/مسابقة/الدوري-الإسباني/34pl8szyvrbwcmfkuocjm3r6t` |
| الدوري الإنجليزي الممتاز | Premier League (England) | `/كرة-القدم/مسابقة/الدوري-الإنجليزي-الممتاز/2kwbbcootiqqgmrzs6o5inle5` |
| الدوري الإيطالي | Serie A (Italy) | `/كرة-القدم/مسابقة/الدوري-الإيطالي/1r097lpxe0xn03ihb7wi98kao` |
| الدوري الألماني | Bundesliga (Germany) | `/كرة-القدم/مسابقة/الدوري-الألماني/6by3h89i2eykc341oz7lv1ddd` |
| الدوري الفرنسي | Ligue 1 (France) | `/كرة-القدم/مسابقة/الدوري-الفرنسي/dm5ka0os1e3dxcp3vh05kmp33` |

**Group: الدول العربية (Arab countries)**
| Arabic | English | href |
|---|---|---|
| دوري روشن السعودي | Saudi Pro League (Roshn) | `/كرة-القدم/مسابقة/دوري-روشن-السعودي/ea0h6cf3bhl698hkxhpulh2zz` |
| الدوري المصري الممتاز | Egyptian Premier League | `/كرة-القدم/مسابقة/الدوري-المصري-الممتاز/8k1xcsyvxapl4jlsluh3eomre` |
| الدوري المغربي الممتاز | Botola Pro (Morocco) | `/كرة-القدم/مسابقة/الدوري-المغربي-الممتاز/1eruend45vd20g9hbrpiggs5u` |
| الرابطة الجزائرية الأولى | Algerian Ligue 1 | `/كرة-القدم/مسابقة/الرابطة-الجزائرية-الأولى/57nu0wygurzkp6fuy5hhrtaa2` |
| الدوري الأردني | Jordanian Pro League | `/كرة-القدم/مسابقة/الدوري-الأردني/145hkd59i6foieuwr4mwi6wlq` |
| دوري نجوم قطر | Qatar Stars League | `/كرة-القدم/مسابقة/دوري-نجوم-قطر/xaouuwuk8qyhv1libkeexwjh` |
| دوري الخليج العربي | UAE Pro League (Arabian Gulf) | `/كرة-القدم/مسابقة/دوري-الخليج-العربي/f39uq10c8xhg5e6rwwcf6lhgc` |
| الرابطة التونسية الأولى | Tunisian Ligue 1 | `/كرة-القدم/مسابقة/الرابطة-التونسية-المحترفة-الأولى/f4jc2cc5nq7flaoptpi5ua4k4` |

### 1.5 فرق (Teams) — mega-menu, 5 country columns; pattern `/كرة-القدم/فريق/<slug>/<id>`

**إسبانيا (Spain):** أتلتيك بلباو (Athletic Bilbao) `/كرة-القدم/فريق/أتلتيك-بيلباو/3czravw89omgc9o4s0w3l1bg5` · أتلتيكو مدريد (Atlético Madrid) `/كرة-القدم/فريق/أتلتيكو-مدريد/4ku8o6uf87yd8iecdalipo6wd` · برشلونة (Barcelona) `/كرة-القدم/فريق/برشلونة/agh9ifb2mw3ivjusgedj7c3fe` · ريال مدريد (Real Madrid) `/كرة-القدم/فريق/ريال-مدريد/3kq9cckrnlogidldtdie2fkbl` · فالنسيا (Valencia) `/كرة-القدم/فريق/فالنسيا/ba5e91hjacvma2sjvixn00pjo`

**إنجلترا (England):** ارسنال (Arsenal) `/كرة-القدم/فريق/آرسنال/4dsgumo7d4zupm2ugsvm4zm4d` · تشيلسي (Chelsea) `/كرة-القدم/فريق/تشيلسي/9q0arba2kbnywth8bkxlhgmdr` · ليفربول (Liverpool) `/كرة-القدم/فريق/ليفربول/c8h9bw1l82s06h77xxrelzhur` · مانشستر سيتي (Manchester City) `/كرة-القدم/فريق/مانشستر-سيتي/a3nyxabgsqlnqfkeg41m6tnpp` · مانشستر يونايتد (Manchester United) `/كرة-القدم/فريق/مانشستر-يونايتد/6eqit8ye8aomdsrrq0hk3v7gh`

**إيطاليا (Italy):** إنتر (Inter) `/كرة-القدم/فريق/إنتر/3vo5mpj7catp66nrwwqiuhuup` · روما (Roma) `/كرة-القدم/فريق/روما/2tk2l9sgktwc9jhzqdd4mpdtb` · ميلان (AC Milan) `/كرة-القدم/فريق/ميلان/9dntj5dioj5ex52yrgwzxrq9l` · نابولي (Napoli) `/كرة-القدم/فريق/نابولي/gi0l1habji5hpgar77dl5jqe` · يوفنتوس (Juventus) `/كرة-القدم/فريق/يوفنتوس/bqbbqm98ud8obe45ds9ohgyrd`

**ألمانيا (Germany):** باير ليفركوزن (Bayer Leverkusen) `/كرة-القدم/فريق/باير-ليفركوزن/7ad69ngbpjuyzv96drf8d9sn2` · بايرن ميونخ (Bayern Munich) `/كرة-القدم/فريق/بايرن-ميونخ/apoawtpvac4zqlancmvw4nk4o` · بوروسيا دورتموند (Borussia Dortmund) `/كرة-القدم/فريق/بوروسيا-دورتموند/dt4pinj0vw0t0cvz7za6mhmzy` · شالكه 04 (Schalke 04) `/كرة-القدم/فريق/شالكه-04/cz4a6wmzx2obyisadhgaccx7b` · فولفسبورج (Wolfsburg) `/كرة-القدم/فريق/فولفسبورج/a8l3w3n0j99qjlsxj3jnmgkz1`

**فرنسا (France):** باريس سان جيرمان (PSG) `/كرة-القدم/فريق/باريس-سان-جيرمان/2b3mar72yy8d6uvat1ka6tn3r` · ليل (Lille) `/كرة-القدم/فريق/ليل/be2k34rut1lz79jxenabttqlc` · ليون (Lyon) `/كرة-القدم/فريق/ليون/121le8unjfzug3iu9pgkqa1c7` · مارسيليا (Marseille) `/كرة-القدم/فريق/مارسيليا/27xvwccz8kpmqsefjv2b2sc0o` · موناكو (Monaco) `/كرة-القدم/فريق/موناكو/4t4hod56fsj7utpjdor8so5q6`

### 1.6 رياضات (Sports) — dropdown of non-football sports; pattern `/<sport-slug>/<blt-id>` (CMS "blt" IDs = Contentstack)
| Arabic | English | href |
|---|---|---|
| التنس | Tennis | `/تنس/blta843d4a7d2cc7ebc` |
| الفورملا1 | Formula 1 | `/الفورملا-1/bltd557251dfd094243` |
| كرة السلة | Basketball | `/كرة-سلة/blt1cf15af39086dfdf` |
| كرة اليد | Handball | `/كرة-يد/blt8ebc9b6d97010e55` |
| الكرة الطائرة | Volleyball | `/كرة-طائرة/bltb20b8f1b4da6ca04` |
| قدم الصالات | Futsal | `/قدم-صالات/blta013beb1cff92434` |

### 1.7 اللاعبون (Players) — mega-menu, 6 country-of-league columns; pattern `/كرة-القدم/لاعب/<slug>/أخبار/<id>`

**إسبانيا (Spain / La Liga):** كيليان مبابي (Kylian Mbappé) `/كرة-القدم/لاعب/كيليان-مبابي/أخبار/5e9ilgrz3tzg9kd1gk3yvrahh` · فينيسيوس جونيور (Vinícius Júnior) `/كرة-القدم/لاعب/فينيسيوس-جونيور/أخبار/b4witgokm49tidm1a83qcvbop` · لامين يامال (Lamine Yamal) `/كرة-القدم/لاعب/لامين-يامال/أخبار/abr79wsl0folgkyvl821ggs2c` · رافينيا (Raphinha) `/كرة-القدم/لاعب/رافينيا/أخبار/2wl6h1rlrfnn2mr65vuo3t815` · جود بيلينجهام (Jude Bellingham) `/كرة-القدم/لاعب/جود-بيلينجهام/أخبار/e83ula4wockmc2xid7185kcq2`

**إنجلترا (England / Premier League):** محمد صلاح (Mohamed Salah) `/كرة-القدم/لاعب/محمد-صلاح/أخبار/5ilkkfbsss0bxd6ttdlqg0uz9` · إيرلينج هالاند (Erling Haaland) `/كرة-القدم/لاعب/إرلينج-هالاند/أخبار/atzboo800gv7gic2rgvgo0kq1` · فلوريان فيرتز (Florian Wirtz) `/كرة-القدم/لاعب/فلوريان-فيرتس/أخبار/2k5g68ywtr79lc45wozvifqlm` · بوكايو ساكا (Bukayo Saka) `/كرة-القدم/لاعب/بوكايو-ساكا/أخبار/8tc7dcuwdeqbjgt7vw5p38xgq` · كول بالمر (Cole Palmer) `/كرة-القدم/لاعب/كول-بالمر/أخبار/dl10343h8yopcgerzur5samwa`

**السعودية (Saudi Arabia / Pro League):** كريستيانو رونالدو (Cristiano Ronaldo) `/كرة-القدم/لاعب/كريستيانو-رونالدو/أخبار/h17s3qts1dz1zqjw19jazzkl` · كريم بنزيما (Karim Benzema) `/كرة-القدم/لاعب/كريم-بنزيما/أخبار/c1mm3hdfcs78obhxmhailzidx` · رياض محرز (Riyad Mahrez) `/كرة-القدم/لاعب/رياض-محرز/أخبار/exiribcpg9vnuq9ekhlk74dlh` · سالم الدوسري (Salem Al-Dawsari) `/كرة-القدم/لاعب/سالم-الدوسري/أخبار/2ghv5nqgyn0wv9ekdx1ow0445` · داروين نونيز (Darwin Núñez) `https://www.kooora.com/كرة-القدم/لاعب/داروين-نونيز/5e300bc3sbmy6iqpvmic34qju` (note: no /أخبار segment on this one)

**ألمانيا (Germany / Bundesliga):** هاري كين (Harry Kane) `/كرة-القدم/لاعب/هاري-كين/أخبار/b9g4qurpll4wizajo95c406hh` · جمال موسيالا (Jamal Musiala) `/كرة-القدم/لاعب/جمال-موسيالا/أخبار/bhsdzppop8jwjsxwftizot1t6` · سيرهو جيراسي (Serhou Guirassy) `/كرة-القدم/لاعب/سيرهو-غيراسي/أخبار/bmrdtenqr84jt4gmw6i7wez11` · ميكايل أوليسيه (Michael Olise) `/كرة-القدم/لاعب/ميكايل-أوليسيه/أخبار/5/m2c4lckkcr7yilnqxrrsgh2i` · ألفونسو ديفيز (Alphonso Davies) `/كرة-القدم/لاعب/ألفونسو-ديفيز/أخبار/9/582uvj0i3dvm0zwbou9q68pgp`

**فرنسا (France / Ligue 1):** أشرف حكيمي (Achraf Hakimi) `/كرة-القدم/لاعب/أشرف-حكيمي/أخبار/1vr896w03yky9kmu8x5tjkicp` · عثمان ديمبلي (Ousmane Dembélé) `/كرة-القدم/لاعب/عثمان-ديمبيلي/أخبار/ckaqxlw257qzj8vjdumvwbaad` · فيتينيا (Vitinha) `/كرة-القدم/لاعب/فيتينيا/أخبار/cionsifzpx5rhc8es29wv6i95` · خفيتشا كفاراتسخيليا (Khvicha Kvaratskhelia) `/كرة-القدم/لاعب/خفيتشا-كفاراتسخيليا/أخبار/4zy4d60v2qmaplm9xbe51y1gp` · ديزيري دوي (Désiré Doué) `/كرة-القدم/لاعب/ديسير-دو/أخبار/Kh8M22qniPI2CnndAURUG`

**إيطاليا (Italy / Serie A):** لاوتارو مارتينيز (Lautaro Martínez) `/كرة-القدم/لاعب/لاوتارو-مارتينيز/أخبار/7r2tgpdrr4v6n9d2gkzwmlazt` · باولو ديبالا (Paulo Dybala) `/كرة-القدم/لاعب/باولو-ديبالا/أخبار/93i0gcndi2m8giqqcm4knduxh` · رافائيل لياو (Rafael Leão) `/كرة-القدم/لاعب/رافائيل-لياو/أخبار/11o903vztwpnmvgrinn208qnd` · سكوت ماكتوميناي (Scott McTominay) `/كرة-القدم/لاعب/سكوت-مكتوميناي/أخبار/9fvo9v5e9r0ptibied6ejv3zd` · لوكا مودريتش (Luka Modrić) `https://www.kooora.com/كرة-القدم/لاعب/لوكا-مودريتش/diod1hv5hv7v7z7mfleev1txx`

### 1.8 تسوق مع كووورة (Shop with Kooora) → `https://www.kooora.com/التسوق-الرياضي/jay1lq18ea7j1bks90cjvzor9`
Affiliate/commerce hub ("sports shopping"), opens with `target="noopener"`.

### Header utility elements (non-nav)
- **Country dropdown** — icon-only `<button class="country-dropdown_...">`; option list is client-rendered (not in SSR HTML). Used to set the user's country (localizes broadcast/TV-channel data); it is NOT a language switcher.
- **Login button** — `<button id="login-button">` (avatar icon). Opens a client-side auth modal (`auth-modal_*`); embedded i18n strings include تسجيل الدخول (Log in) / إنشاء حساب (Create account); Google Identity Services script (`accounts.google.com/gsi/client`) is loaded for social sign-in.
- **No visible search box in the header SSR markup** — search exists in the app's i18n strings (placeholder ابحث "Search") but is client-rendered/secondary, not a primary header element.

---

## 2. Footer navigation (grouped columns, every link + href)

The footer is compact — a single bar, not multi-column link lists. (WebFetch's markdown conversion truncated before it; recovered from raw HTML, `div.footer_page-footer__*`.)

**Logo + copyright**
- Kooora logo → `/`
- Copyright text: كل الحقوق محفوظة كووورة© 2026 ("All rights reserved Kooora © 2026") — text only, no link.

**App download links (`footer_page-footer__app-links`)**
| Store | href |
|---|---|
| Google Play | `https://play.google.com/store/apps/details?id=com.sikooora&hl=ar` |
| Apple App Store | `https://apps.apple.com/app/kooora/id859950269` |

**Social links (`footer_page-footer__social`), icon-only**
| Network | href |
|---|---|
| YouTube | `https://www.youtube.com/channel/UCcHR8e5S-3uWN_yL64emgzg` |
| Facebook | `https://www.facebook.com/kooora` |
| Instagram | `https://www.instagram.com/kooora/` |
| X (Twitter) | `https://x.com/kooora` |
| TikTok | `https://www.tiktok.com/@kooora` |

**Legal/about nav (`footer_page-footer__links`)**
| Arabic | English | href |
|---|---|---|
| اتصل بنا | Contact us | `/اتصل-بنا` (served as `/%D8%A7%D8%AA%D8%B5%D9%84-%D8%A8%D9%86%D8%A7`) |
| ملفات الارتباط | Cookies (consent settings) | `<button>` — opens cookie-consent manager, no href |
| سياسة الخصوصية | Privacy policy | `/سياسة-الخصوصية` |
| الشروط والاحكام | Terms & conditions | `/سياسة-الاستخدام` (slug literally "usage policy") |

**Not present in footer:** no language/region switcher (that lives in the header country dropdown), no sitemap links, no "about us" page link, no advertising page link. An RSS feed is declared in `<head>`: `https://feeds.footballco.com/kooora/feed/6p5bsxot7te8yick`.

---

## 3. Homepage module inventory (ordered list, generic structural descriptions)

Order top → bottom (above the body, an affiliate-links disclaimer strip sits directly under the header; leaderboard ad slots labelled إعلان "Ad" appear between modules throughout):

1. **Live scoreboard / match ticker** (`fco-scoreboard`) — Top. Horizontal strip titled المباريات ("Matches") with live scores, match minute, and a settings (favorites) button. Cards link to match detail pages ("تفاصيل المباراة" match details / "تقديم المباراة" match preview). Entities: matches, teams, competitions.
2. **Match schedule grid by date** — Top-middle. Date scroller (~1 month window) plus competition filters; links to match pages. Entities: matches, competitions.
3. **Featured match hero** — Upper-middle. One spotlighted match with crests, lineups/formations, possession and momentum visuals, player ratings, group standings snippet. Entities: match, teams, players, standings. Includes a "اختر الفائز" ("Pick the winner") prediction widget.
4. **Broadcast/streaming block** — Middle. Channel logos (e.g., beIN Sports) with outbound viewing links; ties to the جدول البث TV-schedule section. Entities: matches, broadcast channels.
5. **Hero news carousel** (`CURATED_NEWS_CAR...` card group) — Middle. Editorially curated headline slider with prev/next controls and slide indicators. Entities: articles → teams/players/competitions.
6. **World Cup 2026 news feed** — Middle-lower. Competition-scoped chronological article list (homepage currently carries World Cup theming, `wc-bg-active` background band). Entities: articles, competition.
7. **Video rail** — Lower-middle. Branded video series cards ("YallaGoal"). Entities: videos.
8. **Competition/region news tabs** — Lower. Tabbed article lists segmented by competition/region (World Cup 2026, Premier League, La Liga, Saudi League, Morocco, Egypt). Entities: articles, competitions.
9. **Transfers block** — Lower. Transfer-market article cluster (rumors, negotiations, official deals). Entities: articles, players, teams.
10. **Regional news sections** — Bottom. Country-specific article blocks (Saudi Arabia, England, Spain, Morocco, Egypt) ending in a "المزيد من الأخبار" ("More news") button → news hub. Entities: articles.
11. **Personalised rail (client-side)** — embedded data declares a `PersonalisedContentRailElement` titled "اختر فريقك المفضل" ("Pick your favorite team") — a follow-your-team onboarding module rendered after hydration for logged-in/preference users.

---

## 4. Secondary hub sub-navigation (news hub, competitions hub)

### 4.1 News hub — `/أخبار` (News)
- **No dedicated sub-navigation or category pills of its own.** It reuses the global header; the body is a hero/banner area + "آخر الأخبار" ("Latest news") heading + multi-column reverse-chronological article grid (thumbnail + headline + timestamp + match/competition context per card).
- **Pagination:** classic paged archive — "أقدم" ("Older") link to `/أخبار/2`, i.e. pattern `/أخبار/<page-number>`. No infinite scroll dependency.
- Category-scoped news lives instead on entity pages (`/كرة-القدم/مسابقة/<slug>/أخبار/<id>`, `/كرة-القدم/لاعب/<slug>/أخبار/<id>`), not as tabs inside the hub.

### 4.2 Competitions hub — `/كل-البطولات` (All competitions)
- **Has its own filter bar**, distinct from the global header:
  | Arabic | English | role |
  |---|---|---|
  | الكل | All | scope filter |
  | شائع | Popular | popularity filter |
  | جميع الدول | All countries | country filter (dropdown) |
  | دوري | League | competition-type filter (league vs cup) |
  | رجال | Men | gender filter |
- **Grouping:** continuous list grouped by confederation/region then country (e.g., Asia, then country headings such as ألمانيا Germany, آيسلندا Iceland), each country expanding to its leagues/cups.
- **Link pattern:** `/كرة-القدم/مسابقة/<arabic-slug>/<opta-style-id>` — e.g. `/كرة-القدم/مسابقة/الدوري-البرازيلي/scf9p4y91yjvqvg5jndxzhxj`, `/كرة-القدم/مسابقة/كاس-المانيا/486rhdgz7yc0sygziht7hje65`, `/كرة-القدم/مسابقة/دوري-ادنوك-للمحترفين/f39uq10c8xhg5e6rwwcf6lhgc`.
- **No pagination, no in-page search field** — one long filterable list.

---

## 5. Language/RTL & global UI notes (search, login/account, app links, region handling)

- **Document attributes:** `<html lang="ar" dir="rtl" class="fco-kooora">` — fully RTL, Arabic-only document.
- **No alternate-locale links:** `<head>` contains only `rel="canonical"` (`https://www.kooora.com`) and one `rel="alternate"` for RSS. **No hreflang alternates, no English version linked anywhere.** Embedded page data sets `"edition":"ar"` — Kooora is the Arabic edition of the Footballco platform (the same component system, `fco-*` class prefix, that powers GOAL; i18n strings even reference "تطبيق GOAL"/the GOAL app), so internationalization is handled by sibling sites, not a locale switcher.
- **Region handling:** header country dropdown (icon button, options hydrate client-side) sets the user's country — used to localize broadcast/TV channel info (الدولة "Country" appears in the i18n dictionary), not the language. Page data has an empty `geoTargeting` array per article, implying optional geo-scoped content.
- **Nav adaptation:** single nav markup served to all; mobile/desktop differences are CSS-driven (`fco-ad__hide-on-desktop` / `hide-on-mobile` ad slots; same `fco-global-navigation` list renders as hamburger/accordion on mobile). Mega-menus are `level-1` items with grouped sub-lists.
- **Search:** no SSR search input in the header; search UI strings exist client-side (placeholder ابحث "Search", "no results / try another search").
- **Account:** login button in header opens an auth modal; Google sign-in (GIS client) supported; account features per i18n strings: create account, change password, favorites/follow teams (sign-in-gated "follow this team", manage favorites, max-favorites cap), newsletters, beta/research program opt-in.
- **Apps:** official mobile app linked in footer — Google Play package `com.sikooora`, Apple app id `859950269` ("Kooora").
- **Monetization signals in structure:** affiliate-disclaimer strip below header (marketing links disclosure), labelled ad slots, betting/odds-related disclaimer strings (18+, "play responsibly", operator price comparison) and a shop section — relevant if mirroring layout.
- **URL conventions summary:** matches `/كرة-القدم/مباريات-اليوم`; competition `/كرة-القدم/مسابقة/<slug>/<id>` with subpages `/أخبار` (news) `/مباريات` (matches) `/جدول` (standings) inserted before the id; team `/كرة-القدم/فريق/<slug>/<id>`; player `/كرة-القدم/لاعب/<slug>/أخبار/<id>`; other sports `/<sport-slug>/<contentstack-blt-id>`; news archive `/أخبار/<page>`; static pages use Arabic slugs (`/اتصل-بنا`, `/سياسة-الخصوصية`, `/سياسة-الاستخدام`).
