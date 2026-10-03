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
async def reach(url: str, want: str = "", full: bool = False) -> dict:
    """Reach one page and judge whether it is good information. Tries the host's learned readers winner-first, then
    scored url rewrites and detail sub-pages. Returns ok, markdown (excerpt unless full=true), the trail `tried`, and on
    failure an `error` {code, message, next_steps}."""
    return await asyncio.to_thread(S.reach, url, want, full)


@mcp.tool()
async def site_map(url: str, filter: str = "", limit: int = 500) -> dict:
    """A site's real urls (robots.txt sitemaps, then a polite link walk) with their url patterns. `filter` is a regex.
    Use it instead of guessing paths."""
    return await asyncio.to_thread(S.site_map, url, filter, limit)


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
      {"extract": {"field": "regex with one group"}}   every field must be found
    Prefer map over a guessed url template: sites are inconsistent about case and slugs."""
    return await asyncio.to_thread(S.compile_recipe, name, steps, examples, about, input_name)


@mcp.tool()
async def run(recipe: str, input: str = "") -> dict:
    """Run a compiled recipe. Reply with the RESULT line."""
    r = await asyncio.to_thread(S.run, recipe, input)
    return {k: r[k] for k in ("result", "fix") if k in r}


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
    if args[:1] == ["run"] and len(args) >= 2:
        r = S.run(args[1], " ".join(args[2:]))
        print(r["result"])
        sys.exit(0 if r["ok"] else 1)
    if args[:1] == ["recipes"]:
        for r in S.recipes(args[1] if len(args) > 1 else ""):
            print(json.dumps({k: r[k] for k in ("name", "about", "status", "bash")}))
        return
    mcp.run()


if __name__ == "__main__":
    main()
