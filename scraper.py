#!/usr/bin/env python3
"""
scraper.py -- Scrapes Macalester's public course schedule search (Ellucian
Banner 9 Self-Service Class Search) and saves it as JSON that the Flask app
in this project can serve.

Macalester's "Browse Classes" search (linked from
https://www.macalester.edu/registrar/schedules/) is a standard Banner 9 SSB
instance at:

    https://oci-macxe.macalester.edu/StudentRegistrationSsb/ssb/

This is the SAME public, no-login search tool students use to browse
classes -- this script just automates clicking through it. It does not
touch anything behind Macalester's login wall (grades, registration
actions, personal records, etc).

USAGE
-----
    python scraper.py --list-terms
        List every term Banner knows about (code + human description).

    python scraper.py --current
        Scrape only the current/upcoming term (this is what the web app
        shows by default).

    python scraper.py --term 202630 --term 202610
        Scrape one or more specific term codes (get codes from --list-terms).

    python scraper.py --all
        Scrape every term Banner returns (can be slow -- Banner often keeps
        10-15+ years of terms; consider --max-terms to cap it).

    python scraper.py --all --max-terms 12
        Scrape only the 12 most recent terms.

Each run writes/refreshes:
    data/<term_code>.json   -- full course list for that term
    data/terms.json         -- index of all known terms + which one is "current"

NOTES ON THE BANNER API
------------------------
Banner 9 SSB's field names are consistent across the ~1000s of schools that
run it, but a handful of institutions customize field labels or add/remove
optional filters. If Macalester's instance differs from what's coded here,
run with --debug on a single term and inspect the raw JSON saved to
data/_debug_<term>.json to see the actual shape and adjust `parse_section()`
accordingly.
"""

import argparse
import json
import os
import sys
import time

import requests

from term_utils import guess_current_term, sort_terms_chronologically

BASE = "https://oci-macxe.macalester.edu/StudentRegistrationSsb/ssb"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "data")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MacScheduleTool/1.0; personal course-search project)",
    "Accept": "application/json, text/plain, */*",
}

PAGE_SIZE = 500  # Banner will accept large page sizes for guest search


def new_session():
    """Start a session and hit the term-selection page once to establish cookies."""
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(f"{BASE}/term/termSelection?mode=search", timeout=30)
    r.raise_for_status()
    return s


def list_terms(session, search_term="", offset=1, max_results=100):
    """GET the list of terms Banner knows about: [{code, description}, ...]"""
    r = session.get(
        f"{BASE}/classSearch/getTerms",
        params={"searchTerm": search_term, "offset": offset, "max": max_results},
        timeout=30,
    )
    r.raise_for_status()
    raw = r.json()
    terms = []
    for item in raw:
        code = item.get("code")
        desc = item.get("description")
        if code and desc:
            terms.append({"code": code, "description": desc})
    return terms


def select_term(session, term_code):
    """Tell Banner which term the session's subsequent searches apply to."""
    r = session.post(
        f"{BASE}/term/search?mode=search",
        data={"term": term_code},
        timeout=30,
    )
    r.raise_for_status()
    # Clear any filters left over from a previous term in this session.
    session.post(f"{BASE}/classSearch/resetDataForm", timeout=30)


def fetch_subjects(session, term_code):
    """GET the list of subjects (departments) offered in a term, e.g. {'code': 'ART', 'description': 'Art and Art History'}."""
    r = session.get(
        f"{BASE}/classSearch/get_subject",
        params={"term": term_code, "offset": 1, "max": 999},
        timeout=30,
    )
    if r.status_code != 200:
        return []
    try:
        raw = r.json()
    except ValueError:
        return []
    return [{"code": s.get("code"), "description": s.get("description")} for s in raw]


def parse_section(raw):
    """Normalize one raw Banner searchResults record into our simpler schema."""
    meetings = []
    for mf in raw.get("meetingsFaculty", []) or []:
        mt = mf.get("meetingTime", {}) or {}
        days = "".join(
            code
            for code, key in [
                ("M", "monday"),
                ("T", "tuesday"),
                ("W", "wednesday"),
                ("R", "thursday"),
                ("F", "friday"),
                ("S", "saturday"),
                ("U", "sunday"),
            ]
            if mt.get(key)
        )
        meetings.append(
            {
                "days": days,
                "start_time": mt.get("beginTime"),  # "HHMM" 24hr string, e.g. "1330"
                "end_time": mt.get("endTime"),
                "building": mt.get("building"),
                "building_description": mt.get("buildingDescription"),
                "room": mt.get("room"),
                "campus": mt.get("campusDescription"),
                "meeting_type": mt.get("meetingTypeDescription"),
            }
        )

    faculty = [
        {
            "name": f.get("displayName"),
            "email": f.get("emailAddress"),
            "primary": bool(f.get("primaryIndicator")),
        }
        for f in raw.get("faculty", []) or []
    ]

    return {
        "crn": raw.get("courseReferenceNumber"),
        "term": raw.get("term"),
        "term_description": raw.get("termDesc"),
        "subject": raw.get("subject"),
        "subject_description": raw.get("subjectDescription"),
        "course_number": raw.get("courseNumber"),
        "section": raw.get("sequenceNumber"),
        "title": raw.get("courseTitle"),
        "credit_hours": raw.get("creditHours") if raw.get("creditHours") is not None else raw.get("creditHourLow"),
        "schedule_type": raw.get("scheduleTypeDescription"),
        "campus": raw.get("campusDescription"),
        "instructional_method": raw.get("instructionalMethodDescription"),
        "part_of_term": raw.get("partOfTermDesc"),
        "seats_available": raw.get("seatsAvailable"),
        "max_enrollment": raw.get("maximumEnrollment"),
        "enrollment": raw.get("enrollment"),
        "waitlist_available": raw.get("waitAvailable"),
        "waitlist_capacity": raw.get("waitCapacity"),
        "open_section": raw.get("openSection"),
        "faculty": faculty,
        "meetings": meetings,
    }


def fetch_courses_for_term(session, term_code, verbose=True):
    """Page through searchResults for a term and return a list of parsed sections."""
    select_term(session, term_code)

    all_sections = []
    offset = 0
    total_count = None

    while True:
        r = session.post(
            f"{BASE}/searchResults/searchResults",
            data={
                "txt_term": term_code,
                "pageOffset": offset,
                "pageMaxSize": PAGE_SIZE,
                "sortColumn": "subjectDescription",
                "sortDirection": "asc",
            },
            timeout=60,
        )
        r.raise_for_status()
        payload = r.json()

        if total_count is None:
            total_count = payload.get("totalCount", 0)
            if verbose:
                print(f"  term {term_code}: {total_count} sections total")

        batch = payload.get("data") or []
        if not batch:
            break

        all_sections.extend(parse_section(s) for s in batch)
        offset += len(batch)

        if verbose:
            print(f"    fetched {offset}/{total_count}", end="\r")

        if total_count and offset >= total_count:
            break
        time.sleep(0.2)  # be polite

    if verbose:
        print()
    return all_sections


def save_term(term_code, description, sections):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{term_code}.json")
    with open(path, "w") as f:
        json.dump(
            {"code": term_code, "description": description, "courses": sections},
            f,
            indent=1,
        )
    print(f"  saved {len(sections)} sections -> {path}")


def update_terms_index(all_known_terms, scraped_codes):
    path = os.path.join(DATA_DIR, "terms.json")
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            existing = {t["code"]: t for t in json.load(f).get("terms", [])}

    for t in all_known_terms:
        existing[t["code"]] = {
            "code": t["code"],
            "description": t["description"],
            "scraped": t["code"] in scraped_codes or existing.get(t["code"], {}).get("scraped", False),
        }

    merged = sort_terms_chronologically(list(existing.values()))
    current = guess_current_term(merged)

    with open(path, "w") as f:
        json.dump(
            {
                "current_term_code": current["code"] if current else None,
                "terms": merged,
            },
            f,
            indent=1,
        )
    print(f"  updated {path} (current/upcoming term: {current['description'] if current else 'unknown'})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-terms", action="store_true", help="List available terms and exit")
    ap.add_argument("--term", action="append", default=[], help="Scrape a specific term code (repeatable)")
    ap.add_argument("--current", action="store_true", help="Scrape only the current/upcoming term")
    ap.add_argument("--all", action="store_true", help="Scrape every term Banner returns")
    ap.add_argument("--max-terms", type=int, default=None, help="Cap how many terms --all scrapes (most recent first)")
    ap.add_argument("--debug", action="store_true", help="Dump raw first-page JSON for inspection")
    args = ap.parse_args()

    session = new_session()

    print("Fetching term list from Banner...")
    known_terms = list_terms(session)
    if not known_terms:
        print("Banner returned no terms -- the API shape may have changed. Try --debug.", file=sys.stderr)
        sys.exit(1)
    known_terms = sort_terms_chronologically(known_terms)

    if args.list_terms:
        for t in known_terms:
            print(f"{t['code']}\t{t['description']}")
        return

    if args.debug:
        os.makedirs(DATA_DIR, exist_ok=True)
        term_code = args.term[0] if args.term else known_terms[0]["code"]
        select_term(session, term_code)
        r = session.post(
            f"{BASE}/searchResults/searchResults",
            data={"txt_term": term_code, "pageOffset": 0, "pageMaxSize": 5},
            timeout=30,
        )
        with open(os.path.join(DATA_DIR, f"_debug_{term_code}.json"), "w") as f:
            f.write(r.text)
        print(f"Wrote raw debug response for term {term_code} to web/data/_debug_{term_code}.json")
        return

    targets = []
    if args.current:
        current = guess_current_term(known_terms)
        if current:
            targets.append(current)
    if args.term:
        by_code = {t["code"]: t for t in known_terms}
        for code in args.term:
            if code in by_code:
                targets.append(by_code[code])
            else:
                print(f"Warning: term code {code} not found in Banner's term list, trying it anyway.")
                targets.append({"code": code, "description": code})
    if args.all:
        targets = known_terms[: args.max_terms] if args.max_terms else known_terms

    if not targets:
        ap.print_help()
        return

    scraped_codes = []
    for t in targets:
        print(f"Scraping {t['description']} ({t['code']})...")
        sections = fetch_courses_for_term(session, t["code"])
        save_term(t["code"], t["description"], sections)
        scraped_codes.append(t["code"])

    update_terms_index(known_terms, scraped_codes)
    print("Done.")


if __name__ == "__main__":
    main()
