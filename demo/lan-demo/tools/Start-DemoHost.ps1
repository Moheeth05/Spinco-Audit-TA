<#
.SYNOPSIS
  LAN DEMO HOST (staging only). Run on the ONE designated demo laptop. It starts the central backend on the local network, advertises it so the other laptops find it
  automatically (mDNS), connects it to the STAGING Google Shared Drive, and creates one personal account per participant.

  * Staging credentials are read from D:\SPINCO-SECRETS by THIS backend only. Client laptops never get any Google credential.
  * Plain HTTP on the local network: SYNTHETIC documents only. Not the company server; no company URL is configured anywhere.
  * No firewall change here. Other laptops can connect only if this laptop's Wi-Fi is on the PRIVATE profile and the narrow rule from Add-DemoFirewallRule.ps1 exists
    (administrator approval; see HOST-SETUP.md). This script tells you what it finds.
  * The backend listens on a dedicated demo port (default 47831; next free port is used if it is taken). Never 8001 / 55686 / 58732.

.EXAMPLE
  .\Start-DemoHost.ps1 -Participants "Priya,Arun,Meena,Rahul"
#>
[CmdletBinding()]
param(
    [string]$Participants = "Participant 1,Participant 2,Participant 3,Participant 4",
    [int]$Port = 47831,
    [string]$Resume,   # a run folder of an earlier start: reuse its database, Drive folder, host admin and participants (e.g. after the host laptop was restarted)
    [string]$Backend = "D:\Bills\backend",
    [string]$FrontendDist,
    [string]$Python
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $Python) { $Python = Join-Path $Backend ".venv\Scripts\python.exe" }
if (-not $FrontendDist) { $FrontendDist = Get-ChildItem "D:\SpincoTA-artifacts\lan-demo" -Directory | Where-Object { Test-Path (Join-Path $_.FullName "frontend-dist\index.html") } | Sort-Object Name -Descending | Select-Object -First 1 | ForEach-Object { Join-Path $_.FullName "frontend-dist" } }
foreach ($p in @($Python, (Join-Path $FrontendDist "index.html"), "D:\SPINCO-SECRETS\spinco-drive-oauth-desktop.json", "D:\SPINCO-SECRETS\spinco-drive-oauth-token.json")) { if (-not (Test-Path $p)) { Write-Host "MISSING: $p" -ForegroundColor Red; exit 1 } }

# ---- port: the requested one, or the next free one (never the old test ports) ----
$blocked = 8001, 55686, 58732
for ($i = 0; $i -lt 60; $i++) { $candidate = $Port + $i; if ($blocked -contains $candidate) { continue }; if (-not (Get-NetTCPConnection -State Listen -LocalPort $candidate -ErrorAction SilentlyContinue)) { $Port = $candidate; break } }

# ---- network facts ----
$ips = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -match '^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)' } | Select-Object -ExpandProperty IPAddress)
if ($ips.Count -eq 0) { Write-Host "No local-network IPv4 address found. Connect this laptop to the Wi-Fi first." -ForegroundColor Red; exit 1 }
$profiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue
$public = @($profiles | Where-Object { $_.NetworkCategory -eq "Public" })
$rule = Get-NetFirewallRule -DisplayName "Spinco TA Audit LAN demo*" -ErrorAction SilentlyContinue

Get-ChildItem Env: | Where-Object { $_.Name -like "SPINCO_*" } | ForEach-Object { Remove-Item "Env:$($_.Name)" }
if ($Resume) {
    $run = (Resolve-Path $Resume).Path
    $settings = Get-Content (Join-Path $run "host-settings.json") -Raw | ConvertFrom-Json
    $folderName = $settings.folderName; $folderId = $settings.folderId
    $adminPassword = ((Get-Content (Join-Path $run "host-admin.txt") | Where-Object { $_ -like "password:*" }) -replace '^password:\s*', '').Trim()
    if (-not $settings.port) { Write-Host "host-settings.json is incomplete." -ForegroundColor Red; exit 1 }
    $Port = [int]$settings.port
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { Write-Host "Port $Port is still in use: stop the previous host first." -ForegroundColor Red; exit 1 }
    Write-Host "Resuming $run (same database, same Drive folder, same accounts)."
    Set-Location $Backend
} else {
$run = "D:\SpincoTA-artifacts\central-staging\lan-demo-" + (Get-Date -Format "yyyyMMdd-HHmmss")
New-Item -ItemType Directory -Path $run | Out-Null
Set-Location $Backend

Write-Host "Creating a new demo folder in the STAGING Shared Drive..."
$folder = & $Python (Join-Path $PSScriptRoot "demo_drive_root.py") $Backend
if ($LASTEXITCODE -ne 0 -or -not $folder) { Write-Host "Could not create the demo Drive folder (staging sign-in problem?)." -ForegroundColor Red; exit 1 }
$folderName, $folderId = $folder.Trim().Split(" ")

& $Python tools\init_central_staging_db.py --db "$run\central.db"
if ($LASTEXITCODE -ne 0) { exit 1 }

# ---- host administrator: random password, shown once, NOT given to participants ----
$bytes = New-Object byte[] 18; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$adminPassword = "Host-" + ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', 'x')
Set-Content "$run\host-admin.txt" "admin: intern@spincotech.com`npassword: $adminPassword" -Encoding ascii
@{ folderName = $folderName; folderId = $folderId; port = $Port } | ConvertTo-Json | Set-Content "$run\host-settings.json" -Encoding ascii
}
$hostName = $env:COMPUTERNAME
$env:SPINCO_ENVIRONMENT = "staging"
$env:SPINCO_DB_PATH = "$run\central.db"
$env:SPINCO_STORAGE_ROOT = "$run\storage"
$env:SPINCO_FRONTEND_DIST = $FrontendDist
$env:SPINCO_CENTRAL_STORAGE_MODE = "serve"
$env:SPINCO_GDRIVE_ENABLED = "true"
$env:SPINCO_GDRIVE_AUTH_MODE = "oauth_user"
$env:SPINCO_GDRIVE_OAUTH_CLIENT_FILE = "D:\SPINCO-SECRETS\spinco-drive-oauth-desktop.json"
$env:SPINCO_GDRIVE_OAUTH_TOKEN_FILE = "D:\SPINCO-SECRETS\spinco-drive-oauth-token.json"
$env:SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING = "0AC6JZ6F0PQY-Uk9PVA"
$env:SPINCO_GDRIVE_ROOT_FOLDER_ID = $folderId
$env:SPINCO_GDRIVE_SPOOL_DIR = "$run\spool"
$env:SPINCO_BIND_HOST = "0.0.0.0"
$env:SPINCO_BACKEND_PORT = "$Port"
$env:SPINCO_ALLOWED_HOSTS = (@($hostName, "$hostName.local") + $ips) -join ","
$env:SPINCO_ADMIN_INITIAL_PASSWORD = $adminPassword
$env:SPINCO_ALLOW_INSECURE_HTTP = "true"   # DEMO / synthetic documents only
$env:SPINCO_LOG_DIR = "$run\logs"
$env:TA_BACKEND_URL = "off"

$backendProc = Start-Process -FilePath $Python -ArgumentList "run_server.py" -WorkingDirectory $Backend -PassThru -WindowStyle Hidden -RedirectStandardOutput "$run\backend.out" -RedirectStandardError "$run\backend.err"
$advertiser = $null
try {
    $up = $false
    for ($i = 0; $i -lt 60; $i++) { Start-Sleep 2; try { if ((Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 3).status -eq "ok") { $up = $true; break } } catch { } }
    if (-not $up) { Write-Host "The backend did not start. See $run\backend.err" -ForegroundColor Red; exit 1 }
    $advertiser = Start-Process -FilePath $Python -ArgumentList "`"$here\demo_advertise.py`"", "--port", $Port -PassThru -WindowStyle Hidden -RedirectStandardOutput "$run\advertise.out" -RedirectStandardError "$run\advertise.err"

    if (-not $Resume) { & $Python "$here\create_demo_users.py" --url "http://127.0.0.1:$Port" --admin-email "intern@spincotech.com" --admin-password $adminPassword --names $Participants --out "$run\participants.csv" }

    Write-Host ""
    Write-Host "==================== SPINCO TA AUDIT - LAN DEMO HOST (staging) ====================" -ForegroundColor Cyan
    Write-Host "  Host laptop     $hostName     addresses: $($ips -join ', ')     port: $Port"
    Write-Host "  Manual address  $($ips[0]):$Port        (type this in the app if automatic discovery is blocked)"
    Write-Host "  Drive folder    $folderName   (inside the STAGING Shared Drive)"
    Write-Host "  Host admin      intern@spincotech.com  /  (saved in $run\host-admin.txt - do NOT share)"
    Write-Host "  Participants    $run\participants.csv   (give each person ONLY their own line)"
    if ($public.Count -gt 0) { Write-Host "  WARNING  Wi-Fi network profile is PUBLIC ($($public[0].Name)): other laptops cannot connect until it is Private (needs administrator / company policy). See HOST-SETUP.md." -ForegroundColor Yellow }
    if (-not $rule) { Write-Host "  WARNING  No firewall rule 'Spinco TA Audit LAN demo' found: other laptops will be blocked. Run Add-DemoFirewallRule.ps1 as administrator (narrow: this port, Private profile, local subnet only)." -ForegroundColor Yellow }
    Write-Host "  Stop            Ctrl+C in this window (the backend and the advertisement stop with it)"
    Write-Host "===================================================================================" -ForegroundColor Cyan
    Wait-Process -Id $backendProc.Id
} finally {
    if ($advertiser -and -not $advertiser.HasExited) { Stop-Process -Id $advertiser.Id -Force -ErrorAction SilentlyContinue }
    if ($backendProc -and -not $backendProc.HasExited) { Stop-Process -Id $backendProc.Id -Force -ErrorAction SilentlyContinue }
}
