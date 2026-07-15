This directory is populated by `scraper.py` / `scrape_classschedule.py` /
GitHub Actions -- it's intentionally empty in the repo.

Previously this project shipped with pre-generated sample data baked in
for local preview. That turned out to cause a real bug: if a sample term
code didn't match a real Banner term code, it would linger in `terms.json`
forever (nothing ever pruned it), showing a fake "semester" with fake
courses in the picker indefinitely.

`update_terms_index()` in `scraper.py` now rebuilds `terms.json` fresh
from Banner's real known terms every run, so this can't happen again --
but as a belt-and-suspenders fix, sample data is no longer shipped at all.

If you want to preview the site locally before your first real scrape,
run `python generate_sample_data.py` yourself -- just make sure you run
a real `scraper.py` pass (or just delete these files) before your first
deploy, so fake data never ends up committed to your repo.
