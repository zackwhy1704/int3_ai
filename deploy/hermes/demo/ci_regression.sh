#!/usr/bin/env bash
# Regression proof for the owner check (run in the throwaway demo stack, in CI).
# Probes N1b end-to-end: ada proposes a fresh operations fact, priya (NOT the owner)
# tries to approve it. Signal is a DB fact — did priya's approve write a workflow record?
#   REFUSED (no record)  = owner check working   = GREEN
#   APPROVED (record)    = owner check removed    = RED
# Sequence: baseline GREEN -> remove the owner check in BOTH _require_owner (capture.py)
# AND approve_workflow_proposal (schema.sql), re-apply, probe -> must be RED -> restore,
# re-apply, probe -> must be GREEN again. No bypass flag ships; we edit+restore source.
set -u
export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"; cd "$ROOT"
PROJECT="${DEPLOY_PROJECT:-hermes-demo}"
if [ "$PROJECT" = "hermes-deploy" ]; then NS=""; else NS="$PROJECT-"; fi
CONNECT_DIR="${CONNECT_DIR:-$ROOT/deploy/hermes/connect/$PROJECT}"
DB="$PROJECT-db-1"; PY="$(command -v python3 || command -v python)"
: "${ADMIN_DB_PASSWORD:?}"
CAP_TXT=""

dbq() { docker exec -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DB" psql -U "${ADMIN_DB_USER:-brain}" -d t_brindlewood -tAc "$1" 2>/dev/null | tr -d '\r'; }
chat() { "$PY" deploy/hermes/demo/hermes_chat.py --connect "$CONNECT_DIR/brindlewood-$1.connect.json" --say "$2" --approve "$3" "${@:4}" >/dev/null 2>&1; }

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

probe() {   # -> echoes GREEN (refused) or RED (approved); uses a fresh tagged fact
  local tag="reg$RANDOM$$" subj
  subj="probe lift $tag"
  chat ada "Remember for operations that the $subj is inspected weekly. Capture it." once --expect-approval
  # priya is NOT the owner: accept any approval elicitation so the owner check actually fires.
  chat priya "Approve the pending $subj operations proposal." once
  local n; n="$(dbq "SELECT count(*) FROM documents d JOIN chunks c ON c.document_id=d.id JOIN claims cl ON cl.source_chunk_id=c.id WHERE d.authority='workflow' AND cl.subject LIKE '%$tag%'")"
  if [ "${n:-0}" -gt 0 ]; then echo RED; else echo GREEN; fi
}

echo "== regression: baseline (owner check intact) =="
b="$(probe)"; echo "  baseline probe: $b"
[ "$b" = GREEN ] || { echo "REGRESSION SETUP FAILED: baseline is not GREEN ($b)"; exit 1; }

echo "== regression: REMOVE the owner check in both places =="
# (a) app layer: _require_owner ownership raise -> no-op
perl -0pi -e 's/if _owner_of\(p, scope\) != p\.user_id:\n\s*raise HTTPException\(403, "only the scope owner may review proposals"\)/if False and _owner_of(p, scope) != p.user_id:\n        raise HTTPException(403, "only the scope owner may review proposals")/s' backend/app/capture.py
# (b) SECURITY DEFINER function: the CB403 owner guard in approve_workflow_proposal
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
  echo "REGRESSION PROOF OK (owner check is load-bearing: removing it lets priya approve; restoring blocks her)"
  exit 0
fi
echo "REGRESSION PROOF FAILED (expected GREEN/RED/GREEN)"; exit 1
