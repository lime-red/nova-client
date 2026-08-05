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

    If you are on PowerShell 7 or later, use NovaClient-PS7.ps1 instead. The two
    scripts are deliberately identical below the HTTP TRANSPORT section so they can
    be diffed against each other; only that one region differs.

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

# ============================================================================
#  HTTP TRANSPORT  --  THIS SECTION DIFFERS BETWEEN THE 5.1 AND 7.x SCRIPTS
#  Everything below it is intended to stay textually identical in both files.
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

function ConvertFrom-NovaErrorBody {
    <#
        FastAPI returns {"detail": "..."} for raised errors and
        {"detail": [ {...}, ... ]} for 422 request-validation failures.
        Flatten both into one readable line.
    #>
    param([string] $Text, [string] $Fallback = '')

    $parsed = ConvertFrom-NovaJson $Text
    if ($null -eq $parsed) {
        if ([string]::IsNullOrWhiteSpace($Text)) { return $Fallback }
        return $Text.Trim()
    }

    $detail = $null
    if ($parsed.PSObject.Properties.Name -contains 'detail') { $detail = $parsed.detail }
    if ($null -eq $detail) { return $Fallback }

    if ($detail -is [string]) { return $detail }

    # 422: a list of {loc, msg, type} objects.
    $parts = foreach ($item in @($detail)) {
        if ($item -is [string]) {
            $item
        }
        else {
            $loc = ''
            if ($item.PSObject.Properties.Name -contains 'loc') { $loc = ($item.loc -join '.') }
            $msg = ''
            if ($item.PSObject.Properties.Name -contains 'msg') { $msg = $item.msg }
            if ($loc) { "$loc`: $msg" } else { $msg }
        }
    }
    return ($parts -join '; ')
}

# ============================================================================
#  Logging
# ============================================================================

$Script:Metrics = $null

function Write-NovaLog {
    <#
        Matches the Python client's line format exactly so log tooling and
        eyeballs treat both implementations the same:
            [2026-08-05 19:30:00] [INFO] message
    #>
    param(
        [ValidateSet('INFO', 'WARN', 'ERROR', 'DEBUG')][string] $Level,
        [string] $Message,
        [string] $League
    )

    if ($Level -eq 'DEBUG' -and $VerbosePreference -ne 'Continue') { return }

    $timestamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "[$timestamp] [$Level] $Message"

    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'DEBUG' { Write-Host $line -ForegroundColor DarkGray }
        default { Write-Host $line }
    }

    if ($Level -eq 'ERROR') {
        if ($null -ne $Script:Metrics) {
            $null = $Script:Metrics.errors.Add([ordered]@{
                time    = $timestamp
                message = $Message
                league  = $League
            })
        }
        if ($VerbosePreference -ne 'Continue') {
            Write-Host '  (use -Verbose to see more details)' -ForegroundColor DarkGray
        }
    }
}

function Write-NovaApiResponse {
    param([string] $Text, [string] $Context = '')
    if ($VerbosePreference -ne 'Continue') { return }
    $prefix = if ($Context) { "[API Response: $Context]" } else { '[API Response]' }
    $parsed = ConvertFrom-NovaJson $Text
    if ($null -ne $parsed) {
        Write-Host $prefix -ForegroundColor DarkGray
        Write-Host ($parsed | ConvertTo-Json -Depth 10) -ForegroundColor DarkGray
    }
    else {
        Write-Host "$prefix $Text" -ForegroundColor DarkGray
    }
}

# ============================================================================
#  Configuration
# ============================================================================

function Get-NovaSetting {
    <#
        Safe nested lookup with a default, so a missing optional key is never a
        crash. $Path is dotted, e.g. 'Sync.MaxRetries'.
    #>
    param([hashtable] $Cfg, [string] $Path, $Default = $null)

    $node = $Cfg
    foreach ($part in $Path.Split('.')) {
        if ($node -isnot [hashtable] -or -not $node.ContainsKey($part)) { return $Default }
        $node = $node[$part]
    }
    if ($null -eq $node) { return $Default }
    return $node
}

function Import-NovaConfig {
    param([string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Config file not found: $Path"
    }

    # Import-PowerShellDataFile parses the file as data only - it will not
    # execute code embedded in the config, unlike dot-sourcing a .ps1.
    $cfg = Import-PowerShellDataFile -LiteralPath (Resolve-Path -LiteralPath $Path)

    if ($cfg -isnot [hashtable]) {
        throw "Config file did not evaluate to a hashtable: $Path"
    }

    # Environment variables win over the file. This mirrors the Python client
    # and lets a service unit or Scheduled Task hold the secret instead of a
    # file sitting in the repo checkout.
    if ($env:HUB_CLIENT_ID)     { $cfg.Hub.ClientId     = $env:HUB_CLIENT_ID }
    if ($env:HUB_CLIENT_SECRET) { $cfg.Hub.ClientSecret = $env:HUB_CLIENT_SECRET }

    return $cfg
}

function Get-NovaLeagues {
    <#
        Normalises the Leagues array into objects with everything resolved:
        LeagueId ("555B"), BbsHex ("02"), and the directories.
    #>
    param([hashtable] $Cfg, [switch] $IncludeDisabled)

    $result = @()
    foreach ($league in @(Get-NovaSetting $Cfg 'Leagues' @())) {
        if ($league -isnot [hashtable]) { continue }

        $enabled = $true
        if ($league.ContainsKey('Enabled')) { $enabled = [bool]$league.Enabled }
        if (-not $enabled -and -not $IncludeDisabled) { continue }

        $game = ''
        if ($league.ContainsKey('Game')) { $game = [string]$league.Game }
        $number = ''
        if ($league.ContainsKey('Number')) { $number = [string]$league.Number }

        $bbsIndex = $null
        if ($league.ContainsKey('BbsIndex')) { $bbsIndex = $league.BbsIndex }

        $result += [pscustomobject]@{
            Game            = $game.ToUpperInvariant()
            Number          = $number
            Enabled         = $enabled
            BbsIndex        = $bbsIndex
            BbsHex          = if ($bbsIndex -is [int]) { '{0:X2}' -f $bbsIndex } else { $null }
            LeagueId        = if ($game -and $number) { '{0}{1}' -f $number, $game.ToUpperInvariant()[0] } else { $null }
            Key             = '{0}_{1}' -f $game.ToUpperInvariant(), $number
            OutboundDir     = Get-NovaSetting $league 'OutboundDir'
            InboundDir      = Get-NovaSetting $league 'InboundDir'
            GameFolder      = Get-NovaSetting $league 'GameFolder'
            GameCommand     = Get-NovaSetting $league 'GameCommand' $game.ToUpperInvariant()
            MaintenanceArgs = Get-NovaSetting $league 'MaintenanceArgs' 'PLANETARY'
        }
    }
    return $result
}

# ============================================================================
#  Filenames
#
#  These rules are security boundaries, not cosmetics. The Python client had
#  path-traversal and file-write vulnerabilities (fixed in c727a9f) that came
#  from trusting a filename returned by the server. Validate at every point
#  where a string becomes part of a path or a URL, even when the caller has
#  already validated it - the redundancy is the point.
# ============================================================================

$Script:PacketPattern   = '^[0-9]{3}[BF][0-9A-Fa-f]{4}\.[0-9]{3}$'
$Script:NodelistPattern = '^(BR|FE)NODES\.[0-9]{3}$'

function Get-SafeNovaFilename {
    <#
        Returns the validated filename, or throws. Any path separator at all is
        an outright rejection rather than something to strip - a filename that
        needed stripping was never legitimate.
    #>
    param([string] $Filename)

    if ([string]::IsNullOrEmpty($Filename)) { throw 'Empty filename' }

    $leaf = [IO.Path]::GetFileName($Filename)
    if ($leaf -ne $Filename) {
        throw "Path traversal attempt detected in filename: $Filename"
    }
    if ($leaf -in @('.', '..', '')) {
        throw "Invalid filename: $Filename"
    }
    if ($leaf -match '[\\/:*?"<>|]') {
        throw "Invalid characters in filename: $Filename"
    }
    if ($leaf -notmatch $Script:PacketPattern -and $leaf -notmatch $Script:NodelistPattern) {
        throw "Filename does not match expected format: $Filename"
    }
    return $leaf
}

function Test-NovaPacketFile {
    <#
        Is this local file an outbound packet for this league, from us?

        The source-index check is what stops a shared outbound folder from
        leaking another node's traffic under our credentials - and the hub
        rejects it with 403 anyway, so catching it here saves a round trip.
    #>
    param([string] $Filename, $League)

    $gameLetter = $League.Game.Substring(0, 1)
    $pattern = '^{0}{1}([0-9A-F]{{2}})[0-9A-F]{{2}}\.\d{{3}}$' -f
        [regex]::Escape($League.Number), [regex]::Escape($gameLetter)

    $match = [regex]::Match($Filename, $pattern, 'IgnoreCase')
    if (-not $match.Success) { return $false }

    return $match.Groups[1].Value.ToUpperInvariant() -eq $League.BbsHex
}

# ============================================================================
#  Hub API
# ============================================================================

$Script:Token = $null
$Script:TokenExpiresAt = [datetime]::MinValue

function Get-NovaJwtExpiry {
    <#
        Read 'exp' out of the JWT so we can refresh before the hub rejects us,
        rather than discovering expiry as a failed upload. Falls back to a
        conservative 15 minutes if the token is not a readable JWT.
    #>
    param([string] $Token)

    try {
        $parts = $Token.Split('.')
        if ($parts.Count -lt 2) { return (Get-Date).AddMinutes(15) }
        $payload = $parts[1].Replace('-', '+').Replace('_', '/')
        switch ($payload.Length % 4) {
            2 { $payload += '==' }
            3 { $payload += '=' }
        }
        $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload))
        $claims = ConvertFrom-NovaJson $json
        if ($null -ne $claims -and $claims.PSObject.Properties.Name -contains 'exp') {
            return ([datetimeoffset]::FromUnixTimeSeconds([long]$claims.exp)).LocalDateTime
        }
    }
    catch { }
    return (Get-Date).AddMinutes(15)
}

function Get-NovaToken {
    <#
        OAuth2 client_credentials. Credentials go in a form-urlencoded body -
        not Basic auth, not JSON.

        Unlike the Python client, the token is cached across sync cycles and
        refreshed a minute before it expires, so a long-running daemon is not
        re-authenticating every cycle.
    #>
    param([hashtable] $Cfg, [switch] $Force)

    if (-not $Force -and $Script:Token -and (Get-Date) -lt $Script:TokenExpiresAt.AddMinutes(-1)) {
        return $Script:Token
    }

    $url = '{0}/service/api/v1/auth/token' -f (Get-NovaSetting $Cfg 'Hub.Url').TrimEnd('/')
    $body = @{
        grant_type    = 'client_credentials'
        client_id     = Get-NovaSetting $Cfg 'Hub.ClientId' ''
        client_secret = Get-NovaSetting $Cfg 'Hub.ClientSecret' ''
    }

    $result = Invoke-NovaRequest -Method 'POST' -Uri $url -Body $body `
        -ContentType 'application/x-www-form-urlencoded' `
        -TimeoutSec (Get-NovaSetting $Cfg 'Sync.TimeoutSeconds' 120)

    if (-not $result.Ok) {
        if ($result.StatusCode -eq 429) {
            Write-NovaLog ERROR "Rate limited by hub on token request: $($result.Detail)"
        }
        else {
            Write-NovaLog ERROR "Token request failed ($($result.StatusCode)): $($result.Detail)"
        }
        Write-NovaApiResponse $result.Content 'token error'
        $Script:Token = $null
        return $null
    }

    $data = ConvertFrom-NovaJson $result.Content
    if ($null -eq $data -or -not $data.access_token) {
        Write-NovaLog ERROR 'Token response did not contain an access_token'
        return $null
    }

    $Script:Token = $data.access_token
    $Script:TokenExpiresAt = Get-NovaJwtExpiry $data.access_token
    Write-NovaLog INFO 'OAuth token obtained'
    if ($VerbosePreference -eq 'Continue') {
        # Never log the token itself.
        Write-NovaApiResponse (@{
            token_type   = $data.token_type
            access_token = '***masked***'
            expires_at   = $Script:TokenExpiresAt.ToString('s')
        } | ConvertTo-Json) 'token'
    }
    return $Script:Token
}

function Get-NovaAuthHeader {
    param([hashtable] $Cfg)
    $token = Get-NovaToken -Cfg $Cfg
    if (-not $token) { return $null }
    return @{ Authorization = "Bearer $token" }
}

function Invoke-NovaApi {
    <#
        Authenticated request with retry and a single automatic re-auth.

        Retry policy, deliberately different from the Python client: it only
        retried transport exceptions, so a 502 from a restarting hub failed the
        whole packet. Here transport failures, 5xx and 429 are retried; 4xx are
        not, because they are configuration errors (wrong BBS index, not a
        league member) that will fail identically forever.
    #>
    param(
        [hashtable] $Cfg,
        [string] $Method,
        [string] $Uri,
        [string] $InFile,
        [string] $OutFile,
        [string] $ContentType
    )

    $maxRetries = [int](Get-NovaSetting $Cfg 'Sync.MaxRetries' 3)
    $retryDelay = [int](Get-NovaSetting $Cfg 'Sync.RetryDelaySeconds' 5)
    $timeout    = [int](Get-NovaSetting $Cfg 'Sync.TimeoutSeconds' 120)
    $reauthed   = $false

    for ($attempt = 1; $attempt -le $maxRetries; $attempt++) {
        $headers = Get-NovaAuthHeader -Cfg $Cfg
        if ($null -eq $headers) {
            return [pscustomobject]@{ Ok = $false; StatusCode = 0; Content = ''; Detail = 'No auth token'; Transport = $true }
        }

        $splat = @{ Method = $Method; Uri = $Uri; Headers = $headers; TimeoutSec = $timeout }
        if ($InFile)      { $splat.InFile      = $InFile }
        if ($OutFile)     { $splat.OutFile     = $OutFile }
        if ($ContentType) { $splat.ContentType = $ContentType }

        $result = Invoke-NovaRequest @splat
        if ($result.Ok) { return $result }

        # An expired token looks like any other 401. Re-auth once, then give up.
        if ($result.StatusCode -eq 401 -and -not $reauthed) {
            Write-NovaLog DEBUG 'Token rejected (401), re-authenticating'
            $reauthed = $true
            $null = Get-NovaToken -Cfg $Cfg -Force
            continue
        }

        $retryable = $result.Transport -or $result.StatusCode -ge 500 -or $result.StatusCode -eq 429
        if (-not $retryable -or $attempt -eq $maxRetries) { return $result }

        Write-NovaLog DEBUG "Attempt $attempt/$maxRetries failed ($($result.StatusCode)), retrying in ${retryDelay}s"
        Start-Sleep -Seconds $retryDelay
    }

    return $result
}

function Send-NovaPacket {
    <#
        PUT the raw file bytes. This endpoint is not multipart and takes no form
        fields - league and filename come from the URL, and the hub computes the
        SHA-256 itself.
    #>
    param([hashtable] $Cfg, $League, [IO.FileInfo] $File)

    $safeName = Get-SafeNovaFilename $File.Name   # defense in depth

    # The hub rejects anything over 10 MiB with a 413. Catch it locally so the
    # message names the file instead of arriving as an HTTP error.
    $maxSize = 10MB
    if ($File.Length -gt $maxSize) {
        Write-NovaLog ERROR "$safeName is $([math]::Round($File.Length / 1MB, 1)) MB, over the 10 MB hub limit" $League.Key
        return $false
    }
    if ($File.Length -eq 0) {
        Write-NovaLog ERROR "$safeName is empty; the hub rejects empty packets" $League.Key
        return $false
    }

    $url = '{0}/service/api/v1/leagues/{1}/packets/{2}' -f
        (Get-NovaSetting $Cfg 'Hub.Url').TrimEnd('/'), $League.LeagueId, $safeName

    $result = Invoke-NovaApi -Cfg $Cfg -Method 'PUT' -Uri $url `
        -InFile $File.FullName -ContentType 'application/octet-stream'

    if ($result.Ok) {
        Write-NovaLog INFO "Uploaded $safeName" $League.Key
        Write-NovaApiResponse $result.Content "upload $safeName"
        return $true
    }

    # 403 on upload is always a configuration fault, and worth saying plainly
    # rather than leaving the sysop to decode it from the hub's message.
    if ($result.StatusCode -eq 403) {
        Write-NovaLog ERROR "Hub refused $safeName (403): $($result.Detail)" $League.Key
        Write-NovaLog ERROR "Check that BbsIndex $($League.BbsIndex) is correct for league $($League.LeagueId) and that this client is an active member." $League.Key
    }
    else {
        Write-NovaLog ERROR "Upload of $safeName failed ($($result.StatusCode)): $($result.Detail)" $League.Key
    }
    return $false
}

function Complete-NovaSentFile {
    param([hashtable] $Cfg, [IO.FileInfo] $File)

    $action = Get-NovaSetting $Cfg 'Sync.SentAction' 'delete'

    if ($action -eq 'archive') {
        $archiveDir = Get-NovaSetting $Cfg 'Sync.ArchiveDir' '.\sent'
        if (-not (Test-Path -LiteralPath $archiveDir)) {
            $null = New-Item -ItemType Directory -Path $archiveDir -Force
        }
        try { $safeName = Get-SafeNovaFilename $File.Name }
        catch { Write-NovaLog ERROR "Invalid filename for archive: $($_.Exception.Message)"; return }
        Move-Item -LiteralPath $File.FullName -Destination (Join-Path $archiveDir $safeName) -Force
        Write-NovaLog DEBUG "Archived: $safeName"
    }
    else {
        Remove-Item -LiteralPath $File.FullName -Force
        Write-NovaLog DEBUG "Deleted: $($File.Name)"
    }
}

function Get-NovaPacketList {
    param([hashtable] $Cfg, $League, [switch] $UnreadOnly)

    $url = '{0}/service/api/v1/leagues/{1}/packets' -f
        (Get-NovaSetting $Cfg 'Hub.Url').TrimEnd('/'), $League.LeagueId
    if ($UnreadOnly) { $url += '?unread=true' }

    $result = Invoke-NovaApi -Cfg $Cfg -Method 'GET' -Uri $url
    if (-not $result.Ok) {
        Write-NovaLog ERROR "Listing packets failed ($($result.StatusCode)): $($result.Detail)" $League.Key
        return @()
    }

    Write-NovaApiResponse $result.Content "list $($League.LeagueId)"
    $data = ConvertFrom-NovaJson $result.Content
    if ($null -eq $data -or $null -eq $data.packets) { return @() }
    return @($data.packets)
}

function Receive-NovaPacket {
    <#
        Downloads to <name>.part and only then renames into place. The GET is
        itself the acknowledgement - the hub marks the packet read the moment it
        serves it - so a crash between "server marked it read" and "file landed
        on disk" loses the packet permanently. A half-written file that the game
        then tries to parse is the worse of the two outcomes.
    #>
    param([hashtable] $Cfg, $League, [string] $Filename, [string] $DestinationDir)

    $safeName = Get-SafeNovaFilename $Filename
    $finalPath = Join-Path $DestinationDir $safeName
    $partPath = "$finalPath.part"

    $url = '{0}/service/api/v1/leagues/{1}/packets/{2}' -f
        (Get-NovaSetting $Cfg 'Hub.Url').TrimEnd('/'), $League.LeagueId, $safeName

    if (Test-Path -LiteralPath $partPath) { Remove-Item -LiteralPath $partPath -Force }

    $result = Invoke-NovaApi -Cfg $Cfg -Method 'GET' -Uri $url -OutFile $partPath

    if (-not $result.Ok) {
        if (Test-Path -LiteralPath $partPath) { Remove-Item -LiteralPath $partPath -Force }
        Write-NovaLog ERROR "Download of $safeName failed ($($result.StatusCode)): $($result.Detail)" $League.Key
        return $false
    }

    try {
        Move-Item -LiteralPath $partPath -Destination $finalPath -Force
    }
    catch {
        Write-NovaLog ERROR "Could not move $safeName into $DestinationDir : $($_.Exception.Message)" $League.Key
        return $false
    }

    $size = (Get-Item -LiteralPath $finalPath).Length
    Write-NovaLog INFO "Downloaded $safeName ($size bytes)" $League.Key
    return $true
}

function Receive-NovaNodelist {
    <#
        The hub derives the nodelist filename itself from the league, so we ask
        for it by league and write whatever it gives us into the game folder.
        Treat the body as an opaque blob - parsing it is the door game's job.
    #>
    param([hashtable] $Cfg, $League)

    if (-not $League.GameFolder) {
        Write-NovaLog DEBUG "No GameFolder for $($League.Key); skipping nodelist"
        return $false
    }
    if (-not (Test-Path -LiteralPath $League.GameFolder)) {
        Write-NovaLog WARN "GameFolder does not exist, skipping nodelist: $($League.GameFolder)"
        return $false
    }

    $prefix = if ($League.Game -eq 'BRE') { 'BRNODES' } else { 'FENODES' }
    $safeName = Get-SafeNovaFilename ('{0}.{1}' -f $prefix, $League.Number)

    $url = '{0}/service/api/v1/leagues/{1}/nodelist' -f
        (Get-NovaSetting $Cfg 'Hub.Url').TrimEnd('/'), $League.LeagueId

    $finalPath = Join-Path $League.GameFolder $safeName
    $partPath = "$finalPath.part"
    if (Test-Path -LiteralPath $partPath) { Remove-Item -LiteralPath $partPath -Force }

    $result = Invoke-NovaApi -Cfg $Cfg -Method 'GET' -Uri $url -OutFile $partPath

    if (-not $result.Ok) {
        if (Test-Path -LiteralPath $partPath) { Remove-Item -LiteralPath $partPath -Force }
        # 404 just means the hub has not generated one yet - not an error.
        if ($result.StatusCode -eq 404) {
            Write-NovaLog DEBUG "No nodelist available yet for $($League.LeagueId)"
        }
        else {
            Write-NovaLog WARN "Nodelist download failed ($($result.StatusCode)): $($result.Detail)"
        }
        return $false
    }

    Move-Item -LiteralPath $partPath -Destination $finalPath -Force
    Write-NovaLog INFO "Updated nodelist $safeName" $League.Key
    return $true
}

# ============================================================================
#  Sync
# ============================================================================

function Invoke-NovaLeagueUpload {
    param([hashtable] $Cfg, $League)

    if (-not (Test-Path -LiteralPath $League.OutboundDir)) {
        Write-NovaLog WARN "Outbound directory not found: $($League.OutboundDir)"
        return 0
    }

    $uploaded = 0
    $candidates = Get-ChildItem -LiteralPath $League.OutboundDir -File -ErrorAction SilentlyContinue
    foreach ($file in $candidates) {
        if (-not (Test-NovaPacketFile -Filename $file.Name -League $League)) { continue }

        if (Send-NovaPacket -Cfg $Cfg -League $League -File $file) {
            # Only once the hub has confirmed receipt do we let go of the file.
            # A failed upload leaves it in place to be retried next cycle -
            # that is the whole retry mechanism, there is no sent-ledger.
            Complete-NovaSentFile -Cfg $Cfg -File $file
            $uploaded++
        }
        else {
            $Script:Metrics.leagues[$League.Key].errors++
        }
    }
    return $uploaded
}

function Invoke-NovaLeagueDownload {
    param([hashtable] $Cfg, $League)

    $packets = Get-NovaPacketList -Cfg $Cfg -League $League -UnreadOnly
    if ($packets.Count -eq 0) {
        Write-NovaLog DEBUG "No inbound packets for $($League.LeagueId)"
        return 0
    }

    if (-not (Test-Path -LiteralPath $League.InboundDir)) {
        $null = New-Item -ItemType Directory -Path $League.InboundDir -Force
    }

    $downloaded = 0
    foreach ($packet in $packets) {
        # The server controls this string; validate before it touches a path.
        try { $safeName = Get-SafeNovaFilename $packet.filename }
        catch {
            Write-NovaLog ERROR "Rejected filename from hub: $($_.Exception.Message)" $League.Key
            $Script:Metrics.leagues[$League.Key].errors++
            continue
        }

        if (Receive-NovaPacket -Cfg $Cfg -League $League -Filename $safeName -DestinationDir $League.InboundDir) {
            $downloaded++
        }
        else {
            $Script:Metrics.leagues[$League.Key].errors++
        }
    }
    return $downloaded
}

function Invoke-NovaSync {
    <#
        One full sync pass over every enabled league.
        Returns an object with Uploaded / Downloaded / Errors.
    #>
    param([hashtable] $Cfg)

    $Script:Metrics = [ordered]@{
        start_time       = (Get-Date).ToString('o')
        end_time         = $null
        leagues          = @{}
        total_uploaded   = 0
        total_downloaded = 0
        errors           = [Collections.ArrayList]::new()
        success          = $true
    }

    Write-NovaLog INFO "Nova Hub Client $Script:NovaClientVersion starting"
    Write-NovaLog INFO "BBS: $(Get-NovaSetting $Cfg 'Bbs.Name' '(unnamed)')"
    Write-NovaLog INFO "Hub: $(Get-NovaSetting $Cfg 'Hub.Url')"

    if ($null -eq (Get-NovaToken -Cfg $Cfg -Force)) {
        Write-NovaLog ERROR 'Authentication failed; no leagues will be synced'
        return Complete-NovaRun -Cfg $Cfg
    }

    foreach ($league in (Get-NovaLeagues -Cfg $Cfg)) {
        $Script:Metrics.leagues[$league.Key] = [ordered]@{
            game_type     = $league.Game
            league_number = $league.Number
            uploaded      = 0
            downloaded    = 0
            errors        = 0
        }

        Write-NovaLog INFO "--- $($league.Game) league $($league.Number) (BBS #$($league.BbsIndex)) ---"

        # Upload first, always. Flushing our own outbound traffic before pulling
        # new inbound work means a slow download never delays our own replies.
        $up = Invoke-NovaLeagueUpload -Cfg $Cfg -League $league
        $Script:Metrics.leagues[$league.Key].uploaded = $up
        $Script:Metrics.total_uploaded += $up

        $down = Invoke-NovaLeagueDownload -Cfg $Cfg -League $league
        $Script:Metrics.leagues[$league.Key].downloaded = $down
        $Script:Metrics.total_downloaded += $down

        $null = Receive-NovaNodelist -Cfg $Cfg -League $league
    }

    return Complete-NovaRun -Cfg $Cfg
}

function Complete-NovaRun {
    param([hashtable] $Cfg)

    $Script:Metrics.end_time = (Get-Date).ToString('o')
    $Script:Metrics.success = ($Script:Metrics.errors.Count -eq 0)

    $metricsFile = Get-NovaSetting $Cfg 'Sync.MetricsFile' 'metrics.json'
    try {
        $Script:Metrics | ConvertTo-Json -Depth 10 |
            Set-Content -LiteralPath $metricsFile -Encoding UTF8
    }
    catch {
        Write-NovaLog WARN "Could not write metrics to $metricsFile : $($_.Exception.Message)"
    }

    Write-NovaLog INFO '=== Run Summary ==='
    Write-NovaLog INFO "Total Uploaded: $($Script:Metrics.total_uploaded)"
    Write-NovaLog INFO "Total Downloaded: $($Script:Metrics.total_downloaded)"
    Write-NovaLog INFO "Total Errors: $($Script:Metrics.errors.Count)"

    return [pscustomobject]@{
        Uploaded   = $Script:Metrics.total_uploaded
        Downloaded = $Script:Metrics.total_downloaded
        Errors     = $Script:Metrics.errors.Count
    }
}

# ============================================================================
#  Game maintenance
# ============================================================================

function Invoke-NovaGameMaintenance {
    <#
        Runs e.g. "BRE PLANETARY" in the game folder.

        Do not "improve" this by adding -NoNewWindow or -RedirectStandardOutput.
        BRE and FE are 16-bit-era DOS console programs and need a real console;
        the Python client burned three commits discovering that CREATE_NO_WINDOW
        and redirected stdio both produce WinError 87 (00bf36a, d5382a7, 6f7ef28).
        Launching through cmd.exe with its own console is what actually works.
        The console flash is expected, not a bug to suppress.

        The game must run with the game folder as its working directory; it
        cannot be invoked by full path.
    #>
    param($League, [int] $TimeoutSeconds = 300)

    if (-not $League.GameFolder) {
        Write-NovaLog WARN "No GameFolder configured for $($League.Key); skipping maintenance"
        return $false
    }
    if (-not (Test-Path -LiteralPath $League.GameFolder)) {
        Write-NovaLog ERROR "GameFolder does not exist: $($League.GameFolder)" $League.Key
        return $false
    }

    $command = '{0} {1}' -f $League.GameCommand, $League.MaintenanceArgs
    Write-NovaLog INFO "Running maintenance: $command (in $($League.GameFolder))" $League.Key

    $process = $null
    try {
        $process = Start-Process -FilePath $env:ComSpec `
            -ArgumentList '/c', $command `
            -WorkingDirectory $League.GameFolder `
            -PassThru
    }
    catch {
        Write-NovaLog ERROR "Could not start $command : $($_.Exception.Message)" $League.Key
        return $false
    }

    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Write-NovaLog ERROR "Maintenance timed out after ${TimeoutSeconds}s; killing process tree" $League.Key
        # /T is the important part: cmd.exe is the child, the game is the
        # grandchild, and killing only the direct child leaves the game running
        # and holding its data files open.
        & taskkill.exe /T /F /PID $process.Id 2>&1 | Out-Null
        return $false
    }

    $process.Refresh()
    $exitCode = $process.ExitCode
    if ($exitCode -eq 0) {
        Write-NovaLog INFO "Maintenance complete: $command" $League.Key
        return $true
    }

    Write-NovaLog ERROR "Maintenance failed: $command exited $exitCode" $League.Key
    return $false
}

function Invoke-NovaAllMaintenance {
    param([hashtable] $Cfg)

    $timeout = [int](Get-NovaSetting $Cfg 'Daemon.MaintenanceTimeoutSeconds' 300)
    $ran = 0
    foreach ($league in (Get-NovaLeagues -Cfg $Cfg)) {
        if (-not $league.GameFolder) { continue }
        if (Invoke-NovaGameMaintenance -League $league -TimeoutSeconds $timeout) { $ran++ }
    }
    return $ran
}

# ============================================================================
#  Validation
# ============================================================================

function Test-NovaConfig {
    <#
        Collects every problem and reports them together, rather than stopping
        at the first. A sysop setting this up for the first time should get one
        complete list, not six runs.
    #>
    param([hashtable] $Cfg)

    $problems = [Collections.ArrayList]::new()
    $warnings = [Collections.ArrayList]::new()

    if (-not (Get-NovaSetting $Cfg 'Hub.Url'))       { $null = $problems.Add('Hub.Url is not set') }
    if (-not (Get-NovaSetting $Cfg 'Hub.ClientId'))  { $null = $problems.Add('Hub.ClientId is not set (or $env:HUB_CLIENT_ID)') }
    if (-not (Get-NovaSetting $Cfg 'Hub.ClientSecret')) { $null = $problems.Add('Hub.ClientSecret is not set (or $env:HUB_CLIENT_SECRET)') }

    $bbsName = Get-NovaSetting $Cfg 'Bbs.Name'
    if (-not $bbsName) { $null = $problems.Add('Bbs.Name is not set') }

    $leagues = Get-NovaLeagues -Cfg $Cfg
    if ($leagues.Count -eq 0) { $null = $warnings.Add('No enabled leagues configured') }

    # Every directory must be used by exactly one league. Two leagues sharing an
    # inbound folder is the classic copy-paste config error, and it silently
    # cross-delivers packets between games.
    $dirUsage = @{}

    foreach ($league in $leagues) {
        $label = $league.Key

        if (-not $league.Game -or $league.Game -notin @('BRE', 'FE')) {
            $null = $problems.Add("${label}: Game must be 'BRE' or 'FE'")
        }
        if ($league.Number -notmatch '^\d{3}$') {
            $null = $problems.Add("${label}: Number must be exactly 3 digits (e.g. '015')")
        }
        if ($league.BbsIndex -isnot [int]) {
            $null = $problems.Add("${label}: BbsIndex is required and must be a number")
        }
        elseif ($league.BbsIndex -lt 1 -or $league.BbsIndex -gt 255) {
            $null = $problems.Add("${label}: BbsIndex must be between 1 and 255 (got $($league.BbsIndex))")
        }

        foreach ($pair in @(
                @{ Name = 'OutboundDir'; Value = $league.OutboundDir; Required = $true },
                @{ Name = 'InboundDir';  Value = $league.InboundDir;  Required = $true },
                @{ Name = 'GameFolder';  Value = $league.GameFolder;  Required = $false })) {

            if (-not $pair.Value) {
                if ($pair.Required) { $null = $problems.Add("${label}: $($pair.Name) is required") }
                continue
            }
            if (-not (Test-Path -LiteralPath $pair.Value -PathType Container)) {
                $null = $problems.Add("${label}: $($pair.Name) does not exist: $($pair.Value)")
                continue
            }

            $resolved = (Resolve-Path -LiteralPath $pair.Value).Path.ToLowerInvariant()
            if (-not $dirUsage.ContainsKey($resolved)) { $dirUsage[$resolved] = @() }
            $dirUsage[$resolved] += "${label}.$($pair.Name)"
        }

        if ($league.GameFolder -and (Test-Path -LiteralPath $league.GameFolder)) {
            Test-NovaNodesFile -League $league -BbsName $bbsName -Problems $problems -Warnings $warnings
        }
        else {
            $null = $warnings.Add("${label}: no GameFolder, so -Daemon cannot run maintenance for this league")
        }
    }

    foreach ($dir in $dirUsage.Keys) {
        if ($dirUsage[$dir].Count -gt 1) {
            $null = $problems.Add("Directory used by more than one league entry: $dir  <- $($dirUsage[$dir] -join ', ')")
        }
    }

    return [pscustomobject]@{ Problems = $problems; Warnings = $warnings }
}

function Test-NovaNodesFile {
    <#
        Cross-checks the game's own nodes.dat against the config. Catches the
        two classic misconfigurations: pointing at the wrong BBS index, and the
        BBS name in the config having drifted from the one in nodes.dat.

        The file is found case-insensitively because these are DOS-era files
        and the casing on disk is anyone's guess.
    #>
    param($League, [string] $BbsName, $Problems, $Warnings)

    $label = $League.Key
    $expected = if ($League.Game -eq 'BRE') { 'brnodes.dat' } else { 'fenodes.dat' }

    $nodesFile = Get-ChildItem -LiteralPath $League.GameFolder -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ieq $expected } | Select-Object -First 1

    if (-not $nodesFile) {
        $null = $Warnings.Add("${label}: $expected not found in $($League.GameFolder)")
        return
    }

    # Six lines per entry - index, name, FidoNet address, city, state, country -
    # separated by a blank line.
    $lines = Get-Content -LiteralPath $nodesFile.FullName -ErrorAction SilentlyContinue
    $entries = @{}
    $buffer = @()
    foreach ($line in @($lines) + @('')) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            if ($buffer.Count -ge 2) {
                $index = 0
                if ([int]::TryParse($buffer[0].Trim(), [ref]$index)) {
                    if ($entries.ContainsKey($index)) {
                        $null = $Problems.Add("${label}: duplicate BBS index $index in $($nodesFile.Name)")
                    }
                    else {
                        $entries[$index] = $buffer[1].Trim()
                    }
                }
            }
            $buffer = @()
            continue
        }
        $buffer += $line
    }

    if (-not $entries.ContainsKey([int]$League.BbsIndex)) {
        $null = $Problems.Add("${label}: BbsIndex $($League.BbsIndex) has no entry in $($nodesFile.Name)")
        return
    }

    $nameInFile = $entries[[int]$League.BbsIndex]
    if ($BbsName -and $nameInFile -and ($nameInFile.Trim() -ine $BbsName.Trim())) {
        $null = $Problems.Add("${label}: Bbs.Name '$BbsName' does not match '$nameInFile' at index $($League.BbsIndex) in $($nodesFile.Name)")
    }
}

function Show-NovaValidation {
    param($Result)

    foreach ($warning in $Result.Warnings) { Write-NovaLog WARN $warning }

    if ($Result.Problems.Count -eq 0) {
        Write-NovaLog INFO 'Configuration is valid'
        return $true
    }

    Write-NovaLog ERROR "Configuration has $($Result.Problems.Count) problem(s):"
    foreach ($problem in $Result.Problems) { Write-Host "  - $problem" -ForegroundColor Red }
    return $false
}

# ============================================================================
#  Daemon
# ============================================================================

$Script:ShutdownRequested = $false

function Register-NovaShutdownHandler {
    try {
        $handler = [ConsoleCancelEventHandler] {
            param($eventSender, $eventArgs)
            $eventArgs.Cancel = $true          # don't let .NET kill us mid-write
            $Script:ShutdownRequested = $true
            Write-Host ''
            Write-Host 'Shutdown requested, finishing current cycle...' -ForegroundColor Yellow
        }
        [Console]::add_CancelKeyPress($handler)
    }
    catch {
        # No console (e.g. running as a Scheduled Task) - nothing to hook.
        Write-NovaLog DEBUG 'No console available for Ctrl+C handling'
    }
}

function Wait-NovaInterval {
    <#
        Sleeps in one-second slices so a Ctrl+C is noticed immediately rather
        than at the end of a two-minute nap.
    #>
    param([int] $Seconds)
    for ($i = 0; $i -lt $Seconds; $i++) {
        if ($Script:ShutdownRequested) { return }
        Start-Sleep -Seconds 1
    }
}

function Start-NovaDaemon {
    param([hashtable] $Cfg)

    $syncInterval = [int](Get-NovaSetting $Cfg 'Daemon.SyncIntervalSeconds' 120)
    $maintInterval = [int](Get-NovaSetting $Cfg 'Daemon.MaintenanceIntervalSeconds' 600)
    $maintOnDownload = [bool](Get-NovaSetting $Cfg 'Daemon.RunMaintenanceOnDownload' $true)

    Write-NovaLog INFO "Nova Client daemon $Script:NovaClientVersion starting"
    Write-NovaLog INFO "Sync every ${syncInterval}s, maintenance every ${maintInterval}s"
    Write-NovaLog INFO "Run maintenance on download: $maintOnDownload"
    Write-NovaLog INFO 'Press Ctrl+C to stop'

    Register-NovaShutdownHandler

    $stats = [ordered]@{
        start_time         = Get-Date
        sync_count         = 0
        maintenance_count  = 0
        packets_uploaded   = 0
        packets_downloaded = 0
        errors             = 0
    }

    $lastSync = [datetime]::MinValue
    $lastMaintenance = [datetime]::MinValue

    while (-not $Script:ShutdownRequested) {
        try {
            $now = Get-Date

            if (($now - $lastSync).TotalSeconds -ge $syncInterval) {
                $result = Invoke-NovaSync -Cfg $Cfg
                $lastSync = Get-Date
                $stats.sync_count++
                $stats.packets_uploaded += $result.Uploaded
                $stats.packets_downloaded += $result.Downloaded
                $stats.errors += $result.Errors

                if ($result.Downloaded -gt 0 -and $maintOnDownload) {
                    Write-NovaLog INFO "$($result.Downloaded) packet(s) arrived; running maintenance now"
                    $stats.maintenance_count += Invoke-NovaAllMaintenance -Cfg $Cfg
                    # Reset the scheduled clock too, so we don't immediately run
                    # a second time for the same batch of packets.
                    $lastMaintenance = Get-Date
                }
            }
            # Never run maintenance before the first sync has happened - on a
            # cold start there is nothing to process yet.
            elseif ($lastSync -ne [datetime]::MinValue -and
                    ($now - $lastMaintenance).TotalSeconds -ge $maintInterval) {
                $stats.maintenance_count += Invoke-NovaAllMaintenance -Cfg $Cfg
                $lastMaintenance = Get-Date
            }

            if ($Script:ShutdownRequested) { break }

            # Wake for whichever is due first, but check in at least every 10s.
            # Recomputing from actual elapsed time rather than trusting the
            # timer means a laptop resuming from sleep doesn't skip a cycle.
            $syncWait = $syncInterval - ((Get-Date) - $lastSync).TotalSeconds
            $maintWait = if ($lastSync -eq [datetime]::MinValue) { [double]::MaxValue }
                         else { $maintInterval - ((Get-Date) - $lastMaintenance).TotalSeconds }
            $wait = [Math]::Max(1, [Math]::Min([Math]::Min($syncWait, $maintWait), 10))
            Wait-NovaInterval -Seconds ([int]$wait)
        }
        catch {
            # A daemon that dies on a transient error is worse than useless -
            # it stops delivering packets and nobody notices until the league
            # complains. Log, count it, and carry on.
            Write-NovaLog ERROR "Daemon cycle error: $($_.Exception.Message)"
            if ($VerbosePreference -eq 'Continue') { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
            $stats.errors++
            Start-Sleep -Seconds 5
        }
    }

    $uptime = (Get-Date) - $stats.start_time
    Write-NovaLog INFO '=== Daemon stopped ==='
    Write-NovaLog INFO ("Uptime: {0:dd\.hh\:mm\:ss}" -f $uptime)
    Write-NovaLog INFO "Syncs: $($stats.sync_count)  Maintenance runs: $($stats.maintenance_count)"
    Write-NovaLog INFO "Uploaded: $($stats.packets_uploaded)  Downloaded: $($stats.packets_downloaded)  Errors: $($stats.errors)"

    return $(if ($stats.errors -gt 0) { 1 } else { 0 })
}

# ============================================================================
#  Entry point
# ============================================================================

function Invoke-NovaMain {
    if ($ShowVersion) {
        Write-Host "nova-client (PowerShell 5.1) $Script:NovaClientVersion"
        return 0
    }

    try {
        $cfg = Import-NovaConfig -Path $Config
    }
    catch {
        Write-NovaLog ERROR $_.Exception.Message
        return 2
    }

    $validation = Test-NovaConfig -Cfg $cfg
    if ($Validate) {
        return $(if (Show-NovaValidation $validation) { 0 } else { 2 })
    }

    # Directories that don't exist and credentials that aren't set are worth
    # failing on before we touch the network, the same way the Python client
    # validates at construction time.
    if ($validation.Problems.Count -gt 0) {
        $null = Show-NovaValidation $validation
        Write-NovaLog ERROR 'Refusing to run with an invalid configuration (use -Validate for detail)'
        return 2
    }
    foreach ($warning in $validation.Warnings) { Write-NovaLog WARN $warning }

    # One instance per config file. The Python client has no such guard, and two
    # daemons against one config will both claim the same packets and both try
    # to run the game in the same folder.
    $configKey = (Resolve-Path -LiteralPath $Config).Path.ToLowerInvariant()
    $hash = [BitConverter]::ToString(
        [Security.Cryptography.MD5]::Create().ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($configKey))).Replace('-', '')
    $mutex = $null
    $acquired = $false
    try {
        try   { $mutex = New-Object System.Threading.Mutex($false, "Global\NovaClient-$hash") }
        catch { $mutex = New-Object System.Threading.Mutex($false, "Local\NovaClient-$hash") }

        try { $acquired = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }

        if (-not $acquired) {
            Write-NovaLog ERROR "Another Nova Client is already running with this config: $Config"
            return 3
        }

        if ($Daemon) { return Start-NovaDaemon -Cfg $cfg }

        $result = Invoke-NovaSync -Cfg $cfg
        return $(if ($result.Errors -gt 0) { 1 } else { 0 })
    }
    finally {
        if ($mutex) {
            if ($acquired) { $mutex.ReleaseMutex() }
            $mutex.Dispose()
        }
        $ProgressPreference = $Script:OriginalProgressPreference
    }
}

if ($Daemon -and $Once) {
    Write-NovaLog ERROR 'Specify only one of -Once or -Daemon'
    exit 2
}

try {
    exit (Invoke-NovaMain)
}
catch {
    Write-NovaLog ERROR "Unhandled error: $($_.Exception.Message)"
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    exit 1
}
