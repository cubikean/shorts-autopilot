# Registers the daily feed as Windows scheduled tasks for the current user:
#   ShortsFeed-Discover  (default 00:00)             python feed.py discover
#   ShortsFeed-Process   (default 01:00)             python feed.py process   (renders what discover just queued)
#   ShortsFeed-Publish   (default every 4 h from 09:00) python feed.py publish
#   ShortsFeed-Stats     (default 08:00)             python feed.py stats
# Logs go to output\logs\. Tasks run while you are logged on (the Claude Code
# CLI needs your session). Remove with: Unregister-ScheduledTask ShortsFeed-*
param(
    [string]$DiscoverAt = "00:00",
    [string]$ProcessAt = "01:00",
    [string]$PublishFrom = "09:00",
    [int]$PublishEveryHours = 4,
    [string]$StatsAt = "08:00"
)

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo "venv\Scripts\python.exe"
$logDir = Join-Path $repo "output\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

function Register-FeedTask([string]$Name, [string]$Command, $Trigger, [string]$When) {
    $log = Join-Path $logDir "$Name.log"
    $cmdArgs = "/c set PYTHONIOENCODING=utf-8&& `"$python`" feed.py $Command >> `"$log`" 2>&1"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArgs -WorkingDirectory $repo
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 8)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $settings -Force | Out-Null
    Write-Host "Registered $Name $When (log: $log)"
}

Register-FeedTask "ShortsFeed-Discover" "discover" (New-ScheduledTaskTrigger -Daily -At $DiscoverAt) "at $DiscoverAt"
Register-FeedTask "ShortsFeed-Process" "process" (New-ScheduledTaskTrigger -Daily -At $ProcessAt) "at $ProcessAt"
$publishTrigger = New-ScheduledTaskTrigger -Once -At $PublishFrom -RepetitionInterval (New-TimeSpan -Hours $PublishEveryHours)
Register-FeedTask "ShortsFeed-Publish" "publish" $publishTrigger "every $PublishEveryHours h from $PublishFrom"
Register-FeedTask "ShortsFeed-Stats" "stats" (New-ScheduledTaskTrigger -Daily -At $StatsAt) "at $StatsAt"
