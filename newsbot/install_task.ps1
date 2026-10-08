# Registers the "NewsBot" scheduled task: runs `newsbot.py run-due` every 15 minutes and at logon.
# Run from this folder:  powershell -ExecutionPolicy Bypass -File install_task.ps1
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"   # pythonw = no console window popping up
if (-not (Test-Path $pythonw)) { throw "Create the virtual environment first (see README.md)." }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dir\newsbot.py`" run-due" -WorkingDirectory $dir
$every15  = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
$atLogon  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "NewsBot" -Action $action -Trigger $every15, $atLogon `
    -Settings $settings -Description "News digest: checks every 15 min which edition is due" -Force | Out-Null
Write-Host "Registered 'NewsBot'. Test it now with:  Start-ScheduledTask NewsBot"
