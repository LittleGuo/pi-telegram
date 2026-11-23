# 看门狗：定时检查 TUI 桥 telegram-tui-bridge.py，不在了就重新拉起。
# 不要启动旧的无头桥 telegram-bridge.py：它会与 TUI 桥争抢同一个 bot 的 getUpdates
#（HTTP 409），并把消息送进只读 session-id=telegram-bridge。
# 运行时产物（log / state / pid）仍写在 ~\.pi\agent，不落在仓库里。
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = "$env:USERPROFILE\.pi\agent\telegram-tui-bridge-watchdog.log"
$script = Join-Path $root 'telegram-tui-bridge.py'
$pythonw = "$env:LOCALAPPDATA\Programs\Python\Python314\pythonw.exe"
$pidFile = "$env:USERPROFILE\.pi\agent\telegram-tui-bridge.pid"

$alive = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*telegram-tui-bridge.py*' }
if ($alive) { exit 0 }

# 换机器后 Python 版本/路径会变，回退到 PATH 里的 pythonw
if (-not (Test-Path -LiteralPath $pythonw)) {
    $onPath = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    $pythonw = if ($onPath) { $onPath.Source } else { 'pythonw.exe' }
}
$p = Start-Process -FilePath $pythonw `
    -ArgumentList "`"$script`"" `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -PassThru
if ($p) {
    Set-Content -LiteralPath $pidFile -Value $p.Id -Encoding ASCII
    Add-Content -Path $log -Value ("{0} TUI 桥不在，已重新拉起 pid={1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $p.Id)
} else {
    Add-Content -Path $log -Value ("{0} 拉起失败：找不到 {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $pythonw)
}
