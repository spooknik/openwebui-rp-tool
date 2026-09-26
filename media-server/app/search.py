"""Hybrid retrieval: vector KNN within a library partition, reranked with tag/caption overlap."""

import math
import random
import re
from dataclasses import dataclass, field

from . import db, ollama
from .config import get_settings

VEC_WEIGHT = 0.75
LEX_WEIGHT = 0.25
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

    @property
    def score(self) -> float:
        return VEC_WEIGHT * self.vec_score + LEX_WEIGHT * self.lex_score


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


def search(library, query: str, media_type: str = "any", exclude: set[int] | None = None, limit: int = 20,
           query_vec: list[float] | None = None) -> list[Hit]:
    exclude = exclude or set()
    if not db.vec_table_exists():
        return []
    if query_vec is None:
        query_vec = ollama.embed([get_settings().prefixes()[1] + query])[0]
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
    rows = db.conn().execute(sql, params).fetchall()

    qt = tokens(query)
    hits: list[Hit] = []
    c = db.conn()
    for r in rows:
        if r["media_id"] in exclude:
            continue
        m = c.execute("SELECT id, kind, caption, rating FROM media WHERE id=?", (r["media_id"],)).fetchone()
        if not m:
            continue
        tags = db.media_tags(m["id"])
        hits.append(Hit(m["id"], m["kind"], m["caption"], m["rating"], tags,
                        vec_score=1.0 - float(r["distance"]), lex_score=lexical_overlap(qt, m["caption"], tags)))
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
