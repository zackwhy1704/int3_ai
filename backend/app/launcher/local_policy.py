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

# How the model credential got onto the PC (reported by label only, never the secret):
#   api_key        a key the user pasted (Hermes .env or its credential pool)
#   local_endpoint a model served on this PC (loopback base_url)
#   hermes_oauth   a sign-in done in Hermes's own Providers screen (auth.json)
#   personal_login a Claude Code / Codex CLI login Hermes adopted from the PC
#                  (agent/credential_sources.py:25-50 @v0.23.1, auth.adopt_external_logins)
#   custom_endpoint a non-local OpenAI-compatible endpoint
CREDENTIAL_SOURCES = ("api_key", "local_endpoint", "hermes_oauth", "personal_login", "custom_endpoint")
DEFAULT_ALLOWED_SOURCES = ("api_key", "local_endpoint", "hermes_oauth")

# Folder names of cloud sync clients. A write root under any of them is refused even if the launcher
# says the workspace is not synced (defence in depth: the launcher's own check could be wrong).
SYNC_SEGMENTS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud drive", "iclouddrive", "box")

# Hermes's own shared-metrics consent answer (hermes_cli/observability/shared_metrics_consent.py:53-90
# @v0.23.1): enabled/send PLUS the current offer_version is "decided", so no "Share with Nous" prompt.
SHARED_METRICS_OFFER_VERSION = 2

# Toolsets that must stay off (hermes v0.23.1 toolsets.py names). `memory` is deliberately absent:
# gating that toolset off also hides an external memory provider's tools (memory_manager_tools.py).
REQUIRED_DISABLED_TOOLSETS = (
    "terminal", "code_execution", "browser", "web", "search", "x_search", "computer_use",
    "image_gen", "video_gen", "tts", "connections", "delegation", "cronjob", "desktop_ui", "kanban",
)

# The only Hermes plugin local mode installs: the int3 memory provider (launcher/hermes-plugin/int3).
MEMORY_PLUGIN = "int3"

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
If a brain tool says your int3 access expired or was refused, tell the user to reopen int3.

## Saving: company knowledge or personal memory
- FIRST: if the user asks you to remember or save something WITHOUT saying who it is for (no
  "for <a team>", "for the company", "for me", and not about themselves), ALWAYS ask before saving:
  "Should I save this for the company (which scope?) or just for you?". Do this even when it
  sounds like a fact about the business. Save nothing until they answer.
- "Remember for <a team> / for the company …": call `propose_claim` with that scope's id, so the
  owner of that scope can approve it. NEVER save company facts with `int3_remember`.
- "Remember that I …", "for me", the user's own preferences, or their personal notes: call
  `int3_remember`.
- Use only the scope ids listed under "Your int3 scopes" below; never invent one. The card the
  user confirms shows the scope; if they want a different one, propose again with that one.
- Never state the status of a proposal you could not see (for example after a refusal): say you
  can't check it."""


def policy(mcp_url: str, allowed_sources: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_SOURCES,
           memory_url: str = "") -> dict[str, Any]:
    """What the launcher must apply. Values are written with `hermes config set` (config.yaml) or
    into .env (UPPER_SNAKE). The brain token itself is NOT here: it is minted only after a good
    report."""
    return {
        "config": {
            "agent.disabled_toolsets": list(REQUIRED_DISABLED_TOOLSETS),
            "memory.memory_enabled": False,
            "memory.user_profile_enabled": False,
            "memory.nudge_interval": 0,
            "memory.provider": MEMORY_PLUGIN,
            "approvals.mode": "manual",
            "approvals.cron_mode": "deny",
            "approvals.unattended_mode": "deny",
            # Tool lookup off: with ~15 toolsets disabled only a few tools are left, so listing them
            # directly is cheaper than the tool_search bridge and the model can call search_brain
            # first time. Measured on turn (a), 2 runs each at 10e1083+session fix: input 61.6k/61.3k
            # -> 45.5k/45.4k tokens, 5 -> 3 API calls, 4 -> 2 tool calls, 1 -> 0 failed calls.
            "tools.tool_search.enabled": "off",
            "telemetry.shared_metrics.enabled": False,
            "telemetry.shared_metrics.send": False,
            "telemetry.shared_metrics.offer_version": SHARED_METRICS_OFFER_VERSION,
            # Never borrow the PC's Claude Code / Codex CLI login unless the tenant allows it.
            "auth.adopt_external_logins": "personal_login" in allowed_sources,
            "skills.inline_shell": False,
            f"mcp_servers.{MCP_SERVER_NAME}": {
                "url": mcp_url,
                "headers": {"Authorization": "Bearer ${INT3_BRAIN_TOKEN}"},
                # resources/prompts off: Hermes would otherwise add list_resources/read_resource/
                # list_prompts/get_prompt for this server (v0.23.1 tools/mcp_tool_registration.py:
                # 190-191, tools/mcp_tool_schema.py:332-335); the endpoint serves neither.
                "tools": {"include": list(MCP_TOOLS), "resources": False, "prompts": False},
                # Covers the user's time on a confirmation card: the endpoint's approval window plus
                # two brain calls (local_server.py: 90 + 2 * 15 = 120) plus 15 s margin for network and
                # scheduling, so the endpoint's clear "no answer, ask again" arrives before Hermes's
                # generic timeout.
                "timeout": 135,
            },
        },
        "workspace_dirname": WORKSPACE_DIRNAME,
        "soul": {"begin": SOUL_BEGIN, "end": SOUL_END, "block": SOUL_BLOCK},
        "mcp_server_name": MCP_SERVER_NAME,
        "memory_plugin": MEMORY_PLUGIN,
        "memory_url": memory_url,
    }


def _in_sync_folder(path: str) -> bool:
    segments = [s.strip().lower() for s in path.replace("/", "\\").split("\\") if s.strip()]
    return any(seg == name or seg.startswith((name + " ", name + "-"))
               for seg in segments for name in SYNC_SEGMENTS)


def evaluate(report: dict[str, Any], mcp_url: str,
             allowed_sources: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_SOURCES,
             memory_url: str = "") -> tuple[bool, list[str]]:
    """(ok, reasons) for the launcher's report of the local Hermes setup. Fail closed: a missing or
    malformed field is a reason."""
    reasons: list[str] = []
    allowed = set(allowed_sources)

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
    else:
        extra = sorted(str(x) for x in plugins if x != MEMORY_PLUGIN)
        if extra:
            reasons.append("extra Hermes plugins installed: " + ", ".join(extra))
    # The int3 memory provider must be the active one (an older launcher reports no field: tolerated
    # only when absent, so an M1 report still passes until the launcher is updated).
    if "memory_provider" in report and report.get("memory_provider") != MEMORY_PLUGIN:
        reasons.append("the int3 memory is switched off or replaced in Hermes")
    if "memory_plugin_intact" in report and report.get("memory_plugin_intact") is not True:
        reasons.append("the int3 memory plugin is missing or was changed")
    if memory_url and "memory_url" in report and report.get("memory_url") != memory_url:
        reasons.append("the int3 memory points somewhere else")

    root = report.get("write_safe_root")
    if not isinstance(root, str) or not root.rstrip("\\/").lower().endswith(WORKSPACE_DIRNAME):
        reasons.append("file writes are not confined to the int3 workspace")
    if report.get("workspace_synced") is not False or (isinstance(root, str) and _in_sync_folder(root)):
        reasons.append("the int3 workspace is inside a synced folder (OneDrive or similar)")

    if report.get("telemetry_enabled") is not False:
        reasons.append("sharing usage data with Nous is on")
    if report.get("inline_shell") is not False:
        reasons.append("skills may run commands (skills.inline_shell)")
    if report.get("mcp_resources") is not False or report.get("mcp_prompts") is not False:
        reasons.append("the int3 brain entry has MCP resources or prompts switched back on")
    if report.get("tool_search") != "off":
        reasons.append("Hermes's tool lookup is switched back on (tools.tool_search)")

    # Hermes v0.23.3 Settings > Backup & Transfer moves this agent into a Hermes Cloud agent: it uploads
    # `hermes backup` of the whole Hermes home, .env (the brain token) and state.db (chats) included
    # (hermes_cli/backup.py:172-174, apps/desktop/src/lib/agent-transfer.ts:365-391). Every transfer
    # first saves that agent in Hermes's connections.json (backup-settings.tsx:186 before :188), which
    # the launcher reports. Tolerated only when absent, so an older launcher's report still passes.
    if "hermes_cloud" in report and report.get("hermes_cloud") is not False:
        reasons.append("Hermes is connected to Hermes Cloud. Moving an agent there copies your int3 access "
                       "and chats off this PC, so the int3 brain is off while it is connected. Remove the "
                       "Hermes Cloud connection in Hermes, then reopen int3")

    if report.get("adopt_external_logins") is not False and "personal_login" not in allowed:
        reasons.append("Hermes may reuse a personal Claude Code or ChatGPT login on this PC")
    sources = report.get("credential_sources")
    if not isinstance(sources, list):
        reasons.append("AI model credentials not reported")
    else:
        bad = sorted({str(s) for s in sources} - allowed)
        if bad:
            label = {"personal_login": "a personal Claude Code or ChatGPT login",
                     "custom_endpoint": "a model server elsewhere on the network",
                     "api_key": "an API key", "hermes_oauth": "a sign-in in Hermes",
                     "local_endpoint": "a model on this PC"}
            reasons.append("this company doesn't allow the AI model to use "
                           + ", ".join(label.get(b, f"unknown credential '{b}'") for b in bad)
                           + ". Choose another model in int3 › Choose AI model")

    return (not reasons, reasons)
