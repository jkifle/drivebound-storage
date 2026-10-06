param(
    [switch]$PrepareOnly,
    [switch]$KeepRunning,
    [string]$RunDirectory = '',
    [string]$PythonExecutable = '',
    [ValidateRange(30, 900)][int]$ReadinessTimeoutSeconds = 300,
    [ValidateRange(60, 3600)][int]$BuildTimeoutSeconds = 1800,
    [switch]$LibraryOnly
)

$ErrorActionPreference = 'Stop'
$Script:PilotRepository = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$Script:PilotRunsRoot = Join-Path $Script:PilotRepository '.drivebound/pilot-runs'
. (Join-Path $PSScriptRoot 'setup-state.ps1')

function Assert-PilotRunDirectory {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $parent = [IO.Path]::GetFullPath((Split-Path -Parent $full)).TrimEnd('\', '/')
    if ($parent -ne [IO.Path]::GetFullPath($Script:PilotRunsRoot).TrimEnd('\', '/') -or (Split-Path -Leaf $full) -cnotmatch '^[a-f0-9]{32}$') {
        throw 'Pilot artifacts must be in a uniquely named .drivebound/pilot-runs/<32 lowercase hex> folder.'
    }
    $cursor = $full
    while ($cursor) {
        if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Pilot paths cannot pass through junctions or symbolic links.' }
        $cursor = Split-Path -Parent $cursor
    }
    return $full
}

function Assert-PilotManifest {
    param($Manifest, [string]$Directory)
    $full = Assert-PilotRunDirectory $Directory
    $expected = 'drivebound-pilot-' + (Split-Path -Leaf $full)
    if ($Manifest.schema -ne 1 -or $Manifest.project_name -cne $expected -or $Manifest.owner_email -cne 'pilot-owner@example.com') { throw 'The pilot run identity is invalid.' }
    $api = $null; $app = $null
    if (-not [Uri]::TryCreate($Manifest.api_url, [UriKind]::Absolute, [ref]$api) -or -not [Uri]::TryCreate($Manifest.app_url, [UriKind]::Absolute, [ref]$app)) { throw 'The pilot URLs are invalid.' }
    if ($api.Scheme -ne 'http' -or $api.Host -cne '127.0.0.1' -or $app.Scheme -ne 'http' -or $app.Host -cne 'localhost' -or $api.Port -lt 10000 -or $app.Port -lt 10000 -or $api.Port -eq $app.Port -or $api.AbsolutePath -ne '/' -or $app.AbsolutePath -ne '/' -or $api.Query -or $app.Query -or $api.Fragment -or $app.Fragment -or $api.UserInfo -or $app.UserInfo) { throw 'Pilot URLs must use distinct high-numbered loopback ports.' }
    if ([IO.Path]::GetFullPath($Manifest.imports_path) -ne (Join-Path $full 'imports') -or [IO.Path]::GetFullPath($Manifest.report_path) -ne (Join-Path $full 'report.json')) { throw 'Pilot fixture or report paths escape their isolated run.' }
    return $Manifest
}

function Get-PilotPorts {
    $listeners = @()
    try {
        foreach ($i in 1..2) {
            $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
            $listener.Start()
            $listeners += $listener
        }
        $ports = @($listeners | ForEach-Object { $_.LocalEndpoint.Port })
        if ($ports[0] -lt 10000 -or $ports[1] -lt 10000) { throw 'Windows allocated a low port; retry preparation.' }
        return ,$ports
    } finally { foreach ($listener in $listeners) { $listener.Stop() } }
}

function Get-PilotOverride {
    param($Manifest)
    $backend = (Join-Path $Script:PilotRepository 'backend').Replace('\', '/').Replace("'", "''").Replace('$', '$$')
    $frontend = (Join-Path $Script:PilotRepository 'frontend').Replace('\', '/').Replace("'", "''").Replace('$', '$$')
    $apiPort = ([Uri]$Manifest.api_url).Port
    $appPort = ([Uri]$Manifest.app_url).Port
    return @"
# Disposable local integration only. Never use this profile for remote access.
services:
  backend:
    build:
      context: '$backend'
    ports: !override
      - '127.0.0.1:${apiPort}:8000'
    restart: 'no'
  worker:
    build:
      context: '$backend'
    restart: 'no'
  scheduler:
    build:
      context: '$backend'
    restart: 'no'
  frontend:
    build:
      context: '$frontend'
    ports: !override
      - '127.0.0.1:${appPort}:3000'
    restart: 'no'
  postgres:
    restart: 'no'
  redis:
    restart: 'no'
"@
}

function New-PilotPreparation {
    param([string]$PythonPath)
    $directory = Assert-PilotRunDirectory (Join-Path $Script:PilotRunsRoot ([Guid]::NewGuid().ToString('N')))
    [void][IO.Directory]::CreateDirectory($directory)
    Set-DriveboundPrivateAcl $directory
    $ports = Get-PilotPorts
    $manifest = [pscustomobject]@{schema = 1; project_name = 'drivebound-pilot-' + (Split-Path -Leaf $directory); api_url = "http://127.0.0.1:$($ports[0])"; app_url = "http://localhost:$($ports[1])"; owner_email = 'pilot-owner@example.com'; imports_path = Join-Path $directory 'imports'; report_path = Join-Path $directory 'report.json'}
    [void](Assert-PilotManifest $manifest $directory)
    Copy-Item -LiteralPath (Join-Path $Script:PilotRepository 'docker-compose.yml') -Destination (Join-Path $directory 'docker-compose.yml')
    $template = [IO.File]::ReadAllText((Join-Path $Script:PilotRepository '.env.example'))
    $overrides = @{COMPOSE_PROJECT_NAME = $manifest.project_name; ENVIRONMENT = 'development'; DEPLOYMENT_MODE = 'self_hosted'; REMOTE_ACCESS_ENABLED = 'false'; EMAIL_DELIVERY_MODE = 'console'; SEMANTIC_ENABLED = 'false'; OCR_ENABLED = 'false'; APP_URL = $manifest.app_url; API_URL = "http://localhost:$($ports[0])"; NEXT_PUBLIC_API_BASE_URL = "http://localhost:$($ports[0])"; NEXT_PUBLIC_APP_URL = $manifest.app_url; CORS_ORIGINS = $manifest.app_url; WEBAUTHN_RP_ID = 'localhost'; WEBAUTHN_ORIGINS = $manifest.app_url; MONITOR_INTERVAL_SECONDS = '30'}
    foreach ($name in $overrides.Keys) {
        $template = [regex]::Replace($template, "(?m)^$name=.*\r?\n?", '')
        $template += [Environment]::NewLine + "$name=$($overrides[$name])" + [Environment]::NewLine
    }
    Write-DurableText (Join-Path $directory '.env.example') $template
    [void][IO.Directory]::CreateDirectory($manifest.imports_path)
    $setup = Invoke-DriveboundNative -FilePath 'powershell.exe' -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'setup-drivebound.ps1'), '-Action', 'Setup', '-NoStart', '-NoBrowser', '-NonInteractive', '-OwnerEmail', $manifest.owner_email, '-ExistingPhotosPath', $manifest.imports_path, '-ManagedDataPath', (Join-Path $directory 'managed'), '-ProtectionDataPath', (Join-Path $directory 'protection'), '-ProjectRootOverride', $directory) -TimeoutSeconds 120
    if ($setup.ExitCode -ne 0) { throw "Isolated guided setup failed. Its private recovery artifacts remain in $directory; no existing installation was changed." }
    Write-DurableText (Join-Path $directory 'pilot-run.json') ($manifest | ConvertTo-Json -Depth 5)
    Write-DurableText (Join-Path $directory 'docker-compose.pilot.yml') (Get-PilotOverride $manifest)
    $fixtures = Invoke-DriveboundNative -FilePath $PythonPath -Arguments @((Join-Path $PSScriptRoot 'pilot_acceptance.py'), '--prepare-fixtures', $manifest.imports_path) -TimeoutSeconds 60
    if ($fixtures.ExitCode -ne 0) { throw "Disposable fixture preparation failed. Artifacts remain in $directory; no stack was started." }
    return $directory
}

function Get-PilotComposeArguments {
    param([string]$Directory, $Manifest, [string[]]$Tail = @())
    [void](Assert-PilotManifest $Manifest $Directory)
    return ,(@('compose', '--project-name', $Manifest.project_name, '--project-directory', $Directory, '--env-file', (Join-Path $Directory '.env'), '-f', (Join-Path $Directory 'docker-compose.yml'), '-f', (Join-Path $Directory 'docker-compose.user.yml'), '-f', (Join-Path $Directory 'docker-compose.pilot.yml')) + $Tail)
}

function Get-PilotDockerExecutable {
    $command = Get-Command docker.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in @(
        (Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'),
        (Join-Path $env:ProgramFiles 'Docker/Docker/resources/bin/docker.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs/Docker/Docker/resources/bin/docker.exe')
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw 'Docker CLI is unavailable. Open Docker Desktop yourself when ready; this runner never installs or starts it.'
}

function Assert-PilotDockerEndpoint {
    param([string]$Endpoint)
    # A named pipe can address another Windows computer. Only a single pipe on
    # this computer is allowed; normalize localhost to the local dot spelling.
    if ($Endpoint -notmatch '^npipe:////(?:\.|localhost)/pipe/([A-Za-z0-9][A-Za-z0-9_.-]*)$') {
        throw 'The Windows pilot requires a local Docker named pipe, not a remote or ambiguous Docker endpoint.'
    }
    return 'npipe:////./pipe/' + $Matches[1]
}

function Get-PilotDockerEndpoint {
    param([string]$DockerExecutable, [string]$DockerHost = $env:DOCKER_HOST, [string]$DockerContext = $env:DOCKER_CONTEXT)
    # DOCKER_CONTEXT takes precedence over DOCKER_HOST. Resolve that choice
    # once; all engine calls below explicitly pin the resulting local endpoint.
    if ($DockerHost -and -not $DockerContext) { return Assert-PilotDockerEndpoint $DockerHost }
    $arguments = @('context', 'inspect')
    if ($DockerContext) { $arguments += $DockerContext }
    $arguments += @('--format', '{{.Endpoints.docker.Host}}')
    $context = Invoke-DriveboundNative -FilePath $DockerExecutable -Arguments $arguments -TimeoutSeconds 10
    if ($context.ExitCode -ne 0) { throw 'The active Docker context could not be inspected. No context was changed.' }
    return Assert-PilotDockerEndpoint $context.StdOut.Trim()
}

function Invoke-PilotDocker {
    param([string]$DockerExecutable, [string]$Endpoint, [string[]]$Arguments, [int]$TimeoutSeconds)
    $pinned = @('--host', (Assert-PilotDockerEndpoint $Endpoint)) + $Arguments
    return Invoke-DriveboundNative -FilePath $DockerExecutable -Arguments $pinned -TimeoutSeconds $TimeoutSeconds
}

function Assert-PilotUnreportedRun {
    param($Manifest, [string]$Directory)
    [void](Assert-PilotManifest $Manifest $Directory)
    if (Test-Path -LiteralPath $Manifest.report_path) {
        throw 'This pilot run already has an acceptance report. Prepare a new disposable run; the previous evidence and containers were not changed.'
    }
}

function Get-PilotAcceptanceFailureMessage {
    param($Manifest, [string]$Directory, [bool]$TimedOut = $false)
    $reason = if ($TimedOut) { 'Pilot acceptance timed out.' } else { 'Pilot acceptance did not pass.' }
    if (Test-Path -LiteralPath $Manifest.report_path -PathType Leaf) {
        return "$reason Review the redacted report at $($Manifest.report_path); hardware and remote-access gates remain separate."
    }
    return "$reason No acceptance report was produced. Private pilot artifacts remain in $Directory; no runtime acceptance pass is credited. Hardware and remote-access gates remain separate."
}

function Assert-PilotComposeConfiguration {
    param($Configuration, $Manifest, [string]$Directory)
    [void](Assert-PilotManifest $Manifest $Directory)
    if ($Configuration.name -cne $Manifest.project_name) { throw 'Docker Compose resolved an unexpected project.' }
    $services = @('backend', 'worker', 'scheduler', 'frontend', 'postgres', 'redis')
    if (@($Configuration.services.PSObject.Properties.Name | Where-Object { $_ -notin $services }).Count -or @($Configuration.services.PSObject.Properties).Count -ne 6) { throw 'The pilot must contain exactly its six expected services.' }
    foreach ($name in $services) {
        $service = $Configuration.services.$name
        if ($service.container_name -or $service.privileged -or $service.network_mode -or $service.pid -or $service.volumes_from) { throw 'Unsafe container configuration in the pilot profile.' }
        foreach ($mount in $service.volumes) {
            if ($mount.type -eq 'bind') {
                $source = [IO.Path]::GetFullPath($mount.source)
                if (-not $source.StartsWith($Directory.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or $mount.bind.create_host_path -ne $false) { throw 'A bind mount escapes the pilot or may create a missing source.' }
                $cursor = $source
                while ($cursor -and $cursor -ne $Directory) {
                    if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Pilot bind sources cannot contain junctions or symbolic links.' }
                    $cursor = Split-Path -Parent $cursor
                }
            } elseif ($mount.type -ne 'volume' -or $mount.source -notin @('postgres_data', 'redis_data', 'model_cache')) { throw 'Unexpected pilot storage mount.' }
        }
        $ports = @($service.ports | Where-Object { $null -ne $_ })
        if ($name -in @('backend', 'frontend')) {
            $port = if ($name -eq 'backend') { ([Uri]$Manifest.api_url).Port } else { ([Uri]$Manifest.app_url).Port }
            $target = if ($name -eq 'backend') { 8000 } else { 3000 }
            if ($ports.Count -ne 1 -or $ports[0].host_ip -ne '127.0.0.1' -or [int]$ports[0].published -ne $port -or [int]$ports[0].target -ne $target) { throw 'Pilot ports are not isolated loopback bindings.' }
        } elseif ($ports.Count) { throw 'Only the pilot frontend and API may publish ports.' }
        if ($name -in @('backend', 'worker', 'scheduler', 'frontend')) {
            $expected = Join-Path $Script:PilotRepository $(if ($name -eq 'frontend') { 'frontend' } else { 'backend' })
            if ([IO.Path]::GetFullPath($service.build.context) -ne $expected) { throw 'Unexpected pilot build context.' }
        }
        if ($name -in @('backend', 'worker', 'scheduler')) {
            $database = [string]$service.environment.DATABASE_URL
            $redis = [string]$service.environment.REDIS_URL
            if ($database -notmatch '^postgresql\+asyncpg://[^/@:]+:[^/@]+@postgres:5432/[^/?#]+$' -or $redis -cne 'redis://redis:6379/0' -or $service.environment.ENVIRONMENT -cne 'development') { throw 'Pilot services must use their private local database and queue.' }
        }
        if ($name -eq 'backend') {
            if ($service.environment.REMOTE_ACCESS_ENABLED -cne 'false' -or $service.environment.EMAIL_DELIVERY_MODE -cne 'console' -or $service.environment.OCR_ENABLED -cne 'false' -or $service.environment.SEMANTIC_ENABLED -cne 'false' -or $service.environment.APP_URL -cne $Manifest.app_url -or $service.environment.API_URL -cne "http://localhost:$(([Uri]$Manifest.api_url).Port)") { throw 'The pilot environment is not the expected isolated local test profile.' }
        }
    }
    foreach ($entry in $Configuration.volumes.PSObject.Properties) {
        if ($entry.Name -notin @('postgres_data', 'redis_data', 'model_cache') -or $entry.Value.external -or $entry.Value.driver_opts -or $entry.Value.name -cne "$($Manifest.project_name)_$($entry.Name)") { throw 'Pilot named volumes must be private to its unique project.' }
    }
    foreach ($entry in $Configuration.networks.PSObject.Properties) {
        if ($entry.Name -ne 'default' -or $entry.Value.external -or $entry.Value.driver_opts -or $entry.Value.name -cne "$($Manifest.project_name)_default") { throw 'The pilot network must be private to its unique project.' }
    }
}

function Wait-PilotReadiness {
    param($Manifest, [int]$TimeoutSeconds)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $ready = Invoke-RestMethod -Uri "$($Manifest.api_url)/api/v1/ready" -TimeoutSec 3
            $frontend = Invoke-WebRequest -UseBasicParsing -Uri $Manifest.app_url -TimeoutSec 3
            if ($ready.status -eq 'ready' -and $frontend.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Seconds 2
    }
    throw 'The isolated frontend, API, database, queue, workers, scheduler, and storage did not become ready before the deadline.'
}

if ($LibraryOnly) { return }

if ($PrepareOnly -and $KeepRunning) { throw 'KeepRunning applies to a live run, not preparation.' }
if (-not $PythonExecutable) { $PythonExecutable = Join-Path $Script:PilotRepository '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw 'The project Python environment is required for disposable fixtures and acceptance checks.' }
$directory = if ($RunDirectory) { Assert-PilotRunDirectory $RunDirectory } else { New-PilotPreparation $PythonExecutable }
$manifest = Assert-PilotManifest (Get-Content -Raw -LiteralPath (Join-Path $directory 'pilot-run.json') | ConvertFrom-Json) $directory
$saved = Get-Content -Raw -LiteralPath (Join-Path $directory '.drivebound/installation.json') | ConvertFrom-Json
if ($saved.compose_project_name -cne $manifest.project_name -or $saved.owner_email -cne $manifest.owner_email -or [IO.Path]::GetFullPath($saved.paths.imports) -ne $manifest.imports_path) { throw 'The pilot manifest and guided setup identity disagree.' }
if ($PrepareOnly) { Write-Host "Prepared only: $directory. Docker was not contacted; runtime checks have not run."; return }
Assert-PilotUnreportedRun $manifest $directory

$docker = Get-PilotDockerExecutable
$dockerEndpoint = Get-PilotDockerEndpoint $docker
$engine = Invoke-PilotDocker -DockerExecutable $docker -Endpoint $dockerEndpoint -Arguments @('info', '--format', '{{.OSType}}') -TimeoutSeconds 10
if ($engine.ExitCode -ne 0 -or $engine.StdOut.Trim() -ne 'linux') { throw 'A running Docker Linux engine is required. This runner will not change Docker settings or existing projects.' }
$config = Invoke-PilotDocker -DockerExecutable $docker -Endpoint $dockerEndpoint -Arguments (Get-PilotComposeArguments $directory $manifest @('config', '--format', 'json')) -TimeoutSeconds 30
if ($config.ExitCode -ne 0) { throw 'Isolated Compose validation failed. A Compose release supporting !override is required. No containers were started.' }
Assert-PilotComposeConfiguration ($config.StdOut | ConvertFrom-Json) $manifest $directory
$attemptedStart = $false
try {
    $attemptedStart = $true
    Write-Host "Building and starting only $($manifest.project_name)..."
    $up = Invoke-PilotDocker -DockerExecutable $docker -Endpoint $dockerEndpoint -Arguments (Get-PilotComposeArguments $directory $manifest @('up', '--build', '-d')) -TimeoutSeconds $BuildTimeoutSeconds
    if ($up.ExitCode -ne 0) { throw 'The isolated build/start failed or timed out. No existing project was changed.' }
    Wait-PilotReadiness $manifest $ReadinessTimeoutSeconds
    $smoke = Invoke-DriveboundNative -FilePath $PythonExecutable -Arguments @((Join-Path $PSScriptRoot 'pilot_acceptance.py'), '--run-dir', $directory) -TimeoutSeconds 600
    if ($smoke.ExitCode -ne 0 -or $smoke.TimedOut) { throw (Get-PilotAcceptanceFailureMessage $manifest $directory ([bool]$smoke.TimedOut)) }
    Write-Host "Local pilot acceptance passed: $($manifest.report_path). This does not certify physical-disk, reboot, video, or external-phone behavior."
} finally {
    if ($attemptedStart -and -not $KeepRunning) {
        $down = Invoke-PilotDocker -DockerExecutable $docker -Endpoint $dockerEndpoint -Arguments (Get-PilotComposeArguments $directory $manifest @('down', '--timeout', '20')) -TimeoutSeconds 60
        if ($down.ExitCode -ne 0) { Write-Warning "Pilot shutdown needs attention for $($manifest.project_name). Its volumes and artifacts were retained." }
        else { Write-Host 'Only the pilot containers/network were removed. Named volumes and all run artifacts were retained.' }
    } elseif ($attemptedStart) { Write-Host "Pilot left running by request: $($manifest.project_name). Artifacts: $directory" }
}
