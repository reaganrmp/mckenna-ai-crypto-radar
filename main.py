"""
AI + Crypto Content Radar
-------------------------
This is your MEDIA tool (different job from the trading radar).
Every time it runs it:
  1. Pulls the newest AI + crypto news, plus regulation/law news from
     Indonesia, the US, and China (in Indonesian, English, and Chinese)
  2. Keeps only what's new in the last few minutes
  3. Asks Claude: "Is this worth posting about? For which market? What's the angle?"
  4. Sends the post-worthy ones to your Telegram so you can review + write

You do NOT need to edit any code to run it. The only things you might tweak
later are the CONFIG lists (feeds + search queries) near the top.
"""

import os
import sys
import json
import time
import html
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ------------------------- CONFIG (safe to tweak later) -------------------------
LOOKBACK_MINUTES = 20         # treat news from the last N minutes as "new"
MAX_ITEMS_PER_RUN = 20        # safety cap so a news burst can't spike your bill
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# FAST feeds: fresh crypto + AI news from outlets directly (no signup needed).
RSS_FEEDS = [
    # --- Crypto ---
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    # --- AI ---
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://the-decoder.com/feed/",
    "https://www.theverge.com/rss/index.xml",
]

# REGULATION / MULTI-MARKET net via Google News search feeds.
# Each entry = (search query, language code, country code). Add/remove freely.
# "when:6h" makes Google return only the last ~6 hours, so it stays fresh.
GOOGLE_NEWS = [
    # --- International / US (English) ---
    ("cryptocurrency regulation SEC when:6h", "en-US", "US"),
    ("artificial intelligence regulation law when:6h", "en-US", "US"),
    # --- Indonesia (Indonesian) ---
    ("regulasi kripto OR pajak kripto Indonesia when:6h", "id", "ID"),
    ("aturan kecerdasan buatan Indonesia when:6h", "id", "ID"),
    ("Indodax OR Tokocrypto OR Pintu when:6h", "id", "ID"),
    ("Bappebti OR OJK kripto when:6h", "id", "ID"),
    # --- China (Chinese) --- PAUSED for now, uncomment when ready ---
    # ("加密货币 政策 监管 when:6h", "zh-CN", "CN"),
    # ("人工智能 监管 法规 when:6h", "zh-CN", "CN"),
]

TEST_MODE = os.environ.get("TEST_MODE") == "1"
# --------------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CRYPTOPANIC_TOKEN = os.environ.get("CRYPTOPANIC_TOKEN")  # optional

FLAGS = {"ID": "🇮🇩", "US": "🇺🇸", "CN": "🇨🇳", "Global": "🌐"}


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def parse_iso(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def within_window(dt):
    if TEST_MODE:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)


def fetch_cryptopanic():
    items = []
    if not CRYPTOPANIC_TOKEN:
        return items
    endpoints = [
        f"https://cryptopanic.com/api/developer/v2/posts/?auth_token={CRYPTOPANIC_TOKEN}&public=true",
        f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_TOKEN}&public=true",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                continue
            for post in r.json().get("results", []):
                title = (post.get("title") or "").strip()
                link = post.get("url") or post.get("original_url") or ""
                dt = parse_iso(post.get("published_at") or post.get("created_at"))
                if title and dt:
                    items.append({"title": title, "url": link,
                                  "source": "CryptoPanic", "dt": dt})
            if items:
                break
        except Exception as e:
            log(f"CryptoPanic error: {e}")
    return items


def _feed_entries(feed_url, source_fallback):
    import feedparser
    items = []
    try:
        parsed = feedparser.parse(feed_url)
        feed_title = parsed.feed.get("title", source_fallback)
        for e in parsed.entries[:40]:
            title = (e.get("title") or "").strip()
            link = e.get("link", "")
            # Google News tags the real publisher inside entry.source
            src = feed_title
            if isinstance(e.get("source"), dict) and e["source"].get("title"):
                src = e["source"]["title"]
            dt = None
            if e.get("published_parsed"):
                dt = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            elif e.get("updated_parsed"):
                dt = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
            if title and dt:
                items.append({"title": title, "url": link, "source": src, "dt": dt})
    except Exception as ex:
        log(f"Feed error for {feed_url}: {ex}")
    return items


def fetch_rss():
    items = []
    for url in RSS_FEEDS:
        items += _feed_entries(url, url)
    return items


def fetch_google_news():
    items = []
    for query, hl, gl in GOOGLE_NEWS:
        url = (f"https://news.google.com/rss/search?q={quote(query)}"
               f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")
        items += _feed_entries(url, "Google News")
    return items


def gather_candidates():
    all_items = fetch_cryptopanic() + fetch_rss() + fetch_google_news()
    seen, fresh = set(), []
    for it in all_items:
        if not within_window(it["dt"]):
            continue
        key = it["url"] or it["title"]
        if key in seen:
            continue
        seen.add(key)
        fresh.append(it)
    fresh.sort(key=lambda x: x["dt"], reverse=True)
    return fresh[:MAX_ITEMS_PER_RUN]


def analyze(items):
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    headlines = "\n".join(
        f'{i + 1}. "{it["title"]}" (source: {it["source"]})'
        for i, it in enumerate(items)
    )

    prompt = f"""You are a news radar for a crypto + AI media brand posting to a young
Indonesian audience (market code "ID") and an international English audience
(market code "US"/"Global"). China is paused for now — never tag "CN".

For EACH headline, apply this STAKES TEST, not just topic relevance:
Would a young crypto/AI-interested person feel real FOMO, worry, or curiosity —
not just "huh, technically related"? Ask: does this change what someone can/can't
do tomorrow, affect their money, their platform access, or expose a scam?

Include ONLY headlines that pass the stakes test AND are one of:
- fast-rising / breaking / high-interest AI or crypto news
- a government/regulatory/legal action affecting AI or crypto (especially
  Indonesia or the US) — new tax, exchange bans, AI laws, export blocks
- a scam, rug-pull, hack, or exploit exposure (protects and builds trust with audience)

EXCLUDE: price predictions, generic opinion pieces, low-effort clickbait,
dry technical/legal stories with no real stakes for a normal person, and
duplicates (if several headlines cover the SAME event, keep only the best one).

Return ONLY a JSON array (no other text, no markdown). Each object:
{{
  "n": <headline number>,
  "topic": "crypto" | "ai" | "both",
  "category": "breaking" | "regulation" | "scam_alert" | "general",
  "markets": [<one or more of "ID","US","Global">],
  "virality_potential": "low" | "medium" | "high",
  "summary": "<1-2 sentence plain-English summary>",
  "why": "<one sentence: why THIS audience feels FOMO/worry/curiosity>",
  "angle": "<a short suggested content hook/angle for a post>"
}}

If none qualify, return []

Headlines:
{headlines}
"""

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except Exception:
        log("Could not read Claude's JSON reply: " + text[:300])
        return []


VIRALITY_DOT = {"high": "🟢🟢🟢", "medium": "🟢🟢⚪", "low": "🟢⚪⚪"}


def format_message(item, a):
    cat = str(a.get("category", "")).lower()
    badge = ("⚖️" if cat == "regulation" else
             "🔥" if cat == "breaking" else
             "🚨" if cat == "scam_alert" else "📰")
    topic = str(a.get("topic", "")).lower()
    topic_emoji = "🪙🤖" if topic == "both" else "🤖" if topic == "ai" else "🪙"
    flags = " ".join(FLAGS.get(m, m) for m in a.get("markets", [])) or "🌐"
    virality = VIRALITY_DOT.get(str(a.get("virality_potential", "")).lower(), "")
    esc = html.escape
    return (
        f"{badge} {topic_emoji} <b>{esc(item['title'])}</b>\n\n"
        f"🌍 <b>Post to:</b> {flags}   {virality}\n"
        f"📝 {esc(a.get('summary', ''))}\n\n"
        f"💡 <b>Why they care:</b> {esc(a.get('why', ''))}\n"
        f"🎬 <b>Angle:</b> {esc(a.get('angle', ''))}\n\n"
        f"🔗 <a href=\"{item['url']}\">Source</a>  ·  <i>{esc(item['source'])}</i>"
    )


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def main():
    missing = [k for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
               if not os.environ.get(k)]
    if missing:
        log("Missing required secrets: " + ", ".join(missing))
        sys.exit(1)

    log("Fetching AI + crypto + regulation news...")
    items = gather_candidates()
    log(f"{len(items)} fresh candidate headline(s).")
    if not items:
        log("Nothing new this run. Exiting cleanly.")
        return

    log("Asking Claude what's post-worthy...")
    results = analyze(items)
    log(f"Claude flagged {len(results)} post-worthy item(s).")

    sent = 0
    for a in results:
        n = a.get("n")
        if not isinstance(n, int) or n < 1 or n > len(items):
            continue
        if send_telegram(format_message(items[n - 1], a)):
            sent += 1
            time.sleep(1)
    log(f"Sent {sent} alert(s). Done.")


if __name__ == "__main__":
    main()
