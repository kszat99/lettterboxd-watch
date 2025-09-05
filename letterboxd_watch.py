import os, json, smtplib, ssl
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime, timezone
import feedparser
from bs4 import BeautifulSoup

# --- ENV ---
FEED_URL = os.environ["LBX_FEED_URL"]              # np. https://letterboxd.com/zawadiaka1/rss/
LBX_USER = os.environ.get("LBX_USER", "user")

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

# wysyłaj e-mail nawet, gdy brak nowości (domyślnie TAK)
ALWAYS_EMAIL = os.environ.get("ALWAYS_EMAIL", "1") == "1"
# ile ostatnich wpisów pokazać jako "preview", gdy brak nowości
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

# --- FEED UTILS ---
def parse_dt(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    return datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=timezone.utc)

def _entry_html(e):
    # Letterboxd umieszcza opis w summary lub content[0].value
    html = getattr(e, "summary", None)
    if not html:
        content = getattr(e, "content", None)
        if content and len(content) > 0 and "value" in content[0]:
            html = content[0]["value"]
    return html or ""

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
        })
    items.sort(key=lambda i: i["published_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items

def enrich_item(item):
    """Wyciąga typ aktywności, treść i plakat z HTML-a."""
    html = item.get("raw_html", "")
    soup = BeautifulSoup(html, "html.parser") if html else None

    poster_url = None
    text_plain = ""
    kind = "activity"  # watched/rewatch/review/list/activity

    if soup:
        # plakat = pierwszy <img>
        img = soup.find("img")
        if img and img.get("src"):
            poster_url = img["src"]

        # tekst – zrzut treści bez HTML
        text_plain = soup.get_text("\n", strip=True)

        # prosta heurystyka typu
        text_low = text_plain.lower()
        title_low = (item.get("title") or "").lower()
        url_low = (item.get("url") or "").lower()

        if "/list/" in url_low or "list" in title_low:
            kind = "list"
        elif "rewatch" in text_low or "rewatched" in text_low or "ponownie" in text_low:
            kind = "rewatch"
        elif "review" in text_low or "recenz" in text_low:
            kind = "review"
        elif "watched" in text_low or "obejrza" in text_low:
            kind = "watched"
        else:
            kind = "activity"

    # bez obcinania recenzji

    item["poster_url"] = poster_url
    item["kind"] = kind
    item["text_plain"] = text_plain
    return item

# --- EMAIL ---
def build_email_payload(items_to_send, is_preview):
    # temat
    if items_to_send:
        suffix = " (preview)" if is_preview else ""
        subject = f"[Letterboxd] {LBX_USER}: {len(items_to_send)} item(s){suffix}"
    else:
        subject = f"[Letterboxd] {LBX_USER}: no new activity today"

    plain_lines = []
    html_parts = []

    if items_to_send:
        if is_preview:
            plain_lines.append(f"Preview of last {len(items_to_send)} activities for {LBX_USER} (not new).")
            html_parts.append(
                f"<h2 style='margin:0 0 12px'>Recent activity for {LBX_USER} (preview)</h2>"
                f"<p style='margin:0 0 12px;color:#555'>No new items today; showing the last {len(items_to_send)} for preview.</p>"
            )
        else:
            plain_lines.append(f"{len(items_to_send)} new activities for {LBX_USER}:")
            html_parts.append(f"<h2 style='margin:0 0 12px'>New activity for {LBX_USER}</h2>")

        html_parts.append("<ul style='padding-left:16px;margin:0'>")

        for it in items_to_send:
            ts = it["published_at"].strftime("%Y-%m-%d %H:%M UTC") if it["published_at"] else ""
            kind_emoji = {"rewatch":"🔁","watched":"🎬","review":"✍️","list":"📃"}.get(it["kind"], "🟢")
            # plain
            plain_lines.append(f"- {ts} {kind_emoji} {it['title']} — {it['url']}")
            if it.get("text_plain"):
                plain_lines.append(f"  {it['text_plain']}")
            # html
            poster_html = ""
            if it.get("poster_url"):
                poster_html = (
                    f"<img src='{it['poster_url']}' width='90' "
                    f"style='border-radius:8px;vertical-align:top;margin-right:10px' alt='poster'/>"
                )
            text_html = (it.get('text_plain','')
                           .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            html_parts.append(
                "<li style='margin:0 0 14px;list-style:disc'>"
                f"<div style='display:flex;gap:10px'>"
                f"{poster_html}"
                f"<div>"
                f"<div style='font-weight:600;margin-bottom:4px'>{kind_emoji} "
                f"<a href='{it['url']}' style='color:#0b57d0;text-decoration:none'>{it['title']}</a></div>"
                f"<div style='color:#555;font-size:13px;margin-bottom:4px'>{ts}</div>"
                f"<div style='white-space:pre-wrap;line-height:1.35'>{text_html}</div>"
                f"</div>"
                f"</div>"
                "</li>"
            )
        html_parts.append("</ul>")
    else:
        # fallback – gdy kompletnie brak elementów do pokazania
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

    # wykryj nowości
    new_items = []
    for i in items:
        if i["published_at"] and (not last_seen or i["published_at"] > last_seen):
            new_items.append(i)
        elif (not i["published_at"]) and i["guid"] and i["guid"] not in seen_guids:
            new_items.append(i)

    # wysyłka:
    # - jeśli są nowe -> wyślij nowe
    # - jeśli nie ma i PREVIEW_LAST_N > 0 -> pokaż ostatnie N jako preview
    # - jeśli PREVIEW_LAST_N == 0 -> wyślij tylko "no new activity" (gdy ALWAYS_EMAIL=1)
    if new_items:
        send_email(new_items, is_preview=False)
    else:
        if PREVIEW_LAST_N > 0 and items:
            items_preview = items[:PREVIEW_LAST_N]
            send_email(items_preview, is_preview=True)
        elif ALWAYS_EMAIL:
            send_email([], is_preview=False)

    # aktualizacja stanu
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
