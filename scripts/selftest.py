#!/usr/bin/env python3
"""Self-test: builds a fixture git repo with known metrics and verifies the
engine against hand-computed expectations, then checks zip ingestion.

Fixture history (fixed committer dates, all +0000):
  c1 Alice <alice@x.com>  1700000100 : add src/app.py (10 lines), README.md (4 lines)
  c2 Alice <alicet@x.com> 1700000200 : app.py +5/-2 (mailmapped to Alice), add docs/guide.md (6)
  c3 Bob <bob@y.com>      1700000300 : add .mailmap (1 line), add photo.png (binary, unmeasured),
                                      pure rename README.md -> docs/README.md
  c4 Bob (branch)         1700000400 : app.py +1/-1
  c5 Alice (main)         1700000500 : docs/README.md +2
  merge (Bob)             1700000550 : EXCLUDED (non-merge rule)
  c6 Alice                1700000600 : delete docs/guide.md (-6)
  c7 Bob                  1700000700 : git mv src/app.py -> src/main.py WITH +1/-2 edit
                                      (numstat brace form src/{app.py => main.py})

Expected over H (7 non-merge commits): added=30, removed=11, growth=19, churn=41,
modifications=7, modFreq=1.0, churnRate=41/7.
Authors: Alice churn 35 / 4 commits; Bob churn 6 / 3 commits.
"""
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import engine  # noqa: E402

FAILURES = []


def check(name, actual, expected):
    ok = actual == expected
    if not ok:
        FAILURES.append(f"{name}: expected {expected!r}, got {actual!r}")
    print(("  ok  " if ok else "  FAIL") + f" {name} = {actual!r}")


def approx(name, actual, expected, tol=1e-3):
    ok = abs(actual - expected) <= tol
    if not ok:
        FAILURES.append(f"{name}: expected ~{expected}, got {actual}")
    print(("  ok  " if ok else "  FAIL") + f" {name} = {actual}")


def run(args, cwd, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    r = subprocess.run(args, cwd=cwd, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"{args} failed: {r.stderr.decode()}")


def author_env(author, when):
    return {"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1],
            "GIT_AUTHOR_DATE": f"{when} +0000", "GIT_COMMITTER_DATE": f"{when} +0000"}


def commit_all(repo, msg, author, when, add=None, rm=None):
    if add:
        for path, content in add.items():
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
    if rm:
        for path in rm:
            os.remove(os.path.join(repo, path))
    env = author_env(author, when)
    run(["git", "add", "-A"], repo, env)
    run(["git", "commit", "-q", "-m", msg], repo, env)


ALICE = ("Alice", "alice@x.com")
ALICE2 = ("Alice", "alicet@x.com")
BOB = ("Bob", "bob@y.com")

APP_V1 = "".join(f"line {i}\n" for i in range(1, 11))          # 10 lines
APP_V2 = ("line 1\nline 3\nline 4\nline 5\nline 6\nline 7\nline 9\nline 10\n"
          "new1\nnew2\nnew3\nnew4\nnew5\n")                     # +5 / -2 vs v1
README = "readme line 1\nreadme line 2\nreadme line 3\nreadme line 4\n"
GUIDE = "guide 1\nguide 2\nguide 3\nguide 4\nguide 5\nguide 6\n"


def build_fixture(repo):
    os.makedirs(repo)
    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.name", "x"], repo)
    run(["git", "config", "user.email", "x@x"], repo)

    # c1
    commit_all(repo, "c1 initial", ALICE, 1700000100,
               add={"src/app.py": APP_V1, "README.md": README})
    # c2 (different email for Alice; .mailmap appears later in c3)
    commit_all(repo, "c2 edit app add guide", ALICE2, 1700000200,
               add={"src/app.py": APP_V2, "docs/guide.md": GUIDE})
    # c3: mailmap + binary + pure rename — all in ONE commit
    with open(os.path.join(repo, ".mailmap"), "w") as f:
        f.write("Alice <alice@x.com> <alicet@x.com>\n")
    with open(os.path.join(repo, "photo.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary\xff\xfe\x00\x01")
    run(["git", "add", "-A"], repo, author_env(BOB, 1700000300))
    run(["git", "mv", "README.md", "docs/README.md"], repo)
    run(["git", "commit", "-q", "-m", "c3 mailmap binary rename"],
        repo, author_env(BOB, 1700000300))
    # c4 on a branch
    run(["git", "checkout", "-q", "-b", "feature"], repo)
    app = os.path.join(repo, "src/app.py")
    with open(app) as f:
        src = f.read()
    with open(app, "w") as f:
        f.write(src.replace("new5\n", "new5-edited\n", 1))
    commit_all(repo, "c4 branch edit", BOB, 1700000400)
    # c5 on main
    run(["git", "checkout", "-q", "main"], repo)
    readme = os.path.join(repo, "docs/README.md")
    with open(readme, "a") as f:
        f.write("extra1\nextra2\n")
    commit_all(repo, "c5 readme extras", ALICE, 1700000500)
    # merge commit (must be excluded everywhere)
    run(["git", "merge", "-q", "--no-ff", "-m", "merge feature", "feature"],
        repo, author_env(BOB, 1700000550))
    # c6: deletion
    os.remove(os.path.join(repo, "docs/guide.md"))
    commit_all(repo, "c6 delete guide", ALICE, 1700000600)
    # c7: rename + edit in one commit (brace-form numstat)
    run(["git", "mv", "src/app.py", "src/main.py"], repo)
    main_py = os.path.join(repo, "src/main.py")
    with open(main_py) as f:
        src = f.read()
    src = src.replace("line 7\n", "", 1).replace("line 9\n", "", 1)
    src = src.replace("new5-edited\n", "new5-edited\nbrand-new\n", 1)
    with open(main_py, "w") as f:
        f.write(src)
    commit_all(repo, "c7 move and edit", BOB, 1700000700)


def main():
    root = os.path.join(BASE, "data", "selftest")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    repo = os.path.join(root, "fixture")
    build_fixture(repo)

    # ---- ingest via the zip path (also covers extraction + subfolder root detection) ----
    parent = os.path.dirname(repo)
    arc = shutil.make_archive(os.path.join(root, "fixture"), "zip",
                              root_dir=parent, base_dir="fixture")
    meta = engine.create_from_zip("fixture.zip", arc)
    rid = meta["id"]
    M = lambda: engine._load_meta(rid)
    _, cd = engine.get_repo_data(rid, "HEAD")

    check("non-merge commit count", len(cd.commits), 7)

    def find(prefix):
        for c in cd.commits:
            if c["subject"].startswith(prefix):
                return c["h"]
        raise KeyError(prefix)

    h_c2, h_c4 = find("c2 "), find("c4 ")

    # ---- author merging via mailmap ----
    check("mailmap merges Alice identities",
          sorted(cd.authors.values()), ["Alice", "Bob"])

    # ---- repository metrics (root) over the full commit set ----
    r = engine.aggregate(M(), cd)
    s = r["summary"]
    check("repo added", s["added"], 30)
    check("repo removed", s["removed"], 11)
    check("repo growth", s["growth"], 19)
    check("repo churn", s["churn"], 41)
    check("|H|", r["commitSet"]["size"], 7)
    check("repo modifications", s["modifications"], 7)
    approx("modFrequency", s["modFrequency"], 1.0)
    approx("churnRate", s["churnRate"], round(41 / 7, 4))

    files = {f["path"]: f for f in r["files"]}
    check("binary excluded", "photo.png" in files, False)
    check("rename attributed to NEW path (brace form)", "src/main.py" in files, True)
    check("src/main.py added", files["src/main.py"]["added"], 1)
    check("src/main.py removed", files["src/main.py"]["removed"], 2)
    check("src/app.py frozen after rename",
          (files["src/app.py"]["added"], files["src/app.py"]["removed"]), (16, 3))
    check("src/app.py modifications", files["src/app.py"]["modifications"], 3)
    check("pure rename zero-change at new path",
          (files["docs/README.md"]["added"], files["docs/README.md"]["removed"]), (2, 0))
    check("old path keeps pre-rename adds",
          (files["README.md"]["added"], files["README.md"]["removed"]), (4, 0))
    check("deletion recorded as removed",
          (files["docs/guide.md"]["added"], files["docs/guide.md"]["removed"]), (6, 6))
    check("mailmap file counted",
          (files[".mailmap"]["added"], files[".mailmap"]["removed"]), (1, 0))

    dirs = {d["path"]: d for d in r["dirs"]}
    check("dir src (added, removed, churn)", (dirs["src"]["added"], dirs["src"]["removed"],
          dirs["src"]["churn"]), (17, 5, 22))
    check("dir docs (added, removed, churn, mods)",
          (dirs["docs"]["added"], dirs["docs"]["removed"], dirs["docs"]["churn"],
           dirs["docs"]["modifications"]), (8, 6, 14, 3))
    check("root row equals repo metrics",
          (dirs["(root)"]["added"], dirs["(root)"]["churn"]), (30, 41))

    authors = {a["author"]: a for a in r["authors"]}
    check("author Alice churn", authors["Alice"]["churn"], 35)
    check("author Alice commits", authors["Alice"]["commits"], 4)
    check("author Bob churn", authors["Bob"]["churn"], 6)
    check("author Bob commits", authors["Bob"]["commits"], 3)
    approx("ownership Alice", authors["Alice"]["ownership"], 35 / 41)
    approx("ownership Bob", authors["Bob"]["ownership"], 6 / 41)

    # ---- time filter [c5, c5+50s) -> exactly {c5} ----
    r2 = engine.aggregate(M(), cd, ts_from=1700000500, ts_to=1700000550)
    check("time filter |H|", r2["commitSet"]["size"], 1)
    check("time filter summary", (r2["summary"]["added"], r2["summary"]["removed"]),
          (2, 0))

    # ---- manual commit set {c2, c4} ----
    r3 = engine.aggregate(M(), cd, commits=[h_c2, h_c4])
    check("manual |H|", r3["commitSet"]["size"], 2)
    check("manual summary", (r3["summary"]["added"], r3["summary"]["removed"]),
          (12, 3))

    # ---- unknown hashes are reported, not fatal ----
    r3b = engine.aggregate(M(), cd, commits=["deadbeef" * 5])
    check("unknown hash reported", len(r3b["commitSet"]["unknownCommits"]), 1)
    check("unknown hash empty set", r3b["commitSet"]["size"], 0)
    check("empty set division-by-zero guard",
          (r3b["summary"]["modFrequency"], r3b["summary"]["churnRate"]), (0, 0))

    # ---- author filter (labels are mailmap-resolved identities) ----
    alice_label = [aid for aid, n in cd.authors.items() if n == "Alice"][0]
    r4 = engine.aggregate(M(), cd, authors=[alice_label])
    check("Alice-filtered |H|", r4["commitSet"]["size"], 4)
    check("Alice-filtered churn", r4["summary"]["churn"], 35)

    # ---- directory / file scope ----
    r5 = engine.aggregate(M(), cd, path="docs")
    check("docs scope summary", (r5["summary"]["added"], r5["summary"]["removed"]),
          (8, 6))
    r6 = engine.aggregate(M(), cd, path="src/main.py")
    check("file scope summary", (r6["summary"]["added"], r6["summary"]["removed"]),
          (1, 2))
    r6b = engine.aggregate(M(), cd, path="nonexistent/")
    check("unknown path scope empty", r6b["summary"]["churn"], 0)

    # ---- manual author merge on top of mailmap ----
    bob_id = [aid for aid, n in cd.authors.items() if n == "Bob"][0]
    alice_id = [aid for aid, n in cd.authors.items() if n == "Alice"][0]
    engine.merge_authors(rid, [alice_id, bob_id], "Whole Team")
    _, cd2 = engine.get_repo_data(rid, "HEAD")
    r7 = engine.aggregate(M(), cd2)
    check("manual merge labels",
          sorted(a["author"] for a in r7["authors"]), ["Whole Team"])
    check("manual merge churn", r7["authors"][0]["churn"], 41)
    check("manual merge commits", r7["authors"][0]["commits"], 7)
    engine.unmerge_authors(rid, "Whole Team")
    _, cd3 = engine.get_repo_data(rid, "HEAD")
    r8 = engine.aggregate(M(), cd3)
    check("unmerge restores", sorted(a["author"] for a in r8["authors"]),
          ["Alice", "Bob"])

    # ---- commit listing / query ----
    q = engine.query_commits(M(), cd, q="branch edit")
    check("commit search", q["total"], 1)
    q2 = engine.query_commits(M(), cd, author=alice_label)
    check("commits by Alice", q2["total"], 4)

    # ---- error handling: unreachable remote ----
    try:
        engine.create_from_url("https://invalid.invalid/repo.git")
        check("bad URL clone fails", "no-error", "RepoError")
    except engine.RepoError:
        check("bad URL clone fails", "RepoError", "RepoError")

    # ---- zip with NO .git must fail cleanly ----
    bad_dir = os.path.join(root, "notarepo")
    os.makedirs(os.path.join(bad_dir, "src"), exist_ok=True)
    with open(os.path.join(bad_dir, "src", "x.py"), "w") as f:
        f.write("print(1)\n")
    bad_arc = shutil.make_archive(os.path.join(root, "notarepo"), "zip",
                                  root_dir=root, base_dir="notarepo")
    try:
        engine.create_from_zip("notarepo.zip", bad_arc)
        check("zip without .git fails", "no-error", "RepoError")
    except engine.RepoError:
        check("zip without .git fails", "RepoError", "RepoError")

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} failure(s)")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("SELFTEST PASSED — all metric expectations verified.")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"(elapsed {time.time() - t0:.1f}s)")
