param([switch]$LibraryOnly, [switch]$NonInteractive, [switch]$NoBrowser, [string]$ProjectRootOverride = "")

$ErrorActionPreference = "Stop"
$Script:RemoteLibraryOnly = $LibraryOnly
$Script:RemoteNoBrowser = $NoBrowser
. (Join-Path $PSScriptRoot "setup-drivebound.ps1") -LibraryOnly -NonInteractive:$NonInteractive -NoBrowser:$NoBrowser -ProjectRootOverride $ProjectRootOverride
$NoBrowser = $true

function Get-RemoteProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $null
}

function Get-TailscaleExecutable {
    $command = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidate = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    throw "Install Tailscale from https://tailscale.com/download/windows, sign in on this PC, and open Drivebound Remote Access again."
}

function Invoke-TailscaleJson {
    param([string]$Executable, [string[]]$Arguments)
    $result = Invoke-DriveboundNative -FilePath $Executable -Arguments $Arguments -TimeoutSeconds 20
    if ($result.ExitCode -ne 0) { throw "Tailscale is not ready. Open Tailscale, sign in, then retry Remote Access." }
    try { return $result.StdOut | ConvertFrom-Json } catch { throw "Tailscale returned an unreadable status. Update Tailscale and retry." }
}

function Get-RemoteHostname {
    param($Status)
    if ($Status.BackendState -ne "Running") { throw "Tailscale must be connected on this PC before remote access can be configured." }
    $name = ([string]$Status.Self.DNSName).TrimEnd('.').ToLowerInvariant()
    if ($name -notmatch '^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.ts\.net$') {
        throw "Enable MagicDNS in your Tailscale network. This PC needs its own name ending in .ts.net."
    }
    return $name
}

function Assert-ServeRoutesSafe {
    param($Configuration, [string]$DnsName, [bool]$PreviouslyConfigured, [switch]$RequireComplete)
    foreach ($port in @(443, 8443)) {
        $key = "${DnsName}:$port"
        $funnel = Get-RemoteProperty $Configuration.AllowFunnel $key
        if ($funnel) { throw "Tailscale Funnel is enabled for port $port. Disable public Funnel for Drivebound before using this private-access wizard." }
        $tcp = Get-RemoteProperty $Configuration.TCP ([string]$port)
        $web = Get-RemoteProperty $Configuration.Web $key
        if (-not $tcp -and -not $web) {
            if ($RequireComplete) { throw "Tailscale port $port did not become available." }
            continue
        }
        $localPort = if ($port -eq 443) { 3000 } else { 8000 }
        $handlers = if ($web) { @($web.Handlers.PSObject.Properties) } else { @() }
        if (-not $PreviouslyConfigured -or -not $tcp -or -not $tcp.HTTPS -or $tcp.TCPForward -or $handlers.Count -ne 1 -or $handlers[0].Name -ne '/' -or $handlers[0].Value.Proxy -ne "http://127.0.0.1:$localPort") {
            throw "Tailscale port $port already has a service. Nothing was changed. Ask the PC owner to free this port or review its existing configuration; Drivebound will not overwrite another service."
        }
    }
}

function ConvertTo-DotEnvLiteral {
    param([string]$Value)
    if ($Value -match '[\r\n\x00]') { throw "Settings must be entered on a single line." }
    # Compose double-quoted values support escaped backslash/quote and $$ for
    # a literal dollar. Single quotes cannot safely represent a trailing slash.
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"').Replace('$', '$$') + '"'
}

function Remove-NewDriveboundServeRoutes {
    param([string]$Executable, [int[]]$Ports)
    $allRemoved = $true
    foreach ($port in $Ports) {
        try {
            $result = Invoke-DriveboundNative -FilePath $Executable -Arguments @('serve', "--https=$port", 'off') -TimeoutSeconds 20
            if ($result.ExitCode -ne 0) { $allRemoved = $false }
        } catch { $allRemoved = $false }
    }
    return $allRemoved
}

function Get-RemoteEnvironment {
    param([hashtable]$Existing, [string]$DnsName, [hashtable]$Mail)
    if ($Existing.ENVIRONMENT -in @('production', 'staging')) {
        throw "This wizard is for the stationary-PC pilot. Keep this production installation's file-backed secrets and use its production deployment guide. No settings were changed."
    }
    foreach ($key in @('JWT_SECRET', 'MFA_ENCRYPTION_SECRET', 'MEDIA_ENCRYPTION_MASTER_KEY', 'METRICS_AUTH_TOKEN', 'POSTGRES_PASSWORD', 'DATABASE_URL')) {
        if (Test-PlaceholderSecret -Value $Existing[$key] -KnownPlaceholders @($Script:DevelopmentMediaKey)) { throw "The existing installation's $key is missing or still a sample value. Run Drivebound Setup or recover its original configuration first. Remote Access will not replace encryption keys." }
    }
    foreach ($key in @('JWT_SECRET', 'MFA_ENCRYPTION_SECRET', 'METRICS_AUTH_TOKEN')) {
        if ($Existing[$key].Length -lt 32) { throw "The existing installation has an unsafe $key. Resolve this with the host owner before remote access; no credentials were replaced." }
    }
    if ($Existing.MEDIA_ENCRYPTION_ENABLED -ne 'true') { throw "Enable media encryption through the deployment recovery guide before remote access." }
    if (-not $Mail.Host -or $Mail.Host -match '[\s/\\:#\x00]' -or [int]$Mail.Port -lt 1 -or [int]$Mail.Port -gt 65535) { throw "Enter a valid SMTP hostname and port. This wizard requires STARTTLS (usually port 587)." }
    try { [void][Net.Mail.MailAddress]::new($Mail.From) } catch { throw "Enter a valid sender email address." }
    $values = @{} + $Existing
    $origin = "https://$DnsName"
    $api = "${origin}:8443"
    $updates = @{
        DEPLOYMENT_MODE = 'self_hosted'; REMOTE_ACCESS_ENABLED = 'true'; APP_URL = $origin; API_URL = $api
        NEXT_PUBLIC_APP_URL = $origin; NEXT_PUBLIC_API_BASE_URL = $api; CORS_ORIGINS = $origin
        AUTH_COOKIE_SECURE = 'true'; AUTH_COOKIE_NAME = '__Host-drivebound_session'; REFRESH_COOKIE_NAME = '__Host-drivebound_refresh'
        WEBAUTHN_RP_ID = $DnsName; WEBAUTHN_ORIGINS = $origin; GOOGLE_REDIRECT_URI = "$api/api/v1/auth/google/callback"
        TRUSTED_HOSTS = "$DnsName,localhost,127.0.0.1"; TRUSTED_PROXY_CIDRS = ''
        EMAIL_DELIVERY_MODE = 'smtp'; SMTP_HOST = $Mail.Host; SMTP_PORT = [string]$Mail.Port
        SMTP_USERNAME = (ConvertTo-DotEnvLiteral $Mail.Username); SMTP_PASSWORD = (ConvertTo-DotEnvLiteral $Mail.Password)
        SMTP_FROM = (ConvertTo-DotEnvLiteral $Mail.From); SMTP_USE_TLS = 'true'
    }
    foreach ($key in $updates.Keys) { $values[$key] = $updates[$key] }
    return $values
}

function Request-RemoteMailSettings {
    param([string]$OwnerEmail, [string]$DnsName)
    if ($NonInteractive) { throw "Remote Access needs the PC owner's email-provider settings. Open Drivebound Remote Access interactively." }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object Windows.Forms.Form
    $form.Text = 'Drivebound - private remote access'
    $form.ClientSize = [Drawing.Size]::new(590, 540)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $intro = New-Object Windows.Forms.Label
    $intro.SetBounds(20, 15, 550, 95)
    $intro.Text = "Your address will be https://$DnsName`r`n`r`nDrivebound needs email delivery for account verification and recovery. Get SMTP settings and an app password from your email provider. Your password stays on this PC. Continuing sends one test email to the approved owner."
    $form.Controls.Add($intro)
    $inputs = @{}
    $fields = @(@('Host', 'SMTP server', ''), @('Port', 'STARTTLS port', '587'), @('Username', 'SMTP username', ''), @('Password', 'SMTP app password', ''), @('From', 'Sender email', $OwnerEmail))
    $y = 120
    foreach ($field in $fields) {
        $label = New-Object Windows.Forms.Label
        $label.Text = $field[1]; $label.SetBounds(20, $y + 5, 155, 25)
        $input = New-Object Windows.Forms.TextBox
        $input.Text = $field[2]; $input.SetBounds(180, $y, 385, 28)
        if ($field[0] -eq 'Password') { $input.UseSystemPasswordChar = $true }
        $inputs[$field[0]] = $input
        $form.Controls.Add($label); $form.Controls.Add($input)
        $y += 52
    }
    $notice = New-Object Windows.Forms.Label
    $notice.SetBounds(20, 390, 550, 60)
    $notice.Text = 'Only devices allowed by your Tailscale network can connect. This changes your sign-in address; existing localhost passkeys will need to be re-enrolled. Password or Google sign-in remains available. Update Google callback settings if you use Google sign-in.'
    $form.Controls.Add($notice)
    $ok = New-Object Windows.Forms.Button
    $ok.Text = 'Test email and enable'; $ok.SetBounds(340, 470, 225, 38); $ok.DialogResult = 'OK'
    $cancel = New-Object Windows.Forms.Button
    $cancel.Text = 'Cancel'; $cancel.SetBounds(200, 470, 125, 38); $cancel.DialogResult = 'Cancel'
    $form.Controls.Add($ok); $form.Controls.Add($cancel)
    $form.AcceptButton = $ok; $form.CancelButton = $cancel
    try {
        if ($form.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { return $null }
        $mail = @{}
        foreach ($key in $inputs.Keys) { $mail[$key] = $inputs[$key].Text }
        return $mail
    } finally { $form.Dispose() }
}

function Send-RemoteTestEmail {
    param([hashtable]$Mail, [string]$Recipient)
    $client = [Net.Mail.SmtpClient]::new($Mail.Host, [int]$Mail.Port)
    $client.EnableSsl = $true; $client.Timeout = 15000
    if ($Mail.Username) { $client.Credentials = [Net.NetworkCredential]::new($Mail.Username, $Mail.Password) }
    $message = [Net.Mail.MailMessage]::new($Mail.From, $Recipient, 'Drivebound email setup check', 'Your Drivebound PC reached your email provider. Return to the setup window to finish private remote access. This message contains no sign-in code or secrets.')
    try { $client.Send($message) }
    catch { throw "The verification email could not be sent. Check the SMTP server, STARTTLS port, sender and app password with your email provider. No remote settings were applied." }
    finally { $message.Dispose(); $client.Dispose() }
}

function Enable-DriveboundRemoteAccess {
    $lock = Enter-InstallationLock
    try {
        Recover-ConfigurationTransaction
        $state = Get-InstallationState
        if (-not $state -or -not $state.owner_email) { throw "Run Drivebound Setup first and approve the account that owns this PC's photo folder." }
        $oldEnvironment = Read-EnvironmentMap -Path $Script:EnvironmentPath
        $tailscale = Get-TailscaleExecutable
        $dns = Get-RemoteHostname (Invoke-TailscaleJson $tailscale @('status', '--json'))
        $serve = Invoke-TailscaleJson $tailscale @('serve', 'status', '--json')
        $previouslyConfigured = $oldEnvironment.REMOTE_ACCESS_ENABLED -eq 'true' -and $oldEnvironment.APP_URL -eq "https://$dns"
        Assert-ServeRoutesSafe $serve $dns $previouslyConfigured
        $mail = Request-RemoteMailSettings $state.owner_email $dns
        if (-not $mail) { return }
        $values = Get-RemoteEnvironment $oldEnvironment $dns $mail
        Send-RemoteTestEmail $mail $state.owner_email
        $oldContent = [IO.File]::ReadAllText($Script:EnvironmentPath)
        Invoke-ConfigurationTransaction -Contents @{ '.env' = (Get-EnvironmentContent -Values $values) }
        $createdPorts = New-Object 'System.Collections.Generic.List[int]'
        try {
            # Publish only after the complete stack has loaded HTTPS/cookie/SMTP guards.
            # Build is required because the browser API origin is compiled into frontend assets.
            Start-Drivebound -Build
            foreach ($port in @(443, 8443)) {
                $localPort = if ($port -eq 443) { 3000 } else { 8000 }
                $existingRoute = Get-RemoteProperty $serve.TCP ([string]$port)
                if ($existingRoute) { continue }
                $latestServe = Invoke-TailscaleJson $tailscale @('serve', 'status', '--json')
                if (Get-RemoteProperty $latestServe.TCP ([string]$port)) { throw 'A Tailscale route changed during setup. Retry without other Tailscale configuration running.' }
                # Journal intent in memory before the subprocess: on an ambiguous failure,
                # turning off only this previously absent port cannot remove another service.
                $createdPorts.Add($port)
                $routeResult = Invoke-DriveboundNative -FilePath $tailscale -Arguments @('serve', '--bg', '--yes', "--https=$port", "http://127.0.0.1:$localPort") -TimeoutSeconds 60
                if ($routeResult.ExitCode -ne 0) { throw 'Tailscale Serve could not start. Enable HTTPS certificates in your Tailscale network and retry.' }
            }
            $verifiedServe = Invoke-TailscaleJson $tailscale @('serve', 'status', '--json')
            Assert-ServeRoutesSafe $verifiedServe $dns $true -RequireComplete
            [void](Invoke-WebRequest -UseBasicParsing -Uri "https://$dns/" -TimeoutSec 15)
            [void](Invoke-RestMethod -Uri "https://${dns}:8443/api/v1/ready" -TimeoutSec 15)
        } catch {
            $routesRemoved = Remove-NewDriveboundServeRoutes -Executable $tailscale -Ports $createdPorts.ToArray()
            if (-not $routesRemoved) {
                throw "Remote setup did not finish and a new Tailscale route could not be removed. Secure HTTPS settings were kept. Close Tailscale or disable Drivebound's routes, then retry. Do not switch back to local settings while these routes are active."
            }
            Invoke-ConfigurationTransaction -Contents @{ '.env' = $oldContent }
            try { Start-Drivebound -Build } catch { }
            throw "Remote setup did not pass its connection check. Previous configuration was restored. Open Drivebound Start to check local service health, enable Tailscale HTTPS if needed, then retry Remote Access. Existing Tailscale services were preserved."
        }
        if (-not $Script:RemoteNoBrowser) { Start-Process "https://$dns" -WindowStyle Hidden }
        Show-DriveboundMessage -Text "Private remote access is ready on this PC: https://$dns`r`n`r`nInstall and sign in to Tailscale on your phone, turn off Wi-Fi, and open this address. The mobile app server address is https://${dns}:8443. Test sign-in, browsing, download and upload before relying on remote backup. Keep this PC awake. Automatic Drivebound startup requires Windows sign-in."
    } finally { $lock.Dispose() }
}

if (-not $Script:RemoteLibraryOnly) {
    try { Enable-DriveboundRemoteAccess }
    catch { Show-DriveboundMessage -Text $_.Exception.Message -Kind Error; exit 1 }
}
