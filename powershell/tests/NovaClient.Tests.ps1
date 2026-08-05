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

        # 6 lines per entry then a blank line: index, name, fido, city, state, country
        @(
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
        foreach ($name in @('NovaClient-WinPS5.ps1', 'NovaClient-PS7.ps1', 'Install-NovaClientTask.ps1')) {
            $path = Join-Path $Script:PsDir $name
            $errors = $null
            $null = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errors)
            $errors | Should -BeNullOrEmpty -Because "$name should parse cleanly"
        }
    }

    It 'the two clients are identical below the transport boundary' {
        $marker = 'END HTTP TRANSPORT'
        $tail = {
            param($p)
            $lines = Get-Content -LiteralPath $p
            $idx = ($lines | Select-String -SimpleMatch $marker | Select-Object -First 1).LineNumber
            $lines[($idx)..($lines.Count - 1)]
        }
        $a = & $tail (Join-Path $Script:PsDir 'NovaClient-WinPS5.ps1')
        $b = & $tail (Join-Path $Script:PsDir 'NovaClient-PS7.ps1')

        # One intentional difference: the version banner names the host.
        $diff = Compare-Object $a $b
        @($diff).Count | Should -BeLessOrEqual 2 -Because 'only the version banner line should differ'
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

        It 'exits 2 when the config file does not exist' {
            $r = Invoke-Client -Target $_ -ConfigPath (Join-Path $Script:Ws.Root 'nope.psd1') -ClientArgs @('-Once')
            $r.ExitCode | Should -Be 2
            $r.Output | Should -Match 'not found'
        }
    }
}
