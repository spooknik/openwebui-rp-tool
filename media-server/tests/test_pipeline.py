"""End-to-end: admin upload -> prepare -> (fake) tag -> (fake) embed -> tool API send -> signed media serving."""

import shutil
import subprocess
import time

import pytest
from conftest import make_image
from fastapi.testclient import TestClient

from app import db

TOOL = {"Authorization": "Bearer tool-key"}


def _wait_ready(n: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = db.conn().execute("SELECT status, error FROM media").fetchall()
        if any(r["status"] == "error" for r in rows):
            raise AssertionError([r["error"] for r in rows])
        if len(rows) >= n and all(r["status"] == "ready" for r in rows):
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out: {[tuple(r) for r in db.conn().execute('SELECT id, status, error FROM media')]}")


@pytest.fixture()
def client(env):
    from app.main import app

    with TestClient(app) as c:
        c.post("/login", data={"key": "admin-key"})
        yield c


def _create_library(client) -> int:
    r = client.post("/libraries/new", data={"name": "Luna", "model_ids": "luna-rp", "rating_cap": "explicit", "cooldown_turns": "2"},
                    follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_auth_required(env):
    from app.main import app

    with TestClient(app) as c:
        assert c.post("/api/send", json={"description": "x", "model_id": "luna-rp"}).status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 303
        assert c.post("/login", data={"key": "wrong"}).text.count("Wrong key") == 1


def test_full_pipeline(client, env):
    lib_id = _create_library(client)
    for color in ("red", "blue", "green"):
        p = make_image(env / "src" / f"{color}.png", color)
        with open(p, "rb") as f:
            r = client.post(f"/libraries/{lib_id}/upload", files={"file": (p.name, f, "image/png")})
        assert r.json()["status"] == "created"
    with open(env / "src" / "red.png", "rb") as f:
        assert client.post(f"/libraries/{lib_id}/upload", files={"file": ("dupe.png", f, "image/png")}).json()["status"] == "duplicate"

    _wait_ready(3)

    # Admin pages render.
    assert client.get("/").status_code == 200
    assert "Luna" in client.get(f"/libraries/{lib_id}").text
    assert "red bikini" in client.get(f"/libraries/{lib_id}/search", params={"q": "beach selfie"}).text

    req = {"description": "selfie at the beach in a red bikini", "model_id": "luna-rp", "chat_id": "c1",
           "user_requested": True, "user_turn": 1}
    r = client.post("/api/send", json=req, headers=TOOL).json()
    assert r["status"] == "sent", r
    media = r["media"]
    assert "red bikini" in media["caption"] and media["url"].startswith("https://media.example.test/m/")

    # Signed URL serves; tampered / unsigned is rejected.
    path = media["url"].replace("https://media.example.test", "")
    assert client.get(path).status_code == 200
    assert client.get(path[:-2] + "00").status_code == 403
    assert client.get(path.split("?")[0]).status_code == 403
    assert client.get(media["thumb_url"].replace("https://media.example.test", "")).headers["content-type"] == "image/jpeg"

    # Same request again in the same chat: never repeats the item.
    r2 = client.post("/api/send", json={**req, "user_turn": 2}, headers=TOOL).json()
    assert r2.get("media", {}).get("id") != media["id"]

    # Spontaneous send right after: cooldown (library cooldown = 2 turns).
    r3 = client.post("/api/send", json={**req, "user_requested": False, "user_turn": 2}, headers=TOOL).json()
    assert r3["status"] == "cooldown"

    # Unknown model -> 404 so the tool can tell the model there's no library.
    assert client.post("/api/send", json={**req, "model_id": "nope"}, headers=TOOL).status_code == 404

    # Manual edit is locked, re-embedded, and searchable.
    mid = media["id"]
    client.post(f"/media/{mid}", data={"caption": "Luna on a sailboat", "tag_location": "sailboat, sea", "rating": "sfw", "enabled": "on"})
    row = db.conn().execute("SELECT caption, tags_locked, rating FROM media WHERE id=?", (mid,)).fetchone()
    assert (row["caption"], row["tags_locked"], row["rating"]) == ("Luna on a sailboat", 1, "sfw")
    time.sleep(1.5)
    hits = client.post("/api/search", json={"description": "sailboat on the sea", "model_id": "luna-rp"}, headers=TOOL).json()
    assert hits[0]["id"] == mid


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_video_prepare_transcode_and_range(client, env):
    lib_id = _create_library(client)
    src = env / "src" / "clip.avi"
    src.parent.mkdir(parents=True, exist_ok=True)
    # Solid red mpeg4/avi clip: not browser-playable, so it must be transcoded.
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=3",
                    "-c:v", "mpeg4", str(src)], check=True)
    with open(src, "rb") as f:
        assert client.post(f"/libraries/{lib_id}/upload", files={"file": ("clip.avi", f, "video/x-msvideo")}).json()["status"] == "created"
    _wait_ready(1, timeout=120)

    m = db.conn().execute("SELECT * FROM media").fetchone()
    assert m["kind"] == "video" and m["play_path"] and m["poster_path"] and m["mime"] == "video/mp4"
    page = client.get(f"/media/{m['id']}").text
    assert "Frames the tagger saw" in page
    assert client.get(f"/media/{m['id']}/frame/0").headers["content-type"] == "image/jpeg"
    assert client.get(f"/media/{m['id']}/frame/9").status_code == 404
    assert 2.5 < m["duration"] < 3.5

    r = client.post("/api/send", json={"description": "beach selfie video", "model_id": "luna-rp", "media_type": "video",
                                       "user_requested": True, "chat_id": "c1"}, headers=TOOL).json()
    assert r["status"] == "sent" and r["media"]["poster_url"]
    path = r["media"]["url"].replace("https://media.example.test", "")
    resp = client.get(path, headers={"Range": "bytes=0-99"})
    assert resp.status_code == 206 and len(resp.content) == 100


def test_folder_import(client, env):
    lib_id = _create_library(client)
    make_image(env / "import" / "luna" / "a.png", "blue")
    make_image(env / "import" / "luna" / "sub" / "b.png", "green")
    (env / "import" / "luna" / "notes.txt").write_text("ignore me")
    assert "/media/luna" in client.get(f"/libraries/{lib_id}").text
    client.post(f"/libraries/{lib_id}/import", data={"subdir": "luna"})
    _wait_ready(2)
    rows = db.conn().execute("SELECT source, rel_path FROM media ORDER BY rel_path").fetchall()
    assert [(r["source"], r["rel_path"]) for r in rows] == [("import", "luna/a.png"), ("import", "luna/sub/b.png")]
    # Path traversal outside the import root is refused.
    client.post(f"/libraries/{lib_id}/import", data={"subdir": "../"})
    time.sleep(0.5)
    assert "not a folder under the import root" in client.get(f"/libraries/{lib_id}/import").text


def test_tagging_prompt_settings_and_preview(client, env, monkeypatch):
    from app import ollama

    assert ollama.current_prompt() == ollama.DEFAULT_PROMPT
    assert "built-in default" in client.get("/settings").text

    client.post("/settings/prompt", data={"prompt": "Custom prompt\r\nline two", "action": "save"})
    assert ollama.current_prompt() == "Custom prompt\nline two"
    assert "custom prompt" in client.get("/settings").text

    # Preview runs the tagger with the unsaved editor text and stores nothing.
    lib_id = _create_library(client)
    p = make_image(env / "src" / "red.png", "red")
    with open(p, "rb") as f:
        mid = client.post(f"/libraries/{lib_id}/upload", files={"file": (p.name, f, "image/png")}).json()["media_id"]
    _wait_ready(1)
    seen = {}
    real = ollama.tag_images
    monkeypatch.setattr(ollama, "tag_images", lambda *a, **kw: (seen.update(kw), real(*a, **kw))[1])
    before = db.conn().execute("SELECT caption FROM media WHERE id=?", (mid,)).fetchone()["caption"]
    r = client.post("/settings/preview", data={"prompt": "Try this", "media_id": f"#{mid}"})
    assert r.status_code == 200 and "red bikini" in r.text and seen["system_prompt"] == "Try this"
    assert db.conn().execute("SELECT caption FROM media WHERE id=?", (mid,)).fetchone()["caption"] == before
    assert "No media #999" in client.post("/settings/preview", data={"prompt": "x", "media_id": "999"}).text

    # Saving the default text (or reset) clears the custom prompt.
    client.post("/settings/prompt", data={"prompt": "", "action": "reset"})
    assert ollama.current_prompt() == ollama.DEFAULT_PROMPT


def test_live_grid_updates(client, env):
    lib_id = _create_library(client)
    r = client.post(f"/libraries/{lib_id}/updates", json={"after": 0, "cards": {}}).json()
    assert r["new"] == [] and r["max_id"] == 0

    p = make_image(env / "src" / "blue.png", "blue")
    with open(p, "rb") as f:
        mid = client.post(f"/libraries/{lib_id}/upload", files={"file": (p.name, f, "image/png")}).json()["media_id"]
    r = client.post(f"/libraries/{lib_id}/updates", json={"after": 0, "cards": {}}).json()
    assert r["max_id"] == mid and f'id="card-{mid}"' in r["new"][0]
    state = r["new"][0].split('data-state="')[1].split('"')[0]

    _wait_ready(1)
    r = client.post(f"/libraries/{lib_id}/updates", json={"after": mid, "cards": {str(mid): state}}).json()
    assert r["new"] == [] and "ready" in r["changed"][str(mid)] and "1 ready" in r["counts"]
    ready_state = r["changed"][str(mid)].split('data-state="')[1].split('"')[0]
    # Nothing changed -> nothing re-rendered; deleted cards are reported as null.
    assert client.post(f"/libraries/{lib_id}/updates", json={"after": mid, "cards": {str(mid): ready_state}}).json()["changed"] == {}
    client.post(f"/media/{mid}/delete")
    assert client.post(f"/libraries/{lib_id}/updates", json={"after": mid, "cards": {str(mid): ready_state}}).json()["changed"] == {str(mid): None}
    assert client.get("/libraries-table").status_code == 200


def test_media_page_polls_until_retag_done(client, env):
    lib_id = _create_library(client)
    p = make_image(env / "src" / "green.png", "green")
    with open(p, "rb") as f:
        mid = client.post(f"/libraries/{lib_id}/upload", files={"file": (p.name, f, "image/png")}).json()["media_id"]
    _wait_ready(1)
    assert 'hx-get="/media/%d/job"' % mid not in client.get(f"/media/{mid}").text

    from app import tagger

    tagger.set_paused(True)
    client.post(f"/media/{mid}/retag")
    page = client.get(f"/media/{mid}").text
    assert 'hx-get="/media/%d/job"' % mid in page and "GPU work is paused" in page
    r = client.get(f"/media/{mid}/job")
    assert "Tagging with the vision model" in r.text and "HX-Refresh" not in r.headers

    tagger.set_paused(False)
    _wait_ready(1)
    deadline = time.time() + 10
    while "HX-Refresh" not in client.get(f"/media/{mid}/job").headers and time.time() < deadline:
        time.sleep(0.2)
    assert client.get(f"/media/{mid}/job").headers.get("HX-Refresh") == "true"


def test_card_state_changes_when_retagged():
    from app.routes.admin import card_state

    base = {"status": "ready", "thumb_path": "1/thumb.jpg", "enabled": 1, "tagged_at": "2026-09-26 10:00:00"}
    assert card_state(base) != card_state({**base, "tagged_at": "2026-09-26 10:05:00"})
