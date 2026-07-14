#!/usr/bin/env python3
"""
registration_calendar.py -- Reads Macalester's public Academic Calendar to
figure out, for *today*, whether course data is likely changing quickly:

  - an early-registration window ("Fall 2026 Registration",
    "Spring 2027 Registration", etc.), or
  - the first couple weeks of a term (from "Classes Begin" through
    "Last Day to Add/Drop a Class")

Used by the GitHub Actions workflow to decide whether to run a full scrape
this hour or skip it, WITHOUT ever hardcoding a specific year's dates --
Macalester republishes this calendar (several years ahead, in practice)
every year, so this just reads it fresh each run.

Source page: https://www.macalester.edu/registrar/academic-calendars/
(linked from https://www.macalester.edu/registrar/schedules/, which lists
current/future/past terms but doesn't itself contain calendar dates.)

USAGE
-----
    python registration_calendar.py
        Prints "true" or "false" plus the reasons, and (when running
        inside GitHub Actions) writes run_full=true/false to $GITHUB_OUTPUT.

    python registration_calendar.py --debug
        Also saves the raw fetched HTML to web/data/_debug_academic_calendar.html
        and prints every window it found, for troubleshooting if Macalester
        changes the page's structure.

NOTE ON RELIABILITY
--------------------
This depends on the calendar page continuing to use HTML tables with
"Classes Begin", "Last Day to Add/Drop a Class", and "<Term> Registration"
text roughly as they appear today. If Macalester restructures the page,
parsing may silently find nothing -- in that case this fails safe (treats
today as "not in an active window", so the workflow falls back to its
once-daily baseline run rather than erroring out). Run with --debug to see
exactly what was parsed.
"""

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

CALENDAR_URL = "https://www.macalester.edu/registrar/academic-calendars/"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "data")

SECTION_RE = re.compile(r"\b(Fall|Spring)\s+(\d{4})\b")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(
    rf"({MONTHS})\s+(\d{{1,2}})(?:\s*[\u2013\u2010-]\s*(?:({MONTHS})\s+)?(\d{{1,2}}))?"
)

PAD_DAYS = 2          # a little slack either side of a window's exact edges
BASELINE_HOUR_UTC = 13  # guarantee at least one full run/day even outside any window


def _resolve_date(month_name, day, year):
    try:
        return datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y").date()
    except ValueError:
        return None


def _parse_date_cell(text, year):
    """'September 8' -> (date, date). 'April 20 - May 1' -> (start, end)."""
    m = DATE_RE.search(text)
    if not m:
        return None, None
    start_month, start_day, end_month, end_day = m.groups()
    start = _resolve_date(start_month, start_day, year)
    if not start:
        return None, None
    if end_day is None:
        return start, start
    end = _resolve_date(end_month or start_month, end_day, year)
    if end and end < start:  # range spans a Dec 31 -> Jan 1 boundary
        end = _resolve_date(end_month or start_month, end_day, year + 1)
    return start, end


def fetch_windows(debug=False):
    """Returns (registration_windows, add_drop_windows), each a list of (label, start_date, end_date)."""
    r = requests.get(
        CALENDAR_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (compatible; MacScheduleTool/1.0; personal course-search project)"},
    )
    r.raise_for_status()

    if debug:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "_debug_academic_calendar.html"), "w", encoding="utf-8") as f:
            f.write(r.text)

    soup = BeautifulSoup(r.text, "html.parser")

    registration_windows = []
    add_drop_windows = []
    add_drop_open = None  # [label, start_date] while waiting for the matching "last day to add/drop" row

    current_label = None
    current_year = None

    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "tr"]):
        if el.name != "tr":
            m = SECTION_RE.search(el.get_text(" ", strip=True))
            if m:
                current_label = f"{m.group(1)} {m.group(2)}"
                current_year = int(m.group(2))
            continue

        if current_year is None:
            continue

        cells = [c.get_text(" ", strip=True) for c in el.find_all(["td", "th"])]
        if len(cells) < 2:
            continue

        date_cell = cells[0]
        desc_cell = " ".join(cells[1:])
        start, end = _parse_date_cell(date_cell, current_year)
        if not start:
            continue

        reg_m = re.search(r"(Fall|Spring)\s+(\d{4})\s+Registration", desc_cell)
        if reg_m:
            registration_windows.append((f"{reg_m.group(1)} {reg_m.group(2)} registration", start, end or start))
            continue

        desc_lower = desc_cell.lower()
        if "classes begin" in desc_lower:
            add_drop_open = [current_label, start]
            continue

        if "last day to add/drop" in desc_lower and add_drop_open and add_drop_open[0] == current_label:
            add_drop_windows.append((f"{add_drop_open[0]} add/drop period", add_drop_open[1], end or start))
            add_drop_open = None

    return registration_windows, add_drop_windows


def active_reasons(today=None, debug=False):
    today = today or date.today()
    reasons = []
    try:
        registration_windows, add_drop_windows = fetch_windows(debug=debug)
    except Exception as e:  # network hiccup, page restructured, etc. -- fail safe
        print(f"warning: couldn't read academic calendar ({e}); assuming not in an active window", file=sys.stderr)
        return reasons

    pad = timedelta(days=PAD_DAYS)
    for label, start, end in registration_windows + add_drop_windows:
        if start - pad <= today <= end + pad:
            reasons.append(f"{label} ({start} to {end})")

    if debug:
        print(f"Parsed {len(registration_windows)} registration window(s), {len(add_drop_windows)} add/drop window(s):")
        for label, start, end in sorted(registration_windows + add_drop_windows, key=lambda x: x[1]):
            marker = "  <-- today" if start - pad <= today <= end + pad else ""
            print(f"  {label}: {start} to {end}{marker}")

    return reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--debug", action="store_true", help="Save raw HTML + print every parsed window")
    args = ap.parse_args()

    reasons = active_reasons(debug=args.debug)
    in_window = bool(reasons)
    for r in reasons:
        print(f"active: {r}")

    is_baseline = datetime.utcnow().hour == BASELINE_HOUR_UTC
    run_full = in_window or is_baseline

    print(f"in_window={in_window} baseline_hour={is_baseline} -> run_full={run_full}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"run_full={'true' if run_full else 'false'}\n")
            f.write(f"in_window={'true' if in_window else 'false'}\n")


if __name__ == "__main__":
    main()
