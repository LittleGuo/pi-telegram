# 安装 / 更新三件事：
#   1) 用 pi 把这个目录登记成包（扩展由 pi 直接从这里加载，不再往 extensions 里复制副本）
#   2) 检查 notify.json 在不在
#   3) 重启桥进程（杀旧的，再由看门狗拉起）
# 用法：pwsh -File install.ps1   然后到 pi TUI 里 /reload
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$notify = "$env:USERPROFILE\.pi\agent\notify.json"

# 没有 notify.json 时桥会在 pythonw 里静默崩掉，先拦住
if (-not (Test-Path -LiteralPath $notify)) {
    Write-Host "缺少 $notify" -ForegroundColor Yellow
    Write-Host '先把 notify.example.json 复制成它，填好 token / chatId（需要代理再填 proxy），然后重跑本脚本。'
    exit 1
}

$pi = (Get-Command pi -ErrorAction SilentlyContinue).Source
if (-not $pi) {
    $fallback = "$env:USERPROFILE\.pi\agent\bin\pi.cmd"
    if (Test-Path -LiteralPath $fallback) { $pi = $fallback }
}
if ($pi) {
    & $pi install $root
} else {
    Write-Host "没找到 pi 命令，请在 pi 里手动登记：pi install $root" -ForegroundColor Yellow
}

Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*telegram-tui-bridge.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2
& (Join-Path $root 'ensure-bridge.ps1')

Write-Host '桥已重启。到 pi TUI 执行 /reload 让扩展从本仓库生效。'
