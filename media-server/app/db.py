import json
import sqlite3
import struct
import threading
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from .config import get_settings

RATING_LEVELS = {"sfw": 0, "suggestive": 1, "explicit": 2}
TAG_CATEGORIES = ["outfit", "location", "activity", "mood", "framing", "time_of_day", "extra"]

_SCHEMA = Path(__file__).with_name("schema.sql")
_local = threading.local()
_write_lock = threading.RLock()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def conn() -> sqlite3.Connection:
    """Per-thread connection (re-opened if the configured DB path changes, e.g. between tests)."""
    path = get_settings().db_path
    c = getattr(_local, "conn", None)
    if c is None or _local.path != path:
        if c is not None:
            c.close()
        c = _connect(path)
        _local.conn, _local.path = c, path
    return c


def reset_connections() -> None:
    """Drop this thread's cached connection (tests)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None


@contextmanager
def tx():
    """Serialized write transaction."""
    c = conn()
    with _write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")


def init_db() -> None:
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    s.derived_dir.mkdir(parents=True, exist_ok=True)
    c = conn()
    c.executescript(_SCHEMA.read_text(encoding="utf-8"))
    # Jobs left 'running' by a crash go back to the queue.
    c.execute("UPDATE jobs SET status='queued' WHERE status='running'")
    c.execute("UPDATE media SET status='pending' WHERE status='tagging'")


# --- settings -------------------------------------------------------------


def get_setting(key: str, default: str | None = None) -> str | None:
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn().execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def delete_setting(key: str) -> None:
    conn().execute("DELETE FROM settings WHERE key=?", (key,))


# --- vectors --------------------------------------------------------------


def pack_vec(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def vec_dim() -> int | None:
    d = get_setting("embed_dim")
    return int(d) if d else None


def ensure_vec_table(dim: int, model: str) -> bool:
    """Create media_vec for this dim/model. Returns True if the table was (re)created and needs a re-embed."""
    cur_dim, cur_model = vec_dim(), get_setting("embed_model")
    if cur_dim == dim and cur_model == model:
        return False
    with tx() as c:
        c.execute("DROP TABLE IF EXISTS media_vec")
        c.execute(
            f"""CREATE VIRTUAL TABLE media_vec USING vec0(
                media_id integer primary key,
                library_id integer partition key,
                kind text,
                rating_level integer,
                enabled integer,
                embedding float[{dim}] distance_metric=cosine
            )"""
        )
        c.execute("INSERT INTO settings(key,value) VALUES('embed_dim',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(dim),))
        c.execute("INSERT INTO settings(key,value) VALUES('embed_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (model,))
    return cur_dim is not None or cur_model is not None


def vec_table_exists() -> bool:
    return conn().execute("SELECT 1 FROM sqlite_master WHERE name='media_vec'").fetchone() is not None


def upsert_vec(c: sqlite3.Connection, media_id: int, library_id: int, kind: str, rating: str | None, enabled: bool, vec: list[float]) -> None:
    c.execute("DELETE FROM media_vec WHERE media_id=?", (media_id,))
    c.execute(
        "INSERT INTO media_vec(media_id, library_id, kind, rating_level, enabled, embedding) VALUES (?,?,?,?,?,?)",
        (media_id, library_id, kind, RATING_LEVELS.get(rating or "explicit", 2), int(enabled), pack_vec(vec)),
    )


def sync_vec_meta(c: sqlite3.Connection, media_id: int) -> None:
    """Push enabled/rating changes from media into media_vec."""
    if not vec_table_exists():
        return
    row = c.execute("SELECT rating, enabled FROM media WHERE id=?", (media_id,)).fetchone()
    if row:
        c.execute(
            "UPDATE media_vec SET rating_level=?, enabled=? WHERE media_id=?",
            (RATING_LEVELS.get(row["rating"] or "explicit", 2), row["enabled"], media_id),
        )


def delete_vec(c: sqlite3.Connection, media_id: int) -> None:
    if vec_table_exists():
        c.execute("DELETE FROM media_vec WHERE media_id=?", (media_id,))


# --- tags / fts -----------------------------------------------------------


def media_tags(media_id: int) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in conn().execute("SELECT category, value FROM media_tags WHERE media_id=? ORDER BY category, value", (media_id,)):
        out.setdefault(r["category"], []).append(r["value"])
    return out


def flatten_tags(tags: dict[str, list[str]]) -> str:
    return ", ".join(v for cat in TAG_CATEGORIES for v in tags.get(cat, []))


def write_tags(c: sqlite3.Connection, media_id: int, caption: str, tags: dict[str, list[str]]) -> None:
    c.execute("DELETE FROM media_tags WHERE media_id=?", (media_id,))
    for cat, values in tags.items():
        for v in {normalize_tag(x) for x in values if normalize_tag(x)}:
            c.execute("INSERT OR IGNORE INTO media_tags(media_id, category, value) VALUES (?,?,?)", (media_id, cat, v))
    c.execute("DELETE FROM media_fts WHERE rowid=?", (media_id,))
    c.execute("INSERT INTO media_fts(rowid, caption, tags) VALUES (?,?,?)", (media_id, caption, flatten_tags(tags)))


def delete_fts(c: sqlite3.Connection, media_id: int) -> None:
    c.execute("DELETE FROM media_fts WHERE rowid=?", (media_id,))


def normalize_tag(t: str) -> str:
    return " ".join(str(t).strip().lower().replace("_", " ").split())


def library_model_ids(row: sqlite3.Row) -> list[str]:
    try:
        return list(json.loads(row["model_ids"] or "[]"))
    except (ValueError, TypeError):
        return []
