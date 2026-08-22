param(
    [ValidateSet("Setup", "Start", "Stop", "Status")]
    [string]$Action = "Setup",
    [switch]$NoStart,
    [switch]$NoBrowser,
    [switch]$NonInteractive,
    [switch]$RefreshPlaceholderSecrets,
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
        [Windows.Forms.MessageBoxIcon]::Question
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

function Write-EnvironmentFile {
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

    $temporary = "$($Script:EnvironmentPath).$PID.tmp"
    $content = ($output -join [Environment]::NewLine) + [Environment]::NewLine
    [IO.File]::WriteAllText($temporary, $content, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Script:EnvironmentPath -Force
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

    $firstPath = (Resolve-DriveboundPath $First) + [IO.Path]::DirectorySeparatorChar
    $secondPath = (Resolve-DriveboundPath $Second) + [IO.Path]::DirectorySeparatorChar
    return $firstPath.StartsWith($secondPath, [StringComparison]::OrdinalIgnoreCase) -or
        $secondPath.StartsWith($firstPath, [StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-ComposePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $normalized = (Resolve-DriveboundPath $Path).Replace("\", "/")
    return $normalized.Replace("'", "''")
}

function Write-UserComposeFile {
    param(
        [Parameter(Mandatory = $true)][string]$ExistingPhotos,
        [Parameter(Mandatory = $true)][string]$ManagedData,
        [Parameter(Mandatory = $true)][string]$ProtectionData
    )

    $originals = Join-Path $ManagedData "originals"
    $derivatives = Join-Path $ManagedData "derivatives"
    $staging = Join-Path $ManagedData "staging"
    $replicas = Join-Path $ProtectionData "replicas"
    $backups = Join-Path $ProtectionData "backups"
    $folders = @($ExistingPhotos, $originals, $derivatives, $staging, $replicas, $backups)
    foreach ($folder in $folders) {
        [void](New-Item -ItemType Directory -Path $folder -Force)
    }
    [void](New-Item -ItemType Directory -Path (Join-Path $backups "account-deletion-suppressions") -Force)

    $importsVolume = "$(ConvertTo-ComposePath $ExistingPhotos):/data/imports:ro"
    $originalsVolume = "$(ConvertTo-ComposePath $originals):/data/originals"
    $derivativesVolume = "$(ConvertTo-ComposePath $derivatives):/data/derivatives"
    $stagingVolume = "$(ConvertTo-ComposePath $staging):/data/staging"
    $replicasVolume = "$(ConvertTo-ComposePath $replicas):/data/replicas"
    $backupsVolume = "$(ConvertTo-ComposePath $backups):/data/backups"
    $yaml = @"
# Generated by Drivebound Setup. Run the setup again to change these folders.
services:
  backend:
    volumes:
      - '$originalsVolume'
      - '$derivativesVolume'
      - '$stagingVolume'
      - '${replicasVolume}:ro'
      - '$backupsVolume'
      - '$importsVolume'
  worker:
    volumes:
      - '$originalsVolume'
      - '$derivativesVolume'
      - '$stagingVolume'
      - '$replicasVolume'
      - '$backupsVolume'
      - '$importsVolume'
  scheduler:
    volumes:
      - '$originalsVolume'
      - '$replicasVolume'
      - '$backupsVolume'
      - '$importsVolume'
"@
    $temporary = "$($Script:UserComposePath).$PID.tmp"
    [IO.File]::WriteAllText($temporary, $yaml, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Script:UserComposePath -Force
}

function Get-ComposeArguments {
    param([string[]]$AdditionalArguments)

    $arguments = @("compose", "-f", $Script:BaseComposePath)
    if (Test-Path -LiteralPath $Script:UserComposePath) {
        $arguments += @("-f", $Script:UserComposePath)
    }
    $arguments += $AdditionalArguments
    return ,$arguments
}

function Test-DockerReady {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return $false
    }
    & docker info --format "{{.ServerVersion}}" *> $null
    return $LASTEXITCODE -eq 0
}

function Wait-ForDocker {
    if (Test-DockerReady) {
        return
    }

    $candidates = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    if ($candidates.Count -eq 0) {
        throw "Docker Desktop is not installed. Install Docker Desktop, start it once, and then run Drivebound Setup again."
    }

    Write-Host "Starting Docker Desktop. This can take a minute..." -ForegroundColor Cyan
    Start-Process -FilePath $candidates[0] | Out-Null
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    do {
        Start-Sleep -Seconds 2
        if (Test-DockerReady) {
            return
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Docker Desktop did not finish starting. Open Docker Desktop, resolve the message shown there, and run setup again."
}

function Invoke-DriveboundCompose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $composeArguments = Get-ComposeArguments -AdditionalArguments $Arguments
    Push-Location -LiteralPath $Script:ProjectRoot
    try {
        & docker @composeArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Drivebound could not complete the Docker startup step."
        }
    }
    finally {
        Pop-Location
    }
}

function Wait-ForDrivebound {
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    do {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 3
            if ($health) {
                return
            }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Drivebound started, but its health check did not become ready. Open Docker Desktop to review the container status."
}

function Start-Drivebound {
    param([switch]$Build)

    Wait-ForDocker
    Write-Host "Starting Drivebound..." -ForegroundColor Cyan
    $arguments = @("up", "-d")
    if ($Build) {
        $arguments = @("up", "--build", "-d")
    }
    Invoke-DriveboundCompose -Arguments $arguments
    Wait-ForDrivebound
    Write-Host "Drivebound is ready at http://localhost:3000" -ForegroundColor Green
    if (-not $NoBrowser) {
        Start-Process "http://localhost:3000"
    }
}

function Initialize-Drivebound {
    if (-not (Test-Path -LiteralPath $Script:EnvironmentTemplatePath)) {
        throw "The setup template .env.example is missing."
    }
    if (-not (Test-Path -LiteralPath $Script:BaseComposePath)) {
        throw "docker-compose.yml is missing."
    }

    $environmentAlreadyExisted = Test-Path -LiteralPath $Script:EnvironmentPath
    if (-not $NonInteractive) {
        Show-DriveboundMessage -Text "Welcome to Drivebound. Setup will prepare your private storage, create encryption keys automatically, and start the application. Your keys are kept on this PC and are reused on future runs."
        if ($environmentAlreadyExisted) {
            $hasExistingData = Confirm-DriveboundChoice -Text "A Drivebound configuration already exists. Has this installation ever stored an account or managed photo? Choose Yes to preserve every existing key. Choose No only for a brand-new, unused configuration copied from the example file." -Default $true
            $RefreshPlaceholderSecrets = -not $hasExistingData
        }
    }

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
    if (-not (Test-Path -LiteralPath $ExistingPhotosPath)) {
        [void](New-Item -ItemType Directory -Path $ExistingPhotosPath -Force)
    }
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

    $templateValues = Read-EnvironmentMap -Path $Script:EnvironmentTemplatePath
    $existingValues = Read-EnvironmentMap -Path $Script:EnvironmentPath
    $values = @{}
    foreach ($name in $templateValues.Keys) { $values[$name] = $templateValues[$name] }
    foreach ($name in $existingValues.Keys) { $values[$name] = $existingValues[$name] }

    $canRefreshPlaceholders = -not $environmentAlreadyExisted -or $RefreshPlaceholderSecrets
    $databasePasswordWasGenerated = (-not $values["POSTGRES_PASSWORD"]) -or
        ($canRefreshPlaceholders -and (Test-PlaceholderSecret -Value $values["POSTGRES_PASSWORD"] -KnownPlaceholders @("change-me")))
    if ($databasePasswordWasGenerated) {
        $values["POSTGRES_PASSWORD"] = New-DriveboundSecret
    }
    if ($databasePasswordWasGenerated -or -not $values["DATABASE_URL"] -or $values["DATABASE_URL"].Contains("change-me@postgres")) {
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

    Write-EnvironmentFile -Values $values
    Write-UserComposeFile -ExistingPhotos $ExistingPhotosPath -ManagedData $ManagedDataPath -ProtectionData $ProtectionDataPath

    Write-Host "Secure configuration created. No encryption key needs to be copied or typed." -ForegroundColor Green
    if ($environmentAlreadyExisted -and -not $RefreshPlaceholderSecrets) {
        Write-Host "Existing passwords and encryption keys were preserved so previously stored data remains readable." -ForegroundColor Green
    }
    if ($ManagedDataPath -eq $ProtectionDataPath) {
        Write-Host "Protection copies currently use the same physical location as uploads. Add a second drive before relying on them for drive-failure recovery." -ForegroundColor Yellow
    }

    if (-not $NoStart) {
        Start-Drivebound -Build
        Show-DriveboundMessage -Text "Drivebound is ready. Create your account in the page that opened. During onboarding, the photo folder selected here is already available as /data/imports."
    }
    else {
        Show-DriveboundMessage -Text "Drivebound configuration is ready. Use Drivebound Start when you want to launch the application."
    }
}

try {
    switch ($Action) {
        "Setup" { Initialize-Drivebound }
        "Start" { Start-Drivebound }
        "Stop" {
            Wait-ForDocker
            Write-Host "Stopping Drivebound without deleting photos or accounts..." -ForegroundColor Cyan
            Invoke-DriveboundCompose -Arguments @("stop")
            Show-DriveboundMessage -Text "Drivebound is stopped. Your photos and account data were not deleted."
        }
        "Status" {
            Wait-ForDocker
            Invoke-DriveboundCompose -Arguments @("ps")
        }
    }
}
catch {
    $message = $_.Exception.Message
    Show-DriveboundMessage -Text $message -Kind "Error"
    exit 1
}

exit 0
