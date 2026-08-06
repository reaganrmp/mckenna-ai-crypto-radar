"""
McKenna + Jayden + Colton - AI/Crypto Content Radar
----------------------------------------------------
McKenna : finds fresh AI/crypto/regulation news (ID + international) and alerts you.
Jayden  : for the highest-priority stories, writes a ready-to-post content pack
          (hooks, slides, caption, formats, hashtags).
Colton  : generates an on-brand background image and composites the FINISHED,
          ready-to-post image (headline + teal highlight + your logo baked in).

ON-DEMAND COMMAND (new):
  Message McKenna's bot with:      regen: <paste the headline or story text>
  On the next run (within ~30 min, or trigger the workflow manually for instant),
  you'll get a fresh Jayden pack + Colton image for exactly that story - even if
  it's old news, got missed, or errored the first time. No code, no waiting on
  auto-detection.

You do NOT need to edit any code to run this. Tweak the CONFIG block if you want
to change check frequency, sources, or the pack cap.
"""

import os
import sys
import io
import json
import time
import html
import textwrap
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ------------------------- CONFIG (safe to tweak later) -------------------------
LOOKBACK_MINUTES = 20          # treat news from the last N minutes as "new"
MAX_ITEMS_PER_RUN = 20         # safety cap so a news burst can't spike your bill
MAX_PACKS_PER_RUN = 4          # cap on Jayden/Colton (slow+costly) per run
CLAUDE_MODEL = "claude-haiku-4-5-20251001"     # McKenna's cheap news filter
JAYDEN_MODEL = "claude-sonnet-5"               # better copywriting for packs
TOGETHER_IMAGE_MODEL = "black-forest-labs/FLUX.1-schnell-Free"  # swap to non-Free if you go paid

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
# --------------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CRYPTOPANIC_TOKEN = os.environ.get("CRYPTOPANIC_TOKEN")    # optional
JAYDEN_BOT_TOKEN = os.environ.get("JAYDEN_BOT_TOKEN")      # optional - enables content packs
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY")      # unused now - Colton runs on free Pollinations.ai instead

FLAGS = {"ID": "🇮🇩", "US": "🇺🇸", "CN": "🇨🇳", "Global": "🌐"}
VIRALITY_DOT = {"high": "🟢🟢🟢", "medium": "🟢🟢⚪", "low": "🟢⚪⚪"}
BRAND_STYLE = (
    "cinematic editorial tech-news key art, near-black background, "
    "deep teal (#367588) rim lighting and atmospheric glow as the dominant accent, "
    "subtle warm amber highlight as secondary accent, high contrast, deep shadows, "
    "dramatic single-source lighting, glossy premium finish, "
    "strong empty negative space in the lower third for headline text, "
    "photorealistic, ultra-detailed, 8k, shallow depth of field, "
    "no text, no letters, no words, no watermark, no logos, "
    "no people, no human figures, no silhouettes of people, no faces, no hands, "
    "purely objects/abstract/environmental scene only"
)


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


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


# ============================== JAYDEN (CONTENT PACK) ============================

def generate_pack(item, extra_context=""):
    """Turns a headline (+ optional summary/context) into a ready-to-post pack."""
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
- ONE slide 1 headline, punchy, confident, not corporate. Mark the single most
  important phrase within it as the "highlight" (the part that goes in accent color).
- Only include a second slide if there's a genuine "receipt" to show - a stat, a quote,
  a price number, something that proves the claim. NOT an explanation. If there's nothing
  worth proving, leave "slides" as an empty list - most posts should have ZERO extra slides.
- No walls of text anywhere. If it can't be said in one short line, cut it.

CAPTION RULE (important - this is what the audience actually reads):
The caption must make ANY reader fully understand the story, even if they know
nothing about crypto or AI. In 3-5 short sentences, plainly cover:
  1. WHAT happened (the concrete event, with the key number/name)
  2. WHY it happened / the context behind it
  3. WHAT it means for the reader - who gains, who loses, what changes
Write in simple everyday language, no jargon. If you must use a technical term,
explain it in the same breath. End with a short question or line that invites replies.
The hook is the dramatic version of the headline; the caption is the clear, honest
explanation behind it. Never leave the reader confused about what actually happened.

VISUAL RULE:
- If the news centers on a WIDELY RECOGNIZED public figure (a well-known CEO,
  official, politician, celebrity - someone the audience would recognize on sight),
  set "visual_type" to "real_photo" and in "visual_direction" describe the REAL
  news photo to search for (dark/desaturated background, subject well-lit,
  dramatic). We never fabricate images of a recognizable real person's likeness.
- If the news involves a real but NOT widely-recognized person (a named individual
  the audience wouldn't recognize on sight - e.g. "a hacker named Chris Brooks",
  "a scam victim", "an anonymous employee"), set "visual_type" to "ai_illustrative".
  In "visual_direction", describe a GENERIC illustrative scene showing anonymous,
  non-specific figures acting out the situation (e.g. "a person in a hoodie at a
  laptop, face obscured/turned away") - never attempt to replicate what the real
  individual actually looks like. This will be honestly labeled "AI Generated"
  on the image, same transparency practice professional crypto media uses for
  illustrative art.
- Otherwise (no person at all in the story) set "visual_type" to "ai_generated".
  In "visual_direction", describe ONE clear, concrete, OBJECT-based or
  ENVIRONMENTAL scene - never a person, figure, or silhouette of any kind, even a
  faceless/mysterious one. Ground it in the story's actual facts (the number, the
  scale, the place) and match its EMOTIONAL TONE:
    - good news / growth / big numbers -> abundance and motion: e.g. glowing money
      or coins flowing/cascading, light trails surging upward, a glowing map lighting
      up, energy radiating outward
    - bad news / risk / warning -> tension and danger: e.g. cracks spreading through
      a glowing coin, a chart line breaking downward in sparks, warning-red fractures
    - regulation / policy -> structure and authority: e.g. glowing gavel-like light
      beams, a sealed/locking mechanism, official document motifs rendered abstractly
  Always concrete and specific to THIS story's number/place/scale - never generic
  "digital finance" stock imagery. No text in the image.

Return ONLY JSON (no markdown, no extra text):
{{
 "hooks": ["3 punchy slide-1 headline options"],
 "why_hook": "one short line: why these headlines stop the scroll",
 "slides": ["at most 1 short 'proof/receipt' line, or leave this list empty"],
 "highlight": "the exact phrase from the chosen headline to put in accent color",
 "thumbnail_text": "the bold words to put ON the cover image",
 "caption": "3-5 sentence plain-language explanation per the CAPTION RULE above",
 "visual_type": "real_photo" | "ai_illustrative" | "ai_generated",
 "visual_direction": "one clear sentence describing the image concept",
 "formats": {{"tiktok": "one-line tip", "instagram": "...", "threads": "...", "x": "..."}},
 "hashtags": {{"tiktok": ["5-7 tags"], "instagram": ["..."], "threads": ["..."], "x": ["..."]}}
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
    return json.loads(text)


def format_pack(p, news_title=""):
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
    label = "📷 <b>Use a REAL photo:</b>" if vtype == "real_photo" else "🎨 <b>Visual:</b>"
    visual_block = f"{label} {esc(p.get('visual_direction', ''))}"

    # Full copyable prompt (same one Colton uses automatically) - so you can also
    # paste it into ChatGPT/Midjourney/Magnific yourself if you want manual control
    # or a different result. Not shown for real_photo since that needs an actual photo.
    prompt_block = ""
    if vtype != "real_photo":
        subject = p.get("visual_direction", "")
        full_prompt = f"{subject}. {BRAND_STYLE}"
        prompt_block = (
            f"\n🖨️ <b>Copy-paste prompt (for ChatGPT/Midjourney/Magnific etc):</b>\n"
            f"<code>{esc(full_prompt)}</code>\n"
        )

    return (
        f"✍️ <b>JAYDEN CONTENT PACK</b>\n"
        f"📰 <b>News:</b> {esc(news_title)}\n\n"
        f"🎣 <b>Slide 1 — Hook options:</b>\n{hooks}\n"
        f"<i>Why it works: {esc(p.get('why_hook', ''))}</i>\n\n"
        f"📄 <b>Next slides:</b>\n{slides}\n\n"
        f"✨ <b>Highlight line:</b> {esc(p.get('highlight', ''))}\n"
        f"🖼️ <b>Thumbnail text:</b> {esc(p.get('thumbnail_text', ''))}\n\n"
        f"{visual_block}\n"
        f"{prompt_block}\n"
        f"📝 <b>Caption:</b>\n{esc(p.get('caption', ''))}\n\n"
        f"📐 <b>Format per platform:</b>\n{fmt_lines}\n\n"
        f"#️⃣ <b>Hashtags:</b>\n{tag_lines}"
    )


# ============================== COLTON (IMAGE) ===================================

NEGATIVE_PROMPT = (
    "person, people, human, man, woman, character, warrior, knight, anime character, "
    "video game character, portrait, face, hands, fantasy character, creature, "
    "text, letters, words, watermark, logo, signature, caption, title, writing, "
    "blurry, low quality, low detail, distorted, deformed, mutated, extra limbs, "
    "grainy, pixelated, jpeg artifacts, oversaturated, ugly, amateur"
)
# Used for the "ai_illustrative" case: generic anonymous people ARE allowed (no
# specific real face is being replicated), just no fantasy/game-character drift.
NEGATIVE_PROMPT_ALLOW_PEOPLE = (
    "text, letters, words, watermark, logo, signature, caption, title, writing, "
    "fantasy character, video game character, anime character, armor, wings, "
    "blurry, low quality, low detail, distorted, deformed, mutated, extra limbs, "
    "grainy, pixelated, jpeg artifacts, oversaturated, ugly, amateur"
)


def generate_image(visual_direction, news_title, allow_generic_people=False):
    """Uses Pollinations.ai (free, no API key, no card needed) - just a URL hit.
    model=flux-realism gives rich detail/lighting. enhance=OFF because it lets an
    LLM rewrite the prompt and was drifting the concept off-topic (e.g. into fantasy
    game characters). A real 'negative' param is used instead of just saying "no X"
    in the positive prompt, which diffusion models routinely ignore."""
    import random
    subject = visual_direction or news_title
    prompt = f"{subject}. {BRAND_STYLE}"
    seed = random.randint(1, 999999)  # avoids getting a cached/repeated image
    negative = NEGATIVE_PROMPT_ALLOW_PEOPLE if allow_generic_people else NEGATIVE_PROMPT
    url = (f"https://image.pollinations.ai/prompt/{quote(prompt[:800])}"
           f"?width=1536&height=1920&model=flux-realism&nologo=true&seed={seed}"
           f"&negative={quote(negative)}")
    try:
        r = requests.get(url, timeout=120)
        if r.status_code != 200 or len(r.content) < 1000:
            log(f"Pollinations error {r.status_code}, size={len(r.content)}")
            return None
        return url  # fetchable directly for both Telegram sendPhoto and our own re-fetch
    except Exception as e:
        log(f"Image generation failed: {e}")
        return None


def compose_final_post(bg_bytes, headline, highlight_phrase, source_name="", ai_disclosure=False):
    """Composites Colton's background + Jayden's headline into a finished,
    ready-to-post 1080x1350 image with logo, gradient, and text baked in."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1080, 1350
    TEAL = (54, 117, 136)
    FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

    bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
    src_ratio, tgt_ratio = bg.width / bg.height, W / H
    if src_ratio > tgt_ratio:
        new_w = int(bg.height * tgt_ratio)
        bg = bg.crop(((bg.width - new_w) // 2, 0, (bg.width + new_w) // 2, bg.height))
    else:
        new_h = int(bg.width / tgt_ratio)
        bg = bg.crop((0, (bg.height - new_h) // 2, bg.width, (bg.height + new_h) // 2))
    bg = bg.resize((W, H))

    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        if y > H * 0.40:
            t = (y - H * 0.40) / (H * 0.60)
            grad.putpixel((0, y), int(240 * t))
    grad = grad.resize((W, H))
    black = Image.new("RGB", (W, H), (5, 5, 5))
    bg = Image.composite(black, bg, grad)

    draw = ImageDraw.Draw(bg)
    logo_font = ImageFont.truetype(f"{FONT_DIR}/Poppins-Bold.ttf", 38)
    src_font = ImageFont.truetype(f"{FONT_DIR}/Poppins-Regular.ttf", 24)

    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.png")
    if os.path.exists(logo_path):
        logo = Image.open(logo_path).convert("RGBA")
        target_h = 110
        scale = target_h / logo.height
        logo = logo.resize((int(logo.width * scale), target_h))
        bg.paste(logo, (50, 45), logo)
    else:
        draw.text((50, 50), "CRYPTOSPARK", font=logo_font, fill=(255, 255, 255))
        draw.rectangle([50, 95, 58, 103], fill=TEAL)

    # Keep the highlight phrase together on one line (swap its spaces for a
    # placeholder so wrapping can't break it apart), then restore before drawing.
    if highlight_phrase and highlight_phrase in headline:
        token = highlight_phrase.replace(" ", "\x00")
        headline_for_wrap = headline.replace(highlight_phrase, token)
    else:
        headline_for_wrap = headline

    max_line_width = W - 100  # 50px margin each side

    # Auto-shrink the font until every unbreakable chunk (words, and the
    # highlight phrase) actually fits within the image - fixes overflow for
    # long headlines/highlights instead of just guessing a fixed size.
    font_size = 62
    while font_size > 32:
        test_font = ImageFont.truetype(f"{FONT_DIR}/Poppins-Bold.ttf", font_size)
        widest_chunk = max(
            draw.textlength(w.replace("\x00", " "), font=test_font)
            for w in headline_for_wrap.split(" ")
        )
        if widest_chunk <= max_line_width:
            break
        font_size -= 4
    else:
        # Even the smallest size can't fit the highlight as one unbreakable chunk -
        # give up protecting it and let it wrap normally, rather than overflow.
        headline_for_wrap = headline_for_wrap.replace("\x00", " ")
    head_font = ImageFont.truetype(f"{FONT_DIR}/Poppins-Bold.ttf", font_size)
    line_height = int(font_size * 1.13)

    # Real pixel-width wrapping (measures actual text, not a character-count guess)
    words = headline_for_wrap.split(" ")
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        trial_measured = trial.replace("\x00", " ")  # measure with real spaces, not the placeholder
        if draw.textlength(trial_measured, font=head_font) <= max_line_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    wrapped = [line.replace("\x00", " ") for line in lines]

    y_text = H - 220 - (len(wrapped) * line_height)
    for line in wrapped:
        x = 50
        if highlight_phrase and highlight_phrase in line:
            pre, _, post = line.partition(highlight_phrase)
            draw.text((x, y_text), pre, font=head_font, fill=(255, 255, 255))
            pre_w = draw.textlength(pre, font=head_font)
            draw.text((x + pre_w, y_text), highlight_phrase, font=head_font, fill=TEAL)
            hi_w = draw.textlength(highlight_phrase, font=head_font)
            draw.text((x + pre_w + hi_w, y_text), post, font=head_font, fill=(255, 255, 255))
        else:
            draw.text((x, y_text), line, font=head_font, fill=(255, 255, 255))
        y_text += 70

    if source_name:
        credit = f"Sumber: AI Generated" if ai_disclosure else f"Sumber: {source_name}"
        draw.text((50, H - 55), credit, font=src_font, fill=(160, 160, 160))
    elif ai_disclosure:
        draw.text((50, H - 55), "Sumber: AI Generated", font=src_font, fill=(160, 160, 160))

    out = io.BytesIO()
    bg.save(out, format="PNG")
    return out.getvalue()


# ============================== TELEGRAM HELPERS =================================

def send_telegram(text, token=None):
    token = token or TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                                     "parse_mode": "HTML", "disable_web_page_preview": False},
                          timeout=20)
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def send_telegram_photo(image_url, caption, token):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "photo": image_url,
                                     "caption": caption[:1000], "parse_mode": "HTML"}, timeout=60)
        if r.status_code != 200:
            log(f"Telegram photo error {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        log(f"Telegram photo send failed: {e}")
        return False


def send_telegram_photo_bytes(image_bytes, caption, token):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        r = requests.post(url,
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000], "parse_mode": "HTML"},
                          files={"photo": ("post.png", image_bytes, "image/png")}, timeout=60)
        if r.status_code != 200:
            log(f"Telegram photo(bytes) error {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        log(f"Telegram photo(bytes) send failed: {e}")
        return False


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


# ============================== PACK + IMAGE PIPELINE =============================

def run_pack_pipeline(item, extra_context="", label="story"):
    """Shared by both the auto high-priority path and the manual 'regen:' command.
    Generates a Jayden pack, sends it, then generates + composites + sends Colton's image."""
    if not JAYDEN_BOT_TOKEN:
        log("No JAYDEN_BOT_TOKEN set - skipping pack generation.")
        return False
    try:
        log(f"Generating pack for {label}: {item['title'][:60]}...")
        pack = generate_pack(item, extra_context)
        send_telegram(format_pack(pack, item["title"]), token=JAYDEN_BOT_TOKEN)
        time.sleep(1)
    except Exception as ex:
        log(f"Pack generation failed for {label}: {ex}")
        send_telegram(f"⚠️ Jayden hit a snag on: {html.escape(item['title'][:100])}\n({ex})",
                      token=JAYDEN_BOT_TOKEN)
        return False

    # Colton (Pollinations) needs no API key - always try

    try:
        log(f"Colton: generating background for {label}...")
        vtype = pack.get("visual_type", "ai_generated")
        img_url = generate_image(pack.get("visual_direction", ""), item["title"],
                                  allow_generic_people=(vtype == "ai_illustrative"))
        if not img_url:
            log("Colton: no image produced.")
            return True

        if vtype == "real_photo":
            note = ("🖼️ <b>Colton background</b> (this story has a real, recognizable "
                    "person - add their real photo yourself, this is a supporting element)")
            send_telegram_photo(img_url, note, JAYDEN_BOT_TOKEN)
            return True

        bg_bytes = requests.get(img_url, timeout=30).content
        hooks = pack.get("hooks", [])
        headline = hooks[0] if hooks else pack.get("thumbnail_text", "")
        final_png = compose_final_post(bg_bytes, headline=headline,
                                       highlight_phrase=pack.get("highlight", ""),
                                       source_name=item.get("source", ""),
                                       ai_disclosure=(vtype == "ai_illustrative"))
        cap = f"✅ Ready to post.\n📝 {html.escape(pack.get('caption', '')[:600])}"
        send_telegram_photo_bytes(final_png, cap, JAYDEN_BOT_TOKEN)
        return True
    except Exception as ex:
        log(f"Colton failed for {label}: {ex}")
        send_telegram(f"⚠️ Colton hit a snag on: {html.escape(item['title'][:100])}\n({ex})",
                      token=JAYDEN_BOT_TOKEN)
        return False


def handle_regen_commands():
    """Checks McKenna's bot for 'regen: <text>' messages from you and (re)generates
    a full Jayden pack + Colton image for that exact text, on demand."""
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

    # 1) Handle any manual "regen:" requests first - fast, and works even if
    #    the news-fetch part below finds nothing new this run.
    handle_regen_commands()

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

    sent = 0
    packs_made = 0
    for a in results:
        n = a.get("n")
        if not isinstance(n, int) or n < 1 or n > len(items):
            continue
        item = dict(items[n - 1])
        item["markets"] = a.get("markets", [])

        if send_telegram(format_message(item, a)):
            sent += 1
            time.sleep(1)

        if packs_made >= MAX_PACKS_PER_RUN:
            continue

        if JAYDEN_BOT_TOKEN and str(a.get("virality_potential", "")).lower() == "high":
            if run_pack_pipeline(item, extra_context=a.get("summary", ""), label="auto high-priority"):
                packs_made += 1

    log(f"Sent {sent} alert(s), generated {packs_made} pack(s). Done.")


if __name__ == "__main__":
    main()
