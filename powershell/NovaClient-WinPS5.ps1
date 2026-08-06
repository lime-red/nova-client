#Requires -Version 5.1
<#
.SYNOPSIS
    Nova Client - syncs BBS game packets with a Nova Hub, and runs BRE/FE maintenance.

.DESCRIPTION
    A self-contained reference client for the Nova Hub Service API, written for
    Windows PowerShell 5.1 - the version that ships with Windows. Nothing to install.

    Three modes:
      -Validate   Check the config (and nodes.dat) and report every problem found.
      -Once       Run a single sync: upload outbound packets, download inbound ones.
      -Daemon     Run continuously: sync on an interval, and run game maintenance
                  on its own interval or immediately when packets arrive.

    If you are on PowerShell 7 or later, use NovaClient-PS7.ps1 instead. This
    file contains only the 5.1-specific HTTP transport; everything else is in
    NovaClient.Common.ps1, which both launchers dot-source and which must stay
    next to this script.

.PARAMETER Config
    Path to the config file. Defaults to config.psd1 next to this script.

.PARAMETER Validate
    Validate configuration and exit. Makes no network calls.

.PARAMETER Once
    Perform a single sync and exit. This is the default if no mode is given.

.PARAMETER Daemon
    Run continuously until Ctrl+C.

.PARAMETER ShowVersion
    Print the client version and exit.

.EXAMPLE
    .\NovaClient-WinPS5.ps1 -Validate

.EXAMPLE
    .\NovaClient-WinPS5.ps1 -Once -Verbose

.EXAMPLE
    .\NovaClient-WinPS5.ps1 -Daemon -Config C:\BBS\nova-client\config.psd1

.NOTES
    Exit codes:  0 success, 1 errors occurred, 2 configuration invalid,
                 3 another instance is already running, 4 interrupted.
#>
# PSScriptAnalyzer: the following are deliberate, not oversights.
#   Write-Host          - this is an interactive console tool whose output IS
#                         the user interface. Write-Output would pollute the
#                         pipeline and break the exit-code contract.
#   ShouldProcess       - -Once and -Daemon are the mode switches; a -WhatIf on
#                         the sync loop would have nothing meaningful to report.
#                         Install-NovaClientTask.ps1 does implement ShouldProcess,
#                         because that one changes machine state.
#   PSUseSingularNouns  - Get-NovaLeagues returns a collection, and the plural
#                         reads correctly at every call site.
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', '')]
[CmdletBinding()]
param(
    [string] $Config = (Join-Path $PSScriptRoot 'config.psd1'),
    [switch] $Validate,
    [switch] $Once,
    [switch] $Daemon,
    [switch] $ShowVersion
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Script:NovaClientVersion = '0.3.0'
$Script:UserAgent = "nova-client-ps/$Script:NovaClientVersion (WinPS5.1)"
$Script:RuntimeLabel = 'PowerShell 5.1'

# ============================================================================
#  HTTP TRANSPORT  --  the only part that differs between the two launchers.
#  Must define Invoke-NovaRequest and ConvertFrom-NovaJson exactly as the
#  contract at the top of NovaClient.Common.ps1 describes them.
# ============================================================================

# Windows PowerShell 5.1 defaults to SSL3/TLS1.0, which every modern hub
# rejects. Without this line HTTPS fails with "Could not create SSL/TLS
# secure channel" and no useful detail.
[Net.ServicePointManager]::SecurityProtocol =
    [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11

# Invoke-WebRequest renders a progress bar per chunk on 5.1, which can slow a
# download by an order of magnitude. We do our own logging instead.
$Script:OriginalProgressPreference = $ProgressPreference
$ProgressPreference = 'SilentlyContinue'

function Invoke-NovaRequest {
    <#
        Single HTTP chokepoint. Never throws for an HTTP-level failure - always
        returns a result object so callers can branch on StatusCode:

            Ok          [bool]    true for 2xx
            StatusCode  [int]     0 means the request never reached the server
            Content     [string]  response body as text (empty for -OutFile)
            Detail      [string]  the hub's error message, unwrapped from {"detail": ...}
            Transport   [bool]    true if this was a connection-level failure

        On 5.1, Invoke-WebRequest throws a terminating error for any non-2xx
        status and the response body is only reachable through the exception's
        response stream. That unwrapping is the main reason this script and the
        PS7 one are separate files.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Method,
        [Parameter(Mandatory)][string] $Uri,
        [hashtable] $Headers = @{},
        $Body,
        [string] $ContentType,
        [string] $InFile,
        [string] $OutFile,
        [int] $TimeoutSec = 120
    )

    $requestHeaders = @{ 'User-Agent' = $Script:UserAgent }
    foreach ($key in $Headers.Keys) { $requestHeaders[$key] = $Headers[$key] }

    $splat = @{
        Method          = $Method
        Uri             = $Uri
        Headers         = $requestHeaders
        TimeoutSec      = $TimeoutSec
        UseBasicParsing = $true
        ErrorAction     = 'Stop'
    }
    if ($PSBoundParameters.ContainsKey('Body') -and $null -ne $Body) { $splat.Body = $Body }
    if ($ContentType) { $splat.ContentType = $ContentType }
    if ($InFile)      { $splat.InFile      = $InFile }
    if ($OutFile)     { $splat.OutFile     = $OutFile }

    try {
        $response = Invoke-WebRequest @splat

        # 5.1 has no -PassThru, so -OutFile returns nothing at all. Reaching
        # here at all means no exception was thrown, and on 5.1 any non-2xx
        # throws - so a null response is a success that went to the file.
        if ($null -eq $response) {
            return [pscustomobject]@{
                Ok = $true; StatusCode = 200; Content = ''; Detail = ''; Transport = $false
            }
        }

        $content = ''
        if (-not $OutFile -and $null -ne $response.Content) {
            $content = if ($response.Content -is [byte[]]) {
                [Text.Encoding]::UTF8.GetString($response.Content)
            } else {
                [string]$response.Content
            }
        }
        return [pscustomobject]@{
            Ok         = $true
            StatusCode = [int]$response.StatusCode
            Content    = $content
            Detail     = ''
            Transport  = $false
        }
    }
    catch [Net.WebException] {
        $webResponse = $_.Exception.Response

        # No response object at all means DNS failure, refused connection,
        # TLS handshake failure or timeout - i.e. worth retrying.
        if ($null -eq $webResponse) {
            return [pscustomobject]@{
                Ok         = $false
                StatusCode = 0
                Content    = ''
                Detail     = $_.Exception.Message
                Transport  = $true
            }
        }

        $status = [int]$webResponse.StatusCode
        $bodyText = ''
        try {
            $stream = $webResponse.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            try { $bodyText = $reader.ReadToEnd() } finally { $reader.Dispose() }
        }
        catch { $bodyText = '' }

        return [pscustomobject]@{
            Ok         = $false
            StatusCode = $status
            Content    = $bodyText
            Detail     = ConvertFrom-NovaErrorBody $bodyText $_.Exception.Message
            Transport  = $false
        }
    }
    catch {
        return [pscustomobject]@{
            Ok         = $false
            StatusCode = 0
            Content    = ''
            Detail     = $_.Exception.Message
            Transport  = $true
        }
    }
}

function ConvertFrom-NovaJson {
    <#
        5.1 has no -AsHashtable, so JSON objects come back as PSCustomObject.
        Callers use property access, which works the same on both.
    #>
    param([string] $Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    try { return ConvertFrom-Json -InputObject $Text } catch { return $null }
}

# ============================================================================
#  END HTTP TRANSPORT
# ============================================================================

# ============================================================================
#  Everything else lives in NovaClient.Common.ps1, shared with the other
#  launcher. Dot-sourcing runs it in this scope, so its functions and
#  $Script: variables land here, and its entry point can read the parameters
#  declared above. That file must stay 5.1-compatible - read the rule at the
#  top of it before editing.
# ============================================================================

$commonPath = Join-Path $PSScriptRoot 'NovaClient.Common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) {
    Write-Error "Missing NovaClient.Common.ps1 - it must sit next to this script. Copy the whole powershell/ folder, not just this one file."
    exit 2
}
. $commonPath

# The exit lives here, not in the common file: `exit` inside a dot-sourced
# script returns to this one rather than setting the process exit code.
exit (Invoke-NovaEntryPoint)
