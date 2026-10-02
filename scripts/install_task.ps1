<#
.SYNOPSIS
    Registers the daily Task Scheduler job for the pipeline (T10).

.DESCRIPTION
    Creates (or replaces) a task that runs scripts\run_daily.ps1 every day at
    02:00 local time (the machine should be on Asia/Phnom_Penh, UTC+7):
      * runs whether the user is logged on or not (S4U logon, no stored password)
      * wakes the computer to run
      * starts as soon as possible if 02:00 was missed
      * never runs two copies at once; stopped after 3 hours

    The machine must be powered and online at 02:00, and the power plan must
    allow wake timers.

    Run from an elevated (Administrator) PowerShell.

.PARAMETER Live
    Register the task with -Live, so it uploads. Default is dry run.

.PARAMETER At
    Daily start time, local, HH:mm. Default 02:00.

.PARAMETER TaskName
    Task Scheduler name. Default "Cradle Not Home daily".

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1
#>
[CmdletBinding()]
param(
    [switch]$Live,
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = '02:00',
    [string]$TaskName = 'Cradle Not Home daily'
)

$ErrorActionPreference = 'Stop'

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an elevated PowerShell (Run as administrator).'
}

$zone = Get-TimeZone
if ($zone.BaseUtcOffset -ne [TimeSpan]::FromHours(7) -or $zone.SupportsDaylightSavingTime) {
    $message = 'Time zone is {0} ({1}); the schedule assumes Asia/Phnom_Penh, UTC+7. ' -f $zone.Id, $zone.BaseUtcOffset
    Write-Warning ($message + 'The task still fires at local time.')
}

$runner = Join-Path $PSScriptRoot 'run_daily.ps1'
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Missing $runner"
}
$repo = Split-Path -Parent $PSScriptRoot

$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $runner
if ($Live) {
    $arguments += ' -Live'
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, 'HH:mm', $null))
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Renders the daily Shorts (scripts\run_daily.ps1). Log: output\scheduler.log' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $taskPrincipal `
    -Force | Out-Null

$mode = if ($Live) { 'LIVE (uploads on)' } else { 'dry run (no uploads)' }
Write-Host "Registered '$TaskName': daily at $At local, $mode, as $user."
Write-Host "Runs: powershell.exe $arguments"
Write-Host "Test now:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:     Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host 'Check that wake timers are enabled: Control Panel > Power Options > Change plan settings >'
Write-Host '  Change advanced power settings > Sleep > Allow wake timers > Enable.'
