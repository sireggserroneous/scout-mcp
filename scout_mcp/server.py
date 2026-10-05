"""Scout MCP server: the scout's tools over stdio."""
import asyncio

try:
    from mcp.server.mcpserver import MCPServer      # mcp >= 2
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

from . import scout as S

mcp = MCPServer("scout", instructions=(
    "Scout reaches good information on websites and learns the route. Big models: scout(url) or reach(url, want) to "
    "work a site out, then compile(...) what worked into a recipe. Small models: call run(recipe, input) and copy its "
    "RESULT line. When ok is false, error.next_steps are concrete calls to try next."))


@mcp.tool()
async def scout(url: str, want: str = "", full: bool = False) -> dict:
    """Scout a url: reach the page (as markdown), map the site's url patterns, find its listing page, and return what
    Scout has learned about the host. `want` is an optional regex the page must carry (a model number, 'price|\\$',
    a heading); without it, good = real content that is not a wall. On failure `page.error` says why and what to try."""
    return await asyncio.to_thread(S.scout, url, want, full)


@mcp.tool()
async def reach(url: str, want: str = "", full: bool = False, fresh: bool = False, cached: bool = False) -> dict:
    """Reach one page and judge whether it is good information. Tries the host's learned readers winner-first, then
    scored url rewrites and detail sub-pages. Returns ok, markdown (excerpt unless full=true), the trail `tried`, and on
    failure an `error` {code, message, next_steps}. A page read today comes from Scout's mirror (fresh=true reads it
    again). When the site walls Scout, or is on hold, the latest Wayback Machine copy is read instead, and `archived`
    says when it was captured. cached=true reads only Scout's copy of the site (see cache) and sends no request."""
    return await asyncio.to_thread(S.reach, url, want, full, 0, False, fresh, cached)


@mcp.tool()
async def site_map(url: str, filter: str = "", limit: int = 500, fresh: bool = False, archive: str = "auto") -> dict:
    """A site's real urls (robots.txt sitemaps, a polite link walk, the catalogue's root) with their url patterns.
    `filter` is a regex. A listing is reused for 12 hours; fresh=true lists the site again (only when it changed:
    re-listing a whole site run after run is how crawlers get blocked). archive='only' lists the site from the Wayback
    Machine's index without a single request to it; 'auto' does so when the site is on hold or listed nothing.
    Use it instead of guessing paths."""
    return await asyncio.to_thread(S.site_map, url, filter, limit, 20, fresh, archive)


@mcp.tool()
async def families(url: str, want: str = "", depth: int = 2) -> dict:
    """Which of a page's link families lead to good pages, and how many. Each family (links whose paths differ only in
    their numbers, like /chapter/1/ and /chapter/14A/) is sampled across its spread, the samples vote on what the family
    leads to (an index of more links, or pages), indexes are walked one level deeper, and families are ranked by yield
    through every level. `best.recipe_hint` is a draft recipe for compile. Use it on sites with no useful sitemap."""
    return await asyncio.to_thread(S.families, url, want, depth)


@mcp.tool()
async def hold(host: str, reason: str = "", lift: bool = False, min_interval: float = 0) -> dict:
    """Keep every agent sharing Scout's memory off a host, or let them back. Use it when a site blocked you and a person
    has asked for the block to be lifted: reason says what was asked and when. min_interval (seconds) slows Scout's pace
    on that host for good. Scout holds a host by itself after a block page or a 429, and probes once when that ends."""
    return await asyncio.to_thread(S.hold, host, reason, lift, min_interval)


@mcp.tool()
async def enroll(action: str = "list", host: str = "", info: dict | None = None, note: str = "", include_all: bool = False) -> dict:
    """Official routes and the enrollments they need: the checklist for whoever updates Scout.
    action=list: open requests, each with a `todo` that says where to sign up, what it costs, and where the key goes.
    action=request host info: record a site's official route. info = {route: open_api|bulk|feed|public_json|alternative
      (no signup) or enroll_free|enroll_paid|enroll_oauth|commercial (a person must enroll), name, signup_url, docs_url,
      base_url, auth: {kind: none|query|header|bearer|oauth2_client_credentials, name, token_url, headers}, cost, terms,
      sample (a url that returns the data; leave the key out)}. Keyed routes become an enrollment request.
    action=applied (signed up, waiting on approval) | done (Scout tests the key on the sample, then marks enrolled) | decline.
    Keys are never passed here: they go in the keys file or SCOUT_KEY_<NAME> env vars."""
    return await asyncio.to_thread(S.enroll, action, host, info, note, include_all)


@mcp.tool()
async def moves(action: str = "list", kind: str = "", scope: str = "*", spec: dict | None = None, id: int = 0,
                note: str = "", host: str = "") -> dict:
    """The moves register: rewrite rules Scout tries where its built-in moves fail, each scored (wins+1)/(tries+2) on
    real reaches and retired after 5 straight losses.
    action=list [kind, host] | propose kind scope spec [note] | retire id.
    kinds: url_rewrite {pattern, repl} (regex on the full url; repl is literal text plus \\1 groups) ·
    locale_prefix {prefix} and listing_root {path}, which compose into listing paths (/en-us + /products) and are
    ranked, never retired · listing_path {path} (a whole path) · detail_suffix {suffix}.
    scope: '*', a domain ('example.com' covers www.example.com) or a host regex. Propose only what you saw work."""
    if action == "propose":
        return await asyncio.to_thread(S.propose, kind, scope, spec or {}, "agent", note)
    if action == "retire":
        return {"ok": await asyncio.to_thread(S.retire, id)}
    return {"moves": await asyncio.to_thread(S.moves, kind or None, host or None, True)}


@mcp.tool()
async def memory(host: str = "") -> dict:
    """What Scout has learned about a host: winning reader, reader order, routes (listing, detail_suffix, patterns),
    the moves that apply, recent failures. No host: recent failures everywhere and the whole register."""
    return await asyncio.to_thread(S.recipe, host)


@mcp.tool()
async def compile(name: str, steps: list, examples: list | None = None, about: str = "", input_name: str = "") -> dict:
    """Compile what you worked out about a site into a recipe a small model can run in one call. Scout runs it on
    every example and saves it only if all pass; the reply carries the one-line card for the small model.
    name: '<host>/<slug>'. input_name: what the caller supplies ('model', 'sku'; empty for none). examples: 2+ inputs.
    steps, run in order, each a one-key object:
      {"input": [[regex, repl], ...], "lower": true}   rewrite the input (slug rules)
      {"map": "https://site", "filter": "/product/{input}$"}   the site's own urls; first match becomes {url}
      {"reach": "https://site/p/{input}" | "{url}", "want": regex}   read a page
      {"find": regex}   first link on the page matching (url or text) becomes {url}
      {"links": "/chapter/<n>/"}   a link family from families(); last step = all its links, else the first is {url}
      {"jsonld": {"price": "offers.price", "sku": "sku"}, "type": "Product"}   from the page's schema.org data
      {"extract": {"field": "regex with one group", "optional?": "..."}}   every field must be found, except a name ending in ?
    Prefer map over a guessed url template: sites are inconsistent about case and slugs."""
    return await asyncio.to_thread(S.compile_recipe, name, steps, examples, about, input_name)


@mcp.tool()
async def run(recipe: str, input: str = "", inputs: list | None = None, cached: bool = False) -> dict:
    """Run a compiled recipe. Reply with the RESULT line. `inputs` runs it over many inputs (one RESULT line each);
    cached=true reads only Scout's copy of the site, sending no request (use after cache)."""
    if inputs:
        return await asyncio.to_thread(S.run_many, recipe, inputs, cached)
    r = await asyncio.to_thread(S.run, recipe, input, None, cached)
    return {k: r[k] for k in ("result", "fix") if k in r}


@mcp.tool()
async def cache(url: str, filter: str = "", source: str = "auto", max_pages: int = 2000, days: float = 7, action: str = "start") -> dict:
    """Cached mode, for pulling a lot from one site: list it once (filter narrows the urls) and download every page once
    into Scout's copy, in the background, at the host's pace. source='archive' downloads from the Wayback Machine with no
    request to the site; 'live' from the site; 'auto' live, with the archive where the site walls Scout. The copy is kept
    `days`; reads and recipe runs with cached=true use only it. action=status follows the job; action=stop ends it."""
    return await asyncio.to_thread(S.cache, url, filter, source, max_pages, days, action)


@mcp.tool()
async def recipes(host: str = "") -> list:
    """The compiled recipes (optionally for one host): what each takes, its score, whether its last run failed, and the
    one-line card a small model runs."""
    return await asyncio.to_thread(S.recipes, host)


def main():
    """No arguments: the MCP server on stdio. `scout-mcp run <recipe> [input]` prints one RESULT line (exit 1 on fail);
    `scout-mcp recipes` lists the cards. Small models are good at one shell command."""
    import json
    import sys
    args = sys.argv[1:]
    cached = "--cached" in args
    args = [a for a in args if a != "--cached"]
    if args[:1] == ["run"] and len(args) >= 2:
        if args[2:] == ["-"]:                       # one input per line on stdin, one RESULT line each
            r = S.run_many(args[1], [l.strip() for l in sys.stdin if l.strip()], cached)
            print("\n".join(r["results"]))
            sys.exit(0 if not r["failed"] else 1)
        r = S.run(args[1], " ".join(args[2:]), cached=cached)
        print(r["result"])
        sys.exit(0 if r["ok"] else 1)
    if args[:1] == ["cache"] and len(args) >= 2:   # scout-mcp cache <url> [filter] [--archive]: downloads in the foreground
        src = "archive" if "--archive" in args else "auto"
        rest = [a for a in args[2:] if a != "--archive"]
        r = S.cache(args[1], rest[0] if rest else "", src, wait=True)
        print(json.dumps({k: r.get(k) for k in ("ok", "host", "listed", "already_cached", "to_fetch", "source", "error")}))
        sys.exit(0 if r.get("ok") else 1)
    if args[:1] == ["recipes"]:
        for r in S.recipes(args[1] if len(args) > 1 else ""):
            print(json.dumps({k: r[k] for k in ("name", "about", "status", "bash")}))
        return
    mcp.run()


if __name__ == "__main__":
    main()
