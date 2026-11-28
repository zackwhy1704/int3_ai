"""Operator commands for local mode (docs/LOCAL_MODE_DEPLOY_PLAN.md step 1): set-launch-mode,
set-credential-sources, and the control v4 migration they depend on."""
from __future__ import annotations

import pytest

from app import config, migrations
from app.launcher import admin as launcher_admin
from app.launcher import local_policy
from app.tenants import admin as tenants_admin
from app.tenants.admin import admin_conn

TENANT = "brindlewood"
V3_OUTCOMES = "('ok', 'empty', 'refused', 'rate_limited', 'error')"  # brain_audit at control v3


def _tenant() -> dict:
    with admin_conn(config.CONTROL_DB) as c:
        return c.execute("SELECT launch_mode, allowed_credential_sources, cancelled_at FROM tenants"
                         " WHERE id = %s", (TENANT,)).fetchone()


@pytest.fixture()
def restore_tenant():
    before = _tenant()
    yield
    with admin_conn(config.CONTROL_DB) as c:
        c.execute("UPDATE tenants SET launch_mode = %s, allowed_credential_sources = %s, cancelled_at = %s"
                  " WHERE id = %s", (before["launch_mode"], before["allowed_credential_sources"],
                                     before["cancelled_at"], TENANT))


def _run(capsys, *argv) -> str:
    launcher_admin.main(list(argv))
    return capsys.readouterr().out


def _refused(*argv) -> str:
    with pytest.raises(SystemExit) as e:
        launcher_admin.main(list(argv))
    return str(e.value)


def test_set_launch_mode_switches_both_ways(capsys, restore_tenant):
    out = _run(capsys, "set-launch-mode", "--tenant", TENANT, "--mode", "local")
    assert _tenant()["launch_mode"] == "local" and f"tenant {TENANT}: launch mode" in out and "-> local" in out
    out = _run(capsys, "set-launch-mode", "--tenant", TENANT, "--mode", "hosted")
    assert _tenant()["launch_mode"] == "hosted"
    assert "local -> hosted" in out and "brain tokens of this tenant stop working" in out


def test_set_launch_mode_refusals(capsys, restore_tenant):
    assert _refused("set-launch-mode", "--tenant", "no-such-co", "--mode", "local") == "no tenant no-such-co"
    with pytest.raises(SystemExit) as e:  # argparse: only local|hosted
        launcher_admin.main(["set-launch-mode", "--tenant", TENANT, "--mode", "Local"])
    assert e.value.code == 2
    with admin_conn(config.CONTROL_DB) as c:
        c.execute("UPDATE tenants SET cancelled_at = now() WHERE id = %s", (TENANT,))
    assert "is cancelled" in _refused("set-launch-mode", "--tenant", TENANT, "--mode", "local")
    assert _tenant()["launch_mode"] == "hosted"


def test_set_credential_sources_validates_and_stores_canonical_order(capsys, restore_tenant):
    out = _run(capsys, "set-credential-sources", "--tenant", TENANT, " hermes_oauth,api_key ,api_key")
    assert _tenant()["allowed_credential_sources"] == ["api_key", "hermes_oauth"]
    assert "-> api_key,hermes_oauth" in out and "next launch" in out and "personal_login" not in out
    out = _run(capsys, "set-credential-sources", "--tenant", TENANT, "api_key,personal_login")
    assert "personal_login lets Hermes use an employee's own" in out
    for bad, msg in (("api_key,openai", "unknown credential source(s): openai"),
                     (" , ", "give at least one credential source")):
        assert msg in _refused("set-credential-sources", "--tenant", TENANT, bad)
    assert _tenant()["allowed_credential_sources"] == ["api_key", "personal_login"]  # unchanged by refusals
    assert set(local_policy.CREDENTIAL_SOURCES) >= {"api_key", "personal_login"}


def test_control_migrations_are_applied_by_migrate_and_required_by_the_commands(capsys, restore_tenant):
    """Rewind the control DB to v3 (the old brain_audit outcome check, no v4+ rows): the commands refuse,
    `migrate` applies the current schema (not just the number), and the commands then work."""
    try:
        with admin_conn(config.CONTROL_DB) as c:
            c.execute("ALTER TABLE brain_audit DROP CONSTRAINT IF EXISTS brain_audit_outcome_check")
            # NOT VALID: the v3 check for new rows only (this DB already holds v4 'not_found' rows)
            c.execute("ALTER TABLE brain_audit ADD CONSTRAINT brain_audit_outcome_check"
                      f" CHECK (outcome IN {V3_OUTCOMES}) NOT VALID")
            c.execute("DELETE FROM schema_migrations WHERE version > 3")
            # A freshly built DB records only the version it was created at, so build the v1-v3 history
            # here instead of assuming a long-lived dev DB has it.
            for v in (1, 2, 3):
                migrations.record(c, v)
        msg = _refused("set-launch-mode", "--tenant", TENANT, "--mode", "local")
        assert msg.startswith("the control database is at v3; this code needs v5.") and "migrate" in msg
        assert "control database is at v3" in _refused("set-credential-sources", "--tenant", TENANT, "api_key")
        assert _tenant()["launch_mode"] == "hosted"

        tenants_admin.migrate()
        assert "migrated control: v3 -> v5" in capsys.readouterr().out
        with admin_conn(config.CONTROL_DB) as c:
            assert migrations.current(c) == migrations.CONTROL_VERSION == 5
            check = c.execute("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint"
                              " WHERE conname = 'brain_audit_outcome_check'").fetchone()["d"]
            assert "'not_found'" in check
        _run(capsys, "set-launch-mode", "--tenant", TENANT, "--mode", "local")
        assert _tenant()["launch_mode"] == "local"
    finally:  # never leave the test DB at v3
        tenants_admin.migrate()
        capsys.readouterr()


def _local_states() -> dict:
    with admin_conn(config.CONTROL_DB) as c:
        return {r["release"]: r["local_state"] for r in c.execute("SELECT release, local_state FROM hermes_compat")}


def test_control_v5_adds_local_state_once_and_keeps_an_operators_later_change(capsys):
    """A v4 control DB (no local_state, rows from the earlier seed): `migrate` adds the column, marks only the
    releases validated live in local mode supported, leaves the hosted `state` alone, and a later re-run of the
    schema (every init) does not undo an operator's change."""
    def hosted_states():
        with admin_conn(config.CONTROL_DB) as c:
            return {r["release"]: r["state"] for r in c.execute("SELECT release, state FROM hermes_compat")}

    hosted_before = hosted_states()
    try:
        with admin_conn(config.CONTROL_DB) as c:
            c.execute("ALTER TABLE hermes_compat DROP COLUMN local_state")
            c.execute("DELETE FROM schema_migrations WHERE version > 4")
            for v in (1, 2, 3, 4):
                migrations.record(c, v)
        tenants_admin.migrate()
        assert "migrated control: v4 -> v5" in capsys.readouterr().out
        local = _local_states()
        assert local["v0.23.3"] == local["v0.23.4"] == "supported"
        assert local["v0.23.1"] == local["v0.22.6"] == local["v0.21.7"] == "untested"
        assert hosted_states() == hosted_before  # additive: the hosted verdict is untouched

        with admin_conn(config.CONTROL_DB) as c:  # operator blocks v0.23.4 for local mode, then init re-runs
            c.execute("UPDATE hermes_compat SET local_state = 'blocked' WHERE release = 'v0.23.4'")
            c.execute((tenants_admin.APP_DIR / "control_schema.sql").read_text())
            with pytest.raises(Exception, match="hermes_compat_local_state_check"):
                with c.transaction():
                    c.execute("UPDATE hermes_compat SET local_state = 'maybe' WHERE release = 'v0.23.4'")
        assert _local_states()["v0.23.4"] == "blocked"
    finally:
        with admin_conn(config.CONTROL_DB) as c:
            c.execute("UPDATE hermes_compat SET local_state = 'supported' WHERE release IN ('v0.23.3', 'v0.23.4')")
        tenants_admin.migrate()
        capsys.readouterr()


def test_record_build_reports_both_verdicts(capsys):
    out = _run(capsys, "record-build", "--platform", "windows", "--package-version", "2026.7855.419.0",
               "--release", "v0.23.4")
    assert out.strip() == ("recorded windows Hermes Desktop 2026.7855.419.0 = v0.23.4"
                           " (launcher will report: hosted untested, local supported)")
