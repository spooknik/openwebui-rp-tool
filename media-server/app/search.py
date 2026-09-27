"""Hybrid retrieval: vector KNN within a library partition, reranked with tag/caption overlap.

Two inputs describe what to send: the *description* (what the picture should show) and an optional *scene*
(what is happening in the story right now). The scene is blended into the query vector and its words count
for lexical overlap, so the tagger's `context` tags ("bedtime", "just got home") pull the right items forward.

Heat (1..5) says how far into an intimate story an item belongs. A chat's heat is the hottest thing already
sent in it (or the library's start heat), and a send may only go one step above that (two when the user asked
outright). The model can also cap it lower with `scene_heat` when the story cooled down.
"""

import math
import random
import re
from dataclasses import dataclass, field

from . import db, ollama
from .config import get_settings

VEC_WEIGHT = 0.75
LEX_WEIGHT = 0.25
SCENE_WEIGHT = 0.3  # share of the query vector taken by the scene text
HEAT_PENALTY = 0.02  # score lost per heat level below the target, so the current heat is preferred but not forced
MAX_K = 4096  # sqlite-vec limit

_STOP = set(
    "a an the and or of in on at to for with from by me my i you your we our she he they her him their it its this that "
    "is are was be being some something send sending show picture pic pics photo photos image video clip".split()
)
_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Hit:
    media_id: int
    kind: str
    caption: str
    rating: str | None
    tags: dict[str, list[str]] = field(default_factory=dict)
    vec_score: float = 0.0
    lex_score: float = 0.0
    heat: int = db.HEAT_DEFAULT
    target_heat: int | None = None

    @property
    def heat_penalty(self) -> float:
        if self.target_heat is None:
            return 0.0
        return HEAT_PENALTY * max(0, self.target_heat - self.heat)

    @property
    def score(self) -> float:
        return VEC_WEIGHT * self.vec_score + LEX_WEIGHT * self.lex_score - self.heat_penalty


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def lexical_overlap(query_tokens: set[str], caption: str, tags: dict[str, list[str]]) -> float:
    if not query_tokens:
        return 0.0
    tag_tokens = tokens(" ".join(v for vals in tags.values() for v in vals))
    cap_tokens = tokens(caption)
    score = sum(1.0 if t in tag_tokens else 0.5 if t in cap_tokens else 0.0 for t in query_tokens)
    return score / len(query_tokens)


def library_for_model(model_id: str):
    for lib in db.conn().execute("SELECT * FROM libraries ORDER BY id"):
        if model_id in db.library_model_ids(lib):
            return lib
    return None


def library_by_slug(slug: str):
    return db.conn().execute("SELECT * FROM libraries WHERE slug=?", (slug,)).fetchone()


def sent_ids(chat_id: str | None) -> set[int]:
    if not chat_id:
        return set()
    return {r["media_id"] for r in db.conn().execute("SELECT media_id FROM sends WHERE chat_id=?", (chat_id,))}


def chat_heat(library, chat_id: str | None) -> int:
    """The hottest item already sent in this chat, or the library's starting heat for a fresh chat."""
    start = db.clamp_heat(library["start_heat"] if "start_heat" in library.keys() else 1, default=1)
    if not chat_id:
        return start
    row = db.conn().execute(
        "SELECT MAX(m.heat) AS h FROM sends s JOIN media m ON m.id=s.media_id WHERE s.chat_id=?", (chat_id,)
    ).fetchone()
    return max(start, db.clamp_heat(row["h"], default=start)) if row and row["h"] is not None else start


def heat_ceiling(library, chat_id: str | None, user_requested: bool, scene_heat: int | None = None) -> int:
    """Hottest heat allowed for this send: one step up from the chat's heat (two on an explicit request),
    capped by the model's own reading of the scene when it gives one."""
    ceiling = min(db.HEAT_MAX, chat_heat(library, chat_id) + (2 if user_requested else 1))
    if scene_heat:
        ceiling = min(ceiling, db.clamp_heat(scene_heat))
    return ceiling


def query_vector(query: str, scene: str | None = None) -> list[float]:
    prefix = get_settings().prefixes()[1]
    if not (scene or "").strip():
        return ollama.embed([prefix + query])[0]
    q, s = ollama.embed([prefix + query, prefix + scene.strip()])
    v = [(1 - SCENE_WEIGHT) * a + SCENE_WEIGHT * b for a, b in zip(q, s)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def search(library, query: str, media_type: str = "any", exclude: set[int] | None = None, limit: int = 20,
           query_vec: list[float] | None = None, scene: str | None = None, max_heat: int | None = None) -> list[Hit]:
    exclude = exclude or set()
    if not db.vec_table_exists():
        return []
    if query_vec is None:
        query_vec = query_vector(query, scene)
    if db.vec_dim() != len(query_vec):
        return []  # mid re-embed after a model switch

    cap = db.RATING_LEVELS.get(library["rating_cap"], 2)
    k = min(get_settings().search_k + len(exclude), MAX_K)
    sql = ("SELECT media_id, distance FROM media_vec WHERE embedding MATCH ? AND k = ? "
           "AND library_id = ? AND enabled = 1 AND rating_level <= ?")
    params: list = [db.pack_vec(query_vec), k, library["id"], cap]
    if media_type in ("image", "video"):
        sql += " AND kind = ?"
        params.append(media_type)
    if max_heat is not None:
        sql += " AND heat <= ?"
        params.append(db.clamp_heat(max_heat))
    rows = db.conn().execute(sql, params).fetchall()

    qt = tokens(query) | (tokens(scene) if scene else set())
    hits: list[Hit] = []
    c = db.conn()
    for r in rows:
        if r["media_id"] in exclude:
            continue
        m = c.execute("SELECT id, kind, caption, rating, heat FROM media WHERE id=?", (r["media_id"],)).fetchone()
        if not m:
            continue
        tags = db.media_tags(m["id"])
        hits.append(Hit(m["id"], m["kind"], m["caption"], m["rating"], tags,
                        vec_score=1.0 - float(r["distance"]), lex_score=lexical_overlap(qt, m["caption"], tags),
                        heat=db.clamp_heat(m["heat"]), target_heat=max_heat))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def pick(hits: list[Hit], min_score: float, rng: random.Random | None = None) -> Hit | None:
    """Weighted-random choice among the top 3 above the threshold, favouring the best match."""
    good = [h for h in hits[:3] if h.score >= min_score]
    if not good:
        return None
    best = good[0].score
    weights = [math.exp((h.score - best) / 0.03) for h in good]
    return (rng or random).choices(good, weights=weights, k=1)[0]


def cooldown_remaining(library, chat_id: str | None, user_turn: int | None) -> int:
    """User turns left before a spontaneous send is allowed again (0 = allowed)."""
    if not chat_id or user_turn is None:
        return 0
    cooldown = library["cooldown_turns"] if library["cooldown_turns"] is not None else get_settings().default_cooldown_turns
    last = db.conn().execute(
        "SELECT user_turn FROM sends WHERE chat_id=? AND user_turn IS NOT NULL ORDER BY id DESC LIMIT 1", (chat_id,)
    ).fetchone()
    if not last:
        return 0
    return max(0, cooldown - (user_turn - last["user_turn"]))


def record_send(chat_id: str, message_id: str | None, media_id: int, library_id: int, model_id: str | None, user_turn: int | None) -> None:
    db.conn().execute(
        "INSERT INTO sends(chat_id, message_id, media_id, library_id, model_id, user_turn) VALUES (?,?,?,?,?,?)",
        (chat_id, message_id, media_id, library_id, model_id, user_turn),
    )
