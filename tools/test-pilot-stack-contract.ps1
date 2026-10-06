$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'test-pilot-stack.ps1') -LibraryOnly

function Assert-PilotTest {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-PilotRejects {
    param([scriptblock]$Action, [string]$Message)
    $failed = $false
    try { & $Action | Out-Null } catch { $failed = $true }
    Assert-PilotTest $failed $Message
}

function New-ContractConfiguration {
    param($Manifest)
    $services = [ordered]@{}
    foreach ($name in @('backend', 'worker', 'scheduler', 'frontend', 'postgres', 'redis')) {
        $service = @{volumes = @(); environment = @{DATABASE_URL = 'postgresql+asyncpg://photos:disposable@postgres:5432/photos'; REDIS_URL = 'redis://redis:6379/0'; ENVIRONMENT = 'development'}}
        if ($name -in @('backend', 'worker', 'scheduler', 'frontend')) {
            $service.build = @{context = Join-Path $Script:PilotRepository $(if ($name -eq 'frontend') { 'frontend' } else { 'backend' })}
        }
        if ($name -eq 'backend') {
            $service.ports = @(@{host_ip = '127.0.0.1'; target = 8000; published = ([Uri]$Manifest.api_url).Port})
            foreach ($pair in @{REMOTE_ACCESS_ENABLED = 'false'; EMAIL_DELIVERY_MODE = 'console'; OCR_ENABLED = 'false'; SEMANTIC_ENABLED = 'false'; APP_URL = $Manifest.app_url; API_URL = "http://localhost:$(([Uri]$Manifest.api_url).Port)"}.GetEnumerator()) { $service.environment[$pair.Key] = $pair.Value }
        }
        if ($name -eq 'frontend') { $service.ports = @(@{host_ip = '127.0.0.1'; target = 3000; published = ([Uri]$Manifest.app_url).Port}) }
        $services[$name] = $service
    }
    $volumes = @{}
    foreach ($name in @('postgres_data', 'redis_data', 'model_cache')) { $volumes[$name] = @{name = "$($Manifest.project_name)_$name"} }
    return (@{name = $Manifest.project_name; services = $services; volumes = $volumes} | ConvertTo-Json -Depth 12 | ConvertFrom-Json)
}

$created = $null
$nativeCalls = New-Object Collections.Generic.List[object]
$contextEndpoint = 'npipe:////./pipe/dockerDesktopLinuxEngine'
# This preparation contract intentionally mocks subprocesses: it never calls
# Docker, setup, or Python. Their real contracts/runtime results are separate.
function Invoke-DriveboundNative {
    param([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSeconds)
    $nativeCalls.Add(@{file = $FilePath; arguments = $Arguments; timeout = $TimeoutSeconds})
    $output = if ($Arguments[0] -eq 'context') { $contextEndpoint } else { '' }
    return [pscustomobject]@{ExitCode = 0; StdOut = $output; StdErr = ''; TimedOut = $false}
}

try {
    $created = New-PilotPreparation 'fixture-python.exe'
    $manifest = Get-Content -Raw -LiteralPath (Join-Path $created 'pilot-run.json') | ConvertFrom-Json
    [void](Assert-PilotManifest $manifest $created)
    Assert-PilotTest ($nativeCalls.Count -eq 2) 'Preparation should invoke only isolated setup and fixture preparation.'
    Assert-PilotTest ($nativeCalls[0].file -eq 'powershell.exe' -and $nativeCalls[0].arguments -contains '-NoStart' -and $nativeCalls[0].arguments -contains '-NoBrowser' -and $nativeCalls[0].arguments -contains $created) 'Preparation can start an existing installation.'
    Assert-PilotTest ($nativeCalls[1].file -eq 'fixture-python.exe' -and $nativeCalls[1].arguments -contains '--prepare-fixtures' -and $nativeCalls[1].arguments -contains $manifest.imports_path) 'Fixture preparation is not confined to the pilot imports.'
    $template = Get-Content -Raw -LiteralPath (Join-Path $created '.env.example')
    Assert-PilotTest ($template.Contains("COMPOSE_PROJECT_NAME=$($manifest.project_name)")) 'The stable project name must be present before guided setup.'
    Assert-PilotTest ($template.Contains('REMOTE_ACCESS_ENABLED=false') -and $template.Contains('EMAIL_DELIVERY_MODE=console') -and $template.Contains('SEMANTIC_ENABLED=false') -and $template.Contains('OCR_ENABLED=false')) 'The preparation profile is not explicitly disposable/local.'
    $overlay = Get-Content -Raw -LiteralPath (Join-Path $created 'docker-compose.pilot.yml')
    Assert-PilotTest (([regex]::Matches($overlay, 'ports: !override')).Count -eq 2) 'Default production/development ports could remain merged into the pilot.'
    Assert-PilotTest ($overlay.Contains("127.0.0.1:$(([Uri]$manifest.api_url).Port):8000") -and $overlay.Contains("127.0.0.1:$(([Uri]$manifest.app_url).Port):3000")) 'Pilot port bindings differ from its manifest.'
    Assert-PilotTest (-not (Test-Path -LiteralPath (Join-Path $created '.env'))) 'Mock preparation unexpectedly copied or generated a private environment.'
    $arguments = Get-PilotComposeArguments $created $manifest @('down', '--timeout', '20')
    Assert-PilotTest ($arguments -contains '--project-name' -and $arguments -contains $manifest.project_name -and $arguments -contains '--env-file' -and $arguments -contains (Join-Path $created '.env') -and $arguments -notcontains '-v') 'Shutdown must retain volumes and name its isolated project explicitly.'

    Assert-PilotRejects { Assert-PilotRunDirectory $Script:PilotRepository } 'The workspace root was accepted as a run.'
    Assert-PilotRejects { Assert-PilotRunDirectory (Join-Path $Script:PilotRunsRoot 'existing-installation') } 'A non-unique run name was accepted.'
    $invalid = $manifest | ConvertTo-Json | ConvertFrom-Json
    $invalid.project_name = 'media-storage'
    Assert-PilotRejects { Assert-PilotManifest $invalid $created } 'The real project name was accepted.'
    $invalid = $manifest | ConvertTo-Json | ConvertFrom-Json
    $invalid.api_url = 'http://127.0.0.1:8000'
    Assert-PilotRejects { Assert-PilotManifest $invalid $created } 'The normal API port was accepted.'
    $invalid.api_url = 'http://example.com:18000'
    Assert-PilotRejects { Assert-PilotManifest $invalid $created } 'A remote host was accepted.'
    $invalid = $manifest | ConvertTo-Json | ConvertFrom-Json
    $invalid.imports_path = Join-Path $Script:PilotRepository 'data/imports'
    Assert-PilotRejects { Assert-PilotManifest $invalid $created } 'Real imports were accepted as disposable fixtures.'

    $config = New-ContractConfiguration $manifest
    Assert-PilotComposeConfiguration $config $manifest $created
    $config.services.backend.ports[0].host_ip = '0.0.0.0'
    Assert-PilotRejects { Assert-PilotComposeConfiguration $config $manifest $created } 'A public interface was accepted.'
    $config = New-ContractConfiguration $manifest
    $config.volumes.postgres_data.name = 'media-storage_postgres_data'
    Assert-PilotRejects { Assert-PilotComposeConfiguration $config $manifest $created } 'An existing PostgreSQL volume was accepted.'
    $config = New-ContractConfiguration $manifest
    $config.services.backend.environment.DATABASE_URL = 'postgresql+asyncpg://user:password@production.example:5432/photos'
    Assert-PilotRejects { Assert-PilotComposeConfiguration $config $manifest $created } 'An inherited remote database was accepted.'
    $config = New-ContractConfiguration $manifest
    $config.services.backend.volumes = @([pscustomobject]@{type = 'bind'; source = Join-Path $Script:PilotRepository 'data/originals'; target = '/data/originals'; bind = @{create_host_path = $false}})
    Assert-PilotRejects { Assert-PilotComposeConfiguration $config $manifest $created } 'A bind into actual managed storage was accepted.'
    $config.services.backend.volumes[0].source = Join-Path $created 'managed/originals'
    $config.services.backend.volumes[0].bind.create_host_path = $true
    Assert-PilotRejects { Assert-PilotComposeConfiguration $config $manifest $created } 'Automatic missing-host-directory creation was accepted.'

    $beforePreflight = $nativeCalls.Count
    Assert-PilotUnreportedRun $manifest $created
    $failure = Get-PilotAcceptanceFailureMessage $manifest $created
    Assert-PilotTest ($failure.Contains('did not pass') -and $failure.Contains('No acceptance report was produced') -and -not $failure.Contains('Review the redacted report')) 'A failed acceptance process without a report produced misleading guidance.'
    $failure = Get-PilotAcceptanceFailureMessage $manifest $created $true
    Assert-PilotTest ($failure.Contains('timed out') -and $failure.Contains('No acceptance report was produced') -and $failure.Contains('no runtime acceptance pass is credited')) 'A timed-out acceptance process without a report produced misleading guidance.'
    Write-DurableText $manifest.report_path '{"schema":1,"status":"failed","checks":[]}'
    Assert-PilotRejects { Assert-PilotUnreportedRun $manifest $created } 'An already reported run was accepted for another start.'
    Assert-PilotTest ($nativeCalls.Count -eq $beforePreflight) 'Reported-run preflight or diagnostics contacted a service.'
    $failure = Get-PilotAcceptanceFailureMessage $manifest $created
    Assert-PilotTest ($failure.Contains('Review the redacted report') -and $failure.Contains($manifest.report_path) -and -not $failure.Contains('No acceptance report')) 'A retained failure report was not identified accurately.'
    $failure = Get-PilotAcceptanceFailureMessage $manifest $created $true
    Assert-PilotTest ($failure.Contains('timed out') -and $failure.Contains('Review the redacted report')) 'Timeout guidance ignored a report that was already retained.'

    foreach ($endpoint in @('npipe:////./pipe/dockerDesktopLinuxEngine', 'npipe:////localhost/pipe/dockerDesktopLinuxEngine')) {
        Assert-PilotTest ((Assert-PilotDockerEndpoint $endpoint) -ceq 'npipe:////./pipe/dockerDesktopLinuxEngine') 'A local Docker pipe was not normalized and accepted.'
    }
    foreach ($endpoint in @('npipe:////another-pc/pipe/docker_engine', 'npipe://another-pc/pipe/docker_engine', 'tcp://127.0.0.1:2375', 'ssh://example.com', 'npipe:////./pipe/../docker_engine', 'npipe:////./pipe/docker_engine?host=other', 'npipe:////./pipe/docker_engine/extra')) {
        Assert-PilotRejects { Assert-PilotDockerEndpoint $endpoint } 'A nonlocal or ambiguous Docker endpoint was accepted.'
    }
    $nativeCalls.Clear()
    $endpoint = Get-PilotDockerEndpoint -DockerExecutable 'fixture-docker.exe' -DockerHost 'npipe:////./pipe/host-engine' -DockerContext ''
    Assert-PilotTest ($endpoint -ceq 'npipe:////./pipe/host-engine' -and $nativeCalls.Count -eq 0) 'DOCKER_HOST should be validated directly when it selects the engine.'
    $endpoint = Get-PilotDockerEndpoint -DockerExecutable 'fixture-docker.exe' -DockerHost 'tcp://unused.example:2375' -DockerContext 'selected-local'
    Assert-PilotTest ($endpoint -ceq $contextEndpoint -and $nativeCalls[0].arguments -contains 'selected-local') 'An explicit Docker context must take precedence over DOCKER_HOST.'
    $contextEndpoint = 'npipe:////another-pc/pipe/docker_engine'
    Assert-PilotRejects { Get-PilotDockerEndpoint -DockerExecutable 'fixture-docker.exe' -DockerHost '' -DockerContext '' } 'A remote named-pipe Docker context was accepted.'
    $nativeCalls.Clear()
    foreach ($tail in @(@('info', '--format', '{{.OSType}}'), @('config', '--format', 'json'), @('up', '--build', '-d'), @('down', '--timeout', '20'))) {
        [void](Invoke-PilotDocker -DockerExecutable 'fixture-docker.exe' -Endpoint $endpoint -Arguments $tail -TimeoutSeconds 10)
    }
    Assert-PilotTest ($nativeCalls.Count -eq 4) 'Pinned Docker calls were not all issued.'
    foreach ($call in $nativeCalls) {
        Assert-PilotTest ($call.arguments[0] -ceq '--host' -and $call.arguments[1] -ceq $endpoint) 'An engine operation could follow a changed Docker context instead of the validated endpoint.'
    }
    & {
        function Get-Command { param($Name, $CommandType, $ErrorAction); return $null }
        function Test-Path { param($LiteralPath, $PathType); return $LiteralPath -eq (Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe') }
        $resolved = Get-PilotDockerExecutable
        Assert-PilotTest ($resolved -eq (Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe')) 'A per-user Docker Desktop install was not found outside PATH.'
    }
    & {
        function Get-Command { param($Name, $CommandType, $ErrorAction); return $null }
        function Test-Path { param($LiteralPath, $PathType); return $LiteralPath -eq (Join-Path $env:ProgramFiles 'Docker/Docker/resources/bin/docker.exe') }
        $resolved = Get-PilotDockerExecutable
        Assert-PilotTest ($resolved -eq (Join-Path $env:ProgramFiles 'Docker/Docker/resources/bin/docker.exe')) 'An all-user Docker Desktop install was not found outside PATH.'
    }
    Write-Host 'Pilot runner contracts passed (mocked preparation; no Docker/runtime checks performed).' -ForegroundColor Green
} finally {
    if ($created -and (Test-Path -LiteralPath $created)) {
        $exact = Assert-PilotRunDirectory $created
        Remove-Item -LiteralPath $exact -Recurse -Force
    }
}
