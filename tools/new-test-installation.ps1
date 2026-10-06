param([switch]$ConfirmDisposable)
$ErrorActionPreference = 'Stop'
if (-not $ConfirmDisposable) { throw 'This creates a fresh test configuration. Specify -ConfirmDisposable only after confirming the existing installation is disposable.' }
. (Join-Path $PSScriptRoot 'setup-drivebound.ps1') -LibraryOnly -NonInteractive -NoStart -NoBrowser
Initialize-DriveboundDiagnostics -ProjectRoot $Script:ProjectRoot -Action 'New disposable test installation'
$lease = $null
try {
    $lease = Enter-InstallationLock
    $state = Get-InstallationState
    if (-not $state -or $state.database_adoption_allowed -or $state.desired_state -ne 'stopped') { throw 'This recovery helper only applies to a stopped setup blocked from adopting an existing database.' }
    foreach ($relative in @('docker-compose.remote.yml', '.drivebound/config-transaction', '.drivebound/initialization-started.json')) {
        if (Test-Path -LiteralPath (Join-Path $Script:ProjectRoot $relative)) { throw 'Remote settings or unfinished configuration recovery need review before creating a new test installation.' }
    }
    if (-not (Test-DockerReady)) { throw "Docker must be available to verify the old test project is stopped. $Script:LastDockerDiagnostic" }
    $docker = (Get-DriveboundDockerCommand).Source
    $containers = Invoke-DriveboundNative -FilePath $docker -Arguments @('ps', '-a', '--filter', "label=com.docker.compose.project=$($state.compose_project_name)", '--format', '{{.ID}}') -TimeoutSeconds 10
    if ($containers.ExitCode -ne 0 -or $containers.TimedOut -or $containers.StdOut.Trim()) { throw 'The old project has containers or could not be checked. Nothing will be stopped or removed automatically.' }
    $id = [Guid]::NewGuid().ToString('N')
    $fresh = Join-Path $Script:ProjectRoot ".drivebound/test-installations/$id"
    $backup = Join-Path $Script:ProjectRoot ".drivebound/recovery-before-test/$id"
    Assert-PlainLocalPath $fresh
    Assert-PlainLocalPath $backup
    [void][IO.Directory]::CreateDirectory($fresh)
    [void][IO.Directory]::CreateDirectory($backup)
    Set-DriveboundPrivateAcl $fresh
    Set-DriveboundPrivateAcl $backup
    Set-DriveboundStage -Stage 'Saving existing test configuration without deleting any storage'
    foreach ($relative in @('.env', 'docker-compose.user.yml', '.drivebound/installation.json', '.drivebound/storage.json')) {
        $source = Join-Path $Script:ProjectRoot $relative
        if (Test-Path -LiteralPath $source) {
            $destination = Join-Path $backup $relative
            [void][IO.Directory]::CreateDirectory((Split-Path -Parent $destination))
            Write-DurableText $destination ([IO.File]::ReadAllText($source))
        }
    }
    $project = "drivebound-test-$id"
    $template = [IO.File]::ReadAllText($Script:EnvironmentTemplatePath)
    $template = [regex]::Replace($template, '(?m)^COMPOSE_PROJECT_NAME=.*\r?\n?', '')
    Write-DurableText (Join-Path $fresh '.env.example') ($template + "`nCOMPOSE_PROJECT_NAME=$project`n")
    Copy-Item -LiteralPath $Script:BaseComposePath -Destination (Join-Path $fresh 'docker-compose.yml')
    [void][IO.Directory]::CreateDirectory((Join-Path $fresh 'photos'))
    Set-DriveboundStage -Stage 'Preparing fresh test credentials and empty storage'
    $prepared = Invoke-DriveboundNative -FilePath (Get-Command powershell.exe | Select-Object -First 1).Source -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'setup-drivebound.ps1'), '-ProjectRootOverride', $fresh, '-NonInteractive', '-NoStart', '-NoBrowser', '-OwnerEmail', $state.owner_email, '-ExistingPhotosPath', (Join-Path $fresh 'photos'), '-ManagedDataPath', (Join-Path $fresh 'managed'), '-ProtectionDataPath', (Join-Path $fresh 'protection')) -TimeoutSeconds 120
    if ($prepared.ExitCode -ne 0 -or $prepared.TimedOut) { throw "Fresh test preparation failed; the original installation configuration remains in place. $(Get-DriveboundNativeFailure $prepared)" }
    $newState = Get-Content -Raw -LiteralPath (Join-Path $fresh '.drivebound/installation.json') | ConvertFrom-Json
    if ($newState.compose_project_name -cne $project -or $newState.owner_email -cne $state.owner_email -or $newState.database_adoption_allowed) { throw 'Prepared test identity did not match the requested clean installation.' }
    Assert-InstallationStorage $newState
    [void](Read-EnvironmentMap (Join-Path $fresh '.env'))
    Set-DriveboundStage -Stage 'Activating fresh test configuration for the standard launchers'
    Invoke-ConfigurationTransaction -Contents @{
        '.env' = [IO.File]::ReadAllText((Join-Path $fresh '.env'))
        'docker-compose.user.yml' = Get-UserComposeContent $newState
        '.drivebound/installation.json' = $newState | ConvertTo-Json -Depth 12
        '.drivebound/storage.json' = [IO.File]::ReadAllText((Join-Path $fresh '.drivebound/storage.json'))
    }
    Write-Host "Fresh test configuration ready. Database project: $project"
    Write-Host "Empty test photo folder: $(Join-Path $fresh 'photos')"
    Write-Host "Previous configuration retained privately at: $backup"
    Write-Host 'No existing Docker volume, account, or media file was deleted. Double-click Drivebound Setup to build and start the new test installation.'
} catch {
    Write-Host (Write-DriveboundFailure -ErrorRecord $_) -ForegroundColor Red
    exit 1
} finally { if ($lease) { $lease.Dispose() } }
