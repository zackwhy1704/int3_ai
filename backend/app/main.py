import logging

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import answer, retrieve
from .db import apply_schema, connect
from .scopes import current_user, resolve
from .seed import seed

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Company Brain demo")


@app.on_event("startup")
def startup() -> None:
    apply_schema()
    seed()


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


class Ask(BaseModel):
    question: str
    brain_id: str | None = None


@app.post("/api/ask")
def ask(body: Ask, user: str = Depends(current_user)) -> dict:
    return {"user": user, **answer.ask(body.question, resolve(user, body.brain_id))}


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
