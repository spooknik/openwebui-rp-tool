import hashlib
import math
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DIM = 64


def fake_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic bag-of-words hashing embedding: similar words -> similar vectors."""
    out = []
    for t in texts:
        v = [0.0] * DIM
        for w in re.findall(r"[a-z]+", t.lower()):
            if w in ("search", "document", "query", "tags"):
                continue
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


# Captions keyed by a marker colour in the test image, so the fake tagger is deterministic.
FAKE_TAGS = {
    "red": {"caption": "Luna takes a selfie at the beach wearing a red bikini, smiling.", "rating": "suggestive", "heat": 3,
            "tags": {"outfit": ["red bikini"], "location": ["beach"], "activity": ["taking a selfie"], "mood": ["smiling"],
                     "framing": ["selfie"], "time_of_day": ["day"], "context": ["on holiday", "teasing"], "extra": []}},
    "blue": {"caption": "Luna reads a book in a cozy cafe wearing a blue sweater.", "rating": "sfw", "heat": 1,
             "tags": {"outfit": ["blue sweater"], "location": ["cafe"], "activity": ["reading"], "mood": ["calm"],
                      "framing": ["half body"], "time_of_day": ["morning"], "extra": ["coffee", "book"]}},
    "green": {"caption": "Luna in the gym doing a workout in green leggings.", "rating": "sfw",
              "tags": {"outfit": ["green leggings"], "location": ["gym"], "activity": ["working out"], "mood": ["focused"],
                       "framing": ["full body"], "time_of_day": ["evening"], "extra": []}},
}


def fake_tag_images(paths, kind, character_notes="", system_prompt=None):
    from PIL import Image

    with Image.open(paths[0]) as im:
        r, g, b = im.convert("RGB").resize((1, 1)).getpixel((0, 0))
    key = "red" if r > max(g, b) else "green" if g > max(r, b) else "blue"
    return FAKE_TAGS[key]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IMPORT_ROOT", str(tmp_path / "import"))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://media.example.test")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("TOOL_API_KEY", "tool-key")
    monkeypatch.setenv("SIGNING_SECRET", "secret")
    monkeypatch.setenv("EMBED_MODEL", "fake-embed")
    monkeypatch.setenv("MIN_SCORE", "0.3")
    monkeypatch.setenv("DEFAULT_COOLDOWN_TURNS", "3")

    from app import config, db, ollama

    config.get_settings.cache_clear()
    db.reset_connections()
    monkeypatch.setattr(ollama, "embed", fake_embed)
    monkeypatch.setattr(ollama, "tag_images", fake_tag_images)
    db.init_db()
    yield tmp_path
    db.reset_connections()
    config.get_settings.cache_clear()


def make_image(path: Path, color: str) -> Path:
    from PIL import Image

    rgb = {"red": (220, 30, 30), "blue": (30, 30, 220), "green": (30, 200, 30)}[color]
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 480), rgb).save(path)
    return path
