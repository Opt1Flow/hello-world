# Starts the news bot's small read-only web server at logon, so the Pi kiosk (or a phone)
# on your home network can show the news:
#     reading page:  http://<this laptop>:8765/
#     TV / kiosk:    http://<this laptop>:8765/kiosk
# Run once from this folder in PowerShell opened with "Run as administrator"
# (needed for the firewall rule):
#     powershell -ExecutionPolicy Bypass -File install_server.ps1
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$port = 8765
$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Create the virtual environment first (see README.md)." }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dir\newsbot.py`" serve --port $port" -WorkingDirectory $dir
$atLogon  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "NewsBot Server" -Action $action -Trigger $atLogon -Settings $settings `
    -Description "Shares the news pages on the home network (Pi kiosk)" -Force | Out-Null

# Let devices on your home network reach the port. Only the Private profile: on public
# Wi-Fi (cafe, campus) the port stays closed.
if (-not (Get-NetFirewallRule -DisplayName "NewsBot Server" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "NewsBot Server" -Direction Inbound -Protocol TCP `
        -LocalPort $port -Action Allow -Profile Private | Out-Null
}

Start-ScheduledTask -TaskName "NewsBot Server"
$ip = (Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred |
       Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet|WSL|Bluetooth' } |
       Select-Object -First 1).IPAddress
Write-Host "Server started. On the Pi, open:  http://$($ip):$port/kiosk"
$netCategory = (Get-NetConnectionProfile | Select-Object -First 1).NetworkCategory
if ($netCategory -ne "Private") {
    Write-Host "Your network is set to '$netCategory'. Set it to Private (Settings > Network & internet >"
    Write-Host "your Wi-Fi > Network profile type), or the Pi won't be able to connect."
}
