"""RAT engine: repository ingestion, metric computation and aggregation.

Metric semantics follow the COMS3011A brief:
- H is a set of non-merge commits reachable from a reference commit (default HEAD).
- Per-commit file metrics come from `git log --numstat -M50%` (rename detection at
  50%, changes attributed to the NEW path, binary files skipped, deletions recorded
  as removed lines on their path, initial commit diffs against the empty tree).
- Committer date is used for time filtering (half-open interval [from, to)).
- Author identity is resolved through git's mailmap (%aN/%aE); manual merges are
  applied on top as groups.
"""
from __future__ import annotations

import json
import os
import pickle
import re
import shutil
import subprocess
import threading
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPOS_DIR = os.path.join(DATA_DIR, "repos")
TMP_DIR = os.path.join(DATA_DIR, "tmp")

_BRACE_RE = re.compile(r"\{([^{}]*) => ([^{}]*)\}")
_HASH_RE = re.compile(r"^[0-9a-f]{7,40}$")

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_CACHE: dict[tuple[str, str], "CommitData"] = {}


class RepoError(Exception):
    """A user-facing repository error."""


class RepoNotFound(RepoError):
    """The requested repository does not exist."""


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def _git(args, cwd=None, check=True, timeout=900) -> subprocess.CompletedProcess:
    # GIT_ASKPASS="echo" neutralises inherited IDE/desktop askpass helpers,
    # so auth failures (e.g. a nonexistent remote repo) fail fast instead of
    # hanging on a GUI credential prompt the server can never answer.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="echo")
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RepoError("git command timed out")
    except FileNotFoundError:
        raise RepoError("git is not installed or not on PATH")
    if check and proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise RepoError("git: " + (msg[-400:] if msg else f"exit code {proc.returncode}"))
    return proc


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.").lower()
    return s[:48] or "repo"


def _repo_dir(rid: str) -> str:
    return os.path.join(REPOS_DIR, rid)


def _meta_path(rid: str) -> str:
    return os.path.join(_repo_dir(rid), "meta.json")


def _ref_slug(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", ref.strip())[:80] or "HEAD"


def _unique_id(name: str) -> str:
    os.makedirs(REPOS_DIR, exist_ok=True)
    base, rid, n = _slug(name), _slug(name), 2
    while os.path.exists(_repo_dir(rid)):
        rid = f"{base}-{n}"
        n += 1
    return rid


def _load_meta(rid: str) -> dict:
    path = _meta_path(rid)
    if not os.path.exists(path):
        raise RepoNotFound(f"Repository '{rid}' not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_meta(meta: dict) -> None:
    with open(_meta_path(meta["id"]), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------

def create_from_zip(filename: str, zip_path: str) -> dict:
    """Extract an uploaded zip (repo with its .git) and ingest it."""
    rid = _unique_id(os.path.splitext(os.path.basename(filename))[0])
    dest = _repo_dir(rid)
    os.makedirs(dest, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            if len(members) > 200000:
                raise RepoError("Zip file too large (over 200,000 entries)")
            dest_real = os.path.realpath(dest)
            for info in members:
                name = info.filename
                if not name or name.startswith("/") or ".." in name.split("/"):
                    raise RepoError(f"Unsafe path in zip: {name!r}")
                target = os.path.realpath(os.path.join(dest, name))
                if target != dest_real and not target.startswith(dest_real + os.sep):
                    raise RepoError(f"Unsafe path in zip: {name!r}")
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
        root = _find_repo_root(dest)
        return _register_repo(rid, os.path.splitext(os.path.basename(filename))[0],
                              "zip", root)
    except RepoError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except zipfile.BadZipFile:
        shutil.rmtree(dest, ignore_errors=True)
        raise RepoError("That file is not a valid zip archive")


def _find_repo_root(dest: str) -> str:
    """Locate the directory containing .git (zip root, or a single subfolder)."""
    direct = os.path.join(dest, ".git")
    if os.path.exists(direct):
        return dest
    subdirs = []
    for entry in sorted(os.listdir(dest)):
        full = os.path.join(dest, entry)
        if os.path.isdir(full):
            if os.path.exists(os.path.join(full, ".git")):
                subdirs.append(full)
    if len(subdirs) == 1:
        return subdirs[0]
    raise RepoError("No .git directory found in the zip — export the repository "
                    "including its .git folder")


def create_from_url(url: str) -> dict:
    """Deep-clone a remote repository (full history) and ingest it."""
    url = url.strip()
    if not re.match(r"^(https?://|git://|ssh://|git@)", url):
        raise RepoError("Please provide a valid git repository URL "
                        "(http(s)://, ssh:// or git@...)")
    rid = _unique_id(url.split("/")[-1].removesuffix(".git") or "repo")
    dest = _repo_dir(rid)
    os.makedirs(dest, exist_ok=True)
    try:
        proc = _git(["clone", url, "."], cwd=dest, check=False, timeout=1200)
        if proc.returncode != 0:
            msg = proc.stderr.decode("utf-8", "replace").strip()
            raise RepoError("Clone failed: " + (msg[-400:] if msg else "unknown error"))
        return _register_repo(rid, url.split("/")[-1].removesuffix(".git") or "repo",
                              "clone", dest)
    except RepoError:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _register_repo(rid: str, name: str, source: str, root: str) -> dict:
    meta = {
        "id": rid,
        "name": name,
        "source": source,
        "path": root,
        "created_at": int(time.time()),
        "stats": {},          # per-ref parse stats
        "author_groups": {},  # manual merges: group name -> [author ids]
    }
    _save_meta(meta)
    _parse_repo(meta, "HEAD")  # parse eagerly so errors surface at ingest time
    return _meta_public(meta)


def delete_repo(rid: str) -> None:
    _load_meta(rid)
    with _lock(rid):
        for key in [k for k in _CACHE if k[0] == rid]:
            _CACHE.pop(key, None)
        shutil.rmtree(_repo_dir(rid), ignore_errors=True)


def list_repos() -> list:
    repos = []
    if not os.path.isdir(REPOS_DIR):
        return repos
    for rid in sorted(os.listdir(REPOS_DIR)):
        meta_path = _meta_path(rid)
        if os.path.exists(meta_path):
            try:
                repos.append(_meta_public(_load_meta(rid)))
            except Exception:
                continue
    return repos


def _meta_public(meta: dict) -> dict:
    return {k: meta.get(k) for k in
            ("id", "name", "source", "created_at", "stats", "author_groups")}


# --------------------------------------------------------------------------
# Parsing: one pass over git log --numstat
# --------------------------------------------------------------------------

class CommitData:
    __slots__ = ("ref", "commits", "hash_to_idx", "ts_order", "paths", "pid",
                 "by_path", "dirs_paths", "path_dirs", "authors", "parsed_at",
                 "num_rows")


_LOG_FORMAT = "%H%x01%aN%x01%aE%x01%ct%x01%s"


def _unquote_path(p: str) -> str:
    """Undo git's C-style quoting of exotic filenames."""
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        body = p[1:-1]
        out = bytearray()
        i = 0
        simple = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92,
                  "a": 7, "b": 8, "f": 12, "v": 11}
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt.isdigit() and body[i + 1:i + 4].isdigit():
                    out.append(int(body[i + 1:i + 4], 8) & 0xFF)
                    i += 4
                    continue
                if nxt in simple:
                    out.append(simple[nxt])
                else:
                    out.append(ord(nxt) & 0xFF)
                i += 2
                continue
            out.append(ord(ch) & 0xFF)
            i += 1
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError:
            return out.decode("utf-8", "replace")
    return p


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _safe_name(s: str) -> str:
    """Make a string JSON-safe (surrogateescape bytes -> replacement char)."""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _rename_new_path(field: str) -> str:
    """Return the NEW path of a numstat rename field, attributed correctly."""
    if " => " in field:
        m = _BRACE_RE.search(field)
        if m:
            pre, post = field[:m.start()], field[m.end():]
            return _unquote_path(pre + m.group(2) + post)
        old, new = field.rsplit(" => ", 1)
        return _unquote_path(new)
    return _unquote_path(field)


def _parse_repo(meta: dict, ref: str) -> CommitData:
    """Single git-log pass building the full per-commit/per-file row store."""
    repo_path = meta["path"]
    cmd = ["-c", "core.quotePath=false", "log", "--no-merges", "--numstat",
           "-M50%", f"--format={_LOG_FORMAT}", ref]
    proc = _git(cmd, cwd=repo_path, check=False, timeout=1800)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise RepoError("Cannot read history: " + (msg[-300:] if msg else "unknown error"))

    commits: list[dict] = []
    rows: list[tuple[int, str, int, int]] = []
    author_ids: dict[str, str] = {}

    stdout = proc.stdout.splitlines()
    cur = -1
    for raw in stdout:
        line = _decode(raw)
        # commit header: 40-hex sha + \x01 separators
        if len(line) > 41 and line[40] == "\x01" and all(c in "0123456789abcdef" for c in line[:40]):
            parts = line.split("\x01", 4)
            while len(parts) < 5:
                parts.append("")
            sha, name, email, ts, subject = parts
            aid = f"{name} <{email}>" if email else name
            author_ids.setdefault(aid, name)
            cur = len(commits)
            commits.append({
                "h": sha,
                "author": _safe_name(aid),
                "name": _safe_name(name),
                "ts": int(ts) if ts.isdigit() else 0,
                "subject": _safe_name(subject),
            })
        elif cur >= 0 and "\t" in line:
            cols = line.split("\t")
            if len(cols) >= 3:
                added, removed = cols[0], cols[1]
                if added == "-" or removed == "-":
                    continue  # binary file: not measured
                if not (added.isdigit() and removed.isdigit()):
                    continue
                path = _safe_name(_rename_new_path("\t".join(cols[2:])))
                if path:
                    rows.append((cur, path, int(added), int(removed)))
        # anything else (blank lines) is ignored

    if not commits:
        raise RepoError(f"No commits found for reference '{ref}' — "
                        f"is this a git repository with history on that ref?")

    cd = CommitData()
    cd.ref = ref
    cd.commits = commits
    cd.hash_to_idx = {c["h"]: i for i, c in enumerate(commits)}
    cd.ts_order = sorted(range(len(commits)), key=lambda i: (commits[i]["ts"], i))
    cd.parsed_at = int(time.time())

    paths = sorted({p for _, p, _, _ in rows})
    pid = {p: i for i, p in enumerate(paths)}
    cd.paths = paths
    cd.num_rows = len(rows)
    n_paths = len(paths)

    by_path: list[list] = [[] for _ in range(n_paths)]
    dirs_paths: dict[str, list] = {"": list(range(n_paths))}
    path_dirs: list[tuple] = [() for _ in range(n_paths)]
    dir_chains: list[tuple] = []
    for p, i in pid.items():
        chain = [""]
        acc = ""
        for part in p.split("/")[:-1]:
            acc = part if not acc else acc + "/" + part
            chain.append(acc)
            dirs_paths.setdefault(acc, []).append(i)
        dir_chains.append(tuple(chain))
    for ci, p, a, r in rows:
        by_path[pid[p]].append((ci, a, r))
    cd.by_path = by_path
    cd.pid = pid
    cd.dirs_paths = dirs_paths
    cd.path_dirs = dir_chains
    cd.authors = author_ids

    refslug = _ref_slug(ref)
    cache_file = os.path.join(_repo_dir(meta["id"]), f"cache_{refslug}.pkl")
    tmp_file = cache_file + ".tmp"
    with open(tmp_file, "wb") as f:
        pickle.dump(cd, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_file, cache_file)

    meta.setdefault("stats", {})[ref] = {
        "ref": ref, "commits": len(commits), "rows": len(rows),
        "files": len(paths), "parsed_at": cd.parsed_at,
    }
    _save_meta(meta)
    with _lock(meta["id"]):
        _CACHE[(meta["id"], ref)] = cd
    return cd


def get_repo_data(rid: str, ref: str = "HEAD") -> tuple[dict, CommitData]:
    meta = _load_meta(rid)
    cached = _CACHE.get((rid, ref))
    if cached is not None:
        return meta, cached
    cache_file = os.path.join(_repo_dir(rid), f"cache_{_ref_slug(ref)}.pkl")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                cd = pickle.load(f)
            if getattr(cd, "ref", None) == ref:
                _CACHE[(rid, ref)] = cd
                return meta, cd
        except Exception:
            pass
    with _lock(f"{rid}:{ref}"):
        cd = _CACHE.get((rid, ref))
        if cd is None:
            cd = _parse_repo(meta, ref)
    return meta, cd


# --------------------------------------------------------------------------
# Author merging (mailmap already applied by git; manual groups on top)
# --------------------------------------------------------------------------

def _label_map(meta: dict) -> dict:
    """author id -> display label (manual group name if merged)."""
    id2label: dict[str, str] = {}
    for gname, ids in (meta.get("author_groups") or {}).items():
        for aid in ids:
            id2label[aid] = gname
    return id2label


def _display(label: str) -> str:
    if " <" in label and label.endswith(">"):
        return label.split(" <", 1)[0]
    return label


def author_options(meta: dict, cd: CommitData) -> list:
    """Author filter options with repo-wide totals (for the merge UI too)."""
    id2label = _label_map(meta)
    groups: dict[str, dict] = {}
    label_counts: dict[str, int] = defaultdict(int)
    for c in cd.commits:
        lab = id2label.get(c["author"], c["author"])
        g = groups.setdefault(lab, {"label": lab, "display": _display(lab),
                                    "ids": [], "commits": 0, "churn": 0})
        g["commits"] += 1
        if c["author"] not in g["ids"]:
            g["ids"].append(c["author"])
    churn_by_id: dict[str, int] = defaultdict(int)
    for ci, _p, a, r in _iter_rows(cd):
        churn_by_id[cd.commits[ci]["author"]] += a + r
    for lab, g in groups.items():
        g["churn"] = sum(churn_by_id.get(i, 0) for i in g["ids"])
        g["merged"] = lab in (meta.get("author_groups") or {})
    return sorted(groups.values(), key=lambda g: -g["churn"])


def merge_authors(rid: str, ids: list, name: str) -> None:
    meta = _load_meta(rid)
    ids = [i for i in ids if isinstance(i, str) and i]
    if len(ids) < 2:
        raise RepoError("Select at least two author identities to merge")
    name = name.strip()[:80] or "Merged authors"
    meta.setdefault("author_groups", {})[name] = ids
    _save_meta(meta)
    _invalidate(rid)


def unmerge_authors(rid: str, name: str) -> None:
    meta = _load_meta(rid)
    groups = meta.get("author_groups") or {}
    if name not in groups:
        raise RepoError(f"No author group named '{name}'", 404)
    del groups[name]
    meta["author_groups"] = groups
    _save_meta(meta)
    _invalidate(rid)


def _invalidate(rid: str) -> None:
    for key in [k for k in _CACHE if k[0] == rid]:
        _CACHE.pop(key, None)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _parse_ts(value):
    """Accept epoch seconds or ISO 'YYYY-MM-DD[THH:MM[:SS]]' (UTC)."""
    if value in (None, "", "null"):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        if len(s) == 10:
            s += "T00:00:00"
        elif len(s) == 16:
            s += ":00"
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        raise RepoError(f"Invalid date: {value!r} (use YYYY-MM-DD)")


def _iter_rows(cd: CommitData):
    """Yield (commit_idx, path, added, removed) over all rows."""
    for i, plist in enumerate(cd.by_path):
        p = cd.paths[i]
        for ci, a, r in plist:
            yield ci, p, a, r


def aggregate(meta: dict, cd: CommitData, authors=None, path=None,
              ts_from=None, ts_to=None, commits=None) -> dict:
    ts_from = _parse_ts(ts_from)
    ts_to = _parse_ts(ts_to)
    id2label = _label_map(meta)
    label_set = set(authors) if authors else None

    # ---- resolve the commit set H ----
    if commits is not None:
        idxs = []
        for h in commits:
            i = cd.hash_to_idx.get(h)
            if i is not None:
                idxs.append(i)
        unknown = [h for h in commits if h not in cd.hash_to_idx]
    else:
        idxs = list(range(len(cd.commits)))
        unknown = []

    if ts_from is not None:
        idxs = [i for i in idxs if cd.commits[i]["ts"] >= ts_from]
    if ts_to is not None:
        idxs = [i for i in idxs if cd.commits[i]["ts"] < ts_to]
    if label_set:
        idxs = [i for i in idxs
                if id2label.get(cd.commits[i]["author"], cd.commits[i]["author"]) in label_set]
    S = set(idxs)
    nH = len(S)

    prefix = (path or "").strip().strip("/")

    # per-commit lookups computed once (not per row) — keeps big repos fast
    label_of: dict[int, str] = {}
    month_of: dict[int, str] = {}
    author_commits: dict[str, int] = defaultdict(int)
    for i in S:
        c = cd.commits[i]
        lab = id2label.get(c["author"], c["author"])
        label_of[i] = lab
        author_commits[lab] += 1
        month_of[i] = datetime.fromtimestamp(c["ts"], timezone.utc).strftime("%Y-%m")

    files: dict[str, list] = {}
    dirs: dict[str, list] = defaultdict(lambda: [0, 0, 0])  # added, removed, mods
    # distinct commits with churn > 0 per directory (a commit touching several
    # files in a dir must count once towards that dir's modifications)
    dir_commits: dict[str, set] = defaultdict(set)
    author_acc: dict[str, list] = defaultdict(lambda: [0, 0, 0, 0])  # add, rem, churn, -
    churn_by_commit: dict[int, int] = {}
    churn_by_month: dict[str, list] = defaultdict(lambda: [0, 0, 0, 0])  # commits, add, rem, churn
    rows_scanned = 0

    # resolve scope: whole repo, a directory, or a single file
    if not prefix:
        pids = cd.dirs_paths[""]
    elif prefix in cd.dirs_paths:
        pids = cd.dirs_paths[prefix]
    elif prefix in cd.pid:
        pids = [cd.pid[prefix]]
    else:
        pids = []
    for pid in pids:
        p = cd.paths[pid]
        for ci, a, r in cd.by_path[pid]:
            if ci not in S:
                continue
            rows_scanned += 1
            churn = a + r
            f = files.get(p)
            if f is None:
                f = files[p] = [0, 0, 0]
            f[0] += a
            f[1] += r
            if churn > 0:
                f[2] += 1
            for d in cd.path_dirs[pid]:
                dd = dirs[d]
                dd[0] += a
                dd[1] += r
            if churn > 0:
                for d in cd.path_dirs[pid]:
                    dir_commits[d].add(ci)
            lab = label_of[ci]
            au = author_acc[lab]
            au[0] += a
            au[1] += r
            au[2] += churn
            churn_by_commit[ci] = churn_by_commit.get(ci, 0) + churn
            mb = churn_by_month[month_of[ci]]
            mb[1] += a
            mb[2] += r
            mb[3] += churn

    for i in S:
        churn_by_month[month_of[i]][0] += 1

    modifications = sum(1 for v in churn_by_commit.values() if v > 0)
    author_mods: dict[str, int] = defaultdict(int)
    for ci, ch in churn_by_commit.items():
        if ch > 0:
            author_mods[label_of[ci]] += 1

    def obj_metrics(add, rem, mods):
        churn = add + rem
        return {
            "added": add, "removed": rem, "growth": add - rem, "churn": churn,
            "modifications": mods,
            "modFrequency": round(mods / nH, 4) if nH else 0,
            "churnRate": round(churn / nH, 4) if nH else 0,
        }

    # summary reflects the selected object: repo root, directory or file
    if not prefix:
        sd = dirs.get("", [0, 0, 0])
        smods = len(dir_commits.get("", ()))
    elif prefix in files:
        sd = files[prefix]
        smods = sd[2]
    else:
        sd = dirs.get(prefix, [0, 0, 0])
        smods = len(dir_commits.get(prefix, ()))
    summary = obj_metrics(sd[0], sd[1], smods)
    total_churn = summary["churn"]

    files_rows = []
    for p, (a, r, m) in files.items():
        row = {"path": p}
        row.update(obj_metrics(a, r, m))
        files_rows.append(row)
    files_rows.sort(key=lambda x: -x["churn"])

    dirs_rows = []
    for d, (a, r, _m) in dirs.items():
        row = {"path": d if d else "(root)"}
        row.update(obj_metrics(a, r, len(dir_commits.get(d, ()))))
        dirs_rows.append(row)
    dirs_rows.sort(key=lambda x: -x["churn"])

    author_rows = []
    for lab, (a, r, churn, _m) in author_acc.items():
        author_rows.append({
            "author": _display(lab),
            "label": lab,
            "commits": author_commits.get(lab, 0),
            "added": a, "removed": r, "churn": churn,
            "modifications": author_mods.get(lab, 0),
            "ownership": round(churn / total_churn, 4) if total_churn else 0,
        })
    author_rows.sort(key=lambda x: -x["churn"])

    timeseries = []
    for month in sorted(churn_by_month):
        c, a, r, ch = churn_by_month[month]
        timeseries.append({"month": month, "commits": c,
                           "added": a, "removed": r, "churn": ch})

    return {
        "repo": {"id": meta["id"], "name": meta["name"], "ref": cd.ref},
        "commitSet": {
            "size": nH,
            "totalInRef": len(cd.commits),
            "from": ts_from, "to": ts_to,
            "authors": sorted(label_set) if label_set else None,
            "path": prefix or None,
            "manualCount": len(commits) if commits is not None else None,
            "unknownCommits": unknown[:20],
        },
        "scope": {"files": len(files_rows), "dirs": len(dirs_rows),
                  "rowsScanned": rows_scanned},
        "summary": summary,
        "timeseries": timeseries,
        "files": files_rows[:1000],
        "filesTotal": len(files_rows),
        "dirs": dirs_rows[:5000],
        "authors": author_rows[:2000],
        "authorsTotal": len(author_rows),
    }


# --------------------------------------------------------------------------
# Commit listing (for the manual commit-set picker)
# --------------------------------------------------------------------------

def query_commits(meta: dict, cd: CommitData, q=None, author=None,
                  ts_from=None, ts_to=None, page=1, per_page=200) -> dict:
    ts_from = _parse_ts(ts_from)
    ts_to = _parse_ts(ts_to)
    id2label = _label_map(meta)
    q = (q or "").lower()
    author = author or None

    hits = []
    for i in cd.ts_order:
        c = cd.commits[i]
        if ts_from is not None and c["ts"] < ts_from:
            continue
        if ts_to is not None and c["ts"] >= ts_to:
            continue
        lab = id2label.get(c["author"], c["author"])
        if author and lab != author:
            continue
        if q and q not in c["h"] and q not in c["subject"].lower() \
                and q not in c["name"].lower():
            continue
        hits.append(c)
    total = len(hits)
    per_page = max(1, min(int(per_page), 1000))
    page = max(1, int(page))
    start = (page - 1) * per_page
    chunk = hits[start:start + per_page]
    return {
        "total": total, "page": page, "perPage": per_page,
        "commits": [{"h": c["h"], "ts": c["ts"], "author": _display(
            id2label.get(c["author"], c["author"])), "subject": c["subject"]}
            for c in chunk],
    }
