<#
.SYNOPSIS
  Starts (or checks) the Spinco TA Audit central backend from an external settings file - nothing is hard-coded in the application.

.DESCRIPTION
  -EnvFile   a text file of KEY=VALUE lines (blank lines and lines starting with # are ignored; optional surrounding quotes are removed).
             Only SPINCO_* settings are accepted; anything else in the file is ignored with a warning, so the file cannot change PATH or other system variables.
             Keep the file OUTSIDE the application folder and restrict its NTFS permissions to the backend's service account. Never commit it.
  -CheckOnly runs tools\check_central_config.py (read-only: starts nothing, creates nothing, makes no Google call, prints no secret) and exits.
  -Connect   with -CheckOnly: also opens one database connection and checks the schema.
  Without -CheckOnly the check runs first; the backend is started only if it reports no FAIL. The backend then runs in the foreground (Ctrl+C stops it) - a service
  wrapper approved by the company can run exactly this command.

.EXAMPLE
  .\tools\Start-SpincoBackend.ps1 -EnvFile C:\ProgramData\Spinco\config\spinco-backend.env -CheckOnly -Connect
  .\tools\Start-SpincoBackend.ps1 -EnvFile C:\ProgramData\Spinco\config\spinco-backend.env
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [switch]$CheckOnly,
    [switch]$Connect,
    [string]$Python
)
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $backend ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Python)) { Write-Host "ERROR: Python not found at $Python (create the virtual environment first)." -ForegroundColor Red; exit 1 }
if (-not (Test-Path -LiteralPath $EnvFile)) { Write-Host "ERROR: settings file not found: $EnvFile" -ForegroundColor Red; exit 1 }

# Load the settings into THIS process only (never into the registry, never written anywhere).
$loaded = 0
foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
    $trimmed = $line.Trim().TrimStart([char]0xFEFF)
    if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
    $eq = $trimmed.IndexOf("=")
    if ($eq -lt 1) { Write-Host "WARN: ignored a line that is not KEY=VALUE." -ForegroundColor Yellow; continue }
    $key = $trimmed.Substring(0, $eq).Trim()
    $value = $trimmed.Substring($eq + 1).Trim()
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) { $value = $value.Substring(1, $value.Length - 2) }
    if ($key -notmatch '^SPINCO_[A-Z0-9_]+$') { Write-Host "WARN: ignored '$key' (only SPINCO_* settings are accepted)." -ForegroundColor Yellow; continue }
    Set-Item -Path ("Env:" + $key) -Value $value
    $loaded++
}
Write-Host "Loaded $loaded setting(s) from the settings file (values are not shown)."

Set-Location $backend
$arguments = @("tools\check_central_config.py")
if ($Connect) { $arguments += "--connect" }
& $Python @arguments
$checkExit = $LASTEXITCODE
if ($CheckOnly) { exit $checkExit }
if ($checkExit -ne 0) { Write-Host "`nNot starting: the configuration check reported FAIL item(s)." -ForegroundColor Red; exit $checkExit }

Write-Host "`nStarting the Spinco backend (Ctrl+C to stop)..."
& $Python run_server.py
exit $LASTEXITCODE
