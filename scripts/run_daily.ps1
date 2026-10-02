<#
.SYNOPSIS
    Daily entry point for Task Scheduler (T10).

.DESCRIPTION
    Moves to the repo, activates .venv when there is one, runs
    `python -m pipeline.run_daily` and appends everything it prints to
    output\scheduler.log.

    Runs with --dry-run unless -Live is given: uploads stay manual (from
    output\YYYY-MM-DD\upload_sheet.txt) until the YouTube API audit passes.

    Exit code is the pipeline's: 0 all good, 1 a video failed, 2 bad config.

.PARAMETER Live
    Upload to YouTube instead of a dry run.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily.ps1
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily.ps1 -Live
#>
[CmdletBinding()]
param(
    [switch]$Live
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo

$outDir = Join-Path $repo 'output'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$log = Join-Path $outDir 'scheduler.log'

function Write-Log([string]$Message) {
    $line = '{0} [run_daily.ps1] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

$activate = Join-Path $repo '.venv\Scripts\Activate.ps1'
if (Test-Path -LiteralPath $activate) {
    . $activate
} else {
    Write-Log 'no .venv found; using python from PATH'
}

$pipelineArgs = @('-m', 'pipeline.run_daily')
if ($Live) {
    $mode = 'LIVE (uploads on)'
} else {
    $pipelineArgs += '--dry-run'
    $mode = 'dry run (no uploads)'
}
Write-Log "start: $mode"

# Python writes the log as UTF-8 whatever the console code page is.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# cmd does the redirect so stderr lines are appended as plain text; in Windows
# PowerShell 5.1, 2>&1 on a native program wraps each line in an error record.
$command = 'python {0} >> "{1}" 2>&1' -f ($pipelineArgs -join ' '), $log
& cmd.exe /d /c $command
$code = $LASTEXITCODE

Write-Log "finished with exit code $code"
exit $code
