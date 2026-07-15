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
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from term_utils import guess_current_term, is_term_finished, sort_terms_chronologically

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


def clean_term_description(description):
    """Banner appends things like '(View Only)' to some term descriptions -- strip that for display."""
    if not description:
        return description
    return re.sub(r"\s*\(view only\)\s*$", "", description, flags=re.IGNORECASE).strip()


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
            terms.append({"code": code, "description": clean_term_description(desc)})
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

    max_enrollment = raw.get("maximumEnrollment")
    enrollment = raw.get("enrollment")
    seats_available = raw.get("seatsAvailable")
    seats_is_estimated = False

    # Guest (not-logged-in) Banner search sometimes omits seatsAvailable, or
    # returns it inconsistently, even when maximumEnrollment and enrollment
    # are present -- Macalester's own site notes that fully accurate
    # open-seat counts require signing in. Fall back to computing it
    # ourselves when we can, and flag it as estimated so the frontend can
    # be transparent about that instead of silently showing a wrong number.
    if seats_available is None and max_enrollment is not None and enrollment is not None:
        seats_available = max_enrollment - enrollment
        seats_is_estimated = True

    open_section = raw.get("openSection")
    if open_section is None and seats_available is not None:
        open_section = seats_available > 0

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
        "seats_available": seats_available,
        "seats_estimated": seats_is_estimated,
        "max_enrollment": max_enrollment,
        "enrollment": enrollment,
        "waitlist_available": raw.get("waitAvailable"),
        "waitlist_capacity": raw.get("waitCapacity"),
        "open_section": open_section,
        "faculty": faculty,
        "meetings": meetings,
    }


def fetch_live_enrollment(session, term_code, crn):
    """
    Best-effort fetch of ONE section's live seat numbers, via the per-CRN
    endpoint Banner 9's own search UI calls to refresh a row's enrollment
    info on demand (separate from the bulk searchResults page, which can
    return a snapshot that lags behind real registration activity for a
    term under active registration).

    Returns a dict on success, or None if this Banner instance doesn't
    expose the endpoint to guest sessions, returns something we can't
    parse, or the request fails -- callers must treat None as "leave the
    bulk-search value alone," never as zero seats.
    """
    try:
        r = session.get(
            f"{BASE}/searchResults/getEnrollmentInfo",
            params={"term": term_code, "courseReferenceNumber": crn},
            timeout=15,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def refresh_live_seats(session, term_code, sections, verbose=True, max_workers=8):
    """
    Best-effort: overwrite each section's seat numbers with a fresh per-CRN
    lookup instead of the bulk-search snapshot. Only called for terms that
    aren't finished yet (see is_term_finished) -- closed terms can't change
    anymore, so their bulk-search numbers are already final.

    Probes a handful of sections first; if none of them return anything
    usable, this Banner instance likely doesn't expose the per-CRN endpoint
    to guest sessions (or it's named/shaped differently here), so we bail
    out immediately rather than making hundreds of requests for nothing.
    """
    if not sections:
        return sections

    probe = sections[: min(5, len(sections))]
    probe_ok = sum(
        1
        for s in probe
        if (info := fetch_live_enrollment(session, term_code, s["crn"]))
        and ("seatsAvailable" in info or "maximumEnrollment" in info)
    )
    if probe_ok == 0:
        if verbose:
            print("    live seat refresh not available for this term (endpoint missing/unsupported) -- keeping bulk-search values")
        return sections

    def _refresh(sec):
        info = fetch_live_enrollment(session, term_code, sec["crn"])
        if not info:
            return False
        max_e = info.get("maximumEnrollment", sec.get("max_enrollment"))
        enr = info.get("enrollment", sec.get("enrollment"))
        seats = info.get("seatsAvailable")
        if seats is None and max_e is not None and enr is not None:
            seats = max_e - enr
        if seats is None:
            return False
        sec["max_enrollment"] = max_e
        sec["enrollment"] = enr
        sec["seats_available"] = seats
        sec["seats_estimated"] = False
        sec["open_section"] = seats > 0
        return True

    updated = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_refresh, s) for s in sections]
        for done, fut in enumerate(as_completed(futures), 1):
            if fut.result():
                updated += 1
            if verbose and done % 100 == 0:
                print(f"    live seats {done}/{len(sections)}", end="\r")

    if verbose:
        print(f"\n    live seat refresh: updated {updated}/{len(sections)} sections")
    return sections


def fetch_courses_for_term(session, term_code, description, verbose=True, live_seats=True):
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

    if live_seats and not is_term_finished(description):
        all_sections = refresh_live_seats(session, term_code, all_sections, verbose=verbose)

    return all_sections


def save_term(term_code, description, sections):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{term_code}.json")
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"code": term_code, "description": description, "scraped_at": scraped_at, "courses": sections},
            f,
            indent=1,
        )
    print(f"  saved {len(sections)} sections -> {path}")


def update_terms_index(all_known_terms, scraped_codes, section_counts=None):
    """
    scraped_codes: term codes we attempted to scrape this run.
    section_counts: {term_code: number_of_sections_found}, used to tell
        "actually has course data" apart from "Banner lists this term but
        it's an empty/not-yet-published shell" (common for placeholder
        Summer/January terms tagged "(View Only)").

    Rebuilds the terms list fresh from all_known_terms every run (using
    the previous terms.json only as a fallback for terms not touched this
    run) rather than merging into the old list -- otherwise a term code
    that Banner no longer reports (a stale/fake entry, e.g. from manually
    seeded sample data) would linger in terms.json forever, since nothing
    would ever remove it. Terms with no course data are dropped from the
    output entirely (not just disabled) -- the picker should only ever
    list semesters that actually have something to show.
    """
    section_counts = section_counts or {}
    path = os.path.join(DATA_DIR, "terms.json")
    prior = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            prior = {t["code"]: t for t in json.load(f).get("terms", [])}

    rebuilt = {}
    for t in all_known_terms:
        code = t["code"]
        if code in section_counts:
            has_data = section_counts[code] > 0
        else:
            # Not touched this run -- carry forward whatever we knew before.
            has_data = prior.get(code, {}).get("scraped", False)
        rebuilt[code] = {"code": code, "description": t["description"], "scraped": has_data}

    terms_with_data = [t for t in rebuilt.values() if t["scraped"]]
    merged = sort_terms_chronologically(terms_with_data)
    current = guess_current_term(merged)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "current_term_code": current["code"] if current else None,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "terms": merged,
            },
            f,
            indent=1,
        )

    # Clean up orphaned per-term files -- anything on disk whose code
    # didn't make the final cut (stale, pruned, or leftover sample data).
    final_codes = {t["code"] for t in merged}
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".json") or fname == "terms.json" or fname.startswith("_"):
            continue
        code = fname[:-5]
        if code not in final_codes:
            try:
                os.remove(os.path.join(DATA_DIR, fname))
                print(f"  removed stale/no-data term file {fname}")
            except OSError:
                pass
    print(f"  updated {path} (current/upcoming term: {current['description'] if current else 'unknown'})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-terms", action="store_true", help="List available terms and exit")
    ap.add_argument("--term", action="append", default=[], help="Scrape a specific term code (repeatable)")
    ap.add_argument("--current", action="store_true", help="Scrape only the current/upcoming term")
    ap.add_argument("--all", action="store_true", help="Scrape every term Banner returns")
    ap.add_argument("--max-terms", type=int, default=None, help="Cap how many terms --all scrapes (most recent first)")
    ap.add_argument("--debug", action="store_true", help="Dump raw first-page JSON for inspection")
    ap.add_argument(
        "--no-live-seats",
        action="store_true",
        help="Skip the per-CRN live seat refresh and only use the bulk search snapshot",
    )
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
        with open(os.path.join(DATA_DIR, f"_debug_{term_code}.json"), "w", encoding="utf-8") as f:
            f.write(r.text)
        print(f"Wrote raw debug response for term {term_code} to web/data/_debug_{term_code}.json")

        # Also probe the per-CRN live-enrollment endpoint on the first
        # section in that response, so you can confirm whether it exists
        # on this Banner instance and what it actually returns.
        try:
            first_crn = r.json().get("data", [{}])[0].get("courseReferenceNumber")
        except (ValueError, IndexError, AttributeError):
            first_crn = None
        if first_crn:
            info = fetch_live_enrollment(session, term_code, first_crn)
            debug_path = os.path.join(DATA_DIR, f"_debug_enrollment_{term_code}_{first_crn}.json")
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(info, f, indent=1)
            print(f"Wrote live-enrollment probe for CRN {first_crn} to web/data/_debug_enrollment_{term_code}_{first_crn}.json"
                  f" ({'got data' if info else 'endpoint returned nothing usable'})")
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
    section_counts = {}
    for t in targets:
        print(f"Scraping {t['description']} ({t['code']})...")
        sections = fetch_courses_for_term(session, t["code"], t["description"], live_seats=not args.no_live_seats)
        save_term(t["code"], t["description"], sections)
        scraped_codes.append(t["code"])
        section_counts[t["code"]] = len(sections)

    update_terms_index(known_terms, scraped_codes, section_counts)
    print("Done.")


if __name__ == "__main__":
    main()
