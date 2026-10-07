import logging

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

import yaml

from . import answer, embed, llm, rerank, retrieve
from .db import apply_schema, connect
from .scopes import current_user, resolve
from .seed import SEED_DIR, seed
from .v1 import router as v1_router

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Company Brain demo")
app.include_router(v1_router)


@app.on_event("startup")
def startup() -> None:
    apply_schema()
    seed()
    # Load both models now so the first question in the meeting isn't the slow one.
    embed.embed_query("warm up")
    rerank.rerank("warm up", [{"doc_title": "", "text": "warm up"}])


@app.get("/api/users")
def users() -> list[dict]:
    with connect() as conn:
        return conn.execute("SELECT id, name, title FROM users ORDER BY name").fetchall()


@app.get("/api/search")
def search(q: str, brain_id: str | None = None, user: str = Depends(current_user)) -> dict:
    scopes = resolve(user, brain_id)
    hits = retrieve.search(q, scopes)
    return {
        "user": user,
        "scopes": scopes,
        "results": [
            {"chunk_id": h["chunk_id"], "doc_title": h["doc_title"], "scope": h["scope"],
             "score": round(float(h["score"]), 4), "text": h["text"]}
            for h in hits
        ],
    }


@app.get("/api/brains")
def brains(user: str = Depends(current_user)) -> dict:
    """Brains the user can open, plus one they can't: name and owner only."""
    with connect() as conn:
        open_ = conn.execute(
            "SELECT s.id, s.name, s.kind, s.description, u.name AS owner"
            " FROM scopes s JOIN users u ON u.id = s.owner_user_id"
            " WHERE s.id IN (SELECT scope_id FROM memberships WHERE user_id = %s)"
            " ORDER BY s.kind = 'everyone' DESC, s.name",
            (user,),
        ).fetchall()
        locked = conn.execute(
            "SELECT s.name, u.name AS owner"
            " FROM scopes s JOIN users u ON u.id = s.owner_user_id"
            " WHERE s.id NOT IN (SELECT scope_id FROM memberships WHERE user_id = %s)"
            " AND s.kind <> 'eval'"
            " ORDER BY s.kind = 'restricted' DESC, s.name LIMIT 1",
            (user,),
        ).fetchone()
    return {"open": open_, "locked": locked}


class Ask(BaseModel):
    question: str
    brain_id: str | None = None


@app.post("/api/ask")
def ask(body: Ask, user: str = Depends(current_user)) -> dict:
    try:
        return {"user": user, **answer.ask(body.question, resolve(user, body.brain_id))}
    except llm.Unavailable:
        raise HTTPException(503, "The model could not be reached, and there is no earlier "
                                 "answer to this question for your access to fall back on.")


@app.get("/api/suggestions")
def suggestions(user: str = Depends(current_user)) -> list[str]:
    """Questions known to be answerable for this user (checked by the preflight)."""
    return yaml.safe_load((SEED_DIR / "demo.yaml").read_text())["try_asking"].get(user, [])


@app.get("/api/documents/{doc_id}")
def document(doc_id: str, user: str = Depends(current_user)) -> dict:
    # Same scope filter as retrieval; hidden and nonexistent documents look identical.
    with connect() as conn:
        row = conn.execute(
            "SELECT id, scope_id AS scope, title, source, owner, effective_date, body"
            " FROM documents WHERE id = %s AND scope_id = ANY(%s)",
            (doc_id, resolve(user, None)),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "document not found")
    return row
