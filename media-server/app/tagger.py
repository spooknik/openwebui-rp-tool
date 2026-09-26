"""Background job workers.

- prepare worker (CPU): thumbnails, keyframes, transcodes. Always runs.
- gpu worker: vision tagging + embeddings via Ollama. Single-threaded and pausable,
  because the GPU is shared with the chat model.
"""

import json
import logging
import threading

import httpx

from . import db, ingest, ollama
from .config import get_settings

log = logging.getLogger("rpmedia.tagger")

_wake = threading.Event()
_stop = threading.Event()
_threads: list[threading.Thread] = []
current: dict[str, dict | None] = {"prepare": None, "gpu": None}


def wake() -> None:
    _wake.set()


def is_paused() -> bool:
    return db.get_setting("tagging_paused", "0") == "1"


def set_paused(paused: bool) -> None:
    db.set_setting("tagging_paused", "1" if paused else "0")
    wake()


def doc_text(caption: str, tags: dict[str, list[str]]) -> str:
    return f"{get_settings().prefixes()[0]}{caption}\nTags: {db.flatten_tags(tags)}"


# --- job helpers ----------------------------------------------------------


def _claim(kinds: tuple[str, ...]):
    with db.tx() as c:
        q = ",".join("?" * len(kinds))
        job = c.execute(f"SELECT * FROM jobs WHERE status='queued' AND kind IN ({q}) ORDER BY id LIMIT 1", kinds).fetchone()
        if job:
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=datetime('now') WHERE id=?", (job["id"],))
        return job


def _finish(job_id: int) -> None:
    db.conn().execute("UPDATE jobs SET status='done', updated_at=datetime('now') WHERE id=?", (job_id,))


def _fail(job, err: Exception, transient: bool) -> None:
    msg = f"{type(err).__name__}: {err}"[:500]
    with db.tx() as c:
        if transient:
            # Ollama unreachable etc.: don't burn an attempt, requeue.
            c.execute("UPDATE jobs SET status='queued', attempts=attempts-1, last_error=? WHERE id=?", (msg, job["id"]))
            return
        if job["attempts"] + 1 >= get_settings().tag_max_attempts:
            c.execute("UPDATE jobs SET status='failed', last_error=?, updated_at=datetime('now') WHERE id=?", (msg, job["id"]))
            c.execute("UPDATE media SET status='error', error=? WHERE id=?", (msg, job["media_id"]))
        else:
            c.execute("UPDATE jobs SET status='queued', last_error=?, updated_at=datetime('now') WHERE id=?", (msg, job["id"]))


def enqueue(media_ids: list[int], kind: str) -> int:
    n = 0
    with db.tx() as c:
        for mid in media_ids:
            if c.execute("SELECT 1 FROM jobs WHERE media_id=? AND kind=? AND status IN ('queued','running')", (mid, kind)).fetchone():
                continue
            c.execute("INSERT INTO jobs(media_id, kind) VALUES (?,?)", (mid, kind))
            if kind == "tag":
                c.execute("UPDATE media SET status='pending', error=NULL WHERE id=?", (mid,))
            n += 1
    wake()
    return n


def queue_stats() -> dict:
    rows = db.conn().execute("SELECT kind, status, COUNT(*) n FROM jobs WHERE status IN ('queued','running','failed') GROUP BY kind, status").fetchall()
    stats = {f"{r['kind']}_{r['status']}": r["n"] for r in rows}
    stats["paused"] = is_paused()
    stats["current"] = dict(current)
    return stats


# --- work -----------------------------------------------------------------


def _store_embedding(media_id: int) -> None:
    c = db.conn()
    m = c.execute("SELECT id, library_id, kind, caption, rating, enabled FROM media WHERE id=?", (media_id,)).fetchone()
    if not m:
        return
    vec = ollama.embed([doc_text(m["caption"], db.media_tags(media_id))])[0]
    s = get_settings()
    if db.ensure_vec_table(len(vec), s.embed_model):
        # Embedding model changed: everything else needs re-embedding too.
        ids = [r["id"] for r in c.execute("SELECT id FROM media WHERE status='ready' AND id<>?", (media_id,))]
        log.info("embedding model changed, re-embedding %d items", len(ids))
        enqueue(ids, "embed")
    with db.tx() as c:
        db.upsert_vec(c, m["id"], m["library_id"], m["kind"], m["rating"], bool(m["enabled"]), vec)


def _tag(media_id: int) -> None:
    c = db.conn()
    m = c.execute(
        "SELECT m.*, l.character_notes FROM media m JOIN libraries l ON l.id=m.library_id WHERE m.id=?", (media_id,)
    ).fetchone()
    if not m:
        return
    c.execute("UPDATE media SET status='tagging' WHERE id=?", (media_id,))
    if m["kind"] == "video":
        paths = [ingest.derived_path(p) for p in json.loads(m["frames"] or "[]")]
        if not paths:
            raise ValueError("video has no keyframes; re-run prepare")
    else:
        paths = [ingest.source_path(m)]
    result = ollama.tag_images(paths, m["kind"], m["character_notes"])
    with db.tx() as c:
        c.execute(
            "UPDATE media SET caption=?, rating=?, status='ready', error=NULL, tagged_at=datetime('now') WHERE id=?",
            (result["caption"], result["rating"], media_id),
        )
        db.write_tags(c, media_id, result["caption"], result["tags"])
    _store_embedding(media_id)


def _run_loop(name: str, kinds: tuple[str, ...], pausable: bool) -> None:
    while not _stop.is_set():
        if pausable and is_paused():
            _wake.wait(5)
            _wake.clear()
            continue
        try:
            job = _claim(kinds)
        except Exception:
            log.exception("claim failed")
            job = None
        if not job:
            _wake.wait(3)
            _wake.clear()
            continue
        current[name] = {"job": job["id"], "kind": job["kind"], "media_id": job["media_id"]}
        try:
            if job["kind"] == "prepare":
                ingest.prepare(job["media_id"])
            elif job["kind"] == "tag":
                _tag(job["media_id"])
            elif job["kind"] == "embed":
                _store_embedding(job["media_id"])
            _finish(job["id"])
            wake()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.warning("ollama unreachable (%s); retrying in 30s", e)
            _fail(job, e, transient=True)
            db.conn().execute("UPDATE media SET status='pending' WHERE id=? AND status='tagging'", (job["media_id"],))
            _stop.wait(30)
        except Exception as e:
            log.exception("job %s (%s) failed for media %s", job["id"], job["kind"], job["media_id"])
            _fail(job, e, transient=False)
        finally:
            current[name] = None


def start() -> None:
    _stop.clear()
    for name, kinds, pausable in (("prepare", ("prepare",), False), ("gpu", ("tag", "embed"), True)):
        t = threading.Thread(target=_run_loop, args=(name, kinds, pausable), name=f"worker-{name}", daemon=True)
        t.start()
        _threads.append(t)
    # Embedding model changed since last run? Re-embed everything (first job recreates the table).
    prev = db.get_setting("embed_model")
    if prev and prev != get_settings().embed_model:
        ids = [r["id"] for r in db.conn().execute("SELECT id FROM media WHERE status='ready'")]
        enqueue(ids, "embed")


def stop() -> None:
    _stop.set()
    _wake.set()
    for t in _threads:
        t.join(timeout=5)
    _threads.clear()
