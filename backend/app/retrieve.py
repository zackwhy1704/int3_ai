from .embed import embed_query


def search(conn, query: str, scopes: list[str], k: int = 8) -> list[dict]:
    """Top-k chunks by cosine similarity, from the caller's tenant connection. The scope
    filter is in the WHERE clause, so out-of-scope chunks are never read, ranked, or
    returned."""
    vec = embed_query(query)
    return conn.execute(
        """
        SELECT c.id AS chunk_id, c.scope_id AS scope, c.text,
               d.id AS document_id, d.title AS doc_title, d.source, d.owner,
               d.effective_date,
               1 - (c.embedding <=> %(vec)s) AS score
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.scope_id = ANY(%(scopes)s)
        ORDER BY c.embedding <=> %(vec)s
        LIMIT %(k)s
        """,
        {"vec": vec, "scopes": scopes, "k": k},
    ).fetchall()
