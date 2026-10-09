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
# Also every 15 minutes: if the server ever stops, it's back within 15 minutes. (IgnoreNew
# below means a running server is left alone, so there's never a second copy.)
$every15  = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "NewsBot Server" -Action $action -Trigger $atLogon, $every15 -Settings $settings `
    -Description "Shares the news pages on the home network (Pi kiosk)" -Force | Out-Null

# Let devices on your home network reach the port. Only the Private profile: on public
# Wi-Fi (cafe, campus) the port stays closed.
if (-not (Get-NetFirewallRule -DisplayName "NewsBot Server" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "NewsBot Server" -Direction Inbound -Protocol TCP `
        -LocalPort $port -Action Allow -Profile Private | Out-Null
}

Start-ScheduledTask -TaskName "NewsBot Server"
Start-Sleep -Seconds 5
if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
    Write-Host "The server didn't start. See data\server.log in this folder for the reason."
    exit 1
}

# The address the Pi should use: the adapter that leads to your router (not VPN/virtual ones).
$net = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" }
foreach ($n in $net) {
    Write-Host "Server running. On the Pi, open:  http://$($n.IPv4Address.IPAddress):$port/kiosk   ($($n.InterfaceAlias))"
    $category = (Get-NetConnectionProfile -InterfaceIndex $n.InterfaceIndex).NetworkCategory
    if ($category -ne "Private") {
        Write-Host "  '$($n.InterfaceAlias)' is set to '$category'. Set it to Private (Settings > Network & internet >"
        Write-Host "  your Wi-Fi > Network profile type), or the Pi won't be able to connect."
    }
}
