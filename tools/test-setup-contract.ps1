$ErrorActionPreference = 'Stop'
$SourceProjectRoot = Split-Path -Parent $PSScriptRoot
$SetupScript = Join-Path $PSScriptRoot 'setup-drivebound.ps1'
$TemporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('drivebound-setup-contract-' + [Guid]::NewGuid().ToString('N'))
$script:Assertions = 0
function Assert-Condition([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:Assertions++
}
function Assert-Rejected([scriptblock]$Command, [string]$Pattern) {
    try { & $Command } catch {
        Assert-Condition ($_.Exception.Message -match $Pattern) "Unexpected rejection: $($_.Exception.Message)"
        return
    }
    throw "Expected rejection matching $Pattern"
}
function New-TestInstallation([string]$Name) {
    $root = Join-Path $TemporaryRoot $Name
    [void][IO.Directory]::CreateDirectory((Join-Path $root 'photos'))
    Copy-Item -LiteralPath (Join-Path $SourceProjectRoot '.env.example') -Destination (Join-Path $root '.env.example')
    Copy-Item -LiteralPath (Join-Path $SourceProjectRoot 'docker-compose.yml') -Destination (Join-Path $root 'docker-compose.yml')
    return $root
}
function Set-TestContext([string]$Root, [switch]$Paths) {
    $script:ProjectRoot = $Root
    $script:EnvironmentPath = Join-Path $Root '.env'
    $script:EnvironmentTemplatePath = Join-Path $Root '.env.example'
    $script:BaseComposePath = Join-Path $Root 'docker-compose.yml'
    $script:UserComposePath = Join-Path $Root 'docker-compose.user.yml'
    $script:OwnerEmail = 'owner@example.test'
    $script:ExistingPhotosPath = if ($Paths) { Join-Path $Root 'photos' } else { '' }
    $script:ManagedDataPath = if ($Paths) { Join-Path $Root 'managed' } else { '' }
    $script:ProtectionDataPath = if ($Paths) { Join-Path $Root 'protection' } else { '' }
    $script:RefreshPlaceholderSecrets = $false
}
function Run-TestSetup {
    $lease = Enter-InstallationLock
    try { Recover-ConfigurationTransaction; Initialize-Drivebound | Out-Null } finally { $lease.Dispose() }
}
try {
    [void][IO.Directory]::CreateDirectory($TemporaryRoot)
    . $SetupScript -LibraryOnly -NonInteractive -NoStart -NoBrowser -OwnerEmail 'owner@example.test' -ProjectRootOverride $TemporaryRoot
    $nativeFixture = Join-Path $TemporaryRoot 'native argument fixture.ps1'
    Write-DurableText $nativeFixture @'
[Console]::Error.WriteLine('disposable stderr fixture')
ConvertTo-Json -InputObject @($args) -Compress
exit 7
'@
    $powershell = (Get-Command powershell.exe).Source
    $argumentSamples = @('with spaces', 'quote"inside', 'trailing\', 'two\\slashes', '$literal', '')
    $native = Invoke-DriveboundNative -FilePath $powershell -Arguments (@('-NoProfile', '-File', $nativeFixture) + $argumentSamples) -TimeoutSeconds 10
    Assert-Condition ($native.ExitCode -eq 7 -and $native.StdErr.Contains('disposable stderr fixture')) 'Native stderr interrupted exit-code handling.'
    Assert-Condition ($ErrorActionPreference -eq 'Stop') 'Native invocation changed caller error preferences.'
    $received = @($native.StdOut | ConvertFrom-Json)
    Assert-Condition ($received.Count -eq $argumentSamples.Count) 'Native argument count changed.'
    for ($index = 0; $index -lt $argumentSamples.Count; $index++) {
        Assert-Condition ($received[$index] -ceq $argumentSamples[$index]) 'Native argument quoting did not preserve a fixture value.'
    }
    # Exercise the actual readiness function with a process that writes stderr,
    # without contacting Docker or launching Desktop.
    $realNative = ${function:Invoke-DriveboundNative}
    & {
        function Get-Command { return [pscustomobject]@{Source = $powershell} }
        function Invoke-DriveboundNative {
            & $realNative -FilePath $powershell -Arguments @('-NoProfile', '-File', $nativeFixture) -TimeoutSeconds 10
        }
        Assert-Condition (-not (Test-DockerReady)) 'Stopped-daemon stderr did not return a retryable false readiness result.'
    }
    Assert-Condition (Test-Path -LiteralPath (Join-Path $SourceProjectRoot 'Drivebound Status.cmd')) 'The user-facing Status launcher is missing.'
    function Get-HostVolumeIdentity([string]$Path) {
        Assert-PlainLocalPath $Path
        return @{volume_id = 'test-volume-one'; physical_disk_id = 'test-disk-one'; root = [IO.Path]::GetPathRoot($Path)}
    }
    function Wait-ForDocker { throw 'Tests must not start Docker.' }
    $fresh = New-TestInstallation 'fresh'
    Set-TestContext $fresh -Paths
    Run-TestSetup
    $first = Read-EnvironmentMap $Script:EnvironmentPath
    $state = Get-InstallationState
    Assert-Condition ($first.POSTGRES_PASSWORD -ne 'change-me') 'Example database password retained.'
    Assert-Condition ($first.MEDIA_ENCRYPTION_MASTER_KEY.Length -eq 44 -and $first.MEDIA_ENCRYPTION_MASTER_KEY -ne $Script:DevelopmentMediaKey) 'Media key was not generated.'
    Assert-Condition ($state.protection_status -eq 'same_disk') 'Two folders on one disk were credited as independent protection.'
    Assert-Condition (-not $state.database_adoption_allowed) 'A fresh installation must not adopt an existing Docker database.'
    $compose = [IO.File]::ReadAllText($Script:UserComposePath)
    Assert-Condition ($compose -match 'target: /data/imports\r?\n\s+read_only: true') 'Import mount is not read-only.'
    Assert-Condition (($compose | Select-String -Pattern 'create_host_path: false' -AllMatches).Matches.Count -eq 30) 'Not every generated bind rejects missing host paths.'
    Assert-Condition ($compose -match 'target: /etc/drivebound/storage.json\r?\n\s+read_only: true') 'Manifest is not read-only.'
    Assert-Condition (@(Get-ChildItem -LiteralPath (Join-Path $fresh 'photos') -Force).Count -eq 0) 'Setup wrote identity markers inside imported photos.'
    Assert-Condition ((Get-Acl -LiteralPath $Script:EnvironmentPath).AreAccessRulesProtected) 'Secrets inherited broad permissions.'
    Set-TestContext $fresh
    Run-TestSetup
    $again = Read-EnvironmentMap $Script:EnvironmentPath
    Assert-Condition ($again.MEDIA_ENCRYPTION_MASTER_KEY -ceq $first.MEDIA_ENCRYPTION_MASTER_KEY) 'Rerun changed encryption key.'
    Assert-Condition ($again.POSTGRES_PASSWORD -ceq $first.POSTGRES_PASSWORD) 'Rerun changed database password.'
    Assert-Condition ((Get-InstallationState).paths.managed -eq $state.paths.managed) 'Rerun without path arguments changed folders.'
    Assert-Condition ((Get-InstallationState).compose_project_name -eq $state.compose_project_name) 'Rerun changed database project identity.'
    Assert-Condition (Test-PathOverlap ([IO.Path]::GetPathRoot($fresh)) $fresh) 'Drive-root overlap not detected.'
    Assert-Condition (-not (Test-PathOverlap (Join-Path $fresh 'photo') (Join-Path $fresh 'photos'))) 'Sibling folder falsely overlaps.'
    Assert-Condition ((ConvertTo-ComposePath (Join-Path $fresh 'cash$and''quote')) -match '\$\$and''''quote') 'Compose special characters not escaped.'
    $originalEnv = [IO.File]::ReadAllText($Script:EnvironmentPath)
    Write-DurableText $Script:EnvironmentPath ($originalEnv -replace '(?m)^MEDIA_ENCRYPTION_MASTER_KEY=.*\r?\n', '')
    Assert-Rejected { Run-TestSetup } 'missing MEDIA_ENCRYPTION_MASTER_KEY'
    Write-DurableText $Script:EnvironmentPath $originalEnv
    $wrongMarker = Join-Path $state.volumes.managed.marker_directory '.drivebound-volume'
    Write-DurableText $wrongMarker 'different-volume-marker'
    Assert-Rejected { Run-TestSetup } 'identity marker'
    Write-DurableText $wrongMarker $state.volumes.managed.marker
    $realAtomic = ${function:Set-AtomicConfigurationFile}
    $script:AtomicCalls = 0
    function Set-AtomicConfigurationFile([string]$Source, [string]$Target) {
        $script:AtomicCalls++
        if ($script:AtomicCalls -eq 2) { throw 'injected publication failure' }
        & $realAtomic -Source $Source -Target $Target
    }
    $beforeCompose = [IO.File]::ReadAllText($Script:UserComposePath)
    $lease = Enter-InstallationLock
    try { Assert-Rejected { Invoke-ConfigurationTransaction -Contents @{'.env' = 'TEST=changed'; 'docker-compose.user.yml' = 'changed'} } 'injected publication failure' }
    finally { $lease.Dispose(); ${function:Set-AtomicConfigurationFile} = $realAtomic }
    Assert-Condition ([IO.File]::ReadAllText($Script:EnvironmentPath) -ceq $originalEnv) 'Transaction failed to recover prior .env.'
    Assert-Condition ([IO.File]::ReadAllText($Script:UserComposePath) -ceq $beforeCompose) 'Transaction failed to recover prior mounts.'
    $lease = Enter-InstallationLock
    try { Assert-Rejected { $otherLease = Enter-InstallationLock; $otherLease.Dispose() } 'already running' } finally { $lease.Dispose() }
    $interrupted = New-TestInstallation 'interrupted'
    Set-TestContext $interrupted -Paths
    $realWrite = ${function:Write-DurableText}
    function Write-DurableText([string]$Path, [AllowEmptyString()][string]$Content) {
        if ((Split-Path -Leaf $Path) -eq '.drivebound-volume') { throw 'injected marker failure' }
        & $realWrite -Path $Path -Content $Content
    }
    try { Assert-Rejected { Run-TestSetup } 'injected marker failure' } finally { ${function:Write-DurableText} = $realWrite }
    $draftPath = Join-Path $interrupted '.drivebound/initialization-started.json'
    $draft = [IO.File]::ReadAllText($draftPath) | ConvertFrom-Json
    $plannedEnv = $draft.contents.'.env'
    Set-TestContext $interrupted
    Run-TestSetup
    Assert-Condition ([IO.File]::ReadAllText($Script:EnvironmentPath) -ceq $plannedEnv) 'Interrupted setup generated different credentials on resume.'
    Assert-Condition (-not (Test-Path -LiteralPath $draftPath)) 'Completed setup left an incomplete draft.'
    $orphan = New-TestInstallation 'orphan'
    Set-TestContext $orphan -Paths
    Write-DurableText $Script:UserComposePath 'legacy mount configuration'
    Assert-Rejected { Run-TestSetup } 'lost its .env'
    Assert-Condition (-not (Test-Path -LiteralPath $Script:EnvironmentPath)) 'Orphaned installation received replacement credentials.'
    $reusedDrive = New-TestInstallation 'old-physical-drive'
    Set-TestContext $reusedDrive -Paths
    $oldOriginals = Join-Path $reusedDrive 'managed/originals'
    [void][IO.Directory]::CreateDirectory($oldOriginals)
    Write-DurableText (Join-Path $oldOriginals 'previously-encrypted-photo') 'disposable old media'
    Assert-Rejected { Run-TestSetup } 'contains existing Drivebound data'
    Assert-Condition (-not (Test-Path -LiteralPath $Script:EnvironmentPath)) 'A reused managed disk received fresh keys.'
    $mixedDisks = New-TestInstallation 'imports-share-protection'
    Set-TestContext $mixedDisks -Paths
    function Get-HostVolumeIdentity([string]$Path) {
        Assert-PlainLocalPath $Path
        $disk = if ((Split-Path -Leaf $Path) -eq 'managed') { 'upload-disk' } else { 'photo-and-copy-disk' }
        return @{volume_id = 'test-volume-one'; physical_disk_id = $disk; root = [IO.Path]::GetPathRoot($Path)}
    }
    Run-TestSetup
    Assert-Condition ((Get-InstallationState).protection_status -eq 'same_disk') 'Imported originals sharing the protection disk were mislabeled independent.'
    $roles = @{imports = @{physical_disk_id = 'photos'}; managed = @{physical_disk_id = 'uploads'}; protection = @{physical_disk_id = 'copies'}}
    Assert-Condition ((Get-ProtectionIndependence $roles) -eq 'independent_disk') 'Three distinct disks were not recognized.'
    $roles.imports.physical_disk_id = $null
    Assert-Condition ((Get-ProtectionIndependence $roles) -eq 'unverified') 'Unknown import disk identity was credited as independent protection.'
    Write-Host "Guided setup contract passed ($script:Assertions assertions)." -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $TemporaryRoot) {
        $resolved = [IO.Path]::GetFullPath($TemporaryRoot)
        $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $resolved.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -or -not (Split-Path -Leaf $resolved).StartsWith('drivebound-setup-contract-')) { throw 'Unsafe test cleanup path.' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
