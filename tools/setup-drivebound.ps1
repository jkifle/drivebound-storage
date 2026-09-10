param(
    [ValidateSet("Setup", "Start", "Stop", "Status")]
    [string]$Action = "Setup",
    [switch]$NoStart,
    [switch]$NoBrowser,
    [switch]$NonInteractive,
    [switch]$RefreshPlaceholderSecrets,
    [switch]$LibraryOnly,
    [switch]$SignInStartup,
    [switch]$EnableSignInStartup,
    [string]$OwnerEmail = "",
    [string]$ExistingPhotosPath = "",
    [string]$ManagedDataPath = "",
    [string]$ProtectionDataPath = "",
    [string]$ProjectRootOverride = ""
)

$ErrorActionPreference = "Stop"
$Script:ProjectRoot = if ($ProjectRootOverride) {
    [IO.Path]::GetFullPath($ProjectRootOverride)
}
else {
    Split-Path -Parent $PSScriptRoot
}
$Script:EnvironmentPath = Join-Path $Script:ProjectRoot ".env"
$Script:EnvironmentTemplatePath = Join-Path $Script:ProjectRoot ".env.example"
$Script:BaseComposePath = Join-Path $Script:ProjectRoot "docker-compose.yml"
$Script:UserComposePath = Join-Path $Script:ProjectRoot "docker-compose.user.yml"
$Script:DevelopmentMediaKey = "RZAIpFM5CAciKTy4UABCTvvyD83LfeB9r_ZR4z0LnKQ="
. (Join-Path $PSScriptRoot 'setup-state.ps1')

function Show-DriveboundMessage {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [string]$Title = "Drivebound Setup",
        [ValidateSet("Info", "Warning", "Error")][string]$Kind = "Info"
    )

    Write-Host $Text
    if ($NonInteractive) {
        return
    }

    Add-Type -AssemblyName System.Windows.Forms
    $icon = switch ($Kind) {
        "Warning" { [Windows.Forms.MessageBoxIcon]::Warning }
        "Error" { [Windows.Forms.MessageBoxIcon]::Error }
        default { [Windows.Forms.MessageBoxIcon]::Information }
    }
    [void][Windows.Forms.MessageBox]::Show(
        $Text,
        $Title,
        [Windows.Forms.MessageBoxButtons]::OK,
        $icon
    )
}

function Confirm-DriveboundChoice {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [string]$Title = "Drivebound Setup",
        [bool]$Default = $true
    )

    if ($NonInteractive) {
        return $Default
    }

    Add-Type -AssemblyName System.Windows.Forms
    $result = [Windows.Forms.MessageBox]::Show(
        $Text,
        $Title,
        [Windows.Forms.MessageBoxButtons]::YesNo,
        [Windows.Forms.MessageBoxIcon]::Question,
        $(if ($Default) { [Windows.Forms.MessageBoxDefaultButton]::Button1 } else { [Windows.Forms.MessageBoxDefaultButton]::Button2 })
    )
    return $result -eq [Windows.Forms.DialogResult]::Yes
}

function Select-DriveboundFolder {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [string]$InitialPath = "",
        [bool]$MustExist = $false
    )

    if ($NonInteractive) {
        if (-not $InitialPath) {
            throw "A folder path is required for unattended setup."
        }
        return [IO.Path]::GetFullPath($InitialPath)
    }

    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object Windows.Forms.FolderBrowserDialog
    $dialog.Description = $Description
    $dialog.ShowNewFolderButton = -not $MustExist
    if ($InitialPath -and (Test-Path -LiteralPath $InitialPath)) {
        $dialog.SelectedPath = [IO.Path]::GetFullPath($InitialPath)
    }
    $result = $dialog.ShowDialog()
    if ($result -ne [Windows.Forms.DialogResult]::OK -or -not $dialog.SelectedPath) {
        throw "Setup was cancelled before a folder was selected."
    }
    return [IO.Path]::GetFullPath($dialog.SelectedPath)
}

function New-SecureRandomBytes {
    param([Parameter(Mandatory = $true)][int]$Count)

    $bytes = New-Object byte[] $Count
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return ,$bytes
}

function ConvertTo-Base64Url {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [switch]$KeepPadding
    )

    $value = [Convert]::ToBase64String($Bytes).Replace("+", "-").Replace("/", "_")
    if (-not $KeepPadding) {
        $value = $value.TrimEnd("=")
    }
    return $value
}

function New-DriveboundSecret {
    return ConvertTo-Base64Url -Bytes (New-SecureRandomBytes -Count 48)
}

function New-DriveboundMediaKey {
    # Fernet-compatible, URL-safe base64 for an exact 32-byte deployment key.
    return ConvertTo-Base64Url -Bytes (New-SecureRandomBytes -Count 32) -KeepPadding
}

function Read-EnvironmentMap {
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $values
    }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -le 0) {
            continue
        }
        $name = $line.Substring(0, $separator).Trim()
        $values[$name] = $line.Substring($separator + 1)
    }
    return $values
}

function Test-PlaceholderSecret {
    param(
        [AllowNull()][string]$Value,
        [string[]]$KnownPlaceholders = @()
    )

    if (-not $Value) {
        return $true
    }
    if ($Value.StartsWith("change-") -or $Value.StartsWith("replace-")) {
        return $true
    }
    return $KnownPlaceholders -contains $Value
}

function Get-EnvironmentContent {
    param([Parameter(Mandatory = $true)][hashtable]$Values)

    $templateLines = [IO.File]::ReadAllLines($Script:EnvironmentTemplatePath)
    $written = @{}
    $output = New-Object Collections.Generic.List[string]
    foreach ($line in $templateLines) {
        $separator = $line.IndexOf("=")
        $trimmed = $line.Trim()
        if ($separator -gt 0 -and -not $trimmed.StartsWith("#")) {
            $name = $line.Substring(0, $separator).Trim()
            if ($Values.ContainsKey($name)) {
                $output.Add("$name=$($Values[$name])")
                $written[$name] = $true
                continue
            }
        }
        $output.Add($line)
    }

    $additional = @($Values.Keys | Where-Object { -not $written.ContainsKey($_) } | Sort-Object)
    if ($additional.Count -gt 0) {
        $output.Add("")
        $output.Add("# Preserved custom settings")
        foreach ($name in $additional) {
            $output.Add("$name=$($Values[$name])")
        }
    }

    return ($output -join [Environment]::NewLine) + [Environment]::NewLine
}

function Resolve-DriveboundPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    $trimmedPath = $fullPath.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $trimmedRoot = $root.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    if ($trimmedPath.Equals($trimmedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        return $root
    }
    return $trimmedPath
}

function Test-PathOverlap {
    param(
        [Parameter(Mandatory = $true)][string]$First,
        [Parameter(Mandatory = $true)][string]$Second
    )

    $firstPath = (Resolve-DriveboundPath $First).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $secondPath = (Resolve-DriveboundPath $Second).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    return $firstPath.StartsWith($secondPath, [StringComparison]::OrdinalIgnoreCase) -or
        $secondPath.StartsWith($firstPath, [StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-ComposePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $normalized = (Resolve-DriveboundPath $Path).Replace("\", "/")
    return $normalized.Replace("'", "''").Replace('$', '$$')
}

function Get-UserComposeContent {
    param([Parameter(Mandatory = $true)]$State)
    $mounts = @{
        imports = $State.paths.imports
        originals = Join-Path $State.paths.managed 'originals'
        derivatives = Join-Path $State.paths.managed 'derivatives'
        staging = Join-Path $State.paths.managed 'staging'
        replicas = Join-Path $State.paths.protection 'replicas'
        backups = Join-Path $State.paths.protection 'backups'
    }
    $lines = New-Object Collections.Generic.List[string]
    $lines.Add('# Generated by Drivebound Setup. Missing host directories must never be recreated by Docker.')
    $lines.Add('services:')
    foreach ($service in @('backend', 'worker', 'scheduler')) {
        $lines.Add("  ${service}:")
        $lines.Add('    environment:')
        $lines.Add('      STORAGE_MANIFEST_PATH: /etc/drivebound/storage.json')
        $lines.Add('    volumes:')
        $binds = @()
        foreach ($role in @('imports', 'originals', 'derivatives', 'staging', 'replicas', 'backups')) {
            $readOnly = $role -eq 'imports' -or ($role -eq 'replicas' -and $service -eq 'backend') -or $service -eq 'scheduler'
            $binds += @{source = $mounts[$role]; target = "/data/$role"; read_only = $readOnly}
        }
        $binds += @{source = Join-Path $Script:ProjectRoot '.drivebound/storage.json'; target = '/etc/drivebound/storage.json'; read_only = $true}
        foreach ($group in @('imports', 'managed', 'protection')) {
            $binds += @{source = $State.volumes.$group.marker_directory; target = "/etc/drivebound/volumes/$group"; read_only = $true}
        }
        foreach ($bind in $binds) {
            $lines.Add('      - type: bind')
            $lines.Add("        source: '$(ConvertTo-ComposePath $bind.source)'")
            $lines.Add("        target: $($bind.target)")
            $lines.Add("        read_only: $($bind.read_only.ToString().ToLowerInvariant())")
            $lines.Add('        bind:')
            $lines.Add('          create_host_path: false')
        }
    }
    return ($lines -join [Environment]::NewLine) + [Environment]::NewLine
}

function Get-ComposeArguments {
    param([string[]]$AdditionalArguments)

    $state = Get-InstallationState
    $arguments = @('compose', '--project-directory', $Script:ProjectRoot, '--env-file', $Script:EnvironmentPath)
    if ($state) { $arguments += @('--project-name', $state.compose_project_name) }
    $arguments += @('-f', $Script:BaseComposePath)
    if (Test-Path -LiteralPath $Script:UserComposePath) {
        $arguments += @("-f", $Script:UserComposePath)
    }
    $remote = Join-Path $Script:ProjectRoot 'docker-compose.remote.yml'
    if (Test-Path -LiteralPath $remote) { $arguments += @('-f', $remote) }
    $arguments += $AdditionalArguments
    return ,$arguments
}

function Test-DockerReady {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        foreach ($directory in @((Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'), (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin'))) {
            if (Test-Path -LiteralPath (Join-Path $directory 'docker.exe')) { $env:PATH = "$directory;$env:PATH"; break }
        }
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return $false
    }
    $result = Invoke-DriveboundNative -FilePath (Get-Command docker).Source -Arguments @('info', '--format', '{{.ServerVersion}}') -TimeoutSeconds 10
    return $result.ExitCode -eq 0
}

function Wait-ForDocker {
    if (Test-DockerReady) {
        return
    }

    $candidates = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    if ($candidates.Count -eq 0) {
        throw "Docker Desktop is not installed. Install Docker Desktop, start it once, and then run Drivebound Setup again."
    }

    Write-Host "Starting Docker Desktop. This can take a minute..." -ForegroundColor Cyan
    Start-Process -FilePath $candidates[0] -WindowStyle Hidden | Out-Null
    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        Start-Sleep -Seconds 2
        if (Test-DockerReady) {
            return
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Docker Desktop did not finish starting. Open Docker Desktop and resolve its startup message. If virtualization is unavailable, enable it in PC firmware and enable Windows Virtual Machine Platform, then restart Windows. Retry with Drivebound Start."
}

function Invoke-DriveboundCompose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $composeArguments = Get-ComposeArguments -AdditionalArguments $Arguments
    Push-Location -LiteralPath $Script:ProjectRoot
    try {
        $result = Invoke-DriveboundNative -FilePath (Get-Command docker).Source -Arguments $composeArguments
        if ($result.ExitCode -ne 0) {
            throw "Drivebound could not complete the Docker startup step."
        }
    }
    finally {
        Pop-Location
    }
}

function Assert-DriveboundPorts {
    param([string]$ProjectName)
    $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    foreach ($port in @(3000, 8000)) {
        if (-not ($listeners | Where-Object { $_.Port -eq $port })) { continue }
        $owned = Invoke-DriveboundNative -FilePath (Get-Command docker).Source -Arguments @('ps', '--filter', "label=com.docker.compose.project=$ProjectName", '--filter', "publish=$port", '--format', '{{.ID}}') -TimeoutSeconds 10
        if ($owned.ExitCode -ne 0) { throw 'Docker port ownership could not be checked. Open Docker Desktop and retry.' }
        if (-not $owned.StdOut.Trim()) { throw "Port $port is already being used by another application. Close that application's local development server or ask the PC owner to free the port, then retry Drivebound Start. No services were stopped automatically." }
    }
}

function Wait-ForDrivebound {
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    do {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ready" -TimeoutSec 3
            $frontend = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:3000' -TimeoutSec 3
            if ($health.status -eq 'ready' -and $frontend.StatusCode -eq 200) {
                return
            }
        }
        catch { }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Drivebound is not ready yet. Check that all selected drives are connected and Docker Desktop shows backend, worker, scheduler, database, Redis, and frontend running. Use Drivebound Status for a redacted report, then retry Drivebound Start."
}

function Start-Drivebound {
    param([switch]$Build)
    $state = Get-InstallationState
    if (-not $state) { throw 'Run Drivebound Setup first so your storage identity and owner can be verified.' }
    if ($SignInStartup -and $state.desired_state -eq 'stopped') { return }
    Assert-InstallationStorage $state
    Wait-ForDocker
    Assert-DriveboundPorts $state.compose_project_name
    if (-not $state.database_adoption_allowed) {
        $existingVolume = Invoke-DriveboundNative -FilePath (Get-Command docker).Source -Arguments @('volume', 'ls', '--filter', "name=^$($state.compose_project_name)_postgres_data$", '--format', '{{.Name}}') -TimeoutSeconds 10
        if ($existingVolume.ExitCode -ne 0) { throw 'Docker database storage could not be checked. Retry when Docker is ready.' }
        if ($existingVolume.StdOut.Trim()) { throw 'A database already exists for this installation, but its original credentials were not present during Setup. Recover its original .env before starting; Drivebound will not attach new keys to existing accounts.' }
        $state | Add-Member -NotePropertyName database_adoption_allowed -NotePropertyValue $true -Force
        # Persist before launching: a crash during up can safely retry the same credentials.
        Invoke-ConfigurationTransaction -Contents @{'.drivebound/installation.json' = ($state | ConvertTo-Json -Depth 12)}
    }
    Write-Host "Starting Drivebound..." -ForegroundColor Cyan
    $arguments = @("up", "-d")
    if ($Build) {
        $arguments = @("up", "--build", "-d")
    }
    Invoke-DriveboundCompose -Arguments $arguments
    $state.desired_state = 'running'
    Invoke-ConfigurationTransaction -Contents @{'.drivebound/installation.json' = ($state | ConvertTo-Json -Depth 12)}
    Wait-ForDrivebound
    $values = Read-EnvironmentMap $Script:EnvironmentPath
    $address = if ($values['APP_URL']) { $values['APP_URL'].Trim('"', "'") } else { 'http://localhost:3000' }
    Write-Host "Drivebound is ready at $address" -ForegroundColor Green
    if (-not $NoBrowser) {
        Start-Process $address -WindowStyle Hidden
    }
}

function Initialize-Drivebound {
    if (-not (Test-Path -LiteralPath $Script:EnvironmentTemplatePath)) {
        throw "The setup template .env.example is missing."
    }
    if (-not (Test-Path -LiteralPath $Script:BaseComposePath)) {
        throw "docker-compose.yml is missing."
    }

    $state = Get-InstallationState
    $environmentAlreadyExisted = Test-Path -LiteralPath $Script:EnvironmentPath
    $incompletePath = Join-Path $Script:ProjectRoot '.drivebound/initialization-started.json'
    if (-not $state -and (Test-Path -LiteralPath $incompletePath)) {
        Complete-InitialConfiguration
        Initialize-Drivebound
        return
    }
    if (-not $environmentAlreadyExisted -and (Test-Path -LiteralPath $Script:UserComposePath)) { throw 'An existing mount configuration has lost its .env credentials. Restore the original configuration before Setup; new keys would make existing data unreadable.' }
    if (-not $environmentAlreadyExisted -and -not $state) {
        foreach ($relative in @('data/originals', 'data/backups', 'data/replicas')) {
            $folder = Join-Path $Script:ProjectRoot $relative
            if ((Test-Path -LiteralPath $folder) -and @(Get-ChildItem -LiteralPath $folder -Force | Where-Object { $_.Name -ne '.gitkeep' }).Count -gt 0) { throw 'Existing managed data was found without its credentials. Restore the original configuration before Setup.' }
        }
    }
    if ($state -and -not $environmentAlreadyExisted) { throw 'This installation is missing its .env credentials. Restore the configuration recovery set. New encryption keys would make existing photos unreadable.' }
    if ($state -and $RefreshPlaceholderSecrets) { throw 'This installation already has durable state. Its encryption keys cannot be regenerated by Setup.' }
    if (-not $NonInteractive) {
        Show-DriveboundMessage -Text "Welcome to Drivebound. Setup will prepare your private storage, create encryption keys automatically, and start the application. Your keys are kept on this PC and are reused on future runs."
        if ($environmentAlreadyExisted -and -not $state) {
            $hasExistingData = Confirm-DriveboundChoice -Text "A Drivebound configuration already exists. Has this installation ever stored an account or managed photo? Choose Yes to preserve every existing key. Choose No only for a brand-new, unused configuration copied from the example file." -Default $true
            $RefreshPlaceholderSecrets = -not $hasExistingData
        }
    }

    if ($state) {
        Assert-InstallationStorage $state
        foreach ($choice in @(@{requested = $ExistingPhotosPath; saved = $state.paths.imports}, @{requested = $ManagedDataPath; saved = $state.paths.managed}, @{requested = $ProtectionDataPath; saved = $state.paths.protection})) {
            if ($choice.requested -and (Resolve-DriveboundPath $choice.requested) -ne $choice.saved) { throw 'Storage relocation needs a verified migration. Setup preserves the approved folders; reconnect the original drives.' }
        }
        if ($OwnerEmail -and $OwnerEmail.ToLowerInvariant() -ne $state.owner_email) { throw 'Changing the installation owner requires administrative recovery; Setup cannot transfer folder access.' }
        $ExistingPhotosPath = $state.paths.imports
        $ManagedDataPath = $state.paths.managed
        $ProtectionDataPath = $state.paths.protection
        $OwnerEmail = $state.owner_email
    } elseif (Test-Path -LiteralPath $Script:UserComposePath) {
        # Recover folders written by the pre-manifest installer without silently resetting them.
        $legacy = [IO.File]::ReadAllText($Script:UserComposePath)
        $recovered = @{}
        foreach ($role in @('imports', 'originals', 'replicas')) {
            $pattern = "(?m)^\s+-\s+'(?<source>.+):/data/${role}(?::ro)?'\s*$"
            if ($legacy -match $pattern) { $recovered[$role] = $matches.source.Replace("''", "'").Replace('$$', '$') }
        }
        if ($recovered.Count -ne 3) { throw 'An existing custom mount configuration needs review before adoption. Setup will not overwrite or detach it.' }
        $adopted = @{imports = $recovered.imports; managed = Split-Path -Parent $recovered.originals; protection = Split-Path -Parent $recovered.replicas}
        foreach ($choice in @(@{requested = $ExistingPhotosPath; saved = $adopted.imports}, @{requested = $ManagedDataPath; saved = $adopted.managed}, @{requested = $ProtectionDataPath; saved = $adopted.protection})) {
            if ($choice.requested -and (Resolve-DriveboundPath $choice.requested) -ne (Resolve-DriveboundPath $choice.saved)) { throw 'The selected folders differ from the existing configuration. Reconnect its original folders before adoption.' }
        }
        $ExistingPhotosPath = $adopted.imports; $ManagedDataPath = $adopted.managed; $ProtectionDataPath = $adopted.protection
        foreach ($path in @($ExistingPhotosPath, $ManagedDataPath, $ProtectionDataPath)) { if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw 'An existing configured storage folder is unavailable. Reconnect the original disk before adoption.' } }
    }

    if (-not $OwnerEmail) {
        if ($NonInteractive) { throw 'An OwnerEmail is required to approve who may import the host photo folder.' }
        Add-Type -AssemblyName Microsoft.VisualBasic
        $OwnerEmail = [Microsoft.VisualBasic.Interaction]::InputBox('Email address of the person who owns this PC photo collection. Only that verified account may connect the selected photo folder.', 'Drivebound owner', '')
    }
    $OwnerEmail = $OwnerEmail.Trim().ToLowerInvariant()
    try { $parsedEmail = New-Object Net.Mail.MailAddress($OwnerEmail) } catch { throw 'Enter a valid owner email address. No configuration was changed.' }
    if ($parsedEmail.Address -ne $OwnerEmail) { throw 'Enter only the owner email address, without a display name.' }

    $defaultImports = Join-Path $Script:ProjectRoot "data\imports"
    $defaultManaged = Join-Path $Script:ProjectRoot "data"
    if (-not $ExistingPhotosPath) {
        $hasExistingPhotos = Confirm-DriveboundChoice -Text "Do you already have a photo folder that Drivebound should index without changing the originals?" -Default $false
        $ExistingPhotosPath = if ($hasExistingPhotos) {
            Select-DriveboundFolder -Description "Choose the folder containing your existing photos" -InitialPath $defaultImports -MustExist $true
        }
        else {
            $defaultImports
        }
    }
    if (-not $ManagedDataPath) {
        $useConnectedDrive = Confirm-DriveboundChoice -Text "Would you like new Drivebound uploads to be stored in a folder on a connected drive?" -Default $false
        $ManagedDataPath = if ($useConnectedDrive) {
            Select-DriveboundFolder -Description "Choose or create a folder for new Drivebound uploads" -InitialPath $defaultManaged
        }
        else {
            $defaultManaged
        }
    }
    if (-not $ProtectionDataPath) {
        $useSeparateProtection = Confirm-DriveboundChoice -Text "Do you have a second physical drive for protection copies and database backups?" -Default $false
        $ProtectionDataPath = if ($useSeparateProtection) {
            Select-DriveboundFolder -Description "Choose or create a folder on the second drive" -InitialPath $defaultManaged
        }
        else {
            $ManagedDataPath
        }
    }

    $ExistingPhotosPath = Resolve-DriveboundPath $ExistingPhotosPath
    $ManagedDataPath = Resolve-DriveboundPath $ManagedDataPath
    $ProtectionDataPath = Resolve-DriveboundPath $ProtectionDataPath
    foreach ($path in @($ExistingPhotosPath, $ManagedDataPath, $ProtectionDataPath)) { Assert-PlainLocalPath $path }
    if ($ExistingPhotosPath -eq [IO.Path]::GetPathRoot($ExistingPhotosPath)) { throw 'Choose a photo subfolder, not the entire drive. Drivebound keeps identity markers outside your original photo folder.' }
    $ownedFolders = @(
        (Join-Path $ManagedDataPath "originals"),
        (Join-Path $ManagedDataPath "derivatives"),
        (Join-Path $ManagedDataPath "staging"),
        (Join-Path $ProtectionDataPath "replicas"),
        (Join-Path $ProtectionDataPath "backups")
    )
    if ($ownedFolders | Where-Object { Test-PathOverlap -First $ExistingPhotosPath -Second $_ }) {
        throw "The existing-photo folder must be separate from Drivebound's upload, protection, and backup folders."
    }
    for ($i = 0; $i -lt $ownedFolders.Count; $i++) {
        for ($j = $i + 1; $j -lt $ownedFolders.Count; $j++) {
            if (Test-PathOverlap $ownedFolders[$i] $ownedFolders[$j]) { throw 'Upload, staging, protection, and backup folders must not overlap each other.' }
        }
    }
    if (-not $state -and (-not $environmentAlreadyExisted -or $RefreshPlaceholderSecrets)) {
        foreach ($folder in $ownedFolders) {
            if ((Test-Path -LiteralPath $folder) -and @(Get-ChildItem -LiteralPath $folder -Force | Where-Object { $_.Name -ne '.gitkeep' }).Count -gt 0) {
                throw 'The selected managed or protection folder contains existing Drivebound data. Restore its original configuration before Setup; new encryption keys will not be generated for it.'
            }
        }
    }

    $templateValues = Read-EnvironmentMap -Path $Script:EnvironmentTemplatePath
    $existingValues = Read-EnvironmentMap -Path $Script:EnvironmentPath
    if (@($existingValues.Keys | Where-Object { $_ -like '*_FILE' -and $existingValues[$_] }).Count -gt 0) { throw 'This installation uses file-backed secrets and needs its existing production deployment workflow. Guided Setup will not change its configuration.' }
    if ($environmentAlreadyExisted -and -not $RefreshPlaceholderSecrets) {
        foreach ($name in @('POSTGRES_PASSWORD', 'DATABASE_URL', 'JWT_SECRET', 'MFA_ENCRYPTION_SECRET', 'MEDIA_ENCRYPTION_MASTER_KEY')) {
            if (-not $existingValues[$name] -and -not $existingValues["${name}_FILE"]) { throw "An existing installation is missing $name. Restore its prior configuration; Setup will not generate replacement credentials." }
        }
    }
    $values = @{}
    foreach ($name in $templateValues.Keys) { $values[$name] = $templateValues[$name] }
    foreach ($name in $existingValues.Keys) { $values[$name] = $existingValues[$name] }

    $canRefreshPlaceholders = -not $environmentAlreadyExisted -or $RefreshPlaceholderSecrets
    $databasePasswordWasGenerated = (-not $values["POSTGRES_PASSWORD"]) -or
        ($canRefreshPlaceholders -and (Test-PlaceholderSecret -Value $values["POSTGRES_PASSWORD"] -KnownPlaceholders @("change-me")))
    if ($databasePasswordWasGenerated) {
        $values["POSTGRES_PASSWORD"] = New-DriveboundSecret
    }
    $defaultDatabaseUrl = -not $existingValues['DATABASE_URL'] -or $existingValues['DATABASE_URL'].Contains(':change-me@postgres:')
    if (($databasePasswordWasGenerated -and (-not $environmentAlreadyExisted -or $defaultDatabaseUrl)) -or (-not $environmentAlreadyExisted -and -not $values['DATABASE_URL'])) {
        $databaseName = if ($values["POSTGRES_DB"]) { $values["POSTGRES_DB"] } else { "photos" }
        $databaseUser = if ($values["POSTGRES_USER"]) { $values["POSTGRES_USER"] } else { "photos" }
        $values["DATABASE_URL"] = "postgresql+asyncpg://${databaseUser}:$($values['POSTGRES_PASSWORD'])@postgres:5432/${databaseName}"
    }
    if ((-not $values["JWT_SECRET"]) -or ($canRefreshPlaceholders -and (Test-PlaceholderSecret -Value $values["JWT_SECRET"]))) {
        $values["JWT_SECRET"] = New-DriveboundSecret
    }
    if ((-not $values["MFA_ENCRYPTION_SECRET"]) -or ($canRefreshPlaceholders -and (Test-PlaceholderSecret -Value $values["MFA_ENCRYPTION_SECRET"]))) {
        $values["MFA_ENCRYPTION_SECRET"] = New-DriveboundSecret
    }
    if ((-not $values["MEDIA_ENCRYPTION_MASTER_KEY"]) -or ($canRefreshPlaceholders -and (Test-PlaceholderSecret -Value $values["MEDIA_ENCRYPTION_MASTER_KEY"] -KnownPlaceholders @($Script:DevelopmentMediaKey)))) {
        $values["MEDIA_ENCRYPTION_MASTER_KEY"] = New-DriveboundMediaKey
    }
    if ((-not $values["METRICS_AUTH_TOKEN"]) -or ($canRefreshPlaceholders -and (Test-PlaceholderSecret -Value $values["METRICS_AUTH_TOKEN"]))) {
        $values["METRICS_AUTH_TOKEN"] = New-DriveboundSecret
    }

    if (-not $state) {
        $installationId = [Guid]::NewGuid().ToString()
        $paths = @{imports = $ExistingPhotosPath; managed = $ManagedDataPath; protection = $ProtectionDataPath}
        $volumes = @{}
        foreach ($group in @('imports', 'managed', 'protection')) {
            $identity = Get-HostVolumeIdentity $paths[$group]
            $markerParent = if ($group -eq 'imports') { Split-Path -Parent $paths[$group] } else { $paths[$group] }
            $identity.marker_directory = Join-Path $markerParent ".drivebound-identities\$installationId-$group"
            $identity.marker = New-DriveboundSecret
            if (Test-PathOverlap $ExistingPhotosPath $identity.marker_directory) { throw 'Identity storage would overlap the imported photo folder. Choose separate photo and managed storage folders.' }
            $volumes[$group] = $identity
        }
        $projectName = if ($existingValues['COMPOSE_PROJECT_NAME']) { $existingValues['COMPOSE_PROJECT_NAME'].Trim('"', "'") } else { ((Split-Path -Leaf $Script:ProjectRoot).ToLowerInvariant() -replace '[^a-z0-9_-]', '').TrimStart('-', '_') }
        if (-not $projectName) { throw 'The project folder name cannot establish a stable Docker project identity.' }
        $protectionStatus = Get-ProtectionIndependence $volumes
        $state = [pscustomobject]@{schema_version = 1; installation_id = $installationId; compose_project_name = $projectName; owner_email = $OwnerEmail; paths = $paths; volumes = $volumes; protection_status = $protectionStatus; desired_state = 'stopped'; sign_in_startup = $false; database_adoption_allowed = [bool]($environmentAlreadyExisted -and -not $RefreshPlaceholderSecrets)}
    }
    # Recompute older saved labels so a rerun also corrects installations that
    # compared only managed uploads and protection while ignoring imports.
    $state.protection_status = Get-ProtectionIndependence $state.volumes
    $bindings = @()
    foreach ($role in @('imports', 'originals', 'derivatives', 'staging', 'replicas', 'backups')) {
        $group = if ($role -eq 'imports') { 'imports' } elseif ($role -in @('replicas', 'backups')) { 'protection' } else { 'managed' }
        $bindings += @{role = $role; path = "/data/$role"; marker_path = "/etc/drivebound/volumes/$group/.drivebound-volume"; marker = $state.volumes.$group.marker}
    }
    $storage = @{version = 1; installation_id = $state.installation_id; owner_email = $state.owner_email; import_display_name = (Split-Path -Leaf $state.paths.imports); bindings = $bindings}
    $values['STORAGE_MANIFEST_PATH'] = '/etc/drivebound/storage.json'
    $values['COMPOSE_PROJECT_NAME'] = $state.compose_project_name
    $contents = @{
        '.env' = Get-EnvironmentContent $values
        'docker-compose.user.yml' = Get-UserComposeContent $state
        '.drivebound/installation.json' = $state | ConvertTo-Json -Depth 12
        '.drivebound/storage.json' = $storage | ConvertTo-Json -Depth 8
    }
    if (-not (Get-InstallationState)) {
        $previousEnvironment = if ($environmentAlreadyExisted) { [IO.File]::ReadAllText($Script:EnvironmentPath) } else { $null }
        Write-DurableText $incompletePath (@{version = 1; contents = $contents; previous_environment = $previousEnvironment} | ConvertTo-Json -Depth 14)
        Complete-InitialConfiguration
    } else { Invoke-ConfigurationTransaction -Contents $contents }
    if (Test-Path -LiteralPath $incompletePath) { Remove-Item -LiteralPath $incompletePath -Force }

    Write-Host "Secure configuration created. No encryption key needs to be copied or typed." -ForegroundColor Green
    if ($environmentAlreadyExisted -and -not $RefreshPlaceholderSecrets) {
        Write-Host "Existing passwords and encryption keys were preserved so previously stored data remains readable." -ForegroundColor Green
    }
    if ($state.protection_status -ne 'independent_disk') {
        Show-DriveboundMessage -Text "Independent protection disk: $($state.protection_status). Different folders or partitions do not prove a second physical disk. Do not rely on these copies for physical drive-failure recovery until a separate disk is verified." -Kind Warning
    }

    if ($EnableSignInStartup -or (-not $NonInteractive -and -not $state.sign_in_startup -and (Confirm-DriveboundChoice 'Start Drivebound after you sign in to Windows? The PC must remain awake. This does not start Docker before Windows sign-in.' -Default $true))) {
        Register-DriveboundSignInStartup $state
    }

    if (-not $NoStart) {
        Start-Drivebound -Build
        Show-DriveboundMessage -Text "Drivebound is ready. Create and verify the account $OwnerEmail in the page that opened, then confirm your selected photo folder during onboarding."
    }
    else {
        Show-DriveboundMessage -Text "Drivebound configuration is ready. Use Drivebound Start when you want to launch the application."
    }
}

function Register-DriveboundSignInStartup {
    param($State)
    $scriptPath = Join-Path $PSScriptRoot 'setup-drivebound.ps1'
    $taskName = "Drivebound-$($State.installation_id)"
    $command = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Action Start -SignInStartup -NonInteractive -NoBrowser' -f $scriptPath
    $actionSpec = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $command -WorkingDirectory $Script:ProjectRoot
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $actionSpec -Trigger $trigger -Principal $principal -Settings $settings -Description 'Drivebound pilot startup after Windows sign-in. Explicit Stop stays stopped.' -Force | Out-Null
    $State.sign_in_startup = $true
    Invoke-ConfigurationTransaction -Contents @{'.drivebound/installation.json' = $State | ConvertTo-Json -Depth 12}
}

function Write-DriveboundStatus {
    $state = Get-InstallationState
    $storageStatus = 'not_configured'
    if ($state) { try { Assert-InstallationStorage $state; $storageStatus = 'available' } catch { $storageStatus = 'unavailable_or_identity_mismatch' } }
    $ready = $false
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/v1/ready' -TimeoutSec 3
        $frontend = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:3000' -TimeoutSec 3
        $ready = $health.status -eq 'ready' -and $frontend.StatusCode -eq 200
    } catch { }
    $report = @{version = 1; recorded_at = [DateTime]::UtcNow.ToString('o'); configured = [bool]$state; storage = $storageStatus; docker_ready = Test-DockerReady; application_ready = $ready; desired_state = $(if ($state) { $state.desired_state } else { 'not_configured' }); startup_scope = 'after_windows_sign_in_only'; protection = $(if ($state) { Get-ProtectionIndependence $state.volumes } else { 'unverified' })}
    $reportPath = Join-Path $Script:ProjectRoot '.drivebound/status-report.json'
    Write-DurableText $reportPath ($report | ConvertTo-Json -Depth 4)
    Show-DriveboundMessage -Text "Application ready: $ready. Storage: $storageStatus. A redacted report is saved in .drivebound/status-report.json. Reconnect approved drives or open Docker Desktop to resolve startup errors, then double-click Drivebound Start to retry. Windows sign-in is required for this pilot's automatic startup."
}

if ($LibraryOnly) { return }

$installationLock = $null
try {
    $installationLock = Enter-InstallationLock
    Recover-ConfigurationTransaction
    switch ($Action) {
        "Setup" { Initialize-Drivebound }
        "Start" { Start-Drivebound }
        "Stop" {
            $state = Get-InstallationState
            if ($state) {
                $state.desired_state = 'stopped'
                Invoke-ConfigurationTransaction -Contents @{'.drivebound/installation.json' = $state | ConvertTo-Json -Depth 12}
            }
            Wait-ForDocker
            Write-Host "Stopping Drivebound without deleting photos or accounts..." -ForegroundColor Cyan
            Invoke-DriveboundCompose -Arguments @("stop")
            Show-DriveboundMessage -Text "Drivebound is stopped. Your photos and account data were not deleted."
        }
        "Status" {
            Write-DriveboundStatus
        }
    }
}
catch {
    $message = $_.Exception.Message
    Show-DriveboundMessage -Text $message -Kind "Error"
    exit 1
}
finally {
    if ($installationLock) { $installationLock.Dispose() }
}

exit 0
