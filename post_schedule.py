import requests
from icalendar import Calendar
from datetime import datetime, timedelta
from dateutil.tz import tzutc
from dateutil.parser import parse
import os

ICS_URL = os.environ["ICS_URL"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK"]

def get_week_range():
    today = datetime.now(tzutc())
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=7)
    return start, end

def clean_description(desc):
    if not desc:
        return ""
    return desc.replace("\\n", "\n").replace("\\,", ",")

def main():
    ics_text = requests.get(ICS_URL).text
    cal = Calendar.from_ical(ics_text)

    week_start, week_end = get_week_range()
    events = []

    for component in cal.walk("VEVENT"):
        dtstart = component.get("DTSTART").dt
        if isinstance(dtstart, datetime):
            dtstart = dtstart.astimezone(tzutc())
        else:
            continue  # skip all-day items for weekly summary

        if week_start <= dtstart < week_end:
            events.append({
                "time": dtstart,
                "title": str(component.get("SUMMARY")),
                "location": str(component.get("LOCATION", "")),
                "description": clean_description(str(component.get("DESCRIPTION", "")))
            })

    events.sort(key=lambda e: e["time"])

    if not events:
        return

    lines = ["📅 **This Week’s Schedule**\n"]

    for e in events:
        time_str = e["time"].strftime("%a %H:%M")
        lines.append(f"**{time_str} — {e['title']}**")
        if e["location"]:
            lines.append(f"📍 {e['location']}")
        if e["description"]:
            lines.append(e["description"])
        lines.append("")

    payload = {
        "content": "\n".join(lines)
    }

    requests.post(WEBHOOK_URL, json=payload)

if __name__ == "__main__":
    main()
