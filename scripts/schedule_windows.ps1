# Registers the daily feed as Windows scheduled tasks for the current user:
#   ShortsFeed-Discover  (default 00:00)             python feed.py discover
#   ShortsFeed-Process   (default every 12 h from 01:00) python feed.py process (tops the queue up to SHORTS_PER_DAY)
#   ShortsFeed-Publish   (default every 4 h from 09:00) python feed.py publish
#   ShortsFeed-Stats     (default 08:00)             python feed.py stats
# Logs go to output\logs\. Every task also fires at logon, so runs missed while
# you were signed out are caught up (signing out stops scheduled tasks; locking
# the session does not). Remove with: Unregister-ScheduledTask ShortsFeed-*
#
# -RunWhenLoggedOff registers the tasks to run even with no session open. It needs
# an ELEVATED PowerShell, and the Claude Code CLI must work outside your session.
param(
    [string]$DiscoverAt = "00:00",
    [string]$ProcessAt = "01:00",
    [int]$ProcessEveryHours = 12,
    [string]$PublishFrom = "09:00",
    [int]$PublishEveryHours = 4,
    [string]$StatsAt = "08:00",
    [switch]$RunWhenLoggedOff
)

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo "venv\Scripts\python.exe"
$logDir = Join-Path $repo "output\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

function New-LogonCatchUpTrigger {
    # Catches up what was missed while signed out; the run lock keeps it from overlapping.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = "PT3M"
    $trigger
}

function Register-FeedTask([string]$Name, [string]$Command, $Trigger, [string]$When) {
    $log = Join-Path $logDir "$Name.log"
    $cmdArgs = "/c set PYTHONIOENCODING=utf-8&& `"$python`" feed.py $Command >> `"$log`" 2>&1"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArgs -WorkingDirectory $repo
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 8)
    $triggers = @($Trigger, (New-LogonCatchUpTrigger))
    $register = @{ TaskName = $Name; Action = $action; Trigger = $triggers; Settings = $settings; Force = $true }
    if ($RunWhenLoggedOff) {
        $register.Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
    }
    Register-ScheduledTask @register | Out-Null
    Write-Host "Registered $Name $When + at logon (log: $log)"
}

Register-FeedTask "ShortsFeed-Discover" "discover" (New-ScheduledTaskTrigger -Daily -At $DiscoverAt) "at $DiscoverAt"
$processTrigger = New-ScheduledTaskTrigger -Once -At $ProcessAt -RepetitionInterval (New-TimeSpan -Hours $ProcessEveryHours)
Register-FeedTask "ShortsFeed-Process" "process" $processTrigger "every $ProcessEveryHours h from $ProcessAt"
$publishTrigger = New-ScheduledTaskTrigger -Once -At $PublishFrom -RepetitionInterval (New-TimeSpan -Hours $PublishEveryHours)
Register-FeedTask "ShortsFeed-Publish" "publish" $publishTrigger "every $PublishEveryHours h from $PublishFrom"
Register-FeedTask "ShortsFeed-Stats" "stats" (New-ScheduledTaskTrigger -Daily -At $StatsAt) "at $StatsAt"
