"""Operator commands for the launcher. Run with Postgres superuser credentials (tools/init
container), never from the API process.

    python -m app.launcher.admin invite --tenant brindlewood --user priya [--ttl-hours 72]
    python -m app.launcher.admin revoke-user --tenant brindlewood --user priya

`invite` prints a single-use code to deliver out of band (e.g. WhatsApp). `revoke-user` ends
every launcher device of that user (offboarding, lost laptop when they cannot do it themselves).
"""

import argparse
import getpass

from .. import config
from ..tenants.admin import admin_conn
from . import tokens


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.launcher.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("invite")
    inv.add_argument("--tenant", required=True)
    inv.add_argument("--user", required=True)
    inv.add_argument("--ttl-hours", type=float, default=72.0)
    rev = sub.add_parser("revoke-user")
    rev.add_argument("--tenant", required=True)
    rev.add_argument("--user", required=True)
    a = ap.parse_args()

    with admin_conn(config.CONTROL_DB) as control:
        if a.cmd == "invite":
            code = tokens.mint_invite(
                control, a.tenant, a.user, a.ttl_hours, f"operator:{getpass.getuser()}"
            )
            print(f"single-use launcher invite for {a.user}@{a.tenant}"
                  f" (expires in {min(a.ttl_hours, config.LAUNCHER_INVITE_MAX_HOURS):g}h):")
            print(f"  {code}")
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
