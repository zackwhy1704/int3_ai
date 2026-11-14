#!/usr/bin/env bash
# Tests for update.sh (deploy plan step 0: pinned updates). Runs the REAL update.sh + lib.sh against a
# throwaway "origin" and VM checkout under a temp dir, with systemctl/install stubbed on PATH (no
# root, no network, nothing outside the temp dir). Run from anywhere:  bash deploy/gcp/vm/test_update.sh
set -uo pipefail
unset MSYS_NO_PATHCONV  # Git Bash on Windows: let it convert the temp paths for git.exe (no-op elsewhere)
HERE="$(cd "$(dirname "$0")" && pwd)"; GCP_SRC="$(cd "$HERE/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail=0; pass=0
ok()  { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }

# --- origin: two commits on the branch, one commit off it -------------------------------------
. "$GCP_SRC/config.env"   # INT3_REF
G="git -c core.autocrlf=false -c user.email=t@example.invalid -c user.name=test -c init.defaultBranch=main -c commit.gpgsign=false"
$G init -q "$T/origin"
mkdir -p "$T/origin/deploy/gcp/vm" "$T/origin/deploy/gcp/systemd"
cp "$GCP_SRC/config.env" "$T/origin/deploy/gcp/"
cp "$HERE/update.sh" "$HERE/lib.sh" "$T/origin/deploy/gcp/vm/"
cp "$GCP_SRC"/systemd/*.service "$GCP_SRC"/systemd/*.timer "$T/origin/deploy/gcp/systemd/"
( cd "$T/origin" && $G checkout -q -b "$INT3_REF" && $G add -A && $G commit -qm one )
OLD="$(git -C "$T/origin" rev-parse HEAD)"
( cd "$T/origin" && echo two > two.txt && $G add two.txt && $G commit -qm two )
HEAD_SHA="$(git -C "$T/origin" rev-parse HEAD)"
( cd "$T/origin" && $G checkout -q -b elsewhere "$OLD" && echo x > x.txt && $G add x.txt && $G commit -qm off \
  && $G checkout -q "$INT3_REF" )
OFF="$(git -C "$T/origin" rev-parse elsewhere)"
git -C "$T/origin" update-ref -d refs/heads/elsewhere  # gone from origin, kept in its object store

# --- the VM checkout, at OLD; stubs log what would have run ---------------------------------------
git -c core.autocrlf=false clone -q --branch "$INT3_REF" "$T/origin" "$T/vm"; git -C "$T/vm" checkout -q --detach "$OLD"
mkdir -p "$T/bin"; LOG="$T/calls.log"
printf '#!/bin/sh\necho "systemctl $*" >> "%s"\n' "$LOG" > "$T/bin/systemctl"
printf '#!/bin/sh\necho "install $*" >> "%s"\n' "$LOG" > "$T/bin/install"
chmod +x "$T/bin/systemctl" "$T/bin/install"

run() {  # run <args...>: prints exit code; output in $T/out
  : > "$LOG"
  ( cd /; PATH="$T/bin:$PATH" bash "$T/vm/deploy/gcp/vm/update.sh" "$@" ) > "$T/out" 2>&1; echo $?
}
at() { git -C "$T/vm" rev-parse HEAD; }
unchanged() { [ "$(at)" = "$OLD" ] && [ ! -s "$LOG" ]; }

c=$(run);                         [ "$c" = 2 ] && unchanged && grep -q "usage: update.sh <commit-sha>" "$T/out" && ok "no argument -> usage, exit 2, nothing changed" || bad "no argument (exit $c)"
c=$(run "$HEAD_SHA" "$OLD");      [ "$c" = 2 ] && unchanged && ok "two arguments -> refused" || bad "two arguments (exit $c)"
c=$(run "$INT3_REF");             [ "$c" = 2 ] && unchanged && grep -q "is not a commit SHA" "$T/out" && ok "branch name -> refused, nothing changed" || bad "branch name (exit $c)"
c=$(run "HEAD");                  [ "$c" = 2 ] && unchanged && ok "HEAD -> refused" || bad "HEAD (exit $c)"
c=$(run "${HEAD_SHA^^}");         [ "$c" = 2 ] && unchanged && ok "uppercase SHA -> refused" || bad "uppercase (exit $c)"
c=$(run "abc12");                 [ "$c" = 2 ] && unchanged && ok "5-char SHA -> refused" || bad "short (exit $c)"
c=$(run "deadbeefdeadbeef");      [ "$c" = 1 ] && unchanged && grep -q "not found after fetching" "$T/out" && ok "unknown SHA -> refused, nothing changed" || bad "unknown SHA (exit $c)"
c=$(run "$OFF");                  [ "$c" = 1 ] && unchanged && grep -q "is not on $INT3_REF" "$T/out" && ok "commit not on the branch -> refused, nothing changed" || bad "off-branch (exit $c: $(cat "$T/out"))"
c=$(run "${HEAD_SHA:0:7}");       [ "$c" = 0 ] && [ "$(at)" = "$HEAD_SHA" ] && grep -q "systemctl restart int3-stack.service" "$LOG" \
                                    && grep -q "\[update\] ${OLD:0:7} -> ${HEAD_SHA:0:7}" "$T/out" && ok "short SHA of the branch head -> checked out, stack restarted" || bad "head (exit $c: $(cat "$T/out"))"
c=$(run "$OLD");                  [ "$c" = 0 ] && [ "$(at)" = "$OLD" ] && grep -q "systemctl restart int3-stack.service" "$LOG" && ok "older commit on the branch (rollback) -> checked out" || bad "rollback (exit $c)"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
