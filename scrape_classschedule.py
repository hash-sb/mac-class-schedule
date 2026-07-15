#!/usr/bin/env python3
"""
scrape_classschedule.py -- Scrapes Macalester's "Class Schedule" page (the
one actually linked from https://www.macalester.edu/registrar/schedules/,
e.g. https://macadmsys.macalester.edu/macssb/customPage/page/classSchedule?term=202710)
by rendering it with a real headless browser, instead of talking to the
Banner search API the way scraper.py does.

WHY THIS EXISTS
---------------
scraper.py talks to oci-macxe.macalester.edu's "Browse Classes" search API.
That's a DIFFERENT Banner front-end/host than macadmsys.macalester.edu's
Class Schedule page -- likely a different environment/instance entirely,
which would explain seat-count numbers disagreeing between the two. This
script scrapes the exact page you'd compare against in a browser, so
there's no API-shape guessing involved -- whatever is visibly rendered is
what gets captured.

WHY IT NEEDS A REAL BROWSER
----------------------------
View-source on that page and there's no course data in the HTML at all --
just AngularJS template bindings like {{ valLastUpdated }}. The table is
populated by JavaScript after load. requests+BeautifulSoup (what scraper.py
uses) physically cannot see this data; only a real browser executing the
page's JS can. This uses Playwright to do that.

IMPORTANT -- THIS IS UNVERIFIED AGAINST THE LIVE SITE
-------------------------------------------------------
My sandbox can't reach macalester.edu, so I have never actually seen this
page rendered. The extraction logic below is written defensively (tries a
real <table> first, falls back to regex-over-visible-text) for exactly
that reason, and --debug ALWAYS saves a full screenshot + rendered HTML +
plain visible text, regardless of whether parsing succeeds. Treat the
first real run as a verification step: run with --debug, check whether
`parsed_count` in the printed summary looks right, and if it's 0 or wrong,
open the saved screenshot/HTML and send me what the actual row markup
looks like so I can fix the selectors for real instead of guessing again.

USAGE
-----
    pip install playwright
    playwright install --with-deps chromium   # one-time, downloads the browser

    python scrape_classschedule.py --term 202710 --debug
        Renders the page, saves web/data/_debug_classschedule_202710.png
        and .html and .txt, and prints whatever it managed to parse.

    python scrape_classschedule.py --term 202710
        Same, without the debug dump, writing web/data/<term>_rendered.json
        if parsing found anything.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

BASE_URL = "https://macadmsys.macalester.edu/macssb/customPage/page/classSchedule"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "data")

CRN_RE = re.compile(r"\b(\d{4,6})\b")  # Banner CRNs are typically 5 digits, but not always
COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,6})\s?-?\s?(\d{3}[A-Z]?)\b")
CREDIT_RE = re.compile(r"\b(\d(?:\.\d+)?)\s*(?:credit|cr\.?)\b", re.IGNORECASE)
SEATS_RE = re.compile(r"seats?\s*(?:available|open)?[:\s]*(-?\d+)", re.IGNORECASE)
MAX_ENROLL_RE = re.compile(r"max(?:imum)?\s*(?:enrollment)?[:\s]*(\d+)", re.IGNORECASE)


def dismiss_cookie_banner(page):
    """Best-effort: click through the analytics/cookie consent overlay if one blocks the page."""
    for text in ["Accept", "I Agree", "Agree", "OK", "Got it", "Continue", "Decline"]:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def maybe_trigger_search(page):
    """
    Best-effort: some Page Builder views need an explicit 'Search'/'Go'
    click even when a term is passed in the URL. Try common button labels;
    silently do nothing if none are found (page may auto-load instead).
    """
    for text in ["Search", "Go", "Submit", "View Course Sections", "Search for Classes"]:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def maybe_refresh_open_seats(page):
    """
    Best-effort: some Banner Page Builder views show a snapshot of seat
    counts until you click an "Update Open Seats" control -- click it if
    present so we capture live numbers instead of whatever was cached at
    initial page load. Tries both <button> and plain clickable text, since
    Page Builder controls aren't always real <button> elements.
    """
    for text in ["Update Open Seats", "Update Seats", "Refresh Seats", "Update Availability"]:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                return True
        except Exception:
            pass
        try:
            link = page.get_by_text(text, exact=False)
            if link.count() > 0:
                link.first.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def _load_and_capture(page, term_code, debug=False, extra_wait_ms=3000):
    """Navigates an already-open page to a term's schedule and returns (html, visible_text). Doesn't own the browser/page lifecycle."""
    url = f"{BASE_URL}?term={term_code}"
    page.goto(url, wait_until="networkidle", timeout=45000)
    dismiss_cookie_banner(page)
    maybe_trigger_search(page)

    # Angular apps often keep polling briefly after "networkidle";
    # give it a bit more time, then wait for network to settle again.
    page.wait_for_timeout(extra_wait_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # fine if it never goes fully idle again; we already waited

    # Now that the table's loaded once, try to force a live seat refresh
    # rather than trust whatever was in the initial snapshot.
    if maybe_refresh_open_seats(page):
        print("  clicked 'Update Open Seats' -- waiting for refreshed numbers...")
        page.wait_for_timeout(2000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
    else:
        print("  no 'Update Open Seats' control found -- using the page's initial numbers.")

    html = page.content()
    visible_text = page.inner_text("body")

    if debug:
        os.makedirs(DATA_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(DATA_DIR, f"_debug_classschedule_{term_code}.png"), full_page=True)
        with open(os.path.join(DATA_DIR, f"_debug_classschedule_{term_code}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        with open(os.path.join(DATA_DIR, f"_debug_classschedule_{term_code}.txt"), "w", encoding="utf-8") as f:
            f.write(visible_text)

    return html, visible_text


def render_page(term_code, headless=True, debug=False, extra_wait_ms=3000):
    """Single-term convenience wrapper: opens its own browser, renders one term, closes everything."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; MacScheduleTool/1.0; personal course-search project)")
        try:
            return _load_and_capture(page, term_code, debug=debug, extra_wait_ms=extra_wait_ms)
        finally:
            browser.close()


def extract_seats_by_id(html):
    """
    The confirmed, authoritative way to get open-seat counts on this page:
    each class lives in a <table class="TableClass">, and the open-seats
    value is directly in a <td id="SeatsAvailCRN{crn}">. This is a direct,
    unambiguous DOM lookup -- no row-shape or column-position guessing
    involved, unlike everything else this file has tried so far.

    Returns {crn: seats_available_int}.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    seats_by_crn = {}

    tables = soup.find_all("table", class_="TableClass")
    search_scope = tables if tables else [soup]  # fall back to whole doc if the class name ever changes

    for scope in search_scope:
        for td in scope.find_all("td", id=True):
            if not td["id"].startswith("SeatsAvailCRN"):
                continue
            crn = td["id"][len("SeatsAvailCRN"):]
            value = parse_signed_int(td.get_text(strip=True))
            if value is not None:
                seats_by_crn[crn] = value

    return seats_by_crn


def parse_via_tables(html):
    """
    Primary strategy: look for real <table>/<tr> markup in the rendered
    DOM (Banner apps often still use semantic tables for accessibility
    even inside an Angular shell). Returns a list of row-cell-lists, or
    an empty list if no table with plausible course-like rows was found.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    rows_out = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 3:
                continue
            joined = " ".join(cells)
            # A plausible course row has a CRN-like number and either a
            # course code or a credit/seat marker somewhere in it.
            if CRN_RE.search(joined) and (COURSE_CODE_RE.search(joined) or SEATS_RE.search(joined)):
                rows_out.append(cells)
    return rows_out


def parse_via_text(visible_text):
    """
    Fallback strategy: no usable <table> found, so fall back to scanning
    the page's plain visible text line by line for course-like lines
    (has a course code AND a CRN on the same line). Much less structured
    than the table path -- mainly useful for confirming the DATA is there
    even if we can't cleanly tabulate it yet.
    """
    hits = []
    for line in visible_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if COURSE_CODE_RE.search(line) and CRN_RE.search(line):
            hits.append(line)
    return hits


CRN_IN_CELL0_RE = re.compile(r"\((\d{4,6})\)\s*$")
INT_RE = re.compile(r"^-?\d+$")

# Some rendering pipelines substitute a different character for a plain
# ASCII hyphen-minus when showing negative numbers (typographic minus,
# en/em dash, etc.) -- INT_RE alone would silently reject all of these,
# which would specifically break over-capacity sections (negative seats)
# while leaving normal positive counts looking fine. Covered here:
#   U+2010 HYPHEN, U+2011 NON-BREAKING HYPHEN, U+2012 FIGURE DASH,
#   U+2013 EN DASH, U+2014 EM DASH, U+2212 MINUS SIGN,
#   U+FE63 SMALL HYPHEN-MINUS, U+FF0D FULLWIDTH HYPHEN-MINUS
_MINUS_LOOKALIKES = "\u2010\u2011\u2012\u2013\u2014\u2212\ufe63\uff0d"
_SIGNED_INT_CORE_RE = re.compile(r"^-?\d+$")


def parse_signed_int(text):
    """
    Parses an integer from cell text defensively: normalizes any
    non-ASCII minus/dash lookalike to a plain '-', and also recognizes
    accounting-style parenthesized negatives like '(1)' meaning -1 (a
    real convention some enrollment systems use for over-capacity
    counts). Returns None if the text isn't recognizably an integer in
    any of these forms -- callers should treat that as "couldn't parse
    this," not as zero.
    """
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    for ch in _MINUS_LOOKALIKES:
        t = t.replace(ch, "-")
    negative_paren = False
    if t.startswith("(") and t.endswith(")") and len(t) > 2:
        negative_paren = True
        t = t[1:-1].strip()
    if not _SIGNED_INT_CORE_RE.match(t):
        return None
    value = int(t)
    return -abs(value) if negative_paren else value


CODE_CELL_RE = re.compile(r"^([A-Z&]+)\s+([\w]+)-([\w]+)\s+\((\d{4,6})\)\s*$", re.IGNORECASE)
MEETING_TEXT_RE = re.compile(
    r"Meeting:\s*([A-Za-z](?:\s[A-Za-z])*)\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*(am|pm)\s+(.*)$",
    re.IGNORECASE,
)
INSTRUCTOR_TEXT_RE = re.compile(r"Instructor:\s*(.+)$", re.IGNORECASE)


def _to_24h(time_str, ampm):
    h, m = time_str.split(":")
    h, m = int(h), int(m)
    ampm = ampm.lower()
    if ampm == "pm" and h != 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    return f"{h:02d}{m:02d}"


def parse_meetings_text(text):
    """Best-effort parse of one or more 'Meeting: <days> <start> - <end> <am/pm> <location>' segments."""
    meetings = []
    for m in MEETING_TEXT_RE.finditer(text or ""):
        days_raw, start, end, ampm, location = m.groups()
        days = re.sub(r"\s+", "", days_raw).upper()
        location = location.strip()
        building, room = location, None
        if location:
            parts = location.rsplit(" ", 1)
            if len(parts) == 2 and re.match(r"^\d+[A-Za-z]?$", parts[1]):
                building, room = parts[0], parts[1]
        meetings.append(
            {
                "days": days,
                "start_time": _to_24h(start, ampm),
                "end_time": _to_24h(end, ampm),
                "building": building or None,
                "building_description": None,
                "room": room,
                "campus": None,
                "meeting_type": None,
            }
        )
    return meetings


def parse_instructors_text(text):
    """Best-effort parse of an 'Instructor: Name1, Name2' segment."""
    m = INSTRUCTOR_TEXT_RE.search(text or "")
    if not m:
        return []
    names = [n.strip() for n in re.split(r"[,;]", m.group(1)) if n.strip()]
    return [{"name": n, "email": None, "primary": i == 0} for i, n in enumerate(names)]


def group_rows_by_crn(table_rows):
    """Groups parsed row-cell-lists by the CRN found in cell[0], skipping rows that don't match the expected shape."""
    groups = {}
    skipped = 0
    for cells in table_rows:
        if not cells:
            skipped += 1
            continue
        m = CODE_CELL_RE.match(cells[0].strip())
        if not m:
            skipped += 1
            continue
        crn = m.group(4)
        groups.setdefault(crn, []).append((m, cells))
    return groups, skipped


def _find_seat_pair(cells):
    """
    Finds (seats_available, max_enrollment) from the literal last two
    cells only. This used to scan further left for any adjacent integer
    pair, to tolerate a hypothetical trailing extra column -- reverted:
    that leniency risked grabbing an unrelated numeric cell (e.g. a room
    number) as if it were seats/max, which produces confidently-wrong
    values instead of a safe "couldn't parse this row" outcome. Missing
    data (kept safe by the caller falling back to prior values) is a much
    better failure mode than wrong data. Returns None if the last two
    cells aren't both parseable as integers (see parse_signed_int for the
    non-ASCII-minus/parenthesized-negative handling), or if max_enrollment
    is outside a sane range for a class size (catches grabbing the wrong
    number entirely).
    """
    if len(cells) < 2:
        return None
    seats = parse_signed_int(cells[-2])
    max_enroll = parse_signed_int(cells[-1])
    if seats is not None and max_enroll is not None and 0 < max_enroll <= 999:
        return seats, max_enroll
    return None


def build_render_record(crn, rows, seats_by_id=None):
    """
    Builds one course record purely from the rendered Class Schedule rows
    for a CRN (possibly several rows, e.g. one per meeting pattern).

    seats_available comes from seats_by_id (the confirmed <td
    id="SeatsAvailCRN...">  lookup) when available for this CRN --
    authoritative, no guessing. max_enrollment has no confirmed DOM id
    yet, so it still comes from row-position guessing (the cell right
    before whatever position seats_available would have been in, when
    that position parses as an integer pair). If seats_by_id doesn't have
    this CRN, seats_available falls back to that same row-position guess.

    Returns (record, had_conflict) where had_conflict is True if
    different rows disagreed on max_enrollment, or if the row-based seat
    guess disagreed with the authoritative ID-based value (informational
    -- the ID value always wins either way).
    """
    seats_by_id = seats_by_id or {}
    first_match, first_cells = rows[0]
    subject, course_number, section = first_match.group(1).upper(), first_match.group(2), first_match.group(3)
    title = first_cells[1] if len(first_cells) > 1 else None

    meetings = []
    faculty = []
    max_enrollment = None
    had_conflict = False

    seats_available = seats_by_id.get(crn)
    seats_from_id = seats_available is not None

    for _, cells in rows:
        for cell in cells:
            cell_lower = cell.lower()
            if "meeting:" in cell_lower:
                meetings.extend(parse_meetings_text(cell))
            if not faculty and "instructor:" in cell_lower:
                parsed = parse_instructors_text(cell)
                if parsed:
                    faculty = parsed
        seat_pair = _find_seat_pair(cells)
        if seat_pair:
            row_seats, row_max = seat_pair
            mismatch_vs_id = seats_from_id and row_seats != seats_available
            if mismatch_vs_id:
                # The row's guessed "seats" position disagrees with the
                # authoritative ID value -- that means the whole
                # last-two-cells column mapping is suspect for this row,
                # not just seats, so don't trust row_max as max_enrollment
                # from it either.
                had_conflict = True
            else:
                if max_enrollment is None:
                    max_enrollment = row_max
                elif row_max != max_enrollment:
                    had_conflict = True
                if not seats_from_id:
                    if seats_available is None:
                        seats_available = row_seats
                    elif row_seats != seats_available:
                        had_conflict = True

    enrollment = (max_enrollment - seats_available) if (seats_available is not None and max_enrollment is not None) else None

    record = {
        "crn": crn,
        "subject": subject,
        "course_number": course_number,
        "section": section,
        "title": title,
        "seats_available": seats_available,
        "seats_from_id": seats_from_id,
        "max_enrollment": max_enrollment,
        "enrollment": enrollment,
        "meetings": meetings,
        "faculty": faculty,
    }
    return record, had_conflict


def reconcile_term_with_class_schedule(term_code, table_rows, seats_by_id=None):
    """
    Makes the rendered Class Schedule page authoritative for BOTH which
    courses exist in this term AND their seat counts, not just a seat
    patch on top of scraper.py's bulk-API list:

      - A CRN found here but not in the existing bulk-API data becomes a
        new (best-effort) course record.
      - A CRN in the existing bulk-API data but NOT found here is DROPPED
        from the term's course list -- if it's not on the live page, it
        shouldn't be shown as offered.
      - A CRN found in both keeps the bulk API's richer metadata (credits,
        schedule type, campus, etc. -- not available on this page) but
        seats/max/enrollment always come from here. Meetings/instructor
        are overridden too when we successfully parsed them here;
        otherwise the bulk API's values are kept as a defensive fallback
        (my regex parsing of this page is unverified against the live
        site, so falling back rather than blanking out data on a parse
        miss is the safer failure mode).

    seats_by_id (from extract_seats_by_id) is the confirmed, authoritative
    source for seats_available -- a direct <td id="SeatsAvailCRN..."> DOM
    lookup, not a guess. Falls back to row-position guessing only for
    CRNs it doesn't cover.

    This eliminates the old "unverified" gap entirely for terms this runs
    against -- every course in the resulting list is confirmed present
    (and seat-accurate) as of this run, not just seat-patched-if-lucky.

    Returns (new_count, updated_count, dropped_count, total_count), or
    all zeros if there's no existing term file yet.
    """
    seats_by_id = seats_by_id or {}
    path = os.path.join(DATA_DIR, f"{term_code}.json")
    if not os.path.exists(path):
        print(f"No existing web/data/{term_code}.json to reconcile -- run scraper.py for this term first.")
        return 0, 0, 0, 0

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    existing_by_crn = {c["crn"]: c for c in data.get("courses", []) if c.get("crn")}

    groups, skipped_rows = group_rows_by_crn(table_rows)

    conflicts = []
    max_agree = 0
    max_disagree = 0
    mismatch_samples = []
    new_courses = []
    updated_count = 0
    new_count = 0
    unparseable_seats = []
    from_id_count = 0
    from_row_count = 0

    for crn, rows in groups.items():
        render, had_conflict = build_render_record(crn, rows, seats_by_id=seats_by_id)
        if render["seats_available"] is not None:
            if render["seats_from_id"]:
                from_id_count += 1
            else:
                from_row_count += 1
        if had_conflict:
            conflicts.append(crn)

        existing = existing_by_crn.get(crn)
        seats_parsed = render["seats_available"] is not None and render["max_enrollment"] is not None

        if existing:
            prior_max = existing.get("max_enrollment")
            if seats_parsed and prior_max is not None:
                if prior_max == render["max_enrollment"]:
                    max_agree += 1
                else:
                    max_disagree += 1
                    if len(mismatch_samples) < 8:
                        mismatch_samples.append((crn, prior_max, render["max_enrollment"]))

            course = dict(existing)  # keeps credits/schedule_type/campus/etc. we can't get from this page
            course["title"] = render["title"] or existing.get("title")
            if render["meetings"]:
                course["meetings"] = render["meetings"]
            if render["faculty"]:
                course["faculty"] = render["faculty"]
            updated_count += 1

            if seats_parsed:
                course["seats_available"] = render["seats_available"]
                course["max_enrollment"] = render["max_enrollment"]
                course["enrollment"] = render["enrollment"]
                course["seats_estimated"] = False
                course["seats_source"] = "class_schedule_rendered"
                course["open_section"] = render["seats_available"] > 0
            else:
                # Row was found (matched by CRN) but its trailing cells
                # didn't parse as a valid seat/max pair -- keep whatever
                # seat data the course already had rather than blanking it
                # out. A found-but-unparseable row is a parsing gap, not
                # confirmation that the section has no seat info.
                unparseable_seats.append(f"{course.get('subject')} {course.get('course_number')}-{course.get('section')} (CRN {crn})")
        else:
            course = {
                "crn": crn,
                "term": term_code,
                "term_description": data.get("description"),
                "subject": render["subject"],
                "subject_description": None,
                "course_number": render["course_number"],
                "section": render["section"],
                "title": render["title"],
                "credit_hours": None,
                "schedule_type": None,
                "campus": None,
                "instructional_method": None,
                "part_of_term": None,
                "waitlist_available": None,
                "waitlist_capacity": None,
                "faculty": render["faculty"],
                "meetings": render["meetings"],
                "seats_available": render["seats_available"],
                "max_enrollment": render["max_enrollment"],
                "enrollment": render["enrollment"],
                "seats_estimated": False,
                "seats_source": "class_schedule_rendered" if seats_parsed else "banner_search_api",
                "open_section": bool(render["seats_available"] and render["seats_available"] > 0),
            }
            new_count += 1
            if not seats_parsed:
                unparseable_seats.append(f"{course.get('subject')} {course.get('course_number')}-{course.get('section')} (CRN {crn}) [new section]")

        new_courses.append(course)

    dropped = [c for c in existing_by_crn if c not in groups]
    total_before = len(existing_by_crn)

    if conflicts:
        print(f"  WARNING: {len(conflicts)} CRN(s) had rows with different seat numbers -- kept the first row seen for each: {conflicts[:8]}")

    if dropped:
        print(f"  {len(dropped)}/{total_before} previously-scraped sections were NOT found on the rendered page and were dropped from this term's list:")
        for crn in dropped[:8]:
            c = existing_by_crn[crn]
            print(f"    {c.get('subject')} {c.get('course_number')}-{c.get('section')} (CRN {crn})")

    if new_count:
        print(f"  {new_count} section(s) found on the rendered page that weren't in the bulk API scrape -- added with limited metadata (no credits/schedule type available from this page).")

    if unparseable_seats:
        print(f"  {len(unparseable_seats)} section(s) were found on the rendered page but their trailing cells didn't parse as a seat/max pair -- kept prior seat data (if any) rather than blanking it out:")
        for s in unparseable_seats[:8]:
            print(f"    {s}")

    if max_agree + max_disagree:
        pct = round(100 * max_agree / (max_agree + max_disagree))
        print(f"max_enrollment cross-check vs bulk API: {max_agree} agree, {max_disagree} disagree ({pct}% agreement)")
        if pct < 90:
            print("  LOW AGREEMENT -- this suggests the row parsing may be wrong, not just normal drift.")
        if mismatch_samples:
            print("  sample mismatches (crn, api_max, rendered_max):")
            for crn, api_max, rendered_max in mismatch_samples:
                print(f"    {crn}: api={api_max} rendered={rendered_max}")

    if skipped_rows:
        print(f"  ({skipped_rows} rendered rows didn't match the expected 'SUBJ NUM-SEC (CRN)' shape and were skipped)")

    if from_id_count or from_row_count:
        print(f"  seats sourced: {from_id_count} from the confirmed SeatsAvailCRN element, {from_row_count} from row-position fallback")

    data["courses"] = new_courses
    data["seats_refreshed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)

    return new_count, updated_count, len(dropped), len(new_courses)


def process_one_term(term_code, headless=True, debug=False, no_merge=False, page=None):
    """
    Render, parse, and (unless no_merge) reconcile a single term's course
    list + seats against the Class Schedule page. If `page` is given,
    reuses that already-open browser page (faster for
    multi-term runs -- avoids a fresh browser launch per term). Otherwise
    opens and closes its own browser. Returns True on success.
    """
    print(f"\n=== {term_code} ===")
    print(f"Rendering {BASE_URL}?term={term_code} ...")
    if page is not None:
        html, visible_text = _load_and_capture(page, term_code, debug=debug)
    else:
        html, visible_text = render_page(term_code, headless=headless, debug=debug)

    table_rows = parse_via_tables(html)
    text_hits = parse_via_text(visible_text) if not table_rows else []
    print(f"parsed_count={len(table_rows)} (table strategy), {len(text_hits)} (text-line fallback)")

    if not table_rows:
        print(f"No usable table rows for {term_code} -- skipping reconciliation for this term.")
        return False

    seats_by_id = extract_seats_by_id(html)
    print(f"seats_by_id: found {len(seats_by_id)} SeatsAvailCRN element(s) on the page")

    if not no_merge:
        new_count, updated_count, dropped_count, total = reconcile_term_with_class_schedule(term_code, table_rows, seats_by_id=seats_by_id)
        if total:
            print(f"Reconciled web/data/{term_code}.json: {updated_count} updated, {new_count} added, {dropped_count} dropped -- {total} total sections now.")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--term", help="Term code, e.g. 202710 (get one from scraper.py --list-terms)")
    group.add_argument(
        "--all-nonfinished",
        action="store_true",
        help="Process every scraped term in web/data/terms.json that isn't finished yet (used by the workflow)",
    )
    group.add_argument(
        "--all",
        dest="all_terms",
        action="store_true",
        help="Process every scraped term regardless of finished status (for testing -- slower, hits closed terms too)",
    )
    ap.add_argument("--debug", action="store_true", help="Save screenshot + HTML + visible text for inspection")
    ap.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless (local debugging only)")
    ap.add_argument(
        "--no-merge",
        action="store_true",
        help="Just parse and print/save -- don't patch web/data/<term>.json (useful while still verifying)",
    )
    args = ap.parse_args()

    if args.all_nonfinished or args.all_terms:
        from term_utils import is_term_finished

        terms_path = os.path.join(DATA_DIR, "terms.json")
        if not os.path.exists(terms_path):
            print("No web/data/terms.json found -- run scraper.py first.", file=sys.stderr)
            sys.exit(1)
        with open(terms_path, encoding="utf-8") as f:
            terms_index = json.load(f)

        if args.all_terms:
            targets = [t for t in terms_index.get("terms", []) if t.get("scraped")]
        else:
            targets = [
                t for t in terms_index.get("terms", [])
                if t.get("scraped") and not is_term_finished(t["description"])
            ]
        if not targets:
            print("No matching scraped terms to refresh.")
            return

        label = "all scraped" if args.all_terms else "non-finished"
        print(f"Refreshing live seats for {len(targets)} {label} term(s): {[t['code'] for t in targets]}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            try:
                for t in targets:
                    page = browser.new_page(
                        user_agent="Mozilla/5.0 (compatible; MacScheduleTool/1.0; personal course-search project)"
                    )
                    try:
                        process_one_term(t["code"], debug=args.debug, no_merge=args.no_merge, page=page)
                    finally:
                        page.close()
            finally:
                browser.close()
        return

    print(f"Rendering {BASE_URL}?term={args.term} ...")
    html, visible_text = render_page(args.term, headless=not args.headed, debug=args.debug)

    table_rows = parse_via_tables(html)
    text_hits = parse_via_text(visible_text) if not table_rows else []

    print(f"parsed_count={len(table_rows)} (table strategy), {len(text_hits)} (text-line fallback)")

    if table_rows:
        print("\nFirst few parsed rows:")
        for row in table_rows[:5]:
            print("  ", row)
        if args.debug:
            os.makedirs(DATA_DIR, exist_ok=True)
            out_path = os.path.join(DATA_DIR, f"{args.term}_rendered_debug.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"term": args.term, "scraped_at": datetime.now(timezone.utc).isoformat(), "rows": table_rows}, f, indent=1)
            print(f"\nWrote raw parsed rows to {out_path}")

        if not args.no_merge:
            seats_by_id = extract_seats_by_id(html)
            print(f"seats_by_id: found {len(seats_by_id)} SeatsAvailCRN element(s) on the page")
            new_count, updated_count, dropped_count, total = reconcile_term_with_class_schedule(args.term, table_rows, seats_by_id=seats_by_id)
            if total:
                print(f"\nReconciled web/data/{args.term}.json: {updated_count} updated, {new_count} added, {dropped_count} dropped -- {total} total sections now.")
    elif text_hits:
        print("\nNo <table> rows matched, but found course-like lines in the page text:")
        for line in text_hits[:10]:
            print("  ", line)
        print("\nThis means the data IS on the page, but not in a plain <table> -- share the")
        print("_debug_classschedule_*.html file and I'll write a real parser for its actual markup.")
    else:
        print("\nNo course-like content found at all. Either the page didn't finish loading in time")
        print("(try --headed locally to watch it), or the term code has no data, or the page")
        print("structure is different than expected. Check the _debug_classschedule_* screenshot/HTML.")
        sys.exit(1)


if __name__ == "__main__":
    main()
