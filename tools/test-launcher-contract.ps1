$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TemporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('dbl-' + [Guid]::NewGuid().ToString('N'))
$script:LauncherAssertions = 0

function Assert-Launcher([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:LauncherAssertions++
}

function Invoke-LauncherFixture {
    param(
        [string]$Launcher,
        [string]$Arguments = '',
        [switch]$EnvironmentNonInteractive,
        [switch]$MissingPowerShell
    )
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $start.Arguments = '/d /c ""{0}" {1}"' -f (Join-Path $TemporaryRoot $Launcher), $Arguments
    $start.WorkingDirectory = $TemporaryRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables.Remove('DRIVEBOUND_NONINTERACTIVE')
    if ($EnvironmentNonInteractive) { $start.EnvironmentVariables['DRIVEBOUND_NONINTERACTIVE'] = '1' }
    if ($MissingPowerShell) { $start.EnvironmentVariables['PATH'] = '' }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        # Pause receives a key only in the disposable fixture; no user window opens.
        $process.StandardInput.WriteLine('x')
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(20000)) {
            $process.Kill()
            throw "Disposable launcher test timed out: $Launcher"
        }
        return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $stdout.Result + $stderr.Result }
    }
    finally { $process.Dispose() }
}

# Every executed launcher is copied into a new disposable tree. Its setup script
# is a test fixture, never the real installer; no configuration or Docker is read.
try {
    New-Item -ItemType Directory -Path $TemporaryRoot -ErrorAction Stop | Out-Null
    $toolsPath = Join-Path $TemporaryRoot 'tools'
    New-Item -ItemType Directory -Path $toolsPath -ErrorAction Stop | Out-Null
    $launchers = @{
        'Drivebound Setup.cmd' = 'Setup'
        'Drivebound Start.cmd' = 'Start'
        'Drivebound Stop.cmd' = 'Stop'
        'Drivebound Status.cmd' = 'Status'
        'Drivebound Remote Access.cmd' = 'Remote'
    }
    $fixture = @'
param([string]$Action = 'Remote', [switch]$NonInteractive, [int]$FixtureExit = 23, [string]$Probe = '')
Write-Output ("FIXTURE action={0}; noninteractive={1}; probe={2}" -f $Action, $NonInteractive.IsPresent, $Probe)
if ($FixtureExit -ne 0) { [Console]::Error.WriteLine('Disposable setup failure detail remains visible.') }
exit $FixtureExit
'@
    [IO.File]::WriteAllText((Join-Path $toolsPath 'setup-drivebound.ps1'), $fixture)
    [IO.File]::WriteAllText((Join-Path $toolsPath 'remote-access.ps1'), $fixture)
    foreach ($name in $launchers.Keys) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $name) -Destination (Join-Path $TemporaryRoot $name)
        $result = Invoke-LauncherFixture $name '-NonInteractive -Probe "value with spaces"'
        Assert-Launcher ($result.ExitCode -eq 23) "$name lost the setup exit code."
        Assert-Launcher ($result.Output.Contains("action=$($launchers[$name]); noninteractive=True; probe=value with spaces")) "$name lost its action or forwarded arguments."
        Assert-Launcher ($result.Output.Contains('Disposable setup failure detail remains visible.') -and $result.Output.Contains('Exit code: 23.')) "$name hid failure details."
        Assert-Launcher (-not $result.Output.Contains('This window will stay open')) "$name paused in noninteractive mode."

        $result = Invoke-LauncherFixture $name -EnvironmentNonInteractive
        Assert-Launcher ($result.ExitCode -eq 23 -and $result.Output.Contains('noninteractive=True') -and -not $result.Output.Contains('This window will stay open')) "$name did not apply noninteractive environment mode."
        $result = Invoke-LauncherFixture $name '-NonInteractive' -EnvironmentNonInteractive
        Assert-Launcher ($result.ExitCode -eq 23 -and $result.Output.Contains('noninteractive=True')) "$name duplicated the noninteractive switch."

        $result = Invoke-LauncherFixture $name
        Assert-Launcher ($result.ExitCode -eq 23 -and $result.Output.Contains('This window will stay open')) "$name did not retain interactive failure or exit code."
        $result = Invoke-LauncherFixture $name '-FixtureExit 0'
        Assert-Launcher ($result.ExitCode -eq 0 -and -not $result.Output.Contains('could not finish') -and -not $result.Output.Contains('This window will stay open')) "$name treated success as failure."

        $result = Invoke-LauncherFixture $name '-NonInteractive' -MissingPowerShell
        Assert-Launcher ($result.ExitCode -eq 9009 -and $result.Output.Contains('Exit code: 9009.')) "$name did not preserve PowerShell startup failure."
    }

    [IO.File]::WriteAllText((Join-Path $toolsPath 'setup-drivebound.ps1'), 'param(')
    [IO.File]::WriteAllText((Join-Path $toolsPath 'remote-access.ps1'), 'param(')
    foreach ($name in $launchers.Keys) {
        $result = Invoke-LauncherFixture $name
        Assert-Launcher ($result.ExitCode -ne 0 -and $result.Output.Contains('This window will stay open') -and $result.Output.Contains('ParserError')) "$name lost the PowerShell parser failure."
    }
    Write-Host "Launcher contracts passed ($script:LauncherAssertions assertions; no real setup or Docker invoked)." -ForegroundColor Green
}
finally {
    # Only remove this invocation's direct, uniquely named temporary child.
    $resolved = [IO.Path]::GetFullPath($TemporaryRoot)
    $expectedParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
    if ([IO.Path]::GetDirectoryName($resolved) -ne $expectedParent -or [IO.Path]::GetFileName($resolved) -notmatch '^dbl-[a-f0-9]{32}$') {
        throw 'Refusing unsafe launcher-fixture cleanup.'
    }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
