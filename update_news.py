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

FEED_URL = "https://www.business-standard.com/rss/latest.rss"
OUT_FILE = "money_shorts.txt"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
BS = "{https://www.business-standard.com/rss/}"

KEEP_SECTIONS = {"Markets", "Companies", "Economy", "Industry", "Finance"}
MAX_NEW = 8        # new stories to add per run
MAX_TOTAL = 24     # stories kept in the file
MIN_WORDS = 30     # shorter than 60 is allowed when the source is thin
MAX_WORDS = 80
PAUSE = 8          # seconds between AI calls (free plan is rate limited)

COLORS = {"Positive": "0F766E", "Neutral": "B7791F", "Negative": "9B2C2C"}
TRENDS = {  # decorative picture only, NOT real price data
    "Positive": [0.1, 0.25, 0.2, 0.5, 0.45, 0.8, 0.95],
    "Neutral": [0.5, 0.55, 0.45, 0.5, 0.47, 0.52, 0.5],
    "Negative": [0.95, 0.85, 0.7, 0.6, 0.4, 0.3, 0.1],
}

SYSTEM = """You write short stock-market news cards for retail investors in India.
Rules:
- Use ONLY the headline and description given. Never add facts, numbers, names, causes or predictions that are not in them.
- Write in your own words, in plain neutral English. Do not copy sentences from the source.
- Do not give investment advice. Never say buy, sell, hold, target price or recommend.
- Length: 60 to 80 words if the source has enough information. If it is thin, write fewer words rather than guessing or padding, but never fewer than 30.
- impact: Positive, Neutral or Negative for the fundamentals of the company or sector in the story. Use Neutral when unclear or when the story is not about a company or sector.
- why: at most 8 words explaining the impact.
- company: the main company named in the headline, or an empty string if none.
- chips: up to 3 key figures copied exactly from the text, each with a short label (for example label "Net profit", value "Rs 545 crore"). Use an empty list if there are no clear figures.
Reply with JSON only: {"summary": "...", "impact": "...", "why": "...", "company": "...", "chips": [{"label": "...", "value": "..."}]}"""


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


def fetch_feed():
    req = urllib.request.Request(
        FEED_URL, headers={"User-Agent": "Mozilla/5.0 (Moneyshorts learning project)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def text_of(item, tag):
    el = item.find(tag)
    return clean(el.text) if el is not None and el.text else ""


def parse_items(xml_bytes):
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
            "section": text_of(it, BS + "section"),
            "subsection": text_of(it, BS + "subsection"),
            "source": text_of(it, BS + "source"),
            "premium": text_of(it, BS + "premium") == "true",
        })
    return out


def wanted(i):
    return (i["section"] in KEEP_SECTIONS and i["source"] != "Sponsored Content"
            and i["title"] and i["url"].startswith("https://") and i["desc"])


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
def call_gemini(title, desc, note=""):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [
            {"text": f"Headline: {title}\nDescription: {desc}\n{note}"}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
            return json.loads(resp["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 3:
                print(f"  AI busy (code {e.code}), waiting...")
                time.sleep(30 * (attempt + 1))
                continue
            print(f"  AI error code {e.code}")
            return None
        except (KeyError, IndexError, ValueError):
            return None
    return None


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


def check(res, title, desc):
    """Return a cleaned result, or None if it fails our safety checks."""
    if not isinstance(res, dict) or not isinstance(res.get("summary"), str):
        return None
    summary = trim_to_limit(clean(res["summary"]))
    if words(summary) < MIN_WORDS:
        return None
    if re.search(r"\b(should (buy|sell)|target price|we recommend|recommended to (buy|sell))\b",
                 summary, re.I):
        return None
    source = (title + " " + desc).replace(",", "")
    chips = {}
    for c in (res.get("chips") or [])[:3]:
        if not isinstance(c, dict):
            continue
        label, value = clean(str(c.get("label", "")))[:20], clean(str(c.get("value", "")))[:20]
        tokens = nums(value)
        # keep a chip only if every number in it really appears in the source
        if label and value and tokens and all(t.replace(",", "") in source for t in tokens):
            chips[label] = value
    impact = res.get("impact") if res.get("impact") in COLORS else "Neutral"
    company = clean(str(res.get("company", "")))
    if company.lower() not in title.lower():
        company = ""
    return {"summary": summary, "impact": impact,
            "why": clean(str(res.get("why", "")))[:60], "company": company, "chips": chips}


def make_story(i, r):
    name = re.sub(r"\b(LIMITED|LTD|PVT)\b\.?", "", r["company"].upper()).strip()
    tk = ""
    for w in (name or i["section"].upper()).split():
        if len((tk + " " + w).strip()) > 16:
            break
        tk = (tk + " " + w).strip()
    tk = tk or (name or i["section"].upper())[:16]
    src = "Business Standard"
    if i["source"] and i["source"] != "Business Standard":
        src += f" / {i['source']}"
    return {
        "cat": category(i), "tk": tk, "title": i["title"], "summary": r["summary"],
        "source": src, "published": i["published"], "ago": ago(i["published"]),
        "impact": r["impact"], "why": r["why"], "url": i["url"], "chips": r["chips"],
        "trend": TRENDS[r["impact"]], "color": COLORS[r["impact"]], "premium": i["premium"],
    }


# ---------- main ----------
def main():
    if not API_KEY:
        print("GEMINI_API_KEY is missing. Add it as a repository secret.")
        return 1
    try:
        items = parse_items(fetch_feed())
    except Exception as e:
        print(f"Could not read the feed: {type(e).__name__}")
        return 1

    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        old = []
    old = [s for s in old if "example.com" not in s.get("url", "")]  # drop sample stories
    seen = {s["url"] for s in old}

    good = sorted((i for i in items if wanted(i)), key=lambda i: i["published"], reverse=True)
    todo = [i for i in good if i["url"] not in seen][:MAX_NEW]
    print(f"Feed items: {len(items)}, business stories: {len(good)}, new to summarise: {len(todo)}")

    added = []
    for n, i in enumerate(todo):
        print(f"- {i['title'][:70]}")
        r = check(call_gemini(i["title"], i["desc"]), i["title"], i["desc"])
        if r is None:
            r = check(call_gemini(i["title"], i["desc"],
                                  "Reminder: write 60-80 words using every fact in the source."),
                      i["title"], i["desc"])
        if r is None:
            print("  skipped (summary did not pass checks)")
        else:
            added.append(make_story(i, r))
        if n < len(todo) - 1:
            time.sleep(PAUSE)

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
