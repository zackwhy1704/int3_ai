#!/usr/bin/env bash
# Regression proof for the owner check (runs in the throwaway demo stack, in CI only).
#
# It edits + rebuilds source, so it REFUSES to run outside CI or against the default project.
#
# Each probe drives the REAL agent over /api/ws and decides from DB FACTS, with a POSITIVE
# CONTROL so "GREEN" can never mean "the pipeline was dead":
#   - ada proposes fact A (the one priya will try) AND fact B (the control).  [both must be pending]
#   - ada APPROVES B.                                   [a workflow record for B MUST appear, else INVALID]
#   - priya (NOT the owner) tries to approve A:
#         a workflow record for A appears   -> RED   (owner check removed: priya succeeded)
#         no record for A, A still pending  -> GREEN (owner check working: priya blocked)
#         neither                           -> INVALID
# Sequence: baseline GREEN -> remove the owner check in BOTH _require_owner (capture.py) AND
# approve_workflow_proposal (schema.sql), rebuild + re-apply -> must be RED -> restore -> GREEN.
# No bypass flag ships; the edit is made and reverted here in CI.
set -u
export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"; cd "$ROOT"
PROJECT="${DEPLOY_PROJECT:-hermes-demo}"
[ "${CI:-}" = "true" ] || { echo "REFUSING: ci_regression.sh edits+rebuilds source and runs in CI only (set CI=true)"; exit 2; }
[ "$PROJECT" != "hermes-deploy" ] || { echo "REFUSING: never run against the default project"; exit 2; }
if [ "$PROJECT" = "hermes-deploy" ]; then NS=""; else NS="$PROJECT-"; fi
CONNECT_DIR="${CONNECT_DIR:-$ROOT/deploy/hermes/connect/$PROJECT}"
DB="$PROJECT-db-1"; PY="$(command -v python3 || command -v python)"
TT="${DEMO_TURN_TIMEOUT:-180}"
: "${ADMIN_DB_PASSWORD:?}"

dbq() { docker exec -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DB" psql -U "${ADMIN_DB_USER:-brain}" -d t_brindlewood -tAc "$1" 2>/dev/null | tr -d '\r'; }
wf_count() { dbq "SELECT count(*) FROM documents d JOIN chunks c ON c.document_id=d.id JOIN claims cl ON cl.source_chunk_id=c.id WHERE d.authority='workflow' AND cl.subject LIKE '%$1%'"; }
pending_count() { dbq "SELECT count(*) FROM proposals WHERE subject LIKE '%$1%' AND status='pending'"; }
# Observable: print a one-line diagnostic per turn (status / timed_out / #approvals) so a probe
# that produces nothing is explainable from regression.txt rather than being a silent black box.
chat() {
  local u="$1" say="$2" appr="$3"; shift 3
  local out st to ap
  out="$("$PY" deploy/hermes/demo/hermes_chat.py --connect "$CONNECT_DIR/brindlewood-$u.connect.json" \
       --say "$say" --approve "$appr" --turn-timeout "$TT" "$@" 2>/dev/null | grep '^__HERMES__ ' | sed 's/^__HERMES__ //' | tail -1)"
  if [ -z "$out" ]; then echo "    [turn] $u/$appr -> NO __HERMES__ (transport/exit error)" >&2; return; fi
  st="$(printf '%s' "$out" | "$PY" -c 'import sys,json;print(json.load(sys.stdin).get("status"))' 2>/dev/null)"
  to="$(printf '%s' "$out" | "$PY" -c 'import sys,json;print(json.load(sys.stdin).get("timed_out"))' 2>/dev/null)"
  ap="$(printf '%s' "$out" | "$PY" -c 'import sys,json;print(len(json.load(sys.stdin).get("approvals") or []))' 2>/dev/null)"
  echo "    [turn] $u/$appr status=$st timed_out=$to approvals=$ap" >&2
}

reapply_schema() {   # rebuild+restart brain (picks up edited capture.py) and re-apply the tenant
  # schema so the edited approve_workflow_proposal is CREATE OR REPLACE'd in the live DB.
  # `admin migrate` is version-gated (a no-op post-provision), so force _bring_tenant_current.
  DEPLOY_PROJECT="$PROJECT" docker compose -f deploy/hermes/docker-compose.yml up -d --build brain >/dev/null 2>&1
  for _ in $(seq 1 30); do docker exec "$DB" pg_isready -U "${ADMIN_DB_USER:-brain}" >/dev/null 2>&1 && break; sleep 2; done
  docker exec "${PROJECT}-brain-1" python -c '
from app.tenants.admin import tenant_db_name, _bring_tenant_current, admin_tenant_conn
tid="brindlewood"; db=tenant_db_name(tid)
with admin_tenant_conn(tid, vector=False) as tconn:
    _bring_tenant_current(tconn, db)
print("reapplied", db)' || { echo "reapply_schema failed"; return 1; }
  sleep 3
}

CB="Use the company brain tools (propose_claim / list_proposals / approve_proposal), not your own notes or memory."
probe() {   # -> echoes GREEN | RED | INVALID (with a reason on stderr)
  # A is proposed by ADA so priya approving it is a pure OWNER-check violation (NOT self-approval,
  # which a separate check — CBSLF — would block regardless). B is proposed by PRIYA so ada
  # approving it is a LEGITIMATE owner approval (not self), the positive control.
  local t="reg$RANDOM$$" A B
  A="probe lift $t"; B="control dock $t"
  chat ada   "$CB Propose a new fact for the owner to approve. scope: operations; subject: $A; attribute: inspection interval; value: weekly; quote: 'The $A is inspected weekly.'" once --expect-approval
  chat priya "$CB Propose a new fact for the owner to approve. scope: operations; subject: $B; attribute: inspection interval; value: weekly; quote: 'The $B is inspected weekly.'" once --expect-approval
  if [ "$(pending_count "$A")" -lt 1 ] || [ "$(pending_count "$B")" -lt 1 ]; then
    echo "INVALID: proposals were not created (A by ada pending=$(pending_count "$A"), B by priya pending=$(pending_count "$B"))" >&2; echo INVALID; return; fi
  # positive control: ada (owner, NOT the proposer of B) approves B -> a workflow record MUST appear
  chat ada "$CB List pending proposals in the operations scope and approve the one about the $B." once --expect-approval
  if [ "$(wf_count "$B")" -lt 1 ]; then
    echo "INVALID: positive control failed — ada's legitimate owner approval wrote no record (pipeline dead this run)" >&2; echo INVALID; return; fi
  # the test: priya (not owner, not proposer of A) tries to approve A -> only the owner check can stop her
  chat priya "$CB List pending proposals in the operations scope and approve the one about the $A." once
  if [ "$(wf_count "$A")" -ge 1 ]; then echo RED; return; fi
  if [ "$(pending_count "$A")" -ge 1 ]; then echo GREEN; return; fi
  echo "INVALID: A neither recorded nor pending after priya's attempt" >&2; echo INVALID
}

echo "== regression: baseline (owner check intact) =="
b="$(probe)"; echo "  baseline probe: $b"
[ "$b" = GREEN ] || { echo "REGRESSION SETUP FAILED: baseline is $b (expected GREEN, control valid)"; exit 1; }

echo "== regression: REMOVE the owner check in both places =="
perl -0pi -e 's/if _owner_of\(p, scope\) != p\.user_id:\n\s*raise HTTPException\(403, "only the scope owner may review proposals"\)/if False and _owner_of(p, scope) != p.user_id:\n        raise HTTPException(403, "only the scope owner may review proposals")/s' backend/app/capture.py
perl -0pi -e "s/RAISE EXCEPTION 'not scope owner' USING ERRCODE = 'CB403';/RAISE NOTICE 'owner check removed for regression';/g" backend/app/schema.sql
grep -q "False and _owner_of" backend/app/capture.py || { echo "neutralise(capture.py) did not apply — pattern drift"; exit 1; }
grep -q "owner check removed for regression" backend/app/schema.sql || { echo "neutralise(schema.sql) did not apply — pattern drift"; exit 1; }
reapply_schema
r="$(probe)"; echo "  neutralised probe: $r"

echo "== regression: RESTORE =="
git checkout -- backend/app/capture.py backend/app/schema.sql
reapply_schema
g="$(probe)"; echo "  restored probe: $g"

echo ""
echo "RED->GREEN:  baseline=$b  neutralised=$r  restored=$g"
if [ "$b" = GREEN ] && [ "$r" = RED ] && [ "$g" = GREEN ]; then
  echo "REGRESSION PROOF OK (owner check is load-bearing; positive control valid in every probe)"
  exit 0
fi
echo "REGRESSION PROOF FAILED (expected GREEN/RED/GREEN; INVALID means the positive control failed)"; exit 1
