import hashlib
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps

from . import db
from .config import get_settings

log = logging.getLogger("rpmedia.ingest")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
PLAYABLE_VIDEO_CODECS = {"h264"}  # safest for every browser; everything else gets transcoded

# Folder-import progress, keyed by library id.
import_status: dict[int, dict] = {}


def kind_for(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


def source_path(row) -> Path:
    s = get_settings()
    root = s.uploads_dir if row["source"] == "upload" else s.import_root
    return root / row["rel_path"]


def derived_path(rel: str) -> Path:
    return get_settings().derived_dir / rel


def playable_path(row) -> Path:
    return derived_path(row["play_path"]) if row["play_path"] else source_path(row)


# --- registration ---------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _register(library_id: int, kind: str, source: str, rel_path: str, sha: str, mime: str) -> tuple[int, bool]:
    """Insert a media row + prepare job. Returns (media_id, created)."""
    with db.tx() as c:
        row = c.execute("SELECT id FROM media WHERE library_id=? AND sha256=?", (library_id, sha)).fetchone()
        if row:
            return row["id"], False
        cur = c.execute(
            "INSERT INTO media(library_id, kind, source, rel_path, sha256, mime) VALUES (?,?,?,?,?,?)",
            (library_id, kind, source, rel_path, sha, mime),
        )
        media_id = cur.lastrowid
        c.execute("INSERT INTO jobs(media_id, kind) VALUES (?, 'prepare')", (media_id,))
        return media_id, True


def save_upload(library_id: int, library_slug: str, filename: str, stream: BinaryIO) -> tuple[int | None, str]:
    """Store an uploaded file. Returns (media_id, status) where status is created|duplicate|unsupported."""
    kind = kind_for(filename)
    if not kind:
        return None, "unsupported"
    s = get_settings()
    ext = Path(filename).suffix.lower()
    h = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=s.uploads_dir, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
        sha = h.hexdigest()
        rel = f"{library_slug}/{sha[:2]}/{sha}{ext}"
        dest = s.uploads_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            os.replace(tmp, dest)
        mime = mimetypes.guess_type(filename)[0] or ("video/mp4" if kind == "video" else "image/jpeg")
        media_id, created = _register(library_id, kind, "upload", rel, sha, mime)
        return media_id, "created" if created else "duplicate"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def import_folder(library_id: int, subdir: str) -> None:
    """Walk import_root/subdir and register every media file (runs in a background thread)."""
    s = get_settings()
    root = (s.import_root / subdir).resolve()
    st = import_status[library_id] = {"running": True, "scanned": 0, "added": 0, "duplicates": 0, "errors": 0, "path": subdir}
    try:
        if not root.is_relative_to(s.import_root.resolve()) or not root.is_dir():
            raise ValueError(f"not a folder under the import root: {subdir}")
        for dirpath, _, files in os.walk(root):
            for name in sorted(files):
                kind = kind_for(name)
                if not kind:
                    continue
                p = Path(dirpath) / name
                st["scanned"] += 1
                try:
                    rel = p.relative_to(s.import_root.resolve()).as_posix()
                    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    _, created = _register(library_id, kind, "import", rel, _sha256_file(p), mime)
                    st["added" if created else "duplicates"] += 1
                except Exception:
                    log.exception("import failed for %s", p)
                    st["errors"] += 1
    except Exception as e:
        st["error"] = str(e)
    finally:
        st["running"] = False


def start_import(library_id: int, subdir: str) -> bool:
    if import_status.get(library_id, {}).get("running"):
        return False
    threading.Thread(target=import_folder, args=(library_id, subdir), daemon=True).start()
    return True


def list_import_dirs() -> list[str]:
    root = get_settings().import_root
    if not root.is_dir():
        return []
    out = [""]
    for dirpath, dirnames, _ in os.walk(root):
        dirnames.sort()
        rel = Path(dirpath).relative_to(root).as_posix()
        if rel != ".":
            out.append(rel)
        if rel.count("/") >= 2:
            dirnames.clear()
    return out


# --- prepare (thumbnails, keyframes, transcode) ---------------------------


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, check=True, text=True,
    ).stdout
    return json.loads(out)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True)


def _thumb_from(img: Image.Image, dest: Path) -> None:
    size = get_settings().thumb_size
    t = img.copy()
    t.thumbnail((size, size))
    if t.mode not in ("RGB", "L"):
        t = t.convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    t.save(dest, "JPEG", quality=82)


def prepare(media_id: int) -> None:
    c = db.conn()
    row = c.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    if not row:
        return
    src = source_path(row)
    if not src.exists():
        raise FileNotFoundError(src)
    out_dir = derived_path(str(media_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    updates: dict = {}

    if row["kind"] == "image":
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            updates.update(width=im.width, height=im.height)
            _thumb_from(im, out_dir / "thumb.jpg")
        updates["thumb_path"] = f"{media_id}/thumb.jpg"
    else:
        probe = _ffprobe(src)
        vs = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
        if not vs:
            raise ValueError("no video stream")
        duration = float(probe.get("format", {}).get("duration") or vs.get("duration") or 0)
        updates.update(width=vs.get("width"), height=vs.get("height"), duration=duration)

        n = max(1, get_settings().keyframes)
        frames = []
        for i in range(n):
            t = duration * (i + 0.5) / n if duration else 0
            fp = out_dir / f"frame{i}.jpg"
            _ffmpeg("-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-vf", "scale='min(1024,iw)':-2", str(fp))
            if fp.exists():
                frames.append(f"{media_id}/frame{i}.jpg")
        if not frames:
            raise ValueError("could not extract frames")
        poster = out_dir / "poster.jpg"
        shutil.copyfile(derived_path(frames[0]), poster)
        with Image.open(poster) as im:
            _thumb_from(im, out_dir / "thumb.jpg")
        updates.update(frames=json.dumps(frames), poster_path=f"{media_id}/poster.jpg", thumb_path=f"{media_id}/thumb.jpg")

        container_ok = src.suffix.lower() in (".mp4", ".m4v")
        if vs.get("codec_name") not in PLAYABLE_VIDEO_CODECS or not container_ok:
            play = out_dir / "play.mp4"
            _ffmpeg(
                "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                "-vf", "scale='min(1280,iw)':-2", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(play),
            )
            updates["play_path"] = f"{media_id}/play.mp4"
            updates["mime"] = "video/mp4"

    cols = ", ".join(f"{k}=?" for k in updates)
    with db.tx() as c:
        c.execute(f"UPDATE media SET {cols} WHERE id=?", (*updates.values(), media_id))
        c.execute("INSERT INTO jobs(media_id, kind) VALUES (?, 'tag')", (media_id,))


def delete_media_files(row) -> None:
    shutil.rmtree(derived_path(str(row["id"])), ignore_errors=True)
    if row["source"] == "upload":
        # Only remove the original if no other library row references the same upload.
        others = db.conn().execute(
            "SELECT 1 FROM media WHERE source='upload' AND rel_path=? AND id<>?", (row["rel_path"], row["id"])
        ).fetchone()
        if not others:
            try:
                source_path(row).unlink()
            except FileNotFoundError:
                pass
