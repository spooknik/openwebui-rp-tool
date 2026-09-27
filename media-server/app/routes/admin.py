"""Admin UI (Jinja + htmx). Protected by ADMIN_API_KEY via a session cookie."""

import asyncio
import hashlib
import hmac
import html
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import db, ingest, ollama, search, tagger, toy
from ..config import get_settings
from ..signing import media_url

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
templates.env.globals["media_url"] = media_url
templates.env.globals["TAG_CATEGORIES"] = db.TAG_CATEGORIES
templates.env.globals["HEAT_LABELS"] = db.HEAT_LABELS


def card_state(m) -> str:
    """Changes whenever a grid card needs re-rendering (used by the live-update poller)."""
    return f"{m['status']}|{1 if m['thumb_path'] else 0}|{m['enabled']}|{m['tagged_at'] or ''}"


templates.env.globals["card_state"] = card_state

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


def _library_rows():
    return db.conn().execute(
        """SELECT l.*,
             SUM(m.status='ready') AS ready, SUM(m.status IN ('pending','tagging')) AS pending,
             SUM(m.status='error') AS errors, COUNT(m.id) AS total
           FROM libraries l LEFT JOIN media m ON m.library_id=l.id GROUP BY l.id ORDER BY l.name"""
    ).fetchall()


@admin.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return _render(request, "dashboard.html", libs=_library_rows(), model_ids=db.library_model_ids, settings=get_settings())


@admin.get("/libraries-table", response_class=HTMLResponse)
def libraries_table(request: Request):
    return _render(request, "_libraries_table.html", libs=_library_rows(), model_ids=db.library_model_ids)


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
        # Retry only the step that failed (a failed embed doesn't re-run vision tagging).
        with db.tx() as c:
            failed = c.execute("SELECT DISTINCT media_id, kind FROM jobs WHERE status='failed'").fetchall()
            c.execute("DELETE FROM jobs WHERE status='failed'")
            for f in failed:
                c.execute("INSERT INTO jobs(media_id, kind) VALUES (?, ?)", (f["media_id"], f["kind"]))
                c.execute("UPDATE media SET status='pending', error=NULL WHERE id=?", (f["media_id"],))
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


def _save_library(library_id: int | None, name: str, character_notes: str, model_ids: str, rating_cap: str, cooldown_turns: str,
                  start_heat: str = "1"):
    ids = [x.strip() for x in re.split(r"[\n,]+", model_ids) if x.strip()]
    cooldown = int(cooldown_turns) if cooldown_turns.strip() else None
    if rating_cap not in db.RATING_LEVELS:
        rating_cap = "explicit"
    heat = db.clamp_heat(start_heat, default=1)
    with db.tx() as c:
        if library_id is None:
            slug, n = _slugify(name), 1
            while c.execute("SELECT 1 FROM libraries WHERE slug=?", (slug,)).fetchone():
                n += 1
                slug = f"{_slugify(name)}-{n}"
            cur = c.execute(
                "INSERT INTO libraries(slug, name, character_notes, model_ids, rating_cap, cooldown_turns, start_heat) "
                "VALUES (?,?,?,?,?,?,?)",
                (slug, name.strip(), character_notes, json.dumps(ids), rating_cap, cooldown, heat),
            )
            return cur.lastrowid
        c.execute(
            "UPDATE libraries SET name=?, character_notes=?, model_ids=?, rating_cap=?, cooldown_turns=?, start_heat=? WHERE id=?",
            (name.strip(), character_notes, json.dumps(ids), rating_cap, cooldown, heat, library_id),
        )
        return library_id


@admin.post("/libraries/new")
def library_create(name: str = Form(...), character_notes: str = Form(""), model_ids: str = Form(""),
                   rating_cap: str = Form("explicit"), cooldown_turns: str = Form(""), start_heat: str = Form("1")):
    lid = _save_library(None, name, character_notes, model_ids, rating_cap, cooldown_turns, start_heat)
    return RedirectResponse(f"/libraries/{lid}", status_code=303)


@admin.get("/libraries/{library_id}/edit", response_class=HTMLResponse)
def library_edit(request: Request, library_id: int):
    lib = _lib_or_404(library_id)
    return _render(request, "library_form.html", lib=lib, model_ids="\n".join(db.library_model_ids(lib)), error=None)


@admin.post("/libraries/{library_id}/edit")
def library_update(library_id: int, name: str = Form(...), character_notes: str = Form(""), model_ids: str = Form(""),
                   rating_cap: str = Form("explicit"), cooldown_turns: str = Form(""), start_heat: str = Form("1")):
    _lib_or_404(library_id)
    _save_library(library_id, name, character_notes, model_ids, rating_cap, cooldown_turns, start_heat)
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


def _grid_query(library_id: int, status: str, kind: str, q: str, offset: int, after_id: int = 0):
    sql = "SELECT m.* FROM media m WHERE m.library_id=? AND m.id>?"
    params: list = [library_id, after_id]
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
    return _render(request, "library.html", counts=_counts(library_id), import_dirs=ingest.list_import_dirs(), scene_weight=search.SCENE_WEIGHT,
                   imp=ingest.import_status.get(library_id), model_ids=db.library_model_ids(lib), **ctx)


def _counts(library_id: int) -> dict:
    return {r["status"]: r["n"] for r in db.conn().execute(
        "SELECT status, COUNT(*) n FROM media WHERE library_id=? GROUP BY status", (library_id,))}


def _card_html(m) -> str:
    return templates.get_template("_card.html").render(m=m, media_url=media_url, card_state=card_state)


class UpdatesRequest(BaseModel):
    after: int = 0
    cards: dict[int, str] = {}  # id -> state the page currently shows
    status: str = ""
    kind: str = ""
    q: str = ""


@admin.post("/libraries/{library_id}/updates")
def library_updates(library_id: int, req: UpdatesRequest):
    """Live grid updates: new items since `after`, plus re-rendered cards whose state changed."""
    _lib_or_404(library_id)
    new_items, _ = _grid_query(library_id, req.status, req.kind, req.q, 0, after_id=req.after)
    changed: dict[int, str | None] = {}
    watched = dict(list(req.cards.items())[:2000])
    if watched:
        marks = ",".join("?" * len(watched))
        found = {m["id"]: m for m in db.conn().execute(
            f"SELECT * FROM media WHERE library_id=? AND id IN ({marks})", (library_id, *watched))}
        for mid, state in watched.items():
            m = found.get(mid)
            if m is None:
                changed[mid] = None  # deleted
            elif card_state(m) != state:
                changed[mid] = _card_html(m)
    return {
        "new": [_card_html(m) for m in new_items],  # newest first
        "max_id": max([req.after, *(m["id"] for m in new_items)]),
        "changed": changed,
        "counts": templates.get_template("_counts.html").render(counts=_counts(library_id)),
    }


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
def library_search(request: Request, library_id: int, q: str = "", media_type: str = "any", scene: str = "", max_heat: str = ""):
    lib = _lib_or_404(library_id)
    hits, error = [], None
    if q.strip():
        try:
            hits = search.search(lib, q, media_type, limit=12, scene=scene or None,
                                 max_heat=db.clamp_heat(max_heat) if max_heat.strip() else None)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    return _render(request, "_search_results.html", hits=hits, error=error, q=q, min_score=get_settings().min_score)


# --- media ----------------------------------------------------------------


@admin.get("/media/{media_id}", response_class=HTMLResponse)
def media_view(request: Request, media_id: int):
    m = _media_or_404(media_id)
    lib = _lib_or_404(m["library_id"])
    sends = db.conn().execute("SELECT COUNT(*) n, MAX(sent_at) last FROM sends WHERE media_id=?", (media_id,)).fetchone()
    return _render(request, "media.html", m=m, lib=lib, tags=db.media_tags(media_id), sends=sends,
                   job=_active_job(media_id), paused=tagger.is_paused(), frames=json.loads(m["frames"] or "[]"))


def _active_job(media_id: int):
    return db.conn().execute(
        "SELECT * FROM jobs WHERE media_id=? AND status IN ('queued','running') ORDER BY id LIMIT 1", (media_id,)
    ).fetchone()


@admin.get("/media/{media_id}/job", response_class=HTMLResponse)
def media_job(request: Request, media_id: int):
    """Polled by the media page while a job is active; reloads the page once the work is done."""
    _media_or_404(media_id)
    job = _active_job(media_id)
    if job:
        return _render(request, "_job_status.html", m={"id": media_id}, job=job, paused=tagger.is_paused())
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@admin.post("/media/{media_id}")
async def media_save(request: Request, media_id: int):
    m = _media_or_404(media_id)
    form = await request.form()
    caption = str(form.get("caption", "")).strip()
    rating = str(form.get("rating", "")) if form.get("rating") in db.RATING_LEVELS else m["rating"]
    heat = db.clamp_heat(form.get("heat"), default=m["heat"])
    enabled = 1 if form.get("enabled") else 0
    tags = {cat: [t for t in str(form.get(f"tag_{cat}", "")).split(",") if t.strip()] for cat in db.TAG_CATEGORIES}
    with db.tx() as c:
        c.execute("UPDATE media SET caption=?, rating=?, heat=?, enabled=?, tags_locked=1 WHERE id=?",
                  (caption, rating, heat, enabled, media_id))
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


@admin.get("/media/{media_id}/frame/{n}")
def media_frame(media_id: int, n: int):
    m = _media_or_404(media_id)
    frames = json.loads(m["frames"] or "[]")
    if not 0 <= n < len(frames):
        raise HTTPException(404)
    return FileResponse(ingest.derived_path(frames[n]), media_type="image/jpeg")


# --- toy (Intiface bridge) ------------------------------------------------


@admin.get("/toy", response_class=HTMLResponse)
def toy_panel(request: Request):
    return _render(request, "_toy.html", t=toy.bridge.status(), note=None)


@admin.post("/toy/{action}", response_class=HTMLResponse)
async def toy_action(request: Request, action: str):
    b = toy.bridge
    note = None
    try:
        if action == "stop":
            await b.stop_pattern()
        elif action == "test":
            used = await b.play("pulse", 30, 5)
            note = f"pulse at {used['intensity']}% for {used['duration_s']}s"
        elif action == "scan":
            await b.scan()
            note = "scanning"
        else:
            raise HTTPException(404)
    except (ConnectionError, LookupError, RuntimeError, asyncio.TimeoutError) as e:
        note = f"error: {e}"
    return _render(request, "_toy.html", t=b.status(), note=note)


# --- settings (tagging prompt) --------------------------------------------


@admin.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, media_id: str = "", saved: str = ""):
    custom = db.get_setting("tag_prompt")
    return _render(request, "settings.html", prompt=custom or ollama.DEFAULT_PROMPT, custom=bool(custom),
                   default_prompt=ollama.DEFAULT_PROMPT, media_id=media_id, saved=saved, settings=get_settings())


@admin.post("/settings/prompt")
def settings_save_prompt(prompt: str = Form(""), action: str = Form("save")):
    text = prompt.replace("\r\n", "\n").strip()
    if action == "reset" or not text or text == ollama.DEFAULT_PROMPT.strip():
        db.delete_setting("tag_prompt")
        return RedirectResponse("/settings?saved=reset", status_code=303)
    db.set_setting("tag_prompt", text)
    return RedirectResponse("/settings?saved=1", status_code=303)


@admin.post("/settings/preview", response_class=HTMLResponse)
def settings_preview(request: Request, prompt: str = Form(""), media_id: str = Form("")):
    """Run the tagger with the (unsaved) prompt on one item and show the result. Nothing is stored."""
    mid = media_id.strip().lstrip("#")
    if not mid.isdigit():
        return HTMLResponse("<p class='err'>Enter a media ID (shown on each item's page, e.g. #42).</p>")
    m = db.conn().execute("SELECT * FROM media WHERE id=?", (int(mid),)).fetchone()
    if not m:
        return HTMLResponse(f"<p class='err'>No media #{mid}.</p>")
    try:
        result = tagger.run_tagger(m["id"], system_prompt=prompt.replace("\r\n", "\n").strip() or None)
    except Exception as e:
        return HTMLResponse(f"<p class='err'>{html.escape(f'{type(e).__name__}: {e}')}</p>")
    return _render(request, "_preview.html", m=m, result=result, current_tags=db.media_tags(m["id"]))


@admin.post("/settings/retag-all", response_class=HTMLResponse)
def settings_retag_all():
    ids = [r["id"] for r in db.conn().execute("SELECT id FROM media WHERE tags_locked=0 AND thumb_path IS NOT NULL")]
    n = tagger.enqueue(ids, "tag")
    return HTMLResponse(f"<span class='ok'>Queued {n} item(s) across all libraries (manually edited items skipped).</span>")


router.include_router(admin)
