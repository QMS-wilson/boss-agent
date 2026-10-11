# 停止开发环境（读 logs/running.json 中的 PID）
$root = "D:\v3-wechat"
$run = Join-Path $root "logs\running.json"
if (Test-Path $run) {
    $info = Get-Content $run -Raw | ConvertFrom-Json
    foreach ($p in @($info.cloud, $info.agent)) {
        if ($p) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue; Write-Output "stopped $p" }
    }
    Remove-Item $run -ErrorAction SilentlyContinue
} else {
    Write-Output "没有 running.json，未停止任何进程"
}
