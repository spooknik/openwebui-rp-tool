"""Admin UI (Jinja + htmx). Protected by ADMIN_API_KEY via a session cookie."""

import hashlib
import hmac
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, ingest, ollama, search, tagger
from ..config import get_settings
from ..signing import media_url

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
templates.env.globals["media_url"] = media_url
templates.env.globals["TAG_CATEGORIES"] = db.TAG_CATEGORIES

COOKIE = "rpm_session"
PAGE_SIZE = 60


def _session_token() -> str:
    s = get_settings()
    return hmac.new(s.signing_secret.encode(), f"admin:{s.admin_api_key}".encode(), hashlib.sha256).hexdigest()


def require_admin(request: Request) -> None:
    if not hmac.compare_digest(request.cookies.get(COOKIE, ""), _session_token()):
        raise HTTPException(303, headers={"Location": "/login", "HX-Redirect": "/login"})


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "library"


def _lib_or_404(library_id: int):
    lib = db.conn().execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()
    if not lib:
        raise HTTPException(404)
    return lib


def _media_or_404(media_id: int):
    m = db.conn().execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    if not m:
        raise HTTPException(404)
    return m


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


# --- auth -----------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return _render(request, "login.html", error=None)


@router.post("/login")
def login(request: Request, key: str = Form(...)):
    if not hmac.compare_digest(key, get_settings().admin_api_key):
        return _render(request, "login.html", error="Wrong key")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _session_token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --- dashboard / queue ----------------------------------------------------

admin = APIRouter(dependencies=[Depends(require_admin)])


@admin.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    libs = db.conn().execute(
        """SELECT l.*,
             SUM(m.status='ready') AS ready, SUM(m.status IN ('pending','tagging')) AS pending,
             SUM(m.status='error') AS errors, COUNT(m.id) AS total
           FROM libraries l LEFT JOIN media m ON m.library_id=l.id GROUP BY l.id ORDER BY l.name"""
    ).fetchall()
    return _render(request, "dashboard.html", libs=libs, model_ids=db.library_model_ids, settings=get_settings())


@admin.get("/queue", response_class=HTMLResponse)
def queue_partial(request: Request):
    return _render(request, "_queue.html", q=tagger.queue_stats())


@admin.post("/queue/{action}", response_class=HTMLResponse)
def queue_action(request: Request, action: str):
    if action == "pause":
        tagger.set_paused(True)
    elif action == "resume":
        tagger.set_paused(False)
    elif action == "retry-failed":
        ids = [r["media_id"] for r in db.conn().execute("SELECT DISTINCT media_id FROM jobs WHERE status='failed'")]
        db.conn().execute("DELETE FROM jobs WHERE status='failed'")
        with db.tx() as c:
            for mid in ids:
                m = c.execute("SELECT thumb_path FROM media WHERE id=?", (mid,)).fetchone()
                c.execute("INSERT INTO jobs(media_id, kind) VALUES (?, ?)", (mid, "tag" if m and m["thumb_path"] else "prepare"))
                c.execute("UPDATE media SET status='pending', error=NULL WHERE id=?", (mid,))
        tagger.wake()
    else:
        raise HTTPException(404)
    return _render(request, "_queue.html", q=tagger.queue_stats())


@admin.get("/ollama/models")
def ollama_models():
    try:
        return {"models": ollama.list_models()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# --- libraries ------------------------------------------------------------


@admin.get("/libraries/new", response_class=HTMLResponse)
def library_new(request: Request):
    return _render(request, "library_form.html", lib=None, error=None)


def _save_library(library_id: int | None, name: str, character_notes: str, model_ids: str, rating_cap: str, cooldown_turns: str):
    ids = [x.strip() for x in re.split(r"[\n,]+", model_ids) if x.strip()]
    cooldown = int(cooldown_turns) if cooldown_turns.strip() else None
    if rating_cap not in db.RATING_LEVELS:
        rating_cap = "explicit"
    with db.tx() as c:
        if library_id is None:
            slug, n = _slugify(name), 1
            while c.execute("SELECT 1 FROM libraries WHERE slug=?", (slug,)).fetchone():
                n += 1
                slug = f"{_slugify(name)}-{n}"
            cur = c.execute(
                "INSERT INTO libraries(slug, name, character_notes, model_ids, rating_cap, cooldown_turns) VALUES (?,?,?,?,?,?)",
                (slug, name.strip(), character_notes, json.dumps(ids), rating_cap, cooldown),
            )
            return cur.lastrowid
        c.execute(
            "UPDATE libraries SET name=?, character_notes=?, model_ids=?, rating_cap=?, cooldown_turns=? WHERE id=?",
            (name.strip(), character_notes, json.dumps(ids), rating_cap, cooldown, library_id),
        )
        return library_id


@admin.post("/libraries/new")
def library_create(name: str = Form(...), character_notes: str = Form(""), model_ids: str = Form(""),
                   rating_cap: str = Form("explicit"), cooldown_turns: str = Form("")):
    lid = _save_library(None, name, character_notes, model_ids, rating_cap, cooldown_turns)
    return RedirectResponse(f"/libraries/{lid}", status_code=303)


@admin.get("/libraries/{library_id}/edit", response_class=HTMLResponse)
def library_edit(request: Request, library_id: int):
    lib = _lib_or_404(library_id)
    return _render(request, "library_form.html", lib=lib, model_ids="\n".join(db.library_model_ids(lib)), error=None)


@admin.post("/libraries/{library_id}/edit")
def library_update(library_id: int, name: str = Form(...), character_notes: str = Form(""), model_ids: str = Form(""),
                   rating_cap: str = Form("explicit"), cooldown_turns: str = Form("")):
    _lib_or_404(library_id)
    _save_library(library_id, name, character_notes, model_ids, rating_cap, cooldown_turns)
    return RedirectResponse(f"/libraries/{library_id}", status_code=303)


@admin.post("/libraries/{library_id}/delete")
def library_delete(library_id: int):
    _lib_or_404(library_id)
    rows = db.conn().execute("SELECT * FROM media WHERE library_id=?", (library_id,)).fetchall()
    with db.tx() as c:
        for m in rows:
            db.delete_vec(c, m["id"])
            db.delete_fts(c, m["id"])
        c.execute("DELETE FROM libraries WHERE id=?", (library_id,))
    for m in rows:
        ingest.delete_media_files(m)
    return RedirectResponse("/", status_code=303)


def _grid_query(library_id: int, status: str, kind: str, q: str, offset: int):
    sql = "SELECT m.* FROM media m WHERE m.library_id=?"
    params: list = [library_id]
    if status:
        sql += " AND m.status=?" if status != "disabled" else " AND m.enabled=0"
        if status != "disabled":
            params.append(status)
    if kind:
        sql += " AND m.kind=?"
        params.append(kind)
    words = re.findall(r"[\w]+", q.lower())
    if words:
        sql += " AND m.id IN (SELECT rowid FROM media_fts WHERE media_fts MATCH ?)"
        params.append(" ".join(f'"{w}"*' for w in words))
    sql += " ORDER BY m.id DESC LIMIT ? OFFSET ?"
    params += [PAGE_SIZE + 1, offset]
    rows = db.conn().execute(sql, params).fetchall()
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


@admin.get("/libraries/{library_id}", response_class=HTMLResponse)
def library_view(request: Request, library_id: int, status: str = "", kind: str = "", q: str = "", offset: int = 0):
    lib = _lib_or_404(library_id)
    items, more = _grid_query(library_id, status, kind, q, offset)
    ctx = dict(lib=lib, items=items, more=more, next_offset=offset + PAGE_SIZE, status=status, kind=kind, q=q)
    if request.headers.get("HX-Request") and offset:
        return _render(request, "_grid.html", **ctx)
    counts = {r["status"]: r["n"] for r in db.conn().execute(
        "SELECT status, COUNT(*) n FROM media WHERE library_id=? GROUP BY status", (library_id,))}
    return _render(request, "library.html", counts=counts, import_dirs=ingest.list_import_dirs(),
                   imp=ingest.import_status.get(library_id), model_ids=db.library_model_ids(lib), **ctx)


@admin.post("/libraries/{library_id}/upload")
def library_upload(library_id: int, file: UploadFile):
    lib = _lib_or_404(library_id)
    media_id, status = ingest.save_upload(library_id, lib["slug"], file.filename or "upload", file.file)
    tagger.wake()
    return {"status": status, "media_id": media_id, "filename": file.filename}


@admin.post("/libraries/{library_id}/import", response_class=HTMLResponse)
def library_import(request: Request, library_id: int, subdir: str = Form("")):
    _lib_or_404(library_id)
    ingest.start_import(library_id, subdir)
    tagger.wake()
    return _render(request, "_import_status.html", lib_id=library_id, imp=ingest.import_status.get(library_id))


@admin.get("/libraries/{library_id}/import", response_class=HTMLResponse)
def library_import_status(request: Request, library_id: int):
    return _render(request, "_import_status.html", lib_id=library_id, imp=ingest.import_status.get(library_id))


@admin.post("/libraries/{library_id}/retag", response_class=HTMLResponse)
def library_retag(library_id: int, scope: str = Form("errors")):
    _lib_or_404(library_id)
    where = {"errors": "status='error'", "all": "tags_locked=0"}.get(scope, "status='error'")
    ids = [r["id"] for r in db.conn().execute(f"SELECT id FROM media WHERE library_id=? AND {where}", (library_id,))]
    n = tagger.enqueue(ids, "tag")
    return HTMLResponse(f"<span class='ok'>Queued {n} item(s) for tagging.</span>")


@admin.get("/libraries/{library_id}/search", response_class=HTMLResponse)
def library_search(request: Request, library_id: int, q: str = "", media_type: str = "any"):
    lib = _lib_or_404(library_id)
    hits, error = [], None
    if q.strip():
        try:
            hits = search.search(lib, q, media_type, limit=12)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    return _render(request, "_search_results.html", hits=hits, error=error, q=q, min_score=get_settings().min_score)


# --- media ----------------------------------------------------------------


@admin.get("/media/{media_id}", response_class=HTMLResponse)
def media_view(request: Request, media_id: int):
    m = _media_or_404(media_id)
    lib = _lib_or_404(m["library_id"])
    sends = db.conn().execute("SELECT COUNT(*) n, MAX(sent_at) last FROM sends WHERE media_id=?", (media_id,)).fetchone()
    job = db.conn().execute("SELECT * FROM jobs WHERE media_id=? ORDER BY id DESC LIMIT 1", (media_id,)).fetchone()
    return _render(request, "media.html", m=m, lib=lib, tags=db.media_tags(media_id), sends=sends, job=job)


@admin.post("/media/{media_id}")
async def media_save(request: Request, media_id: int):
    m = _media_or_404(media_id)
    form = await request.form()
    caption = str(form.get("caption", "")).strip()
    rating = str(form.get("rating", "")) if form.get("rating") in db.RATING_LEVELS else m["rating"]
    enabled = 1 if form.get("enabled") else 0
    tags = {cat: [t for t in str(form.get(f"tag_{cat}", "")).split(",") if t.strip()] for cat in db.TAG_CATEGORIES}
    with db.tx() as c:
        c.execute("UPDATE media SET caption=?, rating=?, enabled=?, tags_locked=1 WHERE id=?", (caption, rating, enabled, media_id))
        db.write_tags(c, media_id, caption, tags)
        db.sync_vec_meta(c, media_id)
    if m["status"] == "ready":
        tagger.enqueue([media_id], "embed")
    return RedirectResponse(f"/media/{media_id}", status_code=303)


@admin.post("/media/{media_id}/toggle", response_class=HTMLResponse)
def media_toggle(request: Request, media_id: int):
    _media_or_404(media_id)
    with db.tx() as c:
        c.execute("UPDATE media SET enabled=1-enabled WHERE id=?", (media_id,))
        db.sync_vec_meta(c, media_id)
    m = _media_or_404(media_id)
    return _render(request, "_card.html", m=m)


@admin.post("/media/{media_id}/retag")
def media_retag(media_id: int):
    _media_or_404(media_id)
    db.conn().execute("UPDATE media SET tags_locked=0 WHERE id=?", (media_id,))
    tagger.enqueue([media_id], "tag")
    return RedirectResponse(f"/media/{media_id}", status_code=303)


@admin.post("/media/{media_id}/delete")
def media_delete(media_id: int):
    m = _media_or_404(media_id)
    with db.tx() as c:
        db.delete_vec(c, media_id)
        db.delete_fts(c, media_id)
        c.execute("DELETE FROM media WHERE id=?", (media_id,))
    ingest.delete_media_files(m)
    return RedirectResponse(f"/libraries/{m['library_id']}", status_code=303)


router.include_router(admin)
