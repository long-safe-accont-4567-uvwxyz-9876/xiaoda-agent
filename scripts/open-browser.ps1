# open-browser.ps1 — Poll the server and open the browser when ready
# Called by start-windows.bat in the background
# Compatible with PowerShell 5.x (Windows built-in) and PowerShell 7+

param(
    [int]$Port = 8082,
    [int]$MaxRetries = 300
)

$url = "http://localhost:$Port/api/v1/setup/first-run"

# 临时禁用代理以避免 localhost 请求被代理拦截
$prevProxy = [System.Net.WebRequest]::GetSystemWebProxy()
[System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy

for ($i = 0; $i -lt $MaxRetries; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($r.StatusCode -eq 200) {
            $j = $r.Content | ConvertFrom-Json
            $firstRun = $false
            try {
                $firstRun = [bool]$j.data.first_run
            } catch {
                $firstRun = $false
            }
            if ($firstRun) {
                Start-Process "http://localhost:$Port/#/setup"
            } else {
                Start-Process "http://localhost:$Port/#/login"
            }
            exit 0
        }
    } catch {
        # Server not ready yet, keep polling
    }
    Start-Sleep -Seconds 1
}
