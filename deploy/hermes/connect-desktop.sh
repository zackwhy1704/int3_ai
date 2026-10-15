#!/usr/bin/env bash
# connect-desktop.sh — macOS counterpart of connect-desktop.ps1.
#
# Point stock Hermes Desktop at ONE provisioned user's gated backend by writing the
# desktop connection registry (connections.json) and making it the active (primary)
# connection. Re-run for a different user to switch in one command. Same behaviour and
# same v2 registry shape as the PowerShell version; this is the LOCAL, SYNTHETIC-DATA
# demo path only (it writes the session token in PLAINTEXT, the keychain-OFF default:
# secret-storage-policy.ts, main.ts decryptDesktopSecret, connection-registry.ts).
#
# macOS has no MSIX AppData virtualization, so there is ONE userData dir:
#   ~/Library/Application Support/<productName>
# productName is the app's Electron productName = displayName from
# apps/desktop/product-identity.cjs ('Hermes' default; 'Hermes Light' the remote-only
# variant). We pick whichever folder already exists, else default to 'Hermes'.
#
# Usage:
#   ./connect-desktop.sh --tenant brindlewood --user priya
#   ./connect-desktop.sh --tenant brindlewood --user ada            # switch to ada
#   ./connect-desktop.sh -t brindlewood -u priya --user-data-dir /path  # explicit dir
#   [--expected-version 0.21.7]  warn if an installed Hermes.app is not this version
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"

TENANT=""; USER_ID=""; USERDATA=""; EXPECTED="0.21.7"
while [ $# -gt 0 ]; do
  case "$1" in
    -t|--tenant) TENANT="$2"; shift 2;;
    -u|--user) USER_ID="$2"; shift 2;;
    --user-data-dir) USERDATA="$2"; shift 2;;
    --expected-version) EXPECTED="$2"; shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$TENANT" ] && [ -n "$USER_ID" ] || { echo "usage: $0 --tenant <t> --user <u> [--user-data-dir DIR] [--expected-version X]" >&2; exit 2; }

connect_file="$HERE/connect/$TENANT-$USER_ID.connect.json"
# demo stacks write under connect/<project>/; fall back to those if the flat path is absent.
if [ ! -f "$connect_file" ]; then
  for alt in "$HERE"/connect/*/"$TENANT-$USER_ID.connect.json"; do
    [ -f "$alt" ] && { connect_file="$alt"; break; }
  done
fi
[ -f "$connect_file" ] || { echo "No connect file for $TENANT-$USER_ID (looked at $HERE/connect[/<project>]). Provision the user first: ./provision-user.sh $TENANT $USER_ID <port>" >&2; exit 1; }

# --- resolve the userData dir ---------------------------------------------------
if [ -n "$USERDATA" ]; then
  target="$USERDATA"
else
  appsup="$HOME/Library/Application Support"
  target="$appsup/Hermes"
  for c in "Hermes" "Hermes Agent" "Hermes Light"; do
    if [ -d "$appsup/$c" ]; then target="$appsup/$c"; break; fi
  done
fi
registry="$target/connections.json"

# --- merge: keep the 'local' device + other users, replace THIS user, make it primary ---
CONN_ID="cb-$TENANT-$USER_ID" LABEL="Company Brain - $USER_ID@$TENANT" \
REGISTRY="$registry" CONNECT_FILE="$connect_file" \
"$PY" - <<'PY'
import json, os
conn_id = os.environ["CONN_ID"]; label = os.environ["LABEL"]
reg_path = os.environ["REGISTRY"]; connect = json.load(open(os.environ["CONNECT_FILE"]))
url, token = connect.get("url"), connect.get("session_token")
if not url or not token:
    raise SystemExit("Connect file is missing url or session_token.")
remote = {"id": conn_id, "kind": "remote", "label": label, "url": str(url),
          "authMode": "token", "token": {"encoding": "plain", "value": str(token)}}
conns, have_local = [], False
if os.path.exists(reg_path):
    try:
        existing = json.load(open(reg_path))
        for c in (existing.get("connections") or []):
            if not c: continue
            if c.get("kind") == "local": conns.append(c); have_local = True; continue
            if c.get("id") == conn_id: continue          # drop stale copy of this user
            conns.append(c)                               # keep other users
    except Exception:
        print("WARNING: existing connections.json was unreadable; starting fresh.")
if not have_local:
    conns.insert(0, {"id": "local", "kind": "local", "label": "This device"})
conns.append(remote)
registry = {"version": 2, "primary": conn_id, "launchMode": "primary",
            "lastUsed": conn_id, "connections": conns}
os.makedirs(os.path.dirname(reg_path), exist_ok=True)
with open(reg_path, "w", encoding="utf-8") as f:   # no BOM (Node JSON.parse rejects one)
    json.dump(registry, f, indent=2)
PY

cat <<EOF

######################################################################
# WARNING: the session token was written in PLAINTEXT to:
#   $registry
# This is the keychain-encryption-OFF default and is intended for a
# LOCAL, SYNTHETIC-DATA demo on this machine ONLY. Delete this file to
# remove the token.
######################################################################

Active Hermes Desktop connection is now: Company Brain - $USER_ID@$TENANT
  url   : $(sed -n 's/.*"url"[^"]*"\([^"]*\)".*/\1/p' "$connect_file" | head -1)
  files : $registry
EOF

# --- version check (best-effort): installed Hermes.app CFBundleShortVersionString ---
plist="/Applications/Hermes.app/Contents/Info.plist"
if [ -f "$plist" ] && command -v defaults >/dev/null 2>&1; then
  ver="$(defaults read "/Applications/Hermes.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo '')"
  if [ -n "$ver" ]; then
    case "$ver" in
      "$EXPECTED"|"$EXPECTED".*) echo "Desktop version check: installed $ver matches pinned v$EXPECTED. OK.";;
      *) echo "WARNING: desktop version DRIFT: installed $ver is not the pinned v$EXPECTED this demo was validated on (see DEMO.md).";;
    esac
  fi
else
  echo "Desktop version check: no /Applications/Hermes.app found; this demo is validated on v$EXPECTED."
fi

echo ""
if [ -n "$USERDATA" ]; then
  echo "This is a NON-DEFAULT userData dir. Launch Hermes Desktop with:"
  echo "  HERMES_DESKTOP_USER_DATA_DIR='$USERDATA' open -a Hermes   # or 'npm start' in apps/desktop for a dev run"
else
  echo "Launch Hermes Desktop normally; it opens this connection. If it is already open,"
  echo "use its in-app connection switcher or restart it. If you switched users AFTER the"
  echo "app had written its own connection file, just re-run this command."
fi
