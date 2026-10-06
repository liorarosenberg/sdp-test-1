/* RAT dashboard — vanilla JS, no build step. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  repos: [],
  repo: null,          // active repo meta
  ref: "HEAD",
  authors: [],         // author options (labels incl. groups)
  authorFilter: [],    // selected labels
  path: "",
  from: "", to: "",    // yyyy-mm-dd or ""
  selected: new Set(), // manually selected commit hashes
  data: null,          // last metrics payload
  tab: "dirs",
  sort: { dirs: { key: "churn", dir: -1 }, files: { key: "churn", dir: -1 }, authors: { key: "churn", dir: -1 } },
  pick: { page: 1, commits: [] },
  charts: {},
};

/* ---------------- helpers ---------------- */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const nf = (n) => Number(n).toLocaleString("en-US");
const pf = (x) => (Number(x) * 100).toFixed(1) + "%";

function toast(msg, kind = "error", ms = 6000) {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}

let loadingCount = 0;
function loading(on, msg = "Working…") {
  loadingCount += on ? 1 : -1;
  if (loadingCount < 0) loadingCount = 0;
  $("loadingMsg").textContent = msg;
  $("loading").classList.toggle("hidden", loadingCount === 0);
}

async function api(url, opts = {}, loadingMsg) {
  loading(true, loadingMsg || "Loading…");
  try {
    const res = await fetch(url, opts);
    let body = null;
    try { body = await res.json(); } catch { /* empty body */ }
    if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
    return body;
  } catch (e) {
    if (e.name === "TypeError") throw new Error("Cannot reach the server — is app.py still running?");
    throw e;
  } finally {
    loading(false);
  }
}

const post = (url, body, msg) => api(url, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
}, msg);

/* UTC helpers: date input "yyyy-mm-dd" -> epoch seconds */
const dayStart = (s) => Math.floor(Date.parse(s + "T00:00:00Z") / 1000);
const dayAfter = (s) => Math.floor(Date.parse(s + "T00:00:00Z") / 1000) + 86400;
const fmtDate = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);
const short7 = (h) => h.slice(0, 7);

/* ---------------- repositories ---------------- */

async function refreshRepos(selectId) {
  state.repos = await api("/api/repos", {}, "Loading repositories…");
  const sel = $("repoSelect");
  sel.innerHTML = state.repos.length
    ? state.repos.map(r => `<option value="${esc(r.id)}">${esc(r.name)} (${nf(r.stats?.HEAD?.commits ?? "?")} commits)</option>`).join("")
    : `<option value="">No repositories yet</option>`;
  if (selectId && state.repos.some(r => r.id === selectId)) sel.value = selectId;
  state.repo = state.repos.find(r => r.id === sel.value) || null;
  updateView();
}

function updateView() {
  const has = !!state.repo;
  $("hero").classList.toggle("hidden", has);
  $("dashboard").classList.toggle("hidden", !has);
  if (has) {
    loadAuthors().then(applyFilters).catch(e => toast(e.message));
  }
}

async function loadAuthors() {
  state.authors = await api(`/api/repos/${state.repo.id}/authors?ref=${encodeURIComponent(state.ref)}`);
  const prev = new Set(state.authorFilter);
  $("authorList").innerHTML = state.authors.map(a =>
    `<label class="ms-item" title="${esc(a.label)}">` +
    `<input type="checkbox" value="${esc(a.label)}" ${prev.has(a.label) ? "checked" : ""}>` +
    `<span class="ms-name">${esc(a.display)}${a.merged ? ' <span class="pill">merged</span>' : ""}</span>` +
    `<span class="muted small">${nf(a.commits)} commits</span></label>`).join("");
  // keep only selections that still exist
  state.authorFilter = [...prev].filter(l => state.authors.some(a => a.label === l));
  [...$("authorList").querySelectorAll("input:checked")].forEach(i => { if (!state.authorFilter.includes(i.value)) i.checked = false; });
  updateAuthorToggle();
  // merge modal table
  $("authorTable").innerHTML = `<thead><tr><th></th><th>Author</th><th>Identities</th><th>Commits</th><th>Churn</th></tr></thead><tbody>` +
    state.authors.map((a, i) =>
      `<tr><td><input type="checkbox" class="a-check" data-i="${i}"></td>` +
      `<td>${esc(a.display)}${a.merged ? ' <span class="pill">merged</span>' : ""}</td>` +
      `<td>${a.ids.length}</td><td>${nf(a.commits)}</td><td>${nf(a.churn)}</td></tr>`).join("") +
    `</tbody>`;
  const groups = Object.entries(state.repo.author_groups || {});
  $("groupsList").innerHTML = groups.length
    ? `<div class="muted small">Manual groups: ` + groups.map(([n]) =>
        `${esc(n)} <button class="chip unmerge" data-name="${esc(n)}">unmerge</button>`).join(" ") + `</div>`
    : "";
}

function updateAuthorToggle() {
  const n = state.authorFilter.length;
  const t = $("authorToggle");
  if (!n) { t.textContent = "All authors ▾"; return; }
  const names = state.authorFilter
    .map(l => (state.authors.find(a => a.label === l) || { display: l }).display);
  t.textContent = (names.length <= 2 ? names.join(", ") : `${names.length} authors selected`) + " ▾";
}

/* ---------------- metrics ---------------- */

function currentFilterBody() {
  const body = { ref: state.ref };
  if (state.authorFilter.length) body.authors = state.authorFilter;
  if (state.path.trim()) body.path = state.path.trim();
  if (state.selected.size) {
    body.commits = [...state.selected];
  } else {
    if (state.from) body.from = dayStart(state.from);
    if (state.to) body.to = dayAfter(state.to); // inclusive end-of-day (half-open interval)
  }
  return body;
}

async function applyFilters() {
  if (!state.repo) return;
  try {
    state.data = await post(`/api/repos/${state.repo.id}/metrics`, currentFilterBody(), "Computing metrics…");
    render();
  } catch (e) {
    toast(e.message);
  }
}

const debouncedApply = (() => { let t; return () => { clearTimeout(t); t = setTimeout(applyFilters, 450); }; })();

/* ---------------- rendering ---------------- */

function render() {
  renderCards();
  renderCharts();
  renderTable();
  renderScope();
  // path autocomplete suggestions: directories + (first 300) files
  $("pathSuggestions").innerHTML =
    state.data.dirs.map(d => `<option value="${esc(d.path === "(root)" ? "" : d.path)}">`).join("") +
    state.data.files.slice(0, 300).map(f => `<option value="${esc(f.path)}">`).join("");
}

function renderCards() {
  const d = state.data, s = d.summary, cs = d.commitSet;
  const cards = [
    ["Commits in set", nf(cs.size), `of ${nf(cs.totalInRef)} non-merge commits @ ${esc(d.repo.ref)}`],
    ["Added lines", nf(s.added), "in selected objects"],
    ["Removed lines", nf(s.removed), "in selected objects"],
    ["Growth", nf(s.growth), "added − removed"],
    ["Churn", nf(s.churn), "added + removed"],
    ["Modifications", nf(s.modifications), "commits touching the object"],
    ["Mod frequency", pf(s.modFrequency), "modifications / |H|"],
    ["Churn rate", nf(Math.round(s.churnRate * 100) / 100), "churn / |H|"],
  ];
  $("cards").innerHTML = cards.map(([label, val, sub]) =>
    `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value">${val}</div><div class="stat-sub">${sub}</div></div>`).join("");
}

function renderScope() {
  const d = state.data;
  const bits = [];
  if (d.commitSet.path) bits.push(`scope: ${esc(d.commitSet.path)}`);
  if (d.commitSet.authors) bits.push(`authors: ${d.commitSet.authors.length}`);
  if (d.commitSet.manualCount != null) bits.push(`manual selection: ${nf(d.commitSet.manualCount)} commits`);
  if (state.from || state.to) bits.push(`period: ${state.from || "…"} → ${state.to || "…"}`);
  $("scopeInfo").textContent = bits.join(" · ");
  $("tableNote").textContent = d.filesTotal > d.files.length
    ? `Showing top ${d.files.length} of ${nf(d.filesTotal)} files by churn — use the path filter to narrow down.`
    : "";
}

/* ---- tables ---- */

const COLS = {
  dirs: [
    ["path", "Path", false], ["added", "Added"], ["removed", "Removed"], ["growth", "Growth"],
    ["churn", "Churn"], ["modifications", "Mods"], ["modFrequency", "Mod freq"], ["churnRate", "Churn rate"],
  ],
  files: [
    ["path", "File", false], ["added", "Added"], ["removed", "Removed"], ["growth", "Growth"],
    ["churn", "Churn"], ["modifications", "Mods"], ["modFrequency", "Mod freq"], ["churnRate", "Churn rate"],
  ],
  authors: [
    ["author", "Author", false], ["commits", "Commits"], ["added", "Added"], ["removed", "Removed"],
    ["churn", "Churn"], ["modifications", "Mods"], ["ownership", "Ownership"],
  ],
};

function renderTable() {
  const tab = state.tab;
  const rows = [...state.data[tab]];
  const { key, dir } = state.sort[tab];
  rows.sort((a, b) => {
    const va = a[key], vb = b[key];
    if (typeof va === "string") return dir * va.localeCompare(vb);
    return dir * ((va ?? 0) - (vb ?? 0));
  });
  const cols = COLS[tab];
  const head = `<thead><tr>` + cols.map(([k, label, sortable = true]) =>
    `<th data-key="${k}" class="${sortable ? "sortable" : ""} ${k === key ? (dir < 0 ? "desc" : "asc") : ""}">${label}${k === key ? (dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("") + `</tr></thead>`;
  const body = `<tbody>` + rows.map(r => {
    if (tab === "authors") {
      return `<tr><td>${esc(r.author)}</td><td>${nf(r.commits)}</td><td>${nf(r.added)}</td><td>${nf(r.removed)}</td><td>${nf(r.churn)}</td><td>${nf(r.modifications)}</td><td>${pf(r.ownership)}</td></tr>`;
    }
    const isRoot = r.path === "(root)";
    const clickable = tab === "dirs" && !isRoot;
    return `<tr class="${clickable ? "clickable" : ""}" ${clickable ? `data-path="${esc(r.path)}"` : ""}>` +
      `<td class="path">${esc(r.path)}${isRoot ? ' <span class="pill">repository metrics</span>' : ""}</td>` +
      `<td>${nf(r.added)}</td><td>${nf(r.removed)}</td><td>${nf(r.growth)}</td><td>${nf(r.churn)}</td>` +
      `<td>${nf(r.modifications)}</td><td>${pf(r.modFrequency)}</td><td>${nf(Math.round(r.churnRate * 100) / 100)}</td></tr>`;
  }).join("") + `</tbody>`;
  $("tbl").innerHTML = head + body;
  if (!rows.length) $("tbl").innerHTML += `<tbody><tr><td class="empty">No data for the current filters.</td></tr></tbody>`;
}

/* ---- charts ---- */

function chartColors(n) {
  const base = ["#4f8ef7", "#2ecc71", "#e67e22", "#9b59b6", "#f1c40f", "#1abc9c", "#e74c3c", "#3498db"];
  return Array.from({ length: n }, (_, i) => base[i % base.length]);
}

function renderCharts() {
  if (window.__noChart || typeof Chart === "undefined") {
    $("chartsCard").innerHTML = `<div class="card-title">Visualisation</div>
      <p class="muted small">Charts unavailable (no network for the Chart.js CDN) — all metrics remain
      fully available in the tables below.</p>`;
    return;
  }
  Object.values(state.charts).forEach(c => c.destroy());
  state.charts = {};
  Chart.defaults.color = "#9aa4b2";
  Chart.defaults.borderColor = "rgba(255,255,255,.07)";

  const ts = state.data.timeseries;
  state.charts.time = new Chart($("chTime"), {
    type: "line",
    data: {
      labels: ts.map(t => t.month),
      datasets: [
        { label: "Added", data: ts.map(t => t.added), borderColor: "#2ecc71", backgroundColor: "rgba(46,204,113,.12)", fill: true, tension: .25, pointRadius: 0 },
        { label: "Removed", data: ts.map(t => t.removed), borderColor: "#e74c3c", backgroundColor: "rgba(231,76,60,.10)", fill: true, tension: .25, pointRadius: 0 },
        { label: "Churn", data: ts.map(t => t.churn), borderColor: "#4f8ef7", tension: .25, pointRadius: 0 },
      ],
    },
    options: { plugins: { title: { display: true, text: "Activity over time (committer month)" } }, scales: { y: { beginAtZero: true } }, maintainAspectRatio: false },
  });

  const files = state.data.files.slice(0, 10);
  state.charts.files = new Chart($("chFiles"), {
    type: "bar",
    data: {
      labels: files.map(f => f.path.split("/").pop()),
      datasets: [{ label: "Churn", data: files.map(f => f.churn), backgroundColor: chartColors(files.length) }],
    },
    options: { indexAxis: "y", plugins: { title: { display: true, text: "Top files by churn" }, legend: { display: false } }, maintainAspectRatio: false },
  });

  const authors = state.data.authors.slice(0, 8);
  const rest = state.data.authors.slice(8).reduce((s, a) => s + a.churn, 0);
  const labels = authors.map(a => a.author).concat(rest > 0 ? ["(others)"] : []);
  const vals = authors.map(a => a.churn).concat(rest > 0 ? [rest] : []);
  state.charts.authors = new Chart($("chAuthors"), {
    type: "doughnut",
    data: { labels, datasets: [{ data: vals, backgroundColor: chartColors(labels.length) }] },
    options: { plugins: { title: { display: true, text: "Churn share by author" } }, maintainAspectRatio: false },
  });
}

/* ---------------- commit picker ---------------- */

async function openPicker() {
  $("modalCommits").classList.remove("hidden");
  state.pick.page = 1;
  await loadPickPage();
}

async function loadPickPage() {
  const q = $("pickSearch").value.trim();
  const from = $("pickFrom").value, to = $("pickTo").value;
  const body = await api(`/api/repos/${state.repo.id}/commits?ref=${encodeURIComponent(state.ref)}` +
    `&page=${state.pick.page}&per_page=100` +
    (q ? `&q=${encodeURIComponent(q)}` : "") +
    (from ? `&from=${dayStart(from)}` : "") +
    (to ? `&to=${dayAfter(to)}` : ""));
  state.pick.commits = body.commits;
  const rows = body.commits.map(c =>
    `<tr><td><input type="checkbox" class="pick-check" data-h="${c.h}" ${state.selected.has(c.h) ? "checked" : ""}></td>` +
    `<td class="mono">${short7(c.h)}</td><td>${new Date(c.ts * 1000).toISOString().slice(0, 16).replace("T", " ")}</td>` +
    `<td>${esc(c.author)}</td><td>${esc(c.subject)}</td></tr>`).join("");
  $("pickTable").innerHTML = `<thead><tr><th></th><th>Hash</th><th>Date (UTC)</th><th>Author</th><th>Subject</th></tr></thead><tbody>${rows}</tbody>`;
  const pages = Math.max(1, Math.ceil(body.total / body.perPage));
  $("pickInfo").textContent = `${nf(body.total)} commits — page ${body.page}/${pages}`;
  $("btnPickPage").disabled = body.page >= pages;
  updatePickCount();
}

function updatePickCount() {
  $("pickCount").textContent = `${nf(state.selected.size)} selected`;
  const has = state.selected.size > 0;
  $("selectionInfo").classList.toggle("hidden", !has);
  $("btnClearSelection").classList.toggle("hidden", !has);
  $("selectionInfo").textContent = `${nf(state.selected.size)} commits manually selected`;
}

/* ---------------- CSV export ---------------- */

function exportCsv() {
  const tab = state.tab;
  const cols = COLS[tab];
  const rows = state.data[tab];
  const cell = (v) => `"${String(v).replace(/"/g, '""')}"`;
  const csv = [cols.map(c => cell(c[1])).join(",")]
    .concat(rows.map(r => cols.map(([k]) => cell(r[k])).join(","))).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = `rat-${state.repo.name}-${tab}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ---------------- add-repo modal ---------------- */

function openAddRepo() { $("modalRepo").classList.remove("hidden"); $("repoStatus").textContent = ""; }

async function doUpload() {
  const f = $("zipFile").files[0];
  if (!f) return toast("Choose a .zip file first");
  const fd = new FormData();
  fd.append("file", f);
  $("repoStatus").textContent = "Uploading…";
  const btn = $("btnUpload"); btn.disabled = true;
  try {
    const meta = await api("/api/repos/upload", { method: "POST", body: fd }, "Extracting & analysing zip…");
    $("modalRepo").classList.add("hidden");
    $("zipFile").value = "";
    await refreshRepos(meta.id);
    toast(`Repository "${meta.name}" added`, "ok");
  } catch (e) {
    $("repoStatus").textContent = "";
    toast(e.message);
  } finally { btn.disabled = false; }
}

async function doClone() {
  const url = $("cloneUrl").value.trim();
  if (!url) return toast("Enter a repository URL");
  const btn = $("btnClone"); btn.disabled = true;
  $("repoStatus").textContent = "Cloning (full history) — this can take a while for large repositories…";
  try {
    const meta = await post("/api/repos/clone", { url }, "Cloning repository…");
    $("modalRepo").classList.add("hidden");
    $("cloneUrl").value = "";
    await refreshRepos(meta.id);
    toast(`Repository "${meta.name}" cloned`, "ok");
  } catch (e) {
    $("repoStatus").textContent = "";
    toast(e.message);
  } finally { btn.disabled = false; }
}

/* ---------------- events ---------------- */

function wire() {
  $("repoSelect").addEventListener("change", async () => {
    state.repo = state.repos.find(r => r.id === $("repoSelect").value) || null;
    state.ref = "HEAD"; $("refInput").value = "HEAD";
    state.authorFilter = []; state.selected.clear(); state.path = ""; $("pathFilter").value = "";
    state.from = state.to = ""; $("dateFrom").value = $("dateTo").value = "";
    updatePickCount();
    updateView();
  });

  $("refInput").addEventListener("change", () => {
    state.ref = $("refInput").value.trim() || "HEAD";
    state.selected.clear(); updatePickCount();
    state.authorFilter = [];
    loadAuthors().then(applyFilters).catch(e => toast(e.message));
  });

  $("authorToggle").addEventListener("click", (e) => {
    e.stopPropagation();
    $("authorPanel").classList.toggle("hidden");
  });
  $("authorPanel").addEventListener("click", e => e.stopPropagation());
  $("authorList").addEventListener("change", () => {
    state.authorFilter = [...$("authorList").querySelectorAll("input:checked")].map(i => i.value);
    updateAuthorToggle();
    applyFilters();
  });
  $("authorClear").addEventListener("click", () => {
    $("authorList").querySelectorAll("input:checked").forEach(i => { i.checked = false; });
    state.authorFilter = [];
    updateAuthorToggle();
    applyFilters();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#authorMulti")) $("authorPanel").classList.add("hidden");
  });
  $("pathFilter").addEventListener("input", () => { state.path = $("pathFilter").value; debouncedApply(); });
  $("dateFrom").addEventListener("change", () => { state.from = $("dateFrom").value; applyFilters(); });
  $("dateTo").addEventListener("change", () => { state.to = $("dateTo").value; applyFilters(); });

  document.querySelectorAll(".chip[data-days]").forEach(b => b.addEventListener("click", () => {
    const days = Number(b.dataset.days);
    const now = new Date();
    const cut = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - days * 86400000);
    state.from = days ? cut.toISOString().slice(0, 10) : "";
    state.to = "";
    $("dateFrom").value = state.from; $("dateTo").value = "";
    applyFilters();
  }));

  $("btnApply").addEventListener("click", applyFilters);
  $("btnAddRepo").addEventListener("click", openAddRepo);
  $("btnUpload").addEventListener("click", doUpload);
  $("btnClone").addEventListener("click", doClone);
  document.querySelectorAll(".chip.sample").forEach(b => b.addEventListener("click", () => { $("cloneUrl").value = b.dataset.url; }));
  document.querySelectorAll("[data-rtab]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("[data-rtab]").forEach(x => x.classList.toggle("active", x === b));
    $("rtab-upload").classList.toggle("hidden", b.dataset.rtab !== "upload");
    $("rtab-clone").classList.toggle("hidden", b.dataset.rtab !== "clone");
  }));

  $("btnPickCommits").addEventListener("click", openPicker);
  $("btnPickSearch").addEventListener("click", () => { state.pick.page = 1; loadPickPage(); });
  $("pickSearch").addEventListener("keydown", e => { if (e.key === "Enter") { state.pick.page = 1; loadPickPage(); } });
  $("pickTable").addEventListener("change", e => {
    const cb = e.target.closest(".pick-check");
    if (!cb) return;
    if (cb.checked) state.selected.add(cb.dataset.h); else state.selected.delete(cb.dataset.h);
    updatePickCount();
  });
  $("btnPickPage").addEventListener("click", () => { state.pick.commits.forEach(c => state.selected.add(c.h)); updatePickCount(); loadPickPage(); });
  $("btnPickClear").addEventListener("click", () => { state.selected.clear(); updatePickCount(); loadPickPage(); });
  $("btnPickDone").addEventListener("click", () => { $("modalCommits").classList.add("hidden"); applyFilters(); });
  $("btnClearSelection").addEventListener("click", () => { state.selected.clear(); updatePickCount(); applyFilters(); });

  $("btnAuthors").addEventListener("click", async () => {
    await loadAuthors().catch(e => toast(e.message));
    $("modalAuthors").classList.remove("hidden");
  });
  $("btnMerge").addEventListener("click", async () => {
    const ids = [...document.querySelectorAll(".a-check:checked")]
      .map(cb => state.authors[Number(cb.dataset.i)]).flatMap(a => a.ids);
    const name = $("mergeName").value.trim();
    if (ids.length < 2) return toast("Tick at least two author rows to merge");
    if (!name) return toast("Give the merged group a name");
    try {
      state.authors = await post(`/api/repos/${state.repo.id}/authors/merge`, { ids, name, ref: state.ref });
      $("mergeName").value = "";
      await refreshRepos(state.repo.id);
      toast(`Merged ${ids.length} identities into "${name}"`, "ok");
    } catch (e) { toast(e.message); }
  });
  $("groupsList").addEventListener("click", async (e) => {
    const b = e.target.closest(".unmerge");
    if (!b) return;
    try {
      state.authors = await post(`/api/repos/${state.repo.id}/authors/unmerge`, { name: b.dataset.name, ref: state.ref });
      await refreshRepos(state.repo.id);
      toast("Group removed", "ok");
    } catch (err) { toast(err.message); }
  });

  document.querySelectorAll(".tab[data-tab]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(".tab[data-tab]").forEach(x => x.classList.toggle("active", x === b));
    state.tab = b.dataset.tab;
    renderTable();
  }));

  $("tbl").addEventListener("click", (e) => {
    const th = e.target.closest("th.sortable");
    if (th) {
      const s = state.sort[state.tab];
      if (s.key === th.dataset.key) s.dir *= -1; else { s.key = th.dataset.key; s.dir = -1; }
      renderTable();
      return;
    }
    const tr = e.target.closest("tr[data-path]");
    if (tr) {
      state.path = tr.dataset.path;
      $("pathFilter").value = state.path;
      applyFilters();
    }
  });
  $("btnCsv").addEventListener("click", exportCsv);

  document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => $(b.dataset.close).classList.add("hidden")));
  document.querySelectorAll(".modal").forEach(m => m.addEventListener("mousedown", e => { if (e.target === m) m.classList.add("hidden"); }));
}

/* ---------------- init ---------------- */

wire();
refreshRepos().catch(e => toast(e.message));
