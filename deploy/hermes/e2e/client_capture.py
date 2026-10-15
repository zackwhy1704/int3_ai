# E2E capture client (behavioral layer). Runs INSIDE a user's u-net container.
# Drives ONE brain capture tool (propose_claim / approve_proposal / reject_proposal)
# on the user's MCP sidecar. The tool ELICITS a human confirmation (server.py
# ctx.elicit); this answers it as a real human would — accept == the desktop's "Run"
# button == approval choice 'once' (apps/desktop/.../tool/approval.tsx:385). It is NOT
# a bypass: the elicitation is genuinely answered through the MCP elicitation callback,
# the same place the gated backend routes the desktop's decision to. Prints one line:
#   __E2E__ {"elicitation_message": <str|null>, "result": <tool result json>}
import asyncio, json, os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult

URL = os.environ["SIDECAR_URL"]; SECRET = os.environ["MCP_SECRET"]
DECISION = os.environ.get("DECISION", "accept")   # accept (= human clicks "Run"/once) | decline
TOOL = os.environ["TOOL"]; ARGS = json.loads(os.environ.get("ARGS", "{}"))

captured = {"msg": None}


async def elicit_cb(context, params):
    captured["msg"] = getattr(params, "message", None)
    if DECISION == "accept":
        return ElicitResult(action="accept", content={})
    return ElicitResult(action=DECISION)   # 'decline' -> nothing is written


async def main():
    result = None
    async with httpx.AsyncClient(headers={"X-MCP-Secret": SECRET}) as hc:
        async with streamable_http_client(URL, http_client=hc) as streams:
            r, w = streams[0], streams[1]
            async with ClientSession(r, w, elicitation_callback=elicit_cb) as s:
                await s.initialize()
                res = await s.call_tool(TOOL, ARGS)
                text = res.content[0].text if res.content else "{}"
                try:
                    result = json.loads(text)
                except Exception:
                    result = {"raw": text}
    print("__E2E__ " + json.dumps({"elicitation_message": captured["msg"], "result": result}))


asyncio.run(main())
