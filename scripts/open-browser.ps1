# open-browser.ps1 — Poll the server and open the browser when ready
# Called by start-windows.bat in the background

param(
    [int]$Port = 8082,
    [int]$MaxRetries = 300
)

$url = "http://localhost:$Port/api/v1/setup/first-run"

for ($i = 0; $i -lt $MaxRetries; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -NoProxy 'localhost'
        if ($r.StatusCode -eq 200) {
            $j = $r.Content | ConvertFrom-Json
            if ($j.data.first_run) {
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
