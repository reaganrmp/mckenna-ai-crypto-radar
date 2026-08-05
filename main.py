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
JAYDEN_BOT_TOKEN = os.environ.get("JAYDEN_BOT_TOKEN")     # optional - enables content packs
JAYDEN_MODEL = "claude-sonnet-5"   # better copywriting than Haiku, worth it for the few high alerts

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


def send_telegram(text, token=None):
    token = token or TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"
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


def generate_pack(item, a):
    """Jayden: turns a high-priority alert into a ready-to-post content pack."""
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    lang = "Indonesian" if "ID" in a.get("markets", []) else "English"
    news_text = f"{item['title']}\n\nSummary: {a.get('summary', '')}"

    prompt = f"""You are Jayden, a social content planner for a crypto + AI media brand
(TikTok, Instagram, Threads, X). Turn this news into a ready-to-post pack in the style
of top Indonesian crypto media (dark dramatic real photo + bold headline + one highlighted
phrase, minimal text, no over-explaining).
Write in {lang}.

Rules:
- ONE slide 1 headline, punchy, confident, not corporate. Mark the single most
  important phrase within it as the "highlight" (the part that goes in accent color).
- Only include a second slide if there's a genuine "receipt" to show - a stat, a quote,
  a price number, something that proves the claim. NOT an explanation. If there's nothing
  worth proving, leave "slides" as an empty list - most posts should have ZERO extra slides.
- No walls of text anywhere. If it can't be said in one short line, cut it.

VISUAL SAFETY RULE (important):
- If the news centers on a real, named, identifiable person (a CEO, official,
  politician, etc.), set "visual_type" to "real_photo" and describe what kind of
  describe a REAL, dramatic news photo to search for (dark/desaturated background,
  subject well-lit, similar to Indonesian crypto media style) in "visual_direction". Do NOT write
  an ai_image_prompt in this case - leave it empty. Realistic AI images of real
  people are risky (deepfakes/misinformation) and not worth the risk for this brand.
- If the news is conceptual/abstract (market moves, charts, general crypto/AI themes,
  no specific real face needed), set "visual_type" to "ai_generated" and give a
  detailed "ai_image_prompt" for an AI art tool - abstract/conceptual imagery only,
  NO real people, NO text baked into the image (text gets added later in Canva).
  Write this prompt like a professional AI-art prompter, not a one-line description.
  CRITICAL: the prompt must be visually TIED to the specific facts of THIS story
  (the scale, the numbers, the specific industry/location/situation) - never a
  generic stock scene that could illustrate any random headline. If the story has
  a number (e.g. "360MW", "$50M hack", "3 data centers"), find a visual way to
  hint at that scale or fact, not just the general topic.
  It MUST include, in order:
  1. Subject + action grounded in the actual story's specifics (concrete, specific
     object/scene tied to this exact news - e.g. for a "$50M exchange hack" story:
     "a shattered digital vault door with glowing binary code leaking out like
     liquid light", not just "a generic hacker scene")
  2. Composition (e.g. "centered subject, shot from a low angle, dramatic negative
     space in the lower third of the frame for text overlay")
  3. Lighting + mood (e.g. "single hard rim light from top-right, deep shadows,
     cinematic, moody, high contrast")
  4. Color grading matching the brand: dark near-black background with teal
     (#367588) accent lighting/glow somewhere in the scene
  5. Style + quality tags (e.g. "photorealistic, 8k, shot on 50mm lens, shallow
     depth of field, ultra-detailed, editorial photography style, no text, no logos,
     no watermark")
  Aim for 3-5 full sentences of specific visual detail, not a short phrase.

Return ONLY JSON (no markdown, no extra text):
{{
 "hooks": ["3 punchy slide-1 headline options"],
 "why_hook": "one short line: why these headlines stop the scroll",
 "slides": ["at most 1 short 'proof/receipt' line, or leave this list empty"],
 "highlight": "the exact phrase from the chosen headline to put in accent color",
 "thumbnail_text": "the bold words to put ON the cover image",
 "caption": "short post caption",
 "visual_type": "real_photo" | "ai_generated",
 "visual_direction": "what photo/image to use and why",
 "ai_image_prompt": "prompt for AI art tool, or empty string if visual_type is real_photo",
 "formats": {{"tiktok": "one-line tip", "instagram": "...", "threads": "...", "x": "..."}},
 "hashtags": {{"tiktok": ["5-7 tags"], "instagram": ["..."], "threads": ["..."], "x": ["..."]}}
}}

News:
{news_text}
"""
    resp = client.messages.create(
        model=JAYDEN_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        text = text[s:e + 1]
    return json.loads(text)


def format_pack(p):
    esc = html.escape
    hooks = "\n".join(f"  {i + 1}. {esc(h)}" for i, h in enumerate(p.get("hooks", [])))
    slides = "\n".join(f"  • {esc(s)}" for s in p.get("slides", []))
    order = ["tiktok", "instagram", "threads", "x"]
    fmts = p.get("formats", {})
    fmt_lines = "\n".join(f"  <b>{k.title()}:</b> {esc(fmts[k])}" for k in order if fmts.get(k))
    tags = p.get("hashtags", {})
    tag_lines = "\n".join(
        f"  <b>{k.title()}:</b> {esc(' '.join(tags[k]))}" for k in order if tags.get(k))

    vtype = p.get("visual_type", "")
    if vtype == "real_photo":
        visual_block = f"📷 <b>Use a REAL photo:</b> {esc(p.get('visual_direction', ''))}"
    else:
        visual_block = (
            f"🎨 <b>AI image prompt:</b> {esc(p.get('visual_direction', ''))}\n"
            f"<code>{esc(p.get('ai_image_prompt', ''))}</code>"
        )

    return (
        f"✍️ <b>JAYDEN CONTENT PACK</b>\n\n"
        f"🎣 <b>Slide 1 — Hook options:</b>\n{hooks}\n"
        f"<i>Why it works: {esc(p.get('why_hook', ''))}</i>\n\n"
        f"📄 <b>Next slides:</b>\n{slides}\n\n"
        f"✨ <b>Highlight line:</b> {esc(p.get('highlight', ''))}\n"
        f"🖼️ <b>Thumbnail text:</b> {esc(p.get('thumbnail_text', ''))}\n\n"
        f"{visual_block}\n\n"
        f"📝 <b>Caption:</b>\n{esc(p.get('caption', ''))}\n\n"
        f"📐 <b>Format per platform:</b>\n{fmt_lines}\n\n"
        f"#️⃣ <b>Hashtags:</b>\n{tag_lines}"
    )


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
        item = items[n - 1]
        if send_telegram(format_message(item, a)):
            sent += 1
            time.sleep(1)

        # Auto-trigger Jayden for high-priority alerts only (keeps cost low)
        if JAYDEN_BOT_TOKEN and str(a.get("virality_potential", "")).lower() == "high":
            try:
                log("High-priority alert -> generating Jayden content pack...")
                pack = generate_pack(item, a)
                send_telegram(format_pack(pack), token=JAYDEN_BOT_TOKEN)
                time.sleep(1)
            except Exception as ex:
                log(f"Jayden pack generation failed: {ex}")
    log(f"Sent {sent} alert(s). Done.")


if __name__ == "__main__":
    main()
