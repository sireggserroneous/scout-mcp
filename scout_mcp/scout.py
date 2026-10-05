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
import gzip, http.client, io, json, os, re, sqlite3, threading, time
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
# The Wayback Machine: a public archive of the site, read when the site itself cannot or should not be asked (Depot,
# 2026-10-03: 1,543 of AMD's product pages, newest a day old, while amd.com blocked us). It never becomes a host's
# winning reader, so live reading resumes when the site allows it; robots.txt still binds what Scout reads from it.
ARCHIVE = os.environ.get("SCOUT_ARCHIVE", "https://web.archive.org").rstrip("/")
ARCHIVE_PACE = 5.0           # the Wayback Machine allows ~15 copies a minute; 2 s got Scout and Depot refused (2026-10-03/04)
ARCHIVE_HOLD = 900           # the archive refused us: leave it alone 15 minutes (doubling while it keeps refusing)
ARCHIVE_WHEN = {"blocked", "rate_limited", "silent_refusal", "challenge", "refused", "login_wall", "network", "server_error"}
MIRROR_DAYS = float(os.environ.get("SCOUT_MIRROR_DAYS", "1"))   # a good page is re-read from disk this long: download once
RENDERERS = {"firecrawl", "crawl4ai", "browser"}
TIMEOUT = {"direct": 20, "firecrawl": 45, "crawl4ai": 45, "browser": 45, "archive": 60}
DEAD_TTL = 6 * 3600          # a move that failed on a host goes to the back for this long; a block today may be gone tomorrow
PRUNE_TRIES = 5              # a register move that lost its first five tries is retired
MIN_GOOD = 300               # fewer readable chars than this is not content
EXCERPT = 6000
MAX_BYTES = 25_000_000
MOVE_KINDS = ("url_rewrite", "listing_path", "detail_suffix", "locale_prefix", "listing_root")
RANKED_ONLY = ("locale_prefix", "listing_root")   # parts that compose: ranked, never retired (a miss may be the other part's)
_LOCALE = re.compile(r"^/((?:[a-z]{2}[-_])?[a-z]{2}(?:[-_][a-z]{2})?)(/(?:[a-z]{2}[-_])?[a-z]{2}(?:[-_][a-z]{2})?)?(?=/|$)", re.I)

_CHALLENGE = re.compile(r"just a moment|attention required|captcha|verify you are (?:a )?human|are you a (?:human|robot)|"
                        r"cf-browser-verification|enable javascript and cookies|unusual traffic|access denied|"
                        r"before you continue|cookie consent|"
                        r"performing security verification|protect against malicious bots|verif(?:y|ies) you are", re.I)   # Cloudflare's 2026 wall
_LOGIN = re.compile(r"\b(sign in|log ?in|api key required|unauthori[sz]ed|subscribe to (?:continue|read))\b", re.I)
_ERROR_PAGE = re.compile(r"\b(404|page not found|not found|page (?:doesn't|does not) exist|no longer available)\b", re.I)
# A block page needs BOTH halves: a refusal, and something about the visitor. A state revisor's site served "Blocked <our
# IP> ... for assistance EMAIL: <its webmaster>" with status 200, so a status-only check never saw it and the crawler kept
# going at full speed (2026-10-03). Two halves keep a page that merely says "blocked" from tripping it.
_BLOCK_A = re.compile(r"(?i)\b(?:blocked|access denied|request (?:was )?(?:rejected|blocked)|too many requests|forbidden|unusual traffic)\b")
# an edge's incident reference is a visitor clue too: Akamai's "Access Denied ... Reference #18.5b2d1102..." names no IP,
# and amd.com served exactly that to everything after four full re-listings in an hour (Depot, 2026-10-03)
_BLOCK_B = re.compile(r"(?i)your (?:public )?ip(?: address)?|\b\d{1,3}(?:\.\d{1,3}){3}\b|captcha|are you a (?:robot|human)|try again later|"
                      r"reference\s*#\s*\d+\.[0-9a-f.]+|ray id|incident id|support id")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HOLD_S = 1800                # an automatic hold after a block: probe once when it ends, double it if still blocked
_JS_NEEDED = re.compile(r"enable javascript|requires javascript|please turn on javascript|you need to enable javascript", re.I)
_LISTING_WORDS = re.compile(r"(?i)/(products?|catalog(ue)?|shop|collections?|all-products|docs|documentation|blog|articles|library)(/|$|\.)")
# Editorial sections are never items: amd.com's scout picked /blogs/ and /newsroom/ as its product pattern and a dry run
# "proved" 689 blog posts as products (Depot, 2026-10-03). Flagged and sorted last wherever Scout ranks url shapes.
# ...nor are partner, request and contact pages (Depot landed AMD partner pages as products, 2026-10-03). Support stays out of
# this list: Cisco's product pages live under /support/.
_EDITORIAL = re.compile(r"(?i)/(blogs?|news(room)?|press(-?releases?|room)?|events?|careers?|jobs|investors?|about(-us)?|media|"
                        r"webinars?|videos?|podcasts?|stories|insights|articles|authors?|tags?|partners?|partner-program|"
                        r"request(-a)?(-(quote|demo|info|sample))?|contact(-us)?|where-to-buy|dealer-locator|find-a-(dealer|partner))(/|$)")
LISTING_TTL = 12 * 3600       # a host's listing is reused this long: re-listing a whole site on every run got amd.com to block
_CATALOG = re.compile(r"(?i)/(products?|catalog(ue)?|shop|store|collections?|parts?|items?)(/|$|\.)")
# A stall or a reset from a host that resolves is its edge refusing this client quietly, not a network fault: amd.com's
# edge timed the plain fetch out and reset the browser's HTTP/2 stream (2026-10-03). Scout said "check the url spelling".
_STALL = re.compile(r"timed out|ERR_HTTP2_PROTOCOL_ERROR|ERR_CONNECTION_RESET|ERR_EMPTY_RESPONSE|ERR_CONNECTION_CLOSED|"
                    r"Connection reset|RemoteDisconnected|EOF occurred|Connection aborted", re.I)
_DNS = {}
_FILE = re.compile(r"(?i)\.(jpg|jpeg|png|gif|svg|webp|css|js|ico|woff2?|ttf|mp4|mp3|zip)(\?|$)")


class Status(Exception):
    """The site answered with an HTTP error status."""
    def __init__(self, status, retry_after=None, challenge=False, blocked=False, body=""):
        super().__init__(f"HTTP {status}" + (" (block page)" if blocked else " (bot challenge page)" if challenge else ""))
        self.status, self.retry_after, self.challenge, self.blocked, self.body = int(status), retry_after, challenge, blocked, body


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
        c.execute("INSERT OR IGNORE INTO move (kind, scope, spec, proposed_by, note, tries, wins) VALUES (?, ?, ?, 'seed', ?, ?, ?)",
                  (m["kind"], m.get("scope", "*"), json.dumps(m["spec"], sort_keys=True), m.get("note"),
                   int(m.get("tries", 0)), int(m.get("wins", 0))))
    _ensure_recipes(c)
    for r in book.get("recipes", []):
        c.execute("INSERT OR IGNORE INTO recipe (name, about, input, steps, examples, updated) VALUES (?, ?, ?, ?, ?, ?)",
                  (r["name"], r.get("about", ""), r.get("input", ""), json.dumps(r["steps"]), json.dumps(r.get("examples", [])), time.time()))
    for h, route in (book.get("hosts") or {}).items():
        r = c.execute("SELECT route FROM host WHERE host=?", (h,)).fetchone()
        if not r:
            c.execute("INSERT INTO host (host, route, updated) VALUES (?, ?, ?)", (h, json.dumps(route), time.time()))
            continue
        have = json.loads(r["route"])                 # an upgrade adds what the book knows; what you learned stays
        new = {**route, **have}
        if new != have:
            c.execute("UPDATE host SET route=? WHERE host=?", (json.dumps(new), h))


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


def hold(host, reason="", lift=False, min_interval=0.0, until=None, contact=None, by="agent"):
    """Keep everyone who shares this memory off a host. An automatic hold (Scout's, after a block or a 429) has an
    `until`: the first reach after it is the probe. A hold an agent or a person sets has none: it stays until lifted."""
    host = _host(host if "://" in host else "https://" + host)
    m = memory(host)
    if lift:
        m["route"].pop("hold", None)
    else:
        m["route"]["hold"] = {"reason": reason[:300], "since": time.time(), "until": until, "by": by}
    if contact:
        m["route"]["contact"] = contact       # the site's own address for this, kept after the hold is lifted
    if min_interval:
        m["route"]["min_interval"] = max(float(m["route"].get("min_interval") or 0), float(min_interval))
    _save(host, m)
    return {"ok": True, "host": host, "hold": m["route"].get("hold"), "min_interval": m["route"].get("min_interval")}


def _held(host):
    h = memory(host)["route"].get("hold")
    if not h or (h.get("until") and time.time() >= h["until"]):
        return None           # no hold, or an automatic one that has ended: the next reach is the probe
    return h


def _hold_contact(host, text=""):
    found = _EMAIL.search(text or "")
    return memory(host)["route"].get("contact") or (found.group(0) if found else None)


def _throttled(host, kind, retry_after, text):
    """A block page or a 429: hold the host (doubling if the probe after a previous hold was refused too), slow its pace
    for good, and keep the contact the block page names."""
    prev = memory(host)["route"].get("hold") or {}
    try:
        secs = float(retry_after or 0)
    except ValueError:                       # Retry-After may be an HTTP date: wait as long as the site asks, every time
        try:
            import email.utils
            secs = max(0.0, email.utils.parsedate_to_datetime(retry_after).timestamp() - time.time())
        except (TypeError, ValueError):
            secs = 0.0
    secs = max(secs, HOLD_S if kind in ("blocked", "silent_refusal") else ARCHIVE_HOLD if kind == "archive" else 120.0)
    if prev.get("until"):
        secs = max(secs, 2 * (prev["until"] - prev["since"]))
    secs = min(secs, 86400.0)
    pace = max(5.0 if kind in ("blocked", "silent_refusal", "archive") else 2.0, 2 * float(memory(host)["route"].get("min_interval") or MIN_INTERVAL))
    email = _EMAIL.search(text or "")
    hold(host, reason=f"{kind}: {' '.join((text or '').split())[:200]}", until=time.time() + secs, min_interval=pace,
         contact=email.group(0) if email else None, by="scout")
    return {"until": round(time.time() + secs), "min_interval": pace}


def _held_error(host, h):
    when = (f"Scout probes once after {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(h['until']))}" if h.get("until")
            else "it stays until someone lifts it")
    return {"code": "HELD", "message": f"{host} is on hold ({h.get('by')}): {h.get('reason')}. No request was sent; {when}.",
            "next_steps": ([f"The site asked to be contacted at {_hold_contact(host)}: a person can request the unblock there."] if _hold_contact(host) else []) +
                          [f"Work on another host meanwhile. hold('{host}', lift=true) once the site has answered.",
                           f"memory('{host}') shows the hold and the pace Scout will use there."]}


def _resolves(host):
    if host not in _DNS:
        import socket
        try:
            socket.getaddrinfo(host.split(":")[0], 443)
            _DNS[host] = True
        except OSError:
            _DNS[host] = False
    return _DNS[host]


def _blocked(text):
    t = (text or "")[:4000]
    return bool(_BLOCK_A.search(t) and _BLOCK_B.search(t))


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
    if kind == "locale_prefix" and not (spec.get("prefix") == "" or str(spec.get("prefix", "")).startswith("/")):
        return "locale_prefix needs spec {'prefix': '/en-us'} ('' for none)"
    if kind == "listing_root" and not str(spec.get("path", "")).startswith("/"):
        return "listing_root needs spec {'path': '/products'} (starts with /)"
    if kind == "listing_path" and not str(spec.get("path", "")).startswith("/"):
        return "listing_path needs spec {'path': '/products'} (starts with /)"
    if kind == "detail_suffix" and not str(spec.get("suffix", "")).startswith(("/", "?", "#")):
        return "detail_suffix needs spec {'suffix': '/specifications'} (starts with / ? or #)"
    if kind == "url_rewrite":
        if not spec.get("pattern") or not isinstance(spec.get("repl"), str):
            return "url_rewrite needs spec {'pattern': <regex on the full url>, 'repl': <replacement, \\1 for groups>}"
        if re.search(r"\.[*+]|\*\?|\\[dwsDWS]|\[\^?|\(\?", spec["repl"]):
            return ("url_rewrite repl is literal text plus \\1 group references, not a regex: capture the part to keep "
                    "in pattern, e.g. {pattern: '^(https://x.com/p/[^/]+)/specs$', repl: '\\1/specs?format=print'}")
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
            c.execute("""UPDATE move SET tries=tries+1, wins=wins+?,
                         dead=(kind NOT IN ('locale_prefix', 'listing_root') AND tries+1 >= ? AND wins+? = 0) WHERE id=?""",
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


def robots(url, archived=False):
    """(parser, state) for the url's site, cached an hour. RFC 9309: a 4xx robots.txt means no rules, a 5xx means
    stay out until it answers. archived=True reads the site's rules from the archive, for a site not to be asked."""
    p = urllib.parse.urlsplit(url)
    root = f"{p.scheme}://{p.netloc}"
    hit = _ROBOTS.get(root) or (_ROBOTS.get(root + "#archive") if archived else None)
    if hit and time.time() - hit[0] < 3600:
        return hit[1], hit[2]
    rp, state = urllib.robotparser.RobotFileParser(root + "/robots.txt"), "ok"
    where = f"{ARCHIVE}/web/{time.strftime('%Y%m%d%H%M%S', time.gmtime())}id_/{root}/robots.txt" if archived else root + "/robots.txt"
    if archived:
        _polite(where)
    try:
        with urllib.request.urlopen(urllib.request.Request(where, headers=HEADERS), timeout=15 if not archived else 60) as r:
            rp.parse(r.read(500_000).decode("utf-8", "replace").splitlines())
    except urllib.error.HTTPError as e:
        state = "unreachable" if e.code >= 500 else "none"
        rp.parse(["User-agent: *", "Disallow: /"] if e.code >= 500 else [])
    except Exception:  # noqa: BLE001 — ponytail: no answer at all = no rules; the page fetch will report the network error
        state = "none"
        rp.parse([])
    _ROBOTS[root + ("#archive" if archived else "")] = (time.time(), rp, state + (" (archived copy)" if archived else ""))
    return rp, state + (" (archived copy)" if archived else "")


def _polite(url):
    """Wait our turn on this host. Returns the robots verdict: (allowed, state)."""
    rp, state = robots(url)
    if not rp.can_fetch(UA, url):
        return False, state
    host = urllib.parse.urlsplit(url).netloc
    gap = max(MIN_INTERVAL, min(float(rp.crawl_delay(UA) or 0), 30.0), float(memory(_host(url))["route"].get("min_interval") or 0),
              ARCHIVE_PACE if _host(url) == _host(ARCHIVE) else 0)
    with _lock:
        wait = _LAST.get(host, 0) + gap - time.time()
        _LAST[host] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)
    return True, state


# ── readers ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _decode(raw, ctype=""):
    """A page's text in the charset it was written in (Depot, 2026-10-03). Strict utf-8 first: bytes that decode cleanly
    as utf-8 are utf-8. Then the declared charset, but never utf-16/32 without a byte-order mark: Delaware's pages declare
    utf-16 and are utf-8, and trusting it gave CJK noise. Then windows-1252: Oregon's pages lost every § as utf-8."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    m = re.search(r"charset=[\"']?([\w.-]+)", ctype or "") or re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", raw[:4096], re.I)
    cs = (m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)).lower() if m else ""
    if cs.startswith(("utf-16", "utf-32", "ucs")) and not raw[:4].startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        cs = ""
    if cs and cs not in ("utf-8", "utf8"):
        try:
            return raw.decode(cs, "replace")
        except LookupError:
            pass
    return raw.decode("cp1252", "replace")


def _jsonld(soup):
    """The page's schema.org data (<script type="application/ld+json">), as a flat list of typed nodes. Product pages that
    are a mess as html often carry clean Product data here (Crucial, Leviton: Depot, 2026-10-03/04)."""
    out = []
    for t in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            d = json.loads(t.string or t.get_text() or "")
        except ValueError:
            continue
        for node in (d if isinstance(d, list) else [d]):
            if isinstance(node, dict):
                out += [n for n in ([node] + list(node.get("@graph") or [])) if isinstance(n, dict) and n.get("@type")]
    return out[:20]


def _convert(raw, ctype, url):
    """Bytes -> {markdown, title, links, shell}. PDFs read by their text layer."""
    if "pdf" in (ctype or "").lower() or raw[:5] == b"%PDF-":
        import logging
        from pypdf import PdfReader
        logging.getLogger("pypdf").setLevel(logging.ERROR)     # font-encoding chatter, not errors
        rd = PdfReader(io.BytesIO(raw))
        md = "\n\n".join(t for t in ((pg.extract_text() or "").strip() for pg in rd.pages[:400]) if t)
        title = (rd.metadata.title if rd.metadata and rd.metadata.title else url.rsplit("/", 1)[-1])
        return {"markdown": md, "title": f"{title} ({len(rd.pages)} pages)", "links": [], "shell": False}
    import warnings
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    from markdownify import markdownify
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(_decode(raw, ctype), "html.parser")
    ld = _jsonld(soup)
    links = []
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(url, a["href"].split("#")[0].strip())
        if href.startswith(("http://", "https://")):
            links.append({"url": href, "text": " ".join(a.get_text(" ").split())[:80]})
    scripts = len(soup.find_all("script"))
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg"]):
        t.decompose()
    title = " ".join(soup.title.string.split()) if soup.title and soup.title.string else ""
    if not re.search(r"[A-Za-z0-9]", title):     # no title, or one like "=====": the page's first real heading
        hd = soup.find(["h1", "h2"])
        title = " ".join(hd.get_text(" ").split())[:200] if hd else title
    body = soup.find("main") or soup.find("article") or soup.body or soup
    md = re.sub(r"\n{3,}", "\n\n", markdownify(str(body), heading_style="ATX", bullets="-")).strip()
    shell = (len(md) < MIN_GOOD and scripts >= 3) or (len(md) < 2000 and bool(_JS_NEEDED.search(md)))
    return {"markdown": md, "title": title, "links": links, "shell": shell, **({"jsonld": ld} if ld else {})}


def _direct(url, timeout):
    asked, (url, extra, secrets) = url, _auth(url)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={**HEADERS, **extra}), timeout=timeout) as r:
            raw, ctype = r.read(MAX_BYTES), r.headers.get("Content-Type", "")
            final = _scrub(r.geturl(), secrets) if secrets else r.geturl()
    except urllib.error.HTTPError as e:
        try:
            body = _scrub(e.read(20_000).decode("utf-8", "replace"), secrets)
        except Exception:  # noqa: BLE001
            body = ""
        raise Status(e.code, e.headers.get("Retry-After") if e.headers else None, bool(_CHALLENGE.search(body)),
                     _blocked(body), " ".join(re.sub(r"<[^>]+>", " ", body).split())[:600]) from None
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    page = {**_convert(raw, ctype, final), "final_url": final if not secrets else asked}
    if secrets:
        page["markdown"] = _scrub(page["markdown"], secrets)
        page["links"] = [{**l, "url": _scrub(l["url"], secrets)} for l in page["links"]]
    return page


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except (ConnectionResetError, http.client.RemoteDisconnected, http.client.IncompleteRead):
            # a pooled connection to a renderer service the network quietly expired: once more, fresh (estate, 2026-10-04)
            if attempt == 2:
                raise
            time.sleep(3)


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


class BadCapture(Exception):
    """The archive answered with a copy of the site's own error or block page (its Memento-Datetime header says it is
    a replay), not with a refusal of its own: AMD's newest copies of some pages are Akamai's 403 (Depot, 2026-10-03)."""


def _archive(url, timeout, stamp=None):
    """The Wayback Machine copy of `url` nearest `stamp` (default: now), as the site served it (the id_ form: no archive
    toolbar), with links resolved against the site's own address. 404 means the archive has no copy."""
    copy = f"{ARCHIVE}/web/{stamp or time.strftime('%Y%m%d%H%M%S', time.gmtime())}id_/{url}"
    _polite(copy)
    try:
        with urllib.request.urlopen(urllib.request.Request(copy, headers=HEADERS), timeout=timeout) as r:
            raw, ctype, final = r.read(MAX_BYTES), r.headers.get("Content-Type", ""), r.geturl()
    except urllib.error.HTTPError as e:
        if e.headers and (e.headers.get("Memento-Datetime") or any(k.lower().startswith("x-archive-orig") for k in e.headers.keys())):
            raise BadCapture(f"the archived copy is the site's own HTTP {e.code}") from None
        raise Status(e.code) from None
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    m = re.search(r"/web/(\d{14})id_/", final)
    page = _convert(raw, ctype, url)
    page.update(final_url=url, archived={"captured": (f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else None), "copy": final})
    return page


def _good_captures(url, n=4):
    """Timestamps of the newest distinct captures of `url` the site served with status 200, newest first. A 200 is not
    proof: crucial.com's edge served its rejection page with a 200, and the archive kept that (2026-10-05)."""
    q = urllib.parse.urlencode([("url", url), ("output", "json"), ("filter", "statuscode:200"), ("fl", "timestamp,digest"),
                                ("collapse", "digest"), ("limit", str(-25))])
    _polite(f"{ARCHIVE}/cdx/")
    with urllib.request.urlopen(urllib.request.Request(f"{ARCHIVE}/cdx/search/cdx?{q}", headers=HEADERS), timeout=60) as r:
        rows = json.loads(r.read() or b"[]")[1:]
    return [row[0] for row in reversed(rows)][:n]


def wayback_urls(url, limit=20000, years=2):
    """The site's pages the Wayback Machine holds (captured with status 200 in the last `years`), from its public index:
    a full listing of a site without a single request to the site."""
    p = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    prefix = p.path if p.path not in ("", "/") else ""
    q = urllib.parse.urlencode([("url", p.netloc + prefix + "*"), ("output", "json"), ("filter", "statuscode:200"),
                                ("filter", "mimetype:text/html"), ("collapse", "urlkey"), ("fl", "original"),
                                ("limit", str(int(limit))), ("from", str(time.gmtime().tm_year - years))])
    _polite(f"{ARCHIVE}/cdx/")
    with urllib.request.urlopen(urllib.request.Request(f"{ARCHIVE}/cdx/search/cdx?{q}", headers=HEADERS), timeout=180) as r:
        rows = json.loads(r.read() or b"[]")[1:]
    out = []
    for (u,) in rows:
        sp = urllib.parse.urlsplit(u)
        qs = [(k, v) for k, v in urllib.parse.parse_qsl(sp.query) if not re.match(r"(?i)utm_|fbclid|gclid|mc_|ref$", k)]
        out.append(urllib.parse.urlunsplit(("https", sp.netloc.lower().removesuffix(":80").removesuffix(":443"), sp.path,
                                            urllib.parse.urlencode(qs), "")))
    return list(dict.fromkeys(out))


def _mirror_path(url):
    import hashlib
    return DB_PATH.parent / "mirror" / _host(url) / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".json")


def _mirror_days(url):
    return max(MIRROR_DAYS, float(memory(_host(url))["route"].get("mirror_days") or 0))


def _mirror_get(url):
    """A good page read in the last MIRROR_DAYS (or the days a cache() of its site asked for): re-runs and recipe
    compiles read the copy, not the site."""
    days = _mirror_days(url)
    if days <= 0:
        return None
    path = _mirror_path(url)
    try:
        if time.time() - path.stat().st_mtime < days * 86400:
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    return None


def _mirror_put(url, page, reader):
    if _mirror_days(url) <= 0:
        return
    try:
        path = _mirror_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        keep = {k: page.get(k) for k in ("markdown", "title", "links", "final_url", "archived", "jsonld")}
        keep["markdown"] = (keep["markdown"] or "")[:2_000_000]
        path.write_text(json.dumps({**keep, "reader": reader}))
    except OSError:
        pass


ENGINES = {"direct": _direct, "firecrawl": _firecrawl, "crawl4ai": _crawl4ai, "browser": _browser, "archive": _archive}


def readers():
    return [n for n, on in (("direct", True), ("firecrawl", bool(FIRECRAWL)), ("crawl4ai", bool(CRAWL4AI)),
                            ("browser", _have_browser()), ("archive", ARCHIVE.lower() not in ("", "off"))) if on]


# ── the goal test ────────────────────────────────────────────────────────────────────────────────────────────────────
def _carries(md, want):
    try:
        return bool(re.search(want, md, re.I))
    except re.error:
        return want.lower() in md.lower()


def judge(page, want=""):
    """ok | want_miss (real content, not what was asked) | js_shell | blocked | challenge | login_wall | error_page | thin | empty"""
    md, title = page.get("markdown") or "", page.get("title") or ""
    if page.get("shell"):
        return "js_shell"
    if not md.strip():
        return "empty"
    if len(md) < 8000 and _blocked(title + " " + md):
        return "blocked"
    if _CHALLENGE.search(title + " " + md[:1500]) and len(md) < 5000:
        return "challenge"
    if _ERROR_PAGE.search(title) or (len(md) < 8000 and _ERROR_PAGE.search(md[:300])):
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
        if getattr(exc, "blocked", False):
            return "blocked"
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


def _try_readers(url, want, tried, fresh=False, archive_only=False):
    """The mirror, then the host's live readers best first, then the archive when the live site walled or failed.
    Returns (good page, reader) or (None, best real page or None)."""
    host, best = _host(url), None
    mp = None if fresh else _mirror_get(url)
    if mp:
        verdict = judge(mp, want)
        tried.append({"url": url, "reader": "mirror", "outcome": verdict, "chars": len(mp.get("markdown") or ""),
                      "copy_of": mp.get("reader")})
        if verdict == "ok":
            return mp, "mirror"
        return None, {**mp, "reader": "mirror"}       # the copy is today's page: asking the site again would say the same
    page, best = (None, None) if archive_only else _live(url, want, tried, host)
    if page:
        return page, tried[-1]["reader"]
    live = [t["outcome"] for t in tried if t["url"] == url]
    if "archive" in readers() and (archive_only or (not best and live and live[-1] in ARCHIVE_WHEN)):
        rp, _ = robots(url, archived=archive_only)
        if not rp.can_fetch(UA, url):
            tried.append({"url": url, "reader": "archive", "outcome": "robots_disallowed"})
            return None, best
        ah = _held(_host(ARCHIVE))
        if ah:
            tried.append({"url": url, "reader": "archive", "outcome": "archive_held", "detail": ah.get("reason", "")[:120],
                          "until": ah.get("until")})
            return None, best
        try:
            page = None
            try:
                page = _archive(url, TIMEOUT["archive"])
                bad = judge(page) in ("blocked", "challenge", "error_page")
            except BadCapture:
                bad = True
            if bad:                     # the newest copy is the site's block or error page: walk back to a real one
                seen = ((page or {}).get("archived") or {}).get("copy", "")
                for stamp in _good_captures(url):
                    if stamp in seen:
                        continue
                    page = _archive(url, TIMEOUT["archive"], stamp)
                    if judge(page) not in ("blocked", "challenge", "error_page"):
                        break
                else:
                    raise BadCapture("the archive holds no good copy of this page: its captures are the site's error or block pages")
        except BadCapture as e:
            tried.append({"url": url, "reader": "archive", "outcome": "no_archive", "detail": str(e)[:120]})
            return None, best
        except Exception as e:  # noqa: BLE001
            refused = getattr(e, "status", 0) in (403, 429, 503) or re.search(r"refused|reset|aborted", str(e), re.I)
            tried.append({"url": url, "reader": "archive", "outcome": "archive_throttled" if refused else "no_archive",
                          "detail": str(e)[:120]})
            if refused:          # the library asked us to slow down: every reader of it stops, for everyone sharing memory
                _throttled(_host(ARCHIVE), "archive", None, f"the Wayback Machine refused a copy ({str(e)[:80]})")
            return None, best
        verdict = judge(page, want)
        tried.append({"url": url, "reader": "archive", "outcome": verdict, "chars": len(page.get("markdown") or ""),
                      "captured": page["archived"]["captured"]})
        if verdict in ("ok", "want_miss"):
            _mirror_put(url, page, "archive")
        if verdict == "ok":
            return page, "archive"
        if verdict == "want_miss":
            best = {**page, "reader": "archive"}
    return None, best


def _live(url, want, tried, host):
    best = None
    for name in order(host):
        if name not in readers() or name == "archive":
            continue
        allowed, _ = _polite(url)
        if not allowed:
            tried.append({"url": url, "reader": name, "outcome": "robots_disallowed"})
            return None, best
        try:
            page = ENGINES[name](url, TIMEOUT[name])
        except Exception as e:  # noqa: BLE001
            out = _outcome(e)
            if out == "network" and _STALL.search(str(e)) and _resolves(host):
                out = "silent_refusal"
            tried.append({"url": url, "reader": name, "outcome": out, "status": getattr(e, "status", None),
                          "retry_after": getattr(e, "retry_after", None), "detail": (getattr(e, "body", "") or str(e))[:200]})
            if out in ("blocked", "rate_limited"):
                tried[-1]["hold"] = _throttled(host, out, getattr(e, "retry_after", None), getattr(e, "body", "") or "")
                break                 # every reader leaves from the same address: stop knocking
            if out not in ("not_found", "http_error"):
                _record(host, name, False)     # a 404 is the page, not this reader's fault
            if out == "not_found":
                break                 # another reader will not fix a 404
            continue
        verdict = judge(page, want)
        tried.append({"url": url, "reader": name, "outcome": verdict, "chars": len(page.get("markdown") or "")})
        if verdict in ("ok", "want_miss"):
            _mirror_put(url, page, name)
        if verdict == "blocked":
            tried[-1]["detail"] = " ".join((page.get("markdown") or "").split())[:200]
            tried[-1]["hold"] = _throttled(host, "blocked", None, page.get("markdown") or "")
            break
        real = verdict in ("ok", "want_miss")
        _record(host, name, real)     # did this reader get a real page; whether it held the want is a separate question
        if verdict == "js_shell" and name == "direct":
            learn(host, "render", True)
        if verdict == "ok":
            return page, name
        if real and best is None:
            best = {**page, "reader": name}
    mine = [t["outcome"] for t in tried if t["url"] == url and t["reader"] not in ("archive", "mirror")]
    if len(mine) >= 2 and all(o == "silent_refusal" for o in mine):
        tried[-1]["hold"] = _throttled(host, "silent_refusal", None, tried[-1].get("detail") or "")   # every reader stalled: stop knocking
    return None, best


# words that never steer a link choice: "law", "and", "section" narrowed searches to nothing (estate scout, 2026-10-03)
_STEER_STOP = frozenset("the of and to in a an or by for with from on at as is are be this that which page pages section sections "
                        "chapter title part article law laws code item items product products".split())


def _want_links(page, want, limit=3):
    if not want:
        return []
    words = [w for w in re.split(r"[^a-z0-9]+", want.lower()) if len(w) > 2 and w not in _STEER_STOP]
    host = _host(page.get("final_url") or "")
    seen, out = set(), []
    for l in page.get("links") or []:
        hay = (l["url"] + " " + l.get("text", "")).lower()
        if _host(l["url"]) == host and l["url"] not in seen and any(w in hay for w in words) and not _FILE.search(l["url"]):
            seen.add(l["url"]); out.append(l["url"])
    return out[:limit]


def _siblings(url, want, limit=2):
    """The page lacks what was asked: climb to the page listing it and its siblings, and take the sibling links whose
    own line names two thirds of the request's words (estate scout, 2026-10-04: Depot held New York's Criminal Procedure
    Law, the prompt asked for its Estates, Powers and Trusts Law, and the CPL's parent listing named the EPTL)."""
    words = {w for w in re.findall(r"[a-z0-9]{3,}", re.sub(r"\\[a-z]", " ", want.lower()))} - _STEER_STOP
    if not words:
        return []
    need, p = max(1, -(-2 * len(words) // 3)), urllib.parse.urlsplit(url)
    own, up, out = p.path.rstrip("/"), p.path.rstrip("/"), []
    for _ in range(2):
        up = up.rsplit("/", 1)[0]
        if not up:
            break
        r = reach(f"{p.scheme}://{p.netloc}{up}/", _depth=1, _links=True)
        for l in r.get("links") or []:
            lp = urllib.parse.urlsplit(l["url"]).path.rstrip("/")
            hits = len(words & set(re.findall(r"[a-z0-9]{3,}", (l.get("text", "") + " " + lp).lower())))
            if _host(l["url"]) == p.netloc.lower() and lp.startswith(up + "/") and lp != own and not own.startswith(lp) and hits >= need:
                out.append((hits, l["url"]))
        if out:
            break
    return [u for _, u in sorted(out, key=lambda x: -x[0])][:limit]


def reach(url, want="", full=False, _depth=0, _links=False, fresh=False, cached=False, _archive_only=False):
    """Reach good information at `url`. `want` is a regex the page must carry (a model number, 'price|\\$', a heading)."""
    if "://" not in url:
        url = "https://" + url
    host, tried = _host(url), []
    if cached and not _mirror_get(url):
        root = f"https://{host}"
        return {"ok": False, "url": url, "tried": [], "error": {"code": "NOT_CACHED",
                "message": f"{url} is not in Scout's copy of {host}, and cached mode sends no request.",
                "next_steps": [f"cache('{root}') downloads the site once (source='archive' for zero load on the site), then read it cached",
                               "or read this page live: call again without cached"]}}
    h = None if cached else _held(host)
    if _archive_only and not cached:
        page, reader = _try_readers(url, want, tried, fresh, archive_only=True)
    elif h:      # the site is not to be asked: its archive may still answer, without a single request to the site
        page, reader = _try_readers(url, want, tried, fresh, archive_only=True) if "archive" in readers() else (None, None)
        if not page:
            err = _held_error(host, h)
            if any(t["outcome"] == "no_archive" for t in tried):
                err["message"] += " The Wayback Machine has no copy of this page either."
            return {"ok": False, "url": url, "error": err, "tried": tried}
    else:
        page, reader = _try_readers(url, want, tried, fresh)
    via = None
    if not page and _depth == 0 and not cached and not any(t["outcome"] in ("robots_disallowed", "rate_limited", "blocked") for t in tried):
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
            hops += [(u, None, "a sibling the parent listing names") for u in _siblings(url, want)]
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
            if any(t["outcome"] in ("rate_limited", "blocked") for t in sub):
                break
    if page:
        md = page.get("markdown") or ""
        if (memory(host)["route"].get("hold") or {}).get("until") and reader not in ("archive", "mirror"):
            hold(host, lift=True)     # the probe after an automatic hold got through; the slower pace stays
        return {"ok": True, "url": page.get("final_url") or url, "requested_url": url, "via": via,
                "reader": [t for t in tried if t["outcome"] == "ok"][-1]["reader"], "title": page.get("title", ""),
                **({"archived": page["archived"]} if page.get("archived") else {}),
                **({"jsonld": page["jsonld"]} if page.get("jsonld") else {}),
                "chars": len(md), "prose": prose(md), "markdown": excerpt(md, want, full), "tried": tried, "learned": memory(host),
                **({"links": page.get("links") or []} if _links else {})}
    best = reader if isinstance(reader, dict) else None
    err = diagnose(url, want, tried, best)
    _log_fail(host, url, want, err["code"], tried)
    out = {"ok": False, "url": url, "error": err, "tried": tried, "readers_available": readers()}
    if best:
        out["closest"] = {"url": best.get("final_url") or url, "title": best.get("title", ""), "chars": len(best.get("markdown") or ""),
                          "markdown": excerpt(best.get("markdown") or "", "", False)[:2000]}
    return out


# ── access: a site's official route, and the enrollment it needs ────────────────────────────────────────────────────
# When a site walls Scout, the best next move is usually its own front door: an open API, a bulk download, or an API
# a person enrolls in. Scout keeps that per site (route['access']). An access that needs a key files an enrollment
# request, and the open requests are the checklist for whoever updates Scout ("sign up here, put the key there").
# Keys live only in the environment (SCOUT_KEY_<NAME>) or the keys file (NAME=value, mode 600). They are applied at
# fetch time to the API's own host and scrubbed, by value, from everything Scout returns.
KEYS_FILE = Path(os.environ.get("SCOUT_KEYS_FILE") or Path.home() / ".config/scout-mcp/keys.env")
ROUTES = ("open_api", "bulk", "feed", "public_json", "enroll_free", "enroll_paid", "enroll_oauth", "commercial", "alternative", "none")
NEEDS_KEY = ("enroll_free", "enroll_paid", "enroll_oauth", "commercial")
_TOKENS, _APIS = {}, [0.0, {}]


def _keyname(name):
    return re.sub(r"[^A-Z0-9]+", "_", (name or "").upper()).strip("_")


def keys_file_state():
    try:
        return "too open: chmod 600 " + str(KEYS_FILE) if KEYS_FILE.stat().st_mode & 0o077 else "ok"
    except FileNotFoundError:
        return "missing"


def _keys():
    out = {k[len("SCOUT_KEY_"):]: v for k, v in os.environ.items() if k.startswith("SCOUT_KEY_") and v}
    if keys_file_state() == "ok":            # a keys file others can read is ignored, not trusted
        for line in KEYS_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out.setdefault(_keyname(k).removeprefix("SCOUT_KEY_"), v.strip().strip("'\""))
    return out


def _have_key(a):
    k = _keys()
    return bool(k.get(a["key"] + "_ID") and k.get(a["key"] + "_SECRET")) if a["auth"]["kind"] == "oauth2_client_credentials" \
        else bool(k.get(a["key"]))


def access(host):
    """The site's official route, if one is known (its own entry, or the www/bare twin's)."""
    h = _host(host if "://" in host else "https://" + host)
    for x in (h, h.removeprefix("www."), "www." + h.removeprefix("www.")):
        a = memory(x)["route"].get("access")
        if a:
            return a
    return None


def _api_access(host):
    """The access whose API lives on `host` (api.legiscan.com belongs to legiscan.com's access)."""
    if time.time() - _APIS[0] > 30:
        try:
            with _db() as c:
                rows = c.execute("""SELECT route FROM host WHERE route LIKE '%"access"%'""").fetchall()
            _APIS[1] = {a["api_host"]: a for a in (json.loads(r["route"]).get("access") for r in rows) if a and a.get("api_host")}
        except sqlite3.Error:
            _APIS[1] = {}
        _APIS[0] = time.time()
    return _APIS[1].get(host)


def _token(a, cid, secret):
    hit = _TOKENS.get(a["key"])
    if hit and hit[0] > time.time() + 60:
        return hit[1]
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}).encode()
    req = urllib.request.Request(a["auth"]["token_url"], data=body, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    _TOKENS[a["key"]] = (time.time() + float(d.get("expires_in") or 600), d["access_token"])
    return d["access_token"]


def _auth(url):
    """(url, extra headers, secrets to scrub) for a request to an enrolled API. Nothing for any other host."""
    a = _api_access(_host(url))
    if not a or a["auth"]["kind"] in ("none", None):
        return url, {}, []
    k, kind, name = _keys(), a["auth"]["kind"], a["auth"].get("name")
    cred, extra, secrets = k.get(a["key"]), {}, []   # never call the secret `key`: Pixel's leak, 2026-10-04
    if kind == "oauth2_client_credentials":
        cid, sec = k.get(a["key"] + "_ID"), k.get(a["key"] + "_SECRET")
        if cid and sec:
            tok = _token(a, cid, sec)
            extra = {"Authorization": f"Bearer {tok}", **{h: v.replace("{client_id}", cid) for h, v in (a["auth"].get("headers") or {}).items()}}
            secrets = [tok, cid, sec]
        return url, extra, secrets
    if not cred:
        return url, {}, []
    if kind == "query":
        p = urllib.parse.urlsplit(url)
        q = urllib.parse.urlencode(urllib.parse.parse_qsl(p.query) + [(name or "key", cred)])
        url = urllib.parse.urlunsplit(p._replace(query=q))
    elif kind == "header":
        extra = {name or "X-Api-Key": cred}
    elif kind == "bearer":
        extra = {"Authorization": f"Bearer {cred}"}
    return url, extra, [cred]


def _scrub(text, secrets):
    for sec in secrets:
        if sec:
            text = text.replace(sec, "[key]").replace(urllib.parse.quote(sec, safe=""), "[key]")
    return text


def _ensure_enroll(c):
    c.execute("""CREATE TABLE IF NOT EXISTS enroll (host TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT, note TEXT,
                 asked REAL, updated REAL)""")


def _todo(host, a, status):
    if status == "enrolled":
        return f"Enrolled. Scout adds the credential to requests for {a.get('api_host')}; recipes stay keyless."
    if a["route"] not in NEEDS_KEY:
        return f"No signup needed: use {a.get('name')} at {a.get('sample') or a.get('base_url')}."
    put = (f"{a['key']}_ID=<client id> and {a['key']}_SECRET=<client secret>" if a["auth"]["kind"] == "oauth2_client_credentials"
           else f"{a['key']}=<your key>")
    verb = "Ask for a license to" if a["route"] == "commercial" else "Sign up for"
    return (f"{verb} {a.get('name')} at {a.get('signup_url') or a.get('docs_url')} ({a.get('cost') or 'cost unknown'}). "
            f"Put {put} in {KEYS_FILE} (chmod 600) or set the SCOUT_KEY_ env vars, then call "
            f"enroll(action='done', host='{host}'). Terms: {(a.get('terms') or 'read them at signup').rstrip('. ')}.")


def enroll(action="list", host="", info=None, note="", include_all=False):
    """Enrollment requests: what a person must sign up for so Scout can use a site's official API.
    action: list | request (info = the access) | applied (signed up, waiting on approval) | done (verify the key) | decline."""
    h = _host(host if "://" in host else "https://" + host) if host else ""
    with _db() as c:
        _ensure_enroll(c)
        if action == "list":
            rows = c.execute("SELECT * FROM enroll ORDER BY asked").fetchall()
            out = []
            for r in rows:
                if not include_all and r["status"] in ("enrolled", "declined"):
                    continue
                a = access(r["host"]) or {}
                out.append({"host": r["host"], "status": r["status"], "why": r["reason"], "note": r["note"], "name": a.get("name"),
                            "cost": a.get("cost"), "todo": _todo(r["host"], a, r["status"]) if a else "no access recorded"})
            return {"enrollments": out, "keys_file": f"{KEYS_FILE} ({keys_file_state()})"}
        if not h:
            return {"ok": False, "error": {"code": "BAD_ENROLL", "message": "give the site's host"}}
        if action == "request":
            a = dict(info or {})
            if a.get("route") not in ROUTES:
                return {"ok": False, "error": {"code": "BAD_ENROLL", "message": f"info.route must be one of {', '.join(ROUTES)}"}}
            if a["route"] in NEEDS_KEY and not (a.get("signup_url") or a.get("docs_url")):
                return {"ok": False, "error": {"code": "BAD_ENROLL", "message": "an enrollment needs info.signup_url (where a person applies)"}}
            a["auth"] = {"kind": "none", **(a.get("auth") or {})}
            a["key"] = _keyname(a.get("key") or re.sub(r"^(www|us|en|api)\.", "", h).rsplit(".", 1)[0])   # named after the site
            a["api_host"] = a.get("api_host") or (_host(a["base_url"]) if a.get("base_url") else None)
            if a.get("sample"):
                a["sample"] = re.sub(r"([?&])[^=&]+=(?:KEY|\{KEY\}|<KEY>|YOUR_?KEY|\{key\}|<key>)(?=&|$)", r"\1", a["sample"]).rstrip("?&")
            learn(h, "access", a)
            _APIS[0] = 0
            if a["route"] in NEEDS_KEY:
                c.execute("""INSERT INTO enroll (host, status, reason, note, asked, updated) VALUES (?, 'needed', ?, ?, ?, ?)
                             ON CONFLICT(host) DO UPDATE SET reason=excluded.reason, note=COALESCE(excluded.note, enroll.note),
                             updated=excluded.updated""", (h, note or "requested", note or None, time.time(), time.time()))
            st = (c.execute("SELECT status FROM enroll WHERE host=?", (h,)).fetchone() or {"status": "no signup needed"})["status"]
            return {"ok": True, "host": h, "status": st, "access": a, "todo": _todo(h, a, st)}
        if action in ("applied", "decline"):
            st = {"applied": "requested", "decline": "declined"}[action]
            n = c.execute("UPDATE enroll SET status=?, note=?, updated=? WHERE host=?", (st, note or None, time.time(), h)).rowcount
            return {"ok": bool(n), "host": h, "status": st}
        if action == "done":
            a = access(h)
            if not a:
                return {"ok": False, "error": {"code": "NO_ACCESS", "message": f"no official route recorded for {h}"}}
            if not _have_key(a):
                return {"ok": False, "error": {"code": "NO_KEY", "message": f"no credential found for {a['key']}. Keys file: {KEYS_FILE} ({keys_file_state()})",
                                               "next_steps": [_todo(h, a, "needed")]}}
            try:
                page = _direct(a["sample"], 30)
                verdict = judge(page)
            except Exception as e:  # noqa: BLE001
                verdict = _outcome(e)
            if verdict != "ok":
                return {"ok": False, "error": {"code": "KEY_TEST_FAILED", "message": f"{a.get('name')} sample answered {verdict} with the key",
                                               "next_steps": ["check the key and the auth kind in the access entry; the provider may still be approving it"]}}
            c.execute("UPDATE enroll SET status='enrolled', updated=? WHERE host=?", (time.time(), h))
            return {"ok": True, "host": h, "status": "enrolled", "todo": _todo(h, a, "enrolled")}
    return {"ok": False, "error": {"code": "BAD_ENROLL", "message": "action is list | request | applied | done | decline"}}


def _access_steps(host, code):
    """The official route as the first next step when a site walls Scout; files an enrollment when it needs one."""
    a = access(host)
    if not a:
        return []
    if a["route"] == "none":
        return [f"No official route exists for {host} (checked {a.get('checked') or 'earlier'}): {a.get('notes') or a.get('terms') or ''}".rstrip(": ")]
    if a["route"] in NEEDS_KEY:
        if _have_key(a):
            return [f"Use {a.get('name')} (enrolled; Scout adds the credential): reach('{a.get('sample') or a.get('base_url')}')"]
        with _db() as c:
            _ensure_enroll(c)
            c.execute("INSERT OR IGNORE INTO enroll (host, status, reason, asked, updated) VALUES (?, 'needed', ?, ?, ?)",
                      (host, code, time.time(), time.time()))
        return [f"{host}'s official route is {a.get('name')}, which needs a person to enroll ({a.get('cost') or 'cost unknown'}): "
                f"{a.get('signup_url') or a.get('docs_url')}. Filed as an enrollment request; enroll(action='list') is the checklist."]
    return [f"Use the site's official route, {a.get('name')} ({a['route'].replace('_', ' ')}): reach('{a.get('sample') or a.get('base_url')}')"
            + (f". Terms: {a['terms']}" if a.get("terms") else "")]


# ── actionable errors: what failed, why, and the next call that could fix it ─────────────────────────────────────────
_PRIORITY = ["robots_disallowed", "blocked", "rate_limited", "challenge", "refused", "silent_refusal", "login_wall", "not_found", "error_page",
             "js_shell", "thin", "empty", "want_miss", "tls", "network", "server_error", "reader_missing", "http_error",
             "archive_throttled", "archive_held", "no_archive"]


def diagnose(url, want, tried, best=None):
    host = _host(url)
    p = urllib.parse.urlsplit(url)
    root = f"{p.scheme}://{p.netloc}"
    seen = {t["outcome"] for t in tried if "via" not in t} or {t["outcome"] for t in tried}   # a guessed sub-page's 404 is not the verdict
    first = next((o for o in _PRIORITY if o in seen), "unknown")
    status = next((t.get("status") for t in tried if t.get("status")), None)
    renderers = [r for r in readers() if r in RENDERERS]
    blocktext = " ".join(t.get("detail") or "" for t in tried if t["outcome"] == "blocked")
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
        "blocked": ("BLOCKED", f"{host} served a block page{f' (HTTP {status})' if status else ''}: "
                    f"\"{next((t.get('detail') or '' for t in tried if t['outcome'] == 'blocked'), '')[:160]}\". "
                    f"Scout put the host on hold and will send one probe when the hold ends; its pace there is now one request every "
                    f"{memory(host)['route'].get('min_interval', MIN_INTERVAL):g} s or slower.",
            ([f"Have a person email {_hold_contact(host, blocktext)} to lift the block: say what reads the site, that it went too fast, "
              f"the new pace (one request every {max(2, memory(host)['route'].get('min_interval', 2)):g} s), and offer to use a bulk "
              "download if they publish one. Then hold(...) as below until they answer."] if _hold_contact(host, blocktext) else
             [f"Find the site's contact (the block page, {root}/contact, the footer) and ask a person to request the unblock."]) +
            [f"hold('{host}', reason='blocked; unblock requested <date>') keeps every agent sharing this memory off the site "
             f"until hold('{host}', lift=true).",
             "Do not switch readers or addresses to get past it: the block is on the address, and getting around a throttle is "
             "what turns it into a ban.",
             f"memory('{host}') shows the hold and when the probe is due."]),
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
        "silent_refusal": ("SILENT_REFUSAL", f"{host} resolves and takes connections, but every request stalled or was reset "
                           f"({next((t.get('detail') or '' for t in tried if t['outcome'] == 'silent_refusal'), '')[:90]}). That is the "
                           "site's edge refusing this client quietly, not a network fault."
                           + (" Scout put the host on hold so nothing keeps knocking." if _held(host) else ""),
            ["The site's own route is the way in: an API, a feed, a bulk download, or its partner or dealer program.",
             f"Ask {host} for access through its contact or partner page; say what reads the site and how slowly.",
             "The same information is often published elsewhere: distributors, registries, the maker's documentation CDN.",
             "Retrying in a loop will not help: a stall is a refusal."]),
        "archive_throttled": ("ARCHIVE_THROTTLED", "The Wayback Machine refused a copy: Scout asked it too fast. Scout holds the "
                              f"archive for {ARCHIVE_HOLD // 60} minutes (doubling while it refuses) and reads it more slowly after.",
            ["Wait for the hold to end; cache jobs wait on their own.", "memory('web.archive.org') shows when it ends."]),
        "archive_held": ("ARCHIVE_HELD", "The Wayback Machine is on hold: it refused Scout recently, so no copy was asked for.",
            ["Wait for the hold to end, then read again; cache jobs wait on their own."]),
        "no_archive": ("NO_ARCHIVED_COPY", f"The Wayback Machine has no copy of {url}.",
            [f"site_map('{root}', archive='only') lists the pages it does hold."]),
        "network": ("NETWORK", f"Could not reach {host}: {next((t.get('detail') for t in tried if t['outcome'] == 'network'), '')}",
            ["Check the url spelling and that the site is up (DNS, timeouts).", "Retry later if the site is slow."]),
        "server_error": ("SERVER_ERROR", f"{host} answered HTTP {status}.", ["Server errors are usually brief: retry later."]),
        "reader_missing": ("READER_MISSING", "A configured reader could not start.", [install]),
        "http_error": ("HTTP_ERROR", f"{host} answered HTTP {status}.", [sitemap]),
    }
    code, message, steps = T.get(first, ("UNKNOWN", "No reader produced good information.", [sitemap]))
    if code in ("CHALLENGE_WALL", "REFUSED", "SILENT_REFUSAL", "LOGIN_REQUIRED", "BLOCKED", "ROBOTS_DISALLOWED", "JS_SHELL", "THIN_CONTENT"):
        steps = _access_steps(host, code) + steps
        if not access(host) and code != "BLOCKED":
            steps = steps + [f"No official route is recorded for {host}. Look for its API, developer program or bulk download, "
                             f"then record it: enroll(action='request', host='{host}', info={{'route': 'enroll_free', 'name': ..., "
                             "'signup_url': ..., 'base_url': ..., 'auth': {'kind': 'query', 'name': 'key'}, 'cost': ..., 'sample': ...}}). "
                             "Use route 'open_api' (or bulk, feed, alternative) when no signup is needed."]
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
    if _held(_host(url)):
        return None
    allowed, _ = _polite(url)
    if not allowed:
        return None
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
        raw = r.read(MAX_BYTES)
    raw = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if len(raw) < 20000 and _blocked(raw.decode("utf-8", "replace")):
        _throttled(_host(url), "blocked", None, raw.decode("utf-8", "replace"))
        return None
    return raw


def _sitemap_text(sm):
    """A sitemap's text: the plain fetch, or the host's learned reader when the plain fetch is refused (amd.com answers
    only a rendering reader; its sitemap read 0 through the plain fetch, 2026-10-03)."""
    try:
        raw = _get(sm)
        if raw:
            return raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    if _held(_host(sm)):
        return ""
    r = reach(sm, _depth=1, full=True)
    return r.get("markdown") or "" if r["ok"] else ""


def _sitemap_urls(sm, seen, cap, depth=0):
    if sm in seen or len(seen) > 25 or depth > 2:
        return []
    seen.add(sm)
    body = _sitemap_text(sm)
    locs = [x.strip() for x in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", body, re.S)]
    if not locs and body:                  # read through a renderer: the tags are gone, the urls are not
        locs = list(dict.fromkeys(u.rstrip(".,)") for u in re.findall(r"https?://[^\s<>\"')\]]+", body)))
    if "<sitemapindex" in body or (locs and all(re.search(r"(?i)sitemap|\.xml(\.gz)?$", u) for u in locs)):
        out = []
        for child in locs:
            out += _sitemap_urls(child, seen, cap, depth + 1)
            if len(out) >= cap:
                break
        return out
    return locs


def _walk_links(starts, host, on, rp, budget, urls):
    """Breadth first over the site's own links from `starts`, through the host's learned reader, politely."""
    frontier, opened = list(starts), set()
    while frontier and len(opened) < budget:
        u = frontier.pop(0)
        if u in opened or not rp.can_fetch(UA, u) or _held(host):
            continue
        opened.add(u)
        r = reach(u, _depth=1, _links=True)
        if not r["ok"]:
            continue
        for l in r.get("links") or []:
            v = l["url"]
            if on(v) and not _FILE.search(v) and v not in urls:
                urls.append(v); frontier.append(v)
    return len(opened)


def _listing_cache(url, urls=None, how=""):
    """Read (urls None) or write the last full listing of a url. Four dry runs in an hour that each re-listed amd.com
    were what got it to block (Depot, 2026-10-03): a listing is reused for LISTING_TTL."""
    try:
        with _db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS listing (url TEXT PRIMARY KEY, at REAL, how TEXT, urls TEXT)")
            if urls is None:
                r = c.execute("SELECT at, how, urls FROM listing WHERE url=?", (url,)).fetchone()
                ttl = max(LISTING_TTL, _mirror_days(url) * 86400)         # a cached site keeps its listing as long as its pages
                return (r["at"], r["how"], json.loads(r["urls"])) if r and time.time() - r["at"] < ttl else None
            c.execute("INSERT OR REPLACE INTO listing (url, at, how, urls) VALUES (?, ?, ?, ?)", (url, time.time(), how, json.dumps(urls)))
    except sqlite3.Error:
        return None


def site_map(url, filter="", limit=500, walk=20, fresh=False, archive="auto", cached=False):
    """The site's real urls: robots.txt sitemaps, a polite link walk when they are thin, and the catalogue's own root
    (found by the scored locale x root parts) when nothing listed so far looks like a catalogue. A listing is reused for
    12 hours unless fresh=True: re-listing a whole site run after run is how crawlers get blocked.
    archive: "auto" lists from the Wayback Machine's index when the site is held or listed nothing; "only" lists from
    the index alone, without a single request to the site (robots rules from its archived robots.txt); "off" never."""
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    root, host = f"{p.scheme}://{p.netloc}", p.netloc.lower()
    hit = None if fresh else _listing_cache(url)
    if cached and not hit:
        return {"ok": False, "site": root, "count": 0, "urls": [], "patterns": [], "robots": None, "how": "cached mode",
                "error": {"code": "NOT_CACHED", "message": f"No listing of {url} in Scout's copy, and cached mode sends no request.",
                          "next_steps": [f"cache('{url}') lists and downloads the site once, then read it cached"]}}
    if hit:
        at, how0, urls = hit
        return _site_map_out(url, root, host, urls, filter, limit, f"reused the listing from {int((time.time() - at) / 60)} min ago ({how0}); "
                             "fresh=true lists again", "cached")
    h = _held(host)
    if archive == "only" or (h and archive != "off" and "archive" in readers()):
        rp, state = robots(url, archived=True)
        try:
            urls = [u for u in wayback_urls(url) if rp.can_fetch(UA, u)]
        except Exception as e:  # noqa: BLE001
            urls, h = [], h or {"reason": f"archive index unavailable: {type(e).__name__}", "by": "scout"}
        how = (f"wayback index: {len(urls)} archived pages, no request to the site" + (" (the site is on hold)" if h else ""))
        if urls:
            _listing_cache(url, urls, how)
        out = _site_map_out(url, root, host, urls, filter, limit, how, state)
        if not urls and h:
            out["error"] = _held_error(host, h) if h.get("since") else {"code": "NO_URLS", "message": h["reason"], "next_steps": []}
        return out
    if h:
        return {"ok": False, "site": root, "count": 0, "urls": [], "patterns": [], "robots": None, "how": "on hold",
                "error": _held_error(host, h)}
    rp, state = robots(url)
    declared = rp.site_maps() or [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    seen, urls = set(), []
    for sm in declared[:5]:
        urls += _sitemap_urls(sm, seen, 20000)
    how = [f"sitemaps: {len(urls)} urls from {len(seen)} file(s)"]
    # a map started inside one section stays inside it (estate scout, 2026-10-03: a walk started in one code wandered
    # into the rest of the site); the site's root maps the whole site
    scope = p.path if p.path.endswith("/") else p.path.rsplit("/", 1)[0] + "/"
    inside = lambda u: scope in ("", "/") or urllib.parse.urlsplit(u).path.startswith(scope) or urllib.parse.urlsplit(u).path.rstrip("/") == scope.rstrip("/")
    on = lambda u: urllib.parse.urlsplit(u).netloc.lower().removeprefix("www.") == host.removeprefix("www.") and inside(u)
    urls = [u for u in dict.fromkeys(urls) if on(u) and rp.can_fetch(UA, u)]
    if len(urls) < 50 and walk and not _held(host):      # thin or no sitemap: walk the site's own links
        how.append(f"link walk: opened {_walk_links([url, root + '/'], host, on, rp, walk, urls)} page(s)")
    if walk and scope in ("", "/") and not _held(host) and (len(urls) < 50 or not any(_CATALOG.search(urllib.parse.urlsplit(u).path) for u in urls)):
        # nothing that looks like a catalogue yet: find its root by the scored parts and walk from there (amd.com's map
        # was blogs until /en/products was walked: 1,561 product pages, Depot 2026-10-03)
        lst = listing(url, urls)
        if lst.get("ok"):
            before = len(urls)
            n = _walk_links([lst["url"]], host, on, rp, walk, urls)
            how.append(f"catalogue root {lst['url']}: opened {n} page(s), {len(urls) - before} new urls")
    if not urls and archive == "auto" and "archive" in readers():     # the live site listed nothing: its archive may
        try:
            urls = [u for u in wayback_urls(url) if rp.can_fetch(UA, u)]
            how.append(f"wayback index: {len(urls)} archived pages")
        except Exception as e:  # noqa: BLE001
            how.append(f"wayback index unavailable ({type(e).__name__})")
    if urls:
        _listing_cache(url, urls, "; ".join(how))
    return _site_map_out(url, root, host, urls, filter, limit, "; ".join(how), state)


def _site_map_out(url, root, host, urls, filter, limit, how, state):
    rx = re.compile(filter, re.I) if filter else None
    hits = [u for u in urls if not rx or rx.search(u)]
    top = Counter(_pattern(u) for u in hits).most_common(25)
    pats = [{"pattern": pt, "count": n, "samples": [u for u in hits if _pattern(u) == pt][:3],
             **({"editorial": True} if _EDITORIAL.search(pt) else {})} for pt, n in top]
    pats = sorted(pats, key=lambda x: (x.get("editorial", False), -x["count"]))[:15]   # editorial sections are never the items
    out = {"ok": bool(hits), "site": root, "count": len(hits), "urls": hits[:int(limit)], "robots": state, "how": how,
           "patterns": pats}
    if not hits:
        h = _held(host)
        out["error"] = _held_error(host, h) if h else {
            "code": "NO_URLS" if not urls else "FILTER_MATCHED_NOTHING",
            "message": (f"No sitemap, no catalogue root, and the link walk found no on-site links from {url}." if not urls
                        else f"{len(urls)} urls found, none match filter={filter!r}."),
            "next_steps": ([f"reach('{url}') to see what the page returns: a refusal or a JS shell has no links to walk.",
                            f"If you know where the catalogue is, teach Scout: moves(action='propose', kind='listing_root', scope='{host}', spec={{'path': '/your/path'}})"]
                           if not urls else [f"Loosen the filter, or call site_map('{root}') without one and read `patterns`."])}
    else:
        learn(host, "patterns", [x["pattern"] for x in out["patterns"][:6]])
    return out


def is_listing_page(asked, r):
    """A listing is a page with 10+ distinct on-site links that is still where we asked: a redirect to the homepage or
    to another host is the site saying 'no such page' politely (ui.com/en/products -> store homepage, 2026-10-03)."""
    a, f = urllib.parse.urlsplit(asked), urllib.parse.urlsplit(r.get("url") or asked)
    if f.netloc.lower().removeprefix("www.") != a.netloc.lower().removeprefix("www."):
        return False
    if a.path.strip("/") and (not f.path.strip("/") or re.search(r"/(default|index|home)\.(aspx?|html?|php)$", f.path, re.I)):
        return False
    paths = {urllib.parse.urlsplit(l["url"]).path for l in r.get("links") or []
             if _host(l["url"]).removeprefix("www.") == a.netloc.lower().removeprefix("www.")}
    return len(paths) >= 10


def listing_candidates(url):
    """Listing paths for a host, best first: every locale prefix x every listing root, scored by the product of the two
    parts' scores (the chain picks each part independently), merged with whole listing_path moves. The host's learned
    locale, then the locale in the url it was given, go first with score 1."""
    p = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    host = p.netloc.lower()
    prefixes = [(m["spec"]["prefix"], m["id"], m["score"]) for m in moves("locale_prefix", host)]
    known = [memory(host)["route"].get("locale"), (_LOCALE.match(p.path) or [None])[0]]
    prefixes = [(k, None, 1.0) for k in dict.fromkeys(x for x in known if x)] + [x for x in prefixes if x[0] not in known]
    out = {}
    for pre, pid, ps in prefixes:
        for r in moves("listing_root", host):
            path = pre.rstrip("/") + r["spec"]["path"]
            sc = ps * r["score"]
            if sc > out.get(path, (None, None, -1))[2]:
                out[path] = (path, {"prefix": (pre, pid), "root": (r["spec"]["path"], r["id"])}, sc)
    for m in moves("listing_path", host):
        if m["score"] > out.get(m["spec"]["path"], (None, None, -1))[2]:
            out[m["spec"]["path"]] = (m["spec"]["path"], {"path": (m["spec"]["path"], m["id"])}, m["score"])
    return sorted(out.values(), key=lambda x: -x[2])


def _credit(tried, win):
    """Learn by contrast. No winner teaches nothing (the site has no listing, or refuses us), so nothing is scored.
    With a winner, its parts win, and each loser before it loses only on the part where it differed from the winner:
    /en/products failing next to a winning /products is /en's fault, not /products'."""
    if not win:
        return
    for slot, (_, mid) in win.items():
        if mid:
            move_result(mid, True)
    for parts in tried:
        for slot, (val, mid) in parts.items():
            if mid and (slot not in win or win[slot][0] != val):
                move_result(mid, False)


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
        r = reach(u, _depth=1, _links=True)
        return bool(r["ok"]) and is_listing_page(u, r)

    cands = sorted({u for u in (urls or []) if _LISTING_WORDS.search(urllib.parse.urlsplit(u).path)},
                   key=lambda u: (urllib.parse.urlsplit(u).path.rstrip("/").count("/"), len(u)))
    for u in cands[:3]:
        if is_listing(u):
            learn(host, "listing", u)
            return {"ok": True, "url": u, "source": "sitemap"}
    tried = []
    for path, parts, score in listing_candidates(url)[:8]:
        u = root + path
        if not is_listing(u):
            tried.append(parts)
            continue
        _credit(tried, parts)
        learn(host, "listing", u)
        m = _LOCALE.match(path)
        if m:
            learn(host, "locale", m.group(0))
        return {"ok": True, "url": u, "source": "moves " + "+".join(f"#{v[1]}" for v in parts.values() if v[1]), "score": round(score, 3)}
    return {"ok": False, "candidates": cands[:8], "error": {"code": "NO_LISTING",
            "message": f"No listing page found on {host}: no learned route, no listing-shaped sitemap url, and the top listing_path moves did not render 10+ links.",
            "next_steps": [f"site_map('{root}') and read `patterns`: the busiest shape is usually the item pages.",
                           f"If you know the path, teach Scout: moves(action='propose', kind='listing_path', scope='{host}', spec={{'path': '/your/path'}})"]}}


# ── link families: how a site's own links lead to its items, walked by sampling ─────────────────────────────────────
# Learned by the Depot recipe chain rebuilding 16 state codes from scratch (2026-10-03). A link's family is its path
# with every number-bearing token generalised, so Chapter_1.html and Chapter_14A.html (and NHTOC-I / NHTOC-XIV) are one.
# Sample a family spread across it, never its first links (a list opens with its odd items); let the majority of the
# samples decide what the family leads to, not whichever came back first; look deeper when the samples are indexes;
# rank by yield through every level, or a five-page index outranks 89 chapters of sections.
_ROMAN = r"(?<=[-_/=])[IVXLC]{1,7}(?=[-_./]|$)"
_FUNCTION = frozenset("the of and to in a an or by for shall be is are any as on with that this which may not such from at "
                      "under who its no if than each other all upon has have been".split())


def prose(text):
    """How much text reads like sentences, not a site's menu: mostly function-word density (the, of, shall, any). Prose
    runs ~0.35-0.5; menus and breadcrumbs ~0.15. Capitals and length could not tell them apart."""
    w = re.findall(r"[A-Za-z]{2,}", (text or "")[:800])
    if len(w) < 8:
        return 0.0
    fw = sum(x.lower() in _FUNCTION for x in w) / len(w)
    lower = sum(x[0].islower() for x in w) / len(w)
    return round(min(1.0, fw / 0.35) * (0.5 + 0.5 * lower), 3)


def family_of(u):
    p = urllib.parse.urlsplit(u)
    path = p.path + ("?" + p.query if p.query else "")
    shape = re.sub(_ROMAN, "<n>", re.sub(r"[0-9][0-9A-Za-z]*", "<n>", path))
    return shape if "<n>" in shape else _pattern(u)       # no numbers: siblings under one parent (/products/*)


def _family_rx(shape):
    return re.compile(re.escape(shape).replace(re.escape("<n>"), "[0-9A-Za-z]+").replace(re.escape("*"), "[^/?]+") + "/?$")


def link_families(links, base, nav=()):
    """[(shape, [urls])] for one page's links: same host, not itself, no assets, 3+ distinct urls, biggest first.
    Siblings group under their parent first; the numeric shape names the family only when one shape covers 80% of them
    (Idaho's Title<n>), else it is the parent (/product/* holds RB<n> and crs<n>_<n>). `nav`: families of the page above,
    which on this page are navigation, not content (2026-10-03, MikroTik's category menu on every page)."""
    host, groups = _host(base).removeprefix("www."), {}
    for l in links or []:
        u = l["url"].split("#")[0]
        if _host(u).removeprefix("www.") != host or u.rstrip("/") == base.split("#")[0].rstrip("/") or _FILE.search(u):
            continue
        groups.setdefault(_pattern(u), []).append(u)
    fam = {}
    for parent, us in groups.items():
        us = list(dict.fromkeys(us))
        shapes = Counter(family_of(u) for u in us)
        top, n = shapes.most_common(1)[0]
        if "<n>" in top and n >= 0.8 * len(us):
            fam.setdefault(top, []).extend(u for u in us if family_of(u) == top)
        elif parent != "/":
            fam.setdefault(parent, []).extend(us)
    out = [(k, list(dict.fromkeys(v))) for k, v in fam.items() if k not in nav]
    return sorted([x for x in out if len(x[1]) >= 3], key=lambda x: -len(x[1]))


def _spread(urls, n):
    return list(dict.fromkeys(urls[len(urls) * k // (n + 1)] for k in range(1, n + 1)))


def families(url, want="", depth=2, fams=4):
    """Which of a page's link families leads to good pages, and how many: spread samples, a majority vote on what they
    are (an index of more links, or pages), a look one level deeper under indexes, and yield through every level."""
    if "://" not in url:
        url = "https://" + url
    top = reach(url, "", _depth=1, _links=True)
    if not top["ok"]:
        return {"ok": False, "url": url, "error": top["error"]}

    def sample(u, nav):
        r = reach(u, want, _depth=1, _links=True)
        if not r["ok"]:
            return {"url": u, "kind": r["error"]["code"]}
        fs = link_families(r.get("links"), r["url"], nav)
        own = urllib.parse.urlsplit(r["url"]).path.rstrip("/") + "/"
        kids = {l["url"].split("#")[0] for l in r.get("links") or [] if _host(l["url"]) == _host(r["url"])
                and urllib.parse.urlsplit(l["url"]).path.startswith(own) and urllib.parse.urlsplit(l["url"]).path.rstrip("/") + "/" != own}
        # a page with 3+ children under its own path is a family hub, not an item (AMD's hubs landed as products, 2026-10-03)
        return {"url": r["url"], "kind": "index" if (fs and len(fs[0][1]) >= 10) or len(kids) >= 3 else "page", "chars": r["chars"],
                "prose": prose(r["markdown"]), "title": r.get("title", "")[:80], "_links": r.get("links")}

    def walk(u, links, level, nav=frozenset()):
        out = []
        here = link_families(links, u, nav)
        seen = nav | {f for f, _ in here}                    # what this page already shows is navigation one level down
        for shape, urls in here[:fams]:
            got = [sample(x, seen) for x in _spread(urls, 5 if level == 0 else 3)]
            good = [g for g in got if g["kind"] in ("index", "page")]
            kind, votes = (Counter(g["kind"] for g in good).most_common(1) or [(None, 0)])[0]
            fam = {"family": shape, "links": len(urls), "level": level, "held": f"{len(good)}/{len(got)}", "leads_to": kind,
                   **({"editorial": True} if _EDITORIAL.search(shape) else {}),
                   "samples": [{k: v for k, v in g.items() if not k.startswith("_")} for g in got],
                   "ok": len(good) >= (2 if level == 0 else 1)}
            fam["yield"] = round(len(urls) * len(good) / len(got)) if fam["ok"] else 0
            fam["_rep"] = next((g for g in good if g["kind"] == kind), None)
            out.append(fam)
        out.sort(key=lambda f: -f["yield"])
        for fam in out[:2]:                                  # look deeper under the two best index families
            rep = fam.pop("_rep", None)
            if fam["ok"] and fam["leads_to"] == "index" and rep and level + 1 < depth:
                below = walk(rep["url"], rep["_links"], level + 1, seen)
                best = next((b for b in below if b["ok"]), None)
                fam["below"] = below[:3]
                if best:
                    fam["yield"] = round(fam["yield"] * best["yield"])
        for fam in out:
            fam.pop("_rep", None)
        return sorted(out, key=lambda f: -f["yield"])

    ranked = walk(top["url"], top.get("links"), 0)
    res = {"ok": any(f["ok"] for f in ranked), "url": top["url"], "families": ranked}
    best = next((f for f in ranked if f["ok"] and not f.get("editorial")), None) or next((f for f in ranked if f["ok"]), None)
    if best:
        chain, f = [best["family"]], best
        while f.get("below") and next((b for b in f["below"] if b["ok"]), None):
            f = next(b for b in f["below"] if b["ok"])
            chain.append(f["family"])
        res["best"] = {"chain": chain, "estimated_items": best["yield"],
                       "recipe_hint": [{"reach": top["url"]}] + [st for c in chain[:-1] for st in ({"find": _family_rx(c).pattern}, {"reach": "{url}"})]
                                      + [{"links": chain[-1]}]}
    else:
        res["error"] = {"code": "NO_FAMILY", "message": f"No link family on {top['url']} led to good pages in its samples.",
                        "next_steps": [f"site_map('{top['url']}') for the site's own list of urls",
                                       "if the page is built by script, its links may be missing: see reach's tried trail"]}
    return res


def scout(url, want="", full=False):
    """The whole maze for one url: reach the page, map the site, find its listing, and report what was learned."""
    if "://" not in url:
        url = "https://" + url
    host = _host(url)
    known = recipes(host)                    # what Scout already has for this site comes first: a passing recipe is the answer
    page = reach(url, want, full, _links=True)
    sm = site_map(f"https://{host}/", limit=50)          # the site's map; a map from a deep url stays inside that section
    lst = listing(url, sm.get("urls"))
    # the site's own hosts: ones its page links to that carry its name (regional sites, a store). A host that carries the
    # name but that the site does not link to is a reseller lead, not the maker (meanwellsource.com, estate 2026-10-04)
    stem = host.removeprefix("www.").split(".")[0]
    own = sorted({_host(l["url"]) for l in page.pop("links", None) or [] if _host(l["url"]) != host and len(stem) >= 3
                  and stem in _host(l["url"]).replace("-", "")})
    return {"ok": page["ok"], "page": page, "recipes": known, "own_hosts": own,
            "terrain": {"urls": sm["count"], "patterns": sm["patterns"][:10], "how": sm["how"], "robots": sm["robots"],
                        "listing": lst.get("url"), "sample_urls": sm["urls"][:20], "map_error": sm.get("error"),
                        "item_patterns": memory(host)["route"].get("item_patterns"),
                        "known_wall": memory(host)["route"].get("wall")},
            "recipe": memory(host)}


def recipe(host=""):
    if not host:
        with _db() as c:
            rows = c.execute("""SELECT host, route FROM host WHERE route LIKE '%"hold"%'""").fetchall()
        holds = [{"host": r["host"], **json.loads(r["route"])["hold"]} for r in rows if json.loads(r["route"]).get("hold")]
        return {"holds": holds, "open_enrollments": len(enroll()["enrollments"]), "recent_failures": failures(limit=50),
                "moves": moves(include_dead=True), "readers": readers(), "db": str(DB_PATH)}
    host = _host(host if "://" in host else "https://" + host)
    with _db() as c:
        _ensure_enroll(c)
        e = c.execute("SELECT status, reason, note FROM enroll WHERE host=?", (host,)).fetchone()
    return {"host": host, **memory(host), "order": order(host), "moves": moves(host=host), "recent_failures": failures(host, 10),
            "enrollment": dict(e) if e else None}


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
            "links": "a link family shape from families(), like '/chapter/<n>/' or '/products/*'; last step = every "
                     "matching link on the current page, else the first becomes {url}",
            "jsonld": "{field: dotted path} read from the current page's schema.org data, e.g. {'price': 'offers.price', "
                      "'sku': 'sku'}; optional 'type': 'Product' (default); a name ending in ? is optional",
            "extract": "{field: regex}; group 1 (or the whole match) from the current page; every field must be found, "
                       "except one named with a trailing ? ('socket?'), which is filled only when present"}
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


# Values that are not content (Depot, 2026-10-04: "every check counted, none read"). 29,299 sections landed as a site's
# menu ("Skip navigation Home Documents …"), and a double-escaped character class turned every "t" of 9,308 Alabama
# sections into a space ("he books… o de ermine he accuracy"); 17,126 landed with 0 errors.
_CHROME = re.compile(r"(?i)skip (?:navigation|to (?:main )?content)|menu website search|\bhref=|[.#][\w-]+\s*\{\s*[\w-]+\s*:")


def not_content(text):
    """Why an extracted value is not content ("" when it may be): the page's chrome, or letters lost to a broken cleaner."""
    if _CHROME.search(text[:6000]):
        return "page chrome (menu, markup or a stylesheet)"
    low = text.lower()
    letters = sum(c.isalpha() for c in low)
    if letters > 80 and min(low.count("t"), low.count("e")) < letters * 0.02:
        return "letters missing (a broken cleaner or a double-escaped regex)"
    return ""


def _untail(v, host=""):
    """A name without the page-title tail: "Crucial T705 4TB SSD | CT4000T705SSD5 | crucial.com" -> "Crucial T705 4TB SSD"
    (Depot, 2026-10-03). Only tail segments that name the site or look like a part number are cut."""
    stem = (host or "").removeprefix("www.").split(".")[0].lower()
    parts = re.split(r"\s+[|·–—]\s+", v)
    while len(parts) > 1 and ((stem and stem in parts[-1].lower()) or re.fullmatch(r"[A-Z0-9][A-Z0-9-]{4,}", parts[-1].strip())):
        parts.pop()
    return " | ".join(parts)


def _dig(node, path):
    """'offers.price' through dicts; a list on the way takes its first element."""
    for part in path.split("."):
        if isinstance(node, list):
            node = node[0] if node else None
        node = node.get(part) if isinstance(node, dict) else None
    return node[0] if isinstance(node, list) and node else node


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
        if op == "jsonld" and not (isinstance(st["jsonld"], dict) and st["jsonld"]):
            return f"step {i}: jsonld needs {{field: 'dotted.path'}}"
        if op == "jsonld":
            rxs = []
        if op == "extract" and not (isinstance(st["extract"], dict) and st["extract"]):
            return f"step {i}: extract needs {{field: regex}}"
        for rx in rxs:
            try:
                re.compile(_fill(str(rx or ""), "x", "https://x", True))
            except re.error as e:
                return f"step {i}: regex {rx!r} does not compile: {e}"
            if re.search(r"\[[^\]]*\\\\[a-z][^\]]*\]", str(rx or "")):
                return (f"step {i}: regex {rx!r} has a double-escaped escape inside a character class: '\\\\t' there means a "
                        "backslash or the letter t, so every t is lost (Depot's Alabama text). Use one backslash.")
    if next((next(iter(st)) for st in steps if next(iter(st)) != "input"), None) not in ("reach", "map"):
        return "the first step after any input step must be reach or map: a recipe starts from a url"
    return ""


def run(name, input="", recipe=None, cached=False):
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
            r = reach(_fill(st["reach"], input, url), _fill(st.get("want", ""), input, url, True), full=True, _links=True, cached=cached)
            if not r["ok"]:
                return fail(r["error"]["code"], i, r["error"]["next_steps"][0])
            url, page = r["url"], r
            out = {"url": url, "title": r.get("title", "")[:80],
                   **({"archived": r["archived"]["captured"]} if r.get("archived") else {})}   # read from the Wayback copy of that day
        elif op == "find":
            rx = re.compile(_fill(st["find"], input, url, True), re.I)
            hit = next((l["url"] for l in (page or {}).get("links", []) if rx.search(l["url"]) or rx.search(l.get("text", ""))), None)
            if not hit:
                return fail("FIND_MISS", i, f"no link on {url} matches {st['find']!r}; the page may have changed: recompile")
            url = hit
        elif op == "map":
            sm = site_map(_fill(st["map"], input, url), _fill(st.get("filter", ""), input, url, True), int(st.get("limit", 20000)), cached=cached)
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
        elif op == "links":
            rx = _family_rx(_fill(st["links"], input, url))
            hits = list(dict.fromkeys(l["url"].split("#")[0] for l in (page or {}).get("links", [])
                                      if rx.search(urllib.parse.urlsplit(l["url"]).path + ("?" + urllib.parse.urlsplit(l["url"]).query if urllib.parse.urlsplit(l["url"]).query else ""))))
            if not hits:
                return fail("LINKS_MISS", i, f"no link on {url} is in family {st['links']!r}; the page may have changed: recompile")
            if i == len(rc["steps"]):
                out = {"count": len(hits), "first": hits[0], "urls": hits}
            url = hits[0]
        elif op == "jsonld":
            want_t = str(st.get("type", "Product")).lower()
            nodes = [n for n in (page or {}).get("jsonld") or [] if want_t in str(n.get("@type", "")).lower()]
            got, missing = {}, []
            for field, path in st["jsonld"].items():
                v = _dig(nodes[0], path) if nodes else None
                if v not in (None, "", []):
                    got[field.rstrip("?")] = _clean(_untail(str(v), _host(url)))
                elif not field.endswith("?"):
                    missing.append(field)
            if missing:
                return fail("JSONLD_MISS", i, f"{', '.join(missing)} not in {url}'s schema.org {st.get('type', 'Product')} data"
                            + ("" if nodes else " (the page carries none)") + ": use extract on the page text, or recompile")
            out.update(got)
        elif op == "extract":
            md = re.sub(r"\[excerpt: .*?\]$", "", (page or {}).get("markdown", ""))
            got, missing = {}, []
            for field, rx in st["extract"].items():
                m = re.search(_fill(rx, input, url, True), md, re.I | re.M)
                val = _clean(m.group(1) if m and m.groups() else m.group(0) if m else "")
                if val:
                    got[field.rstrip("?")] = val
                elif not field.endswith("?"):      # "socket?": filled when the page has it, never a failure when not
                    missing.append(field)
            if missing:
                return fail("EXTRACT_MISS", i, f"{', '.join(missing)} not found on {url}; the page layout may have changed: recompile")
            bad = {f: not_content(v) for f, v in got.items() if not_content(v)}
            if bad:
                return fail("BAD_VALUE", i, "; ".join(f"{f}: {why}" for f, why in bad.items()) + f" on {url}: fix the regex and recompile")
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


# ── cached mode: download a site once, then read the copy ───────────────────────────────────────────────────────────
# For pulling a lot from one site: list it, fetch every page once into the mirror at the host's pace (or from the
# Wayback Machine, with no load on the site at all), then read and run recipes against the copy with cached=true.
_JOBS = {}


def _job(c, host):
    c.execute("""CREATE TABLE IF NOT EXISTS cache_job (host TEXT PRIMARY KEY, url TEXT, source TEXT, total INTEGER, fetched INTEGER,
                 failed INTEGER, state TEXT, started REAL, updated REAL, days REAL, how TEXT, pace REAL)""")
    r = c.execute("SELECT * FROM cache_job WHERE host=?", (host,)).fetchone()
    return dict(r) if r else None


def cache(url, filter="", source="auto", max_pages=2000, days=7, action="start", wait=False):
    """action=start: list the site (filter narrows it) and download every page once into Scout's copy, in the
    background, at the host's pace. source: 'archive' (the Wayback Machine: no request to the site), 'live', or 'auto'
    (live, and the archive where the site walls Scout). The copy is kept `days`. action=status | stop."""
    if "://" not in url:
        url = "https://" + url
    host = _host(url)
    with _db() as c:
        j = _job(c, host)
    if action == "status":
        if not j:
            return {"ok": False, "error": {"code": "NO_CACHE_JOB", "message": f"no cache job for {host}", "next_steps": [f"cache('{url}')"]}}
        left = max(0, j["total"] - j["fetched"] - j["failed"])
        if j["state"] == "running" and not (host in _JOBS and _JOBS[host].is_alive()) and time.time() - j["updated"] > 120:
            # the process that ran it is gone (Depot's re-reads died with the one-off script that started them, 2026-10-05)
            j["state"] = "interrupted"
            return {"ok": True, **j, "left": left, "next": f"cache('{j['url']}') again resumes: pages already copied are skipped"}
        return {"ok": True, **j, "left": left, "eta_minutes": round(left * (j["pace"] or 1) / 60, 1) if j["state"] == "running" else 0,
                "next": f"run(recipe, inputs=[...], cached=true) reads the copy" if j["state"] == "done" else "check back with action='status'"}
    if action == "stop":
        with _db() as c:
            c.execute("UPDATE cache_job SET state='stopping' WHERE host=?", (host,))
        return {"ok": True, "host": host, "state": "stopping"}
    if host in _JOBS and _JOBS[host].is_alive():   # one download per site at a time
        return {**cache(url, action="status"), "note": "already downloading"}
    learn(host, "mirror_days", float(days))
    sm = site_map(url, filter, limit=max_pages, archive="only" if source == "archive" else "off" if source == "live" else "auto")
    if not sm["ok"]:
        return {"ok": False, "host": host, "error": sm.get("error")}
    urls = sm["urls"][:int(max_pages)]
    todo = [u for u in urls if not _mirror_get(u)]
    pace = ARCHIVE_PACE if source == "archive" or _held(host) else max(MIN_INTERVAL, float(memory(host)["route"].get("min_interval") or 0))
    with _db() as c:
        _job(c, host)
        c.execute("INSERT OR REPLACE INTO cache_job VALUES (?, ?, ?, ?, 0, 0, 'running', ?, ?, ?, ?, ?)",
                  (host, url, source, len(todo), time.time(), time.time(), float(days), sm["how"], pace))

    def work():
        got = bad = 0
        why = Counter()
        for i, u in enumerate(todo, 1):
            for attempt in range(3):
                with _db() as c:
                    if (_job(c, host) or {}).get("state") == "stopping":
                        break
                ah = _held(_host(ARCHIVE)) if source != "live" else None
                if ah and ah.get("until"):          # the archive asked us to slow down: wait it out, never knock through it
                    if wait:
                        print(f"  the archive is on hold for {int(ah['until'] - time.time())} s; waiting", flush=True)
                    time.sleep(max(1, min(ah["until"] - time.time() + 1, 3600)))
                r = reach(u, _depth=1, _archive_only=(source == "archive"))
                last = (r["tried"] or [{}])[-1].get("outcome", (r.get("error") or {}).get("code", "?"))
                if last not in ("archive_throttled", "archive_held"):
                    break
            ok = r["ok"] or any(t["outcome"] == "want_miss" for t in r["tried"])
            got, bad = (got + 1, bad) if ok else (got, bad + 1)
            if not ok:
                why[last] += 1
            if i % 10 == 0 or i == len(todo):
                with _db() as c:
                    c.execute("UPDATE cache_job SET fetched=?, failed=?, updated=?, how=? WHERE host=?",
                              (got, bad, time.time(), sm["how"] + (f"; failed: {dict(why)}" if why else ""), host))
                if wait:
                    print(f"  {i}/{len(todo)} fetched {got}, failed {bad}" + (f" ({dict(why)})" if why else ""), flush=True)
        with _db() as c:
            st = (_job(c, host) or {}).get("state")
            c.execute("UPDATE cache_job SET fetched=?, failed=?, updated=?, state=?, how=? WHERE host=?",
                      (got, bad, time.time(), "stopped" if st == "stopping" else "done",
                       sm["how"] + (f"; failed: {dict(why)}" if why else ""), host))

    if wait or not todo:
        work()
    else:
        _JOBS[host] = threading.Thread(target=work, daemon=True, name=f"cache:{host}")
        _JOBS[host].start()
    return {"ok": True, "host": host, "listed": len(urls), "already_cached": len(urls) - len(todo), "to_fetch": len(todo),
            "source": source, "pace_s": pace, "eta_minutes": round(len(todo) * pace / 60, 1), "kept_days": days, "how": sm["how"],
            "next": f"cache('{url}', action='status') to follow it; then run(recipe, inputs=[...], cached=true) or reach(url, cached=true)"}


def run_many(name, inputs, cached=False):
    """One recipe over many inputs: one RESULT line each. With cached=true nothing leaves the machine."""
    res = [run(name, str(x), cached=cached) for x in inputs]
    return {"results": [r["result"] for r in res], "ok": sum(r["ok"] for r in res), "failed": sum(not r["ok"] for r in res)}


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
    assert any(m["kind"] == "listing_root" for m in moves()) and any(m["kind"] == "locale_prefix" for m in moves())
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
    assert _LOCALE.match("/us/en/products").group(0) == "/us/en" and _LOCALE.match("/en-gb/x").group(0) == "/en-gb"
    assert not _LOCALE.match("/products") and _LOCALE.match("/en").group(0) == "/en"
    propose("locale_prefix", "*", {"prefix": "/en"}); propose("locale_prefix", "*", {"prefix": ""})
    propose("listing_root", "*", {"path": "/products"})
    c = [x[0] for x in listing_candidates("https://b.com/us/en/thing")]
    assert c[0].startswith("/us/en/") and "/us/en/products" in c[:3] and "/en/products" in c and "/products" in c, c[:8]
    en = next(m for m in moves("locale_prefix") if m["spec"]["prefix"] == "/en")
    pr = next(m for m in moves("listing_root") if m["spec"]["path"] == "/products")
    _credit([{"prefix": ("/en", en["id"]), "root": ("/products", pr["id"])}], {"prefix": ("", None), "root": ("/products", pr["id"])})
    assert next(m for m in moves("locale_prefix") if m["id"] == en["id"])["tries"] == en["tries"] + 1
    assert next(m for m in moves("listing_root") if m["id"] == pr["id"])["wins"] == pr["wins"] + 1, "the shared part is not blamed"
    before = moves("listing_root")
    _credit([{"root": ("/products", pr["id"])}], None)
    assert moves("listing_root") == before, "no winner, no lesson"
    rid = pr["id"]
    for _ in range(PRUNE_TRIES + 2):
        move_result(rid, False)
    assert any(m["id"] == rid for m in moves("listing_root")), "a composing part is ranked down, never retired"
    many = [{"url": f"https://b.com/p/{i}"} for i in range(12)]
    assert is_listing_page("https://b.com/products", {"url": "https://b.com/products", "links": many})
    assert not is_listing_page("https://b.com/en/products", {"url": "https://b.com/", "links": many})
    assert not is_listing_page("https://b.com/en/products", {"url": "https://b.com/Default.asp", "links": many})
    assert not is_listing_page("https://b.com/en/products", {"url": "https://store.b2.com/us", "links": many})
    assert not is_listing_page("https://b.com/products", {"url": "https://b.com/products", "links": many[:3] * 5})
    assert family_of("https://x.gov/Laws/Chapter_14A.html") == family_of("https://x.gov/Laws/Chapter_1.html") == "/Laws/Chapter_<n>.html"
    assert family_of("https://x.gov/rsa/NHTOC-XIV.htm") == family_of("https://x.gov/rsa/NHTOC-I.htm")
    assert family_of("https://x.com/products/dime-evo") == "/products/*"
    links = [{"url": f"https://x.gov/ch/{i}/"} for i in range(5)] + [{"url": "https://x.gov/img/a1.png"}] * 4 + [{"url": "https://y.gov/ch/1/"}]
    assert link_families(links, "https://x.gov/") == [("/ch/<n>/", [f"https://x.gov/ch/{i}/" for i in range(5)])]
    assert link_families(links, "https://x.gov/", nav={"/ch/<n>/"}) == []
    prods = [{"url": f"https://m.com/product/{x}"} for x in ("RB433AH", "hap_ac3", "crs354_48g", "cap_ax", "RB5009")]
    assert link_families(prods, "https://m.com/") == [("/product/*", [p["url"] for p in prods])]
    assert _family_rx("/ch/<n>/").search("/ch/14A/") and not _family_rx("/ch/<n>/").search("/ch/14/x/")
    assert _spread(list(range(10)), 3) == [2, 5, 7]
    assert prose("The courts enumerated in section 1-101 are courts of record and shall keep a seal.") > 0.6
    assert prose("Home Products Support Contact Search Login Cart Menu Store Locator Careers News") < 0.3
    assert _check_steps([{"reach": "https://x.gov/"}, {"links": "/ch/<n>/"}]) == ""
    mo = "Blocked 203.0.113.7 for assistance EMAIL: webmaster@example.gov"
    assert _blocked(mo) and judge({"markdown": mo}) == "blocked" and _outcome(Status(200, blocked=True)) == "blocked"
    assert not _blocked("1.210. A person blocked from office by law shall not be imprisoned unless by authority of law.")
    assert not _blocked("Your IP address is shown in the footer")
    t = _throttled("mo.example", "blocked", None, mo)
    assert _held("mo.example") and _hold_contact("mo.example") == "webmaster@example.gov" and t["min_interval"] >= 5
    assert reach("https://mo.example/x")["error"]["code"] == "HELD", "a held host is not fetched"
    m = memory("mo.example"); h = m["route"]["hold"]; h["since"] -= HOLD_S + 1; h["until"] -= HOLD_S + 1; _save("mo.example", m)
    assert not _held("mo.example"), "an ended automatic hold lets one probe through"
    t2 = _throttled("mo.example", "blocked", None, mo)
    assert t2["until"] - time.time() > HOLD_S * 1.5 and t2["min_interval"] >= 2 * t["min_interval"], "still blocked: hold doubles"
    hold("mo.example", lift=True); hold("mo.example", reason="unblock requested by email")
    m = memory("mo.example"); assert _held("mo.example") and not m["route"]["hold"]["until"], "a hand-set hold has no end"
    e = diagnose("https://mo.example/x", "", [{"outcome": "blocked", "status": 200, "detail": mo}])
    assert e["code"] == "BLOCKED" and "webmaster@example.gov" in e["next_steps"][0], e
    global KEYS_FILE
    KEYS_FILE = DB_PATH.parent / "keys.env"
    r = enroll("request", "legiscan.example", {"route": "enroll_free", "name": "LegiScan API", "signup_url": "https://legiscan.example/register",
               "base_url": "https://api.legiscan.example/", "auth": {"kind": "query", "name": "key"}, "cost": "free tier",
               "sample": "https://api.legiscan.example/?op=getStateList&key=KEY"})
    assert r["ok"] and r["status"] == "needed" and r["access"]["key"] == "LEGISCAN" and r["access"]["sample"].endswith("op=getStateList"), r
    assert [x["host"] for x in enroll()["enrollments"]] == ["legiscan.example"] and "legiscan.example/register" in enroll()["enrollments"][0]["todo"]
    assert enroll("done", "legiscan.example")["error"]["code"] == "NO_KEY"
    os.environ["SCOUT_KEY_LEGISCAN"] = "s3cr3t-k3y"
    u, hdr, sec = _auth("https://api.legiscan.example/?op=x")
    assert "key=s3cr3t-k3y" in u and sec == ["s3cr3t-k3y"] and _auth("https://legiscan.example/x")[0] == "https://legiscan.example/x"
    assert _scrub("echo s3cr3t-k3y back", sec) == "echo [key] back"
    del os.environ["SCOUT_KEY_LEGISCAN"]
    KEYS_FILE.write_text("LEGISCAN=from-file\n"); KEYS_FILE.chmod(0o644)
    assert "LEGISCAN" not in _keys() and keys_file_state().startswith("too open"), "a readable keys file is ignored"
    KEYS_FILE.chmod(0o600)
    assert _keys()["LEGISCAN"] == "from-file"
    st = _access_steps("legiscan.example", "CHALLENGE_WALL")
    assert st and st[0].startswith("Use LegiScan API (enrolled"), st
    KEYS_FILE.unlink()
    assert "needs a person to enroll" in _access_steps("legiscan.example", "CHALLENGE_WALL")[0]
    enroll("request", "ecfr.example", {"route": "open_api", "name": "eCFR API", "base_url": "https://ecfr.example/api/", "sample": "https://ecfr.example/api/v1/titles"})
    assert _access_steps("ecfr.example", "CHALLENGE_WALL")[0].startswith("Use the site's official route, eCFR API")
    assert [x["host"] for x in enroll()["enrollments"]] == ["legiscan.example"], "an open route files no enrollment"
    assert [h["host"] for h in recipe()["holds"]] == ["mo.example"] and recipe()["open_enrollments"] == 1
    assert _EDITORIAL.search("/en/blogs/N/*") and _EDITORIAL.search("/en/newsroom/press-releases/*") and not _EDITORIAL.search("/en/products/*")
    assert _convert(b"<html><title>=====</title><h1>AMD Ryzen 9 9950X3D</h1><p>x</p></html>", "text/html", "https://a.com/")["title"] == "AMD Ryzen 9 9950X3D"
    assert _STALL.search("Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at https://x") and _STALL.search("The read operation timed out")
    sm = "Sitemap\nhttps://b.com/p/1 https://b.com/p/2\nhttps://b.com/p/3"
    assert re.findall(r"https?://[^\s<>\"')\]]+", sm) == ["https://b.com/p/1", "https://b.com/p/2", "https://b.com/p/3"]
    g = globals()
    saved = {k: g[k] for k in ("robots", "_sitemap_urls", "_walk_links", "listing")}

    class _RP:
        def site_maps(self): return ["https://c.example/sitemap.xml"]
        def can_fetch(self, a, u): return True
    starts = []

    def _fake_walk(st, host, on, rp, budget, urls):
        starts.append(st[0])
        if "products" in st[0]:
            urls += [f"https://c.example/en/products/p{i}" for i in range(4)]
        return 1
    g.update(robots=lambda u: (_RP(), "ok"), _walk_links=_fake_walk, listing=lambda url, urls=None: {"ok": True, "url": "https://c.example/en/products"},
             _sitemap_urls=lambda sm, seen, cap, depth=0: [f"https://c.example/en/blogs/{i}/post-{i}" for i in range(60)])
    try:
        m = site_map("https://c.example")
    finally:
        g.update(saved)
    assert "https://c.example/en/products" in starts and "catalogue root" in m["how"], m["how"]
    assert m["patterns"][0]["pattern"] == "/en/products/*" and m["patterns"][-1].get("editorial"), m["patterns"]
    akamai = "Access Denied You don't have permission to access this server. Reference #18.5b2d1102.1791056400.2f9c1a"
    assert _blocked(akamai) and judge({"markdown": akamai}) == "blocked"
    assert _EDITORIAL.search("/en/partners/*") and _EDITORIAL.search("/request-a-quote") and not _EDITORIAL.search("/c/en/us/support/*")
    _listing_cache("https://c.example", ["https://c.example/p/1", "https://c.example/p/2"], "test")
    m = site_map("https://c.example")
    assert m["robots"] == "cached" and m["count"] == 2 and "reused the listing" in m["how"], m
    p_ = {"markdown": "x" * 400, "title": "T", "links": [], "final_url": "https://m.example/p"}
    _mirror_put("https://m.example/p", p_, "direct")
    tr = []
    pg, rd = _try_readers("https://m.example/p", "", tr)
    assert rd == "mirror" and tr[0]["copy_of"] == "direct", tr
    assert _try_readers("https://m.example/p", "zzz", [])[0] is None, "a want-miss on today's copy does not ask the site again"
    assert reach("https://nc.example/x", cached=True)["error"]["code"] == "NOT_CACHED"
    assert site_map("https://nc.example", cached=True)["error"]["code"] == "NOT_CACHED"
    _mirror_put("https://m.example/q", {"markdown": "y" * 400, "title": "Q", "links": [], "final_url": "https://m.example/q"}, "archive")
    assert reach("https://m.example/q", cached=True)["reader"] == "mirror"
    miss = reach("https://m.example/q", "zzz", cached=True)
    assert not miss["ok"] and [t["reader"] for t in miss["tried"]] == ["mirror"], "cached mode never leaves the machine"
    learn("m.example", "mirror_days", 30.0)
    assert _mirror_days("https://m.example/q") == 30.0
    assert cache("https://nj.example", action="status")["error"]["code"] == "NO_CACHE_JOB"
    e = diagnose("https://a.example/x", "", [{"outcome": "archive_throttled", "reader": "archive"}])
    assert e["code"] == "ARCHIVE_THROTTLED", e
    assert _decode("§ 1-1".encode("utf-8"), "text/html; charset=utf-16") == "§ 1-1", "a utf-16 claim without a BOM is ignored"
    assert _decode("§ 1".encode("cp1252"), "") == "§ 1"
    assert judge({"markdown": "Performing security verification. This website uses a security service to protect against malicious bots. " * 3}) == "challenge"
    ld = _convert(b'<html><script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Product","name":"T705 4TB",'
                  b'"sku":"CT4000T705SSD5","offers":[{"@type":"Offer","price":"565.99"}]}]}</script><body><p>x</p></body></html>', "text/html", "https://c.example/p")["jsonld"]
    assert _dig(ld[0], "offers.price") == "565.99" and _dig(ld[0], "sku") == "CT4000T705SSD5"
    assert _check_steps([{"reach": "https://c.example/p"}, {"jsonld": {"price": "offers.price", "gtin?": "gtin13"}}]) == ""
    assert "section" in _STEER_STOP and not [w for w in ["the", "section", "and"] if w not in _STEER_STOP]
    assert not_content("Skip navigation Home Documents Senate Assembly " * 3) and not_content(
        "he books of accoun shall be kep a he office of he coun y reasurer for regular inspec ion by he audi or and he board")
    assert not_content("The books of account shall be kept at the office of the county treasurer for inspection by the auditor.") == ""
    assert "double-escaped" in _check_steps([{"reach": "https://a.example/"}, {"extract": {"text": "^([^\\\\t]+)$"}}])
    assert _check_steps([{"reach": "https://a.example/"}, {"extract": {"text": "^([^\\t]+)$"}}]) == ""
    import email.utils
    t3 = _throttled("ra.example", "rate_limited", email.utils.formatdate(time.time() + 600, usegmt=True), "")
    assert t3["until"] - time.time() > 500, "an HTTP-date Retry-After is honoured"
    assert _untail("Crucial T705 4TB PCIe Gen5 NVMe M.2 SSD | CT4000T705SSD5 | crucial.com", "www.crucial.com") == "Crucial T705 4TB PCIe Gen5 NVMe M.2 SSD"
    assert _untail("Wiring Devices | Lighting Controls", "www.leviton.com") == "Wiring Devices | Lighting Controls"
    print("scout: ok")


if __name__ == "__main__":
    demo()
