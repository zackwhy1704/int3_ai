"""Launcher credentials in the control database.

Four secrets, all stored as SHA-256 only (as session ids and service tokens are):

  invite   single use, operator-minted, delivered out of band   -> a device session
  access   15 min, authenticates the launcher API
  refresh  30 days, single use; each use rotates it. Presenting a USED refresh token again
           means it was copied: the whole device is revoked.
  desktop  12 h, bound to one device AND one backend. The only token that is written into
           Hermes's connections.json; the backend's ingress checks it via introspect().
  brain    24 h, local mode: bound to one device. Written into the local Hermes .env; the brain
           checks it (and the device's latest Hermes-setup report) on every /v1 call.

A device session always resolves through identities, so removing a user's identity row ends
every launcher credential they hold on their next use, as with browser sessions.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from .. import config

log = logging.getLogger("launcher")

_PREFIX = {"access": "cb_la_", "refresh": "cb_lr_", "desktop": "cb_dt_", "brain": "cb_bt_"}
BRAIN_PREFIX = _PREFIX["brain"]
INVITE_PREFIX = "cb_inv_"
INGRESS_PREFIX = "cb_ing_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mint_invite(control, tenant_id: str, user_id: str, ttl_hours: float, created_by: str) -> str:
    """Return a single-use invite code (plaintext ONCE). The identity must exist."""
    if not control.execute(
        "SELECT 1 FROM identities WHERE tenant_id = %s AND user_id = %s", (tenant_id, user_id)
    ).fetchone():
        raise ValueError(f"no identity for {tenant_id}/{user_id}")
    ttl = max(0.0, min(float(ttl_hours), float(config.LAUNCHER_INVITE_MAX_HOURS)))
    code = INVITE_PREFIX + secrets.token_urlsafe(32)
    control.execute(
        "INSERT INTO launcher_invites (token_hash, tenant_id, user_id, expires_at, created_by)"
        " VALUES (%s, %s, %s, %s, %s)",
        (hash_token(code), tenant_id, user_id, _now() + timedelta(hours=ttl), created_by),
    )
    return code


def _issue(control, kind: str, device_id, lifetime: timedelta, backend_id: str | None = None):
    token = _PREFIX[kind] + secrets.token_urlsafe(32)
    expires = _now() + lifetime
    control.execute(
        "INSERT INTO launcher_tokens (token_hash, kind, device_id, backend_id, expires_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (hash_token(token), kind, device_id, backend_id, expires),
    )
    return token, expires


def _issue_pair(control, device_id) -> dict:
    access, access_exp = _issue(
        control, "access", device_id, timedelta(minutes=config.LAUNCHER_ACCESS_MINUTES)
    )
    refresh_, refresh_exp = _issue(
        control, "refresh", device_id, timedelta(days=config.LAUNCHER_REFRESH_DAYS)
    )
    return {
        "device_id": str(device_id),
        "access_token": access,
        "access_expires_at": access_exp.isoformat(),
        "refresh_token": refresh_,
        "refresh_expires_at": refresh_exp.isoformat(),
    }


def redeem_invite(control, code: str, label: str, platform: str) -> dict | None:
    """Consume an invite (atomically, only while its identity still exists) and open a device
    session. None for an unknown, used or expired invite: the caller answers a generic 401."""
    if not code:
        return None
    row = control.execute(
        "UPDATE launcher_invites v SET used_at = now() FROM identities i"
        " WHERE v.token_hash = %s AND v.used_at IS NULL AND v.expires_at > now()"
        " AND i.tenant_id = v.tenant_id AND i.user_id = v.user_id"
        " RETURNING v.tenant_id, v.user_id",
        (hash_token(code),),
    ).fetchone()
    if row is None:
        return None
    device = control.execute(
        "INSERT INTO launcher_devices (tenant_id, user_id, label, platform)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (row["tenant_id"], row["user_id"], label, platform),
    ).fetchone()
    log.info("launcher device opened: tenant=%s device=%s", row["tenant_id"], device["id"])
    return _issue_pair(control, device["id"])


def refresh(control, refresh_token: str) -> dict | None:
    """Rotate: consume a live refresh token and issue a new pair. A refresh token that was
    already used is evidence of theft (the legitimate holder rotated past it): revoke the
    whole device. None in every failure case."""
    if not refresh_token:
        return None
    h = hash_token(refresh_token)
    row = control.execute(
        "UPDATE launcher_tokens t SET used_at = now() FROM launcher_devices d, identities i"
        " WHERE t.token_hash = %s AND t.kind = 'refresh' AND t.used_at IS NULL"
        " AND t.revoked_at IS NULL AND t.expires_at > now()"
        " AND d.id = t.device_id AND d.revoked_at IS NULL"
        " AND i.tenant_id = d.tenant_id AND i.user_id = d.user_id"
        " RETURNING t.device_id",
        (h,),
    ).fetchone()
    if row is not None:
        return _issue_pair(control, row["device_id"])
    reused = control.execute(
        "SELECT device_id FROM launcher_tokens"
        " WHERE token_hash = %s AND kind = 'refresh' AND used_at IS NOT NULL",
        (h,),
    ).fetchone()
    if reused is not None:
        revoke_device(control, reused["device_id"])
        log.warning("launcher refresh token reused: device %s revoked", reused["device_id"])
    return None


def resolve_access(control, access_token: str) -> dict | None:
    """(tenant_id, user_id, device_id) for a live access token on a live device, else None."""
    if not access_token:
        return None
    row = control.execute(
        "SELECT d.tenant_id, d.user_id, d.id AS device_id FROM launcher_tokens t"
        " JOIN launcher_devices d ON d.id = t.device_id"
        " JOIN identities i ON i.tenant_id = d.tenant_id AND i.user_id = d.user_id"
        " WHERE t.token_hash = %s AND t.kind = 'access' AND t.revoked_at IS NULL"
        " AND t.expires_at > now() AND d.revoked_at IS NULL",
        (hash_token(access_token),),
    ).fetchone()
    if row is not None:
        control.execute(
            "UPDATE launcher_devices SET last_seen = now() WHERE id = %s", (row["device_id"],)
        )
    return row


def mint_desktop(control, device_id, backend_id: str):
    """A fresh desktop token for this device + backend. Earlier live desktop tokens of the
    device are revoked first, so each device holds at most one."""
    control.execute(
        "UPDATE launcher_tokens SET revoked_at = now()"
        " WHERE device_id = %s AND kind = 'desktop' AND revoked_at IS NULL",
        (device_id,),
    )
    return _issue(
        control,
        "desktop",
        device_id,
        timedelta(hours=config.LAUNCHER_DESKTOP_HOURS),
        backend_id=backend_id,
    )


def mint_brain(control, device_id):
    """A fresh brain token for this device; earlier live brain tokens of the device are revoked."""
    control.execute(
        "UPDATE launcher_tokens SET revoked_at = now()"
        " WHERE device_id = %s AND kind = 'brain' AND revoked_at IS NULL",
        (device_id,),
    )
    return _issue(control, "brain", device_id, timedelta(hours=config.LAUNCHER_BRAIN_HOURS))


def resolve_brain(control, brain_token: str) -> dict | None:
    """The live brain token's (tenant_id, user_id, device_id, posture_ok, posture_reasons), else None.
    Live = not revoked or expired, device not revoked, identity present, tenant in local mode."""
    if not brain_token or not brain_token.startswith(BRAIN_PREFIX):
        return None
    return control.execute(
        "SELECT d.tenant_id, d.user_id, d.id AS device_id,"
        "  coalesce(p.ok, false) AS posture_ok, coalesce(p.reasons, '{no report}') AS posture_reasons"
        " FROM launcher_tokens t"
        " JOIN launcher_devices d ON d.id = t.device_id"
        " JOIN identities i ON i.tenant_id = d.tenant_id AND i.user_id = d.user_id"
        " JOIN tenants tn ON tn.id = d.tenant_id AND tn.launch_mode = 'local' AND tn.cancelled_at IS NULL"
        " LEFT JOIN launcher_posture p ON p.device_id = d.id"
        " WHERE t.token_hash = %s AND t.kind = 'brain' AND t.revoked_at IS NULL"
        " AND t.expires_at > now() AND d.revoked_at IS NULL",
        (hash_token(brain_token),),
    ).fetchone()


def backend_for_ingress(control, ingress_secret: str) -> str | None:
    """The backend_id an ingress secret belongs to (the ingress may only check its own)."""
    if not ingress_secret:
        return None
    row = control.execute(
        "SELECT backend_id FROM user_backends WHERE ingress_secret_hash = %s",
        (hash_token(ingress_secret),),
    ).fetchone()
    return row["backend_id"] if row else None


def introspect(control, desktop_token: str, backend_id: str) -> dict | None:
    """(tenant_id, user_id, expires_at) for a live desktop token issued for THIS backend on a
    live device whose identity still exists, else None."""
    if not desktop_token:
        return None
    return control.execute(
        "SELECT d.tenant_id, d.user_id, t.expires_at FROM launcher_tokens t"
        " JOIN launcher_devices d ON d.id = t.device_id"
        " JOIN identities i ON i.tenant_id = d.tenant_id AND i.user_id = d.user_id"
        " JOIN user_backends b ON b.backend_id = t.backend_id"
        "  AND b.tenant_id = d.tenant_id AND b.user_id = d.user_id"
        " WHERE t.token_hash = %s AND t.kind = 'desktop' AND t.backend_id = %s"
        " AND t.revoked_at IS NULL AND t.expires_at > now() AND d.revoked_at IS NULL",
        (hash_token(desktop_token), backend_id),
    ).fetchone()


def revoke_device(control, device_id) -> None:
    control.execute(
        "UPDATE launcher_devices SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
        (device_id,),
    )
    control.execute(
        "UPDATE launcher_tokens SET revoked_at = now() WHERE device_id = %s AND revoked_at IS NULL",
        (device_id,),
    )
