import os, json, smtplib, ssl
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime, timezone
import feedparser

FEED_URL = os.environ["LBX_FEED_URL"]
LBX_USER = os.environ.get("LBX_USER", "user")

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

STATE_PATH = Path("data/state.json")

def load_state():
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_seen": None, "seen_guids": []}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def parse_dt(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t: return None
    return datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=timezone.utc)

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
        })
    items.sort(key=lambda i: i["published_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items

def send_email(new_items):
    if not new_items: return
    subject = f"[Letterboxd] {LBX_USER}: {len(new_items)} new item(s)"
    lines = [f"{len(new_items)} new activities for {LBX_USER}:"]
    for it in new_items:
        ts = it["published_at"].strftime("%Y-%m-%d %H:%M UTC") if it["published_at"] else ""
        lines.append(f"- {ts} — {it['title']} — {it['url']}")
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

def main():
    state = load_state()
    last_seen = datetime.fromisoformat(state["last_seen"]) if state["last_seen"] else None
    seen_guids = set(state.get("seen_guids", []))

    items = fetch_items(FEED_URL)

    new_items = []
    for i in items:
        if i["published_at"] and (not last_seen or i["published_at"] > last_seen):
            new_items.append(i)
        elif (not i["published_at"]) and i["guid"] and i["guid"] not in seen_guids:
            new_items.append(i)

    send_email(new_items)

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
