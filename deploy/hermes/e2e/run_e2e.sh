#!/usr/bin/env bash
# E2E scenario = DEMO.md steps 1-5 + negative checks, driven against the provisioned
# gated Hermes backends (behavioral layer: gateway model + brain MCP; elicitations
# answered 'accept' == the desktop "Run"/choice 'once', never bypassed). Prints a
# pass/fail table and exits non-zero on ANY failure.
#
# Assumes the stack is up and the 3 users are provisioned (marcus/priya/ada) in the
# DEPLOY_PROJECT given (default hermes-deploy). Run via `make e2e`, which isolates it.
set -u
export MSYS_NO_PATHCONV=1   # harmless on Linux/CI; stops Git Bash mangling container paths
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="${DEPLOY_PROJECT:-hermes-deploy}"
if [ "$PROJECT" = "hermes-deploy" ]; then NS=""; else NS="$PROJECT-"; fi
DB="$PROJECT-db-1"
IMG="${MCP_IMAGE:-hermes-deploy-brain-mcp}"
MODEL="${ANTHROPIC_MODEL:-claude-sonnet-5-5}"
TAG="${E2E_TAG:-e2e$RANDOM}"
PY="$(command -v python3 || command -v python)"
: "${ADMIN_DB_PASSWORD:?set ADMIN_DB_PASSWORD}"

PASS=0; FAIL=0; ROWS=""
row() { ROWS="$ROWS$1\t$2\t$3\n"; if [ "$2" = PASS ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi; }
jget() { "$PY" -c 'import sys,json; d=json.load(sys.stdin); print(d'"$1"')' 2>/dev/null; }
contains() { case "$1" in *"$2"*) return 0;; *) return 1;; esac; }   # substring, pipe-free (no SIGPIPE, no em-dash byte issues)

creds() { # <user> -> echoes "GWTOKEN MCPSECRET"
  local u="$1" cfg
  cfg="$(MSYS_NO_PATHCONV=1 docker run --rm -v "hermes-home-${NS}brindlewood-$u:/opt/data" busybox cat /opt/data/config.yaml 2>/dev/null)"
  local gw sec
  gw="$(printf '%s' "$cfg" | grep 'api_key:' | sed -E 's/.*"(cb_svc_[^"]+)".*/\1/')"
  sec="$(printf '%s' "$cfg" | grep 'X-MCP-Secret:' | sed -E 's/.*"([a-f0-9]+)".*/\1/')"
  printf '%s %s' "$gw" "$sec"
}

agent() { # <user> <question> -> __E2E__ json (last line)
  local u="$1" q="$2"; read -r gw sec <<<"$(creds "$u")"
  docker run --rm -i --network "u-${NS}brindlewood-$u" \
    -e GATEWAY_URL="http://brain-gateway:8001" -e GATEWAY_TOKEN="$gw" \
    -e SIDECAR_URL="http://mcp-${NS}brindlewood-$u:9000/mcp" -e MCP_SECRET="$sec" \
    -e MODEL="$MODEL" -e Q="$q" "$IMG" python - < "$HERE/client_agent.py" 2>/dev/null | grep '^__E2E__ ' | sed 's/^__E2E__ //' | tail -1
}

capture() { # <user> <decision> <tool> <args-json> -> __E2E__ json (last line)
  local u="$1" dec="$2" tool="$3" args="$4"; read -r gw sec <<<"$(creds "$u")"
  docker run --rm -i --network "u-${NS}brindlewood-$u" \
    -e SIDECAR_URL="http://mcp-${NS}brindlewood-$u:9000/mcp" -e MCP_SECRET="$sec" \
    -e DECISION="$dec" -e TOOL="$tool" -e ARGS="$args" \
    "$IMG" python - < "$HERE/client_capture.py" 2>/dev/null | grep '^__E2E__ ' | sed 's/^__E2E__ //' | tail -1
}

dbq() { MSYS_NO_PATHCONV=1 docker exec -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DB" psql -U "${ADMIN_DB_USER:-brain}" -d t_brindlewood -tAc "$1" 2>/dev/null | tr -d '\r'; }

echo "== E2E scenario (project=$PROJECT, tag=$TAG) =="
# The scenario is idempotent: every taught fact is tagged with $TAG, and step 3a
# proves that tagged fact is NOT known before teaching — so this re-runs cleanly
# against the same stack (used by the regression red/green proof).

FINQ="When are invoices payable?"
# 1. finance user -> cited
j="$(agent marcus "$FINQ")"
if [ "$(printf '%s' "$j" | jget '["refused"]')" = "False" ] && printf '%s' "$j" | jget '["answered_from"]' | grep -q finance; then
  row "1. marcus finance -> cited (answered_from=finance)" PASS "$(printf '%s' "$j" | jget '["titles"]')"
else row "1. marcus finance -> cited" FAIL "$j"; fi

# 2. operations user, SAME question -> refused, no finance strings leaked
j="$(agent priya "$FINQ")"
leak=""; for s in "30 days" "45 days" "Larkspur" "20,000"; do case "$j" in *"$s"*) leak="$leak[$s]";; esac; done
if [ "$(printf '%s' "$j" | jget '["refused"]')" = "True" ] && [ -z "$leak" ]; then
  row "2. priya same finance Q -> refused, no leak" PASS "refused, no canary"
else row "2. priya same finance Q -> refused, no leak" FAIL "refused=$(printf '%s' "$j" | jget '["refused"]') leak=$leak"; fi

SUBJ="loading bay $TAG"; OPSQ="What time does the $TAG loading bay open?"
STATEMENT="$SUBJ — opening time: 6am on weekdays"
PARGS="$(S="$SUBJ" "$PY" -c 'import json,os;print(json.dumps({"scope":"operations","subject":os.environ["S"],"attribute":"opening time","value":"6am on weekdays","quote":"The "+os.environ["S"]+" opens at 6am on weekdays."}))')"

# baseline: priya asks before teaching -> refused
j="$(agent priya "$OPSQ")"
[ "$(printf '%s' "$j" | jget '["refused"]')" = "True" ] && row "3a. priya pre-teach -> refused" PASS "refused" || row "3a. priya pre-teach -> refused" FAIL "$j"

# 3. priya proposes -> elicitation shows the EXACT stored statement
j="$(capture priya accept propose_claim "$PARGS")"
msg="$(printf '%s' "$j" | jget '["elicitation_message"]')"; pid="$(printf '%s' "$j" | jget '["result"]["proposal_id"]')"
# exact stored statement = "<subject> — opening time: <value>"; assert its fields are
# all present (avoids a fragile em-dash byte compare).
if contains "$msg" "$SUBJ" && contains "$msg" "opening time" && contains "$msg" "6am on weekdays"; then
  row "3. priya propose -> elicitation shows the exact statement" PASS "pid=$pid"
else row "3. priya propose -> elicitation shows the exact statement" FAIL "msg=$(printf '%s' "$msg" | head -c 100)"; fi

# NEGATIVE: priya (not owner) cannot approve -> 403
j="$(capture priya accept approve_proposal "$(P="$pid" "$PY" -c 'import json,os;print(json.dumps({"proposal_id":os.environ["P"]}))')")"
[ "$(printf '%s' "$j" | jget '["result"]["error"]')" = "403" ] && row "N1. priya approve own proposal -> 403" PASS "403" || row "N1. priya approve -> 403" FAIL "$j"

# 4. ada (owner) approves -> elicitation shows statement + proposer
j="$(capture ada accept approve_proposal "$(P="$pid" "$PY" -c 'import json,os;print(json.dumps({"proposal_id":os.environ["P"]}))')")"
msg="$(printf '%s' "$j" | jget '["elicitation_message"]')"; title="$(printf '%s' "$j" | jget '["result"]["title"]')"
if contains "$msg" "$SUBJ" && contains "$msg" "6am on weekdays" && contains "$msg" "priya"; then
  row "4. ada approve -> elicitation has statement + proposer" PASS "title=$title"
else row "4. ada approve -> elicitation has statement + proposer" FAIL "msg=$(printf '%s' "$msg" | head -c 100)"; fi

# 5. priya re-asks -> cited to the workflow record
j="$(agent priya "$OPSQ")"
if [ "$(printf '%s' "$j" | jget '["refused"]')" = "False" ] && printf '%s' "$j" | jget '["titles"]' | grep -qi "operations workflow"; then
  row "5. priya re-ask -> cited workflow record" PASS "$(printf '%s' "$j" | jget '["titles"]')"
else row "5. priya re-ask -> cited workflow record" FAIL "$j"; fi

# NEGATIVE: a DECLINED elicitation creates 0 proposals
DSUBJ="declined fact $TAG"
DARGS="$(S="$DSUBJ" "$PY" -c 'import json,os;print(json.dumps({"scope":"operations","subject":os.environ["S"],"attribute":"x","value":"y","quote":"z"}))')"
j="$(capture priya decline propose_claim "$DARGS")"
n="$(dbq "SELECT count(*) FROM proposals WHERE subject='$DSUBJ'")"
if [ "$(printf '%s' "$j" | jget '["result"]["status"]')" = "not_confirmed" ] && [ "${n:-x}" = 0 ]; then
  row "N2. declined elicitation -> 0 proposals" PASS "status=not_confirmed, rows=0"
else row "N2. declined elicitation -> 0 proposals" FAIL "status=$(printf '%s' "$j" | jget '["result"]["status"]') rows=$n"; fi

# NEGATIVE: verify-isolation.sh passes
if DEPLOY_PROJECT="$PROJECT" MCP_IMAGE="$IMG" bash "$HERE/../verify-isolation.sh" brindlewood priya ada >/tmp/e2e-iso.log 2>&1; then
  row "N3. verify-isolation.sh" PASS "all checks passed"
else row "N3. verify-isolation.sh" FAIL "$(tail -1 /tmp/e2e-iso.log)"; fi

echo ""
echo "================ E2E RESULTS ================"
printf "%b" "$ROWS" | awk -F'\t' '{printf "  %-4s %-48s %s\n", $2, $1, $3}'
echo "============================================="
echo "  PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || { echo "E2E FAILED"; exit 1; }
echo "E2E PASSED"
