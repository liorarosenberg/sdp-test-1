"""RAT — Repo Analysis Tool: Flask API and dashboard server.

Run with:  python3 app.py   then open http://localhost:5000
"""
from __future__ import annotations

import os
import tempfile

from flask import Flask, jsonify, render_template, request

import engine

app = Flask(__name__)


def _asset_ver(fname: str) -> str:
    """Mtime-based version token so browsers re-fetch edited static assets."""
    try:
        return str(int(os.stat(os.path.join(app.static_folder, fname)).st_mtime))
    except OSError:
        return "0"


@app.context_processor
def _inject_asset_ver():
    return {"av": _asset_ver}
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3  # 2 GB uploads


def _err(message: str, code: int = 400):
    return jsonify({"error": message}), code


@app.errorhandler(engine.RepoError)
def handle_repo_error(e):
    return _err(str(e))


@app.errorhandler(engine.RepoNotFound)
def handle_repo_not_found(e):
    return _err(str(e), 404)


@app.errorhandler(404)
def handle_404(_e):
    return _err("Not found", 404)


@app.errorhandler(413)
def handle_too_large(_e):
    return _err("Upload too large (limit is 2 GB)", 413)


@app.errorhandler(Exception)
def handle_unexpected(e):
    app.logger.exception("Unhandled error")
    return _err(f"Unexpected server error: {e}", 500)


# ------------------------------ pages ------------------------------

@app.get("/")
def index():
    return render_template("index.html")


# ------------------------------ repos ------------------------------

@app.get("/api/repos")
def list_repos():
    return jsonify(engine.list_repos())


@app.post("/api/repos/upload")
def upload_repo():
    f = request.files.get("file")
    if not f or not f.filename:
        return _err("No file provided — attach a .zip of the repository")
    if not f.filename.lower().endswith(".zip"):
        return _err("Please upload a .zip file containing the repository "
                    "(including its .git folder)")
    os.makedirs(engine.TMP_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".zip", dir=engine.TMP_DIR)
    try:
        f.save(tmp_path)
        meta = engine.create_from_zip(f.filename, tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return jsonify(meta), 201


@app.post("/api/repos/clone")
def clone_repo():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return _err("No repository URL provided")
    return jsonify(engine.create_from_url(url)), 201


@app.delete("/api/repos/<rid>")
def delete_repo(rid):
    engine.delete_repo(rid)
    return jsonify({"ok": True})


# ------------------------------ authors ------------------------------

@app.get("/api/repos/<rid>/authors")
def author_options(rid):
    ref = request.args.get("ref") or "HEAD"
    meta, cd = engine.get_repo_data(rid, ref)
    return jsonify(engine.author_options(meta, cd))


@app.post("/api/repos/<rid>/authors/merge")
def merge_authors(rid):
    body = request.get_json(silent=True) or {}
    ref = body.get("ref") or "HEAD"
    engine.merge_authors(rid, body.get("ids") or [], body.get("name") or "")
    meta, cd = engine.get_repo_data(rid, ref)
    return jsonify(engine.author_options(meta, cd))


@app.post("/api/repos/<rid>/authors/unmerge")
def unmerge_authors(rid):
    body = request.get_json(silent=True) or {}
    ref = body.get("ref") or "HEAD"
    engine.unmerge_authors(rid, body.get("name") or "")
    meta, cd = engine.get_repo_data(rid, ref)
    return jsonify(engine.author_options(meta, cd))


# ------------------------------ commits ------------------------------

@app.get("/api/repos/<rid>/commits")
def list_commits(rid):
    ref = request.args.get("ref") or "HEAD"
    meta, cd = engine.get_repo_data(rid, ref)
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 200)), 1000)
    except ValueError:
        return _err("page and per_page must be integers")
    return jsonify(engine.query_commits(
        meta, cd,
        q=request.args.get("q"),
        author=request.args.get("author"),
        ts_from=request.args.get("from"),
        ts_to=request.args.get("to"),
        page=page, per_page=per_page))


# ------------------------------ metrics ------------------------------

@app.post("/api/repos/<rid>/metrics")
def metrics(rid):
    body = request.get_json(silent=True) or {}
    ref = body.get("ref") or "HEAD"
    meta, cd = engine.get_repo_data(rid, ref)
    return jsonify(engine.aggregate(
        meta, cd,
        authors=body.get("authors") or None,
        path=body.get("path") or None,
        ts_from=body.get("from"), ts_to=body.get("to"),
        commits=body.get("commits")))


if __name__ == "__main__":
    print("\n  RAT — Repo Analysis Tool")
    print("  Open http://localhost:5000 in your browser.\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
