import os, json, smtplib, ssl, re
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime, timezone
import feedparser
from bs4 import BeautifulSoup

# --- ENV ---
FEED_URL = os.environ["LBX_FEED_URL"]              # e.g. https://letterboxd.com/zawadiaka1/rss/
LBX_USER = os.environ.get("LBX_USER", "user")

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

ALWAYS_EMAIL = os.environ.get("ALWAYS_EMAIL", "1") == "1"
PREVIEW_LAST_N = int(os.environ.get("PREVIEW_LAST_N", "3"))

STATE_PATH = Path("data/state.json")

# --- STATE ---
def load_state():
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_seen": None, "seen_guids": []}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# --- helpers ---
def parse_dt(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    return datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=timezone.utc)

def _entry_html(e):
    html = getattr(e, "summary", None)
    if not html:
        content = getattr(e, "content", None)
        if content and len(content) > 0 and "value" in content[0]:
            html = content[0]["value"]
    return html or ""

def _get_letterboxd_attr(e, name):
    # feedparser maps 'letterboxd:memberRating' -> 'letterboxd_memberrating' (lowercase)
    return getattr(e, f"letterboxd_{name}".lower(), None)

def stars_from_numeric(val_str):
    try:
        x = float(val_str)
    except Exception:
        return None
    full = int(x)
    half = abs(x - full) >= 0.5 - 1e-9
    return "★" * full + ("½" if half else "")

def parse_rating_from_title(title: str) -> str | None:
    if not title:
        return None
    parts = title.rsplit(" - ", 1)
    if len(parts) == 2 and ("★" in parts[1] or "½" in parts[1]):
        return parts[1].strip()
    stars = "".join(ch for ch in title if ch in "★½")
    return stars if stars else None

def strip_rating_from_title(title: str) -> str:
    if not title:
        return title
    # remove trailing " - ★★½" if present
    parts = title.rsplit(" - ", 1)
    if len(parts) == 2 and ("★" in parts[1] or "½" in parts[1]):
        return parts[0]
    return title

def fetch_items(url):
    feed = feedparser.parse(url)
    items = []
    for e in feed.entries:
        guid = getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None)
        items.append({
            "guid": guid,
            "title": getattr(e, "title", "(no title)"),
            "url": getattr(e, "link", ""),
            "published_at": parse_dt(e),
            "raw_html": _entry_html(e),
            # namespaced fields:
            "lbx_rewatch": _get_letterboxd_attr(e, "rewatch"),               # 'Yes' / 'No'
            "lbx_member_rating": _get_letterboxd_attr(e, "memberrating"),    # e.g. '3.5'
            "lbx_watched_date": _get_letterboxd_attr(e, "watcheddate"),      # 'YYYY-MM-DD'
            "lbx_film_title": _get_letterboxd_attr(e, "filmtitle"),
            "lbx_film_year": _get_letterboxd_attr(e, "filmyear"),
        })
    items.sort(key=lambda i: i["published_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items

def detect_kind_and_text(item):
    """
    Base action (watched/rewatch/list) from namespaced fields/URL.
    Review presence from remaining text content.
    Returns (kind, text_plain, poster_url, has_review)
    """
    raw_html = item.get("raw_html", "") or ""
    soup = BeautifulSoup(raw_html, "html.parser") if raw_html else None

    poster_url = None
    text_plain = ""
    has_review = False

    if soup:
        img = soup.find("img")
        if img and img.get("src"):
            poster_url = img["src"]
        text_plain = soup.get_text("\n", strip=True)
        # Remove obvious meta lines to decide if there's review text
        meta_regex = re.compile(r"\b(Re)?watched on\b", re.I)
        remaining = "\n".join([ln for ln in text_plain.split("\n") if not meta_regex.search(ln)]).strip()
        has_review = bool(remaining)

    # list?
    url = item.get("url", "") or ""
    if "/list/" in url and not item.get("lbx_film_title"):
        kind = "list"
    else:
        # explicit rewatch flag wins
        rewatch_val = (item.get("lbx_rewatch") or "").strip().lower()
        if rewatch_val == "yes":
            kind = "rewatch"
        elif rewatch_val == "no":
            kind = "watched"
        else:
            kind = "watched"

    return kind, text_plain, poster_url, has_review

def summarize_action(kind: str, rating: str | None, has_review: bool) -> str:
    parts = []
    parts.append("Rewatched" if kind == "rewatch" else ("Watched" if kind == "watched" else "Created a list" if kind == "list" else "Activity"))
    if rating:
        parts.append(f"Rated {rating}")
    if has_review:
        parts.append("Review added")
    return " • ".join(parts)

def enrich_item(item):
    # rating: prefer numeric from letterboxd, fallback to stars from title
    rating = None
    if item.get("lbx_member_rating"):
        rating = stars_from_numeric(item["lbx_member_rating"])
    if not rating:
        rating = parse_rating_from_title(item.get("title"))

    kind, text_plain, poster_url, has_review = detect_kind_and_text(item)

    # clean display title (strip rating suffix if present)
    display_title = strip_rating_from_title(item.get("title"))

    item["poster_url"] = poster_url
    item["kind"] = kind
    item["text_plain"] = text_plain
    item["rating"] = rating
    item["has_review"] = has_review
    item["display_title"] = display_title
    item["action_summary"] = summarize_action(kind, rating, has_review)
    return item

# --- EMAIL ---
def build_email_payload(items_to_send, is_preview):
    if items_to_send:
        suffix = " (preview)" if is_preview else ""
        subject = f"[Letterboxd] {LBX_USER}: {len(items_to_send)} item(s){suffix}"
    else:
        subject = f"[Letterboxd] {LBX_USER}: no new activity today"

    plain_lines = []
    html_parts = []

    if items_to_send:
        hdr = "Recent activity" if is_preview else "New activity"
        sub = (f"No new items today; showing the last {len(items_to_send)} for preview."
               if is_preview else "")
        html_parts.append(f"<h2 style='margin:0 0 12px'>{hdr} for {LBX_USER}{' (preview)' if is_preview else ''}</h2>")
        if sub:
            html_parts.append(f"<p style='margin:0 0 12px;color:#555'>{sub}</p>")

        html_parts.append("<ul style='padding-left:0;margin:0;list-style:none'>")

        for it in items_to_send:
            ts_event = it["published_at"].strftime("%Y-%m-%d %H:%M UTC") if it["published_at"] else ""
            watch_date = it.get("lbx_watched_date") or ""
            emoji = {"rewatch":"🔁","watched":"🎬","review":"✍️","list":"📃"}.get(it["kind"], "🎬")

            # plain
            plain_lines.append(f"- {ts_event} {it['display_title']} — {it['url']}")
            if watch_date:
                plain_lines.append(f"  Watched date: {watch_date}")
            plain_lines.append(f"  {it['action_summary']}")
            if it.get("has_review"):
                plain_lines.append(f"  {it.get('text_plain','')}")

            # html
            poster_html = ""
            if it.get("poster_url"):
                poster_html = (
                    f"<img src='{it['poster_url']}' width='90' "
                    f"style='border-radius:8px;vertical-align:top;margin-right:12px;flex:0 0 auto' alt='poster'/>"
                )
            review_html = ""
            if it.get("has_review"):
                safe = (it.get("text_plain","")
                        .replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))
                review_html = f"<div style='white-space:pre-wrap;line-height:1.35;margin-top:6px'>{safe}</div>"

            html_parts.append(
                "<li style='margin:0 0 16px'>"
                "<div style='display:flex;gap:12px'>"
                f"{poster_html}"
                "<div style='min-width:0'>"
                f"<div style='font-weight:600;margin-bottom:4px'>{emoji} "
                f"<a href='{it['url']}' style='color:#0b57d0;text-decoration:none'>{it['display_title']}</a></div>"
                f"<div style='color:#555;font-size:13px;margin-bottom:2px'>Event: {ts_event}</div>"
                f"{(f\"<div style='color:#555;font-size:13px;margin-bottom:6px'>Watched date: {watch_date}</div>\") if watch_date else ''}"
                f"<div style='color:#222'>{it['action_summary']}</div>"
                f"{review_html}"
                "</div>"
                "</div>"
                "</li>"
            )

        html_parts.append("</ul>")
    else:
        plain_lines.append(f"No new activity for {LBX_USER} today.")
        html_parts.append(f"<p>No new activity for <strong>{LBX_USER}</strong> today.</p>")

    plain_text = "\n".join(plain_lines) if plain_lines else f"No new activity for {LBX_USER}."
    html_body = (
        "<!doctype html><html><body style='font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        "font-size:14px;color:#111;margin:0;padding:16px'>"
        + "".join(html_parts) +
        "</body></html>"
    )
    return subject, plain_text, html_body

def send_email(items_to_send, is_preview):
    subject, plain_text, html_body = build_email_payload(items_to_send, is_preview)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content(plain_text)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

# --- MAIN ---
def main():
    state = load_state()
    last_seen = datetime.fromisoformat(state["last_seen"]) if state["last_seen"] else None
    seen_guids = set(state.get("seen_guids", []))

    items = fetch_items(FEED_URL)
    items = [enrich_item(i) for i in items]

    # detect new
    new_items = []
    for i in items:
        if i["published_at"] and (not last_seen or i["published_at"] > last_seen):
            new_items.append(i)
        elif (not i["published_at"]) and i["guid"] and i["guid"] not in seen_guids:
            new_items.append(i)

    # send
    if new_items:
        send_email(new_items, is_preview=False)
    else:
        if PREVIEW_LAST_N > 0 and items:
            send_email(items[:PREVIEW_LAST_N], is_preview=True)
        elif ALWAYS_EMAIL:
            send_email([], is_preview=False)

    # state update
    newest_ts = next((i["published_at"] for i in items if i["published_at"]), last_seen)
    if newest_ts:
        state["last_seen"] = newest_ts.isoformat()
    for i in items:
        if i["guid"]:
            seen_guids.add(i["guid"])
    state["seen_guids"] = list(seen_guids)[-4000:]
    save_state(state)

if __name__ == "__main__":
    main()
