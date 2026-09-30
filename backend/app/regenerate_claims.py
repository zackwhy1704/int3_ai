"""Re-extract every claim with the model and print them as seed/claims.json.

    docker compose exec -T backend python -m app.regenerate_claims > seed/claims.json

This deletes the database's claims (and the answer cache, which quotes them) before
extracting. Deletion is a regeneration step, not an edit: the append-only trigger
guards against UPDATEs to existing claims. Extraction takes about a minute and calls
the model once per chunk. Logs go to stderr; only the JSON goes to stdout.
"""
import json
import logging
import sys

from . import claims
from .db import apply_schema, connect

logging.basicConfig(level=logging.INFO, stream=sys.stderr)


def main() -> None:
    apply_schema()
    with connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM answer_cache")
            # Claims reference each other via superseded_by; one statement deletes them all.
            conn.execute("DELETE FROM claims")
        claims.extract_all(conn)
        rows = conn.execute(
            "SELECT cl.id AS ref, c.document_id, c.ord, cl.subject, cl.attribute, cl.condition,"
            " cl.value, cl.quote, cl.valid_from::text, cl.superseded_by, cl.scope_id"
            " FROM claims cl JOIN chunks c ON c.id = cl.source_chunk_id ORDER BY cl.id"
        ).fetchall()
    json.dump(rows, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
