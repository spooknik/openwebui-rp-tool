import base64
import io
import json
import logging

import httpx
from PIL import Image, ImageOps

from . import db
from .config import get_settings

log = logging.getLogger("rpmedia.ollama")

TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "rating": {"type": "string", "enum": ["sfw", "suggestive", "explicit"]},
        "heat": {"type": "integer", "minimum": 1, "maximum": 5},
        "tags": {
            "type": "object",
            "properties": {
                "outfit": {"type": "array", "items": {"type": "string"}},
                "location": {"type": "array", "items": {"type": "string"}},
                "activity": {"type": "array", "items": {"type": "string"}},
                "mood": {"type": "array", "items": {"type": "string"}},
                "framing": {"type": "array", "items": {"type": "string"}},
                "time_of_day": {"type": "array", "items": {"type": "string"}},
                "context": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["outfit", "location", "activity", "mood", "framing", "time_of_day", "context"],
        },
        "extra_tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["caption", "rating", "heat", "tags", "extra_tags"],
}

DEFAULT_PROMPT = """You are a precise media tagger for a private roleplay photo library.
Describe exactly what is visible. Do not invent details, do not moralize, do not refuse.
Return JSON only, matching the schema.

- caption: 1-3 plain sentences describing the subject, pose, expression, clothing, setting and camera framing.
- rating: "sfw" (fully clothed, nothing suggestive), "suggestive" (revealing clothing, lingerie, swimwear, flirtatious poses), "explicit" (nudity or sexual content).
- heat: how far into an intimate story this belongs, 1-5. 1 = everyday and innocent (casual clothes, nothing flirty). 2 = flirty or cute (a playful pose, a little skin, a wink). 3 = teasing (lingerie, swimwear, revealing outfit, suggestive posing, implied nudity). 4 = nude or very explicit posing, no sexual activity. 5 = sexual activity or explicit close-ups. Judge the vibe, not just the clothing: a sweet fully-dressed cuddle is 2, a clothed but very provocative pose is 3.
- tags.outfit: clothing items and colors, e.g. "red sundress", "white sneakers".
- tags.location: e.g. "beach", "bedroom", "cafe", "car", "gym".
- tags.activity: e.g. "taking a selfie", "reading", "cooking", "walking".
- tags.mood: facial expression / vibe, e.g. "smiling", "playful", "sleepy", "pouting".
- tags.framing: pick from "selfie", "mirror selfie", "close-up", "portrait", "half body", "full body", "pov", "candid", "scenery".
- tags.time_of_day: e.g. "morning", "day", "golden hour", "night".
- tags.context: 2-4 moments in a story when this would naturally be sent, judged from what is shown, e.g. "good morning text", "getting ready to go out", "after a workout", "just got home", "bedtime", "lazy sunday", "date night", "teasing", "morning after", "showing off a new outfit", "on holiday".
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


class OllamaError(RuntimeError):
    pass


def _check(r: httpx.Response, model: str) -> None:
    """Raise with Ollama's own error text (e.g. 'model not found') instead of a bare HTTP status."""
    if r.is_success:
        return
    try:
        detail = r.json().get("error") or r.text
    except ValueError:
        detail = r.text
    if r.status_code == 404 and "not found" in detail.lower():
        raise OllamaError(f"Ollama does not have model '{model}'. Run: ollama pull {model}")
    raise OllamaError(f"Ollama {r.status_code} for {r.request.url.path}: {detail[:300]}")


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(base_url=s.ollama_url.rstrip("/"), timeout=s.ollama_timeout)


def current_prompt() -> str:
    """The tagging prompt: the one saved in the Settings page, or the built-in default."""
    return db.get_setting("tag_prompt") or DEFAULT_PROMPT


def tag_images(paths: list, kind: str, character_notes: str = "", system_prompt: str | None = None) -> dict:
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
            {"role": "system", "content": system_prompt or current_prompt()},
            {"role": "user", "content": user, "images": [_img_b64(p) for p in paths]},
        ],
    }
    with _client() as c:
        r = c.post("/api/chat", json=payload)
        if r.status_code == 400 and "think" in r.text:
            payload.pop("think")  # model without thinking support
            r = c.post("/api/chat", json=payload)
        _check(r, s.vision_model)
        content = r.json()["message"]["content"]
    data = json.loads(content)
    tags = data.get("tags") or {}
    rating = data.get("rating") if data.get("rating") in ("sfw", "suggestive", "explicit") else "suggestive"
    return {
        "caption": str(data.get("caption", "")).strip(),
        "rating": rating,
        "heat": consistent_heat(db.clamp_heat(data.get("heat")), rating),
        "tags": {
            **{k: [str(x) for x in (tags.get(k) or []) if str(x).strip()] for k in ("outfit", "location", "activity", "mood", "framing", "time_of_day", "context")},
            "extra": [str(x) for x in (data.get("extra_tags") or []) if str(x).strip()],
        },
    }


# Heat ranges each rating can sensibly have; the vision model occasionally contradicts itself.
HEAT_BY_RATING = {"sfw": (1, 2), "suggestive": (2, 3), "explicit": (4, 5)}


def consistent_heat(heat: int, rating: str) -> int:
    lo, hi = HEAT_BY_RATING.get(rating, (1, 5))
    return max(lo, min(hi, heat))


def embed(texts: list[str]) -> list[list[float]]:
    s = get_settings()
    with _client() as c:
        r = c.post("/api/embed", json={"model": s.embed_model, "input": texts, "keep_alive": s.ollama_keep_alive})
        _check(r, s.embed_model)
        return r.json()["embeddings"]


def list_models() -> list[str]:
    with _client() as c:
        r = c.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
