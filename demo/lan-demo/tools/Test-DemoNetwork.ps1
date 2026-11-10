<#
.SYNOPSIS
  Run on a PARTICIPANT laptop (no administrator needed, changes nothing). Finds out why the demo host is or is not reachable:
  Wi-Fi network and profile, whether the host is found by mDNS, and - if you give the host address - whether its port answers
  and whether the laptop can even reach the host (client isolation / blocked peer-to-peer).

.EXAMPLE
  .\Test-DemoNetwork.ps1
  .\Test-DemoNetwork.ps1 -HostAddress 10.0.0.25:47831
#>
[CmdletBinding()]
param([string]$HostAddress)
Write-Host "This laptop:" ; Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^(127|169\.254)' } | ForEach-Object { Write-Host ("  {0}  {1}/{2}" -f $_.InterfaceAlias, $_.IPAddress, $_.PrefixLength) }
Get-NetConnectionProfile | ForEach-Object { Write-Host ("  Wi-Fi/network '{0}' profile: {1}" -f $_.Name, $_.NetworkCategory) }
if (-not $HostAddress) {
    Write-Host "`nNo -HostAddress given. Open the app: it searches automatically (about 8 seconds). If it finds nothing, ask the host for the manual address (host:port) and run this again with -HostAddress."
    exit 0
}
$parts = $HostAddress.Split(":"); $h = $parts[0]; $p = [int]$parts[1]
Write-Host "`nChecking $h port $p ..."
$ping = Test-Connection -ComputerName $h -Count 2 -Quiet -ErrorAction SilentlyContinue
Write-Host ("  ping (may be blocked even on a good network): {0}" -f $(if ($ping) { "answers" } else { "no answer" }))
$tcp = New-Object Net.Sockets.TcpClient
try {
    $a = $tcp.BeginConnect($h, $p, $null, $null)
    if (-not $a.AsyncWaitHandle.WaitOne(5000)) {
        Write-Host "  TCP: NO ANSWER (timeout)" -ForegroundColor Red
        Write-Host "  -> Most likely the host's firewall blocks it (its Wi-Fi is Public, or the demo rule is missing), OR this Wi-Fi isolates laptops from each other (client isolation)."
        Write-Host "     Quick isolation test: the host also cannot be pinged AND other laptops cannot see each other => the Wi-Fi isolates clients; only a different network (or a hotspot) will work."
        exit 1
    }
    try { $tcp.EndConnect($a); Write-Host "  TCP: connected" -ForegroundColor Green } catch { Write-Host "  TCP: refused - the host is reachable but the demo backend is not running on that port." -ForegroundColor Red; exit 1 }
} finally { $tcp.Close() }
try { $r = Invoke-RestMethod "http://$h`:$p/api/health" -TimeoutSec 5; Write-Host ("  Spinco backend answered: environment={0}, central storage={1}" -f $r.environment, $r.central_storage) -ForegroundColor Green }
catch { Write-Host "  The port answered but not as a Spinco backend." -ForegroundColor Red; exit 1 }
Write-Host "`nOK: type this address in the app if automatic discovery did not find the host."
