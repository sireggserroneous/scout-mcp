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

If `ok` is false, read `page.error.code` and `page.error.next_steps`. Each step is a concrete call. Try them in
order, for at most three rounds:
- A `site_map(...)` step: run it, pick the real url that best fits the request, then `reach` that url.
- A `moves(action='propose', ...)` step: propose the move only if you have evidence it will work, such as a sub-page
  you saw in `site_map`. Then `reach` again.
- A want step: loosen the regex, or read `page.closest.markdown` and use the words the page actually uses.
- `ROBOTS_DISALLOWED`, `LOGIN_REQUIRED` or `CHALLENGE_WALL`: look for the site's API, a feed, or the same
  information on another site.

## 2. Compile it for small models

Once you have reached good information, turn the path you took into a recipe, so nobody has to work it out again.
1. Choose what the caller will supply (`input_name`): a model, a SKU, a slug. Leave it empty when the recipe takes
   nothing, for example "list every product page".
2. Choose 3 example inputs from `terrain.patterns` samples or a `site_map` call. Make them different from each other:
   upper and lower case, a `+`, spaces.
3. Write the steps. Each step is one operation:
   - `input`: rewrite rules for the input (slug rules)
   - `map`: find the real url in the sitemap. Prefer this to a guessed url template.
   - `reach`: read a page
   - `find`: follow a link on the page
   - `extract`: pull one regex per field from the page
4. Call `compile`. Scout runs the recipe on every example and saves it only if all of them pass. If a test fails, read
   the failing RESULT line and `next_steps`, fix that step, and compile again. Stop after three tries.

## 3. Report

- What the page says: `page.title`, plus the useful part of `page.markdown`.
- The site's busiest url patterns, and `terrain.listing`.
- The compiled recipe's test RESULT lines, and its `card`. The card is the single instruction you give a small model.
  Its `bash` form is `scout-mcp run <recipe> "<input>"`.
- If no recipe compiled, give the last error code and the one thing a human could do next.

Do not send dozens of parallel reaches to one host. Scout spaces out its requests to each host, and those requests
queue up.
