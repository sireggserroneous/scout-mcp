---
name: scout
description: Scout a website with the Scout MCP server, then compile what was learned into a recipe a small model (Qwen 4B and down) can run in one call. Use when the user says /scout <url>, "scout this site", or wants good information from a url.
argument-hint: <url> [what you want, as a regex]
---

# /scout

Arguments: `$ARGUMENTS`. The first word is the url. Anything after it is what the user wants from the site.
Turn that into a `want` regex: a model number, `price|\$`, `weight|kg`. If there is nothing after the url, leave `want` empty.

## 1. Work the site out

Call `scout` with `url` and `want`.

- If `recipes` already has one for what the user wants and its status is ok, use it with `run` and stop here.
- If you found the site by searching, check `own_hosts`. A host that carries the subject's name but that its site
  doesn't link to is a reseller, not the maker.
- Make sure the site is the company or body you mean. Same-name companies are common: Edwards Lifesciences is not
  Edwards fire safety, simplex.com is a crypto firm and not Simplex fire, and sol.com is not Sol-Ark. The page should
  name the product line the user means.
- Read the site's terms of use before scouting it in bulk. robots.txt isn't the only rule. LexisNexis, for example,
  forbids automated access without written permission. If the terms forbid it, don't scout: record the permission
  request with `enroll` (route 'commercial', signup_url the contact the terms name), and offer the user a short email
  asking for permission or a bulk copy.
- When `reach` returns `jsonld` (schema.org data), prefer a `jsonld` recipe step over regexes on the page text: names,
  SKUs and prices there are cleaner than the html.
- If the user will pull a lot from this site (many pages, a long list of models), use cached mode. Call
  `cache(url, filter=…)`, with `source='archive'` when the site walls Scout or should not be loaded. Then run recipes
  with `inputs=[…]` and `cached=true`.

If `ok` is false, read `page.error.code` and `page.error.next_steps`. Each step is a concrete call. Try them in
order, for at most three rounds:
- A `site_map(...)` step: run it, pick the real url that best fits the request, then `reach` that url.
- A `moves(action='propose', ...)` step: propose the move only if you have evidence it will work, such as a sub-page
  you saw in `site_map`. Then `reach` again.
- A want step: loosen the regex, or read `page.closest.markdown` and use the words the page actually uses.
- `ROBOTS_DISALLOWED`, `LOGIN_REQUIRED`, `REFUSED` or `CHALLENGE_WALL`: the first next step is the site's official
  route when Scout knows one. An open API or bulk download: reach it. One that needs signing up: Scout has filed an
  enrollment request, so tell the user it is on the `/scout-update` checklist.
- If Scout knows no official route (the last next step says so), look for one yourself: the site's API, developer
  program, bulk download or data feed. LexisNexis, for example, has a developer program a person applies to. Record
  what you find with `enroll` `action='request'`, with the route, signup url, base url, auth kind, cost, terms and a
  sample url. A keyed route becomes an enrollment request for `/scout-update`.
- `BLOCKED`: the site throttled us. Scout holds the host and will probe it later. Tell the user who the error says to
  email.

## 2. Compile it for small models

Once you have reached good information, turn the path you took into a recipe, so nobody has to work it out again.
1. Choose what the caller will supply (`input_name`): a model, a SKU, a slug. Leave it empty when the recipe takes
   nothing, for example "list every product page".
2. Choose 3 example inputs from `terrain.patterns` samples or a `site_map` call. Make them different from each other:
   upper and lower case, a `+`, spaces. Take them from item patterns, never from a pattern marked `editorial`
   (blogs, news, press, careers): those are never the items. A page named "… Series", "… Family" or "… Lineup" is a
   category, not an item, and so is a page that lists 3 or more child pages under its own path (a family hub).
3. Write the steps. Each step is one operation:
   - `input`: rewrite rules for the input (slug rules)
   - `map`: find the real url in the sitemap. Prefer this to a guessed url template.
   - `reach`: read a page
   - `find`: follow a link on the page
   - `extract`: pull one regex per field from the page
4. Call `compile`. Scout runs the recipe on every example and saves it only if all of them pass. If a test fails, read
   the failing RESULT line and `next_steps`, fix that step, and compile again. Stop after three tries.
5. Read the values in the test RESULT lines yourself. Counting found fields proves nothing: Depot once landed 17,126 law
   sections with zero errors, and they were a site's menu or text with every "t" missing. Scout refuses obvious menus
   and missing letters (`BAD_VALUE`), but a wrong-but-plausible value only a reader catches.

## 3. Report

- What the page says: `page.title`, plus the useful part of `page.markdown`.
- The site's busiest url patterns, and `terrain.listing`.
- The compiled recipe's test RESULT lines, and its `card`. The card is the single instruction you give a small model.
  Its `bash` form is `scout-mcp run <recipe> "<input>"`.
- If no recipe compiled, give the last error code and the one thing a human could do next.

Do not send dozens of parallel reaches to one host. Scout spaces out its requests to each host, and those requests
queue up.
