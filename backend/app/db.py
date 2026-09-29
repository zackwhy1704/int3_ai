import os
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://brain:brain@db:5432/brain")


def connect() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
    register_vector(conn)
    return conn


def apply_schema() -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql)
