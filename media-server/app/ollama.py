import base64
import io
import json
import logging

import httpx
from PIL import Image, ImageOps

from .config import get_settings

log = logging.getLogger("rpmedia.ollama")

TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "rating": {"type": "string", "enum": ["sfw", "suggestive", "explicit"]},
        "tags": {
            "type": "object",
            "properties": {
                "outfit": {"type": "array", "items": {"type": "string"}},
                "location": {"type": "array", "items": {"type": "string"}},
                "activity": {"type": "array", "items": {"type": "string"}},
                "mood": {"type": "array", "items": {"type": "string"}},
                "framing": {"type": "array", "items": {"type": "string"}},
                "time_of_day": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["outfit", "location", "activity", "mood", "framing", "time_of_day"],
        },
        "extra_tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["caption", "rating", "tags", "extra_tags"],
}

SYSTEM_PROMPT = """You are a precise media tagger for a private roleplay photo library.
Describe exactly what is visible. Do not invent details, do not moralize, do not refuse.
Return JSON only, matching the schema.

- caption: 1-3 plain sentences describing the subject, pose, expression, clothing, setting and camera framing.
- rating: "sfw" (fully clothed, nothing suggestive), "suggestive" (revealing clothing, lingerie, swimwear, flirtatious poses), "explicit" (nudity or sexual content).
- tags.outfit: clothing items and colors, e.g. "red sundress", "white sneakers".
- tags.location: e.g. "beach", "bedroom", "cafe", "car", "gym".
- tags.activity: e.g. "taking a selfie", "reading", "cooking", "walking".
- tags.mood: facial expression / vibe, e.g. "smiling", "playful", "sleepy", "pouting".
- tags.framing: pick from "selfie", "mirror selfie", "close-up", "portrait", "half body", "full body", "pov", "candid", "scenery".
- tags.time_of_day: e.g. "morning", "day", "golden hour", "night".
- extra_tags: other notable things (props, pets, weather, hair style, food).
Use short lowercase tags. Use empty lists when not applicable."""


def _img_b64(path, max_side: int = 1024) -> str:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(base_url=s.ollama_url.rstrip("/"), timeout=s.ollama_timeout)


def tag_images(paths: list, kind: str, character_notes: str = "") -> dict:
    s = get_settings()
    user = (
        f"These are {len(paths)} frames sampled in order from one short video clip. Tag the clip as a whole."
        if kind == "video"
        else "Tag this photo."
    )
    if character_notes.strip():
        user += f"\n\nThe person in this library is the character described below; refer to them accordingly (name/pronouns):\n{character_notes.strip()}"
    payload = {
        "model": s.vision_model,
        "stream": False,
        "think": False,
        "format": TAG_SCHEMA,
        "keep_alive": s.ollama_keep_alive,
        "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user, "images": [_img_b64(p) for p in paths]},
        ],
    }
    with _client() as c:
        r = c.post("/api/chat", json=payload)
        if r.status_code == 400 and "think" in r.text:
            payload.pop("think")  # model without thinking support
            r = c.post("/api/chat", json=payload)
        r.raise_for_status()
        content = r.json()["message"]["content"]
    data = json.loads(content)
    tags = data.get("tags") or {}
    return {
        "caption": str(data.get("caption", "")).strip(),
        "rating": data.get("rating") if data.get("rating") in ("sfw", "suggestive", "explicit") else "suggestive",
        "tags": {
            **{k: [str(x) for x in (tags.get(k) or []) if str(x).strip()] for k in ("outfit", "location", "activity", "mood", "framing", "time_of_day")},
            "extra": [str(x) for x in (data.get("extra_tags") or []) if str(x).strip()],
        },
    }


def embed(texts: list[str]) -> list[list[float]]:
    s = get_settings()
    with _client() as c:
        r = c.post("/api/embed", json={"model": s.embed_model, "input": texts, "keep_alive": s.ollama_keep_alive})
        r.raise_for_status()
        return r.json()["embeddings"]


def list_models() -> list[str]:
    with _client() as c:
        r = c.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
