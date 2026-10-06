# RAT — Repo Analysis Tool

A web-app dashboard that measures git repository metrics **per file, per directory, per
repository, per commit set and per author**, for **multiple repositories**, ingestible
either as a **zip upload** (including the `.git` folder) or by **deep-cloning a remote URL**.

Built for the COMS3011A test brief.

---

## Quick start

### Prerequisites
- **Python 3.10+** (tested on 3.12) — https://www.python.org/
- **git** on your PATH (tested with 2.43) — https://git-scm.com/
- Internet access (only needed for cloning remote repositories; the app itself runs offline)

### Install & run (from a fresh clone)

```bash
git clone <this-repository-url>
cd sdp-test-1
python3 -m pip install -r requirements.txt
python3 app.py
```

Then open **http://localhost:5000** in your browser.

That's it — one dependency (Flask), no database, no build step.

> If port 5000 is busy: edit the last line of `app.py` (`port=5000`) and restart.

---

## Using the tool

1. **Add a repository** — `+ Add repo` button:
   - **Upload zip**: a `.zip` of a git repository *including its `.git` directory*
     (e.g. zip the folder itself — `git archive` does *not* include `.git`).
   - **Clone URL**: the repo is deep-cloned (full history). One-click presets for the
     brief's sample repositories (cJSON / Redis / Git) are included.
2. **Filter the dashboard**:
   - **Repository** — top-left dropdown (multiple repositories supported, delete via API or the `data/repos` folder)
   - **Author** — checkbox dropdown (`.mailmap` is applied automatically; manual merging via the `Authors` button)
   - **File or directory** — text field with autocomplete, or click any directory row in the table to drill in
   - **Commits** — either a period (`From`/`To` date pickers + quick presets, committer date, UTC) or a
     manually selected list of commits (`Select commits…`, searchable & paginated)
   - **Reference commit** — the `HEAD` box in the top bar accepts any branch/tag/hash; all metrics
     cover the non-merge commits reachable from that reference. **To reproduce a sample metric given
     "from a specific commit hash", paste that hash here.**
3. **Read the metrics**: 8 summary cards for the selected object, three charts
   (activity over time, top files by churn, author churn share), and sortable tables for
   **Directories / Files / Authors** with CSV export.

## Metrics implemented (per the brief)

| Metric | Definition |
|---|---|
| Added / Removed lines `l±` | lines added/removed on an object by a commit (vs. its first parent) |
| Growth `δ` | `l+ − l−` |
| Churn `λ` | `l+ + l−` |
| Directory / Repository metrics | sums over the directory's (recursive) children; repository = root directory |
| Commit-set metrics `l±, δ, λ` over `H` | sums over all commits `h ∈ H` |
| Modifications `n` | number of commits in `H` with `λ(h,o) > 0` (distinct commits, even if several files in one directory changed) |
| Modification frequency `η` | `n / |H|` (0 if `|H| = 0`) |
| Churn rate `ρ` | `λ / |H|` (0 if `|H| = 0`) |
| Author modifications / churn | same, restricted to commits of author `a` |
| Author ownership `ω` | `λ(H,o,a) / λ(H,o)` (0 if `λ(H,o) = 0`) |

**Semantics honoured exactly as specified**
- `H` = **non-merge** commits reachable from the reference commit (`git log --no-merges <ref>`)
- **Committer date** used for time filters, with the half-open interval `from ≤ date < to`
- **Rename detection at 50%** (`git log -M50%`): pure renames don't change metrics, changes from a
  rename+edit are attributed to the **new** path, and deletions are recorded as removed lines on their path
- **Binary files** (git's own detection) are not measured
- The initial commit diffs against the empty tree (its `h[p] = ∅`)
- **Author identity** is resolved through git's mailmap (`%aN`/`%aE`), and manual merges can be applied on
  top (persisted per repository in `data/repos/<id>/meta.json`)

**One documented interpretation:** the file/directory tables list objects that have change data
within the selected commit set `H` (plus everything reachable through filters). Files that existed
in `H`'s trees but never changed contribute zero to every metric formula, so no metric value is
affected by not listing them — listing every historical tree version was judged too costly to
compute for ~100k-commit repositories.

## Architecture (why it's fast)

- **One pass over history**: a single `git log --no-merges --numstat -M50%` process per
  (repository, reference) builds a compact row store `(commit, path, added, removed)` — no
  per-commit subprocesses.
- **Parsed once, cached forever**: the store is pickled per reference (`data/repos/<id>/cache_*.pkl`);
  every later filter query aggregates in memory (no git calls at all).
- **Server-side aggregation**: the browser only receives aggregated tables (top files capped,
  commits paginated), so even ~100k-commit repositories stay responsive.
  Measured: cJSON (955 commits) aggregation ≈ 25 ms; Redis (11,874 commits) ≈ 80 ms;
  Redis clone+parse ≈ 30 s one-time.

## Testing

`scripts/selftest.py` builds a synthetic repository with **hand-computed expected metrics**
covering every tricky rule (merge exclusion, rename+edit brace form, pure rename, binary
exclusion, deletion, mailmap merging, manual merging, time/manual commit sets, author filters,
path scopes, empty-set guards, zip ingestion and failure cases):

```bash
python3 scripts/selftest.py
```

## API summary (all JSON)

| Method & path | Purpose |
|---|---|
| `GET /api/repos` | list repositories |
| `POST /api/repos/upload` | multipart `file=` zip upload |
| `POST /api/repos/clone` | `{"url": "..."}` deep clone |
| `DELETE /api/repos/<id>` | remove a repository |
| `GET /api/repos/<id>/authors?ref=` | author options (post-mailmap, incl. manual groups) |
| `POST /api/repos/<id>/authors/merge` | `{"ids": [...], "name": "..."}` manual merge |
| `POST /api/repos/<id>/authors/unmerge` | `{"name": "..."}` remove a manual group |
| `GET /api/repos/<id>/commits?ref&page&q&author&from&to` | paginated commit picker data |
| `POST /api/repos/<id>/metrics` | `{"ref", "authors", "path", "from", "to", "commits"}` → full dashboard payload |

Errors return `{"error": "..."}` with an appropriate HTTP status; the UI shows them as toasts.

## Project layout

```
app.py              Flask server (API + dashboard)
engine.py           ingestion, git parsing, metric aggregation
templates/index.html  dashboard page
static/app.js       dashboard logic (vanilla JS)
static/style.css    styling
scripts/selftest.py metric verification suite
data/               ingested repositories & caches (gitignored, created at runtime)
```
