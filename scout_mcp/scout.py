"""Scout — a web scout that learns, per host, which path through the maze ends at good information.

Every way of reaching a page is a move: a reader (plain fetch, headless browser, a renderer service), a url rewrite, a
listing path, a detail-page suffix. Each host keeps an ordered list of the moves that worked there, winner first and
recent failures last. Moves that are not built in live in a register as data, each scored by its Laplace win rate
(wins+1)/(tries+2), so a new move is worth one look and a move that never wins is pruned. The goal state is good
information: real content, not a wall, and carrying what the caller wanted.

When it cannot reach good data it says why, in a coded error with concrete next steps another agent can act on.
What a capable model works out about a site, it compiles into a recipe: a tested plan Scout runs itself, so a small
model's whole job is one call and one RESULT line.
"""
import gzip, io, json, os, re, sqlite3, threading, time
import urllib.error, urllib.parse, urllib.request, urllib.robotparser
from collections import Counter
from pathlib import Path

VERSION = "0.1.0"
HOME = "https://github.com/sireggserroneous/scout-mcp"
UA = os.environ.get("SCOUT_USER_AGENT") or f"ScoutMCP/{VERSION} (+{HOME})"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}

DB_PATH = Path(os.environ.get("SCOUT_DB") or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "scout-mcp/scout.db")
FIRECRAWL = os.environ.get("FIRECRAWL_URL", "").rstrip("/")
FIRECRAWL_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
CRAWL4AI = os.environ.get("CRAWL4AI_URL", "").rstrip("/")
CRAWL4AI_TOKEN = os.environ.get("CRAWL4AI_TOKEN", "")
MIN_INTERVAL = float(os.environ.get("SCOUT_MIN_INTERVAL", "1.0"))   # seconds between requests to one host, at least

ENGINE_ORDER = ["direct", "firecrawl", "crawl4ai", "browser"]       # cheapest first, when a host has taught us nothing
RENDERERS = {"firecrawl", "crawl4ai", "browser"}
TIMEOUT = {"direct": 20, "firecrawl": 45, "crawl4ai": 45, "browser": 45}
DEAD_TTL = 6 * 3600          # a move that failed on a host goes to the back for this long; a block today may be gone tomorrow
PRUNE_TRIES = 5              # a register move that lost its first five tries is retired
MIN_GOOD = 300               # fewer readable chars than this is not content
EXCERPT = 6000
MAX_BYTES = 25_000_000
MOVE_KINDS = ("url_rewrite", "listing_path", "detail_suffix")

_CHALLENGE = re.compile(r"just a moment|attention required|captcha|verify you are (?:a )?human|are you a (?:human|robot)|"
                        r"cf-browser-verification|enable javascript and cookies|unusual traffic|access denied|"
                        r"before you continue|cookie consent", re.I)
_LOGIN = re.compile(r"\b(sign in|log ?in|api key required|unauthori[sz]ed|subscribe to (?:continue|read))\b", re.I)
_ERROR_PAGE = re.compile(r"\b(404|page not found|not found|page (?:doesn't|does not) exist|no longer available)\b", re.I)
_JS_NEEDED = re.compile(r"enable javascript|requires javascript|please turn on javascript|you need to enable javascript", re.I)
_LISTING_WORDS = re.compile(r"(?i)/(products?|catalog(ue)?|shop|collections?|all-products|docs|documentation|blog|articles|library)(/|$|\.)")
_FILE = re.compile(r"(?i)\.(jpg|jpeg|png|gif|svg|webp|css|js|ico|woff2?|ttf|mp4|mp3|zip)(\?|$)")


class Status(Exception):
    """The site answered with an HTTP error status."""
    def __init__(self, status, retry_after=None, challenge=False):
        super().__init__(f"HTTP {status}" + (" (bot challenge page)" if challenge else ""))
        self.status, self.retry_after, self.challenge = int(status), retry_after, challenge


# ── memory: one SQLite file, per host routes + the moves register + the failure log ──────────────────────────────────
_lock = threading.Lock()
_ready = [False]


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    if not _ready[0]:
        with _lock:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS host (host TEXT PRIMARY KEY, engine TEXT, dead TEXT NOT NULL DEFAULT '{}',
                    route TEXT NOT NULL DEFAULT '{}', hits INTEGER NOT NULL DEFAULT 0, updated REAL);
                CREATE TABLE IF NOT EXISTS move (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '*',
                    spec TEXT NOT NULL, proposed_by TEXT, note TEXT, tries INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0, dead INTEGER NOT NULL DEFAULT 0, UNIQUE (kind, scope, spec));
                CREATE TABLE IF NOT EXISTS fail (id INTEGER PRIMARY KEY, host TEXT, url TEXT, want TEXT, code TEXT,
                    tried TEXT, at REAL);""")
            _seed(c)                  # insert-or-ignore: new seeds arrive on upgrade, nothing of yours is overwritten
            c.commit()
            _ready[0] = True
    return c


def _seed(c):
    """The recipe book shipped with Scout: seed moves, plus any per-host recipes."""
    book = json.loads((Path(__file__).parent / "recipes.json").read_text())
    for m in book.get("moves", []):
        c.execute("INSERT OR IGNORE INTO move (kind, scope, spec, proposed_by, note) VALUES (?, ?, ?, 'seed', ?)",
                  (m["kind"], m.get("scope", "*"), json.dumps(m["spec"], sort_keys=True), m.get("note")))
    _ensure_recipes(c)
    for r in book.get("recipes", []):
        c.execute("INSERT OR IGNORE INTO recipe (name, about, input, steps, examples, updated) VALUES (?, ?, ?, ?, ?, ?)",
                  (r["name"], r.get("about", ""), r.get("input", ""), json.dumps(r["steps"]), json.dumps(r.get("examples", [])), time.time()))
    for h, route in (book.get("hosts") or {}).items():
        c.execute("INSERT OR IGNORE INTO host (host, route, updated) VALUES (?, ?, ?)", (h, json.dumps(route), time.time()))


def memory(host):
    try:
        with _db() as c:
            r = c.execute("SELECT * FROM host WHERE host=?", (host,)).fetchone()
        if r:
            return {"engine": r["engine"], "dead": json.loads(r["dead"]), "route": json.loads(r["route"]), "hits": r["hits"]}
    except sqlite3.Error:
        pass
    return {"engine": None, "dead": {}, "route": {}, "hits": 0}


def _save(host, m):
    try:
        with _db() as c:
            c.execute("""INSERT INTO host (host, engine, dead, route, hits, updated) VALUES (?, ?, ?, ?, ?, ?)
                         ON CONFLICT(host) DO UPDATE SET engine=excluded.engine, dead=excluded.dead, route=excluded.route,
                         hits=excluded.hits, updated=excluded.updated""",
                      (host, m["engine"], json.dumps(m["dead"]), json.dumps(m["route"]), m["hits"], time.time()))
    except sqlite3.Error:
        pass                  # memory is a help, never a reason a fetch fails


def learn(host, key, value):
    m = memory(host)
    if m["route"].get(key) != value:
        m["route"][key] = value
        _save(host, m)


def _record(host, engine, ok):
    m = memory(host)
    if ok:
        m["hits"] += 1
        m["dead"].pop(engine, None)
        if m["engine"] != engine:
            m["engine"] = engine
    else:
        m["dead"][engine] = time.time()
        if m["engine"] == engine:
            m["engine"] = None
    _save(host, m)


def _order(names, winner, dead, render_first=False):
    """Winner first, readers that failed here recently last, never dropped — the ladder always has a next rung."""
    now = time.time()
    def rank(n):
        fresh_dead = now - (dead or {}).get(n, 0) < DEAD_TTL
        return (n != winner, fresh_dead, render_first and n not in RENDERERS, names.index(n))
    return sorted(names, key=rank)


def order(host):
    m = memory(host)
    return _order(ENGINE_ORDER, m["engine"], m["dead"], bool(m["route"].get("render")))


# ── the moves register ───────────────────────────────────────────────────────────────────────────────────────────────
def _score(tries, wins):
    return (wins + 1) / (tries + 2)       # Laplace: untried 0.5, 0-for-4 0.17, 3-for-3 0.8


def _scope_hit(scope, host):
    scope, host = (scope or "*").lower(), (host or "").lower()
    if scope == "*" or host == scope or host.endswith("." + scope):
        return True
    try:
        return bool(re.fullmatch(scope, host))
    except re.error:
        return False


def _valid(kind, spec):
    if kind not in MOVE_KINDS or not isinstance(spec, dict):
        return f"kind must be one of {', '.join(MOVE_KINDS)} and spec an object"
    if kind == "listing_path" and not str(spec.get("path", "")).startswith("/"):
        return "listing_path needs spec {'path': '/products'} (starts with /)"
    if kind == "detail_suffix" and not str(spec.get("suffix", "")).startswith(("/", "?", "#")):
        return "detail_suffix needs spec {'suffix': '/specifications'} (starts with / ? or #)"
    if kind == "url_rewrite":
        if not spec.get("pattern") or not isinstance(spec.get("repl"), str):
            return "url_rewrite needs spec {'pattern': <regex on the full url>, 'repl': <replacement, \\1 for groups>}"
        try:
            re.compile(spec["pattern"])
        except re.error as e:
            return f"url_rewrite pattern does not compile: {e}"
    return ""


def moves(kind=None, host=None, include_dead=False):
    try:
        with _db() as c:
            rows = c.execute("SELECT * FROM move WHERE (? IS NULL OR kind=?)" + ("" if include_dead else " AND dead=0"),
                             (kind, kind)).fetchall()
    except sqlite3.Error:
        return []
    out = [{"id": r["id"], "kind": r["kind"], "scope": r["scope"], "spec": json.loads(r["spec"]), "proposed_by": r["proposed_by"],
            "note": r["note"], "tries": r["tries"], "wins": r["wins"], "dead": bool(r["dead"]), "score": round(_score(r["tries"], r["wins"]), 3)}
           for r in rows if host is None or _scope_hit(r["scope"], host)]
    return sorted(out, key=lambda m: (-m["score"], m["id"]))


def propose(kind, scope, spec, by="", note=""):
    err = _valid(kind, spec)
    if err:
        return {"ok": False, "error": {"code": "BAD_MOVE", "message": err}}
    with _db() as c:
        c.execute("""INSERT INTO move (kind, scope, spec, proposed_by, note) VALUES (?, ?, ?, ?, ?)
                     ON CONFLICT(kind, scope, spec) DO UPDATE SET note=COALESCE(excluded.note, move.note), dead=0""",
                  (kind, (scope or "*").lower(), json.dumps(spec, sort_keys=True), (by or "")[:40], (note or "")[:300] or None))
        r = c.execute("SELECT id FROM move WHERE kind=? AND scope=? AND spec=?",
                      (kind, (scope or "*").lower(), json.dumps(spec, sort_keys=True))).fetchone()
    return {"ok": True, "id": r["id"], "kind": kind, "scope": (scope or "*").lower(), "spec": spec,
            "next": "Scout tries it on the next reach where its built-in moves fail; check its score with moves(action='list')."}


def move_result(move_id, ok):
    try:
        with _db() as c:
            c.execute("""UPDATE move SET tries=tries+1, wins=wins+?, dead=(tries+1 >= ? AND wins+? = 0) WHERE id=?""",
                      (int(ok), PRUNE_TRIES, int(ok), move_id))
    except sqlite3.Error:
        pass


def retire(move_id):
    with _db() as c:
        return c.execute("UPDATE move SET dead=1 WHERE id=?", (int(move_id),)).rowcount == 1


def _log_fail(host, url, want, code, tried):
    try:
        with _db() as c:
            c.execute("INSERT INTO fail (host, url, want, code, tried, at) VALUES (?, ?, ?, ?, ?, ?)",
                      (host, url[:500], want or None, code, json.dumps(tried)[:4000], time.time()))
            c.execute("DELETE FROM fail WHERE id < (SELECT max(id) FROM fail) - 2000")
    except sqlite3.Error:
        pass


def failures(host=None, limit=50):
    try:
        with _db() as c:
            rows = c.execute("SELECT host, url, want, code, at FROM fail WHERE (? IS NULL OR host=?) ORDER BY id DESC LIMIT ?",
                             (host, host, int(limit))).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


# ── manners: robots.txt, Crawl-delay, one request at a time per host ─────────────────────────────────────────────────
_ROBOTS, _LAST = {}, {}


def robots(url):
    """(parser, state) for the url's site, cached an hour. RFC 9309: a 4xx robots.txt means no rules, a 5xx means
    stay out until it answers."""
    p = urllib.parse.urlsplit(url)
    root = f"{p.scheme}://{p.netloc}"
    hit = _ROBOTS.get(root)
    if hit and time.time() - hit[0] < 3600:
        return hit[1], hit[2]
    rp, state = urllib.robotparser.RobotFileParser(root + "/robots.txt"), "ok"
    try:
        with urllib.request.urlopen(urllib.request.Request(root + "/robots.txt", headers=HEADERS), timeout=15) as r:
            rp.parse(r.read(500_000).decode("utf-8", "replace").splitlines())
    except urllib.error.HTTPError as e:
        state = "unreachable" if e.code >= 500 else "none"
        rp.parse(["User-agent: *", "Disallow: /"] if e.code >= 500 else [])
    except Exception:  # noqa: BLE001 — ponytail: no answer at all = no rules; the page fetch will report the network error
        state = "none"
        rp.parse([])
    _ROBOTS[root] = (time.time(), rp, state)
    return rp, state


def _polite(url):
    """Wait our turn on this host. Returns the robots verdict: (allowed, state)."""
    rp, state = robots(url)
    if not rp.can_fetch(UA, url):
        return False, state
    host = urllib.parse.urlsplit(url).netloc
    gap = max(MIN_INTERVAL, min(float(rp.crawl_delay(UA) or 0), 30.0))
    with _lock:
        wait = _LAST.get(host, 0) + gap - time.time()
        _LAST[host] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)
    return True, state


# ── readers ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _convert(raw, ctype, url):
    """Bytes -> {markdown, title, links, shell}. PDFs read by their text layer."""
    if "pdf" in (ctype or "").lower() or raw[:5] == b"%PDF-":
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(raw))
        md = "\n\n".join(t for t in ((pg.extract_text() or "").strip() for pg in rd.pages[:400]) if t)
        title = (rd.metadata.title if rd.metadata and rd.metadata.title else url.rsplit("/", 1)[-1])
        return {"markdown": md, "title": f"{title} ({len(rd.pages)} pages)", "links": [], "shell": False}
    from bs4 import BeautifulSoup
    from markdownify import markdownify
    soup = BeautifulSoup(raw, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(url, a["href"].split("#")[0].strip())
        if href.startswith(("http://", "https://")):
            links.append({"url": href, "text": " ".join(a.get_text(" ").split())[:80]})
    scripts = len(soup.find_all("script"))
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg"]):
        t.decompose()
    title = " ".join(soup.title.string.split()) if soup.title and soup.title.string else ""
    body = soup.find("main") or soup.find("article") or soup.body or soup
    md = re.sub(r"\n{3,}", "\n\n", markdownify(str(body), heading_style="ATX", bullets="-")).strip()
    shell = (len(md) < MIN_GOOD and scripts >= 3) or (len(md) < 2000 and bool(_JS_NEEDED.search(md)))
    return {"markdown": md, "title": title, "links": links, "shell": shell}


def _direct(url, timeout):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
            raw, ctype = r.read(MAX_BYTES), r.headers.get("Content-Type", "")
            final = r.geturl()
    except urllib.error.HTTPError as e:
        try:
            body = e.read(20_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        raise Status(e.code, e.headers.get("Retry-After") if e.headers else None, bool(_CHALLENGE.search(body))) from None
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return {**_convert(raw, ctype, final), "final_url": final}


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _firecrawl(url, timeout):
    h = {"Authorization": f"Bearer {FIRECRAWL_KEY}"} if FIRECRAWL_KEY else {}
    d = (_post(f"{FIRECRAWL}/v2/scrape", {"url": url, "formats": ["markdown", "links"], "onlyMainContent": True,
                                         "headers": {"User-Agent": UA}}, h, timeout).get("data") or {})
    meta = d.get("metadata") or {}
    if int(meta.get("statusCode") or 200) >= 400:
        raise Status(meta["statusCode"])
    md = d.get("markdown") or ""
    return {"markdown": md, "title": meta.get("title", ""), "links": [{"url": u, "text": ""} for u in d.get("links") or []],
            "shell": False, "final_url": meta.get("sourceURL") or url}


def _crawl4ai(url, timeout):
    h = {"Authorization": f"Bearer {CRAWL4AI_TOKEN}"} if CRAWL4AI_TOKEN else {}
    body = {"urls": [url], "browser_config": {"type": "BrowserConfig", "params": {"headless": True, "user_agent": UA}}}
    res = (_post(f"{CRAWL4AI}/crawl", body, h, timeout).get("results") or [{}])[0]
    if int(res.get("status_code") or 200) >= 400:
        raise Status(res["status_code"])
    md = res.get("markdown") or ""
    if isinstance(md, dict):
        md = md.get("fit_markdown") or md.get("raw_markdown") or ""
    links = [{"url": l["href"], "text": (l.get("text") or "")[:80]} for l in ((res.get("links") or {}).get("internal") or []) if l.get("href")]
    return {"markdown": md, "title": (res.get("metadata") or {}).get("title", ""), "links": links, "shell": False, "final_url": url}


def _browser(url, timeout):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            page = b.new_page(user_agent=UA)       # headless Chromium, still introducing itself as ScoutMCP
            resp = page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            if resp and resp.status >= 400:
                raise Status(resp.status)
            html, final = page.content(), page.url
        finally:
            b.close()
    return {**_convert(html.encode(), "text/html", final), "final_url": final}


def _have_browser():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


ENGINES = {"direct": _direct, "firecrawl": _firecrawl, "crawl4ai": _crawl4ai, "browser": _browser}


def readers():
    return [n for n, on in (("direct", True), ("firecrawl", bool(FIRECRAWL)), ("crawl4ai", bool(CRAWL4AI)),
                            ("browser", _have_browser())) if on]


# ── the goal test ────────────────────────────────────────────────────────────────────────────────────────────────────
def _carries(md, want):
    try:
        return bool(re.search(want, md, re.I))
    except re.error:
        return want.lower() in md.lower()


def judge(page, want=""):
    """ok | want_miss (real content, not what was asked) | js_shell | challenge | login_wall | error_page | thin | empty"""
    md, title = page.get("markdown") or "", page.get("title") or ""
    if page.get("shell"):
        return "js_shell"
    if not md.strip():
        return "empty"
    if _CHALLENGE.search(title + " " + md[:1500]) and len(md) < 5000:
        return "challenge"
    if len(md) < 8000 and _ERROR_PAGE.search(title + " " + md[:300]):
        return "error_page"
    if len(md) < 3000 and _LOGIN.search(title + " " + md[:600]):
        return "login_wall"
    if len(md) < MIN_GOOD:
        return "thin"
    if want and not _carries(md, want):
        return "want_miss"
    return "ok"


def _outcome(exc):
    if isinstance(exc, Status):
        s = exc.status
        if getattr(exc, "challenge", False) and s in (403, 503):
            return "challenge"
        return {429: "rate_limited", 404: "not_found", 410: "not_found"}.get(s) or (
            "refused" if s in (401, 403, 406, 409, 451) else "server_error" if s >= 500 else "http_error")
    text = f"{type(exc).__name__} {exc}"
    if "CERTIFICATE" in text.upper() or "SSL" in text.upper():
        return "tls"
    if isinstance(exc, ImportError):
        return "reader_missing"
    return "network"


def excerpt(md, want="", full=False):
    if full or len(md) <= EXCERPT:
        return md[:200_000]
    out, wins = md[:EXCERPT], []
    if want:
        try:
            for m in re.finditer(want, md[EXCERPT:], re.I):
                a = EXCERPT + m.start()
                wins.append("… " + md[max(0, a - 300):a + 300].strip() + " …")
                if len(wins) >= 6:
                    break
        except re.error:
            pass
    return out + ("\n\n" + "\n".join(wins) if wins else "") + f"\n\n[excerpt: {EXCERPT} of {len(md)} chars; pass full=true for all]"


# ── the maze ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _host(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def _try_readers(url, want, tried):
    """Walk the host's readers, best first. Returns (good page, reader) or (None, best real page or None)."""
    host, best = _host(url), None
    for name in order(host):
        if name not in readers():
            continue
        allowed, _ = _polite(url)
        if not allowed:
            tried.append({"url": url, "reader": name, "outcome": "robots_disallowed"})
            return None, best
        try:
            page = ENGINES[name](url, TIMEOUT[name])
        except Exception as e:  # noqa: BLE001
            out = _outcome(e)
            tried.append({"url": url, "reader": name, "outcome": out, "status": getattr(e, "status", None),
                          "retry_after": getattr(e, "retry_after", None), "detail": str(e)[:160]})
            if out not in ("rate_limited", "not_found", "http_error"):
                _record(host, name, False)     # a 404 or a 429 is the page or the host, not this reader's fault
            if out in ("rate_limited", "not_found"):
                break                 # another reader will not fix a 404, and a 429 means stop knocking
            continue
        verdict = judge(page, want)
        tried.append({"url": url, "reader": name, "outcome": verdict, "chars": len(page.get("markdown") or "")})
        real = verdict in ("ok", "want_miss")
        _record(host, name, real)     # did this reader get a real page; whether it held the want is a separate question
        if verdict == "js_shell" and name == "direct":
            learn(host, "render", True)
        if verdict == "ok":
            return page, name
        if real and best is None:
            best = {**page, "reader": name}
    return None, best


def _want_links(page, want, limit=3):
    if not want:
        return []
    words = [w for w in re.split(r"[^a-z0-9]+", want.lower()) if len(w) > 2]
    host = _host(page.get("final_url") or "")
    seen, out = set(), []
    for l in page.get("links") or []:
        hay = (l["url"] + " " + l.get("text", "")).lower()
        if _host(l["url"]) == host and l["url"] not in seen and any(w in hay for w in words) and not _FILE.search(l["url"]):
            seen.add(l["url"]); out.append(l["url"])
    return out[:limit]


def reach(url, want="", full=False, _depth=0, _links=False):
    """Reach good information at `url`. `want` is a regex the page must carry (a model number, 'price|\\$', a heading)."""
    if "://" not in url:
        url = "https://" + url
    host, tried = _host(url), []
    page, reader = _try_readers(url, want, tried)
    via = None
    if not page and _depth == 0 and not any(t["outcome"] in ("robots_disallowed", "rate_limited") for t in tried):
        best = reader if isinstance(reader, dict) else None
        hops = []                     # (url, move id or None, what it is) — the next rungs, best first
        for mv in moves("url_rewrite", host)[:2]:
            new = re.sub(mv["spec"]["pattern"], mv["spec"]["repl"], url, count=1)
            if new != url:
                hops.append((new, mv["id"], f"url_rewrite #{mv['id']}"))
        if best and want:             # real content, wrong page: the data is often one hop away
            learned = memory(host)["route"].get("detail_suffix")
            base = url.rstrip("/")
            if learned:
                hops.append((base + learned, None, f"learned detail_suffix {learned}"))
            hops += [(base + mv["spec"]["suffix"], mv["id"], f"detail_suffix #{mv['id']} {mv['spec']['suffix']}")
                     for mv in moves("detail_suffix", host)[:3] if mv["spec"]["suffix"] != learned]
            hops += [(u, None, "on-page link matching the want") for u in _want_links(best, want)]
        for new, mid, what in hops:
            sub = []
            got, _ = _try_readers(new, want, sub)
            tried += [{**t, "via": what} for t in sub]
            if mid:
                move_result(mid, bool(got))
            if got:
                page, reader, via = got, _, what
                if what.startswith("detail_suffix"):
                    learn(host, "detail_suffix", new[len(url.rstrip("/")):])
                break
            if any(t["outcome"] == "rate_limited" for t in sub):
                break
    if page:
        md = page.get("markdown") or ""
        return {"ok": True, "url": page.get("final_url") or url, "requested_url": url, "via": via,
                "reader": [t for t in tried if t["outcome"] == "ok"][-1]["reader"], "title": page.get("title", ""),
                "chars": len(md), "markdown": excerpt(md, want, full), "tried": tried, "learned": memory(host),
                **({"links": page.get("links") or []} if _links else {})}
    best = reader if isinstance(reader, dict) else None
    err = diagnose(url, want, tried, best)
    _log_fail(host, url, want, err["code"], tried)
    out = {"ok": False, "url": url, "error": err, "tried": tried, "readers_available": readers()}
    if best:
        out["closest"] = {"url": best.get("final_url") or url, "title": best.get("title", ""), "chars": len(best.get("markdown") or ""),
                          "markdown": excerpt(best.get("markdown") or "", "", False)[:2000]}
    return out


# ── actionable errors: what failed, why, and the next call that could fix it ─────────────────────────────────────────
_PRIORITY = ["robots_disallowed", "rate_limited", "challenge", "refused", "login_wall", "not_found", "error_page",
             "js_shell", "thin", "empty", "want_miss", "tls", "network", "server_error", "reader_missing", "http_error"]


def diagnose(url, want, tried, best=None):
    host = _host(url)
    p = urllib.parse.urlsplit(url)
    root = f"{p.scheme}://{p.netloc}"
    seen = {t["outcome"] for t in tried if "via" not in t} or {t["outcome"] for t in tried}   # a guessed sub-page's 404 is not the verdict
    first = next((o for o in _PRIORITY if o in seen), "unknown")
    status = next((t.get("status") for t in tried if t.get("status")), None)
    renderers = [r for r in readers() if r in RENDERERS]
    archive = (f"moves(action='propose', kind='url_rewrite', scope='{host}', "
               "spec={'pattern': '^(https?://.*)$', 'repl': 'https://web.archive.org/web/\\\\1'}, note='public archive copy') "
               f"then reach('{url}') again")
    sitemap = f"site_map('{root}') — pick a real url from the site's own list instead of guessing"
    install = ("install a rendering reader and retry: add '--with playwright' to Scout's uvx args and run "
               "'uvx playwright install chromium' once (or set FIRECRAWL_URL / CRAWL4AI_URL to a self-hosted renderer)")
    T = {
        "robots_disallowed": ("ROBOTS_DISALLOWED", f"{root}/robots.txt disallows this path for Scout's user agent ({UA}). Scout will not fetch it.",
            [f"Look for an official API, data feed or export: {root}/robots.txt often lists Sitemap lines; also try {root}/api or a developers page.",
             sitemap + " (only robots-allowed pages are listed)",
             "Ask the site owner to allow your user agent."]),
        "rate_limited": ("RATE_LIMITED", f"{host} answered 429 Too Many Requests" +
                         (f" with Retry-After {next((t.get('retry_after') for t in tried if t.get('retry_after')), '')}" if any(t.get('retry_after') for t in tried) else "") + ".",
            ["Wait (Retry-After seconds, or a few minutes) and call reach once more.",
             "Fetch fewer pages from this host per run, or raise SCOUT_MIN_INTERVAL (seconds between requests)."]),
        "challenge": ("CHALLENGE_WALL", f"{host} served a bot challenge or consent wall instead of the page.",
            ["Use the site's official API or data feed if it has one.",
             "Try a public archive copy as a scored move: " + archive,
             "Find the same document elsewhere (a distributor, a government mirror, the PDF itself) and reach that url."]),
        "refused": ("REFUSED", f"{host} answered {status or 'a refusal'} to Scout's user agent on every reader tried.",
            [f"{sitemap}; other paths on the host may be open.",
             "Use the site's official API or data feed if it has one.",
             "Try a public archive copy as a scored move: " + archive,
             "Ask the site owner to allow your user agent."]),
        "login_wall": ("LOGIN_REQUIRED", f"{url} asks for a login, subscription or API key.",
            ["Use the site's official API with your own credentials, outside Scout.",
             f"site_map('{root}', filter='pdf|download|docs|press') may show public copies of the same information."]),
        "not_found": ("NOT_FOUND", f"{url} does not exist (HTTP {status or 404}).",
            [sitemap, f"If the page moved: site_map('{root}', filter='<a distinctive word from the old path>')."]),
        "error_page": ("NOT_FOUND", f"{url} answered with an error page (title or first lines say not found).",
            [sitemap, f"If the page moved: site_map('{root}', filter='<a distinctive word from the old path>')."]),
        "js_shell": ("JS_SHELL", "The page is a JavaScript shell: the plain fetch got script and no content." +
                     (" The rendering readers did not get content either." if renderers else " No rendering reader is installed."),
            ([install] if not renderers else []) +
            [f"Many script-built sites load their data from JSON: site_map('{root}', filter='api|json|feed') may list it.",
             f"Propose a print or AMP view if the site has one: moves(action='propose', kind='url_rewrite', scope='{host}', "
             "spec={'pattern': '$', 'repl': '?print=1'})"]),
        "thin": ("THIN_CONTENT", f"Every reader came back with under {MIN_GOOD} readable characters.",
            ([install] if not renderers else []) + [sitemap]),
        "empty": ("THIN_CONTENT", "Every reader came back empty.", ([install] if not renderers else []) + [sitemap]),
        "want_miss": ("WANT_MISS", f"Reached real content at {url} ({len((best or {}).get('markdown', ''))} chars) but nothing matched want={want!r}, "
                      "and the sub-pages Scout tried did not either.",
            ["`want` is a regex matched case-insensitively against the page text: loosen it (e.g. 'weight|kg|lbs') or drop it to read the page.",
             "Read `closest.markdown` below: the words on the page may differ from the ones you asked for.",
             f"If this site keeps details on a sub-page, teach Scout: moves(action='propose', kind='detail_suffix', scope='{host}', spec={{'suffix': '/specifications'}})",
             f"{sitemap} — the data may live on a different page shape."]),
        "tls": ("TLS_ERROR", f"TLS to {host} failed: {next((t.get('detail') for t in tried if t['outcome'] == 'tls'), '')}",
            ["'unable to get local issuer certificate' usually means the server omits its intermediate certificate; "
             "install it in your trust store from the certificate's AIA url, or report it to the site. Scout never disables verification."]),
        "network": ("NETWORK", f"Could not reach {host}: {next((t.get('detail') for t in tried if t['outcome'] == 'network'), '')}",
            ["Check the url spelling and that the site is up (DNS, timeouts).", "Retry later if the site is slow."]),
        "server_error": ("SERVER_ERROR", f"{host} answered HTTP {status}.", ["Server errors are usually brief: retry later."]),
        "reader_missing": ("READER_MISSING", "A configured reader could not start.", [install]),
        "http_error": ("HTTP_ERROR", f"{host} answered HTTP {status}.", [sitemap]),
    }
    code, message, steps = T.get(first, ("UNKNOWN", "No reader produced good information.", [sitemap]))
    return {"code": code, "message": message, "next_steps": steps,
            "learned": f"logged under {host}; readers that failed there go to the back of its order for {DEAD_TTL // 3600} h. recipe('{host}') shows it"}


# ── the map: a site's own urls, their shapes, and where it keeps its listing ─────────────────────────────────────────
def _pattern(u):
    """A url's shape: digits -> N, the last segment -> *, so /product/RB2011 and /product/hAP-ac3 are one pattern."""
    parts = [re.sub(r"\d+", "N", x) for x in (urllib.parse.urlsplit(u).path.rstrip("/") or "/").split("/")]
    if len(parts) > 2:
        parts[-1] = "*"
    return "/".join(parts)[:200]


def _get(url, timeout=20):
    allowed, _ = _polite(url)
    if not allowed:
        return None
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
        raw = r.read(MAX_BYTES)
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def _sitemap_urls(sm, seen, cap, depth=0):
    if sm in seen or len(seen) > 25 or depth > 2:
        return []
    seen.add(sm)
    try:
        body = (_get(sm) or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    locs = [x.strip() for x in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", body, re.S)]
    if "<sitemapindex" in body:
        out = []
        for child in locs:
            out += _sitemap_urls(child, seen, cap, depth + 1)
            if len(out) >= cap:
                break
        return out
    return locs


def site_map(url, filter="", limit=500, walk=20):
    """The site's real urls: robots.txt sitemaps first, then a polite link walk when they are thin."""
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    root, host = f"{p.scheme}://{p.netloc}", p.netloc.lower()
    rp, state = robots(url)
    declared = rp.site_maps() or [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    seen, urls = set(), []
    for sm in declared[:5]:
        urls += _sitemap_urls(sm, seen, 20000)
    how = [f"sitemaps: {len(urls)} urls from {len(seen)} file(s)"]
    on = lambda u: urllib.parse.urlsplit(u).netloc.lower().removeprefix("www.") == host.removeprefix("www.")
    urls = [u for u in dict.fromkeys(urls) if on(u) and rp.can_fetch(UA, u)]
    if len(urls) < 50 and walk:      # thin or no sitemap: walk the site's own links, breadth first, politely
        frontier, opened = [url, root + "/"], set()
        while frontier and len(opened) < walk:
            u = frontier.pop(0)
            if u in opened or not rp.can_fetch(UA, u):
                continue
            opened.add(u)
            try:
                page = _direct(u, 20)
            except Exception:  # noqa: BLE001
                continue
            for l in page["links"]:
                v = l["url"]
                if on(v) and not _FILE.search(v) and v not in urls:
                    urls.append(v); frontier.append(v)
        how.append(f"link walk: opened {len(opened)} page(s)")
    rx = re.compile(filter, re.I) if filter else None
    hits = [u for u in urls if not rx or rx.search(u)]
    top = Counter(_pattern(u) for u in hits).most_common(15)
    out = {"ok": bool(hits), "site": root, "count": len(hits), "urls": hits[:int(limit)], "robots": state, "how": "; ".join(how),
           "patterns": [{"pattern": pt, "count": n, "samples": [u for u in hits if _pattern(u) == pt][:3]} for pt, n in top]}
    if not hits:
        out["error"] = {"code": "NO_URLS" if not urls else "FILTER_MATCHED_NOTHING",
                        "message": (f"No sitemap and the link walk found no on-site links from {url}." if not urls
                                    else f"{len(urls)} urls found, none match filter={filter!r}."),
                        "next_steps": ([f"reach('{url}') to see what the page returns (a JS shell has no links to walk).",
                                        f"Try the site's listing: listing('{root}')."] if not urls else
                                       [f"Loosen the filter, or call site_map('{root}') without one and read `patterns`."])}
    else:
        learn(host, "patterns", [x["pattern"] for x in out["patterns"][:6]])
    return out


def listing(url, urls=None):
    """Where the site keeps its index of things: the learned route, the sitemap's listing-shaped urls, then the
    register's listing_path moves, each scored. A listing is a page with 10+ on-site links."""
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    root, host = f"{p.scheme}://{p.netloc}", p.netloc.lower()
    learned = memory(host)["route"].get("listing")
    if learned:
        return {"ok": True, "url": learned, "source": "learned"}

    def is_listing(u):
        try:
            if not _polite(u)[0]:
                return False
            pg = _direct(u, 20)
        except Exception:  # noqa: BLE001
            return False
        return judge(pg) == "ok" and sum(_host(l["url"]) == host for l in pg["links"]) >= 10

    cands = sorted({u for u in (urls or []) if _LISTING_WORDS.search(urllib.parse.urlsplit(u).path)},
                   key=lambda u: (urllib.parse.urlsplit(u).path.rstrip("/").count("/"), len(u)))
    for u in cands[:3]:
        if is_listing(u):
            learn(host, "listing", u)
            return {"ok": True, "url": u, "source": "sitemap"}
    for mv in moves("listing_path", host)[:6]:
        u = root + mv["spec"]["path"]
        won = is_listing(u)
        move_result(mv["id"], won)
        if won:
            learn(host, "listing", u)
            return {"ok": True, "url": u, "source": f"listing_path #{mv['id']}"}
    return {"ok": False, "candidates": cands[:8], "error": {"code": "NO_LISTING",
            "message": f"No listing page found on {host}: no learned route, no listing-shaped sitemap url, and the top listing_path moves did not render 10+ links.",
            "next_steps": [f"site_map('{root}') and read `patterns`: the busiest shape is usually the item pages.",
                           f"If you know the path, teach Scout: moves(action='propose', kind='listing_path', scope='{host}', spec={{'path': '/your/path'}})"]}}


def scout(url, want="", full=False):
    """The whole maze for one url: reach the page, map the site, find its listing, and report what was learned."""
    if "://" not in url:
        url = "https://" + url
    page = reach(url, want, full)
    sm = site_map(url, limit=50)
    lst = listing(url, sm.get("urls"))
    host = _host(url)
    return {"ok": page["ok"], "page": page,
            "terrain": {"urls": sm["count"], "patterns": sm["patterns"][:10], "how": sm["how"], "robots": sm["robots"],
                        "listing": lst.get("url"), "sample_urls": sm["urls"][:20], "map_error": sm.get("error")},
            "recipe": memory(host)}


def recipe(host=""):
    if not host:
        return {"recent_failures": failures(limit=50), "moves": moves(include_dead=True), "readers": readers(), "db": str(DB_PATH)}
    host = _host(host if "://" in host else "https://" + host)
    return {"host": host, **memory(host), "order": order(host), "moves": moves(host=host), "recent_failures": failures(host, 10)}


# ── recipes: a big model's site breakdown, compiled into one call a small model can make ────────────────────────────
# Working out a site takes judgment: which url shape, which sub-page, which reader, which words carry the data. A
# capable model does that once, with scout/site_map/reach, and compiles what it learned into a recipe: a short plan
# of deterministic steps Scout runs itself. The recipe is tested on real examples before it is saved. After that the
# whole job of a small model (Qwen 4B and down) is ONE instruction: call run(recipe, input) and copy the RESULT line.
# Every branch lives here, in code, where it can be tested — none of it is left to the small model.
STEP_OPS = {"input": "[[regex, replacement], ...] rewrites the input before use, plus optional 'lower': true",
            "reach": "url template, may use {input} and {url}; optional 'want' regex",
            "find": "regex; the first link on the current page whose url or text matches becomes {url}",
            "map": "site url; optional 'filter' regex; optional 'same': [[regex, repl], ...] keeps the url whose last "
                   "segment equals the input once both are lowercased and rewritten by the rules; last step = the url "
                   "list, else the first match becomes {url}",
            "extract": "{field: regex}; group 1 (or the whole match) from the current page; every field must be found"}
_NAME = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}/[a-z0-9-]+$")


def _ensure_recipes(c):
    c.execute("""CREATE TABLE IF NOT EXISTS recipe (name TEXT PRIMARY KEY, about TEXT, input TEXT, steps TEXT NOT NULL,
                 examples TEXT, tries INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0, last_code TEXT,
                 last_input TEXT, updated REAL)""")


def _fill(t, inp, url, rx=False):
    if rx:
        return t.replace("{input}", re.escape(inp)).replace("{url}", re.escape(url or ""))
    return t.replace("{input}", urllib.parse.quote(inp, safe="")).replace("{url}", url or "")


def _same(v, rules):
    v = urllib.parse.unquote(v).lower()
    for pat, rep in rules:
        v = re.sub(pat, rep, v)
    return v


def _clean(v):
    return " ".join(re.sub(r"[*_`|]+", " ", v or "").split())[:200]


def _line(ok, name, inp, body):
    return f"RESULT: {'ok' if ok else 'fail'} | {name} | {inp or '-'} | {body}"


def _check_steps(steps):
    if not isinstance(steps, list) or not steps:
        return "steps must be a non-empty list"
    for i, st in enumerate(steps, 1):
        op = next(iter(st), None) if isinstance(st, dict) else None
        if op not in STEP_OPS:
            return f"step {i}: the first key must be one of {', '.join(STEP_OPS)}"
        rxs = list(st["extract"].values()) if op == "extract" and isinstance(st["extract"], dict) else \
              [st[op]] if op == "find" else [st.get("want", ""), st.get("filter", "")]
        if op == "map" and st.get("same") is not None:
            rxs = [p[0] for p in st["same"]] if isinstance(st["same"], list) and all(isinstance(p, list) and len(p) == 2 for p in st["same"]) else ["("]
        if op == "input":
            rxs = [p[0] for p in st["input"]] if isinstance(st["input"], list) and all(isinstance(p, list) and len(p) == 2 for p in st["input"]) else ["("]
        if op == "extract" and not (isinstance(st["extract"], dict) and st["extract"]):
            return f"step {i}: extract needs {{field: regex}}"
        for rx in rxs:
            try:
                re.compile(_fill(str(rx or ""), "x", "https://x", True))
            except re.error as e:
                return f"step {i}: regex {rx!r} does not compile: {e}"
    if next((next(iter(st)) for st in steps if next(iter(st)) != "input"), None) not in ("reach", "map"):
        return "the first step after any input step must be reach or map: a recipe starts from a url"
    return ""


def run(name, input="", recipe=None):
    """Run a compiled recipe. Returns {ok, result: one RESULT line, output, fix (on failure, for whoever recompiles)}."""
    rc = recipe or recipe_get(name)
    if not rc:
        return {"ok": False, "result": _line(False, name, input, "NO_RECIPE"), "fix": "recipes() lists the compiled recipes"}
    name, url, page, out = rc["name"], "", None, {}

    def fail(code, i, fix):
        line = _line(False, name, raw, f"{code} at step {i}")
        if not recipe:
            _score_recipe(name, False, code, raw)
        return {"ok": False, "result": line, "code": code, "step": i, "fix": fix, "url": url}

    raw = input
    for i, st in enumerate(rc["steps"], 1):
        op = next(iter(st))
        if op == "input":
            input = input.lower() if st.get("lower") else input
            for pat, rep in st["input"]:
                input = re.sub(pat, rep, input)
        elif op == "reach":
            r = reach(_fill(st["reach"], input, url), _fill(st.get("want", ""), input, url, True), full=True, _links=True)
            if not r["ok"]:
                return fail(r["error"]["code"], i, r["error"]["next_steps"][0])
            url, page = r["url"], r
            out = {"url": url, "title": r.get("title", "")[:80]}
        elif op == "find":
            rx = re.compile(_fill(st["find"], input, url, True), re.I)
            hit = next((l["url"] for l in (page or {}).get("links", []) if rx.search(l["url"]) or rx.search(l.get("text", ""))), None)
            if not hit:
                return fail("FIND_MISS", i, f"no link on {url} matches {st['find']!r}; the page may have changed: recompile")
            url = hit
        elif op == "map":
            sm = site_map(_fill(st["map"], input, url), _fill(st.get("filter", ""), input, url, True), int(st.get("limit", 20000)))
            if not sm["ok"]:
                return fail(sm["error"]["code"], i, sm["error"]["next_steps"][0])
            if st.get("same"):
                key = lambda v: _same(v, st["same"])
                sm["urls"] = [u for u in sm["urls"] if key(urllib.parse.urlsplit(u).path.rstrip("/").rsplit("/", 1)[-1]) == key(input)]
                sm["count"] = len(sm["urls"])
                if not sm["urls"]:
                    return fail("NO_MATCH", i, f"no url on the site matches {raw!r}: check the spelling, or the site does not list it")
            if i == len(rc["steps"]):
                out = {"count": sm["count"], "first": sm["urls"][0], "urls": sm["urls"]}
            url = sm["urls"][0]
        elif op == "extract":
            md = re.sub(r"\[excerpt: .*?\]$", "", (page or {}).get("markdown", ""))
            got, missing = {}, []
            for field, rx in st["extract"].items():
                m = re.search(_fill(rx, input, url, True), md, re.I | re.M)
                val = _clean(m.group(1) if m and m.groups() else m.group(0) if m else "")
                (got.__setitem__(field, val) if val else missing.append(field))
            if missing:
                return fail("EXTRACT_MISS", i, f"{', '.join(missing)} not found on {url}; the page layout may have changed: recompile")
            out.update(got)
    if not recipe:
        _score_recipe(name, True, None, raw)
    body = "; ".join(f"{k}={v}" for k, v in out.items() if k != "urls")
    return {"ok": True, "result": _line(True, name, raw, body), "output": out}


def _score_recipe(name, ok, code, inp):
    try:
        with _db() as c:
            _ensure_recipes(c)
            c.execute("UPDATE recipe SET tries=tries+1, wins=wins+?, last_code=?, last_input=? WHERE name=?",
                      (int(ok), code, inp, name))
    except sqlite3.Error:
        pass


def recipe_get(name):
    with _db() as c:
        _ensure_recipes(c)
        r = c.execute("SELECT * FROM recipe WHERE name=?", (name,)).fetchone()
    return {**dict(r), "steps": json.loads(r["steps"]), "examples": json.loads(r["examples"] or "[]")} if r else None


def card(name, input_name="", example=""):
    """The one instruction a small model gets. The orchestrator fills the real input; the model resolves nothing."""
    arg = f' and input="{example}"' if input_name else ""
    return {"card": f'Call the tool run with recipe="{name}"{arg}. Reply with the RESULT line only.',
            "bash": f"scout-mcp run {name}" + (f' "{example}"' if input_name else "") + "   # prints one RESULT line"}


def compile_recipe(name, steps, examples=None, about="", input_name=""):
    """Test a recipe on every example and save it only if all pass. Returns the tests and the small-model card."""
    if not _NAME.match(name or ""):
        return {"ok": False, "error": {"code": "BAD_NAME", "message": "name is '<host>/<slug>', e.g. 'mikrotik.com/specs'"}}
    err = _check_steps(steps)
    if err:
        return {"ok": False, "error": {"code": "BAD_STEPS", "message": err, "ops": STEP_OPS}}
    examples = [str(e) for e in (examples or [])] or ([""] if not input_name else [])
    if input_name and len(examples) < 2:
        return {"ok": False, "error": {"code": "NEED_EXAMPLES", "message": "give at least 2 example inputs: one success can be luck"}}
    rc = {"name": name, "steps": steps}
    tests = [run(name, e, recipe=rc) for e in examples]
    if not all(t["ok"] for t in tests):
        bad = next(t for t in tests if not t["ok"])
        return {"ok": False, "saved": False, "tests": [t["result"] for t in tests],
                "error": {"code": "TEST_FAILED", "message": bad["result"], "next_steps": [bad["fix"],
                          "fix that step and compile again; reach/site_map the failing example to see the page"]}}
    with _db() as c:
        _ensure_recipes(c)
        c.execute("""INSERT INTO recipe (name, about, input, steps, examples, updated) VALUES (?, ?, ?, ?, ?, ?)
                     ON CONFLICT(name) DO UPDATE SET about=excluded.about, input=excluded.input, steps=excluded.steps,
                     examples=excluded.examples, tries=0, wins=0, last_code=NULL, updated=excluded.updated""",
                  (name, about, input_name, json.dumps(steps), json.dumps(examples), time.time()))
    return {"ok": True, "saved": name, "tests": [t["result"] for t in tests], **card(name, input_name, examples[0])}


def recipes(host=""):
    with _db() as c:
        _ensure_recipes(c)
        rows = c.execute("SELECT * FROM recipe WHERE ? = '' OR name LIKE ? ORDER BY name", (host, f"{host}/%")).fetchall()
    return [{"name": r["name"], "about": r["about"], "input": r["input"], "score": round(_score(r["tries"], r["wins"]), 3),
             "runs": r["tries"], "status": "ok" if not r["last_code"] else f"last run failed: {r['last_code']} on {r['last_input']!r}: recompile",
             **card(r["name"], r["input"], (json.loads(r["examples"] or "[]") or [""])[0])} for r in rows]


def demo():
    """Pure self-checks: no network, a throwaway database."""
    assert _pattern("https://x.com/product/RB2011-UiAS") == "/product/*" and _pattern("https://x.com/a/12/b") == "/a/N/*"
    assert _score(0, 0) == 0.5 and _score(4, 0) < 0.5 < _score(3, 3)
    assert _scope_hit("*", "a.com") and _scope_hit("se.com", "www.se.com") and not _scope_hit("se.com", "sloan.com")
    assert _valid("listing_path", {"path": "/p"}) == "" and _valid("listing_path", {"path": "p"})
    assert _valid("url_rewrite", {"pattern": "(", "repl": ""}) and _valid("identity", {"headers": {}})
    now = time.time()
    assert _order(ENGINE_ORDER, "crawl4ai", {"firecrawl": now}) == ["crawl4ai", "direct", "browser", "firecrawl"]
    assert _order(ENGINE_ORDER, None, {}, render_first=True)[0] == "firecrawl"
    assert judge({"markdown": "x" * 400}) == "ok" and judge({"markdown": "x" * 400}, "price") == "want_miss"
    assert judge({"markdown": "Just a moment... checking your browser " + "x" * 400}) == "challenge"
    assert judge({"markdown": "", "shell": True}) == "js_shell" and judge({"title": "404 Not Found", "markdown": "x" * 400}) == "error_page"
    assert _outcome(Status(403, challenge=True)) == "challenge"
    assert _outcome(Status(429)) == "rate_limited" and _outcome(Status(403)) == "refused" and _outcome(Status(503)) == "server_error"
    for o in _PRIORITY:
        e = diagnose("https://a.com/x", "w", [{"outcome": o, "status": 403}], {"markdown": "x" * 500})
        assert e["code"] and e["next_steps"], o
    global DB_PATH
    import tempfile
    DB_PATH, _ready[0] = Path(tempfile.mkdtemp()) / "t.db", False
    assert any(m["kind"] == "listing_path" for m in moves())
    mid = propose("detail_suffix", "a.com", {"suffix": "/x"})["id"]
    for _ in range(PRUNE_TRIES):
        move_result(mid, False)
    assert not any(m["id"] == mid for m in moves("detail_suffix", "a.com"))
    _record("a.com", "firecrawl", True); _record("a.com", "direct", False)
    assert order("a.com")[0] == "firecrawl" and order("a.com")[-1] == "direct"
    assert _check_steps([{"reach": "https://a.com/p/{input}"}, {"extract": {"price": r"\$([\d.]+)"}}]) == ""
    assert _check_steps([{"input": [["-", "_"]], "lower": True}, {"reach": "https://a.com/{input}"}]) == ""
    assert _check_steps([{"input": "nope"}, {"reach": "https://a.com/{input}"}])
    assert _check_steps([{"extract": {"x": "y"}}]) and _check_steps([{"nope": 1}]) and _check_steps([{"find": "("}])
    assert _fill("https://a.com/p/{input}", "a b/c", "") == "https://a.com/p/a%20b%2Fc" and _fill("{input}", "a.b", "", True) == "a\\.b"
    assert _same("CRS354-48G-4S+2Q+RM", [["plus", ""], ["[^a-z0-9]", ""]]) == _same("crs354_48g_4splus2qplusrm", [["plus", ""], ["[^a-z0-9]", ""]])
    assert _line(True, "a.com/x", "Z", "k=v") == "RESULT: ok | a.com/x | Z | k=v"
    assert compile_recipe("bad name", [])["error"]["code"] == "BAD_NAME"
    assert compile_recipe("a.com/x", [{"reach": "https://a.com/{input}"}], ["one"], input_name="m")["error"]["code"] == "NEED_EXAMPLES"
    assert run("a.com/none")["result"] == "RESULT: fail | a.com/none | - | NO_RECIPE"
    print("scout: ok")


if __name__ == "__main__":
    demo()
