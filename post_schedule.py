import os
import re
import json
import html
import requests
from icalendar import Calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

ICS_URL = os.environ["ICS_URL"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK"]
TZ = ZoneInfo("America/New_York")

SAFE_LIMIT = 1850
STATE_FILE = "schedule_state.json"

# Noise links to remove
BLOCKED_LINK_DOMAINS = {"tel.meet", "support.google.com"}
BLOCKED_SCHEMES = {"tel"}


def fetch_ics(url: str) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def to_local_datetime(dt):
    if hasattr(dt, "hour"):  # datetime
        return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
    return None  # skip all-day events


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip(").,;\"'")


def short_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host.replace("www.", "") or "link"
    except Exception:
        return "link"


def url_host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def is_blocked_url(url: str) -> bool:
    try:
        p = urlparse(url)
        scheme = (p.scheme or "").lower()
        host = (p.netloc or "").lower().replace("www.", "")
        return scheme in BLOCKED_SCHEMES or host in BLOCKED_LINK_DOMAINS
    except Exception:
        return False


def is_google_doc_like(url: str) -> bool:
    host = url_host(url)
    # WSIB links you’re seeing are usually Google Sheets
    return host.endswith("docs.google.com") or host.endswith("drive.google.com")


def extract_links(text: str) -> list[tuple[str, str]]:
    """
    Returns list of (label, url) from HTML anchors and naked URLs.
    De-duped by URL, blocked/noise links filtered out.
    """
    links: list[tuple[str, str]] = []
    t = html.unescape(text or "")

    anchor_pat = re.compile(
        r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for url, label in anchor_pat.findall(t):
        url = normalize_url(url)
        if not url or is_blocked_url(url):
            continue
        label = re.sub(r"<[^>]+>", "", label)
        label = " ".join(label.split()).strip() or short_domain(url)
        links.append((label, url))

    # Remove anchors before scanning for naked URLs (avoid duplicates)
    t_no_anchors = anchor_pat.sub(" ", t)

    url_pat = re.compile(r"(https?://[^\s<>\"]+)")
    for url in url_pat.findall(t_no_anchors):
        url = normalize_url(url)
        if not url or is_blocked_url(url):
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
    Convert HTML-ish ICS descriptions into readable plain text.
    """
    if not desc:
        return ""
    t = desc.replace("\\n", "\n").replace("\\,", ",")
    t = html.unescape(t)

    # <br> => newline
    t = re.sub(r"<\s*br\s*/?\s*>", "\n", t, flags=re.IGNORECASE)

    # Remove bold tags
    t = re.sub(r"</?\s*b\s*>", "", t, flags=re.IGNORECASE)

    # Remove anchor tags but keep visible text
    t = re.sub(r'<a\s+[^>]*href="[^"]+"[^>]*>', "", t, flags=re.IGNORECASE)
    t = re.sub(r"</\s*a\s*>", "", t, flags=re.IGNORECASE)

    # Strip any remaining tags
    t = re.sub(r"<[^>]+>", "", t)

    # Clean whitespace / blank runs
    lines = [ln.strip() for ln in t.splitlines()]
    cleaned = []
    for ln in lines:
        if ln == "" and (not cleaned or cleaned[-1] == ""):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()


def split_into_messages(blocks: list[str], header: str) -> list[str]:
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


def webhook_messages_base(webhook_url: str) -> str:
    return webhook_url.rstrip("/") + "/messages"


def post_to_discord_return_id(content: str) -> str:
    url = WEBHOOK_URL.rstrip("/") + "?wait=true"
    resp = requests.post(url, json={"content": content}, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Discord post failed {resp.status_code}: {resp.text[:500]}")
    return str(resp.json()["id"])


def delete_discord_message(message_id: str) -> None:
    url = f"{webhook_messages_base(WEBHOOK_URL)}/{message_id}"
    resp = requests.delete(url, timeout=30)
    if resp.status_code not in (204, 404):
        raise RuntimeError(f"Discord delete failed {resp.status_code}: {resp.text[:500]}")


def load_previous_message_ids() -> list[str]:
    if not os.path.exists(STATE_FILE):
        return []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ids = data.get("message_ids", [])
        return [str(x) for x in ids if str(x).strip()]
    except Exception:
        return []


def save_message_ids(ids: list[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"message_ids": ids}, f, indent=2)


def next_weekday_range_mon_fri(now: datetime) -> tuple[datetime, datetime]:
    # Next Monday 00:00 → Saturday 00:00 (Mon–Fri)
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_monday = (now + timedelta(days=days_until_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_saturday = next_monday + timedelta(days=5)
    return next_monday, next_saturday


def extract_wsib_title(desc_text: str) -> str | None:
    """
    Pulls the right side from lines like:
      'WSIB | MILMED 100: LAB Cardiac/PTX Ultrasound'
    """
    if not desc_text:
        return None
    for ln in desc_text.splitlines():
        ln = ln.strip()
        m = re.match(r"^WSIB\s*\|\s*(.+)$", ln, flags=re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            return val if val else None
    return None


def make_compact_description(desc_text: str, max_lines: int = 2) -> str | None:
    """
    URL-free snippet, and NEVER include WSIB| lines.
    """
    if not desc_text:
        return None

    keep = []
    for ln in desc_text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if "http://" in ln or "https://" in ln:
            continue
        # Remove any WSIB marker line
        if re.match(r"^WSIB\s*\|", ln, flags=re.IGNORECASE):
            continue
        keep.append(ln)

    if not keep:
        return None

    out = keep[:max_lines]
    if len(keep) > max_lines:
        out.append("…")
    return "\n".join(out)


def classify_links(links: list[tuple[str, str]], wsib_title_present: bool) -> dict:
    """
    Buckets:
      - wsib: url or None
      - canvas: url or None
      - meet: url or None
      - other: list[(label,url)]
    WSIB rule:
      If wsib_title_present and there is at least one Google doc-like link,
      treat the first such link as WSIB (even if label doesn't say WSIB).
    """
    canvas_url = None
    meet_url = None

    google_doc_links = []  # candidates for WSIB
    other = []

    for label, url in links:
        host = url_host(url)

        if host.endswith("canvas.usuhs.edu"):
            if canvas_url is None:
                canvas_url = url
            else:
                other.append((label, url))
            continue

        if host.endswith("meet.google.com"):
            if meet_url is None:
                meet_url = url
            else:
                other.append((label, url))
            continue

        if is_google_doc_like(url):
            google_doc_links.append((label, url))
            continue

        other.append((label, url))

    wsib_url = None

    # If WSIB title exists, "upgrade" the first Google doc link to WSIB
    if wsib_title_present and google_doc_links:
        wsib_url = google_doc_links[0][1]
        # Any remaining google doc links become "Other Links"
        for label, url in google_doc_links[1:]:
            other.append((label, url))
    else:
        # No WSIB title; treat google doc links as other
        for label, url in google_doc_links:
            other.append((label, url))

    return {
        "wsib": wsib_url,
        "canvas": canvas_url,
        "meet": meet_url,
        "other": other,
    }


def format_event_block(event: dict) -> str:
    """
    Title first line, then labeled fields.
    """
    title = event["title"]
    time_str = event["time"].strftime("%a %b %d, %H:%M")
    location = event["location"]
    desc = event.get("desc_snippet")

    wsib_url = event["links"]["wsib"]
    canvas_url = event["links"]["canvas"]
    meet_url = event["links"]["meet"]
    other_links = event["links"]["other"]

    lines = []
    lines.append(f"**{title}**")
    lines.append(f"Time: {time_str}")
    if location:
        lines.append(f"Location: {location}")

    if desc:
        lines.append(desc)

    # Primary labeled links
    if wsib_url:
        lines.append(f"WSIB: {wsib_url}")
    if canvas_url:
        lines.append(f"Canvas: {canvas_url}")
    if meet_url:
        lines.append(f"Meet: {meet_url}")

    # Other Links
    if other_links:
        lines.append("Other Links:")
        for label, url in other_links:
            label = (label or "").strip()
            if label and label.lower() not in {short_domain(url).lower(), url.lower()}:
                lines.append(f"- {label}: {url}")
            else:
                lines.append(f"- {url}")

    return "\n".join(lines).strip()


def main():
    # Delete previous week's messages first
    for mid in load_previous_message_ids():
        delete_discord_message(mid)

    ics_text = fetch_ics(ICS_URL)
    cal = Calendar.from_ical(ics_text)

    start, end = next_weekday_range_mon_fri(datetime.now(TZ))
    header = f"📅 **Next Week (Mon–Fri): {start.strftime('%b %d')}–{(end - timedelta(days=1)).strftime('%b %d')}**"

    events = []
    for component in cal.walk("VEVENT"):
        dtstart_raw = component.get("DTSTART")
        if not dtstart_raw:
            continue

        dtstart = to_local_datetime(dtstart_raw.dt)
        if dtstart is None:
            continue  # skip all-day

        if start <= dtstart < end:
            title = str(component.get("SUMMARY", "(No title)")).strip()
            location = str(component.get("LOCATION", "")).strip()
            raw_desc = str(component.get("DESCRIPTION", "") or "")

            desc_text = html_to_text(raw_desc)
            wsib_title = extract_wsib_title(desc_text)
            desc_snippet = make_compact_description(desc_text, max_lines=2)

            links_raw = extract_links(raw_desc)
            buckets = classify_links(links_raw, wsib_title_present=bool(wsib_title))

            # Remove any "Other Links" that duplicate primary links
            primary_urls = {u for u in [buckets["wsib"], buckets["canvas"], buckets["meet"]] if u}
            buckets["other"] = [(lbl, url) for (lbl, url) in buckets["other"] if url not in primary_urls]

            events.append({
                "time": dtstart,
                "title": title,
                "location": location,
                "desc_snippet": desc_snippet,
                "links": buckets,
            })

    events.sort(key=lambda e: e["time"])

    if not events:
        new_id = post_to_discord_return_id(header + "\n\nNo events found for next Monday–Friday.")
        save_message_ids([new_id])
        return

    # Build blocks grouped by day label (optional, but keeps it readable)
    blocks: list[str] = []
    current_day = None

    for e in events:
        day_label = e["time"].strftime("%a %b %d")
        if day_label != current_day:
            current_day = day_label
            blocks.append(f"__**{day_label}**__")

        blocks.append(format_event_block(e))
        blocks.append("")  # spacer

    messages = split_into_messages(blocks, header)

    if len(messages) > 1:
        total = len(messages)
        messages = [
            msg.replace(header, f"{header} *(Part {i+1}/{total})*", 1)
            for i, msg in enumerate(messages)
        ]

    new_ids = []
    for msg in messages:
        new_ids.append(post_to_discord_return_id(msg))

    save_message_ids(new_ids)


if __name__ == "__main__":
    main()
