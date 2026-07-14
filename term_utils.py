"""
Shared helpers for reasoning about Banner "term" codes and descriptions.

Banner term descriptions look like "Fall 2026", "Spring 2026", "Interim 2026",
"Summer 2026", etc. Banner term *codes* (e.g. "202630") are opaque and their
numbering scheme varies by institution, so we never hardcode what the last
two digits mean -- instead we parse the human-readable description that the
API returns alongside each code.

This module is imported by both scraper.py (to tag each scraped term with a
rough calendar position) and app.py (to pick the default term to show).
"""

import re
from datetime import date

# Rough month a term "starts", used only to order/rank terms and to guess
# which one is current. Not exact -- just good enough to sort chronologically
# and to figure out which term today's date most likely falls in.
SEASON_START_MONTH = {
    "spring": 1,
    "interim": 1,
    "january": 1,
    "winter": 1,
    "summer": 6,
    "fall": 9,
    "autumn": 9,
}

# Rough month a term "ends" (exclusive-ish), used to decide if `today` falls
# inside a term.
SEASON_END_MONTH = {
    "spring": 5,
    "interim": 2,
    "january": 2,
    "winter": 2,
    "summer": 8,
    "fall": 12,
    "autumn": 12,
}


def parse_term_description(description):
    """
    Parse a string like "Fall 2026" -> ("fall", 2026).
    Returns (None, None) if it can't be parsed (rare/irregular terms).
    """
    if not description:
        return None, None
    match = re.search(
        r"(spring|summer|fall|autumn|winter|interim|january)\D*(\d{4})",
        description,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    season = match.group(1).lower()
    year = int(match.group(2))
    return season, year


def term_sort_key(description):
    """Sortable (year, month) tuple for a term description. Unparsable terms sort last."""
    season, year = parse_term_description(description)
    if season is None:
        return (9999, 12)
    return (year, SEASON_START_MONTH.get(season, 6))


def guess_current_term(terms, today=None):
    """
    Given a list of {"code": ..., "description": ...} dicts (as returned by
    Banner's getTerms endpoint), guess which one is "current or upcoming".

    Logic:
      1. If today's date falls within a term's rough start/end window, use it.
      2. Otherwise, pick the soonest future term.
      3. Otherwise (all terms are in the past), pick the most recent past term.

    Returns the matching dict from `terms`, or None if `terms` is empty.
    """
    if not terms:
        return None

    today = today or date.today()
    today_key = (today.year, today.month)

    parsed = []
    for t in terms:
        season, year = parse_term_description(t.get("description", ""))
        if season is None:
            continue
        start_m = SEASON_START_MONTH.get(season, 6)
        end_m = SEASON_END_MONTH.get(season, 6)
        parsed.append(
            {
                "term": t,
                "start": (year, start_m),
                "end": (year, end_m),
            }
        )

    if not parsed:
        # Nothing parsed -- just return the first term Banner gave us
        # (Banner typically returns terms with the most relevant one first).
        return terms[0]

    # 1. Currently inside a term's window
    for p in parsed:
        if p["start"] <= today_key <= p["end"]:
            return p["term"]

    # 2. Soonest future term
    future = [p for p in parsed if p["start"] > today_key]
    if future:
        future.sort(key=lambda p: p["start"])
        return future[0]["term"]

    # 3. Most recent past term
    parsed.sort(key=lambda p: p["start"], reverse=True)
    return parsed[0]["term"]


def sort_terms_chronologically(terms, reverse=True):
    """Sort a list of {"code", "description"} dicts by (year, season). Newest first by default."""
    return sorted(terms, key=lambda t: term_sort_key(t.get("description", "")), reverse=reverse)
