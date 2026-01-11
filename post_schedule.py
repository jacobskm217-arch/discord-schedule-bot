import os
import re
import html
import requests
from icalendar import Calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

ICS_URL = os.environ["ICS_URL"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK"]
TZ = ZoneInfo("America/New_York")

SAFE_LIMIT = 1850  # leave slack for headers/part labels

# Domains we never want to show in Discord (noise)
BLOCKED_LINK_DOMAINS = {
    "tel.meet",
    "support.google.com",
}

# Also block these URL schemes (if they appear)
BLOCKED_SCHEMES = {"tel"}


def fetch_ics(url: str) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def to_local_datetime(dt):
    # dt can be date or datetime depending on event type
    if hasattr(dt, "hour"):  # datetime
        return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
    return None  # skip all-day events for this summary


def short_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host.replace("www.", "") or "link"
    except Exception:
        return "link"


def is_blocked_url(url: str) -> bool:
    try:
        p = urlparse(url)
        scheme = (p.scheme or "").lower()
        host = (p.netloc or "").lower().replace("www.", "")

        if scheme in BLOCKED_SCHEMES:
            return True

        if host in BLOCKED_LINK_DOMAINS:
            return True

        return False
    except Exception:
        return False


def extract_links(text: str) -> list[tuple[str, str]]:
    """
    Returns list of (label, url).
    - If HTML anchors exist, uses anchor text as label.
    - Also captures naked URLs.
    - Filters out blocked/noise links (tel.meet, support.google.com, tel:)
    """
    links: list[tuple[str, str]] = []

    t = html.unescape(text or "")

    # Extract HTML anchors: <a href="URL">TEXT</a>
    anchor_pat = re.compile(
        r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL
    )
    for url, label in anchor_pat.findall(t):
        url = url.strip()
        if is_blocked_url(url):
            continue

        label = re.sub(r"<[^>]+>", "", label)  # strip nested tags
        label = " ".join(label.split()).strip()
        if not label:
            label = short_domain(url)
        links.append((label, url))

    # Remove anchors so we don’t duplicate when scanning naked URLs
    t_no_anchors = anchor_pat.sub(" ", t)

    # Naked URLs
    url_pat = re.compile(r"(https?://[^\s<>\"]+)")
    for url in url_pat.findall(t_no_anchors):
        url = url.rstrip(").,;\"'").strip()
        if is_blocked_url(url):
            continue
        links.append((short_domain(url), url))

    # De-dupe by URL, preserve order
    seen = set()
    deduped: list[tuple[str, str]] = []
    for label, url in links:
        if url not in seen:
            deduped.append((label, url))
            seen.add(url)
    return deduped


def html_to_text(desc: str) -> str:
    """
    Convert common HTML-ish ICS descriptions into readable plain text.
    - <br> => newline
    - strip remaining tags
    - unescape entities
    - collapse excessive blank lines
    """
    if not desc:
        return ""

    t = desc.replace("\\n", "\n").replace("\\,", ",")
    t = html.unescape(t)

    # Normalize <br> to newline
    t = re.sub(r"<\s*br\s*/?\s*>", "\n", t, flags=re.IGNORECASE)

    # Remove bold tags
    t = re.sub(r"</?\s*b\s*>", "", t, flags=re.IGNORECASE)

    # Remove anchor tags but keep visible text (links are listed separately)
    t = re.sub(r'<a\s+[^>]*href="[^"]+"[^>]*>', "", t, flags=re.IGNORECASE)
    t = re.sub(r"</\s*a\s*>", "", t, flags=re.IGNORECASE)

    # Strip any remaining tags
    t = re.sub(r"<[^>]+>", "", t)

    # Clean whitespace
    lines = [ln.strip() for ln in t.splitlines()]
    cleaned = []
    for ln in lines:
        if ln == "" and (not cleaned or cleaned[-1] == ""):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()


def split_into_messages(blocks: list[str], header: str) -> list[str]:
    """
    Packs blocks into multiple Discord messages under SAFE_LIMIT.
    """
    messages = []
    current = header.strip() + "\n\n"
    for b in blocks:
        if len(current) + len(b) + 1 > SAFE_LIMIT:
            messages.append(current.rstrip())
            current = header.strip() + "\n\n" + b + "\n"
        else:
            current += b + "\n"
    if current.strip():
        messages.append(current.rstrip())
    return messages


def post_to_discord(content: str) -> None:
    resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Discord webhook failed {resp.status_code}: {resp.text[:500]}")


def next_weekday_range_mon_fri(now: datetime) -> tuple[datetime, datetime]:
    """
    Returns (start, end) for the *next* Monday 00:00 through Saturday 00:00 (i.e., Mon–Fri).
    Uses local timezone already attached to `now`.
    """
    # weekday(): Mon=0 ... Sun=6
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7  # if it's Monday, target next week's Monday

    next_monday = (now + timedelta(days=days_until_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_saturday = next_monday + timedelta(days=5)  # Saturday 00:00
    return next_monday, next_saturday


def main():
    ics_text = fetch_ics(ICS_URL)
    cal = Calendar.from_ical(ics_text)

    now = datetime.now(TZ)
    start, end = next_weekday_range_mon_fri(now)

    # Header includes the date range for clarity
    header = f"📅 **Next Week (Mon–Fri): {start.strftime('%b %d')}–{(end - timedelta(days=1)).strftime('%b %d')}**"

    events = []
    for component in cal.walk("VEVENT"):
        dtstart_raw = component.get("DTSTART")
        if not dtstart_raw:
            continue

        dtstart = to_local_datetime(dtstart_raw.dt)
        if dtstart is None:
            continue  # skip all-day events

        if start <= dtstart < end:
            title = str(component.get("SUMMARY", "(No title)")).strip()
            location = str(component.get("LOCATION", "")).strip()
            raw_desc = str(component.get("DESCRIPTION", "") or "")
            desc_text = html_to_text(raw_desc)
            links = extract_links(raw_desc)

            events.append({
                "time": dtstart,
                "title": title,
                "location": location,
                "desc": desc_text,
                "links": links,
            })

    events.sort(key=lambda e: e["time"])

    if not events:
        post_to_discord(header + "\n\nNo events found for next Monday–Friday.")
        return

    blocks: list[str] = []
    current_day = None

    for e in events:
        day_label = e["time"].strftime("%a %b %d")
        if day_label != current_day:
            current_day = day_label
            blocks.append(f"__**{day_label}**__")

        time_str = e["time"].strftime("%H:%M")
        blocks.append(f"**{time_str} — {e['title']}**")

        if e["location"]:
            blocks.append(f"📍 {e['location']}")

        # Description: URL-free, compact (first ~3 non-empty lines)
        if e["desc"]:
            desc_lines = []
            for ln in e["desc"].splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                if "http://" in ln or "https://" in ln:
                    continue
                desc_lines.append(ln)

            if desc_lines:
                compact = desc_lines[:3]
                if len(desc_lines) > 3:
                    compact.append("…")
                blocks.append("\n".join(compact))

        # Links: all URLs go here, filtered, always raw URLs
        if e["links"]:
            link_lines = []
            for label, url in e["links"][:4]:
                label = (label or "").strip()
                if label and label.lower() not in {short_domain(url).lower(), url.lower()}:
                    link_lines.append(f"- {label}: {url}")
                else:
                    link_lines.append(f"- {url}")
            blocks.append("🔗 Links:\n" + "\n".join(link_lines))

        blocks.append("")

    messages = split_into_messages(blocks, header)

    if len(messages) > 1:
        total = len(messages)
        messages = [
            msg.replace(header, f"{header} *(Part {i+1}/{total})*", 1)
            for i, msg in enumerate(messages)
        ]

    for msg in messages:
        post_to_discord(msg)


if __name__ == "__main__":
    main()
