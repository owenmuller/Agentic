# Registers the Windows scheduled task for the Class 3 paper trading session.
#
# DELIBERATELY NOT RUN BY THE AGENT. Installing a task that trades — even on paper —
# is the human's trigger to pull. Review, then run:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File "ops\register_paper_task.ps1"
#
# What it registers:
#   - Runs weekdays at 9:15 AM Eastern (converted to this machine's local time at
#     registration; see the DST note below), using the repo's .venv python with the
#     repo as working directory.
#   - The command is `python -m orchestrator run`, which does its own gating: it
#     computes the 9:30-16:00 ET session in America/New_York AT RUNTIME, sleeps until
#     the open, ticks until the close, then shuts down cleanly (cancels working
#     orders, persists session state). The 15-minute-early trigger is slack, not the
#     source of truth.
#   - PAPER_MODE stays whatever .env says. Constraint #4 is enforced inside the
#     process at startup, before anything else; this script adds no mode logic.
#
# DST note: Task Scheduler triggers fire in LOCAL time. If this machine's timezone
# shares the US DST calendar (any US zone does), the ET offset is constant and the
# converted trigger time is correct year-round. If not (e.g. a European zone), the
# trigger drifts by an hour for a few weeks twice a year — the run-mode gating means
# the session is still traded correctly as long as the trigger lands BEFORE the open;
# the script warns if it detects a non-US-DST zone so you can add more slack.

$ErrorActionPreference = "Stop"

$taskName = "Agentic Paper Trading"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "No .venv python at $python - create the venv first (python -m venv .venv; pip install -e .[dev])"
}

# 9:15 AM Eastern today, expressed in this machine's local time.
$eastern = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$todayEastern = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $eastern)
$triggerEastern = [DateTimeOffset]::new($todayEastern.Year, $todayEastern.Month, $todayEastern.Day, 9, 15, 0, $todayEastern.Offset)
$triggerLocal = $triggerEastern.ToLocalTime().DateTime

$localZone = [TimeZoneInfo]::Local
if ($localZone.SupportsDaylightSavingTime -and ($localZone.Id -notmatch "Eastern|Central|Mountain|Pacific|Alaskan|US")) {
    Write-Warning ("This machine's zone ({0}) may not share the US DST calendar; " -f $localZone.Id)
    Write-Warning "the 9:15 ET trigger can drift by an hour near DST transitions. The run gates itself at runtime, but consider re-registering with extra slack."
}

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m orchestrator run" `
    -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At $triggerLocal

# Laptop reality (learned 2026-08-19, the first scheduled session was silently
# missed): the machine was ASLEEP at trigger time, WakeToRun was off, and the
# StartWhenAvailable catch-up did not fire after wake — sleep-missed triggers are
# not reliably treated as "missed" by Task Scheduler. So: wake the machine for the
# session, allow starting on battery, and don't kill a mid-session run on unplug.
# StartWhenAvailable stays as a second net for the powered-off case.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 9) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Agentic Class 3 paper trading session: python -m orchestrator run. The process gates itself on the 9:30-16:00 ET session at runtime; this trigger only has to be early. PAPER_MODE is enforced inside the process (CLAUDE.md Constraint #4)." `
    -Force

Write-Host ""
Write-Host ("Registered '{0}':" -f $taskName)
Write-Host ("  runs:    {0} -m orchestrator run" -f $python)
Write-Host ("  in:      {0}" -f $repo)
Write-Host ("  trigger: weekdays at {0} local ({1} = 9:15 AM ET today)" -f $triggerLocal.ToString("HH:mm"), $localZone.Id)
Write-Host ""
Write-Host "Daily check:   python -m orchestrator health"
Write-Host "Run history:   Get-Content data\run.log -Tail 20"
Write-Host "Unregister:    Unregister-ScheduledTask -TaskName 'Agentic Paper Trading' -Confirm:`$false"
