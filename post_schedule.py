import os
import requests
from icalendar import Calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ICS_URL = os.environ["ICS_URL"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK"]
TZ = ZoneInfo("America/New_York")

def clean_description(desc: str) -> str:
    if not desc:
        return ""
    return desc.replace("\\n", "\n").replace("\\,", ",")

def to_datetime(dt):
    # dt can be date or datetime depending on event type
    if hasattr(dt, "hour"):  # datetime
        return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
    return None  # skip all-day events in this summary

def main():
    print(f"Fetching ICS: {ICS_URL}")
    r = requests.get(ICS_URL, timeout=30)
    print(f"ICS fetch status: {r.status_code}, bytes: {len(r.text)}")
    r.raise_for_status()

    cal = Calendar.from_ical(r.text)

    now = datetime.now(TZ)
    start = now
    end = now + timedelta(days=7)

    print(f"Filtering events from {start.isoformat()} to {end.isoformat()}")

    events = []
    for component in cal.walk("VEVENT"):
        dtstart_raw = component.get("DTSTART")
        if not dtstart_raw:
            continue

        dtstart = to_datetime(dtstart_raw.dt)
        if dtstart is None:
            continue  # skip all-day

        if start <= dtstart < end:
            events.append({
                "time": dtstart,
                "title": str(component.get("SUMMARY", "(No title)")),
                "location": str(component.get("LOCATION", "")),
                "description": clean_description(str(component.get("DESCRIPTION", ""))),
            })

    events.sort(key=lambda e: e["time"])
    print(f"Found {len(events)} events in range.")

    if not events:
        payload = {"content": "📅 **Schedule update:** No events found in the next 7 days from the calendar feed."}
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=30)
        print(f"Discord post status: {resp.status_code}")
        print(resp.text[:500])
        resp.raise_for_status()
        return

    lines = ["📅 **Schedule (Next 7 Days)**", ""]
    for e in events:
        time_str = e["time"].strftime("%a %b %d %H:%M")
        lines.append(f"**{time_str} — {e['title']}**")
        if e["location"]:
            lines.append(f"📍 {e['location']}")
        if e["description"]:
            lines.append(e["description"])
        lines.append("")

    content = "\n".join(lines)

    # Discord content limit is 2000 characters; keep it safe
    if len(content) > 1900:
        content = content[:1900] + "\n\n…(truncated)"

    payload = {"content": content}
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    print(f"Discord post status: {resp.status_code}")
    print(resp.text[:500])
    resp.raise_for_status()

if __name__ == "__main__":
    main()
