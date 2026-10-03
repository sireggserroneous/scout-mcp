"""Scout MCP server: the scout's tools over stdio."""
import asyncio

try:
    from mcp.server.mcpserver import MCPServer      # mcp >= 2
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

from . import scout as S

mcp = MCPServer("scout", instructions=(
    "Scout reaches good information on the open web as an honest bot. Start with scout(url) for a site, reach(url, want) "
    "for one page. When ok is false, read error.code and error.next_steps: each step is a concrete call. Teach Scout what "
    "worked with moves(action='propose', ...) so the next reach, for anyone sharing this memory, starts from the answer."))


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
    kinds: url_rewrite {pattern, repl} (regex on the full url) · listing_path {path} · detail_suffix {suffix}.
    scope: '*', a domain ('example.com' covers www.example.com) or a host regex. Propose only what you saw work."""
    if action == "propose":
        return await asyncio.to_thread(S.propose, kind, scope, spec or {}, "agent", note)
    if action == "retire":
        return {"ok": await asyncio.to_thread(S.retire, id)}
    return {"moves": await asyncio.to_thread(S.moves, kind or None, host or None, True)}


@mcp.tool()
async def recipe(host: str = "") -> dict:
    """What Scout has learned about a host: winning reader, reader order, routes (listing, detail_suffix, patterns),
    the moves that apply, recent failures. No host: recent failures everywhere and the whole register."""
    return await asyncio.to_thread(S.recipe, host)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
