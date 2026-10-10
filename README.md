# Scout MCP

Scout is an MCP server for web crawling that improves with use. A capable model like Claude works a site out once.
Scout compiles what it learned into a **recipe** that a 4B model can run with one instruction.

```
/scout https://mikrotik.com/product/RB433AH price|cpu
```

Claude reaches the page, maps the site and finds the traps. It then hands back a one-line card that Qwen3.5-4B runs
correctly from then on:

```
Call the tool run with recipe="mikrotik.com/specs" and input="hAP ax2". Reply with the RESULT line only.

RESULT: ok | mikrotik.com/specs | hAP ax2 | url=https://mikrotik.com/product/hap_ax2; title=MikroTik · hAP ax²; price=$99.00; cpu=IPQ-6010; ram=1 GB; max_power=27 W
```

## Install

**Claude Code** (installs the MCP server and the `/scout` skill):

```
/plugin marketplace add sireggserroneous/scout-mcp
/plugin install scout@scout-mcp
```

**Any MCP client.** Install [uv](https://docs.astral.sh/uv/), then add this to the client's MCP config:

```json
{ "mcpServers": { "scout": { "command": "uvx",
  "args": ["--from", "git+https://github.com/sireggserroneous/scout-mcp", "scout-mcp"] } } }
```

**Pages built by JavaScript** need a headless browser. Add `"--with", "playwright"` to the uvx args, then run
`uvx playwright install chromium` once. If you already run Firecrawl or crawl4ai, set `FIRECRAWL_URL` or
`CRAWL4AI_URL` and Scout uses it as one more reader.

## From a maze to one line

Getting MikroTik specs looks like a one-step job. Here is what Claude actually ran into on the way:

| Try | Result |
|---|---|
| guess `mikrotik.com/product/{model}` | `RB433AH` works. `hAP ac3` is a 404: that page lives at `hap_ac3`. |
| lowercase it, turn spaces into `_` | Now `RB433AH` is a 404: that one really is uppercase. |
| also turn `+` into `plus` | `CRS354-48G-4S+2Q+RM` works (`crs354_48g_4splus2qplusrm`)… |
| | …but `RB5009UG+S+IN` lives at `rb5009ug_s_in`. Same character, opposite rule. |
| **look the url up in the sitemap, comparing both sides with `plus` and punctuation removed** | **all four styles work** |

Then the data: 4 fields, each a label on one line and its value on a later line, on a page that also has a 40-row
throughput table, a documents list and a retailer map.

Written out as instructions, that is a 7-step program with a string-normalisation rule, a loop over 566 urls,
a branch for "not found", and four lookups in a 7,000-character page. That is too much for a 4B model. So it is
never handed to one. Claude compiles it instead:

```json
{"name": "mikrotik.com/specs", "input_name": "model",
 "examples": ["RB433AH", "hAP ac3", "CRS354-48G-4S+2Q+RM", "RB5009UG+S+IN", "CCR2004-1G-12S+2XS"],
 "steps": [
  {"map": "https://mikrotik.com", "filter": "/product/", "same": [["plus", ""], ["[^a-z0-9]", ""]]},
  {"reach": "{url}", "want": "Specification"},
  {"extract": {"price":     "^\\s*-\\s*Suggested price\\s*\\n\\s*(\\S.*)$",
               "cpu":       "^\\s*-\\s*CPU\\s*\\n\\s*(\\S.*)$",
               "ram":       "^\\s*-\\s*Size of RAM\\s*\\n\\s*(\\S.*)$",
               "max_power": "^\\s*-\\s*Max power consumption\\s*\\n\\s*(\\S.*)$"}}]}
```

`compile` runs the recipe on every example and **saves it only if all of them pass**. The two wrong guesses above
were caught this way: each one passed on some examples and failed on others. Once saved, every branch lives in code.
The small model gets one card and one tool, and its whole job is a single call:

| | the small model gets | the small model must |
|---|---|---|
| without a recipe | 7 numbered steps, `site_map` and `reach` | normalise strings, scan 566 urls, branch, read a 7k-char page, format a line |
| with a recipe | 1 line, the `run` tool | make one call, copy one line |

**Measured on Qwen3.5-4B** (Q4_K_M, llama.cpp on a laptop iGPU), on 5 MikroTik models, as an A/B:

| | exact answer | tool calls | wall time (median) |
|---|---|---|---|
| **A**: the 7 steps as instructions, `site_map` + `reach` | 4 / 5 | 2–4 per model, 15 in all (5 of them wrong turns) | 389–671 s (414 s) |
| **B**: the card, `run` | **5 / 5** | **1 per model** | **22–195 s (88 s)** |

In arm A, Qwen filtered the sitemap with the raw model name, built a url with a space in it, and once dropped the `$`
from a price. It reached the right page in the end, but it took 3 to 11 minutes each time, mostly spent reading 566
urls and a 7,000-character page. In arm B the model never sees any of that. One arm-A run did catch a bug in the
recipe: the RAM regex had matched a sentence in the product description, not the spec list. The fix (anchoring each
field to its list item) went in, the recipe was recompiled with that model as a fifth example, and the arm-B run was
repeated on the fixed recipe. Both arms used the same model and the same Scout. Harness: `bench/qwen_ab.py`.

If a page changes, `run` fails loudly with a code: `NO_MATCH at step 1`, `EXTRACT_MISS at step 3`. The recipe's
score drops, and `recipes()` marks it `last run failed … recompile`. The next big-model session fixes the recipe. The
small model's card stays the same.

The CLI form fits a small model with a shell. It prints one line and exits 1 on failure:

```
$ scout-mcp run mikrotik.com/specs "CCR2004-16G-2S+"
RESULT: ok | mikrotik.com/specs | CCR2004-16G-2S+ | url=https://mikrotik.com/product/ccr2004_16g_2splus; ... price=$465.00; cpu=AL32400; ram=4 GB; max_power=48 W
$ scout-mcp run mikrotik.com/specs "nope 9"
RESULT: fail | mikrotik.com/specs | nope 9 | NO_MATCH at step 1
```

### Recipe steps

| Step | Does |
|---|---|
| `{"input": [[regex, repl], ...], "lower": true}` | rewrites the input (slug rules) |
| `{"map": url, "filter": regex, "same": [[regex, repl], ...]}` | the site's own urls. `same` keeps the url whose last segment equals the input after both are rewritten. Last step: returns the list. Otherwise: the first url becomes `{url}`. |
| `{"reach": "https://…/{input}" \| "{url}", "want": regex}` | reads a page with the full reader ladder (below) |
| `{"find": regex}` | the first link on the page whose url or text matches becomes `{url}` |
| `{"links": "/chapter/<n>/"}` | a link family from `families`. Last step: every matching link on the page. Otherwise: the first becomes `{url}`. |
| `{"jsonld": {"price": "offers.price"}, "type": "Product"}` | reads the page's schema.org data by dotted path. A name ending in `?` is optional. |
| `{"extract": {"field": regex, "optional?": regex}}` | group 1 of each regex. Every field must be found, except one whose name ends in `?`, which is filled only when the page has it. |

## How Scout learns: a maze, a Markov chain, and rewrite rules

Every way to reach a page is a **move**:
- the readers: `direct`, `firecrawl`, `crawl4ai`, `browser`
- `url_rewrite`: a regex on the full url
- `locale_prefix` × `listing_root`: where a site keeps its index of things, such as `/en-us` + `/products`
- `detail_suffix`: where the details live, such as `/specifications`

The idea comes from Markov chains and MarkovJunior-style rewrite rules. Each move takes Scout from one state to the
next. The goal state is **good information**: real content (not a wall, a login page or an error page) that carries
what you asked for.

```mermaid
flowchart LR
  U[url + want] --> O[host's ordered moves<br/>winner first, recent failures last]
  O --> J{good information?}
  J -- yes --> W[return page · learn the winner]
  J -- no --> N[next move: reader → url_rewrite → detail_suffix → on-page link]
  N --> J
  N -- maze exhausted --> E[coded error + next steps · logged]
  W -. a big model compiles the path .-> R[recipe: one call for a small model]
```

- **Each host keeps its readers in order.** The reader that last worked goes first, and one that failed goes to the
  back for six hours. When excalidraw.com gives a plain fetch an empty JavaScript shell, Scout falls back to the
  browser. On the next visit it starts with the browser.
- **The register stores moves as data.** Each move has a scope (`*`, a domain or a host regex) and a Laplace score
  `(wins + 1) / (tries + 2)`. A move nobody has tried scores 0.5. A move that loses its first five tries is retired.
  Agents propose moves with `moves(action='propose', …)`, and real traffic decides which ones stay.
- **Parts compose.** A listing url is a locale prefix (`""`, `/en-us`, `/us/en`, …) plus a catalogue root
  (`/products`, `/collections`, `/catalog`, …). Scout ranks every pairing by the product of the two parts' scores. It
  puts the locale in the url you gave it, or the one the host taught it, first. Credit is by contrast: when
  `/products` wins after `/en/products` failed, only `/en` loses. When nothing wins, nothing is scored, because a
  site with no listing teaches nothing about the parts.
- **Recipes are the compiled layer on top.** Every run scores a recipe, the same way moves are scored.

### The recipe book it ships with

`scout_mcp/recipes.json` was distilled from 417 sites that a larger crawler had learned. Every one was checked live
again on 2026-10-03 with Scout's own user agent, and only what held up was kept:

- **Shared structure became scored parts.** The priors are measured. `/products` was the root of all 13 verified
  catalogue indexes. A bare root (no locale) won 8 of 8. The `/en` prefix the old crawler kept guessing was real on
  only 4 of 41 sites: the rest were 404s, homepage redirects, or soft 200s serving the homepage. `/specifications` is
  the detail page on 5 sites.
- **Per-site facts, only where a site differs** (147 sites): 56 need a rendering reader, 14 have a verified listing
  (7 with a locale), 28 have item url patterns proven by a real catalogue, 6 have a detail suffix, and 59 put up a
  wall (challenge, robots, refusal or login) that Scout reports before trying.
- **Dropped:** unverified listings, including product pages, category pages, tag pages and a census.gov policy page
  that had all passed as "listings", plus item patterns a small model guessed but a catalogue never proved.

Point `SCOUT_DB` at a shared path and a whole team, people and agents alike, learns from each other's runs.

## Link families: walking a site with no sitemap

These rules came from a sibling project: a recipe chain that rebuilt 16 US state codes from scratch, with 7 or 8 of
every 8 sampled sections matching the held text word for word. Its hardest problems were about links, and the fixes
were all general:

- **A link's family is its path with the numbers taken out.** `Chapter_1.html` and `Chapter_14A.html` are one family,
  and so are `NHTOC-I.htm` and `NHTOC-XIV.htm` (Roman numerals). Links with no numbers group as siblings under their
  parent (`/products/*`). Only same-site families with 3 or more links count. Static assets, `#anchors` (each item is
  read once, not once per anchor) and a page's link to itself are ignored.
- **Sample across the family, not its first links.** A list opens with its odd items: a code's first chapter is often
  a one-line "Repealed". Requiring 2 of 3 good samples at every level compounded into no path at all, so it is 2 at
  the top and 1 below.
- **Vote on what the family leads to, not on whichever sample came back first.** Section pages that also list their
  neighbours made one sample look like an index; the majority got it right.
- **Look deeper before trusting a weak match.** A page that looks like one item, but still has families leading down,
  is walked further first.
- **Rank by yield through every level:** links × share of good samples × what each yields below. Scoring one level at a
  time made a five-page index outrank 89 chapters of sections.
- **Tell content from menus by function words.** The density of *the, of, shall, any* separates sentences (≈0.35–0.5)
  from navigation (≈0.15) where capitals and length could not. Every `reach` result carries this `prose` score.
- **Skip families that lead to a different kind of document.** Statute pages link to the bills that enacted them, and
  the chain followed them into scanned slip laws.

`families(url, want?)` runs this walk and returns the ranked families, their samples, and a `recipe_hint` draft for
`compile`. The `links` recipe step then lists a family's urls on sites with no sitemap.

Live, on two sites (2026-10-03):

| Start | Best chain found by sampling | Estimate | Compiled hint |
|---|---|---|---|
| Idaho statutes index | `Title<n>` (74 links, 5/5 samples are indexes) → `Title<n>/T<n>` (34 per title, 3/3 pages) | ≈2,516 chapter pages | `RESULT: ok … count=19; first=…/Title1/T1CH1` |
| a MikroTik category page | `/product/*` (22 links, 5/5 pages) | 22 products | `RESULT: ok … count=22; first=…/product/hex_s_2025` |

MikroTik added two rules the law sites never needed:
- **A family the page above already shows is navigation.** Every MikroTik page carries the same category menu, and
  without this rule the walk descended from the menu back into the menu.
- **Siblings group under their parent first.** The number shape names the family only when one shape covers 80% of
  them (`Title<n>`). `RB<n>` and `crs<n>_<n>_<n>_in` are both just `/product/*`.

## When a site blocks you

Also from the sibling project. A statute site blocked its crawler's address after a re-read at ten requests a second,
and the lessons generalise:

- **A block page can come back as HTTP 200.** That site answered `Blocked <your IP> … for assistance EMAIL:
  WebMaster@…` as an ordinary page, so a check on status codes alone never saw it and the crawler kept going. Scout
  reads a page as a block only when it has **both halves**, a refusal ("blocked", "access denied", "too many
  requests") and something about the visitor (an IP address, "your IP", a CAPTCHA, "are you a robot"). That way a page
  that only mentions "blocked" isn't mistaken for one. It checks error bodies and 200 pages alike.
- **A block stops the whole walk.** Every reader leaves from the same address, so trying the next one only adds to
  the load. Scout stops, puts the host **on hold** for 30 minutes, and slows its pace there for good (5 s or slower).
  A 429 does the same, for its `Retry-After` or 2 minutes.
- **The probe is built in.** When an automatic hold ends, the next reach is a single probe. If it is refused again,
  the hold doubles (up to a day) and the pace slows again. You don't need a separate watcher process.
- **Holds are shared.** They live in Scout's memory, so every agent pointed at the same `SCOUT_DB` stays off the site.
  A held host is not contacted at all: `reach`, `site_map` and `families` return `HELD` and send no request.
- **The way out is a person.** The block page usually names a contact, and Scout keeps it with the host. The
  `BLOCKED` error's first next step says who to email and what to say: what reads the site, that it went too fast,
  the new pace, and an offer to use a bulk download. `hold(host, reason='unblock requested <date>')` then keeps
  everyone off until `hold(host, lift=true)`. A hold set that way has no end date, so Scout never probes it.

## Official routes and enrollment

When a site walls Scout, the best next step is usually the site's own front door: an open API, a bulk download, a
feed, or an API a person signs up for. Scout keeps that per site as its **access** entry.

- **No signup needed** (`open_api`, `bulk`, `feed`, `public_json`, `alternative`): the wall's first next step is that
  route, as a ready `reach(...)`.
- **A person must enroll** (`enroll_free`, `enroll_paid`, `enroll_oauth`, `commercial`): Scout files an **enrollment
  request** the first time the wall is hit. `enroll(action='list')` is the checklist. Each entry says where to sign
  up, what it costs, the terms, and where the key goes.
- **No route known yet:** the error says so, and a big model looks for one (the API, developer program or bulk
  download) and records it with `enroll(action='request', …)`. Say you need LexisNexis but haven't set Scout up for
  it: `/scout https://www.lexisnexis.com/...` hits the login, Claude records LexisNexis's developer program, and it
  lands on the checklist.
- **Updating Scout:** `/scout-update` works through the checklist with you, along with failing recipes, holds waiting
  on a site's answer, and recent failures. When you've put a key in place, `enroll(action='done')` tests it on the
  API's sample url before marking the site enrolled. From then on recipes call the API with no key in them; Scout adds
  it.

**Keys never pass through a tool.** They live in `SCOUT_KEY_<NAME>` environment variables or in
`~/.config/scout-mcp/keys.env` (`NAME=value`). Scout ignores that file unless its mode is 600. A key is added at fetch
time, only to its own API's host, and scrubbed by value from every page, url and error Scout returns. OAuth client
credentials (`NAME_ID` and `NAME_SECRET`) are exchanged for a token, which is cached until it expires.

The book ships with the official routes for 21 sites that challenged Scout, researched and tested on 2026-10-03:

| Site | Official route | Signup | Tested from a datacenter address |
|---|---|---|---|
| www.ecfr.gov, ecfr.federalregister.gov | eCFR API (+ GovInfo bulk XML) | none | 200 JSON, while the pages challenge |
| www.drugs.com | DailyMed, openFDA and RxNav (the FDA's own label data) | none | 200 JSON |
| www.nytimes.com | RSS feeds (full text needs the free Developer API key) | none | 200 RSS |
| www.arlingtonma.gov | the town's RSS feeds and GIS data hub | none | 200 RSS, 200 JSON |
| snuggymom.com | WordPress RSS and REST API | none | 200 JSON |
| www.dmp.com | product literature on assets.dmp.com | none | 200 PDF |
| alaskacountyoffices.org | Alaska's state community database (ArcGIS) | none | 200 JSON |
| legiscan.com | LegiScan Public API | free key | — |
| www.merriam-webster.com | Merriam-Webster Dictionary API | free key (non-commercial) | — |
| www.britannica.com | Britannica Syndication API | free key (non-commercial) | — |
| www.digikey.com | DigiKey Product Information v4 | free OAuth app | — |
| www.bhphotovideo.com, www.guitarcenter.com | affiliate programs (product feeds unverified) | free application | — |
| us.rs-online.com, www.acronymfinder.com | licensed web services | contract | — |
| webfiles.nycourts.gov, dictionary.cambridge.org, www.arcat.com, www.essentialbuildstore.com, aqcheckin.s3.amazonaws.com | none found: no API, a retired one, an unreachable one, or a private bucket | — | the reason is recorded, so no one searches again |

Several "challenged" sites serve their own APIs and feeds without any challenge. A wall in front of the pages is
often not a wall in front of the data.

## Lessons from a big catalogue: AMD

The sibling crawler's scout went after AMD's catalogue, and every mistake it made became a general rule:

- **A wall can be silent.** From some networks amd.com answers no status at all: the plain fetch stalls until it
  times out, and the browser's HTTP/2 stream is reset. Scout used to call that `NETWORK` and suggest checking the
  spelling. A stall or reset from a host that resolves is now `SILENT_REFUSAL`: the site's edge refusing this client.
  It is treated as a wall (official route first, the host held so nothing keeps knocking), not as a typo.
- **A map has to work when plain fetches don't.** `site_map` reads sitemaps and walks links through the host's learned
  reader, not just a plain fetch.
- **Map from the catalogue's root when nothing else looks like a catalogue.** AMD's map ran out on blog posts. Walking
  `/en/products` found 1,561 product pages. When the sitemap and the walk turn up no catalogue-shaped urls, Scout finds
  the root with its scored locale × root parts and walks from there. That way the step is a scored move, not a
  hard-wired rule.
- **Editorial sections are never the items.** The scout picked `/blogs/` and `/newsroom/` as AMD's product pattern and
  "proved" 689 blog posts. Blog, news, press, events, careers, investors and similar patterns are flagged `editorial`
  and ranked last in `site_map` and `families`.
- **A title of `=====` is not a title.** It falls back to the page's first real heading.
- **"… Series" is a category.** So are "… Family" and "… Lineup". Recipes pick their examples from single items.
- **A page with children under its own path is a hub, not an item.** `families` counts 3 or more child pages as an
  index. Partner, request-a-quote and contact pages are flagged with the editorial ones. Support pages are not flagged,
  because some makers keep their product pages under `/support/`.
- **Listing a whole site again and again gets you blocked.** Four dry runs in an hour, each re-listing amd.com, and its
  edge answered "Access Denied" to everything. `site_map` now reuses a host's listing for 12 hours (`fresh=true` to
  list again). The edge's denial page names no IP, only an incident reference (`Reference #18.…`). An incident
  reference (Akamai Reference #, Cloudflare Ray ID) now counts as the visitor half of a block, so the host is held,
  not retried.

## Download once, read from the archive

Also from the sibling crawler: the way to map a whole site without loading it is to ask the library that already
copied it. The Wayback Machine held 1,543 of AMD's product pages, the newest a day old, while amd.com refused every
request.

- **Listing from the archive's index.** `site_map(url, archive='only')` lists a site from the Wayback Machine's public
  index without a single request to the site. Robots rules come from the site's archived robots.txt. With the default
  `archive='auto'`, the index is used when the site is on hold or listed nothing. amd.com's product section: 1,816
  urls in 12 seconds.
- **Reading from the archive.** When a site walls Scout (blocked, a silent refusal, a challenge, a refusal, a login,
  or down), the next rung is the latest archived copy, as the site served it. A held site is read from the archive
  only, with no contact with the site at all. Every result says so: `archived: {captured: 2026-08-21, copy: …}`, and
  recipe RESULT lines carry `archived=<date>`. The url stays the site's own, so provenance is honest.
- **The archive is never a host's winning reader.** Live reading resumes as soon as the site allows it, and robots.txt
  still binds: a path the site disallows is not read from its archive either. The archive itself is paced at one
  request every 4.5 seconds, under its limit of about 15 copies a minute. Scout's first try at 2 seconds got it refused
  by archive.org, so a refusal from the archive now holds the archive too (10 minutes, doubling), and cache jobs wait
  out the hold, never knocking through it.
- **A local mirror.** Every good page is kept on disk for a day (`SCOUT_MIRROR_DAYS`). Re-runs, parser fixes and recipe
  compiles read the copy, not the site; `reach(fresh=true)` reads it again. Scout's pace was already a promise: it only
  ever slows down for a host, never speeds back up.

It compiles like any other recipe. `amd.com/processor-specs` ships in the book, built entirely from archived pages:

```
RESULT: ok | amd.com/processor-specs | EPYC 9575F | url=https://www.amd.com/en/products/processors/server/epyc/9005-series/amd-epyc-9575f.html; title=AMD EPYC™ 9575F; archived=2026-07-10; cores=64; threads=128; boost=Up to 5 GHz; tdp=400W; socket=SP5; launched=10/10/2024
```

## Cached mode: when you'll pull a lot from one site

Asking a site for the same pages again and again is how crawlers get blocked. If you're about to pull a lot from one
site, download it once and work from the copy:

```
cache("https://www.amd.com/en/products/processors/", filter="(9000|9005)-series/.+\\.html$", source="archive")
cache(url, action="status")                       # listed, fetched, failed, eta
run("amd.com/processor-specs", inputs=["Ryzen 9 9950X3D", "EPYC 9575F", ...], cached=true)
```

- **`cache`** lists the site (sitemaps, link walk, catalogue root, or the Wayback index), then fetches every page once
  into Scout's copy, in the background, at the host's pace. `source='archive'` downloads from the Wayback Machine with
  no request to the site at all. `'auto'` reads live, with the archive where the site walls Scout. The copy is kept
  `days` (default 7).
- **`cached=true`** on `reach`, `site_map` and `run` reads only the copy and sends nothing anywhere. A page that isn't in
  it fails with `NOT_CACHED` and says how to get it. A recipe over a cached site runs at memory speed, so a small model
  can work through a long input list without the site ever seeing it.
- **`run(recipe, inputs=[…])`** returns one RESULT line per input. On the command line:
  `scout-mcp cache <url> [filter] [--archive]` downloads in the foreground, and
  `scout-mcp run <recipe> - --cached < models.txt` prints one line per input.

## What a scout checks first

These rules come from the sibling crawler's queue:

- **What already exists.** `scout(url)` returns the site's compiled `recipes` first. A recipe that passes is the
  answer, so nobody re-scouts the site.
- **Whose site it is.** `own_hosts` lists the hosts the site links to that carry its name, such as regional sites or a
  store. A host that carries the name but isn't linked is a reseller lead, not the maker: meanwellsource.com had 1,827
  "Mean Well" pages.
- **The sibling the request names.** When a page lacks what was asked, one of Scout's next steps climbs to the page
  that lists it and its siblings, and follows the sibling link that names two-thirds of the request's words. Depot held
  New York's Criminal Procedure Law and was asked for its Estates, Powers and Trusts Law; the CPL's parent page named it.

## Counted is not read: lessons from a law re-read

The sibling crawler landed 17,126 law sections with zero errors. When someone finally read a sample, much of it was
broken: 29,299 sections were a website's menu ("Skip navigation Home Documents…"), 23,530 were a heading with no
body, and a double-escaped regex had turned every "t" of 9,308 Alabama sections into a space ("he books… o de ermine
he accuracy"). Every check had counted; none had read. In Scout:

- **A content gate on recipe values.** A value that is page chrome (menu text, raw markup, a stylesheet rule) or has
  lost common letters fails as `BAD_VALUE`, at compile and on every run.
- **A regex lint at compile.** A double-escaped escape inside a character class (`[^\\t]` means "not a backslash
  and not the letter t") is refused with that explanation.
- **The skill says to read the test values yourself.** A wrong but plausible value is something only a reader
  catches.
- **Text decoded the way it was written.** Bytes that decode cleanly as UTF-8 are read as UTF-8. Otherwise Scout uses
  the declared charset, but never UTF-16/32 without a byte-order mark (one state's pages declare UTF-16 and are
  UTF-8), and falls back to windows-1252 (another state's pages lost every § when read as UTF-8).
- **A site that asks for slower requests gets them,** for as long as it asks. That includes a `Retry-After` given as
  an HTTP date.
- **A download nobody is running says so.** The re-reads died with the one-off script that started them, and nothing
  noticed. A cache job whose process is gone shows `interrupted`, and starting it again resumes where it stopped.

## The laws of the United States

Scout can read a state's whole code from the legislature's own site and write it out, one JSON line per section:

```
scout-mcp laws                                   # the states it can read, with their citation form and source
scout-mcp laws us-tx --out texas.jsonl           # every section of the Texas statutes
scout-mcp laws us-nv --limit 5                   # a quick look
```

Each line is `{"citation": "NRS 1.010", "heading": "...", "text": "...", "url": "<the page it came from>"}`. A state is a
short script in `scout_mcp/laws/<state>.json`: a `sections()` generator that reads the legislature's table of contents,
API or bulk file and yields each section's own words. Every request goes through Scout's gate (robots.txt and its crawl
delay, holds), at the recipe's own pace when that is slower. Text that is page chrome or has lost its letters is skipped.
Big bulk files (Texas's code zips, North Dakota's single page) are downloaded once a day to Scout's data folder.

**33 ready:** AL, AS, CO, CT, GU, IA, IL, LA, MD, ME, MI, MN, MP, MT, ND, NE, NH, NJ, NV, NY, OR, PA, PR, RI, SD, TX, UT,
VA, VT, WA, WI, WV, WY. These are the scripts a sibling crawler used in October 2026 to land each code whole: Texas 119,000
sections, Illinois 69,000, Nebraska 55,000, Nevada 50,000. A full state takes from minutes (Texas, 31 downloads) to hours
(Illinois and New Hampshire ask for 10 and 11 seconds between requests).

- **New York** reads the Senate's Open Legislation API, which needs a free key: put it in `SCOUT_KEY_NYSENATE`.
- **Louisiana** takes its Revised Statutes list from a dated snapshot, because the site only opens a title by form post.
- **Not here yet:** AK, AZ, DC, DE, FL, GA, HI, ID, KS, KY, MA, MO, NC and SC are read by the sibling crawler's page-pattern
  engine and still need porting to scripts. California needs its 1.28 GB bulk zip read entry by entry. Indiana serves its
  code zip only to browsers.
- **Need permission first:** Arkansas, Mississippi, Tennessee and the Virgin Islands publish their official code only
  through LexisNexis, whose terms forbid automated reading. New Mexico's official site is on a platform whose terms
  require written consent. Ohio's and Oklahoma's robots.txt files disallow all crawlers. The routes section below says
  whom to ask.

The text is the law as each site publishes it. It is not an official or certified copy; cite the source url.

## Official routes to state codes

The sibling crawler rebuilt about 25 state codes on 2026-10-10. For most of them the best route was not the section pages
but something the legislature publishes for whole-code reading. Those routes are now in the book (`enroll("list")` shows
them, and a wall on these hosts leads with them):

| Site | Official route | Why it beats the pages |
|---|---|---|
| statutes.capitol.texas.gov | one zip of chapter HTML per code | all 30 codes, about 122,000 sections, in 31 requests |
| olls.info | Colorado's title download (HTML zip) | the online C.R.S. is a commercial platform's |
| le.utah.gov | XML per title | structured text, current and future versions marked |
| law.lis.virginia.gov | CSV per title | 76 requests instead of 33,000 throttled pages |
| ndlegis.gov | the whole Century Code on one page | one fetch |
| nebraskalegislature.gov | range view, 400 sections a request | 16 minutes instead of 41 hours at its crawl delay |
| sdlegislature.gov | Statutes API | a title per request (UTF-16 with no byte-order mark) |
| mgaleg.maryland.gov | statute text API | an article's section list, then each section |
| legislature.maine.gov, www.palegis.us | whole titles (.docx, HTML) | one file per title |
| downloads.leginfo.legislature.ca.gov | pubinfo bulk zip (1.28 GB) | the website answers with a challenge |
| nmonesource.com | the platform owner's written consent | its terms forbid automated reading |
| codes.ohio.gov, oklegislature.gov, capitol.tn.gov | none: robots.txt disallows all, or the official copy is on a commercial platform | the request to make is recorded |

What else that rebuild taught:

- **A one-line robots.txt still means what it says.** Ohio's reads `User-agent: * Disallow: /` with the newline missing,
  so parsers find no rules. The intent is plain, so it is treated as disallow-all.
- **Counted is still not read.** Virginia's 32,000 sections, Ohio's 12,000 and Minnesota's 6,900 had landed as the
  legislature's own menus, version banners and footers. Each passed every count. Those phrases are in the content gate,
  and the missing-letters test now leaves short prose, all-capitals notices and lists alone.
- **More requests at once don't make one host faster.** Its pace is fixed by its gap; 39 jobs behind one 2-second gate
  each moved a page a minute and looked dead.

## Structured data, archived block pages, and other things worth knowing

- **schema.org data.** Many product pages carry clean `Product` data as JSON-LD even when their html is a mess.
  `reach` returns it as `jsonld`, and the `jsonld` recipe step reads dotted paths (`offers.price`, `sku`). Title tails
  like "| CT4000T705SSD5 | crucial.com" are cut from names. `crucial.com/ssd` ships in the book, built entirely from
  archived pages while crucial.com's edge rejects Scout:
  `RESULT: ok | crucial.com/ssd | CT4000T705SSD5 | … archived=2026-07-20; name=Crucial T705 4TB PCIe Gen5 NVMe M.2 SSD with heatsink; sku=CT4000T705SSD5; price=565.99; currency=USD`
- **An archived copy can itself be a block page.** The newest Wayback copy of some AMD pages is Akamai's 403, and of
  some Crucial pages it's the edge's rejection page, captured with a 200. Scout tells a replayed error (it has a
  `Memento-Datetime`) apart from the archive refusing Scout, so it no longer holds the archive by mistake. It then walks
  back through older, distinct captures until one is real content. The archive pace is 5 seconds, with a 15-minute
  hold when it refuses.
- **Cloudflare's 2026 wall** ("Performing security verification… protect against malicious bots") is read as a
  challenge, not content.
- **A map started inside a section stays inside it.** `site_map("https://site/en/products/processors/")` lists only
  that section. The site's root maps the whole site.
- **Function words don't steer.** "and", "the", "section", "law" no longer pick which link to follow.
- **A dropped connection to a renderer service gets one retry** on a fresh connection before it counts as a failure.
- **Same-name companies and terms of use.** Edwards Lifesciences is not Edwards fire safety, simplex.com is not
  Simplex fire, and sol.com is not Sol-Ark: the skill checks the site names what you mean. robots.txt is not the only
  rule either. LexisNexis's terms forbid automated access without written permission, so its entry in the book is a
  permission request, not a route.

## Errors that say what to do next

When Scout fails, it returns an error `code`, a `message`, and `next_steps` written as concrete tool calls, plus the
`tried` trail it took through the maze. Any capable model can pick up from there.

| Code | Next steps point to |
|---|---|
| `NOT_FOUND` | `site_map` instead of guessing paths |
| `WANT_MISS` | loosening `want`, a `detail_suffix` move, `site_map` |
| `JS_SHELL` / `THIN_CONTENT` | adding a rendering reader, the site's JSON endpoints, a print view |
| `REFUSED` / `SILENT_REFUSAL` / `CHALLENGE_WALL` / `LOGIN_REQUIRED` | the site's official route, its partner program, the same information published elsewhere |
| `BLOCKED` / `HELD` | who to email to lift the block, `hold` to keep every agent off until they answer, when Scout probes |
| `ROBOTS_DISALLOWED` / `RATE_LIMITED` | an official feed, waiting for `Retry-After` |
| `TLS_ERROR` / `NETWORK` / `SERVER_ERROR` | the cause, retrying later |
| `NO_URLS` / `NO_LISTING` / `NO_MATCH` / `EXTRACT_MISS` / `TEST_FAILED` | what to recompile or propose |

## Tools

| Tool | For | Does |
|---|---|---|
| `scout(url, want?)` | big model | reaches the page, maps url patterns, finds the listing, reports what was learned |
| `reach(url, want?, full?)` | big model | reads one page through the learned ladder |
| `site_map(url, filter?)` | big model | the site's own urls and their patterns |
| `families(url, want?, depth?)` | big model | which link families lead to good pages, by sampling and vote |
| `compile(name, steps, examples, …)` | big model | tests a recipe and saves it, returns the small-model card |
| `moves(action, …)` | big model | lists, proposes or retires moves |
| `hold(host, reason?, lift?, min_interval?)` | big model | keeps every agent off a site (or lets them back), slows Scout's pace there |
| `enroll(action, host?, info?, note?)` | big model | records a site's official route, and lists, tests and closes enrollment requests |
| `memory(host?)` | big model | reader order, routes, moves and recent failures for a host |
| `recipes(host?)` | both | compiled recipes with scores, status and cards |
| `run(recipe, input?, inputs?, cached?)` | **small model** | runs a recipe, returns one RESULT line (one per input) |
| `cache(url, filter?, source?, days?, action?)` | big model | downloads a site once (live or from the Wayback Machine) for cached reads |

Give the small model only `run`, or the CLI. One tool means it has nothing to choose between.

## Configuration

| Variable | Default |
|---|---|
| `SCOUT_DB` | `~/.local/share/scout-mcp/scout.db` |
| `SCOUT_USER_AGENT` | `ScoutMCP/<version> (+https://github.com/sireggserroneous/scout-mcp)` |
| `SCOUT_MIN_INTERVAL` | `1.0` seconds between requests to one host |
| `SCOUT_MIRROR_DAYS` | `1`: how long a good page is re-read from disk (`0` turns the mirror off) |
| `SCOUT_ARCHIVE` | `https://web.archive.org` (`off` to never read archived copies) |
| `SCOUT_KEYS_FILE` | `~/.config/scout-mcp/keys.env` (mode 600), next to `SCOUT_KEY_<NAME>` env vars |
| `FIRECRAWL_URL`, `FIRECRAWL_API_KEY`, `CRAWL4AI_URL`, `CRAWL4AI_TOKEN` | unset |

## Develop

```
uv venv && uv pip install -e . playwright
python -m scout_mcp.scout      # self-checks, no network
```

There are two modules. `scout_mcp/scout.py` is the engine, and `scout_mcp/server.py` holds the MCP tools and the CLI.
Seed moves and example recipes are in `scout_mcp/recipes.json`. MIT licensed.
