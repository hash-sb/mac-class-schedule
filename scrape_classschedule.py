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

CRN_RE = re.compile(r"\b(\d{5})\b")  # Banner CRNs are typically 5 digits
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


def render_page(term_code, headless=True, debug=False, extra_wait_ms=3000):
    """Loads the Class Schedule page for a term with a real browser and returns (html, visible_text)."""
    url = f"{BASE_URL}?term={term_code}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; MacScheduleTool/1.0; personal course-search project)")

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

        html = page.content()
        visible_text = page.inner_text("body")

        if debug:
            os.makedirs(DATA_DIR, exist_ok=True)
            page.screenshot(path=os.path.join(DATA_DIR, f"_debug_classschedule_{term_code}.png"), full_page=True)
            with open(os.path.join(DATA_DIR, f"_debug_classschedule_{term_code}.html"), "w", encoding="utf-8") as f:
                f.write(html)
            with open(os.path.join(DATA_DIR, f"_debug_classschedule_{term_code}.txt"), "w", encoding="utf-8") as f:
                f.write(visible_text)

        browser.close()
        return html, visible_text


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", required=True, help="Term code, e.g. 202710 (get one from scraper.py --list-terms)")
    ap.add_argument("--debug", action="store_true", help="Save screenshot + HTML + visible text for inspection")
    ap.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless (local debugging only)")
    args = ap.parse_args()

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
