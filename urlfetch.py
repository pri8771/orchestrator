"""Engine-side URL fetching: ground truth for links in the user's prompt.

Why this exists (a real failure): a prompt said "Build me something like
ordinal: https://www.tryordinal.com". None of the agent CLIs have web access,
so nobody could load the site. The panel free-associated for three rounds
(one model decided it was Bitcoin NFTs), the prompt-contract itself recorded
"nobody has successfully loaded tryordinal.com... best-reasoned guess, not
confirmed fact", and the entire 21-phase run built a well-made app of the
WRONG product. The fix is structural: the ENGINE (which does have network
access) fetches every http(s) URL found in initial_prompt.md at run start,
caches the readable page text under <app>/docs/fetched/, and injects it into
the product-definition phases as ground truth. A fetch failure injects an
explicit "UNVERIFIED — say so" warning instead of letting the panel guess.

Contract:
  * Standard library only, matching the rest of the engine.
  * Nothing here may ever take a run down — every public function that touches
    the network or the filesystem catches everything and degrades to an error
    string / empty result instead of raising.

Wired in orchestrator.py:
  * `_run_app_pipeline` calls `fetch_all` (or reuses a fresh <24h cache) right
    after the docs backfill and stashes `build_url_context(results)` on
    cfg["_url_context"].
  * `build_context` appends cfg["_url_context"] to the phase prompt, but ONLY
    for the phases in URL_CONTEXT_PHASES — the early product-definition phases
    where a hallucinated reading of a linked product poisons the whole run.
"""

import datetime as _dt
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# Hard limits. Prompts are human-written; more than a handful of URLs means
# something pathological (a pasted document), so cap instead of hammering.
MAX_URLS = 5
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_BYTES = 500_000
MAX_TEXT_CHARS = 20_000          # per fetched page, after HTML stripping
MAX_CONTEXT_CHARS = 25_000       # total cfg["_url_context"] budget
CACHE_FRESH_HOURS = 24

# Some sites serve an empty shell (or a 403) to obvious bots; a browser-ish
# User-Agent gets the same HTML a human would see.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

# Phases that receive the fetched content. Deliberately ONLY the phases that
# define WHAT is being built — later phases inherit their decisions, and
# injecting 25k chars into every build turn would just bloat worker prompts.
URL_CONTEXT_PHASES = ("prompt_contract", "product_research",
                      "per_app_product_brief", "app_features")

CONTEXT_HEADER = ("===== FETCHED URL CONTENT "
                  "(ground truth from links in the user's prompt) =====")

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\}]+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------
def extract_urls(text):
    """Every distinct http(s) URL in `text`, in order of first appearance,
    capped at MAX_URLS. Trailing sentence punctuation is stripped so
    "see https://x.com." doesn't produce a 404."""
    urls = []
    seen = set()
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?")
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= MAX_URLS:
            break
    return urls


def should_inject(phase_key):
    """True when this phase's prompt should carry the fetched URL content."""
    return phase_key in URL_CONTEXT_PHASES


# ---------------------------------------------------------------------------
# HTML -> readable text
# ---------------------------------------------------------------------------
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe",
              "nav", "footer"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "header", "aside",
               "blockquote", "pre", "table", "tr", "ul", "ol", "dl", "dt",
               "dd", "figcaption", "br", "hr", "form", "label", "button",
               "option", "figure"}


class _TextExtractor(HTMLParser):
    """Strips a page down to its readable text: drops script/style/nav/footer
    subtrees entirely, marks headings, bullets list items, and inserts line
    breaks at block boundaries so the output reads like a document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts = []
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif self._skip_depth == 0:
            if tag in _HEADING_TAGS:
                self.chunks.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag == "li":
                self.chunks.append("\n- ")
            elif tag in _BLOCK_TAGS:
                self.chunks.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif self._skip_depth == 0 and (tag in _HEADING_TAGS or tag in _BLOCK_TAGS):
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.chunks.append(data)


def _parse_html(html_text):
    """(title, readable_text) — best-effort, never raises."""
    parser = _TextExtractor()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:  # noqa: BLE001 - real-world HTML is hostile; keep what we got
        pass
    raw = "".join(parser.chunks)
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    return title, text[:MAX_TEXT_CHARS]


def html_to_text(html_text):
    """Readable text for an HTML document (script/style/nav/footer dropped,
    headings and list items kept, whitespace collapsed, capped)."""
    return _parse_html(html_text)[1]


def _looks_like_html(content_type, body):
    if "html" in (content_type or "").lower():
        return True
    head = (body or "").lstrip()[:512].lower()
    return head.startswith("<!doctype") or "<html" in head or "<body" in head


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_url(url, timeout=DEFAULT_TIMEOUT, max_bytes=DEFAULT_MAX_BYTES):
    """Fetch one URL and reduce it to readable text.

    Returns {"url", "ok", "title", "text", "error"} and NEVER raises: DNS
    failures, timeouts, 4xx/5xx, TLS errors, binary garbage — everything
    degrades to ok=False plus a short error string. Redirects are followed
    (urllib's default opener). Only http/https is allowed, so a prompt can
    never make the engine read file:// paths."""
    result = {"url": url, "ok": False, "title": "", "text": "", "error": ""}
    try:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            result["error"] = "unsupported URL scheme: %r" % scheme
            return result
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes)
            content_type = resp.headers.get("Content-Type", "") or ""
            charset = resp.headers.get_content_charset() or "utf-8"
        try:
            body = raw.decode(charset, errors="replace")
        except (LookupError, TypeError):
            body = raw.decode("utf-8", errors="replace")
        if _looks_like_html(content_type, body):
            title, text = _parse_html(body)
        else:
            title, text = "", re.sub(r"\n{3,}", "\n\n", body).strip()[:MAX_TEXT_CHARS]
        result["ok"] = True
        result["title"] = title
        result["text"] = text
        if not text:
            result["error"] = "fetched but no readable text extracted"
    except Exception as exc:  # noqa: BLE001 - never let a bad URL take the run down
        result["error"] = ("%s: %s" % (type(exc).__name__, exc))[:300]
        if isinstance(exc, urllib.error.HTTPError):
            try:
                exc.close()  # HTTPError doubles as a response; free its socket
            except Exception:  # noqa: BLE001
                pass
    return result


# ---------------------------------------------------------------------------
# Cache files under <app>/docs/fetched/
# ---------------------------------------------------------------------------
def cache_filename(url):
    """Deterministic <slugified-domain-path>.md name for a URL's cache file."""
    parts = urllib.parse.urlsplit(url)
    slug = re.sub(r"[^a-z0-9]+", "-",
                  ("%s%s" % (parts.netloc, parts.path)).lower()).strip("-")
    return (slug or "url")[:80] + ".md"


def _write_cache_file(cache_dir, res):
    fname = cache_filename(res["url"])
    path = os.path.join(cache_dir, fname)
    if res.get("ok"):
        status = "OK (%d chars)" % len(res.get("text") or "")
    else:
        status = "FAILED: %s" % (res.get("error") or "unknown error")
    header = ("<!-- fetched-url-cache v1 -->\n"
              "# %s\n\n"
              "- URL: %s\n"
              "- Fetched: %s\n"
              "- Status: %s\n\n"
              "---\n\n" % (res.get("title") or res["url"], res["url"],
                           _dt.datetime.now().isoformat(timespec="seconds"),
                           status))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + (res.get("text") or ""))
    return path


def fetch_all(prompt_text, cache_dir, timeout=DEFAULT_TIMEOUT,
              max_bytes=DEFAULT_MAX_BYTES, fetcher=None):
    """Fetch every URL in `prompt_text` (extract_urls rules), write one
    markdown cache file per URL under `cache_dir`, and return the list of
    result dicts. Never raises; a broken disk just skips the cache write."""
    results = []
    try:
        urls = extract_urls(prompt_text)
        if not urls:
            return results
        fetch = fetcher or fetch_url
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            cache_dir = None
        for url in urls:
            try:
                res = fetch(url, timeout=timeout, max_bytes=max_bytes)
            except Exception as exc:  # noqa: BLE001 - fetcher contract is "never raises", but belt+braces
                res = {"url": url, "ok": False, "title": "", "text": "",
                       "error": ("%s: %s" % (type(exc).__name__, exc))[:300]}
            results.append(res)
            if cache_dir:
                try:
                    _write_cache_file(cache_dir, res)
                except Exception:  # noqa: BLE001 - cache is an optimization, not a dependency
                    pass
    except Exception:  # noqa: BLE001
        pass
    return results


def _parse_cache_file(path):
    """Inverse of _write_cache_file; None when the file isn't one of ours."""
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return None
    head, sep, body = content.partition("\n---\n")
    if not sep:
        return None
    res = {"url": "", "ok": False, "title": "", "text": body.strip(),
           "error": ""}
    for line in head.splitlines():
        line = line.strip()
        if line.startswith("- URL: "):
            res["url"] = line[len("- URL: "):].strip()
        elif line.startswith("# "):
            res["title"] = line[2:].strip()
        elif line.startswith("- Status: "):
            status = line[len("- Status: "):].strip()
            if status.startswith("OK"):
                res["ok"] = True
            elif status.startswith("FAILED: "):
                res["error"] = status[len("FAILED: "):]
    return res if res["url"] else None


def load_cached(cache_dir):
    """Parse every cache file in `cache_dir` back into result dicts (sorted by
    filename for determinism). Never raises; missing dir -> []."""
    results = []
    try:
        for fname in sorted(os.listdir(cache_dir)):
            if not fname.endswith(".md"):
                continue
            res = _parse_cache_file(os.path.join(cache_dir, fname))
            if res:
                results.append(res)
    except OSError:
        pass
    return results


def cache_is_fresh(cache_dir, urls, max_age_hours=CACHE_FRESH_HOURS):
    """True only when EVERY url already has a cache file that is younger than
    `max_age_hours` AND recorded a successful fetch. Any missing / stale /
    FAILED entry -> False, so failures are retried on the next run instead of
    sticking for a day (the original tryordinal failure mode). Never raises."""
    try:
        if not urls or not os.path.isdir(cache_dir):
            return False
        import time as _time
        cutoff = _time.time() - float(max_age_hours) * 3600.0
        for url in urls:
            path = os.path.join(cache_dir, cache_filename(url))
            if not os.path.isfile(path) or os.path.getmtime(path) < cutoff:
                return False
            res = _parse_cache_file(path)
            if not res or not res.get("ok"):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Prompt-context assembly
# ---------------------------------------------------------------------------
def build_url_context(results, max_total_chars=MAX_CONTEXT_CHARS):
    """One clearly-delimited prompt block from fetch results.

    Successful fetches contribute their readable text (total budget
    `max_total_chars`, split across pages in order); failures contribute an
    explicit warning so agents label their guesses as UNVERIFIED instead of
    presenting them as fact. Empty input -> ""."""
    if not results:
        return ""
    parts = ["\n" + CONTEXT_HEADER]
    remaining = max(0, int(max_total_chars))
    for res in results:
        url = res.get("url", "")
        if res.get("ok") and res.get("text"):
            if remaining <= 0:
                continue
            title = res.get("title") or ""
            label = ("%s — %s" % (url, title)) if title else url
            text = res["text"][:remaining]
            remaining -= len(text)
            parts.append("\n--- %s ---\n%s" % (label, text))
            if len(text) < len(res["text"]):
                parts.append("[...truncated to fit the context budget]")
        else:
            parts.append(
                "\nURL %s could not be fetched — treat any claims about its "
                "content as UNVERIFIED guesses and say so. (%s)"
                % (url, res.get("error") or "unknown error"))
    parts.append(
        "\nThe fetched text above is REAL content from the linked page(s) — "
        "treat it as authoritative over any prior assumption about what those "
        "links contain.")
    return "\n".join(parts)
