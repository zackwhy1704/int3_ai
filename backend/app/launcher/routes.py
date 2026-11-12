"""/launcher/* — the API the Company Brain Launcher calls.

What crosses this boundary is identity, brain NAMES, a backend URL and short-lived tokens.
Never brain content: no documents, chunks, claims or answers are served here, and the
launcher never calls the brain's content routes.

Auth:
  /launcher/auth/invite, /launcher/auth/refresh   public (the credential is in the body)
  /launcher/introspect                            the backend's ingress (its own secret)
  everything else                                 a launcher access token (Bearer)

Local mode (/launcher/local/*): the launcher writes the policy into the user's own Hermes, reports
what it reads back, and only a good report gets a brain token (docs/LOCAL_MODE_DESIGN.md).
"""

import json
import logging
import secrets
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config
from ..auth.principal import Principal, _bearer, _principal_for, _tenant_name
from ..db import control_conn, tenant_conn
from . import local_policy, tokens

log = logging.getLogger("launcher")
router = APIRouter(prefix="/launcher")

_UNAUTH = HTTPException(401, "not signed in")


def launcher_principal(request: Request) -> Iterator[Principal]:
    """Identity from a launcher access token. Scopes come from memberships on every request
    (via _principal_for), exactly as for browser sessions and service tokens."""
    token = _bearer(request)
    if token is None:
        raise _UNAUTH
    with control_conn() as control:
        row = tokens.resolve_access(control, token)
    if row is None:
        raise _UNAUTH
    tenant_name = _tenant_name(row["tenant_id"])
    if tenant_name is None:
        raise _UNAUTH
    yield from _principal_for(
        row["tenant_id"],
        tenant_name,
        row["user_id"],
        email=None,
        session_id=f"lnch:{row['device_id']}",
        csrf_token="",
    )


def _device_id(p: Principal) -> str:
    return p.session_id.removeprefix("lnch:")


def _is_admin(p: Principal) -> bool:
    row = p.conn.execute("SELECT is_admin FROM users WHERE id = %s", (p.user_id,)).fetchone()
    return bool(row and row["is_admin"])


# ── sign-in / device session ────────────────────────────────────────────────


class InviteRedeem(BaseModel):
    invite: str = Field(max_length=200)
    label: str = Field(min_length=1, max_length=64)
    platform: str = Field(pattern="^(windows|macos)$")


@router.get("/healthz")
def healthz() -> dict:
    """Uptime check: the API answers and the control DB is reachable. Reveals nothing else."""
    try:
        with control_conn() as control:
            control.execute("SELECT 1").fetchone()
    except Exception:
        raise HTTPException(503, "unavailable") from None
    return {"ok": True}


@router.post("/auth/invite")
def redeem_invite(body: InviteRedeem) -> dict:
    with control_conn() as control:
        session = tokens.redeem_invite(control, body.invite.strip(), body.label.strip(), body.platform)
    if session is None:
        raise HTTPException(401, "invite is invalid, used or expired")
    return session


class Refresh(BaseModel):
    refresh_token: str = Field(max_length=200)


@router.post("/auth/refresh")
def refresh(body: Refresh) -> dict:
    with control_conn() as control:
        session = tokens.refresh(control, body.refresh_token)
    if session is None:
        raise _UNAUTH
    return session


@router.post("/auth/revoke")
def revoke_this_device(p: Principal = Depends(launcher_principal)) -> dict:
    with control_conn() as control:
        tokens.revoke_device(control, _device_id(p))
    return {"revoked": True}


@router.get("/devices")
def devices(p: Principal = Depends(launcher_principal)) -> list[dict]:
    with control_conn() as control:
        rows = control.execute(
            "SELECT id, label, platform, created_at, last_seen, revoked_at IS NOT NULL AS revoked"
            " FROM launcher_devices WHERE tenant_id = %s AND user_id = %s ORDER BY created_at",
            (p.tenant_id, p.user_id),
        ).fetchall()
    current = _device_id(p)
    return [{**r, "id": str(r["id"]), "current": str(r["id"]) == current} for r in rows]


@router.post("/devices/{device_id}/revoke")
def revoke_device(device_id: str, p: Principal = Depends(launcher_principal)) -> dict:
    try:
        device_id = str(uuid.UUID(device_id))
    except ValueError:  # not a uuid: indistinguishable from someone else's device
        raise HTTPException(404, "device not found") from None
    with control_conn() as control:
        own = control.execute(
            "SELECT 1 FROM launcher_devices WHERE id = %s AND tenant_id = %s AND user_id = %s",
            (device_id, p.tenant_id, p.user_id),
        ).fetchone()
        if own is None:
            raise HTTPException(404, "device not found")
        tokens.revoke_device(control, device_id)
    return {"revoked": True}


# ── library + launch ────────────────────────────────────────────────────────


def _launch_mode(tenant_id: str) -> str:
    with control_conn() as control:
        row = control.execute("SELECT launch_mode FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    return row["launch_mode"] if row else "hosted"


@router.get("/me")
def me(p: Principal = Depends(launcher_principal)) -> dict:
    """Who is signed in and the brains they may use — by name only."""
    brains = p.conn.execute(
        "SELECT id, name FROM scopes WHERE id = ANY(%s) ORDER BY kind = 'everyone' DESC, name",
        (p.scopes,),
    ).fetchall()
    return {
        "user": {"id": p.user_id, "name": p.name, "email": p.email, "is_admin": _is_admin(p)},
        "tenant": {"id": p.tenant_id, "name": p.tenant_name, "launch_mode": _launch_mode(p.tenant_id)},
        "brains": [{"id": b["id"], "name": b["name"]} for b in brains],
    }


def _compat(control) -> dict:
    releases = control.execute(
        "SELECT release, state, registry_version, is_backend FROM hermes_compat ORDER BY release"
    ).fetchall()
    builds = control.execute(
        "SELECT platform, package_version, release FROM hermes_desktop_builds"
        " ORDER BY platform, package_version"
    ).fetchall()
    backend = next((r["release"] for r in releases if r["is_backend"]), None)
    return {"backend_release": backend, "releases": releases, "desktop_builds": builds}


@router.get("/compat")
def compat(p: Principal = Depends(launcher_principal)) -> dict:
    with control_conn() as control:
        return _compat(control)


@router.post("/launch")
def launch(p: Principal = Depends(launcher_principal)) -> dict:
    """Mint this device's desktop token for the user's own backend. The launcher writes it,
    with the URL, into Hermes's connections.json and starts Hermes."""
    with control_conn() as control:
        backend = control.execute(
            "SELECT backend_id, url, hermes_release FROM user_backends"
            " WHERE tenant_id = %s AND user_id = %s",
            (p.tenant_id, p.user_id),
        ).fetchone()
        if backend is None:
            raise HTTPException(409, "no backend is provisioned for this user yet")
        token, expires = tokens.mint_desktop(control, _device_id(p), backend["backend_id"])
        compat_info = _compat(control)
    return {
        "backend": {
            "id": backend["backend_id"],
            "url": backend["url"],
            "hermes_release": backend["hermes_release"],
            "label": f"int3 - {p.user_id}@{p.tenant_id}",
        },
        "desktop_token": token,
        "expires_at": expires.isoformat(),
        "hermes": compat_info,
    }


# ── account (admin only) ────────────────────────────────────────────────────


class ByokLink(BaseModel):
    provider: str = Field(default="anthropic", pattern="^[a-z0-9_-]{1,32}$")


@router.post("/byok/link")
def byok_link(body: ByokLink, p: Principal = Depends(launcher_principal)) -> dict:
    """A single-use BYOK key-entry link (same intake flow as admin_keys.py mint-key-link).
    The launcher opens it in the system browser: the key goes browser -> intake over TLS and
    never passes through the launcher."""
    if not _is_admin(p):
        raise HTTPException(403, "only a tenant admin can set the provider key")
    if not config.KEY_INTAKE_BASE_URL:
        raise HTTPException(503, "key entry is not configured on this server")
    link = "cb_keylink_" + secrets.token_hex(32)
    with control_conn() as control:
        outcome = control.execute(
            "SELECT create_key_link(%s, %s, %s, %s, %s) AS r",
            (tokens.hash_token(link), p.tenant_id, body.provider, 2, f"launcher:{p.user_id}"),
        ).fetchone()["r"]
    if outcome in ("no_provider", "not_byok"):
        raise HTTPException(409, f"provider {body.provider} is not set up for your own key")
    log.info("launcher byok link minted: tenant=%s outcome=%s", p.tenant_id, outcome)
    return {
        "url": f"{config.KEY_INTAKE_BASE_URL.rstrip('/')}/key/{link}",
        "expires_in_hours": 2,
        "replaces_existing_key": outcome == "created_replacing",
    }


# ── ingress ─────────────────────────────────────────────────────────────────


class Introspect(BaseModel):
    token: str = Field(max_length=200)


@router.post("/introspect")
def introspect(body: Introspect, request: Request) -> dict:
    """Called by a backend's ingress for every desktop token it sees (it caches briefly).
    The ingress authenticates with its own secret, which also fixes WHICH backend is asking."""
    with control_conn() as control:
        backend_id = tokens.backend_for_ingress(control, _bearer(request) or "")
        if backend_id is None:
            raise HTTPException(401, "unknown ingress")
        row = tokens.introspect(control, body.token, backend_id)
    if row is None:
        return {"active": False}
    with tenant_conn(row["tenant_id"]) as conn:
        user = conn.execute(
            "SELECT 1 FROM users WHERE id = %s AND active", (row["user_id"],)
        ).fetchone()
    if user is None:
        return {"active": False}
    return {"active": True, "expires_at": row["expires_at"].isoformat()}


# ── local mode (M1) ─────────────────────────────────────────────────────────


def _require_local(p: Principal) -> None:
    if _launch_mode(p.tenant_id) != "local":
        raise HTTPException(404, "this company does not use local mode")


@router.get("/local/policy")
def local_policy_route(p: Principal = Depends(launcher_principal)) -> dict:
    """The Hermes setup to apply on this PC. Contains no secret; the brain token comes later."""
    _require_local(p)
    return local_policy.policy(config.LOCAL_MCP_URL)


class PostureReport(BaseModel):
    hermes_version: str = Field(default="", max_length=64)
    report: dict


@router.post("/local/posture")
def local_posture(body: PostureReport, p: Principal = Depends(launcher_principal)) -> dict:
    """The launcher's reading of the local Hermes setup. Judged HERE; the latest verdict decides
    whether this device's brain token is honoured (checked on every brain call)."""
    _require_local(p)
    if len(json.dumps(body.report)) > 16_384:
        raise HTTPException(413, "report too large")
    ok, reasons = local_policy.evaluate(body.report, config.LOCAL_MCP_URL)
    with control_conn() as control:
        control.execute(
            "INSERT INTO launcher_posture (device_id, reported_at, hermes_version, report, ok, reasons)"
            " VALUES (%s, now(), %s, %s, %s, %s)"
            " ON CONFLICT (device_id) DO UPDATE SET reported_at = now(),"
            " hermes_version = EXCLUDED.hermes_version, report = EXCLUDED.report,"
            " ok = EXCLUDED.ok, reasons = EXCLUDED.reasons",
            (_device_id(p), body.hermes_version, json.dumps(body.report), ok, reasons),
        )
    if not ok:
        log.info("local posture refused for %s/%s: %s", p.tenant_id, p.user_id, "; ".join(reasons))
    return {"ok": ok, "reasons": reasons}


@router.post("/local/launch")
def local_launch(p: Principal = Depends(launcher_principal)) -> dict:
    """Mint this device's brain token, only after a fresh, good setup report."""
    _require_local(p)
    with control_conn() as control:
        posture = control.execute(
            "SELECT ok, reasons, extract(epoch FROM now() - reported_at) AS age"
            " FROM launcher_posture WHERE device_id = %s",
            (_device_id(p),),
        ).fetchone()
        if posture is None or posture["age"] > config.LOCAL_POSTURE_MAX_AGE_SECONDS:
            raise HTTPException(409, "report the Hermes setup first")
        if not posture["ok"]:
            raise HTTPException(409, "Hermes setup refused: " + "; ".join(posture["reasons"]))
        token, expires = tokens.mint_brain(control, _device_id(p))
    return {"brain_token": token, "expires_at": expires.isoformat(), "mcp_url": config.LOCAL_MCP_URL}
