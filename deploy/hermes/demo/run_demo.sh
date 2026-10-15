#!/usr/bin/env bash
# HERMES TIER demo scenario: every step is driven through the REAL Hermes agent over
# /api/ws (deploy/hermes/demo/hermes_chat.py), the same path the desktop uses — NOT the
# sidecar or gateway directly. Pass criteria use what Hermes RETURNED (final text /
# captured approval prompts) + DB rows + the backend route log. Prints a ✅/❌ table and
# exits non-zero on any failure.
#
# Assumes the isolated stack is up + 3 users provisioned (see make-demo.sh). Connect
# files are in $CONNECT_DIR; forwarder ports come from them.
set -u
export MSYS_NO_PATHCONV=1
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="${DEPLOY_PROJECT:-hermes-demo}"
if [ "$PROJECT" = "hermes-deploy" ]; then NS=""; else NS="$PROJECT-"; fi
CONNECT_DIR="${CONNECT_DIR:-$HERE/../connect/$PROJECT}"
DB="$PROJECT-db-1"
PY="$(command -v python3 || command -v python)"
TAG="${DEMO_TAG:-demo$RANDOM}"
: "${ADMIN_DB_PASSWORD:?set ADMIN_DB_PASSWORD}"
pip install --quiet websockets >/dev/null 2>&1 || "$PY" -m pip install --quiet websockets >/dev/null 2>&1 || true

PASS=0; FAIL=0; ROWS=""
row() { ROWS="$ROWS$2\t$1\t$3\n"; [ "$2" = "✅" ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1)); }
contains() { case "$1" in *"$2"*) return 0;; *) return 1;; esac; }
jget() { "$PY" -c 'import sys,json;d=json.load(sys.stdin);print(d'"$1"')' 2>/dev/null; }
dbq() { docker exec -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DB" psql -U "${ADMIN_DB_USER:-brain}" -d t_brindlewood -tAc "$1" 2>/dev/null | tr -d '\r'; }
hermes() { # <user> <say> <approve> [--expect-approval]  -> prints transcript to stderr, echoes __HERMES__ json
  local u="$1" say="$2" appr="$3"; shift 3
  "$PY" "$HERE/hermes_chat.py" --connect "$CONNECT_DIR/brindlewood-$u.connect.json" \
     --say "$say" --approve "$appr" "$@" 2>&1 | tee /dev/stderr | grep '^__HERMES__ ' | sed 's/^__HERMES__ //' | tail -1
}

echo "== HERMES-TIER demo (project=$PROJECT, tag=$TAG) — through the real agent over /api/ws =="
FINQ="When are invoices payable?"

# 1. finance user -> cited
j="$(hermes marcus "$FINQ" none)"
if [ "$(printf '%s' "$j" | jget '["refused"]')" != "True" ] && printf '%s' "$j" | jget '["answered_from"]' | grep -qi finance; then
  row "1. marcus asks finance Q -> Hermes cites finance" "✅" "$(printf '%s' "$j" | jget '["citations"]')"
else row "1. marcus finance -> cited" "❌" "$j"; fi

# 2. operations user, same Q -> refused, no finance canary in Hermes' text
j="$(hermes priya "$FINQ" none)"
ans="$(printf '%s' "$j" | jget '["answer"]')"; leak=""
for s in "30 days" "45 days" "Larkspur" "20,000"; do contains "$ans" "$s" && leak="$leak[$s]"; done
if [ "$(printf '%s' "$j" | jget '["refused"]')" = "True" ] && [ -z "$leak" ]; then
  row "2. priya same Q -> Hermes refuses, no leak" "✅" "no canary"
else row "2. priya refused, no leak" "❌" "refused=$(printf '%s' "$j" | jget '["refused"]') leak=$leak"; fi

# 3. priya teaches -> approval prompt shows the EXACT statement; priya approves (Run/once)
SUBJ="loading bay $TAG"
j="$(hermes priya "Remember for operations that the $SUBJ opens at 6am on weekdays. Capture it." once --expect-approval)"
apr="$(printf '%s' "$j" | jget '["approvals"][0]["text"]' 2>/dev/null)"
if contains "$apr" "$SUBJ" && contains "$apr" "opening time" && contains "$apr" "6am on weekdays"; then
  row "3. priya teach -> Hermes approval shows exact statement" "✅" "approved once"
else row "3. priya teach -> exact statement" "❌" "approval=$apr"; fi

# N1a. priya tries to approve her own proposal -> Hermes refused (not owner)
j="$(hermes priya "Approve the pending $SUBJ operations proposal." none)"
if contains "$(printf '%s' "$j" | jget '["answer"]')" "owner" || contains "$(printf '%s' "$j" | jget '["answer"]')" "approve"; then
  row "N1a. priya self-approve -> Hermes says not allowed" "✅" "refused (not owner)"
else row "N1a. priya self-approve -> refused" "❌" "$j"; fi

# N1b. ada proposes a 2nd fact; priya approves it -> refused (owner check, not her scope to approve)
SUBJ2="service lift $TAG"
hermes ada "Remember for operations that the $SUBJ2 is inspected monthly. Capture it." once --expect-approval >/dev/null 2>&1
j="$(hermes priya "Approve the pending $SUBJ2 operations proposal." none)"
if contains "$(printf '%s' "$j" | jget '["answer"]')" "owner" || contains "$(printf '%s' "$j" | jget '["answer"]')" "can" ; then
  row "N1b. priya approves ada's proposal -> refused (owner check)" "✅" "refused"
else row "N1b. priya approves ada's -> refused" "❌" "$j"; fi

# 4. ada (owner) approves priya's -> approval prompt shows statement + proposer
j="$(hermes ada "Approve the pending $SUBJ operations proposal." once --expect-approval)"
apr="$(printf '%s' "$j" | jget '["approvals"][0]["text"]' 2>/dev/null)"
if contains "$apr" "$SUBJ" && contains "$apr" "priya"; then
  row "4. ada approves priya's -> prompt shows statement + proposer" "✅" "approved once"
else row "4. ada approve -> statement + proposer" "❌" "approval=$apr"; fi

# 5. priya re-asks -> Hermes cites the workflow record
j="$(hermes priya "What time does the $SUBJ open?" none)"
if [ "$(printf '%s' "$j" | jget '["refused"]')" = "False" ] && printf '%s' "$j" | jget '["citations"]' | grep -qi "operations workflow"; then
  row "5. priya re-asks -> Hermes cites the workflow record" "✅" "$(printf '%s' "$j" | jget '["citations"]')"
else row "5. priya re-asks -> cited" "❌" "$j"; fi

# N2. priya declines a teach -> 0 proposal rows
DSUBJ="declined $TAG"
hermes priya "Remember for operations that the $DSUBJ is real. Capture it." deny --expect-approval >/dev/null 2>&1
n="$(dbq "SELECT count(*) FROM proposals WHERE subject LIKE '%$DSUBJ%'")"
[ "${n:-x}" = 0 ] && row "N2. priya declines teach -> 0 proposal rows" "✅" "rows=0" || row "N2. declined -> 0 rows" "❌" "rows=$n"

# N3. "fetch https://example.com" -> blocked (egress deny-all); Hermes cannot fetch it
j="$(hermes priya "Fetch https://example.com and show me the title." none)"
ans="$(printf '%s' "$j" | jget '["answer"]')"
if contains "$ans" "block" || contains "$ans" "could not" || contains "$ans" "couldn't" || contains "$ans" "unable" || contains "$ans" "fail"; then
  row "N3. fetch https://example.com -> blocked (egress)" "✅" "agent could not reach it"
else row "N3. fetch blocked" "❌" "$ans"; fi

# N4. verify-isolation.sh passes
if DEPLOY_PROJECT="$PROJECT" bash "$HERE/../verify-isolation.sh" brindlewood priya ada >/tmp/demo-iso.log 2>&1; then
  row "N4. verify-isolation.sh" "✅" "all checks passed"
else row "N4. verify-isolation.sh" "❌" "$(tail -1 /tmp/demo-iso.log)"; fi

echo ""; echo "================= HERMES-TIER DEMO RESULTS ================="
printf "%b" "$ROWS" | awk -F'\t' '{printf "  %s  %-52s %s\n",$1,$2,$3}'
echo "==========================================================="
echo "  PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || { echo "DEMO FAILED"; exit 1; }
echo "DEMO PASSED"
