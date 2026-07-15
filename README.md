# Macalester Schedule Search

An unofficial, searchable mirror of Macalester's course schedules, built to
run entirely on GitHub Pages + GitHub Actions -- no server to host or pay for.

- **Scraper** (`scraper.py`) pulls course data from Macalester's public
  Banner 9 class search (the same "Browse Classes" tool at
  `oci-macxe.macalester.edu` that students already use -- no login required).
- **`registration_calendar.py`** reads Macalester's live Academic Calendar
  page each run and decides whether today falls inside an early-registration
  window or the first-couple-weeks add/drop period for any term -- no
  hardcoded dates, so it stays correct year over year automatically.
- **`scrape_classschedule.py`** renders the actual Class Schedule page with
  a real headless browser (Playwright) and patches live seat counts on top
  of what `scraper.py` collected -- see "Live seat counts" below for why
  this exists as a separate step.
- **GitHub Actions** (`.github/workflows/update-schedule.yml`) checks that
  window every hour and only does a full scrape + redeploy when it matters
  (inside a window, or once a day as a baseline) -- otherwise it's a fast
  no-op, so this stays cheap to run continuously.
- **Frontend** (`web/`) is a plain HTML/CSS/JS static site -- no build step,
  no framework, no backend. It loads the JSON straight out of `web/data/`
  and does all searching/filtering/sorting in the browser. Defaults to the
  current-or-upcoming semester, with a dropdown to view any other single
  term or **"All semesters"** to search across everything that's been
  scraped.

## How the pieces fit together

```
registration_calendar.py ──reads──▶ Macalester's live Academic Calendar
        │
        ▼ (only when today is inside a window, or once/day as baseline)
scraper.py ──writes──▶ web/data/<term_code>.json   (one file per term)
                        web/data/terms.json         (index + "current term")
web/index.html, style.css, app.js ──reads──▶ web/data/*.json  (client-side fetch)
```

Nothing in `web/` ever talks to Macalester's servers directly -- only
`scraper.py` and `registration_calendar.py` do that, and only when GitHub
Actions (or you, locally) runs them.

## Forcing a full update while testing

Don't want to wait for (or fake) an actual registration window just to
test a change? On the Actions tab, "Update schedule data & deploy" ->
**Run workflow** has a **force** checkbox -- check it and the run skips
the window check entirely, doing a full scrape + Class Schedule refresh +
deploy regardless of the date. It also refreshes seats for *every*
scraped term (not just non-finished ones), so it's a good end-to-end
smoke test.

Locally, the equivalent is just calling the underlying scripts directly
without the gating wrapper:

```bash
python registration_calendar.py --force   # confirms run_full=true, no window needed
python scraper.py --all --max-terms 16 --no-live-seats
python scrape_classschedule.py --all      # every scraped term, not just non-finished
```

## How the scrape frequency works

The workflow's cron fires every hour, but `registration_calendar.py` runs
first and decides whether the rest of the job actually does anything:

- **Inside a window** (early registration for any term, or the first
  couple weeks of a term when students are adding/dropping) -> full scrape
  + redeploy, every hour.
- **Otherwise** -> skipped, except once a day at a fixed baseline hour
  (13:00 UTC by default -- see `BASELINE_HOUR_UTC` in
  `registration_calendar.py`) so the catalog still stays reasonably fresh
  even during quiet periods.

Within a scrape itself, `scraper.py` separately only does the expensive
per-CRN live-seat refresh for terms that aren't finished yet (see
`is_term_finished` in `term_utils.py`) -- so even a "full scrape" stays
fast, since closed terms just reuse their bulk-search snapshot.

Run `python registration_calendar.py --debug` any time to see every window
it parsed off the live calendar page and whether today falls inside one --
useful both for sanity-checking and if Macalester ever restructures that
page (parsing fails safe: if it can't find any windows, it treats today as
"not active" rather than erroring out, so you'll just fall back to the
once-daily baseline until the parsing is fixed).

## Setting it up on GitHub

1. **Create a repo** and push this project to it (see commands below if
   you're starting from this folder).

2. **Enable Pages via Actions.** In the repo: Settings -> Pages -> under
   "Build and deployment", set **Source** to **GitHub Actions**. (Don't
   point it at a branch -- the workflow deploys directly.)

3. **Run it the first time.** Go to the Actions tab -> "Update schedule
   data & deploy" -> **Run workflow**. This scrapes real data and deploys
   the site. After that it runs automatically on the cron schedule in the
   workflow file (daily by default -- edit the `cron:` line to change it,
   e.g. hourly during registration weeks).

4. **Find your URL.** Once the job finishes, the site's live at
   `https://<your-username>.github.io/<repo-name>/` (also shown in the
   Actions run summary and in Settings -> Pages).

That's it -- no "Workflow permissions" setting to touch. The workflow
scrapes and deploys in a single job without ever committing back to the
repo, so it only needs the default read-only `GITHUB_TOKEN` plus Pages
deploy permissions. (If your repo is under an organization, the "Read and
write permissions" toggle is often locked by the org owner anyway --
this design avoids needing it at all.)

If you *do* want the scraped JSON versioned in git history (e.g. to see
how seat counts changed over time), that's an easy add-on: reintroduce a
`git add / commit / push` step before the deploy step, using either the
default token (if your org allows read/write) or a fine-grained Personal
Access Token stored as a repo secret.

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

## Running the scraper locally

Useful for testing, or for scraping terms you don't want to wait for the
cron job to pick up.

```bash
pip install -r requirements.txt

python scraper.py --list-terms          # see every term code Banner knows about
python scraper.py --current             # scrape just the current/upcoming term
python scraper.py --term 202630         # scrape one specific term code
python scraper.py --all --max-terms 12  # scrape the 12 most recent terms

python registration_calendar.py --debug # see whether today is inside a registration/add-drop window
```

Each run writes into `web/data/`. Preview the site locally with any static
file server (needed because `fetch()` of local JSON won't work from a bare
`file://` URL):

```bash
cd web
python3 -m http.server 8000
# open http://localhost:8000
```

### If Macalester changes their Banner setup

Banner 9's API is standardized across schools, but institutions occasionally
customize field names or add filters. If the scraper starts returning empty
results, run:

```bash
python scraper.py --debug
```

This dumps the raw JSON Banner returns to `web/data/_debug_<term>.json` so
you can see the actual field names and adjust `parse_section()` in
`scraper.py` accordingly.

## Course list + live seat counts

For terms that aren't finished yet (see `is_term_finished` in
`term_utils.py`), `macadmsys.macalester.edu`'s Class Schedule page is now
**authoritative for both which courses exist and their seat counts** --
not just a seat patch layered on top of `scraper.py`'s bulk API scrape.
`scrape_classschedule.py` renders that page with a real headless browser
(it has no course data in its raw HTML -- it's an Angular app, so plain
`requests` can't see it) and reconciles it against whatever `scraper.py`
already collected from Banner's search API on `oci-macxe.macalester.edu`:

- A CRN found on the rendered page but not in the bulk API scrape becomes
  a **new** course record (with limited metadata -- credits, schedule
  type, and campus aren't shown on this page, so those stay blank for
  these).
- A CRN in the bulk API scrape but **not** found on the rendered page is
  **dropped** from the term's list entirely -- if it's not on the live
  page, it shouldn't be shown as offered.
- A CRN found in both keeps the bulk API's richer metadata (credits,
  schedule type, campus, etc.) but title/meetings/instructor/seats are
  always taken from the rendered page when successfully parsed, falling
  back to the bulk API's values only if parsing that specific field came
  up empty (a defensive hedge, since this parsing is inherently harder to
  verify than the well-documented Banner search API).

### Seats specifically: a confirmed DOM lookup, not a guess

Every prior approach to reading `seats_available` off this page guessed
at column positions in the rendered table, which repeatedly produced
wrong or missing values. `extract_seats_by_id()` instead does a direct,
unambiguous lookup: each class lives in a `<table class="TableClass">`,
and the open-seats value sits in a `<td id="SeatsAvailCRN{crn}">` --
confirmed against the real page's structure, not inferred from example
rows. This is now the authoritative source for `seats_available` whenever
it covers a CRN.

`max_enrollment` doesn't have a confirmed DOM id yet, so it still comes
from row-position guessing (the cell alongside wherever seats would have
been). To keep that honest: if the ID-based seats value disagrees with
what the row-position guess found for that same row, the whole column
mapping is treated as suspect for that row -- `max_enrollment` is
withheld rather than kept from a row that already failed one cross-check.
Every run prints `seats sourced: N from the confirmed SeatsAvailCRN
element, M from row-position fallback` so it's obvious how much of a
term's data is on solid ground versus still a guess.

This is what eliminates the need for any "unverified" indicator in the
UI -- every course in a reconciled term's list is confirmed present (and
seat-accurate) as of that run, not just seat-patched when we got lucky
with a CRN match.

Confirmed against real output to correctly parse rows like:

```
AMST 130-F1 (10018)  ...  -1  16
```

(CRN 10018, 1 over capacity out of 16 max -- negative seats-available is
a real Banner state from enrollment overrides, shown as-is with a leading
"-" in the app rather than clamped to zero).

**Bug fixed:** negative seat counts specifically were breaking even after
the ID-based lookup above, because the parsing only recognized a plain
ASCII hyphen-minus (`-1`). Pages often render negative numbers with a
different character for typographic reasons -- a true minus sign
(U+2212), en/em dashes, or accounting-style parentheses (`(1)` meaning
`-1`) -- and the old regex silently rejected all of those, so
over-capacity sections specifically ended up missing or wrong while
normal positive counts looked fine. `parse_signed_int()` now normalizes
all of these before parsing (tested against every variant).

```bash
pip install -r requirements.txt
python -m playwright install chromium   # one-time, downloads the browser

python scrape_classschedule.py --term 202710 --debug   # one term, verbose
python scrape_classschedule.py --all-nonfinished        # every term that can still change (what the workflow runs)
```

`--all-nonfinished` reads `web/data/terms.json` and only processes terms
where `is_term_finished()` is false -- closed terms keep whatever
`scraper.py` already collected, since those numbers can't change anymore
anyway (no course-list reconciliation happens for them either).

### If numbers still look wrong

Every reconciliation run prints:

- **A max_enrollment cross-check** against the bulk API, for CRNs found
  in both sources. Unlike open seats, max capacity is essentially fixed
  for the term, so this is a good signal for whether the row parsing
  itself is right, separate from normal seat-count drift:
  ```
  max_enrollment cross-check vs bulk API: 720/765 agree (94% agreement)
  ```
  High agreement with numbers still looking off is most likely **timing**
  -- seats genuinely change between when a run scrapes and when you check
  the live site. Low agreement is a real parsing problem -- the log
  prints sample mismatches (`crn, api_max, rendered_max`) to spot-check.
- **Dropped sections** -- previously-scraped CRNs not found this run,
  named individually.
- **Newly-added sections** -- CRNs found on the page that weren't in the
  bulk API scrape.
- **Same-CRN conflicts** -- if a section spans multiple rows (e.g.
  multiple meeting patterns) and they disagree on seat numbers, the first
  row is kept and the conflict is logged rather than silently letting
  whichever row came later win.
- **Found-but-unparseable sections** -- a real bug, now fixed: a course
  row could be correctly matched by CRN but have trailing cells that
  didn't parse as a valid seat/max pair, and the old code unconditionally
  overwrote the course's seat fields with that `None` anyway -- silently
  blanking out previously-good data instead of leaving it alone. Fixed:
  seats are only ever overwritten when a real pair was parsed; otherwise
  prior data (if any) is kept and the section is named in this log
  category so gaps stay visible instead of silent.

  Seat-pair extraction briefly scanned from the right for *any* adjacent
  integer pair (not just the literal last two cells), to tolerate a
  hypothetical trailing extra column -- **reverted**, since that leniency
  risked grabbing an unrelated numeric cell (e.g. a room number) and
  confidently presenting it as seats/max, which produces wrong-but-
  plausible-looking values instead of a safe "couldn't parse" outcome.
  Missing data is a much better failure mode than wrong data. Extraction
  now strictly requires the literal last two cells, plus a sanity bound
  (`0 < max_enrollment <= 999`) rejecting anything outside a realistic
  class size.

Also: `scrape_classschedule.py` tries clicking an "Update Open Seats"
control if it finds one on the page before capturing numbers, logging
whether it found one -- worth checking that line too.

## Making runs faster

A "full" run (inside a registration window, or the daily baseline) has two
slow parts: downloading Chromium and launching a browser per term. Both are
addressed:

- **Chromium binary is cached** (`actions/cache`, keyed on `requirements.txt`)
  -- most runs skip the ~150MB+ download entirely and only pay for the
  OS-level dependency install (`playwright install-deps`), which is fast
  (~10-20s) and has to run every time anyway since GitHub-hosted runners
  are a fresh VM each run.
- **pip dependencies are cached** via `actions/setup-python`'s built-in
  `cache: pip`.
- **`scrape_classschedule.py` reuses one browser across all terms** in a
  multi-term run (`--all-nonfinished` / `--all`) instead of launching a
  fresh Chromium process per term -- only `page.new_page()` per term, not
  a full browser relaunch.

Further levers you can pull yourself, each a real speed/coverage trade-off:

- **`--max-terms`** in the "Scrape current data" step (currently 16) --
  fewer terms means a faster `scraper.py` pass and fewer terms for
  `--all-nonfinished` to potentially touch, at the cost of less history
  available in "All semesters" search.
- **The cron frequency** itself (`0 * * * *`, hourly) -- the
  `registration_calendar.py` gate already skips most hours for free, so
  this mostly matters for how promptly a run picks up right as a window
  opens, not for cost (skipped runs finish in seconds).
- **`extra_wait_ms` in `scrape_classschedule.py`** (currently a flat 3000ms
  per term, plus another 2000ms after clicking "Update Open Seats") -- these
  are conservative fixed waits since I can't verify the page's real loading
  behavior from my sandbox. If you find it reliably finishes sooner, this
  is tunable, but shortening it risks silently capturing an unfinished
  render instead of a real speed problem.

## Demo data

Sample data is **not shipped** in this repo (it used to be, but a stale
fake term code lingering in `terms.json` forever was a real bug -- see
`update_terms_index()` in `scraper.py`). `web/data/` starts empty except
for a README explaining why.

If you want to preview the site locally before your first real scrape,
`generate_sample_data.py` still works the same way:

```bash
python generate_sample_data.py
```

Just make sure a real `scraper.py` run happens (or you delete these files
yourself) before your first deploy, so fake data never gets committed.

## Search features

- Free-text search across title, subject, course number, CRN, and instructor
  -- plain substring matching by default, or check **Regex** next to the
  search box to interpret it as a real regular expression (e.g. `^comp`);
  `&` works as an AND operator either way (e.g. `COMP 123 & Amin`)
- Filter by subject (multi-select), instructor, CRN, meeting days (exact
  patterns pulled from whatever's actually offered that semester, e.g.
  `MWF`, `TR`, `M`), meeting-time window, and seat status (any / open only
  / closed only)
- Sort by subject/number, title, instructor, open seats, or term
- Select **any combination of semesters** from the Term(s) picker --
  one, several, or all -- the Term column and term-based sorting kick in
  automatically whenever more than one is active
- Defaults to whatever semester is current or coming up next, computed from
  today's date -- no hardcoded term codes to update every year

## Limitations / notes

- This only reads Macalester's *public* class search -- nothing that
  requires a Macalester login (real-time seat counts on the login-gated
  page may differ slightly from what guest search reports).
- It's an unofficial tool, not affiliated with or endorsed by the
  Registrar's Office. Always confirm registration details in the official
  system before registering.
