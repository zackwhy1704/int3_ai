"""Local mode: the Hermes setup we require on a user's PC, and the server-side judgement of the
launcher's report of it (docs/LOCAL_MODE_DESIGN.md §3.1; Hermes v0.23.1 citations there).

The launcher applies POLICY with Hermes's own `hermes config set`, reads config.yaml back, and
reports what it found. The verdict is made HERE, so a launcher cannot simply claim "ok". This is
detection, not prevention: the PC is the user's, and a determined user can fake a report. Its job
is to stop accidental or casual weakening, and to make the brain refuse when the setup drifts.
"""

from typing import Any

WORKSPACE_DIRNAME = "int3-workspace"
MCP_SERVER_NAME = "int3_brain"

# Toolsets that must stay off (hermes v0.23.1 toolsets.py names). `memory` is deliberately absent:
# gating that toolset off also hides an external memory provider's tools (memory_manager_tools.py).
REQUIRED_DISABLED_TOOLSETS = (
    "terminal", "code_execution", "browser", "web", "search", "x_search", "computer_use",
    "image_gen", "video_gen", "tts", "connections", "delegation", "cronjob", "desktop_ui", "kanban",
)

MCP_TOOLS = ("search_brain", "validate_citations", "propose_claim", "list_proposals",
             "approve_proposal", "reject_proposal")

SOUL_BEGIN = "<!-- int3:begin (managed by the int3 launcher; edits inside are replaced) -->"
SOUL_END = "<!-- int3:end -->"
SOUL_BLOCK = """## Company knowledge (int3)
For questions about the company, its policies, people, prices, processes or history, use the
int3 brain: call `search_brain`, answer ONLY from the passages it returns, and cite every fact
as [claim:ID] using the ids shown. Before you send the answer, call `validate_citations` with the
retrieval_id and the ids you cited, and drop anything it reports as invalid. If the brain returns
no passages, say the company knowledge has no reliable source for it; do not guess.
If a brain tool says your int3 access expired or was refused, tell the user to reopen int3."""


def policy(mcp_url: str) -> dict[str, Any]:
    """What the launcher must apply. Values are written with `hermes config set` (config.yaml) or
    into .env (UPPER_SNAKE). The brain token itself is NOT here: it is minted only after a good
    report."""
    return {
        "config": {
            "agent.disabled_toolsets": list(REQUIRED_DISABLED_TOOLSETS),
            "memory.memory_enabled": False,
            "memory.user_profile_enabled": False,
            "memory.nudge_interval": 0,
            "approvals.mode": "manual",
            "approvals.cron_mode": "deny",
            "approvals.unattended_mode": "deny",
            "telemetry.shared_metrics.enabled": False,
            "skills.inline_shell": False,
            f"mcp_servers.{MCP_SERVER_NAME}": {
                "url": mcp_url,
                "headers": {"Authorization": "Bearer ${INT3_BRAIN_TOKEN}"},
                "tools": {"include": list(MCP_TOOLS)},
                "timeout": 60,
            },
        },
        "workspace_dirname": WORKSPACE_DIRNAME,
        "soul": {"begin": SOUL_BEGIN, "end": SOUL_END, "block": SOUL_BLOCK},
        "mcp_server_name": MCP_SERVER_NAME,
    }


def evaluate(report: dict[str, Any], mcp_url: str) -> tuple[bool, list[str]]:
    """(ok, reasons) for the launcher's report of the local Hermes setup. Fail closed: a missing or
    malformed field is a reason."""
    reasons: list[str] = []

    disabled = report.get("disabled_toolsets")
    if not isinstance(disabled, list):
        reasons.append("disabled toolsets not reported")
    else:
        missing = sorted(set(REQUIRED_DISABLED_TOOLSETS) - {str(x) for x in disabled})
        if missing:
            reasons.append("tools switched back on: " + ", ".join(missing))

    if report.get("approvals_mode") != "manual":
        reasons.append(f"approvals mode is {report.get('approvals_mode')!r}, not 'manual'")
    if report.get("cron_mode") != "deny":
        reasons.append("scheduled jobs may run dangerous commands")
    if report.get("memory_enabled") is not False or report.get("user_profile_enabled") is not False:
        reasons.append("Hermes's own memory is on")

    servers = report.get("mcp_servers")
    if not isinstance(servers, list):
        reasons.append("MCP servers not reported")
    else:
        extra = sorted(str(s) for s in servers if s != MCP_SERVER_NAME)
        if extra:
            reasons.append("other MCP servers added: " + ", ".join(extra))
        if MCP_SERVER_NAME not in servers:
            reasons.append("the int3 brain entry is missing")
    if report.get("mcp_url") != mcp_url:
        reasons.append("the int3 brain entry points somewhere else")

    plugins = report.get("plugins")
    if not isinstance(plugins, list):
        reasons.append("plugins not reported")
    elif plugins:
        reasons.append("extra Hermes plugins installed: " + ", ".join(sorted(map(str, plugins))))

    root = report.get("write_safe_root")
    if not isinstance(root, str) or not root.rstrip("\\/").lower().endswith(WORKSPACE_DIRNAME):
        reasons.append("file writes are not confined to the int3 workspace")
    if report.get("workspace_synced") is not False:
        reasons.append("the int3 workspace is inside a synced folder (OneDrive or similar)")

    return (not reasons, reasons)
