"""Moneyshorts news updater.

Reads the Business Standard RSS feed, asks Gemini for short summaries, and
writes money_shorts.txt, the file your app downloads.
Uses only Python's standard library, so nothing needs installing.
The key comes from the GEMINI_API_KEY secret and is never printed or saved.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# News sources. "cat" is the category used when a feed has no sections of its own.
# Before a public launch keep only the sources you have permission to use.
# Sources in use. Only sources we have written permission for belong here.
FEEDS = [
    # Oquilia: written permission received. Their terms: headline and summary only, credit
    # "Oquilia" with the link, never edit items marked protected (we leave those out, and
    # also leave out enforcement and courts), refresh every few hours, drop dead links.
    {"name": "Oquilia", "url": "https://www.oquilia.com/api/v1/news?limit=50",
     "kind": "oquilia", "cat": "Markets"},
    # Capital Data Wire: its attribution page says content is free to republish in full or in part
    # with credit and a link, with no fee or permission. It is US-focused and AI-written, so we only
    # keep items that mention India, and cards still carry the "not investment advice" note.
    {"name": "Capital Data Wire", "url": "https://capitaldatawire.com/feeds/main.rss",
     "cat": "Markets", "verbatim": True, "max_words": 2500,
     "keywords": r"\b(India|Indian|NSE|BSE|Sensex|Nifty|rupee|RBI|SEBI|Mumbai)\b"},
]

# Switched off until we have permission. To use one, move its line up into FEEDS.
OTHER_FEEDS = [
    # PIB is parked: its feed gives only headlines (no description), and the release text on its
    # pages loads through JavaScript, so there is nothing to summarise yet. The address below
    # returned Hindi items in a live run; the English feed may use a different Lang number.
    {"name": "PIB", "url": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
     "cat": "Economy", "verbatim": True},
    {"name": "Business Standard", "url": "https://www.business-standard.com/rss/latest.rss", "kind": "bs"},
    {"name": "Mint", "url": "https://www.livemint.com/rss/markets", "cat": "Markets"},
    {"name": "Mint", "url": "https://www.livemint.com/rss/companies", "cat": "Companies"},
    {"name": "Reserve Bank of India", "url": "https://rbi.org.in/pressreleases_rss.xml", "cat": "Economy"},
    {"name": "SEBI", "url": "https://www.sebi.gov.in/sebirss.xml", "cat": "Markets"},
]

# Oquilia allowed us in writing (email of 12 Nov 2026) to shorten their summary to about 50-60
# words and to shorten their headline, if facts and meaning stay the same, nothing is added, and
# no claim reads stronger than theirs. Protected and enforcement items stay exactly as supplied
# (we leave those out). If shortening fails our checks, their text is shown as supplied.
OQUILIA_REWRITE = True
MAX_VERBATIM_WORDS = 130  # Oquilia summaries are shown as supplied, so very long ones are skipped
OUT_FILE = "money_shorts.txt"
# Tried in this order; if a name is not available for your key, the next one is used.
MODELS = [m for m in [os.environ.get("GEMINI_MODEL", ""),
                      "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"] if m]
API_KEY = os.environ.get("GEMINI_API_KEY", "")
BS = "{https://www.business-standard.com/rss/}"

KEEP_SECTIONS = {"Markets", "Companies", "Economy", "Industry", "Finance"}
MAX_NEW = 10       # new stories to add per run
PER_FEED = 10      # at most this many new stories from one feed per run
MAX_TOTAL = 40     # stories kept in the file
MIN_WORDS = 35     # shorter than 50 only when the source is thin
MAX_WORDS = 60
PAUSE = 8          # seconds between AI calls (free plan is rate limited)

COLORS = {"Positive": "0F766E", "Neutral": "B7791F", "Negative": "9B2C2C"}
TRENDS = {  # decorative picture only, NOT real price data
    "Positive": [0.1, 0.25, 0.2, 0.5, 0.45, 0.8, 0.95],
    "Neutral": [0.5, 0.55, 0.45, 0.5, 0.47, 0.52, 0.5],
    "Negative": [0.95, 0.85, 0.7, 0.6, 0.4, 0.3, 0.1],
}

SYSTEM = """You write short stock-market news cards for retail investors in India, in English and Hindi.
Rules for all text:
- Use ONLY the headline and description given. Never add facts, numbers, names, causes or predictions that are not in them.
- Write in your own words, in plain neutral language. Do not copy sentences from the source.
- Do not give investment advice. Never say buy, sell, hold, target price or recommend.

title: a punchy, curiosity-driven headline of at most 12 words. Lead with the most striking real fact in the source. You may pose a question or tease the "why" or "how", but the headline must still state the key fact. Never exaggerate or mislead. No exclamation marks, no ALL CAPS, no clickbait phrases. Every number must come from the source. Do not reuse the source headline.
summary: 50 to 60 words, never more than 60. Include the most important facts. If the source is thin, write fewer words rather than guessing or padding, but never fewer than 35.
impact: Positive, Neutral or Negative for the fundamentals of the company or sector in the story. Use Neutral when unclear or when the story is not about a company or sector.
why: at most 8 words explaining the impact.
company: the main company named in the headline, or an empty string if none.
chips: up to 3 key figures copied exactly from the text, each with a short label (for example label "Net profit", value "Rs 545 crore"). Use an empty list if there are no clear figures.
hi: the same card in natural, simple Hindi (Devanagari) for everyday Indian readers, not a word-by-word translation. Keep company names and terms like IPO, NSE, Sensex in English letters. Write numbers with digits 0-9 and use words like करोड़ and लाख. hi.title has at most 14 words and the same curiosity-driven style. hi.summary has at most 75 words. hi.chips must have the same number and order as chips, with Hindi labels and values (for example "₹545 करोड़").
Reply with JSON only:
{"title": "...", "summary": "...", "impact": "...", "why": "...", "company": "...", "chips": [{"label": "...", "value": "..."}], "hi": {"title": "...", "summary": "...", "why": "...", "chips": [{"label": "...", "value": "..."}]}}"""


# ---------- helpers ----------
def fix_mojibake(s):
    """Repair text like 'â‚¹' that should be the rupee sign."""
    if "â" in s:
        try:
            return s.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
    return s


def clean(s):
    s = fix_mojibake(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def words(s):
    return len(s.split())


def ago(iso):
    try:
        m = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() // 60)
    except Exception:
        return ""
    if m < 60:
        return f"{max(m, 1)}m"
    if m < 1440:
        return f"{m // 60}h"
    return f"{m // 1440}d"


def fetch_feed(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Moneyshorts learning project)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def text_of(item, tag):
    el = item.find(tag)
    return clean(el.text) if el is not None and el.text else ""


OQ_SECTIONS = {"markets": "Markets", "corporate": "Companies", "companies": "Companies",
               "economy": "Economy"}  # any other category goes under Finance


def parse_oquilia(raw, feed):
    out, seen_cats = [], {}
    for it in json.loads(raw).get("items", []):
        cat = str(it.get("category", "")).lower()
        seen_cats[cat] = seen_cats.get(cat, 0) + 1
        if it.get("protected") or cat in ("enforcement", "legal"):
            continue  # protected items must not be edited, so we do not use them
        try:
            pub = datetime.fromisoformat(str(it.get("publishedAt", "")).replace("Z", "+00:00")
                                         ).astimezone(timezone.utc).isoformat()
        except Exception:
            pub = ""
        out.append({
            "title": clean(it.get("title", "")), "url": clean(it.get("url", "")),
            "desc": clean(it.get("summary", "")), "published": pub,
            "feed": feed["name"], "feed_url": feed["url"], "section": OQ_SECTIONS.get(cat, "Finance"),
            "subsection": "", "source": feed["name"], "premium": False, "verbatim": True,
        })
    print("  Oquilia categories in the feed:", seen_cats)
    return out


def parse_items(xml_bytes, feed):
    if feed.get("kind") == "oquilia":
        return parse_oquilia(xml_bytes, feed)
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter("item"):
        pub = ""
        try:
            pub = parsedate_to_datetime(text_of(it, "pubDate")).astimezone(timezone.utc).isoformat()
        except Exception:
            pass
        out.append({
            "title": text_of(it, "title"),
            "url": text_of(it, "link"),
            "desc": text_of(it, "description"),
            "published": pub,
            "feed": feed["name"],
            "feed_url": feed["url"],
            "section": text_of(it, BS + "section") or feed.get("cat", ""),
            "subsection": text_of(it, BS + "subsection"),
            "source": text_of(it, BS + "source") or feed["name"],
            "premium": text_of(it, BS + "premium") == "true",
            "verbatim": bool(feed.get("verbatim")), "kw": feed.get("keywords", ""),
            "max_words": feed.get("max_words", MAX_VERBATIM_WORDS),
        })
    return out


def reject_reason(i):
    """Why a story is not used, or None if it is wanted."""
    if i["section"] not in KEEP_SECTIONS:
        return "other section"
    if i["source"] == "Sponsored Content":
        return "sponsored"
    if not i["title"] or not i["url"].startswith("https://"):
        return "no title or link"
    if words(i["desc"]) < 8:
        return "description under 8 words"
    if i.get("verbatim") and words(i["desc"]) > i.get("max_words", MAX_VERBATIM_WORDS):
        return "description too long"
    if i.get("kw") and not re.search(i["kw"], i["title"] + " " + i["desc"], re.I):
        return "not finance or India related"
    if i.get("verbatim") and DEVANAGARI.search(i["title"]):
        return "Hindi title"
    return None


def wanted(i):
    return reject_reason(i) is None


def category(i):
    t = i["title"]
    if i["subsection"] == "Quarterly Results" or re.search(r"\bQ[1-4]\b|\bresults?\b", t, re.I):
        return "Earnings"
    if i["subsection"] == "IPO" or re.search(r"\bIPO\b", t):
        return "IPO"
    if i["section"] == "Economy":
        return "Economy"
    return i["section"]


# ---------- Gemini ----------
def _post(model, body):
    """Returns (status, result): 'ok', 'missing' (model not found) or 'fail'."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
            return "ok", json.loads(resp["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  model {model} not available for this key")
                return "missing", None
            if e.code in (429, 500, 503) and attempt < 3:
                print(f"  AI busy (code {e.code}), waiting...")
                time.sleep(30 * (attempt + 1))
                continue
            print(f"  AI error code {e.code} ({model})")
            return "fail", None
        except (KeyError, IndexError, ValueError):
            return "fail", None
    return "fail", None


def call_json(system, user):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    for model in list(MODELS):
        status, result = _post(model, body)
        if status == "ok":
            MODELS.remove(model)
            MODELS.insert(0, model)  # remember the one that works
            return result
        if status == "fail":
            return None
    return None


def call_gemini(title, desc, note=""):
    return call_json(SYSTEM, f"Headline: {title}\nDescription: {desc}\n{note}")


def trim_to_limit(summary):
    if words(summary) <= MAX_WORDS:
        return summary
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", summary):
        if words((out + " " + s).strip()) > MAX_WORDS:
            break
        out = (out + " " + s).strip()
    return out


def nums(s):
    return re.findall(r"\d[\d,]*\.?\d*", s)


HYPE = re.compile(r"won't believe|shocking|secret|must[- ]buy|multibagger|rocket|jackpot|"
                  r"you need to know|blast|bloodbath", re.I)
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def hclean(s):
    """Clean Hindi text and turn Devanagari digits into 0-9 so numbers can be checked."""
    return clean(s).translate(DIGITS)


def numbers_ok(text, source):
    return all(t.replace(",", "") in source for t in nums(text))


def good_title(t, source, max_words):
    return (3 <= words(t) <= max_words and "!" not in t and not t.isupper()
            and not HYPE.search(t) and numbers_ok(t, source))


def clean_hindi(h, source, lenient=False):
    """Return the checked Hindi card, or None if anything looks wrong."""
    if not isinstance(h, dict):
        return None
    summary = hclean(str(h.get("summary", "")))
    title = hclean(str(h.get("title", "")))
    if not (DEVANAGARI.search(summary) and DEVANAGARI.search(title)):
        return None
    low, high, tmax = (8, 220, 26) if lenient else (25, 100, 20)
    if not (low <= words(summary) <= high) or not good_title(title, source, tmax):
        return None
    if not numbers_ok(summary, source):
        return None
    return {"title": title, "summary": summary, "why": hclean(str(h.get("why", "")))[:80],
            "chips": h.get("chips") if isinstance(h.get("chips"), list) else []}


def check(res, title, desc, need_hi=True, verbatim=False):
    """Return a cleaned result, or None if it fails our safety checks."""
    if not isinstance(res, dict) or not isinstance(res.get("summary"), str):
        return None
    # verbatim = another newsroom's headline and summary, shown exactly as supplied
    summary = clean(res["summary"]) if verbatim else trim_to_limit(clean(res["summary"]))
    if not verbatim and words(summary) < MIN_WORDS:
        return None
    if not verbatim and re.search(r"\b(should (buy|sell)|target price|we recommend|recommended to (buy|sell))\b",
                 summary, re.I):
        return None
    source = (title + " " + desc).replace(",", "")
    if not verbatim and not numbers_ok(summary, source):
        return None
    new_title = title if verbatim else clean(str(res.get("title", "")))
    if not verbatim and not good_title(new_title, source, 12):
        new_title = title  # fall back to the original headline
    hi = clean_hindi(res.get("hi"), source, lenient=verbatim)
    if need_hi and hi is None:
        return None
    chips, chips_hi = {}, {}
    hi_chips = hi["chips"] if hi else []
    for n, c in enumerate((res.get("chips") or [])[:3]):
        if not isinstance(c, dict):
            continue
        label, value = clean(str(c.get("label", "")))[:20], clean(str(c.get("value", "")))[:20]
        tokens = nums(value)
        # keep a chip only if every number in it really appears in the source
        if label and value and tokens and all(t.replace(",", "") in source for t in tokens):
            chips[label] = value
            h = hi_chips[n] if n < len(hi_chips) and isinstance(hi_chips[n], dict) else {}
            h_label, h_value = hclean(str(h.get("label", "")))[:20], hclean(str(h.get("value", "")))[:20]
            if not (h_value and nums(h_value) and numbers_ok(h_value, source)):
                h_value = value
            chips_hi[h_label or label] = h_value
    impact = res.get("impact") if res.get("impact") in COLORS else "Neutral"
    company = clean(str(res.get("company", "")))
    if company.lower() not in title.lower():
        company = ""
    out = {"title": new_title, "summary": summary, "impact": impact,
           "why": clean(str(res.get("why", "")))[:60], "company": company, "chips": chips,
           "hi": None}
    if hi:
        out["hi"] = {"title": hi["title"], "summary": hi["summary"], "why": hi["why"],
                     "chips": chips_hi}
    return out


HEDGE = re.compile(r"alleg|accus|\border|complaint|reportedly|claim|probe|notice|raid|suspect|"
                   r"charge|investigat|expected|likely|\bmay\b|\bcould\b|proposed|set to|\bplan",
                   re.I)


def check_short(res, title, desc):
    """Checks a shortened headline and summary. Returns None if anything looks wrong."""
    if not isinstance(res, dict):
        return None
    source = (title + " " + desc).replace(",", "")
    src_words = words(desc)
    summary = clean(str(res.get("summary", "")))
    if src_words <= MAX_WORDS:
        if words(summary) > src_words:  # shortening must never make it longer
            summary = desc
    else:
        summary = trim_to_limit(summary)
    if words(summary) < min(MIN_WORDS, max(10, int(src_words * 0.6))):
        return None
    if not numbers_ok(summary, source):
        return None
    if HEDGE.search(desc) and not HEDGE.search(summary):
        return None  # a hedge such as "alleged" or "expected" was lost
    new_title = clean(str(res.get("title", "")))
    if not (3 <= words(new_title) <= words(title) and "!" not in new_title
            and not new_title.isupper() and numbers_ok(new_title, source)
            and (not HEDGE.search(title) or HEDGE.search(new_title))):
        new_title = title  # keep their headline if ours is not clearly safe
    r = check(dict(res, title=new_title, summary=summary), title, desc,
              need_hi=False, verbatim=True)
    if r is None:
        return None
    r["title"], r["summary"] = new_title, summary
    return r


def oquilia_result(i):
    title, desc = i["title"], i["desc"]
    if OQUILIA_REWRITE:
        r = check_short(call_json(OQ_SHORT_SYSTEM, f"Headline: {title}\nSummary: {desc}"), title, desc)
        if r is not None:
            r["verbatim_used"] = False
            return r
        print("  shortening did not pass checks: using their text as supplied")
    raw = call_json(OQ_SYSTEM, f"Headline: {title}\nSummary: {desc}")
    if isinstance(raw, dict):
        raw = dict(raw, title=title, summary=desc)
    r = check(raw, title, desc, need_hi=False, verbatim=True)
    if r is not None:
        r["verbatim_used"] = True
    return r


def make_story(i, r):
    name = re.sub(r"\b(LIMITED|LTD|PVT)\b\.?", "", r["company"].upper()).strip()
    tk = ""
    for w in (name or i["section"].upper()).split():
        if len((tk + " " + w).strip()) > 16:
            break
        tk = (tk + " " + w).strip()
    tk = tk or (name or i["section"].upper())[:16]
    src = i["feed"]
    if i["source"] and i["source"] != i["feed"]:
        src += f" / {i['source']}"
    story = {
        "v": 3, "cat": category(i), "tk": tk, "title": r["title"], "summary": r["summary"],
        "source": src, "published": i["published"], "ago": ago(i["published"]),
        "impact": r["impact"], "why": r["why"], "url": i["url"], "chips": r["chips"],
        "trend": TRENDS[r["impact"]], "color": COLORS[r["impact"]], "premium": i["premium"],
    }
    if i.get("verbatim"):  # came from a syndication or official feed
        story["verbatim"] = bool(r.get("verbatim_used"))
        story["src_title"], story["src_summary"] = i["title"], i["desc"][:600]
    if r["hi"]:
        story.update({"title_hi": r["hi"]["title"], "summary_hi": r["hi"]["summary"],
                      "why_hi": r["hi"]["why"], "chips_hi": r["hi"]["chips"]})
    return story


OQ_SHORT_SYSTEM = """You help a small news app for retail investors in India. You are given a headline and a summary written by another publisher. We may SHORTEN both, on strict terms. Reply with JSON only.
- title: a shorter version of the headline, never longer than the original. Same facts and meaning, nothing added. It must not make any claim stronger than the original: an allegation, complaint, order, estimate or plan must not read as a proven fact. No questions, teasers, hype, exclamation marks or ALL CAPS. Keep every hedge word (alleged, ordered, expected, may, reportedly and so on).
- summary: if the original is longer than 60 words, shorten it to 50 to 60 words. If it is 60 words or fewer, keep it as it is. Same facts and meaning, nothing added: no new causes, no predictions, no opinions. Keep every hedge.
- impact: Positive, Neutral or Negative for the fundamentals of the company or sector in the story. Use Neutral when unclear or when the story is not about a company or sector. This is our own analysis.
- why: at most 8 words explaining the impact.
- company: the main company named in the headline, or an empty string.
- chips: up to 3 key figures copied exactly from the text, each with a short label.
- hi: a faithful Hindi (Devanagari) translation of YOUR shortened title and summary, with the same meaning and nothing added, removed or softened. Keep every name and number. Keep company names and terms like IPO, NSE, Sensex in English letters. Write numbers with digits 0-9. hi.why is the Hindi of why, and hi.chips has the same number and order as chips with Hindi labels and values.
Never give investment advice.
{"title": "...", "summary": "...", "impact": "...", "why": "...", "company": "...", "chips": [{"label": "...", "value": "..."}], "hi": {"title": "...", "summary": "...", "why": "...", "chips": [{"label": "...", "value": "..."}]}}"""

OQ_SYSTEM = """You help a small news app for retail investors in India. You are given a headline and a summary written by another newsroom. Do NOT rewrite or shorten them. Reply with JSON only:
- impact: Positive, Neutral or Negative for the fundamentals of the company or sector in the story. Use Neutral when unclear or when the story is not about a company or sector. This is our own analysis.
- why: at most 8 words explaining the impact.
- company: the main company named in the headline, or an empty string.
- chips: up to 3 key figures copied exactly from the text, each with a short label.
- hi: a faithful Hindi (Devanagari) translation of the headline and the summary. Keep the same meaning, with nothing added, removed or softened, and keep every name and number. Keep company names and terms like IPO, NSE, Sensex in English letters. Write numbers with digits 0-9. hi.why is the Hindi of why, and hi.chips has the same number and order as chips with Hindi labels and values.
Never give investment advice.
{"impact": "...", "why": "...", "company": "...", "chips": [{"label": "...", "value": "..."}], "hi": {"title": "...", "summary": "...", "why": "...", "chips": [{"label": "...", "value": "..."}]}}"""

HINDI_FAITHFUL_SYSTEM = """You translate a news headline and summary from English into natural Hindi (Devanagari) faithfully. Keep the same meaning. Add nothing, remove nothing and soften nothing. Keep every name and number. Keep company names and terms like IPO, NSE, Sensex in English letters. Write numbers with digits 0-9. chips: same number and order as the input chips, with Hindi labels and values.
Reply with JSON only: {"title": "...", "summary": "...", "why": "...", "chips": [{"label": "...", "value": "..."}]}"""


HINDI_SYSTEM = """You translate finished stock-market news cards from English into natural, simple Hindi (Devanagari) for everyday Indian readers. Do not translate word by word. Add no facts and no numbers. The summary must have at most 75 words. Keep company names and terms like IPO, NSE, Sensex in English letters. Write numbers with digits 0-9 and use words like करोड़ and लाख. title: at most 14 words, curiosity-driven like the English one. chips: same number and order as the input chips, with Hindi labels and values (for example "₹545 करोड़").
Reply with JSON only: {"title": "...", "summary": "...", "why": "...", "chips": [{"label": "...", "value": "..."}]}"""


def add_hindi(story):
    """Translate a finished English card. Returns True if the Hindi passed the checks."""
    card = {"title": story["title"], "summary": story["summary"], "why": story["why"],
            "chips": [{"label": k, "value": v} for k, v in story["chips"].items()]}
    source = (story["title"] + " " + story["summary"] + " "
              + " ".join(story["chips"].values())).replace(",", "")
    v = "src_summary" in story  # from a syndication feed: translate faithfully
    h = clean_hindi(call_json(HINDI_FAITHFUL_SYSTEM if v else HINDI_SYSTEM,
                              json.dumps(card, ensure_ascii=False)), source, lenient=v)
    story["hi_tries"] = story.get("hi_tries", 0) + 1
    if h is None:
        return False
    chips_hi = {}
    for n, (k, v) in enumerate(story["chips"].items()):
        c = h["chips"][n] if n < len(h["chips"]) and isinstance(h["chips"][n], dict) else {}
        label, value = hclean(str(c.get("label", "")))[:20], hclean(str(c.get("value", "")))[:20]
        if not (value and nums(value) and numbers_ok(value, source)):
            value = v
        chips_hi[label or k] = value
    story.update({"title_hi": h["title"], "summary_hi": h["summary"],
                  "why_hi": h["why"], "chips_hi": chips_hi})
    return True


def link_alive(url):
    """False only when the page is gone (404 or 410). Network trouble keeps the story."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0 (Moneyshorts learning project)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        return e.code not in (404, 410)
    except Exception:
        return True


# ---------- Economy figures (World Bank, CC BY 4.0) ----------
# The World Bank's dataset terms allow reuse under CC BY 4.0 with attribution in the form
# "The World Bank: Dataset name: Data source", and no suggestion that it endorses the app.
# Some indicators come from third parties, so check each indicator page for its licence label.
ECON_FILE = "economy.json"
WB_URL = "https://api.worldbank.org/v2/country/IND/indicator/{code}?format=json&per_page=15"
ECON_MAX_AGE_YEARS = 4  # skip a figure whose latest year is older than this
ECON_ATTRIBUTION = ("The World Bank: World Development Indicators (CC BY 4.0). "
                    "Not endorsed by The World Bank.")
# code, English name, Hindi name, kind, English note, Hindi note
ECONOMY = [
    ("NY.GDP.MKTP.KD.ZG", "GDP growth (annual %)", "जीडीपी वृद्धि (वार्षिक %)", "pct",
     "How fast India's economy grew during the year, after adjusting for price changes.",
     "कीमतों में बदलाव को हटाकर, साल के दौरान भारत की अर्थव्यवस्था कितनी तेज़ी से बढ़ी।"),
    ("FP.CPI.TOTL.ZG", "Inflation, consumer prices (annual %)", "महँगाई, उपभोक्ता कीमतें (वार्षिक %)", "pct",
     "How much consumer prices rose over the year. The RBI watches this when it sets interest rates.",
     "साल में उपभोक्ता कीमतें कितनी बढ़ीं। ब्याज दरें तय करते समय आरबीआई इस पर नज़र रखता है।"),
    ("SL.UEM.TOTL.ZS", "Unemployment rate", "बेरोज़गारी दर", "pct",
     "The share of people in the workforce who want a job but cannot find one (a modelled estimate).",
     "श्रमबल में उन लोगों का हिस्सा जो नौकरी चाहते हैं पर पा नहीं रहे (मॉडल आधारित अनुमान)।"),
    ("FR.INR.LEND", "Lending interest rate", "उधारी ब्याज दर", "pct",
     "The rate banks typically charge their best customers on loans.",
     "बैंक अपने सबसे अच्छे ग्राहकों से कर्ज़ पर आम तौर पर जो दर लेते हैं।"),
    ("FR.INR.RINR", "Real interest rate", "वास्तविक ब्याज दर", "pct",
     "The lending rate after taking out inflation. It shows the true cost of borrowing.",
     "महँगाई घटाने के बाद की कर्ज़ दर। यह उधार लेने की असली लागत दिखाती है।"),
    ("PA.NUS.FCRF", "Exchange rate (₹ per US$)", "विनिमय दर (₹ प्रति US$)", "inr",
     "How many rupees one US dollar bought, on average, during the year.",
     "साल में औसतन एक अमेरिकी डॉलर के बदले कितने रुपये मिले।"),
    ("FI.RES.TOTL.CD", "Total reserves incl. gold", "कुल भंडार (सोना सहित)", "usd_bn",
     "Foreign currency and gold held by the country. These reserves help the RBI manage the rupee.",
     "देश के पास रखी विदेशी मुद्रा और सोना। ये भंडार आरबीआई को रुपये को सँभालने में मदद करते हैं।"),
    ("BN.CAB.XOKA.GD.ZS", "Current account balance (% of GDP)", "चालू खाता शेष (जीडीपी का %)", "pct",
     "India's income and spending with the rest of the world. A negative number means India pays out more than it earns abroad.",
     "बाकी दुनिया के साथ भारत की कमाई और खर्च। नकारात्मक संख्या का मतलब है कि भारत विदेश से कमाने से ज़्यादा बाहर भेजता है।"),
    ("NE.EXP.GNFS.ZS", "Exports of goods and services (% of GDP)", "वस्तुओं और सेवाओं का निर्यात (जीडीपी का %)", "pct",
     "The value of goods and services India sells abroad, compared with the size of the economy.",
     "भारत जो वस्तुएँ और सेवाएँ विदेश में बेचता है उनका मूल्य, अर्थव्यवस्था के आकार की तुलना में।"),
    ("BX.KLT.DINV.WD.GD.ZS", "Foreign direct investment, net inflows (% of GDP)", "प्रत्यक्ष विदेशी निवेश, शुद्ध आवक (जीडीपी का %)", "pct",
     "Money that foreign investors put directly into businesses in India, compared with the size of the economy.",
     "विदेशी निवेशक भारत के कारोबारों में सीधे जितना पैसा लगाते हैं, अर्थव्यवस्था के आकार की तुलना में।"),
    ("NE.GDI.TOTL.ZS", "Gross capital formation (% of GDP)", "सकल पूँजी निर्माण (जीडीपी का %)", "pct",
     "How much of the economy goes into new factories, roads, machines and homes.",
     "अर्थव्यवस्था का कितना हिस्सा नए कारखानों, सड़कों, मशीनों और मकानों में जाता है।"),
    ("NY.GNS.ICTR.ZS", "Gross savings (% of GDP)", "सकल बचत (जीडीपी का %)", "pct",
     "How much of the country's income is saved instead of spent. Savings help fund investment.",
     "देश की आमदनी का कितना हिस्सा खर्च होने के बजाय बचाया जाता है। बचत निवेश के लिए पैसा जुटाने में मदद करती है।"),
]


def wb_latest_two(code):
    req = urllib.request.Request(WB_URL.format(code=code),
                                 headers={"User-Agent": "Mozilla/5.0 (Bazaar Brief)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    rows = data[1] or []
    vals = [(int(x["date"]), float(x["value"])) for x in rows if x.get("value") is not None]
    return sorted(vals, reverse=True)[:2]


def show(kind, v):
    if kind == "inr":
        return f"₹{v:.2f}"
    if kind == "usd_bn":
        return f"US${v / 1e9:,.0f} billion"
    return f"{v:.1f}%"


def update_economy():
    items = []
    this_year = datetime.now(timezone.utc).year
    for code, name, name_hi, kind, note, note_hi in ECONOMY:
        try:
            vals = wb_latest_two(code)
        except Exception as e:
            print(f"  economy: {code} failed ({type(e).__name__})")
            continue
        if not vals or vals[0][0] < this_year - ECON_MAX_AGE_YEARS:
            print(f"  economy: {code} skipped (no recent value)")
            continue
        item = {"id": code, "name": name, "name_hi": name_hi, "display": show(kind, vals[0][1]),
                "year": vals[0][0], "explain": note, "explain_hi": note_hi,
                "url": f"https://data.worldbank.org/indicator/{code}?locations=IN"}
        if len(vals) > 1:
            item["prev_display"], item["prev_year"] = show(kind, vals[1][1]), vals[1][0]
        items.append(item)
    if not items:
        print("economy: nothing to write")
        return
    try:
        with open(ECON_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        old = {}
    if old.get("items") == items:
        print(f"economy: {len(items)} figures, unchanged")
        return
    out = {"source": "World Bank", "licence": "CC BY 4.0", "attribution": ECON_ATTRIBUTION,
           "updated": datetime.now(timezone.utc).isoformat(), "items": items}
    with open(ECON_FILE, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(f"economy: {len(items)} figures written")


# ---------- main ----------
def main():
    try:
        update_economy()
    except Exception as e:
        print(f"economy: skipped ({type(e).__name__})")
    if not API_KEY:
        print("GEMINI_API_KEY is missing. Add it as a repository secret.")
        return 1
    items = []
    for feed in FEEDS:
        try:
            got = parse_items(fetch_feed(feed["url"]), feed)
            print(f"{feed['name']} ({feed['url'].split('/')[-1]}): {len(got)} items")
            items += got
        except Exception as e:
            print(f"Could not read {feed['name']} ({feed['url']}): {type(e).__name__}")
    if not items:
        return 1

    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        old = []
    # keep only stories made by this version (older ones are redone, with titles and Hindi)
    old = [s for s in old if s.get("v") in (2, 3) and "example.com" not in s.get("url", "")]
    # drop stories from any source that is not in FEEDS (no permission for those)
    old = [s for s in old if any(s.get("source", "").startswith(f["name"]) for f in FEEDS)]
    for s in old:  # give Hindi a fresh start once, now that the checks are fairer
        if s.get("hi_ver") != 2:
            s["hi_tries"] = 0
            s["hi_ver"] = 2
    # Oquilia terms: pick up their corrections and drop stories whose link no longer works
    live = {i["url"]: i for i in items if i.get("verbatim")}
    kept = []
    for s in old:
        if "src_summary" in s:  # story came from a syndication feed
            if not link_alive(s["url"]):
                print(f"- removed (link no longer works): {s['title'][:60]}")
                continue
            cur = live.get(s["url"])
            if cur and (cur["title"] != s.get("src_title") or cur["desc"][:600] != s.get("src_summary")):
                print(f"- corrected by publisher, redoing: {cur['title'][:60]}")
                continue  # dropped here and made again below from the corrected text
        kept.append(s)
    old = kept
    seen = {s["url"] for s in old if s.get("v") == 3}  # older versions get redone

    for f in FEEDS:  # one line per source: how many items were used, and why others were not
        mine = [i for i in items if i["feed_url"] == f["url"]]
        tally = {}
        for i in mine:
            r = reject_reason(i) or "USED"
            tally[r] = tally.get(r, 0) + 1
        print(f"  {f['name']}: {tally}")
        if mine and "USED" not in tally:
            print(f"    first item: {mine[0]['title'][:70]!r} / description {words(mine[0]['desc'])} words")
    good = sorted((i for i in items if wanted(i)), key=lambda i: i["published"], reverse=True)
    todo, count = [], {}
    for i in good:  # newest first; a few from each source so one feed cannot take every slot
        if i["url"] not in seen and count.get(i["feed_url"], 0) < PER_FEED:
            count[i["feed_url"]] = count.get(i["feed_url"], 0) + 1
            todo.append(i)
    todo = sorted(todo, key=lambda i: i["published"], reverse=True)[:MAX_NEW]
    print(f"Feed items: {len(items)}, business stories: {len(good)}, new to summarise: {len(todo)}")

    added = []
    for n, i in enumerate(todo):
        print(f"- {i['title'][:70]}")
        if i.get("verbatim"):  # Oquilia and PIB: shown with their rules
            r = oquilia_result(i)
            if r is None:
                print("  skipped (did not pass checks)")
            else:
                added.append(make_story(i, r))
            if n < len(todo) - 1:
                time.sleep(PAUSE)
            continue
        r = check(call_gemini(i["title"], i["desc"]), i["title"], i["desc"])
        if r is None:
            r = check(call_gemini(i["title"], i["desc"],
                                  "Reminder: write 50-60 words using the key facts in the source."),
                      i["title"], i["desc"], need_hi=False)
        if r is None:
            print("  skipped (summary did not pass checks)")
        else:
            added.append(make_story(i, r))
        if n < len(todo) - 1:
            time.sleep(PAUSE)

    new_urls = {x["url"] for x in added}
    old = [x for x in old if x["url"] not in new_urls]  # replaced by the redone version

    # Hindi for any story that does not have it yet (new ones that failed, or older ones)
    fix = [x for x in added + old if "title_hi" not in x and x.get("hi_tries", 0) < 2][:12]
    done = 0
    for x in fix:
        print(f"- Hindi for: {x['title'][:60]}")
        done += add_hindi(x)
        time.sleep(PAUSE)
    print(f"Hindi added for {done} of {len(fix)} stories.")

    stories = sorted(added + old, key=lambda s: s.get("published", ""), reverse=True)[:MAX_TOTAL]
    if not stories:
        print("Nothing to write.")
        return 1
    text = json.dumps(stories, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            unchanged = f.read() == text
    except Exception:
        unchanged = False
    if not unchanged:
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"Added {len(added)} stories, {len(stories)} in file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
