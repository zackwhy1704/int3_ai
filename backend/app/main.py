import logging

from fastapi import Depends, FastAPI

from . import retrieve
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
