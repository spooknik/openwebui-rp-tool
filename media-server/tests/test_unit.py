import json
import random

from conftest import fake_embed

from app import db, search, signing


def _lib(slug="luna", model_ids=("luna-rp",), rating_cap="explicit", cooldown=None):
    cur = db.conn().execute(
        "INSERT INTO libraries(slug, name, model_ids, rating_cap, cooldown_turns) VALUES (?,?,?,?,?)",
        (slug, slug.title(), json.dumps(list(model_ids)), rating_cap, cooldown),
    )
    return db.conn().execute("SELECT * FROM libraries WHERE id=?", (cur.lastrowid,)).fetchone()


def _media(lib, caption, tags, kind="image", rating="sfw", enabled=True):
    c = db.conn()
    cur = c.execute(
        "INSERT INTO media(library_id, kind, source, rel_path, sha256, mime, caption, rating, enabled, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,'ready')",
        (lib["id"], kind, "upload", f"x/{caption}", caption, "image/jpeg", caption, rating, int(enabled)),
    )
    mid = cur.lastrowid
    vec = fake_embed([caption + " " + " ".join(v for vs in tags.values() for v in vs)])[0]
    db.ensure_vec_table(len(vec), "fake-embed")
    with db.tx() as c:
        db.write_tags(c, mid, caption, tags)
        db.upsert_vec(c, mid, lib["id"], kind, rating, enabled, vec)
    return mid


def _q(text):
    return fake_embed([text])[0]


def test_signing_roundtrip_and_tamper(env):
    sig = signing.sign(5, "full")
    assert signing.verify(5, "full", sig)
    assert not signing.verify(6, "full", sig)
    assert not signing.verify(5, "thumb", sig)
    assert not signing.verify(5, "full", sig[:-1] + ("0" if sig[-1] != "0" else "1"))
    assert not signing.verify(5, "etc", signing.sign(5, "etc"))
    assert signing.media_url(5).startswith("https://media.example.test/m/5/full?sig=")


def test_search_filters_and_ranking(env):
    lib = _lib()
    other = _lib("mia", ("mia-rp",))
    beach = _media(lib, "selfie at the beach in a red bikini", {"location": ["beach"], "outfit": ["red bikini"]}, rating="suggestive")
    cafe = _media(lib, "reading in a cafe with coffee", {"location": ["cafe"]})
    clip = _media(lib, "video walking on the beach at sunset", {"location": ["beach"]}, kind="video")
    hidden = _media(lib, "beach selfie disabled", {"location": ["beach"]}, enabled=False)
    explicit = _media(lib, "beach explicit", {"location": ["beach"]}, rating="explicit")
    foreign = _media(other, "selfie at the beach in a red bikini", {"location": ["beach"]})

    q = "selfie at the beach red bikini"
    hits = search.search(lib, q, "any", query_vec=_q(q))
    ids = [h.media_id for h in hits]
    assert ids[0] == beach
    assert foreign not in ids and hidden not in ids  # partition + enabled filter
    assert explicit in ids

    assert [h.media_id for h in search.search(lib, q, "video", query_vec=_q(q))] == [clip]
    assert beach not in [h.media_id for h in search.search(lib, q, "any", exclude={beach}, query_vec=_q(q))]

    db.conn().execute("UPDATE libraries SET rating_cap='sfw' WHERE id=?", (lib["id"],))
    lib_sfw = db.conn().execute("SELECT * FROM libraries WHERE id=?", (lib["id"],)).fetchone()
    ids = [h.media_id for h in search.search(lib_sfw, q, "any", query_vec=_q(q))]
    assert beach not in ids and explicit not in ids and cafe in ids


def test_toggle_enabled_syncs_vector_meta(env):
    lib = _lib()
    mid = _media(lib, "beach selfie", {"location": ["beach"]})
    with db.tx() as c:
        c.execute("UPDATE media SET enabled=0 WHERE id=?", (mid,))
        db.sync_vec_meta(c, mid)
    assert search.search(lib, "beach selfie", query_vec=_q("beach selfie")) == []


def test_lexical_overlap():
    tags = {"location": ["beach"], "outfit": ["red bikini"]}
    assert search.lexical_overlap(search.tokens("red bikini beach"), "", tags) == 1.0
    assert search.lexical_overlap(search.tokens("send me a pic at the gym"), "", tags) == 0.0
    assert 0 < search.lexical_overlap(search.tokens("sunset beach"), "a sunset", tags) < 1


def test_pick_threshold_and_bias():
    H = search.Hit
    hits = [H(1, "image", "", None, vec_score=0.9, lex_score=1), H(2, "image", "", None, vec_score=0.5), H(3, "image", "", None, vec_score=0.1)]
    assert search.pick([H(9, "image", "", None, vec_score=0.1)], 0.3) is None
    rng = random.Random(0)
    picks = [search.pick(hits, 0.3, rng).media_id for _ in range(200)]
    assert set(picks) <= {1, 2} and picks.count(1) > 190  # 3 is below threshold; 1 strongly favoured


def test_cooldown(env):
    lib = _lib(cooldown=3)
    mid = _media(lib, "beach", {})
    assert search.cooldown_remaining(lib, "chat1", 1) == 0
    search.record_send("chat1", "m1", mid, lib["id"], "luna-rp", 2)
    assert search.cooldown_remaining(lib, "chat1", 2) == 3
    assert search.cooldown_remaining(lib, "chat1", 4) == 1
    assert search.cooldown_remaining(lib, "chat1", 5) == 0
    assert search.cooldown_remaining(lib, "other-chat", 2) == 0
    assert search.sent_ids("chat1") == {mid}


def test_library_for_model(env):
    _lib("luna", ("luna-rp", "luna-nsfw"))
    assert search.library_for_model("luna-nsfw")["slug"] == "luna"
    assert search.library_for_model("nobody") is None


def test_ollama_missing_model_error_is_actionable():
    import httpx
    import pytest

    from app import ollama

    req = httpx.Request("POST", "http://ollama/api/embed")
    r = httpx.Response(404, json={"error": 'model "nomic-embed-text" not found, try pulling it first'}, request=req)
    with pytest.raises(ollama.OllamaError, match="ollama pull nomic-embed-text"):
        ollama._check(r, "nomic-embed-text")
