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

DISCORD_LIMIT = 2000
SAFE_LIMIT = 1850  # leave slack for headers/part labels


def fetch_ics(url: str) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def to_local_datetime(dt):
    # dt can be date or datetime depending on event type
    if hasattr(dt, "hour"):  # datetime
        return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
    return None  # skip all-day events for this summary


def extract_links(text: str) -> list[tuple[str, str]]:
    """
    Returns list of (label, url).
    - If HTML anchors exist, uses anchor text as label.
    - Also captures naked URLs.
    """
    links: list[tuple[str, str]] = []

    # Unescape HTML entities first so regex sees normal characters
    t = html.unescape(text or "")

    # Extract HTML anchors: <a href="URL">TEXT</a>
    anchor_pat = re.compile(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    for url, label in anchor_pat.findall(t):
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
        # trim trailing punctuation
        url = url.rstrip(").,;\"'")
        links.append((short_domain(url), url))

    # De-dupe by URL, preserve order
    seen = set()
    deduped = []
    for label, url in links:
        if url not in seen:
            deduped.append((label, url))
            seen.add(url)
    return deduped


def short_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host.replace("www.", "") or "link"
    except Exception:
        return "link"


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

    # Turn bold tags into nothing (Discord formatting handled separately)
    t = re.sub(r"</?\s*b\s*>", "", t, flags=re.IGNORECASE)

    # Remove anchor tags but keep their visible text (we list links separately)
    t = re.sub(r'<a\s+[^>]*href="[^"]+"[^>]*>', "", t, flags=re.IGNORECASE)
    t = re.sub(r"</\s*a\s*>", "", t, flags=re.IGNORECASE)

    # Strip any remaining tags
    t = re.sub(r"<[^>]+>", "", t)

    # Clean whitespace
    lines = [ln.strip() for ln in t.splitlines()]
    # Drop empty runs
    cleaned = []
    for ln in lines:
        if ln == "" and (not cleaned or cleaned[-1] == ""):
            continue
        cleaned.append(ln)
    t = "\n".join(cleaned).strip()

    return t


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
    # If something goes wrong, fail loudly in Actions logs
    if resp.status_code >= 300:
        raise RuntimeError(f"Discord webhook failed {resp.status_code}: {resp.text[:500]}")


def main():
    ics_text = fetch_ics(ICS_URL)
    cal = Calendar.from_ical(ics_text)

    now = datetime.now(TZ)
    start = now
    end = now + timedelta(days=7)

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
        post_to_discord("📅 **Schedule (Next 7 Days)**\n\nNo events found in the next 7 days.")
        return

    # Build readable blocks grouped by day
    blocks = []
    current_day = None

    for e in events:
        day_label = e["time"].strftime("%a %b %d")
        if day_label != current_day:
            current_day = day_label
            blocks.append(f"__**{day_label}**__")

        time_str = e["time"].strftime("%H:%M")
        title_line = f"**{time_str} — {e['title']}**"
        blocks.append(title_line)

        if e["location"]:
            blocks.append(f"📍 {e['location']}")

        # Keep description compact: first ~3 lines max
        if e["desc"]:
            desc_lines = [ln for ln in e["desc"].splitlines() if ln.strip()]
            if desc_lines:
                compact = desc_lines[:3]
                if len(desc_lines) > 3:
                    compact.append("…")
                blocks.append("\n".join(compact))

        if e["links"]:
            # Show up to 4 links to keep messages tight
            link_lines = []
            for label, url in e["links"][:4]:
                # Discord markdown link: [label](url)
                # (Works well in most clients; if you ever see issues, switch to "label: url")
                link_lines.append(f"- [{label}]({url})")
            blocks.append("🔗 Links:\n" + "\n".join(link_lines))

        blocks.append("")  # spacer between events

    header = "📅 **Schedule (Next 7 Days)**"
    messages = split_into_messages(blocks, header)

    # Add part labels if multiple messages
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
