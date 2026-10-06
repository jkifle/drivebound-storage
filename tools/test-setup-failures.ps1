$ErrorActionPreference = 'Stop'
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('drivebound-failure-tests-' + [Guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $fixtureRoot)
$assertions = 0
function Assert-FailureTest([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:assertions++
}
try {
    . (Join-Path $PSScriptRoot 'setup-drivebound.ps1') -LibraryOnly -NonInteractive -ProjectRootOverride $fixtureRoot
    Initialize-DriveboundDiagnostics -ProjectRoot $fixtureRoot -Action 'Failure contracts'
    Register-DriveboundDiagnosticSecrets -Values @{POSTGRES_PASSWORD = 'fixture-secret-never-print'}
    & {
        function Get-Command { return @([pscustomobject]@{Source = 'first-docker.exe'}, [pscustomobject]@{Source = 'duplicate-docker.exe'}) }
        $found = @(Get-DriveboundDockerCommand)
        Assert-FailureTest ($found.Count -eq 1 -and $found[0].Source -eq 'first-docker.exe') 'Duplicate executable discovery must resolve to one path.'
    }
    & {
        function Get-DriveboundDockerCommand { return [pscustomobject]@{Source = 'mock-docker'} }
        function Invoke-DriveboundNative { return [pscustomobject]@{ExitCode = 1; TimedOut = $false; StdOut = ''; StdErr = 'missing dockerDesktopLinuxEngine pipe; password=fixture-secret-never-print'} }
        Assert-FailureTest (-not (Test-DockerReady)) 'Unavailable Docker was accepted.'
        Assert-FailureTest ($Script:LastDockerDiagnostic.Contains('dockerDesktopLinuxEngine')) 'Docker diagnostic lost the original error.'
        Assert-FailureTest (-not $Script:LastDockerDiagnostic.Contains('fixture-secret-never-print')) 'Docker diagnostic leaked a secret.'
        function Invoke-DriveboundNative { return [pscustomobject]@{ExitCode = 0; TimedOut = $false; StdOut = 'windows|1.0'; StdErr = ''} }
        function Start-Process { throw 'Should not launch Desktop for a running wrong engine.' }
        try { Wait-ForDocker; throw 'Wrong engine was accepted.' } catch { Assert-FailureTest ($_.Exception.Message.Contains('Linux containers')) 'Wrong-engine guidance missing.' }
        function Invoke-DriveboundNative { return [pscustomobject]@{ExitCode = 0; TimedOut = $false; StdOut = 'linux|1.0'; StdErr = ''} }
        Assert-FailureTest (Test-DockerReady) 'Linux engine was rejected.'
        function Invoke-DriveboundNative { throw 'fixture process creation failed' }
        Assert-FailureTest (-not (Test-DockerReady)) 'Process creation failure escaped readiness handling.'
        Assert-FailureTest ($Script:LastDockerDiagnostic.Contains('process creation failed')) 'Process launch reason was lost.'
    }
    & {
        function Get-ComposeArguments { return ,@('compose', 'up') }
        function Get-DriveboundDockerCommand { return [pscustomobject]@{Source = 'mock-docker'} }
        function Invoke-DriveboundNative { return [pscustomobject]@{ExitCode = 17; TimedOut = $false; StdOut = 'npm underlying failure on stdout'; StdErr = "fixture build failed`npassword=fixture-secret-never-print"} }
        try { Invoke-DriveboundCompose -Arguments @('up'); throw 'Compose failure was accepted.' } catch {
            Assert-FailureTest ($_.Exception.Message.Contains('17') -and $_.Exception.Message.Contains('fixture build failed')) 'Compose output or exit code was discarded.'
            Assert-FailureTest (-not $_.Exception.Message.Contains('fixture-secret-never-print')) 'Compose error leaked a secret.'
        }
    }
    & {
        function Invoke-RestMethod { throw 'fixture API unavailable' }
        function Invoke-WebRequest { return [pscustomobject]@{StatusCode = 200} }
        try { Wait-ForDrivebound -TimeoutSeconds 1; throw 'Unready API was accepted.' } catch {
            Assert-FailureTest ($_.Exception.Message.Contains('fixture API unavailable') -and $_.Exception.Message.Contains('Website (port 3000): HTTP 200')) 'Readiness must check and report API and website independently.'
        }
    }
    $nativeFixture = Join-Path $fixtureRoot 'slow.exe'
    Add-Type -OutputAssembly $nativeFixture -OutputType ConsoleApplication -TypeDefinition @'
using System;
public class SlowDiagnosticFixture {
    public static int Main(string[] args) {
        Console.WriteLine("fixture stdout before timeout");
        Console.Error.WriteLine("fixture stderr before timeout");
        Console.Out.Flush(); Console.Error.Flush();
        System.Threading.Thread.Sleep(10000);
        return 0;
    }
}
'@
    $result = Invoke-DriveboundNative -FilePath $nativeFixture -TimeoutSeconds 1
    Assert-FailureTest ($result.TimedOut -and $result.ExitCode -eq -1) 'Native timeout status changed.'
    Assert-FailureTest ($result.StdOut.Contains('stdout before timeout') -and $result.StdErr.Contains('stderr before timeout')) 'Native timeout discarded completed output.'

    # Run the real top-level error handler, but only against an empty temporary
    # installation. It fails before configuration or any Docker call is possible.
    $empty = Join-Path $fixtureRoot 'empty'
    [void](New-Item -ItemType Directory -Path $empty)
    $result = Invoke-DriveboundNative -FilePath (Get-Command powershell.exe).Source -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'setup-drivebound.ps1'), '-NonInteractive', '-NoStart', '-NoBrowser', '-ProjectRootOverride', $empty) -TimeoutSeconds 15
    Assert-FailureTest ($result.ExitCode -eq 1 -and -not $result.TimedOut) 'Top-level error must exit nonzero without waiting for input.'
    Assert-FailureTest ($result.StdOut.Contains('Stage: Checking setup files') -and $result.StdOut.Contains('Diagnostic log:')) 'Top-level error is missing stage or saved log.'
    Assert-FailureTest ($result.StdOut.Contains('.env.example is missing')) 'Top-level error lost its cause.'
    Assert-FailureTest (@(Get-ChildItem -LiteralPath (Join-Path $empty '.drivebound/logs') -Filter '*.log').Count -eq 1) 'Top-level failure report was not saved.'
    $log = [IO.File]::ReadAllText($Script:DriveboundDiagnosticLogPath)
    Assert-FailureTest ($log.Contains('npm underlying failure on stdout') -and $log.Contains('fixture build failed')) 'Compose failure log must retain both streams.'
    Assert-FailureTest (-not $log.Contains('fixture-secret-never-print')) 'Saved diagnostics leaked a registered secret.'
    Write-Host "Setup failure contracts passed ($assertions assertions; no real services contacted)."
} finally {
    $exact = [IO.Path]::GetFullPath($fixtureRoot)
    if ((Split-Path -Parent $exact).TrimEnd('\', '/') -ne [IO.Path]::GetTempPath().TrimEnd('\', '/') -or (Split-Path -Leaf $exact) -notmatch '^drivebound-failure-tests-[a-f0-9]{32}$') { throw 'Unsafe fixture cleanup path.' }
    if (Test-Path -LiteralPath $exact) { Remove-Item -LiteralPath $exact -Recurse -Force }
}
