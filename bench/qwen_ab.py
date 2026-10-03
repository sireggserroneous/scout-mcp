"""A/B: can a small model drive Scout? A = the site breakdown as instructions + raw tools. B = one card + run.

    OPENAI_BASE_URL=http://localhost:8080/v1 OPENAI_API_KEY=x MODEL=qwen3.5-4b python bench/qwen_ab.py
Any OpenAI-compatible server with tool calling works (llama.cpp server --jinja, vLLM, LiteLLM, Ollama)."""
import json, os, re, sys, time, urllib.request
os.environ.setdefault("SCOUT_DB", os.path.join(os.path.dirname(__file__), "bench.db"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scout_mcp import scout as S

BASE = os.environ.get("OPENAI_BASE_URL", "http://localhost:8080/v1").rstrip("/")
KEY = os.environ.get("OPENAI_API_KEY", "none")
MODEL = os.environ.get("MODEL", "qwen3.5-4b")
MODELS = ["hAP ax2", "RB5009UG+S+IN", "hEX S", "CCR2004-1G-12S+2XS", "cAP ax"]


def chat(messages, tools):
    body = {"model": MODEL, "messages": messages, "tools": tools, "max_tokens": 800}
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json", "User-Agent": "scout-ab/1.0"})
    return json.load(urllib.request.urlopen(req, timeout=900))["choices"][0]["message"]


def fn(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


RUN = fn("run", "Run a compiled Scout recipe. Returns one RESULT line.",
         {"recipe": {"type": "string"}, "input": {"type": "string"}}, ["recipe"])
SITE_MAP = fn("site_map", "A site's real urls. filter is a case-insensitive regex.",
              {"url": {"type": "string"}, "filter": {"type": "string"}}, ["url"])
REACH = fn("reach", "Read a page as markdown. want is a regex the page must carry.",
           {"url": {"type": "string"}, "want": {"type": "string"}}, ["url"])


def call_tool(name, a):
    if name == "run":
        r = S.run(a.get("recipe", ""), a.get("input", ""))
        return {k: r[k] for k in ("result", "fix") if k in r}
    if name == "site_map":
        r = S.site_map(a.get("url", ""), a.get("filter", ""), 1000)
        return {k: r.get(k) for k in ("ok", "count", "urls", "error")}
    if name == "reach":
        r = S.reach(a.get("url", ""), a.get("want", ""))
        return {k: r.get(k) for k in ("ok", "url", "title", "markdown", "error")}
    return {"error": f"no tool {name}"}


def card_a(model):
    return (f"Get the specs of the MikroTik model \"{model}\".\n"
            "1. Call site_map with url https://mikrotik.com and filter /product/.\n"
            "2. Make a key from the model name: lowercase it, delete the word plus, delete every character that is not a letter or digit.\n"
            "3. For each url, make the same key from its last path segment. Pick the url whose key equals the model's key.\n"
            "4. If no url matches, reply: RESULT: fail | mikrotik.com/specs | <model> | NO_MATCH. Stop.\n"
            "5. Call reach with that url and want Specification.\n"
            "6. In the markdown find these values: Suggested price, CPU, Size of RAM, Max power consumption.\n"
            "7. Reply with one line only: RESULT: ok | mikrotik.com/specs | <model> | url=<url>; price=<price>; cpu=<cpu>; ram=<ram>; max_power=<max power>")


def card_b(model):
    return S.card("mikrotik.com/specs", "model", model)["card"]


def trial(card, tools, max_turns=8):
    msgs, calls, t0 = [{"role": "user", "content": card}], [], time.time()
    for _ in range(max_turns):
        m = chat(msgs, tools)
        tcs = m.get("tool_calls") or []
        msgs.append({"role": "assistant", "content": m.get("content") or "", **({"tool_calls": tcs} if tcs else {})})
        if not tcs:
            return (m.get("content") or "").strip(), calls, time.time() - t0
        for tc in tcs:
            try:
                a = json.loads(tc["function"]["arguments"] or "{}")
            except ValueError:
                a = {}
            calls.append(f"{tc['function']['name']}({json.dumps(a)})")
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(call_tool(tc["function"]["name"], a))[:40000]})
    return "(ran out of turns)", calls, time.time() - t0


def fields(line):
    return dict(re.findall(r"(price|cpu|ram|max_power)=([^;]+)", line or ""))


truth = {m: fields(S.run("mikrotik.com/specs", m)["result"]) for m in MODELS}
print("truth", json.dumps(truth), flush=True)
for arm, mk, tools in (("B", card_b, [RUN]), ("A", card_a, [SITE_MAP, REACH])):
    for m in MODELS:
        try:
            reply, calls, dt = trial(mk(m), tools)
        except Exception as e:  # noqa: BLE001
            reply, calls, dt = f"ERROR {type(e).__name__} {e}", [], 0
        got = fields(reply)
        exact = bool(truth[m]) and all(got.get(k, "").strip() == v.strip() for k, v in truth[m].items())
        print(json.dumps({"arm": arm, "model": m, "exact": exact, "calls": calls, "secs": round(dt), "reply": reply[:300]}), flush=True)
