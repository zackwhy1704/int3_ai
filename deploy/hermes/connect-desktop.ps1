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

  MSIX AppData VIRTUALIZATION (official Windows app is an MSIX package)
  --------------------------------------------------------------------
  The official Windows Hermes app ships as MSIX (Store / MSIX / installer;
  apps/desktop/electron-builder.config.cjs win.target = ['msix']). For a packaged
  app, Windows redirects %APPDATA% (Roaming): "the OS will open the file from the
  per-user, per-package location first. If that location doesn't exist, then the OS
  will attempt to open the file from the real AppData location."
  (learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes).
  So writing only the real %APPDATA%\Hermes works on first launch but is SHADOWED
  once the app writes its own connections file into its private store. This script
  therefore, for a packaged install, writes BOTH the private per-package Roaming
  copy (%LOCALAPPDATA%\Packages\<PackageFamilyName>\LocalCache\Roaming\<app>) AND the
  real %APPDATA%\<app>, and prints which paths it used. It resolves the package
  family name at RUNTIME via Get-AppxPackage (no hard-coded publisher hash), so it
  works for whichever MSIX variant is installed. A plain (non-packaged) install just
  uses %APPDATA%\Hermes as before.

.PARAMETER Tenant
  Tenant id, e.g. brindlewood.

.PARAMETER User
  User id, e.g. priya | ada | marcus. Must have been provisioned (a connect file
  must exist at deploy/hermes/connect/<tenant>-<user>.connect.json).

.PARAMETER UserDataDir
  Override: write the registry to exactly this Hermes userData directory and nothing
  else (skips packaged/MSIX detection). Use it for a from-source dev run — pass the
  directory you will also set as HERMES_DESKTOP_USER_DATA_DIR when launching.

.PARAMETER ExpectedVersion
  The pinned desktop version the demo is validated against (default 0.21.7). If a
  packaged install is found whose version does not match, the script WARNS.

.EXAMPLE
  .\connect-desktop.ps1 brindlewood priya        # make priya the active connection
  .\connect-desktop.ps1 brindlewood ada          # one command: switch to ada
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)] [string] $Tenant,
  [Parameter(Mandatory = $true, Position = 1)] [string] $User,
  [string] $UserDataDir,
  [string] $ExpectedVersion = '0.21.7'
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

# --- find a packaged (MSIX/Store) Hermes install, if any -----------------------
# Identity names come from apps/desktop/product-identity.cjs: out-of-store MSIX
# 'NousResearch.Hermes[Bundled|Light]', Store 'NousResearchInc.HermesAgent',
# publisher 'Nous Research Inc.' (OUT_OF_STORE_PUBLISHER / Store publisherDisplayName).
function Find-HermesPackage {
  try {
    return Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {
      ($_.Name -like 'NousResearch.Hermes*') -or ($_.Name -eq 'NousResearchInc.HermesAgent') -or
      (($_.Name -like '*Hermes*') -and ($_.Publisher -like '*Nous Research*'))
    } | Select-Object -First 1
  } catch { return $null }   # Appx module unavailable (e.g. non-Windows PS) -> treat as plain
}

# --- resolve the userData directory(ies) to write ------------------------------
# $targets is ORDERED; $targets[0] is the primary (what the running app reads).
$targets = @()
$pkg = $null
$usingExplicit = $PSBoundParameters.ContainsKey('UserDataDir') -and $UserDataDir
if ($usingExplicit) {
  $targets = @($UserDataDir)
} else {
  $pkg = Find-HermesPackage
  if ($pkg) {
    $privRoot = Join-Path $env:LOCALAPPDATA ("Packages\{0}\LocalCache\Roaming" -f $pkg.PackageFamilyName)
    $realRoot = $env:APPDATA
    # Electron userData folder = the app's productName; depends on the variant
    # (Hermes / Hermes Agent / Hermes Light). Pick the one that already exists
    # (private first, then real), else default to 'Hermes'.
    $udir = 'Hermes'
    foreach ($c in @('Hermes', 'Hermes Agent', 'Hermes Light')) {
      if (Test-Path (Join-Path $privRoot $c)) { $udir = $c; break }
    }
    if ($udir -eq 'Hermes') {
      foreach ($c in @('Hermes Agent', 'Hermes Light')) {
        if (Test-Path (Join-Path $realRoot $c)) { $udir = $c; break }
      }
    }
    $privTarget = Join-Path $privRoot $udir
    $realTarget = Join-Path $realRoot $udir
    # Primary = the private copy if it already holds a registry (that is what the app
    # reads); otherwise the real path (read on first launch). Write BOTH either way:
    # pre-creating the private copy means the app reads our connection from launch 1,
    # and keeping the real copy covers a first run before any private copy exists.
    if (Test-Path (Join-Path $privTarget 'connections.json')) {
      $targets = @($privTarget, $realTarget)
    } else {
      $targets = @($realTarget, $privTarget)
    }
  } else {
    $targets = @(Join-Path $env:APPDATA 'Hermes')   # plain (non-packaged) install
  }
}

# --- build the registry from the PRIMARY target (preserve local + other users) -
$primary = $targets[0]
$primaryRegistry = Join-Path $primary 'connections.json'
$connections = New-Object System.Collections.ArrayList
$haveLocal = $false
if (Test-Path $primaryRegistry) {
  try {
    $existing = Get-Content -Raw -Path $primaryRegistry | ConvertFrom-Json
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
  version     = 2
  primary     = $connId      # make THIS user the active connection
  launchMode  = 'primary'
  lastUsed    = $connId
  connections = $connections
}
$json = $registry | ConvertTo-Json -Depth 8   # UTF-8 WITHOUT BOM below (Node JSON.parse rejects a BOM)

# --- write to every target -----------------------------------------------------
$written = @()
foreach ($t in $targets) {
  try {
    if (-not (Test-Path $t)) { New-Item -ItemType Directory -Force -Path $t | Out-Null }
    [System.IO.File]::WriteAllText((Join-Path $t 'connections.json'), $json, (New-Object System.Text.UTF8Encoding $false))
    $written += (Join-Path $t 'connections.json')
  } catch {
    Write-Warning ("Could not write {0}: {1}" -f (Join-Path $t 'connections.json'), $_.Exception.Message)
  }
}
if ($written.Count -eq 0) { Write-Error "Wrote no connections.json (all targets failed)."; exit 1 }

Write-Host ""
Write-Host "######################################################################" -ForegroundColor Yellow
Write-Host "# WARNING: the session token was written in PLAINTEXT to:" -ForegroundColor Yellow
foreach ($w in $written) { Write-Host ("#   {0}" -f $w) -ForegroundColor Yellow }
Write-Host "# This is the keychain-encryption-OFF default and is intended for a" -ForegroundColor Yellow
Write-Host "# LOCAL, SYNTHETIC-DATA demo on this machine ONLY. Do not use for real" -ForegroundColor Yellow
Write-Host "# tokens or on a shared machine. Delete these files to remove the token." -ForegroundColor Yellow
Write-Host "######################################################################" -ForegroundColor Yellow
Write-Host ""
Write-Host ("Active Hermes Desktop connection is now: {0}" -f $label) -ForegroundColor Green
Write-Host ("  url   : {0}" -f $connect.url)
if ($pkg) {
  Write-Host ("  app   : packaged MSIX {0} (family {1})" -f $pkg.Name, $pkg.PackageFamilyName)
  Write-Host  "  files : wrote the private per-package copy AND real %APPDATA% (the app reads the private copy first, real as fallback)."
} elseif ($usingExplicit) {
  Write-Host ("  files : explicit userData dir {0}" -f $UserDataDir)
} else {
  Write-Host  "  files : plain install (%APPDATA%\Hermes); no packaged MSIX install detected."
}
Write-Host ""

# --- version compatibility check (item 2: warn on desktop drift off v0.21.7) ----
if ($pkg) {
  if ([string]$pkg.Version -like ("{0}.*" -f $ExpectedVersion) -or [string]$pkg.Version -eq $ExpectedVersion) {
    Write-Host ("Desktop version check: installed {0} matches pinned v{1}. OK." -f $pkg.Version, $ExpectedVersion) -ForegroundColor Green
  } else {
    Write-Warning ("Desktop version DRIFT: installed {0} ({1}) is not the pinned v{2} this demo was validated on. " -f $pkg.Version, $pkg.Name, $ExpectedVersion +
                   "Note: Microsoft Store builds use a timestamp-based version quad, so a numeric mismatch may be a Store build rather than a wrong version. " +
                   "See DEMO.md 'Keeping the desktop on v0.21.7'.")
  }
} else {
  Write-Host ("Desktop version check: no packaged install found, so the version is not determinable here. This demo is validated on v{0}." -f $ExpectedVersion) -ForegroundColor DarkYellow
}
Write-Host ""

# --- launch guidance ------------------------------------------------------------
if ($usingExplicit) {
  Write-Host "This is a NON-DEFAULT userData dir. Launch Hermes Desktop with:"
  Write-Host ("  `$env:HERMES_DESKTOP_USER_DATA_DIR = '{0}'" -f $UserDataDir) -ForegroundColor Cyan
  Write-Host '  # then start Hermes Desktop (packaged exe, or "npm start" in apps/desktop for a dev run)'
} else {
  Write-Host "Launch Hermes Desktop normally; it opens this connection. If it is already"
  Write-Host "open, use its in-app connection switcher or restart it. If you switched users"
  Write-Host "AFTER the app had written its own connection file, just re-run this command."
}
