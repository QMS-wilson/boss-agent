# 启动开发环境：云端 API（含后台管理）+ 本地 Agent
$root = "D:\v3-wechat"
$py = "C:\Users\24773\AppData\Local\Programs\Python\Python313\python.exe"
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null

$cloud = Start-Process -FilePath $py `
    -ArgumentList "-m", "cloudapi.main", "--config", "cloud_config.json" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logs "cloud.out.log") `
    -RedirectStandardError (Join-Path $logs "cloud.err.log")

Start-Sleep -Seconds 2

$agent = Start-Process -FilePath $py `
    -ArgumentList "-m", "v3agent.main", "--config", "config.json", "--ws" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logs "agent.out.log") `
    -RedirectStandardError (Join-Path $logs "agent.err.log")

@{ cloud = $cloud.Id; agent = $agent.Id; started = (Get-Date -Format o) } |
    ConvertTo-Json | Set-Content (Join-Path $logs "running.json") -Encoding utf8

Write-Output "cloud pid=$($cloud.Id)  agent pid=$($agent.Id)"
