<#
  Registers a Windows scheduled task that starts a VirtualBox VM headless
  (no window) every time the laptop boots, even before anyone logs in.

  Run in an Administrator PowerShell:
    Set-ExecutionPolicy -Scope Process Bypass
    .\register-vm-autostart.ps1 -VmName pihole

  VirtualBox VMs belong to the user who created them, so the task runs as
  YOUR account; Windows will ask for your password once to store it.
  (Hyper-V users don't need this -- use the VM's "Automatic Start Action".)
#>
param(
    [string]$VmName = "pihole",
    [int]$DelaySeconds = 60
)

$ErrorActionPreference = "Stop"

$vbox = Join-Path $env:ProgramFiles "Oracle\VirtualBox\VBoxManage.exe"
if (-not (Test-Path $vbox)) { throw "VBoxManage not found at $vbox -- is VirtualBox installed?" }

$vms = & $vbox list vms
if ($vms -notmatch "^`"$([regex]::Escape($VmName))`"") {
    throw "No VM named '$VmName'. Existing VMs:`n$($vms -join "`n")"
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$cred = Get-Credential -UserName $user -Message "Password for $user (stored by Task Scheduler so the VM starts without logging in)"

$action   = New-ScheduledTaskAction -Execute $vbox -Argument "startvm `"$VmName`" --type headless"
$trigger  = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT${DelaySeconds}S"   # give the network adapter time to come up
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "Start VM $VmName" -Action $action -Trigger $trigger -Settings $settings `
    -User $user -Password $cred.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null

Write-Host "Task 'Start VM $VmName' registered. It starts the VM ${DelaySeconds}s after each boot."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName 'Start VM $VmName'"
Write-Host "Note: a headless VM won't show a window; open VirtualBox and use 'Show' to see its console."
