"""The laws of the United States, read from each legislature's own site.

`scout-mcp laws` lists the states; `scout-mcp laws <state> [--limit N] [--out file.jsonl]` reads one state's whole code
and writes one JSON line per section: {"citation", "heading", "text", "url"}. Each state is a small Python script in
laws/<state>.json (the same recipes the sibling crawler runs): a `sections()` generator that yields id, heading, text and
url, using the helpers below. Every request goes through Scout's gate: robots.txt and its crawl delay, holds, and the
recipe's own pace, which is the slower of the two.
"""
import hashlib, io, json, os, re, time, urllib.error, urllib.request, zipfile
from pathlib import Path

from . import scout as S

LAWS = Path(__file__).with_name("laws")
BULK = S.DB_PATH.parent / "bulk"
MAX_BYTES = 500_000_000            # a whole code on one page is 46 MB (North Dakota); a state's zip can be hundreds
_LAST = {}

from html.parser import HTMLParser as _HP
class _Block(_HP):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol", "dd", "dt", "blockquote", "pre"}
    def __init__(self, id_, cls, attr, every):
        _HP.__init__(self, convert_charrefs=True)
        self.id_, self.cls, self.attr, self.every = id_, cls, attr, every
        self.tag, self.depth, self.done, self.skip, self.found = None, 0, False, 0, []
    def _match(self, a):
        if self.id_ and a.get("id") != self.id_:
            return False
        if self.cls:
            toks = (a.get("class") or "").split()
            if not any(t == self.cls or (self.cls.endswith("*") and t.startswith(self.cls[:-1])) for t in toks):
                return False
        if self.attr and a.get(self.attr[0]) != self.attr[1]:
            return False
        return bool(self.id_ or self.cls or self.attr)
    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if not self.depth:
            if self._match(dict(attrs)):
                self.tag, self.depth = tag, 1
                self.found.append([])
            return
        if tag == self.tag:
            self.depth += 1          # only the container's own tag is counted: an unclosed <p> inside cannot end it early
        if tag in ("script", "style"):
            self.skip += 1
        if tag in self.BLOCK:
            self.found[-1].append(chr(10))
    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.found[-1].append(chr(10))
        if tag == self.tag:
            self.depth -= 1
            if not self.depth and not self.every:
                self.done = True
    def handle_data(self, d):
        if self.depth and not self.skip:
            self.found[-1].append(d)
def _lines(parts):
    return chr(10).join(l for l in (" ".join(x.split()) for x in "".join(parts).split(chr(10))) if l)
def block_texts(html, id=None, cls=None, attr=None):
    """The text of EVERY element matching all that is given: its id, its class ("qsatxt*" = any class starting so), an
    attribute (name, value). One string per element: tags dropped, scripts skipped, one line per paragraph."""
    p = _Block(id, cls, attr, True)
    p.feed(html if isinstance(html, str) else html.decode("utf-8", "ignore"))
    p.close()
    return [t for t in (_lines(f) for f in p.found) if t]
def block_text(html, id=None, cls=None, attr=None, every=False):
    """The text of the first matching element (every=True: of all of them, joined). An empty string when none matches."""
    p = _Block(id, cls, attr, every)
    p.feed(html if isinstance(html, str) else html.decode("utf-8", "ignore"))
    p.close()
    return chr(10).join(t for t in (_lines(f) for f in p.found) if t)


def states():
    """[{state, citation, source}] for every state Scout can read."""
    out = []
    for f in sorted(LAWS.glob("*.json")):
        sp = json.loads(f.read_text())
        out.append({"state": f.stem, "citation": sp.get("citation"), "source": (sp.get("doc") or {}).get("source_url")})
    return out


def _pace(url, delay):
    allowed, state = S._polite(url)                      # robots.txt, its crawl delay, Scout's learned pace
    if not allowed:
        raise IOError(f"robots.txt ({state}) asks crawlers not to read {url}")
    host = S._host(url)
    wait = _LAST.get(host, 0) + delay - time.time()     # and the recipe's own promise to the source
    if wait > 0:
        time.sleep(wait)
    _LAST[host] = time.time()


_AIA = {}


def _aia_context(host):
    """An SSL context that also trusts the intermediate certificate a server forgot to send, fetched from the url its own
    certificate names (Connecticut's and Illinois' servers omit it). Verification stays on; None when it cannot be repaired."""
    if host not in _AIA:
        _AIA[host] = None
        try:
            import ssl, tempfile
            pem = ssl.get_server_certificate((host, 443))
            with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
                f.write(pem); f.flush()
                issuers = ssl._ssl._test_decode_cert(f.name).get("caIssuers") or ()
            inter = "".join(ssl.DER_cert_to_PEM_cert(urllib.request.urlopen(u, timeout=20).read()) for u in issuers[:2])
            if inter:
                ctx = ssl.create_default_context()
                ctx.load_verify_locations(cadata=inter)
                _AIA[host] = ctx
        except Exception:  # noqa: BLE001 — no repair: the original error stands
            pass
    return _AIA[host]


def _open(url, delay, stream_to=None):
    """Raw bytes (or a path, when stream_to is given), paced; a 429/503 waits as asked and tries again."""
    if S._held(S._host(url)):
        raise IOError(f"{S._host(url)} is on hold in Scout: {S._held(S._host(url))}")
    for attempt in range(6):
        _pace(url, delay * (2 ** attempt if attempt else 1))
        try:
            wire, extra, _ = S._auth(url)                    # an enrolled API's key goes on the wire only, never in a url we keep
            rq = urllib.request.Request(wire, headers={**S.HEADERS, **extra})
            try:
                r = urllib.request.urlopen(rq, timeout=120, context=_AIA.get(S._host(url)))
            except urllib.error.URLError as e:
                if "local issuer" not in str(e.reason) or not _aia_context(S._host(url)):
                    raise
                r = urllib.request.urlopen(rq, timeout=120, context=_AIA[S._host(url)])
            with r:
                ctype = r.headers.get("Content-Type", "")
                if stream_to:
                    tmp = Path(str(stream_to) + ".part")
                    with open(tmp, "wb") as f:
                        while chunk := r.read(1 << 22):
                            f.write(chunk)
                    tmp.rename(stream_to)
                    return stream_to, ctype
                return r.read(MAX_BYTES), ctype
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 5:
                ra = (e.headers or {}).get("Retry-After")
                time.sleep(min(int(ra) if ra and ra.isdigit() else 30 * (attempt + 1), 600))
                continue
            raise IOError(f"HTTP {e.code} from {url}") from None
    raise IOError(f"{url} kept answering 429/503")


def _make_helpers(delay, log):
    def fetch(url, render=False):
        """(kind, body): html|json (text), pdf (its text, every page), zip (bytes)."""
        raw, ctype = _open(url, delay)
        if raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        if "pdf" in ctype.lower() or raw[:5] == b"%PDF-":
            import logging
            from pypdf import PdfReader
            logging.getLogger("pypdf").setLevel(logging.ERROR)
            return "pdf", "\n\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(raw)).pages)
        if "zip" in ctype.lower() or raw[:4] == b"PK\x03\x04":
            return "zip", raw
        return ("json" if "json" in ctype.lower() else "html"), S._decode(raw, ctype)

    def fetch_json(url):
        return json.loads(fetch(url)[1])

    zips = {}

    def _zip(url):
        if url not in zips:
            BULK.mkdir(parents=True, exist_ok=True)
            path = BULK / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".zip")
            if not path.exists() or time.time() - path.stat().st_mtime > 86400:
                log(f"downloading {url} to {path}")
                _open(url, delay, stream_to=path)
            zips[url] = zipfile.ZipFile(path)
        return zips[url]

    def zip_names(url, pattern=None):
        """Entry names of a bulk zip, downloaded to disk once a day."""
        return [n for n in _zip(url).namelist() if not pattern or re.search(pattern, n, re.I)]

    def zip_read(url, name):
        data = _zip(url).read(name)
        return data.decode("utf-16", "replace") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8", "replace")

    return {"__name__": "recipe", "fetch": fetch, "fetch_json": fetch_json, "log": log, "block_text": block_text,
            "block_texts": block_texts, "zip_names": zip_names, "zip_read": zip_read}


def sections(state, log=print, limit=None):
    """One state's sections as {citation, heading, text, url}: refused when the text is a page's chrome or letters are lost."""
    sp = json.loads((LAWS / f"{state}.json").read_text())
    g = _make_helpers(max(float(sp.get("delay") or 1.0), 0.5), log)
    exec(compile(sp["script"], f"laws/{state}.json", "exec"), g)
    n = 0
    for it in g["sections"]():
        if not isinstance(it, dict) or not it.get("id") or not it.get("text"):
            continue
        try:
            cit = sp["citation"].format(id=re.sub(r"\s+", "", str(it["id"])), **(sp.get("vars") or {}), **(it.get("vars") or {}))
        except KeyError as e:
            log(f"citation needs {e} for {it['id']}"); continue
        why = S.not_content(it["text"])
        if why:
            log(f"skipped {cit}: {why}"); continue
        yield {"citation": cit, "heading": str(it.get("heading") or ""), "text": it["text"], "url": it.get("url") or ""}
        n += 1
        if limit and n >= limit:
            return


def demo():
    page = '<nav>Skip navigation</nav><div id="law"><p>(1) A person may vote.</p><p>(2) Ballots are secret.</p></div>'
    assert block_text(page, id="law") == "(1) A person may vote.\n(2) Ballots are secret."
    assert block_texts(page, cls="nope") == []
    assert {s["state"] for s in states()} >= {"us-tx", "us-nv"}, "the state recipes ship with the package"
    print("laws: ok")
