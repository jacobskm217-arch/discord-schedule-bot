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


def short_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host.replace("www.", "") or "link"
    except Exception:
        return "link"


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip(").,;\"'")


def is_blocked_url(url: str) -> bool:
    try:
        p = urlparse(url)
        scheme = (p.scheme or "").lower()
        host = (p.netloc or "").lower().replace("www.", "")
        return scheme in BLOCKED_SCHEMES or host in BLOCKED_LINK_DOMAINS
    except Exception:
        return False


def extract_links(text: str) -> list[tuple[str, str]]:
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
        label = " ".join(label.split()).strip()
        if not label:
            label = short_domain(url)
        links.append((label, url))

    t_no_anchors = anchor_pat.sub(" ", t)

    url_pat = re.compile(r"(https?://[^\s<>\"]+)")
    for url in url_pat.findall(t_no_anchors):
        url = normalize_url(url)
        if not url or is_blocked_url(url):
            continue
        links.append((short_domain(url), url))

    seen = set()
    deduped: list[tuple[str, str]] = []
    for label, url in links:
        if url not in seen:
            deduped.append((label, url))
            seen.add(url)
    return deduped


def html_to_text(desc: str) -> str:
    if not desc:
        return ""
    t = desc.replace("\\n", "\n").replace("\\,", ",")
    t = html.unescape(t)
    t = re.sub(r"<\s*br\s*/?\s*>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"</?\s*b\s*>", "", t, flags=re.IGNORECASE)
    t = re.sub(r'<a\s+[^>]*href="[^"]+"[^>]*>', "", t, flags=re.IGNORECASE)
    t = re.sub(r"</\s*a\s*>", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)

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
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_monday = (now + timedelta(days=days_until_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_saturday = next_monday + timedelta(days=5)
    return next_monday, next_saturday


def classify_links(links: list[tuple[str, str]]) -> dict:
    wsib = None
    canvas = None
    meet = None
    other = []

    for label, url in links:
        host = urlparse(url).netloc.lower().replace("www.", "")
        label_norm = (label or "").strip()

        if host.endswith("canvas.usuhs.edu"):
            if canvas is None:
                canvas = (label_norm or "Canvas", url)
            else:
                other.append((label_norm or short_domain(url), url))
            continue

        if host.endswith("meet.google.com"):
            if meet is None:
                meet = (label_norm or "Meet", url)
            else:
                other.append((label_norm or short_domain(url), url))
            continue

        if ("WSIB" in label_norm.upper()) and host.endswith("docs.google.com"):
            if wsib is None:
                wsib = (label_norm or "WSIB", url)
            else:
                other.append((label_norm or short_domain(url), url))
            continue

        other.append((label_norm or short_domain(url), url))

    return {"wsib": wsib, "canvas": canvas, "meet": meet, "other": other}


def extract_wsib_title_line(desc_text: str) -> str | None:
    if not desc_text:
        return None
    for ln in desc_text.splitlines():
        ln = ln.strip()
        m = re.match(r"^WSIB\s*\|\s*(.+)$", ln, flags=re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            return val if val else None
    return None


def compact_url_free_description(desc_text: str, max_lines: int = 2) -> str | None:
    if not desc_text:
        return None
    desc_lines = []
    for ln in desc_text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if "http://" in ln or "https://" in ln:
            continue
        # Remove the WSIB label line; we render WSIB separately when possible
        if re.match(r"^WSIB\s*\|", ln, flags=re.IGNORECASE):
            continue
        desc_lines.append(ln)

    if not desc_lines:
        return None

    compact = desc_lines[:max_lines]
    if len(desc_lines) > max_lines:
        compact.append("…")
    return "\n".join(compact)


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
            continue

        if start <= dtstart < end:
            title = str(component.get("SUMMARY", "(No title)")).strip()
            location = str(component.get("LOCATION", "")).strip()
            raw_desc = str(component.get("DESCRIPTION", "") or "")

            links = extract_links(raw_desc)
            buckets = classify_links(links)

            desc_text = html_to_text(raw_desc)
            wsib_title = extract_wsib_title_line(desc_text)

            events.append({
                "time": dtstart,
                "title": title,
                "location": location,
                "desc_text": desc_text,
                "wsib_title": wsib_title,
                "links": buckets,
            })

    events.sort(key=lambda e: e["time"])

    if not events:
        new_id = post_to_discord_return_id(header + "\n\nNo events found for next Monday–Friday.")
        save_message_ids([new_id])
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

        snippet = compact_url_free_description(e["desc_text"], max_lines=2)
        if snippet:
            blocks.append(snippet)

        wsib = e["links"]["wsib"]
        canvas = e["links"]["canvas"]
        meet = e["links"]["meet"]
        other = e["links"]["other"]

        # Primary labeled links
        primary_lines = []

        if wsib is not None:
            primary_lines.append(f"**WSIB:** {wsib[1]}")

        if canvas is not None:
            primary_lines.append(f"**Canvas:** {canvas[1]}")

        if meet is not None:
            primary_lines.append(f"**Meet:** {meet[1]}")

        if primary_lines:
            blocks.append("\n".join(primary_lines))

        # Other links (excluding any URL already used above)
        primary_urls = set()
        for item in (wsib, canvas, meet):
            if item is not None:
                primary_urls.add(item[1])

        other_lines = []
        for label, url in other:
            if url in primary_urls:
                continue
            other_lines.append(f"- {label}: {url}" if label else f"- {url}")

        if other_lines:
            blocks.append("**Other Links:**\n" + "\n".join(other_lines))

        blocks.append("")

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
