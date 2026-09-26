"""Signed media file serving. Starlette's FileResponse handles Range requests for video seeking."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import db, ingest
from ..signing import verify

router = APIRouter()

CACHE = {"Cache-Control": "private, max-age=31536000, immutable"}


@router.get("/m/{media_id}/{variant}")
def serve(media_id: int, variant: str, sig: str = ""):
    if not verify(media_id, variant, sig):
        raise HTTPException(403, "bad signature")
    m = db.conn().execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    if not m:
        raise HTTPException(404)
    if variant == "full":
        path, mime = ingest.playable_path(m), m["mime"]
    else:
        rel = m["thumb_path"] if variant == "thumb" else m["poster_path"]
        if not rel:
            raise HTTPException(404)
        path, mime = ingest.derived_path(rel), "image/jpeg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=mime, headers=CACHE)
