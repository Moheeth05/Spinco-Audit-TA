<#
.SYNOPSIS
  Adds (or removes) ONE narrow Windows Firewall rule so other laptops on the same Wi-Fi can reach the LAN demo backend. Run on the HOST laptop only.

  The rule is deliberately narrow: inbound TCP, ONE port (the demo port), the PRIVATE network profile only, remote addresses = the local subnet only, and (when given) only
  the backend's python.exe. It does not turn the firewall off, does not touch other rules and does not open anything on the Public or Domain profiles.
  It needs ADMINISTRATOR rights (company policy may forbid it - then ask IT, or use a laptop/network where you are allowed). Use -WhatIf to see exactly what it would do.

.EXAMPLE
  .\Add-DemoFirewallRule.ps1 -WhatIf
  .\Add-DemoFirewallRule.ps1 -Port 47831
  .\Add-DemoFirewallRule.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [int]$Port = 47831,
    [string]$Program = "D:\Bills\backend\.venv\Scripts\python.exe",
    [switch]$Remove
)
$name = "Spinco TA Audit LAN demo (TCP $Port, Private, local subnet)"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host "Network profiles on this laptop:"
Get-NetConnectionProfile | ForEach-Object { Write-Host ("  {0}  [{1}]  {2}" -f $_.Name, $_.NetworkCategory, $_.InterfaceAlias) }
if ((Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq "Public" }).Count -gt 0) {
    Write-Host "  NOTE: a PUBLIC profile never accepts this rule (it is Private-only on purpose). To use the demo, set the Wi-Fi network to Private" -ForegroundColor Yellow
    Write-Host "        (Settings > Network > Wi-Fi > the network > Network profile = Private; may need administrator / company approval)." -ForegroundColor Yellow
}

if ($Remove) {
    if ($PSCmdlet.ShouldProcess($name, "Remove the firewall rule")) {
        if (-not $isAdmin) { Write-Host "Administrator rights are required to remove the rule." -ForegroundColor Red; exit 1 }
        Get-NetFirewallRule -DisplayName "Spinco TA Audit LAN demo*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        Write-Host "Removed."
    }
    exit 0
}

$params = @{ DisplayName = $name; Direction = "Inbound"; Action = "Allow"; Protocol = "TCP"; LocalPort = $Port; Profile = "Private"; RemoteAddress = "LocalSubnet"; Description = "LAN demo of Spinco TA Audit (staging, synthetic data). Remove with Add-DemoFirewallRule.ps1 -Remove." }
if ($Program -and (Test-Path $Program)) { $params["Program"] = $Program }
if ($PSCmdlet.ShouldProcess($name, "Create inbound rule: TCP $Port, Private profile, RemoteAddress LocalSubnet" + $(if ($params.ContainsKey("Program")) { ", program $Program" } else { "" }))) {
    if (-not $isAdmin) { Write-Host "Administrator rights are required. Right-click PowerShell > Run as administrator, or ask IT to create this rule." -ForegroundColor Red; exit 1 }
    if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) { Write-Host "The rule already exists."; exit 0 }
    New-NetFirewallRule @params | Out-Null
    Write-Host "Created: $name"
    Write-Host "Windows Firewall itself was NOT changed or disabled. discovery (mDNS, UDP 5353) uses Windows' built-in rules; if a company policy blocks them, use the manual address in the app."
}
