---
name: scout
description: Scout a website with the Scout MCP server. Reaches the page as an honest bot, maps the site's url patterns, finds its listing page, and learns the route for next time. Use when the user says /scout <url>, "scout this site", or wants good information from a url.
argument-hint: <url> [what you want, as a regex]
---

# /scout

Arguments: `$ARGUMENTS`. The first word is the url. Anything after it is what the user wants from the site.
Turn that into a `want` regex: a model number, `price|\$`, `weight|kg`. If there is nothing after the url, leave `want` empty.

1. Call the `scout` tool with `url` and `want`.
2. If `ok` is true, report in a few lines:
   - what the page says (`page.title` and the useful part of `page.markdown`)
   - the site's busiest url patterns from `terrain.patterns`, and `terrain.listing`
   - which reader worked (`page.reader`), and `page.via` if a move found it
3. If `ok` is false, read `page.error.code`, `page.error.message` and `page.error.next_steps`.
   Each step is a concrete call. Try them in order, at most three rounds:
   - a `site_map(...)` step: run it, pick the real url that best fits the request, then `reach` that url.
   - a `moves(action='propose', ...)` step: propose the move **only if the evidence supports it**, for example a
     sub-page you saw in `site_map` or a public archive copy. Then `reach` again. Scout scores the move on that try.
   - a want step: loosen the regex, or read `page.closest.markdown` and use the words the page really uses.
   - `ROBOTS_DISALLOWED`, `CHALLENGE_WALL`, `REFUSED` and `LOGIN_REQUIRED` are the site saying no. Do not try to get
     around them by changing the user agent or solving a challenge. Look for an official API, a feed, or the same
     information on another site.
4. Finish with what was found, or the final error code and the one thing a human could do next.
   Then call `recipe` for the host and say in one line what Scout learned, such as the reader order, a listing,
   or a detail suffix.

Scout runs politely: one request per host per second, and slower if robots.txt sets a Crawl-delay. Do not fan out
dozens of parallel reaches against one host.
