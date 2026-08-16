"""
McKenna + Jayden - AI/Crypto Content Radar
--------------------------------------------
McKenna : finds fresh AI/crypto/regulation news (ID + international) and alerts you.
Jayden  : for the highest-priority stories, writes a ready-to-post content pack
          (hooks, slides, caption, formats, hashtags) PLUS a full, ready-to-paste
          image-generation prompt (styled to your teal brand) - you generate and
          design the image yourself in whatever tool you like.

ON-DEMAND COMMAND:
  Message McKenna's bot with:      regen: <paste the headline or story text>
  On the next run (or trigger the workflow manually for instant), you'll get a
  fresh Jayden pack for exactly that story - even if it's old news, got missed,
  or you just want another version. No code, no waiting on auto-detection.

You do NOT need to edit any code to run this. Tweak the CONFIG block if you want
to change check frequency, sources, or the pack cap.
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
MCKENNA_ENABLED = False        # set True to re-enable automatic news scanning/alerts.
                                # When False: McKenna's scan is fully skipped (saves
                                # Haiku + Sonnet costs) but "regen:" still works normally -
                                # message McKenna's bot with "regen: <headline>" anytime.
LOOKBACK_MINUTES = 20          # treat news from the last N minutes as "new"
MAX_ITEMS_PER_RUN = 20         # safety cap so a news burst can't spike your bill
MAX_PACKS_PER_RUN = 2          # cap on Jayden packs (Sonnet calls) per run
DAILY_PACK_LIMIT = 3           # hard cap on auto-generated packs per day (manual regen: not capped)
STATE_FILE = "state/daily_count.json"  # tiny file committed back to the repo to remember count across runs
CLAUDE_MODEL = "claude-haiku-4-5-20251001"     # McKenna's cheap news filter
JAYDEN_MODEL = "claude-haiku-4-5-20251001"     # switched from Sonnet to cut cost - see note below

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://the-decoder.com/feed/",
    "https://www.theverge.com/rss/index.xml",
]

GOOGLE_NEWS = [
    ("cryptocurrency regulation SEC when:6h", "en-US", "US"),
    ("artificial intelligence regulation law when:6h", "en-US", "US"),
    ("regulasi kripto OR pajak kripto Indonesia when:6h", "id", "ID"),
    ("aturan kecerdasan buatan Indonesia when:6h", "id", "ID"),
    ("Indodax OR Tokocrypto OR Pintu when:6h", "id", "ID"),
    ("Bappebti OR OJK kripto when:6h", "id", "ID"),
    # China paused - uncomment when ready
    # ("加密货币 政策 监管 when:6h", "zh-CN", "CN"),
    # ("人工智能 监管 法规 when:6h", "zh-CN", "CN"),
]

TEST_MODE = os.environ.get("TEST_MODE") == "1"

# The look every image prompt is built around - your brand, applied consistently
# so every prompt (wherever you paste it) points toward the same visual identity.
# --------------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CRYPTOPANIC_TOKEN = os.environ.get("CRYPTOPANIC_TOKEN")    # optional
JAYDEN_BOT_TOKEN = os.environ.get("JAYDEN_BOT_TOKEN")      # optional - enables content packs

FLAGS = {"ID": "🇮🇩", "US": "🇺🇸", "CN": "🇨🇳", "Global": "🌐"}
VIRALITY_DOT = {"high": "🟢🟢🟢", "medium": "🟢🟢⚪", "low": "🟢⚪⚪"}


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ============================== DAILY PACK COUNTER ================================
# Simple state file, committed back to the repo by the workflow, so the daily cap
# survives across separate GitHub Actions runs (each run otherwise starts fresh
# with no memory of earlier runs today).

def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_daily_count():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if data.get("date") != _today_str():
            return 0  # new day, reset
        return int(data.get("count", 0))
    except Exception:
        return 0


def load_seen_keys():
    """Story keys (url or title) already alerted/packed today - prevents the
    same story getting sent twice if it reappears in the feed on a later run."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if data.get("date") != _today_str():
            return set()
        return set(data.get("seen", []))
    except Exception:
        return set()


def save_daily_state(count, seen_keys):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"date": _today_str(), "count": count,
                      "seen": list(seen_keys)[-200:]}, f)  # cap list size, oldest drop off
    except Exception as e:
        log(f"Could not save daily state: {e}")



# ============================== NEWS GATHERING ==================================

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
                    items.append({"title": title, "url": link, "source": "CryptoPanic", "dt": dt})
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


# ============================== MCKENNA (ANALYSIS) ==============================

def analyze(items):
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    headlines = "\n".join(
        f'{i + 1}. "{it["title"]}" (source: {it["source"]})' for i, it in enumerate(items)
    )

    prompt = f"""You are a news radar for a crypto + AI media brand posting to a young
Indonesian audience (market code "ID") and an international English audience
(market code "US"/"Global"). China is paused for now - never tag "CN".

For EACH headline, apply this STAKES TEST, not just topic relevance:
Would a young crypto/AI-interested person feel real FOMO, worry, or curiosity -
not just "huh, technically related"? Ask: does this change what someone can/can't
do tomorrow, affect their money, their platform access, or expose a scam?

Include ONLY headlines that pass the stakes test AND are one of:
- fast-rising / breaking / high-interest AI or crypto news
- a government/regulatory/legal action affecting AI or crypto (especially
  Indonesia or the US) - new tax, exchange bans, AI laws, export blocks
- a scam, rug-pull, hack, or exploit exposure (protects and builds trust with audience)

EXCLUDE: price predictions, generic opinion pieces, low-effort clickbait,
dry technical/legal stories with no real stakes for a normal person, and
duplicates (if several headlines cover the SAME event, keep only the best one).

VIRALITY BAR - be strict, "high" should be RARE, not routine:
- "high": a genuinely major event most of the audience would already be hearing
  about elsewhere today, or a huge number/name that changes something concrete
  right now (a major hack, a landmark regulation, a huge market move). This
  should be maybe 1-3 stories on a typical day, not most of them.
- "medium": solidly relevant and worth knowing, but not urgent or huge.
- "low": minor, niche, or interesting-but-not-important.
When in doubt between high and medium, choose medium. Reserve "high" for
stories that would genuinely make someone stop scrolling immediately.

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
    resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=2000,
                                   messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except Exception:
        log("Could not read Claude's JSON reply: " + text[:300])
        return []


def format_message(item, a):
    cat = str(a.get("category", "")).lower()
    badge = ("⚖️" if cat == "regulation" else "🔥" if cat == "breaking" else
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
        f"🔗 <a href=\"{item['url']}\">Source</a>  ·  <i>{esc(item['source'])}</i>\n\n"
        f"<i>Want a redo? Message me: regen: {esc(item['title'][:80])}</i>"
    )


# ============================== JAYDEN (CONTENT PACK + PROMPT) ===================

def generate_pack(item, extra_context=""):
    """Turns a headline (+ optional summary/context) into a ready-to-post pack,
    including your ChatGPT master-prompt template filled in per the story."""
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    markets = item.get("markets", [])
    lang = "Indonesian" if "ID" in markets else "English"
    news_text = item["title"]
    if extra_context:
        news_text += f"\n\nContext: {extra_context}"

    prompt = f"""You are Jayden, a social content planner for a crypto + AI media brand
(TikTok, Instagram, Threads, X). Turn this news into a ready-to-post pack in the style
of top Indonesian crypto media (dark dramatic image + bold headline + one highlighted
phrase, minimal text, no over-explaining). Write in {lang}.

Rules:
- ONE slide 1 hook, punchy, confident, not corporate. Mark the single most
  important phrase within it as the "highlight" (the part that goes in accent color).
- Only include a second slide if there's a genuine "receipt" to show - a stat, a quote,
  a price number, something that proves the claim. NOT an explanation. If there's nothing
  worth proving, leave "slides" as an empty list - most posts should have ZERO extra slides.
- No walls of text anywhere. If it can't be said in one short line, cut it.

CAPTION RULE (important - this is what the audience actually reads):
The caption must make ANY reader fully understand the story, even if they know
nothing about crypto or AI. Cover, in this order:
  1. WHAT happened (the concrete event, with the key number/name)
  2. WHY it happened / the context behind it
  3. WHAT it means for the reader - who gains, who loses, what changes
Write in simple everyday language, no jargon. If you must use a technical term,
explain it in the same breath. End with a short question or line that invites replies.
The hook is the dramatic version of the headline; the caption is the clear, honest
explanation behind it. Never leave the reader confused about what actually happened.
FORMATTING (critical - this is read on a phone, make it easy to skim, not a wall
of text): write it as SHORT PARAGRAPHS, 1-2 sentences each, separated by a blank
line between each paragraph. Roughly one paragraph per point above (what/why/what
it means), plus a final short paragraph with the reply-inviting question. Never
write it as a single dense block.

MASTER PROMPT TEMPLATE FIELDS (fills the user's saved ChatGPT image-gen template,
which has exactly 3 fields: NEWS, SOURCE, OPTIONAL):
- "master_news": a short 2-4 sentence plain summary of the actual news/article -
  this is what the image AI reads for context, keep it tight
- "master_optional": ONE short line of emphasis/direction for the image, following
  these rules by story type:
  - If the news centers on a WIDELY RECOGNIZED public figure (well-known CEO,
    official, politician, celebrity), set "visual_type" to "real_photo" and make
    this line tell the user to source a real photo themselves (never fabricate a
    recognizable real person's face - deepfake risk).
  - If the news involves a real but NOT widely-recognized person, set "visual_type"
    to "ai_illustrative" and make this line say: "Generic anonymous figures only,
    no real likeness - label this post AI Generated for transparency."
  - Otherwise (no person in the story) set "visual_type" to "ai_generated" and make
    this line ground the image in the story's specific facts (the number/scale/
    place) and emotional tone (growth->abundance/motion, bad news->tension/cracks,
    regulation->structure/authority) - concrete, never generic stock imagery.

Return ONLY JSON (no markdown, no extra text):
{{
 "hooks": ["2 punchy hook options"],
 "highlight": "the exact phrase from the chosen hook to put in accent color",
 "caption": "short-paragraph plain-language explanation per the CAPTION RULE above (use \\n\\n between paragraphs)",
 "visual_type": "real_photo" | "ai_illustrative" | "ai_generated",
 "master_news": "for the NEWS template field",
 "master_optional": "for the OPTIONAL template field, per rules above",
 "top_hashtags": ["exactly 5 hashtags, optimized specifically for Instagram reach -
   mix broad discovery tags (e.g. #crypto #bitcoin) with 1-2 more specific/niche
   ones tied to this story - avoid generic overused tags with no real targeting
   value, avoid anything that reads as spammy or shadowban-prone"],
 "proof_slide": "ONLY if there's a genuine receipt worth a 2nd slide - a stat,
   a quote, or a note like 'screenshot the chart/tweet from the source link'.
   Leave as empty string for most stories - a single slide is usually enough."
}}

News:
{news_text}
"""
    resp = client.messages.create(model=JAYDEN_MODEL, max_tokens=2000,
                                   messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        text = text[s:e + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as je:
        # Log what Claude actually sent back so a repeat failure is diagnosable,
        # then retry once with a firmer instruction before giving up.
        log(f"Jayden JSON parse failed: {je}. Raw (first 300 chars): {text[:300]!r}")
        retry_prompt = prompt + "\n\nIMPORTANT: reply with ONLY the JSON object, nothing else - no caveats, no prose before or after."
        resp2 = client.messages.create(model=JAYDEN_MODEL, max_tokens=2000,
                                       messages=[{"role": "user", "content": retry_prompt}])
        text2 = "".join(b.text for b in resp2.content if b.type == "text").strip()
        s2, e2 = text2.find("{"), text2.rfind("}")
        if s2 != -1 and e2 != -1:
            text2 = text2[s2:e2 + 1]
        return json.loads(text2)  # let it raise clearly if the retry also fails



def format_pack(p, news_title="", item=None):
    esc = html.escape
    hooks = "\n".join(f"  {i + 1}. {esc(h)}" for i, h in enumerate(p.get("hooks", [])))
    tags = " ".join(p.get("top_hashtags", []))

    vtype = p.get("visual_type", "")
    source_name = (item or {}).get("source", "")
    source_url = (item or {}).get("url", "")
    source_line = f"{esc(source_name)}" + (f" - {esc(source_url)}" if source_url else "")

    if vtype == "real_photo":
        template_block = (
            f"📷 <b>Real photo needed:</b> {esc(p.get('master_optional', ''))}\n"
            f"(Skip the ChatGPT template for this one - source and edit a real photo instead.)"
        )
    else:
        template_block = (
            f"🖨️ <b>Master prompt (paste into your ChatGPT template):</b>\n"
            f"<code>NEWS:\n{esc(p.get('master_news', ''))}\n\n"
            f"SOURCE:\n{esc(source_line)}\n\n"
            f"OPTIONAL:\n{esc(p.get('master_optional', ''))}</code>"
        )

    proof = p.get("proof_slide", "").strip()
    proof_block = f"📄 <b>Slide 2 idea:</b> {esc(proof)}\n\n" if proof else ""

    caption_with_tags = f"{p.get('caption', '')}\n\n{tags}"

    return (
        f"✍️ <b>JAYDEN CONTENT PACK</b>\n"
        f"📰 <b>News:</b> {esc(news_title)}\n\n"
        f"🎣 <b>Hook options:</b>\n{hooks}\n\n"
        f"✨ <b>Highlight:</b> {esc(p.get('highlight', ''))}\n\n"
        f"{template_block}\n\n"
        f"{proof_block}"
        f"📝 <b>Caption + hashtags (tap to copy):</b>\n"
        f"<code>{esc(caption_with_tags)}</code>\n\n"
        f"🔗 <b>Source:</b> {source_line}"
    )


# ============================== TELEGRAM HELPERS =================================

TELEGRAM_MAX_LEN = 4000  # Telegram's real limit is 4096; leave margin for safety


def send_telegram(text, token=None):
    """Sends a message, automatically splitting into multiple messages if it's
    too long for Telegram's 4096-char limit - so a long pack never silently
    fails with a 'message is too long' error again."""
    token = token or TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = []
    remaining = text
    while len(remaining) > TELEGRAM_MAX_LEN:
        split_at = remaining.rfind("\n\n", 0, TELEGRAM_MAX_LEN)
        if split_at == -1:
            split_at = TELEGRAM_MAX_LEN
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    chunks.append(remaining)

    ok = True
    for chunk in chunks:
        try:
            r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                                         "parse_mode": "HTML", "disable_web_page_preview": False},
                              timeout=20)
            if r.status_code != 200:
                log(f"Telegram error {r.status_code}: {r.text[:200]}")
                ok = False
            time.sleep(0.4)
        except Exception as e:
            log(f"Telegram send failed: {e}")
            ok = False
    return ok


def get_updates(token):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates?timeout=0", timeout=25)
        return r.json().get("result", [])
    except Exception as e:
        log(f"getUpdates error: {e}")
        return []


def clear_updates(token, offset):
    try:
        requests.get(f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=0", timeout=25)
    except Exception as e:
        log(f"clear_updates error: {e}")


# ============================== PACK PIPELINE =====================================

def run_pack_pipeline(item, extra_context="", label="story"):
    """Shared by both the auto high-priority path and the manual 'regen:' command."""
    if not JAYDEN_BOT_TOKEN:
        log("No JAYDEN_BOT_TOKEN set - skipping pack generation.")
        return False
    try:
        log(f"Generating pack for {label}: {item['title'][:60]}...")
        pack = generate_pack(item, extra_context)
        send_telegram(format_pack(pack, item["title"], item=item), token=JAYDEN_BOT_TOKEN)
        return True
    except Exception as ex:
        log(f"Pack generation failed for {label}: {ex}")
        send_telegram(f"⚠️ Jayden hit a snag on: {html.escape(item['title'][:100])}\n({ex})",
                      token=JAYDEN_BOT_TOKEN)
        return False


def handle_regen_commands():
    """Checks McKenna's bot for 'regen: <text>' messages from you and generates
    a fresh Jayden pack for that exact text, on demand."""
    if not TELEGRAM_BOT_TOKEN:
        return
    updates = get_updates(TELEGRAM_BOT_TOKEN)
    if not updates:
        return

    max_id = max(u["update_id"] for u in updates)
    handled = 0

    for u in updates:
        msg = u.get("message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat_id != str(TELEGRAM_CHAT_ID):
            continue
        if not text.lower().startswith("regen:"):
            continue

        story_text = text.split(":", 1)[1].strip()
        if not story_text:
            continue

        log(f"Manual regen requested: {story_text[:60]}")
        send_telegram(f"🔄 Regenerating pack for: {html.escape(story_text[:100])}", token=TELEGRAM_BOT_TOKEN)
        fake_item = {"title": story_text, "url": "", "source": "manual request", "markets": ["ID", "Global"]}
        run_pack_pipeline(fake_item, label="manual regen")
        handled += 1
        time.sleep(1)

    clear_updates(TELEGRAM_BOT_TOKEN, max_id + 1)
    if handled:
        log(f"Handled {handled} manual regen request(s).")


# ============================== MAIN =============================================

def main():
    missing = [k for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
               if not os.environ.get(k)]
    if missing:
        log("Missing required secrets: " + ", ".join(missing))
        sys.exit(1)

    # 1) Handle any manual "regen:" requests first - always runs, even with
    #    McKenna paused, so you can still get packs on demand.
    handle_regen_commands()

    if not MCKENNA_ENABLED:
        log("McKenna is paused (MCKENNA_ENABLED = False) - skipping automatic news scan.")
        return

    # 2) Normal automatic news scan
    log("Fetching AI + crypto + regulation news...")
    items = gather_candidates()
    log(f"{len(items)} fresh candidate headline(s).")
    if not items:
        log("Nothing new this run. Exiting cleanly.")
        return

    log("Asking Claude what's post-worthy...")
    results = analyze(items)
    log(f"Claude flagged {len(results)} post-worthy item(s).")

    results = [a for a in results if str(a.get("virality_potential", "")).lower() == "high"]
    log(f"{len(results)} are HIGH priority - only these get sent.")

    daily_count = load_daily_count()
    seen_keys = load_seen_keys()
    log(f"Packs generated so far today: {daily_count}/{DAILY_PACK_LIMIT}")

    sent = 0
    packs_made = 0
    new_seen = set()
    for a in results:
        n = a.get("n")
        if not isinstance(n, int) or n < 1 or n > len(items):
            continue
        item = dict(items[n - 1])
        item["markets"] = a.get("markets", [])

        key = item.get("url") or item["title"]
        if key in seen_keys:
            log(f"Already alerted earlier today, skipping duplicate: {item['title'][:60]}")
            continue

        if send_telegram(format_message(item, a)):
            sent += 1
            new_seen.add(key)
            time.sleep(1)

        if packs_made >= MAX_PACKS_PER_RUN:
            continue
        if daily_count + packs_made >= DAILY_PACK_LIMIT:
            log("Daily pack limit reached - skipping auto-pack for remaining stories today.")
            continue

        if JAYDEN_BOT_TOKEN and str(a.get("virality_potential", "")).lower() == "high":
            if run_pack_pipeline(item, extra_context=a.get("summary", ""), label="auto high-priority"):
                packs_made += 1
                time.sleep(1)

    if new_seen or packs_made > 0:
        save_daily_state(daily_count + packs_made, seen_keys | new_seen)

    log(f"Sent {sent} alert(s), generated {packs_made} pack(s). Done.")


if __name__ == "__main__":
    main()
