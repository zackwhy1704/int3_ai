"""Operator commands for the launcher. Run with Postgres superuser credentials (tools/init
container), never from the API process.

    python -m app.launcher.admin invite --tenant brindlewood --user priya [--ttl-hours 72]
    python -m app.launcher.admin revoke-user --tenant brindlewood --user priya
    python -m app.launcher.admin record-build --platform windows --package-version <observed> --release v0.22.6
    python -m app.launcher.admin cancel-tenant --tenant brindlewood
    python -m app.launcher.admin purge-memories [--days 30]
    python -m app.launcher.admin set-launch-mode --tenant brindlewood --mode local|hosted
    python -m app.launcher.admin set-credential-sources --tenant brindlewood api_key,local_endpoint,hermes_oauth

`invite` prints a single-use invite LINK (<server>/invite#<code>) to deliver out of band (e.g.
WhatsApp). `revoke-user` ends
every launcher device of that user (offboarding, lost laptop when they cannot do it themselves).
`record-build` records an OBSERVED official Hermes Desktop package version (the launcher shows it;
MSIX versions cannot be derived from the tag) against a release in hermes_compat, so the launcher
reports that build with the release's state instead of "untested".
`cancel-tenant` ends the company's access at once (every launcher device revoked; brain tokens of a
cancelled tenant are refused) and starts the memory retention clock. `purge-memories` deletes the
memories of every tenant cancelled more than MEMORY_RETENTION_DAYS (default 30) ago and audits it;
run it daily (e.g. a systemd timer on the VM). Both print counts only, never content.
`set-launch-mode` switches a company between hosted Hermes (a container per user on the VM) and local
mode (Hermes on each user's PC, docs/LOCAL_MODE_DESIGN.md); switching to hosted ends local brain tokens
at once (tokens.resolve_brain). `set-credential-sources` sets which kinds of AI model credential the
company's local Hermes may use (local_policy.CREDENTIAL_SOURCES). Both refuse to run until the control
database is at the current version (`python -m app.tenants.admin migrate`).
"""

import argparse
import getpass
import os
from urllib.parse import urlsplit

from .. import config, migrations
from ..tenants.admin import admin_conn
from . import local_policy, memory, tokens


def _require_secure(url: str) -> str:
    """No plain-http fallback: http is accepted for loopback only (local stack / tests)."""
    parts = urlsplit(url)
    if parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
    ):
        return url
    raise SystemExit(f"refusing a non-https server URL: {url}")


def _require_control_current(control) -> None:
    at = migrations.current(control)
    if at < migrations.CONTROL_VERSION:
        raise SystemExit(f"the control database is at v{at}; this code needs v{migrations.CONTROL_VERSION}."
                         " Run `python -m app.tenants.admin migrate` first.")


def _tenant(control, tenant_id: str) -> dict:
    row = control.execute("SELECT id, launch_mode, cancelled_at, allowed_credential_sources FROM tenants"
                          " WHERE id = %s", (tenant_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no tenant {tenant_id}")
    return row


def _sources(text: str) -> list[str]:
    """Comma-separated credential sources -> a validated list in the canonical order."""
    given = [s.strip() for s in text.split(",") if s.strip()]
    if not given:
        raise SystemExit("give at least one credential source, e.g. api_key,local_endpoint,hermes_oauth")
    unknown = sorted(set(given) - set(local_policy.CREDENTIAL_SOURCES))
    if unknown:
        raise SystemExit(f"unknown credential source(s): {', '.join(unknown)}."
                         f" Known: {', '.join(local_policy.CREDENTIAL_SOURCES)}")
    return [s for s in local_policy.CREDENTIAL_SOURCES if s in given]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.launcher.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("invite")
    inv.add_argument("--tenant", required=True)
    inv.add_argument("--user", required=True)
    inv.add_argument("--ttl-hours", type=float, default=72.0)
    # The invite is delivered as a LINK: <server>/invite#<code>. The launcher takes the server from
    # the link's origin and the code from the fragment (a fragment is never sent in an HTTP request,
    # so a browser or proxy that sees the link never logs the code).
    inv.add_argument("--server-url", default=os.environ.get("LAUNCHER_PUBLIC_URL", "http://127.0.0.1:8100"))
    rev = sub.add_parser("revoke-user")
    rev.add_argument("--tenant", required=True)
    rev.add_argument("--user", required=True)
    rb = sub.add_parser("record-build")
    rb.add_argument("--platform", required=True, choices=["windows", "macos"])
    rb.add_argument("--package-version", required=True)
    rb.add_argument("--release", required=True)
    ct = sub.add_parser("cancel-tenant")
    ct.add_argument("--tenant", required=True)
    pm = sub.add_parser("purge-memories")
    pm.add_argument("--days", type=int, default=None)
    lm = sub.add_parser("set-launch-mode")
    lm.add_argument("--tenant", required=True)
    lm.add_argument("--mode", required=True, choices=["local", "hosted"])
    cs = sub.add_parser("set-credential-sources")
    cs.add_argument("--tenant", required=True)
    cs.add_argument("sources", help="comma-separated: " + ",".join(local_policy.CREDENTIAL_SOURCES))
    a = ap.parse_args(argv)

    if a.cmd == "purge-memories":
        done = memory.purge_cancelled(a.days)
        for tenant_id, n in done:
            print(f"purged {n} memor{'y' if n == 1 else 'ies'} of cancelled tenant {tenant_id}")
        print(f"{len(done)} cancelled tenant(s) past retention")
        return

    with admin_conn(config.CONTROL_DB) as control:
        if a.cmd == "invite":
            server_url = _require_secure(a.server_url)
            code = tokens.mint_invite(
                control, a.tenant, a.user, a.ttl_hours, f"operator:{getpass.getuser()}"
            )
            print(f"single-use launcher invite for {a.user}@{a.tenant}"
                  f" (expires in {min(a.ttl_hours, config.LAUNCHER_INVITE_MAX_HOURS):g}h):")
            print(f"  {server_url.rstrip('/')}/invite#{code}")
        elif a.cmd == "record-build":
            if not all(part.isdigit() for part in a.package_version.split(".")):
                raise SystemExit("package version must be dotted digits, e.g. 2026.7300.1.0")
            known = control.execute(
                "SELECT state, local_state FROM hermes_compat WHERE release = %s", (a.release,)
            ).fetchone()
            if known is None:
                raise SystemExit(f"release {a.release} is not in hermes_compat")
            control.execute(
                "INSERT INTO hermes_desktop_builds (platform, package_version, release)"
                " VALUES (%s, %s, %s) ON CONFLICT (platform, package_version)"
                " DO UPDATE SET release = EXCLUDED.release",
                (a.platform, a.package_version, a.release),
            )
            print(f"recorded {a.platform} Hermes Desktop {a.package_version} = {a.release}"
                  f" (launcher will report: hosted {known['state']}, local {known['local_state']})")
        elif a.cmd == "set-launch-mode":
            _require_control_current(control)
            t = _tenant(control, a.tenant)
            if a.mode == "local" and t["cancelled_at"] is not None:
                raise SystemExit(f"tenant {a.tenant} is cancelled; it cannot be switched to local mode")
            control.execute("UPDATE tenants SET launch_mode = %s WHERE id = %s", (a.mode, a.tenant))
            backends = control.execute("SELECT count(*) AS n FROM user_backends WHERE tenant_id = %s",
                                       (a.tenant,)).fetchone()["n"]
            print(f"tenant {a.tenant}: launch mode {t['launch_mode']} -> {a.mode}")
            if a.mode == "local" and backends:
                print(f"  note: {backends} hosted per-user backend(s) still exist; local mode does not use"
                      " them (remove them separately if the company will not switch back)")
            if a.mode == "hosted" and t["launch_mode"] == "local":
                print("  local-mode brain tokens of this tenant stop working at once")
        elif a.cmd == "set-credential-sources":
            _require_control_current(control)
            t = _tenant(control, a.tenant)
            new = _sources(a.sources)
            control.execute("UPDATE tenants SET allowed_credential_sources = %s WHERE id = %s", (new, a.tenant))
            print(f"tenant {a.tenant}: credential sources {','.join(t['allowed_credential_sources'])}"
                  f" -> {','.join(new)}")
            if "personal_login" in new:
                print("  note: personal_login lets Hermes use an employee's own Claude Code / ChatGPT login")
            print("  takes effect at each user's next launch (the launcher reports, the server checks)")
        elif a.cmd == "cancel-tenant":
            row = control.execute(
                "UPDATE tenants SET cancelled_at = coalesce(cancelled_at, now()) WHERE id = %s"
                " RETURNING cancelled_at", (a.tenant,)).fetchone()
            if row is None:
                raise SystemExit(f"no tenant {a.tenant}")
            devices = control.execute(
                "SELECT id FROM launcher_devices WHERE tenant_id = %s AND revoked_at IS NULL", (a.tenant,)
            ).fetchall()
            for d in devices:
                tokens.revoke_device(control, d["id"])
            print(f"tenant {a.tenant} cancelled at {row['cancelled_at'].isoformat()}; revoked"
                  f" {len(devices)} launcher device(s); memories purge after"
                  f" {config.MEMORY_RETENTION_DAYS} days")
        else:
            devices = control.execute(
                "SELECT id FROM launcher_devices WHERE tenant_id = %s AND user_id = %s"
                " AND revoked_at IS NULL",
                (a.tenant, a.user),
            ).fetchall()
            for d in devices:
                tokens.revoke_device(control, d["id"])
            print(f"revoked {len(devices)} launcher device(s) for {a.user}@{a.tenant}")


if __name__ == "__main__":
    main()
