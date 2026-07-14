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

## Verifying against the actual Class Schedule page

If numbers ever look wrong compared to what you see on
`https://www.macalester.edu/registrar/schedules/` (which links to pages
like `macadmsys.macalester.edu/.../classSchedule?term=202710`), that's a
**different Banner front-end/host** than the search API `scraper.py` uses
(`oci-macxe.macalester.edu`) -- possibly a different environment/instance
with different freshness. `scrape_classschedule.py` renders that exact
page with a real headless browser (it's JavaScript-only, so plain
requests can't see its data) and reads the numbers out of the DOM instead.

This one is unverified against the live site (my sandbox can't reach
macalester.edu to test it) -- run it with `--debug` first and check the
output before trusting it:

```bash
pip install -r requirements.txt
playwright install --with-deps chromium   # one-time, downloads the browser

python scrape_classschedule.py --term 202710 --debug
```

It prints how many course-like rows it found. If that number looks right,
you're good. If it's 0 or clearly wrong, open the saved
`web/data/_debug_classschedule_202710.png` (screenshot),
`.html` (rendered markup), and `.txt` (visible text) files it always
writes in debug mode, and share what the actual row markup looks like --
I built the parser defensively (real `<table>` first, falls back to
scanning visible text) specifically because I couldn't see this page's
real structure, so it likely needs one round of fixing against real output
before it's reliable enough to wire into the scheduled workflow.

## Demo data

`generate_sample_data.py` fills `web/data/` with realistic-but-fake course
listings across 7 terms, so you can see the whole app working immediately
without waiting on a real scrape:

```bash
python generate_sample_data.py
```

The real scraper will overwrite these files the next time it runs.

## Search features

- Free-text search across title, subject, course number, CRN, and instructor
- Filter by subject (multi-select), instructor, days of week, meeting-time
  window, credit hours, campus, section type, open-seats-only, and CRN
- Sort by subject/number, title, instructor, credits, or open seats
- View one semester or **all semesters at once**
- Defaults to whatever semester is current or coming up next, computed from
  today's date -- no hardcoded term codes to update every year

## Limitations / notes

- This only reads Macalester's *public* class search -- nothing that
  requires a Macalester login (real-time seat counts on the login-gated
  page may differ slightly from what guest search reports).
- It's an unofficial tool, not affiliated with or endorsed by the
  Registrar's Office. Always confirm registration details in the official
  system before registering.
