#requires -version 5.1
<#
.SYNOPSIS
  Unattended packaged-app smoke test for the Company Brain pilot (Windows, NO clicks).

.DESCRIPTION
  Verifies, WITHOUT typing into or clicking the app, that the OFFICIAL installed Hermes
  Desktop connects to the right per-user gated backend and switches cleanly:

    1. Detect the installed official Hermes (MSIX/Appx or a standalone exe); print its
       version and WARN if it is not the pinned v0.21.7.
    2. For marcus: run connect-desktop.ps1, launch the app, and within -WaitSeconds
       confirm on the SERVER side that marcus's backend received a desktop session
       (the reliable signal is `GET /api/ws` appearing in that backend's route-proxy
       log — route_proxy.py records every path; the gate itself never opens /api/ws).
       Screenshot, then quit the app cleanly.
    3. Switch to priya: relaunch and confirm the session now lands on priya's backend
       and NOT marcus's (priya's route log gains a NEW /api/ws, marcus's does not).
       Screenshot, then quit.
    4. Confirm the telemetry banner setting is OFF (the backend's
       telemetry.shared_metrics is enabled=false/decided).

  It NEVER types into or clicks the app. Screenshots are the whole screen (which shows
  the window) via System.Drawing — no fragile per-window P/Invoke. Output: a pass/fail
  table and screenshots saved under a dated folder.

  Reads backends over `docker` on this host (the per-user backends run in the default
  hermes-deploy project). Any check it cannot perform is reported as NOT VERIFIED
  rather than silently passed.

.PARAMETER Tenant        Tenant id (default brindlewood).
.PARAMETER ExpectedVersion  Pinned desktop version to check against (default 0.21.7).
.PARAMETER WaitSeconds   Seconds to wait for the desktop to attach after launch (default 45).
.PARAMETER OutDir        Screenshot/output folder (default: .\smoke-<yyyyMMdd-HHmmss>).
#>
[CmdletBinding()]
param(
  [string] $Tenant = 'brindlewood',
  [string] $ExpectedVersion = '0.21.7',
  [int]    $WaitSeconds = 45,
  [string] $OutDir
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot ("smoke-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss')) }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$script:rows = @()
function Row($name, $ok, $detail) {
  $script:rows += [pscustomobject]@{ Check = $name; Result = $(if ($ok) { 'PASS' } else { 'FAIL' }); Detail = $detail }
}

# --- docker helpers (per-user backends run in the default hermes-deploy project) ---
function Has-Docker { try { docker version --format '{{.Server.Version}}' *> $null; return $true } catch { return $false } }
function Backend($user) { "hermes-$Tenant-$user" }            # NS="" for the default project
function RouteWsCount($user) {
  # count of /api/ws requests seen by this user's backend route proxy
  try { return [int](docker exec (Backend $user) sh -c "grep -c '/api/ws' /tmp/routes.log 2>/dev/null || echo 0") } catch { return -1 }
}
function TelemetryOff($user) {
  try {
    $cfg = docker run --rm -v ("hermes-home-{0}-{1}:/opt/data" -f $Tenant, $user) busybox sh -c "sed -n '/shared_metrics/,/offer_version/p' /opt/data/config.yaml" 2>$null
    return (($cfg -join "`n") -match 'enabled:\s*false') -and (($cfg -join "`n") -match 'send:\s*false')
  } catch { return $null }
}

# --- app detect / launch / quit ----------------------------------------------------
function Find-HermesPackage {
  try {
    return Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {
      ($_.Name -like 'NousResearch.Hermes*') -or ($_.Name -eq 'NousResearchInc.HermesAgent') -or
      (($_.Name -like '*Hermes*') -and ($_.Publisher -like '*Nous Research*'))
    } | Select-Object -First 1
  } catch { return $null }
}
function Launch-Hermes($pkg) {
  if ($pkg) {
    # MSIX: launch via the AppsFolder alias (applicationId = appNamePascal 'Hermes',
    # apps/desktop/product-identity.cjs / electron-builder.config.cjs msix.applicationId).
    $appId = 'Hermes'
    Start-Process ("shell:AppsFolder\{0}!{1}" -f $pkg.PackageFamilyName, $appId) | Out-Null
  } else {
    $exe = Get-ChildItem "$env:LOCALAPPDATA\Programs","$env:ProgramFiles" -Recurse -Filter 'Hermes.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $exe) { throw "No installed Hermes found (no Appx package and no Hermes.exe)." }
    Start-Process $exe.FullName | Out-Null
  }
}
function Quit-Hermes {
  $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'Hermes' }
  foreach ($p in $procs) { try { $null = $p.CloseMainWindow() } catch {} }
  Start-Sleep -Seconds 4
  foreach ($p in (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'Hermes' })) {
    try { $p | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}   # fallback only
  }
}
function Screenshot($file) {
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
  $bmp.Save($file, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
}

Write-Host "== Company Brain packaged-app smoke test ==" -ForegroundColor Cyan
if (-not (Has-Docker)) { Row 'docker reachable (per-user backends)' $false 'docker not reachable'; }

# 1. detect + version
$pkg = Find-HermesPackage
if ($pkg) {
  $okv = ([string]$pkg.Version -like "$ExpectedVersion.*")
  Row "install detected: $($pkg.Name) $($pkg.Version)" $true "MSIX $($pkg.PackageFamilyName)"
  if (-not $okv) { Write-Warning "Installed version $($pkg.Version) is not pinned v$ExpectedVersion (Store builds use a timestamp quad)." }
  Row "version == v$ExpectedVersion" $okv "installed $($pkg.Version)"
} else {
  $exe = Get-ChildItem "$env:LOCALAPPDATA\Programs","$env:ProgramFiles" -Recurse -Filter 'Hermes.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($exe) { Row "install detected: standalone $($exe.FullName)" $true 'standalone'; Row "version == v$ExpectedVersion" $false 'NOT VERIFIED (standalone version not read)' }
  else { Row 'install detected' $false 'no Appx package and no Hermes.exe — install the official app first'; }
}

function Test-User($user, $otherBackendWsBefore) {
  & (Join-Path $PSScriptRoot 'connect-desktop.ps1') $Tenant $user -ExpectedVersion $ExpectedVersion | Out-Null
  $before = RouteWsCount $user
  Launch-Hermes $pkg
  $deadline = (Get-Date).AddSeconds($WaitSeconds); $after = $before
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3; $after = RouteWsCount $user
    if ($after -gt $before) { break }
  }
  $attached = ($after -gt $before)
  Row "$user : desktop attached (/api/ws on $user backend)" $attached "ws $before -> $after"
  $shot = Join-Path $OutDir "$user.png"; try { Screenshot $shot; Row "$user : screenshot" $true $shot } catch { Row "$user : screenshot" $false $_.Exception.Message }
  Quit-Hermes
  return $after
}

# 2. marcus
$priyaWs0 = RouteWsCount 'priya'
$marcusWs = Test-User 'marcus' $priyaWs0

# 3. priya — session must land on priya and NOT marcus
$marcusWsBeforePriya = RouteWsCount 'marcus'
$priyaWs = Test-User 'priya' $marcusWs
$marcusWsAfterPriya = RouteWsCount 'marcus'
Row "priya launch did NOT attach to marcus's backend" ($marcusWsAfterPriya -le $marcusWsBeforePriya) "marcus ws $marcusWsBeforePriya -> $marcusWsAfterPriya"

# 4. telemetry banner off (backend config)
$tel = TelemetryOff 'marcus'
if ($null -eq $tel) { Row 'telemetry (shared_metrics) off in backend config' $false 'NOT VERIFIED' }
else { Row 'telemetry (shared_metrics) off in backend config' $tel 'enabled=false/send=false' }

# --- table ---
Write-Host ""
$script:rows | Format-Table -AutoSize | Out-String | Write-Host
$fail = @($script:rows | Where-Object { $_.Result -eq 'FAIL' }).Count
"Screenshots + this run: $OutDir" | Write-Host
$script:rows | Export-Csv -NoTypeInformation -Path (Join-Path $OutDir 'results.csv')
if ($fail -gt 0) { Write-Host "SMOKE FAILED ($fail failing checks)" -ForegroundColor Red; exit 1 }
Write-Host "SMOKE PASSED" -ForegroundColor Green
