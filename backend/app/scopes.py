"""The one place a user id becomes a set of scopes. Nothing else decides access."""
from fastapi import Header, HTTPException

from .db import connect


def user_scopes(user_id: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT scope_id FROM memberships WHERE user_id = %s ORDER BY scope_id", (user_id,)
        ).fetchall()
    return [r["scope_id"] for r in rows]


def current_user(x_user_id: str = Header(...)) -> str:
    with connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = %s", (x_user_id,)).fetchone()
    if row is None:
        raise HTTPException(401, "unknown user")
    return row["id"]


def resolve(user_id: str, brain_id: str | None) -> list[str]:
    """User's scopes, narrowed to one brain if requested. A brain the user can't open
    yields 404 so the response doesn't distinguish 'hidden' from 'nonexistent'."""
    scopes = user_scopes(user_id)
    if brain_id is None:
        return scopes
    if brain_id not in scopes:
        raise HTTPException(404, "brain not found")
    return [brain_id]
