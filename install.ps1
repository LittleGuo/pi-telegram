# 安装 / 更新：把扩展复制到 pi 的全局扩展目录，并重启桥进程（杀旧的，再由看门狗拉起）。
# 用法：pwsh -File install.ps1   然后到 pi TUI 里 /reload
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$extDir = "$env:USERPROFILE\.pi\agent\extensions"
$notify = "$env:USERPROFILE\.pi\agent\notify.json"

# 没有 notify.json 时桥会在 pythonw 里静默崩掉，先拦住
if (-not (Test-Path -LiteralPath $notify)) {
    Write-Host "缺少 $notify" -ForegroundColor Yellow
    Write-Host '先把 notify.example.json 复制成它，填好 token / chatId（需要代理再填 proxy），然后重跑本脚本。'
    exit 1
}

New-Item -ItemType Directory -Force -Path $extDir | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'pi-telegram-tui-bridge.ts') -Destination $extDir -Force

Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*telegram-tui-bridge.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2
& (Join-Path $root 'ensure-bridge.ps1')

Write-Host '扩展已复制到 ~\.pi\agent\extensions\，桥已重启。到 pi TUI 执行 /reload 生效。'
