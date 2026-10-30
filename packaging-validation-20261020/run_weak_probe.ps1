$ErrorActionPreference = 'Continue'
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
$root = 'D:\Bills\packaging-validation-20261020'
$exe  = "$root\build\Spinco TA Audit 1.2.5.exe"
$py   = 'D:\Bills\backend\.venv\Scripts\python.exe'
$user = "$root\userdata_weak"; $tmp = "$root\tmp_weak"; $evid = "$root\evidence_weak"
foreach ($d in $user, $tmp, $evid) { if (Test-Path $d) { Write-Output "ABORT: $d already exists"; exit 3 }; New-Item -ItemType Directory $d | Out-Null }

# 1. loopback stand-in for the hosted providers (must receive ZERO requests)
$mockLog = "$evid\provider_mock_hits.log"; New-Item -ItemType File $mockLog | Out-Null
$mock = Start-Process -FilePath $py -ArgumentList "`"$root\mock_provider.py`" 58801 `"$mockLog`"" -PassThru -WindowStyle Hidden
Start-Sleep 2

# 2. HOSTILE provider environment: everything a leaked developer shell could contain (dummy key, not a credential)
$env:SPINCO_NVIDIA_ENABLED = 'true';   $env:SPINCO_NVIDIA_API_KEY = 'dummy-not-a-credential'
$env:SPINCO_NVIDIA_OCR_ENDPOINT = 'http://127.0.0.1:58801/ocr'; $env:SPINCO_NVIDIA_BASE_URL = 'http://127.0.0.1:58801/v1'
$env:SPINCO_DEEPSEEK_ENABLED = 'true'; $env:SPINCO_DEEPSEEK_API_KEY = 'dummy-not-a-credential'
$env:SPINCO_DEEPSEEK_BASE_URL = 'http://127.0.0.1:58801/v1'
# 3. portable wrapper extracts into the disposable TEMP; user data via --user-data-dir (APPDATA is ignored by Electron)
$env:TEMP = $tmp; $env:TMP = $tmp

$args2 = "--user-data-dir=`"$user`" --remote-debugging-port=9335 `"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1`""
$app = Start-Process -FilePath $exe -ArgumentList $args2 -PassThru
Write-Output "launched wrapper pid $($app.Id) at $(Get-Date -Format s)"

# 4. sampler: any non-loopback TCP connection owned by the app's process tree
$sampler = Start-Job -ArgumentList $app.Id, "$evid\nonloopback_connections.log" -ScriptBlock {
    param($rootPid, $log)
    while ($true) {
        $all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name
        $tree = @($rootPid); $grew = $true
        while ($grew) { $grew = $false; foreach ($p in $all) { if ($tree -contains $p.ParentProcessId -and $tree -notcontains $p.ProcessId) { $tree += $p.ProcessId; $grew = $true } } }
        $names = @{}; foreach ($p in $all) { $names[[int]$p.ProcessId] = $p.Name }
        Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object { $tree -contains $_.OwningProcess -and $_.RemoteAddress -notin '127.0.0.1', '::1', '0.0.0.0', '::' } |
            ForEach-Object { "$(Get-Date -Format s) pid=$($_.OwningProcess) $($names[[int]$_.OwningProcess]) $($_.State) -> $($_.RemoteAddress):$($_.RemotePort)" | Add-Content $log }
        "$(Get-Date -Format s) tree=$($tree.Count) procs" | Add-Content "$log.heartbeat"
        Start-Sleep 3
    }
}

# 5. wait for the DevTools port, then drive
$ok = $false
for ($i = 0; $i -lt 90; $i++) { try { Invoke-WebRequest "http://127.0.0.1:9335/json/version" -UseBasicParsing -TimeoutSec 2 | Out-Null; $ok = $true; break } catch { Start-Sleep 2 } }
Write-Output "devtools port up: $ok"
if ($ok) {
    & $py "$root\weak_probe_driver.py" 9335 "$root\fixtures\pf_weak_ocr.pdf" $evid 2>&1 | Tee-Object "$evid\driver_output.txt"
    Write-Output "driver exit code: $LASTEXITCODE"
}

# 6. evidence + teardown (processes only; no data is deleted)
Start-Sleep 5
Copy-Item "$user\logs" "$evid\app_logs" -Recurse -ErrorAction SilentlyContinue
Copy-Item "$user\backend.log" "$evid\backend.log" -ErrorAction SilentlyContinue
Get-ChildItem $user -Recurse -Filter '*.log' -ErrorAction SilentlyContinue | ForEach-Object { Copy-Item $_.FullName "$evid\$($_.Name)" -ErrorAction SilentlyContinue }
Stop-Job $sampler -ErrorAction SilentlyContinue; Remove-Job $sampler -Force -ErrorAction SilentlyContinue
$ids = @($app.Id); $all = Get-CimInstance Win32_Process; $grew = $true
while ($grew) { $grew = $false; foreach ($p in $all) { if ($ids -contains $p.ParentProcessId -and $ids -notcontains $p.ProcessId) { $ids += $p.ProcessId; $grew = $true } } }
$ids | Sort-Object -Descending | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Stop-Process -Id $mock.Id -Force -ErrorAction SilentlyContinue
Write-Output "mock provider hits: $((Get-Content $mockLog | Measure-Object -Line).Lines)"
Write-Output "non-loopback connection samples: $(if (Test-Path "$evid\nonloopback_connections.log") { (Get-Content "$evid\nonloopback_connections.log" | Measure-Object -Line).Lines } else { 0 })"
Write-Output "sampler heartbeats: $(if (Test-Path "$evid\nonloopback_connections.log.heartbeat") { (Get-Content "$evid\nonloopback_connections.log.heartbeat" | Measure-Object -Line).Lines } else { 0 })"
Write-Output "finished $(Get-Date -Format s)"

