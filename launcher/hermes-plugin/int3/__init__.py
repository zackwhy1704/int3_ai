"""int3 memory for Hermes (local mode, docs/LOCAL_MODE_DESIGN.md §3.3).

Installed by the int3 launcher into $HERMES_HOME/plugins/int3/ and selected with
``memory.provider: int3``. Hermes's own on-PC memory stays off; what the employee asks the assistant
to remember is kept on the company's int3 server instead, where they can see and delete it (int3 ›
Memory).

Explicit memories only: nothing is stored unless the assistant calls ``int3_remember`` (the
instructions below say: only when the user asks). Whole conversations are never uploaded:
``sync_turn`` does nothing. Before each turn, the few memories closest to the user's message are
recalled and given to the assistant as background.

Credentials: the brain token the launcher writes into this profile's .env (INT3_BRAIN_TOKEN) and the
server URL next to it (INT3_MEMORY_URL). The .env is re-read on every call so a token the launcher
rotates is picked up without restarting Hermes. Nothing here logs memory content or the token.

Standard library only. Interface: agent/memory_provider.py (Hermes v0.23.1).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

TOKEN_KEY = "INT3_BRAIN_TOKEN"
URL_KEY = "INT3_MEMORY_URL"
CALL_TIMEOUT = 15.0
PREFETCH_TIMEOUT = 3.0
EXPIRED = "Your int3 access has expired or was signed out. Reopen int3 to continue."

_SCHEMAS = [
    {
        "name": "int3_remember",
        "description": (
            "Save one short fact about the user or their work to their int3 memory, so you can recall "
            "it in later conversations. ONLY call this when the user explicitly asks you to remember "
            "something (\"remember that...\", \"note for next time...\"). Write it as one standalone "
            "sentence. Never save passwords, keys, or other people's personal data."
        ),
        "parameters": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "The fact, one sentence."}},
            "required": ["content"],
        },
    },
    {
        "name": "int3_recall",
        "description": "Search the user's int3 memory for facts they asked you to remember earlier.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look for."}},
            "required": ["query"],
        },
    },
    {
        "name": "int3_forget",
        "description": (
            "Delete one memory by its id (from int3_recall or the recalled-memory block) when the user "
            "asks you to forget it or says it is wrong."
        ),
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "The memory id."}},
            "required": ["id"],
        },
    },
]


def _env_file_value(home: Path | None, key: str) -> str:
    if home is None:
        return ""
    try:
        text = (home / ".env").read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    value = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, rest = line.partition("=")
        if sep and name.strip() == key:
            value = rest.strip().strip('"').strip("'")
    return value


class _Refused(Exception):
    pass


class Int3MemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._home: Path | None = None
        self._session_id = ""
        self._writes = True
        self._lock = threading.Lock()
        self._last_prefetch: tuple[str, str] = ("", "")

    @property
    def name(self) -> str:
        return "int3"

    # -- configuration ------------------------------------------------------

    def _setting(self, key: str) -> str:
        value = _env_file_value(self._home, key)
        if value:
            return value
        try:
            from agent.secret_scope import get_secret
            return get_secret(key, "") or ""
        except Exception:
            return os.environ.get(key, "")

    def is_available(self) -> bool:
        from hermes_constants import get_hermes_home
        home = self._home or Path(get_hermes_home())
        return bool(_env_file_value(home, TOKEN_KEY) or os.environ.get(TOKEN_KEY)) and bool(
            _env_file_value(home, URL_KEY) or os.environ.get(URL_KEY))

    def unavailable_reason(self) -> str:
        return "int3 memory needs int3 to be open and signed in. Open int3, then start Hermes from it."

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        self._home = Path(kwargs["hermes_home"]) if kwargs.get("hermes_home") else Path(get_hermes_home())
        self._session_id = session_id or ""
        # Only the user's own conversation writes; subagents / cron / flush only read.
        self._writes = kwargs.get("agent_context", "primary") in ("primary", None)

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id or ""

    # -- HTTP ---------------------------------------------------------------

    def _call(self, path: str, body: dict, timeout: float = CALL_TIMEOUT) -> dict:
        base, token = self._setting(URL_KEY).rstrip("/"), self._setting(TOKEN_KEY)
        if not base or not token:
            raise _Refused(EXPIRED)
        req = urllib.request.Request(
            base + "/launcher/local/memory/" + path,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL from the launcher)
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise _Refused(EXPIRED) from None
            try:
                detail = json.loads(e.read() or b"{}").get("detail")
            except Exception:
                detail = None
            if isinstance(detail, list):  # FastAPI validation errors
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
            raise _Refused(str(detail or f"int3 memory refused the request ({e.code})")) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise _Refused("int3 memory could not be reached; check the internet connection.") from None

    # -- what the assistant sees ------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "# int3 memory\n"
            "The user's memory is kept by their company's int3 service, not on this PC. Save something "
            "with int3_remember ONLY when the user explicitly asks you to remember it. Use int3_recall "
            "to look things up and int3_forget(id) when they ask you to forget something. The user can "
            "see and delete everything saved in int3 > Memory."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall the memories closest to this message. Bounded by a short timeout: a slow or
        unreachable server costs one turn's recall, never the turn."""
        query = (query or "").strip()
        if not query:
            return ""
        with self._lock:
            if self._last_prefetch[0] == query:
                return self._last_prefetch[1]
        try:
            found = self._call("recall", {"query": query[:4000], "k": 5}, timeout=PREFETCH_TIMEOUT)
        except _Refused as e:
            logger.debug("int3 recall skipped: %s", e)
            return ""
        memories = found.get("memories") or []
        block = ""
        if memories:
            lines = [f"- [memory {m['id']}] {m['content']}" for m in memories]
            block = "[int3 memory: things the user asked you to remember]\n" + "\n".join(lines)
        with self._lock:
            self._last_prefetch = (query, block)
        return block

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None:
        """Explicit memories only: conversations are never uploaded."""
        return None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [dict(s) for s in _SCHEMAS]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "int3_remember":
                if not self._writes:
                    return json.dumps({"error": "memory can only be saved from the user's own chat"})
                saved = self._call("save", {"content": str(args.get("content", "")),
                                            "session": self._session_id[:200] or None})
                return json.dumps({"saved": True, "id": saved.get("id")})
            if tool_name == "int3_recall":
                found = self._call("recall", {"query": str(args.get("query", ""))[:4000], "k": 8})
                return json.dumps({"memories": found.get("memories") or []})
            if tool_name == "int3_forget":
                if not self._writes:
                    return json.dumps({"error": "memory can only be changed from the user's own chat"})
                done = self._call("forget", {"id": int(args.get("id"))})
                with self._lock:
                    self._last_prefetch = ("", "")
                return json.dumps({"deleted": bool(done.get("deleted"))})
        except _Refused as e:
            return json.dumps({"error": str(e)})
        except (TypeError, ValueError):
            return json.dumps({"error": "bad arguments"})
        return json.dumps({"error": f"unknown tool {tool_name}"})


def register(ctx) -> None:
    ctx.register_memory_provider(Int3MemoryProvider())
