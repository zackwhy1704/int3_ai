"""Operator commands for the launcher. Run with Postgres superuser credentials (tools/init
container), never from the API process.

    python -m app.launcher.admin invite --tenant brindlewood --user priya [--ttl-hours 72]
    python -m app.launcher.admin revoke-user --tenant brindlewood --user priya
    python -m app.launcher.admin record-build --platform windows --package-version <observed> --release v0.22.6

`invite` prints a single-use invite LINK (<server>/invite#<code>) to deliver out of band (e.g.
WhatsApp). `revoke-user` ends
every launcher device of that user (offboarding, lost laptop when they cannot do it themselves).
`record-build` records an OBSERVED official Hermes Desktop package version (the launcher shows it;
MSIX versions cannot be derived from the tag) against a release in hermes_compat, so the launcher
reports that build with the release's state instead of "untested".
"""

import argparse
import getpass
import os
from urllib.parse import urlsplit

from .. import config
from ..tenants.admin import admin_conn
from . import tokens


def _require_secure(url: str) -> str:
    """No plain-http fallback: http is accepted for loopback only (local stack / tests)."""
    parts = urlsplit(url)
    if parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
    ):
        return url
    raise SystemExit(f"refusing a non-https server URL: {url}")


def main() -> None:
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
    a = ap.parse_args()

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
                "SELECT state FROM hermes_compat WHERE release = %s", (a.release,)
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
                  f" (launcher will report: {known['state']})")
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
