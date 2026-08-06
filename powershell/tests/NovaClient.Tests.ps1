#Requires -Modules Pester
<#
    Integration tests for the PowerShell Nova Client.

    These run the real script as a child process against the Python mock hub in
    python/tests/mock_server.py - the same mock the Python client's own tests
    use, so both implementations are held to one definition of the API.

    Run:
        Invoke-Pester -Path powershell/tests

    A target is only included if its interpreter is present, so on Linux only
    NovaClient-PS7.ps1 is exercised - that is what CI covers. Run this on the
    Windows BBS box to cover NovaClient-WinPS5.ps1 as well.
#>

# --- Discovery scope ---------------------------------------------------------
# Pester evaluates `Describe -ForEach` during discovery, which happens BEFORE
# any BeforeAll runs. The target list therefore has to be built out here; if it
# is built inside BeforeAll the -ForEach sees $null and every case runs once
# with an empty $_.
$RepoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$PsDir    = Join-Path $RepoRoot 'powershell'

# A target is only included if its interpreter actually exists here, rather
# than being included and skipped: Pester's -Skip is awkward to drive from a
# -ForEach item, and an absent shell is not a skipped test, it is a target that
# does not apply to this machine. Windows PowerShell 5.1 exists only on Windows.
$Targets = @()
if (Get-Command pwsh -ErrorAction SilentlyContinue) {
    $Targets += @{
        Name   = 'PS7'
        Shell  = 'pwsh'
        Script = Join-Path $PsDir 'NovaClient-PS7.ps1'
    }
}
if (Get-Command powershell.exe -ErrorAction SilentlyContinue) {
    $Targets += @{
        Name   = 'WinPS5'
        Shell  = 'powershell.exe'
        Script = Join-Path $PsDir 'NovaClient-WinPS5.ps1'
    }
}
if ($Targets.Count -eq 0) { throw 'No PowerShell interpreter found to test against.' }

BeforeAll {
    $Script:RepoRoot   = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
    $Script:PsDir      = Join-Path $Script:RepoRoot 'powershell'
    # The seeded wrapper around python/tests/mock_server.py: same mock, plus
    # an inbound packet and a nodelist already waiting for us.
    $Script:MockServer = Join-Path $Script:PsDir 'tests/seeded_hub.py'
    $Script:ExpectedPacket = Join-Path $Script:PsDir 'tests/expected_packet.bin'
    $Script:HubUrl     = 'http://127.0.0.1:8000'

    function Start-MockHub {
        [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '')]
        [CmdletBinding()] param()

        # Prefer a virtualenv interpreter: the mock hub needs fastapi and
        # uvicorn, which the system python almost certainly does not have.
        $candidates = @(
            (Join-Path $Script:RepoRoot '.venv/bin/python')
            (Join-Path $Script:RepoRoot 'python/.venv/bin/python')
            (Join-Path $Script:RepoRoot '.venv/Scripts/python.exe')
            (Join-Path $Script:RepoRoot 'python/.venv/Scripts/python.exe')
        )
        $python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $python) {
            $python = if (Get-Command python3 -ErrorAction SilentlyContinue) { 'python3' } else { 'python' }
        }

        $proc = Start-Process -FilePath $python -ArgumentList $Script:MockServer `
            -PassThru -RedirectStandardOutput (Join-Path ([IO.Path]::GetTempPath()) 'mockhub.out') `
            -RedirectStandardError (Join-Path ([IO.Path]::GetTempPath()) 'mockhub.err')

        # Wait for it to answer, rather than sleeping a fixed amount and hoping.
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            try {
                $null = Invoke-RestMethod -Uri "$Script:HubUrl/health" -TimeoutSec 2
                return $proc
            }
            catch { Start-Sleep -Milliseconds 250 }
        }
        $hint = Get-Content (Join-Path ([IO.Path]::GetTempPath()) 'mockhub.err') -Raw -ErrorAction SilentlyContinue
        throw "Mock hub did not become ready within 30s (using '$python').`n$hint"
    }

    function New-TestWorkspace {
        [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '')]
        [CmdletBinding()]
        <#
            Builds a throwaway tree with outbound/inbound/game dirs, a nodes.dat
            that agrees with the config, and a config.psd1 pointing at all of it.
        #>
        param([int] $BbsIndex = 2, [string] $BbsName = 'Test BBS')

        $root = Join-Path ([IO.Path]::GetTempPath()) ("nova-test-" + [guid]::NewGuid().ToString('N'))
        $outbound = Join-Path $root 'outbound'
        $inbound  = Join-Path $root 'inbound'
        $game     = Join-Path $root 'game'
        $archive  = Join-Path $root 'sent'
        foreach ($dir in @($root, $outbound, $inbound, $game, $archive)) {
            $null = New-Item -ItemType Directory -Path $dir -Force
        }

        # 6 lines per entry then a blank line: index, name, fido, city, state, country.
        # The hub's own entry leads with "1 HOST ..." routing info, exactly as a
        # real nodelist does - the fixture used to omit it, which is why the
        # parser could drop that line unnoticed.
        @(
            '1 HOST 2 3 4', 'Nova Hub', '135:1/1', 'Brisbane', 'QLD', 'AUS', ''
            "$BbsIndex", $BbsName, '1:2/3', 'Somewhere', 'Somestate', 'Somewhere', ''
        ) | Set-Content -LiteralPath (Join-Path $game 'BRNODES.DAT')

        $configPath = Join-Path $root 'config.psd1'
        @"
@{
    Hub = @{
        Url          = '$Script:HubUrl'
        ClientId     = 'test_client'
        ClientSecret = 'test_secret'
    }
    Bbs  = @{ Name = '$BbsName' }
    Sync = @{
        SentAction        = 'archive'
        ArchiveDir        = '$($archive.Replace('\','\\'))'
        MaxRetries        = 2
        RetryDelaySeconds = 1
        TimeoutSeconds    = 15
        MetricsFile       = '$((Join-Path $root 'metrics.json').Replace('\','\\'))'
    }
    Daemon = @{
        SyncIntervalSeconds        = 5
        MaintenanceIntervalSeconds = 3600
        MaintenanceTimeoutSeconds  = 30
        RunMaintenanceOnDownload   = `$false
    }
    Leagues = @(
        @{
            Game        = 'BRE'
            Number      = '555'
            Enabled     = `$true
            BbsIndex    = $BbsIndex
            OutboundDir = '$($outbound.Replace('\','\\'))'
            InboundDir  = '$($inbound.Replace('\','\\'))'
            GameFolder  = '$($game.Replace('\','\\'))'
        }
    )
}
"@ | Set-Content -LiteralPath $configPath -Encoding UTF8

        return [pscustomobject]@{
            Root = $root; Config = $configPath; Outbound = $outbound
            Inbound = $inbound; Game = $game; Archive = $archive
            Metrics = Join-Path $root 'metrics.json'
        }
    }

    function Set-NodelistCheck {
        <#
            The default is 'bootstrap', which fetches a nodelist only when there
            is none locally - so the caching tests have to opt into 'always' to
            have anything to cache.
        #>
        [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '')]
        [CmdletBinding()]
        param([string] $ConfigPath, [string] $Mode)

        (Get-Content -LiteralPath $ConfigPath -Raw).Replace(
            'MetricsFile       =', "NodelistCheck     = '$Mode'`n        MetricsFile       =") |
            Set-Content -LiteralPath $ConfigPath
    }

    function Invoke-Client {
        param($Target, [string] $ConfigPath, [string[]] $ClientArgs)
        $all = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Target.Script,
                 '-Config', $ConfigPath) + $ClientArgs
        $output = & $Target.Shell @all 2>&1
        return [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output   = ($output | Out-String)
        }
    }

    $Script:MockProc = Start-MockHub
}

AfterAll {
    if ($Script:MockProc -and -not $Script:MockProc.HasExited) {
        Stop-Process -Id $Script:MockProc.Id -Force -ErrorAction SilentlyContinue
    }
}

Describe 'Static analysis' {
    It 'both scripts parse without syntax errors' {
        foreach ($name in @('NovaClient-WinPS5.ps1', 'NovaClient-PS7.ps1',
                            'NovaClient.Common.ps1', 'Install-NovaClientTask.ps1')) {
            $path = Join-Path $Script:PsDir $name
            $errors = $null
            $null = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errors)
            $errors | Should -BeNullOrEmpty -Because "$name should parse cleanly"
        }
    }

    It 'both launchers dot-source the shared implementation' {
        # The shared code used to be duplicated in both files and kept in sync
        # by a diff test. Now there is one copy; this asserts it stays wired up,
        # and that neither launcher has quietly grown its own logic again.
        foreach ($name in @('NovaClient-WinPS5.ps1', 'NovaClient-PS7.ps1')) {
            $path = Join-Path $Script:PsDir $name
            $text = Get-Content -LiteralPath $path -Raw
            $text | Should -Match '\.\s+\$commonPath' -Because "$name should dot-source the common file"

            # A launcher is a transport plus a few lines of wiring. If one grows
            # past this, shared logic has probably leaked back into it.
            (Get-Content -LiteralPath $path).Count |
                Should -BeLessThan 300 -Because "$name should hold transport only"
        }
    }

    It 'the shared implementation stays compatible with Windows PowerShell 5.1' {
        # NovaClient.Common.ps1 runs under both hosts, and CI runs on Linux
        # where only pwsh exists - so a 7-only construct would sail through
        # every other test here and fail on the Windows BBS box instead.
        # PSUseCompatibleSyntax parses against the 5.1 grammar to catch it.
        if (-not (Get-Module -ListAvailable PSScriptAnalyzer)) {
            Set-ItResult -Skipped -Because 'PSScriptAnalyzer is not installed'
            return
        }

        $settings = @{
            IncludeRules = @('PSUseCompatibleSyntax')
            Rules        = @{
                PSUseCompatibleSyntax = @{
                    Enable         = $true
                    TargetVersions = @('5.1', '7.0')
                }
            }
        }
        $found = Invoke-ScriptAnalyzer `
            -Path (Join-Path $Script:PsDir 'NovaClient.Common.ps1') -Settings $settings

        # Read the rule text if this fails: it names the construct and the line.
        # Either rewrite it in 5.1-compatible form, or fork the file - see the
        # rule documented at the top of NovaClient.Common.ps1.
        $found | Should -BeNullOrEmpty -Because (
            'shared code must run on 5.1: ' + (($found | ForEach-Object {
                "line $($_.Line): $($_.Message)" }) -join '; '))
    }

    It 'the config example is valid PowerShell data' {
        $example = Join-Path $Script:PsDir 'config.psd1.example'
        $cfg = Import-PowerShellDataFile -LiteralPath $example
        $cfg.Hub | Should -Not -BeNullOrEmpty
        $cfg.Leagues | Should -Not -BeNullOrEmpty
        $cfg.Leagues[0].BbsIndex | Should -BeOfType [int]
    }
}

Describe 'Nova Client <_.Name>' -ForEach $Targets {

    BeforeEach {
        $Script:Ws = New-TestWorkspace
        # Downloading marks a packet read hub-side, so every test starts from a
        # freshly seeded hub rather than inheriting the previous test's leftovers.
        $null = Invoke-RestMethod -Method Post -Uri "$Script:HubUrl/__test__/reset" -TimeoutSec 5
    }

    AfterEach {
        if ($Script:Ws -and (Test-Path $Script:Ws.Root)) {
            Remove-Item -LiteralPath $Script:Ws.Root -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    Context 'validation' {
        It 'accepts a good config' {
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Validate')
            $r.ExitCode | Should -Be 0
            $r.Output | Should -Match 'Configuration is valid'
        }

        It 'reports a missing directory rather than crashing' {
            (Get-Content $Script:Ws.Config -Raw).Replace(
                $Script:Ws.Inbound.Replace('\', '\\'), 'Z:\does\not\exist') |
                Set-Content $Script:Ws.Config
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Validate')
            $r.ExitCode | Should -Be 2
            $r.Output | Should -Match 'InboundDir does not exist'
        }

        It 'rejects a BbsIndex outside 1-255' {
            (Get-Content $Script:Ws.Config -Raw).Replace('BbsIndex    = 2', 'BbsIndex    = 999') |
                Set-Content $Script:Ws.Config
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Validate')
            $r.ExitCode | Should -Be 2
            $r.Output | Should -Match 'between 1 and 255'
        }

        It 'catches a Bbs.Name that disagrees with nodes.dat' {
            (Get-Content $Script:Ws.Config -Raw).Replace("Name = 'Test BBS'", "Name = 'Wrong Name'") |
                Set-Content $Script:Ws.Config
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Validate')
            $r.ExitCode | Should -Be 2
            $r.Output | Should -Match 'does not match'
        }

        It 'catches two leagues sharing a directory' {
            $extra = @"
        @{
            Game = 'FE'; Number = '555'; Enabled = `$true; BbsIndex = 2
            OutboundDir = '$($Script:Ws.Outbound.Replace('\','\\'))'
            InboundDir  = '$($Script:Ws.Inbound.Replace('\','\\'))'
        }
"@
            (Get-Content $Script:Ws.Config -Raw).Replace('    )', "$extra`n    )") |
                Set-Content $Script:Ws.Config
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Validate')
            $r.ExitCode | Should -Be 2
            $r.Output | Should -Match 'more than one league'
        }

        It 'reads a nodes.dat whose index line carries HOST routing' {
            # "1 HOST 2 3 4" is how the hub's own entry is written, so it is in
            # every real nodelist. Parsing the whole line as an integer fails,
            # and this parser then skipped the entry *silently* - so the only
            # symptom was a node quietly missing from the table.
            #
            # OUR index sits behind the HOST line here on purpose. With it on
            # someone else's entry the old code still passed, because nothing
            # afterwards asked about that node - which is exactly why this went
            # unnoticed on a real box.
            $nodes = Join-Path $Script:Ws.Game 'BRNODES.DAT'
            @(
                '1', 'Nova Hub', '135:1/1', 'Brisbane', 'QLD', 'AUS', ''
                '2 HOST 3 4', 'Test BBS', '1:2/3', 'Somewhere', 'Somestate', 'Somewhere', ''
            ) | Set-Content -LiteralPath $nodes

            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Validate')
            $r.ExitCode | Should -Be 0
            $r.Output | Should -Not -Match 'no entry in'
        }

        It 'still catches a duplicate index behind a HOST line' {
            # Taking the first token must not turn into ignoring the line.
            $nodes = Join-Path $Script:Ws.Game 'BRNODES.DAT'
            @(
                '2 HOST 3 4', 'Impostor', '135:1/1', 'Brisbane', 'QLD', 'AUS', ''
                '2', 'Test BBS', '1:2/3', 'Somewhere', 'Somestate', 'Somewhere', ''
            ) | Set-Content -LiteralPath $nodes

            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Validate')
            $r.ExitCode | Should -Be 2
            $r.Output | Should -Match 'duplicate BBS index 2'
        }

        It 'rejects a UNC GameFolder, which the DOS game cannot use' {
            (Get-Content $Script:Ws.Config -Raw) -replace
                "GameFolder  = '[^']*'", "GameFolder  = '\\\\fileserver\\bbs\\BRE_555'" |
                Set-Content $Script:Ws.Config
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Validate')
            $r.ExitCode | Should -Be 2
            $r.Output | Should -Match 'must not be a UNC path'
            # Not "does not exist" - the point is that the path is wrong in kind,
            # which holds whether or not the share is reachable from here.
            $r.Output | Should -Not -Match 'GameFolder does not exist'
        }
    }

    Context 'sync' {
        It 'authenticates and completes a run with nothing to do' {
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $r.ExitCode | Should -Be 0
            $r.Output | Should -Match 'OAuth token obtained'
            $r.Output | Should -Match 'Run Summary'
        }

        It 'writes metrics.json in the same shape as the Python client' {
            $null = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            Test-Path $Script:Ws.Metrics | Should -BeTrue
            $m = Get-Content $Script:Ws.Metrics -Raw | ConvertFrom-Json
            $m.PSObject.Properties.Name | Should -Contain 'total_uploaded'
            $m.PSObject.Properties.Name | Should -Contain 'total_downloaded'
            $m.PSObject.Properties.Name | Should -Contain 'success'
            $m.PSObject.Properties.Name | Should -Contain 'errors'
            $m.PSObject.Properties.Name | Should -Contain 'leagues'
        }

        It 'uploads an outbound packet and archives it' {
            # 555 B, source 02 (us), dest 01, sequence 001
            $packet = Join-Path $Script:Ws.Outbound '555B0201.001'
            [IO.File]::WriteAllBytes($packet, [byte[]](1..64))

            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $r.ExitCode | Should -Be 0
            $r.Output | Should -Match 'Uploaded 555B0201\.001'

            Test-Path $packet | Should -BeFalse -Because 'a confirmed packet is moved out of outbound'
            Test-Path (Join-Path $Script:Ws.Archive '555B0201.001') | Should -BeTrue
        }

        It 'ignores a packet whose source index is not ours' {
            # source 09 is some other BBS - uploading it would earn a 403
            $foreign = Join-Path $Script:Ws.Outbound '555B0901.001'
            [IO.File]::WriteAllBytes($foreign, [byte[]](1..64))

            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $r.ExitCode | Should -Be 0
            $r.Output | Should -Not -Match '555B0901'
            Test-Path $foreign | Should -BeTrue -Because 'it is not ours to send or delete'
        }

        It 'ignores files that are not packets at all' {
            Set-Content -LiteralPath (Join-Path $Script:Ws.Outbound 'readme.txt') -Value 'hello'
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $r.ExitCode | Should -Be 0
            Test-Path (Join-Path $Script:Ws.Outbound 'readme.txt') | Should -BeTrue
        }

        It 'downloads an inbound packet byte-for-byte, atomically' {
            $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $r.ExitCode | Should -Be 0
            $r.Output | Should -Match 'Downloaded 555B0102\.007'

            $landed = Join-Path $Script:Ws.Inbound '555B0102.007'
            Test-Path $landed | Should -BeTrue

            $got = [IO.File]::ReadAllBytes($landed)
            $want = [IO.File]::ReadAllBytes($Script:ExpectedPacket)
            $got.Length | Should -Be $want.Length
            (Compare-Object $got $want -SyncWindow 0) | Should -BeNullOrEmpty

            # A .part left behind means the rename-into-place failed, and the
            # game could otherwise have seen a half-written packet.
            @(Get-ChildItem $Script:Ws.Inbound -Filter '*.part').Count | Should -Be 0
        }

        It 'writes the nodelist into the game folder' {
            $null = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            Test-Path (Join-Path $Script:Ws.Game 'BRNODES.555') | Should -BeTrue
        }

        It 'stops asking for the nodelist once it has one' {
            # The default. The hub queues a changed nodelist as an ordinary
            # packet, so polling for it every sync is waste - a nodelist changes
            # once or twice a year. The first run has nothing locally and must
            # bootstrap; the second must not ask at all.
            $first = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config `
                -ClientArgs @('-Once', '-Verbose')
            $first.Output | Should -Match 'No local BRNODES\.555 yet'
            Test-Path (Join-Path $Script:Ws.Game 'BRNODES.555') | Should -BeTrue

            $second = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config `
                -ClientArgs @('-Once', '-Verbose')
            $second.Output | Should -Match 'Have BRNODES\.555 already'
            # -Verbose echoes every request, so this proves no call was made
            # rather than merely that no file was written.
            $second.Output | Should -Not -Match 'leagues/555B/nodelist'
        }

        It 'does not re-download an unchanged nodelist' {
            Set-NodelistCheck -ConfigPath $Script:Ws.Config -Mode 'always'
            $nodelist = Join-Path $Script:Ws.Game 'BRNODES.555'

            $first = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $first.Output | Should -Match 'Updated nodelist BRNODES\.555'
            $stamp = (Get-Item -LiteralPath $nodelist).LastWriteTimeUtc

            # The ETag is remembered next to metrics.json, not in the game
            # folder - the door game scans that directory.
            $state = Join-Path $Script:Ws.Root 'nodelist-etags.json'
            Test-Path -LiteralPath $state | Should -BeTrue

            Start-Sleep -Milliseconds 1100   # so a rewrite would move the mtime

            $second = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config `
                -ClientArgs @('-Once', '-Verbose')
            $second.ExitCode | Should -Be 0
            $second.Output | Should -Not -Match 'Updated nodelist'
            # Specifically the 304, not the byte-compare fallback - otherwise
            # this test would still pass with conditional requests broken.
            $second.Output | Should -Match 'unchanged \(304\)'

            # The file must be left completely alone, not rewritten identically.
            (Get-Item -LiteralPath $nodelist).LastWriteTimeUtc | Should -Be $stamp
        }

        It 'picks up a nodelist that has actually changed' {
            Set-NodelistCheck -ConfigPath $Script:Ws.Config -Mode 'always'
            $nodelist = Join-Path $Script:Ws.Game 'BRNODES.555'
            $null = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')

            $token = (Invoke-RestMethod -Method Post -Uri "$Script:HubUrl/service/api/v1/auth/token" `
                -Body @{ grant_type = 'client_credentials'
                         client_id = 'test_client'; client_secret = 'test_secret' }).access_token
            $null = Invoke-RestMethod -Method Post -Uri "$Script:HubUrl/__test__/nodelist/555B" `
                -Headers @{ Authorization = "Bearer $token" } `
                -ContentType 'application/octet-stream' `
                -Body ([Text.Encoding]::ASCII.GetBytes("2`nTest BBS`n1:2/3`n`n`n`n`n9`nNew Node`n"))

            $second = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $second.Output | Should -Match 'Updated nodelist BRNODES\.555'
            (Get-Content -LiteralPath $nodelist -Raw) | Should -Match 'New Node'
        }

        It 'still avoids a needless write if the hub sends no ETag' {
            # Belt and braces for an older hub, or a lost state file: identical
            # bytes must not churn a file the door game may have open.
            Set-NodelistCheck -ConfigPath $Script:Ws.Config -Mode 'always'
            $nodelist = Join-Path $Script:Ws.Game 'BRNODES.555'
            $null = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            Remove-Item -LiteralPath (Join-Path $Script:Ws.Root 'nodelist-etags.json') -Force
            $stamp = (Get-Item -LiteralPath $nodelist).LastWriteTimeUtc

            Start-Sleep -Milliseconds 1100
            $second = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config `
                -ClientArgs @('-Once', '-Verbose')
            $second.Output | Should -Not -Match 'Updated nodelist'
            # With no stored ETag there is nothing to send, so the hub returns
            # 200 and the byte-compare is what saves the write.
            $second.Output | Should -Not -Match '\(304\)'
            (Get-Item -LiteralPath $nodelist).LastWriteTimeUtc | Should -Be $stamp
        }

        It 'stops seeing a packet as unread once downloaded' {
            $first = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $first.Output | Should -Match 'Total Downloaded: 1'

            # The GET is the acknowledgement - there is no separate ack call -
            # so a second run must find nothing.
            $second = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
            $second.ExitCode | Should -Be 0
            $second.Output | Should -Match 'Total Downloaded: 0'
        }

        It 'refuses to upload a packet claiming a source BBS that is not ours' {
            # The client filters these out locally, but the hub enforces it too.
            # Confirm the hub's answer is 403 so the client's handling of that
            # status is exercised against reality, not an assumption.
            $token = (Invoke-RestMethod -Method Post -Uri "$Script:HubUrl/service/api/v1/auth/token" `
                -Body @{ grant_type = 'client_credentials'
                         client_id = 'test_client'; client_secret = 'test_secret' }).access_token

            $resp = Invoke-WebRequest -Method Put `
                -Uri "$Script:HubUrl/service/api/v1/leagues/555B/packets/555B0902.001" `
                -Headers @{ Authorization = "Bearer $token" } `
                -Body ([byte[]](32..96)) -ContentType 'application/octet-stream' `
                -SkipHttpErrorCheck

            [int]$resp.StatusCode | Should -Be 403
        }
    }

    Context 'safety' {
        It 'refuses to start a second instance against the same config' {
            # Hoist out of $_ before the Start-Job: $_ does not survive into a
            # new runspace, and reading it inside -ArgumentList is easy to get
            # subtly wrong.
            $jobShell = $_.Shell
            $jobScript = $_.Script
            $jobConfig = $Script:Ws.Config

            $job = Start-Job -ScriptBlock {
                & $using:jobShell -NoProfile -ExecutionPolicy Bypass `
                    -File $using:jobScript -Config $using:jobConfig -Daemon
            }

            try {
                Start-Sleep -Seconds 3
                $r = Invoke-Client -Target $_ -ConfigPath $Script:Ws.Config -ClientArgs @('-Once')
                $r.ExitCode | Should -Be 3
                $r.Output | Should -Match 'already running'
            }
            finally {
                Stop-Job $job -ErrorAction SilentlyContinue
                Remove-Job $job -Force -ErrorAction SilentlyContinue
            }
        }

        It 'runs daemon cycles without raising an error' {
            # The single-instance test above also starts a daemon, but only ever
            # inspects the *second* process - so the first one was free to throw
            # on every cycle unnoticed, which is exactly what it did: the wait
            # calculation blew up on the first pass, before maintenance had ever
            # run. The daemon's own catch swallowed it and packets still moved,
            # so nothing else noticed either. Watch the output, not just an exit
            # code.
            $jobShell = $_.Shell
            $jobScript = $_.Script
            $jobConfig = $Script:Ws.Config

            $job = Start-Job -ScriptBlock {
                & $using:jobShell -NoProfile -ExecutionPolicy Bypass `
                    -File $using:jobScript -Config $using:jobConfig -Daemon -Verbose
            }

            try {
                # SyncIntervalSeconds is 5 in the test config, so this covers the
                # first cycle, the wait, and at least one more.
                Start-Sleep -Seconds 12
                $output = (Receive-Job $job) | Out-String

                $output | Should -Not -Match 'Daemon cycle error'
                $output | Should -Not -Match 'Unhandled error'
                # Prove it actually looped rather than dying quietly.
                $cycles = ([regex]::Matches($output, 'Run Summary')).Count
                $cycles | Should -BeGreaterThan 1 -Because 'the daemon should complete repeated cycles'

                # The startup banner belongs to the process, not the cycle. It
                # was inside the sync pass, so a daemon reintroduced itself every
                # couple of minutes and the log read like a series of restarts.
                ([regex]::Matches($output, 'Nova Hub Client .* starting')).Count |
                    Should -Be 1 -Because 'the banner should print once per process'

                # The token is good for 24 hours. Invoke-NovaSync used to pass
                # -Force, so every cycle re-authenticated - the cache existed but
                # nothing ever hit it.
                ([regex]::Matches($output, 'OAuth token obtained')).Count |
                    Should -Be 1 -Because 'the token should be cached across cycles'
            }
            finally {
                Stop-Job $job -ErrorAction SilentlyContinue
                Remove-Job $job -Force -ErrorAction SilentlyContinue
            }
        }

        It 'exits 2 when the config file does not exist' {
            $r = Invoke-Client -Target $_ -ConfigPath (Join-Path $Script:Ws.Root 'nope.psd1') -ClientArgs @('-Once')
            $r.ExitCode | Should -Be 2
            $r.Output | Should -Match 'not found'
        }
    }
}
