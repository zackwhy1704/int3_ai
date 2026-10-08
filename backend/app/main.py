import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import answer, embed, llm, rerank, retrieve
from .api.v1 import router as v1_router
from .auth import oidc
from .auth.principal import Principal, csrf_protected, principal
from .auth.routes import router as auth_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    oidc.assert_safe_config()
    # Load both models now so the first question isn't the slow one.
    embed.embed_query("warm up")
    rerank.rerank("warm up", [{"doc_title": "", "text": "warm up"}])
    yield


app = FastAPI(title="Company Brain", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(v1_router)


@app.get("/api/brains")
def brains(p: Principal = Depends(principal)) -> dict:
    """Brains the user can open, plus one they can't: name and owner only."""
    open_ = p.conn.execute(
        "SELECT s.id, s.name, s.kind, s.description, u.name AS owner"
        " FROM scopes s JOIN users u ON u.id = s.owner_user_id"
        " WHERE s.id = ANY(%s) ORDER BY s.kind = 'everyone' DESC, s.name", (p.scopes,)).fetchall()
    locked = p.conn.execute(
        "SELECT s.name, u.name AS owner FROM scopes s JOIN users u ON u.id = s.owner_user_id"
        " WHERE NOT s.id = ANY(%s) ORDER BY s.kind = 'restricted' DESC, s.name LIMIT 1",
        (p.scopes,)).fetchone()
    return {"open": open_, "locked": locked}


@app.get("/api/search")
def search(q: str, brain_id: str | None = None, p: Principal = Depends(principal)) -> dict:
    scopes = p.resolve(brain_id)
    hits = retrieve.search(p.conn, q, scopes)
    return {
        "scopes": scopes,
        "results": [
            {"chunk_id": h["chunk_id"], "doc_title": h["doc_title"], "scope": h["scope"],
             "score": round(float(h["score"]), 4), "text": h["text"]}
            for h in hits
        ],
    }


class Ask(BaseModel):
    question: str
    brain_id: str | None = None


@app.post("/api/ask")
def ask(body: Ask, p: Principal = Depends(csrf_protected)) -> dict:
    try:
        return answer.ask(p.conn, body.question, p.resolve(body.brain_id))
    except llm.Unavailable:
        raise HTTPException(503, "The model could not be reached, and there is no earlier "
                                 "answer to this question for your access to fall back on.") from None


@app.get("/api/documents/{doc_id}")
def document(doc_id: str, p: Principal = Depends(principal)) -> dict:
    # Same scope filter as retrieval; hidden and nonexistent documents look identical.
    row = p.conn.execute(
        "SELECT id, scope_id AS scope, title, source, owner, effective_date, body"
        " FROM documents WHERE id = %s AND scope_id = ANY(%s)", (doc_id, p.scopes)).fetchone()
    if row is None:
        raise HTTPException(404, "document not found")
    return row
