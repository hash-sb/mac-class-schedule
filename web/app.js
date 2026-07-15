// Macalester Schedule Search — fully static, client-side.
// Loads data/terms.json + data/<term>.json (produced by scraper.py via the
// GitHub Actions workflow) and does all searching/filtering/sorting here in
// the browser, so the whole thing can live on GitHub Pages with no backend.

const DATA_BASE = "data";
const PAGE_SIZE = 50;

const state = {
  termsIndex: { current_term_code: null, terms: [] },
  termCache: new Map(), // code -> {code, description, courses}
  selectedTerms: new Set(),  // term codes, or the single value "all"
  meta: { subjects: [], instructors: [], dayPatterns: [] },
  selectedSubjects: new Set(),
  selectedDays: new Set(),  // exact meeting-day patterns, e.g. "MWF", "TR"
  page: 1,
  sortBy: "subject",
  sortDir: "asc",
};

const el = (id) => document.getElementById(id);

// ---------------- data loading ----------------

async function fetchJSON(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} fetching ${path}`);
  return res.json();
}

async function loadTermsIndex() {
  try {
    state.termsIndex = await fetchJSON(`${DATA_BASE}/terms.json`);
  } catch (e) {
    console.error("Could not load terms.json", e);
    state.termsIndex = { current_term_code: null, terms: [] };
  }
}

async function loadTermData(code) {
  if (state.termCache.has(code)) return state.termCache.get(code);
  try {
    const data = await fetchJSON(`${DATA_BASE}/${code}.json`);
    state.termCache.set(code, data);
    return data;
  } catch (e) {
    // Term listed in terms.json but not (yet) scraped -- not an error, just no data.
    return null;
  }
}

/** Which term codes are "active" given the current selection: real codes, or every known term if "all" is selected. */
function activeTermCodes() {
  if (state.selectedTerms.has("all") || state.selectedTerms.size === 0) {
    return state.termsIndex.terms.map((t) => t.code);
  }
  return [...state.selectedTerms];
}

/** Load whatever term(s) the current selection needs, returning a flat array of course records. */
async function getActiveCourses() {
  const codes = activeTermCodes();
  const loaded = await Promise.all(codes.map((c) => loadTermData(c)));
  return loaded.filter(Boolean).flatMap((d) => d.courses);
}

// ---------------- filtering / sorting (mirrors what a backend would do) ----------------

function parseTimeToMinutes(hhmm) {
  if (!hhmm || hhmm.length < 3) return null;
  const padded = hhmm.padStart(4, "0");
  const h = parseInt(padded.slice(0, 2), 10);
  const m = parseInt(padded.slice(2), 10);
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h * 60 + m;
}

function buildSearchClauses(query) {
  // "&" is our AND operator between clauses, e.g. "COMP 123 & Amin"
  return query.split("&").map((s) => s.trim()).filter(Boolean);
}

function readFilters() {
  const startAfter = el("start-after").value; // "HH:MM" or ""
  const endBefore = el("end-before").value;
  const qRaw = el("q").value.trim();
  return {
    qClauses: buildSearchClauses(qRaw),
    regexMode: el("regex-mode").checked,
    subjects: state.selectedSubjects,
    instructor: el("instructor").value.trim().toLowerCase(),
    crn: el("crn").value.trim(),
    seatStatus: el("seat-status").value, // "", "open", "closed"
    days: state.selectedDays,
    startAfterMin: startAfter ? parseInt(startAfter.split(":")[0], 10) * 60 + parseInt(startAfter.split(":")[1], 10) : null,
    endBeforeMin: endBefore ? parseInt(endBefore.split(":")[0], 10) * 60 + parseInt(endBefore.split(":")[1], 10) : null,
  };
}

function activeFilterCount(f) {
  let n = 0;
  if (f.qClauses.length) n++;
  if (f.subjects.size) n++;
  if (f.instructor) n++;
  if (f.crn) n++;
  if (f.seatStatus) n++;
  if (f.days.size) n++;
  if (f.startAfterMin !== null) n++;
  if (f.endBeforeMin !== null) n++;
  return n;
}

const COURSE_CODE_RE = /^([a-z]{2,6})\s*-?\s*(\d{1,4}[a-z]?)$/i;

/** Does ONE search clause match this course? Tries the course-code shortcut
 * first (always, regardless of mode). In regex mode, treats the clause as
 * a case-insensitive regular expression (falling back to plain substring
 * if it isn't valid regex syntax). In simple mode (the default), it's
 * always a plain case-insensitive substring match -- predictable, no
 * special characters to worry about. */
function clauseMatchesCourse(clause, haystackLower, c, regexMode) {
  const codeMatch = clause.match(COURSE_CODE_RE);
  if (codeMatch) {
    const courseSubj = (c.subject || "").toLowerCase();
    const courseNum = (c.course_number || "").toLowerCase();
    if (courseSubj.startsWith(codeMatch[1].toLowerCase()) && courseNum.startsWith(codeMatch[2].toLowerCase())) {
      return true;
    }
  }
  if (regexMode) {
    try {
      const re = new RegExp(clause, "i");
      return re.test(haystackLower);
    } catch (e) {
      return haystackLower.includes(clause.toLowerCase());
    }
  }
  return haystackLower.includes(clause.toLowerCase());
}

function courseMatches(c, f) {
  if (f.qClauses.length) {
    const haystack = [
      c.subject, c.course_number, c.title, c.subject_description, c.crn,
      ...(c.faculty || []).map((x) => x.name || ""),
    ].filter(Boolean).join(" ").toLowerCase();

    const allMatch = f.qClauses.every((clause) => clauseMatchesCourse(clause, haystack, c, f.regexMode));
    if (!allMatch) return false;
  }

  if (f.subjects.size && !f.subjects.has(c.subject)) return false;

  if (f.instructor) {
    const names = (c.faculty || []).map((x) => x.name || "").join(" ").toLowerCase();
    if (!names.includes(f.instructor)) return false;
  }

  if (f.crn && !(c.crn || "").includes(f.crn)) return false;

  if (f.seatStatus === "open" && c.open_section !== true) return false;
  if (f.seatStatus === "closed" && c.open_section !== false) return false;

  if (f.days.size) {
    // Exact meeting-pattern match (e.g. "MWF", "TR"), not "any single day in common".
    const meetings = c.meetings || [];
    const hit = meetings.some((m) => m.days && f.days.has(m.days));
    if (!hit) return false;
  }

  if (f.startAfterMin !== null || f.endBeforeMin !== null) {
    const meetings = c.meetings || [];
    const ok = meetings.some((m) => {
      const s = parseTimeToMinutes(m.start_time);
      const e = parseTimeToMinutes(m.end_time);
      if (s === null || e === null) return false;
      if (f.startAfterMin !== null && s < f.startAfterMin) return false;
      if (f.endBeforeMin !== null && e > f.endBeforeMin) return false;
      return true;
    });
    if (!ok) return false;
  }

  return true;
}

// Mirrors term_utils.py's season->month mapping, used only to sort by term chronologically.
const SEASON_START_MONTH = { spring: 1, interim: 1, january: 1, winter: 1, summer: 6, fall: 9, autumn: 9 };
function termSortKey(course) {
  const desc = course.term_description || "";
  const m = desc.match(/(spring|summer|fall|autumn|winter|interim|january)\D*(\d{4})/i);
  if (!m) return 999912; // unparsable descriptions sort last
  const year = parseInt(m[2], 10);
  const month = SEASON_START_MONTH[m[1].toLowerCase()] || 6;
  return year * 100 + month;
}

function sortCourses(courses) {
  const dir = state.sortDir === "desc" ? -1 : 1;
  const keyFn = {
    title: (c) => c.title || "",
    instructor: (c) => (c.faculty && c.faculty[0] && c.faculty[0].name) || "",
    seats: (c) => {
      if (c.seats_available !== null && c.seats_available !== undefined) return c.seats_available;
      if (c.max_enrollment != null && c.enrollment != null) return c.max_enrollment - c.enrollment;
      return -999;
    },
    term: (c) => termSortKey(c),
    subject: (c) => `${c.subject || ""} ${c.course_number || ""}`,
  }[state.sortBy] || ((c) => `${c.subject || ""} ${c.course_number || ""}`);

  return [...courses].sort((a, b) => {
    const ka = keyFn(a), kb = keyFn(b);
    if (ka < kb) return -1 * dir;
    if (ka > kb) return 1 * dir;
    return 0;
  });
}

// ---------------- rendering ----------------

function dayLabel(days) {
  const map = { M: "M", T: "Tu", W: "W", R: "Th", F: "F", S: "Sa", U: "Su" };
  return [...(days || "")].map((d) => map[d] || d).join("");
}

function timeLabel(hhmm) {
  if (!hhmm) return "";
  const p = hhmm.padStart(4, "0");
  let h = parseInt(p.slice(0, 2), 10);
  const m = p.slice(2);
  const ampm = h >= 12 ? "pm" : "am";
  h = h % 12 || 12;
  return `${h}:${m}${ampm}`;
}

function renderMeetings(course) {
  const meetings = course.meetings || [];
  if (!meetings.length) return '<span class="meet-line">TBA</span>';
  return meetings
    .map((m) => {
      if (!m.days && !m.start_time) return `<span class="meet-line">Arranged</span>`;
      const time = m.start_time && m.end_time ? `${timeLabel(m.start_time)}&ndash;${timeLabel(m.end_time)}` : "";
      return `<span class="meet-line">${dayLabel(m.days)} ${time}</span>`;
    })
    .join("");
}

function renderLocations(course) {
  const meetings = course.meetings || [];
  const seen = new Set();
  const out = [];
  for (const m of meetings) {
    const label = m.building && m.room ? `${m.building} ${m.room}` : (m.building_description || m.campus || "");
    if (label && !seen.has(label)) {
      seen.add(label);
      out.push(`<span class="meet-line">${escapeHTML(label)}</span>`);
    }
  }
  return out.join("") || '<span class="meet-line">&mdash;</span>';
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function seatCellHTML(c) {
  const max = c.max_enrollment;
  const enrolled = c.enrollment;
  const avail = c.seats_available;

  if (max === null || max === undefined || max <= 0) {
    return '<span class="seat-text">&mdash;</span>';
  }

  // Prefer computing "filled" from enrollment when we have it; otherwise
  // derive it from max - available seats.
  const filled = enrolled !== null && enrolled !== undefined ? enrolled : max - (avail !== null && avail !== undefined ? avail : 0);
  const pct = Math.max(0, Math.min(100, (filled / max) * 100));

  let barClass = "ok";
  const overCapacity = filled > max;
  const isFull = (avail !== null && avail !== undefined && avail <= 0) || overCapacity;
  if (isFull) barClass = "full";
  else if (pct >= 85) barClass = "tight";

  const availLabel = avail === null || avail === undefined ? "?" : Math.max(avail, 0);
  const estimatedTag = c.seats_estimated ? ' <span class="seat-estimated" title="Computed from enrollment and capacity because Banner didn\u2019t report an open-seat count directly for this section.">est.</span>' : "";
  const unverifiedTag = c.seats_source !== "class_schedule_rendered"
    ? ' <span class="seat-unverified" title="Not yet cross-checked against macadmsys.macalester.edu\u2019s live Class Schedule page -- this section wasn\u2019t matched on the last refresh pass.">unverified</span>'
    : "";
  const tooltip = overCapacity
    ? `${filled}/${max} enrolled (over capacity)`
    : `${filled}/${max} enrolled`;

  return `
    <div class="seat-cell" title="${escapeHTML(tooltip)}">
      <span class="seat-text">${availLabel} open of ${max}${estimatedTag}${unverifiedTag}</span>
      <div class="seat-bar-track"><div class="seat-bar-fill ${barClass}" style="width:${pct}%"></div></div>
    </div>`;
}

function renderResults(courses) {
  const tbody = el("results-body");
  tbody.innerHTML = "";
  const showTermCol = state.selectedTerms.has("all") || state.selectedTerms.size > 1;
  el("term-col-header").hidden = !showTermCol;

  const start = (state.page - 1) * PAGE_SIZE;
  const pageItems = courses.slice(start, start + PAGE_SIZE);

  for (const c of pageItems) {
    const tr = document.createElement("tr");
    if (c.open_section) tr.classList.add("is-open");

    const instructors = (c.faculty || []).map((f) => f.name).filter(Boolean).join(", ") || "\u2014";

    tr.innerHTML = `
      <td>
        <span class="course-code">${escapeHTML(c.subject || "")} ${escapeHTML(c.course_number || "")}-${escapeHTML(c.section || "")}</span>
        <span class="course-sub">CRN ${escapeHTML(c.crn || "")}</span>
      </td>
      <td>${escapeHTML(c.title || "")}</td>
      <td>${renderMeetings(c)}</td>
      <td>${renderLocations(c)}</td>
      <td>${escapeHTML(instructors)}</td>
      <td>${seatCellHTML(c)}</td>
      <td class="term-tag" ${showTermCol ? "" : "hidden"}>${escapeHTML(c.term_description || "")}</td>
    `;
    tbody.appendChild(tr);
  }

  const total = courses.length;
  el("results-count").textContent =
    total === 0 ? "0 sections found" : `${total} section${total === 1 ? "" : "s"} found`;

  el("empty-state").hidden = total !== 0;
  el("results-wrap").style.display = total === 0 ? "none" : "block";

  const totalPages = Math.max(Math.ceil(total / PAGE_SIZE), 1);
  el("pagination").hidden = total <= PAGE_SIZE;
  el("page-indicator").textContent = `Page ${state.page} of ${totalPages}`;
  el("prev-page").disabled = state.page <= 1;
  el("next-page").disabled = state.page >= totalPages;
}

function renderMeta() {
  const subjectList = el("subject-list");
  subjectList.innerHTML = "";
  for (const s of state.meta.subjects) {
    const id = `subj-${s.code}`;
    const label = document.createElement("label");
    label.className = "checkbox-row";
    label.innerHTML = `<input type="checkbox" id="${id}" value="${escapeHTML(s.code)}"> ${escapeHTML(s.code)} &mdash; ${escapeHTML(s.description)}`;
    const input = label.querySelector("input");
    input.checked = state.selectedSubjects.has(s.code);
    input.addEventListener("change", () => {
      if (input.checked) state.selectedSubjects.add(s.code);
      else state.selectedSubjects.delete(s.code);
      state.page = 1;
      refresh();
    });
    subjectList.appendChild(label);
  }

  const instrList = el("instructor-list");
  instrList.innerHTML = state.meta.instructors.map((n) => `<option value="${escapeHTML(n)}">`).join("");

  const dayContainer = el("day-toggles");
  dayContainer.innerHTML = "";
  for (const pattern of state.meta.dayPatterns) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "day-btn";
    btn.textContent = pattern;
    if (state.selectedDays.has(pattern)) btn.classList.add("active");
    btn.addEventListener("click", () => {
      btn.classList.toggle("active");
      if (state.selectedDays.has(pattern)) state.selectedDays.delete(pattern);
      else state.selectedDays.add(pattern);
      state.page = 1;
      refresh();
    });
    dayContainer.appendChild(btn);
  }
}

// Rough day-of-week order, used only to sort pattern buttons sensibly (MWF before TR before Sa).
const DAY_ORDER = "MTWRFSU";
function dayPatternSortKey(pattern) {
  return [...pattern].map((c) => DAY_ORDER.indexOf(c)).join("");
}

function computeMeta(courses) {
  const subjects = new Map();
  const instructors = new Set();
  const dayPatterns = new Set();
  for (const c of courses) {
    if (c.subject) subjects.set(c.subject, c.subject_description || c.subject);
    for (const f of c.faculty || []) if (f.name) instructors.add(f.name);
    for (const m of c.meetings || []) if (m.days) dayPatterns.add(m.days);
  }
  state.meta = {
    subjects: [...subjects.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([code, description]) => ({ code, description })),
    instructors: [...instructors].sort(),
    dayPatterns: [...dayPatterns].sort((a, b) => {
      if (a.length !== b.length) return a.length - b.length;
      return dayPatternSortKey(a).localeCompare(dayPatternSortKey(b));
    }),
  };
}

function termSelectionLabel() {
  if (state.selectedTerms.has("all")) return "All semesters";
  if (state.selectedTerms.size === 0) return "Select a term";
  if (state.selectedTerms.size === 1) {
    const code = [...state.selectedTerms][0];
    const t = state.termsIndex.terms.find((x) => x.code === code);
    return t ? t.description : code;
  }
  return `${state.selectedTerms.size} semesters`;
}

function updateTermToggleLabel() {
  el("term-toggle-label").textContent = termSelectionLabel();
}

function renderTermPanel() {
  const allBox = el("term-all");
  allBox.checked = state.selectedTerms.has("all");

  const list = el("term-checklist");
  list.innerHTML = "";
  for (const t of state.termsIndex.terms) {
    const label = document.createElement("label");
    label.className = "checkbox-row";
    label.innerHTML = `<input type="checkbox" value="${escapeHTML(t.code)}"> ${escapeHTML(t.description)}`;
    const input = label.querySelector("input");
    input.checked = state.selectedTerms.has(t.code);
    input.addEventListener("change", async () => {
      state.selectedTerms.delete("all"); // picking a specific term always overrides "all"
      if (input.checked) state.selectedTerms.add(t.code);
      else state.selectedTerms.delete(t.code);
      if (state.selectedTerms.size === 0) state.selectedTerms.add("all"); // never leave it empty
      updateTermToggleLabel();
      renderTermPanel();
      await onTermSelectionChange();
    });
    list.appendChild(label);
  }
}

async function onTermAllToggle() {
  const allBox = el("term-all");
  if (allBox.checked) {
    state.selectedTerms = new Set(["all"]);
  } else {
    // Falling back to the current/upcoming term rather than leaving nothing selected.
    state.selectedTerms = new Set([state.termsIndex.current_term_code].filter(Boolean));
    if (state.selectedTerms.size === 0) state.selectedTerms = new Set(["all"]);
  }
  updateTermToggleLabel();
  renderTermPanel();
  await onTermSelectionChange();
}

function formatUpdatedAt(isoString) {
  if (!isoString) return "";
  try {
    const d = new Date(isoString);
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch (e) {
    return "";
  }
}

function updateLastUpdatedFooter() {
  const stampEl = el("last-updated");
  const codes = activeTermCodes().filter((c) => state.termCache.has(c));

  if (codes.length === 0) {
    stampEl.textContent = "";
    return;
  }

  // Show the OLDEST relevant timestamp across whatever's selected, so a
  // multi-term view doesn't hide one stale term behind a fresher one.
  // Prefer each term's seats_refreshed_at (when the Class Schedule live
  // pass touched it) over its original scraped_at, since seat freshness
  // is usually the thing that matters most.
  let oldest = null;
  for (const code of codes) {
    const d = state.termCache.get(code);
    const stamp = d.seats_refreshed_at || d.scraped_at;
    if (!stamp) continue;
    if (!oldest || new Date(stamp) < new Date(oldest)) oldest = stamp;
  }
  if (!oldest) oldest = state.termsIndex.generated_at;

  const formatted = formatUpdatedAt(oldest);
  stampEl.textContent = formatted ? `data as of ${formatted}` : "";
}

// ---------------- orchestration ----------------

async function refresh() {
  const loading = el("loading-state");
  const wrap = el("results-wrap");

  loading.hidden = false;
  el("no-data-state").hidden = true;
  el("empty-state").hidden = true;
  wrap.style.display = "none";

  const courses = await getActiveCourses();
  loading.hidden = true;

  if (!courses.length && activeTermCodes().length === 0) {
    el("no-data-state").hidden = false;
    el("pagination").hidden = true;
    el("results-count").textContent = "No term selected";
    return;
  }

  const filters = readFilters();
  const filterCountEl = el("filter-count");
  const count = activeFilterCount(filters);
  filterCountEl.hidden = count === 0;
  filterCountEl.textContent = count;

  const filtered = sortCourses(courses.filter((c) => courseMatches(c, filters)));
  renderResults(filtered);
  updateLastUpdatedFooter();
}

async function onTermSelectionChange() {
  state.page = 1;
  state.selectedSubjects.clear();
  state.selectedDays.clear();

  const courses = await getActiveCourses();
  computeMeta(courses || []);
  renderMeta();
  await refresh();
}

function wireStaticControls() {
  el("term-toggle").addEventListener("click", () => {
    const panel = el("term-panel");
    const willShow = panel.hidden;
    panel.hidden = !willShow;
    el("term-toggle").setAttribute("aria-expanded", String(willShow));
  });

  document.addEventListener("click", (e) => {
    const picker = document.querySelector(".term-picker");
    if (picker && !picker.contains(e.target)) {
      el("term-panel").hidden = true;
      el("term-toggle").setAttribute("aria-expanded", "false");
    }
  });

  el("term-all").addEventListener("change", onTermAllToggle);

  let debounceTimer;
  const debouncedRefresh = () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      state.page = 1;
      refresh();
    }, 180);
  };

  ["q", "instructor", "crn", "start-after", "end-before"].forEach((id) => {
    el(id).addEventListener("input", debouncedRefresh);
  });
  el("seat-status").addEventListener("change", debouncedRefresh);
  el("regex-mode").addEventListener("change", debouncedRefresh);

  el("toggle-filters").addEventListener("click", () => {
    const panel = el("filters");
    const willShow = panel.hidden;
    panel.hidden = !willShow;
    el("toggle-filters").setAttribute("aria-expanded", String(willShow));
  });

  el("clear-filters").addEventListener("click", () => {
    el("q").value = "";
    el("instructor").value = "";
    el("crn").value = "";
    el("start-after").value = "";
    el("end-before").value = "";
    el("seat-status").value = "";
    el("regex-mode").checked = false;
    state.selectedSubjects.clear();
    state.selectedDays.clear();
    renderMeta();
    state.page = 1;
    refresh();
  });

  el("sort-by").addEventListener("change", () => {
    state.sortBy = el("sort-by").value;
    refresh();
  });

  el("sort-dir").addEventListener("click", () => {
    state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    const btn = el("sort-dir");
    btn.dataset.dir = state.sortDir;
    btn.innerHTML = state.sortDir === "asc" ? "&uarr; Asc" : "&darr; Desc";
    refresh();
  });

  el("prev-page").addEventListener("click", () => {
    if (state.page > 1) { state.page--; refresh(); }
  });
  el("next-page").addEventListener("click", () => {
    state.page++; refresh();
  });
}

async function init() {
  wireStaticControls();
  await loadTermsIndex();

  const initial = state.termsIndex.current_term_code;
  state.selectedTerms = new Set([initial || "all"]);
  updateTermToggleLabel();
  renderTermPanel();

  await onTermSelectionChange();
}

init();
