#Requires -Version 7.0
<#
.SYNOPSIS
    Nova Client - syncs BBS game packets with a Nova Hub, and runs BRE/FE maintenance.

.DESCRIPTION
    A self-contained reference client for the Nova Hub Service API, written for
    PowerShell 7 or later.

    Three modes:
      -Validate   Check the config (and nodes.dat) and report every problem found.
      -Once       Run a single sync: upload outbound packets, download inbound ones.
      -Daemon     Run continuously: sync on an interval, and run game maintenance
                  on its own interval or immediately when packets arrive.

    If you are on the PowerShell that came with Windows (5.1), use
    NovaClient-WinPS5.ps1 instead. This file contains only the 7.x-specific
    HTTP transport; everything else is in NovaClient.Common.ps1, which both
    launchers dot-source and which must stay next to this script.

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
    .\NovaClient-PS7.ps1 -Validate

.EXAMPLE
    .\NovaClient-PS7.ps1 -Once -Verbose

.EXAMPLE
    .\NovaClient-PS7.ps1 -Daemon -Config C:\BBS\nova-client\config.psd1

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
$Script:UserAgent = "nova-client-ps/$Script:NovaClientVersion (PS7)"
$Script:RuntimeLabel = 'PowerShell 7'

# ============================================================================
#  HTTP TRANSPORT  --  the only part that differs between the two launchers.
#  Must define Invoke-NovaRequest and ConvertFrom-NovaJson exactly as the
#  contract at the top of NovaClient.Common.ps1 describes them.
# ============================================================================

# PowerShell 7 negotiates TLS via the OS defaults, so no SecurityProtocol
# fiddling is needed here (5.1 needs it, and has it).

# Even on 7, the progress bar costs real throughput on a slow link and clutters
# a daemon's log. We do our own logging instead.
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

        On 7.x, -SkipHttpErrorCheck turns a non-2xx response into an ordinary
        result whose .StatusCode and body can just be read, so no exception
        unwrapping is needed. That difference is the reason this script and the
        5.1 one are separate files.

        Note: -StatusCodeVariable belongs to Invoke-RestMethod, not
        Invoke-WebRequest - the response object carries .StatusCode itself.
        And -OutFile suppresses the return value unless -PassThru is given,
        which is why that is set below whenever we are downloading to a file.
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
        [int] $TimeoutSec = 120,
        [switch] $Raw
    )

    $requestHeaders = @{ 'User-Agent' = $Script:UserAgent }
    foreach ($key in $Headers.Keys) { $requestHeaders[$key] = $Headers[$key] }

    $splat = @{
        Method             = $Method
        Uri                = $Uri
        Headers            = $requestHeaders
        TimeoutSec         = $TimeoutSec
        SkipHttpErrorCheck = $true
        ErrorAction        = 'Stop'
    }
    if ($PSBoundParameters.ContainsKey('Body') -and $null -ne $Body) { $splat.Body = $Body }
    if ($ContentType) { $splat.ContentType = $ContentType }
    if ($InFile)      { $splat.InFile      = $InFile }
    if ($OutFile)     { $splat.OutFile     = $OutFile; $splat.PassThru = $true }

    try {
        $response = Invoke-WebRequest @splat
        $status = if ($null -ne $response) { [int]$response.StatusCode } else { 0 }

        $content = ''
        if (-not $OutFile -and $null -ne $response.Content) {
            $content = if ($response.Content -is [byte[]]) {
                [Text.Encoding]::UTF8.GetString($response.Content)
            } else {
                [string]$response.Content
            }
        }

        $bytes = $null
        if ($Raw -and $null -ne $response -and $null -ne $response.RawContentStream) {
            $bytes = $response.RawContentStream.ToArray()
        }

        if ($status -ge 200 -and $status -lt 300) {
            return [pscustomobject]@{
                Ok         = $true
                StatusCode = $status
                Content    = $content
                Bytes      = $bytes
                Headers    = ConvertTo-NovaHeaderTable $response.Headers
                Detail     = ''
                Transport  = $false
            }
        }

        # -OutFile writes the error body to the file rather than returning it,
        # so recover the detail from disk and then discard the partial file.
        if ($OutFile -and -not $content -and (Test-Path -LiteralPath $OutFile)) {
            $content = Get-Content -LiteralPath $OutFile -Raw -ErrorAction SilentlyContinue
        }

        return [pscustomobject]@{
            Ok         = $false
            StatusCode = $status
            Content    = $content
            Bytes      = $null
            Headers    = ConvertTo-NovaHeaderTable $response.Headers
            Detail     = ConvertFrom-NovaErrorBody $content "HTTP $status"
            Transport  = $false
        }
    }
    catch {
        # With -SkipHttpErrorCheck the only things left to throw are genuine
        # connection-level failures: DNS, refused, TLS, timeout.
        return [pscustomobject]@{
            Ok         = $false
            StatusCode = 0
            Content    = ''
            Bytes      = $null
            Headers    = @{}
            Detail     = $_.Exception.Message
            Transport  = $true
        }
    }
}

function ConvertFrom-NovaJson {
    <#
        Returns PSCustomObject, matching the 5.1 script, so every caller below
        can use property access identically on both.
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
