#!/usr/bin/env bash
# Prove per-user isolation holds for two provisioned users. Exits non-zero if any
# check fails, so it can gate CI or a post-provision smoke test.
#
#   ./verify-isolation.sh [tenant] [userA] [userB]      # default: brindlewood marcus priya
#
# A = victim, B = attacker.
#   NETWORK  B's backend cannot reach A's sidecar, the brain, or A's backend;
#            B CAN reach its own sidecar.
#   AUTH     B's own sidecar with NO secret -> 401; A's sidecar with B's (wrong)
#            secret -> 401 (from the brain network, which can route to it).
set -euo pipefail
T="${1:-brindlewood}"; A="${2:-marcus}"; B="${3:-priya}"
# Same DEPLOY_PROJECT namespace as provision-user.sh (unset -> default, names unchanged).
PROJECT="${DEPLOY_PROJECT:-hermes-deploy}"
if [ "$PROJECT" = "hermes-deploy" ]; then NS=""; else NS="$PROJECT-"; fi
HB="hermes-${NS}$T-$B"; HA="hermes-${NS}$T-$A"; A_SIDE="mcp-${NS}$T-$A"; B_SIDE="mcp-${NS}$T-$B"
BRAIN_NET="${BRAIN_NET:-$PROJECT-brain}"; MCP_IMAGE="${MCP_IMAGE:-hermes-deploy-brain-mcp}"
fail() { echo "ISOLATION FAIL: $*" >&2; exit 1; }

B_SECRET=$(docker exec "$HB" sh -c 'grep X-MCP-Secret /opt/data/config.yaml' | sed -E 's/.*"([^"]+)".*/\1/' | tr -d '\r\n')

# POST /mcp and echo the HTTP status; direct (no egress proxy). URL + optional
# secret come from env so there is no shell interpolation into the program.
PROBE='import os,urllib.request,urllib.error
req=urllib.request.Request(os.environ["U"],data=b"{}",method="POST")
req.add_header("content-type","application/json")
req.add_header("accept","application/json, text/event-stream")
s=os.environ.get("S","")
if s: req.add_header("x-mcp-secret",s)
try:
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=8); print(200)
except urllib.error.HTTPError as e: print(e.code)
except Exception as e: print("ERR",type(e).__name__)'

echo "[verify] NETWORK from $HB (attacker) ..."
docker exec -e ASIDE="$A_SIDE" -e HA="$HA" -e BSIDE="$B_SIDE" "$HB" python3 -c '
import os,socket,sys
def blocked(h,p):
    try: socket.create_connection((h,p),timeout=4); return False
    except Exception: return True
cross={os.environ["ASIDE"]+":9000":blocked(os.environ["ASIDE"],9000),
       "brain:8000":blocked("brain",8000),
       os.environ["HA"]+":9122":blocked(os.environ["HA"],9122)}
b=os.environ["BSIDE"]
own=not blocked(b,9000)
for k,v in cross.items(): print("  %-42s blocked=%s" % (k,v))
print("  %-42s reachable=%s" % (b+":9000 (own)", own))
sys.exit(0 if all(cross.values()) and own else 1)
' || fail "network isolation breached"

echo "[verify] AUTH: $B_SIDE with NO secret -> expect 401"
c=$(docker exec -e U="http://$B_SIDE:9000/mcp" "$HB" python3 -c "$PROBE")
echo "  -> $c"; [ "$c" = 401 ] || fail "own sidecar without secret returned '$c' (want 401)"

echo "[verify] AUTH: $A_SIDE with $B's (wrong) secret from brain-net -> expect 401"
c=$(docker run --rm -e U="http://$A_SIDE:9000/mcp" -e S="$B_SECRET" --network "$BRAIN_NET" "$MCP_IMAGE" python3 -c "$PROBE")
echo "  -> $c"; [ "$c" = 401 ] || fail "$A sidecar with $B's secret returned '$c' (want 401)"

echo "[verify] ALL ISOLATION CHECKS PASSED ($B cannot reach $A's brain, by network AND by auth)"
