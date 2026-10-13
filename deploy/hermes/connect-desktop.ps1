#requires -version 5.1
<#
.SYNOPSIS
  Add/switch a Company Brain user connection in stock Hermes Desktop (local demo).

.DESCRIPTION
  Points Hermes Desktop at ONE provisioned user's gated backend by writing the
  desktop connection registry (connections.json) and setting it as the active
  (primary) connection. Running it again for a different user switches the active
  connection in ONE command.

  SUPPORTED PATHS, and why this uses the fallback
  -----------------------------------------------
  The supported, UI path is Hermes Desktop's in-app "Add Connection" dialog
  (paste the URL + session token). There is no first-party CLI/import for adding a
  connection. The only NON-UI path is to write the connection registry directly,
  including the session token as a plaintext envelope { encoding: 'plain', value }.
  That envelope is exactly what Hermes itself writes and reads when keychain
  encryption is OFF — which is the DEFAULT (opt-in only):
     * secret-storage-policy.ts  — "Setting OFF (default): secrets are written with
       encoding 'plain' and NO safeStorage API is ever called."
     * apps/desktop/electron/main.ts:8567  decryptDesktopSecret(...) returns
       non-safeStorage encodings (i.e. {encoding:'plain'}) verbatim.
     * apps/desktop/electron/connection-registry.ts  RegistryConnection.token is an
       opaque token envelope kept verbatim on load; version 2 registry.
  So this writes a legitimate, Hermes-readable registry — it does NOT patch Hermes.
  It is for THIS LOCAL, SYNTHETIC-DATA demo only (see the warning it prints).

.PARAMETER Tenant
  Tenant id, e.g. brindlewood.

.PARAMETER User
  User id, e.g. priya | ada | marcus. Must have been provisioned (a connect file
  must exist at deploy/hermes/connect/<tenant>-<user>.connect.json).

.PARAMETER UserDataDir
  Hermes Desktop userData directory (where connections.json lives). Default:
  "$env:APPDATA\Hermes" (stock packaged install). For a from-source dev run, pass
  the directory you will also set as HERMES_DESKTOP_USER_DATA_DIR when launching.

.EXAMPLE
  .\connect-desktop.ps1 brindlewood priya        # make priya the active connection
  .\connect-desktop.ps1 brindlewood ada          # one command: switch to ada
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)] [string] $Tenant,
  [Parameter(Mandatory = $true, Position = 1)] [string] $User,
  [string] $UserDataDir = (Join-Path $env:APPDATA 'Hermes')
)

$ErrorActionPreference = 'Stop'

# --- locate the connect file this user was provisioned with --------------------
$connectFile = Join-Path $PSScriptRoot ("connect/{0}-{1}.connect.json" -f $Tenant, $User)
if (-not (Test-Path $connectFile)) {
  Write-Error ("No connect file at {0}. Provision the user first: ./provision-user.sh {1} {2} <port>" -f $connectFile, $Tenant, $User)
  exit 1
}
$connect = Get-Content -Raw -Path $connectFile | ConvertFrom-Json
if (-not $connect.url -or -not $connect.session_token) {
  Write-Error "Connect file is missing url or session_token."
  exit 1
}

# --- the remote connection entry (stock Hermes v2 registry shape) --------------
$connId = "cb-$Tenant-$User"
$label  = "Company Brain - $User@$Tenant"
$remote = [ordered]@{
  id       = $connId
  kind     = 'remote'
  label    = $label
  url      = [string]$connect.url
  authMode = 'token'
  token    = [ordered]@{ encoding = 'plain'; value = [string]$connect.session_token }  # plaintext fallback
}

# --- load existing registry (preserve local + other users), else start fresh ---
$registryPath = Join-Path $UserDataDir 'connections.json'
$connections  = New-Object System.Collections.ArrayList
$haveLocal    = $false
if (Test-Path $registryPath) {
  try {
    $existing = Get-Content -Raw -Path $registryPath | ConvertFrom-Json
    foreach ($c in @($existing.connections)) {
      if ($null -eq $c) { continue }
      if ($c.kind -eq 'local') { [void]$connections.Add($c); $haveLocal = $true; continue }
      if ($c.id -eq $connId)   { continue }   # drop the stale copy of THIS user; re-added below
      [void]$connections.Add($c)              # keep other users' connections
    }
  } catch {
    Write-Warning "Existing connections.json was unreadable; starting a fresh registry."
  }
}
if (-not $haveLocal) {
  [void]$connections.Insert(0, [ordered]@{ id = 'local'; kind = 'local'; label = 'This device' })
}
[void]$connections.Add($remote)

$registry = [ordered]@{
  version    = 2
  primary    = $connId      # make THIS user the active connection
  launchMode = 'primary'
  lastUsed   = $connId
  connections = $connections
}

# --- write connections.json as UTF-8 WITHOUT BOM (Node JSON.parse rejects a BOM)
if (-not (Test-Path $UserDataDir)) { New-Item -ItemType Directory -Force -Path $UserDataDir | Out-Null }
$json = $registry | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($registryPath, $json, (New-Object System.Text.UTF8Encoding $false))

Write-Host ""
Write-Host "######################################################################" -ForegroundColor Yellow
Write-Host "# WARNING: the session token was written in PLAINTEXT to" -ForegroundColor Yellow
Write-Host "#   $registryPath" -ForegroundColor Yellow
Write-Host "# This is the keychain-encryption-OFF default and is intended for a" -ForegroundColor Yellow
Write-Host "# LOCAL, SYNTHETIC-DATA demo on this machine ONLY. Do not use for real" -ForegroundColor Yellow
Write-Host "# tokens or on a shared machine. Delete this file to remove the token." -ForegroundColor Yellow
Write-Host "######################################################################" -ForegroundColor Yellow
Write-Host ""
Write-Host ("Active Hermes Desktop connection is now: {0}" -f $label) -ForegroundColor Green
Write-Host ("  url  : {0}" -f $connect.url)
Write-Host ("  file : {0}" -f $registryPath)
Write-Host ""
$usingStock = ($UserDataDir -eq (Join-Path $env:APPDATA 'Hermes'))
if ($usingStock) {
  Write-Host "Launch stock Hermes Desktop normally; it opens this connection. If it is"
  Write-Host "already open, use its in-app connection switcher or restart it."
} else {
  Write-Host "This is a NON-DEFAULT userData dir. Launch Hermes Desktop with:"
  Write-Host ("  `$env:HERMES_DESKTOP_USER_DATA_DIR = '{0}'" -f $UserDataDir) -ForegroundColor Cyan
  Write-Host '  # then start Hermes Desktop (packaged exe, or "npm start" in apps/desktop for a dev run)'
}
