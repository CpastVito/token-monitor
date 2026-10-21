$root = $PSScriptRoot + '\'
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and
    $_.CommandLine -like ('*' + $root + '*') -and
    $_.CommandLine -match 'server\.py|overlay\.py'
}
if (-not $procs) {
    Write-Host '  本目录下没有在跑的进程（也许启动的是另一份副本）'
} else {
    foreach ($p in $procs) {
        Write-Host ('  已停止 PID ' + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force
    }
}
