param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'setup-state.ps1')
. (Join-Path $PSScriptRoot 'setup-diagnostics.ps1')

$assertions = 0
function Assert-Diagnostic {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw ('Diagnostic contract failed: ' + $Message) }
    $script:assertions++
}

$testDirectory = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ('drivebound-diagnostic-tests-' + [Guid]::NewGuid().ToString('N'))))
[void](New-Item -ItemType Directory -Path $testDirectory -ErrorAction Stop)
$fallbackDirectories = New-Object 'System.Collections.Generic.List[string]'
try {
    Assert-Diagnostic ($null -eq (Get-DriveboundDiagnosticState)) 'Dot-sourcing must not initialize or create files.'
    Initialize-DriveboundDiagnostics -ProjectRoot $testDirectory -Action 'Setup'
    Assert-Diagnostic ([IO.File]::Exists($Script:DriveboundDiagnosticLogPath)) 'An initialized session has a log.'
    Assert-Diagnostic ($Script:DriveboundDiagnosticLogPath.StartsWith((Join-Path $testDirectory '.drivebound/logs'), [StringComparison]::OrdinalIgnoreCase)) 'Default logs stay in the installation log folder.'
    $firstPath = $Script:DriveboundDiagnosticLogPath
    $firstSession = $Script:DriveboundDiagnosticSessionId
    $privateAcl = Get-Acl -LiteralPath $firstPath
    Assert-Diagnostic $privateAcl.AreAccessRulesProtected 'Diagnostic file ACL inheritance must be disabled.'
    $allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18')
    foreach ($rule in $privateAcl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        Assert-Diagnostic ($sid -in $allowedSids) 'Diagnostic logs must grant access only to this user and SYSTEM.'
    }
    Register-DriveboundDiagnosticSecrets -Values @{
        POSTGRES_PASSWORD = 'sensitive$pass with/slash'
        SMTP_PASSWORD = 'quoted"secret\value'
        BOOTSTRAP_ADMIN_EMAIL = 'private-person@example.test'
        DATABASE_URL = 'postgresql://dbuser:dbpassword@database/private'
        UNRELATED_SETTING = 'KeepThisDiagnosticValue'
    }
    $secrets = @('sensitive$pass with/slash', 'sensitive$$pass with/slash', 'sensitive%24pass%20with%2Fslash', 'quoted"secret\value', 'quoted\"secret\\value', 'private-person@example.test', 'postgresql://dbuser:dbpassword@database/private')
    foreach ($secret in $secrets) {
        $sanitized = Protect-DriveboundDiagnosticText -Text ('Failure ' + $secret + ' KeepThisDiagnosticValue')
        Assert-Diagnostic (-not $sanitized.Contains($secret)) 'Registered raw and escaped secrets must be redacted.'
        Assert-Diagnostic ($sanitized.Contains('KeepThisDiagnosticValue')) 'Nonsecret diagnostics must remain useful.'
    }
    $examples = @(
        @('password=abc123abc', 'abc123abc'),
        @('"api_key": "key with spaces"', 'key with spaces'),
        @("SMTP_PASSWORD='quoted secret'", 'quoted secret'),
        @('Authorization: Bearer authorization-sentinel', 'authorization-sentinel'),
        @('Authorization: Basic QWxhZGRpbjpvcGVuU2VzYW1l', 'QWxhZGRpbjpvcGVuU2VzYW1l'),
        @('https://username:uri-password@localhost:8000/api', 'uri-password'),
        @('https://localhost/?code=opaque-code&next=home', 'opaque-code'),
        @('unexpected-person@example.test', 'unexpected-person@example.test'),
        @('eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl', 'eyJhbGciOiJSUzI1NiJ9'),
        @("-----BEGIN PRIVATE KEY-----`nprivate-material`n-----END PRIVATE KEY-----", 'private-material'),
        @("-----BEGIN PRIVATE KEY-----`nunfinished-pem", 'unfinished-pem')
    )
    foreach ($example in $examples) {
        Assert-Diagnostic (-not (Protect-DriveboundDiagnosticText -Text $example[0]).Contains($example[1])) 'Pattern redaction must catch unregistered sensitive content.'
    }
    Assert-Diagnostic ((Protect-DriveboundDiagnosticText -Text ('x' * 100000)).Length -le 4096) 'Individual diagnostics must be bounded.'
    Assert-Diagnostic (-not (Protect-DriveboundDiagnosticText -Text ([char]27 + '[31mwarning' + [char]0)).Contains([string][char]27)) 'Terminal control escapes must be removed.'
    Set-DriveboundStage -Stage 'Starting Docker Desktop'
    function Invoke-DiagnosticFailureFixture {
        # The source-only sentinel MUST NOT appear in the captured report.
        throw 'Docker unavailable; password=hidden-password' # SOURCE_ONLY_SENTINEL_DO_NOT_LOG
    }
    try { Invoke-DiagnosticFailureFixture } catch { $failure = Write-DriveboundFailure -ErrorRecord $_ }
    Assert-Diagnostic ($failure -match 'Stage: Starting Docker Desktop') 'Failure identifies the active stage.'
    Assert-Diagnostic ($failure -match 'Error type:.*RuntimeException') 'Failure identifies the exception type.'
    Assert-Diagnostic ($failure -match 'Invoke-DiagnosticFailureFixture') 'Failure identifies the failing function.'
    Assert-Diagnostic ($failure -match 'test-setup-diagnostics.ps1:\d+') 'Failure identifies file and line.'
    Assert-Diagnostic (-not $failure.Contains('SOURCE_ONLY_SENTINEL_DO_NOT_LOG')) 'Failure excludes source lines.'
    Assert-Diagnostic (-not $failure.Contains('hidden-password')) 'Failure reasons are redacted.'
    Assert-Diagnostic ($failure.Contains('Diagnostic log:')) 'Failure includes the support log path.'
    $logged = [IO.File]::ReadAllText($firstPath)
    Assert-Diagnostic ($logged.Contains('Starting Docker Desktop')) 'Stages are persisted.'
    Assert-Diagnostic (-not $logged.Contains('hidden-password')) 'Log contents stay redacted.'
    Assert-Diagnostic (-not $logged.Contains('SOURCE_ONLY_SENTINEL_DO_NOT_LOG')) 'Log excludes source lines.'
    foreach ($index in 1..100) { Add-DriveboundDiagnostic -Message ((('z' * 500) + "`n") * 8) }
    Assert-Diagnostic ((Get-Item -LiteralPath $firstPath).Length -le 262144) 'Session logs stay below 256 KiB.'
    Assert-Diagnostic (Get-DriveboundDiagnosticState).Capped 'Session marks its size cap without failing setup.'
    Initialize-DriveboundDiagnostics -ProjectRoot $testDirectory -Action 'Start'
    Assert-Diagnostic ($Script:DriveboundDiagnosticSessionId -ne $firstSession) 'Runs use unique sessions.'
    Assert-Diagnostic ($Script:DriveboundDiagnosticLogPath -ne $firstPath) 'Runs never overwrite prior logs.'
    Assert-Diagnostic ((Get-DriveboundDiagnosticState).Secrets.Count -eq 0) 'New sessions forget old credentials.'

    # A file used as a directory deterministically simulates an unavailable
    # destination without changing permissions on any user or system folder.
    $blockedRoot = Join-Path $testDirectory 'not-a-directory'
    [IO.File]::WriteAllText($blockedRoot, 'fixture')
    Initialize-DriveboundDiagnostics -ProjectRoot $blockedRoot -Action 'Setup'
    Assert-Diagnostic ([IO.File]::Exists($Script:DriveboundDiagnosticLogPath)) 'Unavailable project log path falls back to a private temporary log.'
    $fallbackDirectories.Add((Split-Path -Parent $Script:DriveboundDiagnosticLogPath))
    Assert-Diagnostic ((Get-DriveboundDiagnosticState).StorageNotice -match 'private temporary') 'Fallback location is explained.'
    Assert-Diagnostic (Get-Acl -LiteralPath $Script:DriveboundDiagnosticLogPath).AreAccessRulesProtected 'Fallback log is also private.'

    $savedAclHelper = (Get-Command Set-DriveboundPrivateAcl).ScriptBlock
    function Set-DriveboundPrivateAcl { param([string]$Path) throw 'SIMULATED_ACL_FAILURE_WITH_SENSITIVE_TEXT' }
    try {
        Initialize-DriveboundDiagnostics -ProjectRoot $testDirectory -Action 'Setup'
        $fallbackDirectories.Add((Join-Path ([IO.Path]::GetTempPath()) ('drivebound-diagnostics-' + $Script:DriveboundDiagnosticSessionId)))
        Assert-Diagnostic ($null -eq $Script:DriveboundDiagnosticLogPath) 'Cannot write logs without private protection.'
        try { throw 'Original setup failure' } catch { $unlogged = Write-DriveboundFailure -ErrorRecord $_ }
        Assert-Diagnostic ($unlogged.Contains('Original setup failure')) 'Diagnostic storage failure must not mask the setup error.'
        Assert-Diagnostic ($unlogged.Contains('could not be created')) 'User learns that logging was unavailable.'
        Assert-Diagnostic (-not $unlogged.Contains('SIMULATED_ACL_FAILURE_WITH_SENSITIVE_TEXT')) 'Logging exceptions never leak raw storage error text.'
    } finally { Set-Item -Path Function:Set-DriveboundPrivateAcl -Value $savedAclHelper }

    Initialize-DriveboundDiagnostics -ProjectRoot $testDirectory -Action 'Setup'
    $state = Get-DriveboundDiagnosticState
    $state.LogPath = Join-Path $blockedRoot 'impossible.log'
    Add-DriveboundDiagnostic -Message 'A harmless diagnostic'
    Assert-Diagnostic ($state.StorageNotice -match 'Writing.*failed') 'Append failure is contained and explained.'
    Write-Host ('Setup diagnostics contracts passed: ' + $assertions + ' assertions.')
} finally {
    # Delete only exact, freshly created, GUID-named test folders under TEMP.
    $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
    foreach ($directory in @($testDirectory) + @($fallbackDirectories)) {
        $resolved = [IO.Path]::GetFullPath($directory)
        if ((Split-Path -Parent $resolved).TrimEnd('\', '/') -ne $tempParent -or (Split-Path -Leaf $resolved) -notmatch '^drivebound-(?:diagnostic-tests|diagnostics)-[a-f0-9]{32}$') { throw 'Refusing unsafe diagnostic fixture cleanup.' }
        if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
    }
}
