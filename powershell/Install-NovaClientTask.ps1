#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the Nova Client daemon as a Windows Scheduled Task.

.DESCRIPTION
    Creates a task that starts the daemon at boot and restarts it if it stops.
    This replaces the "leave a PowerShell window open forever" approach.

    Run this from an elevated PowerShell prompt. Use -WhatIf first if you want
    to see what it would do without changing anything.

.PARAMETER Config
    Path to your config.psd1. Defaults to config.psd1 next to this script.

.PARAMETER Script
    Which client to run. Defaults to NovaClient-WinPS5.ps1, which needs nothing
    installed. Point it at NovaClient-PS7.ps1 if you have PowerShell 7.

.PARAMETER TaskName
    Name of the scheduled task. Defaults to 'NovaClient'.

.PARAMETER User
    Account to run as. Defaults to SYSTEM, which survives logoff. Give a real
    account if your game directories are on a mapped drive - SYSTEM cannot see
    per-user drive mappings, only UNC paths and local drives.

.EXAMPLE
    .\Install-NovaClientTask.ps1 -WhatIf

.EXAMPLE
    .\Install-NovaClientTask.ps1 -Config C:\BBS\nova-client\config.psd1

.EXAMPLE
    # Run as a named account, needed if the game lives on a mapped drive
    .\Install-NovaClientTask.ps1 -User 'MYBBS\sysop'

.NOTES
    To remove:  Unregister-ScheduledTask -TaskName NovaClient -Confirm:$false
    To check:   Get-ScheduledTask NovaClient | Get-ScheduledTaskInfo
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Config = (Join-Path $PSScriptRoot 'config.psd1'),
    [string] $Script = (Join-Path $PSScriptRoot 'NovaClient-WinPS5.ps1'),
    [string] $TaskName = 'NovaClient',
    [string] $User = 'SYSTEM'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

foreach ($path in @($Config, $Script)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Not found: $path"
    }
}

$configPath = (Resolve-Path -LiteralPath $Config).Path
$scriptPath = (Resolve-Path -LiteralPath $Script).Path

# Which host to launch under: 5.1 script -> powershell.exe, 7 script -> pwsh.exe
$isPs7Script = (Split-Path $scriptPath -Leaf) -like '*PS7*'
$shell = if ($isPs7Script) { 'pwsh.exe' } else { 'powershell.exe' }

if ($isPs7Script -and -not (Get-Command pwsh.exe -ErrorAction SilentlyContinue)) {
    throw 'NovaClient-PS7.ps1 was selected but pwsh.exe is not on PATH. Install PowerShell 7, or use NovaClient-WinPS5.ps1.'
}

# -NoProfile matters: a profile that prints a banner or prompts will hang a
# non-interactive task forever.
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Daemon -Config "{1}"' -f
    $scriptPath, $configPath

$action = New-ScheduledTaskAction -Execute $shell -Argument $arguments `
    -WorkingDirectory (Split-Path $scriptPath -Parent)

$trigger = New-ScheduledTaskTrigger -AtStartup

if ($User -eq 'SYSTEM') {
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
}
else {
    # Password:$null with LogonType Password means the task runs whether or not
    # the user is logged on; Windows prompts for the password at registration.
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Password -RunLevel Highest
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# ExecutionTimeLimit of zero means "no limit" - without it Windows kills the
# daemon after 3 days. MultipleInstances IgnoreNew is a second line of defence
# behind the client's own mutex.

Write-Host "Task name : $TaskName"
Write-Host "Run as    : $User"
Write-Host "Command   : $shell $arguments"
Write-Host ''

if ($PSCmdlet.ShouldProcess($TaskName, 'Register scheduled task')) {

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Replacing existing task '$TaskName'" -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    $registerSplat = @{
        TaskName    = $TaskName
        Action      = $action
        Trigger     = $trigger
        Principal   = $principal
        Settings    = $settings
        Description = 'Nova Client - syncs BBS game packets with Nova Hub and runs game maintenance.'
    }
    $null = Register-ScheduledTask @registerSplat

    Write-Host "Registered '$TaskName'." -ForegroundColor Green
    Write-Host ''
    Write-Host 'Start it now with:' -ForegroundColor Cyan
    Write-Host "    Start-ScheduledTask -TaskName $TaskName"
    Write-Host 'Check on it with:' -ForegroundColor Cyan
    Write-Host "    Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
    Write-Host ''
    Write-Host 'A scheduled task discards console output. Run the client by hand' -ForegroundColor DarkGray
    Write-Host 'with -Once -Verbose if you need to see what it is actually doing.' -ForegroundColor DarkGray
}
