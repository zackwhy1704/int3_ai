#!/usr/bin/env bash
# Tests for startup.sh's first-boot pin (int3-sha metadata). Runs the REAL startup.sh against a
# throwaway "origin" under a temp dir, with the metadata server (curl), logger, docker, systemctl and
# install stubbed on PATH (no root, no network, nothing outside the temp dir).
# Run from anywhere:  bash deploy/gcp/vm/test_startup.sh
set -uo pipefail
unset MSYS_NO_PATHCONV  # Git Bash on Windows: let it convert the temp paths for git.exe (no-op elsewhere)
HERE="$(cd "$(dirname "$0")" && pwd)"; GCP_SRC="$(cd "$HERE/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail=0; pass=0
ok()  { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }

# --- origin: two commits on the branch, one on another branch ---------------------------------
. "$GCP_SRC/config.env"   # INT3_REF
G="git -c core.autocrlf=false -c user.email=t@example.invalid -c user.name=test -c init.defaultBranch=main -c commit.gpgsign=false"
$G init -q "$T/origin"
mkdir -p "$T/origin/deploy/gcp/systemd"
cp "$GCP_SRC"/systemd/*.service "$GCP_SRC"/systemd/*.timer "$T/origin/deploy/gcp/systemd/"
( cd "$T/origin" && $G checkout -q -b "$INT3_REF" && $G add -A && $G commit -qm one )
OLD="$(git -C "$T/origin" rev-parse HEAD)"
( cd "$T/origin" && echo two > two.txt && $G add two.txt && $G commit -qm two )
HEAD_SHA="$(git -C "$T/origin" rev-parse HEAD)"
# Kept as a branch, so the clone HAS this commit: only the "is it on INT3_REF" check can refuse it.
( cd "$T/origin" && $G checkout -q -b elsewhere "$OLD" && echo x > x.txt && $G add x.txt && $G commit -qm off \
  && $G checkout -q "$INT3_REF" )
OFF="$(git -C "$T/origin" rev-parse elsewhere)"

# --- stubs: metadata attributes are files in $T/md; everything else logs what would have run -------
mkdir -p "$T/bin" "$T/md"; LOG="$T/calls.log"; SYSLOG="$T/syslog"
cat > "$T/bin/curl" <<EOF
#!/bin/sh
for a in "\$@"; do url="\$a"; done
f="$T/md/\${url##*/}"
[ -f "\$f" ] || exit 22
cat "\$f"
EOF
printf '#!/bin/sh\ncat >> "%s"\n' "$SYSLOG" > "$T/bin/logger"
printf '#!/bin/sh\nexit 0\n' > "$T/bin/docker"
printf '#!/bin/sh\necho "systemctl $*" >> "%s"\n' "$LOG" > "$T/bin/systemctl"
printf '#!/bin/sh\necho "install $*" >> "%s"\n' "$LOG" > "$T/bin/install"
chmod +x "$T/bin/"*
printf '%s' "$T/origin" > "$T/md/int3-repo"
printf '%s' "$INT3_REF" > "$T/md/int3-ref"

n=0; c=; VM=
boot() {  # boot <vm-dir>: startup.sh runs on that VM; its exit code is left in $c
  : > "$LOG"; : > "$SYSLOG"
  ( cd /; INT3_DIR="$1/int3" PATH="$T/bin:$PATH" bash "$HERE/startup.sh" ); c=$?
}
run() {  # run [<int3-sha>]: a fresh VM (no checkout yet) boots with that metadata (none if no arg)
  n=$((n+1)); VM="$T/vm$n"; mkdir -p "$VM"
  if [ "$#" -eq 1 ]; then printf '%s' "$1" > "$T/md/int3-sha"; else rm -f "$T/md/int3-sha"; fi
  boot "$VM"
}
said() {  # said <regex>: the boot log (written by the logger stub, possibly a moment late) has it
  for _ in 1 2 3 4 5 6 7 8 9 10; do grep -q -- "$1" "$SYSLOG" && return 0; sleep 0.2; done; return 1
}
at() { git -C "$VM/int3" rev-parse HEAD 2>/dev/null; }
nothing() { [ ! -e "$VM/int3" ] && [ -z "$(ls -A "$VM")" ] && [ ! -s "$LOG" ]; }  # no checkout, no temp clone, no units

run;                    [ "$c" = 1 ] && nothing && said "int3-sha is missing" && ok "no int3-sha metadata -> refused, nothing deployed" || bad "missing (exit $c)"
run "";                 [ "$c" = 1 ] && nothing && said "int3-sha is missing" && ok "empty int3-sha -> refused" || bad "empty (exit $c)"
run "$INT3_REF";        [ "$c" = 1 ] && nothing && said "is not a commit SHA" && ok "branch name -> refused, nothing deployed" || bad "branch name (exit $c)"
run "${HEAD_SHA^^}";    [ "$c" = 1 ] && nothing && ok "uppercase SHA -> refused" || bad "uppercase (exit $c)"
run "deadbeefdeadbeef"; [ "$c" = 1 ] && nothing && said "not found in" && ok "unknown SHA -> refused, no clone left behind" || bad "unknown (exit $c)"
run "$OFF";             [ "$c" = 1 ] && nothing && said "is not on $INT3_REF" && ok "commit not on INT3_REF -> refused, no clone left behind" || bad "off-branch (exit $c: $(cat "$SYSLOG"))"
run "${OLD:0:7}";       [ "$c" = 0 ] && [ "$(at)" = "$OLD" ] && grep -q "systemctl start --no-block int3-stack.service" "$LOG" \
                          && said "cloned .* at ${OLD:0:7}" && ok "older commit on INT3_REF -> exactly that commit (not the head), stack started" || bad "pinned old (exit $c: $(cat "$SYSLOG"))"
first_vm="$VM"
run "$HEAD_SHA";        [ "$c" = 0 ] && [ "$(at)" = "$HEAD_SHA" ] && ok "full SHA of the head -> checked out" || bad "head (exit $c)"
# A later boot of an existing VM: the checkout is never touched, whatever int3-sha says now.
VM="$first_vm"; boot "$VM"
[ "$c" = 0 ] && [ "$(at)" = "$OLD" ] && grep -q "systemctl start --no-block int3-stack.service" "$LOG" \
  && ok "reboot of an existing VM -> checkout unchanged, stack started" || bad "reboot (exit $c)"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
