// Macalester Schedule Search — fully static, client-side.
// Loads data/terms.json + data/<term>.json (produced by scraper.py via the
// GitHub Actions workflow) and does all searching/filtering/sorting here in
// the browser, so the whole thing can live on GitHub Pages with no backend.

const DATA_BASE = "data";
const PAGE_SIZE = 50;

const state = {
  termsIndex: { current_term_code: null, terms: [] },
  termCache: new Map(), // code -> {code, description, courses}
  selectedTerm: null,   // a term code, or "all"
  meta: { subjects: [], instructors: [], campuses: [], scheduleTypes: [] },
  selectedSubjects: new Set(),
  selectedDays: new Set(),
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

/** Load whatever term(s) the current selection needs, returning a flat array of course records. */
async function getActiveCourses() {
  if (state.selectedTerm === "all") {
    const scraped = state.termsIndex.terms.filter((t) => t.scraped);
    const loaded = await Promise.all(scraped.map((t) => loadTermData(t.code)));
    return loaded.filter(Boolean).flatMap((d) => d.courses);
  }
  const data = await loadTermData(state.selectedTerm);
  return data ? data.courses : null; // null => this specific term has no data file
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

function readFilters() {
  const startAfter = el("start-after").value; // "HH:MM" or ""
  const endBefore = el("end-before").value;
  return {
    q: el("q").value.trim().toLowerCase(),
    subjects: state.selectedSubjects,
    instructor: el("instructor").value.trim().toLowerCase(),
    crn: el("crn").value.trim(),
    campus: el("campus").value,
    scheduleType: el("schedule-type").value,
    openOnly: el("open-only").checked,
    days: state.selectedDays,
    creditsMin: el("credits-min").value ? parseFloat(el("credits-min").value) : null,
    creditsMax: el("credits-max").value ? parseFloat(el("credits-max").value) : null,
    startAfterMin: startAfter ? parseInt(startAfter.split(":")[0], 10) * 60 + parseInt(startAfter.split(":")[1], 10) : null,
    endBeforeMin: endBefore ? parseInt(endBefore.split(":")[0], 10) * 60 + parseInt(endBefore.split(":")[1], 10) : null,
  };
}

function activeFilterCount(f) {
  let n = 0;
  if (f.q) n++;
  if (f.subjects.size) n++;
  if (f.instructor) n++;
  if (f.crn) n++;
  if (f.campus) n++;
  if (f.scheduleType) n++;
  if (f.openOnly) n++;
  if (f.days.size) n++;
  if (f.creditsMin !== null) n++;
  if (f.creditsMax !== null) n++;
  if (f.startAfterMin !== null) n++;
  if (f.endBeforeMin !== null) n++;
  return n;
}

function courseMatches(c, f) {
  if (f.q) {
    const haystack = [
      c.subject, c.subject_description, c.course_number, c.title, c.crn,
      ...(c.faculty || []).map((x) => x.name || ""),
    ].filter(Boolean).join(" ").toLowerCase();
    if (!haystack.includes(f.q)) return false;
  }

  if (f.subjects.size && !f.subjects.has(c.subject)) return false;

  if (f.instructor) {
    const names = (c.faculty || []).map((x) => x.name || "").join(" ").toLowerCase();
    if (!names.includes(f.instructor)) return false;
  }

  if (f.crn && !(c.crn || "").includes(f.crn)) return false;
  if (f.campus && c.campus !== f.campus) return false;
  if (f.scheduleType && c.schedule_type !== f.scheduleType) return false;
  if (f.openOnly && !c.open_section) return false;

  if (f.creditsMin !== null && (c.credit_hours === null || c.credit_hours === undefined || c.credit_hours < f.creditsMin)) return false;
  if (f.creditsMax !== null && (c.credit_hours === null || c.credit_hours === undefined || c.credit_hours > f.creditsMax)) return false;

  if (f.days.size) {
    const meetings = c.meetings || [];
    const hit = meetings.some((m) => [...f.days].some((d) => (m.days || "").includes(d)));
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

function sortCourses(courses) {
  const dir = state.sortDir === "desc" ? -1 : 1;
  const keyFn = {
    title: (c) => c.title || "",
    instructor: (c) => (c.faculty && c.faculty[0] && c.faculty[0].name) || "",
    credits: (c) => (c.credit_hours != null ? c.credit_hours : 0),
    seats: (c) => {
      if (c.seats_available !== null && c.seats_available !== undefined) return c.seats_available;
      if (c.max_enrollment != null && c.enrollment != null) return c.max_enrollment - c.enrollment;
      return -999;
    },
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
  const estimatedTag = c.seats_estimated ? ' <span class="seat-estimated" title="Computed from enrollment/capacity -- Macalester\u2019s guest search does not always report live open-seat counts directly.">est.</span>' : "";
  const tooltip = overCapacity
    ? `${filled}/${max} enrolled (over capacity)`
    : `${filled}/${max} enrolled`;

  return `
    <div class="seat-cell" title="${escapeHTML(tooltip)}">
      <span class="seat-text">${availLabel} open of ${max}${estimatedTag}</span>
      <div class="seat-bar-track"><div class="seat-bar-fill ${barClass}" style="width:${pct}%"></div></div>
    </div>`;
}

function renderResults(courses) {
  const tbody = el("results-body");
  tbody.innerHTML = "";
  const showTermCol = state.selectedTerm === "all";
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
      <td>${c.credit_hours != null ? c.credit_hours : "&mdash;"}</td>
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

  const campusSel = el("campus");
  campusSel.innerHTML =
    `<option value="">Any campus</option>` +
    state.meta.campuses.map((c) => `<option value="${escapeHTML(c)}">${escapeHTML(c)}</option>`).join("");

  const typeSel = el("schedule-type");
  typeSel.innerHTML =
    `<option value="">Any type</option>` +
    state.meta.scheduleTypes.map((t) => `<option value="${escapeHTML(t)}">${escapeHTML(t)}</option>`).join("");
}

function computeMeta(courses) {
  const subjects = new Map();
  const instructors = new Set();
  const campuses = new Set();
  const scheduleTypes = new Set();
  for (const c of courses) {
    if (c.subject) subjects.set(c.subject, c.subject_description || c.subject);
    for (const f of c.faculty || []) if (f.name) instructors.add(f.name);
    if (c.campus) campuses.add(c.campus);
    if (c.schedule_type) scheduleTypes.add(c.schedule_type);
  }
  state.meta = {
    subjects: [...subjects.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([code, description]) => ({ code, description })),
    instructors: [...instructors].sort(),
    campuses: [...campuses].sort(),
    scheduleTypes: [...scheduleTypes].sort(),
  };
}

function renderTermSelect() {
  const sel = el("term-select");
  sel.innerHTML = "";

  const allOpt = document.createElement("option");
  allOpt.value = "all";
  allOpt.textContent = "All semesters";
  sel.appendChild(allOpt);

  for (const t of state.termsIndex.terms) {
    const opt = document.createElement("option");
    opt.value = t.code;
    opt.textContent = t.scraped ? t.description : `${t.description} (no data yet)`;
    if (!t.scraped) opt.disabled = true;
    sel.appendChild(opt);
  }

  sel.value = state.selectedTerm;
}

// ---------------- orchestration ----------------

async function refresh() {
  const nodata = el("no-data-state");
  const loading = el("loading-state");
  const wrap = el("results-wrap");

  loading.hidden = false;
  nodata.hidden = true;
  el("empty-state").hidden = true;
  wrap.style.display = "none";

  const courses = await getActiveCourses();
  loading.hidden = true;

  if (courses === null) {
    nodata.hidden = false;
    el("pagination").hidden = true;
    el("results-count").textContent = "No data for this term";
    return;
  }

  const filters = readFilters();
  const filterCountEl = el("filter-count");
  const count = activeFilterCount(filters);
  filterCountEl.hidden = count === 0;
  filterCountEl.textContent = count;

  const filtered = sortCourses(courses.filter((c) => courseMatches(c, filters)));
  renderResults(filtered);
}

async function onTermChange() {
  state.selectedTerm = el("term-select").value;
  state.page = 1;
  state.selectedSubjects.clear();

  const courses = await getActiveCourses();
  computeMeta(courses || []);
  renderMeta();
  await refresh();
}

function wireStaticControls() {
  el("term-select").addEventListener("change", onTermChange);

  let debounceTimer;
  const debouncedRefresh = () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      state.page = 1;
      refresh();
    }, 180);
  };

  ["q", "instructor", "crn", "credits-min", "credits-max", "start-after", "end-before"].forEach((id) => {
    el(id).addEventListener("input", debouncedRefresh);
  });
  ["campus", "schedule-type"].forEach((id) => {
    el(id).addEventListener("change", debouncedRefresh);
  });
  el("open-only").addEventListener("change", debouncedRefresh);

  document.querySelectorAll(".day-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const day = btn.dataset.day;
      btn.classList.toggle("active");
      if (state.selectedDays.has(day)) state.selectedDays.delete(day);
      else state.selectedDays.add(day);
      state.page = 1;
      refresh();
    });
  });

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
    el("credits-min").value = "";
    el("credits-max").value = "";
    el("start-after").value = "";
    el("end-before").value = "";
    el("open-only").checked = false;
    el("campus").value = "";
    el("schedule-type").value = "";
    state.selectedSubjects.clear();
    state.selectedDays.clear();
    document.querySelectorAll(".day-btn.active").forEach((b) => b.classList.remove("active"));
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

  state.selectedTerm = state.termsIndex.current_term_code || "all";
  renderTermSelect();

  await onTermChange();
}

init();
