#!/usr/bin/env bash
# HERMES TIER demo scenario: every step is driven through the REAL Hermes agent over
# /api/ws (deploy/hermes/demo/hermes_chat.py), the same path the desktop uses — NOT the
# sidecar or gateway directly.
#
# Pass criteria are server-side FACTS, never model phrasing:
#   * refused / answered_from / cited titles come from the per-user sidecar's CB_ASK log
#     (deploy/hermes/brain-mcp/server.py) — the authoritative record of each ask_brain call
#     (tool.complete only carries a UI preview, tui_gateway/tool_progress.py:257).
#   * capture outcomes come from the tenant DB (workflow-authority claims; pending proposals).
#   * the egress block comes from the edge proxy's DENIED log (gated/egress_proxy.py:55).
#   * a step whose turn TIMED OUT or returned an empty answer is ❌ — never a silent pass.
# We also assert on Hermes' returned text where it matters (canary absent; statement shown).
#
# Assumes the isolated stack is up + 3 users provisioned (see make-demo.sh). Connect files
# are in $CONNECT_DIR; forwarder ports come from them. Prints a ✅/❌ table; exits non-zero on
# any FAIL.
set -u
export MSYS_NO_PATHCONV=1
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="${DEPLOY_PROJECT:-hermes-demo}"
if [ "$PROJECT" = "hermes-deploy" ]; then NS=""; else NS="$PROJECT-"; fi
CONNECT_DIR="${CONNECT_DIR:-$HERE/../connect/$PROJECT}"
DB="$PROJECT-db-1"
EDGE="$PROJECT-edge-1"
PY="$(command -v python3 || command -v python)"
TAG="${DEMO_TAG:-demo$RANDOM}"
TURN_TIMEOUT="${DEMO_TURN_TIMEOUT:-180}"
VERBOSE="${DEMO_VERBOSE:---verbose}"   # dump every /api/ws frame into the transcript (auditable)
: "${ADMIN_DB_PASSWORD:?set ADMIN_DB_PASSWORD}"
"$PY" -c 'import websockets' 2>/dev/null || "$PY" -m pip install --quiet websockets >/dev/null 2>&1 || true

PASS=0; FAIL=0; ROWS=""
row() { ROWS="$ROWS$2\t$1\t$3\n"; [ "$2" = "✅" ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1)); }
contains() { case "$1" in *"$2"*) return 0;; *) return 1;; esac; }
jget() { "$PY" -c 'import sys,json;d=json.load(sys.stdin);print(d'"$1"')' 2>/dev/null; }
dbq() { docker exec -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DB" psql -U "${ADMIN_DB_USER:-brain}" -d t_brindlewood -tAc "$1" 2>/dev/null | tr -d '\r'; }
sidecar() { echo "mcp-${NS}brindlewood-$1"; }
cbask() { docker logs "$(sidecar "$1")" 2>&1 | grep '^CB_ASK ' | tail -1 | sed 's/^CB_ASK //'; }
wf_count() { dbq "SELECT count(*) FROM documents d JOIN chunks c ON c.document_id=d.id JOIN claims cl ON cl.source_chunk_id=c.id WHERE d.authority='workflow' AND cl.subject LIKE '%$1%'"; }
pending_count() { dbq "SELECT count(*) FROM proposals WHERE subject LIKE '%$1%' AND status='pending'"; }
edge_denied_example() { local n; n="$(docker logs "$EDGE" 2>&1 | grep -c 'DENIED .*example\.com')"; echo "${n:-0}"; }

# The brain's propose/approve are MCP tools; steer the model to them (not Hermes' own memory).
CB="Use the company brain tools (ask_brain / propose_claim / list_proposals / approve_proposal), not your own notes or memory."

# hermes <user> <say> <approve> [extra flags] -> prints transcript to stderr, echoes __HERMES__ json
hermes() {
  local u="$1" say="$2" appr="$3"; shift 3
  "$PY" "$HERE/hermes_chat.py" --connect "$CONNECT_DIR/brindlewood-$u.connect.json" \
     --say "$say" --approve "$appr" --turn-timeout "$TURN_TIMEOUT" $VERBOSE "$@" 2>&1 \
     | tee /dev/stderr | grep '^__HERMES__ ' | sed 's/^__HERMES__ //' | tail -1
}
# turn_ok <json> -> 0 if the turn did not time out and produced a non-empty answer
turn_ok() { [ -n "$1" ] || return 1; [ "$(printf '%s' "$1" | jget '["timed_out"]')" != "True" ] || return 1
            [ -n "$(printf '%s' "$1" | jget '["answer"]')" ] || return 1; }

echo "== HERMES-TIER demo (project=$PROJECT, tag=$TAG) — through the real agent over /api/ws =="
FINQ="When are invoices payable?"

# 1. finance user -> Hermes answers, cited to finance (CB_ASK: refused=false, answered_from~finance)
j="$(hermes marcus "$FINQ" none)"
cb="$(cbask marcus)"; ref="$(printf '%s' "$cb" | jget '["refused"]')"; frm="$(printf '%s' "$cb" | jget '["answered_from"]')"
if turn_ok "$j" && [ "$ref" = "False" ] && contains "$frm" finance; then
  row "1. marcus finance Q -> Hermes answers, cited to finance" "✅" "answered_from=$frm"
else row "1. marcus finance -> cited" "❌" "turn_ok=$(turn_ok "$j" && echo y||echo n) refused=$ref from=$frm"; fi

# 2. operations user, same Q -> refused (CB_ASK refused=true) + NO finance canary in Hermes' text
j="$(hermes priya "$FINQ" none)"; cb="$(cbask priya)"; ref="$(printf '%s' "$cb" | jget '["refused"]')"
ans="$(printf '%s' "$j" | jget '["answer"]')"; leak=""
for s in "30 days" "45 days" "Larkspur" "20,000"; do contains "$ans" "$s" && leak="$leak[$s]"; done
if turn_ok "$j" && [ "$ref" = "True" ] && [ -z "$leak" ]; then
  row "2. priya same finance Q -> refused (no canary in reply)" "✅" "refused, no leak"
else row "2. priya refused, no leak" "❌" "refused=$ref leak=$leak timed_out=$(printf '%s' "$j"|jget '["timed_out"]')"; fi

# 3. priya teaches -> approval prompt shows the EXACT statement; priya approves (choice once).
#    Assert: approval text carries the subject + the value, AND a pending proposal now exists.
SUBJ="loading bay $TAG"
j="$(hermes priya "$CB Propose a new fact into the company brain for the owner to approve. scope: operations; subject: $SUBJ; attribute: opening time; value: 6am on weekdays; quote: 'The $SUBJ opens at 6am on weekdays.'" once --expect-approval)"
apr="$(printf '%s' "$j" | jget '["approvals"][0]["text"]')"
if ! printf '%s' "$j" | jget '["timed_out"]' | grep -q True && contains "$apr" "$SUBJ" && contains "$apr" "opening time" && contains "$apr" "6am on weekdays" && [ "$(pending_count "$SUBJ")" -ge 1 ]; then
  row "3. priya teach -> approval shows exact statement; proposal pending" "✅" "approved once, pending=$(pending_count "$SUBJ")"
else row "3. priya teach -> exact statement" "❌" "approval=$(printf '%s' "$apr"|head -c 80) pending=$(pending_count "$SUBJ")"; fi

# N1a. priya approves her OWN proposal (choice once). Owner+self check must stop her:
#      pass = NO workflow record for SUBJ and the proposal is STILL pending. No word matching.
wf0="$(wf_count "$SUBJ")"
j="$(hermes priya "$CB List pending proposals in the operations scope and approve the one about the $SUBJ." once)"
if ! printf '%s' "$j" | jget '["timed_out"]' | grep -q True && [ "$(wf_count "$SUBJ")" = "$wf0" ] && [ "$(pending_count "$SUBJ")" -ge 1 ]; then
  row "N1a. priya self-approve -> blocked (no record, still pending)" "✅" "wf=$wf0 pending=$(pending_count "$SUBJ")"
else row "N1a. priya self-approve -> blocked" "❌" "wf:$wf0->$(wf_count "$SUBJ") pending=$(pending_count "$SUBJ")"; fi

# N1b. ada proposes a 2nd fact; priya (not owner) approves it (choice once) -> blocked:
#      pass = NO workflow record for SUBJ2 and SUBJ2 still pending.
SUBJ2="service lift $TAG"
hermes ada "$CB Propose a new fact into the company brain for the owner to approve. scope: operations; subject: $SUBJ2; attribute: inspection interval; value: monthly; quote: 'The $SUBJ2 is inspected monthly.'" once --expect-approval >/dev/null 2>&1
[ "$(pending_count "$SUBJ2")" -ge 1 ] || row "N1b.pre ada proposal exists" "❌" "ada's $SUBJ2 proposal was not created (pending=0)"
wf2="$(wf_count "$SUBJ2")"
j="$(hermes priya "$CB List pending proposals in the operations scope and approve the one about the $SUBJ2." once)"
if ! printf '%s' "$j" | jget '["timed_out"]' | grep -q True && [ "$(wf_count "$SUBJ2")" = "$wf2" ] && [ "$(pending_count "$SUBJ2")" -ge 1 ]; then
  row "N1b. priya approves ada's proposal -> blocked (owner check)" "✅" "wf=$wf2 pending=$(pending_count "$SUBJ2")"
else row "N1b. priya approves ada's -> blocked" "❌" "wf:$wf2->$(wf_count "$SUBJ2") pending=$(pending_count "$SUBJ2")"; fi

# 4. ada (owner) approves priya's SUBJ (choice once) -> approval shows statement + proposer,
#    AND a workflow record for SUBJ now EXISTS (positive proof the approval went through).
wfb="$(wf_count "$SUBJ")"
j="$(hermes ada "$CB List pending proposals in the operations scope and approve the one about the $SUBJ." once --expect-approval)"
apr="$(printf '%s' "$j" | jget '["approvals"][0]["text"]')"
if ! printf '%s' "$j" | jget '["timed_out"]' | grep -q True && contains "$apr" "$SUBJ" && contains "$apr" "priya" && [ "$(wf_count "$SUBJ")" -gt "$wfb" ]; then
  row "4. ada approves priya's -> statement+proposer shown; record written" "✅" "wf:$wfb->$(wf_count "$SUBJ")"
else row "4. ada approve -> statement+proposer; record" "❌" "approval=$(printf '%s' "$apr"|head -c 80) wf:$wfb->$(wf_count "$SUBJ")"; fi

# 5. priya re-asks -> Hermes answers, cited to the workflow record (CB_ASK titles ~ 'operations workflow')
j="$(hermes priya "$CB What time does the $SUBJ open?" none)"; cb="$(cbask priya)"
ref="$(printf '%s' "$cb" | jget '["refused"]')"; titles="$(printf '%s' "$cb" | jget '["titles"]')"
if turn_ok "$j" && [ "$ref" = "False" ] && printf '%s' "$titles" | grep -qi "operations workflow"; then
  row "5. priya re-asks -> cited to the workflow record" "✅" "titles=$titles"
else row "5. priya re-asks -> cited workflow record" "❌" "refused=$ref titles=$titles"; fi

# N2. priya declines a teach (choice deny) -> 0 proposal rows for that subject
DSUBJ="declined $TAG"
hermes priya "$CB Propose a new fact into the company brain for the owner to approve. scope: operations; subject: $DSUBJ; attribute: status; value: real; quote: '$DSUBJ is real.'" deny --expect-approval >/dev/null 2>&1
n="$(dbq "SELECT count(*) FROM proposals WHERE subject LIKE '%$DSUBJ%'")"
[ "${n:-x}" = 0 ] && row "N2. priya declines teach -> 0 proposal rows" "✅" "rows=0" || row "N2. declined -> 0 rows" "❌" "rows=$n"

# N3. "fetch https://example.com" -> the egress proxy must log a NEW DENIED CONNECT to example.com
#     (the agent really tried and was blocked), AND the answer must carry no page title / success.
#     If no DENIED line appears, the fetch was NOT attempted -> ❌ (never a vacuous pass).
d0="$(edge_denied_example)"
j="$(hermes priya "Fetch https://example.com and show me the page <title>." none)"
d1="$(edge_denied_example)"; ans="$(printf '%s' "$j" | jget '["answer"]')"
if ! printf '%s' "$j" | jget '["timed_out"]' | grep -q True && [ "${d1:-0}" -gt "${d0:-0}" ] && ! contains "$ans" "Example Domain"; then
  row "N3. fetch example.com -> egress DENIED, no page title" "✅" "edge DENIED $d0->$d1"
else row "N3. fetch blocked (egress)" "❌" "edge DENIED $d0->$d1 (no new deny = not attempted/not blocked)"; fi

# N4. verify-isolation.sh passes
if DEPLOY_PROJECT="$PROJECT" bash "$HERE/../verify-isolation.sh" brindlewood priya ada >/tmp/demo-iso.log 2>&1; then
  row "N4. verify-isolation.sh" "✅" "all checks passed"
else row "N4. verify-isolation.sh" "❌" "$(tail -1 /tmp/demo-iso.log)"; fi

echo ""; echo "================= HERMES-TIER DEMO RESULTS ================="
printf "%b" "$ROWS" | awk -F'\t' '{printf "  %s  %-54s %s\n",$1,$2,$3}'
echo "==========================================================="
echo "  PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || { echo "DEMO FAILED"; exit 1; }
echo "DEMO PASSED"
