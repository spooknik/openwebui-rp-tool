"""JSON API used by the Open WebUI tool (Bearer TOOL_API_KEY)."""

import asyncio
import hmac
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .. import db, search, toy
from ..config import get_settings
from ..signing import media_url

router = APIRouter(prefix="/api")


def require_tool_key(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, get_settings().tool_api_key):
        raise HTTPException(401, "invalid api key")


class SendRequest(BaseModel):
    description: str
    model_id: str | None = None
    library: str | None = None  # slug; overrides model_id lookup
    media_type: Literal["image", "video", "any"] = "image"
    user_requested: bool = False
    chat_id: str | None = None
    message_id: str | None = None
    user_turn: int | None = None


class SearchRequest(BaseModel):
    description: str
    model_id: str | None = None
    library: str | None = None
    media_type: Literal["image", "video", "any"] = "any"
    chat_id: str | None = None
    limit: int = 10


def _resolve_library(model_id: str | None, slug: str | None):
    lib = search.library_by_slug(slug) if slug else (search.library_for_model(model_id) if model_id else None)
    if not lib:
        raise HTTPException(404, f"no library linked to model '{model_id}'" if not slug else f"no library '{slug}'")
    return lib


def media_payload(media_id: int) -> dict:
    m = db.conn().execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    return {
        "id": m["id"],
        "kind": m["kind"],
        "url": media_url(m["id"], "full"),
        "poster_url": media_url(m["id"], "poster") if m["poster_path"] else None,
        "thumb_url": media_url(m["id"], "thumb"),
        "mime": m["mime"],
        "width": m["width"],
        "height": m["height"],
        "duration": m["duration"],
        "caption": m["caption"],
        "rating": m["rating"],
        "tags": db.media_tags(m["id"]),
    }


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/libraries", dependencies=[Depends(require_tool_key)])
def libraries():
    rows = db.conn().execute(
        "SELECT l.*, (SELECT COUNT(*) FROM media m WHERE m.library_id=l.id AND m.status='ready' AND m.enabled=1) AS ready "
        "FROM libraries l ORDER BY name"
    ).fetchall()
    return [{"slug": r["slug"], "name": r["name"], "model_ids": db.library_model_ids(r), "ready": r["ready"]} for r in rows]


@router.post("/search", dependencies=[Depends(require_tool_key)])
def api_search(req: SearchRequest):
    lib = _resolve_library(req.model_id, req.library)
    hits = search.search(lib, req.description, req.media_type, exclude=search.sent_ids(req.chat_id), limit=req.limit)
    return [{"score": round(h.score, 4), "vec": round(h.vec_score, 4), "lex": round(h.lex_score, 4), **media_payload(h.media_id)} for h in hits]


@router.post("/send", dependencies=[Depends(require_tool_key)])
def api_send(req: SendRequest):
    lib = _resolve_library(req.model_id, req.library)

    if not req.user_requested:
        remaining = search.cooldown_remaining(lib, req.chat_id, req.user_turn)
        if remaining:
            return {"status": "cooldown", "turns_remaining": remaining}

    hits = search.search(lib, req.description, req.media_type, exclude=search.sent_ids(req.chat_id))
    chosen = search.pick(hits, get_settings().min_score)
    if not chosen:
        return {"status": "no_match", "best_score": round(hits[0].score, 4) if hits else None}

    if req.chat_id:
        search.record_send(req.chat_id, req.message_id, chosen.media_id, lib["id"], req.model_id, req.user_turn)
    return {"status": "sent", "score": round(chosen.score, 4), "media": media_payload(chosen.media_id)}


# --- toy control (Intiface Central bridge) ----------------------------------


class ToyRequest(BaseModel):
    action: Literal["play", "stop"] = "play"
    pattern: str = "wave"
    intensity: int = 40  # 0..100
    duration_s: int = 60
    chat_id: str | None = None


@router.get("/toy", dependencies=[Depends(require_tool_key)])
def api_toy_status():
    return {"patterns": {k: v[0] for k, v in toy.PATTERNS.items()}, **toy.bridge.status()}


@router.post("/toy", dependencies=[Depends(require_tool_key)])
async def api_toy(req: ToyRequest):
    b = toy.bridge
    if not b.configured:
        return {"status": "unavailable", "reason": "INTIFACE_URL is not set"}
    if req.action == "stop":
        await b.stop_pattern()
        return {"status": "stopped", **b.status()}
    if req.pattern not in toy.PATTERNS:
        raise HTTPException(422, f"unknown pattern; use one of {', '.join(toy.PATTERNS)}")
    if not b.connected:
        return {"status": "unavailable", "reason": b.error or "not connected to Intiface"}
    if not b.vibrating_devices():
        return {"status": "no_device", "reason": "no vibrating device connected in Intiface"}
    try:
        used = await b.play(req.pattern, req.intensity, req.duration_s)
    except (ConnectionError, LookupError, RuntimeError, asyncio.TimeoutError) as e:
        return {"status": "unavailable", "reason": str(e)}
    return {"status": "stopped" if used["intensity"] == 0 else "playing", **used, "devices": b.status()["devices"]}
