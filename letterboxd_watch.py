import os, json, smtplib, ssl, re
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime, timezone
import feedparser
from bs4 import BeautifulSoup

# --- ENV ---
FEED_URL = os.environ["LBX_FEED_URL"]              # https://letterboxd.com/<user>/rss/
LBX_USER = os.environ.get("LBX_USER", "user")

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

# Snowflake (optional; if any missing, writing is skipped)
SF_ACCOUNT   = os.environ.get("SNOWFLAKE_ACCOUNT")
SF_USER      = os.environ.get("SNOWFLAKE_USER")
SF_PASSWORD  = os.environ.get("SNOWFLAKE_PASSWORD")
SF_ROLE      = os.environ.get("SNOWFLAKE_ROLE")
SF_DATABASE  = os.environ.get("SNOWFLAKE_DATABASE")
SF_SCHEMA    = os.environ.get("SNOWFLAKE_SCHEMA")
SF_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE")

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
    return getattr(e, f"letterboxd_{name}".lower(), None)

def stars_from_numeric(val_str):
    """'3.5' -> '★★★½'"""
    try:
        x = float(val_str)
    except Exception:
        return None
    full = int(x)
    half = abs(x - full) >= 0.5 - 1e-9
    return "★" * full + ("½" if half else "")

def numeric_from_stars(star_text):
    """'★★★½' -> 3.5"""
    if not star_text:
        return None
    full = star_text.count("★")
    half = 0.5 if "½" in star_text else 0.0
    return full + half

def parse_rating_from_title(title):
    if not title:
        return None
    parts = title.rsplit(" - ", 1)
    if len(parts) == 2 and ("★" in parts[1] or "½" in parts[1]):
        return parts[1].strip()
    stars = "".join(ch for ch in title if ch in "★½")
    return stars if stars else None

def strip_rating_from_title(title):
    if not title:
        return title
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
            "lbx_rewatch": _get_letterboxd_attr(e, "rewatch"),            # 'Yes' / 'No'
            "lbx_member_rating": _get_letterboxd_attr(e, "memberrating"), # '3.5'
            "lbx_watched_date": _get_letterboxd_attr(e, "watcheddate"),   # 'YYYY-MM-DD'
            "lbx_film_title": _get_letterboxd_attr(e, "filmtitle"),
            "lbx_film_year": _get_letterboxd_attr(e, "filmyear"),
        })
    items.sort(key=lambda i: i["published_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items

def detect_kind_and_text(item):
    """
    Returns (kind, text_plain, poster_url, has_review).
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
        meta_regex = re.compile(r"\b(Re)?watched on\b", re.I)
        remaining = "\n".join([ln for ln in text_plain.split("\n") if not meta_regex.search(ln)]).strip()
        has_review = bool(remaining)

    url = item.get("url", "") or ""
    if "/list/" in url and not item.get("lbx_film_title"):
        kind = "list"
    else:
        rewatch_val = (item.get("lbx_rewatch") or "").strip().lower()
        if rewatch_val == "yes":
            kind = "rewatch"
        elif rewatch_val == "no":
            kind = "watched"
        else:
            kind = "watched"

    return kind, text_plain, poster_url, has_review

def summarize_action(kind, rating_text, has_review):
    parts = []
    if kind == "rewatch":
        parts.append("Rewatched")
    elif kind == "watched":
        parts.append("Watched")
    elif kind == "list":
        parts.append("Created a list")
    else:
        parts.append("Activity")
    if rating_text:
        parts.append(f"Rated {rating_text}")
    if has_review:
        parts.append("Review added")
    return " • ".join(parts)

def enrich_item(item):
    # Build rating_text (stars) and rating_value (numeric)
    rating_text = None
    rating_value = None
    if item.get("lbx_member_rating"):
        try:
            rating_value = float(item["lbx_member_rating"])
            rating_text = stars_from_numeric(item["lbx_member_rating"])
        except Exception:
            rating_value = None
    if rating_text is None:
        rating_text = parse_rating_from_title(item.get("title"))
        if rating_text:
            rating_value = numeric_from_stars(rating_text)

    kind, text_plain, poster_url, has_review = detect_kind_and_text(item)
    display_title = strip_rating_from_title(item.get("title"))

    item["poster_url"] = poster_url
    item["kind"] = kind
    item["text_plain"] = text_plain
    item["rating"] = rating_text           # star string for emails
    item["rating_value"] = rating_value    # numeric for analytics
    item["has_review"] = has_review
    item["display_title"] = display_title
    item["action_summary"] = summarize_action(kind, rating_text, has_review)
    return item

# --- email ---
def build_email_payload(items_to_send, is_preview):
    if items_to_send:
        suffix = " (preview)" if is_preview else ""
        subject = f"[Letterboxd] {LBX_USER}: {len(items_to_send)} item(s){suffix}"
    else:
        subject = f"[Letterboxd] {LBX_USER}: no new activity today"

    plain_lines, html_parts = [], []

    if items_to_send:
        header = "Recent activity" if is_preview else "New activity"
        html_parts.append(f"<h2 style='margin:0 0 12px'>{header} for {LBX_USER}{' (preview)' if is_preview else ''}</h2>")
        if is_preview:
            html_parts.append(f"<p style='margin:0 0 12px;color:#555'>No new items today; showing the last {len(items_to_send)} for preview.</p>")

        html_parts.append("<ul style='padding-left:0;margin:0;list-style:none'>")
        for it in items_to_send:
            ts_event = it["published_at"].strftime("%Y-%m-%d %H:%M UTC") if it["published_at"] else ""
            watch_date = it.get("lbx_watched_date") or ""
            emoji = {"rewatch":"🔁","watched":"🎬","review":"✍️","list":"📃"}.get(it["kind"], "🎬")

            # plain text
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

            watched_html = f"<div style='color:#555;font-size:13px;margin-bottom:6px'>Watched date: {watch_date}</div>" if watch_date else ""

            html_parts.append(
                "<li style='margin:0 0 16px'>"
                "<div style='display:flex;gap:12px'>"
                f"{poster_html}"
                "<div style='min-width:0'>"
                f"<div style='font-weight:600;margin-bottom:4px'>{emoji} "
                f"<a href='{it['url']}' style='color:#0b57d0;text-decoration:none'>{it['display_title']}</a></div>"
                f"<div style='color:#555;font-size:13px;margin-bottom:2px'>Event: {ts_event}</div>"
                f"{watched_html}"
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

# --- Snowflake ---
def have_snowflake():
    needed = {
        "SNOWFLAKE_ACCOUNT": SF_ACCOUNT,
        "SNOWFLAKE_USER": SF_USER,
        "SNOWFLAKE_PASSWORD": SF_PASSWORD,
        "SNOWFLAKE_ROLE": SF_ROLE,
        "SNOWFLAKE_DATABASE": SF_DATABASE,
        "SNOWFLAKE_SCHEMA": SF_SCHEMA,
        "SNOWFLAKE_WAREHOUSE": SF_WAREHOUSE,
    }
    missing = [k for k, v in needed.items() if not v]
    if missing:
        print("[lbx] Snowflake disabled — missing secrets:", ", ".join(missing))
        return False
    return True

def write_to_snowflake(user_handle, items, last_seen_new):
    if not have_snowflake():
        return
    print(f"[lbx] Snowflake: writing {len(items)} items (watermark={last_seen_new})")
    import snowflake.connector
    conn = snowflake.connector.connect(
        account=SF_ACCOUNT, user=SF_USER, password=SF_PASSWORD,
        role=SF_ROLE, warehouse=SF_WAREHOUSE, database=SF_DATABASE, schema=SF_SCHEMA
    )
    try:
        cur = conn.cursor()
        print("[lbx] Snowflake: connected")
        for it in items:
            cur.execute("""
    MERGE INTO LETTERBOXD_ACTIVITY t
    USING (SELECT %s AS guid) s
    ON t.guid = s.guid
    WHEN MATCHED THEN UPDATE SET
      user_handle=%s,
      film_title=%s,
      film_year=%s,
      url=%s,
      kind=%s,
      rating_text=%s,
      rating_value=%s,
      has_review=%s,
      published_at=%s,
      watched_date=%s,
      poster_url=%s,
      action_summary=%s
    WHEN NOT MATCHED THEN INSERT (
      user_handle, guid, film_title, film_year, url, kind,
      rating_text, rating_value, has_review,
      published_at, watched_date, poster_url, action_summary, fetched_at
    ) VALUES (
      %s,%s,%s,%s,%s,%s,
      %s,%s,%s,
      %s,%s,%s,%s, CURRENT_TIMESTAMP()
    )
""", (
    it["guid"],
    # UPDATE values
    user_handle,
    it.get("lbx_film_title"),
    it.get("lbx_film_year"),
    it.get("url"),
    it.get("kind"),
    it.get("rating"),
    it.get("rating_value"),
    bool(it.get("has_review")),
    it.get("published_at"),
    it.get("lbx_watched_date"),
    it.get("poster_url"),
    it.get("action_summary"),

    # INSERT values
    user_handle, it["guid"],
    it.get("lbx_film_title"),
    it.get("lbx_film_year"),
    it.get("url"),
    it.get("kind"),
    it.get("rating"),
    it.get("rating_value"),
    bool(it.get("has_review")),
    it.get("published_at"),
    it.get("lbx_watched_date"),
    it.get("poster_url"),
    it.get("action_summary"),
))


        if last_seen_new:
            cur.execute("""
                MERGE INTO LETTERBOXD_WATERMARK t
                USING (SELECT %s AS user_handle, %s::TIMESTAMP_TZ AS last_seen) s
                ON t.user_handle = s.user_handle
                WHEN MATCHED THEN UPDATE SET last_seen = s.last_seen
                WHEN NOT MATCHED THEN INSERT (user_handle, last_seen) VALUES (s.user_handle, s.last_seen)
            """, (user_handle, last_seen_new))
        conn.commit()
        cur.close()
        print("[lbx] Snowflake: done")
    except Exception as e:
        print("[lbx] Snowflake ERROR:", repr(e))
        raise
    finally:
        conn.close()

# --- MAIN ---
def main():
    state = load_state()
    last_seen = datetime.fromisoformat(state["last_seen"]) if state["last_seen"] else None
    seen_guids = set(state.get("seen_guids", []))

    items = fetch_items(FEED_URL)
    items = [enrich_item(i) for i in items]

    # detect new for email
    new_items = []
    for i in items:
        if i["published_at"] and (not last_seen or i["published_at"] > last_seen):
            new_items.append(i)
        elif (not i["published_at"]) and i["guid"] and i["guid"] not in seen_guids:
            new_items.append(i)

    if new_items:
        send_email(new_items, is_preview=False)
    else:
        if PREVIEW_LAST_N > 0 and items:
            send_email(items[:PREVIEW_LAST_N], is_preview=True)
        elif ALWAYS_EMAIL:
            send_email([], is_preview=False)

    # update watermark in JSON (clamp to now if feed is ahead)
    newest_ts = next((i["published_at"] for i in items if i["published_at"]), last_seen)
    now_utc = datetime.now(timezone.utc)
    if newest_ts and newest_ts > now_utc:
        print(f"[lbx] note: newest_ts {newest_ts} > now {now_utc}; clamping to now")
        newest_ts = now_utc

    if newest_ts:
        state["last_seen"] = newest_ts.isoformat()
    for i in items:
        if i["guid"]:
            seen_guids.add(i["guid"])
    state["seen_guids"] = list(seen_guids)[-4000:]
    save_state(state)

    # write to Snowflake (idempotent MERGE by guid)
    write_to_snowflake(LBX_USER, items, newest_ts)

if __name__ == "__main__":
    main()
