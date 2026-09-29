"""Load seed/ into Postgres, then extract claims. Each step is skipped if already done."""
import logging
import os
from pathlib import Path

import yaml

from . import claims
from .db import connect
from .embed import embed_passages

log = logging.getLogger("seed")
SEED_DIR = Path(os.environ.get("SEED_DIR", "/seed"))


def parse_doc(path: Path) -> tuple[dict, str]:
    _, front, body = path.read_text().split("---\n", 2)
    return yaml.safe_load(front), body.strip()


def chunk(body: str) -> list[str]:
    # One chunk per paragraph: the seed documents are short and paragraph-shaped.
    return [p.strip() for p in body.split("\n\n") if p.strip()]


def seed() -> None:
    with connect() as conn:
        if conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]:
            log.info("seed: documents present, skipping")
        else:
            load_documents(conn)
        if conn.execute("SELECT count(*) AS n FROM claims").fetchone()["n"]:
            log.info("seed: claims present, skipping")
        else:
            claims.extract_all(conn)


def load_documents(conn) -> None:
    people = yaml.safe_load((SEED_DIR / "users.yaml").read_text())
    for u in people["users"]:
        conn.execute("INSERT INTO users VALUES (%s, %s, %s)", (u["id"], u["name"], u["title"]))
    for s in people["scopes"]:
        conn.execute(
            "INSERT INTO scopes VALUES (%s, %s, %s, %s, %s)",
            (s["id"], s["name"], s["kind"], s["description"], s["owner"]),
        )
    for user_id, scope_ids in people["memberships"].items():
        for scope_id in scope_ids:
            conn.execute("INSERT INTO memberships VALUES (%s, %s)", (user_id, scope_id))

    n_chunks = 0
    for path in sorted((SEED_DIR / "docs").glob("*.md")):
        meta, body = parse_doc(path)
        doc_id = path.stem
        conn.execute(
            "INSERT INTO documents VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (doc_id, meta["scope"], meta["title"], meta["source"], meta["owner"],
             meta["effective_date"], body),
        )
        paragraphs = chunk(body)
        # Title goes into the embedded text so a paragraph keeps its document context.
        vectors = embed_passages([f"{meta['title']}\n{p}" for p in paragraphs])
        for ord_, (text, vec) in enumerate(zip(paragraphs, vectors)):
            conn.execute(
                "INSERT INTO chunks (document_id, scope_id, ord, text, embedding)"
                " VALUES (%s, %s, %s, %s, %s)",
                (doc_id, meta["scope"], ord_, text, vec),
            )
            n_chunks += 1
    log.info("seed: loaded %d chunks", n_chunks)
