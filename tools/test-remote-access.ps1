param([switch]$ComposeRoundTrip, [string]$DockerExecutable = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'remote-access.ps1') -LibraryOnly -NonInteractive -NoBrowser
$script:RemoteAssertions = 0
function Assert-Remote([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:RemoteAssertions++
}
function Assert-RemoteRejected([scriptblock]$Action, [string]$Pattern) {
    try { & $Action } catch {
        Assert-Remote ($_.Exception.Message -match $Pattern) "Unexpected failure: $($_.Exception.Message)"
        return
    }
    throw "Expected remote rejection: $Pattern"
}
$dns = Get-RemoteHostname ([pscustomobject]@{BackendState='Running'; Self=[pscustomobject]@{DNSName='pc.pilot.ts.net.'}})
Assert-Remote ($dns -eq 'pc.pilot.ts.net') 'Tailscale hostname was not normalized.'
Assert-RemoteRejected { Get-RemoteHostname ([pscustomobject]@{BackendState='Stopped'}) } 'connected'
Assert-RemoteRejected { Get-RemoteHostname ([pscustomobject]@{BackendState='Running'; Self=[pscustomobject]@{DNSName='attacker.example/route'}}) } 'MagicDNS'
$empty = '{}' | ConvertFrom-Json
Assert-ServeRoutesSafe $empty $dns $false
Assert-RemoteRejected { Assert-ServeRoutesSafe $empty $dns $true -RequireComplete } 'did not become available'
$routes = '{"TCP":{"443":{"HTTPS":true},"8443":{"HTTPS":true}},"Web":{"pc.pilot.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3000"}}},"pc.pilot.ts.net:8443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8000"}}}}}' | ConvertFrom-Json
Assert-ServeRoutesSafe $routes $dns $true -RequireComplete
Assert-RemoteRejected { Assert-ServeRoutesSafe $routes $dns $false } 'already has a service'
$routes | Add-Member -NotePropertyName AllowFunnel -NotePropertyValue ([pscustomobject]@{'pc.pilot.ts.net:443'=$true})
Assert-RemoteRejected { Assert-ServeRoutesSafe $routes $dns $true } 'Funnel'
$routes.AllowFunnel.'pc.pilot.ts.net:443' = $false
$routes.Web.'pc.pilot.ts.net:443'.Handlers.'/'.Proxy = 'http://127.0.0.1:9000'
Assert-RemoteRejected { Assert-ServeRoutesSafe $routes $dns $true } 'already has a service'
$existing = @{
    ENVIRONMENT='development'; JWT_SECRET=('j'*64); MFA_ENCRYPTION_SECRET=('m'*64)
    MEDIA_ENCRYPTION_MASTER_KEY='not-a-real-key-for-config-only-test'; METRICS_AUTH_TOKEN=('t'*64)
    POSTGRES_PASSWORD=('p'*64); DATABASE_URL='postgresql+asyncpg://photos:fixture@postgres/photos'
    MEDIA_ENCRYPTION_ENABLED='true'; CUSTOM_SETTING='preserved'
}
$mail = @{Host='smtp.example.test'; Port='587'; Username='owner@example.test'; Password='quote''cash$secret'; From='owner@example.test'}
$values = Get-RemoteEnvironment $existing $dns $mail
foreach ($key in @('JWT_SECRET','MFA_ENCRYPTION_SECRET','MEDIA_ENCRYPTION_MASTER_KEY','POSTGRES_PASSWORD','DATABASE_URL','CUSTOM_SETTING')) {
    Assert-Remote ($values[$key] -ceq $existing[$key]) "Remote setup changed $key."
}
Assert-Remote ($values.REMOTE_ACCESS_ENABLED -eq 'true') 'Remote security gate disabled.'
Assert-Remote ($values.APP_URL -eq 'https://pc.pilot.ts.net' -and $values.API_URL -eq 'https://pc.pilot.ts.net:8443') 'Remote origin mismatch.'
Assert-Remote ($values.NEXT_PUBLIC_API_BASE_URL -eq $values.API_URL -and $values.NEXT_PUBLIC_APP_URL -eq $values.APP_URL) 'Frontend origin mismatch.'
Assert-Remote ($values.CORS_ORIGINS -eq $values.APP_URL -and $values.WEBAUTHN_ORIGINS -eq $values.APP_URL -and $values.WEBAUTHN_RP_ID -eq $dns) 'Cookie/passkey origin mismatch.'
Assert-Remote ($values.AUTH_COOKIE_SECURE -eq 'true' -and $values.AUTH_COOKIE_NAME.StartsWith('__Host-') -and $values.REFRESH_COOKIE_NAME.StartsWith('__Host-')) 'Insecure cookies.'
Assert-Remote ($values.EMAIL_DELIVERY_MODE -eq 'smtp' -and $values.SMTP_USE_TLS -eq 'true') 'Remote SMTP guard weakened.'
Assert-Remote ($values.TRUSTED_PROXY_CIDRS -eq '') 'Proxy trust was broadened.'
Assert-Remote ($values.SMTP_PASSWORD -ceq '"quote''cash$$secret"') 'SMTP password literal escaped incorrectly.'
Assert-RemoteRejected { ConvertTo-DotEnvLiteral "one`ntwo" } 'single line'
$unsafe = @{} + $existing; $unsafe.ENVIRONMENT = 'production'
Assert-RemoteRejected { Get-RemoteEnvironment $unsafe $dns $mail } 'file-backed'
$unsafe = @{} + $existing; $unsafe.MEDIA_ENCRYPTION_MASTER_KEY = $Script:DevelopmentMediaKey
Assert-RemoteRejected { Get-RemoteEnvironment $unsafe $dns $mail } 'sample value'
$unsafeMail = @{} + $mail; $unsafeMail.Host = 'bad/host'
Assert-RemoteRejected { Get-RemoteEnvironment $existing $dns $unsafeMail } 'SMTP hostname'
& {
    $script:RemovedPorts = New-Object 'System.Collections.Generic.List[string]'
    function Invoke-DriveboundNative {
        param([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSeconds)
        $script:RemovedPorts.Add($Arguments[1])
        if ($Arguments[1] -eq '--https=443') { throw 'disposable native process failure' }
        return [pscustomobject]@{ExitCode = 0; StdOut = ''; StdErr = ''; TimedOut = $false}
    }
    Assert-Remote (-not (Remove-NewDriveboundServeRoutes 'fixture' @(443,8443))) 'Failed route cleanup was incorrectly credited as successful.'
    Assert-Remote ($script:RemovedPorts.Count -eq 2 -and $script:RemovedPorts[1] -eq '--https=8443') 'A failed route removal prevented remaining cleanup.'
}
& {
    function Invoke-DriveboundNative {
        param([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSeconds)
        return [pscustomobject]@{ExitCode = 0; StdOut = '{"BackendState":"Running"}'; StdErr = 'disposable warning'; TimedOut = $false}
    }
    $status = Invoke-TailscaleJson 'fixture' @('status','--json')
    Assert-Remote ($status.BackendState -eq 'Running') 'Native stderr polluted Tailscale JSON.'
}
if ($ComposeRoundTrip) {
    if (-not $DockerExecutable) { throw 'Provide the Docker executable for the optional config-only parser test.' }
    $fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('drivebound-remote-parser-' + [Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($fixtureRoot)
    try {
        $fixtureEnv = Join-Path $fixtureRoot 'fixture.env'
        $fixtureCompose = Join-Path $fixtureRoot 'compose.yml'
        $samples = @('trailing\', 'two\\slashes', 'slash\''quote', 'cash${UNSET_VARIABLE}$', 'double"quote', 'space and # comment', "tab`tvalue", '')
        $environmentLines = @()
        $composeLines = @('services:', '  fixture:', '    image: scratch', '    environment:')
        for ($index = 0; $index -lt $samples.Count; $index++) {
            $environmentLines += "FIXTURE_$index=$(ConvertTo-DotEnvLiteral $samples[$index])"
            $composeLines += ('      FIXTURE_{0}: ${{FIXTURE_{0}}}' -f $index)
        }
        Write-DurableText $fixtureEnv (($environmentLines -join "`n") + "`n")
        Write-DurableText $fixtureCompose (($composeLines -join "`n") + "`n")
        $parsed = Invoke-DriveboundNative -FilePath $DockerExecutable -Arguments @('compose', '--project-name', 'drivebound-parser-fixture', '--project-directory', $fixtureRoot, '--env-file', $fixtureEnv, '-f', $fixtureCompose, 'config', '--format', 'json', '--no-env-resolution') -TimeoutSeconds 30
        Assert-Remote ($parsed.ExitCode -eq 0) 'Docker Compose rejected a generated disposable environment fixture.'
        $configuration = $parsed.StdOut | ConvertFrom-Json
        for ($index = 0; $index -lt $samples.Count; $index++) {
            $value = Get-RemoteProperty $configuration.services.fixture.environment "FIXTURE_$index"
            Assert-Remote ($value -ceq $samples[$index]) "Compose credential roundtrip failed for fixture $index."
        }
        Write-Host 'Actual Docker Compose credential roundtrip passed; config only, no daemon or email used.' -ForegroundColor Green
    } finally {
        $resolved = [IO.Path]::GetFullPath($fixtureRoot)
        $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $resolved.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -or -not (Split-Path -Leaf $resolved).StartsWith('drivebound-remote-parser-')) { throw 'Unsafe parser fixture cleanup path.' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
Write-Host "Remote access contracts passed ($script:RemoteAssertions assertions; no services changed or email sent)." -ForegroundColor Green
