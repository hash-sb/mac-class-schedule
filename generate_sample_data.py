#!/usr/bin/env python3
"""
generate_sample_data.py -- Populates web/data/ with realistic-looking (but
fake) course data, in exactly the schema scraper.py produces, so the site
is fully browsable/demoable before you've run the real scraper or before
GitHub Actions has run for the first time.

Run once locally:  python generate_sample_data.py
Then either open web/index.html with a local server, or just push it so
the very first Pages deploy has something to show.

The real scraper will overwrite these files with real data.
"""

import json
import os
import random
from datetime import datetime, timezone

from term_utils import guess_current_term, sort_terms_chronologically

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "data")

SUBJECTS = [
    ("ART", "Art and Art History"),
    ("BIOL", "Biology"),
    ("CHEM", "Chemistry"),
    ("COMP", "Computer Science"),
    ("ECON", "Economics"),
    ("ENGL", "English"),
    ("HIST", "History"),
    ("MATH", "Mathematics, Statistics, and Computer Science"),
    ("MUSI", "Music"),
    ("PHIL", "Philosophy"),
    ("PHYS", "Physics and Astronomy"),
    ("PSYC", "Psychology"),
    ("POLI", "Political Science"),
    ("SPAN", "Hispanic Studies"),
]

TITLES = [
    "Introduction to {subj}", "Intermediate {subj}", "Topics in {subj}",
    "{subj} and Society", "Advanced {subj} Seminar", "Foundations of {subj}",
    "{subj} Laboratory", "Research Methods in {subj}", "{subj}: A Global Perspective",
]

FACULTY_NAMES = [
    "A. Nguyen", "B. Okafor", "C. Larsson", "D. Ramirez", "E. Chen",
    "F. Osei", "G. Petrov", "H. Ibrahim", "I. Kowalski", "J. Alvarez",
    "K. Whitfield", "L. Tanaka",
]

BUILDINGS = [("OLRI", "Olin-Rice"), ("HUM", "Humanities"), ("CARN", "Carnegie Hall"), ("MARK", "Markim Hall"), ("KAG", "Kagin Commons")]

DAY_PATTERNS = ["MWF", "TR", "MW", "M", "TWR"]

CAMPUSES = ["Main Campus"]
SCHEDULE_TYPES = ["Lecture", "Seminar", "Laboratory", "Studio"]


def make_term_courses(term_code, term_desc, seed):
    rng = random.Random(seed)
    courses = []
    crn = 10000 + seed * 500
    for subj_code, subj_desc in SUBJECTS:
        n_courses = rng.randint(3, 6)
        for i in range(n_courses):
            course_num = str(rng.choice([100, 120, 200, 235, 265, 310, 340, 394]))
            title = rng.choice(TITLES).format(subj=subj_desc.split(",")[0].split(" and ")[0])
            n_sections = rng.choice([1, 1, 1, 2])
            for sec in range(1, n_sections + 1):
                crn += rng.randint(1, 7)
                building, building_desc = rng.choice(BUILDINGS)
                days = rng.choice(DAY_PATTERNS)
                start_hour = rng.choice([8, 9, 10, 11, 13, 14, 15])
                start_min = rng.choice([0, 30])
                dur = rng.choice([50, 65, 80])
                start_total = start_hour * 60 + start_min
                end_total = start_total + dur
                start_time = f"{start_total // 60:02d}{start_total % 60:02d}"
                end_time = f"{end_total // 60:02d}{end_total % 60:02d}"
                max_enroll = rng.choice([12, 16, 20, 24, 30])
                enrolled = rng.randint(0, max_enroll + 2)  # occasionally over capacity, like real overrides
                seats = max_enroll - enrolled
                seats_estimated = False
                if rng.random() < 0.15:
                    # Simulate the guest-search gap: seatsAvailable missing, computed instead.
                    seats_estimated = True
                faculty_name = rng.choice(FACULTY_NAMES)

                courses.append(
                    {
                        "crn": str(crn),
                        "term": term_code,
                        "term_description": term_desc,
                        "subject": subj_code,
                        "subject_description": subj_desc,
                        "course_number": course_num,
                        "section": f"{sec:02d}",
                        "title": title,
                        "credit_hours": rng.choice([1.0, 1.0, 1.0, 0.5]) * 1,
                        "schedule_type": rng.choice(SCHEDULE_TYPES),
                        "campus": rng.choice(CAMPUSES),
                        "instructional_method": "In Person",
                        "part_of_term": "Full Term",
                        "seats_available": seats,
                        "seats_estimated": seats_estimated,
                        "seats_source": "class_schedule_rendered" if rng.random() < 0.85 else "banner_search_api",
                        "max_enrollment": max_enroll,
                        "enrollment": enrolled,
                        "waitlist_available": 5 if seats == 0 else 0,
                        "waitlist_capacity": 5,
                        "open_section": seats > 0,
                        "faculty": [{"name": faculty_name, "email": None, "primary": True}],
                        "meetings": [
                            {
                                "days": days,
                                "start_time": start_time,
                                "end_time": end_time,
                                "building": building,
                                "building_description": building_desc,
                                "room": str(rng.randint(100, 399)),
                                "campus": "Main Campus",
                                "meeting_type": "Class",
                            }
                        ],
                    }
                )
    return courses


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # A representative spread of terms: a couple past, current/upcoming, a couple future.
    terms = [
        {"code": "202410", "description": "Fall 2024"},
        {"code": "202430", "description": "Spring 2025"},
        {"code": "202510", "description": "Fall 2025"},
        {"code": "202630", "description": "Spring 2026"},
        {"code": "202660", "description": "Summer 2026"},
        {"code": "202710", "description": "Fall 2026"},
        {"code": "202730", "description": "Spring 2027"},
    ]
    terms = sort_terms_chronologically(terms)

    for i, t in enumerate(terms):
        courses = make_term_courses(t["code"], t["description"], seed=i + 1)
        path = os.path.join(DATA_DIR, f"{t['code']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"code": t["code"], "description": t["description"], "scraped_at": now, "courses": courses}, f, indent=1
            )
        print(f"wrote {len(courses)} sample sections -> {path}")

    current = guess_current_term(terms)
    terms_index = {
        "current_term_code": current["code"] if current else None,
        "generated_at": now,
        "terms": [{"code": t["code"], "description": t["description"], "scraped": True} for t in terms],
    }
    with open(os.path.join(DATA_DIR, "terms.json"), "w", encoding="utf-8") as f:
        json.dump(terms_index, f, indent=1)
    print(f"wrote terms.json (current: {current['description'] if current else 'none'})")


if __name__ == "__main__":
    main()
