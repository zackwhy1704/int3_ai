"""Sign-in, callback, session and logout endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import config
from ..db import control_conn
from . import oidc, sessions
from .principal import Principal, csrf_protected, principal

log = logging.getLogger("auth")
router = APIRouter(prefix="/api")


def _cookie(resp, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _provider(provider_id: str) -> oidc.Provider:
    p = oidc.providers().get(provider_id)
    if p is None:
        raise HTTPException(404, "unknown sign-in provider")
    return p


@router.get("/auth/providers")
def list_providers() -> list[dict]:
    return [{"id": p.id, "label": p.label} for p in oidc.providers().values()]


@router.get("/auth/login/{provider_id}")
def login(provider_id: str) -> RedirectResponse:
    provider = _provider(provider_id)
    with control_conn() as control:
        url, state = oidc.start_login(control, provider)
    resp = RedirectResponse(url, status_code=302)
    _cookie(
        resp, config.LOGIN_COOKIE, state, max_age=int(oidc.LOGIN_TTL.total_seconds())
    )
    return resp


@router.get("/auth/callback/{provider_id}")
def callback(
    provider_id: str, request: Request, code: str = "", state: str = ""
) -> RedirectResponse:
    provider = _provider(provider_id)
    try:
        if not code or not state:
            raise oidc.LoginRejected("rejected", "missing code or state")
        with control_conn() as control:
            ident = oidc.finish_login(
                control, provider, code, state, request.cookies.get(config.LOGIN_COOKIE)
            )
            try:
                identity = sessions.authorize(control, ident)
            except oidc.LoginRejected as e:
                e.detail += f" (email ref {sessions.email_ref(ident.email)})"
                raise
            session_id, _ = sessions.create(control, identity)
    except oidc.LoginRejected as e:
        log.warning(
            "sign-in rejected: provider=%s reason=%s detail=%s",
            provider_id,
            e.reason,
            e.detail,
        )
        resp = RedirectResponse(f"/?login_error={e.reason}", status_code=303)
        resp.delete_cookie(
            config.LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
        )
        return resp

    log.info("sign-in ok: provider=%s tenant=%s", provider_id, identity["tenant_id"])
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(
        config.LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    _cookie(
        resp,
        config.SESSION_COOKIE,
        session_id,
        max_age=config.SESSION_ABSOLUTE_HOURS * 3600,
    )
    return resp


@router.get("/session")
def session(p: Principal = Depends(principal)) -> dict:
    return {
        "user": {"id": p.user_id, "name": p.name, "email": p.email},
        "tenant": {"id": p.tenant_id, "name": p.tenant_name},
        "csrf_token": p.csrf_token,
    }


@router.post("/auth/logout")
def logout(p: Principal = Depends(csrf_protected)) -> JSONResponse:
    with control_conn() as control:
        sessions.delete(control, p.session_id)
    resp = JSONResponse({"signed_out": True})
    resp.delete_cookie(
        config.SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    return resp
