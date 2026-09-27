"""Heat ceiling per chat, scene blending, context tags, and the migration of older databases."""

from conftest import fake_embed
from fastapi.testclient import TestClient
from test_unit import _lib, _media, _q

from app import db, ollama, search


def test_chat_heat_and_ceiling(env):
    lib = _lib()
    assert search.chat_heat(lib, None) == 1 and search.chat_heat(lib, "c1") == 1
    assert search.heat_ceiling(lib, "c1", user_requested=False) == 2
    assert search.heat_ceiling(lib, "c1", user_requested=True) == 3
    assert search.heat_ceiling(lib, "c1", user_requested=True, scene_heat=1) == 1  # the model can cap it lower

    hot = _media(lib, "nude on the bed", {"location": ["bedroom"]}, rating="explicit", heat=4)
    search.record_send("c1", None, hot, lib["id"], "luna-rp", 3)
    assert search.chat_heat(lib, "c1") == 4
    assert search.heat_ceiling(lib, "c1", False) == 5 and search.heat_ceiling(lib, "c2", False) == 2

    db.conn().execute("UPDATE libraries SET start_heat=3 WHERE id=?", (lib["id"],))
    lib3 = db.conn().execute("SELECT * FROM libraries WHERE id=?", (lib["id"],)).fetchone()
    assert search.chat_heat(lib3, "fresh") == 3 and search.heat_ceiling(lib3, "fresh", False) == 4


def test_search_heat_filter_and_preference(env):
    lib = _lib()
    q = "selfie in the bedroom"
    cute = _media(lib, "selfie in the bedroom in pyjamas", {"location": ["bedroom"]}, heat=1)
    tease = _media(lib, "selfie in the bedroom in lingerie", {"location": ["bedroom"]}, rating="suggestive", heat=3)
    nude = _media(lib, "selfie in the bedroom nude", {"location": ["bedroom"]}, rating="explicit", heat=4)

    ids = [h.media_id for h in search.search(lib, q, "any", query_vec=_q(q), max_heat=3)]
    assert nude not in ids and cute in ids and tease in ids
    assert [h.media_id for h in search.search(lib, q, "any", query_vec=_q(q), max_heat=1)] == [cute]
    # Items below the target heat lose a little score, so the hottest allowed item wins a tie.
    hits = {h.media_id: h for h in search.search(lib, q, "any", query_vec=_q(q), max_heat=3)}
    assert hits[tease].heat_penalty == 0 and hits[cute].heat_penalty == 2 * search.HEAT_PENALTY
    assert hits[tease].score > hits[cute].score

    # Manual edits keep the vector metadata in sync.
    with db.tx() as c:
        c.execute("UPDATE media SET heat=5 WHERE id=?", (tease,))
        db.sync_vec_meta(c, tease)
    assert tease not in [h.media_id for h in search.search(lib, q, "any", query_vec=_q(q), max_heat=4)]


def test_scene_blends_into_query_and_lexical(env, monkeypatch):
    lib = _lib()
    home = _media(lib, "selfie on the couch in a hoodie", {"location": ["living room"], "context": ["just got home", "lazy evening"]}, heat=1)
    gym = _media(lib, "selfie in the gym mirror", {"location": ["gym"], "context": ["after a workout"]}, heat=1)
    monkeypatch.setattr(ollama, "embed", fake_embed)

    plain = [h.media_id for h in search.search(lib, "selfie", "any")]
    with_scene = search.search(lib, "selfie", "any", scene="she just got home and flops on the couch")
    assert with_scene[0].media_id == home and set(plain) == {home, gym}
    assert with_scene[0].lex_score > 0  # scene words hit the context tags
    assert search.search(lib, "selfie", "any", scene="cooling down after a workout")[0].media_id == gym

    v = search.query_vector("selfie", "after a workout")
    assert abs(sum(x * x for x in v) - 1.0) < 1e-6  # blended vector is re-normalised


def test_tagger_heat_consistency_and_context():
    assert ollama.consistent_heat(5, "sfw") == 2
    assert ollama.consistent_heat(1, "explicit") == 4
    assert ollama.consistent_heat(3, "suggestive") == 3
    assert "context" in ollama.TAG_SCHEMA["properties"]["tags"]["properties"]
    assert "heat" in ollama.TAG_SCHEMA["required"] and "tags.context" in ollama.DEFAULT_PROMPT
    assert db.flatten_tags({"context": ["bedtime"], "outfit": ["robe"]}) == "robe, bedtime"


def test_migration_adds_columns_and_triggers_reembed(env):
    # Simulate a database created before heat existed: drop the new columns and the vec schema marker.
    lib = _lib()
    mid = _media(lib, "old item", {"location": ["cafe"]})
    c = db.conn()
    c.execute("ALTER TABLE media DROP COLUMN heat")
    c.execute("ALTER TABLE libraries DROP COLUMN start_heat")
    c.execute("DELETE FROM settings WHERE key='vec_schema'")
    db.init_db()
    assert c.execute("SELECT heat FROM media WHERE id=?", (mid,)).fetchone()["heat"] == db.HEAT_DEFAULT
    assert c.execute("SELECT start_heat FROM libraries").fetchone()["start_heat"] == 1
    # ensure_vec_table sees the missing marker and rebuilds the table with the heat column.
    assert db.ensure_vec_table(len(_q("x")), "fake-embed") is True
    cols = {r["name"] for r in c.execute("PRAGMA table_info(media_vec)")}
    assert "heat" in cols and db.get_setting("vec_schema") == db.VEC_SCHEMA


def test_api_send_respects_ceiling(env, monkeypatch):
    from app.main import app
    from app.routes.admin import _session_token

    lib = _lib()
    cute = _media(lib, "selfie at the beach in a sundress", {"location": ["beach"]}, heat=2)
    nude = _media(lib, "selfie at the beach nude", {"location": ["beach"]}, rating="explicit", heat=4)
    monkeypatch.setattr(ollama, "embed", fake_embed)
    hdr = {"Authorization": "Bearer tool-key"}
    with TestClient(app) as client:
        req = {"description": "selfie at the beach", "model_id": "luna-rp", "chat_id": "chat", "user_requested": True, "user_turn": 1}
        r = client.post("/api/send", json=req, headers=hdr).json()
        assert r["status"] == "sent" and r["media"]["id"] == cute and r["max_heat"] == 3 and r["media"]["heat"] == 2
        r = client.post("/api/send", json={**req, "user_turn": 2, "scene_heat": 5}, headers=hdr).json()
        assert r["status"] == "sent" and r["media"]["id"] == nude and r["max_heat"] == 4  # ratchet: 2 + 2
        # Admin search accepts the same knobs.
        client.cookies.set("rpm_session", _session_token())
        page = client.get(f"/libraries/{lib['id']}/search", params={"q": "beach", "max_heat": "2", "scene": "on holiday"}).text
        assert "sundress" in page and "nude" not in page
        assert "Starting heat" in client.get(f"/libraries/{lib['id']}/edit").text
        assert 'value="2" selected' in client.get(f"/media/{cute}").text
